# -*- coding: utf-8 -*-
"""Job / Attempt 的 SQLite 持久化（**独立库** ``jobs.db``）。

为什么与 ``facts.db`` 分开
-------------------------
评估文档 §P1-5 明确点出本项目「三套任务语义并存」的问题。
本轮只做**收敛的第一步**：给出 Job/Attempt 两级语义并让它成为**新代码的唯一入口**，
而**不删除**既有的 ``tasks.db`` / ``comfyui_jobs.json`` / ``task_lease.py``。

物理分库是这个过渡期的正确选择：

* ``tasks.db`` 是既有链路（断点续跑 + 串行队列）的真源，本轮**不动**它，
  两个库并存 = 可随时对比、可随时回退，不会因为迁移出错把在跑的整集生产搞挂；
* 本库的写入频率与写放大都远高于事实库（每次轮询都可能写进度），
  分开可避免 WAL 锁竞争把「查一镜用了哪一版」也拖慢；
* 万一 Job/Attempt 语义后续再调整，删一个库即可，不影响已经落库的生产事实。

与既有 ``task_store.py`` 的**互补**关系（本模块的注释里写清，避免后来人误以为重复）::

    task_store.units   →  单元级断点续跑判据（"渲到哪了"）
    jobs_repo.attempts →  谁下的令、试了几次、为什么失败、产出了哪些版本

乐观锁
------
``jobs`` / ``attempts`` 都有 ``row_version``；状态迁移一律
``UPDATE ... WHERE id=? AND row_version=?``，``rowcount==0`` ⇒ 并发已被别人推进，
直接抛 :class:`ConflictError`，**绝不**「后写覆盖先写」。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    from domain.jobs import (
        ATTEMPT_TRANSITIONS, Attempt, JOB_TRANSITIONS, Job, JobError, can_transition,
        job_progress, next_attempt_no, reuse_candidate,
    )
    from domain.production_facts import ConflictError
except ImportError:                    # pragma: no cover - 以脚本方式导入时的兜底
    from app.domain.jobs import (  # type: ignore
        ATTEMPT_TRANSITIONS, Attempt, JOB_TRANSITIONS, Job, JobError, can_transition,
        job_progress, next_attempt_no, reuse_candidate,
    )
    from app.domain.production_facts import ConflictError  # type: ignore

logger = logging.getLogger(__name__)

__all__ = ["JobsRepo", "db_path", "SCHEMA_VERSION", "TABLES", "ConflictError"]

SCHEMA_VERSION = 1
TABLES: Tuple[str, ...] = ("jobs", "attempts")

_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    module      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    applied_at  TEXT NOT NULL,
    PRIMARY KEY (module)
);
"""

_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'queued',
    project      TEXT NOT NULL DEFAULT '',
    episode      TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    params       TEXT NOT NULL DEFAULT '{}',
    params_hash  TEXT NOT NULL DEFAULT '',
    intent_ref   TEXT NOT NULL DEFAULT '',
    last_error   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL,
    row_version  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project, episode);
CREATE INDEX IF NOT EXISTS idx_jobs_intent ON jobs(intent_ref);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id      TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL,
    attempt_no      INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    workflow_hash   TEXT NOT NULL DEFAULT '',
    intent_id       TEXT NOT NULL DEFAULT '',
    media_version_ids TEXT NOT NULL DEFAULT '[]',
    units           TEXT NOT NULL DEFAULT '{}',
    started_at      TEXT NOT NULL DEFAULT '',
    finished_at     TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    duration_sec    REAL NOT NULL DEFAULT 0,
    row_version     INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempt_no ON attempts(job_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_attempt_wf ON attempts(workflow_hash);
CREATE INDEX IF NOT EXISTS idx_attempt_intent ON attempts(intent_id);
"""


def db_path() -> str:
    """``<PROJECT_OUTPUT_DIR>/jobs.db``（与 tasks.db 并存，见模块头说明）。"""
    try:
        from config import PROJECT_OUTPUT_DIR
    except ImportError:                     # pragma: no cover
        PROJECT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "output")
    return os.path.join(PROJECT_OUTPUT_DIR, "jobs.db")


class JobsRepo:
    """Job / Attempt 仓库（写路径全部带乐观锁）。"""

    def __init__(self, path: str = ""):
        self.db_path = path or db_path()
        self._lock = threading.Lock()
        self._ensure_schema()

    # ---------- 连接 / 迁移 ----------

    def _conn(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception as e:              # noqa: BLE001
            logger.debug("jobs 库开启 WAL 失败（忽略）：%s", e)
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(_BASE_SCHEMA)
                row = conn.execute("SELECT version FROM schema_version WHERE module=?",
                                   ("jobs_repo",)).fetchone()
                cur = int(row["version"]) if row else 0
                if cur < SCHEMA_VERSION:
                    conn.executescript(_V1_SCHEMA)
                    conn.execute(
                        "INSERT INTO schema_version (module, version, applied_at) VALUES (?,?,?) "
                        "ON CONFLICT(module) DO UPDATE SET version=excluded.version, "
                        "applied_at=excluded.applied_at",
                        ("jobs_repo", SCHEMA_VERSION,
                         datetime.now().isoformat(timespec="seconds")))
                    logger.info("jobs 库已应用迁移 v%d", SCHEMA_VERSION)
                conn.commit()
            finally:
                conn.close()

    def schema_version(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute("SELECT version FROM schema_version WHERE module=?",
                               ("jobs_repo",)).fetchone()
            return int(row["version"]) if row else 0
        finally:
            conn.close()

    # ---------- Job ----------

    def save_job(self, job: Job) -> str:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO jobs (job_id, kind, status, project, episode, title, params, "
                    "params_hash, intent_ref, last_error, created_at, created_by, updated_at, "
                    "row_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (job.job_id, job.kind, job.status, job.project, job.episode, job.title,
                     json.dumps(dict(job.params or {}), ensure_ascii=False), job.params_hash,
                     job.intent_ref, job.last_error, job.created_at, job.created_by,
                     job.updated_at or job.created_at))
                conn.commit()
            finally:
                conn.close()
        return job.job_id

    def get_job(self, job_id: str) -> Optional[Job]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (str(job_id),)).fetchone()
            return Job.from_dict(self._job_row(row)) if row else None
        finally:
            conn.close()

    def list_jobs(self, *, project: str = "", episode: str = "", status: str = "",
                  kind: str = "", limit: int = 100) -> List[Job]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        args: List[Any] = []
        for col, val in (("project", project), ("episode", episode),
                         ("status", status), ("kind", kind)):
            if val:
                sql += " AND %s=?" % col
                args.append(str(val))
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        conn = self._conn()
        try:
            return [Job.from_dict(self._job_row(r)) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def set_job_status(self, job_id: str, target: str, *, expected_row_version: int = 0,
                       error: str = "") -> Dict[str, Any]:
        """带乐观锁的状态迁移。

        ``expected_row_version=0`` 表示"调用方不关心版本"（仍然做状态机校验，
        但不做版本比对）。**生产路径应当显式传入读到的版本号** ——
        否则两个线程同时推进同一 Job 时，后一个会静默覆盖前一个的终态。
        """
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT status, row_version FROM jobs WHERE job_id=?",
                                   (str(job_id),)).fetchone()
                if row is None:
                    raise JobError("Job 不存在：%s" % job_id)
                cur_status = str(row["status"])
                if not can_transition(cur_status, target, table=JOB_TRANSITIONS):
                    raise JobError("非法 Job 状态迁移：%s → %s（合法目标 %s）"
                                   % (cur_status, target,
                                      list(JOB_TRANSITIONS.get(cur_status, ()))))
                sets = ["status=?", "updated_at=?", "row_version=row_version+1"]
                vals: List[Any] = [str(target), datetime.now().isoformat(timespec="seconds")]
                if error:
                    sets.append("last_error=?")
                    vals.append(str(error)[:2000])
                vals.append(str(job_id))
                where = " AND row_version=?" if int(expected_row_version or 0) > 0 else ""
                if where:
                    vals.append(int(expected_row_version))
                cur = conn.execute(
                    "UPDATE jobs SET %s WHERE job_id=?%s" % (", ".join(sets), where), vals)
                if not cur.rowcount:
                    conn.rollback()
                    raise ConflictError(
                        "Job %s 状态迁移冲突：期望 row_version=%s，当前 %s（另一个执行分支已推进）"
                        % (job_id, expected_row_version, row["row_version"]))
                conn.commit()
                new_row = conn.execute("SELECT row_version FROM jobs WHERE job_id=?",
                                       (str(job_id),)).fetchone()
            finally:
                conn.close()
        return {"job_id": job_id, "status": target,
                "row_version": int(new_row["row_version"]) if new_row else 0}

    # ---------- Attempt ----------

    def save_attempt(self, att: Attempt) -> str:
        """写入一次执行尝试。

        并发保护：``UNIQUE (job_id, attempt_no)``。两个线程同时算出同一个
        ``attempt_no`` 时，后者拿 IntegrityError → 转为 :class:`ConflictError`，
        调用方应重新读 ``next_attempt_no`` 后重试。
        """
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO attempts (attempt_id, job_id, attempt_no, status, "
                    "workflow_hash, intent_id, media_version_ids, units, started_at, "
                    "finished_at, error, created_at, duration_sec, row_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (att.attempt_id, att.job_id, int(att.attempt_no), att.status,
                     att.workflow_hash, att.intent_id,
                     json.dumps(list(att.media_version_ids), ensure_ascii=False),
                     json.dumps(dict(att.units or {}), ensure_ascii=False),
                     att.started_at, att.finished_at, att.error, att.created_at,
                     float(att.duration_sec or 0.0)))
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise ConflictError("Attempt 序号冲突（job=%s no=%s）：%s"
                                    % (att.job_id, att.attempt_no, e)) from e
            finally:
                conn.close()
        return att.attempt_id

    def get_attempt(self, attempt_id: str) -> Optional[Attempt]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM attempts WHERE attempt_id=?",
                               (str(attempt_id),)).fetchone()
            return Attempt.from_dict(self._attempt_row(row)) if row else None
        finally:
            conn.close()

    def list_attempts(self, job_id: str) -> List[Attempt]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE job_id=? ORDER BY attempt_no ASC",
                (str(job_id),)).fetchall()
            return [Attempt.from_dict(self._attempt_row(r)) for r in rows]
        finally:
            conn.close()

    def suggest_attempt_no(self, job_id: str) -> int:
        """建议的下一个 attempt_no（并发撞号靠唯一索引兜住）。"""
        conn = self._conn()
        try:
            row = conn.execute("SELECT MAX(attempt_no) AS m FROM attempts WHERE job_id=?",
                               (str(job_id),)).fetchone()
            return int((row["m"] or 0) + 1) if row else 1
        finally:
            conn.close()

    def set_attempt_status(self, attempt_id: str, target: str, *,
                           expected_row_version: int = 0,
                           units: Optional[Mapping[str, Any]] = None,
                           media_version_ids: Optional[Sequence[str]] = None,
                           error: str = "") -> Dict[str, Any]:
        """带乐观锁的 Attempt 状态迁移（可同时写单元进度与产出版本）。"""
        sets = ["status=?", "row_version=row_version+1"]
        vals: List[Any] = [str(target)]
        if units is not None:
            sets.append("units=?")
            vals.append(json.dumps(dict(units), ensure_ascii=False))
        if media_version_ids is not None:
            sets.append("media_version_ids=?")
            vals.append(json.dumps(list(media_version_ids), ensure_ascii=False))
        if error:
            sets.append("error=?")
            vals.append(str(error)[:2000])
        if target in ("running",):
            sets.append("started_at=?")
            vals.append(datetime.now().isoformat(timespec="seconds"))
        if target in ("succeeded", "failed", "cancelled", "interrupted"):
            sets.append("finished_at=?")
            vals.append(datetime.now().isoformat(timespec="seconds"))

        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT status, row_version FROM attempts WHERE attempt_id=?",
                                   (str(attempt_id),)).fetchone()
                if row is None:
                    raise JobError("Attempt 不存在：%s" % attempt_id)
                cur_status = str(row["status"])
                if not can_transition(cur_status, target, table=ATTEMPT_TRANSITIONS):
                    raise JobError("非法 Attempt 状态迁移：%s → %s（合法目标 %s）"
                                   % (cur_status, target,
                                      list(ATTEMPT_TRANSITIONS.get(cur_status, ()))))
                vals.append(str(attempt_id))
                where = " AND row_version=?" if int(expected_row_version or 0) > 0 else ""
                if where:
                    vals.append(int(expected_row_version))
                cur = conn.execute(
                    "UPDATE attempts SET %s WHERE attempt_id=?%s" % (", ".join(sets), where), vals)
                if not cur.rowcount:
                    conn.rollback()
                    raise ConflictError(
                        "Attempt %s 状态迁移冲突：期望 row_version=%s，当前 %s"
                        % (attempt_id, expected_row_version, row["row_version"]))
                conn.commit()
                new_row = conn.execute("SELECT row_version FROM attempts WHERE attempt_id=?",
                                       (str(attempt_id),)).fetchone()
            finally:
                conn.close()
        return {"attempt_id": attempt_id, "status": target,
                "row_version": int(new_row["row_version"]) if new_row else 0}

    # ---------- 聚合查询 ----------

    def job_detail(self, job_id: str) -> Dict[str, Any]:
        """Job + 全部 Attempt + 进度聚合（界面一次拉完）。"""
        job = self.get_job(job_id)
        if job is None:
            return {}
        atts = [a.to_dict() for a in self.list_attempts(job_id)]
        # 免重渲判据：拿最近一次尝试的 workflow_hash 去历史里找可复用项
        wf = str((atts[-1] or {}).get("workflow_hash") or "") if atts else ""
        return {"job": job.to_dict(), "attempts": atts,
                "progress": job_progress(atts),
                "reuse_hint": reuse_candidate(atts, wf)}

    def recycle_interrupted(self) -> int:
        """把残留的 running 状态标记为 interrupted（启动时调用）。

        语义与 ``task_store.recycle_interrupted`` 一致：进程重启后，
        「正在跑」的那些 Attempt 实际上已经没人管了。**不自动重排**
        —— 重排由调用方决定（用户可能只想知道"昨晚断在第 9 镜"）。
        """
        n = 0
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE attempts SET status='interrupted', finished_at=?, "
                    "error=COALESCE(NULLIF(error,''), ?), row_version=row_version+1 "
                    "WHERE status='running'",
                    (datetime.now().isoformat(timespec="seconds"),
                     "进程重启导致执行中断，可基于已完成单元续跑"))
                n += cur.rowcount or 0
                cur = conn.execute(
                    "UPDATE jobs SET status='interrupted', updated_at=?, "
                    "row_version=row_version+1 WHERE status='running'",
                    (datetime.now().isoformat(timespec="seconds"),))
                n += cur.rowcount or 0
                conn.commit()
            finally:
                conn.close()
        if n:
            logger.warning("检测到 %d 条中断的执行记录（进程重启），已标记为 interrupted", n)
        return n

    # ---------- 辅助 ----------

    @staticmethod
    def _job_row(row: Any) -> Dict[str, Any]:
        d = dict(row) if row is not None else {}
        if row is not None:
            try:
                d["params"] = json.loads(d.get("params") or "{}")
            except (TypeError, ValueError):
                d["params"] = {}
        return d

    @staticmethod
    def _attempt_row(row: Any) -> Dict[str, Any]:
        d = dict(row) if row is not None else {}
        if row is not None:
            for col, default in (("media_version_ids", []), ("units", {})):
                try:
                    d[col] = json.loads(d.get(col) or ("[]" if col == "media_version_ids" else "{}"))
                except (TypeError, ValueError):
                    d[col] = default
        return d
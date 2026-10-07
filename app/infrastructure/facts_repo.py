# -*- coding: utf-8 -*-
"""版本化生产事实的 SQLite 持久化（``facts.db``）。

迁移体系（无 Alembic，与 ``task_store.py`` / ``ai_credentials_db.py`` 同一做法）
--------------------------------------------------------------------------
* 全部 DDL 是**幂等**的 ``CREATE TABLE IF NOT EXISTS``；
* 用 ``schema_version`` 表记录每个 ``module`` 的已应用版本号，
  启动时逐个比对，缺哪个补哪个；
* **不引入 Alembic**：本项目是单文件 SQLite + 桌面分发，引入迁移框架
  只会带来一层部署负担，而表结构本身改动频率低。

并发：乐观锁
------------
本库的写入方不止一个（Flask 请求线程、任务消费线程、可能还有 CLI 脚本）。
所以：

* **单行状态变更**走 ``UPDATE ... WHERE row_version=?``，
  ``rowcount==0`` 即判定冲突并抛 :class:`ConflictError`；
* **不可变事实**（intent / media / revision）压根不提供 UPDATE 入口 ——
  要改就 INSERT 新行（铁律 2 / 铁律 4）；
* ``(project, episode, revision_no)`` 与 ``(subject_id, id)`` 唯一索引兜住
  并发重复编号。

WAL + 每方法独立短连接，与 ``TaskStore._conn`` 完全一致（跨线程不复用连接）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from domain.production_facts import (
        ApprovalDecision, CapabilityProfileVersion, ConflictError, DomainError,
        GenerationIntent, MediaVersion, SelectionDecision, approval_is_valid,
        assert_intent_unfrozen, decision_state,
    )
except ImportError:                    # pragma: no cover - 以脚本方式导入时的兜底
    from app.domain.production_facts import (  # type: ignore
        ApprovalDecision, CapabilityProfileVersion, ConflictError, DomainError,
        GenerationIntent, MediaVersion, SelectionDecision, approval_is_valid,
        assert_intent_unfrozen, decision_state,
    )

logger = logging.getLogger(__name__)

__all__ = [
    "FactsRepo", "db_path", "SCHEMA_VERSION", "TABLES",
    "ConflictError",
]

#: 本模块的 schema 版本。**每次改表 +1**，并在 ``_MIGRATIONS`` 里登记一个函数。
SCHEMA_VERSION = 1

#: 本模块拥有的表（供 purge / 审计用）
TABLES: Tuple[str, ...] = ("generation_intents", "media_versions",
                           "selection_decisions", "approval_decisions",
                           "capability_profile_versions")

#: 所有迁移共用的基础表（幂等）
_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    module      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    applied_at  TEXT NOT NULL,
    PRIMARY KEY (module)
);
"""

#: v1：版本化生产事实全套表
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_intents (
    intent_id          TEXT PRIMARY KEY,
    project            TEXT NOT NULL DEFAULT '',
    episode            TEXT NOT NULL DEFAULT '',
    shot_key           TEXT NOT NULL DEFAULT '',
    kind               TEXT NOT NULL DEFAULT 'image',
    prompt             TEXT NOT NULL DEFAULT '',
    negative_prompt    TEXT NOT NULL DEFAULT '',
    ref_slots          TEXT NOT NULL DEFAULT '[]',
    seed               INTEGER NOT NULL DEFAULT -1,
    profile_id         TEXT NOT NULL DEFAULT '',
    profile_version    TEXT NOT NULL DEFAULT '',
    workflow_version   TEXT NOT NULL DEFAULT '',
    workflow_hash      TEXT NOT NULL DEFAULT '',
    intent_hash        TEXT NOT NULL,
    parent_intent_id   TEXT NOT NULL DEFAULT '',
    derivation_reason  TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    created_by         TEXT NOT NULL DEFAULT '',
    frozen             INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_intents_shot ON generation_intents(project, episode, shot_key);
CREATE INDEX IF NOT EXISTS idx_intents_hash ON generation_intents(intent_hash);
CREATE INDEX IF NOT EXISTS idx_intents_parent ON generation_intents(parent_intent_id);

CREATE TABLE IF NOT EXISTS media_versions (
    media_version_id   TEXT PRIMARY KEY,
    intent_id          TEXT NOT NULL,
    kind               TEXT NOT NULL DEFAULT 'image',
    media_sha256       TEXT NOT NULL,
    path               TEXT NOT NULL DEFAULT '',
    bytes              INTEGER NOT NULL DEFAULT 0,
    probe              TEXT NOT NULL DEFAULT '{}',
    attempt_id         TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_intent ON media_versions(intent_id);
CREATE INDEX IF NOT EXISTS idx_media_sha ON media_versions(media_sha256);

-- 采用（append-only）：同一 subject 允许多条，取最新一条为当前态
CREATE TABLE IF NOT EXISTS selection_decisions (
    selection_id       TEXT PRIMARY KEY,
    subject_type       TEXT NOT NULL DEFAULT 'media_version',
    subject_id         TEXT NOT NULL,
    media_version_id   TEXT NOT NULL,
    intent_hash        TEXT NOT NULL DEFAULT '',
    selection_hash     TEXT NOT NULL DEFAULT '',
    project            TEXT NOT NULL DEFAULT '',
    episode            TEXT NOT NULL DEFAULT '',
    shot_key           TEXT NOT NULL DEFAULT '',
    selected           INTEGER NOT NULL DEFAULT 1,
    revoked            INTEGER NOT NULL DEFAULT 0,
    reason             TEXT NOT NULL DEFAULT '',
    decided_by         TEXT NOT NULL DEFAULT '',
    decided_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sel_subject ON selection_decisions(subject_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_sel_shot ON selection_decisions(project, episode, shot_key);
CREATE INDEX IF NOT EXISTS idx_sel_media ON selection_decisions(media_version_id);

-- 批准（append-only + 撤销）：绝不从「已采用」推导，只能显式写入
CREATE TABLE IF NOT EXISTS approval_decisions (
    approval_id        TEXT PRIMARY KEY,
    selection_id       TEXT NOT NULL,
    subject_type       TEXT NOT NULL DEFAULT 'media_version',
    subject_id         TEXT NOT NULL,
    media_version_id   TEXT NOT NULL,
    bound_hash         TEXT NOT NULL,
    authorized_by      TEXT NOT NULL,
    authorization_ref  TEXT NOT NULL DEFAULT '',
    media_sha256       TEXT NOT NULL DEFAULT '',
    intent_hash        TEXT NOT NULL DEFAULT '',
    selection_hash     TEXT NOT NULL DEFAULT '',
    scope              TEXT NOT NULL DEFAULT 'media',
    approved           INTEGER NOT NULL DEFAULT 1,
    revoked            INTEGER NOT NULL DEFAULT 0,
    revoked_by         TEXT NOT NULL DEFAULT '',
    revoked_reason     TEXT NOT NULL DEFAULT '',
    note               TEXT NOT NULL DEFAULT '',
    decided_at         TEXT NOT NULL,
    superseded_by      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_appr_subject ON approval_decisions(subject_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_appr_sel ON approval_decisions(selection_id);
CREATE INDEX IF NOT EXISTS idx_appr_media ON approval_decisions(media_version_id);

CREATE TABLE IF NOT EXISTS capability_profile_versions (
    profile_id         TEXT NOT NULL,
    version            INTEGER NOT NULL,
    params             TEXT NOT NULL DEFAULT '{}',
    profile_hash       TEXT NOT NULL,
    deploy_profile     TEXT NOT NULL DEFAULT '',
    comfyui_workflow_version TEXT NOT NULL DEFAULT '',
    note               TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    created_by         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (profile_id, version)
);
CREATE INDEX IF NOT EXISTS idx_capver_hash ON capability_profile_versions(profile_hash);
"""

#: ``version`` → DDL 脚本。**只增不改**：已发布的版本函数永不修改，
#: 否则老库重新启动会走不同分支。
_MIGRATIONS: Dict[int, str] = {1: _V1_SCHEMA}


def db_path() -> str:
    """事实库路径：``<PROJECT_OUTPUT_DIR>/facts.db``。

    刻意放在 output 根而不是某个项目目录下：能力档案是**跨项目**的，
    intent / media 虽然属项目但需要按 project 索引查询，物理分库只会让
    「这一版能力档案当时是什么」变成跨库 join。
    惰性 import config，避免模块级循环导入（与 ``comfyui_job_store.db_path`` 同做法）。
    """
    try:
        from config import PROJECT_OUTPUT_DIR
    except ImportError:                     # pragma: no cover - 独立导入兜底
        PROJECT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "output")
    return os.path.join(PROJECT_OUTPUT_DIR, "facts.db")


class FactsRepo:
    """版本化生产事实仓库。

    线程安全约定与 ``TaskStore`` 一致：模块锁串行化「读改写」，
    各方法独立开短连接（sqlite3 连接不可跨线程共享）。
    """

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
            logger.debug("facts 库开启 WAL 失败（忽略，回落默认 journal）：%s", e)
        return conn

    def _ensure_schema(self) -> None:
        """幂等迁移：逐个补齐缺失的表结构，并登记 schema_version。

        幂等性来自两处：DDL 全部 ``IF NOT EXISTS``；已登记的版本**不重复执行**。
        """
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(_BASE_SCHEMA)
                applied = {r["module"]: int(r["version"]) for r in
                           conn.execute("SELECT module, version FROM schema_version")}
                cur = int(applied.get("facts_repo", 0))
                for ver in range(cur + 1, SCHEMA_VERSION + 1):
                    script = _MIGRATIONS.get(ver)
                    if script is None:
                        raise RuntimeError("facts_repo 缺少 v%d 的迁移脚本" % ver)
                    conn.executescript(script)
                    conn.execute(
                        "INSERT INTO schema_version (module, version, applied_at) VALUES (?,?,?) "
                        "ON CONFLICT(module) DO UPDATE SET version=excluded.version, "
                        "applied_at=excluded.applied_at",
                        ("facts_repo", ver, datetime.now().isoformat(timespec="seconds")))
                    logger.info("facts 库已应用迁移 v%d", ver)
                conn.commit()
            finally:
                conn.close()

    def schema_version(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute("SELECT version FROM schema_version WHERE module=?",
                               ("facts_repo",)).fetchone()
            return int(row["version"]) if row else 0
        finally:
            conn.close()

    # ---------- GenerationIntent ----------

    def save_intent(self, intent: GenerationIntent) -> str:
        """写入一条**冻结**的 intent。

        写前强制 ``assert_intent_unfrozen``（铁律 2）：
        内容与哈希不一致一律拒绝落库 —— 宁可没有这条事实，也不能有一条不可信的。
        本方法**没有 update 入口**：要改就是 :func:`derive_intent` 后新增一行。
        """
        assert_intent_unfrozen(intent)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO generation_intents (intent_id, project, episode, shot_key, "
                    "kind, prompt, negative_prompt, ref_slots, seed, profile_id, profile_version, "
                    "workflow_version, workflow_hash, intent_hash, parent_intent_id, "
                    "derivation_reason, created_at, created_by, frozen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (intent.intent_id, intent.project, intent.episode, intent.shot_key,
                     intent.kind, intent.prompt, intent.negative_prompt,
                     json.dumps([s.to_dict() for s in intent.ref_slots], ensure_ascii=False),
                     int(intent.seed), intent.profile_id, intent.profile_version,
                     intent.workflow_version, intent.workflow_hash, intent.intent_hash,
                     intent.parent_intent_id, intent.derivation_reason,
                     intent.created_at, intent.created_by, 1))
                conn.commit()
            finally:
                conn.close()
        return intent.intent_id

    def get_intent(self, intent_id: str) -> Optional[GenerationIntent]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM generation_intents WHERE intent_id=?",
                               (str(intent_id),)).fetchone()
            return GenerationIntent.from_dict(self._row(row)) if row else None
        finally:
            conn.close()

    def find_intents_by_hash(self, intent_hash: str,
                             limit: int = 20) -> List[GenerationIntent]:
        """同参数的历史 intent（识别「这是同一次意图的第 N 次执行」）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM generation_intents WHERE intent_hash=? "
                "ORDER BY created_at DESC LIMIT ?", (str(intent_hash), int(limit))).fetchall()
            return [GenerationIntent.from_dict(self._row(r)) for r in rows]
        finally:
            conn.close()

    def list_intents(self, *, project: str = "", episode: str = "",
                     shot_key: str = "", limit: int = 100) -> List[GenerationIntent]:
        sql = "SELECT * FROM generation_intents WHERE 1=1"
        args: List[Any] = []
        for col, val in (("project", project), ("episode", episode), ("shot_key", shot_key)):
            if val:
                sql += " AND %s=?" % col
                args.append(str(val))
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        conn = self._conn()
        try:
            return [GenerationIntent.from_dict(self._row(r))
                    for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def intent_lineage(self, intent_id: str) -> List[GenerationIntent]:
        """派生血缘链（自当前 intent 往上回溯到根）。"""
        chain: List[GenerationIntent] = []
        cur = self.get_intent(intent_id)
        seen: set = set()
        while cur is not None and cur.intent_id not in seen:
            seen.add(cur.intent_id)
            chain.append(cur)
            cur = self.get_intent(cur.parent_intent_id) if cur.parent_intent_id else None
        return chain

    # ---------- MediaVersion ----------

    def save_media_version(self, mv: MediaVersion) -> str:
        if not mv.media_sha256:
            raise DomainError("MediaVersion 缺少 media_sha256，拒绝落库：%s" % mv.media_version_id)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO media_versions (media_version_id, intent_id, kind, "
                    "media_sha256, path, bytes, probe, attempt_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (mv.media_version_id, mv.intent_id, mv.kind, mv.media_sha256,
                     mv.path, int(mv.bytes or 0),
                     json.dumps(dict(mv.probe or {}), ensure_ascii=False),
                     mv.attempt_id, mv.created_at))
                conn.commit()
            finally:
                conn.close()
        return mv.media_version_id

    def get_media_version(self, media_version_id: str) -> Optional[MediaVersion]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM media_versions WHERE media_version_id=?",
                               (str(media_version_id),)).fetchone()
            return MediaVersion.from_dict(self._row(row)) if row else None
        finally:
            conn.close()

    def list_media_versions(self, intent_id: str = "", limit: int = 200) -> List[MediaVersion]:
        conn = self._conn()
        try:
            if intent_id:
                rows = conn.execute(
                    "SELECT * FROM media_versions WHERE intent_id=? "
                    "ORDER BY created_at DESC LIMIT ?", (str(intent_id), int(limit))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM media_versions ORDER BY created_at DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [MediaVersion.from_dict(self._row(r)) for r in rows]
        finally:
            conn.close()

    def media_index(self, media_version_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """批量取候选版本（供时间线预检用）。返回 ``{id: {**版本, approved: bool}}``。"""
        ids = [str(x) for x in (media_version_ids or ()) if x]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM media_versions WHERE media_version_id IN (%s)" % marks,
                ids).fetchall()
        finally:
            conn.close()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            d = self._row(r)
            mv = MediaVersion.from_dict(d)
            appr = self.get_approval_for(mv.media_version_id)
            disk_sha, verified = self._disk_media_digest(mv)
            # 批准状态从独立批准表读，**绝不**从"是否被采用"推断（铁律 1）；
            # 且必须比**磁盘现状**（disk_sha），不能比库内登记值 mv.media_sha256 ——
            # 拿登记值和登记值比等于永远判「没变」，批准失效就永远不会触发。
            # 取不到现状（verified=False）时一律 approved=False（fail-closed）。
            d["approved"] = bool(verified and approval_is_valid(
                appr, media_sha256_now=disk_sha))
            d["media_sha256_now"] = disk_sha
            d["disk_verified"] = verified
            out[mv.media_version_id] = d
        return out

    def _disk_media_digest(self, mv: Optional[MediaVersion]) -> Tuple[str, bool]:
        """重算**磁盘现状**指纹，返回 ``(sha256, verified)``。

        ⚠️ 这里的口径是本模块最关键的一处：拿不到现状 = **不可判定**，
        **绝不等于**「没变」。以下情况一律 ``verified=False``：

        * 记录不存在 / ``path`` 为空（登记性条目，无磁盘副本可比）；
        * 文件已被删除（``os.path.isfile`` 为假）；
        * 文件读不出来（权限、占用、被换成目录，``OSError``）。

        原实现把 ``OSError`` 当成「没变」返回 True —— 而「文件不见了 / 打不开」
        恰恰是最严重的一种变更，等于把最危险的信号当成了安全信号。
        """
        if mv is None:
            return "", False
        path = str(mv.path or "").strip()
        if not path or not os.path.isfile(path):
            return "", False
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError as e:
            logger.warning("媒体指纹复算失败（标记为不可判定）：%s", e)
            return "", False
        return h.hexdigest(), True

    def assert_media_unchanged(self, media_version_id: str) -> bool:
        """重算磁盘产物指纹，与登记值比对。

        用于回答「id 没变但内容被覆盖了」（既有链路 ``ep01_full.mp4`` 原地覆盖）。
        指纹不一致时**必须重新批准**（铁律 3）。

        fail-closed：只有「磁盘现状算得出来且与登记值一致」才返回 True。
        无磁盘副本 / 文件缺失 / 不可读一律 False —— 「不可判定」不等于「没变」。
        """
        mv = self.get_media_version(media_version_id)
        if mv is None:
            raise DomainError("候选版本不存在，无法校验磁盘现状：%s" % media_version_id)
        disk_sha, verified = self._disk_media_digest(mv)
        if not verified:
            logger.warning("媒体无法按磁盘现状校验（视为未变更的**反面**）：%s", media_version_id)
            return False
        return disk_sha == mv.media_sha256

    # ---------- SelectionDecision ----------

    def save_selection(self, sel: SelectionDecision) -> str:
        if not str(sel.reason or "").strip():
            raise DomainError("采用必须写明 reason，拒绝落库：%s" % sel.selection_id)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO selection_decisions (selection_id, subject_type, subject_id, "
                    "media_version_id, intent_hash, selection_hash, project, episode, shot_key, "
                    "selected, revoked, reason, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sel.selection_id, sel.subject_type, sel.subject_id, sel.media_version_id,
                     sel.intent_hash, sel.selection_hash, sel.project, sel.episode,
                     sel.shot_key, 1 if sel.selected else 0, 1 if sel.revoked else 0,
                     sel.reason, sel.decided_by, sel.decided_at))
                conn.commit()
            finally:
                conn.close()
        return sel.selection_id

    def list_selections(self, subject_id: str = "") -> List[SelectionDecision]:
        conn = self._conn()
        try:
            if subject_id:
                rows = conn.execute(
                    "SELECT * FROM selection_decisions WHERE subject_id=? "
                    "ORDER BY decided_at DESC", (str(subject_id),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM selection_decisions ORDER BY decided_at DESC LIMIT 200").fetchall()
            return [SelectionDecision.from_dict(self._row(r)) for r in rows]
        finally:
            conn.close()

    def current_selection(self, subject_id: str) -> Optional[SelectionDecision]:
        """该 subject 当前生效的采用（取最新未撤销的一条）。

        找不到返回 ``None`` 而不是「退化成已批准」——
        「没采用」和「采用了没批准」是两个必须区分的状态（铁律 1）。
        """
        sels = [s for s in self.list_selections(subject_id)
                if s.selected and not s.revoked]
        if not sels:
            return None
        return max(sels, key=lambda s: (s.decided_at, s.selection_id))

    # ---------- ApprovalDecision ----------

    def save_approval(self, ap: ApprovalDecision) -> str:
        """落库一条批准。

        落库前在**应用层**（``production.py``）已用 ``build_approval`` 校验过
        人工授权与哈希绑定；这里再挡一次「空授权主体 / 空 bound_hash」，
        双保险的理由是：批准记录一旦落库就会被交付闸门信任，不能靠上游自觉。
        """
        if not str(ap.authorized_by or "").strip():
            raise DomainError("批准缺少授权主体，拒绝落库：%s" % ap.approval_id)
        if not str(ap.bound_hash or "").strip():
            raise DomainError("批准缺少哈希绑定，拒绝落库：%s" % ap.approval_id)
        if not str(ap.selection_id or "").strip():
            raise DomainError("批准必须引用一条采用（selection_id），拒绝落库：%s" % ap.approval_id)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO approval_decisions (approval_id, selection_id, subject_type, "
                    "subject_id, media_version_id, bound_hash, authorized_by, authorization_ref, "
                    "media_sha256, intent_hash, selection_hash, scope, approved, revoked, "
                    "revoked_by, revoked_reason, note, decided_at, superseded_by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ap.approval_id, ap.selection_id, ap.subject_type, ap.subject_id,
                     ap.media_version_id, ap.bound_hash, ap.authorized_by,
                     ap.authorization_ref, ap.media_sha256, ap.intent_hash,
                     ap.selection_hash, ap.scope, 1 if ap.approved else 0,
                     1 if ap.revoked else 0, ap.revoked_by, ap.revoked_reason,
                     ap.note, ap.decided_at, ap.superseded_by))
                conn.commit()
            finally:
                conn.close()
        return ap.approval_id

    def list_approvals(self, subject_id: str = "") -> List[ApprovalDecision]:
        conn = self._conn()
        try:
            if subject_id:
                rows = conn.execute(
                    "SELECT * FROM approval_decisions WHERE subject_id=? "
                    "ORDER BY decided_at DESC", (str(subject_id),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approval_decisions ORDER BY decided_at DESC LIMIT 200").fetchall()
            return [ApprovalDecision.from_dict(self._row(r)) for r in rows]
        finally:
            conn.close()

    def get_approval(self, approval_id: str) -> Optional[ApprovalDecision]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM approval_decisions WHERE approval_id=?",
                               (str(approval_id),)).fetchone()
            return ApprovalDecision.from_dict(self._row(row)) if row else None
        finally:
            conn.close()

    def get_approval_for(self, media_version_id: str) -> Optional[ApprovalDecision]:
        """某媒体版本当前生效的批准（取最新未撤销的一条；无则 None）。

        ⚠️ 必须按 ``media_version_id`` 列查，**不能**复用按 ``subject_id`` 查的
        :meth:`list_approvals`：``subject_id`` 是调用方自由填的（``subject_type``
        可以是 ``timeline_revision`` 等），两者语义不同。历史上这里把
        ``media_version_id`` 当 ``subject_id`` 传进去，于是
        ``select(subject=B) + approve(media=A, subject=B)`` 会让 B 读到自己那条
        批准而 A 读不到 —— 未批准的候选就能通过时间线的 ``require_approved`` 闸门。
        """
        key = str(media_version_id or "").strip()
        if not key:
            return None
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM approval_decisions WHERE media_version_id=? "
                    "ORDER BY decided_at DESC", (key,)).fetchall()
            finally:
                conn.close()
        aps = [ApprovalDecision.from_dict(self._row(r)) for r in rows
               if r["approved"] and not r["revoked"]]
        if not aps:
            return None
        return max(aps, key=lambda a: (a.decided_at or "", a.approval_id or ""))

    def revoke_approval(self, approval_id: str, *, revoked_by: str,
                        reason: str = "") -> Dict[str, Any]:
        """撤销批准（append-only：写一条撤销记录 + 标记原记录，不删历史）。

        撤销同样要求人工主体 —— 「机器把批准收回去」和「机器授予批准」一样荒唐。
        乐观锁：``WHERE revoked=0`` 保证并发撤销只有一次成功。
        """
        if not str(revoked_by or "").strip():
            raise DomainError("撤销批准必须写明撤销人")
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE approval_decisions SET revoked=1, revoked_by=?, revoked_reason=? "
                    "WHERE approval_id=? AND revoked=0",
                    (str(revoked_by), str(reason or ""), str(approval_id)))
                if not cur.rowcount:
                    conn.rollback()
                    raise ConflictError("批准 %s 不存在或已被撤销（乐观锁冲突）" % approval_id)
                conn.commit()
            finally:
                conn.close()
        return {"approval_id": approval_id, "revoked": True,
                "revoked_by": revoked_by, "reason": reason}

    def decision_state(self, subject_id: str) -> Dict[str, Any]:
        """汇总「采用 + 批准」三态（界面直接消费）。

        这是铁律 1 在读侧的落点：``selection_implies_approval`` 恒为 False，
        调用方拿它做 UI 断言即可。

        ``media_sha256_now`` 取**磁盘现状**的重算值（不是库内登记值），
        取不到时以 ``media_verified=False`` 上报，绝不按「没变」处理。
        """
        sel = self.current_selection(subject_id)
        media_id = sel.media_version_id if sel else (subject_id or "")
        mv = self.get_media_version(media_id) if media_id else None
        ap = self.get_approval_for(media_id) if media_id else None
        disk_sha, verified = self._disk_media_digest(mv)
        st = decision_state(sel, ap, media_sha256_now=disk_sha, media_verified=verified)
        st.update({"subject_id": subject_id, "selection": sel.to_dict() if sel else None,
                   "approval": ap.to_dict() if ap else None,
                   "media_version": mv.to_dict() if mv else None})
        return st

    # ---------- CapabilityProfileVersion ----------

    def save_capability_profile(self, prof: CapabilityProfileVersion) -> Tuple[str, int]:
        """登记能力档案的一个版本。

        并发保护：``PRIMARY KEY (profile_id, version)`` —— 两个分支同时登记
        v3 时后者会拿到 IntegrityError，调用方据此改用 v4，而不是静默覆盖。
        """
        if not prof.verify_hash():
            raise DomainError("能力档案内容与 profile_hash 不一致，拒绝落库：%s/v%s"
                              % (prof.profile_id, prof.version))
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO capability_profile_versions (profile_id, version, params, "
                    "profile_hash, deploy_profile, comfyui_workflow_version, note, "
                    "created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                    (prof.profile_id, int(prof.version),
                     json.dumps(dict(prof.params or {}), ensure_ascii=False),
                     prof.profile_hash, prof.deploy_profile, prof.comfyui_workflow_version,
                     prof.note, prof.created_at, prof.created_by))
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise ConflictError("能力档案 %s v%s 已存在（并发登记冲突）：%s"
                                    % (prof.profile_id, prof.version, e)) from e
            finally:
                conn.close()
        return prof.profile_id, int(prof.version)

    def get_capability_profile(self, profile_id: str,
                               version: int = 0) -> Optional[CapabilityProfileVersion]:
        """取指定版本；``version=0`` 表示取**最新**版本。"""
        conn = self._conn()
        try:
            if int(version or 0) > 0:
                row = conn.execute(
                    "SELECT * FROM capability_profile_versions WHERE profile_id=? AND version=?",
                    (str(profile_id), int(version))).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM capability_profile_versions WHERE profile_id=? "
                    "ORDER BY version DESC LIMIT 1", (str(profile_id),)).fetchone()
            return CapabilityProfileVersion.from_dict(self._row(row)) if row else None
        finally:
            conn.close()

    def list_capability_profiles(self, profile_id: str = "") -> List[CapabilityProfileVersion]:
        conn = self._conn()
        try:
            if profile_id:
                rows = conn.execute(
                    "SELECT * FROM capability_profile_versions WHERE profile_id=? "
                    "ORDER BY version DESC", (str(profile_id),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM capability_profile_versions "
                    "ORDER BY profile_id, version DESC").fetchall()
            return [CapabilityProfileVersion.from_dict(self._row(r)) for r in rows]
        finally:
            conn.close()

    # ---------- 辅助 ----------

    @staticmethod
    def _row(row: Any) -> Dict[str, Any]:
        """``sqlite3.Row`` → dict，并把 JSON 列解回来（损坏时降级为空，不抛）。"""
        if row is None:
            return {}
        d = dict(row)
        for col, default in (("ref_slots", []), ("probe", {}), ("params", {})):
            if col in d:
                try:
                    d[col] = json.loads(d.get(col) or ("[]" if col == "ref_slots" else "{}"))
                except (TypeError, ValueError):
                    d[col] = default
        for col in ("selected", "revoked", "approved", "frozen"):
            if col in d:
                d[col] = bool(d[col])
        return d
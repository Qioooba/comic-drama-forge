# -*- coding: utf-8 -*-
"""不可变时间线的 SQLite 持久化（与 ``facts.db`` **同库不同表**）。

为什么不另开一个库
------------------
时间线里的每一镜都指向 ``media_versions.media_version_id``，
批准记录也在 ``facts.db`` 里。分库会让「预检时确认这一镜已批准」变成跨库查询，
而这条查询恰好是铁律 1 最需要**响亮**的地方。合库 = 一条 SQL 就能把
「采用 / 批准 / 内容指纹」三件事同时看清。

不可变性怎么保证
----------------
1. 表里**没有** ``update_revision`` 方法 —— 代码层面就不提供改内容的入口；
2. ``revision_id`` 主键 + ``(project, episode, revision_no)`` 唯一索引：
   并发派生同号必有一个拿 IntegrityError（乐观锁的落点）；
3. 落库前校验 ``compose_fingerprint`` 自洽（防库被旁路改写）。

``timeline_revisions`` 的表结构与 ``schema_version`` 登记都在本模块内自包含，
与 ``facts_repo`` 各自负责自己的迁移脚本（``schema_version.module`` 区分）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from domain.production_facts import ConflictError, DomainError
    from domain.timeline import (
        EpisodeRenderVersion, TimelineItem, TimelineRevision, build_render_manifest, preflight,
    )
except ImportError:                    # pragma: no cover - 以脚本方式导入时的兜底
    from app.domain.production_facts import ConflictError, DomainError  # type: ignore
    from app.domain.timeline import (  # type: ignore
        EpisodeRenderVersion, TimelineItem, TimelineRevision, build_render_manifest, preflight,
    )

logger = logging.getLogger(__name__)

__all__ = ["TimelineRepo", "SCHEMA_VERSION", "TABLES", "ConflictError"]

SCHEMA_VERSION = 2
TABLES: Tuple[str, ...] = ("timeline_revisions", "episode_render_versions")

_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    module      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    applied_at  TEXT NOT NULL,
    PRIMARY KEY (module)
);
"""

_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline_revisions (
    revision_id         TEXT PRIMARY KEY,
    project             TEXT NOT NULL DEFAULT '',
    episode             TEXT NOT NULL DEFAULT '',
    revision_no         INTEGER NOT NULL,
    items               TEXT NOT NULL DEFAULT '[]',
    compose_fingerprint TEXT NOT NULL,
    parent_revision_id  TEXT NOT NULL DEFAULT '',
    subtitle_revision   TEXT NOT NULL DEFAULT '',
    declared_silences   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    created_by          TEXT NOT NULL DEFAULT '',
    note                TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_timeline_no
    ON timeline_revisions(project, episode, revision_no);
CREATE INDEX IF NOT EXISTS idx_timeline_parent
    ON timeline_revisions(parent_revision_id);
CREATE INDEX IF NOT EXISTS idx_timeline_fp
    ON timeline_revisions(compose_fingerprint);
"""

_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS episode_render_versions (
    render_id              TEXT PRIMARY KEY,
    revision_id            TEXT NOT NULL,
    project                TEXT NOT NULL DEFAULT '',
    episode                TEXT NOT NULL DEFAULT '',
    revision_no            INTEGER NOT NULL DEFAULT 0,
    compose_fingerprint    TEXT NOT NULL,
    plan_fingerprint       TEXT NOT NULL,
    output_path            TEXT NOT NULL DEFAULT '',
    output_sha256          TEXT NOT NULL DEFAULT '',
    output_bytes           INTEGER NOT NULL DEFAULT 0,
    output_duration_sec    REAL NOT NULL DEFAULT 0,
    subtitle_revision      TEXT NOT NULL DEFAULT '',
    entry_media_version_ids TEXT NOT NULL DEFAULT '[]',
    declared_silences      INTEGER NOT NULL DEFAULT 0,
    authorized_by          TEXT NOT NULL DEFAULT '',
    authorization_ref      TEXT NOT NULL DEFAULT '',
    bind_hash              TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL,
    note                   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_render_revision
    ON episode_render_versions(revision_id);
CREATE INDEX IF NOT EXISTS idx_render_fp
    ON episode_render_versions(compose_fingerprint);
"""


def db_path() -> str:
    """与 facts_repo 同库（见模块头「为什么不另开一个库」）。"""
    try:
        from infrastructure.facts_repo import db_path as _facts_db_path
    except ImportError:                 # pragma: no cover
        from app.infrastructure.facts_repo import db_path as _facts_db_path  # type: ignore
    return _facts_db_path()


class TimelineRepo:
    """TimelineRevision 仓库（append-only）。"""

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
        except Exception as e:          # noqa: BLE001
            logger.debug("时间线库开启 WAL 失败（忽略）：%s", e)
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(_BASE_SCHEMA)
                row = conn.execute("SELECT version FROM schema_version WHERE module=?",
                                   ("timeline_repo",)).fetchone()
                cur = int(row["version"]) if row else 0
                if cur < SCHEMA_VERSION:
                    conn.executescript(_V1_SCHEMA)
                    if cur < 2:
                        conn.executescript(_V2_SCHEMA)
                    conn.execute(
                        "INSERT INTO schema_version (module, version, applied_at) VALUES (?,?,?) "
                        "ON CONFLICT(module) DO UPDATE SET version=excluded.version, "
                        "applied_at=excluded.applied_at",
                        ("timeline_repo", SCHEMA_VERSION,
                         datetime.now().isoformat(timespec="seconds")))
                    logger.info("时间线库已应用迁移 v%d", SCHEMA_VERSION)
                conn.commit()
            finally:
                conn.close()

    def schema_version(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute("SELECT version FROM schema_version WHERE module=?",
                               ("timeline_repo",)).fetchone()
            return int(row["version"]) if row else 0
        finally:
            conn.close()

    # ---------- 写入（append-only，无 update） ----------

    def save_revision(self, rev: TimelineRevision) -> str:
        """写入一版冻结的剪辑。

        两道校验：
        * ``compose_fingerprint`` 必须与内容自洽（否则说明有人旁路改了 items）；
        * ``(project, episode, revision_no)`` 唯一 —— 并发派生同号时后者
          拿到 :class:`ConflictError`，由编排层提示"请重新读取最新版本再派生"。

        ⚠️ 本类**刻意不提供 update / delete**。改内容 = 建新 revision（铁律 4）。
        """
        if not rev.verify_fingerprint():
            raise DomainError("revision 指纹与内容不一致，拒绝落库：%s" % rev.revision_id)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO timeline_revisions (revision_id, project, episode, revision_no, "
                    "items, compose_fingerprint, parent_revision_id, subtitle_revision, "
                    "declared_silences, created_at, created_by, note) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rev.revision_id, rev.project, rev.episode, int(rev.revision_no),
                     json.dumps([i.to_dict() for i in rev.items], ensure_ascii=False),
                     rev.compose_fingerprint, rev.parent_revision_id,
                     rev.subtitle_revision, int(rev.declared_silences),
                     rev.created_at, rev.created_by, rev.note))
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise ConflictError(
                    "时间线版本冲突（project=%s episode=%s revision_no=%s 已被占用）：%s"
                    % (rev.project, rev.episode, rev.revision_no, e)) from e
            finally:
                conn.close()
        return rev.revision_id

    # ---------- 读取 ----------

    def get_revision(self, revision_id: str) -> Optional[TimelineRevision]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM timeline_revisions WHERE revision_id=?",
                               (str(revision_id),)).fetchone()
            return self._to_revision(row) if row else None
        finally:
            conn.close()

    def get_revision_by_no(self, project: str, episode: str,
                           revision_no: int) -> Optional[TimelineRevision]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM timeline_revisions WHERE project=? AND episode=? AND revision_no=?",
                (str(project), str(episode), int(revision_no))).fetchone()
            return self._to_revision(row) if row else None
        finally:
            conn.close()

    def latest_revision(self, project: str, episode: str) -> Optional[TimelineRevision]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM timeline_revisions WHERE project=? AND episode=? "
                "ORDER BY revision_no DESC LIMIT 1", (str(project), str(episode))).fetchone()
            return self._to_revision(row) if row else None
        finally:
            conn.close()

    def list_revisions(self, *, project: str = "", episode: str = "",
                       limit: int = 100) -> List[TimelineRevision]:
        sql = "SELECT * FROM timeline_revisions WHERE 1=1"
        args: List[Any] = []
        if project:
            sql += " AND project=?"
            args.append(str(project))
        if episode:
            sql += " AND episode=?"
            args.append(str(episode))
        sql += " ORDER BY created_at DESC, revision_no DESC LIMIT ?"
        args.append(int(limit))
        conn = self._conn()
        try:
            return [self._to_revision(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def next_revision_no(self, project: str, episode: str) -> int:
        """建议的下一个版本号（空集从 1 开始）。

        这是**建议值**不是保证：并发派生可能撞号，靠
        :meth:`save_revision` 的唯一索引兜住（抛 :class:`ConflictError`）。
        """
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT MAX(revision_no) AS m FROM timeline_revisions "
                "WHERE project=? AND episode=?", (str(project), str(episode))).fetchone()
            return int((row["m"] or 0) + 1) if row else 1
        finally:
            conn.close()

    def find_by_fingerprint(self, compose_fingerprint: str) -> List[TimelineRevision]:
        """同指纹的历史 revision（用于「这版剪辑其实渲过」的判据）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM timeline_revisions WHERE compose_fingerprint=? "
                "ORDER BY revision_no DESC", (str(compose_fingerprint),)).fetchall()
            return [self._to_revision(r) for r in rows]
        finally:
            conn.close()

    # ---------- EpisodeRenderVersion（append-only，成片登记） ----------

    def save_render_version(self, rv: EpisodeRenderVersion) -> str:
        """持久化成片登记（ADR-0012：渲染结果不再随请求结束丢失）。"""
        ok, reason = rv.verify()
        if not ok:
            raise DomainError("成片登记与冻结计划不一致，拒绝落库：%s" % reason)
        # fail-closed：没有实际产物摘要的登记不能冒充成片事实。
        if not str(rv.output_path or "").strip() or not str(rv.output_sha256 or "").strip():
            raise DomainError("成片登记必须同时记录 output_path 与 output_sha256")
        if not os.path.isfile(rv.output_path):
            raise DomainError("成片登记的 output_path 不存在，拒绝落库：%s" % rv.output_path)
        digest = hashlib.sha256()
        try:
            with open(rv.output_path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
        except OSError as exc:
            raise DomainError("成片产物不可读，拒绝落库：%s" % exc) from exc
        if digest.hexdigest() != str(rv.output_sha256):
            raise DomainError("成片产物 sha256 与登记值不一致，拒绝落库")
        payload = list(rv.entry_media_version_ids)
        with self._lock:
            conn = self._conn()
            try:
                if conn.execute("SELECT 1 FROM timeline_revisions WHERE revision_id=?",
                                (rv.revision_id,)).fetchone() is None:
                    raise DomainError("成片登记引用的 revision_id 不存在：%s" % rv.revision_id)
                conn.execute(
                    "INSERT INTO episode_render_versions (render_id, revision_id, project,"
                    " episode, revision_no, compose_fingerprint, plan_fingerprint,"
                    " output_path, output_sha256, output_bytes, output_duration_sec,"
                    " subtitle_revision, entry_media_version_ids, declared_silences,"
                    " authorized_by, authorization_ref, bind_hash, created_at, note)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rv.render_id, rv.revision_id, rv.project, rv.episode,
                     int(rv.revision_no), rv.compose_fingerprint, rv.plan_fingerprint,
                     rv.output_path, rv.output_sha256, int(rv.output_bytes or 0),
                     float(rv.output_duration_sec or 0.0), rv.subtitle_revision,
                     json.dumps(payload, ensure_ascii=False), int(rv.declared_silences or 0),
                     rv.authorized_by, rv.authorization_ref, rv.bind_hash(),
                     rv.created_at, rv.note))
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise ConflictError("成片登记冲突（render_id=%s 已存在）：%s"
                                    % (rv.render_id, e)) from e
            finally:
                conn.close()
        return rv.render_id

    def get_render_version(self, render_id: str) -> Optional[EpisodeRenderVersion]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM episode_render_versions WHERE render_id=?",
                               (str(render_id),)).fetchone()
            return self._to_render(row) if row else None
        finally:
            conn.close()

    def list_render_versions(self, *, revision_id: str = "", project: str = "",
                             episode: str = "", limit: int = 100) -> List[EpisodeRenderVersion]:
        sql = "SELECT * FROM episode_render_versions WHERE 1=1"
        args: List[Any] = []
        if revision_id:
            sql += " AND revision_id=?"
            args.append(str(revision_id))
        if project:
            sql += " AND project=?"
            args.append(str(project))
        if episode:
            sql += " AND episode=?"
            args.append(str(episode))
        sql += " ORDER BY created_at DESC, render_id DESC LIMIT ?"
        args.append(int(limit))
        conn = self._conn()
        try:
            return [self._to_render(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def find_render_versions(self, compose_fingerprint: str) -> List[EpisodeRenderVersion]:
        """按 compose_fingerprint 反查成片登记（免重渲/交付追溯）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM episode_render_versions WHERE compose_fingerprint=?"
                " ORDER BY created_at DESC, render_id DESC",
                (str(compose_fingerprint),)).fetchall()
            return [self._to_render(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _to_render(row: Any) -> EpisodeRenderVersion:
        d = dict(row)
        try:
            ids = json.loads(d.get("entry_media_version_ids") or "[]")
        except (TypeError, ValueError) as exc:
            raise DomainError("成片登记的 entry_media_version_ids 已损坏：%s" % exc) from exc
        if not isinstance(ids, list):
            raise DomainError("成片登记的 entry_media_version_ids 必须是数组")
        d["entry_media_version_ids"] = tuple(str(x) for x in ids)
        rv = EpisodeRenderVersion.from_dict(d)
        stored = str(d.get("bind_hash") or "")
        if stored and rv.bind_hash() != stored:
            raise DomainError("成片登记 bind_hash 与字段内容不一致（疑似被旁路改写）")
        return rv

    def lineage(self, revision_id: str) -> List[TimelineRevision]:
        """版本血缘链（当前 → 根）。"""
        chain: List[TimelineRevision] = []
        cur = self.get_revision(revision_id)
        seen: set = set()
        while cur is not None and cur.revision_id not in seen:
            seen.add(cur.revision_id)
            chain.append(cur)
            cur = self.get_revision(cur.parent_revision_id) if cur.parent_revision_id else None
        return chain

    # ---------- 预检（组合 facts_repo 的批准状态） ----------

    def preflight(self, revision_id: str, facts_repo: Any = None, *,
                  approved_only: bool = False, renderer: str = "ffmpeg",
                  preset: str = "") -> Dict[str, Any]:
        """``compose:preflight`` 的仓储编排入口。

        返回体含 ``compose_fingerprint``（评估文档 §P0-4 明确要求）。
        ``facts_repo`` 可选注入：不注入时预检只做时间线自身校验，
        注入后会带上媒体版本事实与**真实的批准状态**（铁律 1 的读侧）。
        """
        rev = self.get_revision(revision_id)
        if rev is None:
            return {"ok": False, "revision_id": revision_id, "compose_fingerprint": "",
                    "errors": ["时间线版本不存在：%s" % revision_id], "warnings": [],
                    "manifest": {}}
        media_index = None
        if facts_repo is not None:
            media_index = facts_repo.media_index(i.media_version_id for i in rev.items)
        report = preflight(rev, media_index=media_index, approved_only=approved_only,
                           renderer=renderer, preset=preset)
        out = report.to_dict()
        out["project"] = rev.project
        out["episode"] = rev.episode
        out["revision_no"] = int(rev.revision_no)
        return out

    # ---------- 辅助 ----------

    @staticmethod
    def _to_revision(row: Any) -> TimelineRevision:
        d = dict(row)
        try:
            items = json.loads(d.get("items") or "[]")
        except (TypeError, ValueError):
            items = []
        d["items"] = items
        return TimelineRevision.from_dict(d)
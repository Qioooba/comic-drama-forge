# -*- coding: utf-8 -*-
"""交付包持久化（SQLite）+ 交付清单构建。

职责划分（与 :mod:`app.domain.delivery` 的分工）
-----------------------------------------------
* ``app/domain/delivery.py`` —— 规则与判定（预设、哈希、批准失效），纯函数；
* **本模块** —— 文件系统扫描 + SQLite 落盘。

**产物复用**：交付包**不重新导出**任何东西。它扫描
:data:`nle_export.EXPORT_DIR`（即 ``output/export/<项目>/``）下已有的
SRT / 剪映草稿 / FCPXML / 帧清单，只做「登记 + 算 SHA-256」。
这样既不会重复造导出逻辑，也不会因为重新导出而把用户刚导出的文件改掉。

数据库位置
----------
``output/delivery/delivery.db``（与 ``output/export`` 平级）。
诊断链可用 ``file:...?mode=ro`` + ``PRAGMA query_only`` 只读打开，
不启动服务、不探显卡。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

import nle_export
from domain import delivery as delivery_domain

logger = logging.getLogger(__name__)

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

#: 交付元数据目录（**刻意不放进 EXPORT_DIR**）
#:
#: 交付清单里含文件自身的 SHA-256；如果把清单也写进导出目录，清单就会
#: 「包含自己的哈希」，形成循环引用。这里让元数据与被校验的产物彻底分家。
DELIVERY_HOME = os.path.join(_ROOT_DIR, "output", "delivery")
DB_PATH = os.path.join(DELIVERY_HOME, "delivery.db")

#: 本模块的 schema 版本。**每次改表 +1**，并在 :data:`_MIGRATIONS` 里登记脚本，
#: 与 ``facts_repo`` 同一套迁移体系：以前本库只有裸 ``CREATE TABLE IF NOT EXISTS``，
#: 将来给表加列时旧库会静默保持旧结构（``IF NOT EXISTS`` 对**已存在的表**不补列），
#: 于是新代码写旧列、旧代码读新列都成静默错误。有了版本表才能显式 ALTER 并记账。
SCHEMA_VERSION = 1

#: 版本表登记用的 module 名（一个库可能由多个模块共用这张表）。
_SCHEMA_MODULE = "delivery_repo"

__all__ = [
    "DELIVERY_HOME",
    "DB_PATH",
    "SCHEMA_VERSION",
    "safe_project",
    "open_readonly",
    "collect_artifacts",
    "build_package",
    "save_package",
    "list_packages",
    "get_package",
    "record_verify",
    "get_approval",
    "approve_package",
    "revoke_approval",
    "export_manifest",
]

#: 所有迁移共用的版本表（幂等）
_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    module      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    applied_at  TEXT NOT NULL,
    PRIMARY KEY (module)
);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_packages (
    package_id      TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    episode_no      INTEGER,
    preset_id       TEXT NOT NULL,
    package_hash    TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    file_count      INTEGER NOT NULL DEFAULT 0,
    total_bytes     INTEGER NOT NULL DEFAULT 0,
    requirement     TEXT,
    licensing_ok    INTEGER NOT NULL DEFAULT 0,
    verified_ok     INTEGER NOT NULL DEFAULT 0,
    verified_at     TEXT,
    created_at      TEXT NOT NULL,
    artifact_root   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_approvals (
    package_id   TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    approver     TEXT NOT NULL,
    note         TEXT,
    reason       TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0,
    revoked_by   TEXT,
    revoked_at   TEXT,
    approved_at  TEXT NOT NULL,
    PRIMARY KEY (package_id)
);

CREATE TABLE IF NOT EXISTS delivery_files (
    package_id TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256     TEXT NOT NULL DEFAULT '',
    mtime      TEXT,
    PRIMARY KEY (package_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_delivery_packages_project
    ON delivery_packages(project);
"""

#: ``version`` → DDL 脚本。**只增不改**：已发布的版本函数永不修改，
#: 否则老库重新启动会走不同分支。加列写在这里（``ALTER TABLE ... ADD COLUMN``）。
_MIGRATIONS: Dict[int, str] = {1: _SCHEMA}


# --------------------------------------------------------------------------
# 路径安全
# --------------------------------------------------------------------------

def safe_project(name: str) -> str:
    """项目名归一 + 目录穿越防护。

    交付包根目录直接来自用户入参，``../`` 必须在这里被拒掉。
    返回空串表示非法（调用方按既有风格回 400）。
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    cleaned = raw.replace("\\", "/").strip("/")
    if not cleaned or cleaned in (".", ".."):
        return ""
    parts = [p for p in cleaned.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return ""
    if any(":" in p for p in parts):  # 盘符 / NTFS ADS
        return ""
    return "/".join(parts)


def _connect() -> sqlite3.Connection:
    os.makedirs(DELIVERY_HOME, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_BASE_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移：补齐缺失的表结构并登记 ``schema_version``（同 ``facts_repo``）。

    幂等性来自两处：DDL 全部 ``IF NOT EXISTS``；已登记的版本**不重复执行**。
    迁移失败必须抛（调用方拿不到一个「结构未知」的连接继续写库），
    否则会出现「代码以为有这列、表里其实没有」的静默错误。

    刻意**不做**「本进程已迁移过」的进程级短路：库文件随时可能被删/被换，
    短路会让新库停留在「只有版本表、没有业务表」的状态。要省这几次 DDL
    应该给仓储加实例（像 ``FactsRepo``），而不是在模块级函数里缓存状态。
    """
    applied = {r["module"]: int(r["version"]) for r in
               conn.execute("SELECT module, version FROM schema_version")}
    cur = int(applied.get(_SCHEMA_MODULE, 0))
    for ver in range(cur + 1, SCHEMA_VERSION + 1):
        script = _MIGRATIONS.get(ver)
        if script is None:
            raise RuntimeError("delivery_repo 缺少 v%d 的迁移脚本" % ver)
        conn.executescript(script)
        conn.execute(
            "INSERT INTO schema_version (module, version, applied_at) VALUES (?,?,?) "
            "ON CONFLICT(module) DO UPDATE SET version=excluded.version, "
            "applied_at=excluded.applied_at",
            (_SCHEMA_MODULE, ver, delivery_domain.now_iso()))
        logger.info("交付库已应用迁移 v%d", ver)
    conn.commit()


def open_readonly():
    """以 ``mode=ro`` + ``query_only`` 只读打开交付库（只读诊断链用）。

    不写库、不建目录 —— 数据库文件不存在时返回 ``None``。
    """
    if not os.path.isfile(DB_PATH):
        return None
    uri = "file:{}?mode=ro".format(DB_PATH.replace("?", "%3f").replace("#", "%23"))
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error as exc:  # pragma: no cover - 只读诊断的降级路径
        logger.warning("交付库只读打开失败：%s", exc)
        return None


# --------------------------------------------------------------------------
# 产物收集（复用 nle_export，不重新导出）
# --------------------------------------------------------------------------

def collect_artifacts(project: str) -> List[Dict[str, Any]]:
    """扫描既有导出产物，为每个文件算 SHA-256。

    只读扫描：不创建目录、不写任何文件。目录不存在时返回空列表。
    """
    root = os.path.join(nle_export.EXPORT_DIR, *safe_project(project).split("/"))
    entries: List[Dict[str, Any]] = []
    for rel, abs_path in delivery_domain.iter_root_files(root):
        try:
            entries.append(delivery_domain.compute_file_entry(abs_path, rel))
        except OSError as exc:
            logger.warning("交付文件哈希失败（跳过 %s）：%s", rel, exc)
    entries.sort(key=lambda e: e["rel_path"])
    return entries


def refresh_disk_state(pkg: Dict[str, Any]) -> Dict[str, Any]:
    """按**磁盘现状**刷新交付包的 ``current_package_hash`` / ``disk_ok``。

    这是批准失效判定成立的**前提**：只有拿磁盘现状去比，批准才会在文件被改后
    自动失效。只看清单基线的话，批准永远不会失效。

    ⚠ 这里**故意不传任何摘要缓存**，每次都让
    :func:`domain.delivery.recompute_package_hash` 全量重算：门禁判定路径上
    任何以 ``(size, mtime)`` 为键的缓存都会被「等长改内容 + 还原 mtime」
    （robocopy /COPY:DAT、docker cp、copy2、tar -x）命中，从而拿旧摘要判成
    「没变」。省 IO 只能省在展示层，不能省在这里。

    写入 ``pkg`` 三个键（就地修改并返回同一个对象）：
      * ``current_package_hash`` —— 磁盘现状的包摘要；有文件缺失/不可读时为 ``""``
      * ``disk_ok``             —— 磁盘现状是否算得出来
      * ``disk_matches_baseline`` —— 磁盘现状是否仍等于清单基线
    """
    root = pkg.get("artifact_root") or ""
    files = pkg.get("files") or []
    current = delivery_domain.recompute_package_hash(
        files, root, pkg.get("preset_id") or "",
        schema_version=int(pkg.get("schema_version")
                           or delivery_domain.DELIVERY_SCHEMA_VERSION))
    pkg["current_package_hash"] = current
    pkg["disk_ok"] = bool(current)
    pkg["disk_matches_baseline"] = bool(
        current and current == str(pkg.get("package_hash") or ""))
    return pkg


# --------------------------------------------------------------------------
# 包
# --------------------------------------------------------------------------

def build_package(project: str, preset_id: str,
                  requirement: Optional[Dict[str, Any]] = None,
                  licensing_gate: Optional[Dict[str, Any]] = None,
                  episode_no: Optional[int] = None) -> Dict[str, Any]:
    """登记一个交付包（不触发任何导出动作）。

    ``package_hash`` 在这里算一次并落库；之后每一次校验/批准都跟它比。
    """
    entries = collect_artifacts(project)
    preset = delivery_domain.get_preset(preset_id)
    pkg_hash = delivery_domain.compute_package_hash(preset["preset_id"], entries)
    gate = dict(licensing_gate or {})
    return {
        "package_id": uuid.uuid4().hex,
        "project": project,
        "episode_no": episode_no,
        "preset": preset,
        "preset_id": preset["preset_id"],
        "package_hash": pkg_hash,
        "schema_version": delivery_domain.DELIVERY_SCHEMA_VERSION,
        "files": entries,
        "file_count": len(entries),
        "total_bytes": sum(int(e.get("size_bytes") or 0) for e in entries),
        "requirement": dict(requirement or {}),
        "licensing_ok": bool(gate.get("ok")),
        "licensing_violations": gate.get("violations") or [],
        "verified_ok": False,
        "verified_at": "",
        "created_at": delivery_domain.now_iso(),
        "artifact_root": os.path.join(nle_export.EXPORT_DIR, *safe_project(project).split("/")),
    }


def save_package(pkg: Dict[str, Any]) -> Dict[str, Any]:
    """把交付包及其文件清单落库。"""
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO delivery_packages "
                "(package_id, project, episode_no, preset_id, package_hash, schema_version,"
                " file_count, total_bytes, requirement, licensing_ok, verified_ok,"
                " verified_at, created_at, artifact_root)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pkg["package_id"], pkg["project"], pkg.get("episode_no"),
                 pkg.get("preset_id") or "", pkg.get("package_hash") or "",
                 int(pkg.get("schema_version") or delivery_domain.DELIVERY_SCHEMA_VERSION),
                 int(pkg.get("file_count") or 0), int(pkg.get("total_bytes") or 0),
                 json.dumps(pkg.get("requirement") or {}, ensure_ascii=False),
                 1 if pkg.get("licensing_ok") else 0,
                 1 if pkg.get("verified_ok") else 0,
                 pkg.get("verified_at") or "", pkg.get("created_at") or "",
                 pkg.get("artifact_root") or ""))
            conn.execute("DELETE FROM delivery_files WHERE package_id=?", (pkg["package_id"],))
            conn.executemany(
                "INSERT OR REPLACE INTO delivery_files"
                " (package_id, rel_path, size_bytes, sha256, mtime) VALUES (?,?,?,?,?)",
                [(pkg["package_id"], e.get("rel_path") or "", int(e.get("size_bytes") or 0),
                  e.get("sha256") or "", e.get("mtime") or "") for e in (pkg.get("files") or [])])
    finally:
        conn.close()
    return refresh_disk_state(pkg)


def _row_to_pkg(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "package_id": row["package_id"],
        "project": row["project"],
        "episode_no": row["episode_no"],
        "preset_id": row["preset_id"],
        "preset": delivery_domain.get_preset(row["preset_id"]),
        "package_hash": row["package_hash"],
        "schema_version": row["schema_version"],
        "file_count": row["file_count"],
        "total_bytes": row["total_bytes"],
        "requirement": json.loads(row["requirement"] or "{}"),
        "licensing_ok": bool(row["licensing_ok"]),
        "verified_ok": bool(row["verified_ok"]),
        "verified_at": row["verified_at"] or "",
        "created_at": row["created_at"],
        "artifact_root": row["artifact_root"],
    }


def get_package(package_id: str) -> Optional[Dict[str, Any]]:
    """取交付包（含文件清单与授权引用）。"""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM delivery_packages WHERE package_id=?",
                           (str(package_id),)).fetchone()
        if not row:
            return None
        pkg = _row_to_pkg(row)
        pkg["files"] = [dict(r) for r in conn.execute(
            "SELECT rel_path, size_bytes, sha256, mtime FROM delivery_files"
            " WHERE package_id=? ORDER BY rel_path", (str(package_id),))]
        pkg["file_count"] = len(pkg["files"])
    finally:
        conn.close()
    return refresh_disk_state(pkg)


def list_packages(project: str = "") -> List[Dict[str, Any]]:
    """列出交付包（可按项目过滤），附机器校验与人工批准状态。

    这里同样走 :func:`refresh_disk_state`：批准有效性必须按磁盘现状判，
    列表页也不例外 —— 否则用户在列表里会看到一个「已批准」、
    点进去才发现文件早被改了的交付包。
    """
    sql = "SELECT * FROM delivery_packages"
    args: List[Any] = []
    if project:
        sql += " WHERE project=?"
        args.append(project)
    sql += " ORDER BY created_at DESC"
    conn = _connect()
    try:
        rows = conn.execute(sql, args).fetchall()
        approvals = {r["package_id"]: dict(r) for r in conn.execute("SELECT * FROM delivery_approvals")}
        files_by_pkg: Dict[str, List[Dict[str, Any]]] = {}
        for r in conn.execute("SELECT * FROM delivery_files ORDER BY package_id, rel_path"):
            files_by_pkg.setdefault(r["package_id"], []).append(
                {"rel_path": r["rel_path"], "size_bytes": r["size_bytes"],
                 "sha256": r["sha256"], "mtime": r["mtime"]})
    finally:
        conn.close()
    out = []
    for row in rows:
        pkg = _row_to_pkg(row)
        pkg["files"] = files_by_pkg.get(pkg["package_id"], [])
        pkg["file_count"] = len(pkg["files"])
        refresh_disk_state(pkg)
        approval = approvals.get(pkg["package_id"])
        pkg["approval"] = delivery_domain.evaluate_approval(pkg, approval)
        pkg["status"] = delivery_domain.derive_status(pkg, approval)
        out.append(pkg)
    return out


def record_verify(package_id: str, result: Dict[str, Any]) -> None:
    """记录**机器校验**结果。

    这里写的 ``verified_ok`` 只能把状态推到 ``verified`` ——
    按协调书 §4，它**永远**不会被写成「已批准」。
    """
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE delivery_packages SET verified_ok=?, verified_at=? WHERE package_id=?",
                (1 if result.get("ok") else 0, delivery_domain.now_iso(), str(package_id)))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 人工批准
# --------------------------------------------------------------------------

def get_approval(package_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM delivery_approvals WHERE package_id=?",
                           (str(package_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def approve_package(package_id: str, package_hash: str, approver: str,
                    note: str = "", reason: str = "") -> Dict[str, Any]:
    """写入人工批准（**哈希绑定**）。

    ``package_hash`` 是批准当时的包摘要；它与之后的
    ``delivery_packages.package_hash`` 不一致时，批准自动失效
    （判定在 :func:`domain.delivery.evaluate_approval`，纯函数，无需后台撤销）。
    """
    row = {
        "package_id": str(package_id),
        "package_hash": str(package_hash or ""),
        "approver": str(approver or "").strip(),
        "note": str(note or ""),
        "reason": str(reason or ""),
        "revoked": 0,
        "approved_at": delivery_domain.now_iso(),
    }
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO delivery_approvals"
                " (package_id, package_hash, approver, note, reason, revoked,"
                "  revoked_by, revoked_at, approved_at)"
                " VALUES (?,?,?,?,?,0,NULL,NULL,?)",
                (row["package_id"], row["package_hash"], row["approver"],
                 row["note"], row["reason"], row["approved_at"]))
    finally:
        conn.close()
    return row


def revoke_approval(package_id: str, operator: str, reason: str = "") -> Dict[str, Any]:
    """显式撤销人工批准。撤销是人工动作，优先于哈希判定（内容变化不应复活它）。"""
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE delivery_approvals SET revoked=1, revoked_by=?, revoked_at=?"
                " WHERE package_id=?",
                (str(operator or ""), delivery_domain.now_iso(), str(package_id)))
    finally:
        conn.close()
    return get_approval(package_id) or {}


def export_manifest(pkg: Dict[str, Any]) -> Dict[str, Any]:
    """交付清单（对外 JSON）：文件 + SHA-256 + 包摘要 + 授权与批准状态。"""
    approval = get_approval(pkg["package_id"])
    approval_view = delivery_domain.evaluate_approval(pkg, approval)
    return {
        "schema_version": delivery_domain.DELIVERY_SCHEMA_VERSION,
        "package_id": pkg["package_id"],
        "project": pkg["project"],
        "episode_no": pkg.get("episode_no"),
        "preset": pkg.get("preset") or delivery_domain.get_preset(pkg.get("preset_id")),
        "package_hash": pkg.get("package_hash"),
        "generated_at": delivery_domain.now_iso(),
        "file_count": pkg.get("file_count") or len(pkg.get("files") or []),
        "total_bytes": pkg.get("total_bytes") or 0,
        "files": list(pkg.get("files") or []),
        "current_package_hash": pkg.get("current_package_hash") or "",
        "disk_matches_baseline": bool(pkg.get("disk_matches_baseline")),
        "licensing_ok": bool(pkg.get("licensing_ok")),
        "verified_ok": bool(pkg.get("verified_ok")),
        "verified_at": pkg.get("verified_at") or "",
        "approval": approval_view,
        "status": delivery_domain.derive_status(pkg, approval),
        "note": "清单中的 SHA-256 覆盖每一个交付文件；内容变化即视为不同交付物。",
    }
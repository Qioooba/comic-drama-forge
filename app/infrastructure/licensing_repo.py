# -*- coding: utf-8 -*-
"""授权登记表仓储：``config/model-licensing.json`` 加载 + 门禁判定 + 审计留痕。

与领域层分工
------------
* ``app/domain/licensing.py`` —— 许可类型规则与门禁判定的**纯逻辑**；
* **本模块** —— 文件读取 + 按 mtime 缓存 + 把每次门禁判定写进 SQLite 审计表。

为什么登记表是 JSON 而不是数据库
------------------------------
授权是**要给人看、要进版本库、要在 code review 里被审**的东西。把它写成
数据库，review 时就看不见「这个模型到底能不能商用」；写成 JSON，每条授权
的变更都是一个可 diff 的行。数据库只用来留门禁判定的痕迹（谁在什么时候
因为哪个授权被拦下）。

审计表刻意设计成 append-only：门禁判定的历史只增不改 —— 改它等于篡改记录。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from domain import delivery as delivery_domain
from domain import licensing as licensing_domain

logger = logging.getLogger(__name__)

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REGISTRY_PATH = os.path.join(_ROOT_DIR, "config", "model-licensing.json")

#: 门禁审计库。与交付库同目录（``output/delivery/``），便于一次性只读诊断。
AUDIT_HOME = os.path.join(_ROOT_DIR, "output", "delivery")
AUDIT_DB_PATH = os.path.join(AUDIT_HOME, "licensing_audit.db")

__all__ = [
    "REGISTRY_PATH",
    "AUDIT_DB_PATH",
    "get_registry",
    "reload_registry",
    "evaluate_delivery_gate",
    "attribution_for",
    "record_gate_decision",
    "list_gate_decisions",
    "open_readonly",
]

_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"registry": None, "mtime": None, "size": None}

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS licensing_gate_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at   TEXT NOT NULL,
    package_id   TEXT,
    project      TEXT,
    ok           INTEGER NOT NULL DEFAULT 0,
    codes        TEXT NOT NULL DEFAULT '[]',
    checked      TEXT NOT NULL DEFAULT '{}',
    requirement  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_licensing_gate_log_decided
    ON licensing_gate_log(decided_at);
"""


# --------------------------------------------------------------------------
# 登记表加载（按 mtime + size 缓存）
# --------------------------------------------------------------------------

def get_registry(path: str = REGISTRY_PATH) -> licensing_domain.LicenseRegistry:
    """取授权登记表；文件未变时走内存缓存。

    开发时改 ``config/model-licensing.json`` 后无需重启即可生效 —— 这点很关键，
    因为门禁配置恰恰是**最需要边调边试**的一类配置。
    """
    p = str(path or REGISTRY_PATH)
    with _LOCK:
        try:
            st = os.stat(p)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            stamp = None
        if _CACHE["registry"] is not None and _CACHE["mtime"] == stamp:
            return _CACHE["registry"]
        registry = licensing_domain.load_registry(p)
        _CACHE.update({"registry": registry, "mtime": stamp, "size": stamp[1] if stamp else None})
        if not registry.loaded:
            # fail-closed 的表还在，但登记表读不出来：必须响亮，不能静默放行
            logger.error("授权登记表不可用，交付门禁将按最严口径阻断：%s", registry.load_error)
        return registry


def reload_registry(path: str = REGISTRY_PATH) -> licensing_domain.LicenseRegistry:
    """强制重载登记表（忽略缓存）。"""
    with _LOCK:
        _CACHE.update({"registry": None, "mtime": None, "size": None})
    return get_registry(path)


# --------------------------------------------------------------------------
# 门禁
# --------------------------------------------------------------------------

#: 门禁汇总文案兜底（正常不会走到：violations 为空且 ok=False 只可能是登记表本身坏了）
LIC_FALLBACK = "授权登记表不可用"


def evaluate_delivery_gate(requirement: Optional[Dict[str, Any]] = None,
                           package_id: str = "", project: str = "",
                           path: str = REGISTRY_PATH,
                           audit: bool = True) -> Dict[str, Any]:
    """对一次交付执行授权门禁，并把结论写入审计表。

    返回结构在 :func:`domain.licensing.evaluate_gate` 之上补了三个字段：
    ``registry``（登记表概览）、``summary``（一行中文结论）、``audited``。
    """
    registry = get_registry(path)
    result = licensing_domain.evaluate_gate(registry, requirement)
    result["registry"] = registry.summary()
    result["summary"] = (
        "授权门禁通过" if result["ok"]
        else "授权门禁未通过：{}".format(
            "；".join(sorted({v["message"] for v in result["violations"]})) or LIC_FALLBACK)
    )
    result["audited"] = bool(audit and record_gate_decision(
        result, requirement, package_id=package_id, project=project))
    return result


def attribution_for(requirement: Optional[Dict[str, Any]] = None,
                    path: str = REGISTRY_PATH) -> List[str]:
    """取该交付包的署名清单（可复制到简介的成对文本）。"""
    return licensing_domain.attribution_lines(get_registry(path), requirement)


# --------------------------------------------------------------------------
# 审计（append-only）
# --------------------------------------------------------------------------

def _audit_conn() -> sqlite3.Connection:
    os.makedirs(AUDIT_HOME, exist_ok=True)
    conn = sqlite3.connect(AUDIT_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_AUDIT_SCHEMA)
    return conn


def record_gate_decision(result: Dict[str, Any], requirement: Optional[Dict[str, Any]],
                         package_id: str = "", project: str = "") -> bool:
    """把一次门禁判定写入审计表。**只增不改**：返回 False 表示写库失败。"""
    try:
        conn = _audit_conn()
    except (sqlite3.Error, OSError) as exc:  # pragma: no cover
        logger.warning("授权门禁审计写入失败：%s", exc)
        return False
    try:
        with conn:
            conn.execute(
                "INSERT INTO licensing_gate_log"
                " (decided_at, package_id, project, ok, codes, checked, requirement)"
                " VALUES (?,?,?,?,?,?,?)",
                (delivery_domain.now_iso(), str(package_id or ""), str(project or ""),
                 1 if result.get("ok") else 0,
                 json.dumps(sorted({v.get("code") for v in (result.get("violations") or []) if v.get("code")}),
                            ensure_ascii=False),
                 json.dumps(result.get("checked") or {}, ensure_ascii=False),
                 json.dumps(requirement or {}, ensure_ascii=False)))
        return True
    except sqlite3.Error as exc:  # pragma: no cover
        logger.warning("授权门禁审计写入失败：%s", exc)
        return False
    finally:
        conn.close()


def list_gate_decisions(limit: int = 50) -> List[Dict[str, Any]]:
    """读回最近若干条门禁判定（诊断链用；只读，不写）。"""
    if not os.path.isfile(AUDIT_DB_PATH):
        return []
    conn = None
    try:
        uri = "file:{}?mode=ro".format(AUDIT_DB_PATH.replace("?", "%3f"))
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT * FROM licensing_gate_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover
        logger.warning("授权门禁审计只读读取失败：%s", exc)
        return []
    finally:
        if conn is not None:
            conn.close()
    out = []
    for r in rows:
        row = dict(r)
        row["ok"] = bool(row.get("ok"))
        for key in ("codes", "checked", "requirement"):
            try:
                row[key] = json.loads(row.get(key) or "[]")
            except (TypeError, ValueError):
                row[key] = [] if key == "codes" else {}
        out.append(row)
    return out


def open_readonly():
    """只读打开审计库（``mode=ro`` + ``query_only``）。文件不存在返回 ``None``。"""
    if not os.path.isfile(AUDIT_DB_PATH):
        return None
    try:
        uri = "file:{}?mode=ro".format(AUDIT_DB_PATH.replace("?", "%3f"))
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error as exc:  # pragma: no cover
        logger.warning("授权审计库只读打开失败：%s", exc)
        return None
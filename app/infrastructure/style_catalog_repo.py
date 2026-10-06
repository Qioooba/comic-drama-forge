# -*- coding: utf-8 -*-
"""风格目录持久化：读 ``app/style_catalog.json``（单一事实源）并缓存。

分层约定（2026-10-07 蓝图拆分）
------------------------------
- 本模块**只做 IO 与缓存**，不含业务规则；规则在 ``domain/style_catalog.py``。
- 目录文件随代码走（版本化），不是用户数据，故只读不写，也**不需要**迁移；
  内容变化 = 改 JSON → 重启即生效。

缓存策略
--------
按 ``(路径, mtime)`` 缓存，命中即返回**同一份** :class:`StyleCatalog` 实例，
避免每次请求都重新解析 JSON（``GET /api/styles`` 会被画廊高频调用）。

⚠️ Windows 上文件被其它进程/编辑器短暂独占时 ``open()`` 会抛 ``PermissionError``
   （暂时拿不到 ≠ 文件坏了），这里退避重试，与 ``ai_config._load_ai_config`` 同策略。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional, Tuple

from domain.style_catalog import StyleCatalog, StyleCatalogError

logger = logging.getLogger(__name__)

#: 单一事实源位置：本文件在 ``app/infrastructure/``，目录文件在 ``app/`` 下。
CATALOG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "style_catalog.json"))

_lock = threading.Lock()
_cache: Optional[StyleCatalog] = None
_cache_key: Optional[Tuple[str, float]] = None


def _mtime(path: str) -> float:
    """取 mtime；文件不存在返回 -1（不抛，便于给出明确报错文案）。"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return -1.0


def load_catalog_dict(path: str = CATALOG_PATH) -> dict:
    """读原始 JSON（带 Windows 共享冲突退避重试）。"""
    err = None
    for i in range(6):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except PermissionError as e:
            err = e
            time.sleep(0.02 * (i + 1))
        except Exception as e:  # noqa: BLE001
            err = e
            break
    raise StyleCatalogError(
        "风格目录读取失败（{}）：{}".format(path, err)) from err


def load_catalog(path: str = CATALOG_PATH, *, use_cache: bool = True) -> StyleCatalog:
    """加载风格目录（默认走 mtime 缓存）。

    目录损坏时**抛异常**（fail-closed）：风格库少条目 / id 重复属于事实源被改坏，
    必须响亮，否则前端会静默少几张风格卡片却没人发现。
    """
    global _cache, _cache_key
    key = (os.path.abspath(path), _mtime(path))
    if use_cache and _cache is not None and _cache_key == key:
        return _cache
    with _lock:
        if use_cache and _cache is not None and _cache_key == key:
            return _cache
        catalog = StyleCatalog.from_dict(load_catalog_dict(path))
        _cache = catalog
        _cache_key = key
        return catalog


def get_catalog() -> StyleCatalog:
    """取当前风格目录（``GET /api/styles`` 的唯一数据来源）。"""
    return load_catalog()


def invalidate_cache() -> None:
    """清缓存（改 JSON 后热重载用；测试亦用）。"""
    global _cache, _cache_key
    with _lock:
        _cache = None
        _cache_key = None


def safe_get_catalog() -> Optional[StyleCatalog]:
    """取目录，失败返回 None（供**可选增强**路径用）。

    ⚠️ 只给「目录缺失不该阻断主流程」的调用点用（如 ``style_kit`` 的解析函数）。
    ``/api/styles`` 这类**必须**有目录的端点请用 :func:`get_catalog`，让错误响亮。
    """
    try:
        return load_catalog()
    except StyleCatalogError:
        logger.warning("风格目录不可用，按无目录处理", exc_info=True)
        return None


__all__ = [
    "CATALOG_PATH",
    "get_catalog",
    "invalidate_cache",
    "load_catalog",
    "load_catalog_dict",
    "safe_get_catalog",
]
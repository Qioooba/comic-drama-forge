# -*- coding: utf-8 -*-
"""API 蓝图包：按域拆分 ``app/app.py`` 的路由，以及新增的领域蓝图。

设计约束（2026-10-07 拆分改造）
--------------------------------
1. **URL 逐字不变**：本包只做「搬家」，不改任何端点的路径、方法与语义。
   ``app/app.py`` 负责创建 Flask 实例与注册本包；本包只提供 ``Blueprint``。
2. **模块约定**：``app/api/`` 下每个模块暴露一个模块级 ``bp = Blueprint(...)``
   （可选 ``DOMAIN`` 字符串）。注册器按约定自动发现，无需改动中心文件。
3. **自动发现**（与既有 ``app/plugin_registry.py`` 的 ``pkgutil`` 做法一致）：
   新增一个域 = 丢一个文件进来，无需改 ``app.py``。这正是本次拆分要换来的收益。

加载失败策略
------------
默认 **fail-closed**：任一域模块导入失败即抛出并阻止启动 —— 端点缺失必须响亮，
不能静默变成 404。需要临时降级排查时设 ``MJSCXT_API_STRICT=0``（记 ERROR 日志后跳过），
该开关只用于本地排障，不得进生产。
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import sys

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))

#: 模块级 Blueprint 的属性名
BLUEPRINT_ATTR = "bp"

#: 不参与自动发现的模块名（工具/内部模块）
_SKIP_MODULES = {"__init__", "_shared"}


def _strict() -> bool:
    return os.environ.get("MJSCXT_API_STRICT", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _pkg_path() -> list:
    """本包在当前解释器里的搜索路径。

    frozen（PyInstaller onefile）下 ``__file__`` 指向 ``<_MEIPASS>/app/api/__init__.pyc``，
    该目录**没有实体文件**，``pkgutil.iter_modules`` 会返回空 → 一个蓝图都注册不上。
    所以优先用 ``__path__``（由 import 系统给出，frozen 下仍指向 PYZ 内的虚拟路径）。
    """
    return list(getattr(__spec__, "submodule_search_locations", None) or __path__)


def _frozen_candidates() -> list:
    """frozen 下的兜底候选模块名。

    为什么要显式列举：``collect_submodules`` 只能保证模块**在** PYZ 里，不能保证
    ``iter_modules`` 能枚举出它。把名单写死，是为了让「打包后少注册一个域」这种
    事故在**本文件里可见**，而不是等用户拿到一个全是 404 的 exe 才发现。
    新增域时**必须**同步这里 —— ``discover_modules`` 与本列表取并集。
    """
    return [
        "agent", "assets", "audio", "audio_qc_ai", "contracts", "delivery", "jobs",
        "licensing", "novels", "ops", "production_facts", "projects", "qc", "script",
        "storyboard", "styles", "timeline", "video",
    ]


def _frozen_list_is_current() -> list:
    """核对 frozen 兜底名单与磁盘上的域模块是否一致，返回缺失项。

    为什么要单独核对：``collect_submodules`` 只能保证模块**在** PYZ 里，
    而名单是手写的 —— 新增域时忘记同步名单，桌面版就会静默少注册一个域
    （症状是「某个接口在网页上有、在 exe 里 404」）。这个检查把该事故
    从「用户反馈」提前到「开发期」。
    """
    on_disk = {
        name for _finder, name, _ispkg in pkgutil.iter_modules(_pkg_path())
        if name not in _SKIP_MODULES and not name.startswith("_")
    }
    listed = set(_frozen_candidates())
    missing = sorted(on_disk - listed)     # 磁盘有、名单漏 → 打包后会丢
    stale = sorted(listed - on_disk)       # 名单有、磁盘无 → 开发期就该删
    if missing or stale:
        logger.error(
            "[api] frozen 兜底名单与磁盘不一致 —— "
            "漏登记=%s（打包后会静默 404）、多登记=%s",
            missing, stale,
        )
    return missing


def discover_modules():
    """按文件名排序返回本包内的域模块名（稳定顺序，便于比对与回归）。

    取「文件系统枚举」与「frozen 兜底名单」的**并集**：前者是日常开发的真相，
    后者保证 onefile 打包不漏。两者取并集后仍会由 ``load_blueprints`` 的
    fail-closed 兜住 —— 名单里若有模块在当前环境不存在，会报错而不是静默跳过。
    """
    names = set()
    for _finder, name, _ispkg in pkgutil.iter_modules(_pkg_path()):
        if name in _SKIP_MODULES or name.startswith("_"):
            continue
        names.add(name)
    # 无论是否 frozen 都核对一次：名单漂移是打包事故的根因，越早暴露越好
    _frozen_list_is_current()
    if getattr(sys, "frozen", False):
        names.update(_frozen_candidates())
    return sorted(names)


def load_blueprints():
    """导入全部域模块并收集 ``(module_name, blueprint)``。"""
    loaded = []
    for name in discover_modules():
        mod_name = "{}.{}".format(__name__, name)
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001
            if _strict():
                raise
            logger.exception("[api] 域模块 %s 导入失败，已跳过（MJSCXT_API_STRICT=0）", name)
            continue
        bp = getattr(mod, BLUEPRINT_ATTR, None)
        if bp is None:
            msg = "域模块 {} 未暴露模块级 `{}`".format(name, BLUEPRINT_ATTR)
            if _strict():
                raise RuntimeError(msg)
            logger.error("[api] %s，已跳过", msg)
            continue
        loaded.append((name, bp))
    return loaded


def register_blueprints(app):
    """把所有域蓝图注册到 Flask 实例，返回 ``{module_name: endpoint_prefix}``。"""
    registered = {}
    for name, bp in load_blueprints():
        # 同一模块重复注册（例如热重载）会让 url_map 出现两条一模一样的 rule，
        # 这里显式跳过，保证「一份代码一条路由」这条不变量。
        if bp.name in app.blueprints:
            logger.debug("[api] 蓝图 %s 已注册，跳过", bp.name)
            continue
        app.register_blueprint(bp)
        registered[name] = bp.name
        logger.info("[api] 已注册蓝图 %-12s url_prefix=%s", bp.name, bp.url_prefix or "/")
    logger.info("[api] 共注册 %d 个域蓝图", len(registered))
    return registered


__all__ = ["BLUEPRINT_ATTR", "discover_modules", "load_blueprints", "register_blueprints"]
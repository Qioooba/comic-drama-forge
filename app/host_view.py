# -*- coding: utf-8 -*-
"""宿主访问视图：把 ``app`` 模块与 ``api._shared`` 的名字合并查找。

为什么需要它（2026-10-07 蓝图拆分的回归修复）
------------------------------------------------
拆分把原先 18,613 行的 ``app/app.py`` 按域搬进了 ``app/api/``，``app.py`` 现在只
负责创建 Flask 实例、注册蓝图、错误兜底与 SPA 回退 —— **不再持有 212 个共用
helper**。

但 ``app/pipeline.py`` 与 ``app/autopilot.py`` 的 ``_A()`` 约定是
``sys.modules["app"]``。拆分前 ``app.py`` 本身就是全部内容的宿主，这个约定成立；
拆分后它只返回那个瘦身后的模块，于是 ``A._shot_seq`` / ``A.SCRIPT_DIR`` /
``A.novel_to_script`` / ``A.keyframe`` 等**全部 AttributeError**。

实测规模：``pipeline.py`` 单文件 90 处 ``A.<name>`` 访问、55 个不同名字，
其中 54 个由 ``api._shared`` 提供（余 1 个是正则误报，实际是 ``data.get(...)``）。

本模块提供合并视图，让那 90 个调用点**一行都不用改**。

为什么单独成文件而不是放进 ``api/_shared.py``
---------------------------------------------
``app/api/_shared.py`` 在**模块层**就 ``import autopilot`` 与 ``import pipeline``。
若把视图放进 ``_shared``、再让 ``pipeline`` / ``autopilot`` 来 import 它，
就形成 ``_shared → pipeline → _shared`` 的循环导入。本模块不 import 任何东西，
天然安全。

查找顺序
--------
先 ``app`` 模块、后 ``api._shared``。``app.py`` 侧的 ``app`` / ``APP_HOST`` /
``APP_PORT`` / ``APP_DEBUG`` 优先，其余名字由 ``_shared`` 补齐。拆分前这些名字
本就在 ``app.py`` 上，因此合并视图与旧语义一致。
"""
from __future__ import annotations

_CACHE: dict = {}


class _HostView:
    """按顺序在多个模块里查找名字的只读视图。

    不是真模块，也不支持赋值 —— 它的唯一用途是让既有的 ``A.<name>`` 读取模式
    在拆分后继续成立。如果有代码试图写 ``A.foo = 1``，应当显式失败而不是悄悄
    写进某个模块的全局字典。
    """

    __slots__ = ("_mods",)

    def __init__(self, mods: tuple) -> None:
        object.__setattr__(self, "_mods", mods)

    def __getattr__(self, name: str):
        for mod in self._mods:
            if mod is None:
                continue
            try:
                return getattr(mod, name)
            except AttributeError:
                continue
        known = ", ".join(sorted(getattr(m, "__name__", "?") for m in self._mods))
        raise AttributeError(
            "宿主视图里找不到 %r；已查找模块：%s。"
            "拆分后 helper 迁到了 app/api/_shared.py，若该名字确实是新增的，"
            "请确认它已被 _shared 导出。" % (name, known)
        )

    def __setattr__(self, name: str, value) -> None:
        raise AttributeError("宿主视图只读，不能赋值：%r" % (name,))

    def __dir__(self):
        out = set()
        for mod in self._mods:
            if mod is not None:
                out.update(dir(mod))
        return sorted(out)

    def __repr__(self) -> str:
        return "<HostView %s>" % "+".join(
            getattr(m, "__name__", "?") for m in self._mods if m is not None
        )


def host_view():
    """返回 ``app`` 与 ``api._shared`` 的合并视图（按 app 模块身份缓存）。

    延迟 import，避开循环导入；缓存以 app 模块对象为键，测试里换模块也能正确
    失效。``app`` 模块尚未加载时 fail-closed 抛错，而不是返回一个空壳让调用方
    在几百行之后才撞上莫名其妙的 AttributeError。
    """
    import sys as _sys

    app_mod = _sys.modules.get("app")
    if app_mod is None:
        raise RuntimeError("宿主模块 app 尚未加载")

    hit = _CACHE.get(id(app_mod))
    if hit is not None and hit[0] is app_mod:
        return hit[1]

    shared = None
    try:
        from api import _shared as shared  # noqa: PLC0415
    except Exception:  # noqa: BLE001 —— _shared 不可用时只退回 app 模块，不让视图本身成为故障点
        shared = None

    view = _HostView((app_mod, shared))
    _CACHE[id(app_mod)] = (app_mod, view)
    return view
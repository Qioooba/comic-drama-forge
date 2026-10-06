# -*- coding: utf-8 -*-
"""风格库蓝图：``GET /api/styles``（61 项可视化风格库的后端单一事实源）。

为什么需要这个端点（2026-10-07）
--------------------------------
风格库此前内联在 ``frontend/src/pages/ProjectsPage.tsx``，前端改了后端不知道，
且条目只有中文自由文本（改名即断历史）。现在事实源是 ``app/style_catalog.json``，
前端只消费本端点。

缩略图**不**由本端点下发：Flask 只挂了 ``/assets`` 路由，61 张缩略图留在
``frontend/src/assets/styles/`` 由 Vite 打包 import（端点只给文件名 ``thumbnail``）。

URL 契约（逐字固定，前端按字符串拼路径）
----------------------------------------
- ``GET /api/styles``                        整份目录（可带 ``?category=`` 过滤）
- ``GET /api/styles/<style_id>``             单个风格条目
- ``GET /api/styles/resolve?style=<自由文本>``  自由文本 → style_id（向后兼容解析）
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from domain.style_catalog import StyleCatalogError
from infrastructure.style_catalog_repo import get_catalog

bp = Blueprint("styles", __name__)
DOMAIN = "styles"


def _fail(message: str, status: int = 500):
    """统一错误响应体（沿用既有 ``{success, error}`` 约定）。"""
    return jsonify({"success": False, "error": message}), status


@bp.get("/api/styles")
def list_styles():
    """风格目录列表。

    查询参数：

    - ``category``：只返回该分类（``2d`` / ``3d`` / ``real``）；非法分类返回 400。
    - ``q``：按 ``style_id`` / label / alias 做大小写无关的包含过滤。

    返回体::

        {
          "success": true,
          "schema_version": 1,
          "default_style_id": "style_2d_urban_romance",
          "default_aspect_id": "aspect_16x9",
          "categories": [{...}],
          "styles": [{...61 条...}],
          "aspect_presets": [{...8 条...}],
          "counts": {"all": 61, "2d": 24, "3d": 11, "real": 26}
        }
    """
    try:
        catalog = get_catalog()
    except StyleCatalogError as e:
        # 事实源坏了必须响亮（fail-closed）：前端据此显式报错，而不是静默给空列表。
        return _fail(str(e), 500)

    payload = catalog.to_payload()
    styles = list(payload["styles"])  # type: ignore[arg-type]

    known = {str(c.get("id")) for c in payload["categories"]}  # type: ignore[union-attr]
    category = (request.args.get("category") or "").strip()
    if category:
        if known and category not in known:
            return _fail("未知分类：" + category, 400)
        styles = [s for s in styles if s.get("category") == category]

    q = (request.args.get("q") or "").strip().lower()
    if q:
        styles = [
            s for s in styles
            if q in str(s.get("style_id") or "").lower()
            or q in str(s.get("label") or "").lower()
            or any(q in str(a).lower() for a in (s.get("aliases") or []))
        ]

    counts = catalog.counts()
    payload["styles"] = styles
    payload["counts"] = dict(counts, filtered=len(styles))
    payload["success"] = True
    return jsonify(payload)


@bp.get("/api/styles/resolve")
def resolve_style():
    """自由文本 → 风格条目（向后兼容：老项目 ``config.style`` 就是中文自由文本）。

    ``?style=国漫风格/偏写实/竖屏9:16`` → 命中最接近的条目；命中不了返回
    ``{"success": true, "resolved": false}``（**200 而非 404**：解析失败是正常
    结果 —— 用户完全可以自定义风格，不能当成接口错误）。
    """
    text = request.args.get("style") or ""
    try:
        catalog = get_catalog()
    except StyleCatalogError as e:
        return _fail(str(e), 500)
    entry = catalog.resolve_text(text)
    if entry is None:
        return jsonify({"success": True, "resolved": False,
                        "style_id": None, "style": str(text)})
    return jsonify({"success": True, "resolved": True,
                    "style_id": entry.style_id, "style": entry.prompt_style,
                    "entry": entry.to_payload()})


@bp.get("/api/styles/<style_id>")
def get_style(style_id: str):
    """单个风格条目（含 ``positive_suffix`` / ``negative_suffix`` / 默认画幅）。"""
    try:
        catalog = get_catalog()
    except StyleCatalogError as e:
        return _fail(str(e), 500)
    entry = catalog.resolve_id(style_id)
    if entry is None:
        return _fail("风格不存在：" + style_id, 404)
    return jsonify({"success": True, "style": entry.to_payload()})


__all__ = ["DOMAIN", "bp"]
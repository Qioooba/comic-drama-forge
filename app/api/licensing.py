# -*- coding: utf-8 -*-
"""授权蓝图：登记查询 + 交付门禁（P1-7）。

改造前 ``grep license`` **零命中** —— 没有模型授权登记、没有音乐素材授权、
没有品牌与水印版本。这个蓝图把授权从「文档里的一句提醒」变成**可查询、
可判定、能拦住交付**的东西。

数据来源
--------
登记数据在 ``config/model-licensing.json``（可 diff、可 code review），
加载与审计在 :mod:`infrastructure.licensing_repo`，判定规则在
:mod:`domain.licensing`。

门禁的错误码（前端按码分支，不解析文案）
--------------------------------------
===============================  ==========================================
``LIC-REGISTRY-MISSING``        登记表读不出来 → 按最严口径整体阻断
``LIC-MODEL-UNREGISTERED``      模型未登记授权
``LIC-MODEL-NONCOMMERCIAL``     模型许可禁止商用（如 FLUX.1-dev）
``LIC-ASSET-UNREGISTERED``      音乐/素材未登记授权
``LIC-ASSET-NONCOMMERCIAL``     音乐/素材许可禁止商用（如 CC BY-NC）
``LIC-LICENSE-UNVERIFIED``      已登记但**待核实**，未确认可商用
``LIC-ATTRIBUTION-MISSING``     要求署名却没提供 attribution_text
``LIC-BRAND-UNRESOLVED``        品牌/水印版本不存在
``LIC-GATE-BLOCKED``            汇总码（交付创建接口返回 403 时带 violations）
===============================  ==========================================
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from domain import licensing as licensing_domain
from infrastructure import licensing_repo

bp = Blueprint("licensing", __name__)
DOMAIN = "licensing"


@bp.get("/api/licensing/registry")
def get_license_registry():
    """授权登记表全量（模型 / 素材 / 品牌）。

    ``summary.loaded=false`` 表示登记表读不出来 —— 此时门禁按最严口径阻断，
    返回空列表**不等于**「没有需要授权的东西」。
    """
    registry = licensing_repo.get_registry()
    return jsonify({
        "success": True,
        "summary": registry.summary(),
        "license_types": licensing_domain.LICENSE_TYPES,
        "models": registry.models,
        "assets": registry.assets,
        "brands": registry.brands,
    })


@bp.get("/api/licensing/music")
def list_licensed_music():
    """音乐库授权列表。

    每条都带 ``attribution_text``：要求署名的曲目，前端「复制署名」按钮
    直接吃这段文本（CC BY 署名必须能复制到简介）。
    """
    registry = licensing_repo.get_registry()
    music = [a for a in registry.assets
             if str(a.get("kind") or "").lower() in ("music", "audio", "sfx", "bgm")]
    return jsonify({
        "success": True,
        "count": len(music),
        "music": music,
        "summary": registry.summary(),
    })


@bp.get("/api/licensing/brands")
def list_brand_kits():
    """品牌与水印版本列表。"""
    registry = licensing_repo.get_registry()
    return jsonify({
        "success": True,
        "count": len(registry.brands),
        "brands": registry.brands,
        "summary": registry.summary(),
    })


@bp.post("/api/licensing/gate")
def evaluate_licensing_gate():
    """授权门禁预检（交付前自查，不产生任何副作用记录以外的东西）。

    body: ``{models?, assets?, music?, brand?, brand_version?,
             attribution_text?, purpose?}``

    返回 ``gate.ok`` 与逐条 ``violations``；``ok=false`` 时
    ``POST /api/delivery/packages`` 会以 403 + ``LIC-GATE-BLOCKED`` 拒绝建包。
    """
    data = request.get_json(silent=True) or {}
    requirement = {
        "models": list(data.get("models") or []),
        "assets": list(data.get("assets") or []),
        "music": list(data.get("music") or []),
        "brand": data.get("brand") or "",
        "brand_version": data.get("brand_version") or "",
        "attribution_text": data.get("attribution_text") or "",
        "purpose": data.get("purpose") or "commercial",
    }
    gate = licensing_repo.evaluate_delivery_gate(requirement, audit=True)
    return jsonify({
        "success": bool(gate.get("ok")),
        "gate": gate,
        "attribution": licensing_repo.attribution_for(requirement),
    })
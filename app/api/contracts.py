# -*- coding: utf-8 -*-
"""契约蓝图：OpenAPI 3.1 导出与契约版本查询。

这个域是「契约优先」（P1-6）的**对外锚点**：前端客户端与 CI 都从这里取规格。

约定
----
* ``url_prefix`` 留空，路径原样写在 ``@bp`` 上（与 ``app/api/__init__.py`` 一致）；
* 不 ``import app`` —— 拆分期间 ``app/app.py`` 可能半完成。
  规格反射用的 Flask 实例在**请求期**通过 ``current_app`` 取，
  此时所有蓝图早已注册完毕，拿到的 ``url_map`` 是全量的。
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from contracts import openapi as openapi_spec

bp = Blueprint("contracts", __name__)
DOMAIN = "contracts"


@bp.get("/api/contracts/openapi.json")
def get_openapi_spec():
    """导出 OpenAPI 3.1 规格（契约本体）。

    规格 = 「从 url_map 反射的全量路由」 + 「三个新域手工补的 schema」，
    由 :func:`contracts.openapi.build_spec` 组装。
    """
    spec = openapi_spec.build_spec(current_app._get_current_object())
    return jsonify(spec)


@bp.get("/api/contracts/version")
def get_contract_version():
    """契约版本 + 规格内容摘要。

    ``spec_hash`` 是前端启动闸门（W5 的契约版本校验）判断「本地客户端是否
    已过期」的依据：只比对摘要，不下载整份规格。
    """
    spec = openapi_spec.build_spec(current_app._get_current_object())
    stats = spec.get("x-stats") or {}
    return jsonify({
        "success": True,
        "contract_version": openapi_spec.CONTRACT_VERSION,
        "openapi": openapi_spec.OPENAPI_VERSION,
        "spec_hash": openapi_spec.spec_hash(),
        "route_digest": openapi_spec.route_digest(current_app._get_current_object()),
        "path_count": int(stats.get("path_count") or 0),
        "operation_count": int(stats.get("operation_count") or 0),
        "reflected_operations": int(stats.get("reflected_ops") or 0),
        "manual_operations": int(stats.get("manual_ops") or 0),
        "manual_paths": openapi_spec.manual_paths(),
    })


@bp.get("/api/contracts/error-codes")
def get_error_codes():
    """交付与授权的错误码对照表。

    前端必须按 ``code`` 分支，不解析 ``message`` 文案（文案是对外契约，
    会变；码不会）。
    """
    from domain import delivery as delivery_domain
    from domain import licensing as licensing_domain

    return jsonify({
        "success": True,
        "delivery": delivery_domain.DELIVERY_ERROR_CODES,
        "licensing": licensing_domain.LICENSING_ERROR_CODES,
    })
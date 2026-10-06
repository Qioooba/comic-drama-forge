# -*- coding: utf-8 -*-
"""OpenAPI 3.1 规格生成（契约优先，P1-6）。

背景
----
本项目此前的契约是「口口相传 + 运行时崩溃」：后端
``app/app.py`` 16.8k 行 / 242 条路由，前端 ``client.ts`` 1,508 行全靠字符串
拼 URL，后端一改返回结构，前端只在用户点下去的那一刻才炸。

本模块把「接口长什么样」变成**可导出的数据**，三段式：

1. **这里**：从 Flask ``url_map`` 反射出全量路径与方法，叠加**手工补的
   schema**，产出 OpenAPI 3.1 字典；
2. ``GET /api/contracts/openapi.json`` —— 对外导出，前端与 CI 消费；
3. ``scripts/generate_client.py`` 读它生成 TS 客户端，
   ``scripts/check_generated_client.py`` 在 CI 里查「生成物是否过期」。

两种路径来源（**故意混用**）
--------------------------
* **反射**：覆盖既有 242 条路由，只给路径 + 方法，标注
  ``x-contract-source: reflected``、**不给 response schema**。
  理由：那 242 条路由本轮是「纯搬迁」（协调书 R5），此刻为每条手写 schema
  既不现实也会立刻过期。先把它们**钉在规格里**（新增路径会被 CI 发现），
  schema 逐域补。
* **手工**：`contracts` / `delivery` / `licensing` 三个新域的端点，
  带完整 requestBody / response schema，标注 ``x-contract-source: manual``。
  理由：这三条是本轮新增的，schema 与实现同时写、同时生效，不会漂移。

「哪些路径是手工补的」这一信息本身也是契约：
:func:`build_spec` 返回的 ``x-manual-paths`` 让 CI 能断言「手工 schema
必须真的覆盖了它声称覆盖的路径」。

安全边界
--------
本模块只读 ``url_map``，**不启动服务、不连 ComfyUI、不碰 GPU**。
它也不 ``import app``（拆分期间 ``app/app.py`` 可能半完成）——
Flask 实例由调用方传入，路由内则用 ``flask.current_app`` 代理取。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "OPENAPI_VERSION",
    "CONTRACT_VERSION",
    "MANUAL_BLUEPRINTS",
    "build_spec",
    "spec_hash",
    "manual_operations",
    "manual_paths",
    "undocumented_routes",
    "write_spec",
]

#: OpenAPI 版本（3.1 允许 ``type: ["string", "null"]`` 与 JSON Schema 关键字）
OPENAPI_VERSION = "3.1.0"

#: 契约版本。前端启动时的兼容性闸门（W5）拿它做比对。
CONTRACT_VERSION = "2026-10-07.2"

_RULE_ARG = re.compile(r"<(?:(?P<conv>[a-zA-Z_]+):)?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)>")

#: 反射时忽略的路径（静态资源 / 前端入口）：它们不是 API 契约
_SKIP_PREFIXES = ("/static", "/assets", "/locales")
_SKIP_EXACT = ("/", "/favicon.ico")


# --------------------------------------------------------------------------
# 路径反射
# --------------------------------------------------------------------------

def _rule_to_path(rule: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Flask rule → OpenAPI path + 路径参数列表。

    ``/api/deliverable/file/<project_name>/<path:filename>``
    → ``/api/deliverable/file/{project_name}/{filename}``
    """
    params: List[Dict[str, Any]] = []

    def _sub(m: "re.Match[str]") -> str:
        conv = (m.group("conv") or "string").lower()
        name = m.group("name")
        schema: Dict[str, Any] = {"type": "string"}
        if conv in ("int",):
            schema = {"type": "integer"}
        elif conv in ("float",):
            schema = {"type": "number"}
        elif conv == "path":
            schema = {"type": "string", "description": "路径参数（可含斜杠）"}
        params.append({
            "name": name,
            "in": "path",
            "required": True,
            "schema": schema,
        })
        return "{%s}" % name

    return _RULE_ARG.sub(_sub, rule), params


def _should_reflect(rule: str, endpoint: str) -> bool:
    """是否把这条 rule 写进规格。

    * 跳过静态资源与前端入口（它们是部署产物，不是接口契约）；
    * 跳过本模块自己的 ``/api/contracts/openapi.json``（自指，且体积巨大）。
    """
    if rule in _SKIP_EXACT:
        return False
    if any(rule.startswith(p) for p in _SKIP_PREFIXES):
        return False
    if rule.startswith("/api/contracts/openapi"):
        return False
    return True


def _reflect(flask_app) -> List[Tuple[str, str, Dict[str, Any], List[Dict[str, Any]]]]:
    """从 ``url_map`` 反射出 ``(path, method, operation, path_params)``。"""
    out: List[Tuple[str, str, Dict[str, Any], List[Dict[str, Any]]]] = []
    if flask_app is None:
        return out
    seen = set()
    for rule in flask_app.url_map.iter_rules():
        raw = str(rule.rule)
        if not _should_reflect(raw, rule.endpoint):
            continue
        path, params = _rule_to_path(raw)
        # 同一 (path, method) 只保留第一条 —— 蓝图重复注册时 url_map 会出现
        # 两条一模一样的 rule，规格里出现两条会让生成物凭空多一倍
        for method in sorted(rule.methods or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            key = (path, method)
            if key in seen:
                continue
            seen.add(key)
            out.append((path, method, {
                "summary": _endpoint_summary(rule.endpoint, method),
                "tags": _endpoint_tags(rule.endpoint),
                "parameters": params,
                "responses": {
                    "200": {"description": "OK"},
                },
                "x-contract-source": "reflected",
            }, params))
    return out


def _endpoint_summary(endpoint: str, method: str) -> str:
    """从 endpoint 名反推一句可读摘要（反射拿不到 docstring 的场景是常态）。"""
    tail = str(endpoint or "").split(".")[-1]
    words = re.sub(r"^api[_-]?", "", tail, flags=re.I) or tail
    return "{} {}".format(method.upper(), words.replace("_", " ").strip() or tail)


def _endpoint_tags(endpoint: str) -> List[str]:
    """按 endpoint 前缀打标签，便于生成物按域分组。"""
    parts = str(endpoint or "").split(".")
    head = parts[0] if len(parts) > 1 else "misc"
    return [head or "misc"]


# --------------------------------------------------------------------------
# 手工 schema（contracts / delivery / licensing 三个新域）
# --------------------------------------------------------------------------

_ERROR_RESPONSE = {
    "type": "object",
    "required": ["success", "error", "code"],
    "properties": {
        "success": {"type": "boolean", "const": False},
        # ⚠ 与 app/failure_codes.py 同性质：这些中文文案是对外契约（R2）
        "error": {"type": "string"},
        "code": {"type": "string", "description": "稳定错误码，代码里以此分支，不要解析文案"},
    },
}


def _ref(name: str) -> Dict[str, Any]:
    return {"$ref": "#/components/schemas/{}".format(name)}


def _ok(schema: Dict[str, Any], description: str = "成功") -> Dict[str, Any]:
    return {"description": description,
            "content": {"application/json": {"schema": schema}}}


def _err(description: str) -> Dict[str, Any]:
    return {"description": description,
            "content": {"application/json": {"schema": _ref("ErrorResponse")}}}


#: 组件 schema。键即 ``#/components/schemas/<键>``。
SCHEMAS: Dict[str, Dict[str, Any]] = {
    "ErrorResponse": _ERROR_RESPONSE,
    "OkResponse": {
        "type": "object",
        "required": ["success"],
        "properties": {"success": {"type": "boolean", "const": True}},
    },
    "DeliveryPreset": {
        "type": "object",
        "required": ["preset_id", "label", "width", "height", "aspect_ratio"],
        "properties": {
            "preset_id": {"type": "string", "examples": ["portrait_9x16"]},
            "label": {"type": "string", "description": "面向用户的中文名"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "aspect_ratio": {"type": "string"},
            "orientation": {"type": "string"},
            "note": {"type": "string"},
        },
    },
    "DeliveryFile": {
        "type": "object",
        "required": ["rel_path", "size_bytes", "sha256"],
        "properties": {
            "rel_path": {"type": "string",
                         "description": "相对交付根目录的路径；跨机器可移植"},
            "size_bytes": {"type": "integer"},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$",
                       "description": "文件内容摘要；内容变则摘要变"},
            "mtime": {"type": "string"},
        },
    },
    "LicenseRequirement": {
        "type": "object",
        "description": "一次交付声明用到的模型与素材。门禁按它逐项核对授权登记。",
        "properties": {
            "models": {"type": "array", "items": {"type": "string"},
                       "description": "模型名称，需与 config/model-licensing.json 的 name 对上"},
            "assets": {"type": "array", "items": {"type": "string"}},
            "music": {"type": "array", "items": {"type": "string"}},
            "brand": {"type": "string"},
            "brand_version": {"type": "string"},
            "attribution_text": {"type": "string",
                                 "description": "交付方提供的署名文本；要求署名的授权缺它即拦"},
            "purpose": {"type": "string", "default": "commercial"},
        },
    },
    "GateViolation": {
        "type": "object",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string", "examples": ["LIC-MODEL-UNREGISTERED"]},
            "message": {"type": "string", "description": "面向用户的中文说明（对外契约）"},
            "subject": {"type": "string", "description": "被拦的对象（模型名/素材 id）"},
            "kind": {"type": "string"},
            "detail": {"type": "string"},
        },
    },
    "LicensingGateResult": {
        "type": "object",
        "required": ["ok", "violations"],
        "properties": {
            "ok": {"type": "boolean", "description": "false 时不得进入交付"},
            "violations": {"type": "array", "items": _ref("GateViolation")},
            "checked": {"type": "object"},
            "summary": {"type": "string"},
            "registry": {"type": "object"},
        },
    },
    "ApprovalView": {
        "type": "object",
        "required": ["state"],
        "properties": {
            # none / valid / stale / revoked / hash_missing
            "state": {"type": "string", "enum": ["none", "valid", "stale", "revoked", "hash_missing"]},
            "approver": {"type": "string"},
            "approved_at": {"type": "string"},
            "reason": {"type": "string"},
            "note": {"type": "string"},
        },
    },
    "DeliveryPackage": {
        "type": "object",
        "required": ["package_id", "project", "preset_id", "package_hash", "status"],
        "properties": {
            "package_id": {"type": "string"},
            "project": {"type": "string"},
            "episode_no": {"type": ["integer", "null"]},
            "preset": _ref("DeliveryPreset"),
            "package_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$",
                             "description": "人工批准绑定它；内容变则批准自动失效"},
            "current_package_hash": {"type": "string",
                                     "description": "按磁盘现状重算的包摘要；与 package_hash "
                                                    "不一致即说明文件被改动过"},
            "disk_ok": {"type": "boolean", "description": "清单里的文件是否都还在"},
            "disk_matches_baseline": {"type": "boolean",
                                      "description": "磁盘现状是否仍等于清单基线"},
            "file_count": {"type": "integer"},
            "total_bytes": {"type": "integer"},
            "files": {"type": "array", "items": _ref("DeliveryFile")},
            "licensing_ok": {"type": "boolean"},
            "verified_ok": {"type": "boolean",
                            "description": "机器校验结果；永远不等于已批准"},
            "verified_at": {"type": "string"},
            "approval": _ref("ApprovalView"),
            # built / verified / approved / approval_invalidated
            "status": {"type": "string",
                       "enum": ["built", "verified", "approved", "approval_invalidated"]},
            "created_at": {"type": "string"},
        },
    },
    "DeliveryManifest": {
        "type": "object",
        "required": ["package_id", "package_hash", "files"],
        "properties": {
            "schema_version": {"type": "integer"},
            "package_id": {"type": "string"},
            "project": {"type": "string"},
            "preset": _ref("DeliveryPreset"),
            "package_hash": {"type": "string"},
            "generated_at": {"type": "string"},
            "file_count": {"type": "integer"},
            "total_bytes": {"type": "integer"},
            "files": {"type": "array", "items": _ref("DeliveryFile")},
            "current_package_hash": {"type": "string"},
            "disk_matches_baseline": {"type": "boolean"},
            "licensing_ok": {"type": "boolean"},
            "verified_ok": {"type": "boolean"},
            "approval": _ref("ApprovalView"),
            "status": {"type": "string"},
            "note": {"type": "string"},
        },
    },
    "VerifyResult": {
        "type": "object",
        "required": ["ok", "checked", "violations"],
        "properties": {
            "ok": {"type": "boolean"},
            "checked": {"type": "integer"},
            "missing": {"type": "array", "items": {"type": "string"}},
            "mismatched": {"type": "array", "items": {"type": "object"}},
            "details": {"type": "array", "items": {"type": "object"}},
            "violations": {"type": "array", "items": _ref("GateViolation")},
        },
    },
    "LicenseRegistrySummary": {
        "type": "object",
        "properties": {
            "loaded": {"type": "boolean",
                       "description": "false 时门禁按最严口径阻断（fail-closed）"},
            "source_path": {"type": "string"},
            "schema_version": {"type": "integer"},
            "load_error": {"type": "string"},
            "model_count": {"type": "integer"},
            "asset_count": {"type": "integer"},
            "brand_count": {"type": "integer"},
        },
    },
    "ContractVersion": {
        "type": "object",
        "required": ["contract_version", "spec_hash"],
        "properties": {
            "contract_version": {"type": "string"},
            "spec_hash": {"type": "string",
                          "description": "规格内容摘要；前端启动闸门据此判断客户端是否过期"},
            "openapi": {"type": "string"},
            "path_count": {"type": "integer"},
            "operation_count": {"type": "integer"},
        },
    },
    # ---- 响应信封（命名而非内联，内联会让生成的 TS 退化成 Record<string, unknown>）----
    "OpenApiSpecResponse": {
        "type": "object",
        "required": ["openapi", "info", "paths"],
        "properties": {
            "openapi": {"type": "string"},
            "info": {"type": "object"},
            "paths": {"type": "object"},
            "components": {"type": "object"},
            "x-contract-version": {"type": "string"},
            "x-manual-paths": {"type": "array", "items": {"type": "string"}},
        },
    },
    "DeliveryPresetsResponse": {
        "type": "object",
        "required": ["success", "count", "presets"],
        "properties": {
            "success": {"type": "boolean"},
            "count": {"type": "integer"},
            "presets": {"type": "array", "items": _ref("DeliveryPreset")},
        },
    },
    "DeliveryPackageListResponse": {
        "type": "object",
        "required": ["success", "count", "packages"],
        "properties": {
            "success": {"type": "boolean"},
            "count": {"type": "integer"},
            "packages": {"type": "array", "items": _ref("DeliveryPackage")},
        },
    },
    "DeliveryPackageResponse": {
        "type": "object",
        "required": ["success", "package"],
        "properties": {
            "success": {"type": "boolean"},
            "package": _ref("DeliveryPackage"),
        },
    },
    "DeliveryManifestResponse": {
        "type": "object",
        "required": ["success", "manifest"],
        "properties": {
            "success": {"type": "boolean"},
            "manifest": _ref("DeliveryManifest"),
        },
    },
    "VerifyDeliveryResponse": {
        "type": "object",
        "required": ["success", "verify", "package"],
        "properties": {
            "success": {"type": "boolean",
                        "description": "与 verify.ok 同值：机器校验结论"},
            "verify": _ref("VerifyResult"),
            "package": _ref("DeliveryPackage"),
        },
    },
    "LicenseEntry": {
        "type": "object",
        "required": ["name", "source", "license_type"],
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "kind": {"type": "string", "description": "music / sfx / footage / image"},
            "source": {"type": "string"},
            "license_type": {"type": "string"},
            "commercial_use": {"type": "boolean"},
            "verified": {"type": "boolean",
                         "description": "false ⇒ 待核实，门禁以 LIC-LICENSE-UNVERIFIED 阻断"},
            "attribution_required": {"type": "boolean"},
            "attribution_text": {"type": "string",
                                 "description": "要求署名时，交付方复制到简介的文本"},
            "notes": {"type": "string"},
        },
    },
    "BrandKit": {
        "type": "object",
        "required": ["brand_id", "name"],
        "properties": {
            "brand_id": {"type": "string"},
            "name": {"type": "string"},
            "versions": {
                "type": "array",
                "description": "品牌与水印的**版本**列表；指定不存在的版本会被门禁拦下",
                "items": {
                    "type": "object",
                    "required": ["version"],
                    "properties": {
                        "version": {"type": "string"},
                        "watermark_text": {"type": "string"},
                        "watermark_position": {"type": "string"},
                        "watermark_opacity": {"type": "number"},
                        "enabled": {"type": "boolean"},
                        "updated_at": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                },
            },
        },
    },
    "LicenseRegistryResponse": {
        "type": "object",
        "required": ["success", "summary", "models", "assets", "brands"],
        "properties": {
            "success": {"type": "boolean"},
            "summary": _ref("LicenseRegistrySummary"),
            "license_types": {"type": "object",
                              "description": "许可类型 → {commercial, attribution, label}"},
            "models": {"type": "array", "items": _ref("LicenseEntry")},
            "assets": {"type": "array", "items": _ref("LicenseEntry")},
            "brands": {"type": "array", "items": _ref("BrandKit")},
        },
    },
    "LicensedMusicResponse": {
        "type": "object",
        "required": ["success", "count", "music"],
        "properties": {
            "success": {"type": "boolean"},
            "count": {"type": "integer"},
            "music": {"type": "array", "items": _ref("LicenseEntry")},
            "summary": _ref("LicenseRegistrySummary"),
        },
    },
    "BrandKitsResponse": {
        "type": "object",
        "required": ["success", "count", "brands"],
        "properties": {
            "success": {"type": "boolean"},
            "count": {"type": "integer"},
            "brands": {"type": "array", "items": _ref("BrandKit")},
            "summary": _ref("LicenseRegistrySummary"),
        },
    },
    "LicensingGateResponse": {
        "type": "object",
        "required": ["success", "gate"],
        "properties": {
            "success": {"type": "boolean", "description": "与 gate.ok 同值"},
            "gate": _ref("LicensingGateResult"),
            "attribution": {"type": "array", "items": {"type": "string"},
                            "description": "可复制到简介的署名文本"},
        },
    },
    "CreateDeliveryPackageRequest": {
        "type": "object",
        "required": ["project_name", "preset_id"],
        "properties": {
            "project_name": {"type": "string"},
            "preset_id": {"type": "string",
                          "description": "portrait_9x16 / landscape_16x9 / portrait_3x4"},
            "episode_no": {"type": ["integer", "null"]},
            "requirement": _ref("LicenseRequirement"),
        },
    },
    "ApproveDeliveryPackageRequest": {
        "type": "object",
        "required": ["approver"],
        "properties": {
            "approver": {"type": "string", "description": "批准人；为空一律拒绝"},
            "note": {"type": "string"},
            "package_hash": {"type": "string",
                             "description": "可选：批准时确认的包摘要，与当前不一致则拒绝"},
        },
    },
    "RevokeDeliveryApprovalRequest": {
        "type": "object",
        "properties": {
            "operator": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
    "ErrorCodesResponse": {
        "type": "object",
        "required": ["success", "delivery", "licensing"],
        "properties": {
            "success": {"type": "boolean"},
            # 错误码 → 中文说明。前端按 key（码）分支，不解析 value（文案）。
            "delivery": {"type": "object",
                         "additionalProperties": {"type": "string"}},
            "licensing": {"type": "object",
                          "additionalProperties": {"type": "string"}},
        },
    },
    "ContractVersionResponse": {
        "type": "object",
        "required": ["success", "contract_version", "spec_hash"],
        "properties": {
            "success": {"type": "boolean"},
            "contract_version": {"type": "string"},
            "openapi": {"type": "string"},
            "spec_hash": {"type": "string"},
            "path_count": {"type": "integer"},
            "operation_count": {"type": "integer"},
            "reflected_operations": {"type": "integer"},
            "manual_operations": {"type": "integer"},
            "manual_paths": {"type": "array", "items": {"type": "string"}},
        },
    },
}

# =============================================================================
# 2026-10-07 收口补齐：production_facts / timeline / jobs / styles 四域的 schema
# =============================================================================
# 这四个域此前「能调通但规格里没有」—— 前端要接只能继续手写 URL 字符串，
# 契约优先在这四域上等于没做。补齐时遵守两条：
#   1. **响应信封一律提为命名 schema**（``{"$ref": ...}``），否则生成的 TS
#      会退化成 ``Record<string, unknown>``，等于没有类型；
#   2. 「采用 ≠ 批准」的三态（selected / selected_not_approved / approved…）
#      在 schema 里写成**枚举**，让类型系统先把「采用即批准」这个 bug 挡掉。

_DECISION_STATES = {
    "type": "string",
    "description": "采用/批准五态。铁律：selected 永不显示为 approved（ADR-0002）",
    "enum": ["selected", "selected_not_approved", "approved",
             "approved_stale", "approved_unverified", "none"],
}


def _list_of(item_ref: str, extra: dict | None = None) -> dict:
    """列表响应信封：``{"success": true, "items": [...], "total": n}``。"""
    node = {
        "type": "object",
        "required": ["success", "items"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "items": {"type": "array", "items": {"$ref": item_ref}},
            "total": {"type": "integer"},
        },
    }
    if extra:
        node["properties"].update(extra)
    return node


def _one_of(item_ref: str, extra: dict | None = None) -> dict:
    """单条响应信封。"""
    node = {
        "type": "object",
        "required": ["success"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "item": {"$ref": item_ref},
        },
    }
    if extra:
        node["properties"].update(extra)
    return node


# 自由字典（键由调用方决定）—— 只允许出现在 schema 的**属性**位置，
# 绝不能出现在**信封**位置，否则信封退化等于没写契约。
_FREEFORM = {"type": "object", "additionalProperties": True}

SCHEMAS.update({
    # ---------------- production_facts ----------------
    "GenerationIntent": {
        "type": "object",
        "description": "一次生成的意图。冻结后不可改，改动必须派生新 intent。",
        "required": ["intent_id", "intent_hash"],
        "properties": {
            "intent_id": {"type": "string"},
            "intent_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "project": {"type": "string"},
            "prompt": {"type": "string"},
            "ref_slots": {"type": "array", "items": _FREEFORM},
            "seed": {"type": "integer"},
            "profile": {"type": "string"},
            "workflow_version": {"type": "string"},
            "derived_from": {"type": "string"},
            "frozen": {"type": "boolean", "const": True},
            "created_at": {"type": "string"},
        },
    },
    "CreateIntentRequest": {
        "type": "object",
        "required": ["project", "episode", "shot_key", "prompt"],
        "properties": {
            "project": {"type": "string"},
            "episode": {"type": ["string", "integer"]},
            "shot_key": {"type": "string"},
            "prompt": {"type": "string"},
            "kind": {"type": "string"},
            "negative_prompt": {"type": "string"},
            "ref_slots": {"type": "array", "items": _FREEFORM},
            "seed": {"type": "integer"},
            "profile_id": {"type": "string"},
            "profile_version": {"type": ["string", "integer"]},
            "workflow_version": {"type": "string"},
            "workflow_hash": {"type": "string"},
            "created_by": {"type": "string"},
            "derived_from": {"type": "string"},
        },
    },
    "IntentResponse": _one_of("GenerationIntent", {"state": _DECISION_STATES}),
    "IntentListResponse": _list_of("GenerationIntent"),
    "MediaVersion": {
        "type": "object",
        "description": "一次生成产出的候选。自身**不带** selected/approved —— "
                       "那两个是独立的决策记录（ADR-0002）。",
        "required": ["media_version_id", "media_sha256"],
        "properties": {
            "media_version_id": {"type": "string"},
            "intent_id": {"type": "string"},
            "path": {"type": "string",
                     "description": "磁盘路径；批准时会按它重算 sha256 以判是否被覆盖"},
            "media_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "kind": {"type": "string", "enum": ["image", "video", "audio"]},
            "ffprobe": _FREEFORM,
            "created_at": {"type": "string"},
        },
    },
    "RegisterMediaRequest": {
        "type": "object",
        "required": ["intent_id", "path"],
        "description": "服务端按 path 重算 sha256，**不接受客户端自带摘要**"
                       "（否则可伪造出「已核验」的假象）。",
        "properties": {
            "intent_id": {"type": "string"},
            "path": {"type": "string"},
            "kind": {"type": "string", "enum": ["image", "video", "audio"]},
        },
    },
    "MediaVersionResponse": _one_of("MediaVersion", {"state": _DECISION_STATES}),
    "MediaVersionListResponse": _list_of("MediaVersionResponse"),
    "SelectMediaRequest": {
        "type": "object",
        "description": "采用这版。创作决定 —— **不会**自动产生批准。",
        "properties": {
            "selected_by": {"type": "string"},
            "note": {"type": "string"},
        },
    },
    "ApproveMediaRequest": {
        "type": "object",
        "required": ["authorized_by"],
        "description": "批准放行。人工决定；`authorized_by` 必须是人工主体，"
                       "机器账号一律 403。必须先采用过（否则 409）。",
        "properties": {
            "authorized_by": {"type": "string"},
            "note": {"type": "string"},
            "authorization_ref": {"type": "string",
                                  "description": "审计引用号；不是身份凭证，不能覆盖主体校验"},
        },
    },
    "RevokeApprovalRequest": {
        "type": "object",
        "required": ["revoked_by", "reason"],
        "properties": {
            "revoked_by": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
    "DecisionRecord": {
        "type": "object",
        "properties": {
            "selection_id": {"type": "string"},
            "approval_id": {"type": "string"},
            "media_version_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["selection", "approval"]},
            "actor": {"type": "string"},
            "note": {"type": "string"},
            "created_at": {"type": "string"},
            "revoked": {"type": "boolean"},
            "bound_hash": {"type": "string",
                           "description": "批准绑定的三元组哈希；内容变则批准自动失效"},
        },
    },
    "DecisionResponse": {
        "type": "object",
        "required": ["success", "state"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "state": _DECISION_STATES,
            "selection": {"$ref": "DecisionRecord"},
            "approval": {"$ref": "DecisionRecord"},
            "media_verified": {
                "type": "boolean",
                "description": "是否真的按磁盘现状核验过。false = 无法验证，"
                               "**不得**当成「没变」。",
            },
        },
    },
    "DecisionListResponse": _list_of("DecisionResponse"),
    "CapabilityProfileVersion": {
        "type": "object",
        "required": ["profile_id", "version"],
        "properties": {
            "profile_id": {"type": "string"},
            "version": {"type": "string"},
            "capabilities": {"type": "array", "items": _FREEFORM},
            "verified": {"type": "boolean"},
            "created_at": {"type": "string"},
        },
    },
    "CapabilityProfileRequest": {
        "type": "object",
        "required": ["profile_id", "version"],
        "properties": {
            "profile_id": {"type": "string"},
            "version": {"type": "string"},
            "capabilities": {"type": "array", "items": _FREEFORM},
            "verified": {"type": "boolean"},
        },
    },
    "CapabilityProfileResponse": _one_of("CapabilityProfileVersion"),
    "CapabilityProfileListResponse": _list_of("CapabilityProfileVersion"),

    # ---------------- timeline ----------------
    "TimelineItem": {
        "type": "object",
        "required": ["shot_key", "media_version_id"],
        "properties": {
            "index": {"type": "integer"},
            "shot_key": {"type": "string"},
            "media_version_id": {"type": "string",
                                 "description": "采用的媒体版本；引用未批准/已失效的版本会阻断"},
            "duration_sec": {"type": "number"},
            "transition_in": {"type": "string"},
            "audio_media_version_id": {"type": "string"},
            "declared_silence": {
                "type": "boolean",
                "description": "合法静音必须**前置声明**。未声明却在渲染后出现静音 → 阻断。",
            },
            "silence_reason": {"type": "string"},
            "note": {"type": "string"},
        },
    },
    "TimelineRevision": {
        "type": "object",
        "description": "不可变时间线。创建后不可原地改；改内容必须派生新 revision。",
        "required": ["revision_id", "project"],
        "properties": {
            "revision_id": {"type": "string"},
            "project": {"type": "string"},
            "episode": {"type": "integer"},
            "subtitle_revision": {"type": "string",
                                  "description": "字幕版本与时间线解耦，仅在此引用"},
            "items": {"type": "array", "items": {"$ref": "TimelineItem"}},
            "revision_no": {"type": "integer"},
            "derived_from": {"type": "string"},
            "created_at": {"type": "string"},
        },
    },
    "CreateTimelineRevisionRequest": {
        "type": "object",
        "required": ["project", "items"],
        "properties": {
            "project": {"type": "string"},
            "episode": {"type": "integer"},
            "subtitle_revision": {"type": "string"},
            "items": {"type": "array", "items": {"$ref": "TimelineItem"}},
            "derived_from": {"type": "string"},
        },
    },
    "TimelineRevisionResponse": _one_of("TimelineRevision"),
    "TimelineRevisionListResponse": _list_of("TimelineRevision"),
    "PreflightReport": {
        "type": "object",
        "required": ["ok", "compose_fingerprint"],
        "properties": {
            "ok": {"type": "boolean"},
            "compose_fingerprint": {
                "type": "string", "pattern": "^[0-9a-f]{64}$",
                "description": "内容指纹：不含 revision_id/created_at，"
                               "所以「同一批镜头换机器重渲」不会误判为内容变化。",
            },
            "issues": {"type": "array", "items": _FREEFORM},
            "undeclared_silence": {"type": "array", "items": _FREEFORM},
        },
    },
    "PreflightResponse": {
        "type": "object",
        "required": ["success", "preflight"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "preflight": {"$ref": "PreflightReport"},
        },
    },
    "ReleaseCheckRequest": {
        "type": "object",
        "description": "发布检查不接受客户端授权结论；服务端按 requirement 重新评估。",
        "properties": {},
    },
    "ReleaseCheckResponse": {
        "type": "object",
        "required": ["success", "release"],
        "properties": {
            "success": {"type": "boolean"},
            "release": {
                "type": "object",
                "required": ["ok", "blockers"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "blockers": {"type": "array", "items": {"type": "string"}},
                    "approval_state": {"type": "string"},
                    "licensing_ok": {"type": "boolean"},
                    "verified_ok": {"type": "boolean"},
                    "disk_ok": {"type": "boolean"},
                },
            },
            "package": {"type": "object"},
        },
    },
    "RenderManifest": {
        "type": "object",
        "description": "渲染清单。合法静音在此**前置声明**，而不是渲染后才发现。",
        "required": ["compose_fingerprint"],
        "properties": {
            "compose_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "renderer": {"type": "string"},
            "segments": {"type": "array", "items": _FREEFORM},
            "declared_silences": {"type": "array", "items": _FREEFORM},
            "subtitle_revision": {"type": "string"},
        },
    },
    "RenderManifestResponse": {
        "type": "object",
        "required": ["success", "manifest"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "manifest": {"$ref": "RenderManifest"},
        },
    },
    "VerifyRenderRequest": {
        "type": "object",
        "required": ["probed"],
        "description": "渲完结果核验：传 ffprobe 结果，不登记 EpisodeRenderVersion。",
        "properties": {
            "probed": {"type": "object", "additionalProperties": True},
            "tolerance_sec": {"type": "number", "minimum": 0},
        },
    },
    "VerifyRenderResponse": {
        "type": "object",
        "required": ["success", "ok", "reason", "compose_fingerprint", "planned_duration_sec"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "ok": {"type": "boolean"},
            "reason": {"type": "string"},
            "compose_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "planned_duration_sec": {"type": "number"},
        },
    },
    "EpisodeRenderVersion": {
        "type": "object",
        "required": ["render_id", "revision_id", "compose_fingerprint", "plan_fingerprint"],
        "properties": {
            "render_id": {"type": "string"},
            "revision_id": {"type": "string"},
            "project": {"type": "string"},
            "episode": {"type": "string"},
            "revision_no": {"type": "integer"},
            "compose_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "plan_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "output_path": {"type": "string"},
            "output_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "output_bytes": {"type": "integer"},
            "output_duration_sec": {"type": "number"},
            "subtitle_revision": {"type": "string"},
            "entry_media_version_ids": {"type": "array", "items": {"type": "string"}},
            "declared_silences": {"type": "integer"},
            "authorized_by": {"type": "string"},
            "authorization_ref": {"type": "string"},
            "created_at": {"type": "string"},
            "note": {"type": "string"},
            "bind_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    },
    "RenderVersionListResponse": {
        "type": "object",
        "required": ["success", "renders", "count"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "renders": {"type": "array", "items": _ref("EpisodeRenderVersion")},
            "count": {"type": "integer"},
        },
    },
    "RenderVersionResponse": {
        "type": "object",
        "required": ["success", "render_version"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "render_version": {"$ref": "EpisodeRenderVersion"},
        },
    },
    "RenderRequest": {
        "type": "object",
        "required": ["authorized_by"],
        "description": "按冻结计划发起渲染。``authorized_by`` 必须是人工主体。",
        "properties": {
            "authorized_by": {"type": "string",
                              "description": "机器账号一律 403（复用 require_human_authorization）"},
            "authorization_ref": {"type": "string"},
            "output_path": {"type": "string"},
            "dry_run": {"type": "boolean",
                        "description": "只做预检不真渲染；用于接上游合成计划的安全接入"},
            "preview": {"type": "boolean"},
            "renderer": {"type": "string"},
            "preset": {"type": "string"},
            "note": {"type": "string"},
            "target_w": {"type": "integer"},
            "target_h": {"type": "integer"},
            "target_fps": {"type": "integer"},
        },
    },

    # ---------------- jobs ----------------
    "Job": {
        "type": "object",
        "description": "Job = 一次用户意图；Attempt = 一次执行尝试。",
        "required": ["job_id"],
        "properties": {
            "job_id": {"type": "string"},
            "project": {"type": "string"},
            "kind": {"type": "string"},
            "status": {"type": "string",
                       "enum": ["pending", "running", "completed", "failed", "cancelled"]},
            "created_at": {"type": "string"},
            "updated_at": {"type": "string"},
        },
    },
    "Attempt": {
        "type": "object",
        "required": ["attempt_id", "job_id"],
        "properties": {
            "attempt_id": {"type": "string"},
            "job_id": {"type": "string"},
            "seq": {"type": "integer"},
            "status": {"type": "string",
                       "enum": ["pending", "running", "completed", "failed", "cancelled"]},
            "workflow_hash": {
                "type": "string",
                "description": "ComfyUI 工作流指纹；一致即可免重渲复用产物。",
            },
            "started_at": {"type": "string"},
            "finished_at": {"type": "string"},
        },
    },
    "CreateJobRequest": {
        "type": "object",
        "required": ["project", "kind"],
        "properties": {
            "project": {"type": "string"},
            "kind": {"type": "string"},
            "payload": _FREEFORM,
        },
    },
    "UpdateJobRequest": {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string"}, "note": {"type": "string"}},
    },
    "CreateAttemptRequest": {
        "type": "object",
        "properties": {"workflow_hash": {"type": "string"}, "note": {"type": "string"}},
    },
    "UpdateAttemptRequest": {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string"}, "note": {"type": "string"}},
    },
    "FinishAttemptRequest": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "result_ref": {"type": "string"},
            "note": {"type": "string"},
        },
    },
    "JobResponse": _one_of("Job"),
    "JobListResponse": _list_of("Job"),
    "AttemptResponse": _one_of("Attempt"),
    "PendingUnit": {
        "type": "object",
        "properties": {
            "unit_key": {"type": "string"},
            "done": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    },
    "PendingUnitListResponse": _list_of("PendingUnit"),
    "ReuseHintResponse": {
        "type": "object",
        "required": ["success"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "reusable": {"type": "boolean"},
            "workflow_hash": {"type": "string"},
            "reason": {"type": "string",
                       "description": "为何不可复用；workflow_hash 不一致 / 产物缺失"},
        },
    },
    "JobStats": {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "by_status": {"type": "object", "additionalProperties": {"type": "integer"}},
            "by_project": {"type": "object", "additionalProperties": {"type": "integer"}},
        },
    },
    "JobStatsResponse": {
        "type": "object",
        "required": ["success", "stats"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "stats": {"$ref": "JobStats"},
        },
    },

    # ---------------- styles ----------------
    "StyleEntry": {
        "type": "object",
        "description": "style_id 是**稳定标识**，label 可改名而 id 永不变"
                       "（否则改名即断历史，ADR-0008）。",
        "required": ["style_id", "label", "category"],
        "properties": {
            "style_id": {"type": "string",
                         "description": "纯 ASCII，与缩略图文件名刻意解耦"},
            "label": {"type": "string", "description": "写入 config.style 的字面值"},
            "category": {"type": "string", "enum": ["2d", "3d", "real"]},
            "thumbnail": {"type": "string"},
            "aspect_default": {"type": "string"},
            "positive_suffix": {"type": "string"},
            "negative_suffix": {"type": "array", "items": {"type": "string"}},
            "aliases": {"type": "array", "items": {"type": "string"}},
        },
    },
    "StyleListResponse": _list_of("StyleEntry", {
        "aspect_presets": {"type": "array", "items": _FREEFORM},
        "categories": {"type": "array", "items": _FREEFORM},
    }),
    "StyleResponse": _one_of("StyleEntry"),
    "StyleResolveResponse": {
        "type": "object",
        "required": ["success", "resolved"],
        "properties": {
            "success": {"type": "boolean", "const": True},
            "resolved": {"type": "boolean",
                         "description": "false = 未命中。调用方**必须**继续按自由文本处理，"
                                        "不得凭空猜一个风格。"},
            "style_id": {"type": "string"},
            "match_kind": {"type": "string", "enum": ["id", "label", "alias", "substring"]},
        },
    },
})


def _op(operation_id: str, summary: str, tags: List[str], *,
        params: Optional[List[Dict[str, Any]]] = None,
        request_body: Optional[Dict[str, Any]] = None,
        ok_schema: Optional[Dict[str, Any]] = None,
        errors: Optional[List[Tuple[int, str]]] = None,
        ) -> Dict[str, Any]:
    """构造一个带 ``x-contract-source: manual`` 的 operation。"""
    responses: Dict[str, Any] = {"200": _ok(ok_schema or _ref("OkResponse"))}
    for code, desc in (errors or []):
        responses[str(code)] = _err(desc)
    node: Dict[str, Any] = {
        "operationId": operation_id,
        "summary": summary,
        "tags": tags,
        "responses": responses,
        "x-contract-source": "manual",
    }
    if params:
        node["parameters"] = params
    if request_body:
        node["requestBody"] = request_body
    return node


def _path_param(name: str, schema: Dict[str, Any], description: str = "") -> Dict[str, Any]:
    node = {"name": name, "in": "path", "required": True, "schema": schema}
    if description:
        node["description"] = description
    return node


def _json_body(schema: Dict[str, Any], required: bool = True) -> Dict[str, Any]:
    return {"required": required,
            "content": {"application/json": {"schema": schema}}}


_PACKAGE_PARAM = _path_param("package_id", {"type": "string"}, "交付包 id")


def _build_manual_paths() -> Dict[str, Dict[str, Any]]:
    """手工补 schema 的路径集合。

    覆盖本轮新增的全部七个域：contracts / delivery / licensing（首批）
    + production_facts / timeline / jobs / styles（2026-10-07 收口补齐）。

    ⚠️ 结构注意：先构造 `d`（dict），再 `d.update({...})` 追加后四域，
    **最后才 return**。曾把 `return {...}` 写在这里，导致后面的
    ``d.update`` 块成为**永不执行的死代码** —— 而外部只看
    ``len(manual_paths())`` 根本发现不了（少的是「少了几条 schema」，
    不是「报错」）。新增域时务必保持「构造 → 追加 → 返回」的顺序。
    """
    d = ["delivery"]
    d0 = {
        # ---------------- contracts ----------------
        "/api/contracts/openapi.json": {
            "get": _op("getOpenapiSpec", "导出 OpenAPI 3.1 规格（契约本体）", ["contracts"],
                       ok_schema=_ref("OpenApiSpecResponse"))},
        "/api/contracts/version": {
            "get": _op("getContractVersion", "契约版本与规格摘要（前端启动闸门用）",
                       ["contracts"], ok_schema=_ref("ContractVersionResponse"))},
        "/api/contracts/error-codes": {
            "get": _op("getErrorCodes", "交付与授权的错误码对照表", ["contracts"],
                       ok_schema=_ref("ErrorCodesResponse"))},
        # ---------------- delivery ----------------
        "/api/delivery/presets": {
            "get": _op("listDeliveryPresets", "交付预设列表（竖屏 / 横屏 / 3:4）", d,
                       ok_schema=_ref("DeliveryPresetsResponse"))},
        "/api/delivery/packages": {
            "get": _op("listDeliveryPackages", "交付包列表（含机器校验与人工批准状态）", d,
                       params=[{"name": "project", "in": "query", "required": False,
                                "schema": {"type": "string"},
                                "description": "按项目过滤；缺省返回全部"}],
                       ok_schema=_ref("DeliveryPackageListResponse")),
            "post": _op("createDeliveryPackage", "登记交付包（扫描既有导出产物并算 SHA-256）",
                        d, request_body=_json_body(_ref("CreateDeliveryPackageRequest")),
                        ok_schema=_ref("DeliveryPackageResponse"),
                        errors=[(400, "参数非法"),
                                (403, "授权门禁未通过，未登记授权的模型/素材不得进入交付"),
                                (422, "导出目录内没有任何交付文件")]),
        },
        "/api/delivery/packages/{package_id}": {
            "get": _op("getDeliveryPackage", "交付包详情（含文件清单与 SHA-256）", d,
                       params=[_PACKAGE_PARAM],
                       ok_schema=_ref("DeliveryPackageResponse"),
                       errors=[(404, "交付包不存在")]),
        },
        "/api/delivery/packages/{package_id}/release-check": {
            "post": _op("releaseCheckDeliveryPackage", "交付发布总门禁（授权+校验+人工批准+磁盘）",
                        d, params=[_path_param("package_id", {"type": "string"}, "交付包 id")],
                        request_body=_json_body(_ref("ReleaseCheckRequest"), required=False),
                        ok_schema=_ref("ReleaseCheckResponse"),
                        errors=[(404, "交付包不存在")]),
        },
        "/api/delivery/packages/{package_id}/manifest": {
            "get": _op("getDeliveryManifest", "交付清单（逐文件 SHA-256 + 包摘要）", d,
                       params=[_PACKAGE_PARAM],
                       ok_schema=_ref("DeliveryManifestResponse"),
                       errors=[(404, "交付包不存在")]),
        },
        "/api/delivery/packages/{package_id}/verify": {
            "post": _op("verifyDeliveryPackage", "机器校验：逐文件重算 SHA-256", d,
                        params=[_PACKAGE_PARAM],
                        ok_schema=_ref("VerifyDeliveryResponse"),
                        errors=[(404, "交付包不存在")]),
        },
        "/api/delivery/packages/{package_id}/approve": {
            "post": _op("approveDeliveryPackage", "人工批准（须带操作者，批准与包哈希绑定）",
                        d, params=[_PACKAGE_PARAM],
                        request_body=_json_body(_ref("ApproveDeliveryPackageRequest")),
                        ok_schema=_ref("DeliveryPackageResponse"),
                        errors=[(400, "缺少操作者 / 包摘要不匹配"),
                                (404, "交付包不存在")]),
        },
        "/api/delivery/packages/{package_id}/revoke": {
            "post": _op("revokeDeliveryApproval", "撤销人工批准", d, params=[_PACKAGE_PARAM],
                        request_body=_json_body(_ref("RevokeDeliveryApprovalRequest")),
                        ok_schema=_ref("DeliveryPackageResponse"),
                        errors=[(404, "交付包不存在")]),
        },
        # ---------------- licensing ----------------
        "/api/licensing/registry": {
            "get": _op("getLicenseRegistry", "授权登记表全量（模型 / 素材 / 品牌）",
                       ["licensing"], ok_schema=_ref("LicenseRegistryResponse")),
        },
        "/api/licensing/music": {
            "get": _op("listLicensedMusic", "音乐库授权列表（CC BY 署名可直接复制）",
                       ["licensing"], ok_schema=_ref("LicensedMusicResponse")),
        },
        "/api/licensing/brands": {
            "get": _op("listBrandKits", "品牌与水印版本列表", ["licensing"],
                       ok_schema=_ref("BrandKitsResponse")),
        },
        "/api/licensing/gate": {
            "post": _op("evaluateLicensingGate", "授权门禁预检（交付前自查）", ["licensing"],
                        request_body=_json_body(_ref("LicenseRequirement"), required=False),
                        ok_schema=_ref("LicensingGateResponse"),
                        errors=[(400, "参数非法")]),
        },
    }
    d = d0
    # ---- 2026-10-07 收口补齐：另四个新增域 ----
    # 下面这些域此前**不在**契约里 —— 端点能调通，但规格与生成物里都没有，
    # 前端要接只能继续手写 URL 字符串。四者的 schema 与实现同时生效，
    # 因此与前面三域一样适用「承诺过的一定写到」的 drift 自检。
    pf = ["production_facts"]
    tl = ["timeline"]
    jb = ["jobs"]
    st = ["styles"]

    _intent_id = _path_param("intent_id", {"type": "string"}, "生成意图 id")
    _media_id = _path_param("media_version_id", {"type": "string"}, "媒体版本 id")
    _revision_id = _path_param("revision_id", {"type": "string"}, "时间线 revision id")
    _job_id = _path_param("job_id", {"type": "string"}, "作业 id")
    _attempt_id = _path_param("attempt_id", {"type": "string"}, "尝试 id")
    _style_id = _path_param("style_id", {"type": "string"}, "稳定风格 id（改名不变）")

    _q = lambda name, desc: {          # noqa: E731  局部简写，仅用于下面的查询参数
        "name": name, "in": "query", "required": False, "schema": {"type": "string"},
        "description": desc,
    }

    d.update({
        # ---------------- production_facts ----------------
        "/api/production_facts/intents": {
            "get": _op("listProductionIntents", "生成意图列表（冻结后的生成事实）", pf,
                       params=[_q("project", "按项目过滤"),
                               _q("intent_id", "按意图过滤")],
                       ok_schema=_ref("IntentListResponse")),
            "post": _op("createProductionIntent", "登记生成意图（冻结：此后不可改）", pf,
                        request_body=_json_body(_ref("CreateIntentRequest")),
                        ok_schema=_ref("IntentResponse"),
                        errors=[(400, "参数非法")]),
        },
        "/api/production_facts/intents/{intent_id}": {
            "get": _op("getProductionIntent", "生成意图详情", pf, params=[_intent_id],
                       ok_schema=_ref("IntentResponse"),
                       errors=[(404, "意图不存在")]),
        },
        "/api/production_facts/intents/{intent_id}/derive": {
            "post": _op("deriveProductionIntent", "派生新意图（冻结意图不可原地改）", pf,
                        params=[_intent_id],
                        ok_schema=_ref("IntentResponse"),
                        errors=[(404, "意图不存在"), (409, "无实质变更，不予派生")]),
        },
        "/api/production_facts/media": {
            "get": _op("listMediaVersions", "候选媒体版本列表（含采用/批准状态）", pf,
                       params=[_q("intent_id", "按意图过滤"),
                               _q("include_state", "是否带上 decision_state 五态")],
                       ok_schema=_ref("MediaVersionListResponse")),
            "post": _op("registerMediaVersion", "登记一次生成产出的候选", pf,
                        request_body=_json_body(_ref("RegisterMediaRequest")),
                        ok_schema=_ref("MediaVersionResponse"),
                        errors=[(400, "参数非法")]),
        },
        "/api/production_facts/media/{media_version_id}": {
            "get": _op("getMediaVersion", "媒体版本详情", pf, params=[_media_id],
                       ok_schema=_ref("MediaVersionResponse"),
                       errors=[(404, "媒体版本不存在")]),
        },
        "/api/production_facts/media/{media_version_id}/select": {
            "post": _op("selectMediaVersion", "采用这版（创作决定；永不隐式升级为批准）", pf,
                        params=[_media_id],
                        request_body=_json_body(_ref("SelectMediaRequest")),
                        ok_schema=_ref("DecisionResponse"),
                        errors=[(404, "媒体版本不存在")]),
        },
        "/api/production_facts/media/{media_version_id}/approve": {
            "post": _op("approveMediaVersion", "批准放行（人工决定；须先采用）", pf,
                        params=[_media_id],
                        request_body=_json_body(_ref("ApproveMediaRequest")),
                        ok_schema=_ref("DecisionResponse"),
                        errors=[(400, "缺少授权者"),
                                (403, "授权者非人工主体（机器账号一律拒绝）"),
                                (404, "媒体版本不存在"),
                                (409, "尚未采用 —— 采用不等于批准")]),
        },
        "/api/production_facts/decisions": {
            "get": _op("listDecisions", "采用/批准决策列表（decision_state 五态）", pf,
                       params=[_q("media_version_id", "按媒体版本过滤")],
                       ok_schema=_ref("DecisionListResponse")),
        },
        "/api/production_facts/decisions/history": {
            "get": _op("listDecisionHistory", "决策变更历史（追加式，不覆盖）", pf,
                       params=[_q("media_version_id", "按媒体版本过滤")],
                       ok_schema=_ref("DecisionListResponse")),
        },
        "/api/production_facts/approvals/{approval_id}/revoke": {
            "post": _op("revokeProductionApproval", "撤销人工批准（须人工授权者）", pf,
                        params=[_path_param("approval_id", {"type": "string"}, "批准记录 id")],
                        request_body=_json_body(_ref("RevokeApprovalRequest")),
                        ok_schema=_ref("DecisionResponse"),
                        errors=[(403, "撤销者非人工主体"),
                                (404, "批准记录不存在")]),
        },
        "/api/production_facts/capability-profiles": {
            "get": _op("listCapabilityProfiles", "能力档案版本列表", pf,
                       ok_schema=_ref("CapabilityProfileListResponse")),
            "post": _op("registerCapabilityProfile", "登记能力档案版本", pf,
                        request_body=_json_body(_ref("CapabilityProfileRequest")),
                        ok_schema=_ref("CapabilityProfileResponse"),
                        errors=[(400, "参数非法")]),
        },
        # ---------------- timeline ----------------
        "/api/timeline/revisions": {
            "get": _op("listTimelineRevisions", "时间线 revision 列表（不可变）", tl,
                       params=[_q("project", "按项目过滤")],
                       ok_schema=_ref("TimelineRevisionListResponse")),
            "post": _op("createTimelineRevision", "冻结一个时间线 revision", tl,
                        request_body=_json_body(_ref("CreateTimelineRevisionRequest")),
                        ok_schema=_ref("TimelineRevisionResponse"),
                        errors=[(400, "参数非法 / 静音未显式声明")]),
        },
        "/api/timeline/revisions/{revision_id}": {
            "get": _op("getTimelineRevision", "时间线 revision 详情", tl,
                       params=[_revision_id], ok_schema=_ref("TimelineRevisionResponse"),
                       errors=[(404, "revision 不存在")]),
        },
        "/api/timeline/revisions/{revision_id}/derive": {
            "post": _op("deriveTimelineRevision", "由既有 revision 派生新版本（不可原地改）", tl,
                        params=[_revision_id], ok_schema=_ref("TimelineRevisionResponse"),
                        errors=[(404, "revision 不存在"), (409, "无实质变更，不予派生")]),
        },
        "/api/timeline/revisions/{revision_id}/preflight": {
            "get": _op("preflightTimelineRevision", "合成预检（含 compose_fingerprint）", tl,
                       params=[_revision_id],
                       ok_schema=_ref("PreflightResponse"),
                       errors=[(404, "revision 不存在"),
                               (409, "存在未声明的合法静音（undeclared_silence）")]),
        },
        "/api/timeline/revisions/{revision_id}/manifest": {
            "get": _op("getTimelineManifest", "渲染清单（合法静音必须前置声明）", tl,
                       params=[_revision_id], ok_schema=_ref("RenderManifestResponse"),
                       errors=[(404, "revision 不存在")]),
        },
        "/api/timeline/revisions/{revision_id}/verify-render": {
            "post": _op("verifyTimelineRender", "核验渲染结果与时长（不登记 EpisodeRenderVersion）",
                        tl, params=[_revision_id],
                        request_body=_json_body(_ref("VerifyRenderRequest")),
                        ok_schema=_ref("VerifyRenderResponse"),
                        errors=[(404, "revision 不存在")]),
        },
        "/api/timeline/revisions/{revision_id}/renders": {
            "get": _op("listTimelineRenderVersions", "列出该 revision 的成片登记", tl,
                       params=[_revision_id],
                       ok_schema=_ref("RenderVersionListResponse"),
                       errors=[(404, "revision 不存在")]),
        },
        "/api/timeline/renders/{render_id}": {
            "get": _op("getTimelineRenderVersion", "读取成片登记（含指纹与 sha256）", tl,
                       params=[_path_param("render_id", {"type": "string"}, "渲染登记 id")],
                       ok_schema=_ref("RenderVersionResponse"),
                       errors=[(404, "成片登记不存在")]),
        },
        "/api/timeline/renders/by-fingerprint/{compose_fingerprint}": {
            "get": _op("findTimelineRenderVersions", "按剪辑指纹反查成片登记", tl,
                       params=[_path_param("compose_fingerprint", {"type": "string"}, "剪辑指纹")],
                       ok_schema=_ref("RenderVersionListResponse")),
        },
        "/api/timeline/revisions/{revision_id}/render": {
            "post": _op("renderTimelineRevision",
                        "按**冻结计划**渲染（只消费 plan，缺任何一镜即 fail-closed）",
                        tl, params=[_revision_id],
                        request_body=_json_body(_ref("RenderRequest"), required=False),
                        ok_schema=_ref("RenderVersionResponse"),
                        errors=[(400, "参数非法"),
                                (403, "授权者非人工主体"),
                                (404, "revision 不存在"),
                                (409, "预检未通过（存在未声明静音 / 引用已失效版本）")]),
        },
        "/api/timeline/revisions/latest": {
            "get": _op("getLatestTimelineRevision", "该集的最新时间线 revision", tl,
                       params=[_q("project", "项目 key"), _q("episode", "集号")],
                       ok_schema=_ref("TimelineRevisionResponse"),
                       errors=[(404, "该集尚无时间线")]),
        },
        "/api/timeline/fingerprints/{fingerprint}": {
            "get": _op("getTimelineByFingerprint", "按合成指纹反查 revision", tl,
                       params=[_path_param("fingerprint", {"type": "string"}, "合成指纹")],
                       ok_schema=_ref("TimelineRevisionResponse"),
                       errors=[(404, "无匹配 revision")]),
        },
        # ---------------- jobs ----------------
        "/api/jobs": {
            "get": _op("listJobs", "作业列表（Job = 一次用户意图）", jb,
                       params=[_q("status", "按状态过滤")],
                       ok_schema=_ref("JobListResponse")),
            "post": _op("createJob", "创建作业", jb,
                        request_body=_json_body(_ref("CreateJobRequest")),
                        ok_schema=_ref("JobResponse"),
                        errors=[(400, "参数非法")]),
        },
        "/api/jobs/{job_id}": {
            "get": _op("getJob", "作业详情（含各次尝试）", jb, params=[_job_id],
                       ok_schema=_ref("JobResponse"), errors=[(404, "作业不存在")]),
        },
        "/api/jobs/{job_id}/attempts": {
            "post": _op("createJobAttempt", "为作业开启一次尝试（Attempt = 一次执行）", jb,
                        params=[_job_id],
                        request_body=_json_body(_ref("CreateAttemptRequest"), required=False),
                        ok_schema=_ref("AttemptResponse"),
                        errors=[(404, "作业不存在")]),
        },
        "/api/jobs/attempts/{attempt_id}/status": {
            "post": _op("updateAttemptStatus", "更新尝试状态", jb, params=[_attempt_id],
                        request_body=_json_body(_ref("UpdateAttemptRequest")),
                        ok_schema=_ref("AttemptResponse"),
                        errors=[(404, "尝试不存在")]),
        },
        "/api/jobs/attempts/{attempt_id}/finish": {
            "post": _op("finishJobAttempt", "结束尝试并结算", jb, params=[_attempt_id],
                        request_body=_json_body(_ref("FinishAttemptRequest")),
                        ok_schema=_ref("AttemptResponse"),
                        errors=[(404, "尝试不存在"), (409, "终态不可再变更")]),
        },
        "/api/jobs/{job_id}/status": {
            "post": _op("updateJobStatus", "更新作业状态", jb, params=[_job_id],
                        request_body=_json_body(_ref("UpdateJobRequest")),
                        ok_schema=_ref("JobResponse"),
                        errors=[(404, "作业不存在")]),
        },
        "/api/jobs/{job_id}/pending-units": {
            "get": _op("listPendingUnits", "待处理单元（断点续跑判据之一）", jb,
                       params=[_job_id], ok_schema=_ref("PendingUnitListResponse"),
                       errors=[(404, "作业不存在")]),
        },
        "/api/jobs/{job_id}/reuse-hint": {
            "get": _op("getJobReuseHint", "免重渲提示（workflow_hash 命中可复用）", jb,
                       params=[_job_id], ok_schema=_ref("ReuseHintResponse"),
                       errors=[(404, "作业不存在")]),
        },
        "/api/jobs/stats": {
            "get": _op("getJobStats", "作业统计（按状态聚合）", jb,
                       params=[_q("project", "按项目过滤")],
                       ok_schema=_ref("JobStatsResponse")),
        },
        # ---------------- styles ----------------
        "/api/styles": {
            "get": _op("listStyles", "风格库全量（后端单一事实源）", st,
                       params=[_q("category", "按分类过滤：2d / 3d / real")],
                       ok_schema=_ref("StyleListResponse")),
        },
        "/api/styles/{style_id}": {
            "get": _op("getStyle", "风格详情（stable style_id，改名不断历史）", st,
                       params=[_style_id], ok_schema=_ref("StyleResponse"),
                       errors=[(404, "风格不存在")]),
        },
        "/api/styles/resolve": {
            "get": _op("resolveStyle", "由自由文本解析风格（存量项目向后兼容）", st,
                       params=[{"name": "text", "in": "query", "required": True,
                                "schema": {"type": "string"},
                                "description": "项目 config.style 里存的原始字面值"}],
                       ok_schema=_ref("StyleResolveResponse")),
        },
    })
    return d


def manual_paths() -> List[str]:
    """手工补 schema 的路径（排序稳定）。"""
    return sorted(_build_manual_paths().keys())


def manual_operations() -> Dict[str, Dict[str, Any]]:
    """手工 operation 集合，供 :mod:`app.api.contracts` 自检用。"""
    return _build_manual_paths()


# --------------------------------------------------------------------------
# 规格组装
# --------------------------------------------------------------------------

def _merge(flask_app, include_reflected: bool = True) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """把反射结果与手工 schema 合并成 ``paths``，返回 ``(paths, stats)``。"""
    paths: Dict[str, Dict[str, Any]] = {}

    if include_reflected:
        for path, method, op, _params in _reflect(flask_app):
            paths.setdefault(path, {})[method.lower()] = op

    # 手工 schema 覆盖同名反射项（手工更权威），但**保留反射的 tags**
    manual = _build_manual_paths()
    for path, ops in manual.items():
        slot = paths.setdefault(path, {})
        for method, op in ops.items():
            slot[method] = op

    stats = {
        "reflected_ops": sum(
            1 for ops in paths.values() for op in ops.values()
            if op.get("x-contract-source") == "reflected"),
        "manual_ops": sum(
            1 for ops in paths.values() for op in ops.values()
            if op.get("x-contract-source") == "manual"),
        "path_count": len(paths),
        "operation_count": sum(len(ops) for ops in paths.values()),
    }
    return paths, stats


#: 本模块承诺「手工补全 schema」的蓝图名。
#:
#: 这三个域是本轮新增的，schema 与实现同时写、同时生效，不会像反射来的
#: 那 200+ 条历史路由那样漂移。把它们列出来是为了让
#: :func:`undocumented_routes` 能在 CI 里断言「承诺过的一定写到了」——
#: 否则「手工补 schema」会退化成一句没人核对的承诺。
MANUAL_BLUEPRINTS = (
    "contracts",
    "delivery",
    "licensing",
    # ---- 2026-10-07 收口补齐 ----
    # 收口审核发现：新增的 production_facts / timeline / jobs / styles 四个域
    # **不在**原三域之内，于是它们「能调通但规格里没有、生成物里也没有」，
    # 前端要接这些接口只能继续手写字符串 URL —— 契约优先在这四域上等于没做。
    # 这四域与前三域一样是本轮新增、schema 与实现同时生效，
    # 因此同样适用「承诺过的一定写到」的自检。
    "production_facts",
    "timeline",
    "jobs",
    "styles",
)


def undocumented_routes(flask_app) -> List[str]:
    """找出**本该有 schema 却没写**的路由（契约漂移的自检）。

    返回 ``["/api/... GET"]`` 形式的列表；空列表表示通过。

    为什么需要它：本模块一半反射、一半手工，最典型的翻车方式是
    「在蓝图上加了个新路由，忘了在 ``_build_manual_paths`` 里登记」——
    端点能调通、规格里没有、生成物里也没有，**没有任何东西会报错**。
    这个函数把那种沉默变成 CI 里的一条红。
    """
    if flask_app is None:
        return []
    manual = _build_manual_paths()
    missing: List[str] = []
    for rule in flask_app.url_map.iter_rules():
        endpoint = str(rule.endpoint or "")
        # 只管本模块承诺过的域；``api.contracts.list_x`` → 蓝图名 contracts
        blueprint = endpoint.split(".")[0] if "." in endpoint else ""
        if blueprint not in MANUAL_BLUEPRINTS:
            continue
        raw = str(rule.rule)
        if not raw.startswith("/api/"):
            continue
        path, _params = _rule_to_path(raw)
        ops = manual.get(path) or {}
        for method in sorted(rule.methods or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            if method.lower() not in ops:
                missing.append("{} {}".format(method, path))
    return sorted(missing)


def build_spec(flask_app=None, include_reflected: bool = True) -> Dict[str, Any]:
    """构建 OpenAPI 3.1 规格字典。

    ``flask_app`` 为 ``None`` 时只输出手工部分（可用于不启服务的 CI / 单测）。
    """
    paths, stats = _merge(flask_app, include_reflected=include_reflected)
    spec: Dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "漫剧锻造台 API",
            "version": CONTRACT_VERSION,
            "description": (
                "本规格是**前端与后端的唯一契约**。前端客户端由 "
                "`scripts/generate_client.py` 从本文件生成，"
                "`scripts/check_generated_client.py` 在 CI 校验生成物是否过期。\n\n"
                "**手工改这个文件没有意义**：它是生成物，改了会被下一次生成覆盖。"
            ),
        },
        "servers": [{"url": "/", "description": "同源部署"}],
        "tags": [
            {"name": "contracts", "description": "契约本体（OpenAPI 导出与版本）"},
            {"name": "delivery", "description": "交付预设、SHA-256 校验、人工批准闸门"},
            {"name": "licensing", "description": "模型/素材授权登记与交付门禁"},
        ],
        "paths": {p: paths[p] for p in sorted(paths)},
        "components": {"schemas": {k: SCHEMAS[k] for k in sorted(SCHEMAS)}},
        "x-contract-version": CONTRACT_VERSION,
        "x-manual-paths": manual_paths(),
        "x-stats": stats,
    }
    return spec


def spec_hash(spec: Optional[Dict[str, Any]] = None) -> str:
    """规格内容摘要。

    摘要排除 ``x-stats`` 与反射出来的**非契约字段**（``summary`` 从 endpoint 名
    反推，蓝图重命名会让它变，但 API 契约没变）—— 否则 W1 改名蓝图就会把
    前端客户端误判为过期。
    """
    doc = spec if spec is not None else build_spec(None)
    slim = {
        "openapi": doc.get("openapi"),
        "info": {"version": (doc.get("info") or {}).get("version")},
        "paths": {
            path: {
                method: {
                    k: v for k, v in (op or {}).items()
                    if k in ("operationId", "requestBody", "responses", "parameters",
                             "x-contract-source")
                }
                for method, op in (ops or {}).items()
            }
            for path, ops in (doc.get("paths") or {}).items()
        },
        "schemas": (doc.get("components") or {}).get("schemas") or {},
    }
    blob = json.dumps(slim, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def write_spec(path: str, flask_app=None, include_reflected: bool = True) -> str:
    """把规格写到磁盘（``scripts/generate_client.py`` 用），返回 ``spec_hash``。"""
    spec = build_spec(flask_app, include_reflected=include_reflected)
    target = os.path.abspath(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return spec_hash(spec)
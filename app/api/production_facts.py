# -*- coding: utf-8 -*-
"""版本化生产事实蓝图：``/api/production_facts/*``。

蓝图约定（协调书 §3）
--------------------
模块级 ``bp`` + ``DOMAIN``，``url_prefix`` 留空、路径原样写在装饰器上；
由 ``app/api/__init__.py`` 的自动发现机制注册，**不需要改 ``app.py``**。

依赖策略
--------
按任务要求**不硬依赖 ``api._shared``**（W1 拆分期间它可能尚未存在）：
用一个 ``try/except ImportError`` 拿到 flask 自身的 ``request/jsonify``，
拿到 ``_shared`` 时优先用它的版本。这样本模块可以**先于** W1 落地独立工作，
也不会在 W1 拆完后突然 404。

三条铁律在路由层的体现
----------------------
* 批准端点 ``POST /api/production_facts/media/<id>/approve`` **必须**带
  ``authorized_by``；缺参直接 400，且服务层还会拒绝机器账号。
* 意图变更只走 ``POST /api/production_facts/intents/<id>/derive``
  （铁律 2）—— **没有任何 PUT / PATCH 改 intent 的路由**。
* 所有"状态"响应都带 ``selection_implies_approval: false``（铁律 1）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

try:                                    # W1 拆分产物存在时优先用它
    from api._shared import jsonify as _jsonify, request as _request  # type: ignore
    jsonify, request = _jsonify, _request
except ImportError:                     # 拆分未完成 → 用 flask 自带（自包含兜底）
    pass

try:
    from application.production import ProductionError, ProductionService
    from domain import delivery as delivery_domain
    from domain.production_facts import (
        ApprovalAuthorizationError, ConflictError, DomainError, FrozenEntityError,
        IncompleteIntentError, NotSelectedError, require_human_authorization,
    )
    from infrastructure.facts_repo import FactsRepo
except ImportError:                     # pragma: no cover - 以脚本方式导入时的兜底
    from app.application.production import ProductionError, ProductionService  # type: ignore
    from app.domain import delivery as delivery_domain  # type: ignore
    from app.domain.production_facts import (  # type: ignore
        ApprovalAuthorizationError, ConflictError, DomainError, FrozenEntityError,
        IncompleteIntentError, NotSelectedError, require_human_authorization,
    )
    from app.infrastructure.facts_repo import FactsRepo  # type: ignore

logger = logging.getLogger(__name__)

bp = Blueprint("production_facts", __name__)
DOMAIN = "production_facts"

#: 进程内单例。仓储自带线程锁与 WAL，短连接取用（与 task_store 同一约定），
#: 因此模块级单例是安全的，也避免每个请求都跑一遍迁移。
_service = None


def service() -> ProductionService:
    global _service
    if _service is None:
        _service = ProductionService(FactsRepo())
    return _service


def _body() -> Dict[str, Any]:
    """取请求体 JSON；不是对象就退化为空 dict（由各端点做必填校验）。"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _err(message: str, status: int = 400, **extra: Any):
    """错误响应。``**extra`` 用于附稳定错误码（与 ``app/api/delivery.py``
    的 ``_fail(message, code, status, **extra)`` 同形状），前端按 ``code``
    分支，不解析 ``error`` 文案。"""
    body: Dict[str, Any] = {"success": False, "error": str(message)}
    body.update(extra)
    return jsonify(body), status


def _ok(**payload: Any):
    body = {"success": True}
    body.update(payload)
    return jsonify(body)


def _approver_allowlist() -> List[str]:
    """人工主体白名单（可选，来自环境变量 ``MJSCXT_APPROVER_ALLOWLIST``，逗号分隔）。

    与 ``app/api/delivery.py`` 的同名函数**逐字同口径**：同一份环境变量、
    同一套 fail-closed 语义。两处各写一份的代价是：改了一处忘了另一处，
    先被放宽的那套就变成实际入口。
    """
    raw = os.environ.get("MJSCXT_APPROVER_ALLOWLIST", "") or ""
    return [p.strip() for p in raw.replace("；", ",").split(",") if p.strip()]


def _require_human_actor(actor: str, authorization_ref: str = ""):
    """人工主体校验 → 403（机器账号无权批准/撤销）。

    与 ``/api/delivery/packages/<id>/revoke`` **共用同一个领域函数**
    （:func:`domain.production_facts.require_human_authorization`）
    与**同一个错误码** ``DLV-APPROVER-NOT-HUMAN``：
    「机器把批准收回去」和「机器授予批准」一样荒唐，两边的口径必须一致 ——
    口径不一致本身就是缺陷，而不只是一个疏漏。
    """
    try:
        require_human_authorization(actor, authorization_ref,
                                    allowlist=_approver_allowlist())
    except ApprovalAuthorizationError as e:
        return _err(str(e), 403,
                    code=delivery_domain.DLV_APPROVER_NOT_HUMAN,
                    detail=delivery_domain.DELIVERY_ERROR_CODES[
                        delivery_domain.DLV_APPROVER_NOT_HUMAN])
    return None


#: 领域异常 → HTTP 状态码。批准类一律 403（"你不能这么做"），
#: 乐观锁冲突一律 409（"重读再试"），不混成笼统的 400。
_ERR_STATUS = (
    (NotSelectedError, 409),
    (ApprovalAuthorizationError, 403),
    (ConflictError, 409),
    (FrozenEntityError, 409),
    (IncompleteIntentError, 400),
    (DomainError, 400),
)


@bp.get("/api/production_facts/health")
def health():
    """自检端点：迁移版本 + 表清单（只读，不连 GPU、不连 ComfyUI）。"""
    svc = service()
    return _ok(schema_version=svc.repo.schema_version(),
               tables=["generation_intents", "media_versions", "selection_decisions",
                       "approval_decisions", "capability_profile_versions"],
               invariants={
                   "selection_implies_approval": False,
                   "intent_frozen": True,
                   "timeline_immutable": True,
               })


# =====================================================================
# GenerationIntent
# =====================================================================

@bp.post("/api/production_facts/intents")
def create_intent():
    """冻结一条生成意图（铁律 2：一旦冻结就不能原地改）。"""
    d = _body()
    missing = [k for k in ("project", "episode", "shot_key", "prompt") if not d.get(k)]
    if missing:
        return _err("缺少必填字段：%s" % ", ".join(missing))
    try:
        intent = service().create_intent(
            project=d["project"], episode=d["episode"], shot_key=d["shot_key"],
            kind=d.get("kind") or "image", prompt=d["prompt"],
            negative_prompt=d.get("negative_prompt") or "",
            ref_slots=d.get("ref_slots"), seed=int(d.get("seed", -1) or -1),
            profile_id=d.get("profile_id") or "",
            profile_version=str(d.get("profile_version") or ""),
            workflow_version=d.get("workflow_version") or "",
            workflow_hash=d.get("workflow_hash") or "",
            created_by=d.get("created_by") or "")
    except DomainError as e:
        return _err(e)
    return _ok(intent=intent)


@bp.get("/api/production_facts/intents")
def list_intents():
    """按 project / episode / shot_key 过滤历史意图（重渲历史全部可查）。"""
    args = request.args
    intents = service().list_intents(
        project=args.get("project") or "", episode=args.get("episode") or "",
        shot_key=args.get("shot_key") or "",
        limit=int(args.get("limit") or 100))
    return _ok(intents=intents, count=len(intents))


@bp.get("/api/production_facts/intents/<intent_id>")
def get_intent(intent_id: str):
    """单条意图 + 派生血缘 + 候选版本列表。"""
    svc = service()
    intent = svc.get_intent(intent_id)
    if not intent:
        return _err("生成意图不存在：%s" % intent_id, 404)
    return _ok(intent=intent, lineage=svc.intent_lineage(intent_id),
               media_versions=svc.list_media_versions(intent_id))


@bp.post("/api/production_facts/intents/<intent_id>/derive")
def derive_intent(intent_id: str):
    """**派生**新意图（铁律 2 的唯一变更入口）。

    没有 PUT/PATCH：想改就派生。原意图永远保留，历史可追。
    """
    d = _body()
    if not str(d.get("reason") or "").strip():
        return _err("派生必须写明 reason（为什么改）")
    changes = {k: d[k] for k in
               ("kind", "prompt", "negative_prompt", "ref_slots", "seed",
                "profile_id", "profile_version", "workflow_version", "workflow_hash")
               if k in d}
    if not changes:
        return _err("至少要改一个内容字段，否则派生无意义")
    try:
        out = service().derive_intent(intent_id, reason=d["reason"],
                                      created_by=d.get("created_by") or "", **changes)
    except DomainError as e:
        return _err(e)
    return _ok(**out)


# =====================================================================
# MediaVersion + 决策
# =====================================================================

@bp.post("/api/production_facts/media")
def register_media():
    """登记一个候选媒体版本（附 ffprobe 客观事实）。

    ``path`` **必填**：没有磁盘路径的候选无法在批准时做磁盘现状校验，
    也就等于「零校验批准」—— 与其留一条永远查不出被覆盖的事实，不如直接拒。
    客户端自带的 ``media_sha256`` **不采信**：一律由服务端按 ``path`` 重算
    （客户端说它是什么不算数，只有磁盘上是什么才算数）；若客户端带了一个
    与重算结果不同的指纹，只记警告，登记的仍然是磁盘真值。
    """
    d = _body()
    if not d.get("intent_id"):
        return _err("缺少 intent_id")
    path = str(d.get("path") or "").strip()
    if not path:
        return _err("缺少 path：媒体版本必须落在磁盘上，否则批准时无法校验磁盘现状")
    declared = str(d.get("media_sha256") or "").strip()
    try:
        mv = service().register_media(
            d["intent_id"], path=path,
            media_sha256="",          # 刻意置空 → 服务端按磁盘重算
            probe=d.get("probe") or {},
            attempt_id=d.get("attempt_id") or "",
            bytes_=int(d.get("bytes") or 0))
    except DomainError as e:
        return _err(e)
    if declared and declared != mv.get("media_sha256"):
        logger.warning("客户端上报的 media_sha256 与磁盘重算不一致，已按磁盘真值登记：%s"
                       "（上报 %s / 磁盘 %s）", path, declared[:12],
                       str(mv.get("media_sha256") or "")[:12])
    return _ok(media_version=mv)


@bp.get("/api/production_facts/media")
def list_media():
    """候选列表；每条都带**独立的** ``selected`` / ``approved`` 字段。"""
    items = service().list_media_versions(intent_id=request.args.get("intent_id") or "")
    return _ok(media_versions=items, count=len(items))


@bp.get("/api/production_facts/media/<media_version_id>")
def get_media(media_version_id: str):
    """单条候选 + 三态汇总。"""
    svc = service()
    mv = svc.repo.get_media_version(media_version_id)
    if mv is None:
        return _err("候选版本不存在：%s" % media_version_id, 404)
    return _ok(media_version=mv.to_dict(), decision=svc.decide(media_version_id))


@bp.post("/api/production_facts/media/<media_version_id>/select")
def select_media(media_version_id: str):
    """记录**采用**（创作决定）—— 不写、也无法写批准记录。"""
    d = _body()
    if not str(d.get("reason") or "").strip():
        return _err("采用必须写明 reason（否则无法回答「为什么用这一版」）")
    try:
        out = service().select(
            subject_type=d.get("subject_type") or "media_version",
            subject_id=d.get("subject_id") or media_version_id,
            media_version_id=media_version_id, reason=d["reason"],
            decided_by=d.get("decided_by") or "",
            project=d.get("project") or "", episode=d.get("episode") or "",
            shot_key=d.get("shot_key") or "")
    except DomainError as e:
        return _err(e)
    return _ok(**out)


@bp.post("/api/production_facts/media/<media_version_id>/approve")
def approve_media(media_version_id: str):
    """记录**批准**（放行决定）—— 必须带人工授权。

    缺 ``authorized_by`` 直接 400（铁律 1：批准不能无主）。
    未采用就批准 → 409；机器账号 → 403；产物被覆盖 → 403。
    """
    d = _body()
    if not str(d.get("authorized_by") or "").strip():
        return _err("批准必须有人工授权主体（authorized_by），机器检查通过不等于批准")
    try:
        out = service().approve(
            media_version_id=media_version_id,
            authorized_by=d["authorized_by"],
            authorization_ref=d.get("authorization_ref") or "",
            reason=d.get("reason") or "", scope=d.get("scope") or "media",
            note=d.get("note") or "",
            subject_type=d.get("subject_type") or "",
            subject_id=d.get("subject_id") or "",
            approver_allowlist=_approver_allowlist())
    except DomainError as e:
        status = 400
        for exc, code in _ERR_STATUS:
            if isinstance(e, exc):
                status = code
                break
        return _err(e, status)
    return _ok(**out)


@bp.post("/api/production_facts/approvals/<approval_id>/revoke")
def revoke_approval(approval_id: str):
    """撤销批准（同样要求人工主体）。

    ``revoked_by`` **必须**过人工闸门：早先这里只校验非空，
    ``revoked_by="bot"`` 能把批准收回去 —— 而交付侧的 revoke 早已加同一闸门，
    两套口径不一致时，先被放宽的那套就是实际入口。
    """
    d = _body()
    if not str(d.get("revoked_by") or "").strip():
        return _err("撤销必须写明撤销人")
    if not str(d.get("reason") or "").strip():
        return _err("撤销必须写明 reason")
    denied = _require_human_actor(d["revoked_by"], str(d.get("authorization_ref") or ""))
    if denied is not None:
        return denied
    try:
        return _ok(**service().revoke_approval(approval_id, revoked_by=d["revoked_by"],
                                               reason=d["reason"],
                                               approver_allowlist=_approver_allowlist()))
    except DomainError as e:
        status = 409 if isinstance(e, ConflictError) else 400
        return _err(e, status)


@bp.get("/api/production_facts/decisions")
def decisions():
    """某 subject 的「采用 / 批准」三态（界面读这个，别自己推）。

    返回体固定含 ``selection_implies_approval: false``（铁律 1）。
    """
    subject_id = request.args.get("subject_id") or ""
    if not subject_id:
        return _err("缺少 subject_id")
    return _ok(decision=service().decide(subject_id))


@bp.get("/api/production_facts/decisions/history")
def decision_history():
    """采用 / 批准历史（append-only 全量，供审计回溯）。"""
    subject_id = request.args.get("subject_id") or ""
    svc = service()
    return _ok(selections=svc.list_selections(subject_id),
               approvals=svc.list_approvals(subject_id))


# =====================================================================
# CapabilityProfileVersion
# =====================================================================

@bp.post("/api/production_facts/capability-profiles")
def publish_capability_profile():
    """发布能力档案的一个新版本（历史版本永不覆盖）。"""
    d = _body()
    if not str(d.get("profile_id") or "").strip():
        return _err("缺少 profile_id")
    try:
        prof = service().publish_capability_profile(
            d["profile_id"], params=d.get("params") or {},
            deploy_profile=d.get("deploy_profile") or "",
            workflow_version=d.get("workflow_version") or "",
            note=d.get("note") or "", created_by=d.get("created_by") or "",
            version=int(d.get("version") or 0))
    except DomainError as e:
        status = 409 if isinstance(e, ConflictError) else 400
        return _err(e, status)
    return _ok(profile=prof)


@bp.get("/api/production_facts/capability-profiles")
def list_capability_profiles():
    """能力档案版本列表（回答「当时这台机器允许什么」）。"""
    profs = service().list_capability_profiles(profile_id=request.args.get("profile_id") or "")
    return _ok(profiles=profs, count=len(profs))
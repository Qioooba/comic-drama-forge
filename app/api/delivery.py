# -*- coding: utf-8 -*-
"""交付蓝图：交付预设、SHA-256 校验、**人工批准闸门**（P1-7）。

与既有导出的关系
----------------
本蓝图**不重新导出**任何东西。``app/nle_export.py`` 负责把成片铺成
SRT / 剪映草稿 / FCPXML / 帧清单；这里负责在那些**既有产物**之上补三件
此前完全缺失的东西：

1. **交付预设**：竖屏 9:16 / 横屏 16:9 / 3:4 —— 此前只能自己猜成片规格；
2. **交付文件清单 + 逐文件 SHA-256**：此前拿到的是「导出目录的拷贝」，
   无法判断它是不是最新一次导出、有没有被人动过；
3. **人工批准闸门**：机器跑完就算「完成」。

「采用 ≠ 批准」（协调书 §4）在 API 层的落法
----------------------------------------
``POST .../verify`` 走的是**机器校验**，它最多把状态推到 ``verified``；
``POST .../approve`` 才是人工批准，且必须带 ``approver``、**批准主体必须是人**
（``system:autopilot`` / ``bot`` / ``scheduler`` / ``worker`` 等机器身份一律 403，
与 ``/api/production_facts/*/approve`` 共用
:func:`domain.production_facts.require_human_authorization` 这一个口径），
并与当时的 ``package_hash`` 绑定。内容一变（清单内文件增删改、目录里新增文件、
换预设、改 schema 版本），哈希不匹配，批准**自动失效** —— 失效判定是纯函数
（见 :func:`domain.delivery.evaluate_approval`），不需要任何后台任务去撤销。

错误响应统一 ``{success: false, error: <中文>, code: <稳定码>}``。
前端按 ``code`` 分支，不解析 ``error`` 文案。
"""

from __future__ import annotations

import logging
import os
from typing import List

from flask import Blueprint, jsonify, request

from domain import delivery as delivery_domain
from domain import licensing as licensing_domain
from domain.production_facts import ApprovalAuthorizationError, require_human_authorization
from infrastructure import delivery_repo, licensing_repo

logger = logging.getLogger(__name__)

bp = Blueprint("delivery", __name__)
DOMAIN = "delivery"


def _fail(message: str, code: str, status: int = 400, **extra):
    """统一错误响应。``code`` 稳定，``message`` 面向用户。"""
    return jsonify({"success": False, "error": message, "code": code, **extra}), status


def _approver_allowlist() -> List[str]:
    """人工主体白名单（可选，来自环境变量 ``MJSCXT_APPROVER_ALLOWLIST``，逗号分隔）。

    给了白名单 → **只有**名单内的主体能批准/撤销（最严格一档，不再依赖名字形状）；
    不给 → 退回 :func:`domain.production_facts.is_human_actor` 的命名口径闸门。
    两档都拒绝机器账号，区别只在「人工」是按名单认还是按名字认。
    """
    raw = os.environ.get("MJSCXT_APPROVER_ALLOWLIST", "") or ""
    return [p.strip() for p in raw.replace("；", ",").split(",") if p.strip()]


def _require_human_actor(actor: str, authorization_ref: str = ""):
    """人工主体校验 → 统一的 403 响应（机器账号无权批准/撤销交付）。

    与 ``/api/production_facts/*/approve`` **共用同一个领域函数**：
    两套「人工批准」口径不一致时，先被放宽的那套就是实际入口。
    """
    try:
        require_human_authorization(actor, authorization_ref,
                                    allowlist=_approver_allowlist())
    except ApprovalAuthorizationError as e:
        return _fail(
            delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_APPROVER_NOT_HUMAN],
            delivery_domain.DLV_APPROVER_NOT_HUMAN, status=403, detail=str(e))
    return None


# --------------------------------------------------------------------------
# 预设
# --------------------------------------------------------------------------

@bp.get("/api/delivery/presets")
def list_delivery_presets():
    """交付预设列表（竖屏 / 横屏 / 3:4）。"""
    presets = delivery_domain.list_presets()
    return jsonify({"success": True, "count": len(presets), "presets": presets})


# --------------------------------------------------------------------------
# 交付包
# --------------------------------------------------------------------------

@bp.get("/api/delivery/packages")
def list_delivery_packages():
    """交付包列表（可按项目过滤），含机器校验与人工批准状态。"""
    project = request.args.get("project") or ""
    safe = delivery_repo.safe_project(project) if project else ""
    if project and not safe:
        return _fail("项目名非法", delivery_domain.DLV_UNKNOWN_PACKAGE)
    packages = delivery_repo.list_packages(safe)
    return jsonify({"success": True, "count": len(packages), "packages": packages})


@bp.post("/api/delivery/packages")
def create_delivery_package():
    """登记交付包：扫描既有导出产物 → 逐文件算 SHA-256 → 过授权门禁。

    body: ``{project_name, preset_id, episode_no?, requirement?}``

    授权门禁**先行**：未登记授权的模型/素材不得进入交付，拦在创建这一步，
    而不是等到真正发出去才发现。
    """
    data = request.get_json(silent=True) or {}
    project = delivery_repo.safe_project(data.get("project_name") or "")
    if not project:
        return _fail("project_name 非法", delivery_domain.DLV_UNKNOWN_PACKAGE)

    preset_id = str(data.get("preset_id") or delivery_domain.DEFAULT_PRESET_ID)
    requirement = data.get("requirement") or {}

    # ① 授权门禁（fail-closed）
    gate = licensing_repo.evaluate_delivery_gate(
        requirement, project=project, audit=True)
    if not gate.get("ok"):
        return _fail(
            gate.get("summary") or "授权门禁未通过",
            licensing_domain.LIC_GATE_BLOCKED,
            status=403,
            violations=gate.get("violations") or [],
            registry=gate.get("registry") or {},
        )

    # ② 产物扫描（复用 nle_export 的导出目录，不重新导出）
    pkg = delivery_repo.build_package(
        project, preset_id, requirement=requirement, licensing_gate=gate,
        episode_no=data.get("episode_no"))
    if not pkg["files"]:
        return _fail(
            delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_EMPTY_PACKAGE],
            delivery_domain.DLV_EMPTY_PACKAGE, status=422,
            hint="请先执行导出，产出 SRT / 剪映草稿 / FCPXML / 帧清单后再登记交付包")

    delivery_repo.save_package(pkg)
    pkg["approval"] = {"state": "none", "approver": "", "approved_at": "", "reason": ""}
    pkg["status"] = delivery_domain.derive_status(pkg, None)
    pkg["attribution"] = licensing_repo.attribution_for(requirement)
    return jsonify({"success": True, "package": pkg}), 201


@bp.get("/api/delivery/packages/<package_id>")
def get_delivery_package(package_id):
    """交付包详情（含逐文件 SHA-256 与批准状态）。"""
    pkg = delivery_repo.get_package(package_id)
    if not pkg:
        return _fail(delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_UNKNOWN_PACKAGE],
                     delivery_domain.DLV_UNKNOWN_PACKAGE, status=404)
    approval = delivery_repo.get_approval(package_id)
    pkg["approval"] = delivery_domain.evaluate_approval(pkg, approval)
    pkg["status"] = delivery_domain.derive_status(pkg, approval)
    return jsonify({"success": True, "package": pkg})


@bp.post("/api/delivery/packages/<package_id>/release-check")
def release_check_delivery_package(package_id):
    """发布总门禁（机器校验 + 人工批准 + 授权 + 磁盘现状）。

    请求体不接受任何授权结论：服务端按包内 requirement + project 重新评估。
    客户端传入的 ``licensing_gate`` 一律忽略，防止伪造 ``{"ok": true}`` 放行。
    """
    pkg = delivery_repo.get_package(package_id)
    if not pkg:
        return _fail(delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_UNKNOWN_PACKAGE],
                     delivery_domain.DLV_UNKNOWN_PACKAGE, status=404)
    gate = licensing_repo.evaluate_delivery_gate(
        pkg.get("requirement") or {}, project=pkg.get("project") or "", audit=True)
    approval = delivery_repo.get_approval(package_id)
    result = delivery_domain.release_ready(pkg, approval, licensing_gate=gate)
    pkg["approval"] = delivery_domain.evaluate_approval(pkg, approval)
    pkg["status"] = delivery_domain.derive_status(pkg, approval)
    pkg["release_ready"] = result
    return jsonify({"success": bool(result.get("ok")), "release": result, "package": pkg})


@bp.get("/api/delivery/packages/<package_id>/manifest")
def get_delivery_manifest(package_id):
    """交付清单：逐文件 SHA-256 + 包摘要 + 授权/批准状态（可直接归档留痕）。"""
    pkg = delivery_repo.get_package(package_id)
    if not pkg:
        return _fail(delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_UNKNOWN_PACKAGE],
                     delivery_domain.DLV_UNKNOWN_PACKAGE, status=404)
    return jsonify({"success": True, "manifest": delivery_repo.export_manifest(pkg)})


# --------------------------------------------------------------------------
# 机器校验
# --------------------------------------------------------------------------

@bp.post("/api/delivery/packages/<package_id>/verify")
def verify_delivery_package(package_id):
    """机器校验：逐个文件重算 SHA-256 并与清单比对。

    ⚠ 通过**不等于批准**。结果只写 ``verified_ok``，状态最多到 ``verified``。
    """
    pkg = delivery_repo.get_package(package_id)
    if not pkg:
        return _fail(delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_UNKNOWN_PACKAGE],
                     delivery_domain.DLV_UNKNOWN_PACKAGE, status=404)

    result = delivery_domain.verify_files(pkg.get("files") or [], root=pkg.get("artifact_root") or "")
    delivery_repo.record_verify(package_id, result)

    approval = delivery_repo.get_approval(package_id)
    view = delivery_domain.evaluate_approval(pkg, approval)
    out = delivery_repo.get_package(package_id) or pkg
    out["approval"] = view
    out["status"] = delivery_domain.derive_status(out, approval)
    # 校验不通过时 HTTP 200 + ok=false：让前端把每个文件的差异列出来，
    # 而不是只看到一个笼统的 500
    return jsonify({"success": bool(result.get("ok")), "verify": result, "package": out})


# --------------------------------------------------------------------------
# 人工批准（与机器校验严格分离）
# --------------------------------------------------------------------------

@bp.post("/api/delivery/packages/<package_id>/approve")
def approve_delivery_package(package_id):
    """人工批准。**必须带操作者**，且与批准当时的包哈希绑定。

    body: ``{approver, note?, package_hash?}``

    ``package_hash`` 可选；给了就当作「我看到的摘要」，与当前不一致直接拒，
    免得用户在另一个标签页批准的包被误批。
    """
    data = request.get_json(silent=True) or {}
    pkg = delivery_repo.get_package(package_id)
    if not pkg:
        return _fail(delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_UNKNOWN_PACKAGE],
                     delivery_domain.DLV_UNKNOWN_PACKAGE, status=404)

    check = delivery_domain.can_approve(pkg)
    if not check["ok"]:
        return _fail(check["message"], check["code"])

    # 批准的是**磁盘上这份内容**。若文件在建包之后被改过，就不能批 ——
    # 否则批准人看到的是一份已经不存在的内容（这正是批准失效判据的同一前提）。
    if not pkg.get("disk_ok"):
        return _fail(delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_FILE_MISSING],
                     delivery_domain.DLV_FILE_MISSING)
    if not pkg.get("disk_matches_baseline"):
        return _fail(
            delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_HASH_MISMATCH],
            delivery_domain.DLV_HASH_MISMATCH,
            hint="交付文件在建包之后被改动，请重新登记交付包后再批准")

    approver = str(data.get("approver") or "").strip()
    if not approver:
        return _fail(delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_APPROVER_REQUIRED],
                     delivery_domain.DLV_APPROVER_REQUIRED)

    # 批准主体必须是**人**：`system:autopilot` / `bot` / `scheduler` / `worker`
    # 这类机器身份一律 403 —— 「机器跑完就算完成」正是交付事故的经典成因。
    denied = _require_human_actor(approver, str(data.get("authorization_ref") or ""))
    if denied is not None:
        return denied

    declared_hash = str(data.get("package_hash") or "").strip()
    if declared_hash and declared_hash != pkg.get("package_hash"):
        return _fail(
            "交付包内容已变化，此前看到的摘要已失效，请刷新后重新批准",
            delivery_domain.DLV_APPROVAL_STALE)

    delivery_repo.approve_package(
        package_id, pkg["package_hash"], approver,
        note=str(data.get("note") or ""))

    approval = delivery_repo.get_approval(package_id)
    view = delivery_domain.evaluate_approval(pkg, approval)
    pkg["approval"] = view
    pkg["status"] = delivery_domain.derive_status(pkg, approval)
    return jsonify({"success": True, "package": pkg})


@bp.post("/api/delivery/packages/<package_id>/revoke")
def revoke_delivery_approval(package_id):
    """撤销人工批准。

    撤销是**人工动作**，优先于哈希判定：内容变化会让批准失效，但撤销不会
    因为「文件又变回来了」而被自动复活。
    """
    pkg = delivery_repo.get_package(package_id)
    if not pkg:
        return _fail(delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_UNKNOWN_PACKAGE],
                     delivery_domain.DLV_UNKNOWN_PACKAGE, status=404)
    data = request.get_json(silent=True) or {}
    operator = str(data.get("operator") or "").strip()
    if not operator:
        # 撤销也是人工动作（与 facts_repo.revoke_approval 同一口径）：
        # 「机器把批准收回去」和「机器授予批准」一样荒唐。
        return _fail(delivery_domain.DELIVERY_ERROR_CODES[delivery_domain.DLV_APPROVER_REQUIRED],
                     delivery_domain.DLV_APPROVER_REQUIRED)
    denied = _require_human_actor(operator, str(data.get("authorization_ref") or ""))
    if denied is not None:
        return denied
    delivery_repo.revoke_approval(package_id,
                                  operator=operator,
                                  reason=str(data.get("reason") or ""))
    approval = delivery_repo.get_approval(package_id)
    view = delivery_domain.evaluate_approval(pkg, approval)
    pkg["approval"] = view
    pkg["status"] = delivery_domain.derive_status(pkg, approval)
    return jsonify({"success": True, "package": pkg})
# -*- coding: utf-8 -*-
"""Job / Attempt 蓝图：``/api/jobs/*``（评估文档 §P1-5 任务系统收敛）。

端点
----
* ``POST /api/jobs``                        —— 登记一次**用户意图**（Job）；
* ``POST /api/jobs/<id>/attempts``          —— 开始一次**执行尝试**（Attempt），
  返回里带 ``reuse_available``（``workflow_hash`` 一致 + 产物非空 ⇒ 可免重渲）；
* ``POST /api/jobs/attempts/<id>/finish``   —— 结束尝试并写入单元产物；
* ``GET  /api/jobs/<id>``                   —— Job + 全部 Attempt + 进度聚合。

⚠️ 铁律 **R1（URL 逐字不变）** 在本文件的具体含义：
本轮**没有**改动任何既有路由，只**新增** ``/api/jobs/*`` 前缀。
既有 ``tasks.db`` 与 ``/api/status`` 链路一律不动 —— 收敛是分步的，
本轮交付语义地基，消费侧接线留到下一轮（见 ``jobs_service`` 模块头）。

所有状态迁移端点都要求 ``expected_row_version``（乐观锁）：
不带就退回"只做状态机校验"并在返回体里标注 ``optimistic_lock: false``，
让调用方清楚自己放弃了并发保护 —— 静默丢掉乐观锁比不提供更危险。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from flask import Blueprint, jsonify, request

try:                                    # W1 拆分产物存在时优先用它
    from api._shared import jsonify as _jsonify, request as _request  # type: ignore
    jsonify, request = _jsonify, _request
except ImportError:                     # 拆分未完成 → flask 自带（自包含兜底）
    pass

try:
    from application.jobs_service import JobsService, JobsServiceError
    from domain.jobs import JobError
    from domain.production_facts import ConflictError, DomainError
    from infrastructure.jobs_repo import JobsRepo
except ImportError:                     # pragma: no cover - 以脚本方式导入时的兜底
    from app.application.jobs_service import JobsService, JobsServiceError  # type: ignore
    from app.domain.jobs import JobError  # type: ignore
    from app.domain.production_facts import ConflictError, DomainError  # type: ignore
    from app.infrastructure.jobs_repo import JobsRepo  # type: ignore

logger = logging.getLogger(__name__)

bp = Blueprint("jobs", __name__)
DOMAIN = "jobs"

_service = None


def service() -> JobsService:
    global _service
    if _service is None:
        _service = JobsService(JobsRepo())
    return _service


def _body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _err(message: str, status: int = 400):
    return jsonify({"success": False, "error": str(message)}), status


def _ok(**payload: Any):
    body = {"success": True}
    body.update(payload)
    return jsonify(body)


def _row_version(d: Dict[str, Any]) -> int:
    try:
        return int(d.get("expected_row_version") or 0)
    except (TypeError, ValueError):
        return 0


@bp.get("/api/jobs/health")
def health():
    """自检：迁移版本 + 库路径（只读）。"""
    svc = service()
    return _ok(schema_version=svc.repo.schema_version(),
               db=svc.repo.db_path,
               invariants={"job_is_user_intent": True, "attempt_is_execution": True,
                           "optimistic_lock": True})


@bp.post("/api/jobs")
def create_job():
    """登记一次用户意图（重试不新建 Job，只加 Attempt）。"""
    d = _body()
    if not str(d.get("kind") or "").strip():
        return _err("缺少 kind（如 storyboard / shot_video / compose / dub）")
    try:
        job = service().create_job(
            kind=d["kind"], project=d.get("project") or "", episode=d.get("episode") or "",
            title=d.get("title") or "", params=d.get("params") or {},
            intent_ref=d.get("intent_ref") or "", created_by=d.get("created_by") or "",
            job_id=d.get("job_id") or "")
    except DomainError as e:
        return _err(e)
    return _ok(job=job)


@bp.get("/api/jobs")
def list_jobs():
    """按 project / episode / status / kind 过滤。"""
    args = request.args
    jobs = service().list_jobs(
        project=args.get("project") or "", episode=args.get("episode") or "",
        status=args.get("status") or "", kind=args.get("kind") or "",
        limit=int(args.get("limit") or 100))
    return _ok(jobs=jobs, count=len(jobs))


@bp.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    """Job + 全部 Attempt + 进度聚合 + 免重渲判据。"""
    try:
        return _ok(**service().get_job(job_id))
    except DomainError as e:
        return _err(e, 404)


@bp.post("/api/jobs/<job_id>/attempts")
def start_attempt(job_id: str):
    """开始一次执行尝试。

    返回 ``reuse_available``：为真表示存在 ``workflow_hash`` 一致且产物非空的
    历史成功尝试，**可以免重渲**（这是本项目最值钱的性能特性）。
    """
    d = _body()
    try:
        out = service().start_attempt(
            job_id, workflow_hash=d.get("workflow_hash") or "",
            intent_id=d.get("intent_id") or "", attempt_id=d.get("attempt_id") or "",
            expected_row_version=_row_version(d))
    except DomainError as e:
        status = 409 if isinstance(e, ConflictError) else 400
        return _err(e, status)
    return _ok(optimistic_lock=bool(_row_version(d)), **out)


@bp.post("/api/jobs/attempts/<attempt_id>/finish")
def finish_attempt(attempt_id: str):
    """结束一次尝试。``units`` / ``media_version_ids`` 一并写入。"""
    d = _body()
    status = str(d.get("status") or "succeeded")
    try:
        out = service().finish_attempt(
            attempt_id, status=status, expected_row_version=_row_version(d),
            units=d.get("units"), media_version_ids=d.get("media_version_ids"),
            error=d.get("error") or "")
    except DomainError as e:
        code = 409 if isinstance(e, ConflictError) else 400
        return _err(e, code)
    return _ok(optimistic_lock=bool(_row_version(d)), **out)


@bp.post("/api/jobs/attempts/<attempt_id>/status")
def set_attempt_status(attempt_id: str):
    """把尝试推进到某状态（``pending → running → …``）。

    为什么单独开一个端点而不在 ``/finish`` 里自动补 ``running``：
    状态机必须**如实反映事实**。执行方在真正开跑时上报 ``running``，
    进程崩在 ``pending`` 与 ``running`` 之间就是另一个诊断结论
    （"排到没跑" vs "跑一半挂了"）。自动补状态会把这个区别抹掉，
    而这正是 ``interrupted`` 回收判据依赖的信息。
    """
    d = _body()
    target = str(d.get("status") or "")
    if not target:
        return _err("缺少目标状态 status")
    try:
        out = service().set_attempt_status(
            attempt_id, target, expected_row_version=_row_version(d),
            units=d.get("units"), media_version_ids=d.get("media_version_ids"),
            error=d.get("error") or "")
    except DomainError as e:
        code = 409 if isinstance(e, ConflictError) else 400
        return _err(e, code)
    return _ok(optimistic_lock=bool(_row_version(d)), **out)


@bp.post("/api/jobs/<job_id>/status")
def set_job_status(job_id: str):
    """推进 Job 状态（状态机 + 乐观锁）。"""
    d = _body()
    target = str(d.get("status") or "")
    if not target:
        return _err("缺少目标状态 status")
    try:
        out = service().set_job_status(job_id, target,
                                       expected_row_version=_row_version(d),
                                       error=d.get("error") or "")
    except DomainError as e:
        code = 409 if isinstance(e, ConflictError) else 400
        return _err(e, code)
    return _ok(optimistic_lock=bool(_row_version(d)), **out)


@bp.get("/api/jobs/<job_id>/pending-units")
def pending_units(job_id: str):
    """未完成单元（断点续跑判据：**磁盘产物存在且非空**即完成）。"""
    units = service().pending_units(job_id)
    return _ok(pending_units=units, count=len(units))


@bp.get("/api/jobs/<job_id>/reuse-hint")
def reuse_hint(job_id: str):
    """免重渲判据：给定 ``workflow_hash`` 看能否复用历史产物。"""
    wf = request.args.get("workflow_hash") or ""
    if not wf:
        return _err("缺少 workflow_hash")
    hint = service().reuse_hint(job_id, wf)
    return _ok(reuse_available=bool(hint), reuse=hint)


@bp.get("/api/jobs/stats")
def stats():
    """按状态统计（任务看板用；只读聚合，不查磁盘）。"""
    return _ok(**service().stats())
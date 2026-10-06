# -*- coding: utf-8 -*-
"""编排层：Job / Attempt（评估文档 §P1-5 任务系统收敛）。

本层的职责边界（写清以免与既有三套任务语义重复）
--------------------------------------------------
* **做**：把"用户点了一次"记成 Job、把"跑了一次"记成 Attempt、
  状态迁移走领域层状态机、写路径全部带乐观锁、
  判定免重渲复用（``workflow_hash`` 一致 + 产物非空）；
* **不做**：不接队列线程、不消费、不接 ComfyUI、不改既有 ``task_store.TaskQueue``。
  消费侧接线属于下一轮（评估文档 §P1-5 的后半段），
  本轮只交付**语义地基**，避免在没有回归测试的情况下动在跑的链路。

编排层不 import flask。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:
    from domain.jobs import (
        Attempt, Job, JobError, build_attempt, build_job, can_transition,
        job_progress, is_unit_done, next_attempt_no, reuse_candidate, resumable_units,
    )
    from domain.production_facts import ConflictError, DomainError
    from infrastructure.jobs_repo import JobsRepo
except ImportError:                    # pragma: no cover - 以脚本方式导入时的兜底
    from app.domain.jobs import (  # type: ignore
        Attempt, Job, JobError, build_attempt, build_job, can_transition,
        job_progress, is_unit_done, next_attempt_no, reuse_candidate, resumable_units,
    )
    from app.domain.production_facts import ConflictError, DomainError  # type: ignore
    from app.infrastructure.jobs_repo import JobsRepo  # type: ignore

logger = logging.getLogger(__name__)

__all__ = ["JobsService", "JobsServiceError"]


class JobsServiceError(DomainError):
    """任务编排错误。"""


def _new_id(prefix: str = "job") -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


class JobsService:
    """Job / Attempt 编排入口。

    所有状态迁移都要求调用方带上**读到的 row_version**（乐观锁）。
    不传（默认 0）时跳过版本比对，但仍走状态机校验 —— 方便脚本/迁移场景，
    生产路径请显式传，避免"后写覆盖先写"。
    """

    def __init__(self, repo: Optional[JobsRepo] = None):
        self.repo = repo or JobsRepo()

    # ============================================================
    # Job
    # ============================================================

    def create_job(self, *, kind: str, project: str = "", episode: str = "",
                   title: str = "", params: Optional[Mapping[str, Any]] = None,
                   intent_ref: str = "", created_by: str = "",
                   job_id: str = "") -> Dict[str, Any]:
        """登记一次**用户意图**（重试不会新建 Job，只会加 Attempt）。"""
        job = build_job(job_id or _new_id(), kind=kind, project=project, episode=episode,
                        title=title, params=params, intent_ref=intent_ref,
                        created_by=created_by)
        self.repo.save_job(job)
        logger.info("已创建 Job：%s kind=%s %s/%s", job.job_id, job.kind, project, episode)
        return job.to_dict()

    def get_job(self, job_id: str) -> Dict[str, Any]:
        d = self.repo.job_detail(job_id)
        if not d:
            raise JobsServiceError("Job 不存在：%s" % job_id)
        return d

    def list_jobs(self, **kw: Any) -> List[Dict[str, Any]]:
        return [j.to_dict() for j in self.repo.list_jobs(**kw)]

    def set_job_status(self, job_id: str, target: str, *,
                       expected_row_version: int = 0, error: str = "") -> Dict[str, Any]:
        """推进 Job 状态（带乐观锁 + 领域层状态机校验）。"""
        return self.repo.set_job_status(job_id, target,
                                        expected_row_version=expected_row_version,
                                        error=error)

    # ============================================================
    # Attempt
    # ============================================================

    def start_attempt(self, job_id: str, *, workflow_hash: str = "",
                      intent_id: str = "", attempt_id: str = "",
                      expected_row_version: int = 0) -> Dict[str, Any]:
        """开始一次执行尝试。

        返回体里带 ``reuse`` 判据：若历史里存在 ``workflow_hash`` 一致且
        产物非空的成功尝试，返回 ``reuse.attempt_id`` ——
        调用方可据此**免重渲**（这是 ``comfyui_job_store`` 最值钱的性能特性）。
        """
        job = self.repo.get_job(job_id)
        if job is None:
            raise JobsServiceError("Job 不存在：%s" % job_id)
        history = [a.to_dict() for a in self.repo.list_attempts(job_id)]
        no = next_attempt_no(history)
        att = build_attempt(attempt_id or _new_id("at"), job_id, attempt_no=no,
                            workflow_hash=workflow_hash, intent_id=intent_id)
        self.repo.save_attempt(att)
        if expected_row_version > 0:
            self.repo.set_job_status(job_id, "running",
                                     expected_row_version=expected_row_version)
        reuse = reuse_candidate(history, workflow_hash) if workflow_hash else None
        logger.info("Job %s 开始第 %d 次尝试（workflow_hash=%s，reuse=%s）",
                    job_id, no, (workflow_hash or "")[:12],
                    reuse.get("attempt_id") if reuse else "无")
        return {"attempt": att.to_dict(),
                "reuse": reuse,
                "reuse_available": bool(reuse),
                "selection_implies_approval": False}

    def finish_attempt(self, attempt_id: str, *, status: str = "succeeded",
                       expected_row_version: int = 0,
                       units: Optional[Mapping[str, Any]] = None,
                       media_version_ids: Optional[Sequence[str]] = None,
                       error: str = "") -> Dict[str, Any]:
        """结束一次尝试（写入单元产物与产出的媒体版本 id）。"""
        if status not in ("succeeded", "failed", "cancelled", "interrupted"):
            raise JobsServiceError("非法结束状态：%s" % status)
        return self.repo.set_attempt_status(attempt_id, status,
                                            expected_row_version=expected_row_version,
                                            units=units,
                                            media_version_ids=media_version_ids,
                                            error=error)

    def set_attempt_status(self, attempt_id: str, target: str, **kw: Any) -> Dict[str, Any]:
        return self.repo.set_attempt_status(attempt_id, target, **kw)

    def list_attempts(self, job_id: str) -> List[Dict[str, Any]]:
        return [a.to_dict() for a in self.repo.list_attempts(job_id)]

    # ============================================================
    # 聚合 / 诊断
    # ============================================================

    def progress(self, job_id: str, *, total: int = 0) -> Dict[str, Any]:
        """进度聚合（取最近一次 Attempt 的完成率，不做历次累加）。"""
        atts = [a.to_dict() for a in self.repo.list_attempts(job_id)]
        return job_progress(atts, total=total)

    def pending_units(self, job_id: str) -> List[Dict[str, Any]]:
        """未完成单元（断点续跑判据：**磁盘产物存在且非空**即完成）。"""
        atts = self.repo.list_attempts(job_id)
        if not atts:
            return []
        return resumable_units(list(atts[-1].units.values()))

    def reuse_hint(self, job_id: str, workflow_hash: str) -> Optional[Dict[str, Any]]:
        """免重渲判据（workflow_hash 一致 + 产物非空 + 上次成功）。"""
        return reuse_candidate([a.to_dict() for a in self.repo.list_attempts(job_id)],
                               workflow_hash)

    def recycle_interrupted(self) -> int:
        """启动时把残留 running 标记为 interrupted（返回处理条数）。"""
        return self.repo.recycle_interrupted()

    def stats(self) -> Dict[str, Any]:
        """按状态统计 Job 数量（任务看板用；只读聚合）。"""
        counts: Dict[str, int] = {}
        for j in self.repo.list_jobs(limit=1000):
            counts[j.status] = counts.get(j.status, 0) + 1
        return {"by_status": counts, "total": sum(counts.values())}
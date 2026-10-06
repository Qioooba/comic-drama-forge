# -*- coding: utf-8 -*-
"""Job / Attempt 两级任务语义（评估文档 §P1-5「任务系统收敛」）。

为什么要收敛
------------
改造前项目里有**三套并存**的任务语义：

============================  =====================================================
模块                          语义
============================  =====================================================
``task_store.py``             SQLite 全生命周期 + 单元级进度 + 串行队列 + 崩溃回收
``comfyui_job_store.py``      ComfyUI 台账（``workflow_hash`` 复用、免重渲）
``task_lease.py``             跨进程文件租约（``O_EXCL`` + 心跳 + TTL）
============================  =====================================================

各自都对，合起来就坏：一条镜头失败**算谁的**说不清 ——
是 ComfyUI 台账没产物？任务表 failed？还是租约被回收了？

收敛为两级（对齐自研）
----------------------
* **Job** = 一次**用户意图**（"渲第 3 集 ep03"）。用户点一次就是一条 Job，
  失败重试不会变成新 Job —— 否则"重试"和"新任务"在报表里混成一团。
* **Attempt** = 一次**执行尝试**。重试 = 同一 Job 下的新 Attempt，
  历史 Attempt 全部保留（这正是"失败为什么失败"的证据链）。

保留 comic 的两个最值钱特性
----------------------------
1. **磁盘产物为准的续跑判据**（``task_store.is_unit_done``）：产物存在且非空即完成，
   状态表丢了也能跳。已搬到 :func:`is_unit_done` 与 :func:`resumable_units`；
2. **``workflow_hash`` 复用**（``comfyui_job_store``）：Attempt 带 ``workflow_hash``，
   哈希一致且产物非空 ⇒ 可免重渲复用。

领域层纯函数，不 import flask。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from domain.production_facts import DomainError, hash_payload
except ImportError:                    # pragma: no cover - 以脚本方式导入时的兜底
    from .production_facts import DomainError, hash_payload  # type: ignore

__all__ = [
    "JobError", "JOB_STATUSES", "ATTEMPT_STATUSES", "JOB_TRANSITIONS", "ATTEMPT_TRANSITIONS",
    "Job", "Attempt",
    "build_job", "build_attempt", "can_transition", "next_attempt_no",
    "is_unit_done", "resumable_units", "reuse_candidate", "job_progress",
]


class JobError(DomainError):
    """任务语义错误。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


#: Job 状态机：一次用户意图的生命周期
JOB_STATUSES: Tuple[str, ...] = ("queued", "running", "succeeded",
                                 "failed", "cancelled", "interrupted")
#: Attempt 状态机：一次执行尝试的生命周期
ATTEMPT_STATUSES: Tuple[str, ...] = ("pending", "running", "succeeded",
                                     "failed", "cancelled", "interrupted")

#: Job 合法迁移。终态（succeeded/failed/cancelled）不可再迁出 ——
#: 否则"已完成"的 Job 会被后来的重试悄悄改回 running。
JOB_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "queued": ("running", "cancelled", "failed"),
    "running": ("succeeded", "failed", "cancelled", "interrupted"),
    "succeeded": (),
    "failed": ("queued",),          # 允许重新入队（= 同一意图的重试）
    "cancelled": (),
    "interrupted": ("queued", "running"),
}

ATTEMPT_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "pending": ("running", "cancelled", "failed"),
    "running": ("succeeded", "failed", "cancelled", "interrupted"),
    "succeeded": (),
    "failed": (),
    "cancelled": (),
    "interrupted": ("pending", "running"),
}

#: 终态判定（对齐 task_store.TERMINAL_STATES 的思路，但区分 succeeded/failed/cancelled）
_TERMINAL = ("succeeded", "failed", "cancelled")


def can_transition(status: str, target: str, *,
                   table: Optional[Mapping[str, Tuple[str, ...]]] = None) -> bool:
    """状态迁移是否合法（fail-closed：未知状态一律 False）。"""
    tbl = table or JOB_TRANSITIONS
    if status not in tbl or target not in tbl:
        return False
    return target in tbl[status]


# =====================================================================
# Job
# =====================================================================

@dataclass(frozen=True)
class Job:
    """一次**用户意图**。

    ``intent_ref`` 指向 :class:`~domain.production_facts.GenerationIntent` 的 id
    （或时间线 revision id）—— 让「任务」与「生产事实」挂上钩：
    渲出来的媒体版本能回溯到是哪条 intent，谁批准的，一查便知。
    """
    job_id: str
    kind: str                                    # storyboard / shot_video / compose / dub ...
    status: str = "queued"
    project: str = ""
    episode: str = ""
    title: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    params_hash: str = ""
    intent_ref: str = ""                         # 关联的 GenerationIntent / TimelineRevision id
    created_at: str = ""
    created_by: str = ""
    updated_at: str = ""
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["params"] = dict(self.params or {})
        return d

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "Job":
        data = {k: raw.get(k) for k in Job.__dataclass_fields__}
        data["params"] = dict(raw.get("params") or {})
        return Job(**data)


def build_job(job_id: str, *, kind: str, project: str = "", episode: str = "",
              title: str = "", params: Optional[Mapping[str, Any]] = None,
              intent_ref: str = "", created_by: str = "",
              created_at: str = "") -> Job:
    """创建一条 Job（状态恒为 ``queued``）。

    ``params_hash`` 对 params 取稳定指纹：同参数重跑可识别为「同一意图」，
    参数变了则哈希变（便于对账「这次重跑到底改了什么」）。
    """
    if not str(kind or "").strip():
        raise JobError("Job 必须有 kind（否则无法归类统计）")
    p = dict(params or {})
    return Job(job_id=str(job_id), kind=str(kind), status="queued",
               project=str(project or ""), episode=str(episode or ""),
               title=str(title or kind), params=p,
               params_hash=hash_payload(p) if p else "",
               intent_ref=str(intent_ref or ""),
               created_at=created_at or _now(), created_by=str(created_by or ""),
               updated_at=created_at or _now())


# =====================================================================
# Attempt
# =====================================================================

@dataclass(frozen=True)
class Attempt:
    """一次**执行尝试**。

    ``attempt_no`` 在同一 Job 内从 1 单调递增。
    ``workflow_hash`` 保留 ``comfyui_job_store`` 的**免重渲**语义：
    哈希一致 + 产物非空 ⇒ 可直接复用，不必重跑 GPU。
    """
    attempt_id: str
    job_id: str
    attempt_no: int = 1
    status: str = "pending"
    workflow_hash: str = ""
    intent_id: str = ""                    # 本次尝试实际执行的 intent
    media_version_ids: Tuple[str, ...] = ()   # 产出的 MediaVersion id
    units: Mapping[str, Any] = field(default_factory=dict)   # 单元级进度/产物路径
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    created_at: str = ""
    duration_sec: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__
             if k not in ("media_version_ids", "units")}
        d["media_version_ids"] = list(self.media_version_ids)
        d["units"] = dict(self.units or {})
        return d

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "Attempt":
        data = {k: raw.get(k) for k in Attempt.__dataclass_fields__
                if k not in ("media_version_ids", "units")}
        data["media_version_ids"] = tuple(raw.get("media_version_ids") or ())
        data["units"] = dict(raw.get("units") or {})
        return Attempt(**data)


def build_attempt(attempt_id: str, job_id: str, *, attempt_no: int,
                  workflow_hash: str = "", intent_id: str = "",
                  created_at: str = "") -> Attempt:
    """创建一次执行尝试（状态恒为 ``pending``）。"""
    if int(attempt_no) < 1:
        raise JobError("attempt_no 从 1 开始（实际 %s）" % attempt_no)
    return Attempt(attempt_id=str(attempt_id), job_id=str(job_id),
                   attempt_no=int(attempt_no), status="pending",
                   workflow_hash=str(workflow_hash or ""),
                   intent_id=str(intent_id or ""),
                   created_at=created_at or _now())


def next_attempt_no(attempts: Iterable[Mapping[str, Any]]) -> int:
    """下一次尝试的序号（空历史从 1 开始；并发下可能撞号，靠唯一索引兜住）。"""
    nums = []
    for a in (attempts or ()):
        try:
            nums.append(int((a or {}).get("attempt_no") or 0))
        except (TypeError, ValueError):
            continue
    return (max(nums) + 1) if nums else 1


# =====================================================================
# 断点续跑判据（保留 comic 最值钱的两个特性）
# =====================================================================

def is_unit_done(result_path: str, min_bytes: int = 1) -> bool:
    """单元完成判据：产物文件存在且非空。

    ⚠️ 这是**故意与 ``task_store.is_unit_done`` 同语义**的搬移，不是新判据。
    理由：判据本身就是对的 —— 状态表可能丢、可能被写坏，但磁盘上的文件不会说谎。
    任何"以状态表为准"的改动都会让崩溃后重跑白烧一遍卡。
    """
    if not result_path:
        return False
    try:
        return os.path.isfile(result_path) and os.path.getsize(result_path) >= min_bytes
    except OSError:
        return False


def resumable_units(units: Iterable[Mapping[str, Any]],
                    *, key: str = "unit_key", path: str = "result_path") -> List[Dict[str, Any]]:
    """筛出**尚未完成**的单元（重跑时只跑这些）。

    与 ``task_store.filter_pending_units`` 同语义：状态表说 done 但文件没了 → 仍算未完成。
    """
    pending: List[Dict[str, Any]] = []
    for u in units or ():
        if not isinstance(u, dict):
            continue
        if is_unit_done(str(u.get(path) or "")):
            continue
        pending.append(u)
    return pending


def reuse_candidate(prev_attempts: Iterable[Mapping[str, Any]], workflow_hash: str, *,
                    min_bytes: int = 1) -> Optional[Dict[str, Any]]:
    """在历史 Attempt 里找**可免重渲复用**的那一次（``comfyui_job_store`` 语义搬家）。

    复用必须同时满足三条，缺一不可：

    1. ``workflow_hash`` 完全一致 —— 工作流 / 提示词 / 参考图 / 种子任一变化即失效；
    2. 该 Attempt 状态为 succeeded；
    3. 产物逐个**存在且非空** —— 文件被清理过就不能当数。

    这是"免重渲"这个最值钱性能特性的入口，返回 ``None`` 即表示必须重跑。
    """
    if not workflow_hash:
        return None
    for a in list(prev_attempts or ())[::-1]:
        a = dict(a or {})
        if str(a.get("workflow_hash") or "") != str(workflow_hash):
            continue
        if str(a.get("status") or "") != "succeeded":
            continue
        units = a.get("units") or {}
        if isinstance(units, dict) and units:
            if not all(is_unit_done(str(v) if isinstance(v, str) else
                                    str((v or {}).get("result_path") or ""), min_bytes)
                       for v in units.values()):
                continue
        return a
    return None


def job_progress(attempts: Sequence[Mapping[str, Any]], *,
                 total: int = 0) -> Dict[str, Any]:
    """由 Attempt 历史聚合 Job 进度（界面只读这个，不再自己数状态）。

    进度取**最近一次 Attempt** 的单元完成率，而不是历次累计 ——
    重试后上一轮的 80% 不该让新一轮一开始就显示 80%。
    """
    items = sorted((attempts or ()), key=lambda a: int((a or {}).get("attempt_no") or 0))
    if not items:
        return {"percent": 0.0, "done_units": 0, "total_units": int(total or 0),
                "attempts": 0, "latest_status": ""}
    latest = dict(items[-1])
    units = latest.get("units") or {}
    done = 0
    if isinstance(units, dict):
        for v in units.values():
            p = v if isinstance(v, str) else (v or {}).get("result_path")
            if is_unit_done(str(p or "")):
                done += 1
    t = int(total or (len(units) if isinstance(units, dict) else 0))
    pct = round(done * 100.0 / t, 1) if t else (100.0 if latest.get("status") == "succeeded" else 0.0)
    return {"percent": pct, "done_units": done, "total_units": t,
            "attempts": len(items), "latest_status": str(latest.get("status") or "")}


#: 迁移表别名导出（供仓库层做 DB 层校验时复用同一份真相）
JOB_TERMINAL: ClassVar[Tuple[str, ...]] = _TERMINAL
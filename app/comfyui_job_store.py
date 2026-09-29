# -*- coding: utf-8 -*-
"""ComfyUI 任务台账 —— 崩溃免重渲检查点（借鉴 NiliX 的渲染检查点恢复）

要解决的问题
------------
H3 整集一次提交要跑几十分钟（22~44 段）。此前 `prompt_id` **只活在内存里**：
应用崩溃 / 重启 / 被计划任务重启后，调用方只能重新提交 —— 同一集几十段 GPU
时间**全部白烧**。

台账把「稳定任务键 → prompt_id → 产物」落盘，重启后按三种情形分流：

======  ==========================================  ==========================
情形    判据                                         动作
======  ==========================================  ==========================
① 已完成  台账里的产物仍在磁盘上                        直接复用，**零渲染**
② 在跑中  远端 `/history` 里该 prompt 仍在队列/执行       **重连等待**，不重复提交
③ 不可复用 哈希不符 / 产物已被删 / 远端报错 / 无记录        正常重渲（现状行为）
======  ==========================================  ==========================

安全阀（为什么可以放心复用）
--------------------------
* **`workflow_hash` 必须一致**：工作流/提示词/参考图/种子任一变化 → 哈希变 → 禁用复用。
  这一条同时挡住「看似同一个任务、实际换了参数」的那类静默错。
* 复用前逐个校验产物**存在且非空**（文件被清理过就不能当数）。
* 台账有 **TTL（默认 7 天）** 与 **条数上限（默认 2000）**，避免无限增长。
* **全程 fail-open**：台账损坏/写失败只降级为「本次不复用」，**绝不阻断生产**。
  这是与 fs_atomic 的刻意差异 —— 生产路径上的缓存不该 fail-loud 拖垮出片。

崩溃恢复的完整闭环
----------------
只有哈希一致才能复用，而哈希包含**种子**。所以崩溃后要复用，种子本身也必须活过重启：
调用方用 :func:`get_or_create_seed` 取种子（有则沿用、无则生成并落盘），
这样重启后重建出的是**逐字节相同**的工作流 → 哈希一致 → 命中台账。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

__all__ = [
    "workflow_hash", "remember", "find", "mark_done", "mark_failed",
    "get_or_create_seed", "forget", "stats", "db_path",
]

#: 台账条目保留时长（秒）。默认 7 天 —— 足够覆盖「隔夜跑崩、第二天续跑」。
DEFAULT_TTL_SEC = 7 * 24 * 3600
#: 台账最多保留多少条（超上限按时间淘汰最旧）。
DEFAULT_MAX_ENTRIES = 2000
#: 任务键前缀，用于 get_or_create_seed 与任务条目共用同一命名空间。
_SEED_KEY = "seed"

_lock = threading.RLock()


def db_path() -> str:
    """台账文件路径：`<PROJECT_OUTPUT_DIR>/comfyui_jobs.json`。

    刻意放在 output 根而不是某项目目录下：`prompt_id` 是 ComfyUI 全局的，
    且复用判据是 `workflow_hash`（已含项目信息），不需要按项目分库。
    惰性 import config，避免模块级循环导入。
    """
    from config import PROJECT_OUTPUT_DIR
    return os.path.join(PROJECT_OUTPUT_DIR, "comfyui_jobs.json")


# --------------------------------------------------------------------- 哈希

def workflow_hash(api_prompt: Dict[str, Any]) -> str:
    """工作流 API prompt 的稳定指纹（32 位十六进制）。

    `sort_keys=True` + 紧凑分隔符 → 同样的图必然得到同样的哈希（与节点/字段顺序无关）。
    任何参数（提示词、种子、尺寸、参考图文件名…）变了哈希就变，
    这正是「不得复用」的判据。
    """
    try:
        blob = json.dumps(api_prompt, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError) as e:      # 极端脏数据 → 退化为不可复用
        logger.warning("工作流指纹计算失败（按不可复用处理）：%s", e)
        return ""
    return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:32]


# --------------------------------------------------------------------- 读写

def _load() -> dict:
    """读取台账（fail-open：损坏/缺失都当作空台账）。"""
    try:
        data = read_json_strict(db_path(), {}) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:                     # noqa: BLE001
        logger.warning("ComfyUI 台账读取失败（按空台账处理，本次不复用）：%s", e)
        return {}


def _save(data: dict) -> None:
    """原子落盘（fail-open：写失败只 warning，不影响生产）。"""
    try:
        atomic_write_json(db_path(), data)
    except Exception as e:                     # noqa: BLE001
        logger.warning("ComfyUI 台账写盘失败（不影响本次生成）：%s", e)


def _prune(jobs: dict, now: float) -> dict:
    """淘汰过期条目与超上限的最旧条目。"""
    keep = {}
    for k, v in (jobs or {}).items():
        if not isinstance(v, dict):
            continue
        try:
            ts = float(v.get("updated_at") or v.get("submitted_at") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts and (now - ts) > DEFAULT_TTL_SEC:
            continue
        keep[k] = v
    if len(keep) > DEFAULT_MAX_ENTRIES:
        ordered = sorted(keep.items(),
                         key=lambda kv: float((kv[1] or {}).get("updated_at") or 0),
                         reverse=True)
        keep = dict(ordered[:DEFAULT_MAX_ENTRIES])
    return keep


# --------------------------------------------------------------------- 公开 API

def find(job_key: str) -> Optional[dict]:
    """查任务记录；不存在返回 None。"""
    if not job_key:
        return None
    with _lock:
        rec = _load().get(job_key)
    return rec if isinstance(rec, dict) else None


def remember(job_key: str, wf_hash: str, prompt_id: str, *,
             label: str = "", meta: Optional[dict] = None) -> None:
    """登记「已提交」的任务（含 prompt_id 与工作流指纹）。"""
    if not job_key:
        return
    now = time.time()
    with _lock:
        data = _load()
        jobs = _prune(data.get("jobs") or {}, now)
        jobs[job_key] = {
            "workflow_hash": wf_hash,
            "prompt_id": prompt_id,
            "label": label or job_key,
            "submitted_at": now,
            "updated_at": now,
            "status": "submitted",
            "outputs": [],
            "meta": dict(meta or {}),
        }
        data["jobs"] = jobs
        data["updated_at"] = now
        _save(data)


def mark_done(job_key: str, outputs: List[str]) -> None:
    """把任务标为完成并记录产物绝对路径（供后续零渲染复用）。"""
    if not job_key:
        return
    _update(job_key, status="completed",
            outputs=[str(p) for p in (outputs or []) if p])


def mark_failed(job_key: str, reason: str = "") -> None:
    """把任务标为失败（下次不得复用该 prompt）。"""
    if not job_key:
        return
    _update(job_key, status="failed", fail_reason=str(reason or "")[:200])


def _update(job_key: str, **fields) -> None:
    now = time.time()
    with _lock:
        data = _load()
        jobs = _prune(data.get("jobs") or {}, now)
        rec = jobs.get(job_key)
        if not isinstance(rec, dict):
            rec = {"submitted_at": now, "outputs": []}
        rec.update(fields)
        rec["updated_at"] = now
        jobs[job_key] = rec
        data["jobs"] = jobs
        data["updated_at"] = now
        _save(data)


def forget(job_key: str) -> None:
    """删除某任务记录（用户显式要求重做时用）。"""
    if not job_key:
        return
    with _lock:
        data = _load()
        jobs = _prune(data.get("jobs") or {}, time.time())
        jobs.pop(job_key, None)
        data["jobs"] = jobs
        data["updated_at"] = time.time()
        _save(data)


def get_or_create_seed(job_key: str, gen: Callable[[], int],
                       *, ttl_sec: int = DEFAULT_TTL_SEC) -> int:
    """取该任务的种子：**有则沿用、无则生成并落盘**。

    为什么种子要单独持久化：`workflow_hash` 包含种子，崩溃后若要命中台账复用，
    重建出的工作流必须与崩溃前逐字节一致 —— 种子漂了哈希就不一致，
    检查点等于白存。TTL 与台账一致（默认 7 天），过期种子不复用（避免老任务串味）。
    """
    if not job_key:
        return int(gen())
    now = time.time()
    with _lock:
        data = _load()
        seeds = data.get(_SEED_KEY) or {}
        rec = seeds.get(job_key)
        if isinstance(rec, dict):
            try:
                ts = float(rec.get("at") or 0)
                val = int(rec.get("value"))
                if ts and (now - ts) <= ttl_sec:
                    return val
            except (TypeError, ValueError):
                pass
        val = int(gen())
        seeds[job_key] = {"value": val, "at": now}
        if len(seeds) > DEFAULT_MAX_ENTRIES:
            ordered = sorted(seeds.items(),
                             key=lambda kv: float((kv[1] or {}).get("at") or 0),
                             reverse=True)
            seeds = dict(ordered[:DEFAULT_MAX_ENTRIES])
        data[_SEED_KEY] = seeds
        data["updated_at"] = now
        _save(data)
        return val


def stats() -> dict:
    """台账概况（给诊断/看板用）。"""
    with _lock:
        data = _load()
    jobs = data.get("jobs") or {}
    by_status: Dict[str, int] = {}
    for v in jobs.values():
        st = str((v or {}).get("status") or "?")
        by_status[st] = by_status.get(st, 0) + 1
    return {"path": db_path(), "entries": len(jobs), "by_status": by_status,
            "seeds": len(data.get(_SEED_KEY) or {})}

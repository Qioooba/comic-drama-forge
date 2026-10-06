# -*- coding: utf-8 -*-
"""P2-11 真实性能采集（只采数，不调参）。

每个 ComfyUI prompt 建一个采样会话：后台 1s 周期采集 GPU 显存/利用率、
系统 CPU/RAM；任务结束时解析 history 的节点耗时与 cache 事件，落
``output/performance/<prompt_id>.json``，并向 ``analytics`` 追加一条聚合事件。
任何异常都只降级为缺失字段，绝不影响生成。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from fs_atomic import atomic_write_json

logger = logging.getLogger(__name__)
_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "performance",
)
_LOCK = threading.RLock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}
_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()
_SAMPLE_INTERVAL = 1.0
_PREV_SYSTEM_TIMES: Optional[Dict[str, float]] = None


def _empty_metrics() -> Dict[str, Any]:
    return {
        "gpu_vram_used_mb": [],
        "gpu_util_percent": [],
        "system_cpu_percent": [],
        "system_ram_used_mb": [],
        "errors": [],
    }


def begin(prompt_id: str, context: Optional[Dict] = None) -> None:
    """开始/确保一个 prompt 的采样会话（幂等）。"""
    if not prompt_id:
        return
    global _THREAD
    try:
        with _LOCK:
            session = _SESSIONS.get(prompt_id)
            if session is None:
                _SESSIONS[prompt_id] = {
                    "started_monotonic": time.monotonic(),
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "samples": _empty_metrics(),
                    "context": dict(context or {}),
                    "history": None,
                }
            elif context:
                session["context"].update(context)
            _STOP.clear()
            if _THREAD is None or not _THREAD.is_alive():
                _THREAD = threading.Thread(
                    target=_sampler_loop, name="perf-sampler",
                    daemon=True)
                _THREAD.start()
    except Exception as exc:  # noqa: BLE001
        logger.debug("性能采样启动失败（忽略）：%s", exc)


def attach_history(prompt_id: str, history: Optional[Dict]) -> None:
    if not prompt_id or not isinstance(history, dict):
        return
    try:
        with _LOCK:
            session = _SESSIONS.get(prompt_id)
            if session is not None:
                session["history"] = history
    except Exception:  # noqa: BLE001
        pass


def _gpu_sample() -> Optional[Dict[str, float]]:
    try:
        proc = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        values = []
        for line in proc.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                values.append((float(parts[0]), float(parts[1])))
        if not values:
            return None
        # 多 GPU 时报告最大占用，避免平均值掩盖任务卡。
        used, util = max(values, key=lambda x: x[0])
        return {"vram_mb": used, "util": util}
    except Exception:  # noqa: BLE001
        return None


def _windows_system_sample() -> Optional[Dict[str, float]]:
    if platform.system() != "Windows":
        return None
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                        ("dwHighDateTime", ctypes.c_uint32)]

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        global _PREV_SYSTEM_TIMES
        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            return None
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None

        def raw(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        now = {"idle": float(raw(idle)), "kernel": float(raw(kernel)),
               "user": float(raw(user)), "wall": time.monotonic()}
        cpu = None
        if _PREV_SYSTEM_TIMES:
            d_idle = now["idle"] - _PREV_SYSTEM_TIMES["idle"]
            d_total = ((now["kernel"] - _PREV_SYSTEM_TIMES["kernel"])
                       + (now["user"] - _PREV_SYSTEM_TIMES["user"]))
            d_wall = now["wall"] - _PREV_SYSTEM_TIMES["wall"]
            if d_total > 0 and d_wall > 0:
                cpu = max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))
        _PREV_SYSTEM_TIMES = now
        used_mb = (mem.ullTotalPhys - mem.ullAvailPhys) / (1024.0 * 1024.0)
        return {"cpu": cpu, "ram_mb": used_mb}
    except Exception:  # noqa: BLE001
        return None


def _sample_once() -> None:
    gpu = _gpu_sample()
    system = _windows_system_sample()
    with _LOCK:
        for session in _SESSIONS.values():
            samples = session["samples"]
            if gpu:
                samples["gpu_vram_used_mb"].append(round(gpu["vram_mb"], 1))
                samples["gpu_util_percent"].append(round(gpu["util"], 1))
            if system:
                if system.get("cpu") is not None:
                    samples["system_cpu_percent"].append(round(system["cpu"], 1))
                samples["system_ram_used_mb"].append(round(system["ram_mb"], 1))


def _sampler_loop() -> None:
    while not _STOP.is_set():
        try:
            _sample_once()
        except Exception as exc:  # noqa: BLE001
            logger.debug("性能采样失败（忽略）：%s", exc)
        _STOP.wait(_SAMPLE_INTERVAL)


def _stats(values: List[float]) -> Dict[str, Optional[float]]:
    clean = [float(v) for v in values]
    if not clean:
        return {"peak": None, "avg": None, "samples": 0}
    return {"peak": round(max(clean), 2),
            "avg": round(sum(clean) / len(clean), 2),
            "samples": len(clean)}


def _walk_timings(value: Any, out: Dict[str, float], prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_l = str(key).lower()
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(child, (int, float)) and not isinstance(child, bool):
                if any(token in key_l for token in
                       ("execution_time", "duration_ms", "duration_sec",
                        "elapsed_ms", "elapsed_sec", "load_time")):
                    out[path] = float(child)
            else:
                _walk_timings(child, out, path)
    elif isinstance(value, list):
        for index, child in enumerate(value[:100]):
            _walk_timings(child, out, f"{prefix}[{index}]")


def _history_metrics(history: Optional[Dict]) -> Dict[str, Any]:
    if not isinstance(history, dict):
        return {"node_times": {}, "execution_cached_nodes": [],
                "remote_duration_sec": None}
    node_times: Dict[str, float] = {}
    _walk_timings(history, node_times)
    cached: List[Any] = []
    timestamps: List[float] = []
    status = history.get("status") or {}
    for message in status.get("messages") or []:
        if not isinstance(message, (list, tuple)) or len(message) < 2:
            continue
        event, payload = message[0], message[1]
        if event == "execution_cached" and isinstance(payload, dict):
            cached.extend(payload.get("nodes") or [])
        if isinstance(payload, dict):
            ts = payload.get("timestamp")
            try:
                timestamps.append(float(ts))
            except (TypeError, ValueError):
                pass
    remote = None
    if len(timestamps) >= 2:
        remote = round(max(timestamps) - min(timestamps), 3)
        if remote < 0:
            remote = None
    return {
        "node_times": node_times,
        "execution_cached_nodes": sorted({str(x) for x in cached}),
        "execution_cached_count": len({str(x) for x in cached}),
        "remote_duration_sec": remote,
    }


def _derived(context: Dict, elapsed: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        segments = int(context.get("segment_count") or 0)
        frames = int(context.get("total_frames") or 0)
        fps = float(context.get("fps") or 0)
        if segments > 0 and elapsed > 0:
            out["seconds_per_segment"] = round(elapsed / segments, 3)
        if frames > 0 and fps > 0 and elapsed > 0:
            video_sec = frames / fps
            out["seconds_per_video_second"] = round(elapsed / video_sec, 3)
            out["video_seconds"] = round(video_sec, 3)
    except (TypeError, ValueError):
        pass
    return out


def _record(prompt_id: str, session: Dict, success: Optional[bool]) -> Dict:
    elapsed = round(time.monotonic() - session["started_monotonic"], 3)
    samples = session["samples"]
    context = session.get("context") or {}
    history = _history_metrics(session.get("history"))
    metrics = {
        "prompt_id": prompt_id,
        "started_at": session.get("started_at"),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": elapsed,
        "success": success,
        "vram": _stats(samples["gpu_vram_used_mb"]),
        "gpu_util": _stats(samples["gpu_util_percent"]),
        "system_cpu": _stats(samples["system_cpu_percent"]),
        "system_ram": _stats(samples["system_ram_used_mb"]),
        "context": context,
        **history,
        **_derived(context, elapsed),
        "collector_errors": samples.get("errors") or [],
    }
    path = ""
    try:
        os.makedirs(_DIR, exist_ok=True)
        path = os.path.join(_DIR, f"{prompt_id}.json")
        atomic_write_json(path, metrics)
    except Exception as exc:  # noqa: BLE001
        metrics["collector_errors"].append(str(exc))
    try:
        import analytics
        analytics.record_event(
            kind="performance", project=str(context.get("project") or ""),
            label=str(prompt_id), duration_sec=elapsed,
            units=int(context.get("node_count") or 0),
            success=success is not False,
            meta={k: metrics.get(k) for k in
                  ("prompt_id", "vram", "gpu_util", "system_cpu",
                   "system_ram", "node_times", "execution_cached_count",
                   "seconds_per_segment", "seconds_per_video_second")},
        )
    except Exception as exc:  # noqa: BLE001
        metrics["collector_errors"].append(str(exc))
    metrics["path"] = path
    return metrics


def finish(prompt_id: str, success: Optional[bool] = None) -> Optional[Dict]:
    """结束采样并落盘；返回本次性能快照。"""
    if not prompt_id:
        return None
    try:
        with _LOCK:
            session = _SESSIONS.pop(prompt_id, None)
            if session is None:
                return None
            if session.get("history") is not None and success is None:
                status = (session["history"].get("status") or {})
                if status.get("status_str") in ("error", "timeout"):
                    success = False
                elif status.get("completed") or status.get("status_str") == "success":
                    success = True
            if not _SESSIONS:
                _STOP.set()
            metrics = _record(prompt_id, session, success)
        return metrics
    except Exception as exc:  # noqa: BLE001
        logger.warning("性能统计收尾失败（不影响生成）：%s", exc)
        return None


def active_count() -> int:
    with _LOCK:
        return len(_SESSIONS)

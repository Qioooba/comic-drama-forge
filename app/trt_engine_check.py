# -*- coding: utf-8 -*-
"""TRT VAE engine 启动前自检（P0-3）。

状态严格三值：
  可用   —— 已完成真实加载探针；
  不兼容 —— 文件缺失/空文件，或 ComfyUI 明确报 TensorRT/CUDA/架构不兼容；
  未测试 —— 文件存在但尚未完成加载探针，或探针无法得出结论。

探针使用最小 Encode→Decode→Preview 工作流，两个 engine 都会被真正执行到，
而不是只检查“文件存在”。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from typing import Dict, List, Optional

import requests

import config
import deps_check
from fs_atomic import atomic_write_json, read_json_strict

ENGINE_SPECS = (
    {"key": "decoder", "name": "minimax_h3_vae_decoder_w4a16_awq.engine"},
    {"key": "encoder", "name": "minimax_h3_vae_encoder.engine"},
)
_CACHE_PATH = os.path.join(config.PROJECT_OUTPUT_DIR, "trt_engine_status.json")
_PROBE_LOCK = threading.Lock()
_COMPAT_ERROR = re.compile(
    r"deserialize|tensorrt|cuda|gpu arch|compute capability|engine file|"
    r"failed to create execution context|out of vram",
    re.I,
)


def _find_engine(name: str) -> str:
    roots = deps_check._model_roots(config.MODELS_DIR)
    # 先走常见 vae/ 目录直查：启动自检不应为了两个已知文件扫完整棵模型树。
    for root in roots:
        candidate = os.path.join(root, "vae", name)
        if os.path.isfile(candidate):
            return candidate
    index = deps_check._walk_index(roots) if roots else {}
    hits = index.get(name.lower()) or []
    preferred = [h for h in hits
                 if h.get("rel", "").replace("\\", "/").lower() == f"vae/{name.lower()}"]
    hit = (preferred or hits or [None])[0]
    if hit:
        return os.path.join(hit["root"], hit["rel"])
    # checklist/递归索引都没找到时，保留一条明确的候选路径供错误信息展示。
    return os.path.join(roots[0], "vae", name) if roots else ""


def _gpu_info() -> Dict[str, str]:
    info = {"name": "", "driver": "", "compute_cap": ""}
    try:
        proc = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,driver_version,compute_cap",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts = [p.strip() for p in proc.stdout.splitlines()[0].split(",")]
            if len(parts) >= 3:
                info.update(name=parts[0], driver=parts[1], compute_cap=parts[2])
    except Exception:  # noqa: BLE001  GPU 信息不可用不阻断静态自检
        pass
    return info


def _static_report() -> Dict:
    engines = []
    for spec in ENGINE_SPECS:
        path = _find_engine(spec["name"])
        exists = bool(path and os.path.isfile(path))
        size = os.path.getsize(path) if exists else 0
        engines.append({
            "key": spec["key"],
            "name": spec["name"],
            "path": path,
            "exists": exists,
            "size": size,
            "ok": bool(exists and size > 0),
        })
    reasons = [f"{e['name']} 缺失" if not e["exists"]
               else f"{e['name']} 文件为空" if e["size"] <= 0
               else "" for e in engines]
    return {
        "engines": engines,
        "ok": all(e["ok"] for e in engines),
        "reason": "; ".join(x for x in reasons if x),
    }


def _fingerprint(static: Dict, gpu: Dict) -> str:
    payload = {
        "gpu": gpu,
        "engines": [
            {"name": e["name"], "size": e["size"],
             "mtime": os.path.getmtime(e["path"]) if e["path"] and os.path.isfile(e["path"]) else 0}
            for e in static["engines"]
        ],
        # 当前应用进程无法直接导入 ComfyUI 内的 TensorRT；engine 自身 mtime/size
        # 与 GPU/driver 已足够识别“环境或文件变了，旧探针结果失效”。
        "trt_version": "unknown",
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _load_cache(fingerprint: str) -> Optional[Dict]:
    try:
        data = read_json_strict(_CACHE_PATH, default={})
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict) and data.get("fingerprint") == fingerprint:
        return data
    return None


def _save_cache(report: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        atomic_write_json(_CACHE_PATH, report)
    except Exception:  # noqa: BLE001  缓存失败不影响本次报告
        pass


def _combo_value(object_info: Optional[Dict], name: str) -> str:
    """从 MiniMaxH3TRTVAELoader 的 decoder/encoder combo 找到该文件的写法。"""
    if not object_info:
        return ""
    blob = object_info.get("MiniMaxH3TRTVAELoader") or {}
    req = (blob.get("input") or {}).get("required") or {}
    for field in ("decoder", "encoder"):
        spec = req.get(field)
        if not isinstance(spec, (list, tuple)) or not spec:
            continue
        opts = spec[0]
        if not isinstance(opts, (list, tuple)):
            continue
        for value in opts:
            if str(value).rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower() == name.lower():
                return str(value)
    return ""


def _build_probe_prompt(decoder: str, encoder: str) -> Dict:
    # 256×256 单图 Encode→Decode→Preview：编码器和解码器都会执行到，
    # 同时避免整段视频采样；这是“加载/执行兼容性”探针，不是画质测试。
    return {
        "1": {
            "class_type": "MiniMaxH3TRTVAELoader",
            "inputs": {"decoder": decoder, "encoder": encoder},
        },
        "2": {
            "class_type": "EmptyImage",
            "inputs": {"width": 256, "height": 256, "batch_size": 1, "color": 0},
        },
        "3": {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["2", 0], "vae": ["1", 0]},
        },
        "4": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["1", 0]},
        },
        "5": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["4", 0]},
        },
    }


def _error_text(entry: Dict) -> str:
    status = entry.get("status") or {}
    messages = status.get("messages") or []
    try:
        return json.dumps(messages, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(messages)


def _run_probe(static: Dict, timeout: float = 90.0) -> Dict:
    if not static["ok"]:
        return {"status": "不兼容", "reason": static["reason"], "probed": True}
    try:
        object_info = requests.get(
            f"{config.COMFYUI_URL}/object_info", timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        return {"status": "未测试", "reason": f"无法读取 ComfyUI object_info：{exc}",
                "probed": False}
    decoder = _combo_value(object_info, ENGINE_SPECS[0]["name"])
    encoder = _combo_value(object_info, ENGINE_SPECS[1]["name"])
    if not decoder or not encoder:
        return {"status": "不兼容",
                "reason": "ComfyUI object_info 未同时列出 decoder/encoder engine",
                "probed": True}

    prompt = _build_probe_prompt(decoder, encoder)
    try:
        response = requests.post(
            f"{config.COMFYUI_URL}/prompt",
            json={"prompt": prompt, "client_id": "comic-drama-forge-trt-probe"},
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "未测试", "reason": f"ComfyUI 探针提交失败：{exc}",
                "probed": False}
    if response.status_code != 200:
        return {"status": "未测试",
                "reason": f"ComfyUI 拒绝探针（HTTP {response.status_code}）："
                          f"{response.text[:300]}",
                "probed": False}
    try:
        prompt_id = response.json().get("prompt_id")
    except Exception:  # noqa: BLE001
        return {"status": "未测试", "reason": "探针响应缺少 prompt_id", "probed": False}
    if not prompt_id:
        return {"status": "未测试", "reason": "探针响应缺少 prompt_id", "probed": False}

    deadline = time.time() + timeout
    entry: Dict = {}
    while time.time() < deadline:
        try:
            history = requests.get(
                f"{config.COMFYUI_URL}/history/{prompt_id}", timeout=10).json()
            entry = history.get(prompt_id) or {}
        except Exception:  # noqa: BLE001
            entry = {}
        if entry:
            break
        time.sleep(0.25)
    if not entry:
        return {"status": "未测试", "reason": f"探针等待超时（{int(timeout)}s）",
                "probed": False, "prompt_id": prompt_id}

    status = entry.get("status") or {}
    completed = bool(status.get("completed")) or status.get("status_str") == "success"
    text = _error_text(entry)
    if completed:
        return {"status": "可用", "reason": "Encode→Decode→Preview 探针成功",
                "probed": True, "prompt_id": prompt_id}
    if _COMPAT_ERROR.search(text):
        return {"status": "不兼容", "reason": text[:800],
                "probed": True, "prompt_id": prompt_id}
    return {"status": "未测试", "reason": f"探针执行失败但未能归类：{text[:800]}",
            "probed": True, "prompt_id": prompt_id}


def check(client=None, probe: bool = False, timeout: float = 90.0) -> Dict:
    """检查两个 TRT engine；``probe=True`` 才提交真实加载探针。"""
    static = _static_report()
    gpu = _gpu_info()
    fingerprint = _fingerprint(static, gpu)
    cached = _load_cache(fingerprint)
    if cached and not probe:
        return {**cached, "cached": True, "checked_from_cache": True}
    runtime = {"status": "未测试", "reason": "尚未执行加载探针", "probed": False}
    if probe:
        if not _PROBE_LOCK.acquire(blocking=False):
            runtime = {"status": "未测试", "reason": "已有探针正在执行", "probed": False}
        else:
            try:
                runtime = _run_probe(static, timeout=timeout)
            finally:
                _PROBE_LOCK.release()
    status = "不兼容" if not static["ok"] else runtime["status"]
    report = {
        "success": True,
        "status": status,
        "status_label": status,
        "reason": static["reason"] or runtime.get("reason", ""),
        "static": static,
        "runtime": runtime,
        "environment": {**gpu, "trt_version": "unknown",
                        "comfyui_url": config.COMFYUI_URL},
        "fingerprint": fingerprint,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "probe_requested": bool(probe),
        "cached": False,
    }
    if probe and runtime.get("probed"):
        _save_cache(report)
    return report

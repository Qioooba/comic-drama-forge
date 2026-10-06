# -*- coding: utf-8 -*-
"""P2-12：已实测稳定参数的机器可读锁定与启动审计。

这里只**检测**漂移，不修改任何参数。任何性能优化若触碰这些值，启动日志与
``tests/test_stable_profile.py`` 会立即暴露，随后必须按 P2-12 走完整 A/B。
"""

from __future__ import annotations

import json
import os
from typing import Dict, List

import config

EXPECTED = {
    "DEPLOY_PROFILE": "16G",
    "TASK_QUEUE_CONCURRENCY": 1,
    "H3_ENABLE_REFINE": False,
    "H3_DISABLE_DLSS": True,
    "H3_WORKFLOW": "h3_director_r2v_单采.json",
    "H3_UNET": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "QWEN_UNET": "qwen_image_2.1_int8_convrot.safetensors",
    "QWEN_CLIP": "qwen3vl_8b_int8_convrot.safetensors",
    "QWEN_VAE": "qwen_image_2.1_vae_bf16.safetensors",
    "SOLATTN": {
        "tau": 1.3, "start_percent": 0.2, "end_percent": 0.9,
        "min_tokens": 4096, "int8_qk": True,
        "sink_conditioning": "exact_kv_and_rows",
        "morton": False, "morton_curve": "3d", "int8_pv": True,
        "verbose": False, "use_tma": False, "dense_blocks": "0-2,-1",
    },
    "EASY_CACHE": {
        "reuse_threshold": 0.3, "start_percent": 0.2,
        "end_percent": 0.9, "verbose": False,
    },
    "TE_SPEED_MINIMAX": {
        "processing_control_value": 0.08,
        "processing_percent_1": 0.1,
        "processing_percent_2": 0.9,
        "mcs": 2, "device": "auto",
    },
}


def _load_workflow(name: str) -> Dict:
    path = config.resolve_workflow_path(name)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _nodes(workflow: Dict, class_type: str) -> List[Dict]:
    return [n for n in (workflow.get("nodes") or [])
            if isinstance(n, dict) and n.get("type") == class_type]


def _widgets(node: Dict) -> Dict:
    value = node.get("widgets_values_named")
    if isinstance(value, dict):
        return value
    return {}


def _basename(value: str) -> str:
    return os.path.basename(str(value or "").replace("\\", "/"))


def current_profile() -> Dict:
    """返回当前运行时/模板实际生效的稳定参数快照。"""
    h3 = _load_workflow(EXPECTED["H3_WORKFLOW"])
    image = _load_workflow(config.WORKFLOW_TEMPLATE.get("character_gen", ""))
    director = _widgets((_nodes(h3, "MiniMaxH3Director") or [{}])[0])
    solattn = _widgets((_nodes(h3, "SolAttnPatch") or [{}])[0])
    easy = _widgets((_nodes(h3, "EasyCache") or [{}])[0])
    te = _widgets((_nodes(h3, "TESpeedMiniMaxH3") or [{}])[0])
    unet = _widgets((_nodes(image, "UNETLoader") or [{}])[0])
    clip = _widgets((_nodes(image, "CLIPLoader") or [{}])[0])
    vae = _widgets((_nodes(image, "VAELoader") or [{}])[0])
    h3_unet = _widgets((_nodes(h3, "UNETLoader") or [{}])[0])
    return {
        "DEPLOY_PROFILE": config.DEPLOY_PROFILE,
        "TASK_QUEUE_CONCURRENCY": config.TASK_QUEUE_CONCURRENCY,
        "H3_ENABLE_REFINE": config.H3_ENABLE_REFINE,
        "H3_DISABLE_DLSS": config.H3_DISABLE_DLSS,
        "H3_WORKFLOW": config.WORKFLOW_TEMPLATE.get("h3_video", ""),
        "H3_UNET": _basename(h3_unet.get("unet_name", "")),
        "QWEN_UNET": _basename(unet.get("unet_name", "")),
        "QWEN_CLIP": _basename(clip.get("clip_name", "")),
        "QWEN_VAE": _basename(vae.get("vae_name", "")),
        "H3_DIRECTOR": {k: director.get(k) for k in
                        ("width", "height", "frame_rate", "steps",
                         "sampler", "scheduler")},
        "SOLATTN": {k: solattn.get(k) for k in EXPECTED["SOLATTN"]},
        "EASY_CACHE": {k: easy.get(k) for k in EXPECTED["EASY_CACHE"]},
        "TE_SPEED_MINIMAX": {k: te.get(k) for k in EXPECTED["TE_SPEED_MINIMAX"]},
    }


def violations() -> List[str]:
    """返回稳定参数漂移列表；空列表表示与已实测红线一致。"""
    current = current_profile()
    out: List[str] = []
    for key in ("DEPLOY_PROFILE", "TASK_QUEUE_CONCURRENCY",
                "H3_ENABLE_REFINE", "H3_DISABLE_DLSS",
                "H3_WORKFLOW", "H3_UNET", "QWEN_UNET", "QWEN_CLIP", "QWEN_VAE"):
        if current.get(key) != EXPECTED[key]:
            out.append(f"{key}: expected={EXPECTED[key]!r}, actual={current.get(key)!r}")
    for group in ("SOLATTN", "EASY_CACHE", "TE_SPEED_MINIMAX"):
        for key, expected in EXPECTED[group].items():
            actual = (current.get(group) or {}).get(key)
            if actual != expected:
                out.append(f"{group}.{key}: expected={expected!r}, actual={actual!r}")
    return out


def report() -> Dict:
    found = violations()
    return {
        "ok": not found,
        "violations": found,
        "expected": EXPECTED,
        "current": current_profile(),
    }

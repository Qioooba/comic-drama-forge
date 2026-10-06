# -*- coding: utf-8 -*-
"""实际提交参数快照（P0-5）：模板 ≠ 最终提交给 ComfyUI 的值。

在 ``queue_prompt`` 拿到 prompt_id 后立即落盘 ``output/actual_params/<id>.json``，
记录最终 API prompt 的节点数、模型、尺寸、seed、段数/帧数、开关和工作流路径/hash。
快照是排障证据，不参与生成决策；任何失败都只 warning，不影响提交。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)
_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "actual_params",
)


def _jsonish(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{":
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return None
    return None


def _first_input(inputs: Dict, names: Iterable[str]) -> Any:
    for name in names:
        if name in inputs and inputs[name] not in (None, ""):
            return inputs[name]
    return None


def _timeline_summary(value: Any) -> Dict:
    data = _jsonish(value)
    if not isinstance(data, dict):
        return {}
    segments = data.get("segments")
    if not isinstance(segments, list):
        segments = []
    frames: List[int] = []
    durations: List[float] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        frame_value = _first_input(segment, ("frameCount", "frame_count", "frames"))
        try:
            frames.append(int(frame_value))
        except (TypeError, ValueError):
            pass
        duration_value = _first_input(segment, ("duration", "duration_sec", "seconds"))
        try:
            durations.append(float(duration_value))
        except (TypeError, ValueError):
            pass
    summary = {
        "segment_count": len(segments) or None,
        "segment_frames": frames or None,
        "total_frames": sum(frames) if frames else None,
        "total_duration_sec": round(sum(durations), 3) if durations else None,
    }
    for key in ("audioMode", "audio_mode", "continuity", "overlapFrames",
                "overlap_frames", "commonRefs", "common_refs", "fps"):
        if key in data:
            summary[key] = data[key]
    return summary


def extract(api_prompt: Dict[str, Any],
            workflow_meta: Optional[Dict[str, Any]] = None,
            prompt_id: str = "") -> Dict[str, Any]:
    """从最终 ``api_prompt`` 提取可读的实际参数。"""
    nodes = api_prompt if isinstance(api_prompt, dict) else {}
    result: Dict[str, Any] = {
        "prompt_id": prompt_id,
        "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "node_count": len(nodes),
        "workflow_path": (workflow_meta or {}).get("path", ""),
        "workflow_file": (workflow_meta or {}).get("file", ""),
        "workflow_hash": (workflow_meta or {}).get("hash", ""),
        "models": {},
        "width": None,
        "height": None,
        "fps": None,
        "seed": None,
        "timeline": {},
        "segment_count": None,
        "total_frames": None,
        "segment_frames": None,
        "audio_mode": None,
        "continuity": None,
        "overlap_frames": None,
        "common_refs": None,
        "refine_present": False,
        "dlss_present": False,
        "refine_expected": None,
        "dlss_bypassed": None,
        "node_types": [],
    }
    types: List[str] = []
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        if class_type:
            types.append(class_type)
        if class_type == "UNETLoader":
            result["models"]["unet_main"] = inputs.get("unet_name")
        elif class_type == "CLIPLoader":
            result["models"]["clip"] = inputs.get("clip_name")
        elif class_type == "VAELoader":
            result["models"]["vae_audio"] = inputs.get("vae_name")
        elif class_type == "LoraLoaderModelOnly":
            result["models"].setdefault("loras", []).append(inputs.get("lora_name"))
        elif class_type == "MiniMaxH3TRTVAELoader":
            result["models"]["vae_video_decoder"] = inputs.get("decoder")
            result["models"]["vae_video_encoder"] = inputs.get("encoder")

        width = _first_input(inputs, ("width", "image_width"))
        height = _first_input(inputs, ("height", "image_height"))
        if isinstance(width, int) and result["width"] is None:
            result["width"] = width
        if isinstance(height, int) and result["height"] is None:
            result["height"] = height
        fps = _first_input(inputs, ("fps", "frame_rate", "frameRate"))
        if fps is not None and result["fps"] is None:
            result["fps"] = fps
        seed = _first_input(inputs, ("seed", "noise_seed"))
        if seed is not None and result["seed"] is None:
            result["seed"] = seed

        timeline_value = _first_input(inputs, ("timeline_data", "timeline", "segments"))
        timeline = _timeline_summary(timeline_value)
        if timeline and not result["timeline"]:
            result["timeline"] = timeline
            result["segment_count"] = timeline.get("segment_count")
            result["total_frames"] = timeline.get("total_frames")
            result["segment_frames"] = timeline.get("segment_frames")
            result["audio_mode"] = timeline.get("audioMode") or timeline.get("audio_mode")
            result["continuity"] = timeline.get("continuity")
            result["overlap_frames"] = (timeline.get("overlapFrames")
                                        or timeline.get("overlap_frames"))
            result["common_refs"] = timeline.get("commonRefs") or timeline.get("common_refs")
            if timeline.get("fps") is not None:
                result["fps"] = timeline["fps"]

        lower_type = class_type.lower()
        if "refine" in lower_type:
            result["refine_present"] = True
        if "dlss" in lower_type:
            result["dlss_present"] = True

    result["node_types"] = sorted(set(types))
    # 开关的实际执行口径来自 config（模板节点存在 ≠ 实际执行）。
    try:
        import config
        result["refine_expected"] = bool(getattr(config, "H3_ENABLE_REFINE", False))
        result["dlss_bypassed"] = bool(getattr(config, "H3_DISABLE_DLSS", False))
    except Exception:  # noqa: BLE001
        pass
    if not result["refine_present"]:
        result["refine_expected"] = False
    return result


def save(snapshot: Dict[str, Any]) -> str:
    prompt_id = str(snapshot.get("prompt_id") or "").strip()
    if not prompt_id:
        return ""
    path = os.path.join(_DIR, f"{prompt_id}.json")
    os.makedirs(_DIR, exist_ok=True)
    atomic_write_json(path, snapshot)
    return path


def latest(limit: int = 20) -> List[Dict[str, Any]]:
    try:
        files = [os.path.join(_DIR, name) for name in os.listdir(_DIR)
                 if name.endswith(".json")]
    except OSError:
        return []
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.isfile(p) else 0,
               reverse=True)
    out: List[Dict[str, Any]] = []
    for path in files[:max(1, min(int(limit), 100))]:
        try:
            data = read_json_strict(path, default={})
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            data["snapshot_path"] = path
            out.append(data)
    return out


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()

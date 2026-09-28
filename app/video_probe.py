"""视频元信息探测（纯 ffprobe，无任何模型/工作流依赖）。

历史背景：``probe_video`` 与 ``UpscaleError`` 原本定义在 ``upscale_client.py`` 里，
但 ``probe_video`` 是通用能力（成片验收 / 混音 prepare / 视频信息接口等多处复用），
与 FlashVSR 超分引擎无耦合。2026-09-27 移除整套超分引擎时，把这两个通用定义
迁到本文件，避免「删超分」连带删掉仍在使用的视频探测能力。
"""
import os
import json
import shutil
import subprocess
from typing import Dict, Optional


class UpscaleError(RuntimeError):
    """视频探测/处理失败（含明确原因）。

    ⚠️ 名字沿用历史（原属超分模块），现作为通用「视频处理失败」异常被
    app.py 的混音/验收接口防御性捕获；语义上已不再专指超分。
    """


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def probe_video(path: str, timeout: int = 60) -> Dict:
    """用 ffprobe 读取视频真实参数（分辨率 / 帧率 / 帧数 / 时长 / 体积 / 是否有音轨）

    失败时返回 {'ok': False, 'error': ...}，绝不猜测。
    """
    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    ffprobe = _which("ffprobe") or "ffprobe"
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        # 显式指定 utf-8：避免 Windows GBK 默认解码把 ffprobe 输出（含中文路径）解坏
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "error": "未找到 ffprobe，可执行文件不在 PATH 中"}
    except Exception as e:
        return {"ok": False, "error": f"ffprobe 执行异常: {e}"}
    if r.returncode != 0:
        return {"ok": False, "error": f"ffprobe 返回 {r.returncode}: {(r.stderr or '').strip()[:300]}"}
    try:
        data = json.loads(r.stdout or "{}")
    except Exception as e:
        return {"ok": False, "error": f"ffprobe 输出解析失败: {e}"}

    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        return {"ok": False, "error": "未找到视频流"}
    fps = 0.0
    try:
        num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except Exception:
        fps = 0.0
    size = 0
    try:
        size = int((data.get("format") or {}).get("size") or os.path.getsize(path))
    except Exception:
        size = os.path.getsize(path)
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 3),
        "frames": int(v.get("nb_frames") or 0),
        "duration": round(float((data.get("format") or {}).get("duration") or 0), 3),
        "size_bytes": size,
        "size_mb": round(size / 1048576, 2),
        "video_codec": v.get("codec_name"),
        "has_audio": bool(a is not None),
        "audio_codec": (a or {}).get("codec_name"),
    }

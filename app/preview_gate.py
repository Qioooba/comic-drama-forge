# -*- coding: utf-8 -*-
"""两级生产：低成本预演 → 人工批准 → 正式生产（借鉴 ai-manga-factory）

要解决的问题
------------
此前点「生成视频」就直接跑**正式**成片。H3 整集一次要跑几十分钟（22~44 段），
如果构图/身份/机位一开始就是错的，这几十段 GPU 时间全是白烧 —— 而且要在
几十分钟后才看得出问题。

两级生产把「**先看一眼**」变成流程里的一等公民：先出一版**低成本预演**，
人工看过、批准了，才排正式生产。

铁律：预演产物**永不可交付**
---------------------------
预演是低分辨率 + 短时长的**决策辅助物**：它长得像成片，但绝不是成片。
一旦混进交付物索引，用户就会拿一版糊图去发布 —— 这比多烧一次 GPU 严重得多。
所以本模块提供 deliverable_ok()，并在成片登记入口硬拦。

降本手段（都走**现有调用参数**，不碰工作流内部）
----------------------------------------------
* **分辨率**：H3 Director 的 size 参数会直接落到 timeline 的 width/height，
  缩小它就是降显存 + 降耗时；
* **时长**：每段 duration 截短 —— 段数不变（每一镜都能看到构图与身份），
  总时长变短，成本随总时长线性下降。

两者都是**纯数据变换**（本模块核心函数即为此），因此可离线完整测试。

开关默认**关**（零行为变更）：MJSCXT_PREVIEW_BEFORE_FINAL=1 打开。
"""
from __future__ import annotations

import copy
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fs_atomic import atomic_write_json, read_json_strict

import quality_stage

logger = logging.getLogger(__name__)

__all__ = [
    "PREVIEW_MARK", "enabled", "preview_size", "preview_segments",
    "preview_prefix", "is_preview_path", "deliverable_ok",
    "approve", "approval", "needs_preview", "clear_approval",
]

#: 预演产物的标记。出现在文件名/目录名里即视为「不可交付」。
PREVIEW_MARK = "_preview_"
#: 预演分辨率倍率（相对正式）。
DEFAULT_PREVIEW_SCALE = 0.5
#: 预演每段时长上限（秒）。段数不变 → 每镜都看得到，总时长大幅缩短。
DEFAULT_PREVIEW_SEGMENT_SEC = 2.0
#: 预演画幅下限（避免缩到模型根本画不出结构）。
MIN_PREVIEW_SIDE = 256
#: 预演每段时长的下限（与 h3_prompt_kit 的段时长下限同量级）。
MIN_PREVIEW_SEGMENT_SEC = 0.5


def enabled() -> bool:
    """预演开关（**默认关**）。读取失败按关闭处理：出片优先。"""
    try:
        from config import PREVIEW_BEFORE_FINAL
        return bool(PREVIEW_BEFORE_FINAL)
    except Exception as e:                                   # noqa: BLE001
        logger.debug("预演开关读取失败，按关闭处理：%s", e)
        return False


def _even(n: int) -> int:
    """视频编码要求偶数边长；向上取偶（宁大不小，避免比下限还小）。"""
    n = int(n)
    return n if n % 2 == 0 else n + 1


def preview_size(size, *, scale: float = None,
                 min_side: int = MIN_PREVIEW_SIDE) -> Optional[Tuple[int, int]]:
    """把正式画幅缩成预演画幅。size 非法/为空时返回 None（= 沿用模板画幅）。"""
    if not size:
        return None
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    if w <= 0 or h <= 0:
        return None
    s = DEFAULT_PREVIEW_SCALE if scale is None else float(scale)
    s = min(max(s, 0.05), 1.0)
    return (_even(max(min_side, round(w * s))), _even(max(min_side, round(h * s))))


def preview_segments(segments: Optional[List[dict]], *,
                     cap_sec: float = None) -> List[dict]:
    """把正式分段截成预演分段：**段数不变**，每段时长压到上限。

    段数不变是刻意的 —— 预演的价值就在于「每一镜都先看一眼构图与身份」；
    若为了省钱直接砍掉后半段，就失去了预演的意义（尾段的问题看不到）。
    """
    cap = DEFAULT_PREVIEW_SEGMENT_SEC if cap_sec is None else float(cap_sec)
    cap = max(MIN_PREVIEW_SEGMENT_SEC, cap)
    out: List[dict] = []
    for seg in (segments or []):
        s = copy.deepcopy(dict(seg or {}))
        try:
            dur = float(s.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        # 只压不放：本来就比上限短的段保持原样（预演不该比正式还长）
        s["duration"] = round(min(dur, cap), 3) if dur > 0 else cap
        s["_preview"] = True
        out.append(s)
    return out


def preview_prefix(prefix: str) -> str:
    """正式的输出前缀 → 预演前缀（带 PREVIEW_MARK，供不可交付判据识别）。"""
    return str(prefix or "comic_drama/episode") + PREVIEW_MARK + "preview"


def is_preview_path(path: str) -> bool:
    """路径是否属于预演产物。**只判文件名**。

    ⚠️ 早期实现扫的是整个路径，结果只要用户目录里出现 "_preview_"
    （例如 C:/Users/x/my_preview_work/...），**所有正式成片都会被判成预演并一律拒收**
    —— 由 verify_preview_gate.py 的 C4/F3 抓到。
    预演产物的命名规则是 "<集标签>_preview_N.mp4"，判文件名既足够又精确。
    """
    name = os.path.basename(str(path or "").replace("\\", "/"))
    return PREVIEW_MARK in name.lower()


def deliverable_ok(path: str) -> Tuple[bool, str]:
    """能否登记为**可交付**成片。返回 (ok, reason)。

    这是本模块最重要的一个函数：预演产物长得像成片，一旦混进交付物索引，
    用户就可能拿一版低分辨率糊图去发布 —— 必须在这里硬拦。
    """
    if not path:
        return False, "路径为空"
    if is_preview_path(path):
        return False, ("这是**预演**产物（低分辨率/短时长），永远不可交付；"
                       "请批准预演后再生产正式成片")
    return True, ""


# =====================================================================
# 预演批准（复用 quality_stage 的哈希绑定，语义一致）
# =====================================================================

def _state_path(project: str, episode: Any) -> str:
    from config import PROJECT_OUTPUT_DIR
    return os.path.join(PROJECT_OUTPUT_DIR, "qc", str(project or ""),
                        quality_stage._norm_ep(episode), "preview.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def approve(project: str, episode: Any, preview_path: str, *, note: str = "") -> Dict[str, Any]:
    """批准某次预演，并**绑定该预演产物**（换了一版预演则批准自动失效）。

    绑定复用 quality_stage.make_binding：预演重出一次 sha 就变了，
    旧批准不会再放行 —— 避免「批准的是上一版预演」。
    """
    binding = quality_stage.make_binding(preview_path or "",
                                         contract={"episode": str(episode)})
    state = {"approved": True, "note": str(note or ""), "preview_path": preview_path,
             "binding": binding, "at": _now()}
    try:
        atomic_write_json(_state_path(project, episode), state)
        logger.info("预演已获批准：%s 第 %s 集（%s）", project, episode,
                    str(binding.get("artifact_sha256") or "")[:12])
    except Exception as e:                                   # noqa: BLE001
        logger.warning("预演批准落盘失败：%s", e)
    return state


def approval(project: str, episode: Any,
             preview_path: Optional[str] = None) -> Dict[str, Any]:
    """读预演批准状态；给了当前预演路径则**校验绑定是否仍然成立**。

    返回 {approved, valid, reason, ...}。读失败一律按「未批准」处理 ——
    宁可让人再批一次，也不放过一版未批的内容。
    """
    try:
        st = read_json_strict(_state_path(project, episode), {}) or {}
    except Exception as e:                                   # noqa: BLE001
        logger.warning("预演批准读取失败（按未批准处理）：%s", e)
        return {"approved": False, "valid": False, "reason": "批准记录不可读"}
    if not st.get("approved"):
        return {"approved": False, "valid": False, "reason": "尚未批准预演"}
    if preview_path:
        status, reason = quality_stage.check_binding(
            st.get("binding"), preview_path, contract={"episode": str(episode)})
        if status == "invalid":
            return {"approved": False, "valid": False,
                    "reason": "预演已变化：%s" % reason, "state": st}
        if status == "unbound":
            return {"approved": False, "valid": False,
                    "reason": "批准未绑定到具体预演产物（无法确认批的是哪一版）",
                    "state": st}
    return {"approved": True, "valid": True, "reason": "", "state": st}


def needs_preview(project: str, episode: Any,
                  preview_path: Optional[str] = None) -> Tuple[bool, str]:
    """当前是否**必须先出/重出预演**。开关关掉时永远返回 (False, ...)。"""
    if not enabled():
        return False, "预演开关未开启（默认关）"
    appr = approval(project, episode, preview_path)
    if appr.get("approved"):
        return False, "已批准"
    return True, str(appr.get("reason") or "尚未批准预演")


def clear_approval(project: str, episode: Any) -> bool:
    """清掉批准（例如用户想重新看一版预演）。"""
    try:
        p = _state_path(project, episode)
        if os.path.isfile(p):
            os.remove(p)
            return True
    except OSError as e:
        logger.warning("清除预演批准失败：%s", e)
    return False

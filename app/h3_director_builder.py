# -*- coding: utf-8 -*-
"""H3 Director 工作流 · 单次调用构建器（程序化注入 timeline_data）

背景：为什么需要这个模块
------------------------
项目原先用 ``h3_episode_builder.H3EpisodeBuilder``：以 ``H3信号10段测试001.json``
为母版，按分镜数**重建整张图**（N 个子图实例 + N-1 个
``H3ContinuousSeamlessJoinV14``），靠 latent + handover 做段间无缝续接。

2026-09-27 用户要求把视频生成工作流换成自己的整合工作流
``minimax_h3_director_二采_加速.json``——官方
`ComfyUI_MiniMaxH3_Director <https://github.com/AIMixer/ComfyUI_MiniMaxH3_Director>`_
插件。它的形态与旧母版**根本不同**：

======================  ==============================  ==============================
                        H3EpisodeBuilder（旧）            H3DirectorBuilder（本模块）
======================  ==============================  ==============================
工作流结构              10 个子图实例 + 9 个 join         扁平单实例（23 节点）
段数来源                由调用方分镜数**重建拓扑**        一个 ``MiniMaxH3Director`` 吃整条 timeline
段间衔接                ``H3ContinuousSeamlessJoinV14``   插件原生「段间引导」（尾 22 帧钉进下一段）
参考图                  ``qwen_reference_1/2`` 两个槽      ``segment.refs`` **逐段**（最多 9 张/段）
二采                    子图内部自带                       外接 ``MiniMaxH3DirectorRefine``
======================  ==============================  ==============================

因此**不能**把 Director 工作流塞给 ``H3EpisodeBuilder``——它没有 ``definitions.subgraphs``，
``analyze()`` 会返回空段链并抛「模板中未找到 H3 段实例」。

本构建器的职责**不是重建拓扑**（Director 工作流本身单实例，无需复制），而是：

1. 按调用方分镜程序化生成 ``timeline_data``（``segments`` / ``totalFrames`` / refs）；
2. 注入**逐段**``segment.refs``（参考图走 ComfyUI ``input/`` 相对文件名，
   每段的第 j 张 = 该段提示词里的 ``<Picture {j+1}>``）；
3. 改写 SaveVideo 的 ``filename_prefix``（新模板输出链已是**单一成片**：
   ``Director.images → CreateVideo → NvidiaDLSSFrameInterpolation → DLSSNR_Video →
   SaveVideo``，不再有「一采」那路 SaveVideo 需要裁），让「一次调用 = 一个 mp4」，
   保住 ``shot_XX.mp4`` 契约；
4. 把一采链上的 ``SolAttnPatch`` 参数**完全跟随模板**（``_SOLATTN_ALIGNED_*``，
   不注入任何 LowVRAM / ChunkFF / Sage 节点——``_insert_lowvram_patches`` /
   ``_insert_refine_accel_chain`` 均已退役）。

关键协议事实（读插件源码 + 实测确认，改动前务必先看这些）
--------------------------------------------------------
* **只有顶层 ``segments`` 被读取**；``batchWorkspaces`` 是前端工作区镜像，
  Python 侧零引用。``timelineMode`` 必须让 ``task_key`` 落进 gen 视频批路径
  （``prompt_batch`` + r2v），否则会走「源视频时间轴」分支并在缺 ``video`` 时报错。
* **``durationSec`` 优先于 ``frameCount``**（video batch 任务）。帧数换算唯一权威是
  官方公式：``max(5, round(sec*fps))`` 再吸附到 **17k+5** 网格。本模块的
  ``frames_for_duration`` 就是它的纯函数复刻，保证 ``total_frames`` 诚实。
* **帧数必须与该公式自洽**：``durationSec`` 写 ``frames/FPS``，插件重算才能得到同一帧数。
* ⭐ **参考图挂在 ``segment.refs``，不是 ``global.refs``**：``timelineMode=prompt_batch``
  时插件**强制** ``edit_mode = "segment"``（``gen_timeline.py:392``），段 refs 只从
  ``seg_data["refs"]`` 读（``gen_timeline.py:529``）；``global.refs`` 仅在
  ``commonEnabled=true`` 时才会 merge 进来当公共底图（同 index 段级优先）。
  项目逐镜分镜图各不相同 → 必须逐段挂；否则整集所有镜头都会用上同一张图。
* **段 refs 条数上限 = 该段提示词实际声明的 ``<Picture N>`` 个数**（见
  :func:`picture_capacity`）。插件 ``reinforce_r2v_prompt`` 只在**完全没有**
  ``<Picture`` 标签时才补标签，多塞的图会以「有 tag、无声明」进 conditioning。
* **``refs[].imageFile`` 是 ComfyUI ``input/`` 下的相对文件名**，
  不支持绝对路径；``<Picture N>`` 的 N = ``refs[].index + 1``。
* **``control_after_generate`` / ``minimax_director_ui`` 是前端伪控件**，
  不在节点 INPUT_TYPES 里，必须从 ``widgets_values_named`` 剥掉，
  否则会作为未知输入进 API prompt（``validate_api_prompt`` 会记成 unexpected_inputs）。
* **``global.prompt`` 优先于节点 widget ``global_prompt``**。本模块**两处都留空**，
  把项目自己的完整提示词放进 ``segments[i].prompt``：``commonEnabled=false`` 时
  段提示词就是 ``seg_data.prompt`` 原样（``gen_timeline.py:527``），
  开启时才是 ``concat(global, seg)`` —— 留空保证两种设置下都不会被污染。
* **``output.audioMode``**：r2v 的 ``source`` 会去取「参考音频」，本项目不挂参考音频，
  会丢掉 H3 原生生成音效（项目要靠它做音效垫底）→ 默认显式写 ``generate``。
* **``output.continuityEnabled`` + ``continuityFromPrev``**：段间引导**必须串行**，
  上一段跑完（或在磁盘缓存里）才能引导下一段。``continuityOverlapFrames`` 只允许
  5 / 22 / 39 / 56（会 snap 到最近值）。
"""
import copy
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 视频帧率（H3 按 24 训练；插件默认亦为 24）
FPS_DEFAULT = 24
#: 单段最小帧数（源 ``MIN_GEN_VIDEO_FRAMES = 4``，但官方换算下限是 5）
MIN_SEGMENT_FRAMES = 5
#: 参考图槽位上限（equal 官方 Reference to Video autogrow：ref_image_0..8）
MAX_REFERENCE_IMAGES = 9
#: 参考音频槽位上限（官方 Reference to Video autogrow：ref_audio_0..2，``<Audio N>``）
MAX_REFERENCE_AUDIOS = 3
#: 段间引导可承接的帧数（插件 ``snap_context_frames`` 只认这四档）
CONTINUITY_OVERLAP_CHOICES = (5, 22, 39, 56)
CONTINUITY_OVERLAP_DEFAULT = 22
#: r2v 任务类型字符串（必须与插件 ``lib/task_prompts.py`` 的 combo 选项逐字一致）
TASK_TYPE_R2V = "r2v — 参考主体生视频(Reference to Video)"

#: 前端伪控件 —— 不是 INPUT_TYPES 声明的输入，绝不能进 API prompt
PSEUDO_WIDGET_KEYS = ("control_after_generate", "minimax_director_ui")

#: ``MiniMaxH3Director`` 关键 widget 在 ``widgets_values``（位置化）里的下标。
#: ``to_api()`` 走 ``widgets_values_named``，但落盘 JSON 要与前端一致，
#: 所以两处一起改（下标取自本机实测的 26 项序列化顺序）。
_DIRECTOR_POS: Dict[str, int] = {
    "task_type": 0, "global_prompt": 1, "cfg": 3, "seed": 4,
    "frame_rate": 6, "width": 7, "height": 8, "ref_max_size": 9,
    "total_frames": 10, "timeline_data": 11,
    "steps": 13, "sampler": 14, "scheduler": 15,
    "shift_video": 16, "shift_audio": 17,
}
#: ``MiniMaxH3DirectorRefine`` 同理（18 项序列化顺序）
_REFINE_POS: Dict[str, int] = {
    "mode": 0, "upscale_method": 1, "latent_upscale_model": 2, "sampler": 3,
    "passes": 4, "seed_mode": 5, "seed": 6, "aspect_ratio": 8, "megapixels": 9,
    "width": 10, "height": 11, "skip_fl2v": 12, "confirm_first_pass": 13,
    "enable_latent_chunking": 14, "enable_tiling": 15, "tile_count": 16,
    "tile_overlap": 17,
}

#: SolAttnPatch 的目标参数（一采链上的唯一 SolAttnPatch）。
#: 2026-09-27 用户定档：**完全跟随最新模板**（``minimax_h3_director_二采_加速.json``，
#: 23 节点版，节点 id=53）逐字段对齐，不再强制覆盖任何字段。
#: 新模板实测：``tau=1.2 / int8_qk=False / sink_conditioning=exact_kv /
#: morton=False / morton_curve=3d / verbose=True / dense_blocks=0-5``。
#: ⚠️ 历史包袱（2026-09-27 竖屏实测）：``morton=True/3d`` 在本机 8GB 竖屏下曾
#: 稳定 ``Fatal Python error: Aborted``（栈顶 ``_morton_h3.py``）。现模板已把
#: ``morton`` 改回 ``False``（且 ``morton_curve=3d``），用户选择**完全跟随**，
#: 不再在构建器里强制覆盖——改这里＝改所有出片口径，只在用户明确要求时动。
_SOLATTN_ALIGNED_WV = [1.2, 0.2, 0.9, 4096, False, "exact_kv", False,
                       "3d", True, True, False, "0-5"]
_SOLATTN_ALIGNED_NAMED = {
    "tau": 1.2, "start_percent": 0.2, "end_percent": 0.9, "min_tokens": 4096,
    "int8_qk": False, "sink_conditioning": "exact_kv", "morton": False,
    "morton_curve": "3d", "int8_pv": True, "verbose": True,
    "use_tma": False, "dense_blocks": "0-5",
}
#: 一采链上允许被「显存补丁」跨过的**单入单出 MODEL 直通节点**。
#: 用户会在链尾自行挂加速节点（当前是 ``EasyCache``）；补丁插在 ``SolAttnPatch``
#: 之后、这些节点之前，从而保持它们「包在最外层」的相对位置不变。
_PASSTHROUGH_MODEL_NODES = frozenset({"EasyCache", "ModelPatchTorchSettings"})
#: 8GB 显存必备的两级显存削减（顺序不能变：Attention 先切头，FFN 再切块）
_LOWVRAM_HEAD_CHUNKS = 4
_CHUNKFF_CHUNKS = 2
_CHUNKFF_SEQ_THRESHOLD = 4096

#: 二采加速链里 ``TESpeedMiniMaxH3`` 的参数（与一采链 id=39 实测同口径）。
#: ``(processing_control_value, processing_percent_1, processing_percent_2, mcs, device)``。
_TESPEED_WV = [0.08, 0.1, 0.9, 2, "auto"]
_TESPEED_NAMED = {"processing_control_value": 0.08, "processing_percent_1": 0.1,
                  "processing_percent_2": 0.9, "mcs": 2, "device": "auto"}

#: 二采加速链里 ``EasyCache`` 的参数（与一采链 id=41 实测同口径）。
_EASYCACHE_WV = [0.3, 0.2, 0.9, False]
_EASYCACHE_NAMED = {"reuse_threshold": 0.3, "start_percent": 0.2,
                    "end_percent": 0.9, "verbose": False}

#: 二采链的「锚点」节点类型——它是二采模型链上最后一个 MODEL patch
#: （``...SageAttention → MiniMaxH3MemoryEfficientSageAttentionPatch → Refine/BasicScheduler``），
#: 加速栈就插在它之后、两个 sink（Refine + BasicScheduler）之前。
_REFINE_ANCHOR_TYPES = ("MiniMaxH3MemoryEfficientSageAttentionPatch",)


def align_frames(n: int) -> int:
    """把帧数吸附到 MiniMax 官方 **17k+5** 网格（与插件 ``frame_align.py`` 同式）。"""
    n = max(MIN_SEGMENT_FRAMES, int(n))
    return n + (5 - (n % 17)) % 17


def frames_for_duration(seconds: float, fps: float = FPS_DEFAULT) -> int:
    """秒 → 帧数，复刻插件 ``_duration_to_minimax_frames``（唯一权威换算）。

    必须是纯函数且与插件逐字同式：构建器写进 ``durationSec`` 的是
    ``frames / fps``，插件再用本式重算一次——只有两式一致，
    ``total_frames`` 才等于各段实际帧数之和（否则报告与缓存键都会错位）。
    """
    a = max(0.1, float(seconds or 0.1))
    rate = max(1.0, float(fps or FPS_DEFAULT))
    return align_frames(int(round(a * rate)))


def snap_overlap_frames(n: Any) -> int:
    """段间引导承接帧数取最接近的合法档（5 / 22 / 39 / 56）。"""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return CONTINUITY_OVERLAP_DEFAULT
    return min(CONTINUITY_OVERLAP_CHOICES, key=lambda c: abs(c - v))


#: 提示词里 ``<Picture N>`` 标签的出现形式（大小写不敏感，与插件 reinforce 判据同源）
_PICTURE_TAG_RE = re.compile(r"<\s*picture\s+(\d+)\s*>", re.IGNORECASE)


def picture_capacity(prompt: str) -> Optional[int]:
    """提示词里实际声明到第几张参考图？无标签返回 ``None``。

    ⭐ 为什么必须按提示词来定参考图条数：插件 ``reinforce_r2v_prompt`` 只在**完全
    没有** ``<Picture`` 标签时才补标签；多塞的图会以「有 tag、无声明」的状态进
    conditioning —— 模型看到一张没被告知用途的图，等于给画面加了不可控变量。
    项目约定是「有几张图就在提示词里声明几张」（见 ``h3_prompt_kit``），
    所以段级参考图条数取「声明数的上限」，多出来的直接丢。
    """
    if not prompt:
        return None
    nums = [int(m.group(1)) for m in _PICTURE_TAG_RE.finditer(str(prompt))]
    return max(nums) if nums else None


#: 模板类型判定缓存：{绝对路径: 是否 Director 模板}。
#: 判定要读 88KB JSON，`generate_h3_sequence` 每次调用都会问一次，缓存掉以免重复 IO。
_TEMPLATE_KIND_CACHE: Dict[str, bool] = {}


def is_director_template(path: str) -> bool:
    """模板里有没有 ``MiniMaxH3Director`` 节点？

    ⭐ 判「走哪个构建器」只看**结构**，绝不看文件名——文件名可以改，
    结构不会骗人；旧母版（子图连续拼接）与 Director 模板必须能自动区分，
    否则一旦有人改了 ``WORKFLOW_TEMPLATE`` 里的文件名，就是一句难查的
    「模板中未找到 H3 段实例」。读不出来时按「非 Director」处理并告警
    （宁可走旧路径报明确错误，也不要在这里静默改变行为）。
    """
    key = os.path.abspath(str(path or ""))
    if not key:
        return False
    if key in _TEMPLATE_KIND_CACHE:
        return _TEMPLATE_KIND_CACHE[key]
    kind = False
    try:
        with open(key, "r", encoding="utf-8-sig") as f:
            wf = json.load(f)
        kind = any(n.get("type") == "MiniMaxH3Director"
                   for n in (wf.get("nodes") or []))
    except (OSError, ValueError) as e:  # noqa: BLE001
        logger.warning("读取模板判定类型失败（按非 Director 处理）：%s -> %s", key, e)
    _TEMPLATE_KIND_CACHE[key] = kind
    return kind


class H3DirectorBuilder:
    """以 Director 工作流为模板，注入一条 timeline，返回可提交的 UI 工作流。"""

    def __init__(self, template_path: str):
        self.template_path = template_path
        with open(template_path, "r", encoding="utf-8-sig") as f:
            self.template = json.load(f)
        if not isinstance(self.template.get("nodes"), list) or not self.template["nodes"]:
            raise ValueError(f"不是合法的 ComfyUI UI 工作流（缺 nodes）：{template_path}")
        if self._first("MiniMaxH3Director") is None:
            raise ValueError(
                f"模板里没有 MiniMaxH3Director 节点，无法注入 timeline：{template_path}"
            )

    # ------------------------------------------------------------------ 基础工具
    def _first(self, ntype: str) -> Optional[dict]:
        return next((n for n in self.template.get("nodes") or []
                     if n.get("type") == ntype), None)

    @staticmethod
    def _slot_index(node: dict, name: str, kind: str = "outputs") -> Optional[int]:
        for i, s in enumerate(node.get(kind) or []):
            if s.get("name") == name:
                return i
        return None

    @staticmethod
    def _set_widget(node: dict, pos_map: Dict[str, int], key: str, value: Any) -> None:
        """同时写 ``widgets_values_named``（to_api 用）与 ``widgets_values``（前端用）。"""
        named = dict(node.get("widgets_values_named") or {})
        named[key] = value
        node["widgets_values_named"] = named
        idx = pos_map.get(key)
        wv = list(node.get("widgets_values") or [])
        if idx is not None and idx < len(wv):
            wv[idx] = value
            node["widgets_values"] = wv

    @staticmethod
    def _strip_pseudo_widgets(node: dict) -> List[str]:
        """剥掉前端伪控件（返回被剥掉的键，便于日志）。"""
        removed = []
        named = dict(node.get("widgets_values_named") or {})
        for k in PSEUDO_WIDGET_KEYS:
            if k in named:
                named.pop(k)
                removed.append(k)
        if removed:
            node["widgets_values_named"] = named
        return removed

    # ------------------------------------------------------------------ 帧数
    def _segment_frames(self, seg: dict, fps: float) -> int:
        """单段帧数：显式 ``frames`` > ``duration``（走官方换算）。"""
        if seg.get("frames"):
            try:
                return align_frames(int(seg["frames"]))
            except (TypeError, ValueError):
                pass
        return frames_for_duration(float(seg.get("duration") or 0.0) or 1.0, fps)

    # ------------------------------------------------------------------ timeline
    def _build_timeline(self, seg_list: List[dict], *, refs: Sequence[str],
                        seg_refs: Optional[Sequence[Sequence[str]]],
                        seg_audios: Optional[Sequence[Sequence[str]]],
                        common_enabled: bool,
                        width: int, height: int, fps: float,
                        frames_each: List[int], ref_max_size: int,
                        continuity: bool, overlap: int,
                        audio_mode: str, export_mode: str,
                        live_tae_preview: bool) -> dict:
        tpl_tl = {}
        director = self._first("MiniMaxH3Director")
        named = director.get("widgets_values_named") or {}
        raw_tl = named.get("timeline_data")
        if isinstance(raw_tl, str) and raw_tl.strip():
            try:
                tpl_tl = json.loads(raw_tl)
            except (TypeError, ValueError):
                logger.warning("模板 timeline_data 不是合法 JSON，将按空模板重建")
        # ``output`` / ``video`` 以模板为底（保留 UI 侧其它参数），只覆盖我们要控的字段
        base_output = dict(tpl_tl.get("output") or {})
        base_video = dict(tpl_tl.get("video") or {})

        segments: List[dict] = []
        start = 0
        for i, (seg, frames) in enumerate(zip(seg_list, frames_each)):
            seg_start, start = start, start + frames
            segments.append({
                "id": f"mscxt{i:04d}",
                "start": seg_start,
                "length": frames,
                "frameCount": frames,
                "durationSec": round(frames / float(fps), 6),
                "prompt": seg.get("prompt") or "",
                "negativePrompt": seg.get("negative_prompt") or "",
                # 留空 = 继承 global.taskType（插件语义，已核对源码）
                "taskType": "",
                # ⭐ 逐段参考图：``prompt_batch`` 时插件强制 edit_mode=segment，
                #    refs 只认 ``segment.refs``（``gen_timeline.py:392/529``）。
                #    这就是「每个镜头用自己的分镜图」得以成立的机制。
                "refs": self._ref_items((seg_refs or [])[i]
                                        if seg_refs is not None and i < len(seg_refs)
                                        else ()),
                # ⭐ 段级参考音频（audioMode=source 时驱动 H3 口型/节奏）：
                #    逐镜 QwenTTS 配音，``<Audio N>`` 标签对应。见 _ref_audio_items。
                "refAudios": self._ref_audio_items((seg_audios or [])[i]
                                                   if seg_audios is not None
                                                   and i < len(seg_audios) else ()),
                "refVideos": [],
                "genImage": {"imageFile": "", "fileName": ""},
                "startImage": None,
                "endImage": None,
                # 第 1 段设 true 也无效果（插件对 index<=0 直接返回 False），
                # 统一写 i > 0 以免读者误解
                "continuityFromPrev": bool(i > 0),
                "refImageSize": "match",
            })

        total = start
        output = {
            **base_output,
            "mode": "fixed",
            "width": width,
            "height": height,
            "maxExportFrames": 0,
            "exportMode": export_mode,
            "audioMode": audio_mode,
            "exportSourceImages": False,
            "exportPreFaceRefine": False,
            "refImageSize": "match",
            "continuityEnabled": bool(continuity and len(segments) > 1),
            "continuityOverlapFrames": snap_overlap_frames(overlap),
            "continuityMode": base_output.get("continuityMode") or "guide",
            "continuityRedraw": base_output.get("continuityRedraw", 0.1),
            "continuityKeepTail": base_output.get("continuityKeepTail", True),
        }
        # 段间引导：第一段没有「上一段」，只有 2 段以上才有意义
        if not output["continuityEnabled"]:
            for s in segments:
                s["continuityFromPrev"] = False

        ref_items = self._ref_items(refs)

        tl = {
            **tpl_tl,
            "version": 5,
            # 显式 segment（prompt_batch 下插件自己也会强制成 segment，这里写出来便于人读）
            "editMode": "segment",
            # ⚠️ 必须让 task_key 落进 gen 视频批路径，否则走「源视频时间轴」分支
            "timelineMode": "prompt_batch",
            "totalFrames": total,
            "frameRate": fps,
            "video": base_video,
            "videoClips": [],
            "global": {
                **(tpl_tl.get("global") or {}),
                "taskType": TASK_TYPE_R2V,
                # 留空：commonEnabled 时插件会把全局提示词拼在段提示词**前面**，
                # 置空才能让 seg_prompt 精确等于项目自己构建的提示词。
                "prompt": "",
                # ⭐ 全局 refs 只在 commonEnabled 时才会 merge 进每段（``gen_timeline.py:530``）。
                #    项目逐镜分镜图各不相同 → 默认**不用**全局 refs，
                #    common_enabled=False：段提示词也不会被全局提示词污染。
                "refs": ref_items,
                "referenceVideo": {"videoFile": "", "fileName": "",
                                   "type": "input", "subfolder": ""},
                "continuousReference": False,
                "genImage": {"imageFile": ""},
                "sourceWidth": width,
                "sourceHeight": height,
                "refAudios": [],
                "commonEnabled": bool(common_enabled),
                "commonCollapsed": True,
                "refVideos": [],
            },
            "output": output,
            "runSelectEnabled": False,
            "runSelection": [],
            "segments": segments,
            "width": width,
            "height": height,
            "refMaxSize": ref_max_size,
            "gen": {**(tpl_tl.get("gen") or {}), "defaultFrameCount": frames_each[0]
                    if frames_each else 124},
            # ⚠️ fl2v 用的两套字段清空：留着旧数据容易让人误判（gen 路径不读它们）
            "shots": [],
            "keyframes": [],
            "durationSec": round(total / float(fps), 6),
            "liveTaePreview": bool(live_tae_preview),
            "batchDetailMode": tpl_tl.get("batchDetailMode") or "solo",
            # 前端工作区镜像，Python 侧零引用；清空避免带着模板的旧素材组让人误解
            "batchWorkspaces": {},
        }
        return tl

    @staticmethod
    def _ref_items(names: Sequence[str]) -> List[dict]:
        """参考图相对文件名 → 插件 ``refs`` 条目（index 从 0 起 = ``<Picture N+1>``）。"""
        return [
            {"index": i, "imageFile": str(name), "fileName": "",
             "type": "input", "subfolder": ""}
            for i, name in enumerate(list(names or [])[:MAX_REFERENCE_IMAGES])
            if str(name or "").strip()
        ]

    @staticmethod
    def _ref_audio_items(names: Sequence[str]) -> List[dict]:
        """参考音频相对文件名 → 插件 ``refAudios`` 条目。

        与 ``_ref_items`` 对称，字段名是 ``audioFile``（不是 ``imageFile``），
        见插件 ``director/pack.py:724`` / ``director/plan.py:_reference_audio_file``。
        ``index`` 从 0 起 = ``<Audio N+1>``（最多 ``MAX_REFERENCE_AUDIOS=3``）。
        """
        return [
            {"index": i, "audioFile": str(name), "fileName": "",
             "type": "input", "subfolder": ""}
            for i, name in enumerate(list(names or [])[:MAX_REFERENCE_AUDIOS])
            if str(name or "").strip()
        ]

    # ------------------------------------------------------------------ 图改造
    def _drop_pre_refine_branch(self, nodes: List[dict], links: List[list]) -> List[int]:
        """裁掉「一采（放大/精修前）」那路 CreateVideo → SaveVideo。

        为什么：``MiniMaxH3Director`` 同时输出 ``images``（二采后）与
        ``images_pre_refine``（一采），模板把两路都接了 SaveVideo → 一次调用落
        **两个** mp4。项目契约是「一个分镜一个 ``shot_XX.mp4``」，调用方只取
        ``files[0]``，多出来的那路会让取回的成片随机（排序不保证）。
        只保留二采成片，契约才稳。想看一采时把 ``keep_pre_refine=True`` 打开。
        """
        director = self._first("MiniMaxH3Director")
        pre_slot = self._slot_index(director, "images_pre_refine", "outputs")
        if pre_slot is None:
            return []
        dropped: List[int] = []
        for n in nodes:
            if n.get("type") != "CreateVideo":
                continue
            src = self._input_source(n, links, "images")
            if src and src[0] == director.get("id") and src[1] == pre_slot:
                dropped.append(n["id"])
        # 连带删掉这些 CreateVideo 的下游（SaveVideo）
        for nid in list(dropped):
            for l in links:
                if isinstance(l, (list, tuple)) and len(l) >= 5 and l[1] == nid:
                    for n in nodes:
                        if n.get("id") == l[3]:
                            dropped.append(n["id"])
        return sorted(set(dropped))

    @staticmethod
    def _input_source(node: dict, links: List[list], input_name: str):
        """返回某输入连线的 (origin_id, origin_slot)，没有则 None。"""
        idx = H3DirectorBuilder._slot_index(node, input_name, "inputs")
        if idx is None:
            return None
        lid = (node.get("inputs") or [])[idx].get("link")
        if lid is None:
            return None
        for l in links:
            if isinstance(l, (list, tuple)) and len(l) >= 5 and l[0] == lid:
                return (l[1], l[2])
            if isinstance(l, dict) and l.get("id") == lid:
                return (l.get("origin_id"), l.get("origin_slot", 0))
        return None

    @staticmethod
    def _remove_nodes(wf: dict, node_ids: Sequence[int]) -> None:
        """删节点并清掉所有相关连线，保持 links / outputs[].links / inputs[].link 三方一致。"""
        if not node_ids:
            return
        dead = set(node_ids)
        nodes = wf["nodes"]
        wf["nodes"] = [n for n in nodes if n.get("id") not in dead]
        kept = []
        for l in wf.get("links") or []:
            if isinstance(l, (list, tuple)) and len(l) >= 5:
                if l[1] in dead or l[3] in dead:
                    continue
            elif isinstance(l, dict):
                if l.get("origin_id") in dead or l.get("target_id") in dead:
                    continue
            kept.append(l)
        wf["links"] = kept
        # 重算 bookkeeping：只保留仍存在的 link id
        alive = {l[0] for l in kept if isinstance(l, (list, tuple)) and l}
        for n in wf["nodes"]:
            for s in n.get("outputs") or []:
                if s.get("links"):
                    s["links"] = [lid for lid in s["links"] if lid in alive]
            for s in n.get("inputs") or []:
                if s.get("link") is not None and s["link"] not in alive:
                    s["link"] = None

    # ⚠️ 已退役（2026-09-27 用户定档「纯跟随模板」）：以下两个加速注入函数
    # （_insert_lowvram_patches / _insert_refine_accel_chain）不再被 build() 调用。
    # 最新模板自带**一条统一加速链**（UNETLoader → Lora → PathchSage → TESpeed →
    # SolAttnPatch → EasyCache → {Director, BasicScheduler, Refine}），一采/二采共用，
    # 且模板**不含** LowVRAM/ChunkFF/MemoryEfficientSage。保留定义仅作历史参考与
    # 将来可能的回退，勿在新代码里调用它们。
    def _insert_lowvram_patches(self, wf: dict) -> Dict[str, int]:
        """把 ``MiniMaxLowVRAMAttention`` + ``MiniMaxChunkFeedForward`` 插进一采链。

        对齐项目最新口径（``H3信号10段测试001.json`` 的加速链）：
        ``SolAttnPatch → LowVRAM(4) → ChunkFeedForward(2,4096) → SigmaShift(12,3)``。
        Director 工作流里 SigmaShift 由 ``shift_video=12 / shift_audio=3`` 在节点内部承担，
        所以这里只补前两级——本机 8GB 显存下它们是能不能跑起来的分水岭。

        ⭐ **插入点＝``SolAttnPatch`` 的 MODEL 输出与它的唯一下游之间**，而不是
        「Director.model 的输入」。区别在于用户会在链尾自己挂加速节点：当前模板是
        ``SolAttnPatch → EasyCache → Director.model``，若按旧判据（要求 Director.model
        的源就是 SolAttnPatch）会**静默跳过补丁**，8GB 下直接 OOM。
        ``_PASSTHROUGH_MODEL_NODES`` 里的单入单出节点允许被跨过，且保持
        「包在最外层」的相对位置不变。
        """
        nodes = wf["nodes"]
        links = wf["links"]
        director = next((n for n in nodes if n.get("type") == "MiniMaxH3Director"), None)
        sol = next((n for n in nodes if n.get("type") == "SolAttnPatch"), None)
        if director is None or sol is None:
            return {}
        if any(n.get("type") == "MiniMaxLowVRAMAttention" for n in nodes):
            return {}   # 模板已自带（幂等）

        # ---- 解析插入点：SolAttnPatch.MODEL 的唯一出边 ----
        sol_out = self._slot_index(sol, "MODEL", "outputs")
        if sol_out is None:
            logger.warning("SolAttnPatch 没有 MODEL 输出端口，跳过显存补丁插入")
            return {}
        out_links = list(((sol.get("outputs") or [])[sol_out].get("links") or []))
        if len(out_links) != 1:
            logger.warning("SolAttnPatch.MODEL 出边数=%d（期望 1），跳过显存补丁插入",
                           len(out_links))
            return {}
        old_lid = out_links[0]
        row = next((l for l in links
                    if isinstance(l, (list, tuple)) and l and l[0] == old_lid), None)
        if row is None:
            logger.warning("SolAttnPatch.MODEL 的连线 #%s 不在 links 表中，跳过显存补丁插入",
                           old_lid)
            return {}
        sink_id, sink_slot = row[3], row[4]

        # ---- 校验这是一采链：沿 MODEL 单出边应能走到 Director ----
        cur_id, hops = sink_id, 0
        while cur_id != director["id"] and hops < 8:
            nxt = next((n for n in nodes if n.get("id") == cur_id), None)
            if nxt is None or nxt.get("type") not in _PASSTHROUGH_MODEL_NODES:
                logger.warning("SolAttnPatch → Director.model 之间遇到非直通节点 %s，"
                               "跳过显存补丁插入", (nxt or {}).get("type"))
                return {}
            outs = [lid for o in (nxt.get("outputs") or []) for lid in (o.get("links") or [])]
            if len(outs) != 1:
                logger.warning("直通节点 %s 的 MODEL 出边数=%d（期望 1），跳过显存补丁插入",
                               nxt.get("type"), len(outs))
                return {}
            nxt_row = next((l for l in links
                            if isinstance(l, (list, tuple)) and l and l[0] == outs[0]), None)
            if nxt_row is None:
                logger.warning("直通节点 %s 的出边 #%s 不在 links 表中，跳过显存补丁插入",
                               nxt.get("type"), outs[0])
                return {}
            cur_id = nxt_row[3]
            hops += 1
        if cur_id != director["id"]:
            logger.warning("一采链无法从 SolAttnPatch 走到 Director.model，跳过显存补丁插入")
            return {}

        max_id = max((n.get("id") for n in nodes if isinstance(n.get("id"), int)), default=0)
        max_lid = max((l[0] for l in links if isinstance(l, (list, tuple)) and l), default=0)
        low_id, ff_id = max_id + 1, max_id + 2

        def _mk(model_node, node_id, ntype, widgets, named, x):
            return {
                "id": node_id, "type": ntype, "pos": [x, sol.get("pos", [0, 0])[1]],
                "size": [250, 80], "flags": {}, "order": 0, "mode": 0,
                "inputs": [{"label": "model", "localized_name": "model", "name": "model",
                            "type": "MODEL", "link": None}],
                "outputs": [{"label": "MODEL", "localized_name": "模型", "name": "MODEL",
                             "type": "MODEL", "links": []}],
                "properties": {"Node name for S&R": ntype},
                "widgets_values": list(widgets),
                "widgets_values_named": dict(named),
            }

        low = _mk(sol, low_id, "MiniMaxLowVRAMAttention", [_LOWVRAM_HEAD_CHUNKS],
                  {"head_chunks": _LOWVRAM_HEAD_CHUNKS}, sol.get("pos", [0, 0])[0] + 300)
        low["inputs"].append({"label": "head_chunks", "localized_name": "head_chunks",
                              "name": "head_chunks", "type": "INT",
                              "widget": {"name": "head_chunks"}, "link": None})
        ff = _mk(low, ff_id, "MiniMaxChunkFeedForward",
                 [_CHUNKFF_CHUNKS, _CHUNKFF_SEQ_THRESHOLD],
                 {"chunks": _CHUNKFF_CHUNKS, "seq_threshold": _CHUNKFF_SEQ_THRESHOLD},
                 sol.get("pos", [0, 0])[0] + 300)
        ff["inputs"] += [
            {"label": "chunks", "localized_name": "chunks", "name": "chunks", "type": "INT",
             "widget": {"name": "chunks"}, "link": None},
            {"label": "seq_threshold", "localized_name": "seq_threshold",
             "name": "seq_threshold", "type": "INT",
             "widget": {"name": "seq_threshold"}, "link": None},
        ]
        nodes.extend([low, ff])

        # 断开 sol → sink，改接成 sol → low → ff → sink
        # （sink 通常是 Director 本身；模板在链尾挂了 EasyCache 时 sink 就是 EasyCache，
        #   补丁落在 SolAttnPatch 之后 → EasyCache 仍是最外层 patch，相对位置不变）
        sink = next(n for n in nodes if n["id"] == sink_id)
        links[:] = [l for l in links
                    if not (isinstance(l, (list, tuple)) and l and l[0] == old_lid)]
        if sol.get("outputs") and sol["outputs"][sol_out].get("links"):
            sol["outputs"][sol_out]["links"] = [x for x in sol["outputs"][sol_out]["links"]
                                                if x != old_lid]
        sink["inputs"][sink_slot]["link"] = None

        for i, (src_id, src_slot, dst_id, dst_slot) in enumerate((
                (sol["id"], sol_out, low_id, 0),
                (low_id, 0, ff_id, 0),
                (ff_id, 0, sink_id, sink_slot))):
            lid = max_lid + 1 + i
            links.append([lid, src_id, src_slot, dst_id, dst_slot, "MODEL"])
            o = next(n for n in nodes if n["id"] == src_id)
            o["outputs"][src_slot].setdefault("links", []).append(lid)
            next(n for n in nodes if n["id"] == dst_id)["inputs"][dst_slot]["link"] = lid

        wf["last_link_id"] = max_lid + 3
        wf["last_node_id"] = ff_id
        return {"lowvram": low_id, "chunk_ff": ff_id}

    def _insert_refine_accel_chain(self, wf: dict) -> Dict[str, int]:
        """【已退役】给**二采（Refine）链**补上与一采相同的加速栈。

        为什么需要（2026-09-27）：二采链当前只有
        ``UNETLoader → LoraLoader → SageAttention → MemoryEfficientSagePatch → Refine``，
        缺一采链那套 ``TESpeedMiniMaxH3 → SolAttnPatch → EasyCache →
        LowVRAM → ChunkFF``。二采在 8GB 卡上放大到 1088×1920 每步约 30 分钟，
        补齐稀疏注意力（SolAttn）与序列削减（LowVRAM/ChunkFF）能显著降显存与耗时。

        插入点：二采链锚点 ``MiniMaxH3MemoryEfficientSageAttentionPatch`` 之后、
        它的两个下游（``MiniMaxH3DirectorRefine.refine_model`` 与
        ``BasicScheduler.model``）之前。锚点的 MODEL 出边通常是**两条**（分叉到
        Refine 与 BasicScheduler），所以补丁链末端要**一进二出**。

        实现策略：**深拷贝一采链的 TESpeed/SolAttn/EasyCache 节点结构当原型**，
        改 id / 清 link 后复用到二采链——这样 widget 声明（含 SolAttnPatch 那一串
        FLOAT/BOOLEAN/COMBO input）与一采链逐字一致，不会漏字段；LowVRAM/ChunkFF
        用与一采相同的方式新建。SolAttnPatch 用 `_SOLATTN_ALIGNED_*`（当前
        morton=False/2d_frame，竖屏安全口径）。
        """
        nodes = wf["nodes"]
        links = wf["links"]
        by_id = {n["id"]: n for n in nodes}

        refine = next((n for n in nodes if n.get("type") == "MiniMaxH3DirectorRefine"), None)
        anchor = next((n for n in nodes
                       if n.get("type") in _REFINE_ANCHOR_TYPES), None)
        if refine is None or anchor is None:
            logger.info("二采加速链插入跳过：缺 Refine(%s) 或锚点(%s)",
                        "有" if refine else "无", "有" if anchor else "无")
            return {}
        # 精确幂等：统计 SolAttnPatch 数量——一采链恒有 1 个；出现 2 个 = 二采已插过
        sol_count = sum(1 for n in nodes if n.get("type") == "SolAttnPatch")
        if sol_count >= 2:
            return {}   # 已插过（幂等）

        # ---- 找锚点 MODEL 出边（一进多出）----
        a_out = self._slot_index(anchor, "MODEL", "outputs")
        if a_out is None:
            logger.warning("二采锚点 %s 无 MODEL 输出，跳过加速链插入", anchor.get("type"))
            return {}
        out_lids = list((anchor.get("outputs") or [])[a_out].get("links") or [])
        if not out_lids:
            logger.warning("二采锚点 MODEL 无出边，跳过加速链插入")
            return {}
        # 解析每条出边 → (sink_node_id, sink_input_slot)
        sinks = []   # [(sink_node_id, sink_slot_index, link_id)]
        for lid in out_lids:
            row = next((l for l in links
                        if isinstance(l, (list, tuple)) and l and l[0] == lid), None)
            if row is None:
                logger.warning("二采锚点出边 #%s 不在 links 表，跳过该路", lid)
                continue
            sinks.append((row[3], row[4], lid))
        if not sinks:
            return {}

        # ---- 原型：深拷贝**原始模板**里的 TESpeed/SolAttn/EasyCache ----
        # ⭐ 从 ``self.template``（构造时加载的原始模板）取，而不是 ``nodes``（构建中的
        #    工作流）——反证夹具会摘掉 EasyCache，此时一采链里没有 EasyCache 原型，
        #    但原始模板里这三个节点恒在，二采链加速仍能独立插入（两条链解耦）。
        tpl_nodes = self.template.get("nodes") or []
        proto = {n.get("type"): n for n in tpl_nodes
                 if n.get("type") in ("TESpeedMiniMaxH3", "SolAttnPatch", "EasyCache")}
        if not all(t in proto for t in ("TESpeedMiniMaxH3", "SolAttnPatch", "EasyCache")):
            logger.warning("模板缺 TESpeed/SolAttn/EasyCache 原型节点，跳过二采加速插入")
            return {}

        max_id = max((n.get("id") for n in nodes if isinstance(n.get("id"), int)), default=0)
        max_lid = max((l[0] for l in links if isinstance(l, (list, tuple)) and l), default=0)

        def _clone(ntype, new_id, x_offset):
            src = proto[ntype]
            n = copy.deepcopy(src)
            n["id"] = new_id
            n["pos"] = [anchor.get("pos", [0, 0])[0] + x_offset,
                        anchor.get("pos", [0, 0])[1]]
            for s in (n.get("inputs") or []):
                s["link"] = None
            for s in (n.get("outputs") or []):
                s["links"] = []
            # SolAttnPatch 强制对齐当前安全口径（morton=False/2d_frame）
            if ntype == "SolAttnPatch":
                n["widgets_values"] = list(_SOLATTN_ALIGNED_WV)
                n["widgets_values_named"] = dict(_SOLATTN_ALIGNED_NAMED)
            if ntype == "TESpeedMiniMaxH3":
                n["widgets_values"] = list(_TESPEED_WV)
                n["widgets_values_named"] = dict(_TESPEED_NAMED)
            if ntype == "EasyCache":
                n["widgets_values"] = list(_EASYCACHE_WV)
                n["widgets_values_named"] = dict(_EASYCACHE_NAMED)
            return n

        te_id = max_id + 1
        sol_id = max_id + 2
        ec_id = max_id + 3
        low_id = max_id + 4
        ff_id = max_id + 5

        te = _clone("TESpeedMiniMaxH3", te_id, 260)
        sol = _clone("SolAttnPatch", sol_id, 520)
        ec = _clone("EasyCache", ec_id, 780)

        def _mk_patch(pid, ntype, widgets, named, x):
            return {
                "id": pid, "type": ntype, "pos": [anchor.get("pos", [0, 0])[0] + x,
                                                  anchor.get("pos", [0, 0])[1]],
                "size": [250, 80], "flags": {}, "order": 0, "mode": 0,
                "inputs": [{"label": "model", "localized_name": "model", "name": "model",
                            "type": "MODEL", "link": None}],
                "outputs": [{"label": "MODEL", "localized_name": "模型", "name": "MODEL",
                             "type": "MODEL", "links": []}],
                "properties": {"Node name for S&R": ntype},
                "widgets_values": list(widgets),
                "widgets_values_named": dict(named),
            }

        low = _mk_patch(low_id, "MiniMaxLowVRAMAttention", [_LOWVRAM_HEAD_CHUNKS],
                        {"head_chunks": _LOWVRAM_HEAD_CHUNKS}, 1040)
        low["inputs"].append({"label": "head_chunks", "localized_name": "head_chunks",
                              "name": "head_chunks", "type": "INT",
                              "widget": {"name": "head_chunks"}, "link": None})
        ff = _mk_patch(ff_id, "MiniMaxChunkFeedForward",
                       [_CHUNKFF_CHUNKS, _CHUNKFF_SEQ_THRESHOLD],
                       {"chunks": _CHUNKFF_CHUNKS, "seq_threshold": _CHUNKFF_SEQ_THRESHOLD},
                       1300)
        ff["inputs"] += [
            {"label": "chunks", "localized_name": "chunks", "name": "chunks", "type": "INT",
             "widget": {"name": "chunks"}, "link": None},
            {"label": "seq_threshold", "localized_name": "seq_threshold",
             "name": "seq_threshold", "type": "INT",
             "widget": {"name": "seq_threshold"}, "link": None},
        ]

        nodes.extend([te, sol, ec, low, ff])

        # ---- 断开锚点旧出边 ----
        for (_, _, lid) in sinks:
            links[:] = [l for l in links
                        if not (isinstance(l, (list, tuple)) and l and l[0] == lid)]
        (anchor.get("outputs") or [])[a_out]["links"] = []
        for (sid, sslot, _) in sinks:
            sn = by_id.get(sid)
            if sn is not None and sslot < len(sn.get("inputs") or []):
                sn["inputs"][sslot]["link"] = None

        # ---- 重接：anchor → te → sol → low → ff → ec → {sinks} ----
        # ⭐ 顺序与一采链严格同构：一采是 `SolAttn → LowVRAM → ChunkFF → EasyCache →
        #    Director`（`_insert_lowvram_patches` 把 LowVRAM/ChunkFF 插在 SolAttn 之后、
        #    EasyCache 之前，保持 EasyCache 包在最外层）。二采照抄。
        chain_in = [te_id, sol_id, low_id, ff_id, ec_id]
        # 首条：anchor → te
        def _add_link(src_id, src_slot, dst_id, dst_slot):
            nonlocal max_lid
            max_lid += 1
            links.append([max_lid, src_id, src_slot, dst_id, dst_slot, "MODEL"])
            src = next(n for n in nodes if n["id"] == src_id)
            src["outputs"][src_slot].setdefault("links", []).append(max_lid)
            dst = next(n for n in nodes if n["id"] == dst_id)
            dst["inputs"][dst_slot]["link"] = max_lid

        # anchor.MODEL → te.model
        _add_link(anchor["id"], a_out, te_id, 0)
        # te → sol → low → ff → ec
        for i in range(len(chain_in) - 1):
            _add_link(chain_in[i], 0, chain_in[i + 1], 0)
        # ec → 每个 sink
        for (sid, sslot, _) in sinks:
            _add_link(ec_id, 0, sid, sslot)

        wf["last_link_id"] = max_lid
        wf["last_node_id"] = ff_id
        return {"refine_tespeed": te_id, "refine_solattn": sol_id,
                "refine_easycache": ec_id, "refine_lowvram": low_id,
                "refine_chunk_ff": ff_id}

    # ------------------------------------------------------------------ 主入口
    def build(self, segments: Sequence[dict], *, refs: Sequence[str] = (),
              seg_refs: Optional[Sequence[Sequence[str]]] = None,
              seg_audios: Optional[Sequence[Sequence[str]]] = None,
              common_enabled: Optional[bool] = None,
              width: Optional[int] = None, height: Optional[int] = None,
              frame_rate: float = FPS_DEFAULT, seed: Optional[int] = None,
              filename_prefix: str = "", keep_pre_refine: bool = False,
              continuity: bool = True,
              continuity_overlap: Any = CONTINUITY_OVERLAP_DEFAULT,
              ref_max_size: Optional[int] = None,
              audio_mode: str = "generate",
              export_mode: str = "all",
              live_tae_preview: bool = False,
              align_accel_chain: bool = True) -> Tuple[dict, dict]:
        """注入一条 timeline，返回 ``(ui_workflow, layout)``。

        segments: ``[{"prompt": str, "duration": 秒, "name": str,
                      "reference_images": [本地路径(仅用于校验), ...]}, ...]``
                  **顺序 = 时间轴顺序**；相邻段由插件「段间引导」衔接。
        seg_refs: ⭐ **逐段参考图**（ComfyUI ``input/`` 下的相对文件名，调用方先 upload）：
                  ``seg_refs[i]`` 服务 ``segments[i]``，第 j 个 → ``ref_image_{j}``
                  → 该段提示词里的 ``<Picture {j+1}>``。长度可与 segments 不等
                  （短的按空处理）。这是**主用法**：整集模式下每个镜头有自己的分镜图。
        seg_audios: ⭐ **逐段参考音频**（同 seg_refs 格式，``audioFile`` 相对名）：
                  ``seg_audios[i]`` 服务 ``segments[i]``，第 j 个 → ``ref_audio_{j}``
                  → 该段提示词里的 ``<Audio {j+1}>``（最多 3 个）。仅当
                  ``audio_mode="source"`` 时插件才会用参考音频驱动口型/节奏；
                  逐镜 QwenTTS 配音走这里。长度可与 segments 不等（短的按空处理）。
        refs:     可选**全局**参考图（同格式）。仅当 ``common_enabled=True`` 时才
                  会 merge 进每段（同 index 段级优先）。项目逐镜参考图不同，
                  默认留空即可。
        common_enabled: 是否把 ``global.prompt`` 前缀拼接到段提示词。
                  默认 ``bool(refs)``——没传全局 refs 就不拼，段提示词 = 原样。
        seed:     采样种子；None = 沿用模板值。
        """
        seg_list = [dict(s or {}) for s in (segments or [])]
        if not seg_list:
            raise ValueError("H3DirectorBuilder.build: segments 不能为空")
        fps = float(frame_rate or FPS_DEFAULT) or FPS_DEFAULT
        if fps <= 0:
            raise ValueError(f"H3DirectorBuilder.build: frame_rate 非法 {frame_rate}")

        # ⭐ 段级参考图条数上限 = 该段提示词实际声明的 ``<Picture N>`` 个数。
        #    多塞的图会以「有 tag、无声明」的状态进 conditioning（见 picture_capacity）。
        #    段自带的 ``reference_images`` 只用来提示「提示词声明数 < 传图数」这种不一致。
        seg_ref_lists: List[List[str]] = []
        for i, seg in enumerate(seg_list):
            raw = list((seg_refs[i] if seg_refs is not None and i < len(seg_refs) else []) or [])
            names = [str(x).strip() for x in raw if str(x or "").strip()]
            cap = picture_capacity(seg.get("prompt") or "")
            declared = len(seg.get("reference_images") or [])
            if cap is not None and len(names) > cap:
                logger.warning(
                    "段%d(%s)：参考图 %d 张 > 提示词声明的 <Picture> 数 %d，多余 %d 张已丢弃",
                    i + 1, seg.get("name") or f"seg{i + 1}", len(names), cap,
                    len(names) - cap)
                names = names[:cap]
            if declared and len(names) < declared:
                logger.warning(
                    "段%d(%s)：可用参考图 %d 张 < 传入 %d 张（部分文件缺失或未上传成功）",
                    i + 1, seg.get("name") or f"seg{i + 1}", len(names), declared)
            seg_ref_lists.append(names)

        wf = copy.deepcopy(self.template)
        nodes = wf["nodes"]
        links = wf["links"]
        director = next(n for n in nodes if n.get("type") == "MiniMaxH3Director")
        refine = next((n for n in nodes if n.get("type") == "MiniMaxH3DirectorRefine"), None)

        d_named = director.get("widgets_values_named") or {}
        w = int(width or d_named.get("width") or 544)
        h = int(height or d_named.get("height") or 960)
        rmax = int(ref_max_size or max(w, h))

        frames_each = [self._segment_frames(s, fps) for s in seg_list]
        total = sum(frames_each)

        if common_enabled is None:
            common_enabled = bool([x for x in (refs or []) if str(x or "").strip()])

        # 段级参考音频：normalize 成与 segments 等长（缺的按空），供 _build_timeline 注入 refAudios
        seg_audio_lists: List[List[str]] = [
            [str(x).strip() for x in ((seg_audios[i] if seg_audios is not None
                                       and i < len(seg_audios) else []) or [])
             if str(x or "").strip()]
            for i in range(len(seg_list))
        ]

        timeline = self._build_timeline(
            seg_list, refs=refs, seg_refs=seg_ref_lists, seg_audios=seg_audio_lists,
            common_enabled=bool(common_enabled),
            width=w, height=h, fps=fps, frames_each=frames_each,
            ref_max_size=rmax, continuity=continuity, overlap=continuity_overlap,
            audio_mode=audio_mode, export_mode=export_mode,
            live_tae_preview=live_tae_preview)

        # ---- Director 节点注入 ----
        self._set_widget(director, _DIRECTOR_POS, "task_type", TASK_TYPE_R2V)
        # 与 timeline.global.prompt 一致置空（插件优先读 timeline，两处都空才不会被拼进段提示词）
        self._set_widget(director, _DIRECTOR_POS, "global_prompt", "")
        self._set_widget(director, _DIRECTOR_POS, "width", w)
        self._set_widget(director, _DIRECTOR_POS, "height", h)
        self._set_widget(director, _DIRECTOR_POS, "ref_max_size", rmax)
        self._set_widget(director, _DIRECTOR_POS, "frame_rate", fps)
        self._set_widget(director, _DIRECTOR_POS, "total_frames", total)
        self._set_widget(director, _DIRECTOR_POS, "timeline_data",
                         json.dumps(timeline, ensure_ascii=False))
        stripped = self._strip_pseudo_widgets(director)
        if seed is not None:
            self._set_widget(director, _DIRECTOR_POS, "seed", int(seed))
            named = dict(director.get("widgets_values_named") or {})
            # seed 变可复现：伪控件已剥掉，这里补回「固定」语义靠的是 seed 本身
            named["control_after_generate"] = "fixed"
            director["widgets_values_named"] = {
                k: v for k, v in named.items() if k not in PSEUDO_WIDGET_KEYS}

        if refine is not None:
            self._strip_pseudo_widgets(refine)

        # ---- 「一采」那路 SaveVideo ----
        save_video_pre = None
        if not keep_pre_refine:
            drop = self._drop_pre_refine_branch(nodes, links)
            if drop:
                self._remove_nodes(wf, drop)
                logger.info("Director：已裁掉一采（放大前）那路 CreateVideo/SaveVideo：%s", drop)
            nodes = wf["nodes"]
            # 裁掉后就不该再报这个节点（否则调用方会去 index 一个不存在的 id）
            save_video_pre = None
        else:
            pre = [n for n in nodes if n.get("type") == "SaveVideo"]
            save_video_pre = pre[-1]["id"] if len(pre) > 1 else None

        # ---- 加速链对齐（2026-09-27 用户定档：纯跟随模板，停用代码注入） ----
        # 最新模板（minimax_h3_director_二采_加速.json，23 节点版）自带**两条独立加速链**：
        #   一采链：UNETLoader → LoraLoader → PathchSage → TESpeed → SolAttnPatch → EasyCache → Director
        #   二采链：UNETLoader → LoraLoader → PathchSage → TESpeed → EasyCache → { BasicScheduler, Refine }
        # （二采链**无** SolAttnPatch；两链各自独立，EasyCache 各一个），输出链：
        #   Director.images → CreateVideo → NvidiaDLSSFrameInterpolation → DLSSNR_Video → SaveVideo
        # （新增 DLSS 帧插值 + 降噪 + MiniMaxH3TRTVAELoader 替代 VAELoader）。
        # 模板**不再含** MiniMaxLowVRAMAttention / MiniMaxChunkFeedForward /
        # MemoryEfficientSagePatch，也不再有两路 SaveVideo。
        # 因此不再程序化注入任何加速节点（_insert_lowvram_patches /
        # _insert_refine_accel_chain 均已退役）——只同步 SolAttnPatch 参数
        # （完全跟随模板，见 _SOLATTN_ALIGNED_*）。
        accel_added: Dict[str, int] = {}
        if align_accel_chain:
            sol = next((n for n in nodes if n.get("type") == "SolAttnPatch"), None)
            if sol is not None:
                sol["widgets_values"] = list(_SOLATTN_ALIGNED_WV)
                sol["widgets_values_named"] = dict(_SOLATTN_ALIGNED_NAMED)

        # ---- 成片文件名 ----
        save_videos = [n for n in wf["nodes"] if n.get("type") == "SaveVideo"]
        save_video = None
        for n in save_videos:
            nid = n["id"]
            prefix = filename_prefix
            if save_video_pre is not None and nid == save_video_pre:
                prefix = (filename_prefix or "video/MiniMaxH3_Director") + "_pre"
            if prefix:
                self._set_widget(n, {"filename_prefix": 0, "codec": 4}, "filename_prefix", prefix)
            if save_video is None or nid != save_video_pre:
                save_video = nid

        layout = {
            "template": self.template_path,
            "director_node": director["id"],
            "refine_node": refine["id"] if refine is not None else None,
            "save_video_node": save_video,
            "save_video_node_pre": save_video_pre,
            "accel_added": accel_added,
            "stripped_pseudo_widgets": stripped,
            "width": w, "height": h, "ref_max_size": rmax,
            "frame_rate": fps,
            "total_frames": total,
            "duration_sec": round(total / fps, 6),
            "refs": [r.get("imageFile") for r in timeline["global"]["refs"]],
            "segment_refs": [self._ref_items(names) for names in seg_ref_lists],
            "segment_audios": [self._ref_audio_items(names) for names in seg_audio_lists],
            "common_enabled": bool(common_enabled),
            "continuity": timeline["output"]["continuityEnabled"],
            "continuity_overlap_frames": timeline["output"]["continuityOverlapFrames"],
            "export_mode": export_mode,
            "audio_mode": audio_mode,
            "segments": [
                {"index": i, "name": seg_list[i].get("name") or f"seg{i + 1:02d}",
                 "start": timeline["segments"][i]["start"],
                 "frames": frames_each[i],
                 "duration_sec": timeline["segments"][i]["durationSec"],
                 "prompt_len": len(seg_list[i].get("prompt") or ""),
                 "ref_count": len(seg_ref_lists[i]),
                 "audio_count": len(seg_audio_lists[i]),
                 "refs": [r["imageFile"] for r in timeline["segments"][i]["refs"]],
                 "audios": [a["audioFile"] for a in timeline["segments"][i]["refAudios"]]}
                for i in range(len(seg_list))
            ],
            "node_total": len(wf["nodes"]),
            "link_total": len(wf["links"]),
        }
        # ⚠️ 参考图是**逐段**挂在 ``segment.refs``（见本文件顶部协议）；
        # ``layout["refs"]`` 统计的是 ``global.refs``（默认空，只有 commonEnabled 时才 merge），
        # 拿它打日志会把「生产其实带了 N 张参考图」误报成「参考图 0 张」，误导排查。
        n_seg_refs = sum(len(names) for names in seg_ref_lists)
        logger.info(
            "H3 Director 工作流已就绪：%d 段 / %d 帧（%.2fs）/ 参考图 %d 张（段级）"
            "+ %d 张（公共）/ 段间引导 %s(%s) / 节点 %d 连线 %d",
            len(seg_list), total, total / fps, n_seg_refs, len(layout["refs"]),
            layout["continuity"], layout["continuity_overlap_frames"],
            len(wf["nodes"]), len(wf["links"]))
        return wf, layout

    # ------------------------------------------------------------------ 落盘
    def build_to_file(self, out_path: str, segments: Sequence[dict],
                      **kwargs) -> Tuple[str, dict]:
        wf, layout = self.build(segments, **kwargs)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False)
        layout["path"] = out_path
        return out_path, layout

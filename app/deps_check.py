# -*- coding: utf-8 -*-
r"""依赖检测模块：核查用户机器上是否具备「模型权重」与「ComfyUI 自定义插件」

背景（见 docs/依赖清单.md）：
  本项目的工作流模板 JSON 已随项目内置，但真正跑起来还差两层：
    ① ComfyUI 侧要装对应的自定义插件包（custom_nodes/）
    ② ComfyUI 侧要下载对应的模型权重（models/ 下若干目录）

本模块做三件事，全部「只读、不阻断」：
  1. 读项目 workflows/ 下 7 个物理 JSON 模板（4×QwenImage2.1 + 3×H3；
     `WORKFLOW_TEMPLATE` 逻辑 key 有 8 个，因 multiview_gen/storyboard_gen
     共用 `分镜生成_Qwen21.json`——**逻辑 key 数 ≠ 物理文件数**），提取它们
     实际用到的**节点类型**集合；
  2. 把这些节点类型映射到所属**插件包**（见 docs/依赖清单.md §1），
     再判定「已装 / 未装 / 无法判定」；
  3. 核查模板 loader 与 MODEL_CHECKLIST 引用的**模型权重**（见下）。

模型核查策略（P0-1，2026-10-06 重写）——三层判据，**避免误报「缺失」**：
  ① **多根**：不再只扫 `MODELS_DIR` 单根，改用 `config.models_search_dirs()`
     （本机 MODELS_DIR 与第二套 ComfyUI 的 models/ 是两棵树，TRT engine 只在后者）；
  ② **相对路径直查**：MODEL_CHECKLIST 的 `path` 是相对 models 根的**多级路径**
     （如 `diffusion_models\minimax-h3\x.safetensors`），先按它逐根精确 `isfile`，
     命中即 found——不经过 basename；
  ③ **递归兜底**：未命中再 `os.walk` 全深度索引 basename，报告**全部候选**。
     同名文件出现在多个目录时标 `ambiguous=True` 并列出 candidates，
     **不以 basename 单独判定**（避免「A 目录的同名文件」冒充「B 目录的正主」）。

再叠一层 **ComfyUI `/object_info` combo 校验**（在线时）：把 checklist 条目换算成
loader 合法候选（`子目录\文件名` 或裸文件名），核对该值是否真在 combo 列表里。
combo 没有 = ComfyUI 侧看不见它（放错目录/没被扫描到），即磁盘存在也会标
`combo_ok=False`——这才是「能不能跑」的权威判据。

判定策略（插件，双通道）：
  - 在线：ComfyUI /object_info 能访问时，以它为准（最权威——能注册的节点才算有）；
  - 离线：ComfyUI 不在线时，退化扫描 COMFYUI_ROOT/custom_nodes/ 目录名
     （插件包名通常＝目录名），给出「疑似已装 / 未装」并标注不可靠。

本模块不依赖 Flask，可单测：check_deps(comfyui_url=None) 强制离线模式。
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

import requests

from config import (
    COMFYUI_ROOT,
    COMFYUI_URL,
    MODELS_DIR,
    WORKFLOW_TEMPLATE,
    models_search_dirs,
    workflow_search_dirs,
)

# 节点类型 → 所属插件包（依据 docs/依赖清单.md §1，本机实测映射）。
# ⚠️ 2026-10-04：H3 视频现役模板已换成 **12 节点单采**（h3_director_r2v_单采.json），
#    故 MiniMaxH3DirectorRefine / BasicScheduler / NvidiaDLSSFrameInterpolation 这三个
#    **仅二采回退模板**（WORKFLOW_TEMPLATE["h3_video_refine"]）需要 —— 这里**不删**映射，
#    避免回退模板被误报「缺插件」。
NODE_TO_PLUGIN = {
    # H3 视频 · Director（现役 h3_director_r2v_单采.json；二采回退 h3_video_refine）
    "MiniMaxH3Director": {"plugin": "ComfyUI_MiniMaxH3_Director", "dir": "ComfyUI_MiniMaxH3_Director", "repo": "github.com/AIMixer/ComfyUI_MiniMaxH3_Director"},
    "MiniMaxH3DirectorRefine": {"plugin": "ComfyUI_MiniMaxH3_Director", "dir": "ComfyUI_MiniMaxH3_Director", "repo": "github.com/AIMixer/ComfyUI_MiniMaxH3_Director", "note": "仅二采回退模板需要"},
    "BasicScheduler": {"plugin": "ComfyUI_MiniMaxH3_Director", "dir": "ComfyUI_MiniMaxH3_Director", "repo": "github.com/AIMixer/ComfyUI_MiniMaxH3_Director", "note": "仅二采回退模板需要"},
    "ResolutionSelector": {"plugin": "ComfyUI_MiniMaxH3_Director / KJNodes", "dir": "ComfyUI_MiniMaxH3_Director", "repo": "github.com/AIMixer/ComfyUI_MiniMaxH3_Director", "note": "KJNodes 亦提供，任一即可"},
    "MiniMaxH3TRTVAELoader": {"plugin": "ComfyUI-H3VAE_TRT", "dir": "ComfyUI-H3VAE_TRT", "repo": "github.com/lihaoyun6/ComfyUI-H3VAE_TRT", "note": "视频 VAE 走 TRT；⚠ 音频 VAE 不能用它"},
    "NvidiaDLSSFrameInterpolation": {"plugin": "ComfyUI-NVIDIA-DLSS-Frame-Interpolation", "dir": "ComfyUI-NVIDIA-DLSS-Frame-Interpolation", "repo": "github.com/Comfy-Org/ComfyUI-NVIDIA-DLSS-Frame-Interpolation", "note": "仅二采回退模板需要"},
    "PathchSageAttentionKJ": {"plugin": "ComfyUI-KJNodes", "dir": "ComfyUI-KJNodes", "repo": "github.com/jjy1998/ComfyUI-KJNodes"},
    "EasyCache": {"plugin": "ComfyUI-KJNodes", "dir": "ComfyUI-KJNodes", "repo": "github.com/jjy1998/ComfyUI-KJNodes"},
    "XHImagePrecision": {"plugin": "ComfyUI-KJNodes", "dir": "ComfyUI-KJNodes", "repo": "github.com/jjy1998/ComfyUI-KJNodes"},
    "SolAttnPatch": {"plugin": "ComfyUI-SolAttn_triton", "dir": "ComfyUI-SolAttn_triton", "repo": None, "note": "⚠ morton=False 定档"},
    "TESpeedMiniMaxH3": {"plugin": "TE-Speed-MiniMaxH3", "dir": "TE-Speed-MiniMaxH3", "repo": None, "note": "加速插件"},
    # 图片 · QwenImage2.1（4 个 *_Qwen21.json）
    "TESpeedQwenImage21": {"plugin": "TE-Speed-QwenImage21", "dir": "TE-Speed-QwenImage21", "repo": "github.com/tl2012tl/TE-Speed-QwenImage21", "note": "本机为编译 .pyd"},
    "TextEncodeQwenImage21": {"plugin": "TE-Speed-QwenImage21", "dir": "TE-Speed-QwenImage21", "repo": "github.com/tl2012tl/TE-Speed-QwenImage21"},
    "SaveImageAdvanced": {"plugin": "TE-Speed-QwenImage21", "dir": "TE-Speed-QwenImage21", "repo": "github.com/tl2012tl/TE-Speed-QwenImage21"},
    "QwenImage21Cache": {"plugin": "TE-Speed-QwenImage21", "dir": "TE-Speed-QwenImage21", "repo": "github.com/tl2012tl/TE-Speed-QwenImage21"},
    # 可选/增强
    "PreviewAny": {"plugin": "ComfyUI_BFSNodes（可选）", "dir": "ComfyUI_BFSNodes", "repo": "github.com/chflouis1985/ComfyUI-BFSNodes", "optional": True, "note": "缺失可回退原生"},
    # legacy 回退链
    "H3ContinuousSeamlessJoinV14": {"plugin": "Herrgotts-H3-Infinite-Continuation-Suite", "dir": "Herrgotts-H3-Infinite-Continuation-Suite", "repo": None, "legacy": True},
    "MiniMaxLowVRAMAttention": {"plugin": "KJNodes / minimax-h3-audio-T8", "dir": "ComfyUI-KJNodes", "repo": None, "legacy": True},
    "MiniMaxChunkFeedForward": {"plugin": "KJNodes / minimax-h3-audio-T8", "dir": "ComfyUI-KJNodes", "repo": None, "legacy": True},
    "MiniMaxH3SigmaShift": {"plugin": "KJNodes / minimax-h3-audio-T8", "dir": "ComfyUI-KJNodes", "repo": None, "legacy": True},
}

# ComfyUI 原生节点白名单（无需任何插件）
CORE_NODE_TYPES = {
    "UNETLoader", "CLIPLoader", "LoraLoaderModelOnly", "VAELoader",
    "KSampler", "KSamplerSelect", "VAEDecode", "VAEEncode", "EmptyLatentImage",
    "CreateVideo", "SaveVideo", "MarkdownNote", "Note", "PreviewImage",
    "LoadImage", "LoadImageOutput", "GetNode", "SetNode", "PrimitiveFloat",
    "Text Multiline", "PrimitiveString", "PrimitiveInt", "Reroute",
}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

def _is_real_node_type(t: str) -> bool:
    """过滤掉 ComfyUI 的 Group 容器（type 为 UUID）与空值，只留真实节点类名。"""
    if not t or not isinstance(t, str):
        return False
    if t in ("Group", "GroupNode"):
        return False
    if _UUID_RE.match(t):
        return False
    return True

# 模型清单（与 docs/依赖清单.md §2 **同源**，2026-09-29 审计对齐双向补齐；
# required=False 的条目缺失只提示、不判"缺依赖"）
MODEL_CHECKLIST = [
    {"path": "diffusion_models\\minimax-h3\\minimax_h3_ref2va_pruned_int8_convrot.safetensors", "kind": "video_unet", "required": True, "note": "H3 UNET 主模型（现役，2026-09-27 起由 minimaxH3Singularity 换为 ref2va）"},
    {"path": "text_encoders\\minimax-h3\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "kind": "video_clip", "required": True, "note": "H3 CLIP（minimax/Qwen3-VL）；ComfyUI 实际归在 text_encoders/ 而非 clip/"},
    {"path": "loras\\minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "kind": "video_lora", "required": True, "note": "通道A 4 步加速 LoRA（单采模板 h3_director_r2v_单采.json 的 LoraLoaderModelOnly id=80，**唯一实际执行**的加速 LoRA）"},
    {"path": "loras\\minimax_h3\\minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors", "kind": "video_lora", "required": False, "note": "通道B fl2v 变体 LoRA（模板 id=55）**仅二采回退模板 h3_video_refine 使用，单采模板不加载**；H3_ENABLE_REFINE=False 时不执行，对成片零影响"},
    {"path": "vae\\minimax-h3\\minimax_h3_audio_vae_fp32.safetensors", "kind": "audio_vae", "required": True, "note": "音频 VAE"},
    {"path": "vae\\minimax_h3_vae_decoder_w4a16_awq.engine", "kind": "video_vae_trt", "required": True, "note": "TRT 视频 VAE 解码（GPU 架构专用）"},
    {"path": "vae\\minimax_h3_vae_encoder.engine", "kind": "video_vae_trt", "required": True, "note": "TRT 视频 VAE 编码（GPU 架构专用）"},
    {"path": "diffusion_models\\qwen_image_2.1_int8_convrot.safetensors", "kind": "image_unet", "required": True, "note": "QwenImage2.1 UNET"},
    {"path": "text_encoders\\qwen3vl_8b_int8_convrot.safetensors", "kind": "image_clip", "required": True, "note": "QwenImage2.1 CLIP；ComfyUI 实际归在 text_encoders/ 而非 clip/"},
    {"path": "vae\\qwen_image_2.1_vae_bf16.safetensors", "kind": "image_vae", "required": True, "note": "QwenImage2.1 VAE"},
    {"path": "diffusion_models\\minimax_h3_latent_upscaler_3d_bf16.safetensors", "kind": "video_latent_upscaler", "required": False, "note": "latent 3D 超分（可选，未进现役模板硬校验）"},
    {"path": "FlashVSR-v1.1\\diffusion_pytorch_model_streaming_dmd.safetensors", "kind": "upscale_dit", "required": False, "note": "超分 DiT（enable_upscale=true 才需要）"},
]


# ---------------- 工作流模板：节点类型 / 模型引用 提取 ----------------

def _resolve_workflow_files() -> List[Dict]:
    """把 WORKFLOW_TEMPLATE（key→文件名）解析成实际存在的文件清单。

    用 config.workflow_search_dirs() 的顺序找每个模板文件；找不到的记 missing。
    """
    dirs = [d for d in workflow_search_dirs() if d]
    items: List[Dict] = []
    seen_files = set()
    for key, fname in WORKFLOW_TEMPLATE.items():
        found = None
        for d in dirs:
            p = os.path.join(d, fname)
            if os.path.isfile(p):
                found = p
                break
        items.append({
            "key": key,
            "file": fname,
            "path": found or "",
            "found": found is not None,
        })
        if found:
            seen_files.add(found)
    # 去重：同一文件被多个 key 引用（multiview_gen/storyboard_gen 共用 分镜生成_Qwen21.json）
    dedup: List[Dict] = []
    for it in items:
        if it["path"] and any(x["path"] == it["path"] for x in dedup):
            continue
        dedup.append(it)
    return dedup


def _extract_nodes_and_models(items: List[Dict]) -> Dict:
    """读每个模板 JSON，聚合节点类型集合 + loader 写死的模型文件名。"""
    node_types: set = set()
    wf_nodes: List[Dict] = []      # 每个工作流的节点类型明细
    ref_models: set = set()        # 模板 loader 里出现的模型文件名（basename）

    for it in items:
        if not it["found"]:
            wf_nodes.append({"key": it["key"], "file": it["file"], "types": [], "error": "文件缺失"})
            continue
        try:
            with open(it["path"], encoding="utf-8") as f:
                j = json.load(f)
        except Exception as e:  # JSON 损坏
            wf_nodes.append({"key": it["key"], "file": it["file"], "types": [], "error": str(e)})
            continue
        types = sorted({
            n.get("type") for n in (j.get("nodes") or [])
            if isinstance(n, dict) and _is_real_node_type(n.get("type"))
        })
        node_types.update(types)
        wf_nodes.append({"key": it["key"], "file": it["file"], "types": types, "error": ""})

        # 提取 loader 的 widgets_values 里第一个字符串（通常是模型文件名）
        for n in j.get("nodes") or []:
            if not isinstance(n, dict):
                continue
            t = n.get("type")
            if t in ("UNETLoader", "CLIPLoader", "LoraLoaderModelOnly", "VAELoader",
                     "MiniMaxH3TRTVAELoader"):
                wv = n.get("widgets_values") or []
                for v in wv:
                    if isinstance(v, str) and (v.endswith(".safetensors") or v.endswith(".engine")
                                                or v.endswith(".ckpt") or v.endswith(".pth")):
                        ref_models.add(v.replace("\\", "/").split("/")[-1])

    return {
        "node_types": sorted(node_types),
        "per_workflow": wf_nodes,
        "ref_models": sorted(ref_models),
    }


# ---------------- ComfyUI 在线探测 ----------------

def _comfyui_online(url: str, timeout: int = 60) -> bool:
    """ComfyUI /system_stats 可达即在线。超时/连接失败/非 200 一律 False。"""
    if not url:
        return False
    try:
        r = requests.get(f"{url}/system_stats", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _fetch_object_info(url: str, timeout: int = 60) -> Optional[Dict]:
    """拉 /object_info（已注册的节点类型全集）。失败返回 None。"""
    if not url:
        return None
    try:
        r = requests.get(f"{url}/object_info", timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


# ---------------- 离线：扫描 custom_nodes 目录 ----------------

def _custom_nodes_dirs(root: str) -> List[str]:
    """COMFYUI_ROOT 下探测 custom_nodes/（两种结构），返回目录名列表。

    ComfyUI 可能装在：
      A) COMFYUI_ROOT/ComfyUI/ComfyUI/custom_nodes（portable）
      B) COMFYUI_ROOT/custom_nodes（标准）
      C) COMFYUI_ROOT 本身即 ComfyUI 根 → COMFYUI_ROOT/custom_nodes
    """
    if not root:
        return []
    cands = [
        os.path.join(root, "custom_nodes"),
        os.path.join(root, "ComfyUI", "ComfyUI", "custom_nodes"),
        os.path.join(root, "ComfyUI", "custom_nodes"),
    ]
    for c in cands:
        if os.path.isdir(c):
            try:
                return sorted(os.listdir(c))
            except OSError:
                continue
    return []


# ---------------- 模型文件核查（P0-1：多根 + 相对路径直查 + 递归兜底 + combo 校验） ----------------

# checklist 的 kind → 用于 /object_info combo 校验的 (节点类型, 字段)。
# 未列出的 kind（超分 DiT / latent upscaler 等）没有可靠 loader 映射 → combo 校验跳过。
_KIND_LOADER = {
    "video_unet":   [("UNETLoader", "unet_name")],
    "image_unet":   [("UNETLoader", "unet_name")],
    "video_clip":   [("CLIPLoader", "clip_name")],
    "image_clip":   [("CLIPLoader", "clip_name")],
    "audio_vae":    [("VAELoader", "vae_name")],
    "image_vae":    [("VAELoader", "vae_name")],
    "video_lora":   [("LoraLoaderModelOnly", "lora_name")],
    # TRT 引擎在 loader 的 decoder / encoder 两个 combo 里各出现一次，两者都验
    "video_vae_trt": [("MiniMaxH3TRTVAELoader", "decoder"),
                      ("MiniMaxH3TRTVAELoader", "encoder")],
}

_MODEL_SUFFIXES = (".safetensors", ".engine", ".ckpt", ".pth", ".pt", ".gguf", ".bin")
# walk 时跳过的目录（缓存/版本库，不是模型存放处）
_SKIP_WALK_DIRS = {"__pycache__", ".git", ".cache", "node_modules", "$RECYCLE.BIN", "System Volume Information"}
# 最大下探深度（模型一般 ≤3 层；留余量但防止扫到巨型目录树）
_MAX_WALK_DEPTH = 8


def _model_roots(models_dir: str = "") -> List[str]:
    """模型根目录列表：config.models_search_dirs() 优先，models_dir 作兜底补充。"""
    roots: List[str] = []
    try:
        roots = [d for d in (models_search_dirs() or []) if d]
    except Exception:  # noqa: BLE001  config 尚未就绪时不影响离线检测
        roots = []
    if models_dir and models_dir not in roots:
        roots.insert(0, models_dir)
    return [r for r in roots if os.path.isdir(r)]


def _walk_index(roots: List[str]) -> Dict[str, List[Dict]]:
    """递归索引全部模型文件：basename(小写) → [{root, rel}]。

    相对深度截断在 ``_MAX_WALK_DEPTH``，跳过 ``_SKIP_WALK_DIRS``。
    """
    index: Dict[str, List[Dict]] = {}
    for root in roots:
        root = os.path.normpath(root)
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                rel_dir = os.path.relpath(dirpath, root)
                depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
                if depth >= _MAX_WALK_DEPTH:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if d not in _SKIP_WALK_DIRS]
                for fn in filenames:
                    low = fn.lower()
                    if not low.endswith(_MODEL_SUFFIXES):
                        continue
                    rel = os.path.normpath(os.path.join(rel_dir, fn)) if rel_dir != "." else fn
                    index.setdefault(low, []).append({"root": root, "rel": rel})
        except OSError:
            continue
    return index


def _combo_values_for(object_info, node_type: str, field: str) -> Optional[List[str]]:
    """取 object_info 里某节点某字段的 combo 合法值列表（拿不到返回 None）。"""
    if not object_info:
        return None
    try:
        blob = object_info.get(node_type) or {}
        spec = ((blob.get("input") or {}).get("required") or {}).get(field)
    except (AttributeError, TypeError):
        return None
    if not isinstance(spec, (list, tuple)) or not spec:
        return None
    opts = spec[0]
    return list(opts) if isinstance(opts, (list, tuple)) else None


def _combo_candidates(rel_paths: List[str]) -> List[str]:
    """把磁盘相对路径换算成 ComfyUI combo 的可能写法。

    combo 值是「相对**类别子目录**的路径」：`diffusion_models/A/B.safetensors`
    → combo 里写作 `A\\B.safetensors`；直接放在类别根下的 → 裸文件名。
    """
    out: List[str] = []
    for rel in rel_paths:
        norm = str(rel).replace("/", "\\")
        parts = norm.split("\\")
        # 全路径去掉类别根（第一段）→ 子目录\文件名
        if len(parts) >= 2:
            out.append("\\".join(parts[1:]))
        # 裸文件名（模型直接放在类别根下时 combo 就是它）
        out.append(parts[-1])
    # 去重保序
    seen, uniq = set(), []
    for c in out:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def _check_models(ref_models: List[str], models_dir: str = "",
                  object_info: Optional[Dict] = None) -> List[Dict]:
    """核查模型权重是否就位（P0-1 三层判据 + object_info combo 交叉验证）。

    输入：
      ref_models  模板 loader 里提取到的 basename（可能为空，空则只按 checklist）
      models_dir  旧参数，作为根目录兜底（真正的根列表来自 config.models_search_dirs）
      object_info ComfyUI /object_info（在线才有）；None → combo 校验跳过

    返回每项：
      {name, kind, required, found, path, rel, roots, candidates, ambiguous,
       match, checklist_path, combo_ok, combo_value, subdirs}
      match: "exact"（按 checklist 相对路径精确命中）/ "recursive"（仅递归找到）/
             "basename"（只在模板引用里、无 checklist 条目）/ ""（未找到）
      combo_ok: True/False/None（None=离线或该 kind 无 loader 映射 → 未测试）
    """
    roots = _model_roots(models_dir)
    items: Dict[str, Dict] = {}

    # ① MODEL_CHECKLIST：按相对路径逐根精确直查（主判据，不经过 basename）
    for m in MODEL_CHECKLIST:
        rel = str(m.get("path") or "").replace("\\", "/").strip("/")
        if not rel:
            continue
        name = rel.split("/")[-1]
        ent = items.setdefault(name, {
            "name": name, "kind": "", "required": False, "found": False,
            "path": "", "rel": "", "roots": list(roots), "candidates": [],
            "ambiguous": False, "match": "", "checklist_path": "",
            "combo_ok": None, "combo_value": "", "subdirs": [],
            "notes": [],
        })
        ent["kind"] = ent["kind"] or str(m.get("kind") or "")
        ent["required"] = bool(ent["required"] or m.get("required"))
        ent["checklist_path"] = ent["checklist_path"] or rel
        if m.get("note"):
            ent["notes"].append(str(m["note"]))

        for root in roots:
            # 大小写不敏感的多级路径直查（Windows 天然不敏感；POSIX 上也按原样试一次）
            cand = os.path.join(root, *rel.split("/"))
            if os.path.isfile(cand):
                ent["found"] = True
                ent["path"] = cand
                ent["rel"] = rel
                ent["match"] = "exact"
                break
            if os.path.isfile(cand.replace("\\", "/")):  # POSIX 兜底
                ent["found"] = True
                ent["path"] = cand.replace("\\", "/")
                ent["rel"] = rel
                ent["match"] = "exact"
                break

    # ② 递归索引兜底（多级目录 / checklist 路径写得与磁盘不一致时）
    walk_idx = _walk_index(roots) if roots else {}

    def _merge_walk(ent: Dict, name: str):
        hits = walk_idx.get(name.lower()) or []
        if not hits:
            return
        cands = [f"{h['root']}::{h['rel']}" for h in hits]
        # 已有精确命中 → 只补充 candidates/歧义信息，不改 found/path
        for c in cands:
            if c not in ent["candidates"]:
                ent["candidates"].append(c)
        if len(cands) > 1:
            ent["ambiguous"] = True
            if not ent["found"]:
                ent["notes"].append(
                    f"同名文件出现在 {len(cands)} 个位置，需人工确认哪个才是正主")
        if not ent["found"]:
            ent["found"] = True
            ent["path"] = os.path.join(hits[0]["root"], hits[0]["rel"])
            ent["rel"] = hits[0]["rel"]
            ent["match"] = "recursive"
        elif ent.get("checklist_path") and hits[0]["rel"].replace("\\", "/") != ent["checklist_path"]:
            # 精确命中了 A，但别处还有同名 → 记一笔，避免"看起来对"其实可能取错
            if len(cands) > 1:
                ent["notes"].append("checklist 相对路径与磁盘布局不完全一致，已按 checklist 路径判定")

    for name, ent in list(items.items()):
        _merge_walk(ent, name)

    # ③ 模板 loader 提取到、但 checklist 没有的引用（basename 判据，标注 match=basename）
    for name in (ref_models or []):
        key = str(name).split("/")[-1].split("\\")[-1]
        if not key:
            continue
        ent = items.get(key)
        if ent is None:
            ent = items[key] = {
                "name": key, "kind": "", "required": False, "found": False,
                "path": "", "rel": "", "roots": list(roots), "candidates": [],
                "ambiguous": False, "match": "basename", "checklist_path": "",
                "combo_ok": None, "combo_value": "", "subdirs": [],
                "notes": ["来自模板 loader 引用，不在 MODEL_CHECKLIST 中"],
            }
            _merge_walk(ent, key)
        elif not ent.get("checklist_path"):
            ent["match"] = ent["match"] or "basename"

    # ④ object_info combo 交叉验证（在线才做）
    for ent in items.values():
        kind = ent.get("kind") or ""
        loaders = _KIND_LOADER.get(kind)
        if object_info is None or not loaders:
            ent["combo_ok"] = None
            continue
        # 候选写法：checklist 相对路径 + 实际命中的磁盘相对路径
        rels = [p for p in [ent.get("checklist_path"), ent.get("rel")] if p]
        cands = _combo_candidates(rels)
        hit_val = ""
        ok = False
        tested = False
        for nt, fld in loaders:
            combo = _combo_values_for(object_info, nt, fld)
            if combo is None:
                continue  # 该 loader/字段取不到 → 不下结论
            tested = True
            low_map = {str(v).lower(): str(v) for v in combo}
            for c in cands:
                got = low_map.get(c.lower())
                if got is not None:
                    ok = True
                    hit_val = got
                    break
            if ok:
                break
        # object_info 在线但缺 loader/字段时保持 None（未测试），不能误判成不兼容。
        ent["combo_ok"] = ok if tested else None
        ent["combo_value"] = hit_val
        if ent["combo_ok"] is False and ent["found"]:
            ent["notes"].append(
                "磁盘存在，但不在 ComfyUI /object_info 合法候选里（目录未被 ComfyUI 扫描到）")

    # 输出顺序：checklist 必需项在前，其余按名称
    out = sorted(items.values(), key=lambda e: (not e["required"], e["name"]))
    for ent in out:
        paths = []
        if ent.get("path"):
            paths.append(ent["path"])
        for candidate in ent.get("candidates") or []:
            if "::" in candidate:
                root, rel = candidate.split("::", 1)
                paths.append(os.path.join(root, rel))
        ent["subdirs"] = sorted({
            os.path.dirname(p) for p in paths if p and os.path.dirname(p)
        })
    return out



# ---------------- 主入口 ----------------

def check_deps(comfyui_url: Optional[str] = None,
               force_offline: bool = False) -> Dict:
    """依赖检测主入口。

    参数：
      comfyui_url  指定 ComfyUI 地址（默认用 config.COMFYUI_URL）。传 None 且
                   force_offline=False 时用默认 URL 尝试在线。
      force_offline  强制离线（跳过 /object_info，只扫 custom_nodes 目录）。

    返回结构化 dict：
      {
        comfyui: {url, online, source},          # source: online|offline|unconfigured
        plugins: {
            needed: [ {node_type, plugin, repo, optional, legacy, status} ],
            ready: bool,                        # 全部必需插件就位（非 legacy/非 optional）
            missing_required: [ ... ],
            missing_optional: [ ... ],
        },
        models: {
            dir, dir_exists,
            items: [ {name, found, path} ],
            ready: bool,
            missing_required: [ ... ],
        },
        workflows: [ {key, file, found, types} ],
        summary: {plugins_ok, models_ok, all_ok, blockers: [ ... ]},
      }
    """
    url = comfyui_url if comfyui_url is not None else COMFYUI_URL
    online = False if force_offline else _comfyui_online(url)
    object_info = None if force_offline else _fetch_object_info(url)
    online_nodes = set(object_info.keys()) if object_info else None

    # ---- 工作流与节点 ----
    wf_items = _resolve_workflow_files()
    extracted = _extract_nodes_and_models(wf_items)
    all_types: set = set(extracted["node_types"])

    # ---- 插件判定 ----
    needed: List[Dict] = []
    for nt in sorted(all_types):
        if nt in CORE_NODE_TYPES:
            continue
        spec = NODE_TO_PLUGIN.get(nt, {"plugin": "未知插件", "dir": nt, "repo": None})
        # 状态：
        if online_nodes is not None:
            # 在线：该节点类型在 object_info 注册即认为对应插件可用
            registered = nt in online_nodes
            # Director 包还负责 ResolutionSelector 等；只要任一节点注册成功即认为该插件就位
            status = "ok" if registered else "missing"
        else:
            # 离线：扫 custom_nodes 目录名（插件包目录≈目录名）
            cdirs = _custom_nodes_dirs(COMFYUI_ROOT)
            hit = any(
                d.replace("-", "").lower() == spec["dir"].replace("-", "").lower()
                for d in cdirs
            ) if cdirs else False
            status = "suspected" if hit else ("missing" if not cdirs else "unknown")
            # cdirs 为空（找不到 custom_nodes）→ 无法判定
            if not cdirs:
                status = "unknown"
        needed.append({
            "node_type": nt,
            "plugin": spec["plugin"],
            "repo": spec.get("repo"),
            "optional": bool(spec.get("optional")),
            "legacy": bool(spec.get("legacy")),
            "note": spec.get("note", ""),
            "status": status,
        })

    missing_required = [p for p in needed
                        if p["status"] in ("missing", "unknown")
                        and not p["optional"] and not p["legacy"]]
    missing_optional = [p for p in needed
                        if p["status"] in ("missing", "unknown") and p["optional"]]

    # ---- 模型核查 ----
    ref_models = extracted["ref_models"]
    # 若模板 loader 里没提取到模型名（例如模板被改动），回落用 MODEL_CHECKLIST 的 basename
    if not ref_models:
        ref_models = [m["path"].split("\\")[-1] for m in MODEL_CHECKLIST if m.get("required")]
    model_roots = _model_roots(MODELS_DIR)
    model_items = _check_models(ref_models, MODELS_DIR, object_info=object_info)

    # 必需模型清单（来自 MODEL_CHECKLIST）
    required_names = {m["path"].split("\\")[-1] for m in MODEL_CHECKLIST if m.get("required")}
    required_lower = {n.lower() for n in required_names}
    models_missing_required = [
        m["name"] for m in model_items
        if m["name"].lower() in required_lower and not m["found"]
    ]
    # 磁盘有但 ComfyUI combo 看不见：属于独立 blocker；保留 missing_required 的旧语义。
    models_combo_invisible = [
        m["name"] for m in model_items
        if m["name"].lower() in required_lower and m["found"] and m.get("combo_ok") is False
    ]
    models_dir_exists = bool(model_roots)

    # ---- 汇总 ----
    plugins_ok = len(missing_required) == 0
    models_ok = not models_missing_required and not models_combo_invisible
    all_ok = plugins_ok and models_ok and online  # 在线是「可实际运行」前提
    blockers: List[str] = []
    if not online:
        blockers.append("ComfyUI 不在线（无法确认插件节点是否注册）")
    if not models_dir_exists:
        blockers.append(f"模型搜索目录不存在：{MODELS_DIR or '(未配置)'}")
    if missing_required:
        blockers.append(f"缺失必需插件：{', '.join(p['plugin'] for p in missing_required)}")
    if models_missing_required:
        blockers.append(f"缺失必需模型：{', '.join(models_missing_required)}")
    if models_combo_invisible:
        blockers.append(
            "必需模型未被 ComfyUI 识别（/object_info combo 不可见）："
            + ", ".join(models_combo_invisible)
        )

    return {
        "comfyui": {
            "url": url,
            "online": online,
            "source": "online" if online_nodes is not None
                      else ("offline_scan" if _custom_nodes_dirs(COMFYUI_ROOT) else "unconfigured"),
            "online_node_count": len(online_nodes) if online_nodes is not None else 0,
        },
        "plugins": {
            "needed": needed,
            "ready": plugins_ok,
            "missing_required": missing_required,
            "missing_optional": missing_optional,
        },
        "models": {
            "dir": MODELS_DIR,
            "dir_exists": models_dir_exists,
            "search_roots": model_roots,
            "items": model_items,
            "required_names": sorted(required_names),
            "ready": models_ok,
            "missing_required": models_missing_required,
            "combo_invisible_required": models_combo_invisible,
        },
        "workflows": [
            {"key": w["key"], "file": w["file"], "found": w["found"],
             "types": [x["types"] for x in extracted["per_workflow"] if x["key"] == w["key"]][0]
             if any(x["key"] == w["key"] for x in extracted["per_workflow"]) else []}
            for w in wf_items
        ],
        "per_workflow_nodes": extracted["per_workflow"],
        "summary": {
            "plugins_ok": plugins_ok,
            "models_ok": models_ok,
            "all_ok": all_ok,
            "blockers": blockers,
        },
    }




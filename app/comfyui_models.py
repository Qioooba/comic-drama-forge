# -*- coding: utf-8 -*-
"""ComfyUI 模型 / 插件扫描 + 用户选定模型的持久化。

背景（2026-09-29）：
    H3 Director 工作流里两个 UNETLoader 原本**写死裸文件名**，而 ComfyUI 的
    UNETLoader combo 合法值取决于模型在磁盘上的**目录布局**（放进
    `diffusion_models/minimax-h3/` 后名字会带 `minimax-h3\\` 前缀），
    于是模板里的裸名不在合法值列表里 → 校验失败 → 视频输出被静默丢弃。

    根因教训：ComfyUI combo 值 = 目录布局 + 文件名，**挪动模型等于改名**。
    任何在模板里写死模型文件名的做法都是脆的。因此这里统一以
    ComfyUI `object_info` 为**唯一权威来源**去枚举可选模型，让用户手动指定。

对外 API：
    scan(force=False)            -> 扫描 ComfyUI 可用模型槽位候选 + 已装插件
    load_selection()             -> 读取用户已选定的模型覆盖表
    save_selection(dict)         -> 原子写入用户选定（None/"" 表示清空该槽位）

设计要点：
    - 只读为主：scan 不修改任何工作流/模板，只上报。
    - 向前兼容：未选择任何槽位时 load_selection() 返回空 dict，
      注入层据此保持模板原样，行为与改动前完全一致。
"""

import logging
import os
import time

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 持久化

# 全局（与项目无关）：ComfyUI 是本机唯一一套模型，按项目分裂反而是负担。
_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "comfyui_models.json",
)

# ---------------------------------------------------------------- 槽位定义
#
# slot_key 面向**用户可读的职责**（"视频主模型"），而不是面向模板节点 id。
# targets 描述该槽位要写入哪个工作流的哪些节点（Task #127 注入层据此落地）。
#
# node_type / field 决定从 object_info 的哪一处取候选值。
SLOTS = {
    "unet_main": {
        "label": "视频主模型（UNET）",
        "hint": "两条采样通道共用同一个主模型，选一次即同时写入两处",
        "node_type": "UNETLoader",
        "field": "unet_name",
        "targets": {"h3_video": [1, 54]},
    },
    "lora_channel_a": {
        "label": "通道A 加速 LoRA",
        "hint": "ref2v 分支的加速 LoRA（模板 id=16）",
        "node_type": "LoraLoaderModelOnly",
        "field": "lora_name",
        "targets": {"h3_video": [16]},
    },
    "lora_channel_b": {
        "label": "通道B 加速 LoRA",
        "hint": "fl2v 分支的加速 LoRA（模板 id=55）",
        "node_type": "LoraLoaderModelOnly",
        "field": "lora_name",
        "targets": {"h3_video": [55]},
    },
    "clip": {
        "label": "文本编码器（CLIP）",
        "hint": "影响中文语义理解与提示词还原度",
        "node_type": "CLIPLoader",
        "field": "clip_name",
        "targets": {"h3_video": [2]},
    },
    "vae_audio": {
        "label": "音频 VAE",
        "hint": "⚠️ 必须接 fp32 音频专用 VAE，接视频 VAE 会因通道数不符崩溃",
        "node_type": "VAELoader",
        "field": "vae_name",
        "targets": {"h3_video": [67]},
    },
    "vae_video_decoder": {
        "label": "视频 VAE 解码器（TRT 引擎）",
        "hint": "MiniMaxH3TRTVAELoader 的 decoder",
        "node_type": "MiniMaxH3TRTVAELoader",
        "field": "decoder",
        "targets": {"h3_video": [48]},
    },
    "vae_video_encoder": {
        "label": "视频 VAE 编码器（TRT 引擎）",
        "hint": "MiniMaxH3TRTVAELoader 的 encoder",
        "node_type": "MiniMaxH3TRTVAELoader",
        "field": "encoder",
        "targets": {"h3_video": [48]},
    },
}

# 槽位展示顺序（保持字典顺序稳定，避免前端乱序）
SLOT_ORDER = ["unet_main", "clip", "vae_video_decoder", "vae_video_encoder",
              "vae_audio", "lora_channel_a", "lora_channel_b"]


# ---------------------------------------------------------------- 扫描

def _combo_values(object_info, node_type, field):
    """从 object_info 取某节点某字段的候选值列表。

    ComfyUI 的 combo 声明在 `input.required[field]` 的第一个元素里，
    形如 `[["a.safetensors", "b.safetensors"], {...}]`。
    """
    try:
        blob = (object_info or {}).get(node_type) or {}
        req = (blob.get("input") or {}).get("required") or {}
        spec = req.get(field)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(spec, (list, tuple)) or not spec:
        return []
    opts = spec[0]
    if isinstance(opts, list):
        return list(opts)
    return []


def _installed_packages(object_info):
    """聚合已安装的自定义节点插件包。

    两种常见做法这里都不用：

    * ``/extensions`` 会返回几百个**内置前端 JS 路径**，噪音极大，没有参考价值；
    * ComfyUI Manager 的 ``cnr_id`` 并非所有环境都装了 Manager，依赖它就等于
      把「能不能看见插件」绑定到「有没有装另一个插件」上。

    改用 ``object_info`` 里每个节点都有的 ``python_module``：

    * 内置核心节点 → ``nodes`` / ``comfy.*``；
    * 自定义节点 → ``custom_nodes.<包名>.<模块>``（实测 MiniMaxH3Director 就是
      ``custom_nodes.ComfyUI_MiniMaxH3_Director``）。

    据此即可稳定地推断出装了哪些自定义节点包，无需额外依赖。
    """
    pkgs = {}
    core = 0
    for cls, blob in (object_info or {}).items():
        mod = ""
        try:
            mod = (blob or {}).get("python_module") or ""
        except Exception:  # noqa: BLE001
            continue
        if not mod.startswith("custom_nodes."):
            core += 1
            continue
        pkg = mod[len("custom_nodes."):].split(".")[0] or "(unknown)"
        entry = pkgs.setdefault(pkg, {"id": pkg, "node_count": 0, "nodes": []})
        entry["node_count"] += 1
        if len(entry["nodes"]) < 12:
            entry["nodes"].append(cls)
    return {
        "custom_packages": sorted(pkgs.values(), key=lambda x: -x["node_count"]),
        "core_node_count": core,
    }


def scan(client, force=False):
    """扫描 ComfyUI：各模型槽位的合法候选值 + 已安装插件。

    失败不抛：返回 success=False 与错误信息，前端可据此提示「ComfyUI 离线」。
    """
    t0 = time.time()
    out = {
        "success": True,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slots": [],
        "plugins": [],
        "error": "",
    }
    try:
        object_info = client.get_object_info(force=bool(force))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ComfyUI 模型扫描失败: {e}")
        return {**out, "success": False, "error": f"ComfyUI 未就绪：{e}"}

    if not object_info:
        return {**out, "success": False, "error": "ComfyUI 在线但未返回 object_info"}

    selection = load_selection()
    for key in SLOT_ORDER:
        meta = SLOTS.get(key)
        if not meta:
            continue
        values = _combo_values(object_info, meta["node_type"], meta["field"])
        current = selection.get(key) or ""
        out["slots"].append({
            "key": key,
            "label": meta["label"],
            "hint": meta["hint"],
            "node_type": meta["node_type"],
            "field": meta["field"],
            "values": values,
            "selected": current,
            # 已选值不在合法值里 → 标红，避免用户以为"选了但没生效"
            "selected_valid": (not current) or (current in values),
            "available": bool(values),
        })

    pkgs = _installed_packages(object_info)
    out["plugins"] = pkgs["custom_packages"]
    out["core_node_count"] = pkgs["core_node_count"]
    out["node_type_count"] = len(object_info)
    out["elapsed_sec"] = round(time.time() - t0, 2)
    return out


# ---------------------------------------------------------------- 持久化

def load_selection():
    """读取用户已选定的模型覆盖表（key -> 模型名）。文件不存在返回空 dict。"""
    try:
        data = read_json_strict(_STORE_PATH, default={})
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    # 只保留已知槽位，防脏数据污染注入层
    return {k: v for k, v in data.items() if k in SLOTS and isinstance(v, str)}


def save_selection(patch):
    """合并写入用户选定。value 为 None/"" 表示清空该槽位（回落到模板原值）。"""
    if not isinstance(patch, dict):
        return load_selection()
    cur = load_selection()
    for key, val in patch.items():
        if key not in SLOTS:
            continue
        if val in (None, ""):
            cur.pop(key, None)
        else:
            cur[key] = str(val)
    try:
        os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
        atomic_write_json(_STORE_PATH, cur)
    except Exception as e:  # noqa: BLE001
        logger.error(f"写入 ComfyUI 模型选择失败: {e}")
        raise
    return cur


def resolve_selection():
    """给注入层用：返回 {slot_key: value}（已剔除空值）。"""
    return {k: v for k, v in load_selection().items() if v}


def overrides_for(workflow_key="h3_video"):
    """把用户选定翻译成「模板节点级」覆盖表：{node_id: {field: value}}。

    这是「用户可读槽位」到「模板具体节点」的唯一转换点——注入层不需要
    知道 slot 语义，只需要按返回的 {id: {field: value}} 写 widgets。

    未做任何选择时返回空 dict，注入层据此保持模板原样（行为等同改动前）。
    """
    selection = resolve_selection()
    if not selection:
        return {}
    out = {}
    for key, val in selection.items():
        meta = SLOTS.get(key)
        if not meta:
            continue
        field = meta.get("field")
        if not field:
            continue
        for nid in ((meta.get("targets") or {}).get(workflow_key) or []):
            out.setdefault(nid, {})[field] = val
    return out

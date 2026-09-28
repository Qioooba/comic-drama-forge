# -*- coding: utf-8 -*-
"""H3 Ref2VA 工作流 · 动态段数构建器（9 张参考图 · 独立起片）

背景
----
项目此前用的是 ``h3_episode_builder.H3EpisodeBuilder``（连续续接模式）：
1 个 ``H3ContinuousStartV14`` + N-1 个 ``H3ContinuousContinueV14`` 靠 latent + handover
无缝续接，参考图只挂了 ``qwen_reference_1/2`` 两个槽位（第 3/4 槽空着）。

用户诉求（2026-09-26）：改用 **MiniMax Ref2VA 多参考图模式**，参考图扩到 **9 张**
（``ref_images.ref_image_0..8``，autogrow 类型）。代价是放弃 latent 级无缝续接，
每个分镜**独立起片**（每段一条独立视频），段间连贯靠参考图 + 提示词硬控。

本模块职责
----------
1. 以 ``多参模式 .json``（单段 Ref2VA 起片 + BUNNY 一采 + BUNNY 二采）为模板蓝本；
2. 按目标段数 N 重建工作流：
   - **共享资源**（只保留一份）：UNET / CLIP / VAE×2 / SageAttentionPatch / 加速 LoRA 链 /
     一采模型链 / 二采模型链 / KSamplerSelect / BasicScheduler / ExtendIntermediateSigmas /
     SplitSigmasDenoise / ResolutionSelector。
   - **逐段复制**（每段独立）：9 个 LoadImage（ref_image_0..8）+ prompt + 时长换算 +
     MiniMaxH3ReferenceToVideo + 一采（RandomNoise/BasicGuider/SamplerCustomAdvanced）+
     二采（DisableNoise/BasicGuider/SamplerCustomAdvanced）+ VAEDecode/VAEDecodeAudio +
     CreateVideo + SaveVideo（独立 filename_prefix = shot_XX）。

输出
----
``build(n_segments)`` 返回 ``(ui_workflow, layout)``。layout 给出每段的：
起片节点 id、参考图节点 id、prompt/时长节点 id、SaveVideo 节点 id，
供 ``comfyui_client.generate_h3_sequence`` 逐段注入 + 逐镜取回。
"""

import copy
import json
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 模板中要保留的「共享资源」节点类型（只保留一份，不逐段复制）
# 注意：这些节点在模板里只有一份，其连线引用的是「模板内其它共享节点 + 段内节点」，
# 重建时只把「段内节点」换成新的，共享节点 id 保持不变。
SHARED_TYPES = frozenset({
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "MiniMaxH3MemoryEfficientSageAttentionPatch",
    "LoraLoaderModelOnly",          # 加速 / 一采 / 二采 全部 LoRA 链都共享
    "KSamplerSelect",
    "BasicScheduler",
    "ExtendIntermediateSigmas",
    "SplitSigmasDenoise",
    "ResolutionSelector",
})

# 段内逐段复制的节点类型（每段一份）
SEGMENT_TYPES = frozenset({
    "MiniMaxH3ReferenceToVideo",
    "LoadImage",
    "PrimitiveStringMultiline",
    "ComfyMathExpression",
    "PrimitiveFloat",
    "RandomNoise",
    "BasicGuider",
    "DisableNoise",
    "SamplerCustomAdvanced",
    "VAEDecode",
    "VAEDecodeAudio",
    "CreateVideo",
    "SaveVideo",
    "easy cleanGpuUsed",
})

# 每段参考图槽位（autogrow：ref_image_0..8 共 9 张）
REF_SLOT_NAMES = [f"ref_images.ref_image_{i}" for i in range(9)]

# 时长（秒）→ 帧数 的换算表达式（与模板 ComfyMathExpression 一致，fps=24，
# 向上对齐到 17 的倍数 + 5）。作为单一事实源，避免手改数学出错。
LENGTH_EXPR = "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"

FPS = 24


class H3Ref2VABuilder:
    """按分镜数动态生成 N 段独立起片的 Ref2VA 工作流（9 张参考图）。"""

    def __init__(self, template_path: str):
        self.template_path = template_path
        with open(template_path, "r", encoding="utf-8-sig") as f:
            self.template = json.load(f)
        self.nodes_by_id = {n.get("id"): n for n in (self.template.get("nodes") or [])}
        self.links_by_id: Dict[int, tuple] = {}
        for l in self.template.get("links") or []:
            if isinstance(l, dict):
                self.links_by_id[l["id"]] = (l["origin_id"], l["origin_slot"],
                                             l["target_id"], l["target_slot"])
            elif isinstance(l, (list, tuple)) and len(l) >= 5:
                self.links_by_id[l[0]] = (l[1], l[2], l[3], l[4])
        node_ids = [n["id"] for n in (self.template.get("nodes") or [])
                    if isinstance(n.get("id"), int)]
        self._max_node_id = max(node_ids) if node_ids else 0
        link_ids = [k for k in self.links_by_id if isinstance(k, int)]
        self._max_link_id = max(link_ids) if link_ids else 0
        self._cursor_node = self._max_node_id
        self._cursor_link = self._max_link_id

    # ------------------------------------------------------------------ 基础工具
    @staticmethod
    def _slot_of(node: dict, name: str, kind: str = "outputs") -> Optional[int]:
        for i, s in enumerate(node.get(kind) or []):
            if s.get("name") == name:
                return i
        return None

    def _new_node_id(self) -> int:
        self._cursor_node += 1
        return self._cursor_node

    def _new_link_id(self) -> int:
        self._cursor_link += 1
        return self._cursor_link

    @staticmethod
    def _connect(links: List[list], index: Dict[int, dict], link_id: int,
                 origin_id: int, origin_name: str,
                 target_id: int, target_name: str) -> list:
        """建立连线，同步维护 outputs[].links + inputs[].link（ComfyUI 前端一致）。"""
        o, t = index[origin_id], index[target_id]
        os_ = H3Ref2VABuilder._slot_of(o, origin_name, "outputs")
        ts = H3Ref2VABuilder._slot_of(t, target_name, "inputs")
        if os_ is None or ts is None:
            raise KeyError(
                f"连线失败: {origin_id}.{origin_name} -> {target_id}.{target_name}")
        typ = (o["outputs"][os_].get("type") or t["inputs"][ts].get("type") or "*")
        lk = [link_id, origin_id, os_, target_id, ts, typ]
        links.append(lk)
        olinks = o["outputs"][os_].get("links")
        if olinks is None:
            o["outputs"][os_]["links"] = olinks = []
        olinks.append(link_id)
        t["inputs"][ts]["link"] = link_id
        return lk

    # ------------------------------------------------------------------ 解析
    def analyze(self) -> dict:
        """解析模板：定位共享节点、段模板节点、参考图槽位上限、输出槽名。

        共享节点 key 统一用 ``type`` 或 ``type#序号``（同类型靠出现顺序区分），
        标题只存在节点本身，不参与 key —— 避免「标题=CLIP」导致 ``CLIPLoader`` 取不到。
        """
        shared: Dict[str, int] = {}
        segment_nodes: Dict[str, dict] = {}
        # 记录每个 type 已出现的次数，用于同类型节点编号
        type_counter: Dict[str, int] = {}
        for n in self.template.get("nodes") or []:
            t = n.get("type") or ""
            if t in SHARED_TYPES:
                idx = type_counter.get(t, 0)
                type_counter[t] = idx + 1
                key = t if idx == 0 else f"{t}#{idx}"
                shared[key] = n["id"]
            elif t in SEGMENT_TYPES:
                # 段内节点按类型收集（LoadImage 有多个，逐段用模板里第 1 个做底）
                if t not in segment_nodes:
                    segment_nodes[t] = n
        return {"shared": shared, "segment_nodes": segment_nodes}

    # ------------------------------------------------------------------ 分辨率
    def _template_resolution(self) -> Optional[Tuple[int, int]]:
        sel = next((n for n in self.template.get("nodes") or []
                    if n.get("type") == "ResolutionSelector"), None)
        if sel is None:
            return None
        named = sel.get("widgets_values_named") or {}
        vals = sel.get("widgets_values") or []

        def _pick(key, idx):
            return named.get(key, vals[idx] if len(vals) > idx else None)

        aspect = _pick("aspect_ratio", 0)
        megapixels = _pick("megapixels", 1)
        multiple = _pick("multiple", 2)
        ratios = {
            "1:1 (Square)": (1, 1), "2:3 (Portrait Photo)": (2, 3),
            "3:2 (Photo)": (3, 2), "3:4 (Portrait Standard)": (3, 4),
            "4:3 (Standard)": (4, 3), "9:16 (Portrait Widescreen)": (9, 16),
            "16:9 (Widescreen)": (16, 9), "21:9 (Ultrawide)": (21, 9),
        }
        if aspect not in ratios or megapixels is None or multiple is None:
            return None
        try:
            megapixels = float(megapixels)
            multiple = int(multiple)
        except (TypeError, ValueError):
            return None
        if multiple <= 0:
            return None
        import math
        w_ratio, h_ratio = ratios[aspect]
        scale = math.sqrt(megapixels * 1024 * 1024 / (w_ratio * h_ratio))
        width = round(w_ratio * scale / multiple) * multiple
        height = round(h_ratio * scale / multiple) * multiple
        return int(width), int(height)

    # ------------------------------------------------------------------ 构建
    def build(self, n_segments: int, resolution_override: Optional[Tuple[int, int]] = None
              ) -> Tuple[dict, dict]:
        """按目标段数重建工作流。

        n_segments: 目标段数（= 该集分镜数，每个分镜独立成片）
        resolution_override: 可选 (宽, 高)，覆盖模板 ResolutionSelector 的分辨率。
        """
        if n_segments < 1:
            raise ValueError(f"段数必须 >= 1，当前 {n_segments}")
        analysis = self.analyze()
        shared = analysis["shared"]
        seg_tpl = analysis["segment_nodes"]
        if "MiniMaxH3ReferenceToVideo" not in seg_tpl:
            raise RuntimeError("模板中未找到 MiniMaxH3ReferenceToVideo，无法重建 Ref2VA 工作流")

        resolution = (tuple(resolution_override) if resolution_override
                      else self._template_resolution())
        if resolution is None:
            logger.warning("模板未找到 ResolutionSelector，沿用模板默认分辨率")

        # ---- 保留共享节点（原 id 不变）----
        shared_ids = set(shared.values())
        new_nodes: List[dict] = [copy.deepcopy(self.nodes_by_id[i]) for i in sorted(shared_ids)]
        index: Dict[int, dict] = {n["id"]: n for n in new_nodes}
        new_links: List[list] = []
        # 保留共享节点之间的原始连线
        for lid, (o, os_, t, ts) in self.links_by_id.items():
            if o in shared_ids and t in shared_ids and o in index and t in index:
                outs = index[o].get("outputs") or []
                typ = outs[os_].get("type") if os_ < len(outs) else "*"
                new_links.append([lid, o, os_, t, ts, typ])
        for n in new_nodes:
            for i, o in enumerate(n.get("outputs") or []):
                o["links"] = [l[0] for l in new_links if l[1] == n["id"] and l[2] == i]
            for i, inp in enumerate(n.get("inputs") or []):
                inp["link"] = next((l[0] for l in new_links
                                    if l[3] == n["id"] and l[4] == i), None)

        # ---- 逐段复制 ----
        seg_layout: List[dict] = []
        base_x_step = 1200
        for i in range(n_segments):
            base_x = i * base_x_step
            base_y = 0
            shot_tag = f"shot_{i + 1:02d}"

            # 1) 起片节点 MiniMaxH3ReferenceToVideo
            ref2va = copy.deepcopy(seg_tpl["MiniMaxH3ReferenceToVideo"])
            ref2va["id"] = self._new_node_id()
            ref2va["pos"] = [base_x, base_y]
            # 清空所有输入连线（重新接）
            for inp in ref2va.get("inputs") or []:
                inp["link"] = None
            for o in ref2va.get("outputs") or []:
                o["links"] = []
            # 参考图槽位：确保 ref_image_0..8 都存在（autogrow 手动展开）
            self._ensure_ref_slots(ref2va)
            index[ref2va["id"]] = ref2va
            new_nodes.append(ref2va)

            # 2) 9 个参考图 LoadImage
            load_tpl = seg_tpl.get("LoadImage")
            if load_tpl is None:
                raise RuntimeError("模板中未找到 LoadImage 节点，无法挂参考图")
            load_ids: List[int] = []
            for slot_idx in range(9):
                ld = copy.deepcopy(load_tpl)
                ld["id"] = self._new_node_id()
                ld["title"] = f"{shot_tag}_ref{slot_idx}"
                ld["pos"] = [base_x - 320, base_y + 120 + slot_idx * 90]
                for inp in ld.get("inputs") or []:
                    inp["link"] = None
                for o in ld.get("outputs") or []:
                    o["links"] = []
                index[ld["id"]] = ld
                new_nodes.append(ld)
                load_ids.append(ld["id"])
                self._connect(new_links, index, self._new_link_id(),
                              ld["id"], ld["outputs"][0]["name"],
                              ref2va["id"], REF_SLOT_NAMES[slot_idx])

            # 3) prompt（PrimitiveStringMultiline）
            p_tpl = seg_tpl.get("PrimitiveStringMultiline")
            if p_tpl is None:
                raise RuntimeError("模板中未找到 PrimitiveStringMultiline（prompt 注入）")
            p_node = copy.deepcopy(p_tpl)
            p_node["id"] = self._new_node_id()
            p_node["pos"] = [base_x - 320, base_y + 900]
            for inp in p_node.get("inputs") or []:
                inp["link"] = None
            for o in p_node.get("outputs") or []:
                o["links"] = []
            index[p_node["id"]] = p_node
            new_nodes.append(p_node)
            self._connect(new_links, index, self._new_link_id(),
                          p_node["id"], p_node["outputs"][0]["name"],
                          ref2va["id"], "prompt")

            # 4) 时长：PrimitiveFloat(秒) → ComfyMathExpression(帧)
            f_tpl = seg_tpl.get("PrimitiveFloat")
            m_tpl = seg_tpl.get("ComfyMathExpression")
            if f_tpl is None or m_tpl is None:
                raise RuntimeError("模板中未找到 PrimitiveFloat/ComfyMathExpression（时长换算）")
            f_node = copy.deepcopy(f_tpl)
            f_node["id"] = self._new_node_id()
            f_node["pos"] = [base_x - 320, base_y + 1000]
            for inp in f_node.get("inputs") or []:
                inp["link"] = None
            for o in f_node.get("outputs") or []:
                o["links"] = []
            index[f_node["id"]] = f_node
            new_nodes.append(f_node)

            m_node = copy.deepcopy(m_tpl)
            m_node["id"] = self._new_node_id()
            m_node["pos"] = [base_x - 160, base_y + 1000]
            m_node["widgets_values"] = [LENGTH_EXPR]
            m_node["widgets_values_named"] = {"expression": LENGTH_EXPR}
            for inp in m_node.get("inputs") or []:
                inp["link"] = None
            for o in m_node.get("outputs") or []:
                o["links"] = []
            index[m_node["id"]] = m_node
            new_nodes.append(m_node)
            # PrimitiveFloat.FLOAT -> ComfyMathExpression.values.a
            self._connect(new_links, index, self._new_link_id(),
                          f_node["id"], "FLOAT", m_node["id"], "values.a")
            # ComfyMathExpression -> ref2va.length
            self._connect(new_links, index, self._new_link_id(),
                          m_node["id"], "INT", ref2va["id"], "length")

            # 5) 共享资源 -> ref2va（clip / vae / audio_vae）
            clip_id = shared.get("CLIPLoader")
            vae_ids = [v for k, v in shared.items() if k == "VAELoader" or k.startswith("VAELoader#")]
            if clip_id is None or len(vae_ids) < 2:
                raise RuntimeError("模板中未找到 CLIPLoader / 2 个 VAELoader")
            self._connect(new_links, index, self._new_link_id(),
                          clip_id, "CLIP", ref2va["id"], "clip")
            self._connect(new_links, index, self._new_link_id(),
                          vae_ids[0], "VAE", ref2va["id"], "vae")
            self._connect(new_links, index, self._new_link_id(),
                          vae_ids[1], "VAE", ref2va["id"], "audio_vae")

            # 6) width / height（widget 类型，直接写 widgets_values，不连线）
            #    但模板里 width/height 是连到 ResolutionSelector 的 link；重建后
            #    改为直接写死分辨率（去掉 link，写 widgets_values_named）。
            self._set_resolution_widgets(ref2va, resolution)

            # 7) 一采：RandomNoise + BasicGuider + SamplerCustomAdvanced
            noise1 = self._clone_seg_node(seg_tpl, "RandomNoise", base_x, base_y + 300,
                                          index, new_nodes)
            guider1 = self._clone_seg_node(seg_tpl, "BasicGuider", base_x + 200, base_y + 300,
                                           index, new_nodes)
            sampler1 = self._clone_seg_node(seg_tpl, "SamplerCustomAdvanced",
                                            base_x + 400, base_y + 300, index, new_nodes)

            # 8) 二采：DisableNoise + BasicGuider + SamplerCustomAdvanced
            disablenoise = self._clone_seg_node(seg_tpl, "DisableNoise", base_x, base_y + 500,
                                                index, new_nodes)
            guider2 = self._clone_seg_node(seg_tpl, "BasicGuider", base_x + 200, base_y + 500,
                                           index, new_nodes)
            sampler2 = self._clone_seg_node(seg_tpl, "SamplerCustomAdvanced",
                                            base_x + 400, base_y + 500, index, new_nodes)

            # 9) 解码 + 成片：VAEDecode + VAEDecodeAudio + CreateVideo + SaveVideo
            vdec = self._clone_seg_node(seg_tpl, "VAEDecode", base_x + 600, base_y + 300,
                                        index, new_nodes)
            adec = self._clone_seg_node(seg_tpl, "VAEDecodeAudio", base_x + 600, base_y + 450,
                                        index, new_nodes)
            create_video = self._clone_seg_node(seg_tpl, "CreateVideo", base_x + 800,
                                                base_y + 300, index, new_nodes)
            clean_gpu = self._clone_seg_node(seg_tpl, "easy cleanGpuUsed", base_x + 900,
                                             base_y + 300, index, new_nodes)
            save_video = self._clone_seg_node(seg_tpl, "SaveVideo", base_x + 1000,
                                              base_y + 300, index, new_nodes)
            # SaveVideo 独立文件名前缀
            self._set_filename_prefix(save_video, shot_tag)

            # ---- 连线：一采 ----
            # noise1.NOISE -> sampler1.noise
            self._connect(new_links, index, self._new_link_id(),
                          noise1["id"], "NOISE", sampler1["id"], "noise")
            # guider1.GUIDER -> sampler1.guider
            self._connect(new_links, index, self._new_link_id(),
                          guider1["id"], "GUIDER", sampler1["id"], "guider")
            # ref2va.LATENT -> sampler1.latent_image
            self._connect(new_links, index, self._new_link_id(),
                          ref2va["id"], "LATENT", sampler1["id"], "latent_image")
            # ref2va.positive -> guider1.conditioning
            self._connect(new_links, index, self._new_link_id(),
                          ref2va["id"], "positive", guider1["id"], "conditioning")

            # ---- 连线：二采 ----
            # disablenoise.NOISE -> sampler2.noise
            self._connect(new_links, index, self._new_link_id(),
                          disablenoise["id"], "NOISE", sampler2["id"], "noise")
            # guider2.GUIDER -> sampler2.guider
            self._connect(new_links, index, self._new_link_id(),
                          guider2["id"], "GUIDER", sampler2["id"], "guider")
            # sampler1.output -> sampler2.latent_image
            self._connect(new_links, index, self._new_link_id(),
                          sampler1["id"], "output", sampler2["id"], "latent_image")
            # ref2va.positive -> guider2.conditioning
            self._connect(new_links, index, self._new_link_id(),
                          ref2va["id"], "positive", guider2["id"], "conditioning")

            # ---- 连线：共享 sigmas / sampler ----
            # SplitSigmasDenoise.high_sigmas -> sampler1.sigmas
            split_id = shared.get("SplitSigmasDenoise")
            ksampler_id = shared.get("KSamplerSelect")
            if split_id is None or ksampler_id is None:
                raise RuntimeError("模板中未找到 SplitSigmasDenoise / KSamplerSelect")
            self._connect(new_links, index, self._new_link_id(),
                          split_id, "high_sigmas", sampler1["id"], "sigmas")
            self._connect(new_links, index, self._new_link_id(),
                          split_id, "low_sigmas", sampler2["id"], "sigmas")
            self._connect(new_links, index, self._new_link_id(),
                          ksampler_id, "SAMPLER", sampler1["id"], "sampler")
            self._connect(new_links, index, self._new_link_id(),
                          ksampler_id, "SAMPLER", sampler2["id"], "sampler")

            # ---- 连线：模型链（共享）-> 一采/二采 guider ----
            # 一采 guider 用一采模型链（LoraLoaderModelOnly 链末端），
            # 二采 guider 用二采模型链（独立）。
            self._connect_model_chain(shared, index, new_links, guider1["id"], first_pass=True)
            self._connect_model_chain(shared, index, new_links, guider2["id"], first_pass=False)

            # ---- 连线：解码 ----
            # sampler2.output -> vdec.samples + adec.samples
            self._connect(new_links, index, self._new_link_id(),
                          sampler2["id"], "output", vdec["id"], "samples")
            self._connect(new_links, index, self._new_link_id(),
                          sampler2["id"], "output", adec["id"], "samples")
            # vae -> vdec.vae / adec.vae
            self._connect(new_links, index, self._new_link_id(),
                          vae_ids[0], "VAE", vdec["id"], "vae")
            self._connect(new_links, index, self._new_link_id(),
                          vae_ids[1], "VAE", adec["id"], "vae")

            # ---- 连线：成片 ----
            # vdec.IMAGE -> create_video.images
            self._connect(new_links, index, self._new_link_id(),
                          vdec["id"], "IMAGE", create_video["id"], "images")
            # adec.AUDIO -> create_video.audio
            self._connect(new_links, index, self._new_link_id(),
                          adec["id"], "AUDIO", create_video["id"], "audio")
            # create_video.VIDEO -> clean_gpu.anything -> clean_gpu.output -> save_video.video
            self._connect(new_links, index, self._new_link_id(),
                          create_video["id"], "VIDEO", clean_gpu["id"], "anything")
            self._connect(new_links, index, self._new_link_id(),
                          clean_gpu["id"], "output", save_video["id"], "video")

            seg_layout.append({
                "index": i,
                "ref2va": ref2va["id"],
                "ref_nodes": load_ids,
                "prompt_node": p_node["id"],
                "duration_node": f_node["id"],
                "length_node": m_node["id"],
                "save_video_node": save_video["id"],
            })

        # ---- 组装 ----
        wf = copy.deepcopy(self.template)
        wf["nodes"] = new_nodes
        wf["links"] = new_links
        wf["last_node_id"] = self._cursor_node
        wf["last_link_id"] = self._cursor_link
        wf["groups"] = []

        layout = {
            "template": self.template_path,
            "segment_count": n_segments,
            "resolution": ({"width": resolution[0], "height": resolution[1]}
                           if resolution is not None else None),
            "segments": seg_layout,
            "ref_slots": REF_SLOT_NAMES,
            "node_total": len(new_nodes),
            "link_total": len(new_links),
        }
        logger.info(f"Ref2VA 工作流已按 {n_segments} 段重建：节点 {len(new_nodes)} 个，"
                    f"连线 {len(new_links)} 条")
        return wf, layout

    # ------------------------------------------------------------------ 辅助
    def _clone_seg_node(self, seg_tpl: Dict[str, dict], ttype: str,
                        x: int, y: int, index: Dict[int, dict],
                        new_nodes: List[dict]) -> dict:
        tpl = seg_tpl.get(ttype)
        if tpl is None:
            raise RuntimeError(f"模板中未找到段内节点 {ttype}")
        n = copy.deepcopy(tpl)
        n["id"] = self._new_node_id()
        n["pos"] = [x, y]
        for inp in n.get("inputs") or []:
            inp["link"] = None
        for o in n.get("outputs") or []:
            o["links"] = []
        index[n["id"]] = n
        new_nodes.append(n)
        return n

    def _ensure_ref_slots(self, ref2va: dict):
        """确保 ref2va 有 ref_image_0..8 共 9 个参考图输入槽（autogrow 手动展开）。"""
        inputs = ref2va.setdefault("inputs", [])
        # 已有槽位名
        existing = {inp.get("name") for inp in inputs}
        # 找到 ref_image_0 的模板（取第一个 ref_images.* 槽位做底）
        tmpl = next((inp for inp in inputs
                     if (inp.get("name") or "").startswith("ref_images.ref_image_")), None)
        if tmpl is None:
            raise RuntimeError("MiniMaxH3ReferenceToVideo 缺少 ref_images 槽位")
        for slot_name in REF_SLOT_NAMES:
            if slot_name in existing:
                continue
            new_slot = copy.deepcopy(tmpl)
            new_slot["name"] = slot_name
            new_slot["localized_name"] = slot_name.rsplit(".", 1)[-1]
            # label 只在 ref_image_0 上出现（模板里 1/2/3 都无 label），
            # 复制的槽位必须清掉，否则 ref_image_4..8 会误带 label: ref_image_0。
            new_slot.pop("label", None)
            new_slot["link"] = None
            inputs.append(new_slot)

    @staticmethod
    def _set_resolution_widgets(ref2va: dict, resolution: Optional[Tuple[int, int]]):
        if resolution is None:
            return
        w, h = resolution
        # width / height 是 widget 输入，直接写 widgets_values_named，清掉 link
        named = dict(ref2va.get("widgets_values_named") or {})
        named["width"] = w
        named["height"] = h
        ref2va["widgets_values_named"] = named
        # widgets_values 顺序：prompt, width, height, length, ref_image_size
        wv = ref2va.get("widgets_values") or []
        # 只更新 width/height 的位置（模板顺序 [prompt, width, height, length, ref_image_size]）
        for inp in ref2va.get("inputs") or []:
            nm = inp.get("name")
            if nm == "width":
                inp["link"] = None
                inp["widget"] = {"name": "width"}
            elif nm == "height":
                inp["link"] = None
                inp["widget"] = {"name": "height"}

    @staticmethod
    def _set_filename_prefix(save_video: dict, prefix: str):
        for inp in save_video.get("inputs") or []:
            if inp.get("name") == "filename_prefix":
                inp["link"] = None
        named = dict(save_video.get("widgets_values_named") or {})
        named["filename_prefix"] = prefix
        save_video["widgets_values_named"] = named
        wv = save_video.get("widgets_values") or []
        if wv:
            wv[0] = prefix
        save_video["widgets_values"] = wv

    def _connect_model_chain(self, shared: Dict[str, int], index: Dict[int, dict],
                             links: List[list], guider_id: int, first_pass: bool):
        """把共享模型链末端接到 guider.model。

        first_pass=True   → 一采模型链（加速LoRA → 动作连续LoRA → LoRA 链末端）
        first_pass=False  → 二采模型链（Turbo → Combat → 动作修复 链末端）
        """
        # 从模板原始连线里，反推每条 LoRA 链的末端（哪个 LoRA 的输出连到了 BasicGuider.model）
        # 模板里有两个 BasicGuider（一采 126 / 二采 157），它们的 model 分别来自
        # LoraLoaderModelOnly(151) 和 LoraLoaderModelOnly(156)。
        # 这里用「模板里 BasicGuider 的 model 连线」定位模型链末端节点。
        lora_ids = [v for k, v in shared.items() if k.startswith("LoraLoaderModelOnly#")]
        # 找到模板中两个 BasicGuider 的 model 来源
        # 通过模板 links_by_id：找 BasicGuider 节点，其 inputs['model'].link 指向的节点
        guider_models = self._template_guider_models()
        if not guider_models:
            raise RuntimeError("无法从模板定位 BasicGuider 的模型链")
        # guider_models 是按模板节点 id 排序的 [一采模型源, 二采模型源]
        src_model_tpl_id = guider_models[0] if first_pass else guider_models[1]
        # 找到该模板节点在新 workflow 中的对应共享节点（LoraLoaderModelOnly 是共享的，id 不变）
        src_id = src_model_tpl_id
        if src_id not in index:
            raise RuntimeError(f"模型链节点 {src_id} 未保留为共享资源")
        self._connect(links, index, self._new_link_id(),
                      src_id, "MODEL", guider_id, "model")

    def _template_guider_models(self) -> List[int]:
        """返回模板中两个 BasicGuider 的 model 输入来源节点 id（一采、二采）。"""
        result: List[int] = []
        for n in self.template.get("nodes") or []:
            if n.get("type") != "BasicGuider":
                continue
            for inp in n.get("inputs") or []:
                if inp.get("name") == "model" and inp.get("link") in self.links_by_id:
                    result.append(self.links_by_id[inp["link"]][0])
        return result

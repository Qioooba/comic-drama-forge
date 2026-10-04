# -*- coding: utf-8 -*-
"""全书结构化大纲（2026-10-02 用户指定的「唯一输入源」）。

流程（整本转剧本的强制前置）：
1. **全文通读 + 清洗**：复用 novel_to_script 的分块与逐块提炼（自带断点缓存），
   提炼阶段即完成去目录/废话/无关章节的正文范围锁定。
2. **抽角色**：全块角色按「出现次数」确定性排序，出现次数达标的标记为
   ``primary``（主角团），其余为 ``minor``（群演/龙套）。
3. **拆段落**：以「场景切换」为边界（换地点/换时间/换事件）拆段，
   段数即导演节点 batch 的段数。
4. **标关键情节**：每段标注剧情任务（推进冲突/交代背景/情绪戏）。
5. **落成结构化文档**：角色表 + 分段大纲 + 故事梗概 → 写
   ``output/continuity/<项目键>/book_outline.json``，
   后续所有环节只引用这一份，不重读小说。

设计约束：
- **幂等**：文档落盘后按内容指纹（分块数+总字数）判定是否复用；
  命中即直接读档，不重跑 LLM。
- **LLM 失败不阻断**：分段大纲的 LLM 调用失败时用确定性兜底
  （按分块切段 + 从各块提炼结果聚合），文档结构永远完整。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime

import novel_to_script
from config import CONTINUITY_DIR
from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

OUTLINE_VERSION = "book_outline_v1"

# 主角团判定：角色在提炼结果中出现的块数 ≥ 该值 → primary。
# 出现在 3 个以上块里的角色几乎必然是贯穿全书的主线人物；
# 出现次数相同的按「姓名总出现次数」二次排序（LLM 提炼已按戏份排序）。
PRIMARY_MIN_CHUNKS = 3

# 分段大纲 LLM 调用：全书按 MAX_SEG_BLOCKS 个分块滚动汇总，
# 单次 prompt 只带「上一段末尾 300 字 + 本窗 4 块摘要/要点」，控制响应体体量。
_MAX_SEG_BLOCKS = 4
_MAX_ROLL_TAIL_CHARS = 300


def outline_path(project_key: str, continuity_dir: str = None) -> str:
    """全书大纲落盘路径：output/continuity/<项目键>/book_outline.json"""
    root = os.path.join(continuity_dir or CONTINUITY_DIR,
                        project_key or "_default")
    return os.path.join(root, "book_outline.json")


def _content_fingerprint(text: str, chunk_count: int) -> str:
    """内容指纹：同一本书重跑时命中缓存/复用文档的依据（正文变更则失效）"""
    h = hashlib.sha1()
    h.update(str(chunk_count).encode("utf-8"))
    h.update(str(len(text)).encode("utf-8"))
    h.update(text[:800].encode("utf-8"))
    h.update(text[-800:].encode("utf-8"))
    return h.hexdigest()[:16]


def load_outline(project_key: str, text: str, chunk_count: int,
                 continuity_dir: str = None) -> dict | None:
    """读取已落盘大纲；指纹不一致（正文/分块变了）返回 None 触发重算"""
    path = outline_path(project_key, continuity_dir)
    if not os.path.isfile(path):
        return None
    try:
        data = read_json_strict(path, None)
    except Exception:  # noqa: BLE001 - 读档失败等同未生成，走重算
        data = None
    if not isinstance(data, dict) or not data.get("characters"):
        return None
    if data.get("fingerprint") and data.get("fingerprint") != _content_fingerprint(text, chunk_count):
        logger.info("book_outline 指纹不一致（正文或分块变化），重新生成：%s", path)
        return None
    return data


def aggregate_characters(outlines: list, style: str = "") -> list:
    """全块角色聚合 + 按戏份（出现次数）确定性排序。

    返回 characters[]，每条含：
    - name / gender / age / identity / appearance / personality / voice_style
      （取首次出现的字段，后续块的补充字段择优合并）
    - role: "primary"（主角团，出现块数 ≥ PRIMARY_MIN_CHUNKS）或 "minor"（群演/龙套）
    - chunk_count: 出现在多少个分块里（戏份主判据）
    - mention_count: 全块姓名被提炼提及的总次数（戏份次判据）

    ⚠️ 全量保留（2026-09-29 决策：资产不设数量上限）——不在此处截断，
    龙套由下游出图策略决定共用模板还是独立出图。
    """
    import style_kit
    merged: dict = {}
    order: list = []
    for o in outlines or []:
        if not isinstance(o, dict):
            continue
        chunk_idx = (o.get("_chunk") or {}).get("index")
        for c in (o.get("characters") or []):
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            if name not in merged:
                merged[name] = {
                    "name": name,
                    "gender": str(c.get("gender") or ""),
                    "age": str(c.get("age") or ""),
                    "identity": str(c.get("identity") or ""),
                    "appearance": str(c.get("appearance") or ""),
                    "personality": str(c.get("personality") or ""),
                    "voice_style": str(c.get("voice_style") or ""),
                    "chunk_count": 0,
                    "mention_count": 0,
                    "_order": len(order),
                }
                order.append(name)
            rec = merged[name]
            rec["mention_count"] += 1
            if chunk_idx is not None:
                rec["chunk_count"] += 1
            # 补空字段（择优：非空覆盖空）
            for f in ("gender", "age", "identity", "appearance", "personality", "voice_style"):
                if not rec[f] and c.get(f):
                    rec[f] = str(c.get(f))

    chars = [merged[n] for n in order]
    # 戏份排序：出现块数 desc → 姓名总提及 desc → 首次出现顺序（确定性稳定）
    chars.sort(key=lambda r: (-r["chunk_count"], -r["mention_count"], r["_order"]))
    for i, r in enumerate(chars):
        r["role"] = "primary" if r["chunk_count"] >= PRIMARY_MIN_CHUNKS else "minor"
        r["seq"] = i + 1
        # 清掉内部排序键
        r.pop("_order", None)
        # 给参考图提示词（中文）：与 build_bible 的登记口径同源——写明性别、三视图版式、
        # 只写画面可见特征，风格词由程序统一追加（下方 apply_asset_style_all）
        r["reference_prompt_zh"] = _char_ref_prompt_zh(r)
        r["reference_prompt_en"] = ""
    # 风格收尾：与整本路径同一口径，确定性补写、幂等
    if style:
        style_kit.apply_asset_style_all(chars, style_kit.normalize_style(style))
    return chars


def _char_ref_prompt_zh(rec: dict) -> str:
    """角色参考图中文提示词：性别开头 + 外貌定妆 + 三视图版式（禁风格/质量词）"""
    gender = rec.get("gender") or ""
    prefix = "女性角色，" if gender == "女" else ("男性角色，" if gender == "男" else "")
    parts = [p for p in [prefix, rec.get("appearance") or "", rec.get("outfit") or ""] if p]
    base = "，".join(parts).strip("，")
    return (base + "。正面、侧面、背面三张全身视图横排，从头到脚完整入画，同一角色身高比例一致") \
        if base else "三视图角色设定图，正面、侧面、背面横排全身"


# 剧情任务白名单（段级标注，导演节点据此排 batch 节奏）
SEGMENT_TASKS = ("推进冲突", "交代背景", "情绪戏")


def plan_segments(outlines: list, novel_title: str, client=None,
                  events: list = None, cache_dir: str = "") -> list:
    """按「场景切换」把全书拆成导演 batch 的段落，每段标剧情任务。

    优先用 LLM 一次汇总（滚动窗口，控制响应体）；失败则确定性兜底——
    直接按分块切段、用各块提炼里的场景名做段边界、用 key_beats 做任务标注，
    保证段落结构永远完整（段数即导演 batch 段数）。
    """
    if client is not None and getattr(client, "configured", True):
        try:
            segs = _plan_segments_llm(client, outlines, novel_title, events, cache_dir)
            if segs:
                return segs
        except Exception as e:  # noqa: BLE001
            logger.warning("分段大纲 LLM 失败，走确定性兜底：%s", e)
    return _plan_segments_fallback(outlines)


def _plan_segments_fallback(outlines: list) -> list:
    """确定性分段：一块一段（块本身已按场景/章节边界聚合），任务从 key_beats 关键词推断。"""
    segs = []
    for o in outlines or []:
        if not isinstance(o, dict):
            continue
        ch = o.get("_chunk") or {}
        scenes = [s.get("name") for s in (o.get("scenes") or [])
                  if isinstance(s, dict) and s.get("name")]
        beats = [b for b in (o.get("key_beats") or []) if b]
        task = _infer_task(beats, o.get("summary") or "")
        segs.append({
            "index": len(segs) + 1,
            "chunk": ch.get("index"),
            "scene": scenes[0] if scenes else (ch.get("title") or ""),
            "task": task,
            "summary": (o.get("summary") or "").strip(),
            "key_beats": beats[:6],
        })
    return segs


def _infer_task(beats: list, summary: str) -> str:
    """从剧情要点/摘要关键词确定性推断段的剧情任务（兜底口径，无 LLM）"""
    blob = " ".join(beats) + " " + (summary or "")
    if any(k in blob for k in ("冲突", "对峙", "决裂", "反转", "揭露", "对抗", "决死", "厮杀", "开战")):
        return "推进冲突"
    if any(k in blob for k in ("背景", "来历", "设定", "世界观", "起源", "铺垫", "交代")):
        return "交代背景"
    if any(k in blob for k in ("情绪", "泪", "感动", "悲", "怒", "离别", "重逢", "牺牲", "告白")):
        return "情绪戏"
    return "推进冲突"  # 默认推进（导演 batch 的主体）


def _plan_segments_llm(client, outlines: list, novel_title: str,
                       events: list, cache_dir: str) -> list:
    """LLM 分段：滚动窗口（每窗 _MAX_SEG_BLOCKS 块）生成段落，窗间带尾部衔接。"""
    import novel_to_script as n2s
    window_digest = []
    for i, o in enumerate(outlines or []):
        if not isinstance(o, dict):
            continue
        ch = o.get("_chunk") or {}
        window_digest.append({
            "chunk": ch.get("index"),
            "summary": (o.get("summary") or "")[:120],
            "scenes": [s.get("name") for s in (o.get("scenes") or [])
                       if isinstance(s, dict) and s.get("name")][:4],
            "beats": [b for b in (o.get("key_beats") or []) if b][:5],
        })
    if not window_digest:
        return []

    segments: list = []
    total = len(window_digest)
    rolls = [window_digest[i:i + _MAX_SEG_BLOCKS]
             for i in range(0, total, _MAX_SEG_BLOCKS)]
    for wi, window in enumerate(rolls):
        prev_tail = ""
        if segments:
            last = segments[-1]
            prev_tail = (last.get("summary") or "")[-_MAX_ROLL_TAIL_CHARS:]
        prompt = _SEG_PROMPT.format(
            novel_title=novel_title,
            window=json.dumps(window, ensure_ascii=False),
            prev_tail=prev_tail,
            valid_tasks="/".join(SEGMENT_TASKS),
        )
        label = f"segplan#{wi + 1}"
        hit = n2s._cache_get(cache_dir, "segplan", prompt, events, label)
        if hit is not None and isinstance(hit.get("segments"), list) and hit["segments"]:
            raw = hit["segments"]
        else:
            try:
                raw = n2s._as_dict(n2s._robust_json(
                    client, prompt, system=n2s.SYSTEM_BIBLE, temperature=0.3,
                    max_tokens=4000, events=events, label=label,
                    max_attempts=3, token_ladder=(4096, 8192, 16384)))
            except Exception as e:  # noqa: BLE001
                raise e
            raw = raw.get("segments") or []
            if isinstance(raw, list) and raw:
                n2s._cache_put(cache_dir, "segplan", prompt, {"segments": raw})
        for s in raw:
            if not isinstance(s, dict):
                continue
            task = str(s.get("task") or "").strip()
            if task not in SEGMENT_TASKS:
                task = _infer_task([str(s.get("summary") or "")], str(s.get("summary") or ""))
            segments.append({
                "index": len(segments) + 1,
                "chunk": int(s.get("chunk") or 0),
                "scene": str(s.get("scene") or "")[:60],
                "task": task,
                "summary": str(s.get("summary") or "").strip()[:200],
                "key_beats": [str(b) for b in (s.get("key_beats") or [])][:6],
            })
    if not segments:
        return []
    # 段号重排 + 兜底补全字段
    for i, s in enumerate(segments):
        s["index"] = i + 1
        s.setdefault("scene", "")
        s.setdefault("task", "推进冲突")
        s.setdefault("summary", "")
        s.setdefault("key_beats", [])
    return segments


_SEG_PROMPT = """【任务】以下是漫剧《{novel_title}》按场景推进的分段提纲（窗口内各块摘要/场景/要点）。请合并同类场景、以「场景切换」（换地点/换时间/换事件）为边界，输出导演 batch 的段落大纲。

【本窗分段提纲（JSON）】
{window}
【上一窗末尾摘要（承接用，可为空）】
{prev_tail}

【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释，结构：
{{
  "segments": [
    {{
      "chunk": 该段起始对应的 chunk 编号（沿用上方数字，不要自造）,
      "scene": "该段的主场景名（取自上方 scenes，40字内）",
      "task": "该段的剧情任务，只允许取值：{valid_tasks}（三选一）",
      "summary": "该段剧情任务说明（60字内，讲清这段在推进什么）",
      "key_beats": ["该段关键情节，2-4条，每条25字内"]
    }}
  ]
}}
【硬约束】段数由场景切换决定：同一场景的连续块合并为一段；场景/时间/事件任一变化即开新段。每段 task 必须三选一（推进冲突/交代背景/情绪戏）。不要逐句复述原文。"""


def aggregate_items_scenes(outlines: list) -> tuple:
    """全块物品 / 场景确定性聚合（全量保留，供导演 batch 与出图参考）"""
    items: dict = {}
    scenes: dict = {}
    for o in outlines or []:
        if not isinstance(o, dict):
            continue
        for it in (o.get("items") or []):
            if isinstance(it, dict) and it.get("name") and it["name"] not in items:
                items[it["name"]] = {
                    "name": it["name"], "category": str(it.get("category") or ""),
                    "appearance": str(it.get("appearance") or ""),
                    "owner": str(it.get("owner") or ""),
                    "importance": str(it.get("importance") or ""),
                }
        for sc in (o.get("scenes") or []):
            if isinstance(sc, dict) and sc.get("name") and sc["name"] not in scenes:
                scenes[sc["name"]] = {
                    "name": sc["name"], "location": str(sc.get("location") or ""),
                    "appearance": str(sc.get("appearance") or ""),
                }
    return list(items.values()), list(scenes.values())


def book_summary(outlines: list, max_sentences: int = 5) -> str:
    """全书梗概：取首、中、末块的摘要拼成 3-5 句（确定性，兜底不阻断）"""
    if not outlines:
        return ""
    n = len(outlines)
    picks = []
    idxs = [0] if n >= 1 else []
    if n >= 3:
        idxs += [n // 2, n - 1]
    elif n == 2:
        idxs += [1]
    seen = set()
    for i in idxs:
        s = (outlines[i].get("summary") or "").strip()
        if s and s not in seen:
            picks.append(s)
            seen.add(s)
    return " ".join(picks)[: max_sentences * 60]


def build_outline(client, novel_meta: dict, novel_text: str, style: str = "3D动漫渲染",
                  progress_cb=None, continuity_dir: str = None,
                  project_key: str = None, cache_dir: str = "") -> dict:
    """全书结构化大纲主流程（整本转剧本的强制前置），返回并落盘 book_outline dict。

    幂等：``load_outline`` 命中（指纹一致）直接读档返回，不重跑 LLM。
    """
    import novel_to_script as n2s
    from llm_client import LLMError

    def report(phase, current, total, message, percent):
        if progress_cb:
            try:
                progress_cb(phase, current, total, message, percent)
            except Exception as e:  # noqa: BLE001
                logger.warning("大纲进度回调异常：%s", e)

    novel_title = novel_meta.get("title") or novel_meta.get("name") or "未命名小说"
    chapters = novel_meta.get("chapters") or []
    chunks = n2s.build_chunks(novel_text, chapters)
    if not chunks:
        raise LLMError("小说正文为空，无法生成全书大纲")
    for i, c in enumerate(chunks):
        c.setdefault("index", i + 1)
        c.setdefault("total", len(chunks))
        c.setdefault("title", f"第{c.get('from_chapter') or i + 1}段")

    # 幂等复用
    if load_outline(project_key or "_default", novel_text, len(chunks),
                    continuity_dir) is not None:
        report("reused", 1, 1, "命中已生成的全书大纲，跳过重算", 100)
        return load_outline(project_key or "_default", novel_text, len(chunks),
                            continuity_dir)

    total_steps = len(chunks) + 2
    events = []
    # ① 全文通读 + 逐块提炼（自带断点缓存，完成去目录/废话/正文范围锁定）
    outlines = [None] * len(chunks)
    for i, chunk in enumerate(chunks):
        report("read", i + 1, total_steps,
               f"通读第 {chunk['index']}/{len(chunks)} 段（{chunk.get('title')}）…",
               int(3 + (i + 1) / total_steps * 55))
        try:
            outlines[i] = n2s.extract_chunk_outline(client, chunk, novel_title,
                                                    events=events, cache_dir=cache_dir)
        except LLMError as e:
            logger.warning("全书大纲：第 %s 段提炼失败（改用空大纲兜底）：%s",
                           chunk.get("index"), e)
            outlines[i] = {"summary": "", "characters": [], "items": [],
                           "scenes": [], "key_beats": [],
                           "_chunk": {"index": chunk.get("index"),
                                      "title": chunk.get("title")}}
    if not any(outlines):
        raise LLMError("全书大纲：所有分块提炼均失败，无法生成角色表与分段大纲")

    # ② 抽角色（按戏份排序）
    report("chars", len(chunks) + 1, total_steps, "按戏份抽取角色表…", 70)
    characters = aggregate_characters(outlines, style)

    # ③④ 拆段 + 标剧情任务（场景切换为边界）
    report("segments", len(chunks) + 2, total_steps, "按场景切换拆分导演 batch 段落…", 85)
    segments = plan_segments(outlines, novel_title, client=client,
                             events=events, cache_dir=cache_dir)
    if not segments:
        segments = _plan_segments_fallback(outlines)

    # 物品 / 场景聚合（供导演 batch 与出图参考，全量保留）
    items, scenes = aggregate_items_scenes(outlines)

    outline = {
        "version": OUTLINE_VERSION,
        "fingerprint": _content_fingerprint(novel_text, len(chunks)),
        "novel_id": novel_meta.get("novel_id"),
        "title": novel_title,
        "style": style,
        "story_summary": book_summary(outlines),
        "characters": characters,
        "primary_count": sum(1 for c in characters if c.get("role") == "primary"),
        "minor_count": sum(1 for c in characters if c.get("role") == "minor"),
        "items": items,
        "scenes": scenes,
        "segments": segments,
        "segment_count": len(segments),
        "chunk_count": len(chunks),
        "warnings": [f"第 {c.get('index')} 段提炼失败，已用空大纲兜底"
                     for c in chunks if not (outlines[c.get("index") - 1] or {}).get("summary")]
                    if any(not (outlines[i] or {}).get("summary") for i in range(len(chunks))) else [],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    report("save", total_steps, total_steps,
           f"全书大纲完成：{len(characters)} 角色（主 {outline['primary_count']}）/ "
           f"{len(segments)} 段 / {len(items)} 物品 / {len(scenes)} 场景", 100)
    save_outline(outline, project_key, continuity_dir)
    return outline


def save_outline(outline: dict, project_key: str, continuity_dir: str = None) -> str:
    """把大纲原子写入 output/continuity/<项目键>/book_outline.json，返回路径"""
    path = outline_path(project_key, continuity_dir)
    atomic_write_json(path, outline)
    logger.info("全书大纲已落盘：%s（%d 角色 / %d 段）",
                path, len(outline.get("characters") or []),
                len(outline.get("segments") or []))
    return path




# -*- coding: utf-8 -*-
"""逐段场景 LoRA 选择（规则表 + LLM 智能选择）

背景：为什么挂到 ``segment.loras``
-------------------------------
ComfyUI 插件 ``ComfyUI_MiniMaxH3_Director`` **原生支持「每段各自的 LoRA」**：

* 每段结构：``segments[i].loras = [{"name": "H3/H3_Combat_V2.safetensors",
  "strength": 1.0, "active": true}]``，上限 **8** 条。
* 插件 ``director/gen_timeline.py`` 的取值逻辑等价于::

      loras = global.loras if use_global else seg_data.loras

  而 ``use_global`` 只在 ``edit_mode == "global"`` 时为真。
* 本项目构建器写的是 ``editMode = "segment"``（``app/h3_director_builder.py``），
  因此 **``segments[i].loras`` 会被插件采纳**（按段生效）。
* 插件归一化口径（``director/segment_loras.py:normalize_lora_rows``，**此处不 import
  插件，它在仓库外**）：``name`` 取 ``name/lora/lora_name``；``strength`` 为 float，
  默认 ``1.0``，clamp 到 ``[-10, 10]``；``active`` 为 bool，默认 ``True``；``name``
  为空则跳过该行；最多 **8** 条。

职责
----
本模块提供两种 LoRA 选择策略：

1. **LLM 智能选择**（优先）：调用用户配置的 AI 模型分析分镜内容，智能判断应使用
   哪个风格 LoRA。需要 AI 模型已配置（``ai_config.json`` 的 ``text`` 模块）。
2. **规则表匹配**（兜底）：按分镜的场景文本，从内置规则表里选出对应的 LoRA 行。
   纯代码判断（内置关键词规则表），**无前端、无 LLM、无文件系统访问、无网络**，
   输入相同则输出恒定（确定性）。

调用方使用 ``select_loras_for_shot_smart(shot)`` 即可自动走「LLM 优先 + 规则兜底」。

设计取舍
--------
* 规则**有序**：命中顺序即输出顺序，先命中者优先；同名去重。
* 无命中返回 **空列表** ``[]``（不是 ``None``）—— 调用方无须做 ``None`` 判空，
  构建器只有在列表非空时才把 ``loras`` 写进 timeline。
* 上限 ``MAX_SEGMENT_LORAS = 8`` 与插件一致；规则表当前仅 3 条，上限是本模块与
  插件之间的**契约护栏**（未来扩表也不会越界）。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MAX_SEGMENT_LORAS = 8
"""段级 LoRA 条数上限，与插件 ``normalize_lora_rows`` 一致。"""

# 2026-10-04：风格 LoRA 强度**硬区间**（用户要求「最高 0.8、最低 0.6，由配置的 AI 决定」）。
# 本清单里所有 LoRA 都是风格/画风类（战斗/运动/写实/风格），其 strength 一律钳到
# [STYLE_LORA_MIN_STRENGTH, STYLE_LORA_MAX_STRENGTH]。具体值由 AI 决定（LLM 返回后
# 钳到该区间）；LLM 缺省或规则表兜底时取区间上限 0.8，避免默认 1.0 过冲。
STYLE_LORA_MIN_STRENGTH = 0.6
STYLE_LORA_MAX_STRENGTH = 0.8

def _style_strength(val) -> float:
    """把风格 LoRA 强度钳到 [0.6, 0.8]（AI 给 1.0 → 0.8，给 0.5 → 0.6，缺省 → 0.8）。"""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return STYLE_LORA_MAX_STRENGTH
    return max(STYLE_LORA_MIN_STRENGTH, min(STYLE_LORA_MAX_STRENGTH, f))

# 可用的 LoRA清单（供 LLM 选择）
# 每项：(lora_name, description_zh, category)
AVAILABLE_LORAS = [
    ("H3/H3_Combat_V2.safetensors", "战斗风格LoRA：强化打斗、兵器、冲突动作的视觉效果", "战斗"),
    ("H3/Motion_Repair.safetensors", "运动修复LoRA：优化位移、高速、腾跃动作的流畅度", "运动"),
    ("h3-realism-people-t2v-i2v-r2v.safetensors", "写实风格LoRA：真人实拍质感，适合现代/现实题材", "写实"),
]


# 已知风格 LoRA 的中文描述/分类（按相对名或文件名兜底；未登记则用文件名当描述）。
_KNOWN_LORA_DESC = {
    "H3/H3_Combat_V2.safetensors": "战斗风格LoRA：强化打斗、兵器、冲突动作的视觉效果",
    "H3/Motion_Repair.safetensors": "运动修复LoRA：优化位移、高速、腾跃动作的流畅度",
    "h3-realism-people-t2v-i2v-r2v.safetensors": "写实风格LoRA：真人实拍质感，适合现代/现实题材",
    "zi-base-gm_male_20.safetensors": "男性角色基础LoRA：国漫男性角色面部/发型统一（按文件名匹配，忽略所在子目录）",
}
_KNOWN_LORA_CAT = {
    "H3/H3_Combat_V2.safetensors": "战斗",
    "H3/Motion_Repair.safetensors": "运动",
    "h3-realism-people-t2v-i2v-r2v.safetensors": "写实",
}
# 结构性/加速类 LoRA 关键词：这些是模型架构/加速（非画风），不能交给 AI 当风格选。
# ⚠️ 2026-10-04 起 discover_style_loras 改为**白名单优先**（只收 H3 系），本黑名单
#    仍保留，用于把「H3 系里的加速/结构类」（turbo / lightx2v / fl2v / 4step …）
#    从风格候选里剔掉。
_EXCLUDE_LORA_KEYWORDS = (
    "turbo", "lightning", "lightx2v", "fl2v", "4step", "ref2v_turbo", "qwen",
    "light", "step",
)

# 显式豁免表：日后装了**不带 h3 字样**的 H3 专用 LoRA 时，把其相对名登记到这里，
# is_h3_family_lora 命中即判为 H3 系（否则会被白名单挡掉）。
EXTRA_H3_LORA_NAMES: tuple = ()


def is_h3_family_lora(name: str) -> bool:
    """是否 MiniMax H3 系 LoRA。判据：名字（含子目录路径）里含 "h3"（大小写不敏感）。

    这是「视频生成只能用 H3 系 LoRA、图片链路 LoRA 绝不能进视频生成」这道红线的
    **唯一公共判据**（两层白名单都用它：段级风格 LoRA 候选池 + 模型槽位下拉）。

    - ``name`` 允许是相对名（如 ``H3/H3_Combat_V2.safetensors``）或裸文件名；
    - 命中 ``EXTRA_H3_LORA_NAMES`` 豁免表也算真（应对不带 h3 字样的 H3 专用 LoRA）；
    - 非字符串 / 空串 → ``False``。
    """
    if not isinstance(name, str):
        return False
    s = name.strip()
    if not s:
        return False
    if s in EXTRA_H3_LORA_NAMES:
        return True
    return "h3" in s.lower()


# 风格 LoRA 目录候选（本项目用 ComfyUI 的 models/loras，按顺序探测第一个存在者）。
_STYLE_LORA_DIRS = tuple(dict.fromkeys([
    os.path.join(os.environ.get("COMFYUI_ROOT") or "", "ComfyUI", "ComfyUI", "models", "loras"),
    os.path.join(os.environ.get("COMFYUI_DIR") or "", "models", "loras"),
    r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\models\loras",
]))


def discover_style_loras(loras_dir=None):
    """扫描 ComfyUI models/loras 下的**MiniMax H3 系**风格 LoRA（排除结构/加速类）。

    返回 [{name, desc, category}]，name 为相对 loras 目录的路径（含子目录，
    如 H3/H3_Combat_V2.safetensors），与 H3 插件 normalize_lora_rows 的取值口径一致。

    ⭐ 白名单（2026-10-04 用户需求 3）：视频生成只能挂 MiniMax H3 系 LoRA，
    绝不能让**图片链路**的 LoRA（Qwen/Flux 等）进视频生成。故这里从「黑名单」
    改为「白名单 + 黑名单」双重过滤：
      1. 白名单：``is_h3_family_lora(rel)`` 为真（名字含 ``h3``，或命中豁免表）；
      2. 黑名单：不命中 ``_EXCLUDE_LORA_KEYWORDS``（剔除 H3 系里的 turbo/lightx2v/
         fl2v/4step 等加速/结构类 —— 它们不是风格 LoRA）。
    扫描为空/目录不存在时回退到内置 AVAILABLE_LORAS（三条全为 H3 系，无条件兜底）。
    """
    dirs = [d for d in ([loras_dir] if loras_dir else []) + list(_STYLE_LORA_DIRS) if d]
    seen = {}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _sub, files in os.walk(d):
            for fn in files:
                if not fn.lower().endswith(".safetensors"):
                    continue
                bn = os.path.splitext(fn)[0]
                # ⭐ 统一口径：ComfyUI LoraLoader 的 lora_name 是相对 models/loras/ 的
                #    子目录路径（如 H3/H3_Combat_V2.safetensors、minimax_h3/xxx.safetensors）。
                #    平铺在 models/loras/ 根的文件（如 h3-realism-people-...safetensors）
                #    相对名即自身文件名，与磁盘一致，保留。
                rel = os.path.relpath(os.path.join(root, fn), d).replace("\\", "/")
                _rel = rel if "/" in rel else fn
                # ⭐ 白名单第 1 层：只收 H3 系（图片链路 LoRA 一律挡在门外）
                if not is_h3_family_lora(_rel):
                    continue
                # ⭐ 白名单第 2 层：H3 系里的加速/结构类仍然不是风格 LoRA
                if any(k in bn.lower() for k in _EXCLUDE_LORA_KEYWORDS):
                    continue
                if _rel in seen:
                    continue
                seen[_rel] = {
                    "name": _rel,
                    "desc": _KNOWN_LORA_DESC.get(_rel) or _KNOWN_LORA_DESC.get(fn)
                    or f"风格LoRA：{bn}",
                    "category": _KNOWN_LORA_CAT.get(_rel) or _KNOWN_LORA_CAT.get(fn) or "风格",
                }
    # 兜底：保证内置三条风格 LoRA 永远在清单里（万一磁盘不在也能选）。
    for name, desc, cat in AVAILABLE_LORAS:
        if name not in seen:
            seen[name] = {"name": name, "desc": desc, "category": cat}
    return list(seen.values())


def available_loras():
    """当前可交给 AI 挑选的风格 LoRA 清单（优先扫描磁盘，失败回退内置表）。"""
    try:
        lst = discover_style_loras()
        if lst:
            return lst
    except Exception:  # noqa: BLE001
        pass
    return [{"name": n, "desc": d, "category": c} for n, d, c in AVAILABLE_LORAS]


# 有序规则表：
# 顺序 = 命中时的输出顺序；``keywords`` 命中任意一个即视为命中该规则。
# ⚠️ 关键词均为中文场景词，靠 ``子串包含`` 匹配（非分词），与剧本自然语言直连。
SCENE_LORA_RULES = (
    # ---- 战斗类：打斗 / 兵器 / 冲突动作 ----
    (
        (
            # 原始词（**一个不删**，删词＝行为回归）
            "战斗", "打斗", "交手", "厮杀", "激战", "对攻", "拔刀", "出剑",
            "挥刀", "格斗", "搏杀", "出招", "拼杀", "围攻", "突袭", "偷袭",
            "追击", "剑光",
            # 2026-10-03 补全（同类近义词；⚠️ 全部 ≥2 汉字 —— 靠子串包含匹配，
            # 单字（刺/打/冲/砍/劈/斩/杀…）会大量误命中，一律不用单字复合写法）
            "搏斗", "对打", "决斗", "交战", "鏖战", "混战", "缠斗", "肉搏",
            "血战", "群战", "砍杀", "劈砍", "斩击", "刺杀", "出拳", "挥拳",
            "拳脚", "兵刃", "刀光", "剑影", "暴起", "扑击", "撞开", "冲杀",
            "恶斗",
            # 2026-10-03 精修：删「斗法」（误命中「斗法比试厨艺」等非战斗语境）
        ),
        "H3/H3_Combat_V2.safetensors",
        1.0,
    ),
    # ---- 运动类：位移 / 高速 / 腾跃动作 ----
    (
        (
            # 原始词（**一个不删**）
            "奔跑", "疾驰", "追逐", "翻滚", "飞跃", "高速", "腾跃", "冲刺",
            "闪避", "跳跃", "坠落", "飞掠", "奔袭",
            # 2026-10-03 补全（同类近义词；均 ≥2 汉字，不用单字「奔/逃/冲」）
            "狂奔", "奔逃", "逃窜", "飞奔", "飞驰", "疾行", "疾走", "急奔",
            "逃跑", "逃命", "追捕", "追赶", "腾空", "跃起", "纵身", "翻越",
            "疾掠", "闪身", "疾奔", "奔走", "纵跃", "俯冲",
            "飞扑", "扑倒", "摔落",
            # 2026-10-03 精修：删「弹射 / 滑步」（误命中弹射座椅 / 街舞滑步），
            # 补缺口词（「竞争」为抽象语义、非物理位移，明确不加）
            "跑步", "逃走", "疾跑", "快跑",
            # 2026-10-03 二次精修：删「极速 / 飞速」（误命中「网络极速下载 /
            # 一键极速出图 / 经济飞速发展」等比喻语境；非物理位移）
        ),
        "H3/Motion_Repair.safetensors",
        1.0,
    ),
    # ---- 写实类：实拍质感 ----
    (
        (
            # 原始词（**一个不删**）
            "写实", "真人", "实拍",
            # 2026-10-03 补全（同类近义词；均 ≥2 汉字）
            "写实风格", "真实感", "真实质感", "真人实拍",
            # 2026-10-03 精修：删「纪实」（误命中「纪实报道」等新闻语境）
        ),
        "h3-realism-people-t2v-i2v-r2v.safetensors",
        1.0,
    ),
)

# 参与场景判断的 shot 文本字段（真实字段名取自 ``app/h3_prompt_kit.py`` 的
# ``shot.get(...)`` 调用）。非字符串 / 空串会被忽略。
_SHOT_TEXT_FIELDS = (
    "description", "action", "motion", "camera", "camera_motion",
    "visual_detail", "narration", "shot_type", "location", "emotion",
)

# dialogue 条目里可能承载台词的键名（按优先级取第一个非空者）。
_DIALOGUE_TEXT_KEYS = ("text", "line", "dialogue", "content")


def _iter_dialogue_texts(raw):
    """把任意形态的 ``dialogue`` 展平成字符串列表（保持出现顺序）。

    兼容三种真实形态：
      * ``str``：整体即一句台词；
      * ``dict``：从 ``text/line/dialogue/content`` 里取第一个非空字符串文案；
      * ``list/tuple``：递归展平（元素可再是 dict 或 str）。

    其它类型一律忽略。返回 ``list[str]``（不含空串）。
    """
    out = []
    if raw is None:
        return out
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            out.append(text)
        return out
    if isinstance(raw, dict):
        for key in _DIALOGUE_TEXT_KEYS:
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                out.append(val.strip())
                break
        return out
    if isinstance(raw, (list, tuple)):
        for item in raw:
            out.extend(_iter_dialogue_texts(item))
    return out


def _shot_text(shot):
    """把 shot 的相关文本字段拼成一个大字符串（空格连接，忽略空值/非字符串）。

    覆盖 ``_SHOT_TEXT_FIELDS`` 里的字段 + ``dialogue`` 展平后的所有台词。
    ``shot`` 非 dict 时返回空串。
    """
    if not isinstance(shot, dict):
        return ""
    parts = []
    for key in _SHOT_TEXT_FIELDS:
        val = shot.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    parts.extend(_iter_dialogue_texts(shot.get("dialogue")))
    return " ".join(parts)


def scene_lora_rows_for_text(text):
    """按场景文本选 LoRA：命中任一关键词即追加一行，同名去重，截断到上限。

    返回 ``list[dict]``，每条形如
    ``{"name": <lora_name>, "strength": <float>, "active": True}``。
    无命中 / ``text`` 非字符串或为空 → ``[]``。
    """
    if not isinstance(text, str) or not text:
        return []
    rows = []
    seen = set()
    for keywords, lora_name, strength in SCENE_LORA_RULES:
        if not any(kw in text for kw in keywords):
            continue
        if lora_name in seen:
            continue
        seen.add(lora_name)
        # 2026-10-04：风格 LoRA 强度钳到 [0.6,0.8]（规则表硬编码 1.0 → 0.8）
        rows.append({"name": lora_name, "strength": _style_strength(strength), "active": True})
        if len(rows) >= MAX_SEGMENT_LORAS:
            break
    return rows


def select_loras_for_shot(shot):
    """分镜 → 段级 LoRA 行列表（= ``scene_lora_rows_for_text(_shot_text(shot))``）。

    无命中返回 ``[]``（**不是** ``None``）。同一分镜切出的所有长镜子段应继承
    同一套 LoRA（调用方自行复用本函数结果）。
    
    这是纯规则表匹配版本（确定性，无 LLM 调用）。
    """
    return scene_lora_rows_for_text(_shot_text(shot))


def select_loras_for_shot_smart(shot, use_llm: bool = True):
    """分镜 → 段级 LoRA 行列表（LLM 智能选择 + 规则表兜底）。

    优先调用用户配置的 AI 模型分析分镜内容，智能判断应使用哪个风格 LoRA。
    若 AI 模型未配置 / 调用失败 / 返回空，则回落到规则表匹配（确定性）。

    参数：
        shot: 分镜字典（包含 description / action / visual_detail 等字段）
        use_llm: 是否尝试 LLM 选择（默认 True；设为 False 则直接走规则表）

    返回：``list[dict]``，每条形如
    ``{"name": <lora_name>, "strength": <float>, "active": True}``。
    无命中返回 ``[]``。
    """
    if not use_llm:
        return select_loras_for_shot(shot)

    # 尝试 LLM 智能选择
    try:
        loras = _select_loras_by_llm(shot)
        if loras:
            logger.info(f"[LoRA] LLM 智能选择命中 {len(loras)} 条：" +
                       ", ".join(f"{l['name']}({l.get('reason', '')})" for l in loras))
            return loras
    except Exception as e:
        logger.warning(f"[LoRA] LLM 选择失败，回落规则表匹配：{e}")

    # 兜底：规则表匹配
    loras = select_loras_for_shot(shot)
    if loras:
        logger.debug(f"[LoRA] 规则表命中 {len(loras)} 条：" +
                    ", ".join(l['name'] for l in loras))
        return loras

    # 兜底 2（2026-10-04，用户要求「由配置的 AI 模型判断风格 LoRA」+ 稳定风格锚点）：
    # LLM 与规则表都没命中具体线索时，给一条通用写实 LoRA，避免段级完全无风格 LoRA。
    # 仅当该 LoRA **真实存在**于本地候选清单时才加（不存在则维持空，不臆造）。
    try:
        _cat2 = {it["name"] for it in available_loras()}
    except Exception:  # noqa: BLE001
        _cat2 = set()
    for _cand in ("h3-realism-people-t2v-i2v-r2v.safetensors",):
        if _cand in _cat2:
            logger.info("[LoRA] LLM/规则表均未命中，回落通用写实 LoRA：%s", _cand)
            return [{"name": _cand, "strength": _style_strength(1.0), "active": True,
                     "reason": "LLM 与规则表均未命中具体线索，使用通用写实风格 LoRA 作为基础风格锚点"}]
    return loras


def _select_loras_by_llm(shot, catalog=None):
    """调用 AI 模型分析分镜内容，返回推荐的 LoRA 列表。

    内部实现：
    1. 构造提示词（包含分镜内容 + 可用 LoRA 清单）
    2. 调用 LLMClient.chat_json_robust 要求输出结构化 JSON
    3. 解析返回的 LoRA 选择并归一化

    返回：``list[dict]``（同 ``select_loras_for_shot_smart``）
    异常：任何异常均向上抛出，由调用方决定是否兜底
    """
    # 延迟导入避免循环依赖（ai_config / llm_client 可能间接依赖 app.py）
    try:
        import ai_config
        import llm_client
    except ImportError as e:
        raise RuntimeError(f"AI 模块导入失败：{e}")

    # 加载 AI 配置
    cfg = ai_config.load_config(
        os.path.join(os.path.dirname(__file__), "..", "ai_config.json"),
        os.path.join(os.path.dirname(__file__), "..", "llm_config.json")
    )
    ep = ai_config.get_module(cfg, "text")

    # 检查是否已配置
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        raise RuntimeError("AI 模型未配置（请在「AI 设置」中配置 text 模块）")

    # 构造客户端
    client = llm_client.LLMClient(
        config_path=os.path.join(os.path.dirname(__file__), "..", "llm_config.json"),
        config=ep,
        timeout=60  # LoRA 选择不需要太长超时
    )

    # 提取分镜关键信息
    shot_text = _shot_text(shot)
    if not shot_text:
        return []

    # 构造可用 LoRA 清单描述（AI 从用户实际安装的风格 LoRA 里挑，而非硬编码三条）
    _catalog = catalog if catalog else available_loras()
    if not _catalog:
        _catalog = [{"name": n, "desc": d, "category": c} for n, d, c in AVAILABLE_LORAS]
    lora_catalog = "\n".join(
        f"- **{item['name']}**（{item.get('category') or '风格'}）：{item.get('desc') or ''}"
        for item in _catalog
    )

    # 构造提示词
    system_prompt = (
        "你是视频生成 LoRA 选择专家。根据分镜内容，从可用清单中选出最适合的 LoRA。\n"
        "输出严格 JSON 格式，不要包含 markdown 代码块或任何解释文字。"
    )

    user_prompt = f"""## 分镜内容
{shot_text}

## 可用 LoRA 清单
{lora_catalog}

## 任务
分析上述分镜内容，判断它属于哪种场景类型（战斗 / 运动 / 写实 / 其他），
然后从可用清单中选出最匹配的 LoRA（可多选，但不超过 {MAX_SEGMENT_LORAS} 条）。

输出 JSON 格式：
{{
  "loras": [
    {{"name": "LoRA文件名", "strength": 0.7, "reason": "选择理由（简短）"}}
  ]
}}

strength（风格 LoRA 强度）取值范围 **0.6 ~ 0.8**（硬区间），请在该区间内给出你判断的具体值，默认 0.7。

如果分镜内容不需要任何特殊 LoRA（例如静态对话、普通场景），返回 {{"loras": []}}。
"""

    # 调用 LLM
    result = client.chat_json_robust(
        prompt=user_prompt,
        system=system_prompt,
        temperature=0.3,  # 低温度保证选择稳定性
        max_tokens=1024,
        max_attempts=2
    )

    # 解析返回
    loras_raw = result.get("loras") or []
    if not isinstance(loras_raw, list):
        return []

    # 归一化并校验
    out = []
    seen = set()
    for item in loras_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        # 校验 name 是否在可用清单中（catalog 优先，兜底内置表）
        valid_names = {item['name'] for item in (_catalog if _catalog else AVAILABLE_LORAS)}
        if name not in valid_names:
            logger.warning(f"[LoRA] LLM 返回的 {name!r} 不在可用清单中，跳过")
            continue
        seen.add(name)
        try:
            strength = float(item.get("strength", 1.0))
        except (TypeError, ValueError):
            strength = STYLE_LORA_MAX_STRENGTH
        # 2026-10-04：风格 LoRA 强度钳到 [0.6,0.8]（AI 在区间内决定具体值）
        strength = _style_strength(strength)
        reason = str(item.get("reason") or "").strip()
        out.append({"name": name, "strength": strength, "active": True, "reason": reason})
        if len(out) >= MAX_SEGMENT_LORAS:
            break

    return out

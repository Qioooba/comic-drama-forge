# -*- coding: utf-8 -*-
"""台词结构统一工具：结构化 {speaker, text} 台词 + 旧剧本字符串兼容。

背景：
- 分镜/剧本生成阶段直接把台词写成结构化对象 ``dialogue = {"speaker": 角色名, "text": 台词}``；
- 配音（tts_client）、字幕（video_postprocess）、H3 提示词（comfyui_client）、
  质检描述（app/script_prompt_analyzer）等下游统一调用本模块读取，
  不再各自对字符串做正则切分；
- 旧剧本（dialogue 为纯字符串，或 "角色名：台词" 形式）保持兼容：
  说话人按「前缀命中角色表 → 文本包含角色名 → 自称句式 → 出场角色首位」逐级回退推断。

本模块不依赖项目内其他模块，可被任意链路安全导入。
"""
import logging
import re
from typing import Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 自称句式标记（用于没有显式说话人时的名字片段匹配）
SELF_REF_MARKERS = (
    "吾乃", "吾是", "老夫", "在下", "本座", "余乃", "某乃", "本人",
    "我叫", "我是", "妾身", "小女子", "晚辈", "师兄", "师姐", "为师",
)

# "角色名：台词" 前缀（1~12 字，且不含标点/换行，避免误吃正文）
_PREFIX_RE = re.compile(r"^\s*([^：:，。！？、\n]{1,12})\s*[：:]\s*")


def _pick(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def iter_names(characters: Optional[Iterable]) -> List[str]:
    """从角色表（[{"name":...}] 或 ["林风"]）中取出全部非空角色名"""
    out: List[str] = []
    for ch in characters or []:
        name = ""
        if isinstance(ch, dict):
            name = _pick(ch.get("name"), ch.get("character"), ch.get("role"))
        elif isinstance(ch, str):
            name = ch.strip()
        if name and name not in out:
            out.append(name)
    return out


# ============================ 读 ============================

def dialogue_text(raw) -> str:
    """取台词纯文本（兼容 str / dict / list / None）"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return _pick(raw.get("text"), raw.get("line"), raw.get("dialogue"), raw.get("content"))
    if isinstance(raw, (list, tuple)):
        return " ".join(t for t in (dialogue_text(i) for i in raw) if t).strip()
    return str(raw).strip()


def dialogue_speaker(raw) -> str:
    """取显式说话人（兼容 str / dict / list / None），无则返回空串"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return ""
    if isinstance(raw, dict):
        return _pick(raw.get("speaker"), raw.get("character"), raw.get("role"), raw.get("name"))
    if isinstance(raw, (list, tuple)):
        for item in raw:
            sp = dialogue_speaker(item)
            if sp:
                return sp
        return ""
    return ""


def has_dialogue(raw) -> bool:
    """是否存在可朗读台词"""
    return bool(dialogue_text(raw))


# ============================ 推断（兼容旧剧本） ============================

def match_prefix_speaker(text: str, characters: Optional[Iterable]) -> str:
    """从 "角色名：台词" 前缀识别说话人（必须与角色表命中，否则视为正文冒号）"""
    m = _PREFIX_RE.match(str(text or ""))
    if not m:
        return ""
    cand = m.group(1).strip()
    for name in iter_names(characters):
        if cand == name or cand in name or name in cand:
            return name
    return ""


def infer_speaker(text: str, characters: Optional[Iterable],
                  cast: Optional[Iterable] = None) -> str:
    """无显式说话人时按文本推断角色名（全名命中 > 名字片段+自称句式 > 出场角色首位）"""
    text = str(text or "")
    if not text:
        return _pick(*(iter_names(cast)[:1]))
    best, best_score, best_len = "", 0, 0
    for name in iter_names(characters):
        score = 0
        if name in text:
            score = 3
        else:
            tokens = [t for t in name.replace("·", " ").replace("・", " ").split() if t]
            for t in tokens:
                if len(t) >= 2 and t in text:
                    score = max(score, 2)
                elif len(t) == 1 and t in text and any(mk in text for mk in SELF_REF_MARKERS):
                    score = max(score, 2)
        if score > best_score or (score == best_score and score and len(name) > best_len):
            best, best_score, best_len = name, score, len(name)
    return best or _pick(*(iter_names(cast)[:1]))


# ============================ 写 / 规范 ============================

def normalize_dialogue(raw, characters: Optional[Iterable] = None,
                       cast: Optional[Iterable] = None, max_text: int = 200,
                       infer_when_empty: bool = True) -> dict:
    """把任意写法的台词规范为 ``{"speaker": str, "text": str}``（空台词返回双空串）。

    - 已是结构化的：保留 speaker / text；
    - 旧字符串且带 "角色名：" 前缀：解析出说话人；
    - 完全无线索：按角色表推断（推断不出则取本镜头出场角色首位）。

    ``infer_when_empty=False`` 时**只做前缀解析、不做角色表推断**，
    推断不出就保持 speaker 为空。给「说话人宁缺毋滥」的调用方用 —— 见
    :func:`extract_quoted_dialogue`：兜底路径的 speaker 空是有意义的信号
    （「宁可退回默认音色，也不要安错人」），若这里再被角色表填成首位，
    就等于把「主角替全场说话」的错归因重新灌回来。
    """
    text = re.sub(r"\s+", " ", dialogue_text(raw)).strip()
    if not text:
        return {"speaker": "", "text": ""}
    speaker = dialogue_speaker(raw)
    if not speaker:
        speaker = match_prefix_speaker(text, characters)
    if not speaker and infer_when_empty:
        speaker = infer_speaker(text, characters, cast)
    return {"speaker": speaker[:40], "text": text[:max_text]}


def normalize_lines(raw, characters: Optional[Iterable] = None,
                    cast: Optional[Iterable] = None, max_text: int = 200,
                    infer_when_empty: bool = True) -> List[dict]:
    """把任意写法的台词规范为 ``[{"speaker": str, "text": str}, ...]``（无台词返回空列表）。

    - 已结构化（dict / list[dict]）：逐条保留 speaker / text；
    - 旧字符串：按 "角色名：" 前缀 → 角色表推断 逐级补全说话人；
    - 空台词条目直接丢弃。

    ``infer_when_empty`` 的语义见 :func:`normalize_dialogue`。
    """
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: List[dict] = []
    for it in items:
        line = normalize_dialogue(it, characters, cast, max_text=max_text,
                                  infer_when_empty=infer_when_empty)
        if line["text"]:
            out.append(line)
    return out


# ===================== 兜底路径：从原文引语恢复台词 =====================

# 中文小说里台词的引号形态。含直角引号与单引号：老式/港台文本常用「」『』，
# 嵌套引语还会再套一层（外层「」+ 内层‘’）——先外后内拆，避免整段当台词。
_QUOTE_PAIRS = (("“", "”"), ("「", "」"), ("『", "』"), ("‘", "’"))

#: 公开的开/闭引号字符集。**引号感知的切句器需要它**（见 novel_to_script._split_sentences）：
#: 朴素按句末标点切会把引语自带的句号当断点，把一句话从引号中间劈开。
#: 单一来源，禁止别处另写一份（口径漂移的老教训）。
QUOTE_OPEN_CHARS = "".join(p[0] for p in _QUOTE_PAIRS)
QUOTE_CLOSE_CHARS = "".join(p[1] for p in _QUOTE_PAIRS)

# 角色名之后、紧邻引语的那段话里，是否出现了说话动词 —— 说话人归因的**唯一**判据。
# 命中才认为「这个名字就是这句台词的说话人」，否则 speaker 留空（宁缺毋滥）。
_SPEECH_TAIL_RE = re.compile(
    r"^[\s，,。.：:；;、\-\u2014]*(?:"
    r"(?:又|才|再|便|就|只|先|随即|接着|继续|低声|大声|冷冷|淡淡|抬头|回头|闻言)?"
    r"[\u4e00-\u9fff]{0,4}"
    r"(?:说|道|问|答|喊|叫|吼|喝|笑|叹|嘀咕|低语|开口)"
    r"|[\u4e00-\u9fff]{0,2}(?:说着|问道|答道|喊道|叫道|笑道|叹道|回道|应道)"
    r")")

# 单条兜底台词的长度上限（与本模块 normalize_dialogue 的 max_text 默认值同口径）
QUOTED_MAX_CHARS = 200


def _strip_attrib(text: str) -> str:
    """只去首尾标点，**不剥动词**。

    ⚠️ 曾经在这里按「说/道/问…」剥掉引语开头的叙述成分，代价是两重的：
    ① 台词**内容**里的动词被误吃 —— “你说什么？” 里的「说」被当成叙述前缀，
       整句归零直接消失（实测《测灵根》漏 1 条）；
    ② 违反产品规则「原文对话尽量原样写进 dialogue.text」（novel_to_script 重写规则 5）：
       兜底路径既然要救台词，就更不能改写它。
    引号内的内容**就是台词原文**，一律原样保留。
    """
    return str(text or "").strip().strip("，,。.：:；;、 \n\t")


def _iter_quoted_spans(s: str, start: int = 0):
    """从 ``start`` 起扫描，逐个产出 ``(引语起点, 引语文本)``。

    一次线性扫描，不做回溯：遇到左引号就找其配对右引号；**找不到配对就整体跳过**
    （原文漏写/错配右引号是常态，猜配对只会把半句话当台词念出来）。
    嵌套引号（外层「」套内层‘’）递归取**最内层**，外层仅作上下文。
    """
    i, n = start, len(s)
    while i < n:
        op = cl = None
        for o, c in _QUOTE_PAIRS:
            if s.startswith(o, i):
                op, cl = o, c
                break
        if op is None:
            i += 1
            continue
        end = s.find(cl, i + 1)
        if end == -1:
            i += 1
            continue
        inner = s[i + 1:end]
        if any(o in inner for o, _ in _QUOTE_PAIRS):
            yield from _iter_quoted_spans(inner, 0)          # 递归进内层
        else:
            yield i, inner
        i = end + 1


#: 归因时向后回看的最大字符数（够容纳「角色名 + 副词 + 动词 + 标点」）
_ATTRIB_LOOKBACK = 24


def _attrib_speaker(before: str, ordered: List[str]) -> str:
    """从引语**紧前方**的文字里判定说话人；判不出就返回空串。

    两条硬约束（缺任一条都会把台词安到错的人头上）：

    1. 角色名之后必须紧跟说话动词，且动词与引号之间只有标点/空白
       —— 判据是 :data:`_SPEECH_TAIL_RE`。
    2. **角色名与本句引号之间不得夹着任何其他引号**。
       没有这条时，``…“杂灵根能修仙吗，”周野问，“能，”执事说，“资质是差了些，”``
       会把「资质是差了些」判给周野（回看窗口里 周野 + 问 恰好成立），
       而真正紧邻的说话人是「执事说」。实测《测灵根》ep01 shot16。

    判不出宁可留空：留空只是退回默认音色，猜错会让主角替全场说话。
    """
    tail = str(before or "")[-_ATTRIB_LOOKBACK:]
    if not tail:
        return ""
    best, best_pos = "", -1
    for nm in ordered:
        pos = tail.rfind(nm)
        if pos > best_pos:
            best, best_pos = nm, pos
    if not best or best_pos < 0:
        return ""
    rest = tail[best_pos + len(best):]
    if any(ch in rest for ch in QUOTE_OPEN_CHARS + QUOTE_CLOSE_CHARS):
        return ""                      # 中间隔着别的引语 → 这个人不是本句说话人
    return best if _SPEECH_TAIL_RE.match(rest) else ""


def extract_quoted_dialogue(text: str, characters: Optional[Iterable] = None,
                            cast: Optional[Iterable] = None,
                            max_text: int = QUOTED_MAX_CHARS,
                            min_text: int = 2) -> List[dict]:
    """从原文叙述里抽出引号中的台词，转成 ``[{"speaker","text"}]``（无则空列表）。

    **为什么需要**：LLM 分镜失败时 novel_to_script 会走「按原文逐句兜底」，
    兜底把整段原文塞进 description、``dialogue`` 写死 ``[]``。而本系统的
    ``dialogue`` 是**唯一人声来源**（旁白 narration 已于 2026-09-19 关闭），
    台词清零 = 整集无声，用户只看到「成片没人声」却无从判断是哪一环坏了。

    原文的引号本身就是台词的权威标记 —— 兜底路径完全有能力把它救回来，
    不该因为模型失败就把已存在于原文的台词一并丢掉。

    说话人归因**只用一级判据**：引语紧邻的「角色名 + 说话动词」
    （"周野道…" / "执事抬眼，道…"）。其余一律留空。

    ⚠️ 为什么只留一级：实测《测灵根》兜底剧本 30 条引语里，真说话人多为
    执事弟子 / 队伍众人，但原文只写「执事」「前面一个少年」，从不写全名。
    早期实现加了「前文最近出现的角色名」与「出场角色首位」两级兜底，
    结果 **30 条全部被安到主角周野头上** —— 主角替全场说话，比留空糟糕得多
    （TTS 用错音色、逐镜 OOC）。所以宁可大面积留空：speaker 空时下游退回默认音色，
    台词文本照念，「丢台词」这件事不会发生。
    配套：:func:`normalize_lines` 的 ``infer_when_empty=False``，防止归一化时
    把留空重新填成角色表首位。
    """
    s = str(text or "")
    if not s or not any(op in s for op, _ in _QUOTE_PAIRS):
        return []
    names = iter_names(characters) or iter_names(cast)
    # 长名优先，避免「周小」抢走「周野」的归因
    ordered = sorted(names, key=len, reverse=True)
    out: List[dict] = []

    def _attrib(before: str) -> str:
        # 唯一一级判据：引语正前方最近的「角色名 + 说话动词」，且中间不得夹别的引语。
        return _attrib_speaker(before, ordered)

    for pos, inner in _iter_quoted_spans(s):
        txt = _strip_attrib(inner)
        if len(re.sub(r"\s+", "", txt)) < min_text:
            continue                       # 「嗯」「……」这类纯语气，按无台词处理
        if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", txt):
            continue                       # 纯标点
        out.append({"speaker": _attrib(s[:pos]), "text": txt[:max_text]})
    return out


#: 剥离台词后残留的「说话引导」尾巴：`周野道` / `执事抬眼，道` 这类。
#: 引号被摘走后它们会变成悬空的「周野道：，」，必须一并清掉。
_ATTRIB_TAIL_RE = re.compile(
    r"(?:[一-鿿A-Za-z0-9]{1,8}(?:抬眼|皱眉|点头|摇头|开口|低声|沉声|冷冷|轻声)?"
    r"(?:说|道|问|答|喊|叫|回|念|想)[：:，,、]?)\s*$"
)


def split_quoted_dialogue(text: str, characters: Optional[Iterable] = None,
                          cast: Optional[Iterable] = None,
                          max_text: int = QUOTED_MAX_CHARS,
                          min_text: int = 2) -> Tuple[List[dict], str]:
    """从文本里**摘出**引号台词，并返回剔除这些引号后的剩余文本。

    与 :func:`extract_quoted_dialogue` 的区别：后者只读不改，本函数是
    「取出 + 就地删除」，供把台词从画面描述里**归位**到 dialogue 字段使用。

    为什么要归位
    ----------
    实测《测灵根》第 1 集：模型把 5 条原文金句原样写进了 ``description``
    （"我爹说，刚放上去的时候石头会凉…"，"四行俱有，俱不纯。…"），
    ``dialogue`` 却全是 ``[]``。而本系统的 ``dialogue`` 是**唯一人声来源**
    （旁白 narration 已关闭），于是成片整集无声。

    更糟的是**校验器也骗人**：``continuity.check_quotes_in_script`` 搜的是整个
    ``shots`` JSON（含 description），金句躺在描述里照样判 ``hit_rate=1.0``。
    三道防线（提示词强制 / 校验 / 质检对白驱动率）在同一处集体失效。

    归位后 description 只剩画面内容，符合项目既有规则
    「禁止把台词塞进 description / visual_detail / audio_cues」。

    返回 ``(lines, cleaned_text)``；无引号时 ``lines=[]`` 且 ``cleaned_text``
    为原文本（不做任何无谓改写）。
    """
    s = str(text or "")
    if not s or not any(op in s for op, _ in _QUOTE_PAIRS):
        return [], s

    names = iter_names(characters) or iter_names(cast)
    ordered = sorted(names, key=len, reverse=True)

    def _attrib(before: str) -> str:
        # 与 extract_quoted_dialogue 同一判据（共享实现，防口径漂移）
        return _attrib_speaker(before, ordered)

    lines: List[dict] = []
    # 倒序删除：正序删会让后续 span 的下标全部失效
    for pos, inner in reversed(list(_iter_quoted_spans(s))):
        txt = _strip_attrib(inner)
        keep = (len(re.sub(r"\s+", "", txt)) >= min_text
                and re.search(r"[一-鿿A-Za-z0-9]", txt))
        if not keep:
            continue                      # 「嗯」「……」按无台词处理，**留在描述里**
        # 定位配对右引号，把「引导 + 引号整段」一起摘掉
        for o, c in _QUOTE_PAIRS:
            if s.startswith(o, pos):
                end = s.find(c, pos + 1)
                break
        else:                              # pragma: no cover - 与 _iter_quoted_spans 同源
            continue
        cut = s[pos:end + 1]
        head = s[:pos]
        # ⚠️ 说话人必须用**剥离引导语之前**的 head 判定。
        #    先剥再判会让 `_attrib` 越过被剥掉的「执事说，」往前找，
        #    命中更靠前的「周野问」→ 把执事的台词安到主角头上（实测 ep01 shot16）。
        speaker = _attrib(head)
        m = _ATTRIB_TAIL_RE.search(head)
        if m:                             # 连同紧邻的「周野道：」一起走
            head = head[:m.start()]
        s = head + s[end + 1:]
        lines.append({"speaker": speaker, "text": txt[:max_text]})

    lines.reverse()                       # 恢复原文顺序
    # 摘完引号可能留下「，，」/「：」/悬空空白，收口但不重写内容
    cleaned = re.sub(r"[，,、]{2,}", "，", s)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"^[，,、：:；;。\s]+", "", cleaned)
    cleaned = re.sub(r"[，,：:；;\s]+$", "", cleaned)
    return lines, cleaned


def format_line(raw, sep: str = "：", with_speaker: bool = True) -> str:
    """把台词格式化为单行文本（字幕 / 提示词用）：有说话人则 "角色：台词"，否则纯台词"""
    text = dialogue_text(raw)
    if not text:
        return ""
    speaker = dialogue_speaker(raw) if with_speaker else ""
    return f"{speaker}{sep}{text}" if speaker else text


# ===================== 剧本「可配音 / 可出片」体检 =====================


def _shot_label(shot: dict, idx: int) -> str:
    sid = shot.get("shot_id")
    seq = shot.get("seq") or shot.get("shot_no")
    return f"#{seq if seq else (sid if sid is not None else idx + 1)}"


def audit_script(script: Optional[dict]) -> dict:
    """体检剧本在进入「配音 / 出片」前是否存在内容缺口。

    为什么需要它：LLM 失败时 novel_to_script 会按原文兜底生成镜头
    （``fallback=True``），这类镜头是 **dialogue=[] 且 prompt_h3=""** ——
    原文照搬、没有任何台词。脚本生成阶段看起来「成功了」（镜头数正常），
    但到配音环节就会一句都合不出来，或者合出整集无声；用户完全不知道
    为什么「配音生成成功却没有声音」。

    这里把这类缺口显式暴露出来，返回：
      - silent_shots：台词、旁白、音效提示**三者全空**（该镜成片既无人声也无音效）
      - no_voice_shots：无台词但写了音效提示的镜头（**正常留白**，不算缺陷）
      - narration_line_count：无台词但残留旁白的镜头数（**旧「旁白时代」剧本的遗留**；
        本系统自 2026-09-19 起剧本阶段不再产出旁白，见 novel_to_script.REWRITE_RULES 第 8 条）
      - fallback_shots：兜底生成的镜头（内容照搬原文，需人工润色）
      - blank_visual_shots：description 与 prompt_h3 均为空（出片没有画面提示）
      - unknown_speaker_shots：出场角色不在角色表里（音色会退回「旁白」）
      - overlong_speech_shots：台词量超出单镜时长上限（配音会溢出到后面几镜）
      - warnings：可直接展示给用户的中文提示

    除了一条 ``narration`` 废弃告警日志外无副作用：本函数是纯逻辑（不依赖项目内其它模块、
    不读写文件、不改动入参），唯一的外部可观测行为是——当统计到旧剧本残留 ``narration`` 时
    打**一条** ``logger.warning``（一次性，不是每镜一条）。之所以允许这条极小副作用：消费方
    （app.py 等）只把返回值展示给用户，日志里本来完全看不到痕迹，而「旁白通道已关闭」这件事
    需要在服务端日志里可诊断；取舍是「极小副作用」换「遗留字段可见」，其余仍保持纯函数语义。

    调用方契约**不变**：返回值结构（ok / stats / warnings / problem_shots）、计数语义、
    warnings 文案与加日志前逐项一致 —— 新增的只是日志，不改任何返回值。
    """
    script = script if isinstance(script, dict) else {}
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    # 角色表名集合（用于判断说话人/出场角色是否可识别）
    known = {n for n in iter_names(script.get("characters") or [])}

    silent, fallback, blank_visual, unknown_speaker = [], [], [], []
    no_voice, overlong, legacy_narration = [], [], []
    speakable_lines = 0        # 有台词的镜头数
    narration_lines = 0        # 无台词但残留旁白的镜头数（旧剧本遗留）
    for i, s in enumerate(shots):
        label = _shot_label(s, i)
        if s.get("fallback"):
            fallback.append(label)
        dlg_text = dialogue_text(s.get("dialogue"))
        if not dlg_text and str(s.get("dialogue_text") or "").strip():
            dlg_text = str(s.get("dialogue_text")).strip()
        narration = str(s.get("narration") or "").strip()
        cues = str(s.get("audio_cues") or "").strip()
        if dlg_text:
            speakable_lines += 1
        elif narration:
            narration_lines += 1
            legacy_narration.append(label)
        elif cues:
            # 无台词但交代了音效/配乐 → 正常留白（成片由音效铺底），不是缺陷
            no_voice.append(label)
        else:
            silent.append(label)
        # 单镜台词量超出时长上限：配音会沿时间轴溢出到后面几镜，尾部被成片截掉。
        # 该字段由 novel_to_script._norm_shots 在标准化时写入（生成期对账），不是此处凭空计算。
        try:
            overflow = float(s.get("duration_overflow_sec") or 0)
        except (TypeError, ValueError):
            overflow = 0.0
        if overflow > 0:
            overlong.append(f"{label}（超出 {overflow:g}s）")
        desc = str(s.get("description") or "").strip()
        if not desc and not str(s.get("prompt_h3") or "").strip():
            blank_visual.append(label)
        cast = s.get("characters_in_shot") or s.get("characters") or []
        names = []
        for c in cast:
            nm = c.get("name") if isinstance(c, dict) else str(c or "")
            if str(nm or "").strip():
                names.append(str(nm).strip())
        if names and known and any(n not in known for n in names):
            unknown_speaker.append(label)

    warnings: List[str] = []
    # 整集兜底＝剧本实际上没被模型加工过（实测 E2E 项目「第1集」6/6 都是兜底），
    # 这种情况必须升级提示，否则用户会以为「剧本生成成功了」。
    all_fallback = bool(shots) and len(fallback) == len(shots)
    if all_fallback:
        warnings.append(
            f"该集全部 {len(shots)} 个镜头都是「模型失败后按原文兜底」生成的，"
            f"剧本实际上未经模型加工（无分镜设计、无台词）。建议重新生成剧本，"
            f"或人工分镜后再进入配音出片。")
    elif fallback:
        warnings.append(
            f"有 {len(fallback)} 个镜头是「模型失败后按原文兜底」生成的（{', '.join(fallback[:8])}"
            f"{' 等' if len(fallback) > 8 else ''}）：内容照搬原文、没有台词，"
            f"建议人工润色后再配音出片。")
    if silent:
        warnings.append(
            f"有 {len(silent)} 个镜头没有台词，也没写音效提示（{', '.join(silent[:8])}"
            f"{' 等' if len(silent) > 8 else ''}）：成片到该镜头既无人声也无音效，"
            f"请补写 audio_cues 音效提示，或把原文的心理活动改写成该角色的自语台词。")
    if no_voice:
        warnings.append(
            f"有 {len(no_voice)} 个镜头是无台词的纯画面镜（{', '.join(no_voice[:8])}"
            f"{' 等' if len(no_voice) > 8 else ''}）：已写明音效提示，成片由音效与配乐铺底，属正常留白。")
    if narration_lines:
        warnings.append(
            f"有 {narration_lines} 个镜头残留了「旁白」文本（{', '.join(legacy_narration[:8])}"
            f"{' 等' if narration_lines > 8 else ''}）：本系统已不再产出旁白，"
            f"这批剧本是改造前生成的，成片会带画外音解说、且旁白时长常远超画面。建议重新生成剧本。")
        # D-11b：遗留 narration 的**一次性**服务端留痕（只在此处打一条，不按镜循环刷屏）。
        # 返回值已能携带 warnings，但消费者只展示给前端；服务端日志此前毫无痕迹，
        # 导致「仍产出 narration 的项目」在日志里不可见。用惰性 %s（D-09 统一日志风格）。
        logger.warning(
            "检测到旧剧本残留「旁白」字段（%d 个镜头：%s%s）：该字段为 2026-09-19 关闭旁白"
            "通道之前的遗留产物，本系统剧本阶段已不再产出旁白、配音链路不会朗读、成片也不会"
            "带画外音；建议重跑剧本生成以清掉这批 narration。",
            narration_lines, ", ".join(legacy_narration[:8]),
            " 等" if narration_lines > 8 else "")
    if overlong:
        warnings.append(
            f"有 {len(overlong)} 个镜头的台词量超出单镜时长上限（{', '.join(overlong[:8])}"
            f"{' 等' if len(overlong) > 8 else ''}）：配音沿时间轴溢出会挤掉后面镜头的台词，"
            f"成片尾部会被静默截断。建议把这些镜头的台词拆成更多镜头。")
    if blank_visual:
        warnings.append(
            f"有 {len(blank_visual)} 个镜头既无画面描述也无 H3 提示词（{', '.join(blank_visual[:8])}"
            f"{' 等' if len(blank_visual) > 8 else ''}）：出片会缺少画面指引。")
    if unknown_speaker:
        warnings.append(
            f"有 {len(unknown_speaker)} 个镜头的出场角色不在角色表里（{', '.join(unknown_speaker[:8])}"
            f"{' 等' if len(unknown_speaker) > 8 else ''}）：音色会退回默认「旁白」。")

    stats = {
        "shot_count": len(shots),
        "speakable_lines": speakable_lines,
        "narration_line_count": narration_lines,
        "speakable_line_count_total": speakable_lines + narration_lines,
        "silent_shot_count": len(silent),
        "no_voice_shot_count": len(no_voice),
        "overlong_speech_shot_count": len(overlong),
        "fallback_shot_count": len(fallback),
        "all_fallback": all_fallback,
        "blank_visual_shot_count": len(blank_visual),
        "unknown_speaker_shot_count": len(unknown_speaker),
    }
    if shots and speakable_lines == 0:
        warnings.insert(0, f"整集 {len(shots)} 个镜头里没有任何台词，配音合成会产出 0 句音频："
                           f"本系统靠 dialogue 出声（旁白已取消），请检查剧本是否漏写台词"
                           f"（原文的心理活动应当改写成该角色的自语台词）。")
    # ok = 「可以直接进配音出片」。三种情况不放行：
    #   有「既无台词也无音效提示」的空响镜头 / 有空洞画面 /
    #   整集无任何**台词**（配音链路只读 dialogue，成片会完全无人声；旁白已取消、不再算数）/
    #   整集全是兜底镜头（剧本未经模型加工）。
    #   有残留旁白的旧剧本同样不放行：旁白通道已关闭，成片不会念它。
    # ⚠️ 「无台词但写了音效提示」的纯画面镜**不算缺陷** —— 新口径允许留白（旧口径曾把它算作
    #    silent，会把合法的动作镜/空镜整集判失败）。
    return {
        "ok": (bool(shots) and not (silent or blank_visual)
               and speakable_lines > 0 and not all_fallback),
        "stats": stats,
        "warnings": warnings,
        "problem_shots": {
            "silent": silent,
            "no_voice": no_voice,
            "overlong_speech": overlong,
            "fallback": fallback,
            "blank_visual": blank_visual,
            "unknown_speaker": unknown_speaker,
        },
    }

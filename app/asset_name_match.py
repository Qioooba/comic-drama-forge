# -*- coding: utf-8 -*-
"""资产名统一匹配（角色 / 物品 / 场景 三类共用）——2026-09-29

## 为什么需要这个模块

「镜头声明了某个资产 → 按名字取出它的设定图」这件事，全流程至少有 **12 处**：
上游剧本规范化阶段过滤 characters / items / location（3 处），下游生成阶段分配
参考图（分镜参考图 / 重跑切段 / H3 视频段 / 图片质检 / 连续性判定，5 处），
再加上各处质检锚点、文字描述取用。

历史实现里每处各写一遍**裸字符串相等**::

    loc if loc in scene_idx else None
    [i for i in items_in_shot if i in items]
    [c for c in characters_in_shot if c in char_idx]

后果是**静默丢弃**：名字只因全角括号、书名号、空格、「（夜）」这类后缀差异对不上，
场景图 / 物品图 / 角色图就**不注入、不打任何日志** —— 画面里建筑形制、道具形制、
角色外观只能靠模型自己想象，是「背景不一致 / 道具走形 / 角色不像」类质检缺陷的
隐蔽来源。

更糟的是**口径漂移**：同一件事在不同模块里有三种写法 —— 上游 `location` 是
「单向子串取第一个命中」（`next(n for n in scenes if n in loc)`，多候选时会误配
而不是放弃），下游 `app.py` 是「裸相等」，角色另有一套 `_normalize_char_alias`。
改一处必然漏掉其它处。

本模块把「归一化 + 匹配」收敛为**唯一实现**，三类资产共用，并确立
「宁可告警、不可静默」。

## 匹配的三级降级（越靠后越保守）

1. ``exact``      —— 原样相等（历史行为，零风险）
2. ``normalized`` —— 归一化后相等（全半角 / 引号 / 空白，角色额外含后缀剥离）
3. ``substring``  —— 一方包含另一方，且**在索引中唯一命中**
   （「铜铃巷」↔「铜铃巷（夜）」、「青霜剑」↔「青霜剑（断）」、「执法堂」↔「执法堂门口」）

⚠️ 第 3 级**强制唯一**：误配（把 A 的设定图给 B）比丢图更糟 —— 丢图只是「没有
锚点」，误配会把**错误外观**主动焊进画面，且事后无法从日志看出来。故多候选一律
返回 ``(None, "")``，由调用方告警。

## 归一化**刻意不做**的事

不剥「（夜）」「外」「门口」「（断）」这类**限定词** —— 它们是资产区分的一部分，
剥了会把「卧室」和「卧室外」、「青霜剑」和「青霜剑（断）」混成一个。这类差异交给
第 3 级「唯一子串」去兜（且必须唯一）。

唯一例外是**角色名的维护后缀**（`_主角` / `_角色` / `_主` / `_人`）：那是资产
目录命名习惯与剧本写法的差异，语义上确实指同一个角色。
"""

from __future__ import annotations

import re

__all__ = [
    "normalize", "strip_char_suffix", "normalize_asset",
    "match", "resolve_names", "LEVEL_EXACT", "LEVEL_NORMALIZED", "LEVEL_SUBSTRING",
]

LEVEL_EXACT = "exact"
LEVEL_NORMALIZED = "normalized"
LEVEL_SUBSTRING = "substring"

#: 全角 ASCII（！-～）→ 半角；另加全角空格。只动标点 / 空白，不动汉字。
_FULLWIDTH_MAP = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20

#: 装饰性符号（引号 / 书名号 / 间隔号）：本身无语义，剥离后仍指向同一资产。
_DECOR_CHARS = ("《", "》", "「", "」", "『", "』", "\"", "'",
                "“", "”", "‘", "’", "·", "•")

#: 角色名的维护后缀：资产目录可能叫「青玉_主角」而剧本写「青玉」，或反之。
#: 与 app._normalize_char_alias 的历史口径保持一致（外层另加标点 / 全半角归一化）。
_CHAR_SUFFIXES = ("_主角", "_角色", "_主", "_人")

_WS_RE = re.compile(r"\s+")


def normalize(name) -> str:
    """资产名公共归一化：全角→半角、剥装饰符、去空白、转小写。

    三类资产（角色 / 物品 / 场景）共用的**底层**归一化，不做语义改写。
    角色请用 :func:`normalize_asset`（`kind="character"`）以追加后缀剥离。
    """
    s = str(name or "").strip()
    if not s:
        return ""
    s = s.translate(_FULLWIDTH_MAP)
    for ch in _DECOR_CHARS:
        s = s.replace(ch, "")
    return _WS_RE.sub("", s).lower()


def strip_char_suffix(name) -> str:
    """剥角色名维护后缀（`_主角` / `_角色` / `_主` / `_人`），只剥一个、且不剥成空。"""
    s = str(name or "").strip()
    for suf in _CHAR_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def normalize_asset(name, kind: str = "asset") -> str:
    """按类别归一化。``kind="character"`` 时额外剥维护后缀。

    ⚠️ 顺序是**先标点归一化、再剥后缀**（不是反过来）。「《青玉_主角》」这类
    组合写法，若按原文先剥后缀会因结尾是「》」而剥不掉（``str.endswith("_主角")``
    为 False）→ 名字仍带着后缀与书名号，匹配不到「青玉」。与
    ``app._normalize_char_alias`` 的既有口径一致（它同样先去《》与空格、再剥后缀）。
    """
    n = normalize(name)
    if str(kind or "").lower() in ("character", "char", "role"):
        n = normalize(strip_char_suffix(n))
    return n


def _as_index(index) -> dict:
    """索引统一成 ``{键: 值}``。传入 list / tuple / set 时值等于键（仅用于匹配）。"""
    if isinstance(index, dict):
        return index
    if isinstance(index, (list, tuple, set)):
        return {k: k for k in index if k}
    return {}


def match(queried, index, kind: str = "asset") -> tuple:
    """把任意写法的资产名解析到 ``index`` 的键 → ``(key, level)``。

    三级降级见模块文档；全部失败返回 ``(None, "")``。``index`` 可以是
    ``dict``（值任意）或 ``list``（此时只做匹配、拿不到值）。
    """
    idx = _as_index(index)
    raw = str(queried or "").strip()
    if not raw or not idx:
        return None, ""
    if raw in idx:
        return raw, LEVEL_EXACT
    n = normalize_asset(raw, kind)
    if not n:
        return None, ""
    # 归一化后可能撞键（两个原名归一到同一串）→ 保留先出现的，避免歧义。
    norm_map = {}
    for k in idx.keys():
        nk = normalize_asset(k, kind)
        if nk and nk not in norm_map:
            norm_map[nk] = k
    if n in norm_map:
        return norm_map[n], LEVEL_NORMALIZED
    hits = []
    for nk, k in norm_map.items():
        if n in nk or nk in n:
            hits.append(k)
            if len(hits) > 1:
                return None, ""            # 多候选 → 放弃（防误配）
    if len(hits) == 1:
        return hits[0], LEVEL_SUBSTRING
    return None, ""


def resolve_names(raw_names, index, kind: str = "asset") -> tuple:
    """列表版：把 ``raw_names`` 逐个解析到 ``index`` 的键。

    返回 ``(resolved, missing, fuzzy)``：

    * ``resolved`` —— 规范名列表（**去重保序**，保持镜头原始先后）
    * ``missing``  —— 未命中的原始名（调用方据此告警，**不要静默丢**）
    * ``fuzzy``    —— ``[(原写法, 规范名, level)]``，非精确命中的记录（供 info 日志）

    空输入 / 空索引 → 三个都为空，不算异常（正常空镜）。
    """
    idx = _as_index(index)
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    names = [str(n).strip() for n in (raw_names or []) if str(n or "").strip()]
    resolved, missing, fuzzy = [], [], []
    if not names or not idx:
        return resolved, missing, fuzzy
    seen = set()
    for n in names:
        k, lv = match(n, idx, kind)
        if not k:
            if n not in missing:
                missing.append(n)
            continue
        if k in seen:
            continue
        seen.add(k)
        resolved.append(k)
        if lv != LEVEL_EXACT:
            fuzzy.append((n, k, lv))
    return resolved, missing, fuzzy

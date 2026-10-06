# -*- coding: utf-8 -*-
"""风格目录领域模型（纯领域层：不 import flask，不做 IO）。

背景（2026-10-07 风格库唯一真源化）
----------------------------------
61 项可视化风格库此前**内联在** ``frontend/src/pages/ProjectsPage.tsx``，导致：

1. 前端改了文案，后端（``style_kit`` 注入链路 / 质检风格闸门）完全不知道；
2. ``value`` 就是**中文自由文本**，改名即断历史 —— 项目 ``config.json`` 里存的
   是字符串，没有任何稳定标识可以回指；
3. 分类、画幅、关键词全在前端硬编码，后端无法复用。

本模块把风格库抽成**后端单一事实源**（``app/style_catalog.json``）的内存表示，
核心不变量：

- :attr:`StyleEntry.style_id` **稳定且不含中文**（纯 ASCII ``snake_case``）。
  改名只改 :attr:`StyleEntry.label`，``style_id`` 永久不变 → 历史不断。
- :attr:`StyleEntry.label` 同时是**旧自由文本字面值**（写入 ``config.style`` 的值），
  因此老项目里的字符串仍能被 :meth:`StyleCatalog.resolve_text` 解析回目录条目。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: ``style_id`` 允许的字符集：小写字母、数字、下划线。
#: 刻意**不含中文**——``style_id`` 会进 URL、数据库主键与日志，ASCII 才稳。
_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789_")

#: ``aspect_default`` 允许的显式比例（与 ``style_kit._SELECTOR_ASPECT_TEXT`` 对齐）。
_KNOWN_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9")


class StyleCatalogError(ValueError):
    """风格目录数据非法（缺字段 / id 重复 / id 含非 ASCII 等）。

    目录是**单一事实源**，坏了必须响亮：宁可启动报错，也不要静默少几个风格。
    """


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def validate_style_id(style_id: str) -> str:
    """校验并归一化 ``style_id``，非法即抛 :class:`StyleCatalogError`。"""
    sid = str(style_id or "").strip()
    if not sid:
        raise StyleCatalogError("style_id 不能为空")
    if _has_cjk(sid):
        raise StyleCatalogError("style_id 不能含中文：" + sid)
    bad = sorted({ch for ch in sid if ch not in _ID_ALLOWED})
    if bad:
        raise StyleCatalogError(
            "style_id 只允许小写字母/数字/下划线，非法字符：" + "".join(bad))
    return sid


@dataclass(frozen=True)
class StyleEntry:
    """一个风格条目。

    :param style_id: 稳定标识（不含中文、不随文案变化），如 ``style_2d_urban_romance``。
    :param label: 展示名 / 写入 ``config.style`` 的旧自由文本字面值。
    :param category: 分类 id（``2d`` / ``3d`` / ``real``）。
    :param thumbnail: 缩略图**文件名**（不含目录）。图仍由前端 import，
        Flask 只挂了 ``/assets`` 路由，不把 61 张图搬走后端。
    :param aspect_default: 该风格建议的默认画幅（``"9:16"`` 等），仅作解析回退。
    :param positive_suffix: 交给 ``style_kit.with_style`` 的风格后缀正文
        （**不含**画幅 token 与画质收尾，否则会与既有注入链路重复）。
    :param negative_suffix: 该风格需要压制的负向词。
    :param aliases: 历史上出现过的其它写法（同样可解析回本条目）。
    """

    style_id: str
    label: str
    category: str
    thumbnail: str = ""
    aspect_default: str = "16:9"
    positive_suffix: str = ""
    negative_suffix: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()

    @property
    def value(self) -> str:
        """写入 ``config.style`` 的字面值（= label，向后兼容老项目）。"""
        return self.label

    @property
    def prompt_style(self) -> str:
        """注入用风格串：优先 ``positive_suffix``，为空回落 label。"""
        return self.positive_suffix or self.label

    def aspect_ratio(self) -> Tuple[int, int]:
        """``aspect_default`` → ``(a, b)``；解析不了返回 ``(16, 9)``。"""
        raw = str(self.aspect_default or "").strip()
        a, _, b = raw.partition(":")
        try:
            ta, tb = int(a), int(b)
        except ValueError:
            return (16, 9)
        if ta <= 0 or tb <= 0:
            return (16, 9)
        return (ta, tb)

    def to_payload(self) -> Dict[str, object]:
        """对外 JSON 载荷（前端画廊 + 质检闸门都消费这一份）。"""
        return {
            "style_id": self.style_id,
            "label": self.label,
            "value": self.value,
            "category": self.category,
            "thumbnail": self.thumbnail,
            "aspect_default": self.aspect_default,
            "positive_suffix": self.prompt_style,
            "negative_suffix": list(self.negative_suffix),
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class AspectPreset:
    """画幅预设（新建项目可选的 8 种比例）。

    :param value: 写入 ``config.aspect_ratio`` 的**字面值**，与前端历史写入逐字一致。
    :param comfy_widget: ComfyUI ``ResolutionSelector`` 节点的下拉字面值
        （与 ``style_kit._SELECTOR_ASPECT_TEXT`` 对齐，大小写不可改）。
    """

    aspect_id: str
    ratio: str
    label: str
    value: str
    orientation: str = ""
    comfy_widget: str = ""

    def to_payload(self) -> Dict[str, object]:
        return {
            "aspect_id": self.aspect_id,
            "ratio": self.ratio,
            "label": self.label,
            "value": self.value,
            "orientation": self.orientation,
            "comfy_widget": self.comfy_widget,
        }


@dataclass(frozen=True)
class StyleCategory:
    """风格分类（2D / 3D / 真人写实）。"""

    id: str
    label: str
    order: int = 0

    def to_payload(self) -> Dict[str, object]:
        return {"id": self.id, "label": self.label, "order": self.order}


@dataclass
class StyleCatalog:
    """风格目录（不可变数据 + 若干查表索引）。"""

    styles: List[StyleEntry] = field(default_factory=list)
    categories: List[StyleCategory] = field(default_factory=list)
    aspect_presets: List[AspectPreset] = field(default_factory=list)
    default_style_id: str = ""
    default_aspect_id: str = "aspect_16x9"
    schema_version: int = 1
    catalog_version: str = ""

    # -- 查表索引（构造时建一次，查询 O(1)） ------------------------------ #
    _by_id: Dict[str, StyleEntry] = field(default_factory=dict, repr=False)
    _by_text: Dict[str, StyleEntry] = field(default_factory=dict, repr=False)
    _by_aspect: Dict[str, AspectPreset] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "StyleCatalog":
        """从 ``style_catalog.json`` 的原始 dict 构建，**严格校验**。

        校验失败即抛 :class:`StyleCatalogError`（fail-closed）：目录少一项风格，
        比多一项更危险，必须响亮。
        """
        if not isinstance(data, dict):
            raise StyleCatalogError("风格目录根节点必须是对象")

        styles: List[StyleEntry] = []
        by_id: Dict[str, StyleEntry] = {}
        by_text: Dict[str, StyleEntry] = {}
        for raw in (data.get("styles") or []):
            entry = _build_entry(raw)
            if entry.style_id in by_id:
                raise StyleCatalogError("style_id 重复：" + entry.style_id)
            by_id[entry.style_id] = entry
            by_text.setdefault(_norm_text(entry.label), entry)
            for alias in entry.aliases:
                by_text.setdefault(_norm_text(alias), entry)
            styles.append(entry)

        categories = [
            StyleCategory(id=str(c.get("id") or "").strip(),
                          label=str(c.get("label") or "").strip(),
                          order=int(c.get("order") or 0))
            for c in (data.get("categories") or [])
        ]

        presets: List[AspectPreset] = []
        by_aspect: Dict[str, AspectPreset] = {}
        for raw in (data.get("aspect_presets") or []):
            ap = _build_aspect(raw)
            if ap.aspect_id in by_aspect:
                raise StyleCatalogError("aspect_id 重复：" + ap.aspect_id)
            by_aspect[ap.aspect_id] = ap
            by_aspect.setdefault(ap.ratio, ap)
            presets.append(ap)

        cat = cls(
            styles=styles,
            categories=categories,
            aspect_presets=presets,
            default_style_id=str(data.get("default_style_id") or "").strip(),
            default_aspect_id=str(data.get("default_aspect_id") or "aspect_16x9").strip(),
            schema_version=int(data.get("schema_version") or 1),
            catalog_version=str(data.get("catalog_version") or ""),
            _by_id=by_id,
            _by_text=by_text,
            _by_aspect=by_aspect,
        )
        if cat.default_style_id and cat.default_style_id not in by_id:
            raise StyleCatalogError(
                "default_style_id 指向不存在的条目：" + cat.default_style_id)
        return cat

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self.styles)

    def resolve_id(self, style_id: str) -> Optional[StyleEntry]:
        """按 ``style_id`` 精确查询（未命中返回 None）。"""
        return self._by_id.get(str(style_id or "").strip())

    def resolve_text(self, text: str) -> Optional[StyleEntry]:
        """把**任意**风格串解析回目录条目 —— 向后兼容的关键。

        匹配顺序（先精确后模糊，宁可模糊也不让存量项目失效）：

        1. 直接命中 ``style_id``；
        2. 归一化后命中 ``label`` 或任一 ``alias``（老项目 ``config.style`` 就是 label）；
        3. **最长子串**命中：风格串可能带修饰词（如「国漫风格/偏写实/竖屏9:16」），
           取被包含的最长 label，长度相同则取目录中靠前者。
        """
        raw = str(text or "").strip()
        if not raw:
            return None
        hit = self._by_id.get(raw)
        if hit:
            return hit
        norm = _norm_text(raw)
        hit = self._by_text.get(norm)
        if hit:
            return hit
        best: Optional[StyleEntry] = None
        best_len = 0
        for entry in self.styles:
            for candidate in (entry.label,) + tuple(entry.aliases):
                cnorm = _norm_text(candidate)
                if len(cnorm) >= 2 and cnorm in norm and len(cnorm) > best_len:
                    best, best_len = entry, len(cnorm)
        return best

    def aspect(self, aspect_id_or_ratio: str) -> Optional[AspectPreset]:
        """按 ``aspect_id``（``aspect_16x9``）或比例（``"16:9"``）取画幅预设。"""
        return self._by_aspect.get(str(aspect_id_or_ratio or "").strip())

    def counts(self) -> Dict[str, int]:
        """各分类风格数（``{"all": 61, "2d": 24, "3d": 11, "real": 26}``）。"""
        out: Dict[str, int] = {"all": len(self.styles)}
        for entry in self.styles:
            out[entry.category] = out.get(entry.category, 0) + 1
        return out

    def to_payload(self) -> Dict[str, object]:
        """整份目录的对外载荷（``GET /api/styles`` 的 body）。"""
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "default_style_id": self.default_style_id,
            "default_aspect_id": self.default_aspect_id,
            "categories": [c.to_payload() for c in self.categories],
            "styles": [s.to_payload() for s in self.styles],
            "aspect_presets": [a.to_payload() for a in self.aspect_presets],
            "counts": self.counts(),
        }


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

#: 风格串归一化用的分隔符（与 ``style_kit._SEP_RE`` 语义对齐，本模块不 import style_kit
#: 以免领域层耦合注入链路；但保持同样规则，老项目的串才能对得上）。
_SEP_CHARS = "，,、/／|｜·・；; \t\r\n　"


def _norm_text(text: str) -> str:
    """归一化风格串：统一分隔符、去空白、压小写。"""
    raw = str(text or "")
    out = []
    for ch in raw:
        out.append("，" if ch in _SEP_CHARS else ch)
    return "".join(out).strip("，").strip().lower()


def _build_entry(raw: object) -> StyleEntry:
    """校验并构建单个 :class:`StyleEntry`。"""
    if not isinstance(raw, dict):
        raise StyleCatalogError("styles 里的条目必须是对象")
    style_id = validate_style_id(raw.get("style_id"))
    label = str(raw.get("label") or "").strip()
    if not label:
        raise StyleCatalogError("风格缺少 label：" + style_id)
    category = str(raw.get("category") or "").strip()
    if not category:
        raise StyleCatalogError("风格缺少 category：" + style_id)
    aspect_default = str(raw.get("aspect_default") or "").strip()
    if aspect_default and aspect_default not in _KNOWN_RATIOS:
        raise StyleCatalogError(
            "aspect_default 不是已知比例：{}（{}）".format(aspect_default, style_id))
    negative = raw.get("negative_suffix")
    if negative is None:
        negative = ()
    elif isinstance(negative, str):
        negative = (negative,)
    aliases = raw.get("aliases") or ()
    if isinstance(aliases, str):
        aliases = (aliases,)
    return StyleEntry(
        style_id=style_id,
        label=label,
        category=category,
        thumbnail=str(raw.get("thumbnail") or "").strip(),
        aspect_default=aspect_default or "16:9",
        positive_suffix=str(raw.get("positive_suffix") or "").strip(),
        negative_suffix=tuple(str(x).strip() for x in negative if str(x).strip()),
        aliases=tuple(str(x).strip() for x in aliases if str(x).strip()),
    )


def _build_aspect(raw: object) -> AspectPreset:
    """校验并构建单个 :class:`AspectPreset`。"""
    if not isinstance(raw, dict):
        raise StyleCatalogError("aspect_presets 里的条目必须是对象")
    aspect_id = validate_style_id(str(raw.get("aspect_id") or ""))
    ratio = str(raw.get("ratio") or "").strip()
    if ratio not in _KNOWN_RATIOS:
        raise StyleCatalogError("画幅预设 ratio 不是已知比例：" + ratio)
    value = str(raw.get("value") or "").strip()
    if not value:
        # value 是写入 config.aspect_ratio 的字面值，缺了就无法与历史数据对齐
        raise StyleCatalogError("画幅预设缺少 value：" + aspect_id)
    return AspectPreset(
        aspect_id=aspect_id,
        ratio=ratio,
        label=str(raw.get("label") or value),
        value=value,
        orientation=str(raw.get("orientation") or "").strip(),
        comfy_widget=str(raw.get("comfy_widget") or "").strip(),
    )


__all__ = [
    "AspectPreset",
    "StyleCatalog",
    "StyleCatalogError",
    "StyleCategory",
    "StyleEntry",
    "validate_style_id",
]
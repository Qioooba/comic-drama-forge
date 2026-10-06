# -*- coding: utf-8 -*-
"""授权领域模型：模型授权登记、音乐/素材授权、品牌与水印版本、**进入交付的门禁**。

背景（P1-7）
------------
本仓库此前 ``grep license`` **零命中** —— 整个「生成 → 导出 → 交付」链路
里没有任何授权概念。后果很具体：用了一个限制商用的底模、把带 CC BY 署名的
音乐直接塞进成片、或者套了别人的水印，最终**无法判断这份交付物能不能发**。

这一模块把授权做成**结构化登记 + 强制门禁**，而不是文档里的一句提醒：

* :data:`LicenseRegistry` —— 由 ``config/model-licensing.json`` 构建的只读索引；
* :func:`evaluate_gate` —— 交付前的门禁判定，输出**带错误码**的拒绝清单；
* :func:`attribution_lines` —— 把 CC BY 之类要求署名的条目汇总成可复制到
  简介的署名文本（自研系统的做法，值得照搬）。

核心不变量
----------
**未登记授权的模型/素材不得进入交付。** 门禁默认 fail-closed：登记表缺失、
条目缺字段、许可类型未知，一律判为「未授权」，而不是「放行」。

同样按协调书 §4：这里判定的只是**客观授权事实**（这个模型能不能商用、
要不要署名），它**不等于**人工批准；两者都要过，缺一不可。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "LICENSE_SCHEMA_VERSION",
    "LICENSING_REGISTRY_PATH",
    "LICENSE_TYPES",
    "LIC_REGISTRY_MISSING",
    "LIC_MODEL_UNREGISTERED",
    "LIC_MODEL_NONCOMMERCIAL",
    "LIC_LICENSE_UNVERIFIED",
    "LIC_ATTRIBUTION_MISSING",
    "LIC_ASSET_UNREGISTERED",
    "LIC_ASSET_NONCOMMERCIAL",
    "LIC_BRAND_UNRESOLVED",
    "LIC_GATE_BLOCKED",
    "LICENSING_ERROR_CODES",
    "normalize_license_type",
    "is_commercial_ok",
    "LicenseRegistry",
    "evaluate_gate",
    "attribution_lines",
    "load_registry",
]

#: 授权登记表 schema 版本。改动结构时 +1，历史登记会被视为旧结构。
LICENSE_SCHEMA_VERSION = 1

#: 登记表默认位置：仓库根的 ``config/model-licensing.json``。
LICENSING_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "model-licensing.json")

# --------------------------------------------------------------------------
# 许可类型
# --------------------------------------------------------------------------

#: 许可类型 → 是否默认可商用。
#:
#: ``unknown`` 刻意存在且**判为不可商用**：登记表里写了个看不懂的类型时，
#: 正确做法是停下来问人，不是猜一个宽松答案放行。
LICENSE_TYPES: Dict[str, Dict[str, Any]] = {
    "apache-2.0": {"commercial": True, "attribution": "NOTICE", "label": "Apache 2.0"},
    "mit": {"commercial": True, "attribution": "NOTICE", "label": "MIT"},
    "openrail++-m": {"commercial": True, "attribution": "REQUIRED",
                     "label": "OpenRAIL++-M（可商用，须遵守使用政策并署名）"},
    "openrail": {"commercial": True, "attribution": "REQUIRED", "label": "OpenRAIL（可商用，须署名）"},
    "cc-by-4.0": {"commercial": True, "attribution": "REQUIRED", "label": "CC BY 4.0（需署名）"},
    "cc-by-sa-4.0": {"commercial": True, "attribution": "REQUIRED",
                     "label": "CC BY-SA 4.0（需署名 + 相同方式共享）"},
    "cc-by-nc-4.0": {"commercial": False, "attribution": "REQUIRED",
                     "label": "CC BY-NC 4.0（禁止商用）"},
    "cc0-1.0": {"commercial": True, "attribution": "NONE", "label": "CC0 1.0（公有领域）"},
    "gpl-3.0": {"commercial": True, "attribution": "REQUIRED", "label": "GPL 3.0（需署名）"},
    "commercial": {"commercial": True, "attribution": "PER-CONTRACT", "label": "商业授权（按合同）"},
    # 明确禁商用的一类（常见于「社区版/研究版权重」）
    "non-commercial": {"commercial": False, "attribution": "REQUIRED",
                       "label": "非商用授权（禁止商业交付）"},
    "research-only": {"commercial": False, "attribution": "REQUIRED", "label": "仅限研究，禁止商用"},
    "unknown": {"commercial": False, "attribution": "REQUIRED", "label": "许可类型未知"},
}

# --------------------------------------------------------------------------
# 错误码（与 app/domain/delivery.py 的 DLV-* 同一思路：
# 既有 app/failure_codes.py 的 F-* 体系一个字不动，另起命名空间）
# --------------------------------------------------------------------------

LIC_REGISTRY_MISSING = "LIC-REGISTRY-MISSING"
LIC_MODEL_UNREGISTERED = "LIC-MODEL-UNREGISTERED"
LIC_MODEL_NONCOMMERCIAL = "LIC-MODEL-NONCOMMERCIAL"
LIC_LICENSE_UNVERIFIED = "LIC-LICENSE-UNVERIFIED"
LIC_ATTRIBUTION_MISSING = "LIC-ATTRIBUTION-MISSING"
LIC_ASSET_UNREGISTERED = "LIC-UNREGISTERED-ASSET"
LIC_ASSET_NONCOMMERCIAL = "LIC-ASSET-NONCOMMERCIAL"
LIC_BRAND_UNRESOLVED = "LIC-BRAND-UNRESOLVED"
LIC_GATE_BLOCKED = "LIC-GATE-BLOCKED"

#: 错误码 → 中文说明。**对外契约**，前端直接展示，改动前先改契约。
LICENSING_ERROR_CODES: Dict[str, str] = {
    LIC_REGISTRY_MISSING: "授权登记表缺失或格式非法，已按最严口径阻断交付",
    LIC_MODEL_UNREGISTERED: "模型未登记授权，不得进入交付",
    LIC_MODEL_NONCOMMERCIAL: "模型许可禁止商用，不得进入交付",
    LIC_LICENSE_UNVERIFIED: "模型/素材授权待核实，未确认可商用，不得进入交付",
    LIC_ATTRIBUTION_MISSING: "该授权要求署名，交付包未提供署名文本",
    LIC_ASSET_UNREGISTERED: "音乐/素材未登记授权，不得进入交付",
    LIC_ASSET_NONCOMMERCIAL: "音乐/素材许可禁止商用，不得进入交付",
    LIC_BRAND_UNRESOLVED: "品牌与水印版本不存在或未启用，不得进入交付",
    LIC_GATE_BLOCKED: "授权门禁未通过",
}


def normalize_license_type(value: str) -> str:
    """许可类型归一：大小写与分隔符无关，未知值落 ``unknown``（= 不可商用）。"""
    key = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    return key if key in LICENSE_TYPES else "unknown"


def is_commercial_ok(license_type: str) -> bool:
    """该许可类型是否允许商用。未知类型一律 ``False``（fail-closed）。"""
    return bool(LICENSE_TYPES[normalize_license_type(license_type)]["commercial"])


# --------------------------------------------------------------------------
# 登记表
# --------------------------------------------------------------------------

class LicenseRegistry:
    """``config/model-licensing.json`` 的内存索引（构建后只读）。

    数据结构（文件内）::

        {
          "schema_version": 1,
          "models":     [ {name, source, license_type, commercial_use,
                            attribution_required, attribution_text, notes} ],
          "assets":     [ {id, name, kind: music|footage|image|audio,
                            source, license_type, attribution_required,
                            attribution_text, notes} ],
          "brands":     [ {brand_id, name, versions:
                            [{version, watermark_text, watermark_position,
                              enabled, updated_at}] } ]
        }
    """

    def __init__(self, doc: Dict[str, Any], source_path: str = "",
                 loaded: bool = True, load_error: str = ""):
        self.source_path = str(source_path or "")
        self.loaded = bool(loaded)
        self.load_error = str(load_error or "")
        self.schema_version = int((doc or {}).get("schema_version") or LICENSE_SCHEMA_VERSION)
        self.models: List[Dict[str, Any]] = [dict(m) for m in (doc or {}).get("models") or []]
        self.assets: List[Dict[str, Any]] = [dict(a) for a in (doc or {}).get("assets") or []]
        self.brands: List[Dict[str, Any]] = [dict(b) for b in (doc or {}).get("brands") or []]

    # -- 查找 ------------------------------------------------------------
    def find_model(self, name: str) -> Optional[Dict[str, Any]]:
        """按名称查模型授权；未登记返回 ``None``（由门禁判为 LIC-MODEL-UNREGISTERED）。"""
        key = str(name or "").strip().lower()
        if not key:
            return None
        for m in self.models:
            if str(m.get("name") or "").strip().lower() == key:
                return m
        return None

    def find_asset(self, asset_id: str, name: str = "") -> Optional[Dict[str, Any]]:
        """按 id（优先）或名称查素材授权。"""
        akey = str(asset_id or "").strip().lower()
        nkey = str(name or "").strip().lower()
        for a in self.assets:
            if akey and str(a.get("id") or "").strip().lower() == akey:
                return a
            if nkey and str(a.get("name") or "").strip().lower() == nkey:
                return a
        return None

    def find_brand(self, brand_id: str, version: str = "") -> Optional[Dict[str, Any]]:
        """查品牌；给了 ``version`` 就必须命中该版本，否则返回 ``None``。"""
        bkey = str(brand_id or "").strip().lower()
        if not bkey:
            return None
        for b in self.brands:
            if str(b.get("brand_id") or "").strip().lower() != bkey:
                continue
            if not version:
                return b
            for v in b.get("versions") or []:
                if str(v.get("version") or "").strip() == str(version).strip():
                    return b
            return None
        return None

    def brand_version(self, brand_id: str, version: str) -> Optional[Dict[str, Any]]:
        """取品牌下的**指定版本**条目（用于水印渲染参数）。"""
        b = self.find_brand(brand_id, version)
        if not b:
            return None
        for v in b.get("versions") or []:
            if str(v.get("version") or "").strip() == str(version).strip():
                return dict(v)
        return None

    def summary(self) -> Dict[str, Any]:
        """登记表概览（供 ``GET /api/licensing/registry`` 直接返回）。"""
        return {
            "loaded": self.loaded,
            "source_path": self.source_path,
            "schema_version": self.schema_version,
            "load_error": self.load_error,
            "model_count": len(self.models),
            "asset_count": len(self.assets),
            "brand_count": len(self.brands),
        }

    def to_doc(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "models": self.models,
            "assets": self.assets,
            "brands": self.brands,
        }


def load_registry(path: str = LICENSING_REGISTRY_PATH) -> LicenseRegistry:
    """读取授权登记表。

    **fail-closed**：文件缺失 / JSON 非法 / 结构不对时，返回
    ``loaded=False`` 的空登记表，由门禁统一报 ``LIC-REGISTRY-MISSING``，
    而不是静默返回一个「看起来没登记 = 放行」的空索引。
    """
    p = str(path or LICENSING_REGISTRY_PATH)
    if not os.path.isfile(p):
        return LicenseRegistry({}, source_path=p, loaded=False,
                               load_error="授权登记表不存在：{}".format(p))
    try:
        with open(p, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as exc:  # noqa: BLE001
        return LicenseRegistry({}, source_path=p, loaded=False,
                               load_error="授权登记表读取失败：{}".format(exc))
    if not isinstance(doc, dict):
        return LicenseRegistry({}, source_path=p, loaded=False,
                               load_error="授权登记表根节点必须是对象")
    return LicenseRegistry(doc, source_path=p, loaded=True)


# --------------------------------------------------------------------------
# 门禁
# --------------------------------------------------------------------------

def _entry_verdict(entry: Dict[str, Any]) -> Tuple[bool, str]:
    """单条登记的「可否用于商用交付」判定，返回 ``(ok, 拒绝原因码后缀)``。

    判定顺序刻意保守：待核实 / 许可类型未知 / 明确禁商用 / 缺必要字段，都算不过。
    返回码后缀由调用方拼成具体错误码（模型用 LIC-MODEL-*，素材用 LIC-ASSET-*），
    这样「同一个判定」不必在模型和素材两条路径上各写一遍、也不会写歪。
    """
    if not entry:
        return False, "UNREGISTERED"
    # ``verified: false`` = 待核实。宁可拦住也不放行 —— 一个「大概能商用」的
    # 授权猜错，赔的是已经发出去的成片。
    if "verified" in entry and not entry.get("verified"):
        return False, "UNVERIFIED"
    if normalize_license_type(entry.get("license_type")) == "unknown":
        return False, "UNVERIFIED"
    # ``commercial_use`` 字段允许显式收紧（例如 CC0 但双方另有约定禁商用）
    if "commercial_use" in entry and entry.get("commercial_use") is False:
        return False, "NONCOMMERCIAL"
    return (True, "") if is_commercial_ok(entry.get("license_type")) else (False, "NONCOMMERCIAL")


def _entry_ok(entry: Dict[str, Any]) -> bool:
    """:func:`_entry_verdict` 的布尔壳（供署名汇总等只需要真假的场合用）。"""
    return _entry_verdict(entry)[0]


def _needs_attribution(entry: Dict[str, Any]) -> bool:
    """该条登记是否要求署名（显式字段优先于许可类型默认值）。"""
    if "attribution_required" in entry:
        return bool(entry.get("attribution_required"))
    rule = LICENSE_TYPES[normalize_license_type(entry.get("license_type"))]
    return rule["attribution"] in ("REQUIRED", "NOTICE", "PER-CONTRACT")


def _violation(code: str, subject: str, kind: str, detail: str = "") -> Dict[str, Any]:
    """构造一条门禁拒绝项（结构固定，前端与日志共用）。"""
    return {
        "code": code,
        "message": LICENSING_ERROR_CODES.get(code, code),
        "subject": str(subject or ""),
        "kind": kind,
        "detail": str(detail or ""),
    }


def evaluate_gate(registry: LicenseRegistry,
                  requirement: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """交付前授权门禁判定。

    ``requirement``（来自交付包登记）::

        {
          "models": ["SDXL 1.0", ...],       # 名称或 id
          "assets": ["bgm_calm", ...],       # 素材 id
          "music":  ["bgm_calm", ...],       # 素材 id（音频类，与 assets 合并判定）
          "brand":  "comicdrama",            # 可选：品牌 id
          "brand_version": "v1",             # 可选：指定水印版本
          "attribution_text": "...",         # 交付方提供的署名文本
          "purpose": "commercial"            # 默认 commercial（交付即商用）
        }

    返回 ``{"ok", "violations", "checked", "attribution", "purpose"}``。
    ``ok=False`` 时 **不得**创建交付包（由 API 层拦截）。
    """
    req = dict(requirement or {})
    violations: List[Dict[str, Any]] = []
    checked = {"models": 0, "assets": 0, "brands": 0}

    # ① 登记表本身必须可用（fail-closed 根）
    if not registry or not registry.loaded:
        return {
            "ok": False,
            "checked": checked,
            "purpose": str(req.get("purpose") or "commercial"),
            "attribution": "",
            "violations": [_violation(LIC_REGISTRY_MISSING, "model-licensing.json", "registry",
                                      (registry.load_error if registry else "") or "")],
        }

    # ② 模型授权
    models: Sequence[str] = list(req.get("models") or [])
    for name in models:
        checked["models"] += 1
        entry = registry.find_model(name)
        ok, why = _entry_verdict(entry)
        if ok:
            continue
        if why == "UNREGISTERED":
            violations.append(_violation(LIC_MODEL_UNREGISTERED, name, "model",
                                         "请在 config/model-licensing.json 登记该模型"))
        elif why == "UNVERIFIED":
            violations.append(_violation(
                LIC_LICENSE_UNVERIFIED, name, "model",
                "许可类型={}（待核实）".format((entry or {}).get("license_type") or "unknown")))
        else:
            violations.append(_violation(
                LIC_MODEL_NONCOMMERCIAL, name, "model",
                "许可类型={}".format((entry or {}).get("license_type") or "unknown")))

    # ③ 音乐/素材授权（assets 与 music 合并；music 只是语义上的高亮）
    asset_ids: List[str] = []
    for key in ("assets", "music"):
        for raw in (req.get(key) or []):
            val = str(raw or "").strip()
            if val and val not in asset_ids:
                asset_ids.append(val)
    for aid in asset_ids:
        checked["assets"] += 1
        entry = registry.find_asset(aid)
        ok, why = _entry_verdict(entry)
        if ok:
            continue
        if why == "UNREGISTERED":
            violations.append(_violation(LIC_ASSET_UNREGISTERED, aid, "asset",
                                         "请在 config/model-licensing.json 登记该素材"))
        elif why == "UNVERIFIED":
            violations.append(_violation(
                LIC_LICENSE_UNVERIFIED, aid, "asset",
                "许可类型={}（待核实）".format((entry or {}).get("license_type") or "unknown")))
        else:
            violations.append(_violation(
                LIC_ASSET_NONCOMMERCIAL, aid, "asset",
                "许可类型={}".format((entry or {}).get("license_type") or "unknown")))

    # ④ 品牌与水印版本
    brand_id = str(req.get("brand") or "").strip()
    if brand_id:
        checked["brands"] += 1
        version = str(req.get("brand_version") or "").strip()
        if not registry.find_brand(brand_id, version):
            violations.append(_violation(LIC_BRAND_UNRESOLVED, brand_id, "brand",
                                         "版本={}".format(version or "未指定")))

    # ⑤ 署名要求：所有「要求署名」且登记为可商用的条目，交付方必须给出署名文本。
    #    注意这一条只对**已登记且可商用**的条目生效 —— 未登记 / 禁商用的条目
    #    已经在 ②③ 被拦下，不必在这里重复报错。
    supplied = str(req.get("attribution_text") or "").strip()
    need_attr: List[str] = []
    for entry in ([registry.find_model(m) for m in models] +
                  [registry.find_asset(a) for a in asset_ids]):
        if entry and _entry_ok(entry) and _needs_attribution(entry) and not supplied:
            label = str(entry.get("name") or entry.get("id") or "")
            if label and label not in need_attr:
                need_attr.append(label)
    if need_attr:
        violations.append(_violation(
            LIC_ATTRIBUTION_MISSING, "、".join(need_attr), "attribution",
            "需要提供 attribution_text（可复制到简介）"))

    return {
        "ok": not violations,
        "checked": checked,
        "purpose": str(req.get("purpose") or "commercial"),
        "attribution": supplied,
        "violations": violations,
    }


def attribution_lines(registry: LicenseRegistry,
                      requirement: Optional[Dict[str, Any]] = None) -> List[str]:
    """汇总「署名清单」：把要求署名的条目整理成可复制到简介的成对文本。

    输出形如 ``["SDXL 1.0 — Stability AI — CreativeML Open RAIL++-M", ...]``，
    前端「复制署名」按钮直接吃这个数组。
    """
    req = dict(requirement or {})
    if not registry or not registry.loaded:
        return []
    entries: List[Dict[str, Any]] = []
    for name in (req.get("models") or []):
        e = registry.find_model(name)
        if e:
            entries.append(e)
    for key in ("assets", "music"):
        for aid in (req.get(key) or []):
            e = registry.find_asset(aid)
            if e:
                entries.append(e)

    lines: List[str] = []
    seen = set()
    for e in entries:
        if not _needs_attribution(e):
            continue
        label = str(e.get("name") or e.get("id") or "").strip()
        text = str(e.get("attribution_text") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        lines.append(text or " — ".join(x for x in (label, str(e.get("source") or "").strip()) if x))
    return lines
# -*- coding: utf-8 -*-
"""角色 Machine Anchor 资产契约：目录扫描、镜头级选择、完整性与服装状态。

Character Sheet（``base.png``/``sheet.png``）只服务人工审阅；本模块负责给生成链路
选择单人物、单视角、单景别的生产参考图。旧 ``front/half/back`` 会自动映射到新键，
只有 ``base.png`` 的旧项目返回 ``legacy_sheet_fallback``，不静默假装完整。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "MANIFEST_FILENAME",
    "ANCHOR_KEYS",
    "REQUIRED_ANCHORS",
    "LEGACY_VIEW_MAP",
    "scan_character_assets",
    "load_manifest",
    "save_manifest",
    "asset_completeness",
    "select_character_reference",
    "resolve_outfit_dir",
    "write_anchor_metadata",
]

MANIFEST_FILENAME = "character_asset_manifest.json"
SHEET_NAMES = ("base.png", "sheet.png", "base.jpg", "sheet.jpg")

# 新机器锚点键：语义明确，不依赖“第几张图”。
ANCHOR_KEYS = (
    "face_front", "face_left45", "face_right45",
    "profile_left", "profile_right",
    "full_front", "full_left45", "full_right45", "full_back",
    "bust_front", "half_front",
)
REQUIRED_ANCHORS = ("face_front", "full_front", "half_front")
LEGACY_VIEW_MAP = {
    "front": "full_front",
    "left": "full_left45",
    "right": "full_right45",
    "back": "full_back",
    "half": "half_front",
}

# 目录/文件候选。扁平命名与语义目录都支持。
_FILE_ALIASES = {
    "face_front": ("identity/face_front.png", "identity/face.png", "face_front.png", "face.png"),
    "face_left45": ("identity/face_left45.png", "face_left45.png"),
    "face_right45": ("identity/face_right45.png", "face_right45.png"),
    "profile_left": ("identity/profile_left.png", "profile_left.png"),
    "profile_right": ("identity/profile_right.png", "profile_right.png"),
    "full_front": ("body/full_front.png", "full_front.png", "front.png"),
    "full_left45": ("body/full_left45.png", "full_left45.png", "left45.png", "left.png"),
    "full_right45": ("body/full_right45.png", "full_right45.png", "right45.png", "right.png"),
    "full_back": ("body/full_back.png", "full_back.png", "back.png"),
    "bust_front": ("framing/bust_front.png", "bust_front.png"),
    "half_front": ("framing/half_front.png", "half_front.png", "half.png"),
}


def _ok_file(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def _manifest_path(asset_dir: str) -> str:
    return os.path.join(str(asset_dir), MANIFEST_FILENAME)


def load_manifest(asset_dir: str) -> Dict[str, Any]:
    p = _manifest_path(asset_dir)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".char_manifest_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_manifest(asset_dir: str, manifest: Dict[str, Any]) -> str:
    payload = dict(manifest or {})
    payload.setdefault("schema_version", 1)
    payload.setdefault("updated_at", datetime.now().isoformat(timespec="seconds"))
    path = _manifest_path(asset_dir)
    _atomic_json(path, payload)
    return path


def write_anchor_metadata(asset_dir: str, key: str, *, path: str, source: str,
                          role: str = "", view: str = "", framing: str = "",
                          outfit_key: str = "", seed: Any = None, prompt: str = "",
                          workflow: str = "", qc: Any = None, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """给单张机器锚点补 manifest 条目；不覆盖已有 QC，除非调用方显式传入。"""
    m = load_manifest(asset_dir)
    anchors = m.get("anchors") if isinstance(m.get("anchors"), dict) else {}
    old = anchors.get(key) if isinstance(anchors.get(key), dict) else {}
    entry = dict(old)
    entry.update({
        "key": key,
        "path": path,
        "asset_role": role or entry.get("asset_role") or "machine_anchor",
        "view": view or entry.get("view") or "",
        "framing": framing or entry.get("framing") or "",
        "outfit_key": outfit_key or entry.get("outfit_key") or "",
        "source": source or entry.get("source") or "",
        "seed": seed if seed is not None else entry.get("seed"),
        "prompt": prompt or entry.get("prompt") or "",
        "workflow": workflow or entry.get("workflow") or "",
        "created_at": entry.get("created_at") or datetime.now().isoformat(timespec="seconds"),
        "version": int(entry.get("version") or 1),
    })
    if qc is not None:
        entry["qc"] = qc
    if extra:
        entry.update(extra)
    anchors[key] = entry
    m.update({"schema_version": 1, "character_id": m.get("character_id") or "", "anchors": anchors})
    save_manifest(asset_dir, m)
    return entry


def scan_character_assets(asset_dir: str, outfit_dir: str = "") -> Dict[str, Any]:
    """扫描角色目录，返回 sheet、机器锚点、旧视角映射与 manifest。"""
    asset_dir = str(asset_dir or "")
    outfit_dir = str(outfit_dir or "")
    manifest = load_manifest(asset_dir)
    anchors: Dict[str, str] = {}
    sources: Dict[str, str] = {}

    def _try(rel: str, root: str) -> Optional[str]:
        p = os.path.join(root, rel.replace("/", os.sep))
        return p if _ok_file(p) else None

    for key, aliases in _FILE_ALIASES.items():
        for root in (outfit_dir, asset_dir):
            if not root:
                continue
            for rel in aliases:
                p = _try(rel, root)
                if p:
                    anchors[key] = p
                    sources[key] = "outfit" if root == outfit_dir and outfit_dir else "disk"
                    break
            if key in anchors:
                break

    # Manifest 中的显式路径优先（允许未来 URL/自定义目录），但仍校验文件。
    man_anchors = manifest.get("anchors") if isinstance(manifest.get("anchors"), dict) else {}
    for key, entry in man_anchors.items():
        if not isinstance(entry, dict):
            continue
        p = str(entry.get("path") or "")
        if _ok_file(p):
            anchors[str(key)] = p
            sources[str(key)] = str(entry.get("source") or "manifest")

    # 旧视角自动映射；只在新键缺失时补，不覆盖更明确的机器锚点。
    for legacy, new_key in LEGACY_VIEW_MAP.items():
        if new_key in anchors:
            continue
        for root in (outfit_dir, asset_dir):
            if not root:
                continue
            p = os.path.join(root, f"{legacy}.png")
            if _ok_file(p):
                anchors[new_key] = p
                sources[new_key] = f"legacy_{legacy}"
                break

    sheet = ""
    for root in (asset_dir, outfit_dir):
        if not root:
            continue
        for name in SHEET_NAMES:
            p = os.path.join(root, name)
            if _ok_file(p):
                sheet = p
                break
        if sheet:
            break
    return {
        "dir": asset_dir,
        "outfit_dir": outfit_dir,
        "sheet": sheet,
        "anchors": anchors,
        "sources": sources,
        "manifest": manifest,
        "legacy_sheet_only": bool(sheet) and not anchors,
    }


def _is_sheet(path: str) -> bool:
    return os.path.basename(str(path or "")).lower() in {x.lower() for x in SHEET_NAMES}


def asset_completeness(asset_dir: str, outfit_dir: str = "", *, strict: bool = False) -> Dict[str, Any]:
    """角色资产完整性。``ready`` 只代表可运行；``complete`` 代表新体系最低门槛达标。"""
    scan = scan_character_assets(asset_dir, outfit_dir)
    have = scan["anchors"]
    missing = [k for k in REQUIRED_ANCHORS if k not in have]
    sheet_ready = bool(scan["sheet"])
    identity_ready = "face_front" in have
    body_ready = "full_front" in have
    framing_ready = "half_front" in have
    outfit_ready = bool(outfit_dir) and any(
        k in have for k in ("full_front", "half_front", "bust_front", "face_front")
    )
    complete = sheet_ready and not missing
    return {
        "sheet_ready": sheet_ready,
        "identity_ready": identity_ready,
        "body_ready": body_ready,
        "framing_ready": framing_ready,
        "outfit_ready": outfit_ready,
        "complete": complete,
        "ready": bool(sheet_ready or have),
        "missing": missing,
        "anchors": have,
        "sources": scan["sources"],
        "legacy_sheet_fallback": bool(scan["legacy_sheet_only"]),
        "status": "complete" if complete else ("legacy_sheet_fallback" if scan["legacy_sheet_only"] else "incomplete"),
        "strict_error": ("缺少最低机器锚点：" + ",".join(missing)) if strict and missing else "",
    }


def resolve_outfit_dir(char_dir: str, outfit_key: str = "") -> str:
    if not char_dir or not outfit_key:
        return ""
    p = os.path.join(char_dir, "outfits", str(outfit_key))
    return p if os.path.isdir(p) else ""


def _camera_text(shot: dict) -> str:
    return " ".join(str(shot.get(k) or "") for k in ("shot_type", "camera", "camera_motion"))


def _requested_view(shot: dict) -> Tuple[str, List[str], str]:
    """返回 (primary_key, fallback_chain, reason)。"""
    text = _camera_text(shot)
    low = text.lower()
    back = any(x in text for x in ("背面", "背拍", "背对", "back view", "from behind")) or "back" in low
    left = any(x in text for x in ("左45", "左侧", "左三分", "left45", "left three"))
    right = any(x in text for x in ("右45", "右侧", "右三分", "right45", "right three"))
    close = any(x in text for x in ("大特写", "特写", "脸部", "面部", "close-up", "closeup"))
    bust = any(x in text for x in ("胸像", "半身", "bust"))
    near = any(x in text for x in ("近景", "中近景"))
    mid = any(x in text for x in ("中景", "medium"))
    wide = any(x in text for x in ("全景", "远景", "大远景", "wide", "establishing"))

    if back:
        return "full_back", ["full_back", "full_right45", "full_front", "front", "base"], "背面镜头"
    if close:
        if left:
            return "face_left45", ["face_left45", "face_front", "bust_front", "half_front", "front", "base"], "特写+左侧机位"
        if right:
            return "face_right45", ["face_right45", "face_front", "bust_front", "half_front", "front", "base"], "特写+右侧机位"
        return "face_front", ["face_front", "bust_front", "half_front", "full_front", "front", "base"], "特写镜头"
    if bust or near:
        return "bust_front", ["bust_front", "half_front", "face_front", "full_front", "front", "base"], "胸像/近景"
    if mid:
        return "half_front", ["half_front", "bust_front", "full_front", "front", "base"], "中景"
    if wide:
        return "full_front", ["full_front", "half_front", "bust_front", "front", "base"], "全景/远景"
    if left:
        return "full_left45", ["full_left45", "full_front", "half_front", "front", "base"], "左侧机位"
    if right:
        return "full_right45", ["full_right45", "full_front", "half_front", "front", "base"], "右侧机位"
    return "full_front", ["full_front", "half_front", "bust_front", "front", "base"], "默认全身正面"


def select_character_reference(payload: dict, shot: dict, outfit_dir: str = "", *,
                               strict: bool = False, character_name: str = "") -> Dict[str, Any]:
    """镜头级角色参考选择器。

    返回 ``path``、请求档位、实际档位、回退链、来源、服装状态和可读 warning/error。
    ``strict=True`` 时，指定服装缺失或最低身份锚点缺失会返回 error，不静默换装。
    """
    payload = dict(payload or {})
    shot = dict(shot or {})
    char_dir = str(payload.get("_dir") or "")
    requested, chain, reason = _requested_view(shot)
    outfit_key = str(shot.get("outfit_key") or "").strip()
    if not outfit_key:
        raw = str(shot.get("outfit") or "").strip()
        if raw:
            outfit_key = re.sub(r"^(?:本套服装|服装|outfit(?:_key)?)\s*[:：=]\s*", "", raw).strip()
    if outfit_key:
        # 兼容 character_outfits 的字典/字符串形态。
        co = shot.get("character_outfits")
        if isinstance(co, dict):
            val = co.get(character_name) or co.get(payload.get("name") or "")
            if isinstance(val, dict):
                val = val.get("outfit_key") or val.get("key") or val.get("outfit") or val.get("desc")
            if val:
                outfit_key = str(val).strip()

    resolved_outfit = outfit_dir or (resolve_outfit_dir(char_dir, outfit_key) if char_dir else "")
    if outfit_key and not resolved_outfit:
        if strict:
            return {
                "path": "", "requested": requested, "actual": "", "fallback": [],
                "reason": reason, "warning": "", "error": f"缺少服装资产：{outfit_key}",
                "outfit_key": outfit_key, "strict": True,
            }
        warning = f"指定服装 {outfit_key} 缺失，已回退主角色资产"
    else:
        warning = ""

    scan = scan_character_assets(char_dir, resolved_outfit)
    # 身份脸优先复用主角色 identity，服装/景别则优先 outfit。
    anchors = dict(scan["anchors"])
    if resolved_outfit:
        outfit_scan = scan_character_assets("", resolved_outfit)
        for k, p in outfit_scan["anchors"].items():
            anchors[k] = p
    if "face_front" not in anchors:
        main_scan = scan_character_assets(char_dir, "")
        anchors.update(main_scan["anchors"])

    actual = ""
    used_source = ""
    used_fallback = []
    for key in chain:
        if key in ("front", "base"):
            # 兼容链中的旧名：先查新键，再查 sheet。
            if key == "front" and "full_front" in anchors:
                actual, used_source = anchors["full_front"], scan["sources"].get("full_front", "disk")
                break
            if key == "base" and scan["sheet"]:
                actual, used_source = scan["sheet"], "legacy_sheet"
                break
            continue
        if key in anchors:
            actual, used_source = anchors[key], scan["sources"].get(key, "disk")
            if key != requested:
                used_fallback.append(key)
            break
    if not actual and scan["sheet"]:
        actual, used_source = scan["sheet"], "legacy_sheet"

    if not actual:
        return {
            "path": "", "requested": requested, "actual": "", "fallback": chain,
            "reason": reason, "warning": warning,
            "error": "角色没有任何可用参考图", "outfit_key": outfit_key, "strict": strict,
        }
    if used_source == "legacy_sheet" or _is_sheet(actual):
        warning = (warning + "；" if warning else "") + "该镜头缺少专用角色锚点，已退化为 Character Sheet"
    if strict and used_source == "legacy_sheet":
        return {
            "path": actual, "requested": requested, "actual": "base", "fallback": used_fallback,
            "reason": reason, "warning": warning,
            "error": "strict 模式禁止使用 Character Sheet 作为生产参考", "outfit_key": outfit_key,
            "strict": True,
        }
    return {
        "path": actual,
        "requested": requested,
        "actual": next((k for k, p in anchors.items() if p == actual), "base" if _is_sheet(actual) else ""),
        "fallback": used_fallback,
        "reason": reason,
        "warning": warning,
        "error": "",
        "source": used_source,
        "outfit_key": outfit_key,
        "asset_role": "sheet" if _is_sheet(actual) else "machine_anchor",
        "strict": strict,
    }

# -*- coding: utf-8 -*-
"""角色身份契约与分镜参考图排序。

设计背景
--------------------------------------------------------------------------------
旧链路把「完整四区角色设定图」直接作为分镜 ``<image1>``。由于 Qwen-Image-2.1
参考图编辑模板的 latent 尺寸继承第一张参考图，调用方为了统一输出画幅会对
``<image1>`` 执行 cover 裁剪；1:1 设定图 cover 到 16:9 会裁掉上下约 44%，
面部特写、头顶和发际线经常正好被裁掉。

本模块把两件事拆开：

1. ``base.png`` 继续作为完整设定图，只供人工审阅，不做任何裁剪；
2. ``identity/face.png`` 是内部身份锚点，从完整设定图用纯 CPU 方式派生；
3. 身份锚点永远不参与输出画幅 cover 裁剪；
4. 分镜 ``<image1>`` 优先由 3D 构图基准或场景画布占据，人物身份锚点后移。

本模块只依赖 Pillow + numpy，不访问网络、不调用 ComfyUI、不使用 GPU。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

__all__ = [
    "IDENTITY_DIRNAME",
    "FACE_ANCHOR_FILENAME",
    "IDENTITY_MANIFEST_FILENAME",
    "IdentityAnchor",
    "stable_character_id",
    "identity_directory",
    "identity_manifest_path",
    "identity_anchor_path",
    "detect_face_bbox",
    "derive_face_anchor",
    "ensure_identity_assets",
    "is_identity_reference",
    "should_unify_reference",
    "promote_canvas_reference",
]


IDENTITY_DIRNAME = "identity"
FACE_ANCHOR_FILENAME = "face.png"
IDENTITY_MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1

#: 与 comfyui_client.BLOCKING_REF_MARK 同值；此处不 import comfyui_client，避免循环依赖。
DEFAULT_BLOCKING_LABEL = "3D导演台构图基准"

#: 这两类参考图包含人物身份，禁止为了输出画幅做 cover 裁剪。
IDENTITY_REFERENCE_KINDS = frozenset({"主角色", "次角色"})


@dataclass(frozen=True)
class IdentityAnchor:
    """一次身份锚点派生结果。"""

    character_id: str
    path: str
    source_path: str
    source_sha256: str
    quality: str
    bbox: tuple[int, int, int, int] | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["bbox"] = list(self.bbox) if self.bbox else None
        return data


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stable_key_part(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch.isalnum() or ch in ("-", "_"))


def stable_character_id(asset_dir: str) -> str:
    """从资产目录生成稳定角色ID。

    目录移动到回收站不会改变角色名/项目名，因此该ID在正常迁移下保持稳定；
    服装变体 ``.../<角色>/outfits/<key>`` 会向上归一到主角色目录。
    """
    raw = os.path.normpath(str(asset_dir or "")).replace("\\", "/").strip("/")
    parts = [p for p in raw.split("/") if p]
    if "outfits" in parts:
        idx = parts.index("outfits")
        char_name = parts[idx - 1] if idx >= 1 else (parts[-1] if parts else "")
        project_name = parts[idx - 2] if idx >= 2 else ""
    else:
        char_name = parts[-1] if parts else ""
        project_name = parts[-2] if len(parts) >= 2 else ""
    basis = f"{_stable_key_part(project_name)}/{_stable_key_part(char_name)}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    return f"char_{digest}"


def identity_directory(asset_dir: str) -> str:
    return os.path.join(str(asset_dir), IDENTITY_DIRNAME)


def identity_manifest_path(asset_dir: str) -> str:
    return os.path.join(identity_directory(asset_dir), IDENTITY_MANIFEST_FILENAME)


def identity_anchor_path(asset_dir: str, require_file: bool = True) -> str:
    """返回身份锚点；没有时返回空串。"""
    path = os.path.join(identity_directory(asset_dir), FACE_ANCHOR_FILENAME)
    if require_file and not (os.path.isfile(path) and os.path.getsize(path) > 0):
        return ""
    return path


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path), prefix=".identity_manifest_", suffix=".json"
    )
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


def _skin_mask(rgb) -> "object":
    """纯numpy肤色候选掩码。

    这里不是完整人脸检测器，而是给「设定图中脸部特写」做高召回候选区域。
    真正的身份硬闸门仍应在后续阶段接入独立人脸检测/特征模型。
    """
    import numpy as np

    arr = np.asarray(rgb, dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    sat = mx - mn

    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b

    mask = (
        (y > 45.0)
        & (sat > 12.0)
        & (r > 45.0)
        & (g > 25.0)
        & (b > 12.0)
        & (r >= g)
        & (g >= b - 18.0)
        & (cb >= 68.0)
        & (cb <= 145.0)
        & (cr >= 118.0)
        & (cr <= 195.0)
        & (mx < 248.0)
    )
    return mask


def _dilate3(mask):
    import numpy as np

    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    out[1:, 1:] |= mask[:-1, :-1]
    out[:-1, :-1] |= mask[1:, 1:]
    out[1:, :-1] |= mask[:-1, 1:]
    out[:-1, 1:] |= mask[1:, :-1]
    return out


def _erode3(mask):
    import numpy as np

    core = mask.copy()
    core[1:, :] &= mask[:-1, :]
    core[:-1, :] &= mask[1:, :]
    core[:, 1:] &= mask[:, :-1]
    core[:, :-1] &= mask[:, 1:]
    core[1:, 1:] &= mask[:-1, :-1]
    core[:-1, :-1] &= mask[1:, 1:]
    core[1:, :-1] &= mask[:-1, 1:]
    core[:-1, 1:] &= mask[1:, :-1]
    return core


def _connected_components(mask, min_area: int = 8) -> list[dict]:
    """小图上的四邻域连通域；仅用于身份锚点候选，不追求通用分割性能。"""
    import numpy as np

    h, w = int(mask.shape[0]), int(mask.shape[1])
    visited = np.zeros((h, w), dtype=bool)
    comps: list[dict] = []
    for y in range(h):
        row = mask[y]
        for x in range(w):
            if not row[x] or visited[y, x]:
                continue
            stack = [(y, x)]
            visited[y, x] = True
            min_y = max_y = y
            min_x = max_x = x
            area = 0
            while stack:
                cy, cx = stack.pop()
                area += 1
                if cy < min_y:
                    min_y = cy
                elif cy > max_y:
                    max_y = cy
                if cx < min_x:
                    min_x = cx
                elif cx > max_x:
                    max_x = cx
                for ny, nx in (
                    (cy - 1, cx),
                    (cy + 1, cx),
                    (cy, cx - 1),
                    (cy, cx + 1),
                ):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
            if area >= min_area:
                comps.append(
                    {
                        "x": min_x,
                        "y": min_y,
                        "w": max_x - min_x + 1,
                        "h": max_y - min_y + 1,
                        "area": area,
                    }
                )
    return comps


def detect_face_bbox(image_path: str) -> dict | None:
    """在角色设定图中寻找最像「面部特写」的肤色连通域。

    返回原图像素坐标的 ``{x,y,w,h,score,method}``；找不到返回 None。
    算法是确定性的纯CPU启发式，不下载模型。
    """
    if not image_path or not os.path.isfile(image_path):
        return None
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return None

    try:
        with Image.open(image_path) as im:
            src_w, src_h = im.size
            max_side = max(src_w, src_h)
            scale = min(1.0, 420.0 / max(max_side, 1))
            work_w = max(32, int(round(src_w * scale)))
            work_h = max(32, int(round(src_h * scale)))
            work = im.convert("RGB").resize((work_w, work_h), Image.BILINEAR)

            mask = _skin_mask(work)
            # 一次闭运算连接眼睛/鼻/嘴造成的内部空洞，再开运算去掉零散噪声。
            mask = _erode3(_dilate3(mask))
            mask = _dilate3(_erode3(mask))
            comps = _connected_components(mask, min_area=10)
    except Exception:
        return None

    gray = work.convert("L")
    gray_arr = np.asarray(gray, dtype=np.float32)
    best = None
    best_score = -1.0
    for c in comps:
        w, h, area = c["w"], c["h"], c["area"]
        if w < 5 or h < 5:
            continue
        aspect = max(w / h, h / w)
        aspect_score = float(np.exp(-abs(np.log(max(w, 1) / max(h, 1))) * 1.15))
        compact = area / float(w * h)
        size = float(np.sqrt(area))
        # 脸部候选：偏方正、填充较实、尺寸适中。
        # 过大区域通常是裸露手臂/腿/身体，过窄区域通常是手指或文字杂色。
        size_score = min(1.0, size / 72.0)
        oversize_penalty = 0.0 if area <= 4200 else min(0.45, (area - 4200) / 14000.0)
        score = (
            0.52 * aspect_score
            + 0.20 * min(1.0, compact)
            + 0.28 * size_score
            - oversize_penalty
        )
        if aspect > 3.2:
            score -= 0.35

        # 五官/头发产生的内部暗部是区分“脸”和“手”的关键信号。
        # 只看肤色形状会把手掌、手指和肤色色块误判成脸。
        sub = gray_arr[c["y"]:c["y"] + h, c["x"]:c["x"] + w]
        dark_ratio = 0.0
        dark_components = 0
        if sub.size:
            dark = sub < 75.0
            if dark.shape[0] > 4 and dark.shape[1] > 4:
                dark[:2, :] = False
                dark[-2:, :] = False
                dark[:, :2] = False
                dark[:, -2:] = False
            dark_ratio = float(dark.mean()) if dark.size else 0.0
            if dark.any():
                dark_components = sum(
                    1
                    for d in _connected_components(dark, min_area=2)
                    if 2 <= d["w"] <= max(4, w // 2)
                    and 2 <= d["h"] <= max(4, h // 2)
                )
        feature_score = min(
            1.0,
            0.70 * min(dark_ratio / 0.12, 1.0)
            + 0.30 * min(dark_components / 3.0, 1.0),
        )
        total_score = score + 0.35 * feature_score
        if total_score > best_score:
            best_score = float(total_score)
            best = c

    if not best or best_score < 0.50:
        return None

    # 把肤色框扩成带头发/下颌/颈部的近方形身份锚点。
    bw, bh = best["w"], best["h"]
    side = max(bw, bh) * 1.10
    cx = best["x"] + bw / 2.0
    cy = best["y"] + bh / 2.0
    left = int(round(cx - side / 2.0))
    top = int(round(cy - side / 2.0))
    right = int(round(cx + side / 2.0))
    bottom = int(round(cy + side / 2.0))
    left = max(0, min(left, work_w - 1))
    top = max(0, min(top, work_h - 1))
    right = max(left + 1, min(right, work_w))
    bottom = max(top + 1, min(bottom, work_h))

    inv = 1.0 / scale if scale > 0 else 1.0
    box = (
        int(round(left * inv)),
        int(round(top * inv)),
        int(round(right * inv)),
        int(round(bottom * inv)),
    )
    return {
        "x": box[0],
        "y": box[1],
        "w": box[2] - box[0],
        "h": box[3] - box[1],
        "score": round(best_score, 4),
        "method": "skin_component_v1",
    }


def derive_face_anchor(
    source_path: str,
    output_path: str,
    *,
    min_size: int = 192,
) -> IdentityAnchor | None:
    """从完整设定图派生干净身份锚点；失败时不写半成品。"""
    if not source_path or not os.path.isfile(source_path):
        return None
    bbox_info = detect_face_bbox(source_path)
    if not bbox_info:
        return None

    try:
        from PIL import Image
    except Exception:
        return None

    try:
        with Image.open(source_path) as im:
            src = im.convert("RGB")
            x, y = bbox_info["x"], bbox_info["y"]
            w, h = bbox_info["w"], bbox_info["h"]
            crop = src.crop((x, y, x + w, y + h))
            if min(crop.size) < min_size:
                factor = min_size / max(1, min(crop.size))
                crop = crop.resize(
                    (max(min_size, int(round(crop.width * factor))),
                     max(min_size, int(round(crop.height * factor)))),
                    Image.LANCZOS,
                )
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(output_path), prefix=".identity_face_", suffix=".png"
            )
            os.close(fd)
            try:
                crop.save(tmp, format="PNG")
                os.replace(tmp, output_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        return None

    return IdentityAnchor(
        character_id="",
        path=os.path.abspath(output_path),
        source_path=os.path.abspath(source_path),
        source_sha256=_file_sha256(source_path),
        quality="derived_face_v1",
        bbox=(x, y, x + w, y + h),
    )


def _read_manifest(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ensure_identity_assets(
    asset_dir: str,
    source_path: str | None = None,
    *,
    character_id: str | None = None,
    logger=None,
    force: bool = False,
) -> dict:
    """幂等建立角色身份资产。

    完整 ``base.png`` 永远不被修改；派生失败时返回 ``status=source_only``，
    调用方继续回退完整设定图，绝不因为身份锚点派生失败阻断资产生成。
    """
    asset_dir = str(asset_dir or "")
    source = os.path.abspath(
        source_path or os.path.join(asset_dir, "base.png")
    )
    cid = character_id or stable_character_id(asset_dir)
    out_dir = identity_directory(asset_dir)
    face_path = os.path.join(out_dir, FACE_ANCHOR_FILENAME)
    manifest_path = identity_manifest_path(asset_dir)

    if not os.path.isfile(source) or os.path.getsize(source) <= 0:
        return {
            "ok": False,
            "status": "source_missing",
            "character_id": cid,
            "source_path": source,
            "face_path": "",
            "reason": "角色 base.png 不存在或为空",
        }

    source_sha = _file_sha256(source)
    manifest = _read_manifest(manifest_path)
    existing_face = identity_anchor_path(asset_dir)
    if (
        not force
        and existing_face
        and manifest.get("source_sha256") == source_sha
        and manifest.get("character_id") == cid
    ):
        return {
            "ok": True,
            "status": str(manifest.get("status") or "ready"),
            "character_id": cid,
            "source_path": source,
            "face_path": existing_face,
            "quality": str(manifest.get("quality") or "existing"),
            "manifest_path": manifest_path,
        }

    # 源图已变化但新锚点派生失败时，必须删除旧锚点，避免继续使用过期身份。
    if existing_face:
        try:
            os.remove(existing_face)
        except OSError:
            pass

    anchor = derive_face_anchor(source, face_path)
    if anchor:
        result = {
            "ok": True,
            "status": "ready",
            "character_id": cid,
            "source_path": source,
            "face_path": anchor.path,
            "quality": anchor.quality,
            "bbox": list(anchor.bbox or ()),
        }
    else:
        result = {
            "ok": True,
            "status": "source_only",
            "character_id": cid,
            "source_path": source,
            "face_path": "",
            "quality": "full_sheet_fallback",
            "reason": "未找到可靠面部候选；保留完整设定图作为回退，不生成错误裁剪",
        }

    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "character_id": cid,
        "status": result["status"],
        "quality": result["quality"],
        "source_path": source,
        "source_sha256": source_sha,
        "source_size": list(_image_size(source)),
        "anchors": {"face": result.get("face_path", "")},
        "bbox": result.get("bbox") or None,
        "generated_by": "identity_contract_v1",
        "auto_derived": True,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if result.get("reason"):
        payload["reason"] = result["reason"]
    try:
        _atomic_write_json(manifest_path, payload)
        result["manifest_path"] = manifest_path
    except Exception as exc:
        result["manifest_error"] = f"{type(exc).__name__}: {exc}"
        if logger:
            logger.warning("角色身份清单写入失败（不影响资产入库）：%s", exc)

    if logger:
        logger.info(
            "角色身份资产：%s status=%s quality=%s face=%s",
            cid,
            result["status"],
            result["quality"],
            os.path.basename(result.get("face_path", "")) or "无（回退完整设定图）",
        )
    return result


def _image_size(path: str) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


def is_identity_reference(ref) -> bool:
    """判断旧版三元组参考图是否承载人物身份。"""
    if not isinstance(ref, (list, tuple)) or len(ref) < 2:
        return False
    kind = str(ref[0] or "")
    label = str(ref[1] or "")
    return kind in IDENTITY_REFERENCE_KINDS or "身份锚点" in label


def should_unify_reference(ref) -> bool:
    """输出画幅 cover 归一只处理画布/场景/道具，不处理身份锚点。"""
    return not is_identity_reference(ref)


def promote_canvas_reference(
    refs: Sequence,
    blocking_ref: str = "",
    blocking_label: str = DEFAULT_BLOCKING_LABEL,
) -> list:
    """把画布型参考图提升到 ``<image1>``，人物身份锚点后移。

    优先级：
      1. 3D 构图基准（若提供）
      2. 场景参考
      3. 任一非身份参考
      4. 只有人物参考时保持原顺序（兼容无人物/特殊模板）
    """
    items = [r for r in (refs or []) if isinstance(r, (list, tuple)) and len(r) >= 3]
    if blocking_ref:
        items = [
            r
            for r in items
            if blocking_label not in str(r[1] or "") and blocking_label not in str(r[0] or "")
        ]
        items.insert(0, (blocking_ref, blocking_label, blocking_ref))
        return items
    if not items:
        return items

    scene_idx = next((i for i, r in enumerate(items) if str(r[0] or "") == "场景"), None)
    if scene_idx is not None:
        canvas = items.pop(scene_idx)
        items.insert(0, canvas)
        return items

    non_identity_idx = next(
        (i for i, r in enumerate(items) if not is_identity_reference(r)), None
    )
    if non_identity_idx is not None:
        canvas = items.pop(non_identity_idx)
        items.insert(0, canvas)
    return items

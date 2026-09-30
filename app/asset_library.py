# -*- coding: utf-8 -*-
"""跨项目角色资产库 —— 形象指纹复用（借鉴 NiliX 的定妆照「指纹复用、零渲染」）

为什么可以复用
------------
角色设定图是**纯确定性**产物：同一段外貌描述 + 同一风格 + 同一画幅 + 同一模板，
目标形象就应当是同一张图。此前每个项目都要重新渲染一遍 —— 同一角色的多项目复用、
同一部小说的续集，全在重复烧 GPU。

指纹口径（关键，别随手改）
------------------------
    sha256( 版本号 + 模板名 + 规范化提示词 + 风格 + 画幅 + 资产类型 )

* **风格必须进指纹**：否则「国漫风」的角色图会被复用进「写实风」项目，直接把画风
  带错 —— 这是最坏的一类静默错，比多烧一次 GPU 严重得多。
* **模板名进指纹**：换了角色生成工作流（如 Qwen 2512 → 2.1）后旧库不再匹配。
* **种子不进指纹**：这是刻意的 —— 复用的目的本就是「同一形象不再重渲」。
  代价是「想换一版形象」时也会命中；出口有两条：forget() 主动清库，
  或关掉 ASSET_LIBRARY_ENABLED。
* FINGERPRINT_VERSION 由维护者在上游链路语义变更时手动 +1，避免旧库被误用。

产物布局
-------
    <PROJECT_OUTPUT_DIR>/asset_lib/characters/<指纹>/manifest.json
    <PROJECT_OUTPUT_DIR>/asset_lib/characters/<指纹>/view_0.png ...

一切失败 **fail-open**：库损坏 / 写失败只降级为「本次不复用」，绝不阻断出图。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from typing import Any, Dict, List, Optional

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

__all__ = ["FINGERPRINT_VERSION", "library_root", "fingerprint", "enabled",
           "lookup", "store", "forget", "stats"]

#: 指纹版本号。**上游链路语义变更（换模型/换版式/换清洗口径）时必须 +1**，
#: 否则旧库里的图会被当成「同一形象」复用出去。
# 2026-09-30：v1→v2。服装/设定图变更后旧指纹仍命中旧图（灰袍被当黑袍复用），
# 导致分镜参考图带错服装 —— 跨镜连续性断裂。语义变更即升版本，让旧库彻底失效。
FINGERPRINT_VERSION = 2


def library_root() -> str:
    """库根目录：<PROJECT_OUTPUT_DIR>/asset_lib/<kind>。惰性读 config 便于测试注入。"""
    from config import PROJECT_OUTPUT_DIR
    return os.path.join(PROJECT_OUTPUT_DIR, "asset_lib", "characters")


def enabled() -> bool:
    """资产库开关（默认开）。读取失败一律按**关闭**处理：出图优先于省算力。"""
    try:
        from config import ASSET_LIBRARY_ENABLED
        return bool(ASSET_LIBRARY_ENABLED)
    except Exception as e:                       # noqa: BLE001
        logger.debug("资产库开关读取失败，按关闭处理：%s", e)
        return False


def _norm(text: Any) -> str:
    """规范化提示词：折叠空白 + 去首尾。"""
    return " ".join(str(text or "").split()).strip()


def fingerprint(*, template: str, prompt: str, style: str = "",
                size: Any = None, kind: str = "character") -> str:
    """形象指纹（32 位十六进制）。任何一项变化都会得到不同指纹。"""
    try:
        size_norm = [int(v) for v in (size or [])] if size else None
    except (TypeError, ValueError):
        size_norm = str(size)
    payload = {
        "v": FINGERPRINT_VERSION,
        "kind": str(kind or ""),
        "template": os.path.basename(str(template or "")),
        "prompt": _norm(prompt),
        "style": _norm(style),
        "size": size_norm,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:32]


def _manifest_path(fp: str) -> str:
    return os.path.join(library_root(), fp, "manifest.json")


def _valid(paths: List[str]) -> List[str]:
    """只认「存在且非空」的产物 —— 被清理过一半的库不能当命中。"""
    out = []
    for p in paths or []:
        try:
            if p and os.path.isfile(p) and os.path.getsize(p) > 0:
                out.append(p)
        except OSError:
            continue
    return out


def lookup(fp: str) -> List[str]:
    """按指纹查库。命中返回**绝对路径列表**；未命中/库损坏返回空列表（fail-open）。"""
    if not fp:
        return []
    try:
        man = read_json_strict(_manifest_path(fp), {}) or {}
        files = man.get("files") or []
        if not isinstance(files, list):
            return []
        root = os.path.join(library_root(), fp)
        abs_files = [f if os.path.isabs(str(f)) else os.path.join(root, os.path.basename(str(f)))
                     for f in files]
        good = _valid(abs_files)
        if len(good) != len(files):
            logger.warning("资产库命中但产物缺失（%d/%d），按未命中处理：%s",
                           len(good), len(files), fp)
            return []
        return good
    except Exception as e:                       # noqa: BLE001
        logger.warning("资产库读取失败（按未命中处理）：%s", e)
        return []


def _link_or_copy(src: str, dst: str) -> None:
    """优先硬链（同卷省磁盘），失败退回复制。

    Windows 上跨卷 / 非 NTFS 会让 os.link 抛 OSError，这里必须能退回去。
    """
    try:
        if os.path.exists(dst):
            os.remove(dst)
        os.link(src, dst)
        return
    except OSError:
        pass
    shutil.copy2(src, dst)


def store(fp: str, files: List[str], *, meta: Optional[Dict[str, Any]] = None) -> List[str]:
    """把产物入库（硬链优先），返回库内路径列表；任何失败返回空列表且不影响出图。

    ⚠️ 用硬链而不是移动：调用方在拿到产物后还会 shutil.move 走它（见 app.py 资产链路），
    移动只改目录项、不会动库里的另一条链。文件被删时链接数归零，库条目自动失效并被
    lookup 的「存在且非空」判据挡掉。
    """
    if not fp:
        return []
    try:
        root = os.path.join(library_root(), fp)
        os.makedirs(root, exist_ok=True)
        kept: List[str] = []
        for i, src in enumerate(files or []):
            if not (src and os.path.isfile(src)):
                continue
            ext = os.path.splitext(src)[1] or ".png"
            dst = os.path.join(root, "view_%d%s" % (i, ext))
            try:
                _link_or_copy(src, dst)
            except OSError as e:
                logger.warning("资产入库失败（跳过该文件）：%s → %s：%s", src, dst, e)
                continue
            kept.append(os.path.basename(dst))
        if not kept:
            return []
        man = {"fingerprint": fp, "v": FINGERPRINT_VERSION,
               "files": kept, "meta": dict(meta or {})}
        atomic_write_json(_manifest_path(fp), man)
        logger.info("角色资产已入库：指纹 %s（%d 个文件）", fp, len(kept))
        return [os.path.join(root, f) for f in kept]
    except Exception as e:                       # noqa: BLE001
        logger.warning("角色资产入库失败（不影响出图）：%s", e)
        return []


def forget(fp: str) -> bool:
    """删除某指纹的库条目（用户想换一版形象时用）。返回是否真的删掉了东西。"""
    if not fp:
        return False
    try:
        d = os.path.join(library_root(), fp)
        if not os.path.isdir(d):
            return False
        shutil.rmtree(d, ignore_errors=True)
        logger.info("角色资产库条目已删除：%s", fp)
        return True
    except Exception as e:                       # noqa: BLE001
        logger.warning("删除资产库条目失败：%s", e)
        return False


def stats() -> dict:
    """库概况（诊断用）。"""
    root = library_root()
    entries, files = 0, 0
    try:
        if os.path.isdir(root):
            for name in os.listdir(root):
                if os.path.isdir(os.path.join(root, name)):
                    entries += 1
                    files += len(_valid([os.path.join(root, name, f)
                                         for f in os.listdir(os.path.join(root, name))]))
    except OSError as e:
        logger.debug("资产库统计失败：%s", e)
    return {"root": root, "enabled": enabled(), "entries": entries, "files": files}

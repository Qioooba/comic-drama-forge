# -*- coding: utf-8 -*-
"""工作流模板副本完整性核对（P0-4）。

程序可能从多个位置读取 workflows/：源码仓库、桌面安装区、资源镜像和 ComfyUI
目录。当前副本一致时无需打扰用户；一旦 hash 漂移，必须在启动日志和状态 API
中明确指出“实际读取路径”和各副本 hash，避免静默用错工作流。
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Callable, Dict, Iterable, List, Optional

import config


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path))) if path else ""


def _split_env_paths(raw: str) -> List[str]:
    if not raw:
        return []
    # Windows 用分号，POSIX 用冒号；同时兼容两种写法。
    parts = []
    for chunk in str(raw).replace(";", os.pathsep).split(os.pathsep):
        chunk = chunk.strip().strip('"')
        if chunk:
            parts.append(os.path.normpath(chunk))
    return parts


def candidate_roots() -> List[str]:
    """返回实际存在、应参与核对的工作流目录（去重保序）。"""
    roots: List[str] = []

    def add(path: str) -> None:
        if not path:
            return
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            return
        key = _norm(path)
        if key not in {_norm(x) for x in roots}:
            roots.append(path)

    # 桌面版 Electron 会显式传入安装区/镜像；开发态默认就是源码仓库。
    add(os.getenv("MJSCXT_WORKFLOW_CANONICAL_DIR", ""))
    add(getattr(config, "WORKFLOW_CANONICAL_DIR", config.PROJECT_WORKFLOWS_DIR))
    add(config.PROJECT_WORKFLOWS_DIR)
    add(os.getenv("MJSCXT_WORKFLOW_INSTALL_DIR", ""))
    add(os.getenv("MJSCXT_WORKFLOW_MIRROR_DIR", ""))
    for raw in (
        os.getenv("MJSCXT_WORKFLOW_EXTRA_DIRS", ""),
        os.getenv("MJSCXT_WORKFLOWS_DIR", ""),
    ):
        for path in _split_env_paths(raw):
            add(path)

    # 旧配置的解析顺序仍然要纳入核对，确保不会漏掉 ComfyUI 目录副本。
    try:
        for path in config.workflow_search_dirs():
            add(path)
    except Exception:  # noqa: BLE001  配置异常不应让核对失败
        pass

    # 桌面版数据根的兄弟目录通常就是 resource-mirror；没有则自然跳过。
    data_root = getattr(config, "PROJECT_DATA_DIR", "")
    if data_root:
        add(os.path.join(os.path.dirname(os.path.abspath(data_root)),
                         "resource-mirror", "workflows"))
    return roots


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_files() -> List[str]:
    values = []
    for value in (getattr(config, "WORKFLOW_TEMPLATE", {}) or {}).values():
        name = os.path.basename(str(value or ""))
        if name and name not in values:
            values.append(name)
    return sorted(values)


def _actual_resolver() -> Callable[[str], str]:
    def resolve(name: str) -> str:
        try:
            return config.resolve_workflow_path(name)
        except Exception:  # noqa: BLE001
            return ""
    return resolve


def check(files: Optional[Iterable[str]] = None,
          roots: Optional[Iterable[str]] = None,
          actual_resolver: Optional[Callable[[str], str]] = None) -> Dict:
    """核对全部工作流副本。

    返回 ``ok``、``canonical_root``、``actual_root``、每个文件的实际路径/hash、
    各候选副本 hash、状态与人类可读 warnings。状态：
      consistent / hash_mismatch / missing_copy / actual_missing。
    """
    names = list(files) if files is not None else _default_files()
    roots_in = list(roots) if roots is not None else candidate_roots()
    root_paths = [os.path.normpath(str(r)) for r in roots_in if r]
    canonical = (os.getenv("MJSCXT_WORKFLOW_CANONICAL_DIR", "")
                 or getattr(config, "WORKFLOW_CANONICAL_DIR", "")
                 or config.PROJECT_WORKFLOWS_DIR)
    if not os.path.isdir(canonical):
        canonical = config.PROJECT_WORKFLOWS_DIR
    if roots is None and canonical not in root_paths and os.path.isdir(canonical):
        root_paths.insert(0, canonical)
    resolver = actual_resolver or _actual_resolver()

    file_reports: List[Dict] = []
    warnings: List[str] = []
    for name in names:
        actual = os.path.normpath(resolver(name)) if resolver else ""
        actual_exists = bool(actual and os.path.isfile(actual))
        candidates = []
        hashes = {}
        for root in root_paths:
            path = os.path.normpath(os.path.join(root, name))
            exists = os.path.isfile(path)
            entry = {
                "root": root,
                "path": path,
                "root_exists": os.path.isdir(root),
                "exists": exists,
                "hash": "",
                "is_actual": bool(actual and _norm(path) == _norm(actual)),
            }
            if exists:
                try:
                    entry["hash"] = _sha256(path)
                    hashes[entry["hash"]] = hashes.get(entry["hash"], 0) + 1
                except OSError as exc:
                    entry["error"] = str(exc)
            candidates.append(entry)

        existing = [c for c in candidates if c["exists"]]
        missing_copy = any(c["root_exists"] and not c["exists"] for c in candidates)
        if not actual_exists:
            status = "actual_missing"
        elif len(hashes) > 1:
            status = "hash_mismatch"
        elif missing_copy:
            status = "missing_copy"
        else:
            status = "consistent"
        actual_hash = ""
        if actual_exists:
            try:
                actual_hash = _sha256(actual)
            except OSError:
                actual_hash = ""
        report = {
            "file": name,
            "actual_path": actual,
            "actual_exists": actual_exists,
            "actual_hash": actual_hash,
            "status": status,
            "ok": status == "consistent",
            "hashes": hashes,
            "candidates": candidates,
        }
        file_reports.append(report)

        if status == "hash_mismatch":
            short = {h[:12]: count for h, count in hashes.items()}
            warnings.append(
                f"工作流 hash 不一致：{name}；实际={actual or '(未找到)'}；"
                f"hash={actual_hash[:12] or '(不可读)'}；候选={short}"
            )
        elif status == "missing_copy":
            missing = [c["root"] for c in candidates
                       if c["root_exists"] and not c["exists"]]
            warnings.append(
                f"工作流副本缺失：{name}；实际={actual or '(未找到)'}；"
                f"缺失目录={missing}"
            )
        elif status == "actual_missing":
            warnings.append(f"工作流文件缺失：{name}；查找路径={actual or '(未解析)'}")

    actual_roots = []
    for report in file_reports:
        for candidate in report["candidates"]:
            if candidate["is_actual"] and candidate["root"] not in actual_roots:
                actual_roots.append(candidate["root"])
    return {
        "success": True,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "canonical_root": canonical,
        "actual_root": actual_roots[0] if actual_roots else "",
        "roots": root_paths,
        "files": file_reports,
        "warnings": warnings,
        "ok": not warnings,
    }

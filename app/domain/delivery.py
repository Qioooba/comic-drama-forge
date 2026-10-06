# -*- coding: utf-8 -*-
"""交付包领域模型：交付预设、SHA-256 校验、人工批准闸门。

这个模块是**纯领域**：不 import flask、不碰数据库、不读环境变量，
只负责「规则」与「判定」，因此可以被单元测试直接调用，也被
:mod:`app.infrastructure.delivery_repo` 与 :mod:`app.api.delivery` 复用。

为什么要有这一层
----------------
本项目此前的「导出」只有产物生成（``app/nle_export.py``：SRT / 剪映 /
FCPXML / 帧清单），**没有交付语义**：不知道交付的是哪一档画幅、不知道
交付包由哪些文件构成、更不知道这些文件有没有被改过。于是：

* 交付方拿到一份「导出目录的拷贝」，无法判断它是不是最新的一次导出；
* 没有任何人工批准动作，机器跑完就算「完成」——这违反了协调书 §4
  「采用 ≠ 批准」：**机器检查通过永远不等于人工批准**。

三条不变量（与 ``docs/decisions/改造契约_并行协调书.md`` §4 一致）
----------------------------------------------------------------
1. **采用 ≠ 批准**：机器校验（``verified``）与人工批准（``approved``）
   是两个独立动作，永不互相隐式升级。
2. **批准带哈希绑定**：批准记录必须携带批准当时的 :func:`compute_package_hash`；
   交付包内容一变（任一文件增删改，或换预设），哈希即不匹配，
   批准**自动失效**，且失效不需要任何后台任务去撤销它。
3. **批准必须有操作者**：``approver`` 为空的批准请求一律拒绝
   （错误码 :data:`DLV_APPROVER_REQUIRED`）。

状态机
------
::

    built ──(机器校验通过)──> verified ──(人工批准且哈希匹配)──> approved
      │                          │                                │
      └── 文件缺失/哈希漂移 ──────┴── 内容变化后批准自动失效 ──> approval_invalidated

「机器校验通过」只把状态推到 ``verified``，**永远不会**推到 ``approved``。
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DELIVERY_PRESETS",
    "DELIVERY_SCHEMA_VERSION",
    "DELIVERY_SKIP_SUFFIXES",
    "DELIVERY_ERROR_CODES",
    "DLV_EMPTY_PACKAGE",
    "DLV_FILE_MISSING",
    "DLV_HASH_MISMATCH",
    "DLV_APPROVAL_STALE",
    "DLV_APPROVER_REQUIRED",
    "DLV_APPROVER_NOT_HUMAN",
    "DLV_ALREADY_APPROVED",
    "DLV_UNKNOWN_PACKAGE",
    "DLV_NOT_BUILT",
    "DLV_NOT_RELEASE_READY",
    "release_ready",
    "STATUS_BUILT",
    "STATUS_VERIFIED",
    "STATUS_APPROVED",
    "STATUS_INVALIDATED",
    "sha256_file",
    "sha256_bytes",
    "get_preset",
    "list_presets",
    "iter_root_files",
    "compute_file_entry",
    "compute_package_hash",
    "recompute_package_hash",
    "verify_files",
    "evaluate_approval",
    "derive_status",
    "can_approve",
    "now_iso",
]

#: 交付包 schema 版本。哈希计算会把它纳入摘要，改了它 ⇒ 旧批准全部失效。
DELIVERY_SCHEMA_VERSION = 1

#: 收集与校验时**都**跳过的临时/半成品后缀。
#:
#: 为什么两侧必须同口径：建包时不算、重算时算进来（或反过来），
#: 会让交付包「刚建完就哈希漂移」，把真失效与假失效混成一个，
#: 人工批准闸门就会被人当成噪声绕过去。
DELIVERY_SKIP_SUFFIXES: Tuple[str, ...] = (".part", ".tmp", ".crdownload")

# --------------------------------------------------------------------------
# 错误码（R2：不改任何既有报错文案；既有 code 体系是 ``F-*``，
# 这里另起 ``DLV-*`` 命名空间，避免与 app/failure_codes.py 冲突）
# --------------------------------------------------------------------------

DLV_EMPTY_PACKAGE = "DLV-EMPTY-PACKAGE"
DLV_FILE_MISSING = "DLV-FILE-MISSING"
DLV_HASH_MISMATCH = "DLV-HASH-MISMATCH"
DLV_APPROVAL_STALE = "DLV-APPROVAL-STALE"
DLV_APPROVER_REQUIRED = "DLV-APPROVER-REQUIRED"
DLV_APPROVER_NOT_HUMAN = "DLV-APPROVER-NOT-HUMAN"
DLV_ALREADY_APPROVED = "DLV-ALREADY-APPROVED"
DLV_UNKNOWN_PACKAGE = "DLV-UNKNOWN-PACKAGE"
DLV_NOT_BUILT = "DLV-NOT-BUILT"
DLV_NOT_RELEASE_READY = "DLV-NOT-RELEASE-READY"

#: 错误码 → 中文说明。**这些文案是对外契约，前端会直接展示给用户**，
#: 与 ``app/failure_codes.py`` 的 ``F-*`` 同性质，改动前必须先改契约。
DELIVERY_ERROR_CODES: Dict[str, str] = {
    DLV_EMPTY_PACKAGE: "交付包内没有任何文件",
    DLV_FILE_MISSING: "交付文件缺失",
    DLV_HASH_MISMATCH: "交付文件内容已变化，与清单记录的 SHA-256 不一致",
    DLV_APPROVAL_STALE: "交付包内容已变化，此前的人工批准已自动失效，请重新校验并批准",
    DLV_APPROVER_REQUIRED: "缺少操作者，无法记录人工批准",
    DLV_APPROVER_NOT_HUMAN: "批准主体不是人工主体，机器账号不能批准交付",
    DLV_ALREADY_APPROVED: "该交付包已被批准",
    DLV_UNKNOWN_PACKAGE: "交付包不存在",
    DLV_NOT_BUILT: "交付包尚未生成",
    DLV_NOT_RELEASE_READY: "交付包尚未达到发布条件（授权门禁、机器校验或人工批准未同时成立）",
}

# --------------------------------------------------------------------------
# 交付预设（P1-7 缺的「交付预设」）
# --------------------------------------------------------------------------

#: 交付预设。键即 ``preset_id``。
#:
#: ``width``/``height`` 是**目标画幅**，用于给下游剪辑软件一个明确的成片规格；
#: 它不改变既有导出产物的内容（导出归 ``nle_export`` 管），只作为交付包的
#: 声明性元数据 —— 并且**参与哈希计算**，换预设会让旧批准失效。
DELIVERY_PRESETS: Dict[str, Dict[str, Any]] = {
    "portrait_9x16": {
        "preset_id": "portrait_9x16",
        "label": "竖屏 9:16",
        "width": 1080,
        "height": 1920,
        "aspect_ratio": "9:16",
        "orientation": "portrait",
        "note": "抖音 / 快手 / 视频号竖屏投放",
    },
    "landscape_16x9": {
        "preset_id": "landscape_16x9",
        "label": "横屏 16:9",
        "width": 1920,
        "height": 1080,
        "aspect_ratio": "16:9",
        "orientation": "landscape",
        "note": "B 站 / YouTube 横屏投放",
    },
    "portrait_3x4": {
        "preset_id": "portrait_3x4",
        "label": "竖屏 3:4",
        "width": 1080,
        "height": 1440,
        "aspect_ratio": "3:4",
        "orientation": "portrait",
        "note": "漫画阅读器 / 图文平台常用比例",
    },
}

DEFAULT_PRESET_ID = "portrait_9x16"

# 状态常量
STATUS_BUILT = "built"
STATUS_VERIFIED = "verified"
STATUS_APPROVED = "approved"
STATUS_INVALIDATED = "approval_invalidated"


def now_iso() -> str:
    """统一时间格式（秒级，本地时区）。全链路只用这一个时间源，便于排序比对。"""
    return datetime.now().replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# 哈希
# --------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    """字节 → 小写十六进制 SHA-256。"""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, _chunk: int = 1024 * 1024) -> str:
    """文件 → 小写十六进制 SHA-256（分块读，交付包里的视频可能很大）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(_chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def iter_root_files(root: str) -> List[Tuple[str, str]]:
    """扫描交付根目录，返回 ``[(相对路径, 绝对路径), ...]``（按相对路径升序）。

    只读扫描：目录不存在时返回空列表。跳过 :data:`DELIVERY_SKIP_SUFFIXES`
    的临时/半成品后缀（跳过口径必须与建包时一致，见该常量注释）。

    为什么放在领域层：**建包**（列基线文件）与**重算**（拿磁盘现状比）必须
    用同一套遍历规则，否则两边看到的文件集合不同，「增删改」就会被算成漂移。
    """
    if not root or not os.path.isdir(root):
        return []
    out: List[Tuple[str, str]] = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(DELIVERY_SKIP_SUFFIXES):
                continue
            abs_path = os.path.join(dirpath, fn)
            if not os.path.isfile(abs_path):
                continue
            rel = os.path.relpath(abs_path, root).replace("\\", "/")
            out.append((rel, abs_path))
    out.sort(key=lambda pair: pair[0])
    return out


def compute_file_entry(abs_path: str, rel_path: str) -> Dict[str, Any]:
    """给一个已存在的文件生成一条清单条目（含 SHA-256）。

    ``rel_path`` 是写进交付清单的相对路径（对 ``abs_path`` 而言），
    必须**与绝对路径无关**，否则换一个机器/目录就全盘漂移。
    """
    return {
        "rel_path": str(rel_path or "").replace("\\", "/"),
        "size_bytes": int(os.path.getsize(abs_path)),
        "sha256": sha256_file(abs_path),
        "mtime": datetime.fromtimestamp(os.path.getmtime(abs_path)).isoformat(timespec="seconds"),
    }


def compute_package_hash(preset_id: str, files: Sequence[Dict[str, Any]],
                         schema_version: int = DELIVERY_SCHEMA_VERSION) -> str:
    """交付包的**规范摘要**（package_hash）——人工批准就绑定它。

    摘要输入（按固定顺序拼接后再哈希，保证同内容同摘要）：

    1. ``schema_version``（交付包 schema 演进会让旧批准失效）
    2. ``preset_id``（换交付画幅 = 换了交付物 ⇒ 必须重新批准）
    3. 全部文件条目：``rel_path`` + ``:`` + ``sha256``，**按 ``rel_path`` 升序**

    刻意**不包含**文件大小与 mtime：mtime 会因为「碰一下文件」「跨机器
    拷贝」而变化，那是假漂移；内容才是交付物本身。
    """
    lines = ["schema_version={}".format(int(schema_version)),
             "preset_id={}".format(preset_id)]
    for entry in sorted(files or [], key=lambda e: str(e.get("rel_path") or "")):
        lines.append("{}:{}".format(entry.get("rel_path") or "", entry.get("sha256") or ""))
    return sha256_bytes("\n".join(lines).encode("utf-8"))


# --------------------------------------------------------------------------
# 预设
# --------------------------------------------------------------------------

def list_presets() -> List[Dict[str, Any]]:
    """返回全部交付预设（稳定顺序：竖屏 → 横屏 → 3:4）。"""
    return [dict(v) for v in DELIVERY_PRESETS.values()]


def get_preset(preset_id: str) -> Dict[str, Any]:
    """按 id 取预设；未知 id 回落到默认竖屏预设（与既有「非法值回落默认」风格一致）。"""
    return dict(DELIVERY_PRESETS.get(str(preset_id or "").strip(), DELIVERY_PRESETS[DEFAULT_PRESET_ID]))


# --------------------------------------------------------------------------
# 校验（机器层）
# --------------------------------------------------------------------------

def verify_files(files: Iterable[Dict[str, Any]], root: str = "") -> Dict[str, Any]:
    """逐个文件重算 SHA-256 并与清单比对。

    ``root`` 是清单里相对路径的解析根目录。返回
    ``{"ok", "checked", "missing", "mismatched", "details", "violations"}``；
    ``violations`` 是可直接透给前端的 ``{code, message, file}`` 列表。
    """
    details: List[Dict[str, Any]] = []
    missing: List[str] = []
    mismatched: List[Dict[str, str]] = []
    violations: List[Dict[str, Any]] = []
    checked = 0

    for entry in files or []:
        rel = str(entry.get("rel_path") or "").replace("\\", "/")
        abs_path = os.path.join(root, rel) if root else rel
        row: Dict[str, Any] = {"rel_path": rel, "expected_sha256": entry.get("sha256") or ""}
        if not os.path.isfile(abs_path):
            row["status"] = "missing"
            missing.append(rel)
            violations.append({"code": DLV_FILE_MISSING,
                               "message": DELIVERY_ERROR_CODES[DLV_FILE_MISSING],
                               "file": rel})
        else:
            actual = sha256_file(abs_path)
            row["actual_sha256"] = actual
            if actual == (entry.get("sha256") or ""):
                row["status"] = "ok"
                checked += 1
            else:
                row["status"] = "mismatch"
                mismatched.append({"rel_path": rel, "expected": entry.get("sha256") or "",
                                   "actual": actual})
                violations.append({"code": DLV_HASH_MISMATCH,
                                   "message": DELIVERY_ERROR_CODES[DLV_HASH_MISMATCH],
                                   "file": rel})
        details.append(row)

    return {
        "ok": not missing and not mismatched and bool(details),
        "checked": checked,
        "missing": missing,
        "mismatched": mismatched,
        "details": details,
        "violations": violations,
    }


# --------------------------------------------------------------------------
# 批准（人工层）
# --------------------------------------------------------------------------

def recompute_package_hash(files: Sequence[Dict[str, Any]], root: str, preset_id: str,
                           schema_version: int = DELIVERY_SCHEMA_VERSION) -> str:
    """按**磁盘现状**重算包摘要；任一文件缺失或不可读则返回 ``""``。

    为什么必须有这个函数
    ------------------
    :func:`compute_package_hash` 算的是「清单里记的是什么」—— 那是**基线**，
    建包之后再也不会变。如果拿批准去跟这个基线比，那么
    **文件被人改了，批准照样显示有效**，人工批准闸门形同虚设。

    「内容变没变」只能问磁盘：把每个文件重算 SHA-256，用同样的规范算法重算
    包摘要，再和批准时绑定的那个摘要比。这就是 :func:`evaluate_approval`
    落在 ``stale`` 分支的输入（由仓储层算好放进 ``pkg['current_package_hash']``）。

    ⚠️ 这里**必须**每次全量重算，**不允许**任何形式的摘要缓存
    -----------------------------------------------------------
    曾经用 ``(绝对路径, size, mtime_ns)`` 当缓存键，并注释称
    「文件一改键就变、不会出现用旧摘要判成没改」。**那个说法是错的**：
    等长改内容再还原 mtime（``robocopy /COPY:DAT``、``docker cp``、
    ``shutil.copy2``、``tar -x`` 默认都保留时间戳）就会命中同一把键，
    于是「磁盘现状」被缓存替换成建包时的旧摘要 —— 批准在内容被换掉后
    依然显示有效。铁律「产物内容一变，批准自动失效」在这一条路上被完全绕过。
    缓存键能观察到的元数据（大小 / 时间戳）**不是**内容，任何基于它们的
    缓存都无法支撑「哈希绑定」这条安全不变量。要省 IO，只能在展示层做，
    绝不能进门禁判定路径。

    「新增」也必须被覆盖
    ------------------
    只重算清单内的文件的话，往导出目录里**塞一个新文件**不会改变包摘要，
    批准照样有效 —— 铁律写的「增删改」里的「增」就落空了。因此这里
    除了清单内文件，还把 ``root`` 目录里**多出来的**文件一并计入摘要
    （跳过口径与建包时相同，见 :func:`iter_root_files`）。
    """
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in files or []:
        rel = str(entry.get("rel_path") or "").replace("\\", "/")
        abs_path = os.path.join(root, rel) if root else rel
        if not os.path.isfile(abs_path):
            return ""                     # 清单内文件被删 → 磁盘现状不可判定
        try:
            digest = sha256_file(abs_path)
        except OSError:
            # 不可读同样是不可判定：绝不能因为「读不出来」就当成「没变」
            # （文件被换成了目录、被占用、被删掉都会走到这里）。
            return ""
        seen.add(rel)
        rows.append({"rel_path": rel, "sha256": digest})

    for rel, abs_path in iter_root_files(root):
        if rel in seen:
            continue
        try:
            rows.append({"rel_path": rel, "sha256": sha256_file(abs_path)})
        except OSError:
            return ""
    return compute_package_hash(preset_id, rows, schema_version)


def evaluate_approval(pkg: Dict[str, Any], approval: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """判定「人工批准是否仍然有效」。

    **失效判据只有一条**：``approval.package_hash != 磁盘现状包摘要``。
    内容变过（清单内文件改 / 删、目录里**新增**文件、换预设、改 schema 版本），
    批准立即失效，且失效是**纯函数**、不需要任何后台任务去撤销——这正是
    把它做成哈希绑定而不是布尔位的理由（布尔位需要有人记得去撤销，通常没人记得）。

    返回 ``{"state", "approver", "approved_at", "reason", "note"}``；
    ``state`` ∈ ``none`` / ``valid`` / ``stale`` / ``revoked`` / ``hash_missing``。
    """
    if not approval:
        return {"state": "none", "approver": "", "approved_at": "",
                "reason": "", "note": ""}

    base = {
        "approver": str(approval.get("approver") or ""),
        "approved_at": str(approval.get("approved_at") or ""),
        "reason": str(approval.get("reason") or ""),
        "note": str(approval.get("note") or ""),
    }

    # 显式撤销优先于哈希判定（撤销是人工动作，不该被内容变化「复活」）。
    if approval.get("revoked"):
        return dict(base, state="revoked", reason="人工批准已被撤销")

    approved_hash = str(approval.get("package_hash") or "")
    if not approved_hash:
        return dict(base, state="hash_missing",
                    reason=DELIVERY_ERROR_CODES[DLV_APPROVAL_STALE])

    # ⚠ 必须比「磁盘现状」，不是清单基线 —— 否则改了文件批准依然有效。
    # ⚠⚠ **磁盘现状为空时也绝不能回落到基线**：recompute_package_hash 在
    # 「有文件缺失 / 不可读」时返回 ""，此时回落 package_hash 恰好等于批准
    # 绑定的值，于是「文件被删光」会被判成「批准依然有效」—— 删除是最严重
    # 的变更，却恰恰因为算不出现状而免于失效。这里 fail-closed。
    current_hash = str(pkg.get("current_package_hash") or "")
    if not current_hash:
        return dict(base, state="hash_missing",
                    reason=DELIVERY_ERROR_CODES[DLV_FILE_MISSING])

    if approved_hash != current_hash:
        return dict(base, state="stale", reason=DELIVERY_ERROR_CODES[DLV_APPROVAL_STALE])

    return dict(base, state="valid", reason="")


def derive_status(pkg: Dict[str, Any], approval: Optional[Dict[str, Any]]) -> str:
    """由「机器校验结果」+「人工批准判定」推导交付包状态。

    注意这里**没有**「机器校验通过 ⇒ approved」的分支：``verified`` 是机器
    能给到的最高状态，``approved`` 只能来自带哈希绑定的人工批准。
    """
    approval_view = evaluate_approval(pkg, approval)
    if approval_view["state"] == "valid":
        return STATUS_APPROVED
    if approval_view["state"] in ("stale", "hash_missing"):
        return STATUS_INVALIDATED
    # 从未批准过（approval 为 None / revoked / 显式撤销）时，状态上限就是机器校验结果
    if (approval or {}).get("verified_ok") or (pkg or {}).get("verified_ok"):
        return STATUS_VERIFIED
    return STATUS_BUILT


def release_ready(pkg: Dict[str, Any], approval: Optional[Dict[str, Any]] = None,
                  *, licensing_gate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """发布前总门禁：授权、机器校验、人工批准、磁盘现状必须**同时**成立。

    这是交付 API 的统一收口，不允许调用方只看其中一个布尔值就导出。
    ``licensing_gate`` 可传入最新一次授权门禁结果；不传时沿用包落库时的
    ``licensing_ok``。任何未知状态都按未就绪处理（fail-closed）。
    """
    if not pkg:
        return {"ok": False, "code": DLV_UNKNOWN_PACKAGE,
                "message": DELIVERY_ERROR_CODES[DLV_UNKNOWN_PACKAGE],
                "blockers": ["unknown_package"]}
    blockers: List[str] = []
    if not (pkg.get("files") or []):
        blockers.append("empty_package")
    if not pkg.get("package_hash"):
        blockers.append("package_not_built")
    licensing_ok = bool((licensing_gate or {}).get("ok", pkg.get("licensing_ok")))
    if not licensing_ok:
        blockers.append("licensing_gate")
    if not pkg.get("verified_ok"):
        blockers.append("machine_verify")
    if not pkg.get("disk_ok") or not pkg.get("disk_matches_baseline"):
        blockers.append("disk_state")
    approval_view = evaluate_approval(pkg, approval)
    if approval_view["state"] != "valid":
        blockers.append("human_approval")
    return {
        "ok": not blockers,
        "code": "" if not blockers else DLV_NOT_RELEASE_READY,
        "message": "" if not blockers else DELIVERY_ERROR_CODES[DLV_NOT_RELEASE_READY],
        "blockers": blockers,
        "approval_state": approval_view["state"],
        "licensing_ok": licensing_ok,
        "verified_ok": bool(pkg.get("verified_ok")),
        "disk_ok": bool(pkg.get("disk_ok") and pkg.get("disk_matches_baseline")),
    }


def can_approve(pkg: Dict[str, Any]) -> Dict[str, Any]:
    """批准前置检查（**只做机器能客观判断的事**，不代替人工决定）。

    前置条件：① 交付包已生成且非空；② 有操作者。二者缺一即拒。
    注意此处**不**要求「机器校验通过」是批准的必要前提之外的任何隐含授权：
    机器校验结果由调用方一并记录，但批准本身就是人工的最终裁量。
    """
    if not pkg:
        return {"ok": False, "code": DLV_UNKNOWN_PACKAGE, "message": DELIVERY_ERROR_CODES[DLV_UNKNOWN_PACKAGE]}
    if not pkg.get("package_hash"):
        return {"ok": False, "code": DLV_NOT_BUILT, "message": DELIVERY_ERROR_CODES[DLV_NOT_BUILT]}
    if not (pkg.get("files") or []):
        return {"ok": False, "code": DLV_EMPTY_PACKAGE, "message": DELIVERY_ERROR_CODES[DLV_EMPTY_PACKAGE]}
    return {"ok": True, "code": "", "message": ""}
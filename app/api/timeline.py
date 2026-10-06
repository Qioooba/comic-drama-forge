# -*- coding: utf-8 -*-
"""不可变时间线蓝图：``/api/timeline/*``（评估文档 §P0-4）。

关键端点
--------
* ``POST /api/timeline/revisions``                —— 冻结一版剪辑；
* ``GET  /api/timeline/revisions/<id>/preflight``  —— **compose:preflight**，
  返回渲染计划 + ``compose_fingerprint``；
* ``GET  /api/timeline/revisions/<id>/manifest``   —— 冻结的渲染计划
  （FFmpeg 渲染器只消费它）；
* ``POST /api/timeline/revisions/<id>/derive``     —— 派生新版本（改内容的唯一路径）。

铁律落点
--------
* **铁律 4**：没有任何 ``PUT``/``PATCH``/``DELETE`` 路由能改 revision 内容，
  改内容只能 ``derive``（新 revision + 血缘）。路由表本身就是这条铁律的体现。
* **铁律 5**（静音必须显式声明）：``preflight`` 在 ``errors`` 里返回
  ``undeclared_silence``，``ok=False`` ⇒ 阻断渲染。

URL 命名说明
------------
用 ``/api/timeline/*`` 而不是 ``/api/video/*`` 下的子路径：本项目此前
**完全没有** 时间线概念（``grep TimelineRevision`` = 0 命中），
新开独立前缀不会与既有 252 条路由里的任何一条冲突，
也便于 W5 前端按域接入（对应 ``features/{output}/``）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

try:                                    # W1 拆分产物存在时优先用它
    from api._shared import jsonify as _jsonify, request as _request  # type: ignore
    jsonify, request = _jsonify, _request
except ImportError:                     # 拆分未完成 → flask 自带（自包含兜底）
    pass

try:
    from application.timeline_service import TimelineService, TimelineServiceError
    from domain.production_facts import (
        ApprovalAuthorizationError, ConflictError, DomainError, require_human_authorization,
    )
    from domain.timeline import TimelineError
    from infrastructure.facts_repo import FactsRepo
    from infrastructure.timeline_repo import TimelineRepo
except ImportError:                     # pragma: no cover - 以脚本方式导入时的兜底
    from app.application.timeline_service import (  # type: ignore
        TimelineService, TimelineServiceError,
    )
    from app.domain.production_facts import (  # type: ignore
        ApprovalAuthorizationError, ConflictError, DomainError, require_human_authorization,
    )
    from app.domain.timeline import TimelineError  # type: ignore
    from app.infrastructure.facts_repo import FactsRepo  # type: ignore
    from app.infrastructure.timeline_repo import TimelineRepo  # type: ignore

logger = logging.getLogger(__name__)

bp = Blueprint("timeline", __name__)
DOMAIN = "timeline"

_service = None


def service() -> TimelineService:
    """进程内单例（时间线与生产事实**共用 facts.db**，
    所以这里注入 FactsRepo，预检才能读到每镜的 ffprobe 事实与真实批准状态）。"""
    global _service
    if _service is None:
        facts = FactsRepo()
        _service = TimelineService(TimelineRepo(facts.db_path), facts)
    return _service


def _body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _err(message: str, status: int = 400, **extra: Any):
    body: Dict[str, Any] = {"success": False, "error": str(message)}
    body.update(extra)
    return jsonify(body), status


def _ok(**payload: Any):
    body = {"success": True}
    body.update(payload)
    return jsonify(body)


@bp.get("/api/timeline/health")
def health():
    """自检：迁移版本 + 不可变说明（只读）。"""
    svc = service()
    return _ok(schema_version=svc.timeline_repo.schema_version(),
               invariants={"timeline_immutable": True, "derive_only": True,
                           "declare_silence_required": True})


# =====================================================================
# 冻结 / 派生
# =====================================================================

@bp.post("/api/timeline/revisions")
def create_revision():
    """冻结一版剪辑。

    ``items`` 里每镜必须给 ``media_version_id``（**不是文件路径**）：
    路径会被重渲覆盖，只有版本记录能回答"成片里那 3 秒是哪一版"。
    """
    d = _body()
    missing = [k for k in ("project", "episode") if not d.get(k)]
    if missing:
        return _err("缺少必填字段：%s" % ", ".join(missing))
    if not isinstance(d.get("items"), list) or not d["items"]:
        return _err("items 必须是非空镜头数组")
    try:
        rev = service().create_revision(
            project=d["project"], episode=d["episode"], items=d["items"],
            subtitle_revision=d.get("subtitle_revision") or "",
            created_by=d.get("created_by") or "", note=d.get("note") or "",
            revision_id=d.get("revision_id") or "",
            revision_no=int(d.get("revision_no") or 0))
    except DomainError as e:
        status = 409 if isinstance(e, ConflictError) else 400
        return _err(e, status)
    return _ok(revision=rev)


@bp.post("/api/timeline/revisions/<revision_id>/derive")
def derive_revision(revision_id: str):
    """**派生**新版本（铁律 4 的唯一改内容路径；原版本一个字节都不动）。"""
    d = _body()
    if not str(d.get("reason") or "").strip():
        return _err("派生必须写明 reason（为什么改）")
    if not isinstance(d.get("items"), list) or not d["items"]:
        return _err("items 必须是非空镜头数组")
    try:
        out = service().derive_revision(
            revision_id, items=d["items"], reason=d["reason"],
            subtitle_revision=d.get("subtitle_revision") or "",
            created_by=d.get("created_by") or "", note=d.get("note") or "")
    except DomainError as e:
        status = 409 if isinstance(e, ConflictError) else 400
        return _err(e, status)
    return _ok(**out)


# =====================================================================
# 读取
# =====================================================================

@bp.get("/api/timeline/revisions")
def list_revisions():
    """按 project / episode 列出历史版本（改一版留一版，可全量回溯）。"""
    args = request.args
    revs = service().list_revisions(project=args.get("project") or "",
                                    episode=args.get("episode") or "",
                                    limit=int(args.get("limit") or 100))
    return _ok(revisions=revs, count=len(revs))


@bp.get("/api/timeline/revisions/latest")
def latest_revision():
    """某集的最新一版剪辑（``project`` + ``episode`` 必填）。"""
    project = request.args.get("project") or ""
    episode = request.args.get("episode") or ""
    if not project or not episode:
        return _err("缺少 project 或 episode")
    rev = service().latest_revision(project, episode)
    if not rev:
        return _err("该集尚无冻结的剪辑版本", 404)
    return _ok(revision=rev)


@bp.get("/api/timeline/revisions/<revision_id>")
def get_revision(revision_id: str):
    """单版剪辑 + 血缘链。"""
    svc = service()
    rev = svc.get_revision(revision_id)
    if not rev:
        return _err("时间线版本不存在：%s" % revision_id, 404)
    return _ok(revision=rev, lineage=svc.lineage(revision_id))


# =====================================================================
# compose:preflight（评估文档 §P0-4 第 2 条）
# =====================================================================

@bp.get("/api/timeline/revisions/<revision_id>/preflight")
def preflight(revision_id: str):
    """渲染预检：返回**渲染计划 + compose_fingerprint**。

    ``ok=false`` ⇒ 必须阻断渲染。典型阻断项：
    ``undeclared_silence``（合法静音没写进计划）、转场时长超过相邻镜头、
    指纹自校验失败。

    查询参数：
      * ``approved_only=1`` —— 未批准的镜头额外给出警告（两级生产要渲预览时用；
        它读的是**独立批准记录**，绝不由"已采用"推导批准）。
    """
    approved_only = str(request.args.get("approved_only") or "").lower() in ("1", "true", "yes")
    report = service().preflight(revision_id, approved_only=approved_only,
                                 renderer=request.args.get("renderer") or "ffmpeg",
                                 preset=request.args.get("preset") or "")
    return _ok(**report)


@bp.get("/api/timeline/revisions/<revision_id>/manifest")
def manifest(revision_id: str):
    """冻结的渲染计划（渲染器只消费它）。

    ``silence_declarations`` 列出计划里显式声明的静音镜头 ——
    「合法静音」在渲染前就是计划中的一条，而不是渲完才发现。
    """
    try:
        return _ok(manifest=service().render_manifest(
            revision_id, renderer=request.args.get("renderer") or "ffmpeg",
            preset=request.args.get("preset") or ""))
    except DomainError as e:
        return _err(e, 404)


@bp.post("/api/timeline/revisions/<revision_id>/verify-render")
def verify_render(revision_id: str):
    """渲完对照计划：时长漂移立刻报出来，而不是等用户看片才发现。"""
    d = _body()
    probed = d.get("probed") or d
    if not isinstance(probed, dict):
        return _err("probed 必须是 ffprobe 结果对象")
    try:
        return _ok(**service().verify_render(
            revision_id, probed, tolerance_sec=float(d.get("tolerance_sec") or 0.5)))
    except DomainError as e:
        return _err(e, 404)


@bp.get("/api/timeline/fingerprints/<fingerprint>")
def by_fingerprint(fingerprint: str):
    """按 compose_fingerprint 查历史版本（判"这版剪辑其实渲过"）。"""
    revs = service().find_by_fingerprint(fingerprint)
    return _ok(revisions=revs, count=len(revs))


# =====================================================================
# 按冻结计划渲染（ADR-0012 决策第 3 点）
# =====================================================================

def _approver_allowlist() -> List[str]:
    """人工主体白名单（``MJSCXT_APPROVER_ALLOWLIST``，逗号分隔）。

    与 :func:`api.delivery._approver_allowlist` **同一口径**（同一环境变量、
    同一解析规则）。此处复制而非 import，是为了让 ``api/timeline`` 保持
    不依赖别的蓝图模块；口径一致靠的是共用同一个领域函数
    :func:`domain.production_facts.require_human_authorization`，
    词表法与白名单法都在那一个函数里，两套「人工」口径不一致时
    先被放宽的那套才是实际入口。
    """
    raw = os.environ.get("MJSCXT_APPROVER_ALLOWLIST", "") or ""
    return [p.strip() for p in raw.replace("；", ",").split(",") if p.strip()]


@bp.post("/api/timeline/revisions/<revision_id>/render")
def render_from_plan(revision_id: str):
    """**按冻结计划渲染** —— 让渲染器真正消费 TimelineRevision。

    流程（fail-closed，任一环不成立都**阻断**，绝不降级放行）：

    1. **人工授权**（``authorized_by`` 必填）：复用
       :func:`domain.production_facts.require_human_authorization`，
       机器账号一律 **403**。渲染是要烧 GPU 的动作，机器账号不该自己拍板。
    2. **合成预检**（:meth:`TimelineService.compose_preflight`）：``undeclared_silence``、
       指纹自校验失败、版本未登记 / 与磁盘现状对不上 / 未批准 —— 任一即阻断。
       机器检查通过**不等于**人工批准（铁律 1）。
    3. **出计划**（:meth:`TimelineService.build_plan`）：纯函数，
       顺序 / 时长 / 转场 / 音轨来源 / 声明静音全部来自 revision。
    4. **渲染**（:func:`video_postprocess.render_from_plan`）：只消费计划。
    5. **登记**（:meth:`TimelineService.record_render`）：把 ``compose_fingerprint``
       与实际产物 sha256 绑在一起（ADR-0012 决策第 3 点）。

    body::

        {"authorized_by": "李工", "authorization_ref": "工单-1234",
         "output_path": "...",            # 可选；缺省写到 final/<project>/epNN_final_tlNN.mp4
         "dry_run": false,                # 可选；true = 只校验计划，不渲
         "preview": false,                # 可选；true = 渲未批准版的预览
         "renderer": "ffmpeg", "preset": ""}

    ⚠️ **审批门不允许请求体关闭**：预览走 ``preview: true``，
    它只能落到 ``preview/`` 目录并在返回里打 ``preview`` / ``deliverable=false`` 标记，
    永远写不到 ``final/``（可交付成片目录）。见 :func:`_resolve_output_path`。

    字幕**不参与本端点**（ADR-0012 决策第 4 点）：``subtitle_revision`` 挂在
    时间线上由字幕链路单独消费。
    """
    d = _body()
    authorized_by = str(d.get("authorized_by") or "").strip()
    if not authorized_by:
        return _err("按冻结计划渲染必须有人工授权主体（authorized_by 为空）", 403)
    try:
        require_human_authorization(authorized_by,
                                    str(d.get("authorization_ref") or ""),
                                    allowlist=_approver_allowlist())
    except ApprovalAuthorizationError as e:
        return _err(e, 403)

    svc = service()
    # 审批门由**服务端**决定，不由请求体决定。曾经这里转发 `require_approved`，
    # 等于任何调用方都能 POST {"require_approved": false} 跳过人工批准，
    # 产物却照样落进 final/（与旧路径交付成片的同一目录），返回里也没有任何
    # 「这只是预览」的标记 —— 预览与可交付成片无法区分。
    preview = bool(d.get("preview"))
    require_approved = not preview
    renderer = str(d.get("renderer") or "ffmpeg")
    preset = str(d.get("preset") or "")
    dry_run = bool(d.get("dry_run"))

    # ① 合成预检（四个阻断点）
    try:
        pre = svc.compose_preflight(revision_id, renderer=renderer, preset=preset,
                                     approved_only=not require_approved,
                                     require_approved=require_approved)
    except DomainError as e:
        status = 409 if isinstance(e, ConflictError) else 400
        return _err(e, status)
    if not pre.get("ok"):
        return _err("合成预检未通过，已阻断渲染：%s" % "；".join(pre.get("errors") or ()),
                    409, preflight=pre)

    # ② 出计划（纯函数）
    try:
        plan = svc.build_plan(revision_id, renderer=renderer, preset=preset,
                              manifest=pre.get("manifest"))
    except DomainError as e:
        return _err(e, 400)

    # ③ 渲染（dry_run 时不落盘、不起 ffmpeg）
    #    ⚠️ `output_path` 直接进 subprocess.run([..., output_path]) 且带 `-y`，
    #    不校验等于任意路径写 + 任意文件覆盖。校验在服务端做，fail-closed。
    try:
        out_path = _resolve_output_path(d.get("output_path"), plan, svc=svc, preview=preview)
    except DomainError as e:
        return _err(e, 400)
    try:
        from video_postprocess import render_from_plan as _render
    except ImportError:                      # pragma: no cover - 兜底
        from app.video_postprocess import render_from_plan as _render  # type: ignore
    from config import PROJECT_OUTPUT_DIR
    rr = _render(plan, out_path, caller=f"api.timeline.render({revision_id})",
                 subtitle_revision=str(plan.get("subtitle_revision") or ""),
                 dry_run=dry_run, target_w=int(d.get("target_w") or 0),
                 target_h=int(d.get("target_h") or 0),
                 target_fps=int(d.get("target_fps") or 0),
                 output_root=os.path.abspath(PROJECT_OUTPUT_DIR))
    # 打标记：预览与可交付成片**在返回里必须可区分**（否则调用方无从判断
    # 能不能拿它去交付）。这三个键在成功与失败两条分支上都存在，不靠「缺省即 false」，
    # 也不靠「只有成功才打标记」—— 渲染失败时文件可能已经建了一半
    # （ffmpeg 中途报错、容器头已写），失败响应缺了这几个键，调用方就无从判断
    # 那半个文件是不是可交付成片。故**先打标记再判成败**。
    rr.update({"preview": bool(preview), "deliverable": not preview,
               "output_kind": "preview" if preview else "final",
               "require_approved": bool(require_approved)})
    if not rr.get("ok"):
        return _err("按冻结计划渲染失败：%s" % (rr.get("error") or "未知原因"), 409, render=rr)

    # ④ 登记（内容变了指纹就变）
    note = str(d.get("note") or "")
    if preview:
        note = ("[preview 非可交付成片；未批准版本；审批门被显式跳过] " + note).strip()
    rv = svc.record_render(plan, output_path=rr.get("output_path") or "",
                           output_sha256=rr.get("output_sha256") or "",
                           output_bytes=rr.get("output_bytes") or 0,
                           output_duration_sec=rr.get("output_duration_sec") or 0.0,
                           authorized_by=authorized_by,
                           authorization_ref=str(d.get("authorization_ref") or ""),
                           note=note)
    rv["preview"] = bool(preview)
    rv["deliverable"] = not preview
    return _ok(render=rr, render_version=rv, preflight=pre)


#: 成片允许的扩展名（``video_postprocess`` 交给 ffmpeg 的 mov 家族）
_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".webm")

#: Windows 保留设备名。**带扩展名也照样命中**：``NUL.mp4`` / ``CON.mp4`` /
#: ``COM1.mp4`` 都指向设备而不是文件 —— ``os.makedirs`` 不报错、ffmpeg 却写不进去，
#: 结果是「渲染成功」但磁盘上什么都没有。
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)])

#: 输出路径长度上限。Win32 传统 ``MAX_PATH`` 是 260，留 20 字符余量给
#: 上层拼的目录名。**必须在这里拦**：超长路径是 ``os.makedirs`` 抛
#: ``OSError``（500），而不是一条干净的 400 —— 请求方无从知道自己该改哪。
MAX_OUTPUT_PATH_LEN = 240

#: 文件名 / 目录名里允许的字符：ASCII 字母数字 + CJK，其余一律换成 ``_``。
#: 顺带挡掉 ``:`` ``*`` ``?`` ``"`` ``<`` ``>`` ``|`` 这些 Windows 非法的字符。
_UNSAFE_NAME_CHARS = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _safe_name(raw: Any, *, limit: int = 24, fallback: str = "x") -> str:
    """任意文本 → 一段可安全当**路径组件**的字符串（清洗 + 截断）。"""
    text = _UNSAFE_NAME_CHARS.sub("_", str(raw or "").strip()).strip("_")
    return text[:limit].strip("_") or fallback


def _episode_slug(episode: Any, *, limit: int = 16) -> str:
    """剧集身份 → **单射**的文件名片段（撞名的真正落点）。

    从自由文本（``S01E02`` / ``第2集`` / ``第一集`` / ``EPISODE-7``）里正则抠一个
    整数当集号**必然有损**：``re.search(r"(\\d+)")`` 取第一段数字，于是
    ``S01E02`` 与 ``S01E03`` 都得到集号 1；``第一集``（按首次出现排序拿到 2）
    与 ``第2集``（抠到 2）也会落在同一槽位。两个不同剧集于是写到**同一个文件名**，
    再叠上 ffmpeg 的 ``-y`` 就是静默覆盖掉已渲好的成片。

    整数集号有损就不该拿它当身份。故：可读片段照旧，**后面固定追加
    episode 原文的短 sha256** —— 原文不同 ⇒ 哈希不同 ⇒ 文件名不可能相同。
    可读性不受损，唯一性也不再依赖「抠数字猜得对」。
    """
    raw = str(episode or "")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (_safe_name(raw, limit=limit, fallback="ep"), digest)


def _reject_reserved_or_too_long(path: str) -> str:
    """成片落盘路径的最后一道**逐段**检查：保留设备名 + 长度上限。

    对「客户端给的路径」与「缺省路径」同样成立 —— 后者含请求体里的 ``project``，
    理论上可以是 ``NUL``。
    """
    for part in path.replace("\\", "/").split("/"):
        if not part:
            continue
        # 设备名判定取「第一个点之前的部分」：``NUL.mp4`` 命中 ``NUL``，
        # 而 ``a.NUL.mp4`` 合法。末尾的空格/点在 Windows 上被忽略，一并去掉。
        stem = os.path.splitext(part)[0].rstrip(" .")
        if stem.upper() in _WINDOWS_RESERVED_NAMES:
            raise DomainError(
                "output_path 的某一段是 Windows 保留设备名 %r（带扩展名同样命中，"
                "且写不进任何文件）：%r" % (part, path))
    abs_path = os.path.abspath(path)
    if len(abs_path) > MAX_OUTPUT_PATH_LEN:
        raise DomainError(
            "output_path 长 %d 字符，超过上限 %d（Win32 MAX_PATH=260 会让落盘直接抛错，"
            "拿不到干净的 400）：%r" % (len(abs_path), MAX_OUTPUT_PATH_LEN, path))
    return abs_path


def _normcase_abs(path: str) -> str:
    """绝对路径 + ``normcase``（Windows 大小写不敏感，比较前必须归一）。

    与 ``app/disk_reclaim._norm`` 同一口径。
    """
    return os.path.normcase(os.path.abspath(path))


def _is_within(path: str, roots: List[str]) -> bool:
    """``path`` 是否位于 ``roots`` 之内。取自 ``app/disk_reclaim._is_within``。"""
    try:
        target = _normcase_abs(path)
    except (OSError, ValueError):
        return False
    for root in roots or ():
        try:
            if os.path.commonpath([target, _normcase_abs(root)]) == _normcase_abs(root):
                return True
        except (OSError, ValueError):
            continue
    return False


def _default_output_path(plan: Dict[str, Any], svc: Any, *, preview: bool) -> str:
    """缺省成片文件名：``<root>/final|preview/<project>/epNN_<集标识>_<bucket>_tlNN.mp4``。

    三个槽各司其职：``epNN`` 是**集号**（只读、**不保证唯一**）、
    ``tlNN`` 是**时间线版本号**、``<集标识>`` 是**集身份的单射片段**
    （唯一性的真正来源）。
    （曾用同一个 ``ep`` 变量填两个槽，第 2/3 版剪辑会 ``-y`` 覆盖第 1 版。）
    """
    from config import PROJECT_OUTPUT_DIR
    episode_no: int = _episode_no_from_revision(plan, svc)          # 集号 → epNN（可读）
    episode_slug: str = _episode_slug(plan.get("episode"))          # 集身份 → 唯一
    timeline_revision_no: int = int(plan.get("revision_no") or 1)    # 版本号 → tlNN
    bucket = "preview" if preview else "final"
    return os.path.join(PROJECT_OUTPUT_DIR, bucket,
                        _safe_name(plan.get("project"), limit=24, fallback="project"),
                        f"ep{episode_no:02d}_{episode_slug}_{bucket}"
                        f"_tl{timeline_revision_no:02d}.mp4")


def _resolve_output_path(raw: Any, plan: Dict[str, Any], *, svc: Any = None,
                         preview: bool = False) -> str:
    """客户端给的 ``output_path`` → 唯一可接受的形式（fail-closed）。

    三道检查，缺一道都能被打穿：

    1. **扩展名白名单**：只收 ``.mp4/.mov/.m4v/.mkv/.webm``，
       挡掉 ``out.sh``、``.bat``、``.py`` 这类「不是成片」的落盘目标；
    2. **显式拒绝 ``..``**：不接受任何一级 ``..``（不做「规范化后还在目录内
       就放过」——规范化之前就已经是可疑输入）；
    3. **realpath 之后再查一次包含关系**：目录内一个指向外面的符号链接
       也会被这步打掉。相对路径按「相对于数据输出根」解释，
       缺省则落到 ``final/``（预览落 ``preview/``）；
    4. **保留设备名 + 长度上限**（:func:`_reject_reserved_or_too_long`）：
       ``NUL.mp4`` 写不进任何文件却不报错；>260 字符的路径会在
       ``os.makedirs`` 抛 ``OSError`` ⇒ 500 而不是 400。
    """
    from config import PROJECT_OUTPUT_DIR
    root = os.path.abspath(PROJECT_OUTPUT_DIR)
    text = str(raw or "").strip()
    if not text:
        return _reject_reserved_or_too_long(
            _default_output_path(plan, svc, preview=preview))

    if os.path.splitext(text)[1].lower() not in _VIDEO_EXTS:
        raise DomainError("output_path 必须是成片视频文件（允许 %s）：%r"
                          % ("、".join(_VIDEO_EXTS), text))
    parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise DomainError("output_path 不允许包含上级目录 '..'（拒绝按路径逃逸）：%r" % text)
    cand = text if os.path.isabs(text) else os.path.join(root, *parts)
    if not _is_within(cand, [root]):
        raise DomainError("output_path 必须位于项目输出目录 %s 之内：%r" % (root, text))
    resolved = os.path.realpath(cand)
    if not _is_within(resolved, [os.path.realpath(root)]):
        raise DomainError("output_path 规范化后落到输出目录之外（拒绝符号链接/上级目录逃逸）：%r"
                          % text)
    if preview:
        # 预览不能借客户端给的路径摸进 final/：强制重定到 preview/ 下。
        rel = os.path.relpath(resolved, os.path.realpath(root))
        head, tail = os.path.split(rel)
        if os.path.normcase(head.split(os.sep)[0] if head else "") != "preview":
            raise DomainError(
                "预览渲染（preview=true）的 output_path 必须位于 %s 之下：%r"
                % (os.path.join(root, "preview"), text))
        return _reject_reserved_or_too_long(resolved)
    return _reject_reserved_or_too_long(resolved)


def _episode_no_from_revision(rev: Dict[str, Any], svc: Any = None) -> int:
    """从 ``episode`` 字段推**集号**（``ep01`` / ``第1集`` / 纯数字都接受）。

    集号与时间线版本号是两件事（文件名里分别是 ``epNN`` 与 ``tlNN``），
    混用会让不同 revision 互相覆盖（ADR-0012 铁律 4）。

    名称里没有数字时（如 ``第一集``）**不再退回 ``revision_no``** —— 那等于把
    「第 2 版剪辑」写进「第 1 集」的文件名槽。改为按该剧集在本项目里
    **首次出现**的先后取序号：稳定、与版本号无关、读得懂。

    ⚠️ **本函数只负责「读得懂」，不再负责「不撞名」**：从自由文本抠整数必然有损
    （``S01E02`` / ``S01E03`` 都抠到 1，``第一集`` 与 ``第2集`` 也会撞），
    撞名由 :func:`_episode_slug` 的哈希片段兜底。
    """
    raw = str(rev.get("episode") or "").strip()
    m = re.search(r"(\d+)", raw)
    if m and int(m.group(1)) > 0:
        return int(m.group(1))
    if raw and svc is not None:
        try:
            rows = list(svc.list_revisions(project=str(rev.get("project") or ""),
                                           limit=1000) or ())
        except Exception:      # noqa: BLE001  推不出集号不影响显式 output_path
            rows = []
        # 自己排序（按创建时间、再按版本号升序），不依赖仓储层的 ORDER BY 方向。
        rows.sort(key=lambda r: (str((r or {}).get("created_at") or ""),
                                 int((r or {}).get("revision_no") or 0)))
        episodes: List[str] = []
        for row in rows:
            name = str((row or {}).get("episode") or "")
            if name and name not in episodes:
                episodes.append(name)
        if raw in episodes:
            return episodes.index(raw) + 1
    return 1
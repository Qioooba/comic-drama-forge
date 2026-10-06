# -*- coding: utf-8 -*-
"""编排层：不可变时间线（建 revision → 预检 → 派生新 revision）。

对应评估文档 §P0-4 的最小可用形态：

1. :meth:`TimelineService.create_revision` —— 冻结镜头序列；
2. :meth:`TimelineService.preflight` —— 返回渲染计划（含 ``compose_fingerprint``）；
3. FFmpeg 渲染器**只消费冻结计划**，产出 ``EpisodeRenderVersion``（本层不负责渲染）。

铁律 4（revision 不可变）与铁律 5（静音必须显式声明）的落点都在本层注释里标出。
编排层不 import flask。
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:
    from domain.production_facts import ConflictError, DomainError
    from domain.timeline import (
        ComposePlan, ComposePlanEntry, EpisodeRenderVersion, PreflightReport, RenderManifest,
        TimelineError, TimelineItem, TimelineRevision, Transition,
        build_compose_plan, build_episode_render_version, build_render_manifest,
        build_revision, derive_revision, plan_fingerprint, preflight,
    )
    from infrastructure.facts_repo import FactsRepo
    from infrastructure.timeline_repo import TimelineRepo
except ImportError:                    # pragma: no cover - 以脚本方式导入时的兜底
    from app.domain.production_facts import ConflictError, DomainError  # type: ignore
    from app.domain.timeline import (  # type: ignore
        ComposePlan, ComposePlanEntry, EpisodeRenderVersion, PreflightReport, RenderManifest,
        TimelineError, TimelineItem, TimelineRevision, Transition,
        build_compose_plan, build_episode_render_version, build_render_manifest,
        build_revision, derive_revision, plan_fingerprint, preflight,
    )
    from app.infrastructure.facts_repo import FactsRepo  # type: ignore
    from app.infrastructure.timeline_repo import TimelineRepo  # type: ignore

logger = logging.getLogger(__name__)

__all__ = ["TimelineService", "TimelineServiceError", "compose_plan_to_ffmpeg_args",
           "XFADE_TRANSITIONS"]


class TimelineServiceError(DomainError):
    """时间线编排错误。"""


#: FFmpeg ``xfade`` 真实支持的转场名 —— 领域层 :class:`Transition` 的白名单
#: 是**剪辑语义**，这里做一次显式映射。**未列出的转场一律 fail-closed 报错**，
#: 绝不"猜一个最像的"：写错名字只会在渲染期炸，那时已经白烧了卡。
XFADE_TRANSITIONS: Mapping[str, str] = {
    "fade": "fade",
    "dissolve": "dissolve",
    "dip_to_black": "fadeblack",
    "slide": "slideleft",
    "wipe": "wipeleft",
}


def _s(value: float) -> str:
    """秒数格式化（3 位小数）。给 ffmpeg 滤镜用，避免 ``1.0`` vs ``1``。"""
    return "%.3f" % float(value)


def compose_plan_to_ffmpeg_args(plan: ComposePlan, *,
                                target_w: int = 0, target_h: int = 0,
                                target_fps: int = 0, sample_rate: int = 48000,
                                crf: int = 20, x264_preset: str = "veryfast",
                                channels: str = "stereo") -> Dict[str, Any]:
    """**纯函数**：合成计划 → FFmpeg argv（无 I/O、无子进程、无日志副作用）。

    两条路径（刻意保守，宁可多编码也不出花）：
    * ``concat_copy`` —— 全部 ``cut``、无外部音轨、无声明静音时走 concat demuxer
      + ``-c copy``（快、不重编码，与既有 :meth:`VideoPostProcessor.concat_videos` 同思路）；
    * ``filter`` —— 只要有一处转场 / 外部配音轨 / 声明静音，就走 filter_complex
      重编码：按计划 ``duration_sec`` ``trim``/``atrim``（计划说了算，不是文件说了算），
      逐镜补 ``anullsrc``（声明静音**在计划里就是一条**，不是渲完才发现没声音）。

    ⚠️ 转场偏移量按 ``start_i = end_{i-1} - D_i`` 递推（xfade/acrossfade 的标准口径），
    输出时长 = 末镜的 ``start + duration``。音轨按**同一条**递推对齐权威成片时长，
    无转场处用 concat 真的混入本镜音轨（不是 apad 补静音），两者对不上即 fail-closed。
    本函数**未经真实渲染验证**（本轮禁跑 FFmpeg），
    首次启用时必须渲一支短样片核对时长与转场边界。

    字幕**不在这里**（ADR-0012 决策第 4 点）：合成只出画面 + 音轨。
    """
    if not isinstance(plan, ComposePlan):
        raise TimelineServiceError("compose_plan_to_ffmpeg_args 只接受 ComposePlan")
    plan.validate()
    entries = list(plan.entries)
    n = len(entries)

    timed = [e for e in entries[1:] if e.transition_in.effective_sec() > 0]
    for e in entries[1:]:
        # 阻断点：转场在领域层与 ffmpeg 层的对应关系必须**显式**存在。
        # cut 恒为 0 秒，xfade 不需要它，故放行；其余未列出的名字一律 fail-closed
        # —— 写错名字只会在渲染期炸，那时已经白烧了卡。
        if e.transition_in.kind != "cut" and e.transition_in.kind not in XFADE_TRANSITIONS:
            raise TimelineServiceError(
                "转场 %r 没有对应的 FFmpeg 实现（可实现转场：%s）"
                % (e.transition_in.kind, sorted(XFADE_TRANSITIONS)))
    has_external_audio = any(e.audio_path for e in entries)
    has_silence = any(e.declared_silence for e in entries)

    # ---- 路径 A：concat demuxer -c copy（仅在完全无转场/无外部音轨/无声明静音时）
    if not timed and not has_external_audio and not has_silence:
        return {
            "mode": "concat_copy",
            "args": [],                       # 由渲染器拼 list 文件后补
            "filter_complex": "",
            "input_count": n,
            "output_duration_sec": plan.expected_duration_sec(),
            # 两种 mode 的返回体**同一形状**：音频流长度也只有一个数。
            "expected_audio_duration_sec": plan.expected_duration_sec(),
            "planned_entries": [e.to_dict() for e in entries],
            "notes": ["concat demuxer + -c copy：不重编码；转场/外部音轨/声明静音"
                      "出现任一情况即自动降级到 filter 重编码"],
        }

    # ---- 路径 B：filter_complex 重编码
    inputs: List[str] = []
    # 逐镜输入下标：视频必 1 个输入，外部配音轨再 1 个（声明静音不需要输入，用 anullsrc）
    v_idx: List[int] = []
    a_idx: List[Optional[int]] = []
    for e in entries:
        # ⚠️ 下标 = **已收集的输入个数**，不是 ``len(inputs) // 2``。
        # 那是本轮查出的另一处静默错渲：只有视频输入时输入数按 1 递增，
        # 除 2 得到 [0,0,1,…] —— 于是第 2 镜去读第 1 镜的文件、第 3 镜读第 2 镜的，
        # 渲出来「顺序看着对、素材全是错的那一版」：计划声明 media_version_id=X，
        # 实际喂给 ffmpeg 的是上一镜的字节，而渲后时长校验照样通过。
        # 配音轨下标紧跟在本镜视频之后（先 append 视频、再取下标）。
        v_idx.append(len(inputs))
        inputs.append(e.video_path)
        a_idx.append(None)
        if e.audio_path:
            a_idx[-1] = len(inputs)
            inputs.append(e.audio_path)

    chain: List[str] = []
    # 每条音轨段统一到的采样参数。concat 滤镜要求各段参数完全一致
    # （否则协商阶段报 "Input link ... parameters do not match"），
    # 而 acrossfade 的输出参数由其两侧输入推导 —— 显式写死一份，
    # 不靠「恰好一致」这种运气（本函数从未跑过真实 ffmpeg）。
    afmt = "sample_fmts=fltp:sample_rates=%d:channel_layouts=%s" % (int(sample_rate), channels)
    for i, e in enumerate(entries):
        d = _s(e.duration_sec)
        vfilters = ["trim=0:%s" % d, "setpts=PTS-STARTPTS"]
        if target_w and target_h:
            vfilters.append("scale=%d:%d:force_original_aspect_ratio=decrease"
                            % (int(target_w), int(target_h)))
            vfilters.append("pad=%d:%d:(ow-iw)/2:(oh-ih)/2" % (int(target_w), int(target_h)))
        if target_fps:
            vfilters.append("fps=%d" % int(target_fps))
        vfilters.append("format=yuv420p")
        vfilters.append("setsar=1")
        chain.append("[%d:v:0]%s[v%d]" % (v_idx[i], ",".join(vfilters), i))

        if e.declared_silence:
            # 铁律 5：合法静音在计划里显式存在，渲染器照办即可，不需要"发现后再判断"
            chain.append("anullsrc=channel_layout=%s:sample_rate=%d:d=%s,aformat=%s[a%d]"
                         % (channels, int(sample_rate), d, afmt, i))
        else:
            src = "[%d:a:0]" % a_idx[i] if a_idx[i] is not None else "[%d:a:0]" % v_idx[i]
            chain.append("%satrim=0:%s,asetpts=PTS-STARTPTS,aresample=%d,aformat=%s[a%d]"
                         % (src, d, int(sample_rate), afmt, i))

    # 视频衔接：有时长转场处走 xfade（offset 递推）；纯 cut 处走 concat 两段拼接。
    # ⚠️ **不**给 cut 生成 ``xfade:duration=0`` —— ffmpeg 会直接报错，
    # 而且「零秒转场」本来就该是 concat 而不是 xfade。
    if timed:
        cursor = float(entries[0].duration_sec)
        prev_label = "[v0]"
        for i, e in enumerate(entries[1:], start=1):
            trans = e.transition_in.effective_sec()
            if trans > 0:
                offset = max(0.0, cursor - trans)
                out_label = "[vx%d]" % i
                chain.append("%s[v%d]xfade=transition=%s:duration=%s:offset=%s%s"
                             % (prev_label, i, XFADE_TRANSITIONS[e.transition_in.kind],
                                _s(trans), _s(offset), out_label))
                cursor = offset + float(e.duration_sec)
            else:
                out_label = "[vx%d]" % i
                chain.append("%s[v%d]concat=n=2:v=1:a=0%s"
                             % (prev_label, i, out_label))
                cursor = cursor + float(e.duration_sec)
            prev_label = out_label
        v_out = prev_label
    else:
        chain.append("".join("[v%d]" % i for i in range(n))
                     + "concat=n=%d:v=1:a=0[vx]" % n)
        v_out = "[vx]"

    # 音频衔接。**唯一权威口径是 :func:`domain.timeline.expected_total_duration_sec`**：
    # 递推 ``cursor = cursor + d_i - min(overlap, cursor)``（与视频 xfade 的
    # ``offset = max(0, cursor - overlap)`` 逐镜同口径）。音画两条链必须落在
    # **同一个数**上 —— 成片总时长只描述较长的那一条，短的那条没有第二个数去校验它，
    # 于是「画面 38.5s / 声音 19s」能一路通过渲后时长校验流出去。
    a_cursor = float(entries[0].duration_sec)
    if timed:
        a_label = "[a0]"
        for i in range(1, n):
            d = entries[i].transition_in.effective_sec()
            nxt = "[ax%d]" % i
            if d > 0:
                # acrossfade 的输出长度是 a+b-d；当转场长过已累计音轨时（d > a_cursor）
                # 它给不出权威值，故按权威递推显式 atrim 收口 —— 多出来的一小段
                # （d - a_cursor）正是「转场吃不掉的内容」，裁掉它才是计划口径。
                target = round(max(0.0, a_cursor + float(entries[i].duration_sec)
                                    - min(d, a_cursor)), 3)
                chain.append("%s[a%d]acrossfade=d=%s:c1=tri:c2=tri,atrim=0:%s%s"
                             % (a_label, i, _s(d), _s(target), nxt))
                a_cursor = target
            else:
                # 无转场 = 首尾相接，**必须把本镜音轨真的混进来**。
                # 曾在这里只做 apad+atrim：[a_i] 从未进入音轨链，最后几秒是静音而
                # 画面正在放有声镜头；而渲后时长校验**照样通过**（时长对得上），
                # 正是「静默出错且验证层看不见」那一类。
                # 目标长度 = 累计 + 本镜（无重叠），与权威递推逐镜相同。
                target = round(a_cursor + float(entries[i].duration_sec), 3)
                # concat 要求两段参数一致：显式各接一次 aformat，而不是靠上游恰好。
                chain.append("%saformat=%s[axl%d]" % (a_label, afmt, i))
                chain.append("[a%d]aformat=%s[axr%d]" % (i, afmt, i))
                chain.append("[axl%d][axr%d]concat=n=2:v=0:a=1%s"
                             % (i, i, nxt))
                a_cursor = target
            a_label = nxt
        a_out = a_label
    else:
        chain.append("".join("[a%d]" % i for i in range(n))
                     + "concat=n=%d:v=0:a=1[ax]" % n)
        a_out = "[ax]"
        # 全是 cut ⇒ 权威递推里没有任何重叠 ⇒ 累计 = 各镜之和。
        a_cursor = float(sum(float(e.duration_sec) for e in entries))

    # 输出时长读领域层的**权威口径**，不复算 —— 计划字段、校验基线、这里
    # 三者必须是同一个数（ADR-0012 的「成片 ↔ 计划」绑定不允许有第二口径）。
    out_duration = plan.expected_duration_sec()
    # 音画两条链各一个数，且两者必须相等：不等就是「画面与声音不是同一条时间线」，
    # 在烧卡**之前** fail-closed，而不是渲完看片才发现。
    if abs(a_cursor - float(out_duration)) > 0.001:
        raise TimelineServiceError(
            "音轨链长 %.3fs 与权威成片时长 %.3fs 不一致（转场重叠必须扣掉，"
            "且每镜音轨必须真的进入拼接）；拒绝渲出画面与声音脱钩的成片"
            % (a_cursor, float(out_duration)))
    args = (["-y", "-v", "error"]
            + [x for p in inputs for x in ("-i", p)]
            + ["-filter_complex", ";".join(chain),
               "-map", v_out, "-map", a_out,
               "-c:v", "libx264", "-preset", str(x264_preset), "-crf", str(int(crf)),
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart"])
    return {
        "mode": "filter",
        "args": args,
        "filter_complex": ";".join(chain),
        "input_count": len(inputs),
        "output_duration_sec": round(float(out_duration), 3),
        # 每条流**一个数**：视频与音频的长度都等于权威成片时长
        # （上面已 fail-closed 复核），不存在「只描述较长那条流」的第二个口径。
        "expected_audio_duration_sec": round(float(a_cursor), 3),
        "planned_entries": [e.to_dict() for e in entries],
        "notes": ["按计划 duration 逐镜 trim/atrim", "声明静音镜由 anullsrc 补齐",
                  "无转场处 concat 真的混入本镜音轨（不靠 apad 补静音）",
                  "音画两条链都在渲前与权威成片时长对齐，不一致即拒绝",
                  "分辨率/帧率未指定时沿用源片（不硬编码，避免竖屏被改横屏）"],
    }



def _new_id(prefix: str = "tl") -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


class TimelineService:
    """时间线编排入口。

    ``timeline_repo`` 与 ``facts_repo`` 都可注入：
    注入 ``facts_repo`` 后，预检才能知道每镜的 ffprobe 事实与**真实批准状态**
    （铁律 1 的读侧）；不注入时只做时间线自身校验。
    """

    def __init__(self, timeline_repo: Optional[TimelineRepo] = None,
                 facts_repo: Optional[FactsRepo] = None):
        self.timeline_repo = timeline_repo or TimelineRepo()
        self.facts_repo = facts_repo

    # ============================================================
    # 建 / 派生
    # ============================================================

    def create_revision(self, *, project: str, episode: str,
                        items: Sequence[Mapping[str, Any]],
                        subtitle_revision: str = "", created_by: str = "",
                        note: str = "", revision_id: str = "",
                        revision_no: int = 0) -> Dict[str, Any]:
        """冻结一版剪辑并落库。

        ``revision_no`` 不给时按库里最大值 +1 **建议**；
        并发派生同号会拿到 :class:`ConflictError`（唯一索引兜底），
        调用方读到错误后重新取号再建，**绝不覆盖已有版本**。
        """
        if not str(project or "").strip() or not str(episode or "").strip():
            raise TimelineServiceError("时间线必须归属 project + episode")
        no = int(revision_no or 0) or self.timeline_repo.next_revision_no(project, episode)
        rev = build_revision(revision_id or _new_id(), project=project, episode=episode,
                             revision_no=no, items=items,
                             subtitle_revision=subtitle_revision,
                             created_by=created_by, note=note)
        self.timeline_repo.save_revision(rev)
        logger.info("已冻结时间线版本：%s（%s/%s rev%s fp=%s）",
                    rev.revision_id, project, episode, rev.revision_no,
                    rev.compose_fingerprint[:12])
        return rev.to_dict()

    def derive_revision(self, revision_id: str, *,
                        items: Sequence[Mapping[str, Any]], reason: str,
                        subtitle_revision: str = "", created_by: str = "",
                        note: str = "") -> Dict[str, Any]:
        """**派生**新 revision（铁律 4 的唯一合法改内容路径）。

        原 revision 一个字节都不动；新 revision 记 ``parent_revision_id``。
        内容没变的派生由领域层拒绝 —— 否则会出现「批准了一个和上一版
        剪辑完全一样的东西」，审批链失去意义。
        """
        parent = self.timeline_repo.get_revision(revision_id)
        if parent is None:
            raise TimelineServiceError("时间线版本不存在：%s" % revision_id)
        no = self.timeline_repo.next_revision_no(parent.project, parent.episode)
        child = derive_revision(parent, _new_id(), revision_no=no, items=items,
                                reason=reason, subtitle_revision=subtitle_revision,
                                created_by=created_by, note=note or reason)
        self.timeline_repo.save_revision(child)
        logger.info("已派生时间线版本：%s ← %s（reason=%s）",
                    child.revision_id, parent.revision_id, reason)
        return {"revision": child.to_dict(), "parent": parent.to_dict()}

    # ============================================================
    # 读取
    # ============================================================

    def get_revision(self, revision_id: str) -> Optional[Dict[str, Any]]:
        rev = self.timeline_repo.get_revision(revision_id)
        return rev.to_dict() if rev else None

    def list_revisions(self, *, project: str = "", episode: str = "",
                       limit: int = 100) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.timeline_repo.list_revisions(
            project=project, episode=episode, limit=limit)]

    def latest_revision(self, project: str, episode: str) -> Optional[Dict[str, Any]]:
        rev = self.timeline_repo.latest_revision(project, episode)
        return rev.to_dict() if rev else None

    def lineage(self, revision_id: str) -> List[Dict[str, Any]]:
        """版本血缘（当前 → 根），回答「这版剪辑是从哪一版改来的」。"""
        return [r.to_dict() for r in self.timeline_repo.lineage(revision_id)]

    # ============================================================
    # 预检（compose:preflight）
    # ============================================================

    def preflight(self, revision_id: str, *, approved_only: bool = False,
                  renderer: str = "ffmpeg", preset: str = "") -> Dict[str, Any]:
        """``compose:preflight`` —— 返回渲染计划 + ``compose_fingerprint``。

        ``ok=False`` 时**必须阻断**渲染（fail-closed）。
        预检的意义就是把问题挡在烧卡之前。

        ``approved_only=True`` 只影响**警告**：未批准的版本仍允许渲预览
        （两级生产需要它），但会明确提示"这是预览，不是可交付成片"。
        它读取的是独立批准记录，**绝不由"已采用"推导批准**（铁律 1）。
        """
        report = self.timeline_repo.preflight(
            revision_id, self.facts_repo, approved_only=approved_only,
            renderer=renderer, preset=preset)
        rev = self.timeline_repo.get_revision(revision_id)
        if rev is not None:
            report["total_duration_sec"] = rev.total_duration_sec()
            report["declared_silences"] = int(rev.declared_silences)
            report["selection_implies_approval"] = False
        logger.info("时间线预检 %s：ok=%s fp=%s errors=%d warnings=%d",
                    revision_id, report.get("ok"), report.get("compose_fingerprint"),
                    len(report.get("errors") or ()), len(report.get("warnings") or ()))
        return report

    def render_manifest(self, revision_id: str, *,
                        renderer: str = "ffmpeg", preset: str = "") -> Dict[str, Any]:
        """取冻结的渲染计划（渲染器**只**消费本对象）。

        含 ``silence_declarations``：合法静音在计划里是显式条目，
        不是渲染器"发现没声音就补一条"。
        """
        rev = self.timeline_repo.get_revision(revision_id)
        if rev is None:
            raise TimelineServiceError("时间线版本不存在：%s" % revision_id)
        manifest = build_render_manifest(rev, renderer=renderer, preset=preset)
        return manifest.to_dict()

    def verify_render(self, revision_id: str, probed: Mapping[str, Any], *,
                      tolerance_sec: float = 0.5) -> Dict[str, Any]:
        """渲完对照计划：时长漂移立即报出来，而不是等用户看片才发现。"""
        rev = self.timeline_repo.get_revision(revision_id)
        if rev is None:
            raise TimelineServiceError("时间线版本不存在：%s" % revision_id)
        manifest = build_render_manifest(rev)
        ok, reason = manifest.verify_result(dict(probed or {}), tolerance_sec=tolerance_sec)
        return {"ok": ok, "reason": reason,
                "compose_fingerprint": rev.compose_fingerprint,
                "planned_duration_sec": manifest.total_duration_sec}

    def find_by_fingerprint(self, compose_fingerprint: str) -> List[Dict[str, Any]]:
        """同指纹的历史版本（判「这版剪辑其实渲过」）。"""
        return [r.to_dict() for r in self.timeline_repo.find_by_fingerprint(compose_fingerprint)]

    # ============================================================
    # 合成预检 + 按冻结计划渲染（ADR-0012 决策第 3 点）
    # -----------------------------------------------------------------
    # 本节是「让渲染器真正消费冻结计划」的**唯一**编排入口。
    # 旧路径（:mod:`video_postprocess` 的 ``generate_final_video``）
    # 仍然按磁盘上现有文件直接合并，**一条行为都不变** ——
    # 新路径默认关闭，只有显式经 :meth:`compose_preflight` → :meth:`render_from_plan`
    # 才走到这里。
    # ============================================================

    def _resolve_media(self, rev: TimelineRevision) -> Dict[str, Dict[str, Any]]:
        """从 ``facts_repo`` 取计划里每个版本的登记事实。

        **fail-closed**：``facts_repo`` 未注入时直接报错。
        「按磁盘上有什么就合什么」正是本 ADR 要消灭的行为，
        没有版本记录就没有「成片里那 3 秒是哪一版」这个答案。
        """
        if self.facts_repo is None:
            raise TimelineServiceError(
                "按冻结计划渲染需要注入 facts_repo（时间线的每镜必须对应一个已登记的 "
                "MediaVersion，否则无法回答成片里那几秒是哪一版）")
        ids = [i.media_version_id for i in rev.items]
        ids += [i.audio_media_version_id for i in rev.items if i.audio_media_version_id]
        return self.facts_repo.media_index(ids)

    def compose_preflight(self, revision_id: str, *, renderer: str = "ffmpeg",
                          preset: str = "", approved_only: bool = True,
                          require_approved: bool = True) -> Dict[str, Any]:
        """**合成前置预检**（四个阻断点都在这里收口，fail-closed）。

        阻断项（任一不成立 → ``ok=False``，调用方**必须**阻断渲染）：

        1. 既有 ``preflight`` 的 ``undeclared_silence`` 与指纹自校验失败；
        2. 计划引用的 ``media_version_id`` **未登记**或磁盘现状对不上
           （``media_index`` 缺失 / ``disk_verified=False``）——
           登记与现状对不上意味着「批准的字节」已不是磁盘上的字节；
        3. ``require_approved=True`` 时任一版本**未批准或批准已失效**。
           口径复用既有 ``approved`` 字段（由 :meth:`FactsRepo.media_index`
           从**独立批准表**读，**绝不由「已采用」推导**）；
        4. 媒体路径解析不出来（版本登记了但 ``path`` 为空/文件不存在）。

        ``approved_only=True`` 时未批准走**警告**（两级生产渲预览用），
        但 ``require_approved=True`` 直接**阻断** —— 「机器检查 ≠ 人工批准」。
        """
        rev = self.timeline_repo.get_revision(revision_id)
        if rev is None:
            raise TimelineServiceError("时间线版本不存在：%s" % revision_id)

        report = self.preflight(revision_id, approved_only=approved_only,
                                renderer=renderer, preset=preset)
        errors = list(report.get("errors") or ())
        media_index = self._resolve_media(rev)
        paths: Dict[str, str] = {}
        unapproved: List[str] = []
        for item in rev.items:
            rec = media_index.get(item.media_version_id)
            if rec is None:
                errors.append("镜头 %s 的 media_version_id=%s 未登记"
                              % (item.shot_key, item.media_version_id))
                continue
            if not rec.get("disk_verified", True):
                errors.append("镜头 %s 的版本 %s 与磁盘现状对不上（登记 sha256=%s 现状=%s）"
                              "，拒绝渲染：批准过的字节已不是磁盘上的字节"
                              % (item.shot_key, item.media_version_id,
                                 str(rec.get("media_sha256") or "")[:12],
                                 str(rec.get("media_sha256_now") or "")[:12]))
            if require_approved and not rec.get("approved"):
                unapproved.append("%s(%s)" % (item.shot_key, item.media_version_id))
            path = str(rec.get("path") or "")
            if not path or not os.path.isfile(path):
                errors.append("镜头 %s 的版本 %s 未解析到可读文件（登记 path=%r）"
                              % (item.shot_key, item.media_version_id, path))
            else:
                paths[item.media_version_id] = path
            if item.audio_media_version_id:
                arec = media_index.get(item.audio_media_version_id)
                apath = str((arec or {}).get("path") or "")
                if arec is None:
                    errors.append("镜头 %s 的配音轨 %s 未登记"
                                  % (item.shot_key, item.audio_media_version_id))
                elif not apath or not os.path.isfile(apath):
                    errors.append("镜头 %s 的配音轨 %s 未解析到可读文件（登记 path=%r）"
                                  % (item.shot_key, item.audio_media_version_id, apath))
                else:
                    paths[item.audio_media_version_id] = apath
        if unapproved:
            errors.append("以下镜头使用的版本未批准或批准已失效（机器检查通过不等于人工批准）：%s"
                          % "、".join(unapproved))
        out = dict(report)
        out["errors"] = errors
        out["ok"] = not errors
        out["require_approved"] = bool(require_approved)
        out["media_versions_checked"] = len(media_index)
        logger.info("合成预检 %s：ok=%s errors=%d（require_approved=%s）",
                    revision_id, out["ok"], len(errors), require_approved)
        return out

    def build_plan(self, revision_id: str, *, renderer: str = "ffmpeg",
                   preset: str = "", manifest: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """冻结计划 → **可执行**合成计划（含 ffmpeg 参数），纯逻辑。"""
        rev = self.timeline_repo.get_revision(revision_id)
        if rev is None:
            raise TimelineServiceError("时间线版本不存在：%s" % revision_id)
        media_index = self._resolve_media(rev)
        paths = {mid: str(rec.get("path") or "")
                 for mid, rec in media_index.items() if rec.get("path")}
        plan = build_compose_plan(rev, manifest=manifest, media_paths=paths,
                                  renderer=renderer, preset=preset)
        ffmpeg = compose_plan_to_ffmpeg_args(plan)
        out = plan.to_dict()
        out["ffmpeg"] = ffmpeg
        return out

    def ffmpeg_args_for_plan(self, plan_data: Mapping[str, Any], **kwargs: Any) -> Dict[str, Any]:
        """已序列化的计划 → ffmpeg 参数（纯函数，便于前端预览合成指令）。"""
        plan = plan_data if isinstance(plan_data, ComposePlan) \
            else ComposePlan.from_dict(plan_data)
        return compose_plan_to_ffmpeg_args(plan, **kwargs)

    def record_render(self, plan_data: Mapping[str, Any], *, render_id: str = "",
                      output_path: str = "", output_sha256: str = "",
                      output_bytes: int = 0, output_duration_sec: float = 0.0,
                      authorized_by: str = "", authorization_ref: str = "",
                      note: str = "") -> Dict[str, Any]:
        """成片登记（ADR-0012 决策第 3 点）：把 ``compose_fingerprint`` 与实际产物绑定。

        指纹**从计划算出来**，不接受调用方手填 —— 成片与计划因此不可分。
        内容变了（剪辑变了 / 媒体被重渲覆盖）指纹就变，历史成片因此永远能回答
        「它是哪一版剪辑、哪一批字节渲出来的」。
        """
        plan = plan_data if isinstance(plan_data, ComposePlan) \
            else ComposePlan.from_dict(plan_data)
        rid = str(render_id or "").strip() or ("rv_%s" % uuid.uuid4().hex[:16])
        rv = build_episode_render_version(rid, plan, output_path=output_path,
                                          output_sha256=output_sha256,
                                          output_bytes=int(output_bytes or 0),
                                          output_duration_sec=float(output_duration_sec or 0.0),
                                          authorized_by=authorized_by,
                                          authorization_ref=authorization_ref, note=note)
        if not authorized_by:
            raise TimelineServiceError("成片登记必须记录授权主体 authorized_by"
                                       "（机器检查通过不等于人工批准）")
        # ADR-0012 residual 已关闭：登记**必须落库**，请求结束即丢的响应字段
        # 不算事实。仓储层 append-only 且按 render_id 唯一，重复登记会冲突。
        self.timeline_repo.save_render_version(rv)
        logger.info("已登记成片版本：%s（rev=%s compose_fp=%s plan_fp=%s）",
                    rv.render_id, rv.revision_id, rv.compose_fingerprint[:12],
                    rv.plan_fingerprint[:12])
        return rv.to_dict()

    def get_render_version(self, render_id: str) -> Optional[Dict[str, Any]]:
        """跨请求读取成片登记（渲染结果的持久化事实）。"""
        rv = self.timeline_repo.get_render_version(render_id)
        return rv.to_dict() if rv else None

    def list_render_versions(self, *, revision_id: str = "", project: str = "",
                             episode: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        """列出某 revision / 项目的成片登记（交付与追溯用）。"""
        return [r.to_dict() for r in self.timeline_repo.list_render_versions(
            revision_id=revision_id, project=project, episode=episode, limit=limit)]

    def find_render_versions(self, compose_fingerprint: str) -> List[Dict[str, Any]]:
        """按剪辑指纹反查历史成片登记（免重渲判据）。"""
        return [r.to_dict() for r in self.timeline_repo.find_render_versions(
            compose_fingerprint)]

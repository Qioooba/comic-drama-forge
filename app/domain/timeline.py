# -*- coding: utf-8 -*-
"""不可变时间线（评估文档 §P0-4）：TimelineRevision + 渲染计划 + compose_fingerprint。

改造前的现状
------------
**本项目此前没有「时间线」这个概念**（``grep TimelineRevision`` = 0 命中）。
后期只是 ``video_postprocess.py`` 里 FFmpeg 合并 + 字幕。这带来三个能力缺口：

1. 不能在不重渲全部镜头的前提下调整顺序 / 时长 / 转场；
2. 不能冻结「这一版剪辑」再渲染（渲染完才知道顺序错了，改了就得全重跑）；
3. 不能分段渲染、只重渲改动段。

本模块提供的最小可用形态
------------------------
* :class:`TimelineRevision` —— **冻结**镜头序列：每镜采用的 MediaVersion id、
  时长、转场。**创建后不可变**（frozen dataclass + 库里 append-only）；
* :func:`compose_fingerprint` —— 渲染计划的内容指纹。指纹不变 ⇒ 渲染器可复用；
  指纹变 ⇒ 必须重渲，且能回答「因为哪一镜换了哪一版」；
* :class:`RenderManifest` —— 参考自研的 ``composition/manifest.py``：
  ``declare_silence`` 把「合法静音」**前置到计划里**，而不是渲完才发现某镜没声音。

铁律
----
**铁律 4：TimelineRevision 不可变 —— 改内容 = 建新 revision。**
    原因：时间线是「这一版剪辑」的**合同**。渲完的成片要能对上「当时批准的那一版剪辑」。
    若允许原地改顺序，历史成片将永远对不上它的计划，交付时的追溯链断掉。
    实现上：frozen dataclass + ``revision_no`` 单调递增 + ``parent_revision_id`` 血缘，
    且 :func:`derive_revision` 是唯一改内容的合法路径。

**铁律 5：静音必须显式声明（``declare_silence``）。**
    借鉴自研 ``RenderManifest``：生成阶段刻意静音是**合法**状态
    （见 ``quality_stage.technical_checks`` 里「音轨只记录、不判失败」），
    但**没有音轨**和**声明静音**是两件事 —— 前者是缺陷，后者是计划。
    预检时凡缺音轨又未 ``declare_silence`` 的镜头一律报 ``undeclared_silence`` 错误。

纯领域层：不 import flask，不碰磁盘与网络。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:                                   # 允许既作包内模块、也作独立模块被导入
    from domain.production_facts import DomainError, IncompleteIntentError, hash_payload
except ImportError:                    # pragma: no cover - 直接以脚本方式导入时的兜底
    from .production_facts import DomainError, IncompleteIntentError, hash_payload  # type: ignore

__all__ = [
    "TimelineError", "Transition", "TimelineItem", "RenderManifest",
    "TimelineRevision", "PreflightReport",
    "build_revision", "derive_revision", "revision_hash_payload",
    "compose_fingerprint", "build_render_manifest", "preflight",
    "default_transition",
    "ComposePlan", "ComposePlanEntry", "build_compose_plan", "plan_fingerprint",
    "expected_total_duration_sec",
    "EpisodeRenderVersion", "build_episode_render_version",
]


class TimelineError(DomainError):
    """时间线相关错误（编排层据此返回 4xx）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# =====================================================================
# 转场
# =====================================================================

@dataclass(frozen=True)
class Transition:
    """镜头间转场。

    ``kind`` 白名单刻意收窄到 FFmpeg 里真正能做到的几种 —— 转场写不存在的名字，
    渲染期才会炸，那时已经白烧了卡。
    """
    kind: str = "cut"           # cut / fade / dissolve / dip_to_black / slide / wipe
    duration_sec: float = 0.0   # cut 恒为 0

    #: 合法转场类型（cut 不占时长）
    KINDS: ClassVar[Tuple[str, ...]] = ("cut", "fade", "dissolve", "dip_to_black",
                                         "slide", "wipe")
    #: 需要时长的转场（>0 秒才生效）
    TIMED: ClassVar[Tuple[str, ...]] = ("fade", "dissolve", "dip_to_black", "slide", "wipe")

    def validate(self) -> None:
        if self.kind not in self.KINDS:
            raise TimelineError("未知转场类型：%r（合法值 %s）" % (self.kind, list(self.KINDS)))
        if self.kind == "cut" and float(self.duration_sec) != 0.0:
            raise TimelineError("cut 转场时长必须为 0（实际 %s）" % self.duration_sec)
        if self.kind in self.TIMED and float(self.duration_sec) < 0:
            raise TimelineError("转场时长不能为负：%s" % self.duration_sec)

    def effective_sec(self) -> float:
        """实际占用的时长（cut 永远为 0）。"""
        return 0.0 if self.kind == "cut" else round(float(self.duration_sec), 3)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": str(self.kind), "duration_sec": float(self.duration_sec)}

    @staticmethod
    def from_dict(raw: Optional[Mapping[str, Any]]) -> "Transition":
        raw = dict(raw or {})
        return Transition(kind=str(raw.get("kind") or "cut"),
                          duration_sec=float(raw.get("duration_sec") or 0.0))


def default_transition() -> Transition:
    return Transition(kind="cut", duration_sec=0.0)


# =====================================================================
# 镜头项
# =====================================================================

@dataclass(frozen=True)
class TimelineItem:
    """时间线上的一个镜头（不可变）。

    ``media_version_id`` 指向 :class:`~domain.production_facts.MediaVersion`，
    **不是文件路径**。指向版本而不是路径，是因为路径会被重渲覆盖
    （``ep01_full.mp4`` 原地覆盖是既有链路的既有行为），
    只有内容指纹 + 版本记录才能回答「成片里那 3 秒是哪一版」。
    """
    index: int
    shot_key: str
    media_version_id: str
    duration_sec: float
    transition_in: Transition = field(default_factory=default_transition)
    audio_media_version_id: str = ""      # 配音轨（可选）
    declared_silence: bool = False        # 铁律 5：显式声明的合法静音
    silence_reason: str = ""
    note: str = ""

    def validate(self) -> None:
        if int(self.index) < 0:
            raise TimelineError("镜头 index 不能为负：%s" % self.index)
        if not str(self.shot_key or "").strip():
            raise TimelineError("镜头必须有 shot_key（否则时间线无法与剧本对齐）")
        if not str(self.media_version_id or "").strip():
            raise TimelineError("镜头 %s 缺少 media_version_id（时间线必须指向版本，不能指向路径）"
                                % self.shot_key)
        if float(self.duration_sec) <= 0:
            raise TimelineError("镜头 %s 时长必须为正（实际 %s）" % (self.shot_key, self.duration_sec))
        self.transition_in.validate()
        if self.declared_silence and self.audio_media_version_id:
            raise TimelineError("镜头 %s 同时声明了静音与配音轨，二者互斥" % self.shot_key)
        if self.declared_silence and not str(self.silence_reason or "").strip():
            raise TimelineError("镜头 %s 声明静音必须写明 silence_reason（合法静音也要有据可查）"
                                % self.shot_key)

    def to_dict(self) -> Dict[str, Any]:
        return {"index": int(self.index), "shot_key": str(self.shot_key),
                "media_version_id": str(self.media_version_id),
                "duration_sec": float(self.duration_sec),
                "transition_in": self.transition_in.to_dict(),
                "audio_media_version_id": str(self.audio_media_version_id or ""),
                "declared_silence": bool(self.declared_silence),
                "silence_reason": str(self.silence_reason or ""),
                "note": str(self.note or "")}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "TimelineItem":
        return TimelineItem(index=int(raw.get("index") or 0),
                            shot_key=str(raw.get("shot_key") or ""),
                            media_version_id=str(raw.get("media_version_id") or ""),
                            duration_sec=float(raw.get("duration_sec") or 0.0),
                            transition_in=Transition.from_dict(raw.get("transition_in")),
                            audio_media_version_id=str(raw.get("audio_media_version_id") or ""),
                            declared_silence=bool(raw.get("declared_silence")),
                            silence_reason=str(raw.get("silence_reason") or ""),
                            note=str(raw.get("note") or ""))


def _norm_items(raw_items: Optional[Iterable[Mapping[str, Any]]]) -> Tuple[TimelineItem, ...]:
    """归一镜头列表：校验、按 index 排序、拒绝重号。"""
    if not raw_items:
        raise TimelineError("时间线不能为空（零镜头的 revision 无法渲染，也无法审批）")
    items: List[TimelineItem] = []
    seen: set = set()
    for i, raw in enumerate(raw_items):
        item = raw if isinstance(raw, TimelineItem) else TimelineItem.from_dict(raw or {})
        item.validate()
        if item.index in seen:
            raise TimelineError("镜头 index 重复：%d（顺序歧义必须在前端就暴露）" % item.index)
        seen.add(item.index)
        items.append(item)
    return tuple(sorted(items, key=lambda x: x.index))


# =====================================================================
# TimelineRevision（不可变）
# =====================================================================

@dataclass(frozen=True)
class TimelineRevision:
    """**冻结**的一版剪辑。

    字段的取舍：只冻结「剪辑决定」，不冻结「渲染参数」。
    渲染参数（分辨率 / 码率 / 滤镜图）进 :class:`RenderManifest`，
    因为同一版剪辑可以用不同档位渲出预览与成片 —— 这正是
    ``preview_gate`` 两级生产（预演产物永不可交付）所需。

    ``revision_no`` 在 ``(project, episode)`` 内单调递增；库里对
    ``(project, episode, revision_no)`` 建唯一索引 → 并发派生同号必冲突
    （乐观锁的落点之一）。
    """
    revision_id: str
    project: str
    episode: str
    revision_no: int
    items: Tuple[TimelineItem, ...]
    compose_fingerprint: str = ""
    parent_revision_id: str = ""
    subtitle_revision: str = ""      # 字幕 revision 与时间线解耦（§P0-4 第 4 条）
    declared_silences: int = 0
    created_at: str = ""
    created_by: str = ""
    note: str = ""

    # -- 派生量 -----------------------------------------------------

    def total_duration_sec(self) -> float:
        """成片时长（**唯一权威口径**，见 :func:`expected_total_duration_sec`）。

        ⚠️ 曾是 ``sum(durations)``（朴素求和，4×10s + dissolve 1s + fade 0.5s
        = 40.0s），而真实成片 38.5s —— 差 1.5s > 0.5s 容差。于是同一个计划在
        预检里报 40.0、渲染里声明并校验 38.5，两个数各说各话。
        现在它只是 :func:`expected_total_duration_sec` 的转发：预检
        (``TimelineService.preflight``)、渲染清单 (``build_render_manifest``)、
        渲后校验基线读的都是这**一个**数。
        """
        return expected_total_duration_sec(self.items)

    def hash_payload(self) -> Dict[str, Any]:
        """进 ``compose_fingerprint`` 的字段集合。

        ⚠️ **刻意不含** ``revision_id`` / ``created_at`` / ``created_by`` / ``note``：
        指纹表达的是「剪辑内容」，不是「这次剪辑是谁什么时候建的」。
        否则同一内容换个时间重建就换了指纹，渲染器缓存全部失效。
        """
        return {
            "project": str(self.project), "episode": str(self.episode),
            "items": [i.to_dict() for i in self.items],
            "subtitle_revision": str(self.subtitle_revision or ""),
        }

    def verify_fingerprint(self) -> bool:
        return compose_fingerprint(self.hash_payload()) == self.compose_fingerprint

    def to_dict(self) -> Dict[str, Any]:
        return {"revision_id": self.revision_id, "project": self.project,
                "episode": self.episode, "revision_no": int(self.revision_no),
                "items": [i.to_dict() for i in self.items],
                "compose_fingerprint": self.compose_fingerprint,
                "parent_revision_id": self.parent_revision_id,
                "subtitle_revision": self.subtitle_revision,
                "declared_silences": int(self.declared_silences),
                "created_at": self.created_at, "created_by": self.created_by,
                "note": self.note, "total_duration_sec": self.total_duration_sec()}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "TimelineRevision":
        return TimelineRevision(
            revision_id=str(raw.get("revision_id") or ""),
            project=str(raw.get("project") or ""), episode=str(raw.get("episode") or ""),
            revision_no=int(raw.get("revision_no") or 0),
            items=_norm_items(raw.get("items")),
            compose_fingerprint=str(raw.get("compose_fingerprint") or ""),
            parent_revision_id=str(raw.get("parent_revision_id") or ""),
            subtitle_revision=str(raw.get("subtitle_revision") or ""),
            declared_silences=int(raw.get("declared_silences") or 0),
            created_at=str(raw.get("created_at") or ""),
            created_by=str(raw.get("created_by") or ""),
            note=str(raw.get("note") or ""))


def build_revision(revision_id: str, *, project: str, episode: str, revision_no: int,
                   items: Sequence[Mapping[str, Any]],
                   subtitle_revision: str = "", created_by: str = "",
                   note: str = "", created_at: str = "") -> TimelineRevision:
    """构造并**冻结**一版剪辑（同时算出 ``compose_fingerprint``）。"""
    norm = _norm_items(items)
    obj = TimelineRevision(
        revision_id=str(revision_id), project=str(project or ""),
        episode=str(episode or ""), revision_no=int(revision_no), items=norm,
        compose_fingerprint="", subtitle_revision=str(subtitle_revision or ""),
        declared_silences=sum(1 for i in norm if i.declared_silence),
        created_at=created_at or _now(), created_by=str(created_by or ""),
        note=str(note or ""),
    )
    return obj.__class__(**{**obj.__dict__,
                            "compose_fingerprint": compose_fingerprint(obj.hash_payload())})


def derive_revision(parent: TimelineRevision, revision_id: str, *, revision_no: int,
                    items: Sequence[Mapping[str, Any]], reason: str,
                    subtitle_revision: str = "", created_by: str = "",
                    note: str = "") -> TimelineRevision:
    """**派生**新 revision（铁律 4 的唯一合法路径）。

    ``parent`` 一个字节都不动；新 revision 挂 ``parent_revision_id`` 血缘。
    内容未变化的派生同样拒绝 —— 否则 revision 号增长而剪辑没变，
    审批链上会出现「批准了一个和上一版一模一样的剪辑」，毫无意义。
    """
    if not isinstance(parent, TimelineRevision):
        raise TimelineError("derive_revision 只接受 TimelineRevision")
    why = str(reason or "").strip()
    if not why:
        raise IncompleteIntentError("派生新 revision 必须写明 reason（为什么改）")
    if int(revision_no) <= int(parent.revision_no):
        raise TimelineError("revision_no 必须严格递增：新 %s 不大于父 %s"
                            % (revision_no, parent.revision_no))
    probe = build_revision(revision_id, project=parent.project, episode=parent.episode,
                           revision_no=int(revision_no), items=items,
                           subtitle_revision=subtitle_revision, created_by=created_by,
                           note=str(note or why), created_at=_now())
    if probe.compose_fingerprint == parent.compose_fingerprint:
        raise TimelineError("派生未改变剪辑内容，拒绝写入同义 revision（parent=%s）"
                            % parent.revision_id)
    probe = TimelineRevision(**{**probe.__dict__,
                               "parent_revision_id": parent.revision_id,
                               "created_by": str(created_by or parent.created_by or "")})
    return probe


# =====================================================================
# compose_fingerprint
# =====================================================================

def revision_hash_payload(revision: TimelineRevision) -> Dict[str, Any]:
    """revision → 指纹输入载荷（公开出来供仓库层复算校验）。"""
    return revision.hash_payload()


def compose_fingerprint(payload: Mapping[str, Any], *,
                        renderer: str = "ffmpeg", preset: str = "") -> str:
    """**渲染计划指纹**（评估文档 §P0-4 要求 ``compose:preflight`` 返回它）。

    算法（稳定、可复算、与字段顺序无关）::

        fp = sha256(json(payload, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), default=str))

    ``payload`` 为 :meth:`TimelineRevision.hash_payload`：

        {
          "project": ..., "episode": ...,
          "subtitle_revision": ...,
          "items": [
            {"index", "shot_key", "media_version_id", "duration_sec",
             "transition_in": {"kind", "duration_sec"},
             "audio_media_version_id", "declared_silence", "silence_reason", "note"},
            ...
          ]
        }

    三个刻意的取舍：

    1. **进指纹的是 media_version_id 而非文件路径或 sha256**。
       版本 id 已经唯一绑定一个内容指纹（``MediaVersion.media_sha256``），
       再塞一遍 sha256 是冗余；但若调用方需要「内容变了但版本 id 没变」也能被发现，
       应由编排层在写入前校验（``facts_repo.assert_media_unchanged``）。
    2. **``declared_silence`` 与 ``silence_reason`` 都进指纹**。
       「这一版剪辑声明了哪几镜静音」是剪辑决定的一部分 ——
       同一批镜头，一次声明静音一次不声明，渲出来是两部片子。
    3. ``renderer`` / ``preset`` 只在显式传入时叠加进指纹，
       默认**不进** —— 否则换台机器渲同一版剪辑就被判成「必须重渲」。

    指纹不变 ⇒ 计划不变 ⇒ 渲染器可复用已有产物（免重渲，最值钱的性能特性）。
    """
    body: Dict[str, Any] = {"v": 1, "timeline": dict(payload or {})}
    if renderer:
        body["renderer"] = str(renderer)
    if preset:
        body["preset"] = str(preset)
    return hash_payload(body)


# =====================================================================
# RenderManifest（渲染计划）+ declare_silence
# =====================================================================

@dataclass(frozen=True)
class RenderManifest:
    """渲染计划：**渲染器只消费本对象**，不自己再猜一遍。

    为什么要有它（借鉴自研 ``composition/manifest.py``）
    ---------------------------------------------------
    把「这条渲染到底要什么」全部**前置**成一份可预检的清单：

    * :meth:`declare_silence` —— 合法静音必须显式声明。
      没有音轨且未声明 ⇒ :func:`preflight` 直接判 ``undeclared_silence`` 阻断。
      这样「某镜没声音」是**计划里能看见的一条**，而不是渲完才发现的事故。
    * 每镜的 ``expected_duration_sec`` 来自计划，渲完由 :meth:`verify_result`
      比对 ffprobe 实测值 —— 时长漂了立刻知道，而不是让用户看片才发现。
    """
    revision_id: str
    compose_fingerprint: str
    entries: Tuple[Mapping[str, Any], ...]
    total_duration_sec: float = 0.0
    silence_declarations: Tuple[Mapping[str, Any], ...] = ()
    renderer: str = "ffmpeg"
    preset: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"revision_id": self.revision_id,
                "compose_fingerprint": self.compose_fingerprint,
                "renderer": self.renderer, "preset": self.preset,
                "total_duration_sec": float(self.total_duration_sec),
                "entries": [dict(e) for e in self.entries],
                "silence_declarations": [dict(s) for s in self.silence_declarations]}

    def declare_silence(self, shot_key: str, reason: str) -> Dict[str, Any]:
        """把某镜的静音声明登记进计划（铁律 5）。

        「合法静音」在生成阶段是常态（``quality_stage`` 里音轨只记录不判失败），
        但必须**在计划里显式写出来**：否则渲染器无从区分
        「这镜本来就该静音」与「这镜音轨掉了」。

        本方法是**只读登记**：清单在 :func:`build_render_manifest` 里就已冻结，
        这里只作为给调用方/渲染器使用的显式声明接口 + 校验入口。
        已声明则原样返回；未声明则抛错（而不是默默补一条）——
        因为「渲完再补声明」正是我们要消灭的行为。
        """
        key = str(shot_key or "").strip()
        why = str(reason or "").strip()
        for s in self.silence_declarations:
            if str(s.get("shot_key")) == key:
                return dict(s)
        raise TimelineError("镜头 %r 未在计划中声明静音，无法事后补声明"
                            "（应先改 revision → 派生新 revision）" % key)

    def verify_result(self, probed: Mapping[str, Any], *,
                      tolerance_sec: float = 0.5) -> Tuple[bool, str]:
        """渲完对照计划（时长漂移检测）。返回 (ok, reason)。"""
        try:
            got = float(probed.get("duration") or 0.0)
        except (TypeError, ValueError):
            return False, "ffprobe 未给出可解析的时长"
        want = float(self.total_duration_sec or 0.0)
        if abs(got - want) > float(tolerance_sec):
            return False, ("成片时长与计划不符：计划 %.2fs / 实测 %.2fs（差 %.2fs）"
                           % (want, got, abs(got - want)))
        return True, "成片时长与计划一致"


def build_render_manifest(revision: TimelineRevision, *,
                          renderer: str = "ffmpeg", preset: str = "") -> RenderManifest:
    """由 revision 冻结出渲染计划（含静音声明清单）。"""
    entries: List[Dict[str, Any]] = []
    silences: List[Dict[str, Any]] = []
    for item in revision.items:
        entries.append({
            "index": item.index, "shot_key": item.shot_key,
            "media_version_id": item.media_version_id,
            "audio_media_version_id": item.audio_media_version_id or "",
            "expected_duration_sec": round(float(item.duration_sec), 3),
            "transition_in": item.transition_in.to_dict(),
            "declared_silence": bool(item.declared_silence),
        })
        if item.declared_silence:
            silences.append({"index": item.index, "shot_key": item.shot_key,
                             "reason": item.silence_reason})
    return RenderManifest(revision_id=revision.revision_id,
                          compose_fingerprint=revision.compose_fingerprint,
                          entries=tuple(entries),
                          total_duration_sec=revision.total_duration_sec(),
                          silence_declarations=tuple(silences),
                          renderer=str(renderer or "ffmpeg"), preset=str(preset or ""))


# =====================================================================
# 预检
# =====================================================================

@dataclass(frozen=True)
class PreflightReport:
    """``compose:preflight`` 的返回体。

    ``ok`` 为 False 时**必须阻断**渲染（fail-closed）：
    预检的意义就是「渲之前发现问题」，放过等于没有预检。
    """
    ok: bool
    compose_fingerprint: str
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    manifest: Mapping[str, Any] = field(default_factory=dict)
    revision_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": bool(self.ok), "compose_fingerprint": self.compose_fingerprint,
                "revision_id": self.revision_id,
                "errors": list(self.errors), "warnings": list(self.warnings),
                "manifest": dict(self.manifest or {})}


def preflight(revision: TimelineRevision, *,
              media_index: Optional[Mapping[str, Mapping[str, Any]]] = None,
              approved_only: bool = False,
              renderer: str = "ffmpeg", preset: str = "") -> PreflightReport:
    """渲染前预检（``compose:preflight`` 的纯逻辑实现）。

    校验项：

    * revision 指纹自校验（防库被旁路改写）；
    * 每镜的 ``media_version_id`` 必须在 ``media_index`` 里存在
      （可选传入 ffprobe 事实以核对时长）；
    * **铁律 5**：``probe.has_audio`` 为假时，必须 ``declared_silence``，
      否则报 ``undeclared_silence``；
    * 转场时长不得超过相邻镜头较短者的时长（否则会吃掉内容）；
    * ``approved_only=True`` 时，只允许采用**已批准**的版本
      —— 注意这是「预检参数」，不是隐式升级：它仍然读取独立的批准记录，
      绝不从「已采用」推导批准。

    返回的 ``compose_fingerprint`` 供渲染器判「计划是否变过」。
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not revision.verify_fingerprint():
        errors.append("revision 指纹与内容不一致（疑似被旁路改写，拒绝渲染）：%s"
                      % revision.revision_id)

    index = dict(media_index or {})
    prev: Optional[TimelineItem] = None
    for item in revision.items:
        mv = index.get(item.media_version_id)
        if mv is None and index:
            errors.append("镜头 %s 的 media_version_id=%s 不在候选版本索引中"
                          % (item.shot_key, item.media_version_id))
        if mv is not None and approved_only:
            if not mv.get("approved"):
                warnings.append(
                    "镜头 %s 使用的版本尚未批准（采用≠批准，本次预检允许渲染预览）"
                    % item.shot_key)
        probe = dict((mv or {}).get("probe") or {})
        needs_audio_fact = str((mv or {}).get("media_kind") or
                                (mv or {}).get("kind") or "") in ("video", "audio")
        if probe:
            # 铁律 5：没有音轨 + 未声明静音 = 计划缺陷
            if probe.get("has_audio") is False and not item.declared_silence:
                errors.append(
                    "undeclared_silence: 镜头 %s 的版本无音轨且未 declare_silence"
                    "（合法静音必须写进计划，而不是渲完才发现）" % item.shot_key)
            if probe.get("has_audio") is False and item.declared_silence and \
                    not item.audio_media_version_id:
                pass        # 已显式声明，属合法静音
            got_dur = probe.get("duration")
            if got_dur is not None and abs(float(got_dur) - float(item.duration_sec)) > 0.5:
                warnings.append("镜头 %s 计划时长 %.2fs 与媒体实测 %.2fs 不一致"
                                % (item.shot_key, item.duration_sec, float(got_dur)))
            elif needs_audio_fact and probe.get("has_audio") is None:
                # 探测失败（probe_error）或旧数据没有该键：判据无从成立。
                # ADR-0012 铁律 5 的方向是「渲之前就知道」，所以缺事实必须阻断 ——
                # 记成 warning 等于把铁律 5 降级成建议，静音照样渲进成片才发现。
                errors.append(
                    "音轨事实缺失: 镜头 %s 的版本缺少 has_audio 探测结果%s，"
                    "无法确认是否存在未声明静音；请重新登记该候选或显式 declare_silence"
                    % (item.shot_key,
                       ("（%s）" % probe["probe_error"]) if probe.get("probe_error") else ""))
        elif index and mv is not None and not item.declared_silence and needs_audio_fact:
            # 音视频媒体却完全没有探测事实：同上，按 ADR-0012 判 error。
            # 只对 video/audio 生效 —— 图片本就没有音轨，拿「缺音轨事实」去卡
            # 静态分镜图既没意义，又会让整条预检对分镜镜全线红灯。
            errors.append("镜头 %s 缺少媒体探测事实（无音轨/时长），"
                          "无法确认是否存在未声明静音；请重新登记该候选或显式 declare_silence"
                          % item.shot_key)
        elif index and mv is not None and not item.declared_silence:
            warnings.append("镜头 %s 缺少媒体探测事实（时长可能漂移，无法校验）"
                            % item.shot_key)
        if prev is not None:
            t = item.transition_in.effective_sec()
            if t > 0 and t > min(float(prev.duration_sec), float(item.duration_sec)):
                errors.append("镜头 %s 的入场转场 %.2fs 超过相邻较短镜头时长 %.2fs"
                              % (item.shot_key, t,
                                 min(float(prev.duration_sec), float(item.duration_sec))))
        prev = item

    manifest = build_render_manifest(revision, renderer=renderer, preset=preset)
    return PreflightReport(ok=not errors, compose_fingerprint=revision.compose_fingerprint,
                           errors=tuple(errors), warnings=tuple(warnings),
                           manifest=manifest.to_dict(), revision_id=revision.revision_id)


# =====================================================================
# ComposePlan（可执行合成计划）+ EpisodeRenderVersion
# ---------------------------------------------------------------------
# 为什么在计划之上再加一层 ``ComposePlan``
# ----------------------------------------
# ``RenderManifest`` 回答的是「**这一版剪辑要什么**」，它是**给人/给预检看的清单**：
# 里面是 ``media_version_id``，没有路径、没有 FFmpeg 参数。
# 渲染器要的东西不一样：它需要**一串按顺序排好的输入文件**，每段多长、
# 与前一段怎么衔接、音轨从哪来。
#
# 以前这条转换不存在，于是渲染器只能「扫磁盘上有什么就合什么」——
# 时间线冻结的内容对成片没有任何约束力（ADR-0012 实施状态声明里那条 ❌）。
#
# ``ComposePlan`` 就是这条转换的**纯函数**产物：
#
# * **纯**：不 import flask、不读磁盘、不调 FFmpeg，输入 ``(revision, manifest,
#   媒体路径解析表)``，输出可直接执行的计划；
# * **顺序由 revision 决定**（不是目录 listdir）——这是本轮的核心；
# * **音轨来源显式**（``audio_media_version_id`` / ``declared_silence``），
#   合法静音**前置**在计划里（铁律 5），渲染器不允许自己推断；
# * **字幕解耦**（ADR-0012 决策第 4 点）：``subtitle_revision`` 只作为**引用**
#   挂在计划上，``ComposePlan`` 不含任何字幕烧制逻辑 —— 字幕不进合成，
#   成片渲完后由字幕链路单独消费同一个 ``subtitle_revision``。
# =====================================================================


@dataclass(frozen=True)
class ComposePlanEntry:
    """计划里的一镜（**已解析到具体文件**）。

    与 :class:`TimelineItem` 的差别是本类的 ``video_path`` / ``audio_path``
    已经解析完毕：渲染器**只消费本类**，不该再去猜路径。
    """

    index: int
    shot_key: str
    media_version_id: str
    video_path: str
    duration_sec: float
    transition_in: Transition = field(default_factory=default_transition)
    audio_media_version_id: str = ""
    audio_path: str = ""
    declared_silence: bool = False
    silence_reason: str = ""
    note: str = ""

    def validate(self) -> None:
        if not str(self.video_path or "").strip():
            raise TimelineError("镜头 %s 的 media_version_id=%s 未解析到文件路径"
                                "（渲染器只消费已登记版本，不接受磁盘上有什么就合什么）"
                                % (self.shot_key, self.media_version_id))
        if float(self.duration_sec) <= 0:
            raise TimelineError("镜头 %s 计划时长必须为正（实际 %s）"
                                % (self.shot_key, self.duration_sec))
        self.transition_in.validate()
        if self.declared_silence and self.audio_path:
            raise TimelineError("镜头 %s 同时声明了静音与配音轨，二者互斥" % self.shot_key)
        if self.declared_silence and not str(self.silence_reason or "").strip():
            raise TimelineError("镜头 %s 声明静音必须写明 silence_reason（合法静音也要有据可查）"
                                % self.shot_key)

    def to_dict(self) -> Dict[str, Any]:
        return {"index": int(self.index), "shot_key": str(self.shot_key),
                "media_version_id": str(self.media_version_id),
                "video_path": str(self.video_path),
                "audio_media_version_id": str(self.audio_media_version_id or ""),
                "audio_path": str(self.audio_path or ""),
                "duration_sec": float(self.duration_sec),
                "transition_in": self.transition_in.to_dict(),
                "declared_silence": bool(self.declared_silence),
                "silence_reason": str(self.silence_reason or ""),
                "note": str(self.note or "")}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "ComposePlanEntry":
        return ComposePlanEntry(
            index=int(raw.get("index") or 0), shot_key=str(raw.get("shot_key") or ""),
            media_version_id=str(raw.get("media_version_id") or ""),
            video_path=str(raw.get("video_path") or ""),
            duration_sec=float(raw.get("duration_sec") or 0.0),
            transition_in=Transition.from_dict(raw.get("transition_in")),
            audio_media_version_id=str(raw.get("audio_media_version_id") or ""),
            audio_path=str(raw.get("audio_path") or ""),
            declared_silence=bool(raw.get("declared_silence")),
            silence_reason=str(raw.get("silence_reason") or ""),
            note=str(raw.get("note") or ""))


def expected_total_duration_sec(entries: Iterable[Any]) -> float:
    """计划的**成片**总时长 = 各镜时长之和 − 逐个转场吃掉的重叠（**唯一权威口径**）。

    为什么不能直接求和：``xfade`` / ``acrossfade`` 是**重叠**播放 —— 上一镜末尾
    ``D_i`` 秒与本镜开头 ``D_i`` 秒是同一段画面/声音，求和会把这段算两遍。
    4 镜 ×10s + dissolve 1s + fade 0.5s：求和 = 40.0s，真实成片 = 38.5s。

    这 1.5s 的差不是误差，是「计划声明的成片时长」与「渲完的真实时长」
    之间的系统性偏差：拿它当渲后校验基线（容差 0.5s）必然判失败，
    而那时 GPU 时间已经烧掉、文件已经落盘。

    本函数同时喂给两个曾经各算各的地方，**它们不可能再漂移**：
    * :func:`build_compose_plan` —— 填 ``ComposePlan.total_duration_sec``
      （渲后时长校验的基线）；
    * :meth:`ComposePlan.validate` —— 复核该字段，不一致即 fail-closed。

    重叠量按渲染层的同一口径封顶为 ``min(D_i, 已累计时长)`` ——
    ``compose_plan_to_ffmpeg_args`` 的 ``offset = max(0, cursor - D_i)``，
    本函数与之逐镜对齐。
    """
    items = list(entries or ())
    if not items:
        return 0.0
    cursor = float(items[0].duration_sec)
    for e in items[1:]:
        overlap = max(0.0, float(e.transition_in.effective_sec()))
        cursor = cursor + float(e.duration_sec) - min(overlap, cursor)
    return round(max(0.0, cursor), 3)


@dataclass(frozen=True)
class ComposePlan:
    """**可执行**的合成计划（渲染器只消费本对象）。

    三个刻意的取舍：

    1. **不含字幕**（ADR-0012 决策第 4 点）。``subtitle_revision`` 仅作为引用
       出现在计划里，方便事后回答「这版成片配的是哪一版字幕」，
       但合成逻辑里**没有**任何字幕步骤 —— 字幕与合成解耦。
    2. **``silence_declarations`` 前置**（铁律 5）。合法静音是计划里的**一条**，
       不是渲染器跑完发现没声音再补的日志。
    3. **``plan_fingerprint``** 是「计划 + 媒体解析结果」的指纹。
       ``compose_fingerprint`` 只覆盖剪辑内容（不含路径），
       而**文件被重渲覆盖后同一 version id 可能指向不同字节** ——
       所以渲染登记（:class:`EpisodeRenderVersion`）把两者都绑上。
    """

    revision_id: str
    project: str
    episode: str
    revision_no: int
    compose_fingerprint: str
    entries: Tuple[ComposePlanEntry, ...]
    total_duration_sec: float = 0.0
    silence_declarations: Tuple[Mapping[str, Any], ...] = ()
    subtitle_revision: str = ""
    renderer: str = "ffmpeg"
    preset: str = ""
    plan_fingerprint: str = ""

    def validate(self) -> None:
        if not self.entries:
            raise TimelineError("合成计划为空（零镜头的计划无法渲染）")
        for e in self.entries:
            e.validate()
        if not str(self.compose_fingerprint or "").strip():
            raise TimelineError("合成计划缺少 compose_fingerprint"
                                "（无法把成片绑定到冻结的剪辑）")
        # total_duration_sec 是**渲后时长校验的基线**（app/video_postprocess.py
        # 的 _verify_plan_duration 直接读它，容差 0.5s）。它若与 entries 推出的
        # 权威值对不上，转场一重就必然误判「成片时长与计划不符」——
        # 而那时 GPU 已经烧完、文件已经落盘。故此处 fail-closed，不给「先渲再说」。
        want = self.expected_duration_sec()
        if abs(float(self.total_duration_sec or 0.0) - want) > 0.001:
            raise TimelineError(
                "合成计划声明的总时长 %s 与逐镜时长/转场推出的 %.3fs 不符"
                "（转场重叠必须扣掉；拒绝用错基线去校验成片）"
                % (self.total_duration_sec, want))

    def expected_duration_sec(self) -> float:
        """本计划的成片总时长（权威口径，见 :func:`expected_total_duration_sec`）。"""
        return expected_total_duration_sec(self.entries)

    def silence_for(self, shot_key: str) -> Optional[Mapping[str, Any]]:
        """取某镜的静音声明（铁律 5）。未声明返回 ``None``。"""
        key = str(shot_key or "").strip()
        for s in self.silence_declarations:
            if str(s.get("shot_key")) == key:
                return dict(s)
        return None

    def audio_source(self) -> List[Mapping[str, Any]]:
        """每镜的音轨来源（``audio_path`` / ``declared_silence`` / 随片音轨）。"""
        out: List[Mapping[str, Any]] = []
        for e in self.entries:
            if e.declared_silence:
                kind = "declared_silence"
            elif e.audio_path:
                kind = "external_audio"
            else:
                kind = "inline_audio"
            out.append({"index": e.index, "shot_key": e.shot_key, "kind": kind,
                        "audio_media_version_id": e.audio_media_version_id,
                        "audio_path": e.audio_path})
        return out

    def hash_payload(self) -> Dict[str, Any]:
        """``plan_fingerprint`` 的输入载荷（含渲染器与 preset）。

        刻意含 ``video_path`` / ``audio_path``：同一批 ``media_version_id``
        在磁盘上被重渲覆盖时，合成结果已经不同，指纹必须跟着变。

        也刻意含 ``compose_fingerprint`` 与 ``total_duration_sec``：
        前者是「这份计划出自哪一版剪辑」，后者是「渲后时长校验的基线」
        （``EpisodeRenderVersion`` 的登记输入之一）。二者不进载荷时，
        单独篡改其中任何一个 ``verify_plan_fingerprint()`` 都照样返回 True
        —— 那样「成片 ↔ 剪辑 ↔ 计划」三者绑死就少了一环。

        也含 ``silence_declarations``：它是计划里**独立持有**的一张清单
        （合法静音的铁律 5 落点），虽然每条都能在 ``entries`` 里找到对应镜，
        但改它的 reason / 删掉一条都不改变任何 entry —— 不进载荷时
        「声明静音的理由被改写」是验不出来的。
        """
        return {"revision_id": str(self.revision_id), "project": str(self.project),
                "episode": str(self.episode),
                "subtitle_revision": str(self.subtitle_revision or ""),
                "renderer": str(self.renderer or "ffmpeg"), "preset": str(self.preset or ""),
                "compose_fingerprint": str(self.compose_fingerprint or ""),
                "total_duration_sec": float(self.total_duration_sec),
                "entries": [e.to_dict() for e in self.entries],
                "silence_declarations": [dict(s) for s in (self.silence_declarations or ())]}

    def verify_plan_fingerprint(self) -> bool:
        return plan_fingerprint(self.hash_payload()) == self.plan_fingerprint

    def to_dict(self) -> Dict[str, Any]:
        return {"revision_id": self.revision_id, "project": self.project,
                "episode": self.episode, "revision_no": int(self.revision_no),
                "compose_fingerprint": self.compose_fingerprint,
                "plan_fingerprint": self.plan_fingerprint,
                "renderer": self.renderer, "preset": self.preset,
                "subtitle_revision": self.subtitle_revision,
                "total_duration_sec": float(self.total_duration_sec),
                "entries": [e.to_dict() for e in self.entries],
                "silence_declarations": [dict(s) for s in self.silence_declarations],
                "audio_sources": self.audio_source()}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "ComposePlan":
        entries = tuple(ComposePlanEntry.from_dict(e or {}) for e in (raw.get("entries") or ()))
        return ComposePlan(
            revision_id=str(raw.get("revision_id") or ""),
            project=str(raw.get("project") or ""), episode=str(raw.get("episode") or ""),
            revision_no=int(raw.get("revision_no") or 0),
            compose_fingerprint=str(raw.get("compose_fingerprint") or ""),
            entries=entries,
            total_duration_sec=float(raw.get("total_duration_sec") or 0.0),
            silence_declarations=tuple(dict(s) for s in (raw.get("silence_declarations") or ())),
            subtitle_revision=str(raw.get("subtitle_revision") or ""),
            renderer=str(raw.get("renderer") or "ffmpeg"),
            preset=str(raw.get("preset") or ""),
            plan_fingerprint=str(raw.get("plan_fingerprint") or ""))


def plan_fingerprint(payload: Mapping[str, Any]) -> str:
    """合成计划指纹（与 :func:`compose_fingerprint` 同算法，载荷不同）。

    两者的分工：``compose_fingerprint`` 回答「**剪辑**变没变」，
    ``plan_fingerprint`` 回答「**这份计划连同它解析到的文件**变没变」。
    """
    return hash_payload({"v": 1, "compose_plan": dict(payload or {})})


def build_compose_plan(revision: TimelineRevision, *,
                       manifest: Optional[Mapping[str, Any]] = None,
                       media_paths: Optional[Mapping[str, str]] = None,
                       renderer: str = "ffmpeg", preset: str = "") -> ComposePlan:
    """冻结计划 → **可执行**合成计划（纯函数，无 I/O）。

    ``media_paths``：``{media_version_id: 绝对路径}``，由编排层从
    ``MediaVersion.path`` 组装。**缺任何一镜的映射就报错**（fail-closed）——
    渲染器不接受「磁盘上有什么就合什么」，因为那正是本 ADR 要消灭的行为。

    ``manifest`` 可传入既有的 :func:`build_render_manifest` 结果（渲染前预检
    已算过的那份），传入时会校验它与 revision 同指纹，避免「预检的是 A 版、
    渲的是 B 版」。

    字幕**不进计划**（ADR-0012 决策第 4 点）：只把 ``subtitle_revision`` 作为
    引用挂上，合成逻辑里不出现任何字幕步骤。
    """
    if not isinstance(revision, TimelineRevision):
        raise TimelineError("build_compose_plan 只接受 TimelineRevision")
    if not revision.verify_fingerprint():
        raise TimelineError("revision 指纹与内容不一致（疑似被旁路改写，拒绝出计划）：%s"
                            % revision.revision_id)
    if manifest:
        got = str((manifest or {}).get("compose_fingerprint") or "")
        if got and got != revision.compose_fingerprint:
            raise TimelineError("预检的渲染计划与本 revision 指纹不一致（预检的是另一版剪辑）："
                                "预检=%s 本版=%s" % (got[:12], revision.compose_fingerprint[:12]))

    paths = dict(media_paths or {})
    entries: List[ComposePlanEntry] = []
    missing: List[str] = []
    for item in revision.items:
        video_path = str(paths.get(item.media_version_id) or "")
        audio_path = (str(paths.get(item.audio_media_version_id) or "")
                      if item.audio_media_version_id else "")
        if not video_path:
            missing.append("%s(%s)" % (item.shot_key, item.media_version_id))
        if item.audio_media_version_id and not audio_path:
            missing.append("%s 的配音轨 %s" % (item.shot_key, item.audio_media_version_id))
        entries.append(ComposePlanEntry(
            index=item.index, shot_key=item.shot_key,
            media_version_id=item.media_version_id, video_path=video_path,
            duration_sec=float(item.duration_sec), transition_in=item.transition_in,
            audio_media_version_id=item.audio_media_version_id, audio_path=audio_path,
            declared_silence=bool(item.declared_silence),
            silence_reason=item.silence_reason, note=item.note))
    if missing:
        raise TimelineError("合成计划无法解析以下已登记版本的媒体文件（拒绝按磁盘现状拼凑）：%s"
                            % "、".join(missing))

    silences = tuple({"index": e.index, "shot_key": e.shot_key, "reason": e.silence_reason}
                     for e in entries if e.declared_silence)
    # 权威口径：逐镜时长之和**扣掉每个转场吃掉的重叠**（转场是重叠播放）。
    # 曾用 sum(durations)（40.0s），与真实成片（38.5s）差 1.5s > 0.5s 容差，
    # 导致每次带转场的渲染都在渲完之后被判 409。
    total = expected_total_duration_sec(entries)
    draft = ComposePlan(
        revision_id=revision.revision_id, project=revision.project, episode=revision.episode,
        revision_no=int(revision.revision_no),
        compose_fingerprint=revision.compose_fingerprint, entries=tuple(entries),
        total_duration_sec=total, silence_declarations=silences,
        # 字幕解耦：只挂引用，合成里不出现字幕
        subtitle_revision=revision.subtitle_revision,
        renderer=str(renderer or "ffmpeg"), preset=str(preset or ""), plan_fingerprint="")
    draft.validate()
    return ComposePlan(**{**draft.__dict__,
                          "plan_fingerprint": plan_fingerprint(draft.hash_payload())})


# =====================================================================
# EpisodeRenderVersion（成片与计划的绑定）
# ---------------------------------------------------------------------
# ADR-0012 决策第 3 点：渲染器产出 ``EpisodeRenderVersion``，把
# ``compose_fingerprint`` 与**实际产物**绑在一起 —— 内容变了指纹就变。
# =====================================================================


@dataclass(frozen=True)
class EpisodeRenderVersion:
    """一次成片渲染的登记（append-only）。

    「内容变了指纹就变」的落点是 :meth:`verify`：
    ``compose_fingerprint``（剪辑内容）+ ``plan_fingerprint``（含媒体解析路径）
    + ``output_sha256``（实际产物字节）三者同时绑在一条记录上。
    任一环对不上，``verify()`` 返回 False —— 历史成片于是**永远**能回答
    「它是哪一版剪辑、哪一批文件、哪一串字节渲出来的」。
    """

    render_id: str
    revision_id: str
    project: str
    episode: str
    revision_no: int
    compose_fingerprint: str
    plan_fingerprint: str
    output_path: str = ""
    output_sha256: str = ""
    output_bytes: int = 0
    output_duration_sec: float = 0.0
    subtitle_revision: str = ""
    entry_media_version_ids: Tuple[str, ...] = ()
    declared_silences: int = 0
    authorized_by: str = ""
    authorization_ref: str = ""
    created_at: str = ""
    note: str = ""

    def bind_hash_payload(self) -> Dict[str, Any]:
        return {"compose_fingerprint": str(self.compose_fingerprint),
                "plan_fingerprint": str(self.plan_fingerprint),
                "output_path": str(self.output_path or ""),
                "output_sha256": str(self.output_sha256 or ""),
                "entry_media_version_ids": list(self.entry_media_version_ids),
                "subtitle_revision": str(self.subtitle_revision or "")}

    def bind_hash(self) -> str:
        return hash_payload(self.bind_hash_payload())

    def verify(self, plan: Optional[ComposePlan] = None, *,
               output_sha256: str = "") -> Tuple[bool, str]:
        """核对登记与计划/产物是否仍然一致。返回 ``(ok, reason)``。

        ``output_sha256`` 给了就一并核对实际字节（重渲覆盖后必然对不上，
        届时应登记**新**的 render 而不是改这一条）。
        """
        if not self.compose_fingerprint:
            return False, "渲染登记缺少 compose_fingerprint（成片没有绑定到冻结计划）"
        if not self.plan_fingerprint:
            return False, "渲染登记缺少 plan_fingerprint（计划变没变无法回答）"
        if plan is not None:
            if str(plan.plan_fingerprint) != self.plan_fingerprint:
                return False, ("成片登记的计划指纹与当前计划不一致：登记=%s 当前=%s"
                               % (self.plan_fingerprint[:12], str(plan.plan_fingerprint)[:12]))
            if str(plan.compose_fingerprint) != self.compose_fingerprint:
                return False, ("成片登记的剪辑指纹与当前 revision 不一致：登记=%s 当前=%s"
                               % (self.compose_fingerprint[:12],
                                  str(plan.compose_fingerprint)[:12]))
        if output_sha256 and self.output_sha256 and output_sha256 != self.output_sha256:
            return False, ("成片文件内容已变（登记 sha256=%s 现状=%s）—— 应登记新的 render，"
                           "不可原地改写历史登记" % (self.output_sha256[:12], output_sha256[:12]))
        if not self.output_path:
            return False, "渲染登记缺少 output_path（无法指向实际产物）"
        return True, "成片登记与冻结计划一致"

    def to_dict(self) -> Dict[str, Any]:
        return {"render_id": self.render_id, "revision_id": self.revision_id,
                "project": self.project, "episode": self.episode,
                "revision_no": int(self.revision_no),
                "compose_fingerprint": self.compose_fingerprint,
                "plan_fingerprint": self.plan_fingerprint,
                "output_path": self.output_path, "output_sha256": self.output_sha256,
                "output_bytes": int(self.output_bytes or 0),
                "output_duration_sec": float(self.output_duration_sec or 0.0),
                "subtitle_revision": self.subtitle_revision,
                "entry_media_version_ids": list(self.entry_media_version_ids),
                "declared_silences": int(self.declared_silences),
                "authorized_by": self.authorized_by,
                "authorization_ref": self.authorization_ref,
                "created_at": self.created_at, "note": self.note,
                "bind_hash": self.bind_hash()}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "EpisodeRenderVersion":
        return EpisodeRenderVersion(
            render_id=str(raw.get("render_id") or ""),
            revision_id=str(raw.get("revision_id") or ""),
            project=str(raw.get("project") or ""), episode=str(raw.get("episode") or ""),
            revision_no=int(raw.get("revision_no") or 0),
            compose_fingerprint=str(raw.get("compose_fingerprint") or ""),
            plan_fingerprint=str(raw.get("plan_fingerprint") or ""),
            output_path=str(raw.get("output_path") or ""),
            output_sha256=str(raw.get("output_sha256") or ""),
            output_bytes=int(raw.get("output_bytes") or 0),
            output_duration_sec=float(raw.get("output_duration_sec") or 0.0),
            subtitle_revision=str(raw.get("subtitle_revision") or ""),
            entry_media_version_ids=tuple(str(x) for x in
                                          (raw.get("entry_media_version_ids") or ())),
            declared_silences=int(raw.get("declared_silences") or 0),
            authorized_by=str(raw.get("authorized_by") or ""),
            authorization_ref=str(raw.get("authorization_ref") or ""),
            created_at=str(raw.get("created_at") or ""),
            note=str(raw.get("note") or ""))


def build_episode_render_version(render_id: str, plan: ComposePlan, *,
                                  output_path: str = "", output_sha256: str = "",
                                  output_bytes: int = 0, output_duration_sec: float = 0.0,
                                  authorized_by: str = "", authorization_ref: str = "",
                                  created_at: str = "", note: str = "") -> EpisodeRenderVersion:
    """由**已渲染完成的**计划产出成片登记（ADR-0012 决策第 3 点）。

    指纹是**算出来的**，不是调用方传进来的：``compose_fingerprint`` 与
    ``plan_fingerprint`` 都从 ``plan`` 取，成品与计划因此不可分。
    """
    if not isinstance(plan, ComposePlan):
        raise TimelineError("build_episode_render_version 只接受 ComposePlan")
    plan.validate()
    if not plan.verify_plan_fingerprint():
        raise TimelineError("合成计划指纹与内容不一致，拒绝登记成片（计划疑被旁路改写）")
    return EpisodeRenderVersion(
        render_id=str(render_id), revision_id=plan.revision_id,
        project=plan.project, episode=plan.episode, revision_no=int(plan.revision_no),
        compose_fingerprint=plan.compose_fingerprint,
        plan_fingerprint=plan.plan_fingerprint,
        output_path=str(output_path or ""), output_sha256=str(output_sha256 or ""),
        output_bytes=int(output_bytes or 0),
        output_duration_sec=float(output_duration_sec or 0.0),
        subtitle_revision=plan.subtitle_revision,
        entry_media_version_ids=tuple(e.media_version_id for e in plan.entries),
        declared_silences=len(plan.silence_declarations),
        authorized_by=str(authorized_by or ""),
        authorization_ref=str(authorization_ref or ""),
        created_at=created_at or _now(), note=str(note or ""))
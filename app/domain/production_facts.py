# -*- coding: utf-8 -*-
"""版本化生产事实（评估文档 §P0-3）：GenerationIntent / MediaVersion /
SelectionDecision / ApprovalDecision / CapabilityProfileVersion。

这一层回答的是本项目此前**无法回答**的问题
------------------------------------------
改造前「生成」= 文件产物 + 台账（``comfyui_job_store`` 只记 workflow_hash 与
产物路径）。于是：

* 「这镜为什么用这一版」只能靠人在群里说一句；
* best-of-N / 九宫格选格没有统一模型，候选比较落不到数据里；
* 重跑**覆盖文件**，历史版本全部消失。

引入这五个实体后，「生成 → 采用 → 批准」三态分离，历史版本全部留存。

三条铁律（违反即视为设计错误，不是实现 bug）
--------------------------------------------
**铁律 1：采用永不隐式升级为批准。**
    :class:`SelectionDecision` 与 :class:`ApprovalDecision` 是两张**互不派生**的事实。
    批准记录**必须**引用一条 selection_id（结构上不可能「凭空批准」），
    且必须带人工授权（:func:`require_human_authorization`）。机器检查通过、
    预检全绿、采用成功，**任何一项都不构成批准**。
    原因：批准是放行决定（对外交付、法律责任），采用是创作决定（选哪一版）。
    两者混同会让「已采用」在交付闸门被读成「已放行」，这正是竞品反复踩的坑。

**铁律 2：Intent 冻结后修改必须派生新 intent。**
    :class:`GenerationIntent` 是 frozen dataclass；任何字段变化都通过
    :func:`derive_intent` 产出 ``parent_intent_id`` 指向原意图的新对象。
    原因：intent 是「这一次生成想做什么」的证据。如果能原地改，
    重渲后无法回答「这条镜当初是用哪版提示词渲的」，
    ``workflow_hash`` 复用安全阀也会被绕过（改了提示词哈希却沿用旧产物）。

**铁律 3：批准必须带哈希绑定。**
    :func:`build_approval` 把 ``media_sha256 + intent_hash + selection_hash``
    三元组钉进 :attr:`ApprovalDecision.bound_hash`（对齐既有
    ``quality_stage.make_binding`` 的血泪教训：产物路径不变、内容已换，
    「已验收」静默沿用）。

本模块是**纯领域层**：不 import flask，不碰磁盘与网络，不读全局配置。
所有对象都是 frozen dataclass（改不动 = 不可变），所有函数都是纯函数。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DomainError", "FrozenEntityError", "ApprovalAuthorizationError",
    "IncompleteIntentError", "ConflictError", "NotSelectedError",
    "REF_SLOT_ROLES", "MEDIA_KINDS", "DECISION_VERDICTS",
    "RefSlot", "GenerationIntent", "MediaVersion", "SelectionDecision",
    "ApprovalDecision", "CapabilityProfileVersion",
    "hash_payload", "build_intent", "derive_intent", "assert_intent_unfrozen",
    "build_media_version", "build_selection", "build_approval",
    "require_human_authorization", "approval_is_valid",
    "actor_tokens", "is_human_actor",
    "assert_selection_not_approved", "decision_state",
    "build_capability_profile_version", "next_profile_version",
]


# =====================================================================
# 异常
# =====================================================================

class DomainError(Exception):
    """领域层错误基类（编排层据此决定 HTTP 状态码）。"""


class FrozenEntityError(DomainError):
    """试图原地修改已冻结实体（intent / media / revision）。

    铁律 2 / 铁律 3 的落点：不是「不允许」而是「**物理上改不了**」——
    frozen dataclass 会在 ``__setattr__`` 抛错，从源头杜绝静默改写。
    """


class ApprovalAuthorizationError(DomainError):
    """批准缺少人工授权 / 哈希绑定不成立。

    铁律 1 的落点：采用方**无权**批准；批量任务、机器 QC、自动流水线
    一律拿不到 ApprovalDecision。
    """


class IncompleteIntentError(DomainError):
    """意图字段不完整（缺 prompt / seed / profile 等），无法冻结。"""


class ConflictError(DomainError):
    """乐观锁冲突：并发写同一实体的另一个分支已经提交。"""


class NotSelectedError(DomainError):
    """对**未采用**的候选请求批准。

    铁律 1 的结构保证：批准必须引用 selection_id，不存在「直接批准某候选」。
    """


# =====================================================================
# 常量
# =====================================================================

#: 参考图槽位的角色白名单。槽位是**有序**的：同一张资产在全集的
#: ``<Picture N>`` 编号必须恒定（见 ``h3_common_refs`` 的官方用法），
#: 所以角色 + 序号一起进 intent_hash，槽位漂移即视为不同意图。
REF_SLOT_ROLES: Tuple[str, ...] = (
    "character",   # 角色形象
    "item",        # 道具
    "scene",       # 场景
    "style",       # 风格锚点
    "start_frame", # 首帧图（关键帧驱动）
    "end_frame",   # 尾帧图
    "audio",       # 参考音频（口型驱动）
)

#: MediaVersion 支持的媒体类型。
MEDIA_KINDS: Tuple[str, ...] = ("image", "video", "audio")

#: 决策结论白名单。**刻意不含 approved/selected 的隐式推导**：
#: 结论只能是人（或显式授权的流程）写下的字面量。
DECISION_VERDICTS: Tuple[str, ...] = ("accepted", "rejected")

def _now() -> str:
    """本地时间戳（与 task_store._now 同一精度口径，避免库间时间戳粒度打架）。"""
    return datetime.now().isoformat(timespec="seconds")


# =====================================================================
# 稳定哈希
# =====================================================================

def hash_payload(payload: Any, *, length: int = 64) -> str:
    """任意 JSON-able 对象的稳定指纹。

    与既有 ``comfyui_job_store.workflow_hash`` 同口径（``sort_keys=True`` +
    紧凑分隔符），但这里**默认给满 64 位**：workflow_hash 截 32 位是为了
    台账短小，而 intent_hash / bound_hash 是法律意义上的「这一版到底是谁」，
    截断会让人为构造碰撞变得可行。

    不可序列化时抛 :class:`DomainError`（fail-closed）—— 绝不返回空串假装算过，
    否则「哈希绑定」会退化成「看起来绑过了」。
    """
    if payload is None:
        raise DomainError("hash_payload: payload 为 None，无法计算指纹")
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), default=_json_default)
    except (TypeError, ValueError) as e:
        raise DomainError("hash_payload: 对象不可稳定序列化：%s" % e) from e
    digest = hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()
    return digest[:length] if length and length < len(digest) else digest


def _json_default(obj: Any) -> Any:
    """让 dataclass / tuple / set 也能进稳定哈希（而不是悄悄退化）。"""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, (set, frozenset)):
        return sorted(str(x) for x in obj)
    if isinstance(obj, tuple):
        return list(obj)
    return str(obj)


# =====================================================================
# 参考图槽位
# =====================================================================

@dataclass(frozen=True)
class RefSlot:
    """参考图**槽位**（不是「参考图列表」）。

    为什么要槽位化：H3 Director 的公共参考图按 index 合并进每段，
    公共图占 0..K-1、段级私有图从 K 起。槽位号一旦漂移，模型看到的
    ``<Picture 3>`` 就从「林昭」变成「道具剑」——画面静默走样。
    因此 ``index`` 是 intent 的一部分，不是排序的副产品。
    """

    index: int
    role: str
    ref: str                      # 资产键 / 文件名 / URL
    sha256: str = ""              # 参考图内容指纹（有则校验，无则留空）

    def to_dict(self) -> Dict[str, Any]:
        return {"index": int(self.index), "role": str(self.role),
                "ref": str(self.ref), "sha256": str(self.sha256 or "")}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "RefSlot":
        return RefSlot(index=int(raw.get("index") or 0),
                       role=str(raw.get("role") or ""),
                       ref=str(raw.get("ref") or ""),
                       sha256=str(raw.get("sha256") or ""))


def _norm_ref_slots(raw: Optional[Iterable[Mapping[str, Any]]]) -> Tuple[RefSlot, ...]:
    """归一化参考图槽位：校验角色、按 index 排序、拒绝重号。

    重号即冲突：两个槽位同一 index 意味着模型收到的参考图编号会互相覆盖，
    这属于**静默错**，必须在写入前拦住而不是渲完才发现。
    """
    if not raw:
        return ()
    out: List[RefSlot] = []
    seen: set = set()
    for item in raw:
        slot = item if isinstance(item, RefSlot) else RefSlot.from_dict(item or {})
        if slot.role not in REF_SLOT_ROLES:
            raise IncompleteIntentError("未知参考图槽位角色：%r（合法值 %s）"
                                        % (slot.role, list(REF_SLOT_ROLES)))
        if slot.index < 0:
            raise IncompleteIntentError("参考图槽位 index 不能为负：%r" % slot.index)
        if slot.index in seen:
            raise IncompleteIntentError("参考图槽位 index 重复：%d（槽位编号必须唯一）"
                                        % slot.index)
        seen.add(slot.index)
        out.append(slot)
    return tuple(sorted(out, key=lambda s: s.index))


# =====================================================================
# GenerationIntent（冻结不可改）
# =====================================================================

@dataclass(frozen=True)
class GenerationIntent:
    """一次生成的**意图**：想做什么，而不是产出什么。

    字段分组刻意对应三类「改了就必须重渲」的输入：

    * 内容：``prompt`` / ``negative_prompt``
    * 锚点：``ref_slots``（有序）
    * 条件：``seed`` / ``profile_id`` / ``workflow_version``

    ``intent_hash`` 覆盖以上全部（**不含** id 与时间戳），因此：
    同参数重跑 → 同哈希（可识别为同一次意图的重复执行）；
    任一参数变化 → 新哈希（不会被误当成可复用产物）。
    """

    intent_id: str
    project: str
    episode: str
    shot_key: str
    kind: str                                   # image / video / audio
    prompt: str
    intent_hash: str
    negative_prompt: str = ""
    ref_slots: Tuple[RefSlot, ...] = ()
    seed: int = -1                              # -1 = 不固定（由生成端随机）
    profile_id: str = ""                        # 能力档案名
    profile_version: str = ""                   # CapabilityProfileVersion 版本
    workflow_version: str = ""                  # 工作流模板版本（文件名 + 内容哈希）
    workflow_hash: str = ""                     # 对齐 comfyui_job_store.workflow_hash
    parent_intent_id: str = ""                  # 派生链（铁律 2）
    derivation_reason: str = ""                 # 为什么派生（人类可读）
    created_at: str = ""
    created_by: str = ""
    frozen: bool = True

    # -- 不可变事实的计算口径 ---------------------------------------

    def hash_payload(self) -> Dict[str, Any]:
        """进 intent_hash 的字段集合。**新增参数必须同时加进这里**。"""
        return {
            "project": str(self.project),
            "episode": str(self.episode),
            "shot_key": str(self.shot_key),
            "kind": str(self.kind),
            "prompt": str(self.prompt),
            "negative_prompt": str(self.negative_prompt),
            "ref_slots": [s.to_dict() for s in self.ref_slots],
            "seed": int(self.seed),
            "profile_id": str(self.profile_id),
            "profile_version": str(self.profile_version),
            "workflow_version": str(self.workflow_version),
            "workflow_hash": str(self.workflow_hash),
        }

    def verify_hash(self) -> bool:
        """自检：重算哈希与冻结时是否一致（检测库被旁路改写）。"""
        return hash_payload(self.hash_payload()) == self.intent_hash

    def to_dict(self) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in (
            "intent_id", "project", "episode", "shot_key", "kind", "prompt",
            "negative_prompt", "seed", "profile_id", "profile_version",
            "workflow_version", "workflow_hash", "parent_intent_id",
            "derivation_reason", "created_at", "created_by", "frozen")}
        d["ref_slots"] = [s.to_dict() for s in self.ref_slots]
        d["intent_hash"] = self.intent_hash
        return d

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "GenerationIntent":
        return GenerationIntent(
            intent_id=str(raw.get("intent_id") or ""),
            project=str(raw.get("project") or ""),
            episode=str(raw.get("episode") or ""),
            shot_key=str(raw.get("shot_key") or ""),
            kind=str(raw.get("kind") or "image"),
            prompt=str(raw.get("prompt") or ""),
            intent_hash=str(raw.get("intent_hash") or ""),
            negative_prompt=str(raw.get("negative_prompt") or ""),
            ref_slots=_norm_ref_slots(raw.get("ref_slots")),
            seed=int(raw.get("seed") if raw.get("seed") is not None else -1),
            profile_id=str(raw.get("profile_id") or ""),
            profile_version=str(raw.get("profile_version") or ""),
            workflow_version=str(raw.get("workflow_version") or ""),
            workflow_hash=str(raw.get("workflow_hash") or ""),
            parent_intent_id=str(raw.get("parent_intent_id") or ""),
            derivation_reason=str(raw.get("derivation_reason") or ""),
            created_at=str(raw.get("created_at") or ""),
            created_by=str(raw.get("created_by") or ""),
            frozen=bool(raw.get("frozen", True)),
        )


def build_intent(intent_id: str, *, project: str, episode: str, shot_key: str,
                 kind: str = "image", prompt: str,
                 negative_prompt: str = "",
                 ref_slots: Optional[Sequence[Mapping[str, Any]]] = None,
                 seed: int = -1, profile_id: str = "",
                 profile_version: str = "", workflow_version: str = "",
                 workflow_hash: str = "", created_by: str = "",
                 created_at: str = "") -> GenerationIntent:
    """构造并**冻结**一条 GenerationIntent（同时算出 intent_hash）。

    缺 prompt 视为不可冻结：没有提示词的 intent 无法回答「这镜想做什么」，
    也就无法在重渲后被复核。
    """
    if kind not in MEDIA_KINDS:
        raise IncompleteIntentError("未知意图类型：%r（合法值 %s）" % (kind, list(MEDIA_KINDS)))
    if not str(prompt or "").strip():
        raise IncompleteIntentError("intent 必须有 prompt（否则无法冻结「想做什么」）")
    slots = _norm_ref_slots(ref_slots)
    obj = GenerationIntent(
        intent_id=str(intent_id), project=str(project or ""),
        episode=str(episode or ""), shot_key=str(shot_key or ""),
        kind=kind, prompt=str(prompt),
        intent_hash="",                                   # 占位，下面回填
        negative_prompt=str(negative_prompt or ""),
        ref_slots=slots,
        seed=int(seed if seed is not None else -1),
        profile_id=str(profile_id or ""), profile_version=str(profile_version or ""),
        workflow_version=str(workflow_version or ""),
        workflow_hash=str(workflow_hash or ""),
        created_at=created_at or _now(), created_by=str(created_by or ""),
        frozen=True,
    )
    return obj.__class__(**{**obj.__dict__, "intent_hash": hash_payload(obj.hash_payload())})


#: 允许派生的内容字段（其余是身份 / 血缘 / 时间戳，不参与内容变更）
_DERIVABLE_FIELDS: Tuple[str, ...] = (
    "kind", "prompt", "negative_prompt", "ref_slots", "seed",
    "profile_id", "profile_version", "workflow_version", "workflow_hash",
)


def derive_intent(parent: GenerationIntent, new_intent_id: str, *,
                  reason: str, created_by: str = "",
                  **changes: Any) -> GenerationIntent:
    """**派生**一条新 intent（铁律 2 的唯一合法路径）。

    原 intent 一个字节都不动（frozen dataclass，物理上改不了）；
    新 intent 通过 ``parent_intent_id`` 挂回原意图，并强制要求 ``reason``，
    事后能回答「为什么改」。

    只有 :data:`_DERIVABLE_FIELDS` 里的内容字段可改 —— 试图改
    ``intent_id`` / ``intent_hash`` / ``parent_intent_id`` 一律拒绝，
    否则就等于允许「伪造血缘」或「改掉冻结内容而不换哈希」。

    无变化的派生同样拒绝：只会污染版本链，且让 intent_hash 失去去重意义。
    """
    if not isinstance(parent, GenerationIntent):
        raise FrozenEntityError("derive_intent 只接受 GenerationIntent")
    why = str(reason or "").strip()
    if not why:
        raise IncompleteIntentError("派生新 intent 必须写明 reason（为什么改）")
    if not changes:
        raise FrozenEntityError("派生必须至少改动一个内容字段，否则产生的是同义副本")
    unknown = set(changes) - set(_DERIVABLE_FIELDS)
    if unknown:
        raise FrozenEntityError(
            "字段不可派生：%s（只允许改内容字段 %s）" % (sorted(unknown), list(_DERIVABLE_FIELDS)))

    base: Dict[str, Any] = parent.to_dict()
    base.update(changes)
    base["ref_slots"] = list(_norm_ref_slots(base.get("ref_slots")))
    base["seed"] = int(base.get("seed") if base.get("seed") is not None else -1)

    # 内容未真正变化 → 拒绝（避免 intent_hash 重复却挂着不同 id 的同义副本）
    probe = GenerationIntent(
        intent_id=str(new_intent_id), project=base.get("project", ""),
        episode=base.get("episode", ""), shot_key=base.get("shot_key", ""),
        kind=base.get("kind", "image"), prompt=base.get("prompt", ""),
        intent_hash="", negative_prompt=base.get("negative_prompt", ""),
        ref_slots=_norm_ref_slots(base.get("ref_slots")), seed=base["seed"],
        profile_id=base.get("profile_id", ""), profile_version=base.get("profile_version", ""),
        workflow_version=base.get("workflow_version", ""),
        workflow_hash=base.get("workflow_hash", ""),
        parent_intent_id=parent.intent_id, derivation_reason=why,
        created_at=_now(), created_by=str(created_by or parent.created_by or ""),
        frozen=True,
    )
    if probe.hash_payload() == parent.hash_payload():
        raise FrozenEntityError("派生未产生任何内容变化，拒绝写入同义 intent（parent=%s）"
                                % parent.intent_id)
    return probe.__class__(**{**probe.__dict__,
                              "intent_hash": hash_payload(probe.hash_payload())})


def assert_intent_unfrozen(intent: GenerationIntent) -> None:
    """冻结校验（写库前的最后一道闸）。

    * ``frozen`` 必须为真 —— 否则有人构造了「可改 intent」；
    * ``intent_hash`` 必须与内容重算一致 —— 库被旁路改写时立刻暴露。

    fail-closed：任何一条不成立都抛错，不放行「先记下来再说」。
    """
    if not getattr(intent, "frozen", False):
        raise FrozenEntityError("intent 未冻结，禁止写入生产事实：%s" % intent.intent_id)
    if not intent.intent_hash:
        raise FrozenEntityError("intent 缺少 intent_hash，无法证明冻结内容：%s" % intent.intent_id)
    if not intent.verify_hash():
        raise FrozenEntityError("intent 内容与 intent_hash 不一致（疑似被旁路改写）：%s"
                                % intent.intent_id)


# =====================================================================
# MediaVersion
# =====================================================================

@dataclass(frozen=True)
class MediaVersion:
    """一次 intent 产出的**候选媒体版本**。

    ``media_sha256`` 是内容指纹，``probe`` 存 ffprobe 的**客观事实**
    （宽高 / fps / 时长 / 编码 / 是否有音轨），不存任何主观评价。
    客观事实进版本记录，才能回答「同一提示词重渲为什么时长变了」。

    ⚠️ MediaVersion 自身**不带** selected / approved 字段。
    这是铁律 1 的结构性表达：媒体版本只陈述「存在这样一个文件」，
    「它被采用了吗 / 被批准了吗」由两张独立决策表回答。
    """
    media_version_id: str
    intent_id: str
    kind: str
    media_sha256: str
    path: str = ""
    bytes: int = 0
    probe: Mapping[str, Any] = field(default_factory=dict)
    attempt_id: str = ""          # 产出它的 Attempt（Job/Attempt 收敛后回链）
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"media_version_id": self.media_version_id, "intent_id": self.intent_id,
                "kind": self.kind, "media_sha256": self.media_sha256,
                "path": self.path, "bytes": int(self.bytes or 0),
                "probe": dict(self.probe or {}), "attempt_id": self.attempt_id,
                "created_at": self.created_at}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "MediaVersion":
        return MediaVersion(
            media_version_id=str(raw.get("media_version_id") or ""),
            intent_id=str(raw.get("intent_id") or ""),
            kind=str(raw.get("kind") or "image"),
            media_sha256=str(raw.get("media_sha256") or ""),
            path=str(raw.get("path") or ""),
            bytes=int(raw.get("bytes") or 0),
            probe=dict(raw.get("probe") or {}),
            attempt_id=str(raw.get("attempt_id") or ""),
            created_at=str(raw.get("created_at") or ""),
        )


def build_media_version(media_version_id: str, *, intent: GenerationIntent,
                        media_sha256: str, path: str = "", bytes_: int = 0,
                        probe: Optional[Mapping[str, Any]] = None,
                        attempt_id: str = "",
                        created_at: str = "") -> MediaVersion:
    """登记候选媒体版本。

    必须给内容指纹：没有 sha256 的候选无法参与哈希绑定批准，
    也就永远拿不到批准 —— 这正是我们要的（宁可不可批准，不可糊涂批准）。
    """
    if not str(media_sha256 or "").strip():
        raise IncompleteIntentError("MediaVersion 必须给 media_sha256（否则永远无法批准）")
    return MediaVersion(media_version_id=str(media_version_id), intent_id=intent.intent_id,
                        kind=intent.kind, media_sha256=str(media_sha256),
                        path=str(path or ""), bytes=int(bytes_ or 0),
                        probe=dict(probe or {}), attempt_id=str(attempt_id or ""),
                        created_at=created_at or _now())


# =====================================================================
# 决策：采用（SelectionDecision）
# =====================================================================

@dataclass(frozen=True)
class SelectionDecision:
    """**采用**决定：这一镜/这一版就用它了。

    采用是**创作决定**（回答「为什么用这一版」），不带任何放行含义。
    ``selected=True`` 只表示「当前采用」，历史决策保留（``revoked=True`` 撤销），
    这样「当初为什么选它、后来为什么换掉」都能回溯。
    """

    selection_id: str
    subject_type: str                 # media_version / timeline_revision
    subject_id: str
    media_version_id: str
    intent_hash: str = ""
    selection_hash: str = ""
    project: str = ""
    episode: str = ""
    shot_key: str = ""
    selected: bool = True
    revoked: bool = False
    reason: str = ""                  # 为什么选它（人类可读，必填）
    decided_by: str = ""
    decided_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "SelectionDecision":
        return SelectionDecision(**{k: raw.get(k) for k in
                                    SelectionDecision.__dataclass_fields__})


def build_selection(selection_id: str, *, subject_type: str, subject_id: str,
                    media_version_id: str, intent_hash: str = "",
                    project: str = "", episode: str = "", shot_key: str = "",
                    reason: str, decided_by: str = "",
                    decided_at: str = "") -> SelectionDecision:
    """构造采用决定。**没有 reason 就无法采用**。

    原因：best-of-N 里「机器分数最高」不等于「该用这版」。
    强制写理由，是为了让「这镜为什么用这一版」在三个月后仍可回答 ——
    这正是评估文档 §P0-3 点名的改造收益。
    """
    if subject_type not in ("media_version", "timeline_revision"):
        raise DomainError("未知决策对象类型：%r" % subject_type)
    if not str(reason or "").strip():
        raise IncompleteIntentError("采用必须写明 reason（否则无法回答「为什么用这一版」）")
    obj = SelectionDecision(
        selection_id=str(selection_id), subject_type=subject_type,
        subject_id=str(subject_id), media_version_id=str(media_version_id),
        intent_hash=str(intent_hash or ""), selection_hash="",
        project=str(project or ""), episode=str(episode or ""),
        shot_key=str(shot_key or ""), selected=True, revoked=False,
        reason=str(reason), decided_by=str(decided_by or ""),
        decided_at=decided_at or _now(),
    )
    return SelectionDecision(**{**obj.__dict__, "selection_hash": hash_payload(
        {"subject_type": obj.subject_type, "subject_id": obj.subject_id,
         "media_version_id": obj.media_version_id, "intent_hash": obj.intent_hash,
         "reason": obj.reason, "decided_by": obj.decided_by})})


# =====================================================================
# 决策：批准（ApprovalDecision）
# =====================================================================

@dataclass(frozen=True)
class ApprovalDecision:
    """**批准**决定：放行，可对外交付。

    铁律 1 的结构性保证（三条，缺一不可）：

    1. ``selection_id`` 必填 —— 批准只能跟在一条**采用**之后，
       不存在「直接批准某个候选」；
    2. ``authorized_by`` 必须是人 / 显式授权主体，且
       :func:`require_human_authorization` 会按**词表 + 主体白名单**口径拒绝
       机器账号（``system:*`` / ``bot*`` / ``auto*`` / ``scheduler`` /
       ``cron`` / ``worker`` 等）—— 机器 QC 通过**不是**批准；
    3. ``bound_hash`` 钉住 (media_sha256, intent_hash, selection_hash) 三元组
       （铁律 3），产物被替换即批准失效。
    """
    approval_id: str
    selection_id: str
    subject_type: str
    subject_id: str
    media_version_id: str
    bound_hash: str
    authorized_by: str
    authorization_ref: str = ""       # 外部授权凭据（工单号 / 签署记录）
    media_sha256: str = ""
    intent_hash: str = ""
    selection_hash: str = ""
    scope: str = "media"              # media / timeline / delivery
    approved: bool = True
    revoked: bool = False
    revoked_by: str = ""
    revoked_reason: str = ""
    note: str = ""
    decided_at: str = ""
    superseded_by: str = ""           # 被新的批准取代（哈希变化后需重新批准）

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "ApprovalDecision":
        return ApprovalDecision(**{k: raw.get(k) for k in
                                   ApprovalDecision.__dataclass_fields__})


#: 机器身份词表（**按 token 匹配，不是按子串**）。
#:
#: 为什么不能只比前缀：前缀 deny-list 是「枚举已知的坏名字」，
#: 只要出现一个没枚举到的新名字就通行 —— ``scheduler`` / ``cron`` / ``worker``
#: 这些前缀根本不在原名单里，却都是真实存在的机器身份（P1-2 实测）。
_MACHINE_ACTOR_TOKENS: Tuple[str, ...] = (
    "system", "sys", "svc", "service", "daemon", "proc", "process",
    "bot", "botnet", "robot", "auto", "autopilot", "automation",
    "agent", "worker", "scheduler", "schedule", "cron", "crontab",
    "job", "runner", "task", "pipeline", "script", "hook", "webhook",
    "ci", "cd", "jenkins", "github", "githubaction", "make",
    # —— 第二轮复审补：以下全部是实测「打表通过」的绕过样本 ——
    "admin", "administrator", "root", "sudo", "su",
    "batch", "bulk", "etl", "importer", "exporter", "import", "export",
    "deploy", "deployer", "nightly", "watchdog", "housekeeping",
    "migration", "migrate", "sync", "backup", "reaper", "cleanup",
    "build", "release", "publish", "monitor", "probe", "healthcheck",
    "fetch", "filler", "scanner", "spider", "crawler", "scraper",
    # `cron` / `ci` / `cd` / `sys` / `su` 这些**短词只按完整 token 匹配**
    # （原因见 _MACHINE_ACTOR_PREFIXES 的说明），它们的复合形态在这里枚举。
    "cronjob", "cronrunner", "cronbot", "cronsvc", "cicd", "cijob",
    "cirunner", "cibot", "cisvc", "cigithubaction", "sysadmin",
    "sysop", "svcaccount", "serviceaccount", "serviceacct", "agentbot",
    "agentworker", "autopilotbot", "robotbot", "deploybot", "deployerbot",
)

#: 同词表里**较长且不会出现在人名里**的词额外做子串匹配
#: （``systemAutopilot`` / ``nightlyworker`` 这类连写没有分隔符的写法，
#: 规范化成小写后已切不出驼峰，只能靠子串兜住）。短词、以及本身可能是姓氏的
#: 词（``ci`` / ``cd`` / ``sys`` / ``jenkins`` / ``cron``）**绝不做子串匹配** ——
#: ``specific`` / ``Tom Jenkins`` / ``Sara Cronin`` 会被误伤，误拒真人比漏一个
#: 机器名字更糟（会逼用户去编一个「像机器」的名字）。这些词仍按**完整 token** 匹配，
#: 想按名字放行真人的话用 ``allowlist``（见 :func:`is_human_actor`）。
_MACHINE_ACTOR_SUBSTRINGS: Tuple[str, ...] = (
    "system", "service", "daemon", "autopilot", "automation", "scheduler",
    "worker", "robot", "webhook", "pipeline",
)

#: **前缀**匹配的机器词干（token 以其开头即判机器）。
#:
#: 为什么需要前缀：规范化会把 ``deployBot`` 压成 ``deploybot`` 一个 token，
#: 完整 token 匹配就漏了；前缀匹配补上这一类。
#:
#: ⚠️ **只收「不可能出现在人名开头」的长词干**。``cron`` / ``ci`` / ``su`` /
#: ``sys`` / ``jenkins`` **刻意不在这里**：它们的前缀一旦生效，
#: ``Sara Cronin``（``cronin``）、``Cindy``、``Susan`` 会被当场误拒 ——
#: 误拒真人的代价是逼用户编假名，最终把闸门教坏。这些词改用
#: :data:`_MACHINE_ACTOR_TOKENS` 里的**完整 token + 枚举复合形态**
#: （``cronjob`` / ``cronrunner`` / ``cicd`` / ``cijob`` …）覆盖。
_MACHINE_ACTOR_PREFIXES: Tuple[str, ...] = (
    "svc", "service", "agent", "robot", "bot", "autopilot",
    "scheduler", "daemon", "worker", "admin", "root", "deploy",
    "nightly", "batch", "etl", "import", "runner", "machine",
)

#: 人工主体的形状：含中日韩字符，或带 ``@``（邮箱），或是 2–64 位的
#: 「汉字/字母/数字/空格/点/下划线/短横」组合。纯符号、过短、过长一律不像人。
#: 字符类一律用 ``\uXXXX`` 转义写死（不依赖源文件编码，字面汉字会随
#: 保存编码漂移）。空格必须放行 —— ``Li Wei`` / ``Mary Ann`` 这类真人姓名很常见，
#: 误拒人工主体会逼用户去编一个「像机器」的名字，反而把闸门教坏了。
_ACTOR_ALLOWED_CHARS = re.compile(
    r"^[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af _.@\-]{2,64}$")
_ACTOR_HAS_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
#: 切 token 用的「分隔符」：**先规范化（NFKC + casefold）再切**。
#: 显式列出 ``_`` / ``-`` / ``.`` / 空格 / ``,`` / ``/`` / ``:`` / ``@``，
#: 后面跟上「任何非字母数字且非中日韩的字符」兜底（引号 / 括号 / 花括号等）。
#: 顺序很关键：规范化**必须**在切词之前 —— 否则 ``bOt`` 会被驼峰规则切成
#: ``('b', 'ot')``，两片都不在词表里，``bot`` 就这么漏了过去（第二轮复审实测）。
_ACTOR_SEPARATORS = re.compile(
    r"[\s_\-\.,/:@]+|[^0-9a-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
#: 数字边界：``svc2`` → ``svc`` / ``2``，让 ``svc`` 前缀规则能命中。
#: 只在「字母/汉字 → 数字」这一侧切，数字串本身保持完整（``build007`` → ``build`` / ``007``）
_ACTOR_DIGIT_SPLIT = re.compile(
    r"(?<=[a-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af])(?=[0-9])")


def _normalize_actor(authorized_by: str) -> str:
    """主体标识规范化：NFKC 全角折叠 → casefold → 分隔符统一。

    为什么必须规范化在前（而不是在切词之后补救）：

    * **NFKC**：``ｓｙｓｔｅｍ``（全角）折成 ``system``，否则全角变体永远
      靠「形状检查」兜底而不是靠词表 —— 换个不含非 ASCII 的写法就又漏了；
    * **casefold**：``bOt`` / ``BoT`` / ``BOt`` 全部等价于 ``bot``。
      大小写归一之后驼峰信息已经不存在，再去按驼峰切词只会把一个完整的
      ``bot`` 切成两片没意义的碎片；
    * **分隔符统一**：``svc_acct`` / ``svc-acct`` / ``svc.acct`` / ``svc/acct``
      是同一个身份，不该因为分隔符不同而绕开。
    """
    who = unicodedata.normalize("NFKC", str(authorized_by or "")).casefold()
    return who.strip()


def actor_tokens(authorized_by: str) -> Tuple[str, ...]:
    """把主体标识规范化后切成 token（大小写已归一，驼峰不再有意义）。

    ``system:autopilot`` → ``('system', 'autopilot')``；
    ``mjscxt-nightlyWorker`` → ``('mjscxt', 'nightly', 'worker')``；
    ``bOt`` → ``('bot',)``（**不是** ``('b', 'ot')``）。
    """
    who = _normalize_actor(authorized_by)
    if not who:
        return ()
    parts: List[str] = []
    for chunk in _ACTOR_SEPARATORS.split(who):
        if not chunk:
            continue
        parts.extend(p for p in _ACTOR_DIGIT_SPLIT.split(chunk) if p)
    return tuple(parts)


def is_human_actor(authorized_by: str,
                   allowlist: Optional[Iterable[str]] = None) -> bool:
    """主体标识是否判定为人工（命名口径，**不是身份认证**）。

    判定顺序：

    1. 空 → 非人工；
    2. 给了 ``allowlist``（人工主体白名单）→ 只有名单内主体算人工，
       这是最严格的一档，可由配置/授权名单注入；
    3. 规范化（NFKC + casefold + 分隔符统一）后，命中机器词表
       （完整 token / 长词干前缀 / 长子串）→ 非人工；
    4. 形状不像人（纯符号 / 过短 / 过长）→ 非人工；
    5. 其余算人工。

    ⚠️⚠️ **词表枚举法有上限，这里不是认证。**
    本项目没有登录态，本函数拿不到任何能证明「操作者是人」的证据，
    它只能判断「这个名字**看起来**是不是机器」。两个不可消除的漏洞：

    * **新造的名字能通过**：一个词表外的机器身份（``ci-cd`` 换个拼写、
      或者干脆叫 ``zhangsan_bot_v2`` 之外的任意词）仍然会被当成真人 ——
      枚举法永远追不上无限的名字空间；
    * **知情人能通过**：知道词表的人只要起一个像人名/像真人的机器身份
      （``Qing``、``build_007``、``李工``）就能直接放行。

    因此**真正的闭合只依赖一条**：由服务层注入
    ``MJSCXT_APPROVER_ALLOWLIST``（见 ``app/api/delivery.py``
    :func:`_approver_allowlist`），配置了它就是**严格白名单**，
    不在名单里的一律拒（fail-closed），词表法完全不再参与判定。
    **未配置该环境变量时，本函数只是「降低误用概率」，不构成任何安全保证** ——
    请勿把它当成认证来依赖。
    """
    who = _normalize_actor(authorized_by)
    if not who:
        return False
    allowed = {_normalize_actor(a) for a in (allowlist or ()) if str(a).strip()}
    if allowed:
        return who in allowed
    tokens = actor_tokens(who)
    for token in tokens:
        if token in _MACHINE_ACTOR_TOKENS:
            return False
        if token.startswith(_MACHINE_ACTOR_PREFIXES):
            return False
    for marker in _MACHINE_ACTOR_SUBSTRINGS:
        if marker in who:
            return False
    if not _ACTOR_ALLOWED_CHARS.match(who):
        return False
    # 非纯 ASCII 且不含中日韩字符（西里尔/希腊文等）一律视为可疑标识
    if any(ord(c) > 127 for c in who) and not _ACTOR_HAS_CJK.search(who):
        return False
    return True


def require_human_authorization(authorized_by: str,
                                authorization_ref: str = "",
                                allowlist: Optional[Iterable[str]] = None) -> None:
    """人工授权校验（铁律 1 的执行点，fail-closed）。

    拒绝：空值；机器账号（按 :func:`is_human_actor` 的口径，
    **不是**前缀 deny-list）；形状不像人的标识。

    「机器 QC 全绿」不能变成「已批准」—— 这是交付事故的经典成因。
    交付审批与媒体审批共用这一个函数，避免两套「人工批准」口径。

    ``authorization_ref``（外部凭据：工单号 / 签署记录）**不**用于放宽机器身份：
    它是客户端自报的一串字符串，机器自己也能编，凭它放行等于没有闸门。
    它只作为人工主体的留痕信息随批准一起落库。

    ⚠️ 本函数同样是**命名口径**，不是身份认证；未配置
    ``MJSCXT_APPROVER_ALLOWLIST`` 时，词表法只是降低误用概率。
    详见 :func:`is_human_actor` 的「已知上限」说明。
    """
    who = str(authorized_by or "").strip()
    if not who:
        raise ApprovalAuthorizationError("批准必须有人工授权主体（authorized_by 为空）")
    if not is_human_actor(who, allowlist):
        raise ApprovalAuthorizationError(
            "批准主体 %r 判定为机器账号：机器检查通过不等于人工批准" % who)
    return None


def build_approval(approval_id: str, *, selection: SelectionDecision,
                   media: MediaVersion, authorized_by: str,
                   authorization_ref: str = "", scope: str = "media",
                   note: str = "", decided_at: str = "",
                   allowlist: Optional[Iterable[str]] = None) -> ApprovalDecision:
    """构造批准决定（铁律 1 + 铁律 3 的执行点）。

    前置条件全部 fail-closed：采用必须存在且未被撤销、人工授权必须成立、
    哈希三元组必须算得出。任何一条不满足都**抛错**，绝不产出「半合法」的批准。

    ``allowlist`` 由服务层注入（``MJSCXT_APPROVER_ALLOWLIST``）：给了就只认名单，
    不给就退回命名口径。**批准与撤销必须传同一份名单** —— 两边口径不同的话，
    先被放宽的那套（默认就是 approve）就是实际入口。
    """
    if not isinstance(selection, SelectionDecision):
        raise NotSelectedError("批准必须引用一条 SelectionDecision（采用）")
    if not selection.selected or selection.revoked:
        raise NotSelectedError("该采用决定已失效（selected=%s revoked=%s），不得批准"
                               % (selection.selected, selection.revoked))
    if selection.media_version_id != media.media_version_id:
        raise ApprovalAuthorizationError(
            "批准对象与采用对象不一致：采用=%s 批准=%s"
            % (selection.media_version_id, media.media_version_id))
    require_human_authorization(authorized_by, authorization_ref, allowlist=allowlist)
    if not media.media_sha256:
        raise ApprovalAuthorizationError("批准需要 media_sha256 做哈希绑定，但该候选没有内容指纹")

    obj = ApprovalDecision(
        approval_id=str(approval_id), selection_id=selection.selection_id,
        subject_type=selection.subject_type, subject_id=selection.subject_id,
        media_version_id=media.media_version_id, bound_hash="",
        authorized_by=str(authorized_by).strip(),
        authorization_ref=str(authorization_ref or ""),
        media_sha256=media.media_sha256, intent_hash=selection.intent_hash,
        selection_hash=selection.selection_hash, scope=str(scope or "media"),
        approved=True, revoked=False, note=str(note or ""),
        decided_at=decided_at or _now(),
    )
    return ApprovalDecision(**{**obj.__dict__, "bound_hash": hash_payload(
        {"media_sha256": obj.media_sha256, "intent_hash": obj.intent_hash,
         "selection_hash": obj.selection_hash, "scope": obj.scope,
         "authorized_by": obj.authorized_by})})


def approval_is_valid(approval: Optional[ApprovalDecision], *,
                      media_sha256_now: str = "",
                      selection_hash_now: str = "") -> bool:
    """批准是否**仍然成立**（铁律 3 的执行点）。

    内容被替换（media_sha256 变了）→ 批准失效，必须重新批准。
    注意这里返回 False 只代表「绑定失效」，**不代表曾经没批准过**：
    界面必须区分「尚未批准」与「批准已失效」两种状态（沿用 quality_stage 的 unbound 思路）。
    """
    if approval is None or not approval.approved or approval.revoked:
        return False
    if not approval.bound_hash:
        return False
    if media_sha256_now and media_sha256_now != approval.media_sha256:
        return False
    if selection_hash_now and selection_hash_now != approval.selection_hash:
        return False
    return True


def assert_selection_not_approved(selection: Optional[SelectionDecision],
                                  approvals: Optional[Iterable[ApprovalDecision]] = None) -> None:
    """铁律 1 的**读侧**护栏：调用方声称「因为采用了所以放行」时必须先过这里。

    ⚠️ 这是一个**有牙齿**的断言 —— 收口审核发现它此前两个分支都直接 `return`，
    是「带着误导性 docstring 的死代码」：最关键的语义不变量，却留着一个永不报警
    的护栏，看起来有保护实则没有。

    现在它的语义是**在以下唯一合法情形放行**：

        存在一条**有效的** :class:`ApprovalDecision` 绑定了这条采用记录
        （即批准确实独立发生过，而不是由采用推导出来的）。

    其余情况一律抛 :class:`DomainError`：

        - ``selection`` 为空却声称在用这条采用；
        - 没有任何批准记录；
        - 批准记录存在但已失效（撤销 / 哈希失配 / 内容已变）——
          此时「因为采用了所以放行」正是本 ADR 要禁的那条路。

    Args:
        selection: 调用方声称正在使用的采用记录。
        approvals: 该采用对应的批准记录（可为空）。

    Raises:
        DomainError: 上述任一「无真批准」的情形。
    """
    if selection is None:
        raise DomainError(
            "selection_implies_approval: 调用方声称「因为采用了所以放行」，"
            "但没有提供任何采用记录。批准必须独立发生。"
        )
    for ap in (approvals or ()):
        if ap.selection_id == selection.selection_id and approval_is_valid(ap):
            return          # 已有**有效**的真批准，走的是正规路径
    raise DomainError(
        "selection_implies_approval: 采用记录 {} 没有对应的有效批准。"
        "采用是创作决定，批准是放行决定，前者永不隐式升级为后者。".format(
            getattr(selection, "selection_id", "<unknown>")
        )
    )


def decision_state(selection: Optional[SelectionDecision],
                   approval: Optional[ApprovalDecision], *,
                   media_sha256_now: str = "",
                   media_verified: bool = True) -> Dict[str, Any]:
    """把「采用 + 批准」压成界面可直接渲染的三态。

    关键点：``state`` 只会是 selected / approved / selected_not_approved / none
    之一，**永远不存在**「采用即视为批准」的分支。

    =========================  ====================================================
    state                      含义（界面必须照此措辞）
    =========================  ====================================================
    none                       尚未采用
    selected_not_approved      已采用，**未批准**（不得当批准用）
    approved                   已采用且已批准（哈希绑定仍成立）
    approved_stale             曾批准，但内容已变 → 必须重新批准
    approved_unverified        曾批准，但**取不到磁盘现状**（无路径 / 文件缺失 /
                               不可读）→ 不可判定，按「未验证」处理，绝不按「没变」
    =========================  ====================================================

    ``media_sha256_now`` 必须是**磁盘现状**的重算值，不是库内登记值 ——
    拿登记值和登记值比等于永远判「没变」，``approved_stale`` 永远不会出现。
    ``media_verified=False`` 表示拿不到现状（而不是「现状等于登记值」），
    此时一律不得返回 ``approved``（fail-closed）。
    """
    sel_state = "none"
    if selection is not None and selection.selected and not selection.revoked:
        sel_state = "selected"
    ap_state = "approved"
    if approval is None or not approval.approved or approval.revoked:
        ap_state = "none"
    elif not media_verified:
        ap_state = "unverified"
    elif media_sha256_now and approval.media_sha256 and \
            media_sha256_now != approval.media_sha256:
        ap_state = "stale"

    if sel_state == "none" and ap_state == "none":
        state = "none"
    elif ap_state == "approved":
        state = "approved"
    elif ap_state == "stale":
        state = "approved_stale"
    elif ap_state == "unverified" and sel_state == "selected":
        state = "approved_unverified"
    else:
        state = "selected_not_approved"
    return {
        "state": state,
        "selected": sel_state == "selected",
        "approved": ap_state == "approved",
        # 采用本身永不携带放行含义，这个字段就是给「别再偷懒」的调用方看的
        "selection_implies_approval": False,
        "media_sha256_now": str(media_sha256_now or ""),
        "media_verified": bool(media_verified),
    }


# =====================================================================
# CapabilityProfileVersion（能力档案版本化）
# =====================================================================

@dataclass(frozen=True)
class CapabilityProfileVersion:
    """能力档案的**一个版本**：某套模型 / 工作流在某台机器上真正能做什么。

    为什么必须版本化
    ----------------
    既有 ``model_capabilities.capability_summary()`` 只给**当前**能力快照。
    但一个 MediaVersion 是过去某一刻渲出来的 —— 半年后回看，
    「当时这台机器允许每镜 12 秒、最多 8 张参考图」还是今天这套上限？
    没有版本化就无法判定「当时合法」还是「当时就非法」。

    ``params`` 记实际能力值（如 ``{"max_shot_refs": 8, "max_shot_sec": 12.0}``），
    ``profile_hash`` 覆盖 params + version，同样是内容指纹。
    """
    profile_id: str
    version: int
    params: Mapping[str, Any] = field(default_factory=dict)
    profile_hash: str = ""
    deploy_profile: str = ""          # 对齐 config.DEPLOY_PROFILE（如 16G / 24G）
    comfyui_workflow_version: str = ""
    note: str = ""
    created_at: str = ""
    created_by: str = ""

    def hash_payload(self) -> Dict[str, Any]:
        return {"profile_id": str(self.profile_id), "version": int(self.version),
                "params": dict(self.params or {}),
                "deploy_profile": str(self.deploy_profile),
                "comfyui_workflow_version": str(self.comfyui_workflow_version)}

    def verify_hash(self) -> bool:
        return hash_payload(self.hash_payload()) == self.profile_hash

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.hash_payload())
        d.update({"profile_hash": self.profile_hash, "note": self.note,
                  "created_at": self.created_at, "created_by": self.created_by})
        return d

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "CapabilityProfileVersion":
        return CapabilityProfileVersion(
            profile_id=str(raw.get("profile_id") or ""), version=int(raw.get("version") or 0),
            params=dict(raw.get("params") or {}), profile_hash=str(raw.get("profile_hash") or ""),
            deploy_profile=str(raw.get("deploy_profile") or ""),
            comfyui_workflow_version=str(raw.get("comfyui_workflow_version") or ""),
            note=str(raw.get("note") or ""), created_at=str(raw.get("created_at") or ""),
            created_by=str(raw.get("created_by") or ""))


def build_capability_profile_version(profile_id: str, version: int, *,
                                     params: Optional[Mapping[str, Any]] = None,
                                     deploy_profile: str = "",
                                     comfyui_workflow_version: str = "",
                                     note: str = "", created_by: str = "",
                                     created_at: str = "") -> CapabilityProfileVersion:
    """登记能力档案的一个版本（版本号由调用方给定，不自动猜）。"""
    obj = CapabilityProfileVersion(
        profile_id=str(profile_id), version=int(version), params=dict(params or {}),
        profile_hash="", deploy_profile=str(deploy_profile or ""),
        comfyui_workflow_version=str(comfyui_workflow_version or ""),
        note=str(note or ""), created_at=created_at or _now(),
        created_by=str(created_by or ""),
    )
    return CapabilityProfileVersion(**{**obj.__dict__,
                                      "profile_hash": hash_payload(obj.hash_payload())})


def next_profile_version(existing: Iterable[Mapping[str, Any]],
                         profile_id: str) -> int:
    """下一个版本号（同一 profile 内单调递增；空表从 1 开始）。

    刻意**不自动 +1 落库**：并发登记两个版本时，
    由数据库唯一约束 (profile_id, version) 兜住并发重复，这里只给建议值。
    """
    versions = [int(e.get("version") or 0) for e in (existing or ())
                if str(e.get("profile_id") or "") == str(profile_id)]
    return (max(versions) + 1) if versions else 1


# =====================================================================
# 小工具（供编排层复用）
# =====================================================================

def latest_of(records: Sequence[Mapping[str, Any]], key: str = "decided_at") -> Optional[Dict[str, Any]]:
    """取同 subject 的最新一条决策（append-only 表的「当前态」读取）。"""
    items = list(records or ())
    if not items:
        return None
    return max(items, key=lambda r: (str(r.get(key) or ""), str(r.get("id") or "")))


def deep_copy_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """探测事实等 JSON 字段的防御性拷贝（避免调用方后续修改污染已落库版本）。"""
    return copy.deepcopy(dict(payload or {}))
# -*- coding: utf-8 -*-
"""自动放行策略引擎：「人事先授权的自动放行」，**不是**「机器人穿人名」。

这个模块存在的前提
------------------
上一轮重构立了一条铁律并且已经写进代码与 ADR：

    **机器校验通过 ≠ 人工批准**（ADR-0002 铁律 1 / ADR-0006）

于是「全自动无人值守」这个需求和这条铁律是直接冲突的 —— 如果照字面实现，
唯一的技术路径就是**让自动流程冒充一个人类主体**去调 ``approve()``：
把 ``authorized_by`` 填成 ``Qing``、``zhangsan`` 这种像人的名字，
``is_human_actor`` 的词表法查不出来，闸门就放行了。

那条路是本项目花了好几轮才关掉的绕过类，本模块**刻意不走**。

本模块的做法：把「人要什么」写成一张**可读、可审、可撤**的授权书
（``config/automation-policy.json``），机器只是照着办事。两者差别是本质的：

* 冒充：审计轨迹里写着「人批准的」，实际没有人看过第 37 集第 12 镜；
* 授权：审计轨迹里写着 ``auto-release/<policy>/<project>/<episode>/approve``，
  署 ``automation:managed-pipeline``，一眼看得出是机器按哪份策略干的。

不可协商的安全属性（本模块的全部意义所在）
----------------------------------------
**自动动作必须先证明自己被允许，再谈别的。**

    ⚠ 自动动作只有在声明的 principal 确实出现在严格白名单
      ``MJSCXT_APPROVER_ALLOWLIST`` 里时才可能放行。白名单为空、或 principal
      不在其中 ⇒ 一律拒绝。

    ⚠ 绝**不**在白名单缺失时退回 ``is_human_actor`` 的名字形状启发式。
      那一层在 :mod:`app.domain.production_facts` 自己的注释里就写明了
      「未配置该环境变量时，本函数只是降低误用概率，**不构成任何安全保证**」。
      把自动放行的安全建立在那一层上，等于把门禁架在流沙上。

这条属性买到三件事，且都是运维真正需要的：

1. 自动化**永远不能放大**操作者已经给出的授权范围（白名单没写它，它就动不了）；
2. 撤销自动化 = **删一行配置**（把 principal 从白名单里去掉），不改代码不必重启；
3. 白名单口径与 ``app/api/delivery.py`` / ``app/api/production_facts.py`` /
   ``app/api/timeline.py`` 三处**逐字同源**，不会「先被放宽的那套」变成实际入口。

为什么这个 principal 默认根本过不了启发式
----------------------------------------
``automation:managed-pipeline`` 规范化后切成 token 是
``('automation', 'managed', 'pipeline')`` —— ``automation`` 与 ``pipeline``
**都在机器词表里**（``production_facts.py`` 的 ``_MACHINE_ACTOR_TOKENS`` 与
``_MACHINE_ACTOR_SUBSTRINGS``）。也就是说：它能被放行的**唯一**通道就是上面那条
白名单。这不是巧合，是刻意的 —— 默认 principal 不可能被「看起来像人」蒙混过去。

模块边界
--------
* **不 import flask**。可以脱离服务器单独跑（运维要先 dry-run 再开）。
* **不碰 GPU / ComfyUI / ffmpeg**，不做任何生成。
* 所有函数**不抛异常**给调用方：策略缺失、损坏、不可读一律变成**拒绝决策**。
  fail-closed 意味着「读不懂 ⇒ 不许动」，而不是「读不懂 ⇒ 抛给上层」。
* 采用与批准是两个独立操作、两个独立开关。**任何代码路径都不会让 adopt 顺带 approve。**
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "STAGES", "QC_GATED_STAGES", "AR_REF_MARKER", "AR_ERROR_CODES",
    "AR_DISABLED", "AR_POLICY_UNREADABLE", "AR_POLICY_MALFORMED", "AR_STAGE_OFF",
    "AR_UNKNOWN_STAGE", "AR_PRINCIPAL_UNSET", "AR_ALLOWLIST_UNSET",
    "AR_PRINCIPAL_NOT_ALLOWLISTED", "AR_QC_THRESHOLD_UNSET", "AR_QC_SCORE_MISSING",
    "AR_QC_SCORE_RANGE", "AR_QC_SCORE_LOW", "AR_QC_CHECK_MISSING",
    "AR_CAP_UNSET", "AR_CAP_EXHAUSTED", "AR_LEDGER_UNREADABLE", "AR_LEDGER_CORRUPT",
    "AR_REF_UNMARKED", "AR_SUBJECT_INVALID", "AR_ACTION_FAILED",
    "Policy", "Decision", "AutoReleaseEngine", "load_policy", "approver_allowlist",
    "default_policy_path", "describe_environment",
]

#: 三个互相独立的阶段。刻意不合并（见策略文件 stages 字段注释）。
STAGES: Tuple[str, ...] = ("adopt", "approve", "deliver")

#: 必须过 QC 门槛的阶段。``adopt`` 不在其中 —— 采用是可逆的创作决定，
#: 且历史版本全留（ADR-0002 铁律 2）；把它和放行绑在一起就等于把两者重新压成一档。
#: （``adopt`` 仍然可以**额外**接受 qc 约束，见 ``_qc_gate`` —— 只可能更严。）
QC_GATED_STAGES: Tuple[str, ...] = ("approve", "deliver")

#: 留痕标记。**必须**出现在每一条 ``authorization_ref`` 里，否则拒绝执行。
#: 这是「自动动作在审计轨迹里一眼可辨」的最后一道机械保证：
#: 就算模板被人配错成 ``ref-{project}``，也产不出一条「看不出是机器」的自动批准。
AR_REF_MARKER = "auto-release"

# ---------------------------------------------------------------------------
# 稳定错误码（前端 / 运维按码分支，不解析文案 —— 与 DLV-*/F-* 同性质）
# ---------------------------------------------------------------------------
AR_POLICY_UNREADABLE = "AR-POLICY-UNREADABLE"
AR_POLICY_MALFORMED = "AR-POLICY-MALFORMED"
AR_DISABLED = "AR-DISABLED"
AR_UNKNOWN_STAGE = "AR-UNKNOWN-STAGE"
AR_STAGE_OFF = "AR-STAGE-OFF"
AR_PRINCIPAL_UNSET = "AR-PRINCIPAL-UNSET"
AR_ALLOWLIST_UNSET = "AR-ALLOWLIST-UNSET"
AR_PRINCIPAL_NOT_ALLOWLISTED = "AR-PRINCIPAL-NOT-ALLOWLISTED"
AR_QC_THRESHOLD_UNSET = "AR-QC-THRESHOLD-UNSET"
AR_QC_SCORE_MISSING = "AR-QC-SCORE-MISSING"
AR_QC_SCORE_RANGE = "AR-QC-SCORE-RANGE"
AR_QC_SCORE_LOW = "AR-QC-SCORE-LOW"
AR_QC_CHECK_MISSING = "AR-QC-CHECK-MISSING"
AR_CAP_UNSET = "AR-CAP-UNSET"
AR_CAP_EXHAUSTED = "AR-CAP-EXHAUSTED"
AR_LEDGER_UNREADABLE = "AR-LEDGER-UNREADABLE"
AR_LEDGER_CORRUPT = "AR-LEDGER-CORRUPT"
AR_REF_UNMARKED = "AR-REF-UNMARKED"
AR_SUBJECT_INVALID = "AR-SUBJECT-INVALID"
AR_ACTION_FAILED = "AR-ACTION-FAILED"

AR_ERROR_CODES: Dict[str, str] = {
    AR_POLICY_UNREADABLE: "策略文件缺失或不可读（fail-closed：读不懂就不许动）",
    AR_POLICY_MALFORMED: "策略文件结构非法（fail-closed：不猜、不兜底、不放行）",
    AR_DISABLED: "策略总开关关闭（enabled=false）",
    AR_UNKNOWN_STAGE: "未知阶段名",
    AR_STAGE_OFF: "该阶段的独立开关未打开",
    AR_PRINCIPAL_UNSET: "策略未声明 principal",
    AR_ALLOWLIST_UNSET: "未配置 MJSCXT_APPROVER_ALLOWLIST，拒绝退回名字形状启发式",
    AR_PRINCIPAL_NOT_ALLOWLISTED: "principal 不在 MJSCXT_APPROVER_ALLOWLIST 白名单内",
    AR_QC_THRESHOLD_UNSET: "QC 门槛未配置（未配置 ≠ 没有门槛，故不可满足）",
    AR_QC_SCORE_MISSING: "调用方未提供 QC 分数",
    AR_QC_SCORE_RANGE: "QC 分数越界，数据不可信",
    AR_QC_SCORE_LOW: "QC 分数未达 min_score 门槛",
    AR_QC_CHECK_MISSING: "必需的 QC 检查项缺失或未通过",
    AR_CAP_UNSET: "每日上限未配置（0 或缺失即不可满足）",
    AR_CAP_EXHAUSTED: "当日自动放行次数已达上限",
    AR_LEDGER_UNREADABLE: "计数台账不可读（fail-closed：无法确认已放行几次）",
    AR_LEDGER_CORRUPT: "计数台账损坏（fail-closed）",
    AR_REF_UNMARKED: "authorization_ref 缺少自动标记或策略 id",
    AR_SUBJECT_INVALID: "缺少动作对象标识（media_version_id / package_id）",
    AR_ACTION_FAILED: "真实闸门拒绝或写入失败（已如实上报，不降级）",
}

_POLICY_RELPATH = os.path.join("config", "automation-policy.json")
_REF_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


# ---------------------------------------------------------------------------
# 环境 / 路径
# ---------------------------------------------------------------------------

def _project_root() -> str:
    """源码根（``app/`` 的上一级）。与 ``env_loader.PROJECT_ROOT_DIR`` 同口径。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_policy_path() -> str:
    return os.path.join(_project_root(), _POLICY_RELPATH)


def _data_root() -> str:
    """可写数据根。**与 app/env_loader.py:53-55 逐字同口径**（MJSCXT_DATA_DIR 守卫）。

    这里刻意不 import env_loader：那会在 import 期牵出 .env 加载等副作用，
    而本模块必须能被单独 import 起来做 dry-run。代价是这条耦合写在注释里 ——
    改 ``MJSCXT_DATA_DIR`` 的语义时要连这里一起改。
    """
    return os.path.abspath(os.environ.get("MJSCXT_DATA_DIR") or _project_root())


def approver_allowlist() -> List[str]:
    """严格人工主体白名单（``MJSCXT_APPROVER_ALLOWLIST``，逗号或全角分号分隔）。

    ⚠ 这段解析是**有意复制**而非 import，与以下三处逐字同源：
      * ``app/api/delivery.py:54-62``       ``_approver_allowlist``
      * ``app/api/production_facts.py:93-101`` ``_approver_allowlist``
      * ``app/api/timeline.py:278-289``     ``_approver_allowlist``
    复制是为了让本模块不依赖 flask 蓝图（要能脱离服务器跑 dry-run）。
    ``app/api/timeline.py`` 的注释已经写过这个取舍：「口径一致靠的是共用同一个
    领域函数」—— 也就是说**判定**最终仍由
    :func:`app.domain.production_facts.require_human_authorization` 做，
    本函数只负责把同一份环境变量读成同一个列表。
    **改这一行，请同时改那三行**：先被放宽的那套才会成为实际入口。
    """
    raw = os.environ.get("MJSCXT_APPROVER_ALLOWLIST", "") or ""
    return [p.strip() for p in raw.replace("；", ",").split(",") if p.strip()]


def _normalize_actor(who: str) -> str:
    """主体标识规范化，**必须**与 ``production_facts._normalize_actor`` 同口径
    （NFKC → casefold → strip），否则「引擎以为自己被授权了、真实闸门不认」。

    注意这只是**预检**：真实判定永远由 ``require_human_authorization`` 做。
    这里对不上，最坏结果是引擎早一步给了个更啰嗦的拒绝理由，不会误放行。
    """
    return unicodedata.normalize("NFKC", str(who or "")).casefold().strip()


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    """一份已解析的策略。**非法即 ``ok=False``，绝不返回半合法的策略。**"""

    ok: bool
    code: str
    error: str
    path: str
    schema_version: int = 0
    policy_id: str = ""
    enabled: bool = False
    principal: str = ""
    ref_template: str = ""
    stages: Mapping[str, bool] = field(default_factory=dict)
    min_score: Optional[float] = None
    required_checks: Tuple[str, ...] = ()
    score_range: Tuple[float, float] = (0.0, 1.0)
    max_releases: int = 0
    counted_stages: Tuple[str, ...] = ("approve", "deliver")
    ledger_rel: str = "output/auto_release/daily-ledger.json"

    def stage_on(self, stage: str) -> bool:
        return bool(self.stages.get(stage, False))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "code": self.code, "error": self.error, "path": self.path,
            "schema_version": self.schema_version, "policy_id": self.policy_id,
            "enabled": self.enabled, "principal": self.principal,
            "authorization_ref_template": self.ref_template,
            "stages": dict(self.stages), "qc": {
                "min_score": self.min_score,
                "required_checks": list(self.required_checks),
                "score_range": list(self.score_range),
            },
            "daily_cap": {"max_releases": self.max_releases,
                          "counted_stages": list(self.counted_stages),
                          "ledger_path": self.ledger_rel},
        }


def _bad(code: str, error: str, path: str) -> Policy:
    return Policy(ok=False, code=code, error=error, path=path)


def _malformed(path: str, field_name: str, detail: str) -> Policy:
    """结构非法 → 整份策略拒收，**错误文案点名具体字段**（运维要照着改）。"""
    return _bad(AR_POLICY_MALFORMED,
                "策略字段 %s 非法：%s（fail-closed：宁可全部不动，也不按猜测放行）"
                % (field_name, detail), path)


def load_policy(path: Optional[str] = None) -> Policy:
    """读取并**严格校验**策略文件。**永不抛异常** —— 任何问题都变成 ``ok=False``。

    严格校验的含义：类型不对就是拒收（``"min_score": "high"`` 不按字符串处理，
    也不按 0 处理）。而 ``"min_score": null`` 是**合法**的「未配置」状态，
    留给 QC 闸门以 ``AR-QC-THRESHOLD-UNSET`` 拒绝 —— 两种「没配」都拒绝，
    但报错的原因不同，运维才知道自己该配什么。
    """
    p = path or default_policy_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return _bad(AR_POLICY_UNREADABLE, "策略文件不存在：%s（fail-closed）" % p, p)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _bad(AR_POLICY_UNREADABLE,
                    "策略文件不可读或不是合法 JSON：%s（%s）" % (p, exc), p)

    if not isinstance(raw, dict):
        return _malformed(p, "<root>", "顶层必须是对象，实际是 %s" % type(raw).__name__)

    # -- schema_version -------------------------------------------------
    sv = raw.get("schema_version", 1)
    if not isinstance(sv, int) or isinstance(sv, bool):
        return _malformed(p, "schema_version", "必须是整数，实际 %r" % (sv,))

    # -- enabled（缺失 = false） -----------------------------------------
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        return _malformed(p, "enabled", "必须是布尔值，实际 %r" % (enabled,))

    # -- policy_id -------------------------------------------------------
    policy_id = raw.get("policy_id", "")
    if not isinstance(policy_id, str):
        return _malformed(p, "policy_id", "必须是字符串，实际 %r" % (policy_id,))
    policy_id = policy_id.strip()

    # -- principal（缺失合法，但闸门会以 AR-PRINCIPAL_UNSET 拒绝）--------
    principal = raw.get("principal", "")
    if not isinstance(principal, str):
        return _malformed(p, "principal", "必须是字符串，实际 %r" % (principal,))

    # -- authorization_ref_template -------------------------------------
    ref_tpl = raw.get("authorization_ref_template", "")
    if not isinstance(ref_tpl, str):
        return _malformed(p, "authorization_ref_template", "必须是字符串，实际 %r" % (ref_tpl,))

    # -- stages（三个独立开关，缺失 = false） -----------------------------
    stages_raw = raw.get("stages", {})
    if not isinstance(stages_raw, dict):
        return _malformed(p, "stages", "必须是对象，实际 %s" % type(stages_raw).__name__)
    stages: Dict[str, bool] = {}
    for name in STAGES:
        v = stages_raw.get(name, False)
        if not isinstance(v, bool):
            return _malformed(p, "stages.%s" % name, "必须是布尔值，实际 %r" % (v,))
        stages[name] = v
    for extra in sorted(set(stages_raw) - set(STAGES)):
        return _malformed(p, "stages.%s" % extra,
                          "未知阶段名（合法值 %s）" % (list(STAGES),))

    # -- qc -------------------------------------------------------------
    qc = raw.get("qc", {})
    if not isinstance(qc, dict):
        return _malformed(p, "qc", "必须是对象，实际 %s" % type(qc).__name__)

    min_score = qc.get("min_score", None)
    if min_score is not None:
        if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
            return _malformed(p, "qc.min_score",
                              "必须是数字或 null（null=未配置=不可满足），实际 %r" % (min_score,))
        min_score = float(min_score)

    checks = qc.get("required_checks", [])
    if not isinstance(checks, list) or any(not isinstance(c, str) for c in checks):
        return _malformed(p, "qc.required_checks",
                          "必须是字符串数组（空数组=未配置=不可满足），实际 %r" % (checks,))
    required_checks = tuple(c.strip() for c in checks if c.strip())

    rng = qc.get("score_range", [0.0, 1.0])
    if (not isinstance(rng, list) or len(rng) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in rng)):
        return _malformed(p, "qc.score_range", "必须是两个数字组成的数组，实际 %r" % (rng,))
    lo, hi = float(rng[0]), float(rng[1])
    if lo > hi:
        return _malformed(p, "qc.score_range", "下界 %r 大于上界 %r" % (lo, hi))

    # -- daily_cap -------------------------------------------------------
    cap = raw.get("daily_cap", {})
    if not isinstance(cap, dict):
        return _malformed(p, "daily_cap", "必须是对象，实际 %s" % type(cap).__name__)
    max_releases = cap.get("max_releases", 0)
    if isinstance(max_releases, bool) or not isinstance(max_releases, int):
        return _malformed(p, "daily_cap.max_releases",
                          "必须是整数（0=未配置=不可满足），实际 %r" % (max_releases,))
    if max_releases < 0:
        return _malformed(p, "daily_cap.max_releases", "不能为负，实际 %r" % (max_releases,))

    counted_raw = cap.get("counted_stages", ["approve", "deliver"])
    if (not isinstance(counted_raw, list)
            or any(not isinstance(c, str) for c in counted_raw)):
        return _malformed(p, "daily_cap.counted_stages", "必须是字符串数组，实际 %r" % (counted_raw,))
    counted = tuple(c.strip() for c in counted_raw if c.strip())
    bad_stage = [c for c in counted if c not in STAGES]
    if bad_stage:
        return _malformed(p, "daily_cap.counted_stages", "含未知阶段 %s" % (bad_stage,))

    ledger_rel = cap.get("ledger_path", "output/auto_release/daily-ledger.json")
    if not isinstance(ledger_rel, str) or not ledger_rel.strip():
        return _malformed(p, "daily_cap.ledger_path", "必须是非空字符串，实际 %r" % (ledger_rel,))

    return Policy(
        ok=True, code="", error="", path=p, schema_version=sv,
        policy_id=policy_id, enabled=enabled, principal=principal.strip(),
        ref_template=ref_tpl.strip(), stages=stages, min_score=min_score,
        required_checks=required_checks, score_range=(lo, hi),
        max_releases=max_releases, counted_stages=counted,
        ledger_rel=ledger_rel.strip(),
    )


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """一次判定的完整结果 —— 既是**放行凭证**，也是**拒绝理由**。

    无论允许还是拒绝，``policy_id`` / ``principal`` / ``thresholds`` / ``cap`` /
    ``authorization_ref`` 都会带上。拒绝时 ``reason`` 必须点名**具体是哪个门槛**
    （"QC 分数 0.62 未达 min_score 0.80"），而不是笼统的「不允许」——
    运维要靠这句话知道下一步改哪里。
    """

    allowed: bool
    stage: str
    code: str
    reason: str
    policy_id: str = ""
    principal: str = ""
    authorization_ref: str = ""
    thresholds: Mapping[str, Any] = field(default_factory=dict)
    cap: Mapping[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    action: str = ""
    subject: str = ""
    result: Optional[Dict[str, Any]] = None
    #: **闸门实际判定过的那一份** :class:`Policy`。执行阶段必须用它，不要重读。
    #:
    #: 此前执行阶段调 ``self.policy`` 又把文件读了一遍（:meth:`AutoReleaseEngine.policy`
    #: 每次访问都 ``load_policy``）。两次读之间策略若被改，闸门用的阈值/额度
    #: 与真正执行用的就不是同一个对象 —— 审计留痕记的是 ``d.policy_id``，
    #: 跑的却是另一份策略的额度。带上这个引用就消除了这个窗口。
    #: 不进 :meth:`to_dict`（它是行为对象，不是对外契约字段）。
    policy: Optional[Policy] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed, "stage": self.stage, "code": self.code,
            "reason": self.reason, "policy_id": self.policy_id,
            "principal": self.principal, "authorization_ref": self.authorization_ref,
            "thresholds": dict(self.thresholds), "cap": dict(self.cap),
            "dry_run": self.dry_run, "action": self.action, "subject": self.subject,
            "result": self.result,
        }


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class AutoReleaseEngine:
    """按策略执行 / 预演自动放行。

    依赖全部**注入**：策略路径、白名单、台账路径、时钟。这样纯逻辑探针可以在
    临时目录里把 12 条判定路径全走一遍，不需要服务器、不需要 GPU。
    真正的仓储 / 领域对象由调用方在每个动作里传入（本模块不建连接、不读全局单例）。
    """

    def __init__(self, policy_path: Optional[str] = None, *,
                 allowlist: Optional[Sequence[str]] = None,
                 ledger_path: Optional[str] = None,
                 now_fn: Optional[Callable[[], datetime]] = None,
                 policy: Optional[Policy] = None):
        self._policy_path = policy_path
        self._policy_override = policy
        # allowlist=None 表示「读环境变量」；显式传 [] 表示「已确认白名单为空」。
        self._allowlist = list(allowlist) if allowlist is not None else approver_allowlist()
        self._ledger_override = ledger_path
        self._now_fn = now_fn or datetime.now

    # -- 只读属性 ---------------------------------------------------------

    @property
    def policy(self) -> Policy:
        return self._policy_override if self._policy_override is not None \
            else load_policy(self._policy_path)

    @property
    def allowlist(self) -> List[str]:
        return list(self._allowlist)

    def ledger_path(self) -> str:
        if self._ledger_override:
            return self._ledger_override
        rel = self.policy.ledger_rel
        return rel if os.path.isabs(rel) else os.path.join(_data_root(), rel)

    def describe(self) -> Dict[str, Any]:
        """当前策略 + 白名单门禁状态的完整快照（运维上线前的自查入口）。"""
        pol = self.policy
        who = _normalize_actor(pol.principal)
        allowed = {_normalize_actor(a) for a in self._allowlist if str(a).strip()}
        if not pol.ok:
            gate = {"allowlist_configured": bool(allowed), "principal_in_allowlist": False,
                    "note": "策略未加载成功，白名单不参与判定"}
        elif not who:
            gate = {"allowlist_configured": bool(allowed), "principal_in_allowlist": False,
                    "note": "策略未声明 principal"}
        elif not allowed:
            gate = {"allowlist_configured": False, "principal_in_allowlist": False,
                    "note": "未配置 MJSCXT_APPROVER_ALLOWLIST：拒绝退回名字形状启发式"}
        else:
            gate = {"allowlist_configured": True, "principal_in_allowlist": who in allowed,
                    "note": "白名单为严格档：只有名单内主体可放行"}
        return {
            "policy": pol.to_dict(),
            "policy_path": pol.path,
            "allowlist": list(self._allowlist),
            "allowlist_gate": gate,
            "ledger_path": self.ledger_path(),
            "effective_now": self._now_fn().isoformat(timespec="seconds"),
        }

    # -- 内部：拒绝构造 ---------------------------------------------------

    def _deny(self, stage: str, code: str, reason: str, *, pol: Policy,
              ref: str = "", thresholds: Optional[Mapping[str, Any]] = None,
              cap: Optional[Mapping[str, Any]] = None, subject: str = "",
              action: str = "", dry_run: bool = False) -> Decision:
        return Decision(allowed=False, stage=stage, code=code, reason=reason,
                        policy_id=pol.policy_id, principal=pol.principal,
                        authorization_ref=ref,
                        thresholds=dict(thresholds or {}), cap=dict(cap or {}),
                        dry_run=dry_run, action=action, subject=subject, policy=pol)

    # -- 闸门 1：白名单（**最重要的一条**） --------------------------------

    def _allowlist_gate(self, pol: Policy) -> Tuple[bool, str, str]:
        """principal 是否真的被操作者授权。返回 ``(ok, code, reason)``。

        ══════════════════════════════════════════════════════════════════
        ⚠⚠⚠ 本函数是整个模块的安全支点，改它之前请先读完上面那段模块 docstring。

        规则只有两条，都很死：

        1. 白名单**为空**（未配置 MJSCXT_APPROVER_ALLOWLIST）⇒ 拒绝。
        2. principal **不在**白名单里 ⇒ 拒绝。

        两条都**不**回退到 :func:`app.domain.production_facts.is_human_actor`
        的词表 / 形状启发式。那一层明确不是认证（见该函数「已知上限」：
        新造的名字能通过、知情人起个像人名的机器身份也能通过），
        把自动放行建在它上面，等于本项目前几轮关掉的绕过类又通电了。

        预检过了之后，真正的判定仍由
        :func:`app.domain.production_facts.require_human_authorization` 在
        ``ProductionService.approve`` / 交付批准路径里**再执行一次**。
        本模块只是提前给出可读的拒绝理由，不替代它。
        ══════════════════════════════════════════════════════════════════
        """
        principal = str(pol.principal or "").strip()
        if not principal:
            return False, AR_PRINCIPAL_UNSET, (
                "策略未声明 principal：没有署名主体的自动动作不允许发生")
        allowed = {_normalize_actor(a) for a in self._allowlist if str(a).strip()}
        if not allowed:
            return False, AR_ALLOWLIST_UNSET, (
                "未配置 MJSCXT_APPROVER_ALLOWLIST。自动放行**拒绝**退回 "
                "is_human_actor 的名字形状启发式（该层不是认证，只降低误用概率）。"
                "请显式把 %r 加入白名单后再开自动放行。" % principal)
        if _normalize_actor(principal) not in allowed:
            return False, AR_PRINCIPAL_NOT_ALLOWLISTED, (
                "principal %r 不在 MJSCXT_APPROVER_ALLOWLIST 内。自动化不得放大操作者"
                "已给出的授权范围 —— 请把该主体显式加入白名单（本步即撤销点：删掉即可）。"
                "当前白名单：%s" % (principal, sorted(allowed)))
        return True, "", ""

    # -- 闸门 2：QC 门槛（fail-closed：未配置即不可满足） ------------------

    def _qc_gate(self, stage: str, pol: Policy,
                 qc: Optional[Mapping[str, Any]]) -> Tuple[bool, str, str, Dict[str, Any]]:
        """QC 门槛。返回 ``(ok, code, reason, thresholds)``。

        「未配置」与「没达标」在这里**都**是拒绝，区别只在 ``min_score=None`` /
        ``required_checks=()`` 这一种：那是**不可满足**（fail-closed），
        绝不是「没有门槛所以放行」。后者是本项目吃过的那类亏。
        """
        thresholds: Dict[str, Any] = {
            "min_score": pol.min_score, "required_checks": list(pol.required_checks),
            "score_range": list(pol.score_range),
        }
        if stage not in QC_GATED_STAGES and qc is None:
            return True, "", "", thresholds          # adopt 默认不受 QC 约束

        if pol.min_score is None:
            return False, AR_QC_THRESHOLD_UNSET, (
                "qc.min_score 未配置 ⇒ **不可满足**。未配置门槛不等于没有门槛，"
                "本引擎拒绝按「无门槛」放行。请显式写死一个分数下界。"), thresholds
        if not pol.required_checks:
            return False, AR_QC_THRESHOLD_UNSET, (
                "qc.required_checks 为空 ⇒ **不可满足**。未配置检查项不等于不需要检查。"
                "请显式列出必须为 true 的检查项名字。"), thresholds
        if not isinstance(qc, Mapping):
            return False, AR_QC_SCORE_MISSING, (
                "本阶段需要 QC 数据（score + checks），调用方未提供。"), thresholds

        score = qc.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return False, AR_QC_SCORE_MISSING, (
                "QC 数据缺少可用的数值 score（实际 %r）。" % (score,)), thresholds
        score = float(score)
        lo, hi = pol.score_range
        if not (lo <= score <= hi):
            return False, AR_QC_SCORE_RANGE, (
                "QC 分数 %r 越出合法区间 [%r, %r]：数据不可信，拒绝放行。"
                % (score, lo, hi)), thresholds
        if score < pol.min_score:
            return False, AR_QC_SCORE_LOW, (
                "QC 分数 %.4f 未达门槛 min_score %.4f（差 %.4f）。"
                % (score, pol.min_score, pol.min_score - score)), thresholds

        checks = qc.get("checks")
        if not isinstance(checks, Mapping):
            return False, AR_QC_CHECK_MISSING, (
                "QC 数据缺少 checks 字典，无法核对必需检查项。"), thresholds
        missing = [name for name in pol.required_checks if not checks.get(name)]
        if missing:
            return False, AR_QC_CHECK_MISSING, (
                "必需检查项未通过或缺失：%s（required_checks=%s）。"
                % (missing, list(pol.required_checks))), thresholds

        thresholds["score_actual"] = score
        thresholds["checks_passed"] = list(pol.required_checks)
        return True, "", "", thresholds

    # -- 闸门 3：每日上限 -------------------------------------------------

    #: 每日额度台账的**进程内**互斥锁。
    #:
    #: ``os.replace`` 只保证**单次写**原子，保证不了「读 → 改 → 写」整段不可分割：
    #: 两个执行体同时读到 ``used=N``，各自算出 ``N+1``，后写的覆盖先写的 ——
    #: 台账停在 N+1，实际却放行了 N+2 次，``max_releases`` 被突破。
    #: :mod:`app.autopilot` 明确存在并发执行体（busy 分支），所以这不是假想。
    #:
    #: 锁覆盖**读-改-写整段**（不是只包住写），这样额度判定与占用之间没有窗口。
    #: 已知上限：这是**进程内**锁，两个进程指向同一台账仍会互撞 ——
    #: 彻底解决需要文件锁或 SQLite，那会牵扯 delivery.db 迁移，超出本次范围。
    _LEDGER_LOCK = threading.RLock()

    def _read_ledger(self) -> Tuple[Optional[Dict[str, Any]], str, str]:
        """读计数台账。返回 ``(ledger, code, reason)``；``code`` 非空即读不了。

        缺失文件 → ``{}``（今天确实还没放行过，这是安全的一侧）。
        **损坏 → 拒绝**：算不出今天放行了几次，就等于上限不可信，
        此时放行会静默突破上限 —— 那是 fail-open，不能要。

        读是**纯读**，不持锁；调用方若随后要写（见 :meth:`_consume_cap`），
        必须在同一把锁里完成读-改-写。
        """
        path = self.ledger_path()
        if not os.path.exists(path):
            return {}, "", ""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return None, AR_LEDGER_UNREADABLE, (
                "计数台账不可读：%s（%s）。无法确认当日已放行次数 ⇒ 拒绝。" % (path, exc))
        if not isinstance(data, dict):
            return None, AR_LEDGER_CORRUPT, "计数台账顶层不是对象：%s" % path
        return data, "", ""

    def _count_today(self, ledger: Mapping[str, Any], day: str) -> Tuple[int, str]:
        entry = ledger.get(day)
        if entry is None:
            return 0, ""
        if not isinstance(entry, dict):
            return 0, AR_LEDGER_CORRUPT
        used = entry.get("releases")
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            return 0, AR_LEDGER_CORRUPT
        return used, ""

    def _cap_gate(self, pol: Policy, stage: str) -> Tuple[bool, str, str, Dict[str, Any]]:
        cap: Dict[str, Any] = {
            "max_releases": pol.max_releases, "counted": stage in pol.counted_stages,
            "ledger_path": self.ledger_path(), "used": None, "remaining": None,
        }
        if stage not in pol.counted_stages:
            return True, "", "", cap          # 该阶段不计入上限
        if pol.max_releases <= 0:
            return False, AR_CAP_UNSET, (
                "daily_cap.max_releases = %r（未配置）⇒ **不可满足**。"
                "每日上限是限制配错策略时爆炸半径的唯一手段，引擎拒绝按「无上限」放行。"
                % (pol.max_releases,)), cap
        with self._LEDGER_LOCK:          # 与 _consume_cap 同一把锁，读到的 used 不会是陈旧值
            ledger, code, reason = self._read_ledger()
            if code:
                return False, code, reason, cap
            day = self._now_fn().strftime("%Y-%m-%d")
            used, err = self._count_today(ledger or {}, day)
            if err:
                return False, AR_LEDGER_CORRUPT, (
                    "计数台账条目 %r 结构非法：无法确认当日已放行次数 ⇒ 拒绝。" % day), cap
            cap["used"] = used
            cap["remaining"] = max(0, pol.max_releases - used)
            if used >= pol.max_releases:
                return False, AR_CAP_EXHAUSTED, (
                    "当日（%s）自动放行已达上限 %d 次（daily_cap.max_releases），拒绝。"
                    "台账：%s" % (day, pol.max_releases, self.ledger_path())), cap
        return True, "", "", cap

    def _consume_cap(self, pol: Policy, stage: str, subject: str, ref: str) -> str:
        """**先**占一个额度再执行动作（顺序不能反）。

        反过来（先放行再记账）的话，记账失败时批准已经落库了，
        上限就被静默突破 —— 而「上限」的全部意义就是兜住这种情况。
        记账不可写 ⇒ 直接不放行。

        返回错误码，空串表示占用成功。崩溃在「占用成功」与「退款」之间的窗口里
        会**烧掉一个额度**（而不是多放行一次）—— 方向是 fail-closed，可以接受。

        ⚠ ``max_releases <= 0`` 返回的是**明确的拒绝码** ``AR-CAP-UNSET``，
        不是空串。此前它和「该阶段不计入上限」共用一个 ``return ""``，于是
        调用方把「策略没配额度」当成「占用成功」：不扣额度，却继续放行 ——
        而 :meth:`_cap_gate` 对同一份策略是判 ``AR-CAP-UNSET`` 拒绝的。
        闸门拒、执行放行，两边对同一份配置给出相反结论。
        """
        if stage not in pol.counted_stages:
            return ""                    # 该阶段不计入上限：不是拒绝，是不适用
        if pol.max_releases <= 0:
            # 未配置 = 不可满足（与 _cap_gate 同一口径）。这里必须**拒**，
            # 静默跳过额度等于把「没配」读成「不限量」。
            return AR_CAP_UNSET
        path = self.ledger_path()
        with self._LEDGER_LOCK:          # 读-改-写整段串行化
            ledger, code, _ = self._read_ledger()
            if code or ledger is None:
                return code or AR_LEDGER_UNREADABLE
            day = self._now_fn().strftime("%Y-%m-%d")
            used, err = self._count_today(ledger, day)
            if err or used >= pol.max_releases:
                return AR_LEDGER_CORRUPT if err else AR_CAP_EXHAUSTED
            entry = ledger.get(day)
            entry = dict(entry) if isinstance(entry, dict) else {}
            entries = entry.get("entries")
            entries = list(entries) if isinstance(entries, list) else []
            entries.append({
                "at": self._now_fn().isoformat(timespec="seconds"), "stage": stage,
                "subject": str(subject or ""), "principal": pol.principal,
                "policy_id": pol.policy_id, "authorization_ref": ref, "automatic": True,
            })
            entry["releases"] = used + 1
            entry["entries"] = entries[-200:]      # 只留最近 200 条，文件不无限涨
            ledger[day] = entry
            try:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(ledger, fh, ensure_ascii=False, indent=2, sort_keys=True)
                os.replace(tmp, path)              # 原子替换，避免半截台账
            except OSError as exc:
                logger.error("自动放行：台账写入失败 %s（%s）⇒ 拒绝放行", path, exc)
                return AR_LEDGER_UNREADABLE
        return ""

    def _refund_cap(self, pol: Policy, stage: str) -> None:
        """动作失败后**尽力**退回额度。失败也只是多烧一个额度（fail-closed）。"""
        if stage not in pol.counted_stages or pol.max_releases <= 0:
            return
        path = self.ledger_path()
        with self._LEDGER_LOCK:          # 与 _consume_cap 同一把锁，避免退款覆盖别人的占用
            ledger, code, _ = self._read_ledger()
            if code or ledger is None:
                return
            day = self._now_fn().strftime("%Y-%m-%d")
            entry = ledger.get(day)
            if not isinstance(entry, dict):
                return
            used = entry.get("releases")
            if isinstance(used, bool) or not isinstance(used, int) or used <= 0:
                return
            entry["releases"] = used - 1
            entries = entry.get("entries")
            if isinstance(entries, list) and entries:
                entries.pop()
            ledger[day] = entry
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(ledger, fh, ensure_ascii=False, indent=2, sort_keys=True)
                os.replace(tmp, path)
            except OSError:
                logger.warning("自动放行：额度退款失败（台账 %s），已消耗的额度不找回", path)

    # -- 留痕串 -----------------------------------------------------------

    def _render_ref(self, pol: Policy, stage: str, *, project: str, episode: str,
                    subject: str) -> Tuple[str, str]:
        """渲染 ``authorization_ref`` 并**校验它自带自动标记**。

        未知占位符原样保留（``{typo}`` 就留在串里）—— 让配置错误在审计轨迹里
        显形，比悄悄渲染成空串好。
        """
        def _fill(value: Any, fallback: str) -> str:
            """占位符取值统一成 str。

            两处必须小心：
            1. ``re.sub`` 的替换值必须是 str。调用方传进来的 ``episode`` 常是 int
               （流水线里就是 ``episode_no``），直接塞进去会抛
               ``TypeError: sequence item N: expected str instance, int found`` ——
               表现是「策略一开就崩」，而不是安静地拒绝。
            2. 不能用 ``value or fallback``：集号 0 是合法输入，
               ``0 or "unknown-episode"`` 会把它变成 unknown，审计轨迹就撒谎了。
               只把 ``None`` / 空串当缺失。
            """
            if value is None:
                return fallback
            if isinstance(value, str):
                return value if value.strip() else fallback
            return str(value)

        values = {
            "policy_id": _fill(pol.policy_id, "unidentified-policy"),
            "project": _fill(project, "unknown-project"),
            "episode": _fill(episode, "unknown-episode"),
            "stage": _fill(stage, "unknown-stage"),
            "subject": _fill(subject, "unknown-subject"),
            "principal": _fill(pol.principal, "unknown-principal"),
            "date": self._now_fn().strftime("%Y-%m-%d"),
        }
        ref = _REF_PLACEHOLDER.sub(
            lambda m: str(values.get(m.group(1), m.group(0))), pol.ref_template)
        if AR_REF_MARKER not in ref:
            return ref, ("authorization_ref 渲染结果 %r 不含自动标记 %r。"
                         "自动动作必须一眼可辨，拒绝对外执行。" % (ref, AR_REF_MARKER))
        if not pol.policy_id:
            # 与 principal 的既有处理对齐：缺字段在 load_policy 阶段是**合法**的
            # （只校验类型），由闸门以明确错误码拒绝，而不是靠下游代码容忍。
            return ref, ("策略未声明 policy_id（为空）。没有策略标识的自动批准留痕，"
                         "事后无法回答「是哪份策略干的」—— 正是这条检查要防的事，拒绝执行。")
        if pol.policy_id not in ref:
            return ref, ("authorization_ref 渲染结果 %r 不含 policy_id %r。"
                         "事后无法回答「是哪份策略干的」，拒绝执行。"
                         % (ref, pol.policy_id))
        return ref, ""

    # -- 总闸门 -----------------------------------------------------------

    def _preflight(self, stage: str, *, subject: str = "", project: str = "",
                   episode: str = "", qc: Optional[Mapping[str, Any]] = None,
                   action: str = "", dry_run: bool = False) -> Decision:
        """七道闸门串成一个纯判定。**不碰任何数据库。**"""
        pol = self.policy
        if not pol.ok:
            return self._deny(stage, pol.code, pol.error, pol=pol, subject=subject,
                              action=action, dry_run=dry_run)
        if stage not in STAGES:
            return self._deny(stage, AR_UNKNOWN_STAGE,
                              "未知阶段 %r（合法值 %s）" % (stage, list(STAGES)),
                              pol=pol, subject=subject, action=action, dry_run=dry_run)
        if not pol.enabled:
            return self._deny(stage, AR_DISABLED, AR_ERROR_CODES[AR_DISABLED], pol=pol,
                              subject=subject, action=action, dry_run=dry_run)
        if not pol.stage_on(stage):
            return self._deny(stage, AR_STAGE_OFF,
                              "阶段 %s 的独立开关为 false（采用/批准/交付互不影响，"
                              "关掉一个不影响其余）。" % stage, pol=pol, subject=subject,
                              action=action, dry_run=dry_run)
        if not str(subject or "").strip():
            return self._deny(stage, AR_SUBJECT_INVALID,
                              "缺少动作对象标识（media_version_id / package_id），"
                              "拒绝在没有明确对象的情况下动任何记录。", pol=pol,
                              action=action, dry_run=dry_run)

        # 闸门顺序刻意如此：先证明「被允许」，再谈「做不做得到」。
        ok, code, reason = self._allowlist_gate(pol)
        if not ok:
            return self._deny(stage, code, reason, pol=pol, subject=subject,
                              action=action, dry_run=dry_run)

        ref, ref_err = self._render_ref(pol, stage, project=project, episode=episode,
                                        subject=str(subject))
        if ref_err:
            return self._deny(stage, AR_REF_UNMARKED, ref_err, pol=pol, ref=ref,
                              subject=subject, action=action, dry_run=dry_run)

        ok, code, reason, thresholds = self._qc_gate(stage, pol, qc)
        if not ok:
            return self._deny(stage, code, reason, pol=pol, ref=ref,
                              thresholds=thresholds, subject=subject, action=action,
                              dry_run=dry_run)

        ok, code, reason, cap = self._cap_gate(pol, stage)
        if not ok:
            return self._deny(stage, code, reason, pol=pol, ref=ref,
                              thresholds=thresholds, cap=cap, subject=subject,
                              action=action, dry_run=dry_run)

        return Decision(
            allowed=True, stage=stage, code="", reason="放行",
            policy_id=pol.policy_id, principal=pol.principal, authorization_ref=ref,
            thresholds=thresholds, cap=cap, dry_run=dry_run, action=action,
            subject=str(subject), policy=pol,
        )

    # -- 采用（adopt）-----------------------------------------------------

    def plan_adopt(self, *, media_version_id: str, subject_id: str,
                   project: str = "", episode: str = "",
                   qc: Optional[Mapping[str, Any]] = None) -> Decision:
        """预演采用。**零写入**（不碰事实库、不碰交付库）。"""
        return self._preflight("adopt", subject=media_version_id, project=project,
                               episode=episode, qc=qc, dry_run=True,
                               action="ProductionService.select（仅采用）")

    def auto_adopt(self, service: Any, *, media_version_id: str, subject_id: str,
                   subject_type: str = "shot",
                   reason: str = "auto-release：按策略自动采用",
                   project: str = "", episode: str = "",
                   qc: Optional[Mapping[str, Any]] = None,
                   dry_run: bool = False) -> Decision:
        """自动**采用**（创作决定）。

        ⚠ 本方法**只**调 ``ProductionService.select``。它没有任何一条代码路径会写
        approval —— 采用永不隐式升级为批准（ADR-0002 铁律 1）。
        ``adopt`` 与 ``approve`` 是两个方法、两个开关、两个决策对象。

        采用不计入每日上限（可逆、且历史版本全留），但仍受总开关 / 阶段开关 /
        白名单 / 留痕标记四道闸门约束。
        """
        action = "ProductionService.select（仅采用，不产生批准）"
        d = self._preflight("adopt", subject=media_version_id, project=project,
                            episode=episode, qc=qc, action=action, dry_run=dry_run)
        if not d.allowed or dry_run:
            return d
        # 用**闸门判定过的那份**策略，不再重读文件（否则两次读之间策略被改，
        # 跑的额度/阈值就不是闸门放行时看到的那一套）。
        pol = d.policy or self.policy
        try:
            # 只写 selection_decisions；approve 表不在本方法的可达范围内。
            out = service.select(subject_type=subject_type, subject_id=subject_id,
                                 media_version_id=media_version_id, reason=reason,
                                 decided_by=pol.principal, project=project,
                                 episode=episode)
        except Exception as exc:                  # noqa: BLE001 —— 如实上报，不降级
            logger.error("自动采用失败：media=%s（%s）", media_version_id, exc)
            return _failed(d, exc, "自动采用被真实闸门/仓储拒绝：%s" % exc)
        return _with_result(d, out)

    # -- 批准（approve）---------------------------------------------------

    def plan_approve(self, *, media_version_id: str, project: str = "",
                     episode: str = "",
                     qc: Optional[Mapping[str, Any]] = None) -> Decision:
        """预演批准。**零写入**。"""
        return self._preflight("approve", subject=media_version_id, project=project,
                               episode=episode, qc=qc, dry_run=True,
                               action="ProductionService.approve（走 require_human_authorization）")

    def auto_approve(self, service: Any, *, media_version_id: str,
                     reason: str = "auto-release：按策略自动批准",
                     scope: str = "media", note: str = "",
                     subject_type: str = "shot", subject_id: str = "",
                     project: str = "", episode: str = "",
                     qc: Optional[Mapping[str, Any]] = None,
                     dry_run: bool = False) -> Decision:
        """自动**批准**（放行决定）。

        走 ``ProductionService.approve`` 的**真实路径**，并把白名单原样传下去，
        于是 :func:`app.domain.production_facts.require_human_authorization`
        会在 ``build_approval`` 内部**真的执行一次**（用声明的 principal）。
        本模块不重写、不短路、不替代任何授权检查。

        真实闸门仍然会再拒一次（这是应该的）：没被采用过、
        产物内容与登记指纹不符、哈希三元组算不出 —— 任何一条都照样抛错。
        """
        action = "ProductionService.approve（走 require_human_authorization，非旁路）"
        d = self._preflight("approve", subject=media_version_id, project=project,
                            episode=episode, qc=qc, action=action, dry_run=dry_run)
        if not d.allowed or dry_run:
            return d
        pol = d.policy or self.policy          # 闸门判定过的那份，不再重读
        cap_code = self._consume_cap(pol, "approve", media_version_id, d.authorization_ref)
        if cap_code:
            return self._deny("approve", cap_code,
                              AR_ERROR_CODES.get(cap_code, cap_code), pol=pol,
                              ref=d.authorization_ref, thresholds=d.thresholds,
                              cap=d.cap, subject=media_version_id, action=action,
                              dry_run=dry_run)
        try:
            out = service.approve(media_version_id=media_version_id,
                                  authorized_by=pol.principal,       # 声明的机器主体
                                  authorization_ref=d.authorization_ref,
                                  reason=reason, scope=scope, note=note,
                                  subject_type=subject_type, subject_id=subject_id,
                                  approver_allowlist=list(self._allowlist))
        except Exception as exc:                  # noqa: BLE001
            logger.error("自动批准被拒：media=%s by=%s（%s）", media_version_id,
                         pol.principal, exc)
            self._refund_cap(pol, "approve")
            return _failed(d, exc, "自动批准被真实闸门/仓储拒绝：%s" % exc)
        return _with_result(d, out)

    # -- 交付放行（deliver）----------------------------------------------

    def plan_deliver(self, *, package_id: str, project: str = "", episode: str = "",
                     qc: Optional[Mapping[str, Any]] = None) -> Decision:
        """预演交付放行。**零写入**（只读交付包 + 纯函数校验）。"""
        return self._preflight("deliver", subject=package_id, project=project,
                               episode=episode, qc=qc, dry_run=True,
                               action="delivery_domain.can_approve → delivery_repo.approve_package")

    def auto_deliver(self, package_id: str, *, project: str = "",
                     episode: str = "",
                     qc: Optional[Mapping[str, Any]] = None,
                     dry_run: bool = False) -> Decision:
        """自动**交付放行**（把交付包从 ``verified`` 推到 ``approved``）。

        走的是与 ``POST /api/delivery/packages/<id>/approve`` **同一套**领域函数
        与仓储：``delivery_domain.can_approve`` → ``require_human_authorization``
        → ``delivery_repo.approve_package``。不旁路、不自建审批通道。

        延迟 import delivery 层：让本模块在只做 adopt/approve 预演时不牵出交付栈
        （也避免 import 期就落到真实数据目录上）。
        """
        action = "delivery_domain.can_approve → require_human_authorization → approve_package"
        d = self._preflight("deliver", subject=package_id, project=project,
                            episode=episode, qc=qc, action=action, dry_run=dry_run)
        if not d.allowed or dry_run:
            return d
        pol = d.policy or self.policy          # 闸门判定过的那份，不再重读
        # 必须用顶层模块名：app/ 没有 __init__.py，而 app/app.py 作为顶层模块
        # 占用了 sys.modules['app']，所以 `from app.api import …` /
        # `from app.domain import …` 在源码运行与 PyInstaller 下都必然失败
        # （"'app' is not a package"）。原先两个分支都写错，等于交付阶段从未
        # 执行过任何一次，却报 AR-ACTION-FAILED —— 方向虽是 fail-closed，但
        # 审计留痕把「import 路径写错」伪装成了「动作失败」。
        from domain import delivery as delivery_domain        # type: ignore  # noqa: PLC0415
        from infrastructure import delivery_repo              # type: ignore  # noqa: PLC0415

        try:
            pkg = delivery_repo.get_package(package_id)
        except Exception as exc:                                   # noqa: BLE001
            return _failed(d, exc, "读取交付包失败：%s" % exc)
        if not pkg:
            return self._deny("deliver", AR_ACTION_FAILED, "交付包不存在：%s" % package_id,
                              pol=pol, ref=d.authorization_ref, thresholds=d.thresholds,
                              cap=d.cap, subject=package_id, action=action, dry_run=dry_run)
        # 以下三道是 ADR-0006 的既有语义，一个都不许绕过。
        check = delivery_domain.can_approve(pkg)
        if not check.get("ok"):
            return self._deny("deliver", str(check.get("code") or AR_ACTION_FAILED),
                              "交付包不满足批准前置条件：%s" % check.get("message"),
                              pol=pol, ref=d.authorization_ref, thresholds=d.thresholds,
                              cap=d.cap, subject=package_id, action=action, dry_run=dry_run)
        # ⚠ 机器校验与授权门禁必须**已经**成立，才谈人工批准。
        # ``can_approve`` 只检查「建好了 / 非空」两件事（domain/delivery.py:487-500），
        # 仓储 ``approve_package`` 也不看这两个标志位，于是此前
        # ``verified_ok=False`` 的包能被写成 approved —— 「已批准」与
        # 「机器校验从未通过」同时成立，直接违反 ADR-0006 状态机
        # ``built → verified → approved``。这两道与 :func:`api.delivery` 人工路径
        # 的 ``release_ready`` 口径一致。
        if not pkg.get("verified_ok"):
            return self._deny("deliver", delivery_domain.DLV_NOT_RELEASE_READY,
                              "交付包的机器校验未通过（verified_ok=false）："
                              "机器校验没绿就没有 approved，自动放行不得越过这一步。",
                              pol=pol, ref=d.authorization_ref, thresholds=d.thresholds,
                              cap=d.cap, subject=package_id, action=action, dry_run=dry_run)
        if not pkg.get("licensing_ok"):
            return self._deny("deliver", delivery_domain.DLV_NOT_RELEASE_READY,
                              "交付包的授权门禁未通过（licensing_ok=false）："
                              "存在授权门禁拒绝项的包不得进入 approved。",
                              pol=pol, ref=d.authorization_ref, thresholds=d.thresholds,
                              cap=d.cap, subject=package_id, action=action, dry_run=dry_run)
        if not pkg.get("disk_ok"):
            return self._deny("deliver", "DLV-FILE-MISSING",
                              "交付文件在磁盘上已缺失，拒绝放行。", pol=pol,
                              ref=d.authorization_ref, thresholds=d.thresholds,
                              cap=d.cap, subject=package_id, action=action, dry_run=dry_run)
        if not pkg.get("disk_matches_baseline"):
            return self._deny("deliver", "DLV-HASH-MISMATCH",
                              "交付文件在建包之后被改动，必须重新登记交付包再放行"
                              "（批准过 A 交付了 B 是交付事故的经典形态）。", pol=pol,
                              ref=d.authorization_ref, thresholds=d.thresholds,
                              cap=d.cap, subject=package_id, action=action, dry_run=dry_run)

        # ⚠ 真实人工授权闸门，与 api/delivery.py 用同一个领域函数、同一份名单。
        # 用顶层模块名：app/ 不是包，`from app.domain.…` 必然抛 "not a package"
        #（原代码把它放在 try 的第一支，每次调用都要先失败一次才落到兜底支）。
        from domain.production_facts import (                   # type: ignore  # noqa: PLC0415
            ApprovalAuthorizationError, require_human_authorization)
        try:
            require_human_authorization(pol.principal, d.authorization_ref,
                                        allowlist=list(self._allowlist))
        except ApprovalAuthorizationError as exc:
            return self._deny("deliver", "DLV-APPROVER-NOT-HUMAN",
                              "真实人工授权闸门拒绝：%s" % exc, pol=pol,
                              ref=d.authorization_ref, thresholds=d.thresholds,
                              cap=d.cap, subject=package_id, action=action, dry_run=dry_run)

        cap_code = self._consume_cap(pol, "deliver", package_id, d.authorization_ref)
        if cap_code:
            return self._deny("deliver", cap_code,
                              AR_ERROR_CODES.get(cap_code, cap_code), pol=pol,
                              ref=d.authorization_ref, thresholds=d.thresholds,
                              cap=d.cap, subject=package_id, action=action, dry_run=dry_run)
        try:
            delivery_repo.approve_package(package_id, pkg["package_hash"],
                                          pol.principal,
                                          note=d.authorization_ref)
        except Exception as exc:                                   # noqa: BLE001
            logger.error("自动交付放行失败：pkg=%s（%s）", package_id, exc)
            self._refund_cap(pol, "deliver")
            return _failed(d, exc, "交付放行写入失败：%s" % exc)
        after = delivery_repo.get_package(package_id) or {}
        approval = delivery_repo.get_approval(package_id)
        return _with_result(d, {
            "package_id": package_id, "status": after.get("status"),
            "approval": delivery_domain.evaluate_approval(after, approval),
        })

    # -- 运维自查 ---------------------------------------------------------

    def explain(self, stage: str, *, subject: str = "dry-run",
                project: str = "", episode: str = "",
                qc: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """三阶段一次性预演报告（上线前先看这个）。零写入。"""
        out: Dict[str, Any] = {"environment": self.describe(), "stages": {}}
        for name in STAGES:
            d = self._preflight(name, subject=subject, project=project, episode=episode,
                                qc=qc, dry_run=True)
            out["stages"][name] = d.to_dict()
        return out


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def pol_principal(engine: AutoReleaseEngine) -> str:
    """取引擎声明的 principal（``decided_by`` 署名用）。"""
    return engine.policy.principal


def _with_result(d: Decision, out: Any) -> Decision:
    return Decision(allowed=True, stage=d.stage, code="", reason=d.reason,
                    policy_id=d.policy_id, principal=d.principal,
                    authorization_ref=d.authorization_ref, thresholds=d.thresholds,
                    cap=d.cap, dry_run=d.dry_run, action=d.action, subject=d.subject,
                    result=out if isinstance(out, dict) else {"raw": repr(out)},
                    policy=d.policy)


def _failed(d: Decision, exc: Exception, reason: str) -> Decision:
    """真实动作失败后的如实上报 —— **不降级成「成功」，也不重试成另一种行为**。"""
    return Decision(allowed=False, stage=d.stage, code=AR_ACTION_FAILED, reason=reason,
                    policy_id=d.policy_id, principal=d.principal,
                    authorization_ref=d.authorization_ref, thresholds=d.thresholds,
                    cap=d.cap, dry_run=d.dry_run, action=d.action, subject=d.subject,
                    result={"error_type": type(exc).__name__, "error": str(exc)},
                    policy=d.policy)


def describe_environment() -> Dict[str, Any]:
    """环境快照：策略路径、白名单、是否配了白名单。不 import 任何仓储。"""
    return {
        "policy_path": default_policy_path(),
        "allowlist": approver_allowlist(),
        "allowlist_configured": bool(approver_allowlist()),
        "ref_marker": AR_REF_MARKER,
        "stages": list(STAGES),
    }

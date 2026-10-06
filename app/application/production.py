# -*- coding: utf-8 -*-
"""编排层：版本化生产事实（生成 → 采用 → 批准）。

这一层是**唯一**允许把领域实体与仓储拼起来的地方。三条铁律在这里的落点：

* :func:`ProductionService.approve` —— 批准必须显式调用，且内部强制
  「采用存在 + 人工授权 + 哈希绑定」三重校验（铁律 1 + 铁律 3）。
  **本层没有任何一条路径能从"已采用"推出"已批准"**。
* :func:`ProductionService.derive_intent` —— 冻结 intent 的唯一变更入口（铁律 2）。
* :func:`ProductionService.decide` —— 界面读三态的入口，返回体里
  ``selection_implies_approval`` 恒为 ``False``，供前端做断言。

编排层不 import flask（路由在 ``app/api/production_facts.py``）。
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:
    from domain.production_facts import (
        ApprovalAuthorizationError, ApprovalDecision, CapabilityProfileVersion, ConflictError,
        DomainError, FrozenEntityError, GenerationIntent, IncompleteIntentError, MediaVersion,
        NotSelectedError, RefSlot, SelectionDecision, approval_is_valid, build_approval,
        build_capability_profile_version, build_intent, build_media_version, build_selection,
        decision_state, derive_intent, hash_payload, next_profile_version,
        require_human_authorization,
    )
    from infrastructure.facts_repo import FactsRepo
except ImportError:                    # pragma: no cover - 以脚本方式导入时的兜底
    from app.domain.production_facts import (  # type: ignore
        ApprovalAuthorizationError, ApprovalDecision, CapabilityProfileVersion, ConflictError,
        DomainError, FrozenEntityError, GenerationIntent, IncompleteIntentError, MediaVersion,
        NotSelectedError, RefSlot, SelectionDecision, approval_is_valid, build_approval,
        build_capability_profile_version, build_intent, build_media_version, build_selection,
        decision_state, derive_intent, hash_payload, next_profile_version,
        require_human_authorization,
    )
    from app.infrastructure.facts_repo import FactsRepo  # type: ignore

logger = logging.getLogger(__name__)

__all__ = ["ProductionService", "ProductionError", "new_id", "sha256_file"]


class ProductionError(DomainError):
    """编排层错误（路由层据此映射 4xx/409）。"""


def new_id(prefix: str = "") -> str:
    """短 id（与 ``task_store`` 的 ``uuid4().hex[:16]`` 同长度口径）。"""
    raw = uuid.uuid4().hex[:16]
    return "%s_%s" % (prefix, raw) if prefix else raw


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """流式 sha256；失败返回空串（调用方按「无指纹」处理，绝不假装算过）。"""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(chunk), b""):
                h.update(block)
        return h.hexdigest()
    except OSError as e:
        logger.warning("产物哈希计算失败（按无指纹处理）：%s", e)
        return ""


class ProductionService:
    """版本化生产事实的编排入口。

    仓储是**注入**的：本层不自己建连接，也不读全局单例，
    这样同一套编排逻辑既能跑在 Flask 请求里，也能跑在 CLI 脚本里。
    """

    def __init__(self, repo: Optional[FactsRepo] = None):
        self.repo = repo or FactsRepo()

    # ============================================================
    # GenerationIntent
    # ============================================================

    def create_intent(self, *, project: str, episode: str, shot_key: str,
                      prompt: str, kind: str = "image",
                      negative_prompt: str = "",
                      ref_slots: Optional[Sequence[Mapping[str, Any]]] = None,
                      seed: int = -1, profile_id: str = "", profile_version: str = "",
                      workflow_version: str = "", workflow_hash: str = "",
                      created_by: str = "", intent_id: str = "") -> Dict[str, Any]:
        """冻结并登记一条生成意图。

        若 ``ref_slots`` 里给的是本地路径且文件存在，会**自动补 sha256**
        （`:meth:`_enrich_ref_slots`）—— 参考图被换掉必须能被哈希发现，
        否则同一 intent_hash 会盖住两个不同的画面。
        """
        slots = self._enrich_ref_slots(ref_slots)
        intent = build_intent(intent_id or new_id("in"), project=project, episode=episode,
                              shot_key=shot_key, kind=kind, prompt=prompt,
                              negative_prompt=negative_prompt, ref_slots=slots,
                              seed=seed, profile_id=profile_id,
                              profile_version=profile_version,
                              workflow_version=workflow_version,
                              workflow_hash=workflow_hash, created_by=created_by)
        self.repo.save_intent(intent)
        logger.info("已冻结生成意图：%s（shot=%s hash=%s）",
                    intent.intent_id, intent.shot_key, intent.intent_hash[:12])
        return intent.to_dict()

    def derive_intent(self, intent_id: str, *, reason: str, created_by: str = "",
                      new_intent_id: str = "", **changes: Any) -> Dict[str, Any]:
        """**派生**新 intent（铁律 2 在编排层的唯一入口）。

        落库前再算一次新旧哈希对比：新旧内容哈希相同说明派生没生效，
        领域层已经拦过，这里兜底防止有人绕过领域层直接调仓储。
        """
        parent = self.repo.get_intent(intent_id)
        if parent is None:
            raise ProductionError("生成意图不存在：%s" % intent_id)
        child = derive_intent(parent, new_intent_id or new_id("in"), reason=reason,
                              created_by=created_by, **changes)
        if child.intent_hash == parent.intent_hash:
            raise FrozenEntityError("派生未改变内容，拒绝写入（parent=%s）" % intent_id)
        self.repo.save_intent(child)
        return {"intent": child.to_dict(), "parent": parent.to_dict()}

    def get_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        it = self.repo.get_intent(intent_id)
        return it.to_dict() if it else None

    def list_intents(self, **kw: Any) -> List[Dict[str, Any]]:
        return [i.to_dict() for i in self.repo.list_intents(**kw)]

    def intent_lineage(self, intent_id: str) -> List[Dict[str, Any]]:
        """派生链（当前 → 根），供「这一镜改过几次、每次改了什么」查询。"""
        return [i.to_dict() for i in self.repo.intent_lineage(intent_id)]

    def find_intents_by_hash(self, intent_hash: str) -> List[Dict[str, Any]]:
        """同参数的历史意图（识别重复执行 / 免重渲候选）。"""
        return [i.to_dict() for i in self.repo.find_intents_by_hash(intent_hash)]

    # ============================================================
    # MediaVersion
    # ============================================================

    def register_media(self, intent_id: str, *, path: str = "",
                       media_sha256: str = "", probe: Optional[Mapping[str, Any]] = None,
                       attempt_id: str = "", media_version_id: str = "",
                       bytes_: int = 0) -> Dict[str, Any]:
        """登记一次生成产出的候选版本。

        给 ``path`` 没给 sha256 时会自动算；算不出就**拒绝登记**
        （没有内容指纹的候选永远拿不到批准 —— 与其留一条不可批准的事实，
        不如让调用方看到明确失败）。
        """
        intent = self.repo.get_intent(intent_id)
        if intent is None:
            raise ProductionError("生成意图不存在：%s" % intent_id)
        digest = str(media_sha256 or "")
        size = int(bytes_ or 0)
        if not digest and path:
            digest = sha256_file(path)
            if digest and not size:
                try:
                    size = int(os.path.getsize(path))
                except OSError:
                    size = 0
        if not digest:
            raise IncompleteIntentError(
                "无法确定媒体指纹（给了 path=%s 也没有 sha256）：拒绝登记无指纹候选" % path)
        mv = build_media_version(media_version_id or new_id("mv"), intent=intent,
                                 media_sha256=digest, path=str(path or ""),
                                 bytes_=size, probe=dict(probe or {}),
                                 attempt_id=str(attempt_id or ""))
        self.repo.save_media_version(mv)
        return mv.to_dict()

    def list_media_versions(self, intent_id: str = "") -> List[Dict[str, Any]]:
        """候选列表，**每条都带上真实的采用 / 批准三态**。

        注意：``selected`` 与 ``approved`` 是两个独立字段，
        前端必须分别渲染（采用=青，批准=绿，见协调书 §4）。
        """
        out = []
        for mv in self.repo.list_media_versions(intent_id=intent_id):
            d = mv.to_dict()
            state = self.repo.decision_state(mv.media_version_id)
            d["decision"] = state
            d["selected"] = bool(state.get("selected"))
            d["approved"] = bool(state.get("approved"))
            out.append(d)
        return out

    # ============================================================
    # 采用 / 批准（铁律 1 的主战场）
    # ============================================================

    def select(self, *, subject_type: str, subject_id: str, media_version_id: str,
               reason: str, decided_by: str = "",
               project: str = "", episode: str = "", shot_key: str = "",
               selection_id: str = "") -> Dict[str, Any]:
        """记录**采用**（创作决定）。

        本方法**只**写 selection_decisions 表。它不会、也无权写批准表。
        返回体里显式带 ``selection_implies_approval: False``，
        让调用方（以及读代码的人）立刻看到这条边界。
        """
        mv = self.repo.get_media_version(media_version_id)
        if mv is None:
            raise ProductionError("候选版本不存在：%s" % media_version_id)
        intent = self.repo.get_intent(mv.intent_id)
        sel = build_selection(selection_id or new_id("sel"), subject_type=subject_type,
                              subject_id=subject_id, media_version_id=media_version_id,
                              intent_hash=intent.intent_hash if intent else "",
                              project=project, episode=episode, shot_key=shot_key,
                              reason=reason, decided_by=decided_by)
        self.repo.save_selection(sel)
        logger.info("已记录采用：subject=%s media=%s by=%s", subject_id, media_version_id,
                    decided_by or "(未署名)")
        return {"selection": sel.to_dict(), "selection_implies_approval": False}

    def approve(self, *, media_version_id: str, authorized_by: str,
                authorization_ref: str = "", reason: str = "",
                scope: str = "media", note: str = "", subject_type: str = "",
                subject_id: str = "", approval_id: str = "",
                approver_allowlist: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """记录**批准**（放行决定）—— 铁律 1 + 铁律 3 的执行点。

        四道强制校验，任何一道不过都**抛错且不写库**：

        1. ``media_version_id`` 必须存在；
        2. 必须有一条**未撤销的采用**指向它
           （:func:`build_approval` 内部抛 :class:`NotSelectedError`）；
        3. ``authorized_by`` 必须是人工主体
           （拒绝 ``system:*`` / ``bot*`` / ``auto*``，
           :func:`require_human_authorization`）；
        4. ``bound_hash`` 必须钉住 (media_sha256, intent_hash, selection_hash) 三元组。

        额外防一手：若磁盘上的产物已被覆盖（指纹对不上登记值），
        直接拒绝批准 —— 因为批的已经不是眼前这个文件了。

        ``approver_allowlist`` 必须与 :meth:`revoke_approval` 用**同一份**名单，
        否则「批准按词表、撤销按名单」就成了一条可绕过的窄门。
        """
        mv = self.repo.get_media_version(media_version_id)
        if mv is None:
            raise ProductionError("候选版本不存在：%s" % media_version_id)
        sel = self.repo.current_selection(mv.media_version_id) or \
            self.repo.current_selection(subject_id or mv.media_version_id)
        if sel is None:
            raise NotSelectedError(
                "该候选尚未被采用，不能批准（采用 ≠ 批准；请先 select 再 approve）：%s"
                % media_version_id)
        if not self.repo.assert_media_unchanged(mv.media_version_id):
            raise ApprovalAuthorizationError(
                "产物内容已与登记指纹不符（文件被覆盖），必须重新登记 MediaVersion 后再批准：%s"
                % media_version_id)
        ap = build_approval(approval_id or new_id("ap"), selection=sel, media=mv,
                            authorized_by=authorized_by,
                            authorization_ref=authorization_ref,
                            scope=scope, note=note,
                            allowlist=list(approver_allowlist or ()))
        self.repo.save_approval(ap)
        logger.info("已记录批准：media=%s by=%s（人工授权）", media_version_id, authorized_by)
        return {"approval": ap.to_dict(), "selection": sel.to_dict()}

    def revoke_approval(self, approval_id: str, *, revoked_by: str,
                        reason: str = "",
                        approver_allowlist: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """撤销批准（同样要求人工主体，名单口径与 :meth:`approve` 一致）。"""
        if not str(reason or "").strip():
            raise ProductionError("撤销批准必须写明 reason")
        require_human_authorization(revoked_by, "", allowlist=list(approver_allowlist or ()))
        return self.repo.revoke_approval(approval_id, revoked_by=revoked_by, reason=reason)

    def decide(self, subject_id: str) -> Dict[str, Any]:
        """「采用 / 批准」三态汇总（界面读这个）。

        返回体固定含 ``selection_implies_approval: False``。
        """
        return self.repo.decision_state(subject_id)

    def list_selections(self, subject_id: str = "") -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.repo.list_selections(subject_id)]

    def list_approvals(self, subject_id: str = "") -> List[Dict[str, Any]]:
        return [a.to_dict() for a in self.repo.list_approvals(subject_id)]

    def approval_state(self, approval_id: str,
                       *, media_sha256_now: str = "") -> Dict[str, Any]:
        """某条批准是否**仍然成立**（内容变过 → 失效，必须重新批准）。

        fail-closed 口径（与 :func:`domain.production_facts.decision_state` 同一套）：
        ``media_sha256_now`` 为空**不表示「跳过校验」，而表示「无法验证」**
        （调用方没去磁盘上重算）。此时一律不得返回 ``valid: True`` ——
        否则产物被覆盖后，不传 digest 的调用方依然读到「批准有效」。

        早先这里写的是 ``if media_sha256_now and ...``，空串即整条跳过磁盘比对，
        与已修好的 ``decision_state``（强制传 ``media_verified``）不对称，
        且与 P0-4 的 fail-closed 原则直接冲突。

        这是**只读查询**，所以拿不到现状时返回 ``unverified`` 状态而**不是抛异常** ——
        炸掉调用方不会让批准更安全，只会让调用方拿不到任何信息。
        """
        ap = self.repo.get_approval(approval_id)
        verified = bool(str(media_sha256_now or "").strip())
        valid = approval_is_valid(ap, media_sha256_now=media_sha256_now)
        return {
            "approval_id": approval_id,
            # 无法验证磁盘现状时，绝不因为「没去查」就报 valid
            "valid": bool(valid) and verified,
            # verified=False = 取不到磁盘现状，按「不可判定」处理，不是「没变」
            "state": ("valid" if (valid and verified)
                      else "unverified" if not verified else "invalid"),
            "verified": verified,
            "approval": ap.to_dict() if ap else None,
        }

    # ============================================================
    # CapabilityProfileVersion
    # ============================================================

    def publish_capability_profile(self, profile_id: str, *,
                                   params: Optional[Mapping[str, Any]] = None,
                                   deploy_profile: str = "", workflow_version: str = "",
                                   note: str = "", created_by: str = "",
                                   version: int = 0) -> Dict[str, Any]:
        """发布能力档案的一个新版本。

        版本号不给时按"当前最大 +1"建议；并发下若被别的分支占了，
        :class:`ConflictError` 会明确提示换号 —— **不静默覆盖**历史版本。
        """
        if not str(profile_id or "").strip():
            raise ProductionError("能力档案必须有 profile_id")
        existing = [p.to_dict() for p in self.repo.list_capability_profiles(profile_id)]
        ver = int(version or 0) or next_profile_version(existing, profile_id)
        prof = build_capability_profile_version(
            profile_id, ver, params=params, deploy_profile=deploy_profile,
            comfyui_workflow_version=workflow_version, note=note, created_by=created_by)
        self.repo.save_capability_profile(prof)
        return prof.to_dict()

    def get_capability_profile(self, profile_id: str, version: int = 0) -> Optional[Dict[str, Any]]:
        p = self.repo.get_capability_profile(profile_id, version)
        return p.to_dict() if p else None

    def list_capability_profiles(self, profile_id: str = "") -> List[Dict[str, Any]]:
        return [p.to_dict() for p in self.repo.list_capability_profiles(profile_id)]

    # ============================================================
    # 内部
    # ============================================================

    @staticmethod
    def _enrich_ref_slots(ref_slots: Optional[Sequence[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
        """给参考图槽位补内容指纹（本地文件存在时）。

        为什么必须补：``h3_common_refs`` 保证 ``<Picture N>`` 编号在全集恒定，
        而槽位里的图一旦被换掉，编号不变会让模型把 A 的锚点用在 B 身上 ——
        这类静默错只有靠哈希才发现。
        """
        out: List[Dict[str, Any]] = []
        for raw in (ref_slots or ()):
            d = dict(raw or {})
            ref = str(d.get("ref") or "")
            if not d.get("sha256") and ref and os.path.isfile(ref):
                d["sha256"] = sha256_file(ref)
            out.append(d)
        return out
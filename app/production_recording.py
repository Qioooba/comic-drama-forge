# -*- coding: utf-8 -*-
"""生成链 → 版本化生产事实的**适配层**（Generation → ProductionFacts）。

它存在的理由
------------
``app/application/production.py`` 的 :class:`ProductionService` 有
``create_intent`` / ``register_media``，但它**只**回答「怎么登记」，
不回答「这次渲染对应哪条意图」。生成路径（``app/comfyui_client.py``）
手里有全部证据，却没有一条调用把它接上 —— 于是 Shot Studio 永远看不到
真实候选，「采用 ≠ 批准」在真实项目上就是空转。

本模块就是那条接缝，并且**只**做这一件事：

    渲染发生 → 找出/建立同参数意图 → 把每个产物登记为 MediaVersion

三条铁律在本层的落点
--------------------
* **采用 ≠ 批准**：本模块**只**调用 ``create_intent`` 与 ``register_media``。
  全文件不出现 ``select`` / ``approve`` 字样 —— 登记是**记录**，不是放行。
  任何「登记完顺手帮用户采用」的写法都会让批准链失去人工闸门，故禁止。
* **登记失败绝不阻断生成**：本模块任何异常都在内部吞掉并落到失败台账
  （:func:`_record_failure`）。已核实：没有任何批准闸门依赖生产事实，
  为一次数据库瞬时故障丢掉一整轮 GPU 渲染是净损失。
* **内容指纹由本层自算**：不采信调用方给的 ``media_sha256``
  （``register_media`` 本身也是 fail-closed：算不出就拒绝登记）。

身份怎么来（决策 1：重试 / 换种子 / 崩溃免重渲 = 同一意图，新候选）
------------------------------------------------------------------
``intent_hash`` 由领域层按固定字段集算出，其中 ``seed`` 与 ``workflow_hash``
**都会随换种子而变**。若原样写入，重渲一次就是一个新意图 —— 与决策 1
直接矛盾。故本层做两处**口径分离**：

* ``seed`` → 意图层固定写 ``-1``（领域语义：种子不固定、由生成端随机）。
  本次真正用的种子作为**客观事实**落在 MediaVersion 的 ``probe.seed``。
  换种子因此不换意图，只多一个候选。
* ``workflow_hash`` → 改用 :func:`seed_neutral_workflow_hash`
  （把 api_prompt 里所有 ``seed`` / ``noise_seed`` 标量抹平后再算）。
  记录的是「工作流形状 + 其余全部参数」，不含这一次抽签结果。

于是「同参数重跑」= 同一 ``intent_hash`` = :func:`ProductionService.find_intents_by_hash`
命中并**复用**；提示词 / 风格 / 参考图 / 画幅 / 工作流模板任一变化 → 新意图。

纯度
----
本模块**不 import** ``comfyui_client``、不碰 GPU、不连 ComfyUI，
只有 :func:`record_generation` 在真正落库时才会构造 :class:`ProductionService`。
身份推导（:func:`intent_identity` / :func:`seed_neutral_workflow_hash`）
是纯函数，可无 GPU 单独验证。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "RenderContext",
    "record_generation",
    "generation_status",
    "recent_failures",
    "intent_identity",
    "seed_neutral_workflow_hash",
    "recording_health",
    "STATE_NEVER_GENERATED",
    "STATE_GENERATED_UNRECORDED",
    "STATE_RECORDED",
]

#: 领域语义「种子不固定」——见 ``domain.production_facts.GenerationIntent.seed``。
UNPINNED_SEED = -1

#: 整集级（episode scope）视频的 shot_key。
#:
#: 决策 2：整集 H3 **没有**分镜身份，绝不编一个 shot 出来。
#: 这里给空串 = 「本条意图不属于任何单镜」，Shot Studio 按 shot_key 过滤时
#: 自然不会把它混进某一镜的候选列表（那才是真正的「主体错位」）。
EPISODE_SCOPE_SHOT_KEY = ""

STATE_NEVER_GENERATED = "never_generated"
STATE_GENERATED_UNRECORDED = "generated_unrecorded"
STATE_RECORDED = "recorded"

#: 抹平随机种子的输入键（与 ``comfyui_client._inject_seed`` 同口径）。
_SEED_KEYS = ("seed", "noise_seed")

_LOCK = threading.Lock()
_SERVICE = None           # 进程内单例；构造 FactsRepo 会跑迁移，故延迟到首次使用
_SERVICE_FAILED = False


# =====================================================================
# 输入
# =====================================================================

@dataclass
class RenderContext:
    """一次渲染的登记上下文。

    只有 ``project`` / ``episode`` / ``prompt`` 是真正必需的（缺任何一个都
    无法回答「这是谁、哪一集、想做什么」）。其余都是可选增强。
    """

    kind: str = "image"                    # image / video / audio（领域白名单）
    project: str = ""
    episode: str = ""
    shot_key: str = ""
    prompt: str = ""
    negative_prompt: str = ""

    #: 本次实际使用的种子。**不**进意图哈希，只作为候选的客观事实。
    seed: Optional[int] = None
    #: 参考图本地路径列表（顺序即槽位序）。本地存在的会由服务层补 sha256。
    ref_paths: Sequence[str] = field(default_factory=tuple)
    ref_slots: Optional[Sequence[Dict[str, Any]]] = None
    #: 参考图**角色**（``domain.production_facts.REF_SLOT_ROLES`` 之一，如
    #: ``"start_frame"``）。只有调用方**真的知道**每张参考图是什么时才给 —— 本层不猜。
    #: 缺省空串且调用方也未给 ``ref_slots`` 时，自动槽位不带角色，领域层会拒绝这次登记
    #: （落失败台账）。见 :func:`_norm_ref_slots`。
    ref_slot_role: str = ""

    #: 工作流模板名（``分镜生成_Qwen21.json`` 之类）—— 输出确实由它决定。
    workflow_version: str = ""
    #: 原始 api_prompt。给了就算**去种子**工作流指纹；不给则用 workflow_hash 兜底。
    api_prompt: Optional[Dict[str, Any]] = None
    workflow_hash: str = ""

    #: 已落在磁盘上的产物绝对路径。
    artifacts: Sequence[str] = field(default_factory=tuple)
    #: 本次尝试的标识（重试会变；免重渲复用时保持稳定）。
    attempt_id: str = ""
    #: True = 这次是崩溃免重渲复用（GPU 未重花），产物仍是磁盘上真实文件。
    resumed: bool = False
    #: 登记来源标记，便于日后按链路排查（storyboard / h3_episode / …）。
    source: str = ""
    created_by: str = "generation"


# =====================================================================
# 身份推导（纯函数）
# =====================================================================

def seed_neutral_workflow_hash(api_prompt: Optional[Dict[str, Any]]) -> str:
    """**去种子**的工作流指纹（32 位十六进制）。

    为什么必须去种子：``comfyui_job_store.workflow_hash`` 哈希整个 api_prompt，
    而种子是写进采样节点输入的（``_inject_seed``）。直接用会让「换种子重渲」
    换一个意图 —— 与决策 1（同一创作意图下多个候选）矛盾。

    口径与 ``comfyui_job_store.workflow_hash`` 一致（``sort_keys=True`` +
    紧凑分隔符），只是先把标量种子抹成固定占位。
    """
    if not isinstance(api_prompt, dict) or not api_prompt:
        return ""
    try:
        stripped = json.loads(json.dumps(api_prompt, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":"), default=str))
    except (TypeError, ValueError) as e:
        logger.warning("去种子工作流指纹：api_prompt 序列化失败（按无指纹处理）：%s", e)
        return ""
    for _node in stripped.values():
        if not isinstance(_node, dict):
            continue
        inputs = _node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key in _SEED_KEYS:
            # 只抹标量种子；连线（list）不是种子，不动。
            if key in inputs and not isinstance(inputs.get(key), list):
                inputs[key] = "<seed>"
    try:
        blob = json.dumps(stripped, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError) as e:
        logger.warning("去种子工作流指纹：序列化失败（按无指纹处理）：%s", e)
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def intent_identity(ctx: RenderContext) -> Dict[str, Any]:
    """从**决定输出的参数**推导稳定意图身份（纯函数，无 IO、无时间戳、无计数器）。

    口径与领域层 ``GenerationIntent.hash_payload`` 对齐，唯一的刻意差异：

    * ``seed`` 恒为 :data:`UNPINNED_SEED`（换种子不换意图，决策 1）；
    * ``workflow_hash`` 优先用 :func:`seed_neutral_workflow_hash`。

    这里**没有**时间戳、没有自增序号 —— 否则重跑永远找不到旧意图。
    """
    if ctx.negative_prompt:
        negative = str(ctx.negative_prompt)
    else:
        negative = _negative_from_api_prompt(ctx.api_prompt)
    return {
        "project": str(ctx.project or ""),
        "episode": str(ctx.episode or ""),
        "shot_key": str(ctx.shot_key or ""),
        "kind": str(ctx.kind or "image"),
        "prompt": str(ctx.prompt or ""),
        "negative_prompt": negative,
        "ref_slots": _norm_ref_slots(ctx),
        "seed": UNPINNED_SEED,
        "workflow_version": str(ctx.workflow_version or ""),
        "workflow_hash": seed_neutral_workflow_hash(ctx.api_prompt)
                         or str(ctx.workflow_hash or ""),
    }


def _norm_ref_slots(ctx: RenderContext) -> List[Dict[str, Any]]:
    """参考图槽位：调用方给了显式槽位就用，否则按 ``ref_paths`` 顺序自动编号。

    ⚠️ 2026-10-07 修正（真实缺陷，探针实测）：自动槽位此前写成
    ``{"slot", "ref", "sha256"}``，而领域层 ``RefSlot.from_dict`` 读的是
    ``index`` / ``role`` —— 于是 role 恒为 ``""``、index 恒为 0，
    **任何带参考图的渲染都在身份计算处被领域层拒绝**
    （``IncompleteIntentError: 未知参考图槽位角色：''``）：一次都登记不上，
    只在失败台账留痕。分镜 / 尾帧 / 带参考图的 H3 全体中招。

    角色（role）本层**不猜**：调用方要么直接给 ``ref_slots``，要么给出
    ``ref_slot_role``（它真的知道每张参考图是什么）。两者都没给时，槽位照旧
    缺角色 → 领域层拒绝、失败台账留痕（**与修正前行为逐字一致**）：
    宁可不登记，也不编一个角色 —— 参考图角色错一次，「同一意图」的判定就跟着错，
    而生产事实下游是人工批准闸门。
    """
    if ctx.ref_slots:
        return [dict(s) for s in ctx.ref_slots]
    role = str(getattr(ctx, "ref_slot_role", "") or "").strip()
    out: List[Dict[str, Any]] = []
    for idx, raw in enumerate(ctx.ref_paths or (), 1):
        ref = str(raw or "")
        if not ref:
            continue
        # index 从 0 起（与 H3 公共参考图 0..K-1 的槽位口径一致），且必须唯一。
        slot: Dict[str, Any] = {"index": idx - 1, "role": role, "ref": ref}
        try:
            if ref and os.path.isfile(ref):
                slot["sha256"] = _sha256_file(ref)
        except OSError as e:
            logger.warning("参考图指纹计算失败（按无指纹处理）：%s（%s）", ref, e)
        out.append(slot)
    return out


def _negative_from_api_prompt(api_prompt: Optional[Dict[str, Any]]) -> str:
    """从 api_prompt 里把负向提示词抽出来（参与意图身份）。

    取第一个非空负向文本节点。抽不到就给空串 —— 负向词为空与负向词为
    某串是两个不同的创作决定，不该共用一个意图。
    """
    if not isinstance(api_prompt, dict):
        return ""
    for node in api_prompt.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key in ("negative_prompt", "negative"):
            val = inputs.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """流式 sha256。**失败返回空串**（绝不假装算过 —— 空串会被下游 fail-closed 拒绝）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(chunk), b""):
                h.update(block)
        return h.hexdigest()
    except OSError as e:
        logger.warning("产物哈希计算失败（按无指纹处理）：%s（%s）", path, e)
        return ""


def _identity_fingerprint(identity: Dict[str, Any]) -> str:
    """意图身份的可读指纹（给日志/台账看，不是领域层的 intent_hash）。"""
    try:
        blob = json.dumps(identity, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# =====================================================================
# 失败台账（独立小库，不碰 facts.db 的 schema 所有权）
# =====================================================================

def _failures_db_path() -> str:
    """失败台账路径：``<PROJECT_OUTPUT_DIR>/production_recording/failures.db``。

    刻意独立于 ``facts.db``：那张库的表与迁移归
    ``infrastructure.facts_repo`` 所有，本模块不写它的 schema。
    失败台账只是「本模块没登记成功」的证据，不是生产事实本身。
    """
    try:
        from config import PROJECT_OUTPUT_DIR
    except ImportError:                       # pragma: no cover - 独立导入兜底
        PROJECT_OUTPUT_DIR = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
    return os.path.join(PROJECT_OUTPUT_DIR, "production_recording", "failures.db")


def _record_failure(ctx: RenderContext, stage: str, reason: str,
                    artifact: str = "") -> None:
    """把一次登记失败落成可查询的一行。

    决策 3 的落地：登记失败不阻断生成，但**必须可被发现**（不读日志也能查）。
    这里任何异常都被吞掉 —— 写台账失败不能反过来变成阻断。
    """
    try:
        path = _failures_db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 身份只算一次：它要读参考图指纹，逐张 sha256 并不便宜，
        # 而失败台账是**每次登记失败都要写**的热路径。
        identity = intent_identity(ctx)
        with _LOCK, sqlite3.connect(path, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS recording_failures ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL NOT NULL, stage TEXT NOT NULL, kind TEXT NOT NULL,"
                " project TEXT, episode TEXT, shot_key TEXT, source TEXT,"
                " attempt_id TEXT, artifact TEXT, intent_fingerprint TEXT,"
                " identity_json TEXT, reason TEXT)")
            conn.execute(
                "INSERT INTO recording_failures"
                " (ts, stage, kind, project, episode, shot_key, source, attempt_id,"
                "  artifact, intent_fingerprint, identity_json, reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), str(stage or ""), str(ctx.kind or ""),
                 str(ctx.project or ""), str(ctx.episode or ""),
                 str(ctx.shot_key or ""), str(ctx.source or ""),
                 str(ctx.attempt_id or ""), str(artifact or ""),
                 _identity_fingerprint(identity),
                 json.dumps(identity, ensure_ascii=False, default=str),
                 str(reason or "")[:2000]))
            conn.commit()
    except Exception as e:                      # noqa: BLE001 台账失败也不阻断
        logger.debug("登记失败台账写入失败（忽略）：%s", e)


def recent_failures(limit: int = 50, *, project: str = "",
                    episode: str = "", shot_key: str = "") -> List[Dict[str, Any]]:
    """读回最近的登记失败（供诊断端点 / 排障）。只读，不 import app。"""
    try:
        path = _failures_db_path()
        if not os.path.isfile(path):
            return []
        sql = ("SELECT ts, stage, kind, project, episode, shot_key, source, attempt_id,"
               " artifact, intent_fingerprint, reason FROM recording_failures WHERE 1=1")
        args: List[Any] = []
        for col, val in (("project", project), ("episode", episode),
                         ("shot_key", shot_key)):
            if val:
                sql += " AND %s=?" % col
                args.append(str(val))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, int(limit or 50)))
        with sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                             uri=True, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:                      # noqa: BLE001 只读查询不该炸调用方
        logger.debug("读取登记失败台账失败：%s", e)
        return []


def recording_health() -> Dict[str, Any]:
    """登记链路自检：失败台账在不在、累计失败多少条。"""
    path = _failures_db_path()
    exists = os.path.isfile(path)
    return {"failures_db": path, "failures_db_exists": exists,
            "recent_failure_count": len(recent_failures(limit=200)) if exists else 0}


# =====================================================================
# 服务获取（延迟构造，失败即永久降级）
# =====================================================================

def _service():
    """取进程内 :class:`ProductionService` 单例。

    构造它会触发 ``FactsRepo._ensure_schema``（建表 + 迁移），所以**绝不在模块
    import 期做** —— 否则仅仅 ``import production_recording`` 就会写磁盘。
    构造失败则永久降级为 None（``_SERVICE_FAILED``），后续调用直接跳过登记，
    不再反复重试拖慢生成。
    """
    global _SERVICE, _SERVICE_FAILED
    if _SERVICE is not None:
        return _SERVICE
    if _SERVICE_FAILED:
        return None
    with _LOCK:
        if _SERVICE is not None:
            return _SERVICE
        if _SERVICE_FAILED:
            return None
        try:
            try:
                from application.production import ProductionService
            except ImportError:                # pragma: no cover - 脚本导入兜底
                from app.application.production import ProductionService  # type: ignore
            _SERVICE = ProductionService()
            return _SERVICE
        except Exception as e:                  # noqa: BLE001 库不可用 → 永久降级
            _SERVICE_FAILED = True
            logger.warning("版本化生产事实不可用，本次起**跳过登记**（不影响生成）：%s", e)
            return None


# =====================================================================
# 主入口
# =====================================================================

def record_generation(ctx: RenderContext) -> Dict[str, Any]:
    """登记一次渲染。**永不抛异常**（决策 3）。

    返回体（不抛错，调用方按 ``ok`` 分支即可）::

        {"ok": bool, "intent_id": str, "reused_intent": bool,
         "media_version_ids": [...], "skipped": [...], "reason": str}

    流程：校验必填 → 同 ``intent_hash`` 复用或新建意图 → 每个产物自算指纹后
    登记为 MediaVersion。**任何一步失败都只记账 + 告警，绝不向上抛。**
    """
    result: Dict[str, Any] = {"ok": False, "intent_id": "", "reused_intent": False,
                              "media_version_ids": [], "skipped": [], "reason": ""}

    try:
        return _record_generation(ctx, result)
    except Exception as e:                      # noqa: BLE001 兜底：绝不炸生成链
        msg = "%s: %s" % (type(e).__name__, e)
        logger.warning("[生产事实] 登记异常（已忽略，不影响生成）：%s", msg)
        result["reason"] = msg
        _record_failure(ctx, "unexpected", msg)
        return result


def _record_generation(ctx: RenderContext, result: Dict[str, Any]) -> Dict[str, Any]:
    # ---- 1) 必填校验 -------------------------------------------------
    # 缺 project/episode/prompt 时**不猜**：编一个 project 比不登记更糟
    # （界面会显示一条来路不明的意图，比空列表更难排查）。
    missing = [k for k in ("project", "episode", "prompt")
               if not str(getattr(ctx, k, "") or "").strip()]
    if missing:
        msg = "缺少登记必填字段：%s（不猜测身份，跳过登记）" % ", ".join(missing)
        logger.info("[生产事实] %s（source=%s）", msg, ctx.source or "?")
        result["reason"] = msg
        result["ok"] = True          # 显式跳过也算「按预期走完」
        result["skipped"] = ["missing_identity:%s" % ",".join(missing)]
        return result

    svc = _service()
    if svc is None:
        result["ok"] = True
        result["reason"] = "production_facts_unavailable"
        result["skipped"] = ["facts_db_unavailable"]
        return result

    identity = intent_identity(ctx)

    # ---- 2) 同参数复用意图，否则新建 ---------------------------------
    # 这里必须先算 intent_hash 再查（领域层没有「按参数查」的入口，
    # 只有按已冻结的 hash 查）。用 build_intent 造一个**内存态**对象算 hash，
    # 不落库；命中才复用，未命中才真正 create。
    try:
        from domain.production_facts import build_intent, hash_payload  # type: ignore
    except ImportError:                        # pragma: no cover
        from app.domain.production_facts import build_intent, hash_payload  # type: ignore
    try:
        probe = build_intent("_probe", **identity)
        intent_hash = hash_payload(probe.hash_payload())
    except Exception as e:                     # noqa: BLE001
        msg = "意图身份计算失败：%s: %s" % (type(e).__name__, e)
        logger.warning("[生产事实] %s（source=%s）", msg, ctx.source or "?")
        result["reason"] = msg
        _record_failure(ctx, "identity", msg)
        return result

    intent_id = ""
    reused = False
    try:
        existing = svc.find_intents_by_hash(intent_hash)
    except Exception as e:                     # noqa: BLE001
        existing = []
        logger.warning("[生产事实] 意图哈希查询失败（按新建处理）：%s", e)
    if existing:
        intent_id = str(existing[0].get("intent_id") or "")
        reused = True
    if not intent_id:
        try:
            created = svc.create_intent(**identity, created_by=ctx.created_by or "generation")
            intent_id = str(created.get("intent_id") or "")
        except Exception as e:                 # noqa: BLE001
            msg = "意图创建失败：%s: %s" % (type(e).__name__, e)
            logger.warning("[生产事实] %s（source=%s hash=%s）",
                           msg, ctx.source or "?", intent_hash[:12])
            result["reason"] = msg
            _record_failure(ctx, "create_intent", msg)
            return result
    if not intent_id:
        msg = "意图创建后仍无 intent_id"
        result["reason"] = msg
        _record_failure(ctx, "create_intent", msg)
        return result

    result["intent_id"] = intent_id
    result["reused_intent"] = reused

    # ---- 3) 每个产物登记为 MediaVersion ------------------------------
    # 内容指纹**由本层自算**（sha256_file），不采信任何调用方上报值。
    probe_obj: Dict[str, Any] = {"source": ctx.source, "resumed": bool(ctx.resumed)}
    if ctx.workflow_version:
        probe_obj["workflow_version"] = ctx.workflow_version
    if ctx.api_prompt:
        probe_obj["workflow_hash"] = seed_neutral_workflow_hash(ctx.api_prompt)
    if ctx.seed is not None:
        probe_obj["seed"] = int(ctx.seed)

    # 同一意图下**内容完全相同**的产物只登记一次。
    # 为什么：崩溃免重渲（resumed=True）与整集重试会反复走同一条路径，
    # 产物文件逐字节相同。没有这道去重，候选列表会被同一个文件刷屏 ——
    # Shot Studio 的「候选对比」就退化成同一个图出现 N 次。
    # 真正的重渲染（换种子）内容必然不同，不受此影响。
    try:
        seen_digests = {str(mv.get("media_sha256") or "")
                        for mv in svc.list_media_versions(intent_id)}
    except Exception as e:                     # noqa: BLE001
        seen_digests = set()
        logger.debug("[生产事实] 既有候选指纹读取失败（本次不去重）：%s", e)

    for art in (ctx.artifacts or ()):
        path = str(art or "")
        if not path:
            continue
        if not os.path.isfile(path):
            # 磁盘上没有 → 不登记。领域层本身也会因算不出指纹而拒绝，
            # 这里提前拦一层，省掉一次无谓的失败告警。
            result["skipped"].append("missing_file:%s" % path)
            _record_failure(ctx, "artifact_missing",
                            "产物不在磁盘上，无法确定内容指纹：%s" % path, artifact=path)
            logger.warning("[生产事实] 产物不存在，跳过登记：%s（source=%s）",
                           path, ctx.source or "?")
            continue
        try:
            digest = _sha256_file(path)
        except Exception as e:                 # noqa: BLE001
            digest = ""
            logger.warning("[生产事实] 产物指纹计算异常：%s（%s）", path, e)
        if not digest:
            # fail-closed：算不出指纹就没有可批准的事实。
            result["skipped"].append("no_digest:%s" % path)
            _record_failure(ctx, "no_digest",
                            "无法确定产物内容指纹，拒绝登记无指纹候选：%s" % path,
                            artifact=path)
            logger.warning("[生产事实] 产物指纹不可判定，拒绝登记：%s", path)
            continue
        if digest in seen_digests:
            # 内容重复（免重渲复用 / 整集重试同产物）→ 不新增候选。
            result["skipped"].append("duplicate_content:%s" % path)
            continue
        try:
            mv = svc.register_media(intent_id, path=path, media_sha256=digest,
                                    probe=probe_obj,
                                    attempt_id=ctx.attempt_id or "")
            mv_id = str(mv.get("media_version_id") or "")
            if mv_id:
                result["media_version_ids"].append(mv_id)
                seen_digests.add(digest)
        except Exception as e:                 # noqa: BLE001
            msg = "候选登记失败：%s: %s" % (type(e).__name__, e)
            result["skipped"].append("register_failed:%s" % path)
            _record_failure(ctx, "register_media", "%s（产物=%s）" % (msg, path),
                            artifact=path)
            logger.warning("[生产事实] %s（source=%s）", msg, ctx.source or "?")

    result["ok"] = True
    if result["media_version_ids"]:
        logger.info("[生产事实] 已登记 %d 个候选（意图=%s 复用=%s kind=%s shot=%s）",
                    len(result["media_version_ids"]), intent_id, reused,
                    ctx.kind, ctx.shot_key or "(整集)")
    elif result["skipped"]:
        logger.warning("[生产事实] 未登记任何候选（意图=%s 跳过=%d）",
                       intent_id, len(result["skipped"]))
    return result


# =====================================================================
# 读侧（决策 4：区分「从未生成」与「生成了但没登记」）
# =====================================================================

def generation_status(*, project: str = "", episode: str = "",
                      shot_key: str = "", kind: str = "",
                      known_paths: Sequence[str] = ()) -> Dict[str, Any]:
    """回答「这一镜到底有没有东西可看」（决策 4）。

    返回 ``state`` 三态，Shot Studio 不再只有一个含糊的空状态：

    * ``recorded`` —— 有意图且有 MediaVersion（正常候选可比对）；
    * ``generated_unrecorded`` —— 磁盘上有产物、或台账里有登记失败，
      但**没有** MediaVersion（说明确实渲出来了，只是没进事实库）；
    * ``never_generated`` —— 什么都没有（真的还没渲过）。

    ``known_paths`` 由调用方给出（它才知道分镜/成片的落盘位置）；
    传空则只依据意图、候选与失败台账判断。
    """
    out: Dict[str, Any] = {
        "project": str(project or ""), "episode": str(episode or ""),
        "shot_key": str(shot_key or ""), "kind": str(kind or ""),
        "state": STATE_NEVER_GENERATED,
        "intent_count": 0, "media_version_count": 0,
        "intents": [], "candidate_paths": [], "unregistered_paths": [],
        "failures": [],
        "selection_implies_approval": False,
    }
    try:
        svc = _service()
        if svc is None:
            out["state"] = STATE_NEVER_GENERATED
            out["reason"] = "production_facts_unavailable"
            return out
        intents = svc.list_intents(project=project, episode=episode,
                                   shot_key=shot_key, limit=50)
        if kind:
            intents = [i for i in intents if str(i.get("kind") or "") == str(kind)]
        out["intents"] = [{"intent_id": i.get("intent_id"),
                           "intent_hash": i.get("intent_hash"),
                           "kind": i.get("kind"),
                           "episode": i.get("episode"),
                           "shot_key": i.get("shot_key"),
                           "created_at": i.get("created_at"),
                           "media_version_count": 0} for i in intents]
        out["intent_count"] = len(intents)
        for meta, it in zip(out["intents"], intents):
            try:
                mvs = svc.list_media_versions(str(it.get("intent_id") or ""))
            except Exception:                  # noqa: BLE001
                mvs = []
            meta["media_version_count"] = len(mvs)
            for mv in mvs:
                if mv.get("path"):
                    out["candidate_paths"].append(str(mv["path"]))
        out["media_version_count"] = len(out["candidate_paths"])

        # 磁盘现状：产物在、但不在候选列表里 = 生成了没登记。
        known = [str(p) for p in (known_paths or ()) if p]
        registered = {os.path.normcase(os.path.normpath(p))
                      for p in out["candidate_paths"]}
        for p in known:
            if os.path.normcase(os.path.normpath(p)) in registered:
                continue
            if os.path.isfile(p):
                # 磁盘上有、事实库里没有 → 这就是「生成了但没登记」。
                out["unregistered_paths"].append(p)

        out["failures"] = recent_failures(limit=20, project=project,
                                          episode=episode, shot_key=shot_key)

        if out["media_version_count"] > 0:
            out["state"] = STATE_RECORDED
        elif out["unregistered_paths"] or out["failures"]:
            out["state"] = STATE_GENERATED_UNRECORDED
        else:
            out["state"] = STATE_NEVER_GENERATED
        return out
    except Exception as e:                      # noqa: BLE001 只读查询不炸界面
        logger.warning("[生产事实] 状态查询失败：%s", e)
        out["reason"] = "%s: %s" % (type(e).__name__, e)
        return out

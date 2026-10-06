# -*- coding: utf-8 -*-
"""音频 AI 语义复核层（频谱图 + 波形图 → 多模态模型）

## 位置：客观层**之后**的第二道复核

本模块只做一件事：把 ``audio_qc.render_visuals`` 渲染出的**频谱图 + 波形图**交给
**既有的多模态 HTTP 客户端**（``qc_client.run_custom_vision`` → ``requests``），
再把模型对「声音形状」的判读结论**收紧**到客观层结论之上。

它**不重复实现**客观层的任何判据 —— 时长 / 平均电平 / 峰值 / 有声占比的硬闸全部留在
``audio_qc.probe_metrics`` + ``audio_qc.evaluate``（含「解析不出平均电平即判失败」这条
fail-closed 铁律）。本层只回答一个客观数字回答不了的问题：**这条音轨听起来像不像一段
真的、有内容的人声**。

## 两条铁律（改动本模块前必读）

1. **单向收紧**：AI 层只能让结论**更严格**，永远不能放宽。客观层判死 → 最终一定判死，
   AI 说「非常好」也翻不回来。代码上体现为三条且仅三条收敛运算（见 ``merge()``）：
   ``blocked`` 只做 **OR**、``passed`` 只做 **AND**、``score`` 取**最小值**。
   ⚠️ 禁止写成「取平均」「取 AI 的」「用 AI 覆盖」——那会把客观层的硬缺陷摊薄掉。

2. **fail-open 边界**：客观层的失败判定是 **fail-closed**（解析不出电平即判失败）；
   但**本层自己**的失败（模型不可达 / 超时 / 返回不可解析 / 未配置接口）是 **fail-open**
   —— 语义复核是增强项，它挂了不该阻断生产。代价是「未完成」有可能被误读成「通过」，
   所以每次都**显式标注** ``ai_review_state`` / ``ai_review_incomplete`` /
   ``pass_basis`` 三个字段，并在 ``reason`` 里带明文标记（见 ``merge()`` 标注小节）。

## 与 GPU 的关系

本模块**不加载任何本地模型、不 import torch、不做任何本地推理**：AI 语义复核全部走
既有 HTTP 多模态路径；频谱/波形图由 ffmpeg 的 **CPU** 滤镜渲染（客观层已在用）。
音频的「眼睛」（模型）仍然是外部服务，GPU 归渲染流水线，与本层无关。

## 依赖方向（⚠️ 改 import 前先看这里）

``qc_client`` 在顶层 ``import audio_qc``。因此本模块**可以**同时 import 两者，
但 ``audio_qc`` **绝不能**在顶层 import 本模块或 ``qc_client``（会成环）。
打破这个环的唯一入口是 ``audio_qc.check_with_ai`` 里的**函数内延迟 import**。

## 整轨口径

沿用既有铁律：整轨（成片 / 整集合成音轨）**自动关闭**「有声占比」判定 ——
成片天然有大段无台词留白，拿单句标准去卡它必然误报「漏句」。
除此影响客观层阈值外，本层还会把整轨口径**告知模型**（提示词显式声明留白是预期的），
否则模型会把正常的镜头留白报成「静音过多」。
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

# 客观层：硬阈值持有方 + 频谱/波形渲染（复用，不复制）
import audio_qc
# 多模态 HTTP 客户端（requests 路径；不碰 GPU）
import qc_client

logger = logging.getLogger(__name__)

__all__ = [
    "REVIEW_STATE_DONE", "REVIEW_STATE_SKIPPED", "REVIEW_STATE_FAILED",
    "AI_INCOMPLETE_NOTE", "is_whole_track", "ready", "check",
    "review", "merge",
]

# --------------------------------------------------------------------------- #
# 复核状态（三态，取代「有没有 AI 结论」这个二值）
# --------------------------------------------------------------------------- #

#: 模型真的返回了可用结论
REVIEW_STATE_DONE = "done"
#: 本层**按设计**没跑（with_ai=false / 接口未配置 / 客观层已判死，不值得再花一次调用）
REVIEW_STATE_SKIPPED = "skipped"
#: 本层**尝试了但没成**（渲染失败 / 模型不可达 / 超时 / 返回无法解析）
REVIEW_STATE_FAILED = "failed"

#: 未完成时出现在 ``reason`` 里的明文标记 —— 不得让「未完成」看起来像「通过」
AI_INCOMPLETE_NOTE = "AI 语义复核未完成"

#: 整轨口径的判定依据（与 ``api/qc.py`` 现有规则保持一致）
WHOLE_TRACK_SOURCES = ("mix", "merged")
WHOLE_TRACK_EXTS = (".mp4", ".mkv", ".mov")


def is_whole_track(path: str, source: str = "") -> bool:
    """是否整轨（成片 / 整集合成音轨）。

    判据是「整轨口径」而不是「调用方有没有传 source」—— 前端直接拖一个 mp4 过来检查
    （source 会是 path）时同样必须走整轨口径，否则一进来就是满屏「静音过多」。
    """
    src = str(source or "").strip().lower()
    if src in WHOLE_TRACK_SOURCES:
        return True
    return str(path or "").lower().endswith(WHOLE_TRACK_EXTS)


def ready(cfg: dict = None, override: dict = None) -> bool:
    """AI 语义复核是否可执行（质检总开关 + 音频质检开关 + 多模态接口三者齐备）。

    客观层不要求接口（``qc_client.audio_qc_ready``），本层要求 —— 没有模型就没有复核。
    """
    try:
        return bool(qc_client.audio_ai_ready(cfg or {}, override))
    except Exception:  # noqa: BLE001 - 门控判断不允许抛
        return False


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #

def _objective_facts(metrics: dict, expect_sec: Optional[float]) -> List[str]:
    """把客观层实测值拼成给模型的事实段。

    ⚠️ 这些数字比模型「看图」的观感可信，必须显式告诉模型，否则模型会拿观感去推翻
    实测值（本层不允许反向放宽，所以任何基于误判的翻案都必须在提示词层面就被堵住）。
    """
    m = metrics or {}
    facts = [f"实测时长 {float(m.get('duration') or 0):.2f} 秒"]
    if expect_sec:
        facts.append(f"期望时长 {float(expect_sec):.2f} 秒")
    if isinstance(m.get("speech_ratio"), float):
        facts.append(f"有声占比 {float(m['speech_ratio']) * 100:.1f}%")
    if isinstance(m.get("mean_db"), float):
        facts.append(f"平均电平 {float(m['mean_db']):.1f} dB")
    if isinstance(m.get("max_db"), float):
        facts.append(f"峰值电平 {float(m['max_db']):.1f} dB")
    return facts


def build_prompt(objective: dict, cfg: dict = None, line_text: str = "",
                 expect_sec: Optional[float] = None,
                 whole_track: bool = False) -> str:
    """构造语义复核提示词（复用既有 ``audio_prompt``，含整轨口径声明）。"""
    cfg = cfg or {}
    prompt = str(cfg.get("audio_prompt") or qc_client.DEFAULT_AUDIO_PROMPT)
    prompt = prompt.replace("{line_text}", str(line_text or "（未提供）")[:200])
    prompt = prompt.replace("{pass_score}", str(cfg.get("pass_score", 70)))
    facts = _objective_facts((objective or {}).get("metrics") or {}, expect_sec)
    head = ("以下是该段配音的 ffmpeg 客观指标（可信的实测值，请结合图片一并判断）："
            + "，".join(facts) + "。\n")
    if whole_track:
        # 整轨口径：把「留白是预期行为」写进提示词，否则模型会把无台词镜头判成「静音过多」
        head += ("⚠️ 口径声明：这是一条**整轨**（成片 / 整集合成音轨），其中存在大量"
                 "**刻意留白**（无台词镜头）。留白本身**不算缺陷**，请勿据此报「漏句」"
                 "或「静音过多」；只判断真正异常的部分（爆音/削波/断续/非人声/损坏）。\n")
    return head + prompt


# --------------------------------------------------------------------------- #
# 语义复核（只跑 AI 层，不碰客观层判定）
# --------------------------------------------------------------------------- #

def _skipped(note: str) -> dict:
    return {"ok": False, "state": REVIEW_STATE_SKIPPED, "note": str(note),
            "verdict": None, "visuals": []}


def _failed(note: str, visuals: Optional[list] = None) -> dict:
    return {"ok": False, "state": REVIEW_STATE_FAILED, "note": str(note),
            "verdict": None, "visuals": list(visuals or [])}


def review(audio_path: str, objective: dict = None, cfg: dict = None,
           override: dict = None, visuals_dir: str = None,
           line_text: str = "", expect_sec: Optional[float] = None,
           source: str = "") -> dict:
    """跑 AI 语义复核，返回**本层自己的**结论（永不抛异常）。

    返回 ``{"ok", "state", "note", "verdict", "visuals"}``：

    * ``ok``    —— 模型真的返回了可用结论（**唯一** ``True`` 的来源）
    * ``state`` —— ``done`` / ``skipped`` / ``failed``
    * ``note``  —— 未完成原因（人类可读），``done`` 时为空
    * ``verdict`` —— 模型结论（与 ``qc_client.parse_verdict`` 同构），未完成时为 None

    本函数**不做**与客观层的合并 —— 合并只发生在 ``merge()``，且只有一条路径。
    """
    cfg = cfg or {}

    # ---- 门控 ①：客观层已判死就没必要再花一次模型调用 ----
    # 单向收紧的推论：客观层判死 AI 翻不回来，所以这里跳过**不损失任何结论**。
    if (objective or {}).get("blocked"):
        return _skipped("客观层已判定致命缺陷（整段无声/无音轨/空文件），跳过 AI 语义复核")

    # ---- 门控 ②：接口未配置 ----
    if not ready(cfg, override):
        return _skipped("质检接口未配置（base_url/api_key/model），仅客观层结论生效")

    if not audio_path or not os.path.isfile(audio_path):
        return _skipped(f"音频文件不存在：{audio_path}")

    # ---- 渲染频谱图 + 波形图（CPU，复用客观层渲染器）----
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    if not visuals_dir:
        visuals_dir = os.path.join(os.path.dirname(os.path.abspath(audio_path)),
                                   "_qc_audio", stem)
    vis = audio_qc.render_visuals(audio_path, visuals_dir, prefix=stem)
    if not vis.get("ok"):
        return _failed(f"频谱/波形图渲染失败：{vis.get('error')}")

    prompt = build_prompt(objective or {}, cfg=cfg, line_text=line_text,
                          expect_sec=expect_sec,
                          whole_track=is_whole_track(audio_path, source))

    # ---- 走既有多模态 HTTP 路径（无本地模型 / 无 GPU）----
    try:
        resp = qc_client.run_custom_vision(
            prompt, list(vis.get("images") or []), cfg=cfg or None,
            override=override, max_tokens=int(cfg.get("image_max_tokens", 8192)))
    except Exception as e:  # noqa: BLE001 - 复核失败不得影响主流程
        logger.warning("音频 AI 语义复核调用失败：%s", e)
        return _failed(f"AI 语义复核调用失败：{e}", vis.get("images"))

    if not resp.get("ok"):
        # run_custom_vision 已吞掉网络异常并返回 {"ok": False, "error": ...}
        return _failed(f"AI 语义复核未完成：{resp.get('error') or '模型调用失败'}",
                       vis.get("images"))

    try:
        verdict = qc_client.parse_verdict(resp.get("content") or "",
                                          int(cfg.get("pass_score", 70)))
    except Exception as e:  # noqa: BLE001 - 畸形输出按「未完成」处理，不按「不通过」处理
        logger.warning("音频 AI 语义复核结论无法解析：%s", e)
        return _failed("AI 语义复核结论无法解析为 JSON（按未完成处理，不计入缺陷）",
                       vis.get("images"))

    verdict = dict(verdict)
    verdict.update({"ok": True, "skipped": False,
                    "latency_ms": resp.get("latency_ms"),
                    "api_total_ms": resp.get("api_total_ms"),
                    "model": resp.get("model")})
    return {"ok": True, "state": REVIEW_STATE_DONE, "note": "",
            "verdict": verdict, "visuals": list(vis.get("images") or [])}


# --------------------------------------------------------------------------- #
# 合并（单向收紧的唯一落点）
# --------------------------------------------------------------------------- #

def merge(objective: dict, review_result: dict) -> dict:
    """把客观层结论与语义复核结论合成一份 verdict（单向收紧的唯一落点）。

    收紧方向（只有这三条，且都只能朝「更严」走）：

    ========  ==========================================================
    字段       收敛方式（⚠️ 不是平均、不是覆盖）
    ========  ==========================================================
    blocked    ``客观层 blocked`` **OR** ``AI 命中音频关键缺陷词``
    passed     ``客观层 passed`` **AND** ``AI passed``（AI 未完成则不动它）
    score      两层**取最小值**（避免「客观 20 + AI 90 → 平均 55」摊薄硬缺陷）
    ========  ==========================================================

    未完成标注（fail-open 的必要代价，防止「未完成」被误读成「通过」）：

    * ``ai_review_state``       ``done`` / ``skipped`` / ``failed``
    * ``ai_review_incomplete``  ``state != "done"``（机器可读闸）
    * ``pass_basis``            ``objective_only`` / ``objective+ai``
    * ``reason``                未完成时追加明文「AI 语义复核未完成：<原因>」

    ⚠️ 未完成时 ``passed`` 保持**客观层的原值**（fail-open：不让外部接口抖动把成片
    判成不通过），代价是它只代表客观层结论 —— 严格的下游必须读 ``pass_basis`` /
    ``ai_review_state``，不能只看 ``passed``。
    """
    obj = dict(objective or {})
    rv = review_result or {}

    state = str(rv.get("state") or REVIEW_STATE_SKIPPED)
    ai = dict(rv.get("verdict") or {})
    note = str(rv.get("note") or "")

    # ---- 只有「模型真的返回了可用结论」才算 AI 有效 ----
    ai_used = bool(rv.get("ok")) and state == REVIEW_STATE_DONE and bool(ai.get("ok"))

    ai_issues = [str(x) for x in (ai.get("issues") or [])]
    ai_passed = ai.get("passed") if ai_used else None

    # pass 字段识别不出来 = **未知**，不是「不通过」。既不翻红也不放行，按未完成标注，
    # issues 仍原样保留在 ai 子字典里供人排查。
    if ai_used and ai_passed is None:
        ai_used = False
        state = REVIEW_STATE_FAILED
        note = "模型返回的 pass 字段无法识别（未知），按复核未完成处理"
        logger.warning("音频 AI 语义复核：pass 字段无法识别，不计入判定")

    # AI 关键缺陷词复用既有词表与否定语境逻辑（``find_critical_issues`` 内部已处理
    # 「无明显畸变」这类否定，**不要**在本模块重写一套关键词匹配）
    ai_fatal = (qc_client.find_critical_issues(ai_issues, qc_client.AUDIO_CRITICAL_KEYWORDS)
                if ai_used else [])

    obj_blocked = bool(obj.get("blocked"))
    obj_passed = bool(obj.get("passed"))
    obj_fatal = [str(x) for x in (obj.get("critical_issues") or [])]
    obj_issues = [str(x) for x in (obj.get("issues") or [])]

    # ---- 单向收紧 ①：blocked 只做 OR（客观层判死 → 一定判死）----
    blocked = obj_blocked or bool(ai_fatal)

    # ---- 单向收紧 ②：passed 只做 AND（AI 只能说不好，不能说好）----
    passed = (obj_passed and bool(ai_passed)) if ai_used else obj_passed

    # ---- 单向收紧 ③：score 取最小值 ----
    ai_score = ai.get("score") if isinstance(ai.get("score"), int) else None
    scores = [s for s in (obj.get("score"), ai_score) if isinstance(s, int)]
    score = min(scores) if scores else obj.get("score")

    # issues：客观层硬缺陷排最前，然后 AI 硬缺陷，再是各自软项（去重）
    issues: List[str] = []
    for it in list(obj_fatal) + list(ai_fatal) + obj_issues + ai_issues:
        s = str(it)
        if s and s not in issues:
            issues.append(s)

    fatal_all = list(obj_fatal)
    for it in ai_fatal:
        if it not in fatal_all:
            fatal_all.append(it)

    incomplete = state != REVIEW_STATE_DONE
    if blocked:
        reason = "关键缺陷：" + "；".join(fatal_all[:2])
    elif not passed:
        reason = "存在可优化项：" + "；".join(issues[:2])
    elif incomplete:
        reason = str(obj.get("reason") or "客观指标正常")
    else:
        reason = str(ai.get("reason") or obj.get("reason") or "音频达标")
    # fail-open 的代价必须可见：未完成不能读起来像「通过」
    if incomplete:
        reason = f"{reason}（{AI_INCOMPLETE_NOTE}：{note or state}）"

    out = dict(obj)
    out.update({
        "ok": True,
        "skipped": False,
        "objective_only": not ai_used,
        "ai_used": ai_used,
        "ai_review_state": state,
        "ai_review_incomplete": incomplete,
        "ai_review_note": note,
        "pass_basis": "objective+ai" if ai_used else "objective_only",
        "passed": passed,
        "accepted": not blocked,
        "blocked": blocked,
        "issues": [str(x)[:300] for x in issues][:8],
        "critical_issues": [str(x)[:200] for x in fatal_all][:6],
        "reason": str(reason)[:500],
        "objective": {"passed": obj_passed, "blocked": obj_blocked,
                      "score": obj.get("score"),
                      "issues": obj_issues, "critical_issues": obj_fatal},
        "ai": (dict(ai) if ai_used or ai else None),
    })
    if isinstance(score, int):
        out["score"] = score
    if rv.get("visuals"):
        out["visuals"] = list(rv.get("visuals") or [])
    # 音频没有「画面风格」维度，AI 层的 style_match 在这里没有意义（与既有合并一致）
    out.pop("style_match", None)
    return out


# --------------------------------------------------------------------------- #
# 一条龙：客观层 → AI 语义复核 → 单向收紧合并
# --------------------------------------------------------------------------- #

def check(audio_path: str, expect_sec: Optional[float] = None, line_text: str = "",
          cfg: dict = None, override: dict = None, visuals_dir: str = None,
          source: str = "", check_speech_ratio: Optional[bool] = None,
          min_speech_ratio: float = 0.50, min_mean_db: float = -45.0,
          max_drift: float = 0.50, with_ai: bool = True) -> dict:
    """客观层 + AI 语义复核一条龙（永不抛异常）。

    ``with_ai=False`` —— 只跑客观层，返回结构与 ``audio_qc.quick_check`` 同构
    （额外带 ``ai_review_state="skipped"`` 标注），**既有接口行为不变**。

    ``check_speech_ratio=None``（默认）—— 整轨口径自动关闭「有声占比」判定；
    非整轨才默认开启。整轨即成片 / 整集合成音轨，见 ``is_whole_track``。
    """
    if not audio_path or not os.path.isfile(audio_path):
        return {"ok": False, "skipped": False, "blocked": True, "passed": False,
                "score": 0, "issues": [], "critical_issues": [f"音频文件不存在：{audio_path}"],
                "reason": f"音频文件不存在：{audio_path}", "metrics": {},
                "objective_only": True, "ai_used": False,
                "ai_review_state": REVIEW_STATE_SKIPPED,
                "ai_review_incomplete": True,
                "ai_review_note": "音频文件不存在，未进入复核",
                "pass_basis": "objective_only"}

    # ---- ① 客观层（零模型依赖，始终执行；判定逻辑一字未动）----
    if check_speech_ratio is None:
        check_speech_ratio = not is_whole_track(audio_path, source)
    objective = audio_qc.quick_check(
        audio_path, expect_sec=expect_sec,
        min_speech_ratio=(min_speech_ratio if check_speech_ratio else None),
        min_mean_db=min_mean_db, max_drift=max_drift)
    objective = dict(objective)

    # ---- ② AI 语义复核（可关闭；本层失败不阻断）----
    if not with_ai:
        rv = _skipped("请求显式要求只做客观层（with_ai=false）")
    else:
        rv = review(audio_path, objective=objective, cfg=cfg, override=override,
                    visuals_dir=visuals_dir, line_text=line_text,
                    expect_sec=expect_sec, source=source)

    # ---- ③ 单向收紧合并（唯一收敛点）----
    return merge(objective, rv)

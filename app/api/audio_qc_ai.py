# -*- coding: utf-8 -*-
"""audio_qc_ai 域蓝图：音频 AI 语义复核层的独立入口。

2026-10-07 新增（对应评估文档 §★2.3「把 AI 层补到客观层之后，作为可选的语义复核」）。

为什么是独立端点而不是改 ``/api/qc/audio``
------------------------------------------------
既有 ``app/api/qc.py`` 归 W1 所有，本轮不得改动；而 AI 语义复核层需要一个**能被直接
触发**的入口才能验证与联调。本蓝图按 ``app/api/__init__.py`` 的自动发现机制注册
（只需暴露模块级 ``bp``），**无需改 ``app/app.py``**，也不触碰任何既有 URL（R1）。

判定语义全部在 ``app/audio_qc_ai.py``：客观层在前、AI 语义复核在后、两层**单向收紧**，
本文件只做「定位待检文件 → 调一条龙 → 序列化」。
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

# 共享 runtime 上下文（只读，见协调书 §2：其它 agent 不得修改 _shared）。
from api._shared import (
    PROJECT_DATA_DIR, PROJECT_OUTPUT_DIR, QC_DIR,
    _audio_qc_visuals_key, _body, _qc_load_cfg, _resolve_audio_qc_target,
    _safe_project, qc_client,
)

import audio_qc_ai

bp = Blueprint("audio_qc_ai", __name__)
DOMAIN = "audio_qc_ai"


@bp.route('/api/qc/audio/ai-review', methods=['POST'])
def api_audio_qc_ai_review():
    """音频 AI 语义复核：客观层（ffmpeg 指标）+ 频谱/波形图 → 多模态语义判定。

    body::

        {project_name?: "...",
         path?: "output/dub/<项目>/lines/xxx.wav",   # 显式指定文件（必须位于 output/ 内）
         source?: "mix" | "merged" | "line",        # 未给 path 时按此推导（默认 mix > merged）
         expect_sec?: 3.2,
         line_text?: "三年了，我回来了。",
         check_speech_ratio?: true,                 # 省略时按整轨口径自动判定
         with_ai?: true}                            # false 时只跑客观层

    与 ``/api/qc/audio`` 的关系：**并行**的两个入口，都走
    ``audio_qc_ai.check`` → 客观层在前、AI 复核在后、两层单向收紧。

    ``passed`` 语义与本端点特别说明：AI 复核未完成（模型不可达 / 超时 / 未配置）时
    本层是 **fail-open**，「不通过」不会因此被扣上；此时 ``passed`` 只代表客观层结论，
    必须配合 ``ai_review_incomplete`` / ``pass_basis`` 一起读 ——
    响应里 ``ai_review_state`` 不为 ``done`` 即表示**未完成，不等于通过**。
    """
    data = _body()
    project = _safe_project(data.get('project_name') or '')
    cfg = _qc_load_cfg()
    if not qc_client.audio_qc_ready(cfg):
        return jsonify({"success": False,
                        "error": "音频质检未启用（请检查质检总开关与音频质检开关）",
                        "audio_qc_active": False}), 400

    # ---- 定位待检文件：显式 path 优先，否则按 source 推导 ----
    # 包含性校验与 /api/qc/audio 同口径：只允许检查 output/ 内的产物
    raw_path = str(data.get('path') or '').strip()
    source = str(data.get('source') or '').strip().lower()
    target, why = '', ''
    if raw_path:
        cand = os.path.abspath(os.path.join(PROJECT_DATA_DIR, raw_path)) \
            if not os.path.isabs(raw_path) else os.path.abspath(raw_path)
        root = os.path.abspath(PROJECT_OUTPUT_DIR)
        if not cand.startswith(root + os.sep):
            return jsonify({"success": False,
                            "error": f"只允许检查 output/ 目录内的文件：{raw_path}"}), 400
        if not os.path.isfile(cand):
            return jsonify({"success": False, "error": f"文件不存在：{cand}"}), 404
        target, source = cand, (source or 'path')
    elif project:
        target, source, why = _resolve_audio_qc_target(project, source)
        if not target:
            return jsonify({"success": False, "error": why or "未找到可质检的音频产物",
                            "project_name": project}), 404
    else:
        return jsonify({"success": False, "error": "需要 project_name 或 path"}), 400

    expect = data.get('expect_sec')
    try:
        expect = float(expect) if expect not in (None, '') else None
    except (TypeError, ValueError):
        expect = None

    # 整轨口径：省略时由 AI 层按 source + 扩展名自动判定（成片音轨关闭有声占比判定）
    check_ratio = data.get('check_speech_ratio')
    check_ratio = check_ratio if isinstance(check_ratio, bool) else None
    # 回显**实际生效**的口径（而不是回显入参），否则整轨自动关闭时前端会显示成「开启」
    effective_ratio = (check_ratio if check_ratio is not None
                      else not audio_qc_ai.is_whole_track(target, source))

    stem = os.path.splitext(os.path.basename(target))[0]
    bucket = 'audio_mix' if (source == 'mix' or target.lower().endswith('.mp4')) else 'audio'
    visuals_dir = os.path.join(
        QC_DIR, bucket,
        _audio_qc_visuals_key(data.get('project_name'), target), stem)

    verdict = audio_qc_ai.check(
        target, expect_sec=expect, line_text=str(data.get('line_text') or ''),
        cfg=cfg, visuals_dir=visuals_dir, source=source,
        check_speech_ratio=check_ratio, with_ai=bool(data.get('with_ai', True)))

    vis_urls = []
    for p in (verdict.get("visuals") or []):
        try:
            rel = os.path.relpath(p, QC_DIR).replace(os.sep, '/')
        except ValueError:
            continue
        if not rel.startswith('..'):
            vis_urls.append(f"/api/qc/frames/{rel}")

    return jsonify({
        "success": True,
        "project_name": project, "source": source, "path": os.path.abspath(target),
        "expect_sec": expect,
        "check_speech_ratio": effective_ratio,
        "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
        "score": verdict.get("score"), "reason": verdict.get("reason"),
        "issues": verdict.get("issues") or [],
        "critical_issues": verdict.get("critical_issues") or [],
        "metrics": verdict.get("metrics") or {},
        # ---- 未完成 ≠ 通过：四个字段必须一起读 ----
        "ai_used": bool(verdict.get("ai_used")),
        "ai_review_state": verdict.get("ai_review_state"),
        "ai_review_incomplete": bool(verdict.get("ai_review_incomplete")),
        "ai_review_note": verdict.get("ai_review_note") or "",
        "pass_basis": verdict.get("pass_basis"),
        "objective_only": bool(verdict.get("objective_only")),
        "visuals": vis_urls,
        "verdict": verdict,
        "audio_qc_active": True,
        "audio_ai_active": qc_client.audio_ai_ready(cfg),
    })

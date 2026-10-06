# -*- coding: utf-8 -*-
"""script 域蓝图：剧本生成、覆盖率、剧本一致性、提示词分析。

2026-10-07 由 ``app/app.py`` 按域拆分而来，**纯搬迁**：路径、方法、报错文案逐字
不变（守门脚本 ``scripts/snapshot_routes.py`` 比对 rule + methods）。
``url_prefix`` 留空，路径直接写死在各 ``@bp.route`` 上。
"""
from __future__ import annotations

from flask import Blueprint

# 共享 runtime 上下文（app 实例 / 212 个模块级 helper / config 常量）。
# __all__ 里显式列出了下划线开头的名字，故这里能整体取到。
from api._shared import *  # noqa: F401,F403

bp = Blueprint("script", __name__)
DOMAIN = "script"

# ===== 步骤1：剧本生成 =====

@bp.route('/api/script/generate', methods=['POST'])
def api_generate_script():
    # P0-5 门禁：文本分析模型未配置 / 端点不可达 → 直接阻断，不给「静默跑进执行中」的机会
    _gate = _ai_gate_or_400("script")
    if _gate is not None:
        return _gate
    data = _body()
    theme = data.get('theme', '')
    episodes = data.get('episodes', 1)
    duration = data.get('duration', 60)
    style = data.get('style', '古风仙侠')
    style = _apply_project_settings(style, _safe_project(data.get('project_name') or (theme or '')[:20] or 'project'))

    if not theme:
        return jsonify({"error": "请提供主题"}), 400

    try:
        # 统一走前端「AI 设置 → 文本分析模型」的密钥（OpenAI 兼容，任意厂商），
        # 不再读环境变量 ANTHROPIC_API_KEY / 硬编码 CLAUDE_MODEL。
        client = _current_llm_client()
        script = script_gen.generate_script_with_client(
            client, theme=theme, episodes=episodes,
            duration_per_episode=duration, style=style
        )
        project_name = _safe_project(theme[:20])
        script_path = script_gen.save_script(script, project_name,
                                             provider="openai-compatible")
        script["metadata"]["script_path"] = script_path

        # S1：剧本质检接线（原为死代码——生产路径只调无质检的 generate_script，
        # 结构缺陷直接流入分镜/视频后才暴露）。这里在剧本落盘后跑一次 check_script，
        # 按 script_qc_ready 门控（与 image/video qc 同模式，开关关时 no-op、不报错），
        # 结果透出给前端如实回显「剧本是否已质检」。单次检测（非 generate_script_with_qc
        # 的 3 次重试+AI 判断重链路），止血优先、行为保守。
        qc_cfg = _qc_load_cfg()
        qc_result = {"skipped": True, "reason": "剧本质检未启用"}
        if qc_client.script_qc_ready(qc_cfg):
            qc_result = qc_client.check_script(
                script_data=script, style=style, target_duration=duration, cfg=qc_cfg)
            app.logger.info("剧本质检：passed=%s score=%s reason=%s",
                            qc_result.get("passed"), qc_result.get("score"),
                            qc_result.get("reason"))

        return jsonify({
            "success": True,
            "script_path": script_path,
            "script": script,
            "project_name": project_name,
            "characters_count": len(script.get("characters", [])),
            "items_count": len(script.get("items", [])),
            "scenes_count": len(script.get("scenes", [])),
            "shots_count": len(script.get("shots", [])),
            "script_qc_active": qc_client.script_qc_ready(qc_cfg),
            "qc_result": qc_result,
        })
    except LLMError as e:
        # 未配置「文本分析模型」或密钥错误：给引导（400）而非裸 500，
        # 引导用户去 AI 设置配置 base_url / api_key / model。
        # _ai_guide_response 已返回 (jsonify, code) 元组，直接透传。
        app.logger.error(f"剧本生成 LLM 调用失败: {e}")
        return _ai_guide_response(str(e))
    except Exception as e:
        app.logger.error(f"生成剧本失败: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route('/api/script/fallback', methods=['POST'])
def api_fallback_script():
    """一键加载本地兜底剧本（免 API Key 演示）"""
    try:
        script = script_gen.load_fallback_script()
        if not script:
            return jsonify({"error": "未找到本地兜底剧本（output/scripts/剑心初醒_兼容版.json）"}), 404
        data = request.json or {}
        project_name = _safe_project(data.get('project_name') or script.get("title", "fallback"))
        script_path = script_gen.save_script(script, project_name)
        script.setdefault("metadata", {})["script_path"] = script_path
        return jsonify({
            "success": True,
            "script_path": script_path,
            "script": script,
            "project_name": project_name,
            "characters_count": len(script.get("characters", [])),
            "items_count": len(script.get("items", [])),
            "scenes_count": len(script.get("scenes", [])),
            "shots_count": len(script.get("shots", [])),
            "fallback": True
        })
    except Exception as e:
        app.logger.error(f"加载兜底剧本失败: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route('/api/coverage/<novel_id>', methods=['GET'])
def api_coverage_overview(novel_id):
    """项目级原文覆盖率总览（④⑤）：逐集覆盖率百分比 / 阈值 / 是否达标 / 遗漏数 / 补生成镜数"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
    rows = []
    for e in eps:
        ep = int(e.get("episode_no") or 0)
        rep = coverage.load_coverage_report(CONTINUITY_DIR, key, ep)
        if rep:
            row = coverage.summary_for_meta(rep)
            row.update({"episode_no": ep, "available": True,
                        "chapter_title": e.get("chapter_title"),
                        "script_path": e.get("path")})
        else:
            row = {"episode_no": ep, "available": False,
                   "coverage_percent": None, "passed": None,
                   "chapter_title": e.get("chapter_title"),
                   "script_path": e.get("path"),
                   "note": "该集尚无覆盖率报告（未按新流程重跑）"}
        rows.append(row)
    return jsonify({
        "success": True, "novel_id": novel_id,
        "project_id": (proj or {}).get("id", ""),
        "novel_title": meta.get("title") or meta.get("name"),
        "project_key": key,
        "threshold_percent": round(float(getattr(novel_to_script, "COVERAGE_THRESHOLD", 0.95)) * 100, 2),
        "coverage_dir": os.path.abspath(os.path.join(CONTINUITY_DIR, key, "episodes")),
        "count": len(rows),
        "episodes": rows,
    })


@bp.route('/api/coverage/<novel_id>/<int:episode_no>', methods=['GET'])
def api_coverage_episode(novel_id, episode_no):
    """单集原文覆盖率详情（⑤）：覆盖率 / 阈值 / 遗漏清单 / 补生成记录 / 逐轮复检轨迹"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = continuity.episode_coverage_view(CONTINUITY_DIR, key, episode_no)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", "")})
    return jsonify(data)


@bp.route('/api/script-consistency/<novel_id>', methods=['GET'])
def api_script_consistency_overview(novel_id):
    """P0-3 剧本↔原著一致性总览：逐集三件套结论 / 泄漏数 / 要素覆盖率 / 是否通过 / 报告路径
    （注意与 /api/consistency/*「资产多视图一致性」区分：本组专指剧本 ↔ 本章原著）"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
    rows = []
    for e in eps:
        ep = int(e.get("episode_no") or 0)
        rep = script_consistency.load_consistency_report(CONTINUITY_DIR, key, ep)
        if rep:
            row = script_consistency.summary_for_meta(
                rep, script_consistency.consistency_report_path(CONTINUITY_DIR, key, ep))
            row.update({"episode_no": ep, "available": True,
                        "chapter_title": e.get("chapter_title"),
                        "script_path": e.get("path")})
        else:
            row = {"episode_no": ep, "available": False, "passed": None,
                   "chapter_title": e.get("chapter_title"),
                   "script_path": e.get("path"),
                   "note": "该集尚无一致性校验报告（未按新流程重跑）"}
        rows.append(row)
    return jsonify({
        "success": True, "novel_id": novel_id,
        "project_id": (proj or {}).get("id", ""),
        "novel_title": meta.get("title") or meta.get("name"),
        "project_key": key,
        "algorithm": script_consistency.SCRIPT_CONSISTENCY_VERSION,
        "consistency_dir": os.path.abspath(os.path.join(CONTINUITY_DIR, key, "episodes")),
        "count": len(rows),
        "episodes": rows,
    })


@bp.route('/api/script-consistency/<novel_id>/<int:episode_no>', methods=['GET'])
def api_script_consistency_episode(novel_id, episode_no):
    """单集 P0-3 一致性详情：章节锚定 / 元信息泄漏 / 要素覆盖 / 问题清单 / 定向修复轨迹"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = script_consistency.episode_consistency_view(CONTINUITY_DIR, key, episode_no)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", "")})
    return jsonify(data)


@bp.route('/api/scripts/analyze-prompts', methods=['POST'])
def api_analyze_prompts():
    """为剧本生成/优化 prompt_h3 与参考图提示词（后台任务）

    body: {script, script_path, mode: shots|assets|all, shot_ids: [...], project_name, extra_instruction}
    单镜头重写：mode=shots 且 shot_ids=[该镜头号]
    """
    data = request.json or {}
    script = data.get('script')
    if not isinstance(script, dict) or not script.get('shots'):
        return jsonify({"success": False, "error": "缺少剧本数据（或剧本中没有镜头）"}), 400

    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法生成提示词")

    mode = data.get('mode') or 'all'
    if mode not in ('shots', 'assets', 'all'):
        return jsonify({"success": False, "error": "mode 必须是 shots / assets / all"}), 400
    shot_ids = data.get('shot_ids') or None
    if isinstance(shot_ids, (str, int)):
        shot_ids = [shot_ids]
    project_name = _safe_project(data.get('project_name')
                                 or (script.get('title') or 'project'))
    script_path = data.get('script_path') or script.get('metadata', {}).get('script_path') or ''
    # P0-4：这条链路会把分析结果**写回** script_path（_ensure_script_file → save_script_inplace
    # 覆盖式 json.dump），是「剧本路径越界」的写盘面 —— 与 /api/final/video、_dub_resolve_script、
    # bind_script 同一校验口径。越界一律 400 拒写（空值仍允许：worker 会在 SCRIPT_DIR 内另存）。
    if script_path and not project_store.is_path_inside_output(script_path):
        return jsonify({"success": False,
                        "error": "剧本路径必须在项目输出目录内（output/），越界路径已拒写"}), 400
    extra = str(data.get('extra_instruction') or '')[:500]

    task_id = f"prompts_{project_name}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "prepare",
            "message": "正在准备提示词生成…", "mode": mode,
            "project_name": project_name,
            "total_shots": len(script.get('shots') or []),
        }
    threading.Thread(target=_analyze_worker,
                     args=(task_id, script, script_path, mode, shot_ids, project_name, extra),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "mode": mode, "project_name": project_name,
                    "script_path": script_path, "shot_ids": shot_ids})

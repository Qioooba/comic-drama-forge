# -*- coding: utf-8 -*-
"""qc 域蓝图：质检（配置、抽帧、音频、集级四层质量）。

2026-10-07 由 ``app/app.py`` 按域拆分而来，**纯搬迁**：路径、方法、报错文案逐字
不变（守门脚本 ``scripts/snapshot_routes.py`` 比对 rule + methods）。
``url_prefix`` 留空，路径直接写死在各 ``@bp.route`` 上。
"""
from __future__ import annotations

from flask import Blueprint

# 共享 runtime 上下文（app 实例 / 212 个模块级 helper / config 常量）。
# __all__ 里显式列出了下划线开头的名字，故这里能整体取到。
from api._shared import *  # noqa: F401,F403

bp = Blueprint("qc", __name__)
DOMAIN = "qc"

@bp.route('/api/qc/config', methods=['GET'])
def api_qc_config_get():
    cfg = _qc_load_cfg()
    view = qc_client.public_view(cfg)
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "history_dir": os.path.abspath(QC_DIR),
                    "ai_settings_path": os.path.abspath(AI_CONFIG_PATH)})


@bp.route('/api/qc/config', methods=['POST'])
def api_qc_config_save():
    data = request.json or {}
    data.pop("reuse_llm", None)   # 旧字段：质检不再复用文本分析 LLM，直接忽略
    # Q1（2026-10-07）：此前直接把 request.json 丢给 save_config，而前端发的是
    # {"config": {...}} —— save_config 按 CONFIG_KEYS 白名单**静默跳过**不认识的键，
    # 于是接口回「质检配置已保存」而磁盘零改动（质检页 + 音频页保存全废）。
    # 归一化 + fail-loud 的口径见 qc_client.normalize_config_patch 的 docstring。
    patch = qc_client.normalize_config_patch(data)
    if not patch:
        # 空补丁一律**拒收**，不再回「已保存」：走到这里说明请求体形状又不对了。
        return jsonify({"success": False,
                        "error": "未识别到任何可保存的质检配置字段：请求体需要是扁平字段"
                                 "（如 {\"pass_score\": 80}）或 {\"config\": {...}}",
                        "accepted_keys": sorted(qc_client.ACCEPTED_PATCH_KEYS)}), 400
    cfg = qc_client.save_config(QC_CONFIG_PATH, patch)
    view = qc_client.public_view(cfg)
    visual_requested = bool(view.get("enabled") and
                            (view.get("image_enabled") or view.get("video_enabled")))
    vision_bad = visual_requested and view.get("vision_status") != "ok"
    if view["enabled"] and not view["ready"]:
        return jsonify({"success": True, "config": view,
                        "config_path": os.path.abspath(QC_CONFIG_PATH),
                        "warning": "质检开关已开启，但质检接口信息不完整（base_url / api_key / model），"
                                   "生成流程将跳过质检且不会报错。",
                        "message": "配置已保存（接口未就绪）"})
    if vision_bad:
        status = view.get("vision_status") or "untested"
        reason = {"failed": "当前质检模型不支持或未通过视觉输入测试",
                  "uncertain": "视觉能力未确认",
                  }.get(status, "尚未完成视觉能力自检")
        return jsonify({"success": True, "config": view,
                        "config_path": os.path.abspath(QC_CONFIG_PATH),
                        "vision_warning": f"{reason}；图片/视频 AI 质检未生效。",
                        "message": "配置已保存（视觉自检未通过）"})
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "message": "质检配置已保存"})


@bp.route('/api/qc/config/vision', methods=['POST'])
def api_qc_config_vision():
    """P1-9：强制重测质检模型是否支持图像输入；uncertain 也按未生效处理。"""
    try:
        cfg = qc_client.refresh_vision_status(QC_CONFIG_PATH, force=True)
        view = qc_client.public_view(cfg)
        return jsonify({"success": True, "config": view,
                        "vision_status": view.get("vision_status"),
                        "vision_ok": view.get("vision_ok"),
                        "vision_error": view.get("vision_error", ""),
                        "message": "视觉能力已重测"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"视觉自检失败：{e}"}), 500


@bp.route('/api/qc/config/clear', methods=['POST'])
def api_qc_config_clear():
    cfg = qc_client.clear_config(QC_CONFIG_PATH)
    return jsonify({"success": True, "config": qc_client.public_view(cfg),
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "message": "质检配置已清除（质检总开关关闭）"})


@bp.route('/api/qc/config/reset-endpoint', methods=['POST'])
def api_qc_config_reset_endpoint():
    """把质检接口恢复为「AI 设置 → 质检模型」的独立配置"""
    cfg = qc_client.reset_endpoint(QC_CONFIG_PATH)
    return jsonify({"success": True, "config": qc_client.public_view(cfg),
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "message": "已清空质检页面内的接口覆盖，将使用「AI 设置 → 质检模型」"})


@bp.route('/api/qc/config/sync-from-ai', methods=['POST'])
def api_qc_config_sync_from_ai():
    """一键把「AI 设置 → 质检模型」的接口同步到质检配置（可选，便于统一维护）"""
    ai_cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(ai_cfg, "qc")
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        return jsonify({"success": False,
                        "error": "「AI 设置 → 质检模型」尚未配置完整，请先在那里填写并保存"}), 400
    cfg = qc_client.set_endpoint(QC_CONFIG_PATH, ep["base_url"], ep["api_key"], ep["model"])
    return jsonify({"success": True, "config": qc_client.public_view(cfg),
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "endpoint": {"base_url": ep["base_url"], "model": ep["model"], "source": "ai_settings"},
                    "message": "已同步「AI 设置 → 质检模型」到质检配置"})


@bp.route('/api/qc/test', methods=['POST'])
def api_qc_test():
    """测试质检接口：可用页面暂存参数直接测试，不落盘。
    传 image_path 时用真实图片走一次图片质检；传 video_path 走视频抽帧质检；
    否则做连通性测试（vision=true 时用极小图片探测视觉能力）。"""
    data = request.json or {}
    data.pop("reuse_llm", None)
    cfg = _qc_load_cfg()
    for k in ("image_prompt", "video_prompt", "pass_score",
              "max_retries", "video_frame_count"):
        if data.get(k) not in (None, ""):
            cfg[k] = data[k]
    cfg["enabled"] = True
    cfg["image_enabled"] = True
    cfg["video_enabled"] = True
    cfg = qc_client.load_config_dict(cfg)

    override = _qc_test_override(data)
    ep = qc_client.resolve_endpoint(cfg, override or None)
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return jsonify({"success": False,
                        "error": "质检接口未配置完整（质检需独立配置 base_url / api_key / model）",
                        "endpoint_source": ep["source"]}), 400

    def _endpoint_view():
        # 思考状态随端点一起下发（2026-10-06）：此前只回 base_url/model，
        # 页面无法区分「档位 low」与「关思考」，测试通过也不代表真实质检用的是同一档。
        return {"base_url": ep["base_url"], "model": ep["model"], "source": ep["source"],
                "reasoning_effort": ep.get("reasoning_effort", ""),
                "disable_thinking": bool(ep.get("disable_thinking", False))}

    image_path = data.get("image_path") or ""
    if not image_path:
        # 自动挑一张已有分镜图作为测试样张
        probe = _ep_read_dir(STORYBOARDS_DIR,
                             _safe_project(data.get("project_name") or "project"),
                             data.get("episode_no"))
        if os.path.isdir(probe):
            pngs = sorted([f for f in os.listdir(probe) if f.lower().endswith(".png")])
            if pngs:
                image_path = os.path.join(probe, pngs[0])

    video_path = data.get("video_path") or ""
    if video_path and os.path.isfile(video_path):
        verdict = qc_client.check_video(video_path, "接口连通性测试样张", cfg, override or None,
                                        frames_dir=os.path.join(QC_DIR, "_selftest", "frames"))
        ok = bool(verdict.get("ok"))
        return jsonify({
            "success": ok,
            "mode": "video",
            "video_path": os.path.abspath(video_path),
            "frame_count": verdict.get("frame_count"),
            "duration": verdict.get("duration"),
            "frames": verdict.get("frames"),
            "verdict": verdict,
            "endpoint": _endpoint_view(),
            "error": None if ok else (verdict.get("error") or verdict.get("reason")),
        }), (200 if ok else 400)

    if image_path and os.path.isfile(image_path):
        verdict = qc_client.check_image(image_path, "接口连通性测试样张", cfg, override or None)
        ok = bool(verdict.get("ok"))
        return jsonify({
            "success": ok,
            "mode": "image",
            "image_path": os.path.abspath(image_path),
            "verdict": verdict,
            "endpoint": _endpoint_view(),
            "error": None if ok else (verdict.get("error") or verdict.get("reason")),
        }), (200 if ok else 400)

    # 无样张：连通性测试。vision=true 或未指定时用极小图片探测视觉能力
    if data.get("vision", True):
        result = qc_client.test_vision(ep, timeout=60)
        return jsonify({"success": result.get("success"), "mode": "vision",
                        "vision": result.get("vision"), "reply": result.get("reply"),
                        "latency_ms": result.get("latency_ms"), "url": result.get("url"),
                        "endpoint": _endpoint_view(), "error": result.get("error")}), (
            200 if result.get("success") else 400)
    try:
        resp = qc_client._post_chat(ep, {
            "model": ep["model"],
            "messages": [{"role": "user", "content": "回复 JSON：{\"ok\": true}"}],
            "temperature": 0, "max_tokens": 64},
            cfg.get("timeout", 180))
        return jsonify({"success": True, "mode": "text", "raw": resp["content"][:300],
                        "latency_ms": resp["latency_ms"], "endpoint": _endpoint_view()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "mode": "text", "error": str(e),
                        "endpoint": _endpoint_view()}), 400


@bp.route('/api/qc/history/<path:project_name>/<kind>/<path:shot_key>')
def api_qc_history(project_name, kind, shot_key):
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    data = qc_client.read_history(QC_DIR, project, kind, shot_key)
    path = qc_client.history_path(QC_DIR, project, kind, shot_key)
    return jsonify({"success": bool(data), "project_name": project, "kind": kind,
                    "shot_id": shot_key, "exists": bool(data), "history": data,
                    "history_file": os.path.abspath(path)})


@bp.route('/api/qc/prompt', methods=['POST'])
def api_qc_prompt():
    """提示词预检（生成前质检）：按需检查一条提示词，并按配置自愈。

    body::

        {kind: "storyboard"|"h3"|"asset", prompt: "...", style?: "...",
         context?: <镜头/资产 dict>, ref_count?: int, expect_refs?: bool,
         repair?: true}   # repair=false 时只检查、不改写

    与图片/视频质检不同，这一层**不依赖质检接口**（纯确定性检查），因此未配置质检
    接口也能用；返回的 verdict 与 check_image/check_video 同构，便于前端统一展示。
    """
    data = _body()
    kind = str(data.get('kind') or '').strip().lower()
    if not kind:
        return jsonify({"success": False,
                        "error": f"缺少 kind（可选 {' / '.join(prompt_qc.PROMPT_KINDS)}）"}), 400
    if kind not in prompt_qc.PROMPT_KINDS:
        return jsonify({"success": False,
                        "error": f"不支持的 kind：{kind}（可选 {'/'.join(prompt_qc.PROMPT_KINDS)}）"}), 400
    prompt = str(data.get('prompt') or '')
    if not prompt.strip():
        return jsonify({"success": False, "error": "缺少 prompt"}), 400

    cfg = _qc_load_cfg()
    style = str(data.get('style') or '').strip()
    ctx = data.get('context') if isinstance(data.get('context'), dict) else None
    rc = data.get('ref_count')
    try:
        rc = int(rc) if rc is not None else None
    except (TypeError, ValueError):
        rc = None
    expect_refs = data.get('expect_refs')
    expect_refs = bool(expect_refs) if isinstance(expect_refs, (bool, int)) else None

    if bool(data.get('repair', True)):
        pf = prompt_qc.preflight(kind, prompt, ctx=ctx, style=style, cfg=cfg,
                                 ref_count=rc, expect_refs=expect_refs)
    else:
        v = prompt_qc.check_prompt(kind, prompt, ctx=ctx, style=style, cfg=cfg,
                                   ref_count=rc, expect_refs=expect_refs)
        blocked = bool(v.get("critical_issues"))
        pf = {"prompt": prompt, "verdict": v, "repairs": [], "accept": not blocked,
              "blocked": blocked, "skipped": not prompt_qc.prompt_qc_ready(cfg),
              "label": "提示词达标" if v.get("passed") and not v.get("issues") else "提示词不达标",
              "reason": v.get("reason") or "", "rebuild_hint": v.get("rebuild_hint") or ""}
    return jsonify({"success": True, "kind": kind,
                    "prompt": pf.get("prompt"), "verdict": pf.get("verdict"),
                    "repairs": pf.get("repairs") or [],
                    "gate": prompt_qc.prompt_qc_gate(pf, cfg),
                    "mode": prompt_qc.prompt_qc_mode(cfg),
                    "accept": bool(pf.get("accept")),
                    "label": pf.get("label"), "reason": pf.get("reason"),
                    "rebuild_hint": pf.get("rebuild_hint") or ""})


@bp.route('/api/qc/audio', methods=['POST'])
def api_qc_audio():
    """音频质检（成品质检）：客观层（ffmpeg 指标）+ AI 层（频谱图/波形图送多模态）。

    body::

        {project_name?: "...",
         path?: "output/dub/<项目>/lines/xxx.wav",   # 显式指定文件（必须位于 output/ 内）
         source?: "mix" | "merged" | "line",         # 未给 path 时按此推导（默认 mix > merged）
         expect_sec?: 3.2,          # 期望时长；不给则只做无声/削波判定，不做时长偏差
         line_text?: "三年了，我回来了。",
         check_speech_ratio?: true, # 「有声占比下限」判定。单句传 true；整轨必须 false
         with_ai?: true}            # false 时只跑客观层（毫秒级、零模型调用）

    与 ``/api/qc/prompt``（生成前预检）配套：那个管「台词写对没有」，这个管
    「录出来是不是真的有人声」。
    """
    data = _body()
    project = _safe_project(data.get('project_name') or '')
    cfg = _qc_load_cfg()
    if not qc_client.audio_qc_ready(cfg):
        return jsonify({"success": False,
                        "error": "音频质检未启用（请检查质检总开关与音频质检开关）",
                        "audio_qc_active": False}), 400

    # ---- 定位待检文件：显式 path 优先，否则按 source 推导 ----
    raw_path = str(data.get('path') or '').strip()
    source = str(data.get('source') or '').strip().lower()
    target, why = '', ''
    if raw_path:
        # 相对路径挂到**数据根**（output/ 就在它下面）：frozen 时 PROJECT_ROOT_DIR 是
        # _MEIPASS（只读资源区），拿它当基址会让下面 output/ 的包含性校验恒不通过。
        cand = os.path.abspath(os.path.join(PROJECT_DATA_DIR, raw_path)) \
            if not os.path.isabs(raw_path) else os.path.abspath(raw_path)
        root = os.path.abspath(PROJECT_OUTPUT_DIR)
        # 只允许检查 output/ 内的产物：这是「回显用户自己的成品」，不是任意文件读取接口
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

    # ---- 期望时长 / 有声占比口径 ----
    expect = data.get('expect_sec')
    try:
        expect = float(expect) if expect not in (None, '') else None
    except (TypeError, ValueError):
        expect = None
    if expect is None and source == 'mix':
        try:
            expect = float(mix_probe_audio(target).get('duration') or 0) or None
        except Exception:  # noqa: BLE001
            expect = None
    # ⚠️ 整轨（成片 mp4 / 整集合成音轨）默认**关闭**有声占比判定：
    #    成片天然有大段无台词留白，拿单句的 50% 标准卡它必然误报「漏句」。
    #    判据是「整轨口径」而不是「调用方有没有传 source」—— 前端直接拖一个 mp4 过来
    #    检查（source 会是 path）时同样必须关掉，否则一进来就是满屏「静音过多」。
    is_whole_track = source in ('mix', 'merged') or target.lower().endswith(('.mp4', '.mkv', '.mov'))
    default_ratio = not is_whole_track
    check_ratio = data.get('check_speech_ratio')
    check_ratio = default_ratio if not isinstance(check_ratio, bool) else check_ratio

    stem = os.path.splitext(os.path.basename(target))[0]
    bucket = 'audio_mix' if (source == 'mix' or target.lower().endswith('.mp4')) else 'audio'
    visuals_dir = os.path.join(
        QC_DIR, bucket,
        _audio_qc_visuals_key(data.get('project_name'), target), stem)

    with_ai = bool(data.get('with_ai', True))
    if not with_ai:
        verdict = audio_qc.quick_check(
            target, expect_sec=expect,
            min_speech_ratio=(cfg.get("audio_min_speech_ratio", 0.50) if check_ratio else None),
            min_mean_db=cfg.get("audio_min_mean_db", -45.0),
            max_drift=cfg.get("audio_max_drift", 0.50))
        verdict["ai_skipped"] = True
        verdict["ai_skip_reason"] = "请求显式要求只做客观层（with_ai=false）"
    else:
        verdict = qc_client.check_audio(
            target, expect_sec=expect, line_text=str(data.get('line_text') or ''),
            cfg=cfg, visuals_dir=visuals_dir, check_speech_ratio=check_ratio)

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
        "expect_sec": expect, "check_speech_ratio": check_ratio,
        "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
        "score": verdict.get("score"), "reason": verdict.get("reason"),
        "issues": verdict.get("issues") or [],
        "critical_issues": verdict.get("critical_issues") or [],
        "metrics": verdict.get("metrics") or {},
        "ai_used": bool(verdict.get("ai_used")),
        "ai_skipped": bool(verdict.get("ai_skipped")),
        "ai_skip_reason": verdict.get("ai_skip_reason") or "",
        "objective_only": bool(verdict.get("objective_only")),
        "visuals": vis_urls,
        "verdict": verdict,
        "audio_qc_active": True,
        "audio_ai_active": qc_client.audio_ai_ready(cfg),
        "file_url": _audio_qc_file_url(project, target),
    })


@bp.route('/api/qc/project-summary', methods=['GET'])
def api_qc_project_summary():
    """项目级 QC 聚合：列出所有质检项的最新结论，供前端总览页使用

    ⚠️ 审计 G3：本接口此前**恒返回空统计**（线上 149 个质检历史文件一个都统计不到），
    根因有三处，缺一不可：
      ① `project = _safe_project(request.args.get('project', ''))` 后面接
         `if not project:` —— `_safe_project('')` 返回**字面量 'project'**（真值），
         守卫恒不成立（死守卫）。漏传项目名不会报错，而是聚合到共享 `project` 命名空间。
         判空必须看**原始入参**。
      ② 它枚举的是 `QC_DIR` 下以 `shot_` 开头的**目录**，而真实落盘路径是
         `QC_DIR/<项目>/<kind>_<键>.json` —— 一个都匹配不到，于是总览页永远
         「0 通过 / 0 失败」，用户以为质检从未运行过。
      ③ `read_history` 返回 `{"records": [...]}` 字典、记录里的字段是
         `passed` / `time`，**没有** `verdict` / `timestamp`。旧代码把 dict 当 list 用
         （`hist[-1]`）并按 `verdict` 判通过 —— 即使目录判对了也统计不出来。
    """
    raw = (request.args.get('project') or '').strip()
    if not raw:
        return jsonify({"success": False, "error": "缺少 project 参数"}), 400
    project = _safe_project(raw)

    qc_cfg = _qc_load_cfg()
    qc_dir = os.path.join(QC_DIR, project)
    shots: list = []
    passed = failed = retry_count = 0

    if os.path.isdir(qc_dir):
        for fn in sorted(os.listdir(qc_dir)):
            if not fn.lower().endswith('.json'):
                continue
            # 文件名形如 <kind>_<键>.json（image_1.json / asset_image_七转蛊仙_base.json）
            kind, sep, shot_key = fn[:-5].partition('_')
            if not sep or not shot_key:
                continue
            try:
                with open(os.path.join(qc_dir, fn), 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
            except Exception as e:  # noqa: BLE001 - 单条坏文件不该拖垮总览
                app.logger.warning("质检历史读取失败（已跳过）：%s：%s", fn, e)
                continue
            records = data.get('records') or []
            latest = records[-1] if (records and isinstance(records[-1], dict)) else {}
            # 通过与否以记录里的 `passed` 为准；接口异常（ok=False）单独归入「待重试」
            if latest.get('passed') is True or data.get('last_passed') is True:
                verdict = 'pass'
            elif latest.get('ok') is False:
                verdict = 'error'
            elif latest.get('passed') is False or data.get('last_passed') is False:
                verdict = 'fail'
            else:
                verdict = 'unknown'
            shots.append({
                'shot_id': shot_key,
                'kind': kind,
                'stage': latest.get('stage') or '',
                'score': latest.get('score'),
                'verdict': verdict,
                'timestamp': latest.get('time') or data.get('updated_at') or '',
                'attempts': int(data.get('total_attempts') or len(records) or 0),
                'error': latest.get('error') or '',
                'reason': latest.get('reason') or '',
                'file': latest.get('file') or '',
            })
            if verdict == 'pass':
                passed += 1
            elif verdict == 'fail':
                failed += 1
            else:
                retry_count += 1

    view = qc_client.public_view(qc_cfg)
    return jsonify({
        "success": True,
        "project": project,
        "config": view,
        "stats": {"total": len(shots), "passed": passed, "failed": failed,
                  "retry_count": retry_count},
        "history": shots,
        "qc_dir": qc_dir,
    })


@bp.route('/api/qc/frames/<path:filename>')
def api_qc_frame_file(filename):
    """回显视频质检抽帧图（只读）"""
    safe = filename.replace('\\', '/')
    target = os.path.abspath(os.path.join(QC_DIR, safe))
    root = os.path.abspath(QC_DIR)
    # 审计 P2（2026-09-29）：前缀必须带路径分隔符 —— 否则 output/qc_backup 等
    # 「qc 开头」的兄弟目录也能通过前缀判断（与 _serve_safe 的 base+os.sep 口径对齐）
    if not target.startswith(root + os.sep):
        abort(403)
    if not os.path.exists(target):
        abort(404)
    return send_file(target, conditional=True)


@bp.route('/api/quality/episodes', methods=['GET'])
def api_quality_episodes():
    """集列表 + 每集四层状态（审片界面左栏）。"""
    raw = (request.args.get('project') or '').strip()
    if not raw:
        return jsonify({"success": False, "error": "缺少 project 参数"}), 400
    project = _safe_project(raw)
    rows = [_quality_episode_row(project, e) for e in _quality_ep_numbers(project)]
    return jsonify({"success": True, "project": project, "episodes": rows,
                    "count": len(rows),
                    "summary": {"total": len(rows),
                                "ready": sum(1 for r in rows if r["release"]["ready"]),
                                "awaiting_review": sum(
                                    1 for r in rows
                                    if r["state"]["C"]["status"] == "pending"
                                    and r["artifact"]["exists"])}})


@bp.route('/api/quality/review', methods=['GET'])
def api_quality_review():
    """单集完整审片载荷：契约 + 逐镜（分镜/参考/视频/质检）+ 四层状态。"""
    raw = (request.args.get('project') or '').strip()
    if not raw:
        return jsonify({"success": False, "error": "缺少 project 参数"}), 400
    project = _safe_project(raw)
    try:
        ep = int(request.args.get('episode') or 1)
    except (TypeError, ValueError):
        ep = 1

    scr = _load_script_for(project, ep) or {}
    shots = [s for s in (scr.get("shots") or []) if isinstance(s, dict)]
    ep_dir = _ep_read_dir(VIDEOS_DIR, project, ep)

    out_shots = []
    for i, s in enumerate(shots):
        seq = shot_key.shot_seq(s.get("shot_id"), i + 1) or (i + 1)
        vpath = os.path.join(ep_dir, "shot_%02d.mp4" % seq)
        vexists = os.path.isfile(vpath)
        sb_url = _quality_storyboard_url(project, seq)
        qc = {"found": False, "attempts": 0, "last_passed": None, "latest": {}}
        try:
            hist = qc_client.read_history(QC_DIR, project, "video",
                                          s.get("shot_id", seq)) or {}
            recs = hist.get("records") or []
            if recs:
                last = recs[-1] if isinstance(recs[-1], dict) else {}
                qc = {"found": True, "attempts": len(recs),
                      "last_passed": hist.get("last_passed"),
                      "latest": {"attempt": last.get("attempt"),
                                 "ok": last.get("ok"), "passed": last.get("passed"),
                                 "score": last.get("score"),
                                 "reason": last.get("reason") or "",
                                 "issues": last.get("issues") or [],
                                 "critical_issues": last.get("critical_issues") or [],
                                 "style_mismatch": bool(last.get("style_mismatch")),
                                 "duration": last.get("duration"),
                                 "time": last.get("time") or "",
                                 "frames": last.get("frames") or []}}
        except Exception as e:                               # noqa: BLE001
            app.logger.warning("读逐镜质检历史失败（shot %s）：%s", seq, e)
        out_shots.append({
            "seq": seq, "shot_id": s.get("shot_id", seq),
            "duration": s.get("duration"), "camera": s.get("camera") or "",
            "location": s.get("location") or "",
            "description": s.get("description") or "",
            "dialogue_text": s.get("dialogue_text") or "",
            "first_frame": s.get("first_frame") or "",
            "last_frame": s.get("last_frame") or "",
            "motion": s.get("motion") or "", "emotion": s.get("emotion") or "",
            "beat": s.get("beat") or "",
            "video": {"exists": vexists, "url": _quality_video_url(vpath) if vexists else ""},
            "storyboard": {"exists": bool(sb_url), "url": sb_url},
            "refs": _quality_refs_for_shot(project, s), "qc": qc})

    full = _quality_find_full(project, ep)
    sv = _quality_state_view(project, ep)
    return jsonify({"success": True, "project": project, "episode": ep,
                    "contract": {"title": scr.get("episode_title") or scr.get("title") or "",
                                 "style": scr.get("style") or "",
                                 "shots_total": int(scr.get("shot_count") or len(shots)),
                                 "duration_sec": float(scr.get("episode_duration_sec") or 0)},
                    "shots": out_shots,
                    "artifact": {"exists": bool(full),
                                 "name": os.path.basename(full) if full else "",
                                 "url": _quality_video_url(full)},
                    "state": sv["stages"], "release": sv["release"],
                    "stale": sv["stale"], "preview": _quality_find_preview(project, ep)})


@bp.route('/api/quality/stage', methods=['POST'])
def api_quality_stage():
    """人工层（C 编辑复核 / D 发布批准）状态写入。

    「passed」必须绑定当刻产物 + 合同哈希：重渲或改剧本后批准自动失效。
    A/B 由系统判定，本接口不接受 —— 防止人工结论覆盖机器证据。
    """
    data = _body()
    project, err = _project_or_400(
        (data.get('project') or data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        ep = int(data.get('episode') or data.get('episode_no') or 1)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode 必须是整数"}), 400
    stage = str(data.get('stage') or '').strip().upper()
    status = str(data.get('status') or '').strip().lower()
    note = str(data.get('note') or '')
    if stage not in ('C', 'D'):
        return jsonify({"success": False,
                        "error": "stage 仅允许 C(编辑复核)/D(发布批准)，A/B 由系统判定"}), 400
    if status not in quality_stage.STATUSES:
        return jsonify({"success": False,
                        "error": "status 非法（允许 %s）" % (quality_stage.STATUSES,)}), 400

    full = _quality_find_full(project, ep)
    binding = None
    if status == 'passed':
        if not full:
            return jsonify({"success": False,
                            "error": "该集尚无整集成片，无法复核/批准（请先完成整集生成）"}), 400
        binding = quality_stage.make_binding(
            full, contract=_quality_contract_summary(project, ep))

    state = quality_stage.record_stage(
        project, ep, stage, status, note=note,
        evidence={"by": "review_ui",
                  "artifact": os.path.basename(full) if full else ""},
        binding=binding)
    app.logger.info("[审片] %s 第%s集 %s → %s%s", project, ep,
                    quality_stage.STAGE_LABELS[stage], status,
                    ("（%s）" % note) if note else "")
    sv = _quality_state_view(project, ep)
    return jsonify({"success": True, "stage": stage, "status": status,
                    "state": sv["stages"], "release": sv["release"],
                    "stale": sv["stale"]})

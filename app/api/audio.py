# -*- coding: utf-8 -*-
"""audio 域蓝图：TTS 配音、音效、混音。

2026-10-07 由 ``app/app.py`` 按域拆分而来，**纯搬迁**：路径、方法、报错文案逐字
不变（守门脚本 ``scripts/snapshot_routes.py`` 比对 rule + methods）。
``url_prefix`` 留空，路径直接写死在各 ``@bp.route`` 上。
"""
from __future__ import annotations

from flask import Blueprint

# 共享 runtime 上下文（app 实例 / 212 个模块级 helper / config 常量）。
# __all__ 里显式列出了下划线开头的名字，故这里能整体取到。
from api._shared import *  # noqa: F401,F403

bp = Blueprint("audio", __name__)
DOMAIN = "audio"

@bp.route('/api/continuity/<novel_id>', methods=['GET'])
def api_continuity_overview(novel_id):
    """项目级连贯性总览：bible / style_guide / 金句清单 / 口吻词典 / 运镜术语表 / 各集文件清单"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = continuity.continuity_overview(CONTINUITY_DIR, key)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", ""),
                 "novel_title": meta.get("title") or meta.get("name")})
    return jsonify(data)


@bp.route('/api/continuity/<novel_id>/<int:episode_no>', methods=['GET'])
def api_continuity_episode(novel_id, episode_no):
    """单集连贯性：上集摘要卡 / 本集 state_in·state_out / 跨集一致性校验结果"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = continuity.episode_continuity_view(CONTINUITY_DIR, key, episode_no)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", "")})
    return jsonify(data)


@bp.route('/api/continuity/<novel_id>/<int:episode_no>/revalidate', methods=['POST'])
def api_continuity_revalidate(novel_id, episode_no):
    """对已落盘剧本重跑「相邻集六类一致性校验」（D⑨ 可选闸门；body.rewrite=true 时命中问题会局部重写）"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法执行一致性校验")
    try:
        script = novel_to_script.load_episode_script(SCRIPT_DIR, key, episode_no)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本读取失败：{e}"}), 404

    state = continuity.load_state(CONTINUITY_DIR, key, episode_no) or {
        "state_in": script.get("state_in") or {},
        "state_out": script.get("state_out") or {},
        "key_events": (script.get("continuity") or {}).get("key_events") or [],
    }
    prev_state = continuity.load_state(CONTINUITY_DIR, key, episode_no - 1)
    prev_script = continuity._load_prev_script(SCRIPT_DIR, key, episode_no - 1)
    rule_issues = continuity.rule_timeline_check(prev_state, state, script) if prev_state else []
    events = []
    validation = continuity.validate_continuity(
        client, script, prev_script, prev_state, state, episode_no,
        events=events, rule_issues=rule_issues)
    validation["quotes"] = continuity.check_quotes_in_script(CONTINUITY_DIR, key, script, episode_no)

    rewrite = {"triggered": False, "rounds": 0, "rewritten_shot_ids": [], "notes": []}
    body = request.json or {}
    if body.get("rewrite") and validation.get("rewrite_needed") and int(episode_no) > 1:
        issues = [i for i in (validation.get("issues") or [])
                  if i.get("severity") in ("high", "medium")]
        ctx = continuity.build_continuity_context(
            CONTINUITY_DIR, key, episode_no, script.get("style") or "3D动漫渲染")
        rw = continuity.rewrite_shots_for_issues(client, script, issues, episode_no,
                                                 continuity_ctx=ctx, events=events,
                                                 shot_ids=validation.get("rewrite_shot_ids"))
        if rw.get("rewritten_shot_ids"):
            rewrite.update({"triggered": True, "rounds": 1,
                            "rewritten_shot_ids": rw["rewritten_shot_ids"],
                            "notes": rw.get("notes") or []})
            novel_to_script.save_episode_script(script, SCRIPT_DIR, key, episode_no, key)
            state = continuity.extract_episode_state(client, script, prev_state, episode_no,
                                                     events=events)
            script["state_in"], script["state_out"] = state["state_in"], state["state_out"]
            rule_issues = continuity.rule_timeline_check(prev_state, state, script) if prev_state else []
            validation = continuity.validate_continuity(
                client, script, prev_script, prev_state, state, episode_no,
                events=events, rule_issues=rule_issues)
            validation["quotes"] = continuity.check_quotes_in_script(
                CONTINUITY_DIR, key, script, episode_no)
            continuity.save_state(CONTINUITY_DIR, key, state)
            novel_to_script.save_episode_script(script, SCRIPT_DIR, key, episode_no, key)
        else:
            rewrite["error"] = rw.get("error") or "未产生修改"

    validation["rewrite"] = rewrite
    validation["generated_at"] = continuity._now()
    continuity.save_json(continuity.validation_path(CONTINUITY_DIR, key, episode_no), validation)
    # P0-3 剧本↔原著一致性摘要：直接复用生成时写入剧本的结论（三件套为确定性计算，
    # 不在本接口重算，避免无章节正文时误报）
    consistency = (script.get("metadata") or {}).get("script_consistency") or {}
    return jsonify({"success": True, "novel_id": novel_id, "episode_no": episode_no,
                    "project_key": key, "validation": validation, "rewrite": rewrite,
                    "consistency": consistency,
                    "events": events})


@bp.route('/api/tts/env', methods=['GET'])
def api_tts_env():
    """配音环境自检：ComfyUI 在线 / Qwen-TTS 节点 / 模型权重 / 可用音色"""
    env = tts_env_check()
    env["voices"] = tts_list_voices()
    env["dub_dir"] = DUB_DIR
    env["out_dir"] = _dub_project_dir(_safe_project(request.args.get('project_name') or 'project'))
    return jsonify(env)


@bp.route('/api/tts/plan', methods=['GET', 'POST'])
def api_tts_plan():
    """生成配音计划预览（逐句说话人 + 音色 + 落盘文件名，不合成）"""
    data = request.json or {} if request.method == 'POST' else dict(request.args)
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        resolved = _dub_resolve_script(dict(data, project_name=project_name))
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    script = resolved["script"]
    episode = int(data.get('episode') or script.get('episode_no')
                  or (script.get('metadata') or {}).get('episode_no') or 1)
    out_dir = _dub_project_dir(project_name)
    vm_path = os.path.join(out_dir, "voice_map.json")
    voice_map = data.get('voice_map') or load_voice_map(vm_path) \
        or default_voice_map(script.get('characters') or [], project_name, episode)
    shot_ids = data.get('shot_ids') or None
    if isinstance(shot_ids, (str, int)):
        shot_ids = [shot_ids]

    try:
        plan = build_dub_plan(script, voice_map, project_name, episode,
                              shot_ids=shot_ids,
                              only_missing=bool(data.get('only_missing')),
                              out_dir_wav=os.path.join(out_dir, "lines"))
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # T03b：配音计划构建后、预览/合成前，按 kind="audio" 召回历史教训做计划级纠偏
    # （说话人回填 / preset→design / 期望时长），绝不进台词。无教训时零行为变更。
    _apply_audio_lessons(plan.get("lines") or [], project_name)

    for ln in plan["lines"]:
        ln["url"] = _dub_audio_url(project_name, ln.get("out_path") or "")
        ln["exists"] = bool(ln.get("out_path") and os.path.exists(ln["out_path"]))
    # 剧本体检：兜底镜头（dialogue=[] 且 prompt_h3=""）会让配音一句都合不出来，
    # 但脚本生成阶段看起来是「成功」的 —— 这里把缺口提前摆到用户面前。
    audit = dialogue_utils.audit_script(script)
    plan.update({"success": True, "script_path": resolved["script_path"],
                 "script_source": resolved["source"], "voice_map_path": vm_path,
                 "out_dir": out_dir,
                 "audit": audit["stats"], "warnings": audit["warnings"],
                 "warnings_ok": audit["ok"]})
    return jsonify(plan)


@bp.route('/api/tts/voice-map', methods=['POST'])
def api_tts_voice_map_save():
    """保存角色音色配置（所有角色固定 speaker/seed，保证全剧音色一致）"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    voice_map = data.get('voice_map')
    if not isinstance(voice_map, dict):
        return jsonify({"success": False, "error": "缺少 voice_map"}), 400
    try:
        for name, v in (voice_map.get("characters") or {}).items():
            normalize_voice(v)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    path = os.path.join(_dub_project_dir(project_name), "voice_map.json")
    save_voice_map(voice_map, path)
    return jsonify({"success": True, "path": path, "voice_map": voice_map})


@bp.route('/api/tts/voice-bank', methods=['GET'])
def api_tts_voice_bank_list():
    """列出本项目已绑定参考音频的角色（含时长/原文/更新时间）。"""
    project_name = _safe_project(request.args.get('project_name') or '')
    if not project_name:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
    items = list_voice_bank(_dub_project_dir(project_name))
    return jsonify({"success": True, "items": items,
                    "clone_available": tts_clone_available(),
                    "supported_exts": list(VOICE_BANK_EXTS)})


@bp.route('/api/tts/voice-bank/upload', methods=['POST'])
def api_tts_voice_bank_upload():
    """上传某角色的参考音频（form-data: file, project_name, character, ref_text?）。

    ref_text = 这段参考音频里**实际说出的那句话**。填了克隆相似度显著更高，
    但可留空（节点支持 x_vector_only 路径，只用说话人向量）。
    """
    files = request.files.getlist('file') or request.files.getlist('files')
    if not files:
        return jsonify({"success": False, "error": "未收到音频，请通过 file 字段上传"}), 400
    f = files[0]
    project_name, err = _project_or_400(
        (request.form.get('project_name') or request.args.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (request.form.get('character') or request.args.get('character') or '').strip()
    ref_text = str(request.form.get('ref_text') or request.args.get('ref_text') or '').strip()
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400

    raw_name = _safe_upload_name(f.filename)
    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in VOICE_BANK_EXTS:
        return jsonify({"success": False,
                        "error": f"不支持的音频格式 {ext or '（无扩展名）'}；"
                                 f"支持 {'、'.join(VOICE_BANK_EXTS)}"}), 400

    os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(UPLOAD_TMP_DIR,
                            f"vref_{time.strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}{ext}")
    try:
        f.save(tmp_path)
        # 客观校验：能读出时长且落在合理区间。参考音频太短克隆不出音色、
        # 太长拖慢每次合成（每次都要解码），都要在入口拦下并给出可读原因。
        info = probe_audio_info(tmp_path)
        if not info.get("ok"):
            return jsonify({"success": False,
                            "error": f"音频无法读取：{info.get('error') or '解码失败'}"}), 400
        dur = float(info.get("duration") or 0)
        if dur < tts_client.VOICE_BANK_MIN_SEC:
            return jsonify({"success": False,
                            "error": f"参考音频过短（{dur:.1f}s）—— 建议 3–15 秒清晰人声"}), 400
        if dur > tts_client.VOICE_BANK_MAX_SEC:
            return jsonify({"success": False,
                            "error": f"参考音频过长（{dur:.1f}s > "
                                     f"{tts_client.VOICE_BANK_MAX_SEC:.0f}s）—— 请截取 3–15 秒"}), 400

        dub_dir = _dub_project_dir(project_name)
        dst = save_voice_bank_ref(dub_dir, character, tmp_path,
                                  ref_text=ref_text, original_filename=raw_name)
        app.logger.info("[参考音频] %s/%s ← %s（%.1fs）", project_name, character, raw_name, dur)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("参考音频入库失败：%s", e)
        return jsonify({"success": False, "error": f"参考音频保存失败：{e}"}), 500
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return jsonify({"success": True, "character": character, "file": dst,
                    "ref_text": ref_text, "duration_sec": dur,
                    "clone_available": tts_clone_available(),
                    "message": f"已绑定参考音频（{dur:.1f}s）；该角色下次配音将自动使用克隆声线"})


@bp.route('/api/tts/voice-bank/delete', methods=['POST'])
def api_tts_voice_bank_delete():
    """解绑某角色的参考音频（删掉整个 voice_bank/<角色>/ 目录）。

    只删 voice_bank 子目录内的内容（由 voice_bank_dir 给出路径），不递归到别处。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (data.get('character') or '').strip()
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400
    d = voice_bank_dir(_dub_project_dir(project_name), character)
    removed = []
    if os.path.isdir(d):
        try:
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if os.path.isfile(p):
                    os.remove(p)
                    removed.append(fn)
            os.rmdir(d)
        except OSError as e:
            app.logger.warning("解绑参考音频失败 %s：%s", d, e)
            return jsonify({"success": False, "error": f"删除失败：{e}"}), 500
    return jsonify({"success": True, "character": character, "removed": removed,
                    "message": "已解绑参考音频" if removed else "该角色未绑定参考音频"})


@bp.route('/api/tts/voice-bank/preview', methods=['POST'])
def api_tts_voice_bank_preview():
    """用已绑定的参考音频试听克隆效果（合成一句样例文本）。

    body = {project_name, character, text?}
    刻意**不读** voice_map：直接以 clone 模式合成，让用户在决定保存前先听效果。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (data.get('character') or '').strip()
    if not character:
        return jsonify({"success": False, "error": "缺少 character"}), 400
    text = clean_line_text(data.get('text') or '', character) or \
        f"我是{character}，今日便让你见识见识。"
    if not tts_clone_available():
        return jsonify({"success": False,
                        "error": "当前 ComfyUI 未提供参考音频克隆节点"
                                 "（FB_Qwen3TTSVoiceClone / LoadAudio）"}), 503
    dub_dir = _dub_project_dir(project_name)
    ref, ref_text = find_voice_bank_ref(dub_dir, character)
    if not ref:
        return jsonify({"success": False,
                        "error": f"角色「{character}」未绑定参考音频，请先上传"}), 400
    # ⭐ 2026-10-05 修复「试听 500」：
    # ① voice 带上 character —— synthesize_one 的批项此前恒为 character=None，
    #    教训库 / 台词清洗全部丢了角色维度；
    # ② 整段包一层兜底 try：此前 normalize_voice / safe_name / makedirs 里任何
    #    未预期异常都会直接变 Flask 500（界面只剩「Internal Server Error」一句，
    #    用户完全不知道发生了什么）。现在统一返回可读的 502/500 JSON。
    try:
        voice = normalize_voice({
            "mode": "clone", "ref_audio": ref, "ref_text": ref_text,
            "speaker": "Ryan", "seed": 0, "character": character,
        })
        preview_dir = os.path.join(dub_dir, "preview")
        os.makedirs(preview_dir, exist_ok=True)
        out_path = os.path.join(preview_dir,
                                f"clone_{int(time.time())}_{tts_client.safe_name(character, 12)}.wav")
        client = QwenTTSClient(out_root=DUB_DIR, params=TTS_DEFAULT_PARAMS)
        rec = client.synthesize_one(text, voice, out_path)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 502
    except Exception as e:  # noqa: BLE001 兜底：绝不让试听变裸 500
        app.logger.exception("参考音色试听失败")
        return jsonify({"success": False, "error": f"试听失败：{e}"}), 500
    if not rec.get("ok"):
        return jsonify({"success": False, "error": rec.get("error") or "合成失败",
                        "clone_fallback": rec.get("clone_fallback")}), 502
    info = probe_audio_info(rec["out_path"])
    return jsonify({"success": True, "result": rec, "audio": info, "voice": voice,
                    "url": _dub_audio_url(project_name, rec["out_path"]),
                    "text_used": text})


@bp.route('/api/tts/preview', methods=['POST'])
def api_tts_preview():
    """单句试听合成：指定 text + 音色（或角色），返回可播放音频与时长"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    text = clean_line_text(data.get('text') or '', str(data.get('character') or ''))
    if not text:
        return jsonify({"success": False, "error": "缺少待合成文本 text"}), 400

    # 试听前先跑一遍台词预检：用户在这里就能看到「这句会被念成什么样」以及为什么，
    # 不必等到整集配完才发现结构残留被念了出来。
    _pf = None
    try:
        _pcfg = _qc_load_cfg()
        _pf = prompt_qc.preflight(
            "audio", text,
            ctx={"character": str(data.get('character') or '') or None,
                 "emotion": data.get('emotion'),
                 "voice_mode": (data.get('voice') or {}).get('mode')
                               if isinstance(data.get('voice'), dict) else None},
            cfg=_pcfg)
        if _pf.get("prompt"):
            text = _pf["prompt"]
    except Exception as e:  # noqa: BLE001 - 预检失败不影响试听
        app.logger.debug(f"试听台词预检跳过：{e}")

    env = tts_env_check()
    if not env.get("available"):
        return jsonify({"success": False, "error": "TTS 环境不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    out_dir = _dub_project_dir(project_name)
    vm_path = os.path.join(out_dir, "voice_map.json")
    voice_map = load_voice_map(vm_path) or {}
    char_name = str(data.get('character') or '').strip()
    try:
        base = (voice_map.get("characters") or {}).get(char_name) if char_name else None
        if base is None and char_name:
            base = default_voice_map([{"name": char_name}], project_name, 1)["characters"].get(char_name)
        voice = normalize_voice(data.get('voice'), base)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    # 2026-10-05：voice 补上 character 维度（教训库记账）+ 整段兜底，不再裸 500
    voice = dict(voice)
    if char_name:
        voice["character"] = char_name
    try:
        preview_dir = os.path.join(out_dir, "preview")
        os.makedirs(preview_dir, exist_ok=True)
        out_path = os.path.join(preview_dir, f"preview_{int(time.time())}_"
                                          f"{tts_client.safe_name(char_name or 'line', 12)}.wav")
        client = QwenTTSClient(out_root=DUB_DIR, params=TTS_DEFAULT_PARAMS)
        rec = client.synthesize_one(text, voice, out_path)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 502
    except Exception as e:  # noqa: BLE001 兜底：绝不让试听变裸 500
        app.logger.exception("单句试听失败")
        return jsonify({"success": False, "error": f"试听失败：{e}"}), 500
        return jsonify({"success": False, "error": str(e)}), 502
    if not rec.get("ok"):
        return jsonify({"success": False, "error": rec.get("error") or "合成失败", "result": rec}), 502

    info = probe_audio_info(rec["out_path"])
    # 试听只跑**客观层**音频质检：纯 ffmpeg、毫秒级，不花模型调用，保证试听依然「点一下就响」。
    # 需要含 AI 层（频谱/波形送检）的完整结论时走 POST /api/qc/audio。
    _objs = None
    try:
        _ocfg = _qc_load_cfg()
        if qc_client.audio_qc_ready(_ocfg):
            # ⚠️ quick_check 的 verdict 里已经带了 metrics，不要再单独 probe 一次 ——
            #    那会重复解码一遍音频（白等一次 ffmpeg）。
            _objs = audio_qc.quick_check(
                rec["out_path"], expect_sec=audio_qc.estimate_speech_sec(text) or None,
                min_speech_ratio=_ocfg.get("audio_min_speech_ratio", 0.50),
                min_mean_db=_ocfg.get("audio_min_mean_db", -45.0),
                max_drift=_ocfg.get("audio_max_drift", 0.50))
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"试听音频客观质检跳过：{e}")
    return jsonify({"success": True, "result": dict(rec, url=_dub_audio_url(project_name, rec["out_path"])),
                    "audio": info, "voice": voice, "url": _dub_audio_url(project_name, rec["out_path"]),
                    "prompt_qc": (_pf or {}).get("verdict"),
                    "prompt_qc_label": (_pf or {}).get("label"),
                    "prompt_qc_repairs": (_pf or {}).get("repairs") or [],
                    "text_used": text,
                    "audio_qc": _objs})


@bp.route('/api/tts/generate', methods=['POST'])
def api_tts_generate():
    """发起批量配音（异步任务）：逐句/逐角色合成 + 整集合并"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err

    env = tts_env_check()
    if not env.get("available"):
        return jsonify({"success": False,
                        "error": "TTS 环境不可用，无法配音：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    try:
        resolved = _dub_resolve_script(dict(data, project_name=project_name))
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    script = resolved["script"]
    episode = int(data.get('episode') or script.get('episode_no')
                  or (script.get('metadata') or {}).get('episode_no') or 1)
    out_dir = _dub_project_dir(project_name)
    vm_path = os.path.join(out_dir, "voice_map.json")
    voice_map = data.get('voice_map') or load_voice_map(vm_path) \
        or default_voice_map(script.get('characters') or [], project_name, episode)
    shot_ids = data.get('shot_ids') or None
    if isinstance(shot_ids, (str, int)):
        shot_ids = [shot_ids]
    fmt = str(data.get('format') or 'wav').lower()
    if fmt not in ('wav', 'mp3'):
        return jsonify({"success": False, "error": "format 仅支持 wav / mp3"}), 400

    try:
        plan = build_dub_plan(script, voice_map, project_name, episode,
                              shot_ids=shot_ids,
                              only_missing=bool(data.get('only_missing')),
                              out_dir_wav=os.path.join(out_dir, "lines"))
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    if not plan.get("lines"):
        # 兜底剧本（dialogue=[]、prompt_h3=""）会走到这里，但用户看到「没有台词」
        # 完全不知道是自己没写、还是模型兜底了 —— 把体检结论一并给出。
        audit = dialogue_utils.audit_script(script)
        stats = audit["stats"]
        return jsonify({
            "success": False,
            "error": "剧本中没有可朗读台词（或所选镜头无台词）",
            "hint": ("该集可能是「模型失败后按原文兜底」生成的：镜头内容是原文照搬，"
                     "没有任何台词，因此配音无从合成。请先人工润色该集剧本（补台词），"
                     "或重新生成剧本。"
                     if stats.get("fallback_shot_count") else
                     "请检查剧本该集是否确实没有台词内容。"),
            "audit": stats, "problem_shots": audit["problem_shots"],
        }), 400

    # T03b：合成前同样召回 kind="audio" 历史教训做计划级纠偏（与 /api/tts/plan 一致），
    # 避免用户「预览没纠偏、合成又纠偏」的不一致；无教训时零行为变更。
    _apply_audio_lessons(plan.get("lines") or [], project_name)

    # 保存音色映射，保证同角色跨轮次音色一致
    try:
        save_voice_map(plan["voice_map"], vm_path)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"音色映射保存失败：{e}")

    plan_summary = {"script_path": resolved["script_path"], "script_source": resolved["source"],
                    "episode": episode, "line_count": plan["line_count"],
                    "characters": [{"name": c["name"], "voice": c["voice"],
                                    "line_count": c["line_count"]} for c in plan["characters"]]}
    with dub_lock:
        # G5：同项目已有 running 的配音任务 → 复用（uuid 任务 ID 防止同秒覆盖）
        _existing_dub = next((tid for tid, t in dub_tasks.items()
                               if t.get("status") == "running"
                               and t.get("project_name") == project_name), None)
        if _existing_dub:
            return jsonify({"success": True, "task_id": _existing_dub, "status": "started",
                            "reused": True, "project_name": project_name, "episode": episode,
                            "line_count": plan["line_count"], "plan": plan_summary,
                            "out_dir": out_dir})
        task_id = f"dub_{project_name}_{uuid.uuid4().hex[:12]}"
        dub_tasks[task_id] = {
            "status": "running", "progress": 0, "phase": "准备配音",
            "message": "正在准备配音…", "project_name": project_name,
            "total": plan["line_count"], "current": 0, "results": [],
            "plan": plan_summary, "out_dir": out_dir,
        }
    plan["script_path"] = resolved["script_path"]
    threading.Thread(target=_dub_worker,
                     args=(task_id, project_name, plan, out_dir, fmt, episode),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "project_name": project_name, "episode": episode,
                    "line_count": plan["line_count"], "plan": plan_summary,
                    "out_dir": out_dir})


@bp.route('/api/tts/status/<task_id>', methods=['GET'])
def api_tts_status(task_id):
    with dub_lock:
        task = dub_tasks.get(task_id)
        if not task:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        return jsonify(dict(task, success=True, task_id=task_id))


@bp.route('/api/tts/tasks', methods=['GET'])
def api_tts_tasks():
    with dub_lock:
        items = [dict(t, task_id=k) for k, t in dub_tasks.items()]
    items.sort(key=lambda t: t.get("task_id", ""), reverse=True)
    return jsonify({"success": True, "items": items[:50]})


@bp.route('/api/tts/list', methods=['GET'])
def api_tts_list():
    """列出某项目已生成的配音产物（单句 + 合并音轨）"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    out_dir = os.path.join(DUB_DIR, project_name)
    lines, merged = [], []
    if os.path.isdir(out_dir):
        lines_dir = os.path.join(out_dir, "lines")
        for base, bucket in ((lines_dir, lines), (out_dir, merged)):
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                if not name.lower().endswith((".wav", ".mp3", ".flac")):
                    continue
                p = os.path.join(base, name)
                if not os.path.isfile(p):
                    continue
                info = probe_audio_info(p)
                bucket.append({
                    "name": name, "path": os.path.abspath(p),
                    "url": _dub_audio_url(project_name, p),
                    "duration": info.get("duration"), "size_kb": round((info.get("size_bytes") or 0) / 1024, 1),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
                })
    merged.sort(key=lambda x: x["name"], reverse=True)
    vm_path = os.path.join(out_dir, "voice_map.json")
    return jsonify({"success": True, "project_name": project_name, "out_dir": out_dir,
                    "lines": lines, "merged": merged,
                    "voice_map": load_voice_map(vm_path)})


@bp.route('/api/tts/file/<project_name>/<path:filename>')
def api_tts_file(project_name, filename):
    """播放 / 下载配音产物（支持 Range 拖动试听，download=1 触发下载）"""
    project_name = _safe_project(project_name)
    base = os.path.abspath(os.path.join(DUB_DIR, project_name))
    filepath = os.path.abspath(os.path.join(base, filename.replace("\\", "/").lstrip("/")))
    if not filepath.startswith(base + os.sep) or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True,
                     as_attachment=request.args.get('download') == '1')


@bp.route('/api/mix/env', methods=['GET'])
def api_mix_env():
    """音画合成环境自检：ffmpeg/ffprobe + 默认参数 + 最近一次配音清单"""
    env = mix_ffmpeg_check()
    project_name = _safe_project(request.args.get('project_name') or 'project')
    env["default_params"] = MIX_DEFAULT_PARAMS
    env["out_dir"] = mix_out_dir(project_name)
    try:
        mf = _mix_manifest(project_name)
        env["dub_manifest"] = mf["path"]
        env["dub_lines_ok"] = sum(1 for l in (mf["manifest"].get("lines") or [])
                                  if l.get("ok") and l.get("out_path"))
        env["merged_audio"] = mf["manifest"].get("merged_audio") or ""
        env["episode"] = mf["manifest"].get("episode")
    except DubMixError as e:
        env["dub_manifest"] = ""
        env["dub_reason"] = str(e)
    return jsonify(env)


@bp.route('/api/mix/plan', methods=['GET', 'POST'])
def api_mix_plan():
    """合成计划预览（dry-run）：镜头时间轴 + 逐句落点 + 告警，不做 ffmpeg 合成"""
    data = request.json or {} if request.method == 'POST' else dict(request.args)
    try:
        prepared = _mix_prepare(data)
    except (DubMixError, TTSError, UpscaleError) as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({
        "success": True,
        "project_name": prepared["project_name"],
        "video_path": prepared["video_path"], "video_info": prepared["video_info"],
        "script_path": prepared["script_path"], "manifest_path": prepared["manifest_path"],
        "segments_dir": prepared["segments_dir"], "mode": prepared["mode"],
        "episode": prepared["episode"],
        "timeline": prepared["timeline"],
        "entries": prepared["entries"], "line_count": len(prepared["entries"]),
        "coverage_sec": round(sum(e.get("audio_dur") or 0 for e in prepared["entries"]), 3),
        "warnings": prepared["warnings"], "params": prepared["params"],
    })


@bp.route('/api/mix/generate', methods=['POST'])
def api_mix_generate():
    """发起合成（异步任务）：产出自定义命名的带配音成片"""
    data = request.json or {}
    env = mix_ffmpeg_check()
    if not env.get("available"):
        return jsonify({"success": False,
                        "error": "ffmpeg/ffprobe 不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503
    try:
        prepared = _mix_prepare(data)
    except (DubMixError, TTSError, UpscaleError) as e:
        body = {"success": False, "error": str(e)}
        # 「没有可用的配音音频」最常见原因不是没跑 TTS，而是剧本本身没有台词
        # （模型兜底生成的镜头 dialogue=[] ）—— 补一句体检结论，别让用户白跑。
        if "配音" in str(e):
            try:
                _pj = _safe_project(data.get('project_name') or 'project')
                _audit = dialogue_utils.audit_script(_load_script_for(_pj, data.get('episode')))
                if _audit["stats"].get("fallback_shot_count") or _audit["stats"].get(
                        "silent_shot_count"):
                    body["audit"] = _audit["stats"]
                    body["hint"] = "；".join(_audit["warnings"][:2])
                    body["problem_shots"] = _audit["problem_shots"]
            except Exception as ae:  # noqa: BLE001 - 体检失败不影响主错误
                app.logger.debug(f"混音失败时的剧本体检跳过：{ae}")
        return jsonify(body), 400

    project_name = prepared["project_name"]
    out_name = (data.get("out_name") or "").strip()
    if not out_name:
        base = os.path.splitext(os.path.basename(prepared["video_path"]))[0]
        out_name = f"{base}_ep{int(prepared['episode']):02d}_dubbed.mp4"
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    out_name = os.path.basename(out_name.replace("\\", "/"))

    task_id = f"mix_{project_name}_{uuid.uuid4().hex[:12]}"
    with mix_lock:
        # G5：同一视频已有 running 的混音任务 → 复用（匹配 video_path：同项目可能有多个视频）
        _existing_mix = next((tid for tid, t in mix_tasks.items()
                               if t.get("status") == "running"
                               and t.get("video_path") == prepared["video_path"]), None)
        if _existing_mix:
            return jsonify({"success": True, "task_id": _existing_mix, "status": "started",
                            "reused": True, "project_name": project_name, "out_name": out_name,
                            "line_count": len(prepared["entries"]), "mode": prepared["mode"],
                            "warnings": prepared["warnings"],
                            "video_path": prepared["video_path"],
                            "out_dir": mix_out_dir(project_name)})
        mix_tasks[task_id] = {
            "task_id": task_id, "status": "running", "progress": 5,
            "phase": "准备音画对齐", "project_name": project_name,
            "video_path": prepared["video_path"], "out_name": out_name,
            "line_count": len(prepared["entries"]), "mode": prepared["mode"],
            "warnings": prepared["warnings"],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    threading.Thread(target=_mix_worker, args=(task_id, prepared, out_name),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "project_name": project_name, "out_name": out_name,
                    "line_count": len(prepared["entries"]), "mode": prepared["mode"],
                    "warnings": prepared["warnings"],
                    "video_path": prepared["video_path"],
                    "out_dir": mix_out_dir(project_name)})


@bp.route('/api/mix/status/<task_id>', methods=['GET'])
def api_mix_status(task_id):
    with mix_lock:
        task = mix_tasks.get(task_id)
        if not task:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        if task.get("output_path"):
            task = dict(task, url=_mix_audio_url(task["project_name"], task["output_path"]))
    return jsonify({"success": True, "task": task})


@bp.route('/api/mix/tasks', methods=['GET'])
def api_mix_tasks():
    with mix_lock:
        items = [dict(t) for t in mix_tasks.values()]
    items.sort(key=lambda t: t.get("task_id", ""), reverse=True)
    return jsonify({"success": True, "items": items[:50]})


@bp.route('/api/mix/list', methods=['GET'])
def api_mix_list():
    """列出某项目已生成的带配音成片 + 最近合成报告摘要"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    out_dir = os.path.join(DUB_MIX_DIR, project_name)
    items = []
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir), reverse=True):
            if not name.lower().endswith(".mp4"):
                continue
            p = os.path.join(out_dir, name)
            info = probe_video_info(p)
            items.append({
                "name": name, "path": os.path.abspath(p),
                "url": _mix_audio_url(project_name, p),
                "duration": info.get("duration"), "width": info.get("width"),
                "height": info.get("height"),
                "size_mb": round((info.get("size_bytes") or 0) / 1048576, 3),
                "has_audio": info.get("has_audio"), "audio_codec": info.get("audio_codec"),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    reports = []
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir), reverse=True):
            if name.endswith("_mix_report.json"):
                reports.append(os.path.join(out_dir, name))
    return jsonify({"success": True, "project_name": project_name, "out_dir": out_dir,
                    "items": items, "report_path": reports[0] if reports else "",
                    "default_params": MIX_DEFAULT_PARAMS})


@bp.route('/api/mix/file/<project_name>/<path:filename>')
def api_mix_file(project_name, filename):
    """播放 / 下载带配音成片（支持 Range，download=1 触发下载）"""
    project_name = _safe_project(project_name)
    base = os.path.abspath(os.path.join(DUB_MIX_DIR, project_name))
    filepath = os.path.abspath(os.path.join(base, filename.replace("\\", "/").lstrip("/")))
    if not filepath.startswith(base + os.sep) or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True,
                     as_attachment=request.args.get('download') == '1')

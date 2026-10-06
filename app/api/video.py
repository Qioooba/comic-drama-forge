# -*- coding: utf-8 -*-
"""video 域蓝图：视频生成、超分、水印、provider。

2026-10-07 由 ``app/app.py`` 按域拆分而来，**纯搬迁**：路径、方法、报错文案逐字
不变（守门脚本 ``scripts/snapshot_routes.py`` 比对 rule + methods）。
``url_prefix`` 留空，路径直接写死在各 ``@bp.route`` 上。
"""
from __future__ import annotations

from flask import Blueprint

# 共享 runtime 上下文（app 实例 / 212 个模块级 helper / config 常量）。
# __all__ 里显式列出了下划线开头的名字，故这里能整体取到。
from api._shared import *  # noqa: F401,F403

bp = Blueprint("video", __name__)
DOMAIN = "video"

@bp.route('/api/video/retry-shot', methods=['POST'])
@_autopilot_guard
def api_video_retry_shot():
    """单镜视频重跑（同步；只重生成该镜的 mp4）

    支持 mode：reference（默认，分镜图+主角锚点）/ keyframe（首尾帧插值）

    D1（2026-09-23）：与分镜重跑同口径——加 @_autopilot_guard（异常不再泄漏成
    裸 HTML 500）+ 整段关键区进入 gpu_task_gate（与批量视频 worker 互斥，
    避免两个 ComfyUI 任务抢同一张 GPU）。
    """
    with gpu_task_gate.run_gpu_task(
            f"video_retry_{uuid.uuid4().hex[:8]}", "单镜视频重跑"):
        return _video_retry_shot_impl()


@bp.route('/api/video/retry-shots-batch', methods=['POST'])
@_autopilot_guard
def api_video_retry_shots_batch():
    """批量单镜重生成（2026-10-02）：body = {project_name, episode_no?, shot_ids: [...]}

    逐镜**串行**复用 `_video_retry_shot_impl` 的完整链路（切段 / 提示词预检 /
    生成 / 质检 / 落盘 / manifest 回写），GPU 闸门包住**整个批次**（批内不再嵌套
    加锁 —— impl 本身无闸门，闸门在单镜路由壳上）。单镜失败不中断批次；
    上限 12 镜防误触全量重跑。同步返回逐镜结果（前端逐条展示）。
    """
    data = request.json or {}
    ids = data.get('shot_ids')
    if not isinstance(ids, list) or not [s for s in ids if str(s).strip()]:
        return jsonify({"success": False,
                        "error": "shot_ids 必须是非空数组（如 [\"shot_03\", \"shot_07\"]）"}), 400
    ids = [str(s).strip() for s in ids if str(s).strip()][:12]
    base_body = {k: v for k, v in data.items() if k != 'shot_ids'}
    results = []
    with gpu_task_gate.run_gpu_task(
            f"video_retry_batch_{uuid.uuid4().hex[:8]}", "批量单镜重生成"):
        for sid in ids:
            body = dict(base_body)
            body['shot_id'] = sid
            try:
                with app.test_request_context(json=body):
                    resp = _video_retry_shot_impl()
                    payload = (resp[0].get_json() if isinstance(resp, tuple)
                               else resp.get_json())
                    status = resp[1] if isinstance(resp, tuple) else resp.status_code
                    results.append({"shot_id": sid, "http_status": status,
                                    **(payload if isinstance(payload, dict) else {})})
            except Exception as e:  # noqa: BLE001  单镜失败不断批次
                app.logger.warning("[批量重生成] 镜头 %s 失败：%s", sid, e)
                results.append({"shot_id": sid, "success": False, "error": str(e)})
    ok_n = sum(1 for r in results if r.get("success"))
    app.logger.info("[批量重生成] 完成：%d/%d 镜成功", ok_n, len(results))
    return jsonify({"success": ok_n > 0, "total": len(results), "ok_count": ok_n,
                    "results": results})


# ==========================================================================
# P1-4 / P2-4  引擎 Provider 与插件注册表（透明化，只读为主）
# ==========================================================================

@bp.route('/api/providers', methods=['GET'])
def api_providers():
    """列出各环节可用引擎与当前生效实现

    refresh=1 时绕过可用性探测缓存重新探测（可用性探测会真连 ComfyUI / TTS，
    因此默认走 TTL 缓存，避免前端刷新把列表接口拖到数秒）。
    """
    force = str(request.args.get('refresh') or '').strip() in ('1', 'true', 'yes')
    try:
        data = providers.catalog(force=force)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"引擎目录读取失败：{e}"}), 500
    env_keys = {kind: info.get("env_key") for kind, info in (data or {}).items()}
    return jsonify({"success": True, "kinds": data, "env_keys": env_keys})


@bp.route('/api/providers/select', methods=['POST'])
def api_providers_select():
    """切换某环节引擎（写入运行时环境变量；持久化请改 .env 的 MJSCXT_PROVIDER_*）"""
    data = request.json or {}
    kind = str(data.get('kind') or '').strip().lower()
    name = str(data.get('name') or '').strip()
    if kind not in ('image', 'video', 'tts') or not name:
        return jsonify({"success": False, "error": "参数非法（kind ∈ image/video/tts）"}), 400
    try:
        info = providers.set_active(kind, name)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "kind": kind, **info})


@bp.route('/api/generation/status/<task_id>', methods=['GET'])
def api_generation_status(task_id):
    with lock:
        # P2-14（A-21）：锁内深拷贝快照再出锁。旧实现在锁外 jsonify 会读到
        # 「progress 已 100 但 results 只有 3 条」这类半更新快照（与写端点
        # 在锁内 .append/.update 竞态）。深拷贝把一致性窗口收敛到持锁段内。
        state = copy.deepcopy(generation_state.get(task_id, {}))
    return jsonify(state)


@bp.route('/api/videos/generate', methods=['POST'])
def api_generate_videos():
    data = _body()
    # P2-T2：写盘路由统一走 _project_or_400（同 api_generate_storyboards 口径）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    shots = data.get('shots', [])
    character_refs = data.get('character_refs', [])
    scene_refs = data.get('scene_refs', [])
    storyboards = data.get('storyboards', {}) or {}   # {shot_id: /api/storyboards/file/... 或本地路径}
    use_storyboard = data.get('use_storyboard', True)
    # 视频生成模式：
    #   per_shot（默认）= 逐镜头提交，工作流段数=1（一个分镜一段）
    #   episode        = 整集一次提交，工作流段数=该集分镜数（如第 4 集 22 段）
    #   keyframe       = 逐镜头提交，参考图槽位改为 [首帧=分镜图, 尾帧]（P1-2 关键帧驱动）
    # ⭐ 2026-09-29：模式优先级 = 本次请求显式 mode → **项目级设定**（新建项目时
    #    用户选择，见 project_store.video_mode）→ per_shot。
    #    历史缺陷：前端「整集生成视频」按钮固定发 mode=episode，而这里缺省 per_shot，
    #    项目级又没有任何选择入口 —— 同一件事三处口径，用户选了也不生效。
    #    显式传了非法值仍然 400（错误必须可见），缺省才走项目设定回落。
    _mode_req = str(data.get('mode') or '').strip().lower()
    if _mode_req and _mode_req not in ('per_shot', 'episode', 'keyframe'):
        return jsonify({"error": f"mode 参数非法: {_mode_req}"
                                 f"（仅支持 per_shot / episode / keyframe）"}), 400
    # 2026-10-01：只保留整集一次生成（per_shot / keyframe 废弃）
    mode = _mode_req or project_store.video_mode(project_name) or 'episode'
    timeout_per_segment = int(data.get('timeout_per_segment') or 900)
    episode_tag = str(data.get('episode_tag') or '').strip()
    # 跨镜链式：上一镜尾帧 = 下一镜首帧（auto / always / off，默认取 KEYFRAME_CHAIN_MODE）
    chain_mode = keyframe.norm_chain_mode(
        data.get('chain_mode') or KEYFRAME_CHAIN_MODE)

    # 集号：作为入口幂等键的一部分（见下），也写进 generation_state 供状态回显。
    # 裸 int(episode_no) 会抛 —— 历史前端可能传 "" / null / "2"，统一走 _ep_of_script 同口径的容错。
    # ⚠️ 提前到这里解析：空 shots 时要用它读剧本兜底，后面幂等键 / 状态 / worker 全部复用同一个值。
    _vid_ep = data.get('episode_no')
    try:
        _vid_ep = int(_vid_ep) if str(_vid_ep or "").strip() else 1
    except (TypeError, ValueError):
        _vid_ep = 1

    # ⚠️ 2026-09-28 修复：前端「整集生成视频」按钮（工程台 handleGenerateEpisode）只发
    #    {project_name, episode_no}，从不带 shots —— 旧代码在此直接 400「没有镜头数据」，
    #    按钮永久失败（client.ts 注释承诺的「后端按剧本兜底」从未实现）。
    #    现在：shots 为空时按本集剧本兜底（剧本里的 shots 就是生成视频所需的镜头表）。
    #    ⚠️ 兜底只发生在空 shots 时，有 shots 的调用路径（流水线 / 单镜重跑）行为**完全不变**。
    if not shots:
        _scr_fb = _load_script_for(project_name, _vid_ep)
        if not isinstance(_scr_fb, dict):
            _scr_fb = {}
        shots = _scr_fb.get("shots") or []
        # 参考图同理兜底：只在调用方没显式传时补剧本里已判定的角色 / 场景。
        # （物品的权威来源是剧本、由 _video_generate_worker_body 自行兜底；此处不覆盖显式传参）
        if not character_refs:
            character_refs = _scr_fb.get("characters") or []
        if not scene_refs:
            scene_refs = _scr_fb.get("scenes") or []
    if not shots:
        return jsonify({"error": "没有镜头数据"}), 400
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g

    # ---- 台词闸门（2026-10-07 fail-closed）----
    # 为什么必须在这里拦：本系统的 dialogue 是**唯一人声来源**（旁白 narration 已关闭）。
    # 一旦整集 0 台词，这次出片必然是完全无声的成片，而 GPU 时间已经花掉、
    # 画面也已经生成完了 —— 用户拿到手才发现「没声音」，返工成本最高。
    #
    # 实测《测灵根》第 1 集：模型分镜失败（只返回 reasoning_content 没有正文）→
    # 整集 21 镜走原文兜底 → dialogue 全空 → 流程一路「成功」走到出片。
    # 剧本体检 audit_script 早就报了警，但它只是**告警**、从不阻断，
    # 于是这份「未经模型加工」的剧本畅通无阻地跑完了全链路。
    #
    # 覆盖开关 allow_silent=true：用户明确知道是静音成片（例如只要画面做素材、
    # 或后续单独配音）时显式放行。**默认拒绝** —— 缺台词必须是有意识的决定。
    if not data.get("allow_silent"):
        try:
            _scr_guard = _load_script_for(project_name, _vid_ep)
        except Exception as ge:      # noqa: BLE001 - 读剧本失败不该阻断出片
            app.logger.warning("台词闸门读取剧本失败（放行）：project=%s ep=%s err=%s",
                               project_name, _vid_ep, ge)
            _scr_guard = None
        if isinstance(_scr_guard, dict) and (_scr_guard.get("shots") or []):
            _aud = dialogue_utils.audit_script(_scr_guard)
            if not _aud["ok"]:
                _hint = "；".join(_aud["warnings"][:3])
                app.logger.error(
                    "出片被台词闸门拦下：project=%s ep=%s stats=%s | %s",
                    project_name, _vid_ep, _aud["stats"], _hint)
                return jsonify({
                    "success": False,
                    "error": "该集剧本没有可朗读台词，出片会得到完全无声的成片，已阻止提交",
                    "blocked_by": "dialogue_gate",
                    "audit": _aud["stats"],
                    "warnings": _aud["warnings"],
                    "problem_shots": _aud["problem_shots"],
                    "hint": _hint,
                    "can_override": True,
                    "override_flag": "allow_silent",
                }), 409

    # ⑥ 视频链路自动引用剧本自动判定的镜头时长（缺 duration 时按项目配置兜底）
    episode_stats = _episode_schema_defaults(project_name, shots)

    task_id = f"video_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        _prune_task_registry(generation_state)
        # G5 + B-11 P1-8：同项目**同集**已有 running 的视频任务 → 复用。
        # ⚠️ 修复（2026-09-25）：旧键只匹配 project_name + step=="video"，**不含集号** ——
        #    用户在第 2 集点「生成视频」，若第 1 集的视频任务还在跑，会被直接吞掉：
        #    返回 reused=True 且 task_id 指向第 1 集的任务，第 2 集永远不生成，
        #    而界面显示「已开始」。分镜侧早已加 episode_no（见 api_generate_storyboards），
        #    视频侧漏了 —— 两条链路口径不一致。
        # 用 `==` 精确比集号（而非 `!=` 排除），历史任务无 episode_no 字段时按 1 处理，
        # 与 `_ep_dir` 的「第 1 集平铺」口径一致。
        def _st_ep(st):
            try:
                return int(st.get("episode_no") or 1)
            except (TypeError, ValueError):
                return 1

        _existing_vid = next((tid for tid, st in generation_state.items()
                              if st.get("status") == "running"
                              and st.get("project_name") == project_name
                              and st.get("step") == "video"
                              and _st_ep(st) == _vid_ep), None)
        if _existing_vid:
            return jsonify({"success": True, "task_id": _existing_vid, "status": "started",
                            "reused": True, "total": len(shots),
                            "mode": mode, "project_name": project_name,
                            "episode_no": _vid_ep})
        generation_state[task_id] = {
            "status": "running", "progress": 0,
            "total": len(shots), "current": 0, "results": [],
            "phase": "视频生成", "qc": _qc_brief("video"),
            "project_name": project_name, "step": "video",
            "episode_no": _vid_ep,
            "episode_stats": episode_stats,
        }

    # 抽取 worker 时这里被截断了：既没启动线程也没有 return，
    # 导致 POST /api/videos/generate 抛 "did not return a valid response" (500)。
    # 现在把「启动后台线程 + 返回 task_id」补回路由本身（worker 只负责干活）。
    # B-01 P1-12：GPU 并发闸门
    def _video_generate_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, f"视频生成({mode})"):
            _video_generate_worker(
                task_id, project_name, shots, character_refs, scene_refs,
                storyboards, use_storyboard, mode, timeout_per_segment,
                # 传规范化后的 _vid_ep（与上面幂等键 / 状态里的集号同源），
                # 而不是原始 data['episode_no'] —— 否则 "" / None 会让落盘目录与状态不一致。
                episode_tag, _vid_ep,
                chain_mode=chain_mode,
                style=(data.get('style') or _project_style(project_name)),
                overwrite=bool(data.get('overwrite')),
                build_only=bool(data.get('build_only')),
                only_scenes=(data.get('only_scenes')
                             if isinstance(data.get('only_scenes'), list) else None))
    thread = threading.Thread(target=_video_generate_worker_gated, daemon=True)
    thread.daemon = True
    thread.start()

    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "total": len(shots), "mode": mode})


# ===== 步骤6：成片合成 =====

@bp.route('/api/final/video', methods=['POST'])
def api_generate_final():
    data = _body()
    # P2-T2：写盘路由统一走 _project_or_400（成片合成需定位项目内剧本，缺省无合理语义）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    script_path = data.get('script_path', '')

    # P0-4：剧本路径必须先落在项目输出目录内（与 project_store.bind_script 同口径），
    # 越界（如 C:/Windows/... 或项目外路径）直接拒读，避免被 index.json 里被污染的
    # 绝对路径拖出目录读走任意文件。
    if not script_path or not project_store.is_path_inside_output(script_path):
        return jsonify({"error": "剧本路径必须在项目输出目录内（output/），越界路径已拒读"}), 400
    if not os.path.exists(script_path):
        return jsonify({"error": "剧本文件不存在"}), 400

    try:
        # 集号与剧本一起解析：合成必须知道是第几集（审计 S4 —— 旧代码合成完才读集号，
        # 而合成函数压根没有集号入参，于是第 2 集及以后合成的是第 1 集的片段）
        ep_no = 0
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                _sc = json.load(f) or {}
            ep_no = int(_sc.get('episode_no')
                        or (_sc.get('metadata') or {}).get('episode_no') or 0)
        except Exception:  # noqa: BLE001 - 剧本读不到就退回第 1 集
            ep_no = 0
        output = video_processor.generate_final_video(script_path, project_name, ep_no or 1)
        if not output:
            return jsonify({"error": f"没有可合并的视频片段（第 {ep_no or 1} 集），"
                                     "请先完成步骤5的视频生成"}), 400
        filename = os.path.basename(output)
        # URL 按「相对 FINAL_DIR 的路径」拼，避免成片落在项目子目录时 404
        try:
            rel_path = os.path.relpath(output, FINAL_DIR).replace(os.sep, "/")
        except ValueError:
            rel_path = f"{project_name}/{filename}"
        resp = {
            "success": True,
            "output_path": output,                       # 保留原字段（本地绝对路径）
            "episode_no": ep_no or 1,
            "filename": filename,
            "url": f"/api/final/{rel_path}"              # 前端可直接播放/下载的 URL
        }
        # 整集成片落盘 → 自动登记进「成品验收」队列（用户只需看这里）
        reg = register_final_deliverable(
            project_name, ep_no or 1, output,
            meta={"source": "final_video", "script": os.path.basename(script_path)})
        resp["deliverable"] = {"registered": bool(reg.get("registered")),
                               "reason": reg.get("reason") or "",
                               "episode_no": ep_no or 1}
        # 视频水印（默认关闭；开启后额外产出一份带水印成片，不影响上面的无水印成片）
        resp["watermark"] = _wm_apply_to_final(output, project_name)
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route('/api/videos/<path:filename>')
def api_video_file(filename):
    """提供视频文件访问"""
    return _serve_safe(VIDEOS_DIR, filename, conditional=True)


@bp.route('/api/final/<path:filename>')
def api_final_file(filename):
    """提供最终成片文件访问（新增：使成片可在页面内联播放/下载）"""
    # conditional=True 支持 Range 请求，视频可拖动进度条
    return _serve_safe(FINAL_DIR, filename, conditional=True,
                       as_attachment=request.args.get('download') == '1')


@bp.route('/api/watermark/config', methods=['GET'])
def api_watermark_config_get():
    cfg = _wm_load_cfg()
    return jsonify({"success": True, "config": cfg, "view": _wm_view(cfg),
                    "config_path": os.path.abspath(WATERMARK_CONFIG_PATH),
                    "output_dir": os.path.abspath(WATERMARK_DIR),
                    "message": "视频水印默认关闭；开启并保存后，成片会自动追加一份带水印版本"})


@bp.route('/api/watermark/config', methods=['POST'])
def api_watermark_config_save():
    data = request.json or {}
    cfg = video_watermark.save_config(WATERMARK_CONFIG_PATH, data)
    view = _wm_view(cfg)
    warning = ""
    if cfg.get("enabled") and not view.get("ready"):
        warning = {
            "disabled": "禁用",
            "no_ffmpeg": "未找到 ffmpeg，无法烧写水印",
            "no_font": "未找到可用字体，文字水印无法生效",
            "no_image": "图片水印文件不存在",
        }.get(view.get("ready_state"), "")
    return jsonify({"success": True, "config": cfg, "view": view,
                    "config_path": os.path.abspath(WATERMARK_CONFIG_PATH),
                    "warning": warning,
                    "message": ("视频水印已开启（模式：%s）" % view["mode_label"]) if cfg.get("enabled")
                               else "视频水印已关闭（成片不会带水印）"})


@bp.route('/api/watermark/apply', methods=['POST'])
def api_watermark_apply():
    """对指定视频烧写水印（手动单段验证用）。body: video_path / project_name / config(临时覆盖)"""
    data = request.json or {}
    src = str(data.get("video_path") or "").strip()
    project_name = _safe_project(data.get("project_name") or "watermark")
    cfg = video_watermark.load_config(WATERMARK_CONFIG_PATH)
    if isinstance(data.get("config"), dict):
        cfg = video_watermark.normalize(data["config"], base=cfg)
    if not src:
        return jsonify({"success": False, "error": "缺少 video_path"}), 400
    if not os.path.isfile(src):
        return jsonify({"success": False, "error": f"视频不存在：{src}"}), 400
    if data.get("enabled") is not None:
        cfg["enabled"] = bool(data.get("enabled"))
    res = video_watermark.apply_watermark(src, cfg=cfg,
                                          out_dir=os.path.join(WATERMARK_DIR, project_name))
    out_path = res.get("out_path") or ""
    if res.get("skipped"):
        return jsonify({"success": True, "skipped": True, "config": cfg,
                        "message": "水印未开启，未产出带水印文件"})
    if not res.get("ok"):
        return jsonify({"success": False, "config": cfg, "error": res.get("error")}), 500
    return jsonify({"success": True, "config": cfg, "output_path": out_path,
                    "url": f"/api/watermark/file/{project_name}/{os.path.basename(out_path)}",
                    "mode": cfg.get("mode"),
                    "mode_label": video_watermark.MODES_LABEL.get(cfg.get("mode")),
                    "elapsed": res.get("elapsed"),
                    "message": "水印已烧写"})


@bp.route('/api/watermark/list/<path:project_name>', methods=['GET'])
def api_watermark_list(project_name):
    folder = project_store.project_dirs(WATERMARK_DIR, project_name)
    files = []
    for d in folder:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith((".mp4", ".mov", ".mkv")):
                    files.append(os.path.join(d, f))
    return jsonify({"success": True, "files": files, "count": len(files),
                    "output_dir": os.path.abspath(WATERMARK_DIR)})


@bp.route('/api/watermark/file/<path:filename>', methods=['GET'])
def api_watermark_file(filename):
    return _serve_safe(WATERMARK_DIR, filename, conditional=True,
                       as_attachment=request.args.get('download') == '1')


@bp.route('/api/upscale/env', methods=['GET'])
def api_upscale_env():
    """超分环境自检：ComfyUI 在线、节点、FlashVSR 模型文件、TE-Speed 加速模板是否就位"""
    env = upscale_env_check()
    env["defaults"] = dict(TE_UPSCALE_DEFAULT_PARAMS) if env.get("te_ready") \
        else dict(UPSCALE_DEFAULT_PARAMS)
    env["legacy_defaults"] = dict(UPSCALE_DEFAULT_PARAMS)
    env["te_defaults"] = dict(TE_UPSCALE_DEFAULT_PARAMS)
    env["te_lowvram"] = dict(TE_UPSCALE_LOWVRAM_PARAMS)
    env["default_engine"] = UPSCALE_ENGINE
    return jsonify(env)


@bp.route('/api/workflows/integrity', methods=['GET'])
def api_workflow_integrity():
    """P0-4：核对 workflows/ 多副本 hash，返回实际读取路径与漂移告警。"""
    try:
        return jsonify(workflow_integrity.check())
    except Exception as e:  # noqa: BLE001  状态接口也不应打挂
        return jsonify({"success": False, "ok": False,
                        "warnings": [f"核对异常：{e}"]}), 200


@bp.route('/api/upscale/sources', methods=['GET'])
def api_upscale_sources():
    """列出可作为超分输入的候选视频（成片 final / 片段 videos / 已有超分产物 / ComfyUI 侧产出）"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    dirs = [("成片", FINAL_DIR), ("视频片段", VIDEOS_DIR), ("超分产物", UPSCALE_DIR)]
    items = []
    prefix_by_base = {os.path.abspath(b): p for p, b in UPSCALE_URL_PREFIXES}
    for label, base in dirs:
        d = os.path.join(base, project_name)
        if not os.path.isdir(d):
            continue
        url_prefix = prefix_by_base.get(os.path.abspath(base), "")
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            items.append({
                "kind": label,
                "name": name,
                "path": os.path.abspath(p),
                "url": f"{url_prefix}{project_name}/{name}",
                "size_mb": round(os.path.getsize(p) / 1048576, 2),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    # ComfyUI 侧产出（项目真实生成的镜头视频默认落在 ComfyUI/output 各子目录，
    # 如 video / v5video / 自定义工作流目录；成片前也能直接选中超分）
    comfy_root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    comfy_dirs = [(comfy_root, "ComfyUI")]
    if os.path.isdir(comfy_root):
        try:
            for name in sorted(os.listdir(comfy_root)):
                sub = os.path.join(comfy_root, name)
                if os.path.isdir(sub):
                    comfy_dirs.append((sub, f"ComfyUI/{name}"))
        except OSError as e:
            app.logger.debug("扫描 ComfyUI 子目录失败（忽略）：%s", e)
    for base, label in comfy_dirs:
        if not os.path.isdir(base):
            continue
        subfolder = os.path.relpath(base, comfy_root).replace(os.sep, "/")
        if subfolder == ".":
            subfolder = ""
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                continue
            p = os.path.join(base, name)
            if not os.path.isfile(p):
                continue
            items.append({
                "kind": label,
                "name": name,
                "path": os.path.abspath(p),
                "url": _comfy_view_url(name, subfolder),
                "size_mb": round(os.path.getsize(p) / 1048576, 2),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    items.sort(key=lambda it: (it["kind"] not in ("成片", "视频片段"), it["kind"], it["name"]))
    return jsonify({"success": True, "project_name": project_name, "items": items[:300]})


@bp.route('/api/upscale/comfyview')
def api_upscale_comfyview():
    """代理播放 ComfyUI 侧产出视频：302 重定向到 ComfyUI /view（仅允许 output 目录内文件）"""
    filename = request.args.get('filename') or ''
    subfolder = request.args.get('subfolder') or ''
    try:
        _upscale_resolve_comfyview({'filename': filename, 'subfolder': subfolder})
    except UpscaleError as e:
        app.logger.warning(f"comfyview 拒绝访问: {e}")
        abort(404)
    from urllib.parse import urlencode
    q = urlencode({'filename': filename, 'subfolder': subfolder, 'type': 'output'})
    return redirect(f"{COMFYUI_URL.rstrip('/')}/view?{q}")


@bp.route('/api/upscale/list', methods=['GET'])
def api_upscale_list():
    """列出某项目已生成的超分产物"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    d = os.path.join(UPSCALE_DIR, project_name)
    items = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d), reverse=True):
            if not name.lower().endswith(".mp4"):
                continue
            p = os.path.join(d, name)
            items.append({
                "name": name, "path": p,
                "url": f"/api/upscale/{project_name}/{name}",
                "size_mb": round(os.path.getsize(p) / 1048576, 2),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    return jsonify({"success": True, "project_name": project_name, "items": items})


@bp.route('/api/upscale/video', methods=['POST'])
def api_upscale_video():
    """发起视频超分（异步任务）：body 支持 project_name / video_path|video_url / scale / mode 等"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（前端契约必填 project_name，
    # 缺省只会静默写进共享 'project' 命名空间造成串项目）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err

    try:
        video_path = _upscale_resolve_video(data)
    except UpscaleError as e:
        return jsonify({"error": str(e)}), 400

    before = probe_video_info(video_path)
    if not before.get("ok"):
        return jsonify({"error": f"输入视频无法解析: {before.get('error')}"}), 400

    env = upscale_env_check()
    if not env.get("available"):
        return jsonify({"error": "超分环境不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    try:
        scale = int(data.get('scale') or TE_UPSCALE_DEFAULT_PARAMS.get('scale', 2))
    except (TypeError, ValueError):
        return jsonify({"error": "scale 参数非法，仅支持 2 / 3 / 4"}), 400
    if scale not in (2, 3, 4):
        return jsonify({"error": "倍率仅支持 2 / 3 / 4（FlashVSR 支持范围）"}), 400

    # 引擎：默认 TE-Speed-flashVSR 加速链路，可显式指定 legacy-flashvsr
    engine = str(data.get('engine') or UPSCALE_ENGINE or "te-speed-flashvsr").strip().lower()
    if engine not in ("te-speed-flashvsr", "legacy-flashvsr"):
        return jsonify({"error": "engine 仅支持 te-speed-flashvsr / legacy-flashvsr"}), 400
    if engine == "te-speed-flashvsr" and not env.get("te_ready"):
        return jsonify({"error": "TE-Speed 加速链路不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    # 参数：TE-Speed 加速链路（sparse_sage2 + 分块）与旧链路字段都接受，按引擎生效
    params = {k: data.get(k) for k in (
        # TE-Speed-flashVSR 加速参数
        "mode", "precision", "device", "quality_profile", "intensity",
        "spatial_strategy", "memory_policy", "attention_backend",
        "attention_budget", "kv_retention", "local_radius",
        "max_tile_edge", "blend_overlap", "preprocess_batch",
        "quality_value", "color_fix", "frame_load_cap", "skip_first_frames", "free_vram",
        "seed", "timeout",
        # ⚠️ 音轨旁路开关：TE-Speed 链路默认 attach_audio=False，对「成片」超分时
        # 不显式打开会把已合成的配音丢掉，产出无声视频。
        "attach_audio",
        # 旧 FlashVSR 链路参数（回退时生效）
        "tile_size", "tile_overlap", "tiled_vae", "tiled_dit", "unload_dit",
        "sparse_ratio", "kv_ratio", "local_range", "attention_mode", "force_offload",
    ) if data.get(k) is not None}
    params["scale"] = scale
    params["engine"] = engine

    task_id = f"upscale_{int(time.time() * 1000)}"
    with upscale_lock:
        upscale_tasks[task_id] = {
            "task_id": task_id, "status": "pending", "progress": 0,
            "message": "任务已创建", "project_name": project_name,
            "input_path": video_path, "input_probe": before,
            "scale": scale, "engine": engine, "params": params, "created_at": time.time(),
        }
    # B-01 P1-12：GPU 并发闸门
    def _upscale_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, f"超分({engine} {scale}x)"):
            _upscale_worker(task_id, video_path, project_name, params)
    threading.Thread(target=_upscale_worker_gated, daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "input_path": video_path,
                    "input_probe": before, "scale": scale, "engine": engine,
                    "params": params})


@bp.route('/api/upscale/status/<task_id>', methods=['GET'])
def api_upscale_status(task_id):
    """查询超分任务进度/结果"""
    with upscale_lock:
        task = upscale_tasks.get(task_id)
        task = dict(task) if task else None
    if not task:
        return jsonify({"error": f"未找到超分任务 {task_id}"}), 404
    return jsonify({"success": True, **task})


@bp.route('/api/upscale/tasks', methods=['GET'])
def api_upscale_tasks():
    """列出全部超分任务（按创建时间倒序）"""
    with upscale_lock:
        items = [dict(t) for t in upscale_tasks.values()]
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return jsonify({"success": True, "items": items[:50]})


@bp.route('/api/upscale/<path:filename>')
def api_upscale_file(filename):
    """提供超分产物访问（支持 Range 拖动进度条与下载）"""
    return _serve_safe(UPSCALE_DIR, filename, conditional=True,
                      as_attachment=request.args.get('download') == '1')


@bp.route('/api/videos/preview/approve', methods=['POST'])
def api_video_preview_approve():
    """批准某集预演 → 之后重新生成本集即走**正式**生产（两级生产第二阶段）。

    批准会绑定该预演产物的哈希（见 preview_gate.approve）：预演被重出一版，
    旧批准自动失效，避免「批的是上一版预演」。
    """
    data = request.json or {}
    project, err = _project_or_400(
        data.get('project') or data.get('project_name') or '', field_name="project")
    if err is not None:
        return err
    ep = data.get('episode_no') or 1
    path = str(data.get('path') or '')
    if not path:
        # 没传路径 → 在该集视频目录里找预演产物（文件名带 PREVIEW_MARK）
        try:
            _d = _ep_dir(os.path.join(VIDEOS_DIR, project), ep)
            _cand = [os.path.join(_d, f) for f in sorted(os.listdir(_d))
                     if preview_gate.is_preview_path(f)] if os.path.isdir(_d) else []
            path = _cand[-1] if _cand else ''
        except Exception as e:                                       # noqa: BLE001
            app.logger.warning("查找预演产物失败：%s", e)
    if not path or not os.path.isfile(path):
        return jsonify({"success": False,
                        "error": "未找到该集的预演产物（请先生成预演）"}), 404
    state = preview_gate.approve(project, ep, path, note=str(data.get('note') or ''))
    app.logger.info("[预演] 已批准：%s 第%s集 → %s", project, ep, os.path.basename(path))
    return jsonify({"success": True, "preview": state, "path": path})

# -*- coding: utf-8 -*-
"""storyboard 域蓝图：分镜、九宫格、关键帧、剧集进度。

2026-10-07 由 ``app/app.py`` 按域拆分而来，**纯搬迁**：路径、方法、报错文案逐字
不变（守门脚本 ``scripts/snapshot_routes.py`` 比对 rule + methods）。
``url_prefix`` 留空，路径直接写死在各 ``@bp.route`` 上。
"""
from __future__ import annotations

from flask import Blueprint

# 共享 runtime 上下文（app 实例 / 212 个模块级 helper / config 常量）。
# __all__ 里显式列出了下划线开头的名字，故这里能整体取到。
from api._shared import *  # noqa: F401,F403

# ``_facts_identity`` 不在 ``_shared.__all__`` 里（星号导入拿不到），故显式取一次。
# 复用共享实现而不是在这里重写一遍：整集级 shot_key 口径（EPISODE_SCOPE_SHOT_KEY）
# 与「组装失败即返回 {}、绝不阻断生成」这条纪律只有一份。
from api._shared import _facts_identity

bp = Blueprint("storyboard", __name__)
DOMAIN = "storyboard"

@bp.route('/api/keyframes/plan', methods=['GET'])
def api_keyframes_plan():
    """关键帧尾帧生成预检（不调用模型）"""
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(request.args.get('project_name') or '')
    if err is not None:
        return err
    episode_no = request.args.get('episode_no')
    script = _load_script_for(project, episode_no)
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "该剧本没有镜头数据",
                        "project": project}), 404
    kf_dir = _keyframes_dir(project, episode_no)
    sb_map = _keyframe_sb_map(project, script, episode_no=episode_no)
    chain_mode = keyframe.norm_chain_mode(
        request.args.get('chain_mode') or KEYFRAME_CHAIN_MODE)
    plan = keyframe.plan_keyframes(shots, sb_map, kf_dir,
                                   only_missing=(request.args.get('only_missing', '1') != '0'),
                                   chain_mode=chain_mode)
    return jsonify({"success": True, "project": project,
                    "shot_count": len(shots),
                    "keyframes_dir": kf_dir,
                    "chain_mode": chain_mode,
                    "start_frames_ready": sum(1 for p in plan if p["has_start"]),
                    "end_frames_ready": sum(1 for p in plan if p["has_end"]),
                    "chained_count": sum(1 for p in plan if p.get("chained")),
                    "to_generate": sum(1 for p in plan if p["need_gen"]),
                    "plan": plan})


@bp.route('/api/keyframes/generate', methods=['POST'])
def api_keyframes_generate():
    """批量生成尾帧（Qwen Edit，以分镜图为首帧参考）——后台任务 + 断点续跑"""
    # ⚠️ 故意不设 AI 门禁：尾帧提示词在 shot/剧本数据里（上游产出），本步只做 Qwen Edit
    # 图生图 + 质检，不读 AI 凭证。门禁挂这里会误伤「有存量分镜、但 AI key 未配」的续跑。
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no')) if not data.get('shots') \
        else {"shots": data.get('shots') or []}
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "没有镜头数据"}), 400
    _g = _style_aspect_guard(project)
    if _g is not None:
        return _g
    _kf_ep = _ep_of_script(script, data.get('episode_no'))
    kf_dir = _keyframes_dir(project, _kf_ep)

    def _kf_facts(sid):
        """尾帧「生产事实」身份工厂：本层才看得到真实的 project / episode / 镜号。

        尾帧是**逐镜**产物，故身份必须逐镜给（一个工厂而不是一个 dict）。
        shot_id 原样传（与 ``_facts_identity`` 的既有 7 处调用同口径，不另做归一化）。
        组装失败由 ``_facts_identity`` 内部吞掉并返回 {} → 该镜不登记，生成不受影响。
        """
        return _facts_identity(project, _kf_ep, sid)
    sb_map = _keyframe_sb_map(project, script, data.get('storyboards'), episode_no=_kf_ep)
    only_missing = bool(data.get('only_missing', True))
    seed = data.get('seed')
    timeout = int(data.get('timeout') or 900)
    chain_mode = keyframe.norm_chain_mode(
        data.get('chain_mode') or KEYFRAME_CHAIN_MODE)

    task_id = f"keyframe_{project}_{int(time.time())}"
    plan = keyframe.plan_keyframes(shots, sb_map, kf_dir, only_missing=only_missing,
                                   chain_mode=chain_mode)
    # 尾帧质检（可选）：默认跟随图片质检开关，不达标换 seed 重画，仍不通过则本镜判失败
    _kf_verify, _kf_vretries = _keyframe_qc_verifier(project, script=script)
    # 尾帧提示词预检（生成前质检）：能自愈的先自愈再出图；成批生成不阻断
    # （与资产 / 整集视频同一取舍 —— 为一条提示词打断整批代价过大）
    _kf_pre, _kf_pre_on = _keyframe_prompt_preflight(project)
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "关键帧尾帧生成",
            "total": len([p for p in plan if p["need_gen"]]), "current": 0,
            "results": [], "keyframes_dir": kf_dir,
        }
    try:
        task_store.get_store(TASKS_DB_PATH).create(
            kind="keyframe", project=project, label=f"{project} 尾帧生成",
            total=len([p for p in plan if p["need_gen"]]), task_id=task_id)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"任务库登记失败（不影响生成）：{e}")

    def _kf_worker():
        store = None
        try:
            store = task_store.get_store(TASKS_DB_PATH)
        except Exception:  # noqa: BLE001
            store = None
        if store:
            try:
                store.start(task_id)     # 登记开始时间（否则任务列表「开始」为空）
            except Exception as e:  # noqa: BLE001
                app.logger.debug("任务库 start 登记失败（不影响执行）：%s", e)

        def _progress(done, total, item):
            with lock:
                st = generation_state.get(task_id) or {}
                st.update({"current": done, "total": total,
                           "progress": int(done / max(total, 1) * 100)})
                st.setdefault("results", []).append(item)
            if store:
                # 单元级进度：尾帧产物落盘即视为该镜完成（断点续跑判据同源）
                try:
                    store.set_progress(task_id, progress=int(done / max(total, 1) * 100))
                    store.mark_unit(task_id, f"shot_{item.get('seq') or item.get('shot_id')}",
                                    task_store.ST_DONE if item.get("ok") else task_store.ST_FAILED,
                                    result_path=item.get("path") or "",
                                    error=item.get("error") or "")
                except Exception as e:  # noqa: BLE001
                    app.logger.debug("任务库单元进度写入失败（忽略）：%s", e)

        try:
            report = keyframe.generate_keyframes(
                shots, sb_map, kf_dir, seed=seed, timeout=timeout,
                only_missing=only_missing, progress_cb=_progress,
                chain_mode=chain_mode, verify_cb=_kf_verify,
                max_verify_retries=_kf_vretries, preflight_cb=_kf_pre,
                client=comfyui_client,  # S-04：注入全局 ComfyUIClient 实例（复用连接/共享状态）
                qc_stop_cb=_qc_retry_hopeless,  # G1：尾帧连续两次缺陷相同 → 止损
                recall_cb=_keyframe_recall_cb(project),  # T03a：尾帧质检重试召回历史教训
                project_name=project,  # A-16：尾帧达标落盘时写旁路 .meta.json 用
                # 2026-10-07：尾帧逐镜登记为 GenerationIntent + MediaVersion。
                # 身份只在这里问 app 层要（_kf_facts），keyframe.py 不从路径反推。
                facts_cb=_kf_facts,
            )
            with lock:
                generation_state[task_id].update({
                    "status": "completed" if report.get("ok") else "failed",
                    "progress": 100, "report": report,
                    "error": "" if report.get("ok") else "全部尾帧生成失败",
                })
            if store:
                if report.get("ok"):
                    store.finish(task_id, result_path=os.path.join(kf_dir, "keyframes_manifest.json"))
                else:
                    store.fail(task_id, "全部尾帧生成失败")
        except Exception as e:  # noqa: BLE001
            app.logger.exception("关键帧生成任务失败")
            with lock:
                generation_state[task_id].update({"status": "failed", "error": str(e)})
            if store:
                store.fail(task_id, str(e))

    # B-01 P1-12：GPU 并发闸门（不接管 task_db 生命周期，worker 内部已写好）
    def _kf_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, "关键帧生成"):
            _kf_worker()
    th = threading.Thread(target=_kf_worker_gated, daemon=True)
    th.start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "total": len([p for p in plan if p["need_gen"]]),
                    "keyframes_dir": kf_dir, "chain_mode": chain_mode,
                    "qc_enabled": bool(_kf_verify),
                    "prompt_qc_enabled": bool(_kf_pre_on)})


@bp.route('/api/keyframes/file/<path:filename>')
def api_keyframes_file(filename):
    """关键帧图片访问：/api/keyframes/file/<项目>/shot_01_end.png"""
    return _serve_safe(KEYFRAMES_DIR, filename)


@bp.route('/api/keyframes/list/<path:project_name>')
def api_keyframes_list(project_name):
    """列出项目已有首尾帧"""
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    _kl_ep = request.args.get('episode_no')
    # 审计 P2-2（2026-09-29）：episode_no 非数字时裸 int() 会 500（全库只注册了
    # BadRequest 处理器，ValueError 漏成 HTML 500）。与 _ep_read_dir 的容错口径对齐。
    try:
        _kl_no = int(_kl_ep)
    except (TypeError, ValueError):
        _kl_no = 1
    _kl_sub = f"ep{_kl_no:02d}/" if _kl_no > 1 else ""
    kf_dir = _ep_read_dir(KEYFRAMES_DIR, project, _kl_ep)
    items = []
    if os.path.isdir(kf_dir):
        for fn in sorted(os.listdir(kf_dir)):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            seq = "".join(ch for ch in fn.split("_")[1] if ch.isdigit()) if "_" in fn else ""
            kind = "end" if "_end." in fn else ("start" if "_start." in fn else "other")
            items.append({"file": fn, "shot": int(seq) if seq else None, "kind": kind,
                          "url": f"/api/keyframes/file/{project}/{_kl_sub}{fn}",
                          "size": os.path.getsize(os.path.join(kf_dir, fn))})
    return jsonify({"success": True, "project": project, "dir": kf_dir,
                    "count": len(items), "items": items})


@bp.route('/api/storyboard/canvas/<path:project_name>', methods=['GET'])
def api_storyboard_canvas(project_name):
    """分镜画布数据：每镜一张卡片（分镜图/视频 + 质检分 + 一致性分 + 承载原文 + 台词）

    卡片按剧本 shots 顺序排列；手动排序（order）保存在剧本 metadata.shot_order，
    因此画布顺序与后续视频生成顺序始终一致。

    ⚠️ 2026-10-02 起每张卡片的 `storyboard` 额外带**生成中**信息（`generating` /
    `scratch_url`），让前端在整步落盘之前就能显示「第 N 镜生成中」的预览快照；
    每镜 `storyboard.exists` 的语义不变（仍只代表正式产物已落盘）。
    """
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    script = _load_script_for(project, request.args.get('episode_no'))
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "该剧本没有镜头数据",
                        "project": project}), 404
    meta = script.get("metadata") or {}

    # 分镜图 + 质检（集级目录：第 1 集平铺，第 2 集起含 epNN）
    _cv_ep = _ep_of_script(script, request.args.get('episode_no'))
    _cv_sub = f"ep{int(_cv_ep):02d}/" if _cv_ep and int(_cv_ep) > 1 else ""
    sb_map = _keyframe_sb_map(project, script, episode_no=_cv_ep)
    sb_dir = _ep_read_dir(STORYBOARDS_DIR, project, _cv_ep)
    sb_manifest = {}
    mpath = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(mpath):
        try:
            with open(mpath, "r", encoding="utf-8") as f:
                for s in ((json.load(f) or {}).get("shots") or []):
                    if isinstance(s, dict):
                        sb_manifest[_shot_num_key(s.get("shot_id"))] = s
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"分镜 manifest 读取失败：{e}")

    # 视频
    vid_dir = _ep_dir(os.path.join(VIDEOS_DIR, project), _cv_ep)
    vid_map = {}
    if os.path.isdir(vid_dir):
        for fn in sorted(os.listdir(vid_dir)):
            if fn.lower().endswith((".mp4", ".mov", ".webm")):
                vid_map[_shot_num_key(os.path.splitext(fn)[0])] = os.path.join(vid_dir, fn)

    # 一致性报告（按镜头取最低分）
    consistency_by_shot = {}
    try:
        rep = consistency.load_report(project) or {}
        for r in ((rep.get("shot_check") or {}).get("results") or []):
            k = _shot_num_key(r.get("shot"))
            cur = consistency_by_shot.get(k)
            if cur is None or (r.get("score") or 0) < (cur.get("score") or 0):
                consistency_by_shot[k] = {"score": r.get("score"),
                                          "verdict": r.get("verdict"),
                                          "character": r.get("character"),
                                          "mode": r.get("mode")}
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"一致性报告读取失败（画布将不含一致性分）：{e}")

    # 原文承载归属
    try:
        cover_map = _shot_coverage_map(script)
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"覆盖率归属计算失败：{e}")
        cover_map = {}
    cov_report = {}
    try:
        cov_report = coverage.load_coverage_report(CONTINUITY_DIR, project,
                                                   meta.get("episode_no") or script.get("episode_no") or 1)
    except Exception:  # noqa: BLE001
        cov_report = {}

    order = meta.get("shot_order") or []
    ordered = list(shots)
    if isinstance(order, list) and order:
        ordered = sorted(shots, key=lambda s: (order.index(str(s.get("shot_id")))
                                                if str(s.get("shot_id")) in order else 10 ** 6))
    kf_dir = _ep_dir(os.path.join(KEYFRAMES_DIR, project), _cv_ep)
    # 生成中快照（整步落盘之前也能预览）；纯展示增强，失败即空表
    scratch_map = _storyboard_scratch_map(project)
    cards = []
    for i, s in enumerate(ordered):
        sid = s.get("shot_id", i + 1)
        k = _shot_num_key(sid)
        seq = _shot_seq(sid, i + 1)
        sb_file = sb_map.get(k) or sb_map.get(f"shot_{seq:02d}")
        sb_item = sb_manifest.get(k) or {}
        vid = vid_map.get(k) or vid_map.get(f"shot_{seq:02d}")
        kf_end = os.path.join(kf_dir, f"shot_{seq:02d}_end.png")
        cards.append({
            "order": i,
            "shot_id": sid,
            "seq": seq,
            "camera": s.get("camera"),
            "duration": s.get("duration"),
            "location": s.get("location"),
            "emotion": s.get("emotion"),
            "description": s.get("description"),
            "dialogue": s.get("dialogue") or [],
            "dialogue_text": s.get("dialogue_text"),
            "characters_in_shot": s.get("characters_in_shot") or [],
            "items_in_shot": s.get("items_in_shot") or [],
            "storyboard": {
                "exists": bool(sb_file),
                "url": (f"/api/storyboards/file/{project}/{_cv_sub}shot_{seq:02d}.png"
                        if sb_file else ""),
                "path": sb_file or "",
                "qc": sb_item.get("qc") or {},
                "success": bool(sb_item.get("success")),
                "blocked": bool(sb_item.get("qc_blocked")),
                "error": sb_item.get("error") or "",
                # ⭐ 生成中快照（2026-10-02）：正式产物未落盘、但 scratch 里已有该镜
                #    的中间图时给出预览。exists/url 语义不变，前端据此显示「生成中」。
                "generating": bool(not sb_file and scratch_map.get(seq)),
                "scratch_url": (scratch_map.get(seq) or {}).get("url", ""),
            },
            "video": {
                "exists": bool(vid),
                "url": (f"/api/videos/{project}/{_cv_sub}{os.path.basename(vid)}"
                        if vid else ""),
                "path": vid or "",
            },
            "keyframe": {
                "start": bool(sb_file),
                "end_exists": os.path.isfile(kf_end),
                "end_url": (f"/api/keyframes/file/{project}/{_cv_sub}shot_{seq:02d}_end.png"
                            if os.path.isfile(kf_end) else ""),
            },
            "consistency": consistency_by_shot.get(k) or {},
            "coverage": {"units": cover_map.get(str(sid)) or [],
                         "unit_count": len(cover_map.get(str(sid)) or [])},
        })

    summary = {
        "shot_count": len(cards),
        "storyboard_ready": sum(1 for c in cards if c["storyboard"]["exists"]),
        "video_ready": sum(1 for c in cards if c["video"]["exists"]),
        "keyframe_end_ready": sum(1 for c in cards if c["keyframe"]["end_exists"]),
        "qc_blocked": sum(1 for c in cards if c["storyboard"]["blocked"]),
        # ⭐ 生成中快照统计（2026-10-02）：正式产物未落盘、但 scratch 已有中间图的镜数。
        #    前端据此显示「生成中 3/6」进度条，不必等整步完成。
        "storyboard_generating": sum(1 for c in cards if c["storyboard"].get("generating")),
        "coverage": {
            "plot_coverage_percent": cov_report.get("plot_coverage_percent"),
            "detail_coverage_percent": cov_report.get("detail_coverage_percent"),
            "missing_count": cov_report.get("missing_count"),
            "passed": cov_report.get("passed"),
            "checked_at": cov_report.get("checked_at"),
        } if cov_report else {},
    }
    return jsonify({"success": True, "project": project,
                    "episode_no": meta.get("episode_no") or script.get("episode_no"),
                    "episode_title": meta.get("episode_title") or script.get("episode_title"),
                    "title": script.get("title"),
                    "summary": summary, "cards": cards,
                    "shot_order": order or [str(s.get("shot_id")) for s in shots]})


@bp.route('/api/storyboard/shot/reorder', methods=['POST'])
def api_storyboard_shot_reorder():
    """分镜拖拽排序：写回剧本 shots 顺序 + metadata.shot_order

    body: {project_name, episode_no, order: [shot_id, ...]}
    副作用：shot_id 保持原值不变（避免打断既有产物文件名映射），
    仅调整 shots 数组顺序与 shot_order 记录。
    """
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    order = data.get('order') or []
    if err is not None:
        return err
    if not order:
        return jsonify({"success": False, "error": "缺少 project_name / order"}), 400
    key = project_store.safe_key(project)
    episode_no = data.get('episode_no')
    script = _load_script_for(project, episode_no)
    if not script:
        return jsonify({"success": False, "error": "剧本不存在"}), 404
    shots = script.get("shots") or []
    idx = {str(s.get("shot_id")): s for s in shots}
    new_shots = [idx[str(sid)] for sid in order if str(sid) in idx]
    if len(new_shots) != len(shots):
        missing = [str(s.get("shot_id")) for s in shots if str(s.get("shot_id")) not in
                   {str(x) for x in order}]
        return jsonify({"success": False,
                        "error": f"排序清单与镜头不匹配（缺少：{missing[:5]}）"}), 400
    script["shots"] = new_shots
    ep_no = script.get("episode_no") or episode_no or 1
    script.setdefault("metadata", {})["shot_order"] = [str(x) for x in order]
    script["metadata"]["shot_order_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        novel_to_script.save_episode_script(script, SCRIPT_DIR, key, ep_no)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本落盘失败：{e}"}), 500
    return jsonify({"success": True, "project": project, "episode_no": ep_no,
                    "shot_order": [str(x) for x in order]})


@bp.route('/api/storyboard/retry-shot', methods=['POST'])
@_autopilot_guard
def api_storyboard_retry_shot():
    """单镜分镜图重跑（同步返回；只影响该镜，不触碰其它镜头产物）

    body: {project_name, shot: {...}, seed?, episode_no?}
    未传 shot 时按 shot_id 从剧本取。

    D1（2026-09-23）：见 `_storyboard_retry_shot_impl` 上方说明。
    """
    with gpu_task_gate.run_gpu_task(
            f"sb_retry_{uuid.uuid4().hex[:8]}", "分镜图单镜重跑"):
        return _storyboard_retry_shot_impl()


# ===================== 分镜九宫格候选构图（2026-10-01，对标 BigBanana） =====================
# 一镜一次生成「3x3 九候选构图联系表」→ 选格裁切为正式分镜图。与 best-of-N 相比：
# 一次生成出 9 个机位变体，省时省卡；选格可由 QC 模型打分或用户手动指定。

@bp.route('/api/storyboard/grid-candidates', methods=['POST'])
def api_storyboard_grid_candidates():
    """为一镜生成九宫格候选构图（异步）。body: {project_name, episode_no, shot_id}"""
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        episode_no = max(1, int(data.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    shot_key = (data.get('shot_id') or '').strip()
    script = _load_script_for(project_name, episode_no) or {}
    shots = script.get("shots") or []
    shot = next((s for s in shots if isinstance(s, dict) and
                 (str(s.get("shot_id")) == shot_key or
                  str(_shot_seq(s.get("shot_id"), 0)) == shot_key.replace("shot_", ""))), None)
    if shot is None:
        return jsonify({"success": False,
                        "error": f"剧本里找不到镜头：{shot_key}"}), 404
    char_idx = _build_asset_index(script.get("characters") or [], project_name, "character")
    item_idx = _build_asset_index(script.get("items") or [], project_name, "item")
    scene_idx = _build_asset_index(script.get("scenes") or [], project_name, "scene")
    refs = _allocate_storyboard_refs(shot, char_idx, item_idx, scene_idx, project_name)
    if not refs:
        return jsonify({"success": False,
                        "error": "该镜无可用参考图（请先生成资产生成）"}), 400
    style = data.get('style') or _project_style(project_name)
    _res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO,
                             megapixels=style_kit.storyboard_megapixels())
    refs = _unify_ref_canvas(refs, _res["size"], project_name)
    refs = _cap_storyboard_refs(refs, shot)
    labels = [r[1] for r in refs]
    seq = _shot_seq(shot.get("shot_id"), 1)
    task_id = f"shot_grid_{project_name}_{int(time.time() * 1000)}"

    with lock:
        generation_state[task_id] = {
            "status": "running", "phase": "分镜九宫格候选构图",
            "project": project_name, "shot": shot.get("shot_id"),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _grid_worker():
        try:
            with gpu_task_gate.run_gpu_task(task_id, "分镜九宫格候选构图"):
                # ---- 3D 导演台：九宫格「3D 构图基准网格」（2026-10-03）----
                # 逐格渲染 9 个候选构图的 3D 站位/机位基准，拼成一张与目标九宫格一一对应的
                # 3x3 网格 → 作 <image1> 构图基准 → 提示词追加 COMPOSITION BASELINE GRID 段。
                # 渲染失败静默降级为纯文字站位锚点（fail-open，绝不阻断九宫格主链路）。
                _grid_refs = [r[2] for r in refs]
                _grid_labels = list(labels)
                _grid_has_blocking = False
                try:
                    from config import ENABLE_3D_BLOCKING_IMAGE
                    if ENABLE_3D_BLOCKING_IMAGE:
                        import te_3d_render
                        if te_3d_render.available():
                            _blk_out = os.path.join(QC_DIR, project_name, "te3d_blocking")
                            _grid_sheet = te_3d_render.render_blocking_grid(
                                shot, _blk_out, target_size=_res["size"]) or ""
                            if _grid_sheet:
                                app.logger.info("[3D导演台] 九宫格 shot=%s 3D 构图基准网格已渲染：%s",
                                                shot.get("shot_id"), os.path.basename(_grid_sheet))
                                _grid_refs.insert(0, _grid_sheet)
                                _grid_labels.insert(0, "3D构图基准网格")
                                _grid_has_blocking = True
                except Exception as _3d_e:  # noqa: BLE001
                    app.logger.warning("[3D导演台] 九宫格 shot=%s 3D 基准渲染失败（降级为纯文字站位）：%s",
                                       shot.get("shot_id"), _3d_e)
                result = comfyui_client.generate_shot_grid_candidates(
                    shot, _grid_labels, _grid_refs, project_name,
                    f"shot_{seq:02d}", style=style,
                    has_blocking_image=_grid_has_blocking,
                    seed=random.randint(1, 2 ** 31 - 1), size=_res["size"])
            grid_png = (result.get("files") or [""])[0]
            if not grid_png or not os.path.isfile(grid_png):
                raise RuntimeError("九宫格候选构图生成未返回文件")
            # 集级目录（2026-10-02 修复）：分镜画布读 epNN/ 子目录，grid 产物此前
            # 落平铺目录，第 2 集起选格结果不会出现在该集画布 —— 与
            # _update_storyboard_manifest_shot 的目录/URL 口径对齐。
            _flat = os.path.join(STORYBOARDS_DIR, project_name)
            _sb_dir = _ep_dir(_flat, episode_no)
            _sub = os.path.basename(_sb_dir) if _sb_dir != _flat else ""
            dst = os.path.join(_sb_dir, f"shot_{seq:02d}_grid.png")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(grid_png, dst)
            with lock:
                generation_state[task_id].update({
                    "status": "completed", "progress": 100,
                    "grid_url": (f"/api/storyboards/file/{project_name}/"
                                 f"{_sub + '/' if _sub else ''}shot_{seq:02d}_grid.png"),
                    "result": {"grid": dst},
                })
        except cancellation.Cancelled as e:
            with lock:
                generation_state[task_id].update({"status": "cancelled", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            app.logger.error("分镜九宫格候选构图失败：%s", e, exc_info=True)
            with lock:
                generation_state[task_id].update({"status": "failed", "error": str(e)})

    threading.Thread(target=_grid_worker, daemon=True, name=task_id).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started"})


@bp.route('/api/storyboard/grid-apply', methods=['POST'])
def api_storyboard_grid_apply():
    """把九宫格里选中的格（1-9）裁切为该镜正式分镜图（旧图移入回收站，可恢复）。

    ⚠️ 选格应用视为**用户人工定稿**：裁切结果直接入库，不再走图片 AI 质检。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        episode_no = max(1, int(data.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    shot_key = (data.get('shot_id') or '').strip()
    try:
        cell = max(1, int(data.get('cell') or 0))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "cell 必须是 1-9 的整数"}), 400
    if cell > 9:
        return jsonify({"success": False, "error": "cell 必须是 1-9"}), 400
    seq = _shot_seq(shot_key, 0)
    if seq <= 0:
        return jsonify({"success": False, "error": f"无法解析镜号：{shot_key}"}), 400
    # 集级目录（2026-10-02 修复）：与 grid-candidates / 分镜画布同口径，第 2 集
    # 起读写 epNN/ 子目录，选格裁切结果才能落到该集画布实际读取的位置。
    _flat = os.path.join(STORYBOARDS_DIR, project_name)
    _sb_dir = _ep_dir(_flat, episode_no)
    _sub = os.path.basename(_sb_dir) if _sb_dir != _flat else ""
    grid_png = os.path.join(_sb_dir, f"shot_{seq:02d}_grid.png")
    if not os.path.isfile(grid_png):
        return jsonify({"success": False,
                        "error": f"九宫格候选图不存在：{grid_png}（请先生成候选构图）"}), 404
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_OUTPUT_DIR, "projects", "_trash", "shot_grid",
                              f"{stamp}_{project_name}")
    dst = os.path.join(_sb_dir, f"shot_{seq:02d}.png")
    cleared, skipped = [], []
    if os.path.isfile(dst):
        _trash_move(dst, "storyboards", trash_root, cleared, skipped)
    comfyui_client.crop_grid_cell(grid_png, cell - 1, dst)
    app.logger.info("[shot-grid-apply] 项目=%s 集=%s 镜=%s 第 %d 格已应用（旧图 %d 项入回收站）",
                    project_name, episode_no, shot_key, cell, len(cleared))
    return jsonify({"success": True, "project": project_name, "shot_id": shot_key,
                    "cell": cell, "applied": dst,
                    "url": (f"/api/storyboards/file/{project_name}/"
                            f"{_sub + '/' if _sub else ''}shot_{seq:02d}.png"),
                    "cleared": cleared,
                    "hint": "选中格已裁切为该镜正式分镜图（人工定稿，未走 AI 质检）"})


@bp.route('/api/storyboards/generate', methods=['POST'])
def api_generate_storyboards():
    """为剧本的每个 shot 生成一张分镜图（参考角色/物品/场景资产图）"""
    # ⚠️ 故意不设 AI 门禁：分镜图 = 消费剧本里已产出的 shot.prompt + ComfyUI 出图 + 质检，
    # 不读任何 AI 凭证。挂在 LLM 门禁上会把「没配 key 但有存量剧本」的用户一起拦死。
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    shots = data.get('shots', [])
    if not shots:
        return jsonify({"error": "没有镜头数据"}), 400

    limit = data.get('limit')
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g
    if isinstance(limit, int) and limit > 0:
        shots = shots[:limit]

    # ⑥ 自动引用剧本中已判定的「镜头数 / 每集时长」字段（缺 duration 时按项目配置兜底）
    episode_stats = _episode_schema_defaults(project_name, shots)

    char_idx = _build_asset_index(data.get('characters', []), project_name, "character")
    item_idx = _build_asset_index(data.get('items', []), project_name, "item")
    scene_idx = _build_asset_index(data.get('scenes', []), project_name, "scene")

    # G5：任务 ID 用 uuid（秒级时间戳同秒双 POST 会覆盖 generation_state 且双线程并发抢同一目标路径）；
    # 入口幂等：同项目+同集已有 running 的分镜任务 → 复用其 task_id（reused=True），不重复开线程。
    # 匹配用稳定的 step 字段（worker 运行中 phase 会变化，不能用 phase 判）。
    # B-11 P1-8：守卫键加 episode_no —— 第 2 集请求不再被第 1 集运行中任务吞掉。
    _ep_no = data.get('episode_no')
    with lock:
        _prune_task_registry(generation_state)
        _existing_sb = next((tid for tid, st in generation_state.items()
                             if st.get("status") == "running"
                             and st.get("project_name") == project_name
                             and st.get("step") == "storyboard"
                             and st.get("episode_no") == _ep_no), None)
        if _existing_sb:
            return jsonify({"task_id": _existing_sb, "status": "started", "reused": True,
                            "total": len(shots), "overwrite": bool(data.get('overwrite')),
                            "episode_stats": episode_stats})
        task_id = f"storyboard_{project_name}_{uuid.uuid4().hex[:12]}"
        generation_state[task_id] = {
            "status": "running", "progress": 0, "total": len(shots),
            "current": 0, "phase": "分镜图生成", "results": [],
            "qc": _qc_brief("image"),
            "project_name": project_name, "step": "storyboard",
            "episode_no": _ep_no,
            "refs_available": {
                "characters": {k: bool(v["image"]) for k, v in char_idx.items()},
                "items": {k: bool(v["image"]) for k, v in item_idx.items()},
                "scenes": {k: bool(v["image"]) for k, v in scene_idx.items()},
            },
        }

    # B-01 P1-12：GPU 并发闸门
    def _storyboard_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, "分镜图生成"):
            _storyboard_worker(task_id, project_name, shots, char_idx, item_idx,
                               scene_idx, data.get('episode_no'),
                               _project_style(project_name), bool(data.get('overwrite')))
    thread = threading.Thread(target=_storyboard_worker_gated, daemon=True)
    thread.daemon = True
    thread.start()
    return jsonify({"task_id": task_id, "status": "started", "total": len(shots),
                    "overwrite": bool(data.get('overwrite')),
                    "episode_stats": episode_stats})


@bp.route('/api/storyboards/manifest/<path:project_name>')
def api_storyboard_manifest(project_name):
    """读取已生成的分镜图清单（用于页面回看）"""
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    _mf_ep = request.args.get('episode_no')
    out_dir = _ep_read_dir(STORYBOARDS_DIR, project, _mf_ep)
    manifest_path = os.path.join(out_dir, "storyboard_manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        return jsonify({"success": True, "exists": True, "project_name": project,
                        "manifest": manifest})

    # 无清单时按磁盘文件兜底（项目可能由其他会话生成）
    # ⚠️ URL 必须带集前缀：out_dir 是集级目录（第 2 集起 <项目>/epNN/），
    #    漏掉 epNN 段会让第 2 集起的所有图 404（同 _storyboard_worker 的旧 bug）。
    #    审计 P2-2：episode_no 非数字时裸 int() 会 500，改容错解析。
    try:
        _mf_no = int(_mf_ep)
    except (TypeError, ValueError):
        _mf_no = 1
    _mf_sub = f"ep{_mf_no:02d}/" if _mf_no > 1 else ""
    shots = []
    if os.path.isdir(out_dir):
        for fn in sorted(os.listdir(out_dir)):
            if fn.lower().endswith(".png"):
                sid = fn.replace("shot_", "").replace(".png", "")
                shots.append({
                    "shot_id": int(sid) if sid.isdigit() else sid,
                    "success": True,
                    "file": os.path.join(out_dir, fn),
                    "url": f"/api/storyboards/file/{project}/{_mf_sub}{fn}",
                })
    return jsonify({"success": True, "exists": bool(shots), "project_name": project,
                    "manifest": {"project_name": project, "shots": shots,
                                 "total": len(shots),
                                 "success_count": sum(1 for s in shots if s.get("success"))}})


@bp.route('/api/storyboards/file/<path:filename>')
def api_storyboard_file(filename):
    """提供分镜图文件访问"""
    return _serve_safe(STORYBOARDS_DIR, filename)


@bp.route('/api/storyboards/scratch/<path:project_name>/<path:filename>')
def api_storyboard_scratch_file(project_name, filename):
    """提供「分镜生成中」中间产物（storyboard_scratch）访问。

    ⭐ 2026-10-02：分镜步骤是整步落盘，正式产物要等 6 镜全跑完才写进
    STORYBOARDS_DIR；期间画布靠本路由读 scratch 目录显示「生成中」预览。
    与 api_storyboard_file 同用 `_serve_safe` 做目录穿越防护
    （base 按项目隔离在 QC_DIR/<项目>/storyboard_scratch 之内）。
    """
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    base = os.path.join(QC_DIR, project, "storyboard_scratch")
    return _serve_safe(base, filename)


@bp.route('/api/episode/scenes', methods=['GET'])
def api_episode_scenes():
    """场次级状态（层级展示用，2026-10-03）：第N集 → 第1场/第2场…

    query: project=<项目键>&episode_no=N。返回每场的场次号/标题/镜数、分镜图完成数、
    场次视频（scene_XX.mp4）是否就绪 —— 前端按「集 → 场」两级树渲染。
    """
    project = _safe_project(request.args.get('project') or '')
    try:
        episode_no = max(1, int(request.args.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    if not project:
        return jsonify({"success": False, "error": "project 必填"}), 400
    script = _load_script_for(project, episode_no) or {}
    flow = [s for s in (script.get("scene_flow") or []) if isinstance(s, dict)]
    # 分镜 manifest（按镜 success 统计每场完成数）
    _flat = os.path.join(STORYBOARDS_DIR, project)
    _sb_dir = _ep_dir(_flat, episode_no)
    _sub = os.path.basename(_sb_dir) if _sb_dir != _flat else ""
    mpath = os.path.join(_sb_dir, "storyboard_manifest.json")
    _mshots = {}
    if os.path.isfile(mpath):
        try:
            m = read_json_strict(mpath, {})
            _mshots = {str(s.get("shot_id")): s
                       for s in ((m or {}).get("shots") or []) if isinstance(s, dict)}
        except Exception:  # noqa: BLE001
            _mshots = {}
    vdir = _ep_dir(os.path.join(VIDEOS_DIR, project), episode_no)
    scenes = []
    for sc in flow:
        sids = [str(x) for x in (sc.get("shot_ids") or [])]
        sb_ok = sum(1 for sid in sids
                    if (_mshots.get(sid) or {}).get("success"))
        _sn = int(sc.get("scene_no") or len(scenes) + 1)
        vfile = os.path.join(vdir, f"scene_{_sn:02d}.mp4")
        scenes.append({
            "scene_no": _sn,
            "heading": sc.get("heading") or f"第{_sn}场",
            "location": sc.get("location"),
            "int_ext": sc.get("int_ext"),
            "time_of_day": sc.get("time_of_day"),
            "shot_ids": sids,
            "shot_count": len(sids),
            "storyboard_ok": sb_ok,
            "video_ready": os.path.isfile(vfile) and os.path.getsize(vfile) > 0,
            "video_url": (f"/api/videos/{project}/{_sub + '/' if _sub else ''}"
                          f"scene_{_sn:02d}.mp4"),
        })
    full = os.path.join(vdir, f"ep{episode_no:02d}_full.mp4")
    if not os.path.isfile(full):
        full = os.path.join(vdir, "episode_full.mp4")
    return jsonify({
        "success": True, "project": project, "episode_no": episode_no,
        "scene_count": len(scenes), "scenes": scenes,
        "full_video_ready": os.path.isfile(full) and os.path.getsize(full) > 0,
    })


@bp.route('/api/episodes/<novel_id>', methods=['GET'])
def api_list_episodes(novel_id):
    """某小说已生成的剧集清单（含从磁盘产物推导的真实状态与进度）"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    # 支持 ?project_id= 指定项目查看（不传时自动按小说归属的项目）
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, proj["dir_key"] if proj else None)
    episodes = novel_to_script.list_episodes(SCRIPT_DIR, key)
    # 逐集补齐状态 / 进度（前端 EpisodeInfo.status、completed_shots 的数据源）。
    # ⚠️ 产物目录（storyboards/videos/final/autopilot）用的是项目 dir_key（= key），
    #    而非剧本 meta.project_name（那是「每集独立项目名」，如 极短小说_雨夜归人_第1集）。
    for row in episodes:
        try:
            row.update(_episode_progress(key, int(row.get("episode_no") or 1),
                                         int(row.get("shot_count") or 0)))
        except Exception as e:  # noqa: BLE001  单集探测失败不拖垮整表
            app.logger.warning(f"第{row.get('episode_no')}集进度探测失败：{e}")
            row.setdefault("status", "pending")
            row.setdefault("completed_shots", 0)
    done = sum(1 for r in episodes if r.get("status") == "done")
    failed = sum(1 for r in episodes if r.get("status") == "failed")
    producing = sum(1 for r in episodes if r.get("status") == "producing")
    return jsonify({
        "success": True, "novel_id": novel_id, "name": key,
        "project_id": (proj or {}).get("id", ""),
        "project_key": key,
        "novel_title": meta.get("title") or meta.get("name"),
        "chapter_count": meta.get("chapter_count"),
        "count": len(episodes),
        "total": len(episodes),
        "stats": {"total": len(episodes), "done": done, "failed": failed,
                  "producing": producing,
                  "pending": len(episodes) - done - failed - producing},
        "episode_dir": os.path.abspath(os.path.join(SCRIPT_DIR, key)),
        "episodes": episodes,
    })


@bp.route('/api/episodes/<novel_id>/<int:episode_no>', methods=['PUT'])
def api_update_episode(novel_id, episode_no):
    """修改单集剧本（shot.description / motion / emotion / camera 等字段）。

    payload: {"shots": [{"shot_id": 2, "description": "...", "motion": "...", ...}, ...]}
    按 shot_id 匹配后合并字段（只更新 payload 里出现的 key，不整镜替换）。
    落盘前备份原始剧本到 .bak；任何失败 fail-closed（不写盘）。
    """
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, proj["dir_key"] if proj else None)
    script_path = novel_to_script.episode_script_path(SCRIPT_DIR, key, episode_no)
    if not os.path.isfile(script_path):
        return jsonify({"success": False, "error": f"剧本文件不存在：{script_path}"}), 404
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            script = json.load(f)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本读取失败：{e}"}), 500
    data = request.json or {}
    updates = data.get("shots") or []
    if not updates:
        return jsonify({"success": False, "error": "没有要修改的镜头"}), 400
    _ALLOWED = ("description", "visual_detail", "motion", "emotion", "edit_reason",
                "beat", "camera", "camera_motion", "shot_type", "duration", "audio_cues")
    shots = script.get("shots") or []
    by_id = {str(s.get("shot_id")): s for s in shots}
    changed = 0
    for up in updates:
        sid = str((up or {}).get("shot_id", ""))
        target = by_id.get(sid)
        if target is None:
            continue
        for fld in _ALLOWED:
            if fld in up and up[fld] is not None:
                target[fld] = up[fld]
                changed += 1
    if changed == 0:
        return jsonify({"success": False, "error": "没有匹配到任何镜头（shot_id 对不上）"}), 400
    bak = script_path + ".bak"
    if not os.path.isfile(bak):
        import shutil as _shutil
        _shutil.copy2(script_path, bak)
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    app.logger.info("[剧本编辑] novel=%s ep=%s 更新了 %d 个字段（shot_ids=%s）",
                   novel_id, episode_no, changed,
                   [str((u or {}).get("shot_id")) for u in updates])
    return jsonify({"success": True, "changed": changed, "script_path": script_path})


@bp.route('/api/episodes/<novel_id>/<int:episode_no>', methods=['GET'])
def api_get_episode(novel_id, episode_no):
    """读取单集剧本（供切集预览 / 载入后续步骤）"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, proj["dir_key"] if proj else None)
    try:
        script = novel_to_script.load_episode_script(SCRIPT_DIR, key, episode_no)
    except novel_to_script.EpisodeNotFoundError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本读取失败：{e}"}), 500

    meta_i = script.get("metadata") or {}
    dir_info = os.path.abspath(os.path.join(SCRIPT_DIR, key))
    return jsonify({
        "success": True,
        "novel_id": novel_id,
        "episode_no": episode_no,
        "project_id": (proj or {}).get("id", ""),
        "project_key": key,
        "episode_title": script.get("episode_title") or meta_i.get("chapter_title"),
        "chapter_index": meta_i.get("chapter_index"),
        "project_name": meta_i.get("project_name") or novel_to_script.episode_project_name(key, episode_no),
        "script_path": novel_to_script.episode_script_path(SCRIPT_DIR, key, episode_no),
        "episode_dir": dir_info,
        "script": script,
        "stats": {
            "characters": len(script.get("characters") or []),
            "items": len(script.get("items") or []),
            "scenes": len(script.get("scenes") or []),
            "shots": len(script.get("shots") or []),
            "shot_count": script.get("shot_count") or len(script.get("shots") or []),
            "episode_duration_sec": script.get("episode_duration_sec"),
            "duration_per_shot_sec": script.get("duration_per_shot_sec"),
        },
        "episode_plan": script.get("episode_plan"),
        "warnings": meta_i.get("warnings") or [],
        "chunks_total": meta_i.get("chunks_total"),
        "chunks_used": meta_i.get("chunks_used"),
        "chapter_char_count": meta_i.get("chapter_char_count"),
        "generated_at": meta_i.get("generated_at"),
        "continuity_meta": meta_i.get("continuity") or None,
        "coverage_meta": meta_i.get("coverage") or None,
        "coverage_report_path": meta_i.get("coverage_report_path"),
        "state_in": script.get("state_in"),
        "state_out": script.get("state_out"),
        "continuity": continuity.episode_continuity_view(CONTINUITY_DIR, key, episode_no),
    })


# ===================== 九宫格分镜API =====================

@bp.route('/api/storyboard/nine-grid', methods=['POST'])
@_autopilot_guard
def api_generate_nine_grid():
    """生成九宫格分镜"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    scene_description = data.get('scene_description', '')
    character_ids = data.get('character_ids', [])
    emotion = data.get('emotion', 'neutral')

    if not scene_description:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    sb = NineGridStoryboard(project_name, project_dir)
    nine_grid = sb.generate_nine_grid(scene_description, character_ids, emotion)

    # 关键：必须把 grid_id 返回给前端，且落盘文件名要与 grid_id 一致。
    # 之前 save_nine_grid() 存成 nine_grid_<时间戳>.json，却没有任何字段告诉前端这个名字，
    # 于是「选最佳构图」只能拼出 .../nine-grid/undefined/select → 404，功能等于不可用。
    grid_id = f"nine_grid_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    grid_dir = os.path.join(project_dir, "storyboards")
    os.makedirs(grid_dir, exist_ok=True)
    filepath = os.path.join(grid_dir, f"{grid_id}.json")
    nine_grid["grid_id"] = grid_id
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(nine_grid, f, ensure_ascii=False, indent=2)

    return jsonify({
        "success": True,
        "grid_id": grid_id,
        "project": project_name,
        "scene_description": scene_description,
        "created_at": nine_grid.get("created_at"),
        "filepath": filepath,
        "shots": nine_grid["shots"],
    })


@bp.route('/api/storyboard/nine-grid/<grid_id>/select', methods=['POST'])
@_autopilot_guard
def api_select_nine_grid_shot(grid_id):
    """选择九宫格中的最佳镜头"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    selected_index = data.get('selected_index')

    if selected_index is None:
        return jsonify({"success": False, "error": "缺少 selected_index"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    sb = NineGridStoryboard(project_name, project_dir)
    # 加载九宫格数据
    grid_file = os.path.join(project_dir, "storyboards", f"{grid_id}.json")
    if not os.path.exists(grid_file):
        return jsonify({"success": False, "error": f"分镜文件不存在：{grid_id}"}), 404

    with open(grid_file, 'r', encoding='utf-8') as f:
        nine_grid = json.load(f)

    try:
        selected = sb.select_best_shot(nine_grid, int(selected_index))
    except (ValueError, IndexError, TypeError) as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"无效的选择：{e}"}), 400

    # 把选择结果落盘，否则「已选最佳构图」刷新后就丢了
    nine_grid["selected_index"] = int(selected_index)
    nine_grid["selected_at"] = datetime.now().isoformat()
    with open(grid_file, 'w', encoding='utf-8') as f:
        json.dump(nine_grid, f, ensure_ascii=False, indent=2)

    return jsonify({"success": True, "grid_id": grid_id, "shot": selected})

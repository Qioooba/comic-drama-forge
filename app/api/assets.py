# -*- coding: utf-8 -*-
"""assets 域蓝图：角色/物品/场景资产与关系图。

2026-10-07 由 ``app/app.py`` 按域拆分而来，**纯搬迁**：路径、方法、报错文案逐字
不变（守门脚本 ``scripts/snapshot_routes.py`` 比对 rule + methods）。
``url_prefix`` 留空，路径直接写死在各 ``@bp.route`` 上。
"""
from __future__ import annotations

from flask import Blueprint

# 共享 runtime 上下文（app 实例 / 212 个模块级 helper / config 常量）。
# __all__ 里显式列出了下划线开头的名字，故这里能整体取到。
from api._shared import *  # noqa: F401,F403

bp = Blueprint("assets", __name__)
DOMAIN = "assets"

@bp.route('/api/scenes/grid-preview', methods=['POST'])
def api_scenes_grid_preview():
    """异步发起场景九宫格机位预览（9 个机位逐张生成 + 3x3 拼接），返回 task_id"""
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    name = (data.get('name') or '').strip()
    if not name or '/' in name or '\\' in name or name in ('.', '..'):
        return jsonify({"success": False,
                        "error": "场景名不能为空且不得含路径分隔符"}), 400
    asset_dir = _find_scene_asset_dir(project_name, name)
    if not asset_dir:
        return jsonify({"success": False,
                        "error": f"未找到场景资产目录（{name}），请先生成场景资产"}), 404
    base_png = os.path.join(asset_dir, "base.png")
    if not os.path.isfile(base_png):
        return jsonify({"success": False,
                        "error": "该场景还没有 base 图，请先生成场景资产"}), 400
    task_id = f"scene_grid_{project_name}_{int(time.time() * 1000)}"
    style = _project_style(project_name)
    scene_prompt = _scene_grid_prompt_for(project_name, name)
    seed = random.randint(1, 2 ** 31 - 1)

    with lock:
        generation_state[task_id] = {
            "status": "running", "phase": "场景九宫格机位预览",
            "total": len(scene_grid.SCENE_GRID_ANGLES), "current": 0, "progress": 0,
            "project": project_name, "scene": name,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _grid_worker():
        def _cb(done, total, item):
            with lock:
                generation_state[task_id].update({
                    "current": done, "total": total,
                    "progress": int(done / max(total, 1) * 100),
                    "phase": f"机位 {done + 1}/{total}：{(item or {}).get('label', '')}",
                })
        try:
            with gpu_task_gate.run_gpu_task(task_id, "场景九宫格机位预览"):
                out = scene_grid.generate_scene_grid(
                    comfyui_client, project_name, name, base_png,
                    scene_prompt, style, asset_dir, seed=seed, progress_cb=_cb)
            _dir_name = os.path.basename(asset_dir)
            with lock:
                generation_state[task_id].update({
                    "status": "completed", "progress": 100, "result": out,
                    "grid_url": (f"/api/scenes/grid/file/{project_name}/{_dir_name}"
                                 "/grid/grid_preview.png") if out.get("grid") else "",
                    "angle_urls": [
                        {"key": a["key"], "label": a["label"],
                         "url": f"/api/scenes/grid/file/{project_name}/{_dir_name}/{a['key']}.png"}
                        for a in (out.get("angles") or [])],
                })
        except cancellation.Cancelled as e:
            with lock:
                generation_state[task_id].update({"status": "cancelled", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            app.logger.error("场景九宫格生成失败：%s", e, exc_info=True)
            with lock:
                generation_state[task_id].update({"status": "failed", "error": str(e)})

    threading.Thread(target=_grid_worker, daemon=True, name=task_id).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "total": len(scene_grid.SCENE_GRID_ANGLES)})


@bp.route('/api/scenes/grid/file/<project_name>/<path:relpath>')
def api_scenes_grid_file(project_name, relpath):
    """九宫格产物文件服务（限 scenes/<项目>/<场景>/grid/ 内，防目录穿越）"""
    project_name = _safe_project(project_name)
    base = os.path.abspath(os.path.join(SCENES_DIR, project_name))
    filepath = os.path.abspath(os.path.join(base, relpath.replace("\\", "/").lstrip("/")))
    grid_root = os.path.abspath(os.path.join(base, "grid"))
    if not filepath.startswith(grid_root + os.sep) or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True)


@bp.route('/api/scenes/grid-apply', methods=['POST'])
def api_scenes_grid_apply():
    """把选中的机位预览图升级为场景新 base（旧 base 移入回收站，可恢复）"""
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    name = (data.get('name') or '').strip()
    angle_key = (data.get('angle') or '').strip()
    if not name or not angle_key:
        return jsonify({"success": False, "error": "name 与 angle 必填"}), 400
    asset_dir = _find_scene_asset_dir(project_name, name)
    if not asset_dir:
        return jsonify({"success": False, "error": f"未找到场景资产目录（{name}）"}), 404
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_OUTPUT_DIR, "projects", "_trash", "scene_grid",
                              f"{stamp}_{project_name}")
    cleared, skipped = [], []
    try:
        res = scene_grid.apply_grid_angle(
            asset_dir, angle_key,
            lambda src: _trash_move(src, "scene_grid_base", trash_root, cleared, skipped))
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    app.logger.info("[scene-grid-apply] 项目=%s 场景=%s 机位=%s（清理 %d 项）",
                    project_name, name, angle_key, len(cleared))
    return jsonify({"success": True, "project": project_name, "name": name,
                    "angle": angle_key, **res, "cleared": cleared, "skipped": skipped,
                    "hint": "选中机位图已升级为场景 base；下次分镜参考图生成立即使用新视角；"
                            "各机位视角图可在资产重生时刷新"})


@bp.route('/api/assets/generate', methods=['POST'])
def api_generate_assets():
    """生成资产（角色/物品/场景，含多视角）"""
    # ⚠️ 这里**故意不设** AI 前置门禁：资产生成是「消费已产出的提示词 + ComfyUI 出图 +
    # 质检」的链路，全程不读 text/qc/chat 凭证（提示词由上游剧本步骤产出、随 assets 传入）。
    # 早前一版把门禁挂在这里，后果是「AI key 没配 → 连本来能出的图也一起被拦」，
    # 属于护栏误伤业务。凡是光跑 ComfyUI 就能完成的入口都不挂门禁。
    data = _body()
    asset_type = data.get('asset_type', '')  # character / item / scene
    # P2-T2：写盘路由统一走 _project_or_400（契约必填 project_name）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    assets = data.get('assets', [])
    # A-2 P0：断点续跑开关。默认 False → 已达标入库的资产跳过；显式传 true 强制重生成
    overwrite = bool(data.get('overwrite'))

    if asset_type not in ("character", "item", "scene"):
        return jsonify({"error": "asset_type 必须是 character/item/scene"}), 400
    if not assets:
        return jsonify({"error": "没有资产数据"}), 400
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g

    # B-12 P1-15：资产任务 ID 改用 uuid（G5 只改了分镜/视频/配音/混音，资产漏改），
    # 同秒并发请求不再互撞。
    task_id = f"{asset_type}_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "asset_type": asset_type,
            "progress": 0, "total": len(assets), "current": 0,
            "phase": "基础图", "results": [],
            "overwrite": overwrite,
        }

    thread = threading.Thread(
        target=_generate_asset_task,
        args=(task_id, assets, asset_type, project_name,
              data.get('style') or _project_style(project_name), overwrite)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id, "status": "started"})


@bp.route('/api/assets/character/upload-sheet', methods=['POST'])
def api_character_upload_sheet():
    """上传角色形象图 → 落 base.png → 本地切分三视图（零 GPU）。

    form-data: file（图片，必填）, project_name, character, outfit_key?（空=主设定）,
               overwrite?（"1"/"true" 强制覆盖）

    返回：{success, character, base, views:{front,left,back,half}, derive_error?}
      · derive_error 非空 = 图已落 base.png 但**版式不符未能切分**（下游会回落
        整图，属可用状态），此时 success 仍为 True 但带 `derive_ok: false` 提示。
      · 只有「图本身不可读 / 版式不符且 overwrite 已覆盖旧图」才 4xx/回滚。
    """
    files = request.files.getlist('file') or request.files.getlist('files')
    if not files:
        return jsonify({"success": False,
                        "error": "未收到图片，请通过 file 字段上传"}), 400
    f = files[0]
    project_name, err = _project_or_400(
        (request.form.get('project_name') or request.args.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (request.form.get('character') or request.args.get('character') or '').strip()
    outfit_key = _sanitize_outfit_key(
        request.form.get('outfit_key') or request.args.get('outfit_key'))
    _ow_raw = (request.form.get('overwrite') or request.args.get('overwrite') or '').strip()
    upload_role = (request.form.get('asset_role') or request.args.get('asset_role')
                   or request.form.get('upload_type') or request.args.get('upload_type')
                   or 'sheet').strip().lower()
    # 宽容布尔（与 qc_client._as_bool 同口径）：表单/query 传 "1"/"true"/"on"/"yes" 都算真
    overwrite = _ow_raw.lower() not in ("", "0", "false", "no", "off")

    # 角色名是路径段，与 api_character_outfit_generate 同一守卫口径
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400
    # outfit_key 传了但非法（非空且清洗后为空）→ 明确拒绝，不静默当主设定
    _ow_in = (request.form.get('outfit_key') or request.args.get('outfit_key') or '').strip()
    if _ow_in and not outfit_key:
        return jsonify({"success": False,
                        "error": "outfit_key 非法（≤40 字符，剔除 \\ / : * ? \" < > |）"}), 400

    from PIL import Image
    raw_name = _safe_upload_name(f.filename)
    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in _UPLOAD_IMAGE_EXTS:
        return jsonify({"success": False,
                        "error": f"不支持的图片格式 {ext or '（无扩展名）'}；"
                                 f"支持 {'、'.join(_UPLOAD_IMAGE_EXTS)}"}), 400

    _role_alias = {
        "sheet": "sheet", "master": "sheet", "character_sheet": "sheet",
        "face": "face_front", "face_front": "face_front",
        "full": "full_front", "full_front": "full_front",
        "half": "half_front", "half_front": "half_front",
        "bust": "bust_front", "bust_front": "bust_front",
        "back": "full_back", "full_back": "full_back",
    }
    upload_role = _role_alias.get(upload_role, upload_role)
    if upload_role != "sheet" and upload_role not in (
            "face_front", "full_front", "half_front", "bust_front", "full_back"):
        return jsonify({"success": False,
                        "error": f"asset_role 非法：{upload_role}"}), 400
    asset_dir = (_character_outfit_dir(project_name, character, outfit_key) if outfit_key
                 else os.path.join(CHARACTERS_DIR, project_name, character))
    base_dst = os.path.join(asset_dir, "base.png")
    if upload_role != "sheet":
        # 单角色正面/脸部图直接落语义机器锚点，不覆盖 master sheet。
        _subdir = {"face_front": "identity", "bust_front": "framing",
                   "half_front": "framing", "full_back": "body",
                   "full_front": "body"}.get(upload_role, "")
        _target_dir = os.path.join(asset_dir, _subdir) if _subdir else asset_dir
        os.makedirs(_target_dir, exist_ok=True)
        _target = os.path.join(_target_dir, f"{upload_role}.png")
        if not overwrite and os.path.isfile(_target) and os.path.getsize(_target) > 0:
            return jsonify({"success": True, "skipped": True, "character": character,
                            "outfit_key": outfit_key, "asset_role": upload_role,
                            "path": _target, "message": "该机器锚点已存在"})
        try:
            with Image.open(f.stream if hasattr(f, "stream") else f) as _im:
                _im.convert("RGB").save(_target, format="PNG")
        except Exception:
            f.stream.seek(0)
            with Image.open(f.stream) as _im:
                _im.convert("RGB").save(_target, format="PNG")
        character_assets.write_anchor_metadata(
            asset_dir, upload_role, path=_target, source="user_upload",
            role=("identity" if upload_role.startswith("face") else
                  "body" if upload_role.startswith("full") else "framing"),
            view="front" if "front" in upload_role else upload_role,
            framing=("face" if upload_role.startswith("face") else
                     "half" if upload_role == "half_front" else "full"),
            outfit_key=outfit_key,
            qc={"accept": True, "blocked": False, "skipped": True,
                "label": "用户上传机器锚点", "reason": "用户上传单图，不做图片质检",
                "critical_issues": []})
        return jsonify({"success": True, "skipped": False, "character": character,
                        "outfit_key": outfit_key, "asset_role": upload_role,
                        "path": _target, "base": base_dst,
                        "anchors": character_assets.scan_character_assets(
                            asset_dir, asset_dir if outfit_key else "")["anchors"],
                        "completeness": _character_asset_completeness(
                            asset_dir, asset_dir if outfit_key else ""),
                        "message": "已上传独立 Machine Anchor"})
    if not overwrite and os.path.isfile(base_dst) and os.path.getsize(base_dst) > 0:
        try:
            identity_contract.ensure_identity_assets(asset_dir, base_dst, logger=app.logger)
        except Exception as _ide:
            app.logger.warning("上传形象图：身份锚点派生失败（忽略）：%s", _ide)
        return jsonify({"success": True, "skipped": True, "character": character,
                        "outfit_key": outfit_key, "base": base_dst,
                        "message": "该角色已有设定图（base.png 已就绪）；"
                                   "如需替换请带 overwrite=true"})

    # 暂存上传件 → 校验可解码 → 统一转 PNG 落 base.png
    os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(
        UPLOAD_TMP_DIR,
        f"sheet_{time.strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}{ext}")
    _old_backup = ""
    try:
        f.save(tmp_path)
        try:
            from PIL import Image
            with Image.open(tmp_path) as _im:
                _w, _h = _im.size
                _im.convert("RGB")
        except Exception as _ie:  # noqa: BLE001 不可解码的图绝不能入库
            return jsonify({"success": False,
                            "error": f"图片无法读取或已损坏：{type(_ie).__name__} {_ie}"}), 400

        os.makedirs(asset_dir, exist_ok=True)
        # overwrite 时先把旧 base 挪走做回滚点：切分失败要能把旧状复原
        if os.path.isfile(base_dst):
            _old_backup = base_dst + ".preupload.bak"
            try:
                shutil.copy2(base_dst, _old_backup)
            except OSError as _be:
                app.logger.warning("上传形象图：旧 base.png 备份失败（忽略）：%s", _be)
                _old_backup = ""
        with Image.open(tmp_path) as _im:
            _im.convert("RGB").save(base_dst, format="PNG")
        app.logger.info("[上传形象图] %s/%s%s ← %s（%dx%d）",
                        project_name, character, f" ({outfit_key})" if outfit_key else "",
                        raw_name, _w, _h)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # ---- 2026-10-02 用户指定：角色图**不裁剪** → 不再切分三视图 ----
    # 角色设定图已改为英文四区 character sheet（见 comfyui_client._CHARACTER_SHEET_EN_LAYOUT），
    # 四区不对称布局无法切分 → 上传图直接落 base.png 整图，下游取整图。
    # 并清掉旧视角图（front/left/back/half），避免 _ASSET_IMG_PRIORITY 取到旧单视角。
    views: dict = {}
    # 上传 master sheet 不删除旧 front/half/back 或新机器锚点：旧项目兼容与
    # 单档补全都依赖它们；selector 负责按镜头选图。

    if _old_backup:
        try:
            os.remove(_old_backup)
        except OSError:
            pass

    # 旁路元数据：显式标注「用户上传」，与模型生成的资产可区分、可追溯
    _view_gate = {"accept": True, "blocked": False, "skipped": True,
                  "label": "用户上传（未质检）",
                  "reason": "用户上传的定稿形象图，不做图片质检", "critical_issues": []}
    try:
        _write_artifact_meta(
            base_dst, kind="asset_base", project_name=project_name,
            seed=None, prompt="", workflow_key=None, qc=_view_gate,
            asset_name=character,
            extra={"asset_type": "character", "source": "user_upload",
                   "original_filename": raw_name, "outfit_key": outfit_key or None})
    except Exception as _me:  # noqa: BLE001
        app.logger.warning("上传形象图：元数据写入失败（忽略）：%s", _me)

    try:
        identity_contract.ensure_identity_assets(asset_dir, base_dst, logger=app.logger)
    except Exception as _ide:
        app.logger.warning("上传形象图：身份锚点派生失败（忽略）：%s", _ide)

    return jsonify({"success": True, "derive_ok": True, "skipped": False,
                    "character": character, "outfit_key": outfit_key,
                    "base": base_dst,
                    "views": {},
                    "view_files": {},
                    "anchors": character_assets.scan_character_assets(
                        asset_dir, asset_dir if outfit_key else "")["anchors"],
                    "completeness": _character_asset_completeness(
                        asset_dir, asset_dir if outfit_key else ""),
                    "layout_hint": "Master Sheet 已保存；生产镜头由 Machine Anchor selector 选图",
                    "message": "已上传 master sheet（生产参考将按镜头选择独立 Machine Anchor）"})


@bp.route('/api/assets/character/anchors', methods=['GET'])
def api_character_anchors():
    """查询角色 Master Sheet、Machine Anchors、manifest 与 completeness。"""
    project_name, err = _project_or_400(
        (request.args.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (request.args.get('character') or '').strip()
    outfit_key = _sanitize_outfit_key(request.args.get('outfit_key'))
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False, "error": "character 非法"}), 400
    asset_dir = (_character_outfit_dir(project_name, character, outfit_key) if outfit_key
                 else os.path.join(CHARACTERS_DIR, project_name, character))
    if not os.path.isdir(asset_dir):
        return jsonify({"success": False, "error": "未找到角色资产目录"}), 404
    scan = character_assets.scan_character_assets(asset_dir)
    return jsonify({
        "success": True, "project_name": project_name, "character": character,
        "outfit_key": outfit_key, "dir": asset_dir,
        "sheet": scan["sheet"], "anchors": scan["anchors"],
        "sources": scan["sources"], "manifest": scan["manifest"],
        "completeness": _character_asset_completeness(asset_dir),
        "layout_version": CHARACTER_SHEET_LAYOUT_VERSION,
        "required": list(CHARACTER_REQUIRED_ANCHORS),
    })


@bp.route('/api/assets/character/anchor/regenerate', methods=['POST'])
def api_character_anchor_regenerate():
    """单独重生成一个角色 Machine Anchor（不覆盖已通过的其他档位）。"""
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (data.get('character') or '').strip()
    anchor = str(data.get('anchor') or data.get('asset_role') or '').strip()
    outfit_key = _sanitize_outfit_key(data.get('outfit_key'))
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False, "error": "character 非法"}), 400
    if anchor not in CHARACTER_ANCHOR_KEYS:
        return jsonify({"success": False,
                        "error": f"anchor 非法，可选：{', '.join(CHARACTER_ANCHOR_KEYS)}"}), 400
    asset_dir = (_character_outfit_dir(project_name, character, outfit_key) if outfit_key
                 else os.path.join(CHARACTERS_DIR, project_name, character))
    base_png = os.path.join(asset_dir, "base.png")
    if not os.path.isfile(base_png):
        return jsonify({"success": False, "error": "缺少 master base.png"}), 404

    prompt = _character_base_prompt(project_name, character)
    if outfit_key:
        prompt = _append_outfit_prompt(prompt, _outfit_desc_of(asset_dir))
    if not prompt:
        meta_path = os.path.join(asset_dir, "base.png.meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as _mf:
                prompt = str((json.load(_mf) or {}).get("prompt") or "")
        except Exception:
            prompt = ""
    style = str(data.get('style') or _project_style(project_name) or '')
    _res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO)
    _size = style_kit.aspect_size(_res["ratio"], style_kit.asset_megapixels())
    task_id = f"character_anchor_{project_name}_{uuid.uuid4().hex[:10]}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "phase": f"生成角色锚点 {anchor}",
            "project": project_name, "character": character, "anchor": anchor,
            "progress": 0, "total": 1, "current": 0, "results": [],
        }

    def _worker():
        try:
            with gpu_task_gate.run_gpu_task(task_id, f"角色锚点 {character}/{anchor}"):
                result = _generate_character_machine_anchors(
                    asset_dir, prompt, style=_res["style"], size=_size,
                    seed=int(data.get('seed') or random.randint(1, 2 ** 31 - 1)),
                    qc_cfg=_qc_load_cfg(),
                    qc_on=qc_client.image_qc_ready(_qc_load_cfg()),
                    project_name=project_name, asset_name=character,
                    only_keys=[anchor])
                ok = bool(result.get("generated") or result.get("existing"))
                with lock:
                    generation_state[task_id].update({
                        "status": "done" if ok else "failed",
                        "success": ok, "progress": 100,
                        "phase": "完成" if ok else "生成失败",
                        "results": [result],
                        "error": "" if ok else "；".join(result.get("errors") or []),
                        "completeness": _character_asset_completeness(asset_dir),
                    })
        except Exception as exc:  # noqa: BLE001
            with lock:
                generation_state[task_id].update({
                    "status": "failed", "success": False, "phase": "生成失败",
                    "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "anchor": anchor, "character": character,
                    "outfit_key": outfit_key})


@bp.route('/api/assets/character/outfit', methods=['POST'])
def api_character_outfit_generate():
    """生成角色服装变体（衣柜）。

    body = {project_name, character(角色名), outfit_key, outfit_desc, style?, overwrite?}
    返回 {task_id, status:"started"}（与 api_generate_assets 同构，进度轮询
    /api/generation/status/<task_id>）。已存在同 outfit_key 且 base.png 非空 →
    默认跳过（A-2 overwrite 语义，overwrite=true 强制重画）。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (data.get('character') or '').strip()
    outfit_key = _sanitize_outfit_key(data.get('outfit_key'))
    outfit_desc = str(data.get('outfit_desc') or '').strip()
    overwrite = bool(data.get('overwrite'))
    # 角色名是路径段，与 api_autopilot_reset_asset 同一守卫口径
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400
    if not outfit_key:
        return jsonify({"success": False,
                        "error": "outfit_key 不能为空（≤40 字符，剔除 \\ / : * ? \" < > |）"}), 400
    if not outfit_desc:
        return jsonify({"success": False, "error": "outfit_desc（服装描述）不能为空"}), 400

    outfit_dir = _character_outfit_dir(project_name, character, outfit_key)
    # A-2 断点续跑语义：已有同 key 且 base.png 非空 → 默认跳过（幂等入口）
    _base_png = os.path.join(outfit_dir, "base.png")
    if not overwrite and os.path.isfile(_base_png) and os.path.getsize(_base_png) > 0:
        return jsonify({"success": True, "skipped": True, "task_id": "",
                        "outfit_key": outfit_key, "character": character,
                        "message": "该服装变体已存在（base.png 已就绪）；如需重画请带 overwrite=true"})

    # 角色基础设定（剧本优先，回落主设定 meta）→ 追加服装描述（幂等）
    _base_prompt = _character_base_prompt(project_name, character)
    _merged_prompt = _append_outfit_prompt(_base_prompt, outfit_desc)
    asset = _find_script_character(project_name, character)
    asset.update({
        "name": character,
        "reference_prompt_zh": _merged_prompt,
        "prompt_zh": _merged_prompt,
    })
    if not str(asset.get("appearance") or "").strip():
        # 剧本里没有 appearance 时把合并提示词兜进去，保证 qc_desc / 性别判据有料可用
        asset["appearance"] = _merged_prompt

    # 服装档案：先落 outfit.json（查询端点回显 desc 用；写失败不影响生成 ——
    # ready 判据看 base.png，desc 还有 meta sidecar 兜底）
    try:
        os.makedirs(outfit_dir, exist_ok=True)
        atomic_write_json(os.path.join(outfit_dir, _OUTFIT_RECORD_FILE), {
            "outfit_key": outfit_key,
            "desc": outfit_desc,
            "character": character,
            "project": project_name,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as _oe:  # noqa: BLE001
        app.logger.warning("服装变体档案写入失败（不影响生成）：%s", _oe)

    task_id = f"outfit_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "asset_type": "character",
            "progress": 0, "total": 1, "current": 0,
            "phase": "基础图", "results": [],
            "overwrite": overwrite,
            "project_name": project_name, "step": "asset_outfit",
            "character": character, "outfit_key": outfit_key,
        }
    # 线程 + 状态登记照抄 api_generate_assets（裸线程，资产链路不进 GPU 闸门）；
    # 生成→质检→重试→切分全链路由 _generate_asset_task 承担，变体经 sub_dir 落位
    thread = threading.Thread(
        target=_generate_asset_task,
        args=(task_id, [asset], "character", project_name,
              data.get('style') or _project_style(project_name), overwrite,
              os.path.join(_OUTFITS_DIRNAME, outfit_key))
    )
    thread.daemon = True
    thread.start()
    app.logger.info("[服装变体] 项目=%s 角色=%s 服装=%s（overwrite=%s）任务=%s 已启动",
                    project_name, character, outfit_key, overwrite, task_id)
    return jsonify({"task_id": task_id, "status": "started",
                    "outfit_key": outfit_key, "character": character})


@bp.route('/api/assets/character/outfits', methods=['GET'])
def api_character_outfits_list():
    """列出角色服装变体（衣柜）：query = project_name, character。

    返回 [{outfit_key, desc, ready(base.png 存在且>0字节),
    views:{front/left/back/half 是否存在}}]。outfits 目录不存在 / 任何读取异常
    → 空数组（fail-open，绝不抛错）。
    """
    project_name = _safe_project((request.args.get('project_name') or '').strip())
    character = (request.args.get('character') or '').strip()
    if not project_name or not character or '/' in character or '\\' in character \
            or character in ('.', '..'):
        return jsonify({"success": False, "error": "project_name 与 character 必填",
                        "outfits": []}), 400
    outfits_root = _character_outfit_dir(project_name, character)
    out = []
    if not os.path.isdir(outfits_root):
        # 目录不存在 = 该角色还没做过服装变体（正常态，不是错误）
        return jsonify({"success": True, "outfits": out})
    try:
        # 纵深防御（安全复查 2026-10-02）：listdir 条目本不可能携带路径分隔符
        # （listdir 不返回 ./..，文件名也无法含 \ /），此处仍显式校验 realpath
        # 未越出 outfits_root，阻断符号链接等非常规文件系统状态造成的目录逃逸。
        _root_real = os.path.realpath(outfits_root)
        for _dir_name in sorted(os.listdir(outfits_root)):
            _od = os.path.join(outfits_root, _dir_name)
            if not os.path.isdir(_od):
                continue
            if not os.path.realpath(_od).startswith(_root_real + os.sep):
                app.logger.warning("[服装变体] 异常目录项已跳过（越界防护）：%s", _dir_name)
                continue
            _base_png = os.path.join(_od, "base.png")
            _ready = os.path.isfile(_base_png) and os.path.getsize(_base_png) > 0
            _scan = character_assets.scan_character_assets(_od)
            _views = {}
            for _stem in _OUTFIT_VIEW_STEMS:
                _vp = os.path.join(_od, f"{_stem}.png")
                _views[_stem] = os.path.isfile(_vp) and os.path.getsize(_vp) > 0
            out.append({
                "outfit_key": _dir_name,
                "desc": _outfit_desc_of(_od),
                "ready": _ready,
                "views": _views,
                "sheet": _scan["sheet"],
                "anchors": _scan["anchors"],
                "completeness": _character_asset_completeness(_od),
            })
    except Exception as e:  # noqa: BLE001  目录枚举失败按空数组处理（fail-open）
        app.logger.warning("服装变体列表读取失败（返回空数组）：%s", e)
        out = []
    return jsonify({"success": True, "outfits": out})


# ===== 静态资产访问 =====

@bp.route('/api/assets/<path:filename>')
def api_asset_file(filename):
    """提供资产文件访问

    ⭐ 2026-10-05 资产自动刷新：资产图会被**原地覆盖重生成**（路径不变），必须让浏览器
    每次回源校验，否则轮询拿到新 JSON 后 <img> 仍显示旧缓存图，用户以为「必须手动刷新」。
    与前端 ?v=<mtime> 双重保险：前端换 URL 触发重挂（主），本处 no-cache 兜底回源。
    """
    resp = _serve_safe(os.path.join(PROJECT_OUTPUT_DIR, "assets"), filename)
    try:
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    except Exception:  # noqa: BLE001  abort 响应(403/404) 无 headers，忽略
        pass
    return resp


# ===================== 角色管理API =====================

@bp.route('/api/characters', methods=['GET'])
@_autopilot_guard
def api_list_characters():
    """列出项目所有角色"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)
    characters = mgr.get_all_characters()
    return jsonify({"success": True, "characters": characters})


@bp.route('/api/characters', methods=['POST'])
@_autopilot_guard
def api_add_character():
    """添加新角色"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    name = data.get('name', '')
    role = data.get('role', '配角')
    description = data.get('description', '')
    outfit = data.get('outfit', '')

    if not name:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)
    char_id = mgr.add_character(name, role, description, outfit)

    return jsonify({"success": True, "character_id": char_id, "name": name})


@bp.route('/api/characters/<char_id>', methods=['PUT'])
@_autopilot_guard
def api_update_character(char_id):
    """更新角色信息"""
    data = request.get_json(silent=True) or {}
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(data.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)
    mgr.update_character(
        char_id,
        name=data.get('name'),
        role=data.get('role'),
        description=data.get('description'),
        outfit=data.get('outfit'),
        status=data.get('status')
    )
    return jsonify({"success": True})


@bp.route('/api/characters/<char_id>/reference', methods=['POST'])
@_autopilot_guard
def api_upload_character_reference(char_id):
    """上传角色参考图"""
    project_name = request.form.get('project')
    view_type = request.form.get('view_type', 'front')
    file = request.files.get('image')

    if not file:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)

    # 保存上传的文件
    upload_dir = os.path.join(project_dir, "characters", "references")
    os.makedirs(upload_dir, exist_ok=True)
    ext = os.path.splitext(file.filename)[1]
    filename = f"{char_id}_{view_type}{ext}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)

    mgr.set_reference(char_id, view_type, filepath)
    return jsonify({"success": True, "path": filepath})


@bp.route('/api/characters/<char_id>/prompt', methods=['GET'])
@_autopilot_guard
def api_get_character_prompt(char_id):
    """获取角色生成提示词"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err
    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)
    prompt = mgr.get_character_prompt(char_id)
    return jsonify({"success": True, "prompt": prompt})

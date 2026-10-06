# -*- coding: utf-8 -*-
"""projects 域蓝图：项目 CRUD、封面、资产明细、资产沉淀。

2026-10-07 由 ``app/app.py`` 按域拆分而来，**纯搬迁**：路径、方法、报错文案逐字
不变（守门脚本 ``scripts/snapshot_routes.py`` 比对 rule + methods）。
``url_prefix`` 留空，路径直接写死在各 ``@bp.route`` 上。
"""
from __future__ import annotations

from flask import Blueprint

# 共享 runtime 上下文（app 实例 / 212 个模块级 helper / config 常量）。
# __all__ 里显式列出了下划线开头的名字，故这里能整体取到。
from api._shared import *  # noqa: F401,F403

bp = Blueprint("projects", __name__)
DOMAIN = "projects"

# ===== 项目管理（A：每部小说 = 一个独立项目） =====

@bp.route('/api/projects', methods=['GET', 'POST'])
def api_projects_list():
    if request.method == 'POST':
        return api_projects_create()
    with_stats = (request.args.get('stats', '1') not in ('0', 'false', 'no'))
    projects = project_store.list_projects(with_stats=with_stats)
    return jsonify({"success": True, "total": len(projects), "projects": projects,
                    "index_path": project_store.PROJECT_INDEX_PATH})


@bp.route('/api/projects', methods=['POST'])
def api_projects_create():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "项目名称不能为空"}), 400
    pid = (data.get('id') or '').strip() or None
    existing = project_store.get_project(pid) if pid else None
    if existing:
        return jsonify({"error": f"项目 ID 已存在：{pid}", "project": existing}), 409
    novel_id = (data.get('novel_id') or '').strip()
    novel_name = (data.get('novel_name') or '').strip()
    # 前端只传 novel_id，不传 novel_name。此前直接 `or name` 兜底，等于把
    # 「小说名」写成了「项目名」（两者通常不同，项目名可以是任意自定义名称）。
    # 这里回查小说库取真实标题，取不到才退回项目名。
    if novel_id and not novel_name:
        try:
            meta = get_novel(NOVELS_DIR, novel_id) or {}
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"读取小说元信息失败 {novel_id}: {e}")
            meta = {}
        novel_name = str(meta.get('title') or meta.get('name') or '').strip()
    # 视频生成方式（项目级）：允许前端放在 config 里，也允许顶层 video_mode 直传；
    # 两者都走 create_project 内的 norm_video_mode 归一（非法值回落 episode）。
    _cfg_in = dict(data.get('config')) if isinstance(data.get('config'), dict) else {}
    if str(data.get('video_mode') or '').strip():
        _cfg_in['video_mode'] = data.get('video_mode')
    rec = project_store.create_project(
        name,
        novel_id=novel_id,
        novel_name=novel_name or name,
        config=_cfg_in or None,
        pid=pid,
    )
    return jsonify({"success": True, "project": project_store.summarize(rec["id"]),
                    "created": True})


@bp.route('/api/projects/ensure-for-novel', methods=['POST'])
def api_projects_ensure_for_novel():
    """确保某部小说有对应项目（有则复用，无则创建），并绑定小说

    自动生产控制台的第 1 步：用户上传小说后，前端立刻调用本接口把项目建好并选中，
    避免「小说传上来了但托管没项目可生产」的断档。名称取小说标题（清洗后）。
    """
    data = request.json or {}
    novel_id = str(data.get('novel_id') or '').strip()
    if not novel_id:
        return jsonify({"success": False, "error": "缺少 novel_id"}), 400
    meta = {}
    try:
        meta = get_novel(NOVELS_DIR, novel_id) or {}
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"小说读取失败：{e}"}), 400
    if not meta:
        return jsonify({"success": False, "error": f"小说不存在：{novel_id}"}), 404
    raw_name = (data.get('name') or meta.get('title') or meta.get('name')
                or novel_id)
    name = re.sub(r"[《》〈〉【】「」『』\s]+", "", str(raw_name)).strip() or novel_id
    # 同一个「新建项目」入口可能走本接口（上传小说后自动建项目）→ 同样要能带上
    # 用户选的视频生成方式，否则只有「显式创建」那条路生效、这条路悄悄回落默认值。
    _cfg_in = dict(data.get('config')) if isinstance(data.get('config'), dict) else {}
    if str(data.get('video_mode') or '').strip():
        _cfg_in['video_mode'] = data.get('video_mode')
    rec = project_store.ensure_project_for_novel(novel_id, name, config=_cfg_in or None)
    if not rec:
        return jsonify({"success": False, "error": "项目创建失败"}), 500
    return jsonify({"success": True, "project": project_store.summarize(rec["id"]),
                    "novel_id": novel_id,
                    "project_key": rec.get("dir_key") or rec.get("name")})


@bp.route('/api/projects/<path:pid>', methods=['GET'])
def api_project_detail(pid):
    detail = project_store.summarize(pid)
    if not detail:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    scripts = []
    for sp in project_store.project_scripts(detail["dir_key"]):
        st = project_store.script_stats(sp)
        st.update({"path": sp})
        scripts.append(st)
    detail["scripts"] = scripts
    detail["gallery_summary"] = project_store.gallery_summary(detail["dir_key"])
    detail["product_dirs"] = {
        k: [os.path.basename(d) for d in project_store.project_dirs(v, detail["dir_key"])]
        for k, v in {"characters": CHARACTERS_DIR, "items": ITEMS_DIR, "scenes": SCENES_DIR,
                     "storyboards": STORYBOARDS_DIR, "videos": VIDEOS_DIR,
                     "final": FINAL_DIR, "upscale": UPSCALE_DIR, "dub": DUB_DIR}.items()
    }
    return jsonify({"success": True, "project": detail})


@bp.route('/api/projects/<path:pid>/detail', methods=['GET'])
def api_project_detail_alias(pid):
    return api_project_detail(pid)


@bp.route('/api/projects/<path:pid>/novel-brief', methods=['GET'])
def api_project_novel_brief(pid):
    """本项目「原著简报」：书名 + 章节数 + 开篇正文 + 已定风格。

    ⚠️ 2026-09-19 用户旅程实测教训：AI 总控此前**没有任何读取本项目小说的能力**，
    而它在「启动生产前先与用户沟通风格」这一步必须知道原著讲什么。缺了这个接口，
    模型只能靠上下文里别的东西瞎猜 —— 实测拿《蛊真人》的方案回答了《铜铃巷》的项目。
    """
    rec = project_store.summarize(pid)
    if not rec:
        return jsonify({"success": False, "error": f"项目不存在：{pid}"}), 404
    dir_key = rec.get("dir_key") or rec.get("key") or pid
    try:
        cfg = project_store.read_config(dir_key) or {}
    except Exception as e:  # noqa: BLE001
        app.logger.warning("读取项目配置失败（%s）：%s", dir_key, e)
        cfg = {}
    novel_id = str(rec.get("novel_id") or "")
    out = {
        "project": rec.get("name") or dir_key,
        "project_key": dir_key,
        "style": cfg.get("style") or "",
        "art_style": cfg.get("art_style") or "",
        "episodes": cfg.get("episodes"),
        "target_shots": cfg.get("target_shots") or cfg.get("shots_per_episode"),
        "novel_id": novel_id,
        "novel_name": rec.get("novel_name") or "",
        "novel_title": "",
        "chapter_count": 0,
        "preview": "",
        "preview_chars": 0,
        "total_chars": 0,
        "note": "",
    }
    if not novel_id:
        out["note"] = "该项目未关联小说，无法给出原著依据"
        return jsonify({"success": True, **out})
    try:
        meta = get_novel(NOVELS_DIR, novel_id) or {}
        out["novel_title"] = meta.get("title") or ""
        out["chapter_count"] = len(meta.get("chapters") or [])
    except Exception as e:  # noqa: BLE001
        app.logger.warning("读取小说元信息失败（%s）：%s", novel_id, e)
    try:
        pv = preview_novel(NOVELS_DIR, novel_id, offset=0, limit=NOVEL_BRIEF_CHARS)
        out["preview"] = pv.get("text") or ""
        out["preview_chars"] = len(out["preview"])
        out["total_chars"] = pv.get("total_chars") or 0
    except Exception as e:  # noqa: BLE001
        out["note"] = f"原著正文读取失败：{e}"
        app.logger.warning("读取小说正文失败（%s）：%s", novel_id, e)
    return jsonify({"success": True, **out})


@bp.route('/api/projects/<path:pid>/rename', methods=['POST'])
def api_project_rename(pid):
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "新项目名不能为空"}), 400
    rec = project_store.rename_project(pid, name)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    return jsonify({"success": True, "project": project_store.summarize(rec["id"]),
                    "note": "项目键与目录未变，已有产物保持原样，零丢失"})


@bp.route('/api/projects/<path:pid>/config', methods=['GET', 'POST'])
def api_project_config(pid):
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    if request.method == 'GET':
        return jsonify({"success": True, "project": rec["dir_key"],
                        "config": project_store.read_config(rec["dir_key"]),
                        "config_path": project_store.paths(rec["dir_key"])["config"]})
    cfg = project_store.update_config(rec["dir_key"], request.json or {})
    return jsonify({"success": True, "project": rec["dir_key"], "config": cfg,
                    "project_record": project_store.get_project(rec["id"])})


@bp.route('/api/projects/<path:pid>/cover')
def api_project_cover(pid):
    """项目封面图（无封面 404，前端回落占位图标）"""
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    cover = _project_cover_path(rec["dir_key"])
    if not os.path.isfile(cover):
        abort(404)
    return send_file(cover, conditional=True)


@bp.route('/api/projects/<path:pid>/cover/generate', methods=['POST'])
def api_project_cover_generate(pid):
    """生成项目封面（同步阻塞，单图 t2i 约 10~60s）。

    故意不设 AI/总控确认门禁：封面属装饰性产物（与分镜图同理不挂 LLM 门禁），
    且项目刚建时总控设定往往还没敲定，门禁会把「建完项目就想给个封面」拦死。
    body 可选 {"seed": int}（换一张）。
    """
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    data = request.json or {}
    try:
        cover = _generate_project_cover(rec, seed=data.get('seed'))
    except Exception as e:  # noqa: BLE001
        app.logger.error(f"封面生成失败 {rec['dir_key']}: {e}")
        return jsonify({"success": False, "error": f"封面生成失败：{e}"}), 500
    return jsonify({"success": True, "cover_path": cover,
                    "cover_url": f"/api/projects/{rec['dir_key']}/cover?t={int(time.time())}"})


@bp.route('/api/projects/<path:pid>/delete', methods=['POST'])
def api_project_delete(pid):
    data = request.json or {}
    confirm = bool(data.get('confirm'))
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    if not confirm:
        return jsonify({
            "error": "删除项目需要二次确认",
            "requires_confirm": True,
            "warning": (f"将把项目《{rec['name']}》的工作区与全部产物目录"
                        f"（剧本/角色/物品/场景/分镜/视频/成片/超分/配音/质检）"
                        f"整体移入回收站 {project_store.PROJECT_TRASH_DIR}，可从磁盘还原，非物理删除。"),
            "project": rec,
            "stats": project_store.project_stats(rec["dir_key"]),
        }), 409
    result = project_store.delete_project(rec["id"], confirm=True)
    return jsonify({"success": True, **result})


@bp.route('/api/projects/migrate', methods=['POST'])
def api_projects_migrate():
    report = project_store.migrate_legacy()
    return jsonify({"success": True, "report": report,
                    "report_path": project_store.PROJECT_MIGRATE_REPORT})


@bp.route('/api/projects/<path:pid>/scripts', methods=['GET'])
def api_project_scripts(pid):
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    rows = []
    for sp in project_store.project_scripts(rec["dir_key"]):
        st = project_store.script_stats(sp)
        st["path"] = sp
        rows.append(st)
    return jsonify({"success": True, "project": rec["dir_key"], "total": len(rows),
                    "scripts": rows})


@bp.route('/api/projects/<path:pid>/assets', methods=['GET'])
def api_project_assets(pid):
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    key = rec["dir_key"]
    gallery = project_store.asset_gallery(key)
    # 分镜图（点击可进入详情）
    storyboards = []
    for d in project_store.project_dirs(project_store.paths(key)["storyboards"], key):
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".png"):
                continue
            full = os.path.join(d, fn)
            storyboards.append({
                "name": os.path.splitext(fn)[0],
                "file": full,
                "size": os.path.getsize(full),
                "url": f"/api/storyboards/file/{os.path.basename(d)}/{fn}",
                # 2026-10-05：mtime 版本令牌（前端拼 ?v= 击穿缓存，避免重生成后仍显示旧图）
                "mtime": int(os.path.getmtime(full)),
                "project_dir": os.path.basename(d),
            })
    # 视频 / 成片 / 超分 / 配音 产物（按项目隔离后的实际目录）
    def _files(root, exts):
        out = []
        for d in project_store.project_dirs(root, key):
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith(exts):
                    full = os.path.join(d, fn)
                    out.append({"name": fn, "file": full, "size": os.path.getsize(full),
                                # 2026-10-05：mtime 版本令牌（前端拼 ?v= 击穿缓存，避免重生成后仍显示旧图）
                                "mtime": int(os.path.getmtime(full)),
                                "project_dir": os.path.basename(d)})
    p = project_store.paths(key)
    return jsonify({"success": True, "project": key, "project_id": rec["id"],
                    "gallery": gallery,
                    "storyboards": storyboards,
                    "videos": _files(p["videos"], (".mp4", ".mov", ".mkv")),
                    "final": _files(p["final"], (".mp4", ".mov", ".mkv")),
                    "upscale": _files(p["upscale"], (".mp4", ".mov", ".mkv")),
                    "dub": _files(p["dub"], (".wav", ".flac", ".mp3")),
                    "counts": {k: len(v) for k, v in gallery.items()},
                    "product_dirs": {k: [os.path.basename(d) for d in project_store.project_dirs(v, key)]
                                     for k, v in {"characters": CHARACTERS_DIR, "items": ITEMS_DIR,
                                                  "scenes": SCENES_DIR, "storyboards": STORYBOARDS_DIR,
                                                  "videos": VIDEOS_DIR, "final": FINAL_DIR,
                                                  "upscale": UPSCALE_DIR, "dub": DUB_DIR}.items()}})


@bp.route('/api/projects/<path:pid>/asset-detail', methods=['GET'])
def api_project_asset_detail(pid):
    """资产条目点击后的详情：大图列表 + 名称 / 提示词 / 所属镜头 + 下载 + 重新生成入口"""
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    kind = (request.args.get('kind') or 'character').strip().lower()
    name = (request.args.get('name') or '').strip()
    key = rec["dir_key"]
    p = project_store.paths(key)

    kind_map = {
        "character": ("characters", p["characters"], "character"),
        "characters": ("characters", p["characters"], "character"),
        "item": ("items", p["items"], "item"),
        "items": ("items", p["items"], "item"),
        "scene": ("scenes", p["scenes"], "scene"),
        "scenes": ("scenes", p["scenes"], "scene"),
        "storyboard": ("storyboards", p["storyboards"], "storyboard"),
        "storyboards": ("storyboards", p["storyboards"], "storyboard"),
    }
    if kind not in kind_map:
        return jsonify({"error": f"不支持的资产类型：{kind}",
                        "supported": sorted(set(kind_map.keys()))}), 400
    folder_kind, folder, asset_type = kind_map[kind]

    detail_scripts = []
    for sp in project_store.project_scripts(key):
        try:
            with open(sp, "r", encoding="utf-8") as f:
                detail_scripts.append({"path": sp, "data": json.load(f)})
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"剧本读取失败（{sp}）：{e}")

    if asset_type == "storyboard":
        sid = re.sub(r"[^0-9]", "", name) or "1"
        png = project_store.find_asset_file(key, "storyboards", f"shot_{int(sid):02d}.png") \
            if sid.isdigit() else ""
        if not png:
            hits = []
            for d in project_store.project_dirs(p["storyboards"], key):
                hits += [os.path.join(d, f) for f in sorted(os.listdir(d))
                         if f.lower().endswith('.png') and sid in f]
            png = hits[0] if hits else ""
        views = [{
            "view": "storyboard", "file": png, "size": os.path.getsize(png),
            "url": f"/api/storyboards/file/{os.path.basename(os.path.dirname(png))}/{os.path.basename(png)}",
            "download_url": f"/api/storyboards/file/{os.path.basename(os.path.dirname(png))}/{os.path.basename(png)}",
        }] if png and os.path.exists(png) else []
        shot_meta = {}
        for sp in detail_scripts:
            for sh in (sp["data"].get("shots") or []):
                if str(sh.get("shot_id")) == str(int(sid) if sid.isdigit() else sid):
                    shot_meta = {k: sh.get(k) for k in
                                 ("shot_id", "duration", "camera", "location", "description",
                                  "dialogue", "emotion", "prompt_h3", "episode")}
                    shot_meta["script_path"] = sp["path"]
                    break
            if shot_meta:
                break
        manifest = {}
        for d in project_store.project_dirs(p["storyboards"], key):
            mpath = os.path.join(d, "storyboard_manifest.json")
            if os.path.exists(mpath):
                manifest = (project_store._read_json(mpath, {}) or {})
                manifest = next((s for s in (manifest.get("shots") or [])
                                 if str(s.get("shot_id")) == str(int(sid) if sid.isdigit() else sid)), {})
                if manifest:
                    break
        return jsonify({
            "success": True, "kind": "storyboard", "name": f"shot_{sid}",
            "project": key, "project_id": rec["id"],
            "views": views, "exists": bool(views),
            "meta": {"shot": shot_meta, "manifest": manifest,
                     "description": shot_meta.get("description") or manifest.get("prompt") or "",
                     # 分镜图提示词：优先给**实际用于分镜图生成**的那条
                     # （build_storyboard_prompt 会优先采用 storyboard_prompt_zh / description，
                     #   manifest.prompt 就是当时真正提交给 ComfyUI 的提示词）
                     "prompt": (manifest.get("prompt")
                                or shot_meta.get("storyboard_prompt_zh")
                                or shot_meta.get("description") or ""),
                     # 视频提示词单列，避免与分镜图提示词混在一个字段里
                     "video_prompt": shot_meta.get("prompt_h3") or "",
                     "shot_id": shot_meta.get("shot_id") or (int(sid) if sid.isdigit() else sid),
                     "duration_sec": shot_meta.get("duration"),
                     "location": shot_meta.get("location"),
                     "dialogue": shot_meta.get("dialogue")},
            "regenerate": {"endpoint": "/api/storyboards/generate", "method": "POST",
                           "payload": {"project_name": (os.path.basename(os.path.dirname(png))
                                                        if png else key),
                                       "shots": [shot_meta] if shot_meta else [{"shot_id": sid}],
                                       "limit": 1}},
            "downloads": [v["url"] for v in views],
        })

    # 角色 / 物品 / 场景
    if not name or os.sep in name or "/" in name or ".." in name:
        return jsonify({"error": "资产名称非法", "name": name}), 400
    hit_dir = project_store.find_kind_dir(key, folder_kind, name)
    target = hit_dir["asset_dir"]
    if not target:
        return jsonify({"error": f"未找到资产：{name}", "kind": folder_kind,
                        "searched_root": hit_dir["kind_root"]}), 404
    views = project_store._views_of(target)
    base = next((v for v in views if v["view"] == "base"), None)
    meta = {}
    for sp in detail_scripts:
        bucket = sp["data"].get(folder_kind) or []
        hit = next((x for x in bucket if isinstance(x, dict) and x.get("name") == name), None)
        if hit:
            meta = dict(hit)
            meta["script_path"] = sp["path"]
            break
    shot_refs = []
    for sp in detail_scripts:
        for sh in (sp["data"].get("shots") or []):
            if name in (sh.get("characters_in_shot") or []) or name in (sh.get("items_in_shot") or []) \
                    or sh.get("location") == name:
                shot_refs.append({"shot_id": sh.get("shot_id"),
                                  "duration_sec": sh.get("duration"),
                                  "episode": sh.get("episode"),
                                  "script_path": sp["path"]})
    return jsonify({
        "success": True, "kind": asset_type, "name": name,
        "project": key, "project_id": rec["id"],
        "views": views, "exists": bool(views),
        "thumb_url": (base or (views[0] if views else {})).get("url") if views else "",
        "meta": {
            "name": name,
            "prompt_zh": meta.get("reference_prompt_zh") or "",
            "prompt_en": meta.get("reference_prompt_en") or "",
            "appearance": meta.get("appearance") or "",
            "personality": meta.get("personality") or "",
            "age": meta.get("age") or "",
            "voice_style": meta.get("voice_style") or "",
            "category": meta.get("category") or "",
            "owner": meta.get("owner") or "",
            "location": meta.get("location") or "",
            "script_path": meta.get("script_path") or "",
            "shots": sorted(shot_refs, key=lambda r: (r.get("episode") or 1, r.get("shot_id") or 0))[:60],
        },
        "regenerate": {"endpoint": "/api/assets/generate", "method": "POST",
                       "payload": {"asset_type": asset_type,
                                   "project_name": hit_dir["project_dir"] or key,
                                   "overwrite": True,
                                   "assets": [{"name": name,
                                               "reference_prompt_zh": meta.get("reference_prompt_zh") or "",
                                               "reference_prompt_en": meta.get("reference_prompt_en") or "",
                                               "appearance": meta.get("appearance") or ""}]}},
        "downloads": [v["url"] for v in views],
    })


@bp.route('/api/projects/<path:pid>/asset-precipitation', methods=['GET'])
def api_project_asset_precipitation(pid):
    """某资产的沉淀过程（时间线 + 步骤状态），供资产详情可视化。

    query: kind=character|item|scene, name=<资产名>
    → {success, steps:[{id,label,status,detail,at,items[]}], summary:{...}}
    """
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    kind = (request.args.get('kind') or 'character').strip().lower()
    name = (request.args.get('name') or '').strip()
    key = rec["dir_key"]

    kind_map = {"character": "characters", "characters": "characters",
                "item": "items", "items": "items",
                "scene": "scenes", "scenes": "scenes"}
    if kind not in kind_map:
        return jsonify({"error": f"不支持的资产类型：{kind}"}), 400
    folder_kind = kind_map[kind]
    if not name or "/" in name or "\\" in name or ".." in name:
        return jsonify({"error": "资产名称非法", "name": name}), 400

    hit = project_store.find_kind_dir(key, folder_kind, name)
    asset_dir = hit.get("asset_dir") or ""
    steps: list = []

    def _step(sid: str, status: str, detail: str = "", items=None, at=None) -> dict:
        label = dict((s[0], s[1]) for s in _PRECIP_STEPS).get(sid, sid)
        return {"id": sid, "label": label, "status": status,
                "detail": detail, "items": items or [], "at": at}

    # ---- 1. 资产抽取：剧本里这个资产的档案 ----
    _char_meta = {}
    _script_path = ""
    try:
        for sp in project_store.project_scripts(key):
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    d = json.load(f) or {}
            except Exception:  # noqa: BLE001
                continue
            for x in (d.get(folder_kind) or []):
                if isinstance(x, dict) and str(x.get("name") or "") == name:
                    _char_meta = dict(x)
                    _script_path = sp
                    break
            if _char_meta:
                break
    except Exception as e:  # noqa: BLE001
        app.logger.debug("资产抽取信息读取失败：%s", e)
    if _char_meta:
        _fields = [(k, str(_char_meta.get(k) or "").strip())
                   for k in ("appearance", "personality", "age", "gender", "category", "owner")
                   if str(_char_meta.get(k) or "").strip()]
        steps.append(_step("extract", "done",
                           f"从剧本抽取到 {len(_fields)} 项设定",
                           items=[{"key": k, "value": v[:200]} for k, v in _fields]))
    else:
        steps.append(_step("extract", "skipped", "剧本里没有该资产的档案（可能为手工上传）"))

    # ---- 2. 提示词定稿 ----
    base_meta = _precip_meta_of(asset_dir, "base.png") if asset_dir else {}
    _prompt = str(_char_meta.get("reference_prompt_zh") or base_meta.get("prompt") or "").strip()
    if _prompt:
        steps.append(_step("prompt", "done", f"{len(_prompt)} 字",
                           items=[{"key": "prompt", "value": _prompt[:1200]}]))
    else:
        steps.append(_step("prompt", "skipped", "未记录提示词"))

    # ---- 3. 基础图出图 + 4. 质检迭代（同一份历史，拆成两步展示） ----
    hist = {}
    try:
        hist = qc_client.read_history(QC_DIR, key, "asset", name) or {}
    except Exception as e:  # noqa: BLE001
        app.logger.debug("资产质检历史读取失败：%s", e)
    records = [r for r in (hist.get("records") or []) if isinstance(r, dict)]
    base_records = [r for r in records if str(r.get("stage") or "").startswith("基础图")
                    or r.get("stage") == "资产基础图质检"]
    if not base_records:
        base_records = records    # 旧数据没有 stage 字段时全当基础图阶段

    _source = (base_meta.get("extra") or {}).get("source") if base_meta else ""
    _has_base = bool(asset_dir) and os.path.isfile(os.path.join(asset_dir, "base.png"))
    if base_meta and _source == "user_upload":
        steps.append(_step("base", "done", "用户上传（未走 GPU 出图）",
                           items=[{"key": "file", "value": "base.png"}]))
    elif base_meta or _has_base:
        _wf = base_meta.get("workflow") or ""
        steps.append(_step("base", "done",
                           f"seed={base_meta.get('seed')}"
                           + (f"；工作流 {_wf}" if _wf else ""),
                           items=[{"key": "seed", "value": str(base_meta.get('seed') or '')},
                                  {"key": "workflow", "value": _wf}]))
    else:
        steps.append(_step("base", "pending", "尚未出图"))

    if base_records:
        _passed = next((r for r in base_records if r.get("passed")), None)
        _scores = [r.get("score") for r in base_records if r.get("score") is not None]
        _qitems = []
        for r in base_records:
            _qitems.append({
                "attempt": r.get("attempt"),
                "passed": bool(r.get("passed")),
                "score": r.get("score"),
                "reason": str(r.get("reason") or "")[:300],
                "issues": [str(x)[:160] for x in
                           (list(r.get("issues") or []) + list(r.get("critical_issues") or []))[:6]],
                "at": r.get("time"),
                "seed": r.get("seed"),
            })
        _detail = (f"共 {len(base_records)} 次尝试"
                   + (f"，第 {_passed.get('attempt')} 次达标" if _passed else "，未达标")
                   + (f"；评分 {min(_scores)}→{max(_scores)}" if len(_scores) > 1 else
                      (f"；评分 {_scores[0]}" if _scores else "")))
        steps.append(_step("qc", "done" if _passed else "failed", _detail, items=_qitems,
                           at=(base_records[-1] or {}).get("time")))
    else:
        steps.append(_step("qc", "skipped", "无质检记录（可能质检未开启或为用户上传）"))

    # ---- 5. 教训沉淀（该资产提示词命中的教训） ----
    _lessons = []
    try:
        if _prompt:
            _m = prompt_memory.get_memory(PROJECT_OUTPUT_DIR)
            # 用 suggestions_with_priority（返回 dict 列表）而非 suggestions
            # （后者返回渲染好的字符串，分类/优先级信息已丢，无法结构化展示）
            sug = _m.suggestions_with_priority("asset", _prompt, max_hints=8) or []
            _lessons = [{
                "issue": str(x.get("issue") or "")[:200],
                "category": x.get("category") or "",
                "priority": x.get("priority") or "",
                "project": x.get("project") or "",
                "score": x.get("score"),
            } for x in sug if isinstance(x, dict) and str(x.get("issue") or "").strip()]
    except Exception as e:  # noqa: BLE001
        app.logger.debug("教训召回失败：%s", e)
    if _lessons:
        steps.append(_step("lesson", "done",
                           f"命中 {len(_lessons)} 条历史教训（下次重画时用于改写提示词）",
                           items=_lessons))
    else:
        steps.append(_step("lesson", "skipped", "无相关教训"))

    # ---- 6. 视角切分 ----
    _views = project_store._views_of(asset_dir) if asset_dir else []
    _view_names = [v["view"] for v in _views]
    _derived = [k for k in ("front", "left", "back", "half") if k in _view_names]
    _view_meta = _precip_meta_of(asset_dir, "front.png") if asset_dir else {}
    if _derived:
        _mode = ((_view_meta.get("extra") or {}).get("derive_mode") or "sheet_crop")
        steps.append(_step("derive", "done",
                           f"切分出 {len(_derived)} 张视角图（{_mode}）",
                           items=[{"key": "views", "value": "、".join(_derived)},
                                  {"key": "derive_mode", "value": _mode}]))
    else:
        steps.append(_step("derive", "skipped",
                           "无派生视角（物品/场景仅一张基础图，或切分失败已回落整图）"))

    # ---- 7. 入库 ----
    if _views:
        steps.append(_step("store", "done",
                           f"入库 {len(_views)} 个文件：{'、'.join(_view_names)}",
                           items=[{"key": "dir", "value": asset_dir}]))
    else:
        steps.append(_step("store", "pending", "尚未入库任何图片"))

    _done = sum(1 for s in steps if s["status"] == "done")
    return jsonify({
        "success": True, "kind": kind_map[kind], "name": name, "project": key,
        "asset_dir": asset_dir, "script_path": _script_path,
        "steps": steps,
        "summary": {
            "total_steps": len(steps),
            "done": _done,
            "qc_attempts": len(base_records),
            "qc_passed": bool(next((r for r in base_records if r.get("passed")), None)),
            "lessons": len(_lessons),
            "views": _derived,
            "is_user_upload": _source == "user_upload",
        },
    })

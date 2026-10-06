# -*- coding: utf-8 -*-
"""agent 域蓝图：总控 AI 自主执行、对话、记忆。

2026-10-07 由 ``app/app.py`` 按域拆分而来，**纯搬迁**：路径、方法、报错文案逐字
不变（守门脚本 ``scripts/snapshot_routes.py`` 比对 rule + methods）。
``url_prefix`` 留空，路径直接写死在各 ``@bp.route`` 上。
"""
from __future__ import annotations

from flask import Blueprint

# 共享 runtime 上下文（app 实例 / 212 个模块级 helper / config 常量）。
# __all__ 里显式列出了下划线开头的名字，故这里能整体取到。
from api._shared import *  # noqa: F401,F403

bp = Blueprint("agent", __name__)
DOMAIN = "agent"

# ===== P0-4 持久化任务队列查询 =====

@bp.route('/api/tasks', methods=['GET'])
def api_tasks_list():
    """查询任务列表（可按项目 / 状态 / 类型过滤），用于重启后查看进度与续跑提示"""
    project = (request.args.get('project') or '').strip()
    status = (request.args.get('status') or '').strip()
    kind = (request.args.get('kind') or '').strip()
    try:
        limit = max(1, min(500, int(request.args.get('limit') or 100)))
    except (TypeError, ValueError):
        limit = 100
    try:
        items = task_db.list(project=project or None, status=status or None,
                             kind=kind or None, limit=limit)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"任务查询失败：{e}"}), 500
    return jsonify({"success": True, "count": len(items), "items": items,
                    "queue": _task_queue_status()})


@bp.route('/api/tasks/<task_id>', methods=['GET'])
def api_task_detail(task_id):
    """查询单个任务详情（含单元级进度，用于展示断点续跑可跳过的部分）"""
    try:
        t = task_db.get(task_id)
        if not t:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        units = task_db.list_units(task_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"任务查询失败：{e}"}), 500
    done = [u for u in units if u.get("status") == task_store.ST_DONE]
    t["units"] = units
    t["unit_summary"] = {"total": len(units), "done": len(done),
                         "pending": len(units) - len(done)}
    return jsonify({"success": True, "task": t})


@bp.route('/api/tasks/<task_id>/resume-preview', methods=['GET'])
def api_task_resume_preview(task_id):
    """断点续跑预检：给出该任务「已完成 / 待重跑」的单元清单

    判据以磁盘产物为准（产物存在且非空即视为已完成），
    因此即使任务状态表丢失，也能正确识别可跳过的部分。
    """
    try:
        t = task_db.get(task_id)
        if not t:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        units = task_db.list_units(task_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"查询失败：{e}"}), 500

    done_units, pending_units = [], []
    for u in units:
        path = u.get("result_path") or ""
        if path and task_store.is_unit_done(path, min_bytes=TASK_UNIT_MIN_BYTES):
            done_units.append(u.get("unit_key"))
        else:
            pending_units.append(u.get("unit_key"))
    return jsonify({"success": True, "task_id": task_id,
                    "status": t.get("status"),
                    "done_units": done_units, "pending_units": pending_units,
                    "resumable": t.get("status") in (task_store.ST_INTERRUPTED,
                                                     task_store.ST_FAILED)})


# ===================== 清空重做一集（总控 AI 工具 reset_episode 的后端） =====================
# 2026-09-30：总控 AI 需要独立的「清空重做」能力。安全性三原则：
#   ① 只**移入回收站**（output/projects/_trash/reset/<时间戳>_<项目>/），绝不真删——可恢复；
#   ② 只动**该集**的产物（第 1 集平铺、第 2 集起 epNN/ 子目录，口径与 _ep_dir 一致）；
#   ③ 逐项 try/except：单项被占用（生产仍在写）→ 跳过并如实上报，绝不中途抛异常。
# ⚠️ 调用方（总控 AI）应先用 stop_production 停止该项目生产，否则正在写入的文件会
#    清理失败（skipped 里会如实列出）。
@bp.route('/api/autopilot/reset-episode', methods=['POST'])
def api_autopilot_reset_episode():
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        episode_no = max(1, int(data.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    include_script = bool(data.get('include_script'))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_OUTPUT_DIR, "projects", "_trash", "reset",
                              f"{stamp}_{project_name}")
    cleared, skipped, notes = [], [], []

    def _trash_move(src, category):
        """把单个文件/目录移入回收站；不存在=无事发生，被占用=记入 skipped"""
        if not src or not os.path.exists(src):
            return
        try:
            dst = os.path.join(trash_root, category,
                               os.path.basename(src.rstrip('\\/')) or category)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            cleared.append({"category": category, "path": src})
        except Exception as e:  # noqa: BLE001  单项失败不阻断其余清理
            skipped.append({"path": src, "error": str(e)})

    # ---- 目录级产物：分镜图 / 尾帧 / 视频 / 成片 ----
    # 第 1 集平铺（只移文件，其它集的 epNN/ 子目录原地保留）；第 2 集起整块移 epNN/ 子目录
    for category, base_dir in (("storyboards", STORYBOARDS_DIR), ("keyframes", KEYFRAMES_DIR),
                               ("videos", VIDEOS_DIR), ("final", FINAL_DIR)):
        proj_dir = os.path.join(base_dir, project_name)
        if not os.path.isdir(proj_dir):
            continue
        if episode_no > 1:
            _trash_move(os.path.join(proj_dir, f"ep{episode_no:02d}"), category)
        else:
            for _name in os.listdir(proj_dir):
                _p = os.path.join(proj_dir, _name)
                if os.path.isfile(_p):
                    _trash_move(_p, category)

    # ---- 文件级产物：配音 / 混音 / 质检（文件名带 epNN_ 前缀的归该集）----
    for category, base_dir in (("dub", DUB_DIR), ("dub_mix", DUB_MIX_DIR), ("qc", QC_DIR)):
        proj_dir = os.path.join(base_dir, project_name)
        if not os.path.isdir(proj_dir):
            continue
        _prefix = f"ep{episode_no:02d}_"
        _hit = False
        for _root, _dirs, _files in os.walk(proj_dir):
            for _name in list(_files):
                if _name.startswith(_prefix):
                    _hit = True
                    _trash_move(os.path.join(_root, _name), category)
        if not _hit and episode_no == 1 and category == "qc":
            # 兜底：qc 里不带 ep 前缀的记录（如 storyboard_scratch、prompt_*.json）属第 1 集
            # 平铺口径 —— 整目录移入回收站（qc 是派生数据，可恢复）。
            _trash_move(proj_dir, category)

    # ---- 剧本（可选）：include_script=true 时连剧本一并移入回收站 ----
    if include_script:
        try:
            _script_path = novel_to_script.episode_script_path(SCRIPT_DIR, project_name, episode_no)
            _trash_move(_script_path, "scripts")
        except Exception as e:  # noqa: BLE001
            notes.append(f"剧本定位失败（忽略）：{e}")

    # ---- 交付记录：把该集条目从 deliverables.json 摘除（被摘条目进回收站，可恢复）----
    _dl_file = os.path.join(PROJECT_OUTPUT_DIR, "autopilot", project_name, "deliverables.json")
    if os.path.isfile(_dl_file):
        try:
            with open(_dl_file, "r", encoding="utf-8") as f:
                _dl = json.load(f)
            _items = _dl if isinstance(_dl, list) else (_dl.get("items") if isinstance(_dl, dict) else None) or []
            _removed = [_it for _it in _items
                        if isinstance(_it, dict) and int(_it.get("episode_no") or 0) == episode_no]
            if _removed:
                _keep = [_it for _it in _items if _it not in _removed]
                os.makedirs(os.path.join(trash_root, "autopilot"), exist_ok=True)
                with open(os.path.join(trash_root, "autopilot", "deliverables_removed.json"),
                          "w", encoding="utf-8") as f:
                    json.dump(_removed, f, ensure_ascii=False, indent=2)
                if isinstance(_dl, list):
                    atomic_write_json(_dl_file, _keep)
                else:
                    _dl["items"] = _keep
                    atomic_write_json(_dl_file, _dl)
                notes.append(f"交付记录已摘除 {len(_removed)} 条（被摘条目在回收站，可恢复）")
        except Exception as e:  # noqa: BLE001
            notes.append(f"交付记录清理失败（忽略）：{e}")

    if not cleared and not skipped:
        notes.append("该集没有可清理的产物（本来就是空的）")
    app.logger.info("[reset-episode] 项目=%s 集=%s 清理 %d 项、跳过 %d 项，回收站=%s",
                    project_name, episode_no, len(cleared), len(skipped), trash_root)
    return jsonify({
        "success": True, "project": project_name, "episode_no": episode_no,
        "include_script": include_script, "cleared": cleared, "skipped": skipped,
        "notes": notes, "trash": trash_root,
        "hint": "产物已移入回收站（可恢复）；接着用 produce_episode 从头重产该集",
    })


@bp.route('/api/autopilot/reset-shot', methods=['POST'])
def api_autopilot_reset_shot():
    """清空重做单个镜头：分镜图 / 尾帧 / 视频 / 配音 / 质检记录 → 回收站（可恢复）。

    清完之后用 produce_episode 重产该集，断点续跑只会重做缺失的这一镜（其余镜不重烧）。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        episode_no = max(1, int(data.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    shot_id = (data.get('shot_id') or '').strip()
    seq = _shot_seq(shot_id, 0)
    if seq <= 0:
        return jsonify({"success": False,
                        "error": f"无法解析镜号：{shot_id}（形如 shot_3，可从 get_shots 拿到）"})
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_OUTPUT_DIR, "projects", "_trash", "reset",
                              f"{stamp}_{project_name}")
    ep_tag = "" if episode_no <= 1 else f"ep{episode_no:02d}"
    ep_prefix = f"ep{episode_no:02d}_shot{seq:02d}"
    shot_prefix = f"shot_{seq:02d}"
    cleared, skipped = [], []
    # （目录, 名字前缀列表, 类别）——前缀按各目录的实际命名口径
    for base_dir, prefixes, category in (
            (os.path.join(STORYBOARDS_DIR, project_name, ep_tag), [shot_prefix], "storyboards"),
            (os.path.join(KEYFRAMES_DIR, project_name, ep_tag), [shot_prefix], "keyframes"),
            (os.path.join(VIDEOS_DIR, project_name, ep_tag), [shot_prefix], "videos"),
            (os.path.join(DUB_DIR, project_name), [ep_prefix], "dub"),
            (os.path.join(DUB_MIX_DIR, project_name), [ep_prefix], "dub_mix"),
            (os.path.join(QC_DIR, project_name), [ep_prefix, shot_prefix,
                                                  f"prompt_shot_{seq:02d}"], "qc"),
    ):
        for _p in _collect_matching(base_dir, prefixes):
            _trash_move(_p, category, trash_root, cleared, skipped)
    if not cleared and not skipped:
        return jsonify({"success": True, "project": project_name, "episode_no": episode_no,
                        "shot_id": shot_id, "cleared": [], "skipped": [],
                        "notes": ["该镜没有已生成的产物（本来就是空的）"], "trash": trash_root})
    app.logger.info("[reset-shot] 项目=%s 集=%s 镜=%s(seq %02d) 清理 %d 项、跳过 %d 项",
                    project_name, episode_no, shot_id, seq, len(cleared), len(skipped))
    return jsonify({"success": True, "project": project_name, "episode_no": episode_no,
                    "shot_id": shot_id, "cleared": cleared, "skipped": skipped,
                    "trash": trash_root,
                    "hint": "该镜产物已移入回收站；用 produce_episode 重产该集时只会重做这一镜"})


@bp.route('/api/autopilot/reset-asset', methods=['POST'])
def api_autopilot_reset_asset():
    """清空重做单个资产（角色/物品/场景）：基础图 + 多视图 + 质检记录 → 回收站（可恢复）。

    资产卡描述保留（只清图）；下一轮生产的资产步骤会因「base 图缺失」自动补做该资产。
    ⚠️ 若命中跨项目资产库的形象指纹，重做可能直接复用既有图；要换形象请先改描述再重做。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    kind = (data.get('kind') or '').strip().lower()
    name = (data.get('name') or '').strip()
    _kind_dirs = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}
    base_dir = _kind_dirs.get(kind)
    if not base_dir:
        return jsonify({"success": False,
                        "error": "kind 必须是 character / item / scene 之一"})
    if not name or '/' in name or '\\' in name or name in ('.', '..'):
        return jsonify({"success": False, "error": "name 不能为空且不得含路径分隔符"})
    asset_dir = os.path.join(base_dir, project_name, name)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_OUTPUT_DIR, "projects", "_trash", "reset",
                              f"{stamp}_{project_name}")
    cleared, skipped, notes = [], [], []
    if not os.path.isdir(asset_dir):
        notes.append("该资产没有已生成的图（本来就是空的），无需清理")
    else:
        _trash_move(asset_dir, f"assets/{kind}", trash_root, cleared, skipped)
    app.logger.info("[reset-asset] 项目=%s kind=%s 资产=%s 清理 %d 项、跳过 %d 项",
                    project_name, kind, name, len(cleared), len(skipped))
    return jsonify({"success": True, "project": project_name, "kind": kind, "name": name,
                    "cleared": cleared, "skipped": skipped, "notes": notes,
                    "trash": trash_root,
                    "hint": "资产图已移入回收站（角色卡描述保留）；下一轮生产会自动补做该资产。"
                            "若命中跨项目资产库指纹可能直接复用旧图，要换形象请先改描述"})


@bp.route('/api/autopilot/status', methods=['GET'])
@_autopilot_guard
def api_autopilot_status():
    """托管总览：开关状态、当前在做什么、待验收数、异常数、24h 生产曲线

    brief=1：只回状态判断必要的短字段（AI 总控用；见 autopilot.status 的 brief 说明）。
    """
    project = request.args.get("project", "").strip()
    brief = str(request.args.get("brief") or "").strip().lower() in ("1", "true", "yes", "on")
    return jsonify({"success": True, **autopilot.status(project, brief=brief)})


@bp.route('/api/autopilot/ready', methods=['GET'])
@_autopilot_guard
def api_autopilot_ready():
    """托管可行性自检：告诉用户还差什么才能真正无人值守"""
    return jsonify({"success": True, **autopilot.ready()})


@bp.route('/api/autopilot/curve', methods=['GET'])
@_autopilot_guard
def api_autopilot_curve():
    """24 小时生产曲线（每小时完成/失败集数、平均耗时、吞吐）"""
    try:
        hours = max(1, min(int(request.args.get('hours') or 24), 168))
    except (TypeError, ValueError):
        hours = 24
    return jsonify({"success": True, **autopilot.production_curve(hours)})


@bp.route('/api/autopilot/plans', methods=['GET'])
@_autopilot_guard
def api_autopilot_plans():
    """全部项目的托管计划"""
    plans = autopilot.list_plans()
    return jsonify({"success": True, "count": len(plans), "items": plans,
                    "defaults": autopilot.PLAN_DEFAULTS})


@bp.route('/api/autopilot/plan/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autopilot_plan_get(project_name):
    project = _safe_project(project_name)
    return jsonify({"success": True, "project": project,
                    "plan": autopilot.get_plan(project),
                    "defaults": autopilot.PLAN_DEFAULTS})


@bp.route('/api/autopilot/plan/<project_name>', methods=['POST'])
@_autopilot_guard
def api_autopilot_plan_set(project_name):
    """设置某项目的自动生产计划（目标集数、镜头数、风格、质量阈值、重试上限…）"""
    project = _safe_project(project_name)
    data = request.json or {}
    novel_id = str(data.pop('novel_id', '') or '').strip()
    patch = {k: v for k, v in (data or {}).items() if k in autopilot.PLAN_DEFAULTS}
    if not patch and not novel_id:
        return jsonify({"success": False,
                        "error": f"没有可更新字段；可用字段：{sorted(autopilot.PLAN_DEFAULTS)}"}), 400
    plan = autopilot.set_plan(project, patch, novel_id=novel_id)
    return jsonify({"success": True, "project": project, "plan": plan})


@bp.route('/api/autopilot/enable', methods=['POST'])
@_autopilot_guard
def api_autopilot_enable():
    """开启托管（可同时带生产配置）。novel_id 缺省时从项目注册表自动关联。"""
    # P0-5 门禁：托管是无人值守路径，配置不全时开闸只会白跑一整晚 —— 先在门口拦下
    _gate = _ai_gate_or_400("episode")
    if _gate is not None:
        return _gate
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project') or data.get('project_name') or '',
                                    field_name="project")
    if err is not None:
        return err
    novel_id = str(data.get('novel_id') or '').strip()
    if not novel_id:
        try:
            rec = project_store.get_project(project) or {}
            novel_id = (rec.get('novel_id') or '').strip()
        except Exception:  # noqa: BLE001
            novel_id = ''
    if not novel_id:
        # 退回「按项目名匹配同名小说」，尽量让用户少填一步
        try:
            for n in list_novels(NOVELS_DIR):
                if (n.get('title') or '').strip() == project:
                    novel_id = n.get('novel_id') or ''
                    break
        except Exception:  # noqa: BLE001
            novel_id = ''
    patch = {k: v for k, v in (data or {}).items()
             if k in autopilot.PLAN_DEFAULTS and k != 'enabled'}
    plan = autopilot.enable(project, patch)
    if novel_id:
        plan = autopilot.set_plan(project, {}, novel_id=novel_id)
    # 顺带校验输入是否齐备（小说正文 / 项目绑定），避免"开了但没活干"
    warn = ""
    if not plan.get('novel_id'):
        warn = "该项目未关联小说，托管不会生产；请先在「小说库」上传并建项目"
    return jsonify({"success": True, "project": project, "plan": plan,
                    "novel_id": plan.get('novel_id') or "", "warning": warn})


@bp.route('/api/autopilot/disable', methods=['POST'])
@_autopilot_guard
def api_autopilot_disable():
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project') or data.get('project_name') or '',
                                   field_name="project")
    if err is not None:
        return err
    return jsonify({"success": True, "project": project,
                    "plan": autopilot.disable(project)})


@bp.route('/api/autopilot/pause', methods=['POST'])
@_autopilot_guard
def api_autopilot_pause():
    """暂停托管（在当前步骤边界生效，不产生半成品）

    ⚠️ 这是**全局**开关：``autopilot.pause()`` 置的是全局 ``_STATE["paused"]``，
    **不区分项目**。此前接口既不读 ``project`` 也不提示，调用方（尤其是总控 AI）
    很容易以为「只暂停了某个项目」，实际把所有项目都停了 —— 静默越权。
    这里保留 reason 语义，并在收到 project 时**显式告知**它被忽略、给出替代做法。
    """
    data = request.json or {}
    result = autopilot.pause(str(data.get('reason') or '手动暂停'))
    ignored_project = str(data.get('project') or data.get('project_name') or '').strip()
    payload = {"success": True, **result}
    if ignored_project:
        payload["warning"] = (f"暂停托管是**全局**开关，已忽略 project='{ignored_project}'"
                              "（所有项目都会暂停）。若只想停某个项目，"
                              "请调用 /api/autopilot/disable 并带 project。")
        payload["scope"] = "global"
    return jsonify(payload)


@bp.route('/api/autopilot/resume', methods=['POST'])
@_autopilot_guard
def api_autopilot_resume():
    """恢复托管（服务重启后也可用它手动拉起守护进程）"""
    return jsonify({"success": True, **autopilot.resume()})


@bp.route('/api/autopilot/progress', methods=['GET'])
@_autopilot_guard
def api_autopilot_progress_all():
    """全部项目的分集进度（剧本/资产/分镜/视频/成片 逐集状态）"""
    items = autopilot.all_progress()
    return jsonify({"success": True, "count": len(items), "items": items})


@bp.route('/api/autopilot/progress/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autopilot_progress_one(project_name):
    project = _safe_project(project_name)
    return jsonify({"success": True, **autopilot.project_progress(project)})


@bp.route('/api/autopilot/deliverables', methods=['GET'])
@_autopilot_guard
def api_autopilot_deliverables():
    """成品验收队列——用户唯一需要重点看的清单（只含最终成片）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else ''
    items = pipeline.list_deliverables(project)
    return jsonify({"success": True, "count": len(items),
                    "pending": sum(1 for x in items if x.get('review') == 'pending'),
                    "items": items})


@bp.route('/api/autopilot/deliverables/review', methods=['POST'])
@_autopilot_guard
def api_autopilot_review():
    """验收 / 打回成片（打回 = 该集在下次轮转时自动重做）"""
    data = request.json or {}
    # 口径统一（F-01 收口）：与 run-once 一致，改走 _project_or_400 拒绝越界/空/控制字符。
    project, err = _project_or_400(
        data.get('project') or data.get('project_name') or '', field_name="project")
    if err is not None:
        return err
    review = str(data.get('review') or '').strip().lower()
    if review not in ('accepted', 'rejected', 'pending'):
        return jsonify({"success": False, "error": "review 仅支持 accepted / rejected / pending"}), 400
    try:
        ep = int(data.get('episode_no') or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    item = pipeline.set_deliverable_review(project, ep, review, str(data.get('note') or ''))
    if not item:
        return jsonify({"success": False, "error": f"未找到 {project} 第{ep}集的成片记录"}), 404
    return jsonify({"success": True, "item": item})


@bp.route('/api/autopilot/deliverable/file/<project_name>/<path:filename>')
@_autopilot_guard
def api_autopilot_deliverable_file(project_name, filename):
    """播放 / 下载成片（以交付物索引为准解析真实路径，防目录穿越）"""
    project = _safe_project(project_name)
    filepath = pipeline.deliverable_path(project, filename)
    if not filepath or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True,
                     as_attachment=request.args.get('download') == '1')


@bp.route('/api/autopilot/exceptions', methods=['GET'])
@_autopilot_guard
def api_autopilot_exceptions():
    """需人工介入清单（超过重试上限 / 环境性缺失导致挂起）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else ''
    projects = [project] if project else [p['project'] for p in autopilot.list_plans()]
    items = []
    for pj in projects:
        for d in pipeline.list_dead_letters(pj):
            if not d.get('resolved'):
                # P0-4：list_dead_letters 返回的是磁盘 dead_letter.json 里的原始项
                # （字段只有 episode_no/reason/detail/...，不含 project）——不传
                # ?project= 时每条无法归属项目、排序键 x.get('project') 形同虚设。
                # 用**拷贝**补 project（不就地改磁盘读出的对象，避免污染其他调用方），
                # 让 items.sort 的 project 排序键从此真正生效。
                items.append({**d, "project": pj})
    items.sort(key=lambda x: (x.get('project') or '', int(x.get('episode_no') or 0)))
    return jsonify({"success": True, "count": len(items), "items": items})


@bp.route('/api/autopilot/exceptions/resolve', methods=['POST'])
@_autopilot_guard
def api_autopilot_exception_resolve():
    """处理异常：标记已解决 / 忽略（之后托管会重新尝试该集）"""
    data = request.json or {}
    project = _safe_project(data.get('project') or data.get('project_name') or '')
    try:
        ep = int(data.get('episode_no') or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    item = pipeline.resolve_dead_letter(project, ep, str(data.get('note') or ''))
    if not item:
        return jsonify({"success": False, "error": f"未找到 {project} 第{ep}集的异常记录"}), 404
    autopilot.wake()
    return jsonify({"success": True, "item": item})


@bp.route('/api/autopilot/run-once', methods=['POST'])
@_autopilot_guard
def api_autopilot_run_once():
    """立即生产指定一集（**异步**：校验通过即返回 task_id，后台执行）

    审计 P2-6（2026-09-29）：旧实现同步跑完整集流水线（可数小时），占用 waitress
    工作线程（默认 8）—— 几次并发就把线程池占满，连 /api/status 轮询一并饿死
    （表现为「全站卡死」）。现在校验通过即返回 task_id，生产在后台线程执行：
    进度经 _cb 写进 autopilot.current，用 /api/autopilot/status 轮询；
    完成态看交付清单（/api/autopilot/deliverables）。
    """
    # P0-5 门禁：整集生产依赖文本分析模型，未配置直接阻断（不再「跑一半才 401」）
    _gate = _ai_gate_or_400("episode")
    if _gate is not None:
        return _gate
    data = request.json or {}
    # 口径统一（F-01 收口）：run-once / review 此前用 _safe_project，会把越界/空/控制字符
    # 项目名静默收敛成合法键，与全仓 46 处 _project_or_400 不一致；现改走同一入口。
    project, err = _project_or_400(
        data.get('project') or data.get('project_name') or '', field_name="project")
    if err is not None:
        return err
    try:
        ep = int(data.get('episode_no') or 1)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    plan = autopilot.get_plan(project)
    meta = autopilot._novel_meta(project, plan)
    if not meta:
        return jsonify({"success": False,
                        "error": "该项目未关联小说，无法生产（请先在小说库上传建项目）"}), 400
    chapters, novel_text = autopilot.chapters_and_text(meta)
    # 集号 ≠ 章号（超长章会拆成多集）→ 必须走单元表反查，不能按章号找
    unit = autopilot.find_episode_unit(chapters, plan, ep, novel_text)
    chapter = (unit or {}).get("chapter")
    if not chapter:
        _n_units = len(autopilot.episode_units(chapters, plan, novel_text))
        return jsonify({"success": False,
                        "error": f"该小说没有第{ep}集（共 {len(chapters)} 章 / {_n_units} 集）"}), 400
    cfg = pipeline.normalize_config({**plan, 'novel_id': meta.get('novel_id')},
                                    default_project_key=project)
    # 2026-09-30：已被判定「需人工介入」的集，在**入口**就挡住并把原因回给调用方。
    # 旧行为是放它进后台、由 run_episode 立刻返回 needs_human，总控只拿到 200 started，
    # 用户这边什么都看不到（第1集 401 那次就是这样静默掉的）。现在总控能当场拿到 409+原因。
    _dead = pipeline._is_dead_letter(cfg, project, ep)
    if _dead:
        return jsonify({"success": False, "episode_no": ep,
                        "error": f"第{ep}集此前已判定需人工介入：{_dead.get('reason') or '未知原因'}",
                        "hint": "请先在「异常 / 需人工介入」里处理或标记忽略，然后重跑本集"}), 409
    # P1-5：run-once 的 progress_cb 把进度实时写进 autopilot 的 current 状态，
    # `/api/autopilot/status` 轮询即可拿到实时进度（当前在哪一步 / 百分之几 / 卡在重试）。
    _seen: list = []

    def _cb(message, percent, phase=None):
        try:
            import pipeline as _pl
            base = str(phase or "").split(":")[0]
            if base in _pl.STEP_SEQUENCE and base not in _seen:
                idx = _pl.STEP_SEQUENCE.index(base)
                _seen.extend(_pl.STEP_SEQUENCE[:idx])
                _seen.append(base)
            autopilot._set_current(project=project, episode=ep,
                                   title=chapter.get('title') or f"第{ep}章",
                                   step=base or "running", message=message,
                                   percent=int(percent or 0),
                                   steps_done=list(dict.fromkeys(_seen)),
                                   phase=phase or "start", started_at=autopilot._now())
        except Exception as e:  # noqa: BLE001  进度上报失败不得阻断生产
            app.logger.warning("run-once 进度上报失败：%s", e)

    # 审计 P2-6：先做一次非阻塞忙检（保留旧「busy → 409」语义），再转后台执行
    if pipeline.is_episode_running(project, ep):
        # B-02 P0-5：该集正被另一执行体（托管轮转）生产，集级锁拒绝双跑
        return jsonify({"success": False,
                        "error": "该集正在生产中",
                        "retry_after_sec": 30}), 409

    # 总控「停止 → 再生产」流程修复（2026-09-30 实测）：stop_production 置的全局
    # 暂停**持久化到磁盘**（重启也恢复），而本集视频 worker 的中止判定器
    # （_video_should_stop）对托管任务「is_paused → 停」——于是总控停止后再
    # 「生产一集」，每次 ComfyUI 提交都被秒级掐断，3 次重试瞬间烧完、整集失败。
    # 显式 run-once = 用户要求「现在就生产」，先解除暂停再开跑。
    try:
        autopilot.resume()
    except Exception as e:  # noqa: BLE001  解除暂停失败不得阻断本次生产
        app.logger.warning("run-once 前解除托管暂停失败（忽略）：%s", e)

    def _run_once_worker():
        result = {}
        try:
            result = pipeline.run_episode(cfg, project, ep, meta, chapter, progress_cb=_cb)
            if result.get('status') == 'busy':
                # 罕见竞态：预检后另一执行体抢跑 —— 集级锁拒绝双跑，留痕即可
                app.logger.warning("run-once %s 第%s集被集级锁拒绝（另一执行体正在生产）",
                                   project, ep)
                return
            if result.get('ok') and result.get('deliverable'):
                pipeline.record_deliverable(project, ep, result['deliverable'], meta={
                    'title': chapter.get('title') or '', 'chapter_index': chapter.get('index'),
                    'elapsed_sec': result.get('elapsed_sec')})
        except Exception as e:  # noqa: BLE001  后台任务异常不得带崩进程/线程静默死亡
            app.logger.error("run-once 后台生产失败（%s 第%s集）：%s",
                             project, ep, e, exc_info=True)
            result = {"ok": False, "status": "failed", "episode_no": ep,
                      "project": project, "error": f"{type(e).__name__}: {e}",
                      "elapsed_sec": 0, "deliverable": "", "steps": {}}
        finally:
            # 2026-09-30 修复「失败了总控不知道」：run-once 此前只 logger.error 再清 current，
            # 既不落死信也不写 last_run ⇒ /api/autopilot/status 回报 current=null /
            # exceptions=0 / last_error=""，总控 get_status 看到的是「空闲且零异常」，
            # 于是第1集的 401 失败永远汇报不出来。现在成功与失败都登记 last_run
            # （带项目归属、落盘）；硬失败同时落死信，让「异常」列表和总控都看得见。
            try:
                _st = str(result.get('status') or '')
                _ok = bool(result.get('ok'))
                if _st != 'busy':
                    autopilot.record_run_result(
                        project, ep, _ok, status=_st or ('done' if _ok else 'failed'),
                        error=str(result.get('error') or ''),
                        deliverable=str(result.get('deliverable') or ''),
                        elapsed_sec=result.get('elapsed_sec') or 0,
                        title=chapter.get('title') or '', source='run-once')
                    if not _ok and _st != 'cancelled':
                        # 取消＝用户/托管主动停，不算「需人工介入」，不落死信
                        autopilot._mark_dead(project, ep,
                                             str(result.get('error') or '未知错误'),
                                             {"source": "run-once",
                                              "status": _st or "failed"})
            except Exception as e:  # noqa: BLE001  登记失败不得影响 current 清理
                app.logger.warning("run-once 结果登记失败：%s", e)
            try:
                autopilot._clear_current()
            except Exception as e:  # noqa: BLE001
                app.logger.warning("run-once 清理 current 失败：%s", e)

    threading.Thread(target=_run_once_worker, daemon=True,
                     name=f"runonce_{project}_{ep}").start()
    return jsonify({"success": True, "task_id": f"runonce_{project}_{ep}",
                    "project": project, "episode_no": ep, "status": "started",
                    "message": "已开始生产；进度见 /api/autopilot/status，完成态见交付清单"})


@bp.route('/api/autopilot/plan-from-settings/<project_name>', methods=['POST'])
@_autopilot_guard
def api_autopilot_plan_from_settings(project_name):
    """总控 AI 一键设定：把对话敲定的创作设定（风格/画风/镜头数/节奏…）落成托管计划

    这是「对话 → 自动生产」的接缝：用户在总控 AI 里谈好风格后点一下，
    风格纲要写进流水线配置，之后每集剧本生成都会带上它，无需再手工填表。
    """
    project = _safe_project(project_name)
    view = ai_chat.settings_view(AI_SETTINGS_PATH, project)
    brief = (view.get('style_brief') or '').strip()
    s = view.get('settings') or {}
    patch = {}
    if brief:
        patch['style'] = brief
    # 单集镜头数：从设定里解析数字（如 "12 个" → 12）
    shots = str(s.get('shots_per_episode') or '')
    digits = ''.join(ch for ch in shots if ch.isdigit())
    if digits:
        try:
            patch['target_shots'] = max(4, min(int(digits), 40))
        except ValueError as e:
            app.logger.debug("target_shots 字段解析失败（忽略）：%s", e)
    if not patch:
        return jsonify({"success": False,
                        "error": "总控 AI 还没有生效设定；请先在「总控 AI 对话」里谈好风格再一键设定"}), 400
    plan = autopilot.set_plan(project, patch)
    return jsonify({"success": True, "project": project, "plan": plan,
                    "applied": patch, "settings_filled": view.get('filled'),
                    "settings_missing": view.get('missing'),
                    "style_brief": brief})


# ==================== 全自动生产主控路由 ====================

@bp.route('/api/autonomous/start', methods=['POST'])
@_autopilot_guard
def api_autonomous_start():
    """一键启动全自动生产：上传小说后，AI对话定风格，然后一键启动24h自动生产"""
    data = request.json or {}
    project_name = _safe_project(data.get('project_name') or '')
    novel_id = str(data.get('novel_id') or '').strip()
    plan_overrides = {k: v for k, v in data.items()
                      if k in ('style', 'target_shots', 'video_mode', 'enable_assets',
                               'enable_keyframe', 'enable_video', 'enable_final',
                               'enable_tts', 'enable_tts_pre', 'enable_mix',
                               'step_max_retries')}

    if not novel_id and project_name:
        # 尝试从现有计划获取 novel_id
        import autopilot as _ap
        plan = _ap.get_plan(project_name)
        novel_id = plan.get('novel_id', '')

    if not novel_id:
        return jsonify({"success": False, "error": "缺少 novel_id，请先上传小说"}), 400

    _g = _style_aspect_guard(project_name, override_style=str(plan_overrides.get('style') or ''))
    if _g is not None:
        return _g

    result = autonomous.start_autonomous(project_name or novel_id, novel_id, plan_overrides)
    # D5：返回给前端的错误统一脱敏，避免把异常栈/模块名直接显示在错误框里
    if isinstance(result, dict) and result.get('error'):
        result['error'] = _friendly_error(result['error'])
    status_code = 200 if result.get('success') else 400
    return jsonify(result), status_code


@bp.route('/api/autonomous/stop', methods=['POST'])
@_autopilot_guard
def api_autonomous_stop():
    """停止全自动生产"""
    data = request.json or {}
    project = _safe_project(data.get('project_name') or '')
    result = autonomous.stop_autonomous(project)
    return jsonify(result)


@bp.route('/api/autonomous/resume', methods=['POST'])
@_autopilot_guard
def api_autonomous_resume():
    """恢复全自动生产"""
    data = request.json or {}
    project = _safe_project(data.get('project_name') or '')
    result = autonomous.resume_autonomous(project)
    return jsonify(result)


@bp.route('/api/autonomous/status', methods=['GET'])
@_autopilot_guard
def api_autonomous_status():
    """查询全自动生产状态"""
    project = _safe_project(request.args.get('project_name', ''))
    result = autonomous.status(project)
    return jsonify({"success": True, **result})


@bp.route('/api/autonomous/chat', methods=['POST'])
@_autopilot_guard
def api_autonomous_chat():
    """AI 对话指令解析：将用户的自然语言指令转化为生产动作"""
    data = request.json or {}
    message = str(data.get('message') or '').strip()
    project = _safe_project(data.get('project_name') or '')

    if not message:
        return jsonify({"success": False, "error": "消息不能为空"}), 400

    result = autonomous.interpret_chat_command(message, project)
    return jsonify(result)


@bp.route('/api/autonomous/report/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_report(project_name):
    """获取生产报告"""
    project = _safe_project(project_name)
    episode_no = request.args.get('episode_no', type=int)
    result = autonomous.generate_report(project, episode_no)
    return jsonify(result)


@bp.route('/api/autonomous/report/export/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_report_export(project_name):
    """导出生产报告"""
    project = _safe_project(project_name)
    fmt = request.args.get('format', 'json')
    filepath = autonomous.export_report(project, fmt)
    if not filepath:
        return jsonify({"success": False, "error": "暂无生产记录"}), 404
    return send_file(filepath, as_attachment=True,
                     download_name=os.path.basename(filepath))


@bp.route('/api/autonomous/projects', methods=['GET'])
@_autopilot_guard
def api_autonomous_projects():
    """列出所有有生产记录的项目"""
    projects = autonomous.list_all_projects()
    return jsonify({"success": True, "projects": projects})


@bp.route('/api/autonomous/deliverables/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_deliverables(project_name):
    """获取项目的交付物列表"""
    project = _safe_project(project_name)
    deliverables = autonomous.get_deliverables(project)
    return jsonify({"success": True, "project": project, "deliverables": deliverables})


# ===================== 角色关系图谱API =====================

@bp.route('/api/relations/graph', methods=['GET'])
@_autopilot_guard
def api_get_relation_graph():
    """获取角色关系图谱数据"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    graph = rmgr.get_graph_data()
    return jsonify({"success": True, "graph": graph})


@bp.route('/api/relations', methods=['GET'])
@_autopilot_guard
def api_list_relations():
    """列出项目所有角色关系"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    relations = rmgr.get_all_relations()
    return jsonify({"success": True, "relations": relations})


@bp.route('/api/relations', methods=['POST'])
@_autopilot_guard
def api_add_relation():
    """添加角色关系"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    char_a = data.get('char_a', '')
    char_b = data.get('char_b', '')
    rel_type = data.get('type', 'friend')
    strength = data.get('strength', 'medium')
    note = data.get('note', '')

    if not char_a or not char_b:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    rel_id = rmgr.add_relation(char_a, char_b, rel_type, strength, note)
    return jsonify({"success": True, "relation_id": rel_id})


@bp.route('/api/relations/<rel_id>', methods=['PUT'])
@_autopilot_guard
def api_update_relation(rel_id):
    """更新角色关系"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    try:
        rmgr.update_relation(rel_id, **{k: v for k, v in data.items() if k != 'project'})
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404


@bp.route('/api/relations/<rel_id>', methods=['DELETE'])
@_autopilot_guard
def api_delete_relation(rel_id):
    """删除角色关系"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    try:
        rmgr.delete_relation(rel_id)
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404


@bp.route('/api/relations/sync', methods=['POST'])
@_autopilot_guard
def api_sync_relations():
    """同步角色数据到关系库"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    characters = data.get('characters', {})

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    rmgr.sync_characters(characters)
    return jsonify({"success": True})


@bp.route('/api/relations/conflicts', methods=['GET'])
@_autopilot_guard
def api_check_relation_conflicts():
    """检测关系冲突"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    detector = RelationConflictDetector(rmgr)
    summary = detector.get_conflict_summary()
    return jsonify({"success": True, "conflicts": summary})


@bp.route('/api/relations/svg', methods=['GET'])
@_autopilot_guard
def api_export_relation_svg():
    """导出关系图谱为SVG"""
    project_name = request.args.get('project')
    width = int(request.args.get('width', 800))
    height = int(request.args.get('height', 600))

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    svg_content = rmgr.export_svg(width, height)
    return jsonify({"success": True, "svg": svg_content})


# ===================== 总控 AI 自主执行（function-calling agent） =====================
#
# 与 /api/ai/chat 的区别：
#   /api/ai/chat      —— 只聊天 + 抽取创作设定，不执行任何生产动作
#   /api/agent/chat   —— 由模型自己决定调用哪些工具，直接把活干完（无人确认）
#
# 安全不靠弹窗，靠 agent_core 里的自动护栏：工具白名单 / 昂贵动作配额 /
# 步数上限 / 同参数冷却 / 全局急停 / 单项目互斥 / 审计日志。

@bp.route('/api/agent/tools', methods=['GET'])
def api_agent_tools():
    """列出总控 AI 可调用的工具与当前护栏状态（供前端展示能力边界）"""
    return jsonify({
        "success": True,
        "count": len(agent_core.TOOLS),
        "tools": agent_core.tool_index(),
        "guards": {
            "max_steps": agent_core.MAX_STEPS,
            "max_expensive": agent_core.MAX_EXPENSIVE,
            "max_turn_sec": agent_core.MAX_TURN_SEC,
            "cooldown_sec": agent_core.COOLDOWN_SEC,
        },
        "kill": agent_core.kill_state(),
    })


@bp.route('/api/agent/kill', methods=['GET', 'POST'])
def api_agent_kill():
    """急停开关：一键中止所有正在跑的总控动作（无人值守时的刹车）"""
    if request.method == 'GET':
        return jsonify({"success": True, "kill": agent_core.kill_state()})
    data = request.json or {}
    on = bool(data.get("on", True))
    reason = str(data.get("reason") or "").strip() or ("手动急停" if on else "")
    return jsonify({"success": True, "kill": agent_core.set_kill(on, reason)})


@bp.route('/api/agent/chat', methods=['POST'])
def api_agent_chat():
    """下发一条自然语言指令，总控 AI 自主决策并执行（异步任务，返回 job_id 供轮询）"""
    # P0-5 门禁：总控对话模型未配置 → 闸在门口，避免 issue 落库后才发现跑不动
    _gate = _ai_gate_or_400("chat")
    if _gate is not None:
        return _gate
    data = request.json or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "message 不能为空"}), 400
    if len(message) > ai_chat.MAX_CHARS_PER_MESSAGE:
        message = message[:ai_chat.MAX_CHARS_PER_MESSAGE]

    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    # ⚠️ _chat_project 只认 project_name，而本接口/前端传的是 project。
    # 不转换的话总控会静默落到「上一个活跃项目」上，把 A 项目的指令干到 B 项目头上。
    _explicit = (data.get("project_name") or data.get("project") or "").strip()
    project = _chat_project({"project_name": _explicit} if _explicit else (data or {}), history)

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(cfg, "chat")
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        return jsonify({
            "success": False,
            "error": "「对话总控模型」尚未配置（base_url / api_key / model），总控无法自主执行",
            "guide": LLM_NOT_CONFIGURED_GUIDE_MAP["chat"],
            "need_config": True,
            "state": _chat_state(project),
        }), 400

    history["active_project"] = project
    ai_chat.append_message(history, "user", message, project)
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)

    started = agent_core.start_job(
        message=message,
        project=project,
        ep=ep,
        history=ai_chat.project_messages(history, project)[:-1],
        timeout=LLM_REQUEST_TIMEOUT,
    )
    if not started.get("ok"):
        ai_chat.drop_last_message(history, project)
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({"success": False, "error": started.get("error"),
                        "state": _chat_state(project)}), 409
    return jsonify({"success": True, "job_id": started["job_id"], "project": project,
                    "state": _chat_state(project)})


@bp.route('/api/agent/job/<job_id>', methods=['GET'])
def api_agent_job(job_id):
    """轮询总控任务进度（steps 逐条追加，status: running/done/failed/killed/timeout）"""
    job = agent_core.get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": f"未找到任务 {job_id}"}), 404
    # P1-1：最终回复已在 agent 线程内即时落盘（agent_core._finish → _persist_agent_reply）；
    # 这里仅作幂等兜底——线程内失败/尚未完成落盘时由 persist_job_reply 补写（共用同一份
    # 认领/去重逻辑，保证回复只追加一次，不重复不丢失）。前端刷新/离开不再导致回复丢失。
    if job.get("status") in ("done", "failed", "killed", "timeout") and job.get("reply"):
        agent_core.persist_job_reply(job_id)
    return jsonify({"success": True, **{k: v for k, v in job.items() if not k.startswith("_")}})


@bp.route('/api/agent/log', methods=['GET'])
def api_agent_log():
    """查看总控 AI 的审计日志（干了什么、成功没有、花了多久）"""
    try:
        limit = max(1, min(int(request.args.get("limit") or 100), 1000))
    except (TypeError, ValueError):
        limit = 100
    path = os.path.join(agent_core.AUDIT_DIR,
                        f"audit-{datetime.now().strftime('%Y%m%d')}.jsonl")
    items = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
            for ln in lines:
                try:
                    items.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": f"读取审计日志失败：{e}"}), 500
    return jsonify({"success": True, "count": len(items), "items": items})

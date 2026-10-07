# -*- coding: utf-8 -*-
"""ops 域蓝图：状态、诊断、导出、托管/自主化、关系、记忆、页���与回退。

2026-10-07 由 ``app/app.py`` 按域拆分而来，**纯搬迁**：路径、方法、报错文案逐字
不变（守门脚本 ``scripts/snapshot_routes.py`` 比对 rule + methods）。
``url_prefix`` 留空，路径直接写死在各 ``@bp.route`` 上。
"""
from __future__ import annotations

from flask import Blueprint

# 共享 runtime 上下文（app 实例 / 212 个模块级 helper / config 常量）。
# __all__ 里显式列出了下划线开头的名字，故这里能整体取到。
from api._shared import *  # noqa: F401,F403

bp = Blueprint("ops", __name__)
DOMAIN = "ops"

@bp.route('/api/status')
def api_status():
    comfyui_status = comfyui_client.get_status()
    return jsonify({
        "comfyui": comfyui_status,
        "assets": {
            "characters": sum(
                len(files) for _, _, files in os.walk(CHARACTERS_DIR)
            ) if os.path.exists(CHARACTERS_DIR) else 0,
            "items": sum(
                len(files) for _, _, files in os.walk(ITEMS_DIR)
            ) if os.path.exists(ITEMS_DIR) else 0,
            "scenes": sum(
                len(files) for _, _, files in os.walk(SCENES_DIR)
            ) if os.path.exists(SCENES_DIR) else 0,
        },
        "task_queue": _task_queue_status(),
        "gpu_gate": gpu_task_gate.status(),
        "interrupted_tasks": _interrupted,
    })


@bp.route('/api/consistency/run', methods=['POST'])
def api_consistency_run():
    """执行一致性校验（可指定 project_name / episode_no / 是否含资产多视图）"""
    data = request.json or {}
    project_name = _safe_project(data.get('project_name') or '')
    if not project_name:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
    episode_no = data.get('episode_no')
    try:
        collect = _consistency_collect(project_name, episode_no)
        cfg = _qc_load_cfg()
        report = consistency.run(
            project_name,
            character_refs=collect["character_refs"],
            shot_images=collect["shot_images"],
            asset_dirs=collect["asset_dirs"],
            cfg=cfg,
            include_assets=bool(data.get('include_assets', True)),
            include_shots=bool(data.get('include_shots', True)),
        )
    except Exception as e:  # noqa: BLE001
        app.logger.exception("一致性校验失败")
        return jsonify({"success": False, "error": f"一致性校验失败：{e}"}), 500
    return jsonify({"success": True, "project": project_name,
                    "summary": report.get("summary"),
                    "report": report, "report_path": report.get("report_path")})


@bp.route('/api/consistency/report/<path:project_name>', methods=['GET'])
def api_consistency_report(project_name):
    """读取已有的一致性报告（不重新校验）"""
    project_name = _safe_project(project_name)
    rep = consistency.load_report(project_name)
    if not rep:
        return jsonify({"success": False, "error": "暂无一致性报告，请先执行校验",
                        "project": project_name}), 404
    return jsonify({"success": True, "project": project_name, "report": rep,
                    "summary": rep.get("summary")})


# ===== P2-3 成本与耗时看板 =====

@bp.route('/api/analytics/summary', methods=['GET'])
def api_analytics_summary():
    """全局或按项目的成本/耗时汇总"""
    project = (request.args.get('project') or '').strip()
    try:
        limit = max(1, min(500, int(request.args.get('recent') or 100)))
    except (TypeError, ValueError):
        limit = 100
    try:
        data = analytics.summarize(project=project or None, recent_limit=limit)
        data["projects"] = analytics.list_projects()
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"统计读取失败：{e}"}), 500
    return jsonify({"success": True, **data})


@bp.route('/api/analytics/project/<path:project_name>', methods=['GET'])
def api_analytics_project(project_name):
    """单项目成本/耗时"""
    project_name = _safe_project(project_name)
    try:
        data = analytics.summarize(project=project_name)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"统计读取失败：{e}"}), 500
    return jsonify({"success": True, **data})


@bp.route('/api/analytics/event', methods=['POST'])
def api_analytics_record():
    """手工登记一条耗时事件（供前端/外部脚本补充统计）"""
    data = request.json or {}
    ok = analytics.record_event(
        kind=str(data.get('kind') or 'other'),
        project=_safe_project(data.get('project') or ''),
        label=str(data.get('label') or ''),
        duration_sec=float(data.get('duration_sec') or 0),
        units=int(data.get('units') or 0),
        success=bool(data.get('success', True)),
        meta=data.get('meta') if isinstance(data.get('meta'), dict) else None,
    )
    return jsonify({"success": bool(ok)})


@bp.route('/api/analytics/reset', methods=['POST'])
def api_analytics_reset():
    """清空统计（project 为空则整体清空）"""
    data = request.json or {}
    project = _safe_project(data.get('project') or '') if data.get('project') else None
    return jsonify({"success": True, **analytics.reset(project=project)})


# ==========================================================================
# P2-1 / P2-2  NLE 导出（剪映草稿 / FCPXML / SRT / 帧序列）
# ==========================================================================

def _quality_artifact_for(project_name: str, episode_no) -> str:
    """发布门禁绑定真实交付物：优先最终成片，缺回退到整集视频。"""
    try:
        ep = int(episode_no)
    except (TypeError, ValueError):
        return _quality_find_full(project_name, episode_no)
    try:
        for item in pipeline.list_deliverables(project_name) or []:
            try:
                if int(item.get("episode_no") or 0) != ep:
                    continue
            except (TypeError, ValueError):
                continue
            path = str(item.get("path") or "")
            if path and os.path.isfile(path):
                return path
    except Exception:
        pass
    return _quality_find_full(project_name, ep)


def _delivery_release_gate_for(project_name: str, filename: str = ""):
    """下载/取回导出文件前统一交付包门禁。

    质量门禁只说明“可以导出”；真正取回文件还必须有非空交付包、授权门禁、
    机器校验、人工批准与磁盘哈希同时成立。没有交付包时 fail-closed。
    """
    try:
        from domain import delivery as delivery_domain
        from infrastructure import delivery_repo, licensing_repo
    except ImportError:  # pragma: no cover - 脚本方式导入兜底
        from app.domain import delivery as delivery_domain
        from app.infrastructure import delivery_repo, licensing_repo
    blockers = []
    pkg_project = project_name
    try:
        rec = project_store.get_project(project_name)
        if rec:
            pkg_project = rec.get("dir_key") or project_name
    except Exception:
        pass
    try:
        packages = delivery_repo.list_packages(pkg_project)
    except Exception as exc:  # noqa: BLE001
        return [f"交付包清单读取失败：{exc}"]
    if not packages:
        return ["未登记交付包，无法下载发布文件"]
    target = str(filename or "").replace("\\", "/").lstrip("/")
    target_base = os.path.basename(target)
    # 只有「无目录的请求」才允许 basename 匹配：/api/export/<project>/<format>
    # 传的是裸文件名（如 <project>_fcpml.xml），而带目录的请求是导出根目录下的
    # 多集产物（ep02/ep02_final.mp4），必须 rel_path 精确相等。否则 ep01/final.mp4
    # 会凭同名 basename 命中别的集的那个包，借用它的批准。
    flat_request = "/" not in target
    candidates = []
    for pkg in packages:
        files = pkg.get("files") or []
        if target and any(
            str(f.get("rel_path") or "").replace("\\", "/") == target
            or (flat_request
                and os.path.basename(str(f.get("rel_path") or "")) == target_base)
            for f in files
        ):
            candidates.append(pkg)
    if not candidates:
        # 归属不明就拒绝。原先这里退化成「拿所有包去试」，只要任意一个已批准的包
        # 门禁通过就放行 —— 于是从未登记进任何包的中间产物也能被下载，而 ADR-0006
        # 的批准绑定的是**那个包**的 sha256，与本文件无关，批准形同虚设。
        return [f"交付文件不属于任何已登记交付包，已阻止下载：{target or '(空路径)'}"]
    passed = False
    for pkg in candidates:
        try:
            gate = licensing_repo.evaluate_delivery_gate(
                pkg.get("requirement") or {}, project=pkg.get("project") or pkg_project, audit=True)
            approval = delivery_repo.get_approval(pkg.get("package_id") or "")
            result = delivery_domain.release_ready(pkg, approval, licensing_gate=gate)
            if result.get("ok"):
                passed = True
                break
            blockers.extend([f"交付包 {pkg.get('package_id')}：{b}"
                             for b in (result.get("blockers") or [])])
        except Exception as exc:  # noqa: BLE001
            blockers.append(f"交付包 {pkg.get('package_id')} 门禁异常：{exc}")
    if not passed:
        return blockers or ["交付包发布门禁未通过"]
    return []


def _release_gate_for(project_name: str, episode_no=None):
    """导出前统一发布门禁：D 层批准 + 产物哈希绑定未失效。"""
    blockers = []
    eps = []
    if episode_no not in (None, "", 0, "0"):
        try:
            eps = [int(episode_no)]
        except (TypeError, ValueError):
            blockers.append(f"episode_no 非法：{episode_no!r}")
    else:
        try:
            for path in project_store.project_scripts(project_name):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                    ep = data.get("episode_no") or (data.get("metadata") or {}).get("episode_no")
                    if ep is not None:
                        eps.append(int(ep))
                except Exception:
                    continue
            eps = sorted(set(eps))
        except Exception:
            eps = []
    if not eps:
        blockers.append("未找到可发布剧集，导出已阻止")
        return blockers
    for ep in eps:
        state = quality_stage.load_state(project_name, ep)
        ready, reasons = quality_stage.release_ready(state)
        if not ready:
            blockers.extend([f"第{ep}集：{r}" for r in reasons if not str(r).startswith("提示：")])
        full = _quality_artifact_for(project_name, ep)
        for stage in ("C", "D"):
            entry = (state.get("stages") or {}).get(stage) or {}
            if entry.get("status") != "passed":
                continue
            binding = entry.get("binding")
            if not isinstance(binding, dict) or not binding:
                blockers.append(f"第{ep}集：{stage} 层已通过但缺少产物哈希绑定")
                continue
            if not str(binding.get("artifact_sha256") or "").strip():
                blockers.append(f"第{ep}集：{stage} 层绑定缺少 artifact_sha256")
                continue
            status, reason = quality_stage.check_stage_binding(state, stage, full)
            if status != "valid":
                blockers.append(f"第{ep}集：{stage} 层批准无效（{reason}）")
    return blockers



@bp.route('/api/export/run', methods=['POST'])
def api_export_run():
    """一键导出：剪映草稿 + FCPXML + SRT + 帧序列清单

    body: {project_name, episode_no?, formats?: ["jianying","fcpxml","srt","frames"]}
    """
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    blockers = _release_gate_for(project, data.get('episode_no'))
    if blockers:
        return jsonify({"success": False, "error": "导出发布门禁未通过",
                        "blockers": blockers}), 409
    script = _load_script_for(project, data.get('episode_no'))
    if not script:
        return jsonify({"success": False, "error": "剧本不存在"}), 404
    formats = data.get('formats')
    # B-17 P2-13：传集号给 nle_export，按集号过滤视频目录，避免跨集混用素材
    ep_no = data.get('episode_no')
    try:
        results = nle_export.export_all(project, script,
                                        formats=formats if isinstance(formats, list) else None,
                                        episode=ep_no)
    except Exception as e:  # noqa: BLE001
        app.logger.exception("NLE 导出失败")
        return jsonify({"success": False, "error": f"导出失败：{e}"}), 500
    return jsonify({"success": bool(results.get("ok")), "project": project, **results})


@bp.route('/api/export/list', methods=['GET'])
def api_export_list():
    """导出记录列表（可按项目过滤）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else None
    try:
        items = nle_export.list_exports(project)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": str(e)}), 500
    # 前端（ExportPage / 工作台 ExportTab）统一消费 `files` 字段：
    # 这里在保留 `items` 原始结构的同时，补一份前端可直接渲染的规范化列表。
    files = []
    seen_names = set()
    for it in items:
        p = it.get("path") or ""
        name = os.path.basename(p) if p else ""
        if name:
            seen_names.add(name)
        files.append({
            "format": it.get("format"),
            "filename": name,
            "exists": bool(p and os.path.isfile(p)),
            "path": p,
            "dir": it.get("dir"),
            "project": it.get("project"),
            "exported_at": it.get("exported_at"),
            "shot_count": it.get("shot_count"),
            "total_sec": it.get("total_sec"),
            "size_mb": it.get("size_mb"),
        })

    # 合并 ExportManager 产物（output/exports/<project>/），
    # 该目录与 nle_export.EXPORT_DIR（output/export/）不同，需单独扫描，
    # 否则「生成导出文件」后列表仍显示为空。
    if project:
        em_dir = os.path.join(PROJECT_OUTPUT_DIR, "exports", project)
        known = [
            ("fcpml", f"{project}_fcpml.xml"),
            ("edl", f"{project}_edl.edl"),
            ("json", f"{project}_timeline.json"),
        ]
        for fmt, fn in known:
            fp = os.path.join(em_dir, fn)
            if os.path.isfile(fp) and fn not in seen_names:
                files.append({
                    "format": fmt,
                    "filename": fn,
                    "exists": True,
                    "path": fp,
                    "dir": em_dir,
                    "project": project,
                    "exported_at": datetime.fromtimestamp(
                        os.path.getmtime(fp)).isoformat(timespec="seconds"),
                    "size_mb": round(os.path.getsize(fp) / 1048576, 3),
                })

    return jsonify({"success": True, "count": len(files), "items": items, "files": files})


@bp.route('/api/export/download/<path:filename>')
def api_export_download(filename):
    """导出产物下载（限导出根目录内；下载前同样走发布门禁）"""
    parts = [x for x in str(filename or "").replace("\\", "/").split("/") if x and x != "."]
    project = str(request.args.get("project") or "").strip()
    if not project and len(parts) > 1:
        project = parts[0]
    if not project:
        base = os.path.basename(parts[-1]) if parts else ""
        for rec in project_store.list_projects(with_stats=False):
            for key in (rec.get("dir_key"), rec.get("id"), rec.get("name")):
                key = str(key or "")
                if key and (base == key or base.startswith(key + "_")):
                    project = rec.get("dir_key") or key
                    break
            if project:
                break
    blockers = _release_gate_for(project) if project else ["无法识别导出文件所属项目，下载已阻止"]
    if project:
        blockers.extend(_delivery_release_gate_for(project, filename))
    if blockers:
        return jsonify({"success": False, "error": "导出发布门禁未通过",
                        "blockers": blockers}), 409
    return _serve_attachment(nle_export.EXPORT_DIR, filename)


@bp.route('/api/plugins', methods=['GET'])
def api_plugins():
    """插件目录（内置环节 + plugins/ 目录下用户自定义 Agent）"""
    try:
        data = plugin_registry.catalog()
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"插件目录读取失败：{e}"}), 500
    return jsonify({"success": True, **data,
                    "dependency_order": plugin_registry.get_registry().dependency_order()})


@bp.route('/api/plugins/run', methods=['POST'])
def api_plugins_run():
    """执行指定插件（把剧本等上下文传入插件，返回插件产出）"""
    data = request.json or {}
    pid = str(data.get('plugin_id') or '').strip()
    if not pid:
        return jsonify({"success": False, "error": "缺少 plugin_id"}), 400
    ctx = data.get('context') if isinstance(data.get('context'), dict) else {}
    project = _safe_project(data.get('project_name') or '')
    if project and 'script' not in ctx:
        ctx['script'] = _load_script_for(project, data.get('episode_no'))
        ctx['project_name'] = project
    try:
        result = plugin_registry.run(pid, ctx)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"插件执行失败：{e}"}), 500
    return jsonify({"success": bool(result.get("ok")), "plugin_id": pid, "result": result})


# ==========================================================================
# ComfyUI 模型 / 插件扫描 + 手选模型
#
#  为什么要"扫描"而不是直接读模板：ComfyUI 某节点 combo 的合法值取决于模型在
#  磁盘上的**目录布局**（放进 diffusion_models/minimax-h3/ 后名字会带
#  `minimax-h3\` 前缀），而工作流模板里通常写死的是裸文件名。两边一旦不符，
#  ComfyUI 校验失败 → 该节点产出被丢弃（H3 视频就曾因此整段静默失败）。
#  所以这里以 ComfyUI 的 object_info 为**唯一权威来源**给出候选，由用户手选。
# ==========================================================================

@bp.route('/api/comfyui/models', methods=['GET'])
def api_comfyui_models():
    """扫描 ComfyUI 真实可用的模型槽位候选值 + 已安装自定义节点包。

    refresh=1 强制绕过 object_info 缓存重新拉取（前端「重新扫描」按钮用）。
    返回体含 success；ComfyUI 离线时 success=False 且带 error，不抛异常。
    """
    force = str(request.args.get('refresh') or '').strip() in ('1', 'true', 'yes')
    data = comfyui_models.scan(comfyui_client, force=force)
    status = 200 if data.get("success") else 503
    return jsonify(data), status


@bp.route('/api/comfyui/models', methods=['POST'])
def api_comfyui_models_select():
    """保存用户手动指定的模型（按槽位）。

    body: {"unet_main": "<模型名>", ...}
    显式传 null 或 "" 表示**清空**该槽位，回落到工作流模板自身的取值。
    """
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "body 必须是 JSON 对象"}), 400
    # 只允许已知槽位，脏 key 直接忽略（不报错，避免前后端版本差导致整体失败）
    known = {k: v for k, v in data.items() if k in comfyui_models.SLOTS}
    try:
        # fail-closed：保存前重新拉一次最新 object_info，核验语义 + combo 双重合法性。
        object_info = comfyui_client.get_object_info(force=True)
        selection = comfyui_models.save_selection(known, object_info=object_info)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"保存失败：{e}"}), 500
    return jsonify({"success": True, "selection": selection})


@bp.route('/api/comfyui/actual-params', methods=['GET'])
def api_comfyui_actual_params():
    """P0-5：返回最近提交的“实际生效参数”快照（模板 ≠ 最终提交值）。"""
    try:
        limit = int(request.args.get('limit') or 20)
    except (TypeError, ValueError):
        limit = 20
    try:
        return jsonify({"success": True,
                        "items": actual_params.latest(limit)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "items": [], "error": str(e)}), 200


@bp.route('/api/logs', methods=['GET'])
def api_logs():
    """查看后台服务日志（只读）。

    服务由计划任务后台启动、没有终端窗口，日志原本只能去翻磁盘上的
    ``.workbuddy/test/_out/serve_stdout.log``。这里把它接到 Web 上，方便实时排查。

    查询参数:
        source: serve（默认） / comfyui —— **仅接受白名单 key，绝不接受路径**（防穿越）
        tail:   返回尾部多少行，默认 300，上限 2000
        since:  字节偏移，>0 时只返回此后新增的内容（前端「自动刷新」用）
        q:      关键字过滤；level: ERROR / WARNING / INFO / DEBUG
    """
    src = str(request.args.get('source') or '').strip()
    try:
        tail = int(request.args.get('tail') or 0)
        since = int(request.args.get('since') or 0)
    except ValueError:
        return jsonify({"success": False, "error": "tail / since 必须是整数"}), 400
    data = log_viewer.tail_lines(
        source=src or log_viewer.DEFAULT_SOURCE,
        tail=tail or log_viewer.DEFAULT_TAIL,
        since=since,
        query=str(request.args.get('q') or '').strip(),
        level=str(request.args.get('level') or '').strip().upper(),
    )
    status = 200 if data.get("success") else 400
    return jsonify(data), status


@bp.route('/api/logs/sources', methods=['GET'])
def api_logs_sources():
    """列出可查看的日志源及其大小 / 最后更新时间。"""
    return jsonify({"success": True, "sources": log_viewer.available_sources()})


# ==========================================================================
# P2-5 国际化
# ==========================================================================

@bp.route('/api/i18n/<lang>', methods=['GET'])
def api_i18n(lang):
    """读取前端语言包（zh-CN / en-US）"""
    lang = (lang or 'zh-CN').strip()
    safe = "".join(c for c in lang if c.isalnum() or c in "-_") or "zh-CN"
    locale_dir = os.path.join(PROJECT_ROOT_DIR, "locales")
    path = os.path.join(locale_dir, f"{safe}.json")
    if not os.path.isfile(path):
        fallback = os.path.join(locale_dir, "zh-CN.json")
        if not os.path.isfile(fallback):
            return jsonify({"success": False, "error": f"语言包不存在：{safe}",
                            "available": []}), 404
        path = fallback
        safe = "zh-CN"
    try:
        with open(path, "r", encoding="utf-8") as f:
            pack = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"语言包解析失败：{e}"}), 500
    available = []
    if os.path.isdir(locale_dir):
        available = sorted(os.path.splitext(f)[0] for f in os.listdir(locale_dir)
                           if f.lower().endswith(".json"))
    return jsonify({"success": True, "lang": safe, "messages": pack,
                    "available": available})


# ===================== P2-2：RefMod 节点探测 + UI→API 兼容自检（Fizgig/MiniMaxH3Mod） =====================

@bp.route('/api/comfyui/refmod-status', methods=['GET'])
def api_comfyui_refmod_status():
    """探测 ComfyUI 是否安装 RefMod / MiniMaxH3Mod 节点 + UI→API 兼容自检（P2-2 PoC）。

    RefMod：把一个角色的多张参考图/视频打包成单个 .safetensors，像 LoRA 一样
    直接喂给 H3 context（免训练、不占参考图槽位）。未安装时先 clone
    ComfyUI-MiniMaxH3Mod 到 custom_nodes 并重启 ComfyUI，再回来看本接口。

    在既有返回键（success / comfyui / installed / nodes / hint，不可达时
    success=False + error + 502）基础上追加 ``refmod`` 结构化结果：

        {"refmod": {"reachable": bool, "nodes": [{"class", "input"}],
                    "poc": {"node_class", "ui_to_api_ok", "api_nodes",
                            "missing_inputs", "error"?}}}

    poc 是对探测到的**第一个** refmod 类节点跑的 UI→API 兼容自检
    （comfyui_client.refmod_ui_to_api_poc：合成最小 UI 图 → to_api →
    validate_api_prompt）。fail-open：ComfyUI 不可达 / 节点不存在 / 任何异常
    都返回结构化结果，绝不抛错、不影响任何生成链路。
    """
    # 模块级函数必须从模块对象 import（本文件里 `comfyui_client` 名字被实例占用，
    # 见文件顶部 import 处注释；函数内 from-import 命中的是 sys.modules 里的模块）
    from comfyui_client import probe_refmod_nodes, refmod_ui_to_api_poc
    try:
        probe = probe_refmod_nodes(timeout=15)
        # 与旧实现一致按类名排序（object_info 键序不保证稳定），排序后第一个即 PoC 对象
        nodes = sorted(probe.get("nodes") or [],
                       key=lambda n: str((n or {}).get("class") or ""))
        names = [n.get("class") for n in nodes if isinstance(n, dict) and n.get("class")]
        base = {
            "success": bool(probe.get("reachable")),
            "comfyui": COMFYUI_URL,
            "installed": bool(names),
            "nodes": names,
            "hint": ("已安装，可进入 RefMod PoC" if names else
                     "未安装：clone ComfyUI-MiniMaxH3Mod 到 custom_nodes 后重启 ComfyUI"),
        }
        refmod = {"reachable": bool(probe.get("reachable")), "nodes": nodes}
        if probe.get("error"):
            refmod["error"] = probe.get("error")
        # 对第一个 refmod 类节点跑 UI→API 兼容自检（失败只记录，不影响探测结论）
        if refmod["reachable"] and nodes:
            first = nodes[0]
            try:
                refmod["poc"] = refmod_ui_to_api_poc(
                    first.get("class"), first.get("input") or {}, timeout=15)
            except Exception as e:  # noqa: BLE001  PoC 兜底（其内部已 fail-open，正常到不了这里）
                refmod["poc"] = {"node_class": first.get("class"), "ui_to_api_ok": False,
                                 "api_nodes": 0, "missing_inputs": [],
                                 "error": f"{type(e).__name__}: {e}"}
        base["refmod"] = refmod
        if not probe.get("reachable"):
            # 保持既有 502 契约（success=False + error），并附结构化 refmod 不可用结果
            base["error"] = probe.get("error") or "ComfyUI 不可达"
            return jsonify(base), 502
        return jsonify(base)
    except Exception as e:  # noqa: BLE001  整体 fail-open：绝不把异常抛成裸 HTML 500
        err = f"{type(e).__name__}: {e}"
        return jsonify({"success": False, "comfyui": COMFYUI_URL, "installed": False,
                        "nodes": [], "hint": "RefMod 探测/自检异常", "error": err,
                        "refmod": {"reachable": False, "nodes": [], "error": err}}), 502


@bp.route('/api/prompt_enhance/config', methods=['GET', 'POST'])
def api_prompt_enhance_config():
    """提示词增强总开关（前端「AI 配置」页的开关）。

    - GET  返回当前生效值 + 各来源（env 覆盖 / 文件开关 / 代码默认），让页面如实标注状态；
    - POST body {enhance_enabled?, review_enabled?} 写文件级开关，**立即生效、无需重启**：
      prompt_enhance.enhance_enabled()/review_enabled() 每次调用都重读该文件。
      优先级：env（MJSCXT_PROMPT_ENHANCE / MJSCXT_PROMPT_MODEL_REVIEW，运维最高）> 文件 > 代码默认。
    """
    import prompt_enhance
    if request.method == "POST":
        data = request.json or {}
        if not any(k in data for k in ("enhance_enabled", "review_enabled")):
            return jsonify({"success": False, "error": "缺少 enhance_enabled / review_enabled"}), 400
        cfg = save_prompt_enhance_config(data)
    else:
        cfg = _prompt_enhance_file_flags()
    flags = _prompt_enhance_file_flags()
    return jsonify({
        "success": True,
        "config": cfg,
        "file_config": flags,
        "effective": {
            "enhance_enabled": prompt_enhance.enhance_enabled(),
            "review_enabled": prompt_enhance.review_enabled(),
        },
        "env_overridden": {
            "enhance": os.environ.get("MJSCXT_PROMPT_ENHANCE", "").strip() != "",
            "review": os.environ.get("MJSCXT_PROMPT_MODEL_REVIEW", "").strip() != "",
        },
        "config_path": os.path.abspath(PROMPT_ENHANCE_CONFIG_PATH),
        "message": "提示词增强开关已保存，立即对后续生成生效（无需重启）",
    })


@bp.route('/api/deps/check', methods=['GET'])
def api_deps_check():
    """依赖自检：核查 ComfyUI 侧「插件节点」与「模型权重」是否齐备（只读，绝不阻断）。

    依据 docs/依赖清单.md：模板 JSON 已内置，但跑起来还差①插件包（custom_nodes/）
    ②模型权重（models/）。本端点把两层逐一报出「就位 / 缺失 / 无法判定」。

    query 参数：
      offline=1        强制离线（不探 ComfyUI，只扫本地 custom_nodes/ 目录名，标注不可靠）
      url=<host:port>  指定 ComfyUI 地址（覆盖 config.COMFYUI_URL）
    返回：comfyui / plugins / models / workflows / summary（见 deps_check.check_deps 契约）
    """
    offline = request.args.get('offline', '').strip().lower() in ('1', 'true', 'yes')
    url_arg = (request.args.get('url') or '').strip()
    try:
        result = deps_check.check_deps(comfyui_url=(url_arg or None), force_offline=offline)
    except Exception as e:
        # 检测本身是「锦上添花」，任何异常都降级为 200 + 明确标记，不把自检打挂
        return jsonify({"error": str(e), "comfyui": {"online": False},
                        "summary": {"all_ok": False, "blockers": [f"检测异常：{e}"]}})
    result["docs"] = "docs/依赖清单.md"
    return jsonify(result)


@bp.route('/api/trt-engine/check', methods=['GET'])
def api_trt_engine_check():
    """P0-3：TRT engine 三态自检；?probe=1 提交最小 Encode→Decode 加载探针。"""
    probe = str(request.args.get('probe', '')).strip().lower() in ('1', 'true', 'yes')
    try:
        timeout = float(request.args.get('timeout') or 90)
    except (TypeError, ValueError):
        timeout = 90.0
    try:
        return jsonify(trt_engine_check.check(
            client=comfyui_client, probe=probe, timeout=max(5.0, min(timeout, 300.0))))
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "status": "未测试",
                        "reason": f"自检异常：{e}"}), 200


# =====================================================================
# AI 设置：文本分析 / 质检 / 对话总控 三个独立模块
# =====================================================================

@bp.route('/api/ai/config', methods=['GET'])
def api_ai_config_get():
    """读取统一 AI 设置（三模块，api_key 一律脱敏）"""
    return jsonify({"success": True, "config": _ai_config_view()})


@bp.route('/api/ai/config/reveal', methods=['GET'])
def api_ai_config_reveal():
    """按需回显某个模块已保存的 api_key 明文（前端「眼睛」按钮点开时调用）。

    默认 GET /api/ai/config 仍一律脱敏（module_public_view 永不含明文）；
    本端点只在用户显式点「显示」时被调用，把明文回填进输入框——否则已保存的
    密钥在前端只是 placeholder 圆点，切 type 什么都显不出来。
    本应用是本地单用户工具（仅 127.0.0.1），密钥明文本就只存在本机。
    """
    module = (request.args.get('module') or '').strip()
    if module not in ai_config.MODULES:
        return jsonify({"success": False, "error": f"未知的 AI 模块：{module}"}), 400
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(cfg, module)
    key = ep.get("api_key") or ""
    return jsonify({"success": True, "module": module,
                    "has_api_key": bool(key), "api_key": key})


@bp.route('/api/ai/config', methods=['POST'])
def api_ai_config_save():
    """保存单个模块：{module: text|qc|chat, base_url, model, api_key?}"""
    return _save_ai_module(request.json or {})


@bp.route('/api/ai/config/clear', methods=['POST'])
def api_ai_config_clear():
    """清空单个模块；不传 module 则整体重置三个模块"""
    data = request.json or {}
    module = (data.get("module") or "").strip() or None
    if module and module not in AI_MODULES:
        return jsonify({"success": False, "error": f"unknown module：{module}"}), 400
    # A-22（M5）：clear_module 有落盘副作用（清空模块配置 + 同步清密钥库 + **镜像清 DB**），
    # 必须保留调用；返回值此前被赋给 cfg 却从未使用（响应改由下方 _ai_config_view() 重新取整份视图），故去掉赋值。
    ai_config.clear_module(AI_CONFIG_PATH, module=module, legacy_path=LLM_CONFIG_PATH)
    # ⭐ 与「保存」对称：清库同样由 `ai_config.clear_module` 内部闭合（`_mirror_credentials_db(
    # clear=True)`），这里只**读回核对** —— 避免「AI 设置显示已清空，任务却仍读到 DB 旧凭证」
    # 这种页面上看不出来的漂移（清库失败会让任务继续用旧密钥跑）。
    db_clear_note, db_clear_error = _ai_credentials_verify(module, cleared=True)
    if db_clear_error:
        app.logger.error("AI 凭证库清空核对不通过（module=%s）：%s", module, db_clear_error)
    reset_note, reset_error = "", ""
    if module in (None, "qc"):
        try:
            qc_client.reset_endpoint(QC_CONFIG_PATH)
            reset_note = "，质检接口已同步重置"
        except Exception as e:  # noqa: BLE001
            reset_error = f"{type(e).__name__}: {e}"
            app.logger.warning(f"质检接口重置失败（AI 设置已清空，质检可能仍用旧接口）：{reset_error}")
    return jsonify({
        "success": True,
        "module": module,
        "config": _ai_config_view(),
        "config_path": os.path.abspath(AI_CONFIG_PATH),
        "credentials_db_cleared": bool(not db_clear_error),
        "credentials_db_error": db_clear_error,
        "qc_reset": bool(module in (None, "qc") and not reset_error),
        "qc_reset_error": reset_error,
        "message": ((f"{AI_MODULE_LABEL.get(module, module)}配置已清除" if module else "AI 设置已整体重置")
                    + db_clear_note
                    + reset_note
                    + (f"；但凭证清空核对未通过：{db_clear_error}" if db_clear_error else "")
                    + (f"；但质检接口重置失败：{reset_error}" if reset_error else "")),
    })


@bp.route('/api/ai/selfcheck', methods=['GET'])
def api_ai_selfcheck():
    """AI 前置自检（P0-5）：三个模块（text/qc/chat）的配置完整性 + 可选端点可达性。

    query:
      probe=1  附带端点可达性探测（GET {base_url}/models，结果带 60s 缓存）
      fresh=1  强制绕过探测缓存（刚改完配置时用）

    返回的 `ok=False` 即「当前配置一开跑就会失败」，前端据此在 AI 设置页 / 项目页
    显示红字阻断提示；`modules[*].hint` 给出逐模块的修复指引。
    """
    _probe = str(request.args.get("probe") or "").strip().lower() in ("1", "true", "yes", "on")
    _fresh = str(request.args.get("fresh") or "").strip().lower() in ("1", "true", "yes", "on")
    try:
        import ai_selfcheck
        rep = ai_selfcheck.check_modules(probe=_probe, force_probe=_fresh)
        _gate_off = ai_selfcheck.gate_disabled()
    except Exception as e:  # noqa: BLE001  自检失败按 500 明确报出，不假装通过
        return jsonify({"success": False,
                        "error": f"自检执行失败：{type(e).__name__}: {e}"}), 500
    return jsonify({
        "success": True,
        "ok": rep["ok"],
        "probed": rep["probed"],
        "message": rep["message"],
        "hint": rep["hint"],
        "blocked_modules": rep["blocked_modules"],
        "blocked_labels": rep["blocked_labels"],
        "modules": rep["modules"],
        "gate_off": _gate_off,
    })


@bp.route('/api/ai/test', methods=['POST'])
def api_ai_test():
    """测试单个模块连通性（可用页面暂存参数，不落盘）。

    - text / chat：文本连通性对话测试
    - qc：默认做「视觉能力」探测（发一张极小图片），确认模型支持图像输入；
          也可传 probe="text" 只测文本连通性。
    """
    data = request.json or {}
    module = (data.get("module") or "").strip()
    if module not in AI_MODULES:
        return jsonify({"success": False, "error": f"unknown module：{module or '(空)'}"}), 400

    # 用户点了「测试连接」= 刚改完配置想验证 → 必须清掉旧熔断。
    # 否则网关恢复/换好网关后，这里会被上一轮的熔断状态直接拦下并报"不可用"，
    # 用户会以为新配置也没用。
    LLMClient.reset_gateway_circuits()
    # 同理清掉前置自检的端点探测缓存：测试通过后马上开跑，门禁不该还拿着改之前的旧结论
    try:
        import ai_selfcheck
        ai_selfcheck.reset_probe_cache()
    except Exception as e:  # noqa: BLE001
        app.logger.debug("重置 AI 前置自检探测缓存失败（忽略）：%s", e)

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    saved = ai_config.get_module(cfg, module)
    # 备用模型「测试连接」：前端拿不到已保存备用的明文密钥（对外视图恒脱敏），
    # 所以按 fallback_index 在**服务端**取那条备用的完整配置（含解密后的 key）再探。
    # 显式传了 base_url/model/api_key 时以显式值为准（用于「还没保存」的草稿条目）。
    _fb = None
    _fb_idx_raw = data.get("fallback_index")
    if _fb_idx_raw is not None and str(_fb_idx_raw).strip() != "":
        try:
            _fbs_saved = saved.get("fallbacks") or []
            _fb_i = int(_fb_idx_raw)
            if 0 <= _fb_i < len(_fbs_saved):
                _fb = _fbs_saved[_fb_i]
        except (TypeError, ValueError):
            _fb = None
        if _fb is None:
            return jsonify({"success": False, "module": module,
                            "error": f"备用模型 #{_fb_idx_raw} 不存在（可能已被删除），请刷新页面后重试"}), 400
    _fb_base = str((_fb or {}).get("base_url") or "").strip()
    _fb_model = str((_fb or {}).get("model") or "").strip()
    _fb_key = str((_fb or {}).get("api_key") or "").strip()
    base_url = (data.get("base_url") or _fb_base or saved["base_url"] or "").strip()
    model = (data.get("model") or _fb_model or saved["model"] or "").strip()
    api_key = data.get("api_key")
    if not api_key or not str(api_key).strip() or "*" in str(api_key):
        api_key = _fb_key or saved["api_key"] or ""
    # 思考档位：页面暂存值优先，否则沿用该备用 / 主模型的已保存值
    _re = data.get("reasoning_effort")
    if _re is None:
        _re = (_fb or {}).get("reasoning_effort") or saved.get("reasoning_effort") or ""
    ep = {"base_url": base_url, "api_key": str(api_key).strip(), "model": model,
          "reasoning_effort": str(_re).strip()}
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return jsonify({"success": False, "module": module, "endpoint": {k: v for k, v in ep.items() if k != "api_key"},
                        "error": "base_url / api_key / model 均为必填，请填写完整后再测试"}), 400

    probe = (data.get("probe") or ("vision" if module == "qc" else "text")).strip()
    if module == "qc" and probe == "vision":
        result = qc_client.test_vision(ep, timeout=int(data.get("timeout") or 60))
        result.update({"module": module, "probe": "vision", "model": ep["model"],
                       "base_url": ep["base_url"]})
        if not result.get("success"):
            result["guide"] = ("该接口或模型不支持图像输入（或不可达）。质检需要多模态模型，"
                               "请改用支持视觉的模型（如 gpt-4o-mini / qwen-vl-max / glm-4v）。")
        return jsonify(result), (200 if result.get("success") else 400)

    try:
        # ⭐ P0-5 第②项收尾：探针与运行态**必须走同一条客户端构造路径**。
        # 此前这里自建 LLMClient(AI_CONFIG_PATH, config=ep)，运行态走 _ai_client_for_module，
        # 两边各写一份「参数取谁 / reasoning_effort 怎么注入 / 默认超时多少」的推导 ——
        # 一旦分叉就是「测试连接通过、运行时行为不一致」的经典温床。现在只此一条路径。
        # with_fallbacks=False：测试连接必须**只测这一个端点**（详见 _ai_client_for_module 的说明）。
        # 运行态照旧带备用链，两者在「参数推导」上仍然同源，只是探针不参与故障转移。
        client = _ai_client_for_module(
            module, base_url=ep["base_url"], api_key=ep["api_key"], model=ep["model"],
            timeout=int(data.get("timeout") or 60), reasoning_effort=ep["reasoning_effort"],
            with_fallbacks=False)
        result = dict(client.test_connection() or {})
    except LLMError as e:
        # 与运行态同一个报错：未配置时的提示语完全一致，不再出现"测试说没问题、跑起来报未配置"
        result = {"success": False, "error": str(e)}
    except LLMGatewayUnavailable as e:
        # 网关整体不可用：明确区分于「密钥写错」
        result = {"success": False, "verdict": "gateway_unavailable",
                  "error": str(e), "hint": e.hint, "streak": e.streak,
                  "guide": "连通测试已跳过重试（上游报无可用算力时重试无意义）。"
                           "请更换 base_url 或模型，本项目的 AI 设置是三个模块各自独立的。"}
    except Exception as e:  # noqa: BLE001
        result = {"success": False, "error": f"{type(e).__name__}: {e}"}
    result.update({"module": module, "probe": "text", "model": ep["model"], "base_url": ep["base_url"],
                   "circuit": LLMClient.gateway_circuit_state(ep["base_url"])})
    return jsonify(result), (200 if result.get("success") else 400)


# ---- 兼容旧接口：/api/llm/config 系列（等价于「文本分析模型」模块） ----

@bp.route('/api/llm/config', methods=['GET'])
def api_llm_config_get():
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    view = ai_config.module_public_view(ai_config.get_module(cfg, "text"))
    view["guide"] = None if view["configured"] else LLM_NOT_CONFIGURED_GUIDE
    view["deprecated"] = "该接口为兼容旧前端保留，等价于 /api/ai/config 的 text 模块"
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(AI_CONFIG_PATH)})


@bp.route('/api/llm/config', methods=['POST'])
def api_llm_config_save():
    """兼容旧前端：强制保存 text 模块（= 文本分析模型 = LLM 引擎）

    旧实现是 `data["module"]="text"; return api_ai_config_save()`，
    但 `api_ai_config_save` 内部会重新从 `request.json` 取值，本地 dict 的修改
    完全无效 —— 结果是旧接口永远存不进 text 模块（调用方不传 module 时直接报
    "unknown module：(空)"）。这里改为把改写后的 data 显式传进共用的实现。
    """
    data = dict(request.json or {})
    data["module"] = "text"
    return _save_ai_module(data)


@bp.route('/api/llm/config/clear', methods=['POST'])
def api_llm_config_clear():
    cfg = ai_config.clear_module(AI_CONFIG_PATH, module="text", legacy_path=LLM_CONFIG_PATH)
    view = ai_config.module_public_view(ai_config.get_module(cfg, "text"))
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(AI_CONFIG_PATH),
                    "message": "文本分析模型配置已清除"})


@bp.route('/api/llm/test', methods=['POST'])
def api_llm_test():
    data = request.json or {}
    data["module"] = "text"
    data.setdefault("probe", "text")
    return api_ai_test()


@bp.route('/api/ai/chat/history', methods=['GET'])
def api_ai_chat_history():
    """读取会话历史 + 当前草稿 + 已生效设定（供界面恢复）"""
    project = (request.args.get("project") or "").strip()
    return jsonify({"success": True, "state": _chat_state(project)})


@bp.route('/api/ai/chat/archive', methods=['GET'])
def api_ai_chat_archive():
    """列出某项目归档的日期与每日条数（只读；全量真相源的浏览入口）。

    query: project（可空 → 回退当前活跃项目）
    → {success, project, dates:[{date,count}], total}
    """
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    key = _archive_project_key(request.args.get("project") or "", history)
    if not key:
        return jsonify({"success": True, "project": "", "dates": [], "total": 0})
    root = os.path.dirname(os.path.abspath(AI_CHAT_HISTORY_PATH))
    dates = ai_chat.archive_dates(root, key)
    return jsonify({"success": True, "project": key, "dates": dates,
                    "total": sum(int(d.get("count") or 0) for d in dates)})


@bp.route('/api/ai/chat/archive/<date>', methods=['GET'])
def api_ai_chat_archive_date(date):
    """读取某项目某天的归档消息（只读分页）。

    query: project（可空 → 回退当前活跃项目）/ limit（默认 0=全部）/ offset
    → {success, date, total, messages:[...]}
    """
    date = str(date or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return jsonify({"success": False, "error": "日期格式应为 YYYY-MM-DD"}), 400
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    key = _archive_project_key(request.args.get("project") or "", history)
    if not key:
        return jsonify({"success": True, "date": date, "total": 0, "messages": []})
    try:
        limit = max(0, int(request.args.get("limit", 0)))
    except (TypeError, ValueError):
        limit = 0
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    root = os.path.dirname(os.path.abspath(AI_CHAT_HISTORY_PATH))
    messages = ai_chat.load_archive(root, key, date, limit=limit, offset=offset)
    return jsonify({"success": True, "date": date,
                    "total": ai_chat.archive_count(root, key, date),
                    "messages": messages})


@bp.route('/api/ai/chat/clear', methods=['POST'])
def api_ai_chat_clear():
    """清空会话（默认保留创作设定草稿与已生效设定）"""
    data = request.json or {}
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    history = ai_chat.clear_history(AI_CHAT_HISTORY_PATH,
                                    keep_settings=bool(data.get("keep_settings", True)),
                                    project=project)
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
    return jsonify({"success": True, "message": "会话已清空", "state": _chat_state(project)})


@bp.route('/api/ai/chat', methods=['POST'])
def api_ai_chat():
    """一轮对话：调用「对话总控模型」，返回回复并同步更新创作设定草稿"""
    data = request.json or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "message 不能为空"}), 400
    if len(message) > ai_chat.MAX_CHARS_PER_MESSAGE:
        message = message[:ai_chat.MAX_CHARS_PER_MESSAGE]

    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    history["active_project"] = project
    ai_chat.append_message(history, "user", message, project)

    draft = ai_chat.get_draft(history, project)
    if isinstance(data.get("draft"), dict) and data["draft"]:
        draft = ai_chat.merge_settings(draft, data["draft"])       # 界面手工补充的设定

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(cfg, "chat")
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        ai_chat.drop_last_message(history, project)               # 未配置则不落用户消息，避免脏历史
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({
            "success": False,
            "error": "「对话总控模型」尚未配置（base_url / api_key / model）",
            "guide": LLM_NOT_CONFIGURED_GUIDE_MAP["chat"],
            "need_config": True,
            "state": _chat_state(project),
        }), 400

    messages = ai_chat.build_messages(history, draft, project)
    try:
        client = LLMClient(AI_CONFIG_PATH, config=ep, timeout=LLM_REQUEST_TIMEOUT)
        reply = client.chat(messages, temperature=0.7, max_tokens=2048)
    except LLMError as e:
        ai_chat.drop_last_message(history, project)
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({"success": False, "error": f"对话总控模型调用失败：{e}",
                        "state": _chat_state(project)}), 400
    except Exception as e:  # noqa: BLE001
        ai_chat.drop_last_message(history, project)
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({"success": False, "error": f"对话总控模型调用异常：{e}",
                        "state": _chat_state(project)}), 500

    new_settings = ai_chat.extract_settings(reply)
    if new_settings:
        draft = ai_chat.merge_settings(draft, new_settings)
        ai_chat.set_draft(history, project, draft)
    display = ai_chat.strip_json_block(reply)
    ai_chat.append_message(history, "assistant", display, project)
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)

    state = _chat_state(project)
    return jsonify({"success": True, "reply": display, "raw_reply": reply,
                    "new_settings": new_settings, "draft": draft,
                    "model": {"base_url": ep["base_url"], "model": ep["model"]},
                    "state": state})


@bp.route('/api/ai/chat/apply', methods=['POST'])
def api_ai_chat_apply():
    """应用设定：把当前草稿（可叠加 patch）落盘为项目创作设定配置"""
    data = request.json or {}
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    draft = ai_chat.get_draft(history, project)
    patch = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    merged = ai_chat.merge_settings(draft, patch)
    if not merged:
        return jsonify({"success": False, "error": "当前没有任何已确认的创作设定，无法应用",
                        "state": _chat_state(project)}), 400

    ai_chat.save_project_settings(AI_SETTINGS_PATH, project, merged)
    ai_chat.set_draft(history, project, merged)
    history["active_project"] = project
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)

    # 同步风格到项目 config.json：否则 config.json 的 style 停在建项目时的默认值
    # （如 3D动漫渲染），与总控刚敲定的设定不一致，用户会以为「设定没生效」。
    state = _chat_state(project)
    _sync_project_config_style(project, ai_chat.style_brief(state.get("settings") or {}))
    # ⚠️ normalize_settings 只保留白名单字段，其余**静默丢弃**。
    # 模型自造键名（实测出现过 color_tone / camera_language）时，
    # 用户以为「冷色调、克制镜头」已经写进去了，落盘却只剩 style —— 白沟通一场。
    # 这里把被丢弃的键如实回传，总控才能纠正键名并如实告知用户。
    dropped = sorted(k for k in (patch or {}).keys()
                     if k not in ai_chat.FIELD_LABELS)
    message = f"创作设定已应用（{len(merged)} 项）"
    if dropped:
        message += (f"；以下字段名不被支持，已忽略：{'、'.join(dropped)}"
                    "（合法字段见 allowed_fields，请用合法键名重试）")
    return jsonify({
        "success": True,
        "message": message,
        "dropped_fields": dropped,
        "allowed_fields": list(ai_chat.FIELD_LABELS.keys()),
        "settings": state["settings"],
        "draft": merged,
        "state": state,
    })


@bp.route('/api/ai/chat/draft', methods=['POST'])
def api_ai_chat_draft():
    """直接补充 / 修改创作设定草稿（不调用模型，供界面表单编辑）"""
    data = request.json or {}
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    patch = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    if data.get("replace"):
        history.setdefault("drafts", {})[project] = ai_chat.normalize_settings(patch)
    else:
        ai_chat.set_draft(history, project, ai_chat.merge_settings(
            ai_chat.get_draft(history, project), patch))
    history["active_project"] = project
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
    return jsonify({"success": True, "state": _chat_state(project)})


@bp.route('/api/ai/chat/settings', methods=['GET'])
def api_ai_chat_settings():
    """当前已生效的创作设定（供界面查看，也供剧本 / 提示词 / 分镜链路引用）"""
    project = (request.args.get("project") or "").strip()
    return jsonify({"success": True, "settings": ai_chat.settings_view(AI_SETTINGS_PATH, project),
                    "settings_file": os.path.abspath(AI_SETTINGS_PATH)})


@bp.route('/api/ai/settings', methods=['GET', 'POST'])
def api_ai_settings():
    """统一读取/保存创作设定（简版）"""
    project = (request.args.get("project") or "").strip()

    if request.method == 'POST':
        data = request.json or {}
        message = "设置已保存"

        # 「LLM 引擎」= 「文本分析模型」——同一个模块、同一份配置。
        # 后端自己也标注了：/api/llm/config 返回的 `deprecated` 字段写着
        # 「等价于 /api/ai/config 的 text 模块」。
        #
        # 旧实现在这里调 `load_llm_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)`，
        # 但这个别名来自 **llm_client**（只需 1 个 path 参数），却被按 ai_config 的
        # 2 参签名调用 → TypeError → 整个「保存系统设置」按钮必然 500
        # （实测报错：load_config() takes 1 positional argument but 2 were given）。
        # 现在统一落到 text 模块，不再往 llm_config 形态的扁平键里写死配置。
        if 'llm_api_key' in data or 'llm_provider' in data:
            cur = ai_config.get_module(ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH), "text")
            key = str(data.get('llm_api_key') or '').strip()
            if key and '*' not in key and cur.get("base_url") and cur.get("model"):
                ai_config.save_module(AI_CONFIG_PATH, "text",
                                      base_url=cur["base_url"], model=cur["model"],
                                      api_key=key, legacy_path=LLM_CONFIG_PATH)
                message = "LLM 引擎密钥已更新（与「文本分析模型」是同一份配置）"
            else:
                message = ("「LLM 引擎」就是「文本分析模型」，"
                           "请在上方「文本分析模型」卡片里填写 base_url / model / api_key")

        # ComfyUI 地址：全项目只有这里写、没有任何地方读（各 ComfyUI 客户端都直接取
        # 模块级常量 COMFYUI_URL，来源是环境变量）。所以旧实现只是「假装保存成功」。
        # 这里如实告知，避免用户以为改了地址就生效。
        if 'comfyui_url' in data:
            message = (f"ComfyUI 地址由环境变量 COMFYUI_URL 决定，当前为 {COMFYUI_URL}；"
                       "如需修改请改环境变量后重启服务")

        # Save watermark config if provided
        if 'watermark_enabled' in data or 'watermark_text' in data:
            wm_cfg = _wm_load_cfg()
            if 'watermark_enabled' in data:
                wm_cfg['enabled'] = data['watermark_enabled']
            if 'watermark_text' in data:
                wm_cfg['text'] = data['watermark_text']
            video_watermark.save_config(WATERMARK_CONFIG_PATH, wm_cfg)
            message = "水印设置已保存"

        return jsonify({"success": True, "message": message})

    view = ai_chat.settings_view(AI_SETTINGS_PATH, project)
    return jsonify({"success": True, "project_name": view.get("project_name"),
                    "active": view.get("active"), "settings": view.get("settings"),
                    "style_brief": view.get("style_brief"),
                    "settings_file": view.get("settings_file")})


@bp.route('/api/export/<project_name>', methods=['POST'])
@_autopilot_guard
def api_export_project(project_name):
    """导出项目为多种格式"""
    data = request.get_json(silent=True) or {}
    formats = data.get('formats', ['fcpml', 'edl', 'json'])
    # A-01（F-01）：项目名统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project_name")
    if err is not None:
        return err
    blockers = _release_gate_for(project_name, data.get('episode_no'))
    if blockers:
        return jsonify({"success": False, "error": "导出发布门禁未通过",
                        "blockers": blockers}), 409
    # 前端不传 timeline（工作台 ExportTab 就只传 formats）。此处必须自己从剧本
    # 构建时间轴，否则会导出成空的占位文件。
    timeline = _timeline_for_export(project_name, data.get('episode_no'),
                                    data.get('timeline'))

    em = ExportManager(project_name, PROJECT_OUTPUT_DIR)
    exports = em.export_all(timeline)
    # 交付包扫描的是 nle_export.EXPORT_DIR（output/export/<project>/），
    # 而 ExportManager 历史上写 output/exports/<project>/。把本轮产物同步一份到
    # 交付扫描根目录，避免“导出成功但交付包永远看不到这些文件”。
    try:
        delivery_root = os.path.join(nle_export.EXPORT_DIR, project_name)
        os.makedirs(delivery_root, exist_ok=True)
        for src in exports.values():
            dst = os.path.join(delivery_root, os.path.basename(src))
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("导出产物同步到交付目录失败（不阻断导出）：%s", exc)


    # 转换为前端期望的格式
    result_files = []
    for fmt in formats:
        if fmt in exports:
            filepath = exports[fmt]
            exists = os.path.isfile(filepath)
            filename = os.path.basename(filepath) if exists else f"{project_name}_{fmt}.xml"
            result_files.append({
                "format": fmt,
                "filename": filename,
                "exists": exists,
                "path": filepath
            })

    return jsonify({
        "success": True,
        "files": result_files,
        "shot_count": len(timeline.get("shots") or timeline.get("sequences", [{}])[0].get("clips", [])),
        "total_sec": timeline.get("duration", 0),
    })


@bp.route('/api/export/<project_name>/<format>', methods=['GET'])
@_autopilot_guard
def api_get_export_file(project_name, format):
    """获取导出的文件"""
    # A-01（F-01）：项目名统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project_name")
    if err is not None:
        return err
    blockers = _release_gate_for(project_name)
    if blockers:
        return jsonify({"success": False, "error": "导出发布门禁未通过",
                        "blockers": blockers}), 409
    export_dir = os.path.join(PROJECT_OUTPUT_DIR, "exports", project_name)
    if format == 'fcpml':
        filename = f"{project_name}_fcpml.xml"
    elif format == 'edl':
        filename = f"{project_name}_edl.edl"
    elif format == 'json':
        filename = f"{project_name}_timeline.json"
    else:
        return jsonify({"success": False, "error": "不支持的格式"}), 400

    blockers.extend(_delivery_release_gate_for(project_name, filename))
    if blockers:
        return jsonify({"success": False, "error": "导出发布门禁未通过",
                        "blockers": blockers}), 409
    filepath = os.path.join(export_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"success": False, "error": "文件不存在"}), 404

    return send_file(filepath, as_attachment=True)


@bp.route('/api/export/current', methods=['POST'])
@_autopilot_guard
def api_export_current():
    """导出当前项目的所有格式"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project_name', '')
    formats = data.get('formats', ['fcpml', 'edl', 'json'])

    # A-01（F-01）：项目名统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project_name")
    if err is not None:
        return err
    blockers = _release_gate_for(project_name, data.get('episode_no'))
    if blockers:
        return jsonify({"success": False, "error": "导出发布门禁未通过",
                        "blockers": blockers}), 409

    export_dir = os.path.join(PROJECT_OUTPUT_DIR, "exports", project_name)
    os.makedirs(export_dir, exist_ok=True)

    result = {}
    for fmt in formats:
        if fmt == 'fcpml':
            filename = f"{project_name}_fcpml.xml"
        elif fmt == 'edl':
            filename = f"{project_name}_edl.edl"
        elif fmt == 'json':
            filename = f"{project_name}_timeline.json"
        else:
            continue
        filepath = os.path.join(export_dir, filename)
        if os.path.exists(filepath):
            result[fmt] = filepath

    return jsonify({"success": True, "files": result})


@bp.route('/api/memory/lessons', methods=['GET'])
@_autopilot_guard
def api_memory_lessons():
    """质检教训库（generation 链路自动学习成果）

    query: kind（逗号分隔多值）/ project / since / until(ISO，只到日期按当天末闭区间) /
           q（关键词）/ limit（默认 50）/ offset（默认 0）/ prune_empty=1（顺手清理空记录）
    → {success, pruned, total, filtered, offset, limit, by_kind, dead_lessons, lessons[]}
    """
    kind = str(request.args.get('kind') or '')
    project = str(request.args.get('project') or '').strip()
    since = str(request.args.get('since') or '').strip()
    until = str(request.args.get('until') or '').strip()
    q = str(request.args.get('q') or '').strip()
    try:
        limit = max(0, min(500, int(request.args.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    removed = 0
    if str(request.args.get('prune_empty') or '') in ('1', 'true', 'yes'):
        try:
            removed = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).prune_empty()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("清理空教训失败：%s", e)
    try:
        page = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).query(
            kind=kind, project=project, since=since, until=until, q=q,
            limit=limit, offset=offset)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("读取质检教训库失败：%s", e)
        return jsonify({"success": False, "pruned": removed, "total": 0, "filtered": 0,
                        "offset": offset, "limit": limit, "by_kind": {}, "dead_lessons": 0,
                        "lessons": [], "error": str(e)})
    return jsonify({
        "success": True, "pruned": removed,
        "total": page["total"], "filtered": page["filtered"],
        "offset": offset, "limit": limit,
        "by_kind": page["by_kind"], "dead_lessons": page["dead_lessons"],
        "lessons": page["items"],
    })


@bp.route('/api/memory/lessons/<lesson_id>', methods=['DELETE'])
@_autopilot_guard
def api_memory_lesson_delete(lesson_id):
    """删除单条教训（按确定性主键 lesson_id，"L"+sha1 前 16 位）。

    → {success, deleted, lesson_id}；未命中返回 404 {"success":false,"error":"未找到该教训"}。
    """
    lid = str(lesson_id or "").strip()
    if not lid:
        return jsonify({"success": False, "error": "缺少 lesson_id"}), 400
    try:
        ok = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).delete(lid)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("删除教训失败：%s", e)
        return jsonify({"success": False, "error": str(e)}), 500
    if not ok:
        return jsonify({"success": False, "deleted": 0, "lesson_id": lid,
                        "error": "未找到该教训"}), 404
    return jsonify({"success": True, "deleted": 1, "lesson_id": lid})


@bp.route('/api/memory/lessons/clear', methods=['POST'])
@_autopilot_guard
def api_memory_lessons_clear():
    """清空某一环节的全部教训（body {kind}；kind 为空 = 清空全部）。

    → {success, cleared, kind}。
    """
    data = request.json or {}
    kind = str(data.get("kind") or "").strip()
    try:
        cleared = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).clear(kind)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("清空教训失败：%s", e)
        return jsonify({"success": False, "cleared": 0, "kind": kind,
                        "error": str(e)}), 500
    return jsonify({"success": True, "cleared": cleared, "kind": kind})


@bp.route('/api/memory/lessons/search', methods=['GET'])
@_autopilot_guard
def api_memory_lessons_search():
    """按提示词召回教训建议（可视化「如果现在生成，会带上哪些历史修正」）

    query: kind（默认 storyboard）/ prompt（必填）/ project / style
    """
    kind = str(request.args.get('kind') or 'storyboard')
    prompt = str(request.args.get('prompt') or '').strip()
    project = str(request.args.get('project') or '').strip()
    style = str(request.args.get('style') or '').strip()
    if not prompt:
        return jsonify({"success": False, "error": "缺少 prompt 参数"}), 400
    try:
        hints = prompt_memory.suggest(kind=kind, prompt=prompt, project=project,
                                      root_dir=PROJECT_OUTPUT_DIR, style=style)
        learned = prompt_memory.learned_prompt(kind=kind, prompt=prompt, project=project,
                                               root_dir=PROJECT_OUTPUT_DIR, style=style)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"召回失败：{e}"}), 500
    return jsonify({"success": True, "kind": kind, "project": project, "style": style,
                    "hints": hints, "learned_prompt": learned,
                    "changed": learned != prompt})


@bp.route('/api/memory/stats', methods=['GET'])
@_autopilot_guard
def api_memory_stats():
    """获取 AI 记忆统计（含**真实质检教训库**的条数，不再只报手动登记的几条）"""
    mem = get_memory_system()
    stats = mem.get_stats()
    trends = mem.evolution.analyze_trends()
    insights = mem.evolution.generate_insights(limit=5)
    lessons = _prompt_memory_view(limit=0)
    # 让前端「经验条数」反映真实学习成果，而不是 3 条测试数据
    stats = dict(stats or {})
    stats["prompt_lessons"] = lessons["total"]
    stats["prompt_lessons_by_kind"] = lessons["by_kind"]
    return jsonify({
        "success": True,
        "stats": stats,
        "trends": trends,
        "insights": insights,
        "lessons": {
            "total": lessons["total"],
            "by_kind": lessons["by_kind"],
            "dead_lessons": _prompt_memory_dead_count(),
            "used_total": _prompt_memory_used_total(),
        },
    })


@bp.route('/api/memory/list', methods=['GET'])
@_autopilot_guard
def api_memory_list():
    """列出记忆"""
    mem_type = request.args.get('type')
    limit = int(request.args.get('limit', 50))
    mem = get_memory_system()
    entries = mem.list_all(mem_type=mem_type, limit=limit)
    return jsonify({
        "success": True,
        "memories": [e.to_dict() for e in entries],
        "total": len(entries),
    })


@bp.route('/api/memory/search', methods=['GET'])
@_autopilot_guard
def api_memory_search():
    """搜索记忆"""
    query = request.args.get('query', '')
    mem_type = request.args.get('type')
    limit = int(request.args.get('limit', 10))
    if not query:
        return jsonify({"success": False, "error": "缺少 query 参数"}), 400
    mem = get_memory_system()
    results = mem.search_by_pattern(query, mem_type=mem_type, limit=limit)
    return jsonify({
        "success": True,
        "memories": [e.to_dict() for e in results],
        "total": len(results),
    })


@bp.route('/api/memory/record', methods=['POST'])
@_autopilot_guard
def api_memory_record():
    """记录记忆"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    mem_type = data.get('type', 'insight')
    content = data.get('content', '')
    context = data.get('context', {})
    confidence = data.get('confidence', 1.0)
    tags = data.get('tags', [])
    source = data.get('source', 'manual')

    if not content:
        return jsonify({"success": False, "error": "缺少 content"}), 400

    mem = get_memory_system()
    entry = mem.add_memory(
        mem_type=mem_type,
        content=content,
        context=context,
        confidence=confidence,
        tags=tags,
        source=source,
    )
    return jsonify({"success": True, "memory": entry.to_dict()})


@bp.route('/api/memory/record-lesson', methods=['POST'])
@_autopilot_guard
def api_memory_record_lesson():
    """记录质检教训"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    project = data.get('project', '')
    episode = data.get('episode', 0)
    prompt = data.get('prompt', '')
    issues = data.get('issues', [])
    category = data.get('category', 'quality')

    if not project or not issues:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400

    mem = get_memory_system()
    entry = mem.record_lesson(
        project=project,
        episode=episode,
        prompt=prompt,
        issues=issues,
        category=category,
    )
    return jsonify({"success": True, "memory": entry.to_dict()})


@bp.route('/api/memory/record-success', methods=['POST'])
@_autopilot_guard
def api_memory_record_success():
    """记录成功经验"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    project = data.get('project', '')
    episode = data.get('episode', 0)
    prompt = data.get('prompt', '')
    highlights = data.get('highlights', [])
    category = data.get('category', 'quality')

    if not project or not highlights:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400

    mem = get_memory_system()
    entry = mem.record_success(
        project=project,
        episode=episode,
        prompt=prompt,
        highlights=highlights,
        category=category,
    )
    return jsonify({"success": True, "memory": entry.to_dict()})


@bp.route('/api/memory/optimize-prompt', methods=['POST'])
@_autopilot_guard
def api_memory_optimize_prompt():
    """基于记忆优化提示词"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    base_prompt = data.get('prompt', '')
    issues = data.get('issues', [])

    if not base_prompt:
        return jsonify({"success": False, "error": "缺少 prompt"}), 400

    mem = get_memory_system()
    optimized = mem.evolution.auto_optimize_prompt(base_prompt, issues)
    return jsonify({
        "success": True,
        "original": base_prompt,
        "optimized": optimized,
        "improvements": len(issues),
    })


@bp.route('/api/memory/insights', methods=['GET'])
@_autopilot_guard
def api_memory_insights():
    """获取 AI 洞察和建议"""
    limit = int(request.args.get('limit', 5))
    mem = get_memory_system()
    insights = mem.evolution.generate_insights(limit=limit)
    return jsonify({
        "success": True,
        "insights": insights,
        "total": len(insights),
    })


@bp.route('/api/memory/clear-old', methods=['POST'])
@_autopilot_guard
def api_memory_clear_old():
    """清理过期记忆"""
    data = request.get_json(silent=True) or {}
    days = int(data.get('days', 90))
    mem = get_memory_system()
    cleared = mem.clear_old(days=days)
    return jsonify({
        "success": True,
        "cleared": cleared,
        "days": days,
    })


@bp.route('/api/memory/export', methods=['GET'])
@_autopilot_guard
def api_memory_export():
    """导出记忆数据"""
    mem_type = request.args.get('type')
    mem = get_memory_system()
    entries = mem.list_all(mem_type=mem_type) if mem_type else mem.list_all()
    return jsonify({
        "success": True,
        "memories": [e.to_dict() for e in entries],
        "total": len(entries),
        "exported_at": datetime.now().isoformat(),
    })


@bp.route('/api/memory/trends', methods=['GET'])
@_autopilot_guard
def api_memory_trends():
    """获取趋势分析"""
    mem = get_memory_system()
    trends = mem.evolution.analyze_trends()
    return jsonify({
        "success": True,
        "trends": trends,
    })

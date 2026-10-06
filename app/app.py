# -*- coding: utf-8 -*-
"""漫剧生成系统 - Flask Web 应用（composition root）

2026-10-07 拆分：原先 18,613 行 / 242 条路由的单文件已按域搬进 ``app/api/``：
``projects`` / ``novels`` / ``script`` / ``assets`` / ``storyboard`` / ``video`` /
``audio`` / ``qc`` / ``agent`` / ``ops`` 十个蓝图，外加 ``app/api/_shared.py``
（模块级状态与 212 个共用 helper）。**URL 逐字不变**（前端全靠字符串拼 URL），
由 ``scripts/snapshot_routes.py`` 守门。

本文件只负责四件事：创建 Flask 实例、注册蓝图、错误处理兜底、SPA 回退。
Flask 实例必须留在这里创建 —— ``root_path`` / ``template_folder`` 取决于本文件
所在目录（``app/``），挪进 ``app/api/`` 会让 ``render_template`` 与静态目录定位
全部失效。

图片资产三类：角色（多视图）/ 物品（3D多视角）/ 场景（3D多视角）
"""
from __future__ import annotations

import os

from flask import Flask, abort, jsonify, send_from_directory
from werkzeug.exceptions import BadRequest

import agent_core

from api import register_blueprints
from api import _shared as _shared_runtime
from api._shared import APP_HOST, APP_PORT, APP_DEBUG

app = Flask(__name__)
# 总控 AI 自主执行内核：注入 Flask 实例，工具调用走进程内直连（不走网络/不绑端口）
agent_core.bind_app(app)
# ⚠️ 审计 P1-6（2026-09-29）：不再启用全局 CORS。
# 旧实现 `CORS(app)` 等价于 Access-Control-Allow-Origin: * —— 任意网页都能在用户
# 浏览器里跨源 fetch 本服务（包括 GET /api/ai/config/reveal 明文回显密钥、全部写操作路由），
# 属「drive-by 偷密钥」面。而本应用三端全部同源：Web 前端由本服务直接托管（app/static）、
# Electron 桌面壳 loadURL('http://127.0.0.1:<port>/')、Vite 开发期走 server.proxy ——
# 没有任何跨源调用方，全局 CORS 纯属攻击面。如未来确需跨源，必须按路由白名单收紧。


# 共享运行时：先把 Flask 实例注入 `_shared`，再注册蓝图。
# 顺序不能反 —— 域模块在 import 时执行 `from api._shared import *`，
# 那时 `_shared.app` 必须已经是真实实例，否则各域拿到的是 None。
_shared_runtime.bind_app(app)
register_blueprints(app)


# ===== 页面（Vite SPA）=====
def _resolve_static_dir():
    """定位前端静态目录（app/static），兼容源码运行与 PyInstaller 单文件打包。

    2026-09-30 修复「桌面版页面出不来（GET / 返回 404）」：
    打包后模块以**裸名 app** 从 PYZ 导入（serve.py 的 app.app 回退路径），
    `__file__` = <_MEIPASS>/app.pyc → 旧写法得到 <_MEIPASS>/static，
    而 spec 的 datas 实际把前端放在 <_MEIPASS>/app/static → 找不到 index.html。
    现按「打包布局优先、源码布局兜底」依次探测。
    """
    import sys as _sys
    _cands = []
    if getattr(_sys, "frozen", False):
        _mp = getattr(_sys, "_MEIPASS", "") or ""
        if _mp:
            _cands.append(os.path.join(_mp, "app", "static"))
            _cands.append(os.path.join(_mp, "static"))
    _cands.append(os.path.join(os.path.dirname(__file__), "static"))
    for _c in _cands:
        if os.path.isdir(_c):
            return _c
    return _cands[-1]

_STATIC_DIR = _resolve_static_dir()

@app.route('/')
def index():
    return send_from_directory(_STATIC_DIR, 'index.html')

@app.route('/assets/<path:filename>')
def static_assets(filename):
    """服务 Vite 构建的静态资源"""
    return send_from_directory(os.path.join(_STATIC_DIR, 'assets'), filename)

@app.route('/vite.svg')
def vite_icon():
    """Vite favicon 回退（旧版本 index.html 仍可能引用，保留向后兼容）"""
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">⚡</text></svg>'

@app.route('/favicon.svg')
def favicon_svg():
    """站点图标：由 Vite 从 frontend/public/favicon.svg 复制到 static 根目录"""
    return send_from_directory(_STATIC_DIR, 'favicon.svg')

@app.errorhandler(BadRequest)
def _handle_bad_request(e):
    """请求体无法解析（非合法 JSON / Content-Type 不匹配）→ 400。

    没有这个兜底时，未被 _autopilot_guard 包裹的路由会直接把 werkzeug 的
    400 渲染成 HTML 错误页，前端拿到一坨 HTML 而无法解析成 JSON。
    """
    return jsonify({
        "success": False,
        "error": "请求体格式不正确：需要合法的 JSON（并带上 Content-Type: application/json）",
    }), 400

# ===== SPA 路由（必须在所有 API 路由之后，Flask 默认静态路由之前）=====
# 显式注册每个 SPA 页面，避免与 Flask 默认 /static 路由冲突
_SPA_PAGES = ['projects', 'upload', 'auto', 'memory', 'characters', 'grid', 'deliver', 'export', 'settings']

for _page in _SPA_PAGES:
    _endpoint = f'spa_{_page}'
    def _make_spa_page(_p=_page, _ep=_endpoint):
        @app.route(f'/{_p}', endpoint=_ep)
        def _spa_page():
            return send_from_directory(_STATIC_DIR, 'index.html')
        return _spa_page
    _make_spa_page()

# 通用回退：任意未匹配路径返回 index.html（用于 SPA 客户端路由）
@app.route('/<path:path>')
def spa_fallback(path):
    """SPA 路由回退：非 API、非静态文件请求返回 Vite index.html"""
    # 放行 API 前缀
    if path.startswith('api/'):
        abort(404)
    # 若 static 目录下确实存在该文件（favicon.svg / robots.txt 等），直接返回真实文件，
    # 避免被下面的「带扩展名一律 404」误伤。
    # 用 normpath + 前缀校验防目录穿越；send_from_directory 自身也会做安全校验。
    safe = os.path.normpath(os.path.join(_STATIC_DIR, path))
    if safe.startswith(os.path.abspath(_STATIC_DIR)) and os.path.isfile(safe):
        return send_from_directory(_STATIC_DIR, path)
    # 放行带扩展名的静态资源
    if '.' in path.split('/')[-1]:
        abort(404)
    return send_from_directory(_STATIC_DIR, 'index.html')

if __name__ == '__main__':
    # P2-T4（F-02）：直跑分支也走同一套回环护栏（与 serve.main 共用 host_guard）。
    # 此前 `python app/app.py` 直跑完全绕过 serve.py 护栏 —— APP_HOST=0.0.0.0 时
    # 零鉴权的 debug 模式对同网段全裸暴露。现：非回环且未设 MJSCXT_ALLOW_NON_LOOPBACK
    # 时拒绝启动（fail-closed），与 serve.py 口径一致。
    from host_guard import _guard_host
    try:
        _guard_host(APP_HOST)
    except RuntimeError as e:
        app.logger.error("启动被安全护栏拦截（F-02 直跑分支）：%s", e)
        raise SystemExit(2)
    app.logger.info(f"漫剧生成系统启动: http://{APP_HOST}:{APP_PORT} (debug={APP_DEBUG})")
    app.run(host=APP_HOST, port=APP_PORT, debug=APP_DEBUG, threaded=True)

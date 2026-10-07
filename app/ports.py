"""端口唯一事实源（2026-10-07）。

为什么需要这个模块
------------------
改端口这件事以前散落在 6 个文件里，各写各的默认值，而且**已经漂移了**：

| 位置                          | 旧默认值 | 谁在读                                  |
|-------------------------------|----------|-----------------------------------------|
| `app/serve.py` `_safe_run`    | 5210     | 日常入口（run_app.bat / 浏览器版）      |
| `app/api/_shared.py` `APP_PORT` | 5000   | `python app/app.py` 直跑分支            |
| `main.py` `run_web_mode`      | 5000     | 打包 exe 的 web 入口                     |
| `electron-app/main.js` `DEFAULT_PORT` | 5211 | 桌面版                |
| `deploy/desktop/main.js` `FLASK_RUN_PORT` | 5000 | 旧桌面壳    |
| `docker-compose.yml`          | 5000     | 容器版                                      |

后果是同一种启动方式换个入口就落到不同端口，`ServiceMonitor` 探活、`/api` 代理、
Electron 复用检测（`isPortListening(DEFAULT_PORT)`）会各自指向不同的进程，出现
「A 入口起的服务，B 入口连不上」这类难查的问题。本模块把默认值收敛到一处，
上面 6 处全部改为引用它 —— 以后再改端口只改这里一行。

命名约定
--------
- `BACKEND_PORT`：后端 Flask。它同时托管 `app/static` 里的前端产物，
  所以**生产模式下没有独立前端端口**，浏览器直接开这个端口就是完整应用。
- `FRONTEND_PORT`：仅 `frontend/` 里 `pnpm dev` 的 vite dev server 用。
  它把 `/api` 代理到 `BACKEND_PORT`，因此两者必须成对改动。

覆盖方式
--------
两个值都能用环境变量覆盖，且**运行时读、不是导入时冻结**：

    APP_PORT=12345 FRONTEND_PORT=12346 python app/serve.py

之所以强调运行时读：`electron-app/main.js` 会在 5211 被占用时自动往后找一个
空闲端口（`findFreePort`），再把结果写进子进程的 `APP_PORT` 环境变量。
若端口在模块导入时被冻结成常量，Electron 的自动避让就会失效。
"""

import os

# 后端 Flask / WSGI 端口。
# 为什么不用 5000 / 8000：这两个是 flask run 与 uvicorn 的默认，任何一台装了
# 开发环境的机器上都可能被别的进程占着；8080/3000 则是各类本地 Web 中间件与
# 前端脚手架的常客。选高位端口可以显著降低「本机撞车」的概率。
# 注意必须 < 49152（IANA 动态/私有端口区下界），否则会和操作系统临时分配的
# 客户端端口抢区间，出现偶发的 bind 失败。
BACKEND_PORT = 45871

# 前端 vite dev server 端口。仅开发期使用；生产由后端同端口托管前端产物。
FRONTEND_PORT = 47311

# Electron 桌面版后端端口。⚠️ 必须 ≠ BACKEND_PORT。
# 桌面版与浏览器版是两个独立 Flask 实例却共享数据根（output/ novels/）。若共用
# 端口，桌面版的 resolvePort() 会判定「端口上已有健康后端」而直接复用，于是两个
# 实例同时读写同一份数据 → 项目数据串档。2026-10-07 改端口时一度把两边都设成
# 45871，正是这个回归，由本注释固化该约束。
# 单一事实源在 JS 侧（Electron 主进程读不到 Python 模块）：改这里必须同步
# electron-app/main.js 的 DEFAULT_PORT，两处注释互为提醒。
DESKTOP_PORT = 45872


def _env_int(key: str, default: int) -> int:
    """读环境变量为 int，缺失/非法时回退默认值。

    与 `serve._env_int` 同语义，但放在这里是为了让 `ports` 不依赖 `serve`
    —— `serve.py` 顶层会 exec 整份 `app.py`，反向 import 会触发 Flask 路由
    重复注册（详见 serve.py:290-297 的注释）。
    """
    try:
        return int((os.getenv(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def backend_port() -> int:
    """后端监听端口。`APP_PORT` 优先，否则 `BACKEND_PORT`。"""
    return _env_int("APP_PORT", BACKEND_PORT)


def frontend_port() -> int:
    """前端 vite dev server 端口。`FRONTEND_PORT` 优先，否则 `FRONTEND_PORT`。"""
    return _env_int("FRONTEND_PORT", FRONTEND_PORT)
# electron-app · 漫剧工坊 桌面版（Electron 壳）

浏览器版（`app/` + `app/static/`）**保持不动**；本目录是独立的 Electron 外壳，
UI 唯一事实源 = 运行中的 Flask 服务（`loadURL http://127.0.0.1:<port>`），不复制静态文件。

## 架构

```
Electron 主进程 (main.js)
 ├─ 单实例锁 (requestSingleInstanceLock)
 ├─ 端口决策：5000 被占用且 GET / == 200 → 复用浏览器版实例（不 spawn）
 │            否则找 5001+ 空闲端口
 ├─ 后端布局分流 (resolveBackendLayout)
 │   打包态：spawn 自带 _backend/python/python.exe（嵌入式）
 │          serve.py 从「资源镜像」userData/resource-mirror 读取（可写，增量更新覆盖它）
 │          可写数据区 → MJSCXT_DATA_DIR=userData（Program Files 只读）
 │   开发态：spawn 本机 venv，serve.py 读项目源码，数据落源根
 ├─ spawn  python <abs>/app/serve.py   （绝对路径；项目目录含中文）
 │         env: APP_HOST=127.0.0.1 / APP_PORT / PYTHONUNBUFFERED
 │         （COMFYUI_* / MJSCXT_* 不手动转发 —— 应用 env_loader 自动读 .env）
 ├─ 就绪轮询：GET / 每 500ms，60s 超时（不用 /api/status —— 它会探 ComfyUI）
 ├─ 日志环形缓冲（500 行）→ 菜单「查看日志」/ 超时对话框
 ├─ 更新系统（updater.js + update-config.js，纯 Node 无第三方依赖）
 │   · 资源增量：下载 resources-x.y.z.zip → SHA256SUMS 强校验 → 覆盖资源镜像 → 重启后端生效
 │   · 整包：下载 漫剧工坊-Portable-x.y.z.exe → 校验 → 用户确认后才 spawn 接管 + 自重启
 │   · 触发：启动检查一次 + 每 24h 静默轮询 + 菜单「帮助 → 检查更新」
 │   · 来源：GitHub 公开仓库 release（zdljh/mjscxt），匿名下载
 └─ 优雅停机：SIGINT → 10s → taskkill /PID <pid> /T /F 兜底
```

## 硬约束（别改回去）

1. **必须** `spawn(python, [<abs>/app/serve.py])` —— **禁止** `python -m app.serve`
   （serve.py D1 缺陷：`-m` 方式加载错误模块，全部路由 500）。
2. **绝对路径**引用 `serve.py`（项目目录 `...\漫剧生成系统` 含中文，相对路径 + CWD 有编码歧义风险）。
3. 就绪判定只用 `GET /` 返回 200；ComfyUI 离线不是启动门槛（前端自带离线状态展示）。
4. 复用 5000 上的既有 Flask 实例时**不杀别人进程**（浏览器版/桌面版可并存）。

## 配置（userData/backend.json）

`{ projectRoot, pythonExe, port }`。pythonExe 默认走
`C:\Users\liujianghua\.workbuddy\binaries\python\envs\mjscxt\Scripts\python.exe`，
找不到则 `py -0` 枚举；项目根可经菜单/IPC 选择（校验 `app/serve.py` 存在）。

## 开发 / 打包

```bat
cd electron-app
npm install            :: 拉 electron + electron-builder（Node 18+）
npm start              :: 开发运行（自动起/复用本机 venv 后端，不设数据目录重定向）
node pack_backend.js   :: 收集 _backend/{app,workflows,locales,main.py}（源码 + 只读资源）
npm run build:win      :: electron-builder 打 NSIS + Portable（产物名 漫剧工坊-Setup/Portable-<v>.exe）
```

**独立 exe（前后端自包含，零 Python 安装）**：
- `extraResources` 把 `_backend/{python,app,workflows,locales}` 打进安装包（只读安装区）；
- `pack_backend.js` 只收集源码，**Python 运行时需另行烘焙**到 `_backend/python/`
  （embeddable 3.13 + get-pip + `app/requirements.txt`，详见 `pack_backend.js` 顶部说明）；
- 可写数据（`output/` / `novels/` / `*.config.json` / `.secret_key` / `secrets.enc`）
  经 `MJSCXT_DATA_DIR=userData` 重定向，安装目录保持只读。

**自动更新**（`updater.js` + `update-config.js`，见上文架构块）：
资源增量 / 整包两条通道，SHA256SUMS 强校验，GitHub 公开仓库匿名下载。
发版走根目录 `.github/workflows/desktop-release.yml`（打 tag 触发 → 烘焙 Python + 打便携包 + 资源包 + 清单 → 发 Release）。

打包只含 `main.js` + `preload.js`（package.json `files` 白名单）——
**不把项目文件 / .env / 密钥打进安装包**；安装目录与项目目录分离，项目路径写在 userData 配置里。

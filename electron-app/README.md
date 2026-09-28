# electron-app · 漫剧工坊 桌面版（Electron 壳）

浏览器版（`app/` + `app/static/`）**保持不动**；本目录是独立的 Electron 外壳，
UI 唯一事实源 = 运行中的 Flask 服务（`loadURL http://127.0.0.1:<port>`），不复制静态文件。

## 架构

```
Electron 主进程 (main.js)
 ├─ 单实例锁 (requestSingleInstanceLock)
 ├─ 端口决策：5000 被占用且 GET / == 200 → 复用浏览器版实例（不 spawn）
 │            否则找 5001+ 空闲端口
 ├─ spawn  python <abs>/app/serve.py   （绝对路径；项目目录含中文）
 │         env: APP_HOST=127.0.0.1 / APP_PORT / PYTHONUNBUFFERED
 │         （COMFYUI_* / MJSCXT_* 不手动转发 —— 应用 env_loader 自动读 .env）
 ├─ 就绪轮询：GET / 每 500ms，60s 超时（不用 /api/status —— 它会探 ComfyUI）
 ├─ 日志环形缓冲（500 行）→ 菜单「查看日志」/ 超时对话框
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
npm start              :: 开发运行（自动起/复用后端）
npm run build:win      :: 产出 dist-electron/漫剧工坊 Setup 1.0.0.exe (NSIS) + 便携单文件
```

打包只含 `main.js` + `preload.js`（package.json `files` 白名单）——
**不把项目文件 / .env / 密钥打进安装包**；安装目录与项目目录分离，项目路径写在 userData 配置里。

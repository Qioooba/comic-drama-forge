# 漫剧工坊 - 桌面应用部署指南

## ⚠️ 重要：桌面版 EXE 不要放在 WorkBuddy 工作区目录里运行

**实测结论（2026-10-05 对照实验）**：Electron 打包产物只要位于
`C:\Users\liujianghua\WorkBuddy\...` 之内，启动就会**瞬间静默死亡**——
无窗口、无提示、`desktop.log` 一行都不写（死点在 JS 执行之前）。

崩溃指纹（想自己确认时按这个找）：
- 退出码 `-2147483645`（= `0x80000003` STATUS_BREAKPOINT）
- stderr 出现 `Received fatal exception EXCEPTION_BREAKPOINT`，栈内含 `IsSandboxedProcess`
- 加 `--no-sandbox` 不再崩，但接着报 `process_singleton_win.cc(465) Lock file can not be created! Error code: 5`
- `%APPDATA%\mjscxt-desktop\desktop.log` **零新增**（关键判别特征）

**正确做法**：把打包产物复制到工作区之外的普通目录再运行，例如
`C:\Users\liujianghua\mjscxt_desktop\desktop-app\`（本机已验证可出窗口）。
同一份字节换个目录就能跑 —— 与路径长度、中文路径均无关。

**另一个假故障**：`comic-drama-forge-Portable-*.exe` 首次启动要**约 65 秒**静默
自解压约 280MB 到 `%TEMP%`，期间"任务管理器里有进程、但没有任何界面"。
这段时间**不要重复双击**（第二次会因单实例锁直接静默退出）。想避免等待，
请用 Setup 安装版，或直接运行解压好的目录版。

## 快速开始

### 方法一：Web模式（推荐）
```bash
cd "C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统"
python app/serve.py
# 访问 http://127.0.0.1:5210  （默认端口；可用环境变量 APP_PORT 覆盖）
```

### 方法二：双击启动
```bash
# Windows用户直接运行（根目录启动脚本，自动找 python 环境并起 app/serve.py）
双击运行 run_app.bat
# 或绿色便携包：packaging/启动.bat（配 packaging/安装依赖.bat 一次性装依赖）
```

### 方法三：Electron桌面应用
```bash
# 安装依赖
cd electron-app
npm install

# 开发模式运行（自动起/复用本机后端）
npm start

# 收集后端资源 + 打包为EXE（前后端自包含，零 Python 安装）
node pack_backend.js
npm run build:win          # ← CI / 有网环境（electron-builder 自行下载 Electron）
npm run build:win:local    # ← 本机离线/受限网络（复用本地已备好的 Electron 目录）
```
> ⚠️ **`build.electronDist` 已从 `package.json` 移除**，改为只在 `build:win:local`
> 里以 `--config.electronDist=<本机 Electron 目录>` 传入。原因：写在 `package.json`
> 里会带上**本机绝对路径**，GitHub Actions runner 上不存在该路径 ⇒ 打包必失败；
> 而本机受限网络下又下不动 Electron，需要一个本机专用入口。**两者不可混用。**
>
> 桌面版支持**自动更新**（资源增量 / 整包，SHA256 校验，GitHub 匿名下载）：
> 启动检查一次 + 每 24h 静默轮询 + 菜单「帮助 → 检查更新」。
> 发版走根目录 `.github/workflows/desktop-release.yml`（打 tag `v*` 自动发 Release）。
> ⚠️ `electron-app/update-config.js` 的 `GITHUB.owner/repo` 必须与实际发 Release 的仓库
> 一致，否则客户端更新会**静默空转**（找不到 release 且不报错）。

## 打包EXE

### PyInstaller方式（推荐）
```bash
pip install pyinstaller
pyinstaller 漫剧工坊.spec
# 输出: dist/漫剧工坊.exe
```

### Electron方式
```bash
cd electron-app
npm run build:win
# 输出: dist-electron/漫剧工坊 Setup 1.0.0.exe
```

## 功能特性

### 新增功能（对比分析报告改进后）
| 功能 | 状态 | 入口 |
|------|------|------|
| 角色四视图管理 | ✅ | 侧边栏「人物」图标 |
| 九宫格分镜 | ✅ | 侧边栏「网格」图标 |
| FCPXML/EDL导出 | ✅ | 侧边栏「导出」图标 |
| Docker部署 | ✅ | docker-compose.yml |
| EXE打包 | ✅ | run_app.bat / packaging/启动.bat |
| 依赖自检（模型+插件） | ✅ | `GET /api/deps/check`（缺什么一目了然） |

### 核心能力
- 🎬 全自动生产流水线
- 🤖 AI对话总控
- ⏰ 24小时无人值守
- 🔍 AI质检自动重试
- 📊 多项目并行管理

## 系统要求

- Windows 10/11
- Python 3.11+
- Node.js 18+ (Electron模式)
- 至少 8GB 内存
- NVIDIA GPU (推荐，用于ComfyUI)

## 依赖安装

```bash
pip install -r requirements.txt
pip install pyinstaller --upgrade
```

## 目录结构

```
漫剧生成系统/
├── app/                      # Flask后端（UI 由 app/static 提供，非 Jinja 模板）
│   ├── serve.py             # WSGI 启动入口（waitress）
│   ├── app.py               # 主应用（路由 + 管线）
│   ├── config.py            # 单点配置（COMFYUI_ROOT 推导各路径）
│   ├── deps_check.py        # 依赖自检（插件节点 + 模型权重）
│   ├── autonomous.py        # 全自动生产
│   └── static/             # 前端构建产物（Vite）
├── electron-app/            # Electron桌面应用
│   ├── main.js              # 主进程（后端生命周期 + 更新系统）
│   ├── preload.js           # contextBridge 暴露面
│   ├── updater.js           # 更新引擎（纯 Node，无第三方依赖）
│   ├── update-config.js     # 更新常量（GitHub 仓库 + 资产命名）
│   ├── pack_backend.js      # 收集后端资源到 _backend/
│   └── package.json
├── packaging/               # 绿色便携打包（make_package.py + 启动.bat + 安装依赖.bat）
├── .github/workflows/desktop-release.yml  # CI：打 tag 自动烘焙 Python + 发 Release
├── docs/依赖清单.md           # 插件/模型 权威清单（deps_check.py 映射来源）
├── novels/                  # 小说库
├── output/                  # 输出目录
├── main.py                  # 启动入口
└── 漫剧工坊.spec             # PyInstaller配置
```

## 上线 / 交付运维清单（凭据与配置安全）

> 以下为**上线前必须逐条确认**的运维事项，属「F-03」上线闸门的一部分。

1. **备份文件不得携带明文 key。**
   禁止把 `qc_config.json` / `ai_config.json` / `llm_config.json` 等含 API key 的配置文件
   以 `*.backup_*`、`*.bak`、`*.old`、`*.orig` 等任何形式复制到仓库或交付包内
   （即便已被 `.gitignore` 忽略，**机器上的副本仍是活凭据**）。
2. **`qc_config.backup_20260914.json` 内的 API key 视为已泄露，需轮换。**
   该文件（位于仓库根目录）曾残留真实明文 key（形如 `sk-vZx9…`）。文件已删除，
   但 key 需在服务商控制台**立即作废并轮换**，并更新当前生效的 `qc_config.json`。
3. **密钥只以密文存在。** 生产配置请通过 `/api/qc/config` 写入（内部经 `secrets.enc`
   加密），不要把明文 key 直接落进仓库文件。
4. **交付/打包前自查：**

   ```bash
   # 列出所有可能含 key 的备份/副本文件（应为空）
   git ls-files | grep -Ei "backup|\.bak|\.old|\.orig" ; \
   ls -a | grep -Ei "backup|\.bak|\.old|\.orig"
   # 全仓库扫描明文 key 指纹（应无命中）
   grep -rEn "sk-[A-Za-z0-9]{8,}" --include=*.json --include=*.env . 2>/dev/null
   ```
5. **监听面保持回环。** 本应用默认 `APP_HOST=127.0.0.1` 且**无鉴权**；`serve.py` 已对
   非回环 host 做启动拦截（见 F-02 护栏）。网络部署前必须先补鉴权。
6. **凭据文件 ACL 收紧至当前用户最小授权（F-04）。**
   主密钥 `.secret_key`、加密密钥库 `output/secrets.enc`、`.env` 三者齐泄即可破整个
   密钥库（Fernet 主密钥泄露后 `secrets.enc` 的全部密文可被离线解出）。默认 Windows
   新建文件常继承宽泛组权限（如 `CodexSandboxUsers`、`SYSTEM`/`Administrators` 全权），
   须收紧到「仅当前交互用户读写、其它主体无权限」。在**管理员 PowerShell** 执行（路径
   换成实际仓库根目录）：

   ```powershell
   # 1) 清空三个凭据文件上的继承权限，只保留当前用户
   $here = "<仓库根目录>"
   foreach ($f in @(".secret_key", ".env", "output\secrets.enc")) {
       $p = Join-Path $here $f
       if (Test-Path $p) {
           # 去掉继承项（仅当前用户保留）
           & icacls $p /inheritance:r | Out-Null
           & icacls $p /grant:r "${env:USERNAME}:(R,W)" | Out-Null
           & icacls $p /remove:g "NT AUTHORITY\SYSTEM" "BUILTIN\Administrators" "CodexSandboxUsers" 2>$null | Out-Null
       }
   }
   # 2) 复检：除当前用户外不应再有 (R)/(F)/(W) 主体
   icacls "$here\.secret_key"; icacls "$here\.env"; icacls "$here\output\secrets.enc"
   ```

   > 注意：`.env`/`.secret_key`/`secrets.enc` 已 `.gitignore`，**本机副本仍是活凭据**，
   > 收紧 ACL 属必要但**非充分**——若曾以宽 ACL 暴露过，应视为已泄露，走第 2 条的
   > 密钥轮换流程。`icacls /inheritance:r` 会移除继承来源，故须在**确认当前用户仍被
   > 显式授权**后执行，避免把自己也锁在外面（执行前先 `icacls <file>` 记录原状）。

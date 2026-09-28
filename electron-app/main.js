'use strict';
// 漫剧工坊 桌面版 — Electron 主进程
// 职责：管理本地 Flask 后端（serve.py）的生命周期，并把 BrowserWindow 指向运行中的
//       Flask URL（http://127.0.0.1:<port>）。UI 的唯一事实源仍是 Flask，不复制 app/static。

const {
  app,
  BrowserWindow,
  Menu,
  dialog,
  ipcMain,
  shell,
} = require('electron');
const { spawn } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

// 默认项目根 = 本 electron-app/ 的上级目录（即 漫剧生成系统/）
const DEFAULT_PROJECT_ROOT = path.resolve(__dirname, '..');
// 已知 venv python（用户机上的默认解释器）
const KNOWN_PYTHON =
  'C:\\Users\\liujianghua\\.workbuddy\\binaries\\python\\envs\\mjscxt\\Scripts\\python.exe';
const DEFAULT_PORT = 5000;

// 环形缓冲：子进程 stdout/stderr 最多保留 500 行
const LOG_RING_MAX = 500;
// 就绪探测：GET / 返回 200 才算就绪（不用 /api/status —— 它会去探 ComfyUI）
const READINESS_INTERVAL_MS = 500;
const READINESS_TIMEOUT_MS = 60000;
// 优雅停机：先 SIGINT，等 10s 还活着就 taskkill 兜底
const GRACEFUL_WAIT_MS = 10000;

// ---------------------------------------------------------------------------
// 配置（存于 Electron userData/backend.json）
// ---------------------------------------------------------------------------

function configPath() {
  return path.join(app.getPath('userData'), 'backend.json');
}

function defaultConfig() {
  return { projectRoot: DEFAULT_PROJECT_ROOT, pythonExe: KNOWN_PYTHON, port: DEFAULT_PORT };
}

function loadConfig() {
  const def = defaultConfig();
  try {
    if (fs.existsSync(configPath())) {
      const raw = JSON.parse(fs.readFileSync(configPath(), 'utf8'));
      return {
        projectRoot: typeof raw.projectRoot === 'string' ? raw.projectRoot : def.projectRoot,
        pythonExe: typeof raw.pythonExe === 'string' ? raw.pythonExe : def.pythonExe,
        port: Number.isFinite(raw.port) ? raw.port : def.port,
      };
    }
  } catch {
    /* 配置损坏 → 用默认 */
  }
  return def;
}

function saveConfig(cfg) {
  try {
    fs.mkdirSync(path.dirname(configPath()), { recursive: true });
    fs.writeFileSync(configPath(), JSON.stringify(cfg, null, 2), 'utf8');
  } catch (e) {
    console.error('保存 backend.json 失败：', e);
  }
}

let config = loadConfig();

// 校验项目根：app/serve.py 必须存在；.env 缺失只是警告（应用有内置默认值）
function validateProjectRoot(root) {
  const warnings = [];
  const servePy = path.join(root, 'app', 'serve.py');
  if (!fs.existsSync(servePy)) {
    throw new Error(`项目根目录无效：找不到 ${servePy}`);
  }
  if (!fs.existsSync(path.join(root, '.env'))) {
    warnings.push('未找到 .env（将使用应用内置默认配置）');
  }
  return warnings;
}

// python 解释器检测顺序：用户配置 → 已知 venv → `py -0` 枚举
async function detectPython() {
  if (config.pythonExe && fs.existsSync(config.pythonExe)) {
    return config.pythonExe;
  }
  if (fs.existsSync(KNOWN_PYTHON)) {
    return KNOWN_PYTHON;
  }
  try {
    const { execFile } = require('node:child_process');
    const out = await new Promise((resolve, reject) => {
      execFile('py', ['-0'], { windowsHide: true }, (err, so, se) =>
        err ? reject(err) : resolve(`${so}\n${se}`)
      );
    });
    // `py -0` 输出形如： -3.12-64 C:\...\python.exe
    const m = out.match(/-\d[\d.]*(?:-\d+)?\s+([^\s]+\s*python[\d._-]*\.exe)/i);
    if (m) return m[1].trim();
  } catch {
    /* py 不可用 */
  }
  throw new Error('未找到可用的 Python 解释器，请在设置中手动指定 python 路径');
}

// ---------------------------------------------------------------------------
// 后端布局：独立 exe vs 开发
// ---------------------------------------------------------------------------
// 打包态（app.isPackaged）= 前后端都自包含：electron-builder 的 extraResources
//   里带一份「嵌入式 Python + app 源码 + workflows + 前端静态」，spawn 它自带的
//   python，并把可写数据目录指向 Electron userData（Program Files 只读）。
// 开发态（npm start）= 前后端分离：用本机 venv 跑项目源码，数据落源根（可写）。
function resolveBackendLayout() {
  if (app.isPackaged) {
    const res = process.resourcesPath; // extraResources 落点（打包态只读，可写数据另指 userData）
    const exeName = process.platform === 'win32' ? 'python.exe' : 'python';
    return {
      pythonExe: path.join(res, 'python', exeName),
      servePy: path.join(res, 'app', 'serve.py'),
      dataDir: app.getPath('userData'), // 可写
      cwd: res,
    };
  }
  // 开发态：沿用 config（项目根 + 本机 venv），不设 MJSCXT_DATA_DIR（源根可写）
  return {
    pythonExe: null, // 交给 detectPython() 解析本机 venv
    servePy: path.join(config.projectRoot, 'app', 'serve.py'),
    dataDir: null,
    cwd: config.projectRoot,
  };
}

// ---------------------------------------------------------------------------
// 日志环形缓冲
// ---------------------------------------------------------------------------

const logLines = [];

function pushLog(stream, text) {
  for (const line of String(text).split(/\r?\n/)) {
    if (line.length > 0) {
      logLines.push(`[${stream}] ${line}`);
      if (logLines.length > LOG_RING_MAX) logLines.shift();
    }
  }
}

function tailLog(n = 100) {
  return logLines.slice(-n);
}

// ---------------------------------------------------------------------------
// HTTP / 端口探测
// ---------------------------------------------------------------------------

// 单次 GET，返回响应状态码；连接失败/超时/非 200 一律返回 null
function httpGetStatus(url, timeoutMs = 2000) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => {
      if (!settled) {
        settled = true;
        resolve(v);
      }
    };
    try {
      const req = http.get(url, (res) => {
        done(res.statusCode === 200 ? 200 : null);
        res.resume();
      });
      req.on('error', () => done(null));
      req.setTimeout(timeoutMs, () => {
        req.destroy();
        done(null);
      });
    } catch {
      done(null);
    }
  });
}

// 端口上是否有监听者（不关心是谁）
function isPortListening(port) {
  return new Promise((resolve) => {
    const sock = net
      .connect({ host: '127.0.0.1', port })
      .on('connect', () => {
        sock.destroy();
        resolve(true);
      })
      .on('error', () => resolve(false));
  });
}

// 找下一个空闲端口（从 5001 起）
async function findFreePort() {
  for (let p = DEFAULT_PORT + 1; p < DEFAULT_PORT + 1000; p += 1) {
    if (!(await isPortListening(p))) return p;
  }
  throw new Error('5001–5999 端口均被占用，无法启动后端');
}

// 端口决策：5000 被占用且 GET / 是 200 → 复用；否则找空闲端口
async function resolvePort() {
  if (await isPortListening(DEFAULT_PORT)) {
    if ((await httpGetStatus(`http://127.0.0.1:${DEFAULT_PORT}/`)) === 200) {
      return { port: DEFAULT_PORT, reuse: true };
    }
    // 被占用但不是我们的 Flask → 找下一个
    const port = await findFreePort();
    return { port, reuse: false };
  }
  return { port: DEFAULT_PORT, reuse: false };
}

// ---------------------------------------------------------------------------
// 后端生命周期
// ---------------------------------------------------------------------------

const backend = {
  child: null,      // 我们 spawn 的 python 子进程；复用实例时为 null
  reused: false,    // 是否复用了 5000 上已有的 Flask（例如浏览器版已在跑）
  port: null,
  starting: false,
  shuttingDown: false,
};

function childAlive() {
  return backend.child != null && !backend.child.killed;
}

// 就绪探测：轮询 GET / 直到 200 或超时
function waitForReady(port) {
  const deadline = Date.now() + READINESS_TIMEOUT_MS;
  return new Promise((resolve) => {
    const tick = async () => {
      if (backend.shuttingDown || !childAlive()) {
        resolve(false);
        return;
      }
      const status = await httpGetStatus(`http://127.0.0.1:${port}/`);
      if (status === 200) {
        resolve(true);
        return;
      }
      if (Date.now() >= deadline) {
        resolve(false);
        return;
      }
      setTimeout(tick, READINESS_INTERVAL_MS);
    };
    tick();
  });
}

// spawn 绝对路径的 serve.py（项目目录含中文，相对路径有 CWD 编码歧义风险）
async function startBackend() {
  if (backend.starting) return backend;
  if (childAlive()) return backend;
  backend.starting = true;
  backend.reused = false;
  try {
    const layout = resolveBackendLayout();
    const warnings = app.isPackaged
      ? []
      : validateProjectRoot(config.projectRoot);
    for (const w of warnings) console.warn(w);

    // 打包态用自带的嵌入式 Python；开发态探测本机 venv
    const python = layout.pythonExe || await detectPython();
    if (!fs.existsSync(layout.servePy)) {
      throw new Error(`找不到后端入口 ${layout.servePy}（请确认已运行 build:win 打包或项目路径正确）`);
    }
    const { port, reuse } = await resolvePort();
    if (reuse) {
      // 已有健康实例（例如浏览器版）占用 → 直接复用，不 spawn
      backend.reused = true;
      backend.port = port;
      backend.starting = false;
      return backend;
    }
    logLines.length = 0;
    backend.port = port;
    const childEnv = {
      ...process.env,
      APP_HOST: '127.0.0.1',
      APP_PORT: String(port),
      PYTHONUNBUFFERED: '1',
      // 不手动转发 COMFYUI_* / MJSCXT_* —— 应用自带 env_loader 会读 .env
    };
    // 可写数据目录：打包态指向 userData（Program Files 只读），开发态不设（源根可写）
    if (layout.dataDir) {
      fs.mkdirSync(layout.dataDir, { recursive: true });
      childEnv.MJSCXT_DATA_DIR = layout.dataDir;
    }
    backend.child = spawn(python, [layout.servePy], {
      cwd: layout.cwd,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: childEnv,
      windowsHide: true,
    });
    backend.child.stdout.on('data', (d) => pushLog('out', d));
    backend.child.stderr.on('data', (d) => pushLog('err', d));
    backend.child.on('error', (e) => pushLog('err', String(e)));
    backend.child.on('exit', (code, signal) => {
      pushLog('sys', `python 进程退出（code=${code}, signal=${signal}）`);
      backend.child = null;
      if (!backend.shuttingDown) {
        backend.starting = false;
        // 只有「serve.py 真正退出」才提示用户；Flask 内部自动重启发生在
        // serve.py 进程内部，子进程本身不会退出，因此不会误报。
        notifyBackendExited(code, signal);
      }
    });
    backend.starting = false;
    const ready = await waitForReady(port);
    if (!ready) {
      // 超时：杀掉这次 spawn 的子进程，把日志尾部交给用户（重试/查看日志）
      if (childAlive()) {
        backend.shuttingDown = true;
        backend.child.kill('SIGINT');
        await new Promise((r) => setTimeout(r, 1500));
        backend.shuttingDown = false;
        forceKillIfAlive();
      }
      await notifyReadinessTimeout(port);
      return backend;
    }
    return backend;
  } catch (e) {
    backend.starting = false;
    pushLog('sys', String(e && e.message ? e.message : e));
    await notifyBackendExited('detect', e.message);
    return backend;
  }
}

async function stopBackend() {
  backend.shuttingDown = true;
  if (backend.reused) {
    // 复用实例不是我们 spawn 的，不杀别人的进程
    backend.reused = false;
    backend.port = null;
    backend.shuttingDown = false;
    return;
  }
  if (childAlive()) {
    const pid = backend.child.pid;
    backend.child.kill('SIGINT'); // 先给 serve.py 优雅停机钩子机会
    const exited = await new Promise((resolve) => {
      const c = backend.child;
      if (c == null) return resolve(true);
      const t = setTimeout(() => resolve(false), GRACEFUL_WAIT_MS);
      c.on('exit', () => {
        clearTimeout(t);
        resolve(true);
      });
    });
    if (!exited) {
      // Windows 非交互场景 SIGINT 可能送不达 → taskkill 兜底（进程树）
      forceKillIfAlive(pid);
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
  backend.child = null;
  backend.reused = false;
  backend.port = null;
  backend.shuttingDown = false;
}

function restartBackend() {
  // stop 后重新 spawn（复用模式则重新走端口探测）
  return stopBackend().then(startBackend);
}

// 兜底：taskkill /PID <pid> /T /F
function forceKillIfAlive(pid) {
  const c = backend.child;
  if (c == null) return;
  const p = pid != null ? pid : c.pid;
  if (p == null) return;
  try {
    spawn('taskkill', ['/PID', String(p), '/T', '/F'], {
      stdio: 'ignore',
      windowsHide: true,
    });
    pushLog('sys', `SIGINT 未能在 ${GRACEFUL_WAIT_MS / 1000}s 内结束，使用 taskkill /PID ${p} /T /F 兜底`);
  } catch (e) {
    pushLog('err', `taskkill 失败：${e}`);
  }
}

function backendStatus() {
  return {
    running: childAlive() || backend.reused,
    reused: backend.reused,
    port: backend.port,
    pid: childAlive() ? backend.child.pid : null,
    starting: backend.starting,
  };
}

// ---------------------------------------------------------------------------
// 用户提示（日志尾部 + 重试/查看日志）
// ---------------------------------------------------------------------------

function notifyBackendExited(code, signal) {
  const win = BrowserWindow.getAllWindows()[0];
  const detail = tailLog(30).join('\n');
  dialog
    .showMessageBox(win || undefined, {
      type: 'error',
      noLink: true,
      title: '后端已退出',
      message: `后端进程意外退出（code=${code}, signal=${signal}）`,
      detail,
      buttons: ['重试', '查看日志', '关闭'],
    })
    .then(({ response }) => {
      if (response === 0) startBackend();
      else if (response === 1) dialog.showMessageBox(win || undefined, {
        type: 'none',
        title: '后端日志（尾部）',
        message: tailLog(100).join('\n'),
        buttons: ['好'],
      });
    });
}

function notifyReadinessTimeout(port) {
  const win = BrowserWindow.getAllWindows()[0];
  const detail = tailLog(30).join('\n');
  dialog
    .showMessageBox(win || undefined, {
      type: 'error',
      noLink: true,
      title: '后端启动超时',
      message: `等待 http://127.0.0.1:${port}/ 就绪超时（60s），后端日志尾部如下`,
      detail,
      buttons: ['重试', '查看日志', '关闭'],
    })
    .then(({ response }) => {
      if (response === 0) startBackend();
      else if (response === 1) dialog.showMessageBox(win || undefined, {
        type: 'none',
        title: '后端日志（尾部）',
        message: tailLog(100).join('\n'),
        buttons: ['好'],
      });
    });
}

// ---------------------------------------------------------------------------
// IPC（preload 暴露的最小面）
// ---------------------------------------------------------------------------

function registerIpc() {
  ipcMain.handle('backend:start', () => startBackend().then(backendStatus));
  ipcMain.handle('backend:stop', () => stopBackend().then(backendStatus));
  ipcMain.handle('backend:restart', () =>
    restartBackend().then(() => backendStatus())
  );
  ipcMain.handle('backend:log', (_e, n = 100) => tailLog(Math.max(1, Number(n) || 100)));
  ipcMain.handle('backend:status', () => backendStatus());
  ipcMain.handle('config:get', () => ({ ...config, configPath: configPath() }));
  ipcMain.handle('config:setProjectRoot', async (_e, root) => {
    root = String(root || '').trim();
    if (!root) throw new Error('项目根目录不能为空');
    const warnings = validateProjectRoot(root); // 无效则抛错，preload 侧会显示
    config.projectRoot = root;
    saveConfig(config);
    return { ok: true, warnings, config: { ...config } };
  });
  ipcMain.handle('config:chooseProjectRoot', async () => {
    const win = BrowserWindow.getAllWindows()[0];
    const r = await dialog.showOpenDialog(win, {
      title: '选择项目根目录（应包含 app/serve.py）',
      defaultPath: config.projectRoot,
      properties: ['openDirectory'],
    });
    if (r.canceled || r.filePaths.length === 0) return { ok: false };
    try {
      const res = await ipcMain.handle('config:setProjectRoot', null, r.filePaths[0]);
      return { ok: true, ...res };
    } catch (e) {
      dialog.showErrorBox('项目根目录无效', String(e.message || e));
      return { ok: false, error: String(e.message || e) };
    }
  });
  ipcMain.handle('shell:openExternal', (_e, url) => {
    if (String(url).startsWith('http')) shell.openExternal(url);
  });
}

// ---------------------------------------------------------------------------
// 窗口
// ---------------------------------------------------------------------------

function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    title: '漫剧工坊',
    autoHideMenuBar: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  const port = backend.port || DEFAULT_PORT;
  win.loadURL(`http://127.0.0.1:${port}/`);
  return win;
}

function buildMenu() {
  const template = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    {
      label: '后端',
      submenu: [
        { label: '重启后端', click: () => restartBackend() },
        { label: '查看日志', click: async () => {
          const lines = tailLog(100).join('\n');
          const win = BrowserWindow.getAllWindows()[0];
          dialog.showMessageBox(win, {
            type: 'none',
            title: '后端日志（尾部 100 行）',
            message: lines || '（暂无日志）',
            buttons: ['好'],
          });
        } },
        { type: 'separator' },
        { label: '停止后端', click: () => stopBackend() },
      ],
    },
    { role: 'fileMenu' },
    { role: 'editMenu' },
    { role: 'viewMenu' },
    { role: 'windowMenu' },
    { role: 'help' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------------------------------------------------------------------------
// App 生命周期
// ---------------------------------------------------------------------------

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const wins = BrowserWindow.getAllWindows();
    if (wins.length > 0) {
      if (wins[0].isMinimized()) wins[0].restore();
      wins[0].focus();
    }
  });

  app.whenReady().then(async () => {
    registerIpc();
    buildMenu();
    // 后端与窗口并行：先起窗口（指向端口），后端就绪后 reload，避免首屏白屏死等
    const win = createWindow();
    startBackend().then((b) => {
      if (b.reused || b.port) {
        win.loadURL(`http://127.0.0.1:${b.port}/`);
      }
    });
  });

  // 退出前：SIGINT → 等 10s → taskkill 兜底（仅我们 spawn 的子进程）
  app.on('before-quit', (event) => {
    if (!childAlive()) return; // 复用模式或已退出：无需处理
    event.preventDefault();
    backend.shuttingDown = true;
    const child = backend.child;
    const pid = child ? child.pid : null;
    if (child) child.kill('SIGINT');
    setTimeout(() => {
      forceKillIfAlive(pid);
      backend.child = null;
      app.quit(); // 二次 before-quit 时 childAlive() 为 false，直接放行
    }, GRACEFUL_WAIT_MS);
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
}

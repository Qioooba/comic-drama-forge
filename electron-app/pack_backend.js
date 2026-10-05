'use strict';
// 收集「独立 exe」要随包的后端资源到 electron-app/_backend/（供 electron-builder extraResources）。
//
//   _backend/
//   ├── python/      嵌入式 Python 运行时（含 site-packages，已装 app/requirements）
//   │                —— 本脚本**不生成**它（环境相关重资产），需要用户/CI 预先放置；
//   │                放置方式见下方「如何准备 python/」。
//   ├── app/         后端源码（Flask + providers/plugins/templates + static 前端产物）
//   ├── workflows/   ComfyUI 工作流模板（只读）
//   ├── locales/     i18n（zh-CN / en-US）
//   └── main.py      根入口（可选）
//
// 用法（node 直接跑，无需 npm install）：
//   node pack_backend.js
// 幂等：每次先清空 _backend/app|workflows|locales（python/ 保留，除非 --fresh）。
//
// 如何准备 python/（三选一）：
//   1) 官方 embeddable zip（python.org windows/embeddable）：
//        解压到 _backend/python/，改 python3xx._pth 放开 Lib\site-packages，
//        再 get-pip.py + pip install -r <项目>/app/requirements.txt（不含 torch/opencv，推理在 ComfyUI 侧）
//   2) 直接拷一个能跑的便携 venv 的 python.exe + Lib + Scripts
//   3) 在 CI 里用 `pip wheel` 预烘焙后并入
//  本脚本只做**校验与收集源码**；python/ 缺失时给出明确提示但不失败（CI 可后置注入）。

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');      // 项目根（漫剧生成系统/）
const OUT = path.join(__dirname, '_backend');
const PYDIR = path.join(OUT, 'python');

// 排除项：构建期缓存 + 密钥/活凭据（不随包）
const EXCLUDE_DIRS = new Set(['__pycache__', 'node_modules', '.git']);
const SECRET_FILES = new Set([
  '.env', '.secret_key', 'secrets.enc', 'secrets.enc.bak',
  'ai_config.json', 'llm_config.json', 'qc_config.json',
  'watermark_config.json', 'ref_img_path.txt',
  'qc_config.json.bak', 'tasks.db',
]);

// 递归拷目录（中文路径安全：逐个 fs.copyFileSync + mkdirSync，不用 cpSync filter）
function copyDirRec(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  let files = 0;
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      if (EXCLUDE_DIRS.has(entry.name)) continue;
      copyDirRec(s, d);
    } else if (entry.isFile()) {
      if (entry.name.endsWith('.pyc') || SECRET_FILES.has(entry.name.toLowerCase())) continue;
      fs.copyFileSync(s, d);
      files += 1;
    }
  }
  return files;
}

function main() {
  const fresh = process.argv.includes('--fresh');
  console.log(`项目根: ${ROOT}`);
  console.log(`输出:   ${OUT}`);

  // 1. 清（python/ 仅 --fresh 时清）
  if (fresh && fs.existsSync(PYDIR)) {
    fs.rmSync(PYDIR, { recursive: true, force: true });
    console.log('已清空 python/（--fresh）');
  }
  const dirItems = ['app', 'workflows', 'locales'];
  for (const name of dirItems) {
    const dst = path.join(OUT, name);
    if (fs.existsSync(dst)) fs.rmSync(dst, { recursive: true, force: true });
  }
  const mainPyDst = path.join(OUT, 'main.py');
  if (fs.existsSync(mainPyDst)) fs.rmSync(mainPyDst, { force: true });
  fs.mkdirSync(OUT, { recursive: true });

  // 2. 收集源码
  for (const name of dirItems) {
    const src = path.join(ROOT, name);
    if (!fs.existsSync(src)) {
      console.warn(`[跳过] 源缺失: ${src}`);
      continue;
    }
    const n = copyDirRec(src, path.join(OUT, name));
    console.log(`[收集] ${name}（${n} 文件）`);
  }
  const mainSrc = path.join(ROOT, 'main.py');
  if (fs.existsSync(mainSrc)) {
    fs.copyFileSync(mainSrc, mainPyDst);
    console.log('[收集] main.py');
  } else {
    console.warn('[跳过] main.py 源缺失');
  }

  // ⭐ 前端构建产物（2026-10-04）：Flask 提供的前端 UI 来自 app/static（vite 构建输出），
  // 资源增量/整包更新包都要带上最新前端 —— 但 pack_backend 此前**不会构建前端**，
  // 更新包里的 static/ 是上次构建期残留，导致「手动更新后前端仍是旧的」。
  // 现在：若本地有前端源码，就先跑一遍 vite build，让上面的目录收集自然带上最新产物。
  // 构建失败仅警告（沿用 app/static 旧产物），不阻塞后端资源收集。
  const FE_SRC = path.join(ROOT, 'frontend');
  const FE_STATIC = path.join(ROOT, 'app', 'static');
  if (fs.existsSync(path.join(FE_SRC, 'package.json'))) {
    try {
      const { execSync } = require('node:child_process');
      const t0 = Date.now();
      execSync('npm run build', { cwd: FE_SRC, stdio: 'pipe', timeout: 600000 });
      const distOk = fs.existsSync(FE_STATIC);
      console.log(`[前端] vite 构建完成（${Math.round((Date.now() - t0) / 1000)}s），static/ ${distOk ? '已更新' : '未生成（请检查 frontend 构建）'}`);
    } catch (e) {
      const msg = (e && e.message ? String(e.message) : String(e)).split('\n').slice(0, 6).join(' | ');
      console.warn('[前端] vite 构建失败（沿用 app/static 旧产物，不阻塞打包）：' + msg);
    }
  } else {
    console.warn('[前端] 未找到 frontend/package.json，跳过前端构建（沿用既有 app/static）');
  }

  // 3. 校验 Python 运行时
  const hasPy = fs.existsSync(path.join(PYDIR, 'python.exe')) ||
                fs.existsSync(path.join(PYDIR, 'python3'));
  if (hasPy) {
    console.log('[OK] 嵌入式 Python 就位: ' + PYDIR);
  } else {
    console.warn('[提示] 未找到嵌入式 Python（' + PYDIR + '/python.exe）');
    console.warn('       独立 exe 打包前请按本文件顶部说明放置 python/。');
    console.warn('       （源码已收集完毕，可在 CI 注入 python/ 后再 build:win）');
  }

  console.log('\n完成。下一步：npm install && npm run build:win');
  return 0;
}

module.exports = { main };

if (require.main === module) {
  try {
    process.exit(main());
  } catch (e) {
    console.error('收集失败:', e);
    process.exit(1);
  }
}

'use strict';
// 桌面版更新引擎（纯 Node，无第三方依赖；网络走 Node18+ 内置 fetch）
//
// 两种更新：
//   applyResources(zipPath)  资源增量：解 SHA256SUMS 校验后的 resources-x.y.z.zip，
//                            同步 workflows/locales/app 到「资源覆盖层」(userData)，
//                            只读资源、不动 userData 里的密钥/数据。需重启后端生效。
//   applyFull(exePath)       整包：把 portable.exe 写到 %LOCALAPPDATA%/temp，
//                            spawn 它接管替换 + 自重启（Electron 进程不能就地替换自身）。
//
// 检查：checkForUpdates() 拉 latest release，语义化版本比较，返回是否需要更新。
//
// 公开仓库匿名：API 走 api.github.com（带 User-Agent，匿名限速 60/h），
// 资产走 github.com 直链（不耗 API 配额）。

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { GITHUB, ASSET, cmpSemver } = require('./update-config');

const USER_AGENT = 'mjscxt-desktop-updater';

// ---------- 小工具 ----------

function sha256File(filePath) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('sha256');
    const st = fs.createReadStream(filePath);
    st.on('data', (d) => h.update(d));
    st.on('end', () => resolve(h.digest('hex')));
    st.on('error', reject);
  });
}

// 解析 "SHA256SUMS.txt"：一行 ` <64hex>  <文件名>`，返回 { 文件名: hex }
function parseSha256Sums(text) {
  const map = {};
  for (const line of String(text).split(/\r?\n/)) {
    const m = line.trim().match(/^([0-9a-f]{64})\s+(\S+)$/i);
    if (m) map[m[2]] = m[1].toLowerCase();
  }
  return map;
}

async function fetchText(url) {
  const res = await fetch(url, { headers: { 'User-Agent': USER_AGENT } });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`);
  return res.text();
}

// 下载（带进度回调 onProgress(fraction)）。流式写入，避免大资产全进内存。
async function downloadFile(url, destPath, onProgress) {
  const res = await fetch(url, { headers: { 'User-Agent': USER_AGENT } });
  if (!res.ok) throw new Error(`下载失败 HTTP ${res.status}: ${url}`);
  const total = Number(res.headers.get('content-length')) || 0;
  let got = 0;
  const out = fs.createWriteStream(destPath);
  try {
    // Node fetch 的 res.body 是 web ReadableStream，支持 for-await 异步迭代
    for await (const chunk of res.body) {
      out.write(chunk);
      got += chunk.length;
      if (total && onProgress) onProgress(got / total);
    }
  } finally {
    out.end();
  }
  await new Promise((r, j) => { out.on('close', r); out.on('error', j); });
  if (onProgress) onProgress(1);
}

// ---------- GitHub 检查 ----------

// 取 latest release。返回 { tag, name, assets:[{name, url, browser_download_url}] }
async function fetchLatestRelease() {
  const { owner, repo, apiBase } = GITHUB;
  const txt = await fetchText(`${apiBase}/repos/${owner}/${repo}/releases/latest`);
  let rel;
  try {
    rel = JSON.parse(txt);
  } catch {
    throw new Error(`GitHub 返回非 JSON（可能被限速或无 release）：${txt.slice(0, 200)}`);
  }
  if (!rel || !rel.tag_name) throw new Error('仓库暂无 release，无法检查更新');
  return rel;
}

// 检查更新。返回 { update, latest, current, needFull, needResource, reasons }
async function checkForUpdates(currentVer) {
  const rel = await fetchLatestRelease();
  const latest = rel.tag_name.replace(/^v/i, '');
  const needUpdate = cmpSemver(latest, currentVer) > 0;
  const assets = Array.isArray(rel.assets) ? rel.assets.map((a) => ({ name: a.name, url: a.browser_download_url })) : [];
  const has = (n) => assets.some((a) => a.name === n);
  return {
    update: needUpdate,
    latest,
    current: currentVer,
    releaseName: rel.name || '',
    needFull: needUpdate && has(ASSET.portable(latest)),
    needResource: needUpdate && has(ASSET.resources(latest)),
    assets,
  };
}

// 找资产下载直链
function assetUrl(assets, name) {
  const hit = assets.find((a) => a.name === name);
  return hit ? hit.url : null;
}

// 下载并校验某资产。返回 { localPath, ok }
async function downloadAndVerify(assets, assetName, shaSumsText, destDir, onProgress) {
  const url = assetUrl(assets, assetName);
  if (!url) throw new Error(`release 里没有资产 ${assetName}`);
  fs.mkdirSync(destDir, { recursive: true });
  const localPath = path.join(destDir, path.basename(assetName));
  await downloadFile(url, localPath, onProgress);
  // 若提供 SHA256SUMS 清单则强制校验；否则仅本地记录
  let verified = false;
  if (shaSumsText) {
    const sums = parseSha256Sums(shaSumsText);
    const expected = sums[assetName];
    const actual = await sha256File(localPath);
    verified = !!expected && expected.toLowerCase() === actual;
    if (expected && !verified) {
      throw new Error(`SHA256 校验失败：${assetName}（期望 ${expected} 实际 ${actual}）`);
    }
  }
  return { localPath, verified };
}

// ---------- 应用：资源增量 ----------

// 资源增量：下载 resources-x.y.z.zip → SHA256SUMS 校验 → 解包到「资源覆盖层」。
// 覆盖层里只有 workflows/ locales/ app/（只读资源），**不动 userData 里的
// .secret_key / secrets.enc / output / novels**（那些是可写数据区，与资源分离）。
// Windows 用 PowerShell Expand-Archive，跨平台回落 tar -xf。
async function downloadAndUnpackResources(latestVer, assets, shaSumsText, destDir, overrideDir, onProgress) {
  const resName = ASSET.resources(latestVer);
  const { localPath } = await downloadAndVerify(assets, resName, shaSumsText, destDir, onProgress);
  // 拉 SHA256SUMS（若 release 带）已在 downloadAndVerify 校验过；这里仅解包
  fs.mkdirSync(overrideDir, { recursive: true });
  if (process.platform === 'win32') {
    await new Promise((resolve, reject) => {
      const ps = spawn('powershell', [
        '-NoProfile', '-Command',
        `Expand-Archive -Path "${localPath}" -DestinationPath "${overrideDir}" -Force`,
      ], { windowsHide: true });
      let err = '';
      ps.stderr.on('data', (d) => { err += d; });
      ps.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`Expand-Archive 失败：${err}`))));
    });
  } else {
    await new Promise((resolve, reject) => {
      const p = spawn('tar', ['-xf', localPath, '-C', overrideDir], { windowsHide: true });
      let err = '';
      p.stderr.on('data', (d) => { err += d; });
      p.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`tar 解包失败：${err}`))));
    });
  }
  // 清掉临时 zip
  try { fs.rmSync(localPath, { force: true }); } catch { /* ignore */ }
  return overrideDir;
}

// ---------- 应用：整包 ----------

// 把 portable.exe 放到可写临时位置，校验后（可选）spawn 它接管替换 + 自重启。
// 本 Electron 进程不能就地替换自己 → 交给 bootstrapper。
// spawnNow=false：只下载+校验并返回 localPath，由调用方在用户确认后手动 spawn
//                 （经 spawnPortable(localPath) 完成接管），用于「稍后/手动确认」路径。
async function applyFullPortable(latestVer, assets, shaSumsText, destDir, onProgress, spawnNow = true) {
  const { localPath } = await downloadAndVerify(assets, ASSET.portable(latestVer), shaSumsText, destDir, onProgress);
  if (!spawnNow) return { localPath, spawned: false };
  const spawnOpts = process.platform === 'win32'
    ? { detached: true, stdio: 'ignore', windowsHide: true }
    : { detached: true, stdio: 'ignore' };
  const child = spawn(localPath, [], spawnOpts);
  if (process.platform !== 'win32') child.unref();
  return { localPath, spawned: true };
}

// 由调用方在用户确认后再触发 bootstrapper（接管替换 + 自重启）
function spawnPortable(localPath) {
  const spawnOpts = process.platform === 'win32'
    ? { detached: true, stdio: 'ignore', windowsHide: true }
    : { detached: true, stdio: 'ignore' };
  const child = spawn(localPath, [], spawnOpts);
  if (process.platform !== 'win32') child.unref();
  return true;
}

module.exports = {
  checkForUpdates,
  downloadAndUnpackResources,
  applyFullPortable,
  spawnPortable,
  fetchLatestRelease,
  parseSha256Sums,
  sha256File,
};

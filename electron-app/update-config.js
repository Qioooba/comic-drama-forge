'use strict';
// 桌面版更新系统 · 常量层（公开仓库匿名下载，无需 token）
//
// 设计（按已拍板决策）：
//   · 更新粒度：整包 + 资源增量，两者都支持
//       - 资源增量：workflows/ locales/ app/（前端 static 在 app/static）同步到安装目录，
//         只读资源更新，**不动 userData**（密钥 .secret_key / secrets.enc / 数据全在 userData）。
//         需重启后端(serve.py) 生效。
//       - 整包：下载 portable.exe bootstrapper，校验后交给它替换 + 自重启。
//   · 触发：启动检查一次 + 运行中每 24h 后台轮询 + 菜单「检查更新」手动。
//   · 仓库：GitHub 公开仓库，匿名访问 api.github.com + 资产直链，终端用户零配置。
//
// 与 update-config 严格分离的是「安装模型」：
//   桌面版把 _backend/{python,app,workflows,locales} 打进 resources（extraResources，只读安装区）；
//   可写数据（output/novels/*.config.json/.secret_key/secrets.enc/tasks.db）走 userData（MJSCXT_DATA_DIR）。
//   资源增量更新时，若安装区只读（Program Files），改写到 userData 下的「资源覆盖层」，
//   后端启动时优先用覆盖层（由 main.js 设 MJSCXT_RESOURCE_DIR 指向它，serve.py 侧读只读资源时合并）。

// GitHub 仓库（公开）。release 资产命名规范见下。
// ⚠️ 必须与「实际发 Release 的仓库」一致：本仓 git remote origin =
//    xianjing2000/comic-drama-forge，`.github/workflows/desktop-release.yml` 也把
//    Release 发在这里。两者不一致 → 客户端只会去另一个仓库找 `releases/latest`，
//    找不到就永久静默、自动更新空转（且不会报错，极难发现）。
// ⚠️ 旧注释声称「运行时可用 userData/update.json 覆盖 owner/repo」——**该覆盖并未实现**
//    （updater.js:78-79 直接读本常量，全仓无 update.json 读取代码）。换仓库/分发行版
//    请改这里并重新发版。
const GITHUB = {
  owner: 'xianjing2000',
  repo: 'comic-drama-forge',
  apiBase: 'https://api.github.com',
  // 资产下载前缀（公开仓库匿名：直接走对象直链，不必走 api 逐字节）
  downloadBase: 'https://github.com',
};

// 资产命名规范（与 .github/workflows/desktop-release.yml 对齐）：
//   comic-drama-forge-Portable-x.y.z.exe  整包 bootstrapper（portable target）
//   resources-x.y.z.zip                   资源增量包（workflows+locales+app 只读资源）
//   SHA256SUMS.txt                        上述资产的 sha256 清单（一行一资产： <hex>  <文件名>）
//   resource-manifest.json                增量包内文件清单 + 版本（updater 校验用，可选）
// ⚠️ 资产名必须 ASCII：GitHub Release 会吞掉文件名里的中文（如 漫剧工坊-Portable-…exe
//    上传后变成 -Portable-…exe，前缀被剥离），导致 updater 按中文名找不到资产、自动更新
//    永久空转。故 portable 名用仓库名 comic-drama-forge，与「实际发 Release 的仓库」同源。
const ASSET = {
  portable: (v) => `comic-drama-forge-Portable-${v}.exe`,
  resources: (v) => `resources-${v}.zip`,
  sha256sums: 'SHA256SUMS.txt',
  manifest: 'resource-manifest.json',
};

// 版本比较用：读 package.json 的 version（主进程启动时缓存）
function currentVersion() {
  try {
    // eslint-disable-next-line global-require
    return require('./package.json').version || '0.0.0';
  } catch {
    return '0.0.0';
  }
}

// 语义化版本比较：返回 -1/0/1（a vs b）；解析失败视为 0
function cmpSemver(a, b) {
  const norm = (s) => String(s || '').replace(/^v/, '').split('.').map((x) => parseInt(x, 10) || 0);
  const A = norm(a), B = norm(b);
  for (let i = 0; i < 3; i += 1) {
    const x = A[i] || 0, y = B[i] || 0;
    if (x !== y) return x > y ? 1 : -1;
  }
  return 0;
}

module.exports = { GITHUB, ASSET, currentVersion, cmpSemver };

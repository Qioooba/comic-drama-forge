/**
 * 旧入口退役层（301 层）。
 *
 * 为什么不能一刀切删
 * ------------------
 * 改造前 `App.tsx` 是**手写 hash 解析**（`parseRoute()`），它承认过三种历史形态：
 *   `#projects?p=<key>`      侧边栏/App 早期写法
 *   `#/projects?p=<key>`     规范化之后的写法
 *   `#ai` / `#memory` / …    旧全局 section（无前导斜杠）
 * 另外 `ProjectsPage` / `OverviewPage` / `ErrorBoundary` 至今仍在
 * `window.location.hash = '/?p=…'`（**无斜杠、无 section**）——这三个文件
 * 不在本轮改造范围内，所以这些形态**仍会继续被写出来**。
 *
 * 直接删掉的后果是死链且无人报警：用户点旧书签 / 外部文档里的链接进来，
 * 落到 404 却没有一个地方告诉他「这个入口已经迁移到哪」。保留这一层就是把
 * 死链变成**一次可见的重定向**。
 *
 * 两条硬规矩（MASTER §6 invariant 12）
 * ------------------------------------
 * 1. 必须保留 query —— 旧链接可能带 `?p=`，也可能带别的参数；
 * 2. 必须保留 hash —— 锚点（`#shot-12`）丢了等于跳错位置。
 */
import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { isWorkbenchTab, workbenchPath, WORKBENCH_TABS } from './workbenchTabs';

/** 旧全局 section → 新路径。`projects` 单独处理（它带 `?p=`）。 */
const LEGACY_SECTIONS: Record<string, string> = {
  projects: '/projects',
  ai: '/ai',
  memory: '/memory',
  comfyui: '/comfyui',
  logs: '/logs',
  // 以下是 Flask 侧 `_SPA_PAGES` 里存在过、前端却从未真正实现的孤儿入口。
  // 改造前它们会被 parseRoute 落到 'projects'；这里显式登记，避免将来
  // 有人从旧书签里翻出来时行为不可预期。
  upload: '/projects',
  auto: '/projects',
  characters: '/projects',
  grid: '/projects',
  deliver: '/projects',
  export: '/projects',
  settings: '/projects',
};

/**
 * 把旧 hash 解析成新路径。**纯函数** —— 不碰 history，便于静态审阅与后续补测
 * （评估 §P0-2 第 5 点点名要测 `pickRoute` 一类纯函数）。
 *
 * @param rawHash `window.location.hash` 的原始值（含前导 `#`）
 * @returns 新路径；已是新路由时返回 null（不干预）
 */
export function resolveLegacyHash(rawHash: string): string | null {
  let raw = rawHash.replace(/^#/, '');
  if (!raw) return null;
  // 循环剥掉历史归一化残留的 `#` / `/` 前缀：`#/#/projects?p=x` → `/projects?p=x`
  // （改造前 parseRoute 只处理了一层 `/#`，这里补齐更深的历史形态）
  for (;;) {
    if (raw.startsWith('/#')) { raw = raw.slice(1); continue; }
    if (raw.startsWith('#')) { raw = raw.slice(1); continue; }
    break;
  }
  if (!raw) return null;
  // ⚠️ 必须记住「原写法有没有前导斜杠」：`#ai` 是旧 section，`#/ai` 才是新路由。
  // 两者归一化后同形，若不记住这一点，旧入口会被误判成「已经是新路由」而不跳转。
  const hadLeadingSlash = raw.startsWith('/');
  if (!hadLeadingSlash) raw = `/${raw}`;

  const [beforeHash, ...hashRest] = raw.split('#');
  const hash = hashRest.length ? `#${hashRest.join('#')}` : '';
  const qIndex = beforeHash.indexOf('?');
  const pathname = qIndex >= 0 ? beforeHash.slice(0, qIndex) : beforeHash;
  const search = qIndex >= 0 ? beforeHash.slice(qIndex) : '';
  const params = new URLSearchParams(search);

  // 已经是新路由：`/projects/<key>[/<tab>]`、`/ai` 等 —— 不干预。
  // 只在「原本带前导斜杠」时才认定为新路由。
  // ⚠️ `/projects` 本身也是新路由，但 `#/projects?p=<key>` 仍是**旧深链**
  //（旧 App 的 parseRoute 就是这么解析的），所以要先看有没有 `p` 再决定。
  const legacyProjectKey = params.get('p');
  if (hadLeadingSlash && !legacyProjectKey) {
    if (/^\/projects\/[^/]+$/.test(pathname) || /^\/projects\/[^/]+\/[^/]+$/.test(pathname)) {
      return null;
    }
    if (['/ai', '/memory', '/comfyui', '/logs', '/projects'].includes(pathname)) return null;
  }

  // ① 项目工作台深链：`?p=<key>` 是旧实现承载全部深链的唯一手段
  const projectKey = legacyProjectKey;
  if (projectKey) {
    // 旧 `?tab=` 若存在且合法则尊重它；否则回落到第一个 tab。
    const legacyTab = params.get('tab') || undefined;
    const tab = isWorkbenchTab(legacyTab) ? legacyTab : WORKBENCH_TABS[0];
    // `p` 是旧路由的载体，新路由用 path segment —— 保留其余 query 与锚点。
    const kept = new URLSearchParams(params);
    kept.delete('p');
    kept.delete('tab');
    const rest = kept.toString();
    return `${workbenchPath(projectKey, tab)}${rest ? `?${rest}` : ''}${hash}`;
  }

  // ② 旧全局 section
  const legacySection = LEGACY_SECTIONS[pathname.replace(/^\//, '')];
  if (legacySection) {
    return `${legacySection}${search}${hash}`;
  }

  return null;
}

/**
 * 挂在 Router 内的守卫组件：挂载时与每次路由变化时检查一次当前 hash，
 * 命中旧入口就用 `replace` 跳转（**replace 而非 push** —— 301 语义，
 * 否则用户「后退」会退回到旧 URL 再被弹回来，形成死循环）。
 */
export function LegacyRouteRedirect(): null {
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    const target = resolveLegacyHash(window.location.hash);
    if (target && target !== `${location.pathname}${location.search}${location.hash}`) {
      navigate(target, { replace: true });
    }
  }, [location.pathname, location.search, location.hash, navigate]);

  return null;
}

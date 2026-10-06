/**
 * 路由表。
 *
 * 为什么用 **HashRouter** 而不是 BrowserRouter
 * ------------------------------------------
 * 改造前 `App.tsx:28-41` 是手写 hash 解析。本轮直接换 BrowserRouter 会留下
 * 三类**仍在生效**的旧写入方（它们不在本轮可改文件清单里）：
 *   - `pages/ProjectsPage.tsx:224,398,402` 写 `location.hash = '/?p=<key>'`
 *   - `pages/OverviewPage.tsx:156,160`      同上
 *   - `components/ErrorBoundary.tsx:71`     写 `location.hash = '#/'`
 * BrowserRouter 下 hash 不是路由源，这三处写入不会触发路由变化，表现为
 * 「点了没反应」；HashRouter 下 hash 即路由源，它们继续可用。
 * 桌面端 `electron-app/main.js` 亦以 `http://127.0.0.1:<port>/` 起本地服务，
 * 与浏览器同构。**hash 保持唯一事实源**是本轮风险最低的选择。
 *
 * 路由表
 * ------
 *   /                            → 重定向到 /projects
 *   /projects                    项目中心
 *   /projects/:projectKey        补默认 tab（总览）
 *   /projects/:projectKey/:tab   项目工作台（8 个域，顺序见 workbenchTabs）
 *   /ai /memory /comfyui /logs   全局「系统中心」页
 *   *                            404
 *
 * 旧 hash 入口不在此兜底 —— 由 `LegacyRouteRedirect` 在 hash 层 301，
 * 这样死链会变成一次**可见的跳转**，而不是静默落到 404（MASTER invariant 12）。
 */
import type { ReactNode } from 'react';
import {
  HashRouter, Navigate, Route, Routes,
} from 'react-router-dom';
import { LegacyRouteRedirect } from './routes/legacyRedirect';
import {
  AIRoute, ComfyUIRoute, LogsRoute, MemoryRoute,
  ProjectWorkbenchIndexRoute, ProjectWorkbenchRoute, ProjectsRoute,
} from './routes/routeElements';

/** 未知路径：回项目中心。侧边栏第一项即项目中心，回这里符合用户预期。 */
function NotFoundRedirect(): JSX.Element {
  return <Navigate to="/projects" replace />;
}

/**
 * 路由表本体。渲染在 AppShell 的 `<main>` 内。
 * 旧 hash 301 层作为兄弟节点先于 `<Routes>` 判定 —— 命中即 replace 跳转。
 */
export function AppRouteTable(): JSX.Element {
  return (
    <>
      <LegacyRouteRedirect />
      <Routes>
        <Route path="/" element={<Navigate to="/projects" replace />} />
        <Route path="/projects" element={<ProjectsRoute />} />
        <Route path="/projects/:projectKey" element={<ProjectWorkbenchIndexRoute />} />
        <Route path="/projects/:projectKey/:tab" element={<ProjectWorkbenchRoute />} />
        <Route path="/ai" element={<AIRoute />} />
        <Route path="/memory" element={<MemoryRoute />} />
        <Route path="/comfyui" element={<ComfyUIRoute />} />
        <Route path="/logs" element={<LogsRoute />} />
        {/* 孤儿 SPA 页（upload/auto/characters/grid/deliver/export/settings）
            与未知路径同归一处：显式回项目中心，而不是静默渲染空白页。 */}
        <Route path="*" element={<NotFoundRedirect />} />
      </Routes>
    </>
  );
}

/**
 * Router 提供者。必须包在所有会调用 `useNavigate` / `useLocation` 的组件之外 ——
 * `App.tsx` 的 AppShellContent 自身就要用这两个 hook，所以提供者放在 App 里层。
 */
export function AppRouterProvider({ children }: { children: ReactNode }): JSX.Element {
  return <HashRouter>{children}</HashRouter>;
}

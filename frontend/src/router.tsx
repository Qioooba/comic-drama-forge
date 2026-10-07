/**
 * 路由表。
 *
 * 为什么用 **HashRouter** 而不是 BrowserRouter
 * ------------------------------------------
 * 改造前 `App.tsx:28-41` 是手写 hash 解析，换 BrowserRouter 会被仍在写
 * `location.hash` 的旧代码挡住。那三处写入
 * （`pages/ProjectsPage.tsx`、`pages/OverviewPage.tsx`、`components/ErrorBoundary.tsx`）
 * **现已全部改为 `useNavigate()`**，不再直接写 hash —— 所以「它们会挡住换路由」
 * 这个理由已经失效。
 *
 * 仍然保留 HashRouter 的理由是另一条：**旧书签与外部文档里的链接**仍是
 * `#ai` / `#projects?p=<key>` 这类 hash 形态（见 `routes/legacyRedirect.tsx` 的
 * 301 层）。保持 hash 为唯一事实源，这些链接继续可见地跳转而不是静默 404。
 * 若将来要换 BrowserRouter，必须同时给旧链接安排一层**服务端/入口层**的重定向，
 * 否则外部链接会全部失效 —— 那不是一次前端重构能覆盖的范围。
 *
 * 桌面端 `electron-app/main.js` 亦以 `http://127.0.0.1:<port>/` 起本地服务，
 * 与浏览器同构。
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

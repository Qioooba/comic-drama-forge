/**
 * 路由出口组件。
 *
 * 全部保持「薄壳」：只负责从 URL 取参数、把既有页面组件原样挂上去。
 * 页面内部逻辑仍住在原页面文件里，等对应域搬进 `features/` 后再逐步瘦身
 * （评估 §P0-2 第 1 点：换真路由；逻辑搬迁是第 2 点，分开做便于逐步验证）。
 */
import { Navigate, useParams } from 'react-router-dom';
import { ProjectWorkbenchPage } from '@/pages/ProjectWorkbenchPage';
import { ProjectsPage } from '@/pages/ProjectsPage';
import { AIVaultPage } from '@/pages/AIVaultPage';
import { MemoryPage } from '@/pages/MemoryPage';
import { ComfyUIPage } from '@/pages/ComfyUIPage';
import { LogsPage } from '@/pages/LogsPage';
import { isWorkbenchTab, WORKBENCH_TABS, workbenchPath } from './workbenchTabs';

/**
 * `/projects/:projectKey/:tab`
 *
 * `key={projectKey}` 保留审计 P2-36 的修复：切项目时整页重挂，旧项目响应
 * 不会覆盖新项目的选中集（组件实例复用会留下上一个项目的状态与竞态）。
 */
export function ProjectWorkbenchRoute(): JSX.Element {
  const { projectKey = '', tab } = useParams();
  if (!projectKey) return <Navigate to="/projects" replace />;
  // 非法段不静默渲染空白页：归一到第一个 tab（生产顺序的总览）
  if (!isWorkbenchTab(tab)) {
    return <Navigate to={workbenchPath(projectKey, WORKBENCH_TABS[0])} replace />;
  }
  // `tab` 已被上面的 isWorkbenchTab 收窄为 WorkbenchTab，必须**往下传**：
  // 页面自己再解一次 URL 就会出现两个真源，而这里解出来的值才是路由认定的值。
  // 不传的后果是 `/projects/<key>/qc` 与 `/projects/<key>/overview` 渲染出同一个页面
  // （页面内部 state 恒为 overview），深链静默失效。
  return <ProjectWorkbenchPage key={projectKey} projectKey={projectKey} tab={tab} />;
}

/** `/projects/:projectKey` —— 缺 tab 时补默认段，让 URL 始终是完整深链。 */
export function ProjectWorkbenchIndexRoute(): JSX.Element {
  const { projectKey = '' } = useParams();
  if (!projectKey) return <Navigate to="/projects" replace />;
  return <Navigate to={workbenchPath(projectKey, WORKBENCH_TABS[0])} replace />;
}

export function ProjectsRoute(): JSX.Element {
  return <ProjectsPage />;
}
export function AIRoute(): JSX.Element {
  return <AIVaultPage />;
}
export function MemoryRoute(): JSX.Element {
  return <MemoryPage />;
}
export function ComfyUIRoute(): JSX.Element {
  return <ComfyUIPage />;
}
export function LogsRoute(): JSX.Element {
  return <LogsPage />;
}

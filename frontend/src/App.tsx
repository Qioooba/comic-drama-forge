import React, { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { AppProvider } from '@/context/AppContext';
import { ThemeProvider } from '@/context/ThemeContext';
import { ToastProvider } from '@/components/ui/toast';
import { Navbar } from '@/components/layout/Navbar';
import { Sidebar } from '@/components/layout/Sidebar';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { ServiceMonitor } from '@/components/ServiceMonitor';
import { ApiCompatibilityGate } from '@/components/ApiCompatibilityGate';
import { AppRouteTable, AppRouterProvider } from '@/router';

// 全局（与具体项目无关）的功能放这里；单个项目的功能一律进 /projects/:key/:tab。
// 「上传小说」已并入「项目中心 → 新建项目」，不再单独占一个入口。
//
// ⚠️ 侧边栏高亮用的 id 必须与 Sidebar.tsx 的 navItems[].id 逐字一致 ——
// 那个文件不在本轮可改清单里，两边只能靠约定对齐，改一处必须同步另一处。
const SECTION_ROUTES: Record<string, string> = {
  projects: '/projects',
  ai: '/ai',
  memory: '/memory',
  comfyui: '/comfyui',
  logs: '/logs',
};

/** 由当前 pathname 反推侧边栏高亮项：项目工作台整体高亮「项目中心」。 */
function activeSectionOf(pathname: string): string {
  if (pathname.startsWith('/projects')) return 'projects';
  const hit = Object.keys(SECTION_ROUTES).find(
    (id) => SECTION_ROUTES[id] === pathname,
  );
  return hit || 'projects';
}

function AppShellContent() {
  const location = useLocation();
  const navigate = useNavigate();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  return (
    // bg-canvas：令牌层承载面。侧边栏/导航栏自己带 glass-chrome，主区不叠模糊。
    <div className="flex h-screen bg-canvas text-ink-1">
      {/* Sidebar */}
      <Sidebar
        activeSection={activeSectionOf(location.pathname)}
        onNavigate={(id) => {
          const target = SECTION_ROUTES[id];
          if (target) navigate(target);
        }}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
      />

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        <div className="flex-1 flex flex-col overflow-hidden">
          <Navbar />
          {/* 超宽屏下限宽居中，避免一行文字拉到 2000px+ 难以阅读 */}
          <main className="w-full flex-1 overflow-y-auto p-6">
            <AppRouteTable />
          </main>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      {/* ThemeProvider 放最外层（不依赖其他 context）：深色实现 = 给 <html> 注入
          .dark 类，配合 index.css 的 :root.dark token 覆盖层整站换肤，
          组件层不感知主题、无 dark: 变体。 */}
      <ThemeProvider>
        {/* ToastProvider 必须在 AppProvider 之外：AppContext.showError 要往 Toast 里推消息 */}
        <ToastProvider>
          <AppProvider>
            {/* Router 提供者包在 AppShellContent 之外 —— 壳层自身就要用
                useLocation（侧边栏高亮）与 useNavigate（侧边栏点击） */}
            <AppRouterProvider>
              <ApiCompatibilityGate>
                <AppShellContent />
              </ApiCompatibilityGate>
              <ServiceMonitor />
            </AppRouterProvider>
          </AppProvider>
        </ToastProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

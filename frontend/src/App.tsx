import React, { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { AppProvider, useApp } from '@/context/AppContext';
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

/**
 * 语言包就绪门（P0：冷启动整屏漏出原始 key）
 * ----------------------------------------------------
 * `/api/i18n/<lang>` 是**异步**的，而 `t()` 在语言包到达前会原样返回 key。
 * AppProvider 在 mount 后才发起加载，所以首屏必然有一段时间 `messages` 为空，
 * 此时渲染出的界面全是 `nav.userMenu` / `theme.dark` / `app.title` 这种
 * 内部 key —— 屏幕阅读器也会照着念。实测冷启动后端（首包 ~880ms）下肉眼可见。
 *
 * 修法：**语言包就绪前不渲染业务界面**，只给一个不依赖 t() 的极简占位。
 * 注意占位里**不能有任何文案**（此刻还没有可翻译的词），所以是一个呼吸点。
 * 这也顺带消除了「首屏先错后对」的抖动。
 */
function I18nGate({ children }: { children: React.ReactNode }) {
  const { loading } = useApp();
  if (loading) {
    return (
      <div className="flex h-dvh items-center justify-center bg-canvas" role="status" aria-busy="true">
        <span
          className="h-6 w-6 animate-spin rounded-full border-2 border-brand/30 border-t-brand"
          aria-hidden="true"
        />
      </div>
    );
  }
  return <>{children}</>;
}

function AppShellContent() {
  const location = useLocation();
  const navigate = useNavigate();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  return (
    // bg-canvas：令牌层承载面。侧边栏/导航栏自己带 glass-chrome，主区不叠模糊。
    // ⚠️ h-dvh 而非 h-screen：100vh 不等于移动端/收起地址栏后的可见高度，
    //    底部 60–100px 永远够不到（index.css 的 #root 同样已加 100dvh 兜底）。
    <div className="flex h-dvh bg-canvas text-ink-1">
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
          {/* 超宽屏下限宽居中，避免一行文字拉到 2000px+ 难以阅读。
              ⚠️ min-w-0 不可省：flex 子项默认 min-width:auto，工作台里同时存在
              44px 竖条 + 聊天面板 + 三栏 grid-cols-[285px_1fr_350px]，任何一处
              内在宽度超出都会把 main 撑出横向滚动条，而不是压缩子项。
              overflow-x-hidden 兜住 sticky 元素（EpisodeContextBar 的负边距）。 */}
          <main className="w-full min-w-0 flex-1 overflow-y-auto overflow-x-hidden p-6">
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
      {/* ThemeProvider 放最外层（不依赖其他 context）：主题实现 = 给 <html> 注入
          **.light** 类（浅色皮肤），深色是 index.css `:root` 的默认取值，
          整站换肤走 `:root` / `:root.light` 两套**变量覆盖**，组件层不感知主题、
          无 dark: 变体。⚠️ 项目里**不存在** `.dark` 类，别按 v4 那套去注入它。 */}
      <ThemeProvider>
        {/* ToastProvider 必须在 AppProvider 之外：AppContext.showError 要往 Toast 里推消息 */}
        <ToastProvider>
          <AppProvider>
            {/* Router 提供者包在 AppShellContent 之外 —— 壳层自身就要用
                useLocation（侧边栏高亮）与 useNavigate（侧边栏点击） */}
            <AppRouterProvider>
              {/* ⚠️ I18nGate 包住下面**全部**业务界面（含 ServiceMonitor）：
                  语言包没到位之前不渲染，否则首屏会漏出 nav.userMenu 这类原始 key。 */}
              <I18nGate>
                <ApiCompatibilityGate>
                  <AppShellContent />
                </ApiCompatibilityGate>
                <ServiceMonitor />
              </I18nGate>
            </AppRouterProvider>
          </AppProvider>
        </ToastProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

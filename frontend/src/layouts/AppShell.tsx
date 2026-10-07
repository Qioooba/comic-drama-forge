/**
 * 三栏应用外壳（MASTER §3）。
 *
 *   ┌─────────────────────────────────────────────────────────┐
 *   │ EpisodeContextBar  项目 → 季 → 集 → 镜 + 队列/GPU/磁盘  │  常驻
 *   ├──────────┬──────────────────────────────┬───────────────┤
 *   │ 导航器   │        媒体舞台              │   检查器      │
 *   │ 285px    │        1fr                   │   350px       │
 *   │ (可收)   │   bg-media，比 UI 更深        │  意图/参数/   │
 *   │          │                              │  候选/参考    │
 *   └──────────┴──────────────────────────────┴───────────────┘
 *
 * 响应式三档（MASTER §3）
 *   ≥1440 三栏全展开 / 1280 检查器收窄、导航器可折叠 / ≤1024 单列、检查器转抽屉
 *
 * ⚠️ 关于列宽 token：下方 `lg:grid-cols-[285px_1fr_350px]` 里的 285px / 350px
 *    目前是**字面量**，本文件并没有 `--nav-w` / `--inspector-w` 这样的 CSS 变量
 *    （全仓未定义）。原文此处承诺「宽度用 token，改 token 即可传导」是不成立的 ——
 *    按那条承诺去 grep 只会找到一个不存在的定义。
 *    两个数值各自在本文件只出现一次（上面的示意图除外），所以集中在此处；
 *    真要跨文件统一（例如与 EpisodeContextBar 的高度对齐），正确顺序是
 *    先在 index.css 定义变量、再把这里改成 var() 引用，而不是只在注释里承诺。
 */
import React, { useEffect, useState } from 'react';
import {
  EpisodeContextBar,
  type EpisodeContext,
  type ResourceStatus,
} from './EpisodeContextBar';

/**
 * 断点判定（≥1024 即 lg）。
 *
 * ⚠️ 为什么检查器要用它而不是纯 CSS：原先 `inspector` 被渲染**两次**
 * （桌面 aside + 抽屉），只是靠 `lg:block` / `lg:hidden` 藏起来。
 * DOM 里有两份，就有两套 `<label for>` / `<input id>` —— 检查器里
 * 任何带 id 的控件（导演意图编辑器就有 `id="director-intent-prompt"`）
 * 都会产生重复 id，读屏与点标签聚焦都会指错。
 * 这里让同一时刻只有一份在 DOM 里，视觉表现不变（同一断点、同一位置）。
 */
function useIsWide(): boolean {
  const [wide, setWide] = useState(() =>
    typeof window === 'undefined' ? true : window.matchMedia('(min-width: 1024px)').matches,
  );
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)');
    const on = () => setWide(mq.matches);
    on();
    // 老浏览器只有 addListener；两套都挂上，缺一套就静默不跟随断点
    if (mq.addEventListener) mq.addEventListener('change', on);
    else mq.addListener(on);
    return () => {
      if (mq.removeEventListener) mq.removeEventListener('change', on);
      else mq.removeListener(on);
    };
  }, []);
  return wide;
}

export interface AppShellProps {
  context: EpisodeContext;
  status?: ResourceStatus;
  /** 左栏：镜头导航 */
  navigator: React.ReactNode;
  /** 中栏：媒体舞台 */
  stage: React.ReactNode;
  /** 右栏：检查器（意图 / 参数 / 候选 / 参考） */
  inspector: React.ReactNode;
  /** 上下文条上的动作（如决策态徽标，见 EpisodeContextBar.actions） */
  actions?: React.ReactNode;
  className?: string;
}

export function AppShell({
  context,
  status,
  navigator,
  stage,
  inspector,
  actions,
  className = '',
}: AppShellProps): JSX.Element {
  const [navOpen, setNavOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const wide = useIsWide();

  return (
    <div className={`flex min-h-0 flex-col ${className}`}>
      <EpisodeContextBar
        context={context}
        status={status}
        navigatorOpen={navOpen}
        onToggleNavigator={() => setNavOpen((v) => !v)}
        actions={actions}
      />

      {/* ≥1440 三栏全展开；1280 检查器收窄；≤1024 单列 + 检查器转抽屉 */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 p-3 lg:grid-cols-[285px_1fr_350px]">
        {/* 左：导航器 */}
        <aside className={`min-h-0 ${navOpen ? '' : 'hidden lg:block'}`}>
          {navOpen && navigator}
        </aside>

        {/* 中：媒体舞台。
            ⚠️ 用 `<section>` 而不是 `<main>`：`App.tsx` 的 AppShellContent 已经
            有一个 `<main>` 包住整条路由表，同一文档里出现两个 main 地标会让
            读屏与「跳转主内容」失效。这里是主内容**内部**的一块区域。 */}
        <section className="min-h-[320px] min-w-0 rounded-lg border border-line bg-media p-3">
          {stage}
        </section>

        {/* 右：检查器。≤1024 转抽屉（用 lg:hidden 的固定底栏承载）。
            同一时刻只挂载一份，见 useIsWide 的注释。 */}
        {wide && <aside className="min-h-0">{inspector}</aside>}

        {!wide && (
          <div className="fixed inset-x-0 bottom-0 z-drawer border-t border-line bg-surface p-2">
            <button
              onClick={() => setInspectorOpen((v) => !v)}
              aria-expanded={inspectorOpen}
              className="control-compact w-full rounded border border-line text-sm text-ink-1"
            >
              {inspectorOpen ? '收起检查器' : '展开检查器'}
            </button>
          </div>
        )}
      </div>

      {/* ≤1024 的检查器抽屉内容 */}
      {!wide && inspectorOpen && (
        <div className="border-t border-line bg-surface p-3">{inspector}</div>
      )}
    </div>
  );
}

export default AppShell;
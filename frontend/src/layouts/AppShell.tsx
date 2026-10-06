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
 * ⚠️ 宽度用 token（`--nav-w` 等）而不是魔法数字：改 token 即可传导，
 *    散落的 285px/350px 字面量改一处要翻三个文件。
 */
import React, { useState } from 'react';
import { EpisodeContextBar, type EpisodeContext, type ResourceStatus } from './EpisodeContextBar';

export interface AppShellProps {
  context: EpisodeContext;
  status?: ResourceStatus;
  /** 左栏：镜头导航 */
  navigator: React.ReactNode;
  /** 中栏：媒体舞台 */
  stage: React.ReactNode;
  /** 右栏：检查器（意图 / 参数 / 候选 / 参考） */
  inspector: React.ReactNode;
  className?: string;
}

export function AppShell({
  context,
  status,
  navigator,
  stage,
  inspector,
  className = '',
}: AppShellProps): JSX.Element {
  const [navOpen, setNavOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(false);

  return (
    <div className={`flex min-h-0 flex-col ${className}`}>
      <EpisodeContextBar
        context={context}
        status={status}
        navigatorOpen={navOpen}
        onToggleNavigator={() => setNavOpen((v) => !v)}
      />

      {/* ≥1440 三栏全展开；1280 检查器收窄；≤1024 单列 + 检查器转抽屉 */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 p-3 lg:grid-cols-[285px_1fr_350px]">
        {/* 左：导航器 */}
        <aside className={`min-h-0 ${navOpen ? '' : 'hidden lg:block'}`}>
          {navOpen && navigator}
        </aside>

        {/* 中：媒体舞台 */}
        <main className="min-h-[320px] min-w-0 rounded-lg border border-line bg-media p-3">
          {stage}
        </main>

        {/* 右：检查器。≤1024 转抽屉（用 lg:hidden 的固定底栏承载） */}
        <aside className="hidden min-h-0 lg:block">{inspector}</aside>

        <div className="fixed inset-x-0 bottom-0 z-drawer border-t border-line bg-surface p-2 lg:hidden">
          <button
            onClick={() => setInspectorOpen((v) => !v)}
            aria-expanded={inspectorOpen}
            className="control-compact w-full rounded border border-line text-sm text-ink-1"
          >
            {inspectorOpen ? '收起检查器' : '展开检查器'}
          </button>
        </div>
      </div>

      {/* ≤1024 的检查器抽屉内容 */}
      {inspectorOpen && (
        <div className="border-t border-line bg-surface p-3 lg:hidden">{inspector}</div>
      )}
    </div>
  );
}

export default AppShell;
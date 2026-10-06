import React, { useState, useEffect, useRef } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { useApp } from '@/context/AppContext';
import { t } from '@/i18n';
import { projectsApi, autopilotApi, chatApi, agentApi } from '@/api/client';
import { Button, Input, EmptyState, Skeleton } from '@/components/ui';
import {
  AlertTriangle, BarChart3, Check, CheckCircle2, Clapperboard, MessageSquare,
  Music, Network, Play, Share2, X, ZoomIn,
} from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';
import { Markdown } from '@/components/Markdown';
import { useComfyProgress } from '@/hooks/useComfyProgress';
import { getAgentSession, type ChatMsg } from '@/agentSession';
import { AutopilotPanel } from '@/components/AutopilotPanel';
import type { Project, AgentStep } from '@/types';
// 资产类型与取数已随 ADR-0010 的查询层搬走；壳只持有类型，不自己发请求
import type { ProjectAssets } from '@/api/queries';
// ---- 按域拆出的 Tab（ADR-0010 配套的 features/ 拆分）--------------------
// 页面壳只负责「装配 + 路由」：八个域统一从 `@/features/<域>` 取，
// 各自持有自己的取数 / 状态 / 轮询。
import { OverviewTab } from '@/features/overview/OverviewTab';
import { QcTab } from '@/features/qc/QcTab';
import { StoryboardHubTab } from '@/features/storyboard/StoryboardTab';
import { UpscaleTab } from '@/features/upscale/UpscaleTab';
import { AudioTab } from '@/features/audio';
import { OutputReviewTab } from '@/features/output';
import { RelationGraphTab } from '@/features/relation';
import { useProjectAssets, useAutopilotStatus } from '@/api/queries';
import { queryClient } from '@/api/queryClient';

// 焦点环：与 components/ui/index.tsx 里的 FOCUS_RING 逐字一致。
// index.css 有全局 :focus-visible outline 兜底，这里显式加 focus:outline-none 把它压掉，
// 否则 outline + ring 会叠成双环。凡因形状/类型原因换不成共享组件的原生控件，统一补这一串。
const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';


// ========== Workbench Tab Types ==========
// 注意：'chat' 已移除 —— AI 总控改成了右侧常驻面板，不再是标签页（见 ChatPanel）
type WorkbenchTab = 'overview' | 'autopilot' | 'storyboard' | 'qc' | 'upscale' | 'relation' | 'audio' | 'output';

interface ProjectWorkbenchPageProps {
  projectKey: string;
}

/**
 * 工作台外壳 —— 唯一持有 QueryClientProvider 的地方。
 *
 * 为什么 Provider 挂在这里，而不是 App.tsx / main.tsx
 * ---------------------------------------------
 * 那两个文件不在本轮可写清单内（`App.tsx` 与 `main.tsx` 明确禁改）。
 * 而 Provider 挂得越靠内，作用域越小、风险越低：工作台子树之外没有任何
 * query，不该被这套缓存策略影响。工作台本身又是全部轮询的所在地，
 * 因此这里就是作用域的**恰好正确边界**。
 *
 * ⚠️ client 必须用 `api/queryClient.ts` 里的**模块级单例**：
 *    若在组件内 `useState(() => new QueryClient())`，Provider 重挂会造出
 *    第二个 client，缓存分裂成两份，「跨 Tab 共享」直接失效 ——
 *    而共享正是这次改造的主要收益。
 */
export function ProjectWorkbenchPage(props: ProjectWorkbenchPageProps): JSX.Element {
  return (
    <QueryClientProvider client={queryClient}>
      <ProjectWorkbenchShell {...props} />
    </QueryClientProvider>
  );
}

/** 实际的工作台内容（壳 + 八域装配 + AI 总控常驻面板）。 */
function ProjectWorkbenchShell({ projectKey }: ProjectWorkbenchPageProps) {
  const { t } = useApp();
  const [project, setProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<WorkbenchTab>('overview');
  // AI 总控默认展开为右侧常驻面板（不占标签页）；用户可收起，收起后右侧只剩一个竖条按钮
  const [chatOpen, setChatOpen] = useState(true);

  // ⭐ 资产自动刷新（ADR-0010：原先是手写 setInterval，现收敛为 query）
  //    改造前这里是 `useState` + `setInterval(tick, 12000)` + focus/visibilitychange
  //    三个监听 + 一个 `alive` 标志，共 30 行、5 个易错点。现在由 query 统一负责：
  //      ① 12s 静默重拉（节奏不变）；窗口聚焦时立刻重拉（refetchOnWindowFocus）；
  //      ② 切 Tab 回来命中缓存，不再重拉 —— 改造前每次切 Tab 都要重新请求一遍；
  //      ③ 总览 / 交付 / 关系三个域共用同一份缓存，同一时刻只有一个请求在飞。
  //    ⚠️ 跨 Tab 共享资产时**刻意不开** keepPreviousData：切项目必须显示新项目的
  //       真实状态，拿上一个项目的资产数去展示新项目，比闪一下空态更糟。
  const assetsQuery = useProjectAssets(projectKey);
  const assets = (assetsQuery.data as ProjectAssets | null) ?? null;

  // 资产刷新失败**不阻塞页面**（与改造前 `reloadAssets` 内部的 catch 同一口径）
  const reloadAssets = React.useCallback(async () => {
    await assetsQuery.refetch();
  }, [assetsQuery]);

  // Load project data
  useEffect(() => {
    if (!projectKey) return;
    setLoading(true);
    projectsApi.get(projectKey)
      .then(d => setProject(d as Project))
      .catch(() => null)
      .finally(() => setLoading(false));
  }, [projectKey]);

  const tabs: { id: WorkbenchTab; icon: React.ReactNode; label: string }[] = [
    { id: 'overview', icon: <BarChart3 className="h-4 w-4" />, label: t('wb.overview') },
    // 托管生产（2026-10-07 恢复独立标签页）：这是系统**唯一**的 24/7 无人值守生产主路径。
    // 后端 autopilot.py 的守护线程 / 断点续跑 / 失败隔离 / 自检门禁本就完整，端点也全在，
    // 但前端一个调用点都没有（「自动生产」此前被移进 AI总控 聊天面板，等于主路径在界面上
    // 彻底消失）—— 用户不知道可以配置好计划后交给机器跑一夜，只能全程手动。
    // 放在概览之后：它是「生产方式」层面的选择，先于分镜/质检/导出这些具体环节。
    { id: 'autopilot', icon: <Play className="h-4 w-4" />, label: t('wb.autopilot') },
    // 分镜管理：合并 九宫格构图 + 关键帧生成 + 分镜序列 三个子标签（见 StoryboardHubTab）
    { id: 'storyboard', icon: <Clapperboard className="h-4 w-4" />, label: t('wb.storyboardHub') },
    { id: 'qc', icon: <CheckCircle2 className="h-4 w-4" />, label: t('wb.qc') },
    // 超分：后端 upscale_client 与其 8 个端点早已可用，但前端此前零引用 ——
    // 与已删除的孤儿页面同属「建好没入口」的能力，这里补上手工入口。
    { id: 'upscale', icon: <ZoomIn className="h-4 w-4" />, label: t('wb.upscale') },
    // 角色关系图：后端 API 早已完整实现，前端此前缺失可视化组件
    { id: 'relation', icon: <Network className="h-4 w-4" />, label: t('wb.relation') },
    // 声音处理：合并 TTS 配音 + 音画混音 + 音频质检
    { id: 'audio', icon: <Music className="h-4 w-4" />, label: t('wb.audio') },
    // 输出与验收：合并导出 + 成品验收
    { id: 'output', icon: <Share2 className="h-4 w-4" />, label: t('wb.output') },
    // AI总控 不再是标签页 —— 已改为右侧常驻面板（默认展开，见下方 ChatPanel）
  ];

  if (loading) return (
    // 骨架沿用真实内容的外层布局（左列 + 右侧常驻面板），避免「白屏 → 内容」的高度跳变
    <div className="fade-in" role="status" aria-live="polite" aria-label={t('common.loading')}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <div className="flex-1 min-w-0 space-y-4">
          <div className="flex items-center justify-between">
            <div className="min-w-0 space-y-2">
              <Skeleton className="h-7 w-48" />
              <Skeleton className="h-4 w-64" />
            </div>
            <Skeleton className="h-9 w-32" />
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-20 rounded-lg" />
            ))}
          </div>
          <div className="flex flex-wrap gap-2 border-b border-line pb-4">
            {[0, 1, 2, 3, 4, 5, 6].map((i) => (
              <Skeleton key={i} className="h-9 w-24 rounded-lg" />
            ))}
          </div>
          <Skeleton className="h-64 rounded-lg" />
        </div>
        <Skeleton className="min-h-[420px] w-full rounded-xl lg:h-[calc(100vh-7rem)] lg:w-[340px] lg:shrink-0" />
      </div>
    </div>
  );
  if (!project) return (
    <EmptyState
      icon={<AlertTriangle className="h-10 w-10" />}
      title={t('wb.projectNotFound')}
      description={projectKey}
    />
  );

  return (
    <div className="fade-in">
      {/* 主体：左列（项目头 + 统计 + 标签内容） + 右列「AI总控」常驻面板。
          头部与统计放进左列，右侧面板才能从顶部一直贯通到底部，不会变成悬空小盒。
          ⚠️ < lg 时改为上下堆叠：面板固定 340px，375 视口扣掉侧边栏后只剩 311px，
          横排必然把页面撑出横向滚动条。 */}
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <div className="flex-1 min-w-0 space-y-4">
          {/* Header */}
          <div className="flex items-center justify-between">
            <div>
              {/* 原 text-gradient（品牌→青渐变文字）已删：渐变标题在深浅两套
                  皮肤上都需要额外 fallback，且标题应有稳定的纯色底。
                  §5.3 要求删装饰性渐变，这里用语义色 ink-1 承接层级。 */}
              <h2 className="font-serif text-2xl font-bold text-ink-1">{project.name}</h2>
              <p className="text-sm text-ink-2 mt-1">
                {t('project.style')}: {project.config?.style} • {project.episode_count} {t('ep.suffix')}
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() => { window.location.hash = '#/projects'; }}
            >
              ← {t('wb.backToProjects')}
            </Button>
          </div>

          {/* Stats Bar —— 窄屏折成两行，避免 4 列挤压成一竖条（方案 P1-8） */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {[
              { label: t('wb.characters'), count: assets?.counts?.characters || 0, color: 'text-brand' },
              { label: t('wb.items'), count: assets?.counts?.items || 0, color: 'text-success-strong' },
              { label: t('wb.scenes'), count: assets?.counts?.scenes || 0, color: 'text-warning-strong' },
              { label: t('wb.storyboard'), count: assets?.counts?.storyboards || 0, color: 'text-info-strong' },
            ].map((stat) => (
              <div key={stat.label} className="bg-surface rounded-lg p-4 border border-line transition-all hover:-translate-y-0.5 hover:shadow-md hover:border-line-strong">
                <div className={`text-2xl font-bold tabular-nums ${stat.color}`}>{stat.count}</div>
                <div className="text-sm text-ink-2">{stat.label}</div>
              </div>
            ))}
          </div>

          {/* Tab Navigation */}
          <div className="flex flex-wrap gap-2 border-b border-line pb-4">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${FOCUS_RING} ${
                  activeTab === tab.id
                    ? 'bg-brand-subtle text-brand shadow-xs'
                    : 'text-ink-2 hover:bg-surface-2 hover:text-ink-1'
                }`}
              >
                <span className="flex shrink-0">{tab.icon}</span>
                <span>{tab.label}</span>
              </button>
            ))}
          </div>

          {/* Tab Content */}
          <div className="min-h-[400px]">
            {activeTab === 'overview' && (
              <OverviewTab
                assets={assets}
                projectKey={projectKey}
                novelId={project.novel_id}
                onRefreshAssets={reloadAssets}
              />
            )}
            {activeTab === 'autopilot' && (
              <AutopilotPanel projectKey={projectKey} />
            )}
            {activeTab === 'storyboard' && (
              <StoryboardHubTab projectKey={projectKey} novelId={project.novel_id} />
            )}
            {activeTab === 'qc' && (
              <QcTab projectKey={projectKey} />
            )}
            {activeTab === 'audio' && (
              <AudioTab projectKey={projectKey} />
            )}
            {activeTab === 'output' && (
              <OutputReviewTab projectKey={projectKey} assets={assets} />
            )}
            {activeTab === 'upscale' && (
              <UpscaleTab projectKey={projectKey} />
            )}
            {activeTab === 'relation' && (
              <RelationGraphTab projectKey={projectKey} />
            )}
          </div>
        </div>

        {/* AI总控：右侧常驻面板（默认展开，可收起为竖条） */}
        {chatOpen ? (
          <ChatPanel projectKey={projectKey} onClose={() => setChatOpen(false)} />
        ) : (
          <button
            onClick={() => setChatOpen(true)}
            title={t('wb.expandChat')}
            className={`sticky top-0 shrink-0 w-11 h-[calc(100vh-7rem)] min-h-[420px] flex flex-col items-center gap-3 py-4 rounded-xl border border-line bg-surface text-ink-2 hover:text-brand hover:border-brand transition-colors ${FOCUS_RING}`}
          >
            <span className="w-7 h-7 rounded-lg bg-brand-subtle flex items-center justify-center"><MessageSquare className="h-4 w-4" /></span>
            <span className="text-xs tracking-wide" style={{ writingMode: 'vertical-rl' }}>{t('wb.aiControl')}</span>
          </button>
        )}
      </div>
    </div>
  );
}
// ========== AI总控（项目内右侧常驻面板） ==========
// 原先它是工作台里的第 10 个标签页，排在最后、还会换行，用户反馈「进去后找不到了」。
// 现改为右侧常驻、可折叠：与标签内容并排，切换标签页时对话不丢失。
// 工具名 → 人话。用户在总控面板看到的应该是「生产一集」而不是 `produce_episode`。
// `t()` 找不到键时会**原样返回 key**，所以缺映射时回退到工具名本身（不显示 `toolLabel.xxx`）。
function toolLabel(t: (k: string, p?: Record<string, string | number>) => string, name: string): string {
  const key = `toolLabel.${name}`;
  const hit = t(key);
  return hit === key ? name : hit;
}

// 生产状态：让用户在总控面板里随时看到「现在在生成什么」。
//
// 背景：此前总控面板只显示「总控执行中 · 已完成 N 步」+ 工具名，用户完全不知道
// 后台正在拍哪一集、走到哪个环节。后端 `current` 里其实有完整的
// 集号 / 章节标题 / 阶段 / 百分比，这里把它拉到前端常驻展示。
//
// ⚠️ ADR-0010：原先这里是手写的自适应 setTimeout 链（有活 3s / 空闲 12s），
//    还有一个 `alive` 标志防卸载后 setState。现在整段收敛为 query：
//    自适应节奏由 `refetchInterval` 的函数式取值承担（按上一次结果决定下一次），
//    卸载即停是 query 的生命周期语义，不需要手动清理。
//
//    另一个收益：总览域的 `ProductionProgress` 也在轮询**同一个端点**，
//    改造前两处各自发请求（每 3 秒打两遍），现在共用一份缓存。
function useProductionStatus(projectKey: string) {
  // ⚠️ 返回类型刻意仍是 `any`：改造前这里是 `useState<any>`，下游读 `current.describe`
  //    而 `AutopilotCurrent` 类型里并没有这个字段（后端在用）。收紧它需要改共享的
  //    `AutopilotCurrent` 声明，而 `types/index.ts` 本轮只允许追加、不允许改既有声明，
  //    因此这一处保留原样，等下一个能改类型的批次处理。
  return (useAutopilotStatus(projectKey).data as any) ?? null;
}

// 正在生产的状态条 → 实时状态卡（合并了原先叠加的两条进度条）：
//   一行「正在生产」= 哪一集 / 哪一步 / 几成（/api/autopilot/status）；
//   一行「ComfyUI 采样」= 解析 comfyui 日志 tqdm 的 N/M 实时步数（如 10/19）。
// 有生产任务才轮询日志源，纯聊天时整卡不渲染。
function AgentStatusCard({ projectKey }: { projectKey: string }) {
  const current = useProductionStatus(projectKey);
  const comfy = useComfyProgress(!!current);

  if (!current && !comfy.active) return null;

  const pct = current ? Math.max(0, Math.min(100, Number(current.percent) || 0)) : 0;
  const stalled = current ? Number(current.step_stalled_sec) || 0 : 0;
  const stallMin = Math.floor(stalled / 60);

  return (
    <div
      className="glass-chrome mx-3 mt-3 shrink-0 space-y-2.5 rounded-lg border border-line p-3"
      role="status"
      aria-live="polite"
      aria-label={t('wb.productionProgress')}
    >
      {current && (
        <>
          <div className="flex items-center gap-2 text-[11px]">
            <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-brand" />
            <span className="shrink-0 text-ink-2">{t('chat.producingNow')}</span>
            <span className="truncate font-medium text-ink-1">
              {current.describe || current.message || ''}
            </span>
            <span className="ml-auto shrink-0 tabular-nums text-ink-2">{pct}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-surface-2">
            <div
              className="progress-fill h-full rounded-full transition-colors duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
          {stallMin >= 3 && (
            <div className="text-[10px] text-warning-strong">
              {t('chat.producingStalled', { m: stallMin })}
            </div>
          )}
        </>
      )}
      {comfy.active && comfy.total > 0 && (
        <div className="flex items-center gap-2 text-[11px]">
          <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-accent" />
          <span className="shrink-0 text-ink-2">{t('live.comfySampling')}</span>
          <span className="shrink-0 font-medium tabular-nums text-ink-1">
            {comfy.current}/{comfy.total}
          </span>
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-surface-2">
            <div
              className="progress-fill h-full rounded-full transition-colors duration-300"
              style={{ width: `${comfy.percent}%` }}
            />
          </div>
          <span className="shrink-0 tabular-nums text-ink-2">{comfy.percent}%</span>
        </div>
      )}
    </div>
  );
}

/** 总控执行轨迹：像 Agent 工作台一样把「它正在干什么」摊开 —— 一次工具调用一个节点
 *  （工具名徽标 + 耗时 + 结果摘要），执行中最新一步高亮、末尾挂「等待下一步」。
 *  live=true（执行中）始终展开、标题实时计时；job 结束后作为一条 run 消息留在
 *  对话里（默认展开、可收起）——此前 run 结束即被置 null，「它做过什么」无处可查。 */
function AgentTrace({ steps, status, startedAt, live = false }: {
  steps: AgentStep[];
  status: string;
  startedAt?: number;
  live?: boolean;
}) {
  const [open, setOpen] = useState(true);
  // 执行中每秒重渲染一次，让标题里的耗时走秒
  //
  // ⚠️ 这一处 `setInterval` **刻意保留**：它不是服务端状态轮询，而是纯客户端的
  //    「重渲染节拍」—— 数据（startedAt / status）由上方 query 提供，本定时器
  //    不发任何请求，只让 `Date.now()` 的差值每秒重新求值一次。
  //    把它塞进 query 反而是错的：query 的 refetch 会去重新取数，
  //    而这里要的是「不取数、只重画」。ADR-0010 收敛的是「取数」，
  //    不是「所有定时器」—— 两者混为一谈会让重渲染节拍变成每分钟一次请求。
  const [, tick] = useState(0);
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => tick(v => v + 1), 1000);
    return () => window.clearInterval(id);
  }, [live]);

  const running = status === 'running';
  const failed = steps.some((s) => !s.ok && !s.blocked);
  const elapsed = live && startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1000)) : null;
  const titleKey =
    running ? 'live.agentRunning'
    : status === 'failed' ? 'live.agentRunFailed'
    : status === 'killed' ? 'live.agentRunKilled'
    : status === 'timeout' ? 'live.agentTimeout'
    : 'live.agentRunDone';

  return (
    <div className={`mr-6 rounded-lg border bg-surface ${running ? 'border-brand/50' : 'border-line'}`}>
      <div className="flex items-center gap-2 px-3 py-2 text-xs">
        {running ? (
          <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-brand/40 border-t-brand" aria-hidden="true" />
        ) : (
          <span className={`h-2 w-2 shrink-0 rounded-full ${failed ? 'bg-danger' : 'bg-success'}`} aria-hidden="true" />
        )}
        <span className={`shrink-0 font-medium tabular-nums ${running ? 'text-brand' : 'text-ink-2'}`}>
          {t(titleKey, { n: steps.length, s: elapsed ?? 0 })}
        </span>
        {!running && (
          <button
            type="button"
            onClick={() => setOpen(v => !v)}
            aria-expanded={open}
            title={open ? t('live.collapse') : t('live.expand')}
            className={`ml-auto rounded p-0.5 text-ink-3 transition-colors hover:text-ink-1 ${FOCUS_RING}`}
          >
            <svg
              className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`}
              fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
        )}
      </div>
      {open && (
        <ol className="relative mx-3 mb-3 space-y-2 border-l border-line pl-3.5">
          {steps.map((s, i) => {
            const isCurrent = live && i === steps.length - 1;
            return (
              <li key={i} className="relative text-[11px] leading-snug">
                <span
                  className={`absolute -left-[22px] top-0 flex h-3.5 w-3.5 items-center justify-center rounded-full border bg-surface ${
                    s.blocked
                      ? 'border-warning/50 text-warning-strong'
                      : s.ok
                        ? 'border-success/50 text-success-strong'
                        : 'border-danger/50 text-danger-strong'
                  }`}
                  aria-hidden="true"
                >
                  {s.blocked
                    ? <AlertTriangle className="h-2.5 w-2.5" />
                    : s.ok
                      ? <Check className="h-2.5 w-2.5" />
                      : <X className="h-2.5 w-2.5" />}
                </span>
                <div className={isCurrent ? 'rounded-md bg-brand-subtle/40 px-1.5 py-1' : ''}>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="rounded bg-brand-subtle px-1.5 py-0.5 font-medium text-brand" title={s.tool}>
                      {toolLabel(t, s.tool)}
                    </span>
                    {s.elapsed_sec != null && (
                      <span className="tabular-nums text-ink-3">{t('live.elapsed', { s: Math.round(s.elapsed_sec) })}</span>
                    )}
                  </div>
                  <p className="mt-0.5 break-all text-ink-2">
                    {s.summary}
                    {s.cached ? t('chat.cached') : ''}
                  </p>
                </div>
              </li>
            );
          })}
          {running && (
            <li className="relative text-[11px] text-ink-3">
              <span
                className="absolute -left-[22px] top-0 flex h-3.5 w-3.5 items-center justify-center rounded-full border border-brand/40 bg-surface"
                aria-hidden="true"
              >
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />
              </span>
              {t('live.waitingNext')}
            </li>
          )}
        </ol>
      )}
    </div>
  );
}

function ChatPanel({ projectKey, onClose }: { projectKey: string; onClose: () => void }) {
  // 会话缓存（@/agentSession）：切菜单/收起面板时组件卸载，消息与进行中的 job
  // 存在模块级 session 里，重挂载时原样恢复并继续跟踪同一个 job。
  const [messages, setMessages] = useState<ChatMsg[]>(() => [...getAgentSession(projectKey).messages]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  // 自主执行模式：默认开启。指令交给总控模型自己决策并调用工具，全过程无需人工确认。
  const [autoMode, setAutoMode] = useState(true);
  const [run, setRun] = useState<{ steps: AgentStep[]; status: string; startedAt?: number } | null>(null);
  const [toolCount, setToolCount] = useState(0);
  const [killOn, setKillOn] = useState(false);
  const messagesEndRef = React.useRef<HTMLDivElement>(null);
  // 审计 P2-35（2026-09-29）：发送路径的 trackJob 此前没传取消守卫 —— 面板卸载后
  // 轮询最长还会空转 35 分钟并对已卸载组件 setState。与「恢复跟踪」路径同一口径：
  // 卸载即让位（session 里的 job 信息保留，重挂载的新实例接手）。
  const panelAliveRef = React.useRef(true);
  React.useEffect(() => {
    panelAliveRef.current = true;
    return () => { panelAliveRef.current = false; };
  }, []);

  // 拖拽调宽（2026-09-29 用户需求）：面板左缘手柄，按住左右拖改宽度，
  // 钳制 [300, 600]；松手落 localStorage 记住偏好；双击恢复默认 340。
  // 宽度经 CSS 变量 --chat-w 注入 lg:w-[var(--chat-w)] —— 小屏（<lg）本就
  // w-full 全宽堆叠，变量与手柄都不生效，行为零变化。
  const CHAT_W_DEFAULT = 340;
  const CHAT_W_MIN = 300;
  const CHAT_W_MAX = 600;
  const [chatW, setChatW] = useState<number>(() => {
    try {
      const v = Number(window.localStorage.getItem('mjscxt.chatPanelWidth'));
      if (Number.isFinite(v) && v >= CHAT_W_MIN && v <= CHAT_W_MAX) return Math.round(v);
    } catch { /* localStorage 不可用：用默认宽度 */ }
    return CHAT_W_DEFAULT;
  });
  const chatWRef = React.useRef(chatW);
  chatWRef.current = chatW;

  const onHandleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = chatWRef.current;
    const prevCursor = document.body.style.cursor;
    const prevSelect = document.body.style.userSelect;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    const onMove = (ev: MouseEvent) => {
      // 面板在右侧：往左拖（clientX 变小）= 变宽
      const next = Math.round(startW + (startX - ev.clientX));
      setChatW(Math.max(CHAT_W_MIN, Math.min(CHAT_W_MAX, next)));
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = prevCursor;
      document.body.style.userSelect = prevSelect;
      try { window.localStorage.setItem('mjscxt.chatPanelWidth', String(chatWRef.current)); } catch { /* ignore */ }
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  const onHandleDoubleClick = () => {
    setChatW(CHAT_W_DEFAULT);
    try { window.localStorage.setItem('mjscxt.chatPanelWidth', String(CHAT_W_DEFAULT)); } catch { /* ignore */ }
  };

  // 追加消息：session 是唯一真源，state 只是它的投影——组件卸载后 session 仍会更新，
  // 回来时轨迹不丢（直接写 setMessages 的话，卸载期间发生的事就没人记了）。
  const pushMsg = (m: ChatMsg) => {
    const s = getAgentSession(projectKey);
    s.messages = [...s.messages, m];
    setMessages(s.messages);
  };

  // 加载该项目的历史对话。会话缓存非空时直接还原（保留 run 轨迹与进行中的 job），
  // 不回后端重拉——重拉会把结构化轨迹洗掉；「刷新」按钮传 force 才真正重拉。
  const loadHistory = async (force = false) => {
    const s = getAgentSession(projectKey);
    if (!force && s.messages.length > 0) {
      setMessages([...s.messages]);
      return;
    }
    try {
      const d = await chatApi.history(projectKey);
      s.messages = (d.messages || []) as ChatMsg[];
      setMessages(s.messages);
    } catch (err) {
      console.error('加载对话历史失败:', err);
    }
  };

  useEffect(() => { void loadHistory(); }, [projectKey]);

  // 拉取总控可用工具数与急停状态（失败不影响对话，静默降级）
  useEffect(() => {
    agentApi.tools()
      .then((d) => { setToolCount(d.count || 0); setKillOn(!!d.kill?.on); })
      .catch(() => {});
  }, []);

  // 恢复跟踪：切菜单/收起面板前若有进行中的 job，回来后继续轮询同一个 job。
  // （StrictMode 双挂载/组件卸载时通过 cancelled 停掉旧循环，session 状态留给新实例）
  useEffect(() => {
    const s = getAgentSession(projectKey);
    if (!s.runningJobId) return;
    let cancelled = false;
    void trackJob(s.runningJobId, s.runningStartedAt ?? Date.now(), () => cancelled);
    return () => { cancelled = true; };
  }, [projectKey]);

  const toggleKill = async () => {
    try {
      const d = await agentApi.setKill(!killOn, !killOn ? '前端手动急停' : '');
      setKillOn(!!d.kill?.on);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('chat.killFailed'));
    }
  };

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  /** 轮询一个总控 job 到结束：实时刷新 run（AgentTrace 时间线），结束后把轨迹与回复落进对话。
   *  isCancelled=组件卸载/重挂载时停掉旧循环——不清 session 状态，重挂载的新实例会接手。 */
  const trackJob = async (jobId: string, startedAt?: number, isCancelled?: () => boolean) => {
    const s = getAgentSession(projectKey);
    s.runningJobId = jobId;
    s.runningStartedAt = startedAt ?? s.runningStartedAt ?? Date.now();
    setSending(true);
    setRun({ steps: [], status: 'running', startedAt: s.runningStartedAt });
    let failures = 0;
    const deadline = Date.now() + 35 * 60 * 1000; // 兜底，避免异常时永久轮询
    for (;;) {
      await new Promise(r => setTimeout(r, 1200));
      if (isCancelled?.()) return; // 旧实例让位：session 里的 job 信息由重挂载的新实例接手
      let job;
      try {
        job = await agentApi.job(jobId);
        failures = 0;
      } catch {
        failures += 1;
        // 后端连续不可达（重启中/挂了）：停止跟踪，避免空转到 35 分钟兜底
        if (failures >= 5) { setError(t('live.pollFailed')); break; }
        if (Date.now() > deadline) break;
        continue;
      }
      setRun({ steps: job.steps || [], status: job.status || 'running', startedAt: s.runningStartedAt ?? undefined });
      if (job.status !== 'running') {
        pushMsg({ role: 'assistant', kind: 'run', steps: job.steps || [], status: job.status || 'done', timestamp: new Date().toISOString() });
        if (job.reply) {
          pushMsg({ role: 'assistant', content: job.reply, timestamp: new Date().toISOString() });
        } else if (job.error) {
          setError(job.error);
        }
        break;
      }
      if (Date.now() > deadline) { setError(t('chat.timeout')); break; }
    }
    s.runningJobId = null;
    s.runningStartedAt = null;
    setRun(null);
    setSending(false);
  };

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setSending(true);
    setError('');
    // 乐观渲染用户消息
    pushMsg({ role: 'user', content: text, timestamp: new Date().toISOString() });
    setInput('');
    try {
      // 纯聊天模式：走老链路，只做对话 + 抽取创作设定
      if (!autoMode) {
        const data = await chatApi.send(text, projectKey);
        if (data.success && data.reply) {
          pushMsg({ role: 'assistant', content: data.reply, timestamp: new Date().toISOString() });
        } else {
          setError(t('chat.noReply'));
        }
        return;
      }

      // 自主执行模式：下发任务 → trackJob 轮询 → 时间线摊开「它自己做了什么」
      const started = await agentApi.send(text, projectKey);
      if (!started.success || !started.job_id) {
        setError(t('chat.startFailed'));
        return;
      }
      await trackJob(started.job_id, Date.now(), () => !panelAliveRef.current);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('chat.sendFailed'));
      const s = getAgentSession(projectKey);
      s.runningJobId = null;
      s.runningStartedAt = null;
      setRun(null);
    } finally {
      setSending(false);
    }
  };

  return (
    <aside
      // h-[calc(100vh-7rem)] = 视口高 −（顶栏 ~63px + main 上下 padding 48px），
      // 让面板与左列内容等高、上下贯通；sticky 使其随页面滚动保持停靠。
      // glass-chrome：半透明 + 背景模糊，**仅限常驻 chrome**（原 `.glass` 已降级
      // 重命名，见 index.css §5.3）。长列表卡片不可用 backdrop-blur：滚动时逐帧
      // 重绘会把帧率拖垮。
      // 2026-09-29：宽度可拖拽 —— lg 宽度由 CSS 变量 --chat-w 注入（左缘手柄拖动
      // 调节，双击恢复 340，偏好落 localStorage）；小屏 <lg 仍 w-full 全宽堆叠。
      className="glass-chrome relative flex w-full flex-col overflow-hidden rounded-xl border border-line lg:sticky lg:top-0 lg:h-[calc(100vh-7rem)] lg:w-[var(--chat-w)] lg:shrink-0 min-h-[420px]"
      style={{ '--chat-w': `${chatW}px` } as React.CSSProperties}
    >
      {/* 拖拽调宽手柄：贴左缘 6px 竖条，hover 高亮；小屏堆叠全宽无意义 → hidden，lg 才显示 */}
      <div
        onMouseDown={onHandleMouseDown}
        onDoubleClick={onHandleDoubleClick}
        title={t('chat.resizeHint')}
        className="absolute left-0 top-0 z-10 hidden h-full w-1.5 cursor-col-resize bg-transparent transition-colors hover:bg-brand/40 lg:block"
      />
      {/* 头部：与工作台其他面板一致的白底 + 灰边 + indigo 强调 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-line shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="w-7 h-7 rounded-lg bg-brand-subtle flex items-center justify-center shrink-0">
            <MessageSquare className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <h3 className="font-semibold text-ink-1 leading-tight">{t('chat.panelTitle')}</h3>
            <div className="flex items-center gap-1.5 mt-0.5">
              <p className="text-[11px] text-ink-2 leading-tight">
                {autoMode ? t('chat.autoExec', { n: toolCount || '…' }) : t('chat.chatOnly')}
              </p>
              <button
                onClick={() => setAutoMode(v => !v)}
                title={autoMode ? t('chat.switchToChat') : t('chat.switchToAuto')}
                className={`text-[10px] leading-none px-1.5 py-0.5 rounded border transition-colors ${FOCUS_RING} ${
                  autoMode
                    ? 'border-brand/30 text-brand bg-brand-subtle'
                    : 'border-line text-ink-2'
                }`}
              >
                {autoMode ? t('chat.auto') : t('chat.chat')}
              </button>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-0.5 shrink-0">
          {autoMode && (
            <button
              onClick={toggleKill}
              title={killOn ? t('chat.releaseKill') : t('chat.kill')}
              className={`p-1.5 rounded-lg transition-colors ${FOCUS_RING} ${
                killOn
                  ? 'text-danger bg-danger-subtle'
                  : 'text-ink-3 hover:text-danger hover:bg-danger-subtle'
              }`}
            >
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            </button>
          )}
          <Button variant="ghost" size="sm" onClick={loadHistory} title={t('chat.refreshHistory')}>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </Button>
          <Button variant="ghost" size="sm" onClick={onClose} title={t('chat.collapse')}>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" />
            </svg>
          </Button>
        </div>
      </div>

      {/* 实时状态卡：正在生产（哪一集/哪一步/几成）+ ComfyUI 采样进度（如 10/19）。
          此处原先是两条几乎相同的进度条叠加，已合并为一张卡。 */}
      <AgentStatusCard projectKey={projectKey} />

      {error && (
        <div className="mx-3 mt-3 p-2 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-xs shrink-0">
          {error}
        </div>
      )}

      {/* 消息区：浅色底以区别于面板头部/输入区，形成「对话」区域感。
          注意：空态与消息列表要二选一渲染 —— 若把滚动哨兵 <div> 和 h-full 的空态
          放在同一个 space-y-3 容器里，哨兵会额外吃到 12px margin 而撑出滚动条。 */}
      <div className="flex-1 min-h-0 overflow-y-auto bg-surface-2">
        {messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center px-4">
            <EmptyState
              icon={<MessageSquare className="h-10 w-10" />}
              title={autoMode ? t('chat.autoEmptyTitle') : t('chat.chatEmptyTitle')}
              description={autoMode ? t('chat.autoEmptyDesc') : t('chat.chatEmptyDesc')}
            />
            <div className="w-full space-y-1.5">
              {(autoMode
                ? [t('chat.suggestAuto1'), t('chat.suggestAuto2'), t('chat.suggestAuto3'), t('chat.suggestAuto4')]
                : [t('chat.suggestChat1'), t('chat.suggestChat2')]
              ).map((ex) => (
                <button
                  key={ex}
                  onClick={() => setInput(ex)}
                  className={`w-full text-left text-xs px-3 py-2 rounded-lg bg-surface border border-line text-ink-2 hover:border-brand hover:text-brand transition-colors ${FOCUS_RING}`}
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="p-3 space-y-3">
            {messages.map((msg: ChatMsg, idx: number) =>
              msg.kind === 'run' ? (
                <AgentTrace key={idx} steps={msg.steps || []} status={msg.status || 'done'} />
              ) : (
                <div
                  key={idx}
                  className={`px-3 py-2 rounded-lg text-sm ${
                    msg.role === 'user'
                      ? 'bg-brand text-white ml-6 rounded-br-sm'
                      : 'bg-surface border border-line text-ink-1 mr-6 rounded-bl-sm'
                  }`}
                >
                  {/* AI 回复按 Markdown 渲染（2026-10-07）：总控模型返回的是 Markdown
                      源码，此前原样当纯文本吐给用户，满屏的 ## 和 ** 。用户自己发的气泡
                      仍是纯文本 —— 那是输入，不是展示内容，且白底反白底时行内代码/表格
                      的配色会跟气泡打架。 */}
                  {msg.role === 'user' ? (
                    <p className="whitespace-pre-wrap break-words">{msg.content}</p>
                  ) : (
                    <Markdown>{msg.content || ''}</Markdown>
                  )}
                </div>
              )
            )}
            {/* 自主执行过程（实时）：正在跑的 job 摊开在对话流里，像 Agent 工作台一样看它干活 */}
            {run && <AgentTrace steps={run.steps} status={run.status} startedAt={run.startedAt} live />}
            {sending && !run && (
              <div className="bg-surface border border-line mr-6 px-3 py-2 rounded-lg text-sm text-ink-2 flex items-center gap-2">
                <span className="w-3 h-3 border-2 border-brand/40 border-t-brand rounded-full animate-spin inline-block" />
                {t('chat.thinking')}
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* 输入区 */}
      <div className="p-3 border-t border-line shrink-0 bg-surface">
        <div className="flex gap-2">
          <Input
            value={input}
            onChange={setInput}
            onEnter={sendMessage}
            placeholder={t('chat.panelPlaceholder')}
            className="flex-1 min-w-0"
          />
          <Button
            variant="brand"
            onClick={sendMessage}
            disabled={sending || !input.trim()}
            className="shrink-0"
          >
            {t('chat.send')}
          </Button>
        </div>
      </div>
    </aside>
  );
}

/**
 * 项目 → 季 → 集 → 镜 面包屑 + 队列 / GPU / 磁盘状态（MASTER §3 常驻条）。
 *
 * 为什么这一条必须**常驻**
 * ----------------------
 * 工作台有八���域，每一域都会改变「当前上下文」。上下文不在视野里时，
 * 用户会以为自己还在第 12 镜，实际正在改第 37 镜 —— 而短剧的返工成本极高。
 *
 * ⚠️ 三类状态都要**显式呈现**，不许静默：
 *    - GPU：超分/出图正在用卡时必须看得见（否则用户以为没在跑，又点一次提交）
 *    - 队列：排队任务数（否则用户不知道为什么还没开始）
 *    - 磁盘：产物落盘是本项目的产物，改一次重跑就要占空间
 * 拿不到状态时显示「未知」而不是隐藏或留空 —— invariant 1：
 * Never silently fall back。
 */
import React from 'react';
import { t } from '@/i18n';

export interface EpisodeContext {
  projectKey: string;
  /** 季（部分项目无季概念，可空） */
  season?: string | null;
  episodeNo?: number | null;
  shotSeq?: number | null;
}

export interface ResourceStatus {
  /** 队列中的任务数 */
  queueDepth?: number | null;
  /** GPU 是否在忙 */
  gpuBusy?: boolean | null;
  /** GPU 型号（可选） */
  gpuName?: string | null;
  /** 磁盘占用（可选，已格式化） */
  diskUsed?: string | null;
}

export interface EpisodeContextBarProps {
  context: EpisodeContext;
  status?: ResourceStatus;
  /** 导航器是否展开（决定左栏宽度） */
  navigatorOpen?: boolean;
  onToggleNavigator?: () => void;
  /**
   * 上下文条右侧的**动作插槽**（放在状态区左边）。
   *
   * 为什么需要它：决策态徽标（ADR-0002 的唯一渲染入口）必须与
   * 「项目 → 季 → 集 → 镜」同处一条常驻带 —— 用户在第 37 镜看到
   * 「已采用·未批准」，才有办法确认自己看的是哪一镜的决定。
   * 但决策态**不属于**上下文本身（它是产物状态，不是位置状态），
   * 所以不塞进 EpisodeContext，由调用方经本插槽注入。
   */
  actions?: React.ReactNode;
  className?: string;
}

export function EpisodeContextBar({
  context,
  status,
  navigatorOpen,
  onToggleNavigator,
  actions,
  className = '',
}: EpisodeContextBarProps): JSX.Element {
  const { projectKey, season, episodeNo, shotSeq } = context;

  return (
    // ⚠️ sticky 必须用负 margin 抵消父级 <main class="p-6"> 的 24px padding。
    //   top-0 是相对**滚动容器**（main）算的，而 sticky 元素仍被父级 padding 框住，
    //   于是它会停在距容器顶 24px 处 —— Navbar 与本条之间露出 24px 正在滚过的
    //   内容，glass-chrome 的 backdrop-blur 会把它糊成一条脏带。
    //   -mx-6 -mt-6 + px-6 pt-2 = 视觉上仍然对齐，但盒子铺满整个滚动宽度。
    //   （App.tsx 的 main 已加 overflow-x-hidden 兜住横向溢出。）
    <div
      className={`glass-chrome sticky -mx-6 -mt-6 top-0 z-sticky flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-6 py-2 text-sm ${className}`}
    >
      {onToggleNavigator && (
        <button
          type="button"
          onClick={onToggleNavigator}
          aria-expanded={navigatorOpen}
          aria-label={navigatorOpen ? t('ctx.collapseNavigator') : t('ctx.expandNavigator')}
          // 纯图标按钮触达区 ≥ 44×44（MASTER §5 无障碍）
          className="flex h-hit-target w-hit-target shrink-0 items-center justify-center rounded text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
        >
          <span aria-hidden="true" className="font-mono text-sm">☰</span>
        </button>
      )}

      {/* 面包屑：集号 / 镜号用等宽数字，便于逐位核对 */}
      <nav aria-label={t('ctx.navLabel')} className="flex min-w-0 flex-wrap items-center gap-1.5">
        <Crumb label={t('common.project')}>{projectKey}</Crumb>
        {season != null && <><Sep /><Crumb label={t('ctx.season')}>{season}</Crumb></>}
        <Sep />
        <Crumb label={t('common.episode')}>
          <span className="font-mono tabular-nums">
            {episodeNo != null ? t('wb.episodeNo', { n: episodeNo }) : t('ctx.noEpisode')}
          </span>
        </Crumb>
        <Sep />
        <Crumb label={t('common.shot')}>
          <span className="font-mono tabular-nums">
            {shotSeq != null ? t('ctx.shotN', { n: shotSeq }) : t('ctx.noShot')}
          </span>
        </Crumb>
      </nav>

      {/* ---- 资源状态：未知就写「未知」，不留空、不隐藏 ---- */}
      <div className="ml-auto flex flex-wrap items-center gap-3 text-xs">
        {actions}
        <StatusChip
          label={t('ctx.queue')}
          value={status?.queueDepth != null ? String(status.queueDepth) : t('common.unknown')}
        />
        <StatusChip
          label="GPU"
          value={
            status?.gpuBusy == null
              ? t('common.unknown')
              : status.gpuBusy
                ? (status.gpuName
                  ? t('ctx.gpuBusyNamed', { gpu: status.gpuName })
                  : t('ctx.gpuBusy'))
                : t('ctx.gpuIdle')
          }
          // 色 + 文本 + 图标三重表达（MASTER §4.2）：不能只靠颜色
          glyph={status?.gpuBusy == null ? '?' : status.gpuBusy ? '▲' : '○'}
          tone={status?.gpuBusy == null ? 'unknown' : status.gpuBusy ? 'busy' : 'idle'}
        />
        <StatusChip
          label={t('ctx.disk')}
          value={status?.diskUsed ?? t('common.unknown')}
        />
      </div>
    </div>
  );
}

function Crumb({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <span className="flex min-w-0 items-center gap-1">
      <span className="text-ink-3">{label}</span>
      {/* max-w-measure 防超长项目名把状态区挤出视野 */}
      <span className="max-w-measure truncate text-ink-1">{children}</span>
    </span>
  );
}

function Sep(): JSX.Element {
  return <span aria-hidden="true" className="text-ink-3">/</span>;
}

type Tone = 'idle' | 'busy' | 'unknown';

// ⚠️ 必须用 `-strong` 档，不能用主档：主档是**饱和色相**（emerald-500 / cyan-500），
//    浅色主题下压在白底上只有 2.54:1 / 2.43:1 —— 这是**常驻**状态条，
//    任何语言、任何页面都会露馅。-strong 是按各主题背景配过的深字档。
const TONE_CLS: Record<Tone, string> = {
  idle: 'text-state-done-strong',
  busy: 'text-state-running-strong',
  unknown: 'text-ink-3',
};

function StatusChip({
  label,
  value,
  glyph,
  tone = 'idle',
}: {
  label: string;
  value: string;
  glyph?: string;
  tone?: Tone;
}): JSX.Element {
  return (
    <span className="flex items-center gap-1 whitespace-nowrap">
      <span className="text-ink-3">{label}</span>
      {glyph && <span aria-hidden="true" className={`font-mono ${TONE_CLS[tone]}`}>{glyph}</span>}
      <span className={`tabular-nums ${TONE_CLS[tone]}`}>{value}</span>
    </span>
  );
}

export default EpisodeContextBar;
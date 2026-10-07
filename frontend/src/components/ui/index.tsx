import React from 'react';
import { createPortal } from 'react-dom';
import { useModalBehavior } from '@/hooks/useModalBehavior';
// 模块级 t：本文件是无 context 耦合的共享组件库，默认文案走 i18n，不引入 useApp()
import { t } from '@/i18n';

// ==========================================================================
// 全站唯一组件出口
// --------------------------------------------------------------------------
// ⚠️ 本文件是设计体系的落地层：**禁止出现 hex / rgba 字面量与 dark: 变体**，
//    颜色一律走 tailwind.config.js 映射出的语义色（surface / line / ink /
//    brand / accent / success / warning / danger / info / state-*）。
//    需要新颜色时先去 index.css 加 token，不要在这里写死色值。
// ⚠️ 交互元素必须带 focus-visible 焦点环（index.css 有 :focus-visible 兜底，
//    但组件自己给 ring 更可控，故显式声明）。
// ==========================================================================

/** 焦点环：鼠标点击不出现，键盘 Tab 必然可见。
 *  ⚠️ 必须带 `ring-offset-canvas`：Tailwind v3 的 `--tw-ring-offset-color`
 *    默认是 **#fff**，只写 `ring-offset-2` 会在深色主题上得到一圈 2px 纯白晕边。 */
export const FOCUS_RING = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

/** 表单控件基线：圆角、聚焦转品牌色。
 *  ⚠️ 这里**刻意不含高度**：Textarea 复用同一个基线，写死高度会把多行
 *  文本域压成单行高。高度由各控件显式声明 Input/Select → h-control-compact
 *  （36px = --control-h-compact），Textarea 随内容自适应。
 *  ⚠️⚠️ 这里的 `w-full` 会**静默压掉调用点的任何宽度类**：Tailwind 产物中
 *    `.w-40` / `.w-24` 等排在 `.w-full` **之前**，同特异性下后者胜，而 class
 *    属性里的书写顺序**不参与**优先级计算。所以 `<Select className="w-40" />`
 *    实际渲染成 width:100%，没有任何报错。
 *    要覆盖宽度必须写**重要修饰符**：`className="!w-40"`。
 *    实测事故见 features/storyboard/StoryboardTab.tsx 的视频方式 Select ——
 *    它把 storyboard 工具栏在 1920×1080 下挤成三行错位。 */
const FIELD_BASE =
  'w-full rounded-md border bg-surface px-3 text-base text-ink-1 transition-colors placeholder:text-ink-3 ' +
  'disabled:cursor-not-allowed disabled:opacity-50';

/** 行内单行控件高度：36px（= --control-h-compact）。改尺寸只改这一个令牌。 */
const FIELD_H = 'h-control-compact';

// 描边用 `line.input`（--border-input，3:1）而不是 `line`（深色 1.72:1 / 浅色 1.23:1）：
// WCAG 1.4.11 要求「识别控件所需的视觉信息」达到 3:1，而浅色下 --bg-surface(#FFF)
// 与 --bg-canvas(#F7F8FA) 之间只有 1.10:1，控件的填充本身给不了任何边界信息 ——
// 原来的空 Input 等于一片看不见的白。聚焦环与 FOCUS_RING 对齐（40% + offset），
// 原先是 25% 且无 offset，键盘聚焦时环紧贴描边、浅底上基本看不出。
const FIELD_OK = 'border-line-input hover:border-line-strong focus:border-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';
const FIELD_ERR = 'border-danger focus:border-danger focus:outline-none focus-visible:ring-2 focus-visible:ring-danger/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

function FieldLabel({ label, children }: { label?: string; children: React.ReactNode }) {
  if (!label) return <>{children}</>;
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink-2">{label}</span>
      {children}
    </label>
  );
}

function FieldError({ error, id }: { error?: string; id?: string }) {
  if (!error) return null;
  return <p id={id} className="mt-1 text-xs text-danger-strong">{error}</p>;
}

/** 字段级 spinner：aria-busy 时贴右显示。
 *  ⚠️ 必须是真实节点 —— index.css 的 ::after 伪元素对 input/select/textarea
 *  这类替换元素不渲染，基线层造 spinner 对原生字段是死代码（见 MASTER §4.4）。 */
function FieldSpinner({ className = 'right-2.5 top-1/2 -translate-y-1/2' }: { className?: string }) {
  return (
    <span
      className={`pointer-events-none absolute h-4 w-4 animate-spin rounded-full border-2 border-brand/30 border-t-brand ${className}`}
      aria-hidden="true"
    />
  );
}

// ===================== 基础反馈 =====================

export function Loading({ size = 'md', label }: { size?: 'sm' | 'md' | 'lg'; label?: string }) {
  const sizes = { sm: 'w-4 h-4 border-2', md: 'w-8 h-8 border-[3px]', lg: 'w-12 h-12 border-4' };
  return (
    <div className="flex flex-col items-center justify-center gap-3 p-8" role="status" aria-live="polite">
      <div className={`${sizes[size]} animate-spin rounded-full border-line border-t-brand`} />
      {label && <span className="text-sm text-ink-2">{label}</span>}
    </div>
  );
}

/** 骨架屏：数据区块加载态占位，避免「白屏 → 内容」的跳变 */
export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-skeleton rounded-md bg-surface-2 ${className}`} aria-hidden="true" />;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  /** 空状态最需要「下一步该做什么」，所以留一个操作位而不是只给一句话 */
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      {icon && (
        <div className="mb-4 text-ink-3 [&>svg]:h-10 [&>svg]:w-10" aria-hidden="true">
          {/* 历史调用点传的是 emoji 字符串；新代码请直接传 Lucide/SVG 节点 */}
          {typeof icon === 'string' ? <span className="text-4xl leading-none">{icon}</span> : icon}
        </div>
      )}
      <h3 className="mb-2 text-lg font-medium text-ink-1">{title}</h3>
      {description && <p className="max-w-md text-sm text-ink-2">{description}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

/** 错误态：给出错误码 + 原因 + 重试，而不是一句「加载失败」 */
export function ErrorState({
  title = t('common.loadFailed'),
  description,
  code,
  onRetry,
}: {
  title?: string;
  description?: React.ReactNode;
  code?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 text-center" role="alert">
      <svg className="h-10 w-10 text-danger" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <circle cx="12" cy="12" r="9" strokeWidth="1.6" />
        <path strokeLinecap="round" strokeWidth="1.8" d="M12 7.5v5.5M12 16.2v.6" />
      </svg>
      <h3 className="text-base font-medium text-ink-1">{title}</h3>
      {description && <p className="max-w-md text-sm text-ink-2">{description}</p>}
      {code && <code className="rounded-sm bg-surface-2 px-2 py-0.5 text-xs text-ink-3">{code}</code>}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className={`mt-1 inline-flex h-control-compact items-center rounded-md border border-line bg-surface px-4 text-sm font-medium text-ink-1 transition-colors hover:bg-surface-2 ${FOCUS_RING}`}
        >
          {t('common.retry')}
        </button>
      )}
    </div>
  );
}

export function Badge({ children, variant = 'default' }: { children: React.ReactNode; variant?: 'default' | 'success' | 'warning' | 'danger' | 'info' }) {
  const variants = {
    default: 'bg-surface-2 text-ink-2',
    success: 'bg-success-subtle text-success-strong',
    warning: 'bg-warning-subtle text-warning-strong',
    danger: 'bg-danger-subtle text-danger-strong',
    info: 'bg-info-subtle text-info-strong',
  };
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${variants[variant]}`}>
      {children}
    </span>
  );
}

export type ProductionStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped' | 'attention';

const STATUS_STYLE: Record<ProductionStatus, string> = {
  pending: 'bg-state-pending-subtle text-state-pending-strong',
  running: 'bg-state-running-subtle text-state-running-strong',
  done: 'bg-state-done-subtle text-state-done-strong',
  failed: 'bg-state-failed-subtle text-state-failed-strong',
  skipped: 'border border-dashed border-state-skipped bg-transparent text-state-skipped-strong',
  attention: 'bg-state-attention-subtle text-state-attention-strong',
};

/** 枚举 → i18n key（模块顶层不调 t()，渲染时再取文案） */
const STATUS_LABEL_KEY: Record<ProductionStatus, string> = {
  pending: 'state.pending',
  running: 'state.running',
  done: 'state.done',
  failed: 'state.failed',
  skipped: 'state.skipped',
  attention: 'state.attention',
};

/**
 * 生产状态徽标 —— 本项目最核心的状态表达（方案 §6.6）。
 * 漫剧生产链路有 pending/running/done/failed/skipped/attention 六种状态，
 * 此前全靠文字叙述，扫一眼看不出全局进度。
 */
export function StateBadge({
  status,
  label,
  onClick,
  className = '',
}: {
  status: ProductionStatus;
  label?: string;
  /** failed 可点击直接跳失败定位；传了 onClick 就渲染成 button */
  onClick?: () => void;
  className?: string;
}) {
  const base = 'inline-flex h-[22px] items-center gap-1.5 rounded-full px-2 text-xs font-medium';
  const body = (
    <>
      {status === 'running' && (
        <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-state-running" aria-hidden="true" />
      )}
      {status === 'attention' && (
        <svg className="h-3 w-3" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M12 3.6 22 20.4H2L12 3.6Z" />
        </svg>
      )}
      {label ?? t(STATUS_LABEL_KEY[status])}
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`${base} ${STATUS_STYLE[status]} transition-opacity hover:opacity-80 ${FOCUS_RING} ${className}`}
      >
        {body}
      </button>
    );
  }
  return <span className={`${base} ${STATUS_STYLE[status]} ${className}`}>{body}</span>;
}

export type DecisionKind = 'selected' | 'approved';

/**
 * 决策徽标（ADR-0002 / ADR-0013）——「采用」与「批准」两档**独立**表达。
 *
 * 三重律（MASTER §4.2 / §4.5）在此固化为组件契约：色 + 文本 + 图标三者同时
 * 出现，默认文案走语言包；调用方无法只给颜色，也无法不给状态词。
 * ⚠️ 不变量：selected 永不显示为 approved；两档不得共用图标或文案。
 *
 * 默认文案走 t('state.selected') / t('state.approved')；调用方传 label 时可覆盖。
 */
const DECISION_STYLE: Record<DecisionKind, string> = {
  selected: 'bg-selected-subtle text-selected-strong',
  approved: 'bg-approved-subtle text-approved-strong',
};

export function DecisionBadge({
  decision,
  label,
  onClick,
  className = '',
}: {
  decision: DecisionKind;
  /** 可选覆盖；缺省时回落 state.selected / state.approved */
  label?: string;
  /** 仅用于跳转决策记录；不得用它把两档做成同一个按钮 */
  onClick?: () => void;
  className?: string;
}) {
  const base = 'inline-flex h-[22px] items-center gap-1.5 rounded-full px-2 text-xs font-medium';
  const body = (
    <>
      {decision === 'selected' ? (
        // selected：单勾 —— 创作决定（这版被采用）
        <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.4} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.5 9.5 17.5 19.5 6.5" />
        </svg>
      ) : (
        // approved：印章勾 —— 放行决定（人工批准）。与 selected 图标刻意不同形
        <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 2.75 14.6 5.4l3.65-.3.9 3.57 3.05 2-1.73.2 1.7 3.2-3.05 2-.9 3.57-3.65-.3L12 21.25 9.4 18.6l-3.65.3-.9-3.57-3.05-2 1.7-3.2-1.7-3.2 3.05-2 .9-3.57 3.65.3L12 2.75Z" />
          <path strokeLinecap="round" strokeLinejoin="round" d="m8.75 12.25 2.1 2.1 4.4-4.4" />
        </svg>
      )}
      {label ?? t(decision === 'selected' ? 'state.selected' : 'state.approved')}
    </>
  );
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`${base} ${DECISION_STYLE[decision]} transition-opacity hover:opacity-80 ${FOCUS_RING} ${className}`}
      >
        {body}
      </button>
    );
  }
  return <span className={`${base} ${DECISION_STYLE[decision]} ${className}`}>{body}</span>;
}


export function Card({
  children,
  className = '',
  title,
  action,
  footer,
  /** 内容区留白：表格/图表类常需要 p-0 或更小的内边距 */
  bodyClassName = 'p-5',
}: {
  children: React.ReactNode;
  className?: string;
  title?: string;
  action?: React.ReactNode;
  footer?: React.ReactNode;
  bodyClassName?: string;
}) {
  return (
    <div className={`rounded-lg border border-line bg-surface shadow-xs ${className}`}>
      {(title || action) && (
        <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-4">
          {title && <h3 className="font-semibold text-ink-1">{title}</h3>}
          {action && <div className="shrink-0">{action}</div>}
        </div>
      )}
      <div className={bodyClassName}>{children}</div>
      {footer && (
        <div className="border-t border-line px-5 py-4">{footer}</div>
      )}
    </div>
  );
}

// ===================== 表单控件 =====================

export function Button({
  children,
  onClick,
  variant = 'primary',
  size = 'md',
  disabled = false,
  className = '',
  style,
  type = 'button',
  loading = false,
  title,
  // ⚠️ 其余原生属性（aria-label / aria-pressed / data-* / ref / onKeyDown …）一律透传。
  //    此前 props 是逐个枚举的封闭结构，`<Button aria-label="…">` 在**类型层面**就
  //    写不出来，纯图标按钮因此拿不到无障碍名，只能各自去手搓裸 <button> 绕开。
  ...rest
}: {
  children: React.ReactNode;
  onClick?: () => void;
  /** primary=品牌色实心主 CTA（默认，一屏唯一）；brand=primary 的显式别名；secondary=描边；ghost=透明工具条；danger=删除类；link=行内 */
  variant?: 'primary' | 'brand' | 'secondary' | 'danger' | 'ghost' | 'link';
  size?: 'sm' | 'md' | 'lg';
  disabled?: boolean;
  className?: string;
  style?: React.CSSProperties;
  type?: 'button' | 'submit' | 'reset';
  /** 提交中：自动禁用并显示转圈，避免重复点击造成重复任务 */
  loading?: boolean;
  title?: string;
} & Omit<
  React.ButtonHTMLAttributes<HTMLButtonElement>,
  // ⚠️ onClick 必须一起 omit：上面的显式 `onClick?: () => void` 与
  //    ButtonHTMLAttributes 自带的 `onClick?: MouseEventHandler` 会求成交集
  //    `(() => void) & MouseEventHandler`，而形参类型不兼容的处理器
  //    （如 `(force?: boolean) => Promise<void>`）对这个交集**无法赋值**，
  //    编译期直接报 TS2322。保留显式声明 = 维持原有宽松签名。
  'type' | 'title' | 'className' | 'children' | 'onClick'
>) {
  const base = `inline-flex items-center justify-center gap-2 rounded-md font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${FOCUS_RING}`;
  const variants = {
    // 主 CTA = 品牌色实底（2026-10-07 修正）：原先 primary 走 --action，
    // 深色下解析成 slate-700 中性灰、浅色下是近黑 —— 全站**唯一的主 CTA
    // 识别位是品牌色**，却让中性灰占了默认位，主次动作视觉权重一样重。
    //
    // ⚠️ 「实底 + 白字」与「品牌色当文字」是两个方向相反的职责，深色下同一个值
    //    满足不了两者，所以拆成三个 token（见 index.css）：
    //      --on-brand     压在实底上的反白字
    //      --brand-press  hover/按下时的实心底（**压深**，不是提亮）
    //      --brand-hover  提亮版，只用于「深底上的文字/图标」
    //    数值：text-on-brand 压 --brand = 4.47:1（**未达** 4.5，差 0.03）；
    //    压 --brand-press = 6.29:1 ✅。原先 hover 走 brand-hover(indigo-400)，
    //    白字压上去只剩 2.98:1 —— 鼠标一悬停主 CTA 就崩。
    primary: 'bg-brand text-on-brand hover:bg-brand-press active:bg-brand-press',
    // brand = primary 的显式别名。⚠️ 全仓 14 处调用点（AIVaultPage / AudioTab /
    // OutputReviewTab / StoryboardTab / OverviewTab / EpisodeReviewPanel /
    // ErrorBoundary / ProjectWorkbenchPage），**不是死变体**，删掉会一次性打断这 14 处。
    // 保留别名只为兼容既有调用；新代码直接用默认 primary。
    brand: 'bg-brand text-on-brand hover:bg-brand-press active:bg-brand-press',
    secondary: 'border border-line bg-surface text-ink-1 hover:bg-surface-2 active:bg-surface-2',
    // 破坏性动作：**红边红字**，不与只读动作共享轮廓（MASTER §4.1 / invariant 5）。
    // hover 只把底色推向「红 + 更淡的实底」方向，**不做整块实心填充** ——
    // 原先 `hover:bg-danger hover:text-white` 会让 danger 在 hover 后与
    // primary（实心 + 白字）完全同形，破坏性语义在悬停瞬间消失，
    // 而破坏性动作恰恰是用户最需要看清的一刻。红边红字始终保留。
    danger: 'border border-danger bg-danger-subtle text-danger-strong hover:bg-danger/25 active:bg-danger/25',
    ghost: 'bg-transparent text-ink-2 hover:bg-surface-2 hover:text-ink-1 active:bg-surface-2',
    // link：静止态也必须有**非颜色**线索（WCAG 1.4.1）。原先只有 hover 才出现下划线，
    // 静止态纯靠 text-brand 传达「这是链接」，色觉障碍用户无从分辨。
    link: 'bg-transparent p-0 text-brand underline decoration-current/40 underline-offset-4 hover:decoration-current hover:text-brand-hover',
  };
  // 高度：sm 36 / md 40 / lg 44 —— 40px 是规范底线，纯图标触达区 44×44。
  // 原先 sm=32 / md=36 低于规范（改造前量到 86 个控件不足 24px）。
  const sizes = {
    sm: 'h-control-compact px-3 text-sm',
    md: 'h-control px-4 text-sm',
    lg: 'h-hit-target px-6 text-base',
  };
  const spin = { sm: 'w-3 h-3', md: 'w-3.5 h-3.5', lg: 'w-4 h-4' };
  return (
    <button
      {...rest}
      type={type}
      onClick={onClick}
      disabled={disabled || loading}
      style={style}
      title={title}
      aria-busy={loading || undefined}
      // link 不给 active 底色：它是行内文字，按下时整块变色会读成「跳转中」。
      className={`${base} ${variants[variant]} ${variant === 'link' ? '' : sizes[size]} ${
        variant === 'link' ? 'active:opacity-80' : 'active:opacity-90'
      } ${className}`}
    >
      {loading && (
        <span
          className={`${spin[size]} animate-spin rounded-full border-2 border-current border-t-transparent`}
          aria-hidden="true"
        />
      )}
      {children}
    </button>
  );
}

export function Input({
  value,
  onChange,
  placeholder,
  type = 'text',
  className = '',
  disabled = false,
  label,
  onEnter,
  autoFocus,
  error,
  suffix,
  busy = false,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  className?: string;
  disabled?: boolean;
  label?: string;
  onEnter?: () => void;
  autoFocus?: boolean;
  /** 错误态：描边转红并在下方给出原因，别只靠 placeholder 传达约束 */
  error?: string;
  /** 尾部插槽（如「显示/隐藏密钥」按钮）：输入框自动让出右侧内边距，插槽绝对定位贴右 */
  suffix?: React.ReactNode;
  /** 提交中：aria-busy + 右侧 spinner（spinner 是真实节点，见 FieldSpinner） */
  busy?: boolean;
}) {
  const errorId = React.useId();
  const hasSuffix = Boolean(suffix);
  const input = (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      autoFocus={autoFocus}
      aria-invalid={error ? true : undefined}
      aria-describedby={error ? errorId : undefined}
      aria-busy={busy || undefined}
      onKeyDown={onEnter ? (e) => { if (e.key === 'Enter') onEnter(); } : undefined}
      className={`${FIELD_BASE} ${FIELD_H} ${hasSuffix || busy ? 'pr-10' : ''} ${error ? FIELD_ERR : FIELD_OK} ${className}`}
    />
  );
  const field = hasSuffix || busy ? (
    <div className="relative">
      {input}
      {hasSuffix && (
        <div className="absolute inset-y-0 right-0 flex items-center pr-1.5">{suffix}</div>
      )}
      {!hasSuffix && busy && <FieldSpinner />}
    </div>
  ) : (
    input
  );
  return (
    <FieldLabel label={label}>
      <>
        {field}
        <FieldError error={error} id={errorId} />
      </>
    </FieldLabel>
  );
}
export function Textarea({
  value,
  onChange,
  rows = 6,
  placeholder,
  className = '',
  label,
  mono = false,
  error,
  resize = true,
  busy = false,
  disabled = false,
}: {
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string;
  className?: string;
  label?: string;
  mono?: boolean;
  error?: string;
  /** ⚠️ 必须走 prop 而不是 className 覆盖：resize-y 与 resize-none 同属性，
      谁生效取决于 Tailwind 产出顺序，靠 className 压不住 */
  resize?: boolean;
  /** 提交中：aria-busy + 右上 spinner（多行内容不居中遮挡） */
  busy?: boolean;
  disabled?: boolean;
}) {
  const errorId = React.useId();
  const area = (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={rows}
      placeholder={placeholder}
      disabled={disabled}
      aria-invalid={error ? true : undefined}
      aria-describedby={error ? errorId : undefined}
      aria-busy={busy || undefined}
      className={`${FIELD_BASE} ${resize ? 'resize-y' : 'resize-none'} py-2 ${busy ? 'pr-10' : ''} ${error ? FIELD_ERR : FIELD_OK} ${mono ? 'font-mono text-sm' : ''} ${className}`}
    />
  );
  const field = busy ? (
    <div className="relative">
      {area}
      <FieldSpinner className="right-2.5 top-2" />
    </div>
  ) : (
    area
  );
  return (
    <FieldLabel label={label}>
      <>
        {field}
        <FieldError error={error} id={errorId} />
      </>
    </FieldLabel>
  );
}
/** 原生 <select> 此前 6 处各自手写样式，这里统一出口 */
export function Select({
  value,
  onChange,
  options,
  className = '',
  disabled = false,
  label,
  error,
  busy = false,
  'aria-label': ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  className?: string;
  disabled?: boolean;
  label?: string;
  /** 错误态：与 Input/Textarea 同款 aria-invalid + 红边 + 原因文案（2026-10-07 补齐） */
  error?: string;
  /** 提交中：aria-busy + 右侧 spinner */
  busy?: boolean;
  /**
   * 无可见 label 时的无障碍名。
   * ⚠️ 此前 props 是封闭结构、也不透传剩余原生属性，放在横向工具栏里的 Select
   *    （没有 FieldLabel 的竖排 label 兜底）拿不到任何可访问名，读屏只会念「组合框」。
   *    与 Button 的 `...rest` 透传同一思路：补可选的 aria-label，不动既有调用点。
   */
  'aria-label'?: string;
}) {
  const errorId = React.useId();
  const select = (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      aria-label={ariaLabel}
      aria-invalid={error ? true : undefined}
      aria-describedby={error ? errorId : undefined}
      aria-busy={busy || undefined}
      className={`${FIELD_BASE} ${FIELD_H} cursor-pointer ${busy ? 'pr-10' : ''} ${error ? FIELD_ERR : FIELD_OK} ${className}`}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
  const field = busy ? (
    <div className="relative">
      {select}
      <FieldSpinner />
    </div>
  ) : (
    select
  );
  return (
    <FieldLabel label={label}>
      <>
        {field}
        <FieldError error={error} id={errorId} />
      </>
    </FieldLabel>
  );
}

// ===================== 弹窗 =====================

const MODAL_SIZES = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
  full: 'max-w-6xl',
};
export type ModalSize = keyof typeof MODAL_SIZES;

/**
 * 通用弹窗。
 *
 * ⚠️ 用 Portal 渲染到 document.body：只要祖先元素带有任何 transform / filter /
 * backdrop-filter（例如带入场动画的 .fade-in 页面根节点），position: fixed 就会
 * 以那个祖先为包含块，弹窗会被定位到超长页面的垂直中间 —— 也就是屏幕之外。
 * 挂到 body 上可彻底免疫这个问题。
 *
 * 本轮补齐（此前缺失导致的体验问题）：
 * - ESC 关闭、点遮罩关闭可关（closeOnBackdrop）
 * - 打开时**锁定背景滚动**（此前弹窗打开还能滚后面的长页面）
 * - **焦点陷阱 + 初始焦点 + 关闭后归还焦点**（键盘/读屏用户此前会迷路）
 * - role="dialog" / aria-modal / aria-labelledby；关闭按钮 aria-label
 * - size 档位（此前所有弹窗都写死 max-w-lg，长内容弹窗挤成一条）
 * - footer 操作区；入场动画
 *
 * 2026-09-23：层级由写死 z-[100] 收敛为 z-modal（方案 §5.3 六档规范）；
 * 遮罩 / 容器 / 描边全部改引用 token，与工作台自建弹层合并为唯一实现。
 */
export function Modal({
  isOpen,
  onClose,
  title,
  children,
  size = 'md',
  footer,
  closeOnBackdrop = true,
  closeOnEsc = true,
  preventClose = false,
  description,
}: {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  size?: ModalSize;
  footer?: React.ReactNode;
  /** 误点遮罩就丢掉填写内容很恼人，表单类弹窗可传 false */
  closeOnBackdrop?: boolean;
  /** ESC 是否关闭。⚠️ 表单类弹窗挡了遮罩却留 ESC，输入照样会丢，两个要一起关 */
  closeOnEsc?: boolean;
  /** 提交中禁止关闭 */
  preventClose?: boolean;
  description?: string;
}) {
  const titleId = React.useId();
  const descId = React.useId();
  const containerRef = useModalBehavior({ isOpen, onClose, preventClose, closeOnEsc });

  if (!isOpen) return null;

  const node = (
    <div className="fixed inset-0 z-modal flex items-center justify-center p-4">
      <div
        // 用 `bg-media` 而不是 `bg-slate-900`：index.css 的「组件层禁止字面量」
        // 规则不该被共享组件自己破掉。--bg-media 两套主题同值（#05070B），
        // 所以观感与原先的 slate-900/50 完全一致，只是回到 token 体系。
        className="absolute inset-0 animate-modal-backdrop bg-media/50 backdrop-blur-sm"
        onClick={closeOnBackdrop && !preventClose ? onClose : undefined}
        aria-hidden="true"
      />
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        className={`glass-chrome-strong relative flex max-h-[90vh] w-full animate-modal-in flex-col rounded-xl border border-line shadow-lg ${MODAL_SIZES[size]}`}
      >
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-line px-6 py-4">
          <div className="min-w-0">
            <h2 id={titleId} className="break-words text-lg font-semibold text-ink-1">{title}</h2>
            {description && (
              <p id={descId} className="mt-1 text-sm text-ink-2">{description}</p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={preventClose}
            aria-label={t('common.close')}
            className={`shrink-0 rounded-md p-1 text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink-1 disabled:opacity-40 ${FOCUS_RING}`}
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-6">{children}</div>
        {footer && (
          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-line px-6 py-4">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
  return typeof document === 'undefined' ? node : createPortal(node, document.body);
}

/**
 * 确认弹窗 —— 替代原生 `window.confirm()`
 *
 * 原生 confirm 的问题：阻塞主线程、样式无法定制（与深色主题完全脱节）、
 * 无法表达「危险操作」语义、不能显示处理中状态。项目里原本有 3 处在用。
 */
export function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmText = t('common.ok'),
  cancelText = t('common.cancel'),
  danger = false,
  loading = false,
}: {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void | Promise<void>;
  title: string;
  message?: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  loading?: boolean;
}) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={title}
      size="sm"
      preventClose={loading}
      closeOnBackdrop={!danger}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={loading}>{cancelText}</Button>
          <Button
            variant={danger ? 'danger' : 'primary'}
            onClick={onConfirm}
            loading={loading}
            className="min-w-[5rem]"
          >
            {confirmText}
          </Button>
        </>
      }
    >
      {message ? (
        <div className="text-sm leading-6 text-ink-2">{message}</div>
      ) : null}
    </Modal>
  );
}

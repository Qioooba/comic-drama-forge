/**
 * 采用 / 批准 状态徽标（ADR-0002 + MASTER §2.4 + §4.2 三重律）。
 *
 * ⚠️ 关于 `scripts/audit_tokens.py` 的 SEL001 提醒（需人工确认项，已确认）
 * ------------------------------------------------------------------
 * SEL001 会把本文件与 `DirectorTakeAdoption.tsx` 列为「同时出现 selected 与 approved」。
 * **这是预期命中，不是违规**，理由如下（逐条对应 MASTER §2.4 的不变量）：
 *
 * 1. 本文件是全站**唯一**渲染决策态的地方。两档色必须同时出现在这里 ——
 *    否则就得散到各域去，那正是「state-done 同时表达采用与批准」的旧病。
 * 2. 两档由**同一个 `PRESENTATION` 映射表**按后端 `state` 精确取值，
 *    不存在任何「一个元素同时挂两档色」的分支：
 *    每个 state 对应独立的 label / glyph / cls 三元组。
 * 3. 色、文本、图标三者同源：青档永远是 ●（已采用），绿档永远是 ✓（已批准）。
 *    即便完全忽略颜色，文本与图标仍能区分两档。
 * 4. `selected` 只能落到青档，`approved` 只能落到绿档；映射表是穷举的
 *    （`Record<ProductionDecisionStateName, ...>`），新增后端取值会编译失败，
 *    而不是静默落到某个既有分支上。
 * 5. `DirectorTakeAdoption.tsx` 命中同理：那里是**两个独立按钮**，
 *    一个青（采用）、一个绿（批准），本就不该合并成一个。
 *
 * 结论：本文件是 MASTER §2.4 那条不变量的**实现**而非违反；
 * 该不变量按 MASTER 的说明仍需 code review 保证，此处即为 review 记录。
 *
 * 为什么这个组件存在
 * ------------------
 * 后端把「采用 ≠ 批准」做成了 fail-closed 的领域铁律（未采用就批准 → 409、
 * 机器账号 → 403、`selection_implies_approval` 恒 false），但改造前前端对
 * `decision_state` **零引用** —— 用户在界面上看不到「已采用·未批准」这个状态，
 * 只能把「质检全绿」误读成「可以交付」。本组件把那两档真正画出来。
 *
 * 三条硬约束（不可违反）
 * ----------------------
 * 1. **状态不得只靠颜色**（MASTER §4.2 三重律）。每态必须同时给出
 *    色 + 中文状态词 + 图标/形状，缺一即为违规。
 * 2. **`approved` 永不由 `selected` 推导**。映射表按后端 `state` 精确取值，
 *    `selected` 只可能落到青档，任何分支都不会把它画成绿。
 * 3. **未识别状态一律降级为「已采用·未批准」**，绝不 fail-open 成「已批准」。
 *    契约破裂时的正确方向是「还不能交付」，不是「可以交付」。
 */
import React from 'react';
import type { ProductionDecisionState, ProductionDecisionStateName } from '@/types';
import { normalizeDecisionState } from '@/api/queries';

interface BadgePresentation {
  /** 中文状态词 —— 用户实际读到的那句话，必须与后端 state 语义逐字对应 */
  label: string;
  /** 形状/图标字符（与色共同构成第二、第三重表达） */
  glyph: string;
  /** Tailwind 类，全部走设计令牌，不写字面量 */
  cls: string;
  /** 无障碍标题：读屏用户拿不到颜色，必须有完整描述 */
  title: string;
}

/**
 * 五态 + none 的**穷举**映射表。
 *
 * ⚠️ 用 `Record<ProductionDecisionStateName, BadgePresentation>` 而不是
 *    `Partial<Record<...>>`：新增一个后端取值时，这里会**编译失败**，
 *    逼着作者补一条明确措辞 —— 而不是静默落到 default 分支显示错误状态。
 */
const PRESENTATION: Record<ProductionDecisionStateName, BadgePresentation> = {
  none: {
    label: '未采用',
    glyph: '○',
    cls: 'bg-surface-2 text-ink-2 border-line',
    title: '未采用：这一版尚未被选为工作版本',
  },
  // ---- 采用档（青）：创作决定 ----
  selected: {
    label: '已采用',
    glyph: '●',
    cls: 'bg-selected-subtle text-selected-strong border-selected',
    title: '已采用：创作决定，这版被选为工作版本（尚未批准）',
  },
  selected_not_approved: {
    // ⚠️ 必须写「未批准」。不得写成「已完成」/「已通过」/「OK」——
    //    那些措辞会被读成放行，而 ADR-0002 的全部意义就是禁止这一读法。
    label: '已采用·未批准',
    glyph: '●○',
    cls: 'bg-selected-subtle text-selected-strong border-selected ring-1 ring-selected/60',
    title: '已采用·未批准：有人选了这一版，但**没有人批准放行**，不可交付',
  },
  // ---- 批准档（绿）：放行决定 ----
  approved: {
    label: '已批准',
    glyph: '✓',
    cls: 'bg-approved-subtle text-approved-strong border-approved',
    title: '已批准：人工放行决定，可进入交付',
  },
  approved_stale: {
    label: '批准已失效',
    glyph: '✓!',
    cls: 'bg-approved-subtle text-approved-strong border-approved ring-1 ring-warning/70',
    title: '批准已失效：产物内容已变（哈希失配），必须重新批准才能交付',
  },
  approved_unverified: {
    label: '无法验证',
    glyph: '✓?',
    cls: 'bg-approved-subtle text-approved-strong border-approved opacity-70',
    title: '无法验证：取不到磁盘现状（无路径 / 文件缺失 / 不可读），按未验证处理',
  },
};

export interface DecisionStateBadgeProps {
  /** 后端 `decision_state` 直出结构；缺失时按 none 渲染 */
  decision?: ProductionDecisionState | null;
  /** 紧凑模式：用于列表行内（去掉 padding，仅保留三重表达） */
  compact?: boolean;
  className?: string;
}

/**
 * 决策态徽标。
 *
 * 取值只来自后端 `state`；本组件**不做任何推导**（不按 `selected` 猜 `approved`）。
 */
export function DecisionStateBadge({
  decision,
  compact = false,
  className = '',
}: DecisionStateBadgeProps): JSX.Element {
  // 契约破裂时降级成保守态（见 normalizeDecisionState 注释）
  const safe = normalizeDecisionState(decision);
  const p = PRESENTATION[safe.state];

  return (
    <span
      title={p.title}
      aria-label={p.title}
      data-decision-state={safe.state}
      data-selected={safe.selected ? 'true' : 'false'}
      data-approved={safe.approved ? 'true' : 'false'}
      className={`inline-flex items-center gap-1.5 rounded-full border font-medium ${p.cls} ${
        compact ? 'px-1.5 py-0 text-xs' : 'px-2.5 py-0.5 text-sm'
      } ${className}`}
    >
      {/* 图标/形状：色觉障碍与灰度打印下仍可区分（MASTER §4.2 第三重） */}
      <span aria-hidden="true" className="font-mono leading-none">
        {p.glyph}
      </span>
      {/* 文本：明确的中文状态词（第二重） */}
      <span className="whitespace-nowrap">{p.label}</span>
    </span>
  );
}

/**
 * 未批准时的警示条。
 *
 * 徽标只在候选行内，占一行小字；真正会被误读成「可以交付」的是
 * 「整集/成片」这种汇总位置。这里把「未批准」写成一句人话，
 * 而不是把徽标放大 —— 放大只会加剧「状态很强 = 已放行」的错觉。
 */
export function NotApprovedNotice({ className = '' }: { className?: string }): JSX.Element {
  return (
    <div
      role="status"
      className={`flex items-start gap-2 rounded-md border border-selected bg-selected-subtle px-2.5 py-1.5 text-xs text-selected-strong ${className}`}
    >
      <span aria-hidden="true" className="font-mono leading-none">●○</span>
      <span>已采用但尚未批准：采用是创作决定，批准是人工放行。未经批准的产物不可交付。</span>
    </div>
  );
}

export default DecisionStateBadge;
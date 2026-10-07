/**
 * 候选并排对比（ADR-0002 的界面落点）。
 *
 * 这是「采用 ≠ 批准」第一次真正出现在用户眼前的地方：
 * 并排 N 个候选，每个候选各自带自己的决策态徽标（`DecisionStateBadge`）
 * 与自己的采用/批准动作（`DirectorTakeAdoption`）。
 *
 * 三条硬约束在这里落地
 * --------------------
 * 1. 每个候选**独立**显示自己的采用 / 批准状态 —— 不做「整组已批准」这种汇总态，
 *    因为后端的批准是逐版本的；汇总显示等于凭空制造一个不存在的批准。
 * 2. 「采用」按钮不隐含「批准」：两者是同一行里两个独立按钮。
 * 3. 批准态**只**来自后端 `decision_state`，前端不从「已采用」推导。
 *
 * ⚠️ 本组件只消费查询结果，不自己推导状态：`normalizeDecisionState` 已在
 *    数据层做过 fail-closed 降级（契约破裂 → 「已采用·未批准」而不是「已批准」）。
 */
import React from 'react';
import type { ProductionDecisionState, ProductionMediaVersion } from '@/types';
import { mediaVersionsOf, useMediaDecision, useMediaVersions } from '@/api/queries';
import { assetSrc } from '@/api/queries/assets';
import { DecisionStateBadge, NotApprovedNotice } from './DecisionStateBadge';
import { DirectorTakeAdoption } from './DirectorTakeAdoption';
import { Modal } from '@/components/ui';
import { t } from '@/i18n';

export interface CandidateCompareDialogProps {
  isOpen: boolean;
  onClose: () => void;
  /** 一次生成的意图 id —— 候选列表按它取。传空即不取数。 */
  intentId?: string;
  project?: string;
  episode?: string;
  shotKey?: string;
  /** 单选某候选（= 采用的轻量入口；完整采用仍走 DirectorTakeAdoption） */
  onPick?: (v: ProductionMediaVersion) => void;
}

export function CandidateCompareDialog({
  isOpen,
  onClose,
  intentId,
  project,
  episode,
  shotKey,
  onPick,
}: CandidateCompareDialogProps): JSX.Element | null {
  // ⚠️ 必须按 isOpen 传 intentId：`if (!isOpen) return null` 写在 hook **之后**，
  //    hooks 不会因为提前 return 被跳过 —— 改造前每次父组件重新渲染（包括每次
  //    切换选中镜头）都会拿 intentId 打一次候选列表接口，而对话框是关着的。
  //    传 undefined 即 `enabled: false`，关闭期间完全不发请求（见 useMediaVersions）。
  const q = useMediaVersions(isOpen ? intentId : undefined);
  const items = mediaVersionsOf(q.data);

  if (!isOpen) return null;

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={t('studio.compareTitle')} size="xl">
      {q.isPending && (
        <div role="status" aria-live="polite" className="p-6 text-center text-sm text-ink-2">
          {t('studio.compareLoading')}
        </div>
      )}

      {/* 契约破裂 / 取不到候选：报错必须给原因 + 重试（MASTER invariant 10） */}
      {q.isError && (
        <div role="alert" className="rounded-md border border-danger bg-danger-subtle p-3 text-sm text-danger-strong">
          <p>{t('studio.compareLoadFailed', {
            err: q.error instanceof Error ? q.error.message : String(q.error),
          })}</p>
          <button
            onClick={() => void q.refetch()}
            className="mt-2 rounded border border-danger px-2 py-1 text-xs"
          >
            {t('common.retry')}
          </button>
        </div>
      )}

      {!q.isPending && !q.isError && items.length === 0 && (
        <div className="p-6 text-center text-sm text-ink-2">{t('studio.noCandidates')}</div>
      )}

      {items.length > 0 && (
        <>
          {/* 汇总警示：只要还有「已采用·未批准」的候选，就明确说「不可交付」。
              注意措辞 —— 不能写成「已完成」「已通过」。 */}
          <NotApprovedNotice className="mb-3" />
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {items.map((v) => (
              <CandidateCard
                key={v.media_version_id}
                version={v}
                project={project}
                episode={episode}
                shotKey={shotKey}
                onPick={onPick}
              />
            ))}
          </div>
        </>
      )}
    </Modal>
  );
}

/** 单个候选卡：图 + 指纹 + 决策徽标 + 采用/批准。 */
function CandidateCard({
  version,
  project,
  episode,
  shotKey,
  onPick,
}: {
  version: ProductionMediaVersion;
  project?: string;
  episode?: string;
  shotKey?: string;
  onPick?: (v: ProductionMediaVersion) => void;
}): JSX.Element {
  const id = version.media_version_id;
  // 与徽标共用同一份三态查询：徽标与两个按钮必须看到**同一个** decision，
  // 否则会出现「徽标说未批准、按钮却按已批准禁用」的自相矛盾。
  const decisionQ = useMediaDecision(id);
  const src = assetSrc(typeof version.path === 'string' ? version.path : undefined);
  const sha = typeof version.media_sha256 === 'string' ? version.media_sha256 : '';

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-line bg-surface p-2">
      <div className="flex aspect-video items-center justify-center overflow-hidden rounded bg-media">
        {src ? (
          <img src={src} alt="" className="h-full w-full object-contain" loading="lazy" />
        ) : (
          <span className="text-xs text-ink-3">{t('storyboard.noPreview')}</span>
        )}
      </div>

      {/* 指纹：等宽 + 截断显示前 12 位，便于与后端日志/审计记录逐位核对 */}
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-xs tabular-nums text-ink-3" title={sha}>
          {sha ? sha.slice(0, 12) : '—'}
        </span>
        {onPick && (
          <button
            onClick={() => onPick(version)}
            className="rounded border border-line px-1.5 py-0.5 text-xs text-ink-2 hover:border-brand hover:text-brand active:opacity-90"
          >
            {t('studio.pick')}
          </button>
        )}
      </div>

      {/* 决策态只读展示（真正的动作在下面的 DirectorTakeAdoption 里）。
          ⚠️ 必须逐候选取自己的三态，不能拿「列表里有没有被采用过」这种汇总值
          代替 —— 后端的采用/批准都是**逐版本**的，汇总显示会凭空造出一个
          不存在的批准/未批准。取数走 useMediaDecision（有缓存，切开关不重打）。 */}
      <CandidateDecisionBadge decision={decisionQ.data?.decision} />

      <DirectorTakeAdoption
        mediaVersionId={id}
        decision={decisionQ.data?.decision}
        project={project}
        episode={episode}
        shotKey={shotKey}
      />
    </div>
  );
}

/** 单个候选的决策徽标（数据未就绪时不渲染，避免闪一下「未采用」） */
function CandidateDecisionBadge({
  decision,
}: {
  decision?: ProductionDecisionState;
}): JSX.Element | null {
  if (!decision) return null;
  return <DecisionStateBadge decision={decision} compact />;
}

export default CandidateCompareDialog;
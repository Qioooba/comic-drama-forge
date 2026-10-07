/**
 * 采用 / 批准 两个独立动作（ADR-0002）。
 *
 * 为什么必须是**两个按钮**
 * ----------------------
 * 改造前界面上只有一档「完成」语义，机器检查通过与人工放行被压成同一个绿档。
 * 那正是「从来没有人看过第 37 集第 12 镜」却显示为可交付的视觉根因。
 * 把它们做成两个按钮不是 UI 偏好，是把**两类性质不同的决定**在界面上分开：
 *
 *   采用（创作决定） →  POST /production_facts/media/<id>/select
 *                      后端只写 SelectionDecision，**不写也无法写**批准记录。
 *   批准（放行决定） →  POST /production_facts/media/<id>/approve
 *                      必须带人工授权主体；机器账号 403，未采用 409。
 *
 * 合并成一个「采用并批准」按钮，会让后端精心设计的 409/403 闸门在前端被绕过，
 * 审批记录看起来有人负责、实际没有任何人看过那一版。
 */
import React, { useState } from 'react';
import type { ProductionDecisionState } from '@/types';
import { t } from '@/i18n';
import {
  normalizeDecisionState,
  useApproveMediaVersion,
  useSelectMediaVersion,
} from '@/api/queries';
import { Button } from '@/components/ui';
import { DecisionStateBadge, NotApprovedNotice } from './DecisionStateBadge';

export interface DirectorTakeAdoptionProps {
  mediaVersionId: string;
  /** 后端三态；缺失时按 none 处理 */
  decision?: ProductionDecisionState | null;
  project?: string;
  episode?: string;
  shotKey?: string;
  /** 供审计留痕的操作者（采用侧）。批准侧必须由用户在输入框里显式填写。 */
  decidedBy?: string;
  /** 关闭后是否仍可操作（例如只读审片模式） */
  readOnly?: boolean;
}

/** 聚焦环：与工作台既有控件逐字一致（换不成共享组件的原生控件统一补这一串）。 */
const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

export function DirectorTakeAdoption({
  mediaVersionId,
  decision,
  project,
  episode,
  shotKey,
  decidedBy,
  readOnly = false,
}: DirectorTakeAdoptionProps): JSX.Element {
  const safe = normalizeDecisionState(decision);
  const select = useSelectMediaVersion();
  const approve = useApproveMediaVersion();

  // 两个**独立**的输入态：采用理由与批准授权主体不可互相顶替。
  const [reason, setReason] = useState('');
  const [approver, setApprover] = useState('');
  const [localError, setLocalError] = useState('');

  const busy = select.isPending || approve.isPending;
  const selected = safe.selected;
  const approved = safe.approved;
  // ⚠️ 「是否还持有**有效**的批准」必须看 state，不能看 `approved` 布尔：
  //    `normalizeDecisionState` 要求 approved_stale / approved_unverified 都带
  //    `approved: true`（确实批准过，只是内容变了 / 验证不了），所以这两个状态下
  //    `safe.approved` 依然是 true。
  const approvedNow = safe.state === 'approved';
  // 「曾批准、现已失效」= 需要**重新做一次**放行决定，不是「已批准、别再点」。
  const approvalInvalid = safe.state === 'approved_stale' || safe.state === 'approved_unverified';

  const submitSelect = async () => {
    setLocalError('');
    // 客户端也拦一道：后端会 400，但先给出即时反馈，
    // 免得用户提交后才发现自己没写理由（且提示要过一轮网络往返）。
    if (!reason.trim()) {
      setLocalError(t('decision.errAdoptNeedsReason'));
      return;
    }
    try {
      await select.mutateAsync({
        mediaVersionId,
        reason: reason.trim(),
        decided_by: decidedBy,
        project,
        episode,
        shot_key: shotKey,
      });
      setReason('');
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : String(e));
    }
  };

  const submitApprove = async () => {
    setLocalError('');
    // 批准必须有**人工**主体。这里刻意不代填、不预填、不从 decidedBy 复制 ——
    // 那是替用户伪造人工授权，前端绕过机器账号闸门与后端放行机器账号等价。
    if (!approver.trim()) {
      setLocalError(t('decision.errApproveNeedsApprover'));
      return;
    }
    if (!selected) {
      // 后端同样会 409；这里提前一步，避免把一次必然失败的请求发出去。
      setLocalError(t('decision.errApproveNeedsSelected'));
      return;
    }
    try {
      await approve.mutateAsync({
        mediaVersionId,
        authorized_by: approver.trim(),
        reason: reason.trim() || undefined,
      });
      setApprover('');
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="rounded-lg border border-line bg-surface p-3 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-ink-1">{t('decision.title')}</span>
        <DecisionStateBadge decision={safe} />
      </div>

      {/* 已采用未批准：这是最容易被误读成「可以交付」的状态，必须显式说明 */}
      {selected && !approved && <NotApprovedNotice />}

      {!readOnly && (
        <>
          {/* ---- 动作一：采用（创作决定，青） ---- */}
          <div className="space-y-1.5">
            <label className="block text-xs text-ink-2" htmlFor={`sel-reason-${mediaVersionId}`}>
              {t('decision.adoptReasonLabel')}
            </label>
            <input
              id={`sel-reason-${mediaVersionId}`}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={busy}
              placeholder={t('decision.adoptReasonPlaceholder')}
              className={`w-full rounded border border-line bg-surface-2 px-2.5 py-1.5 text-sm text-ink-1 placeholder:text-ink-3 disabled:opacity-60 disabled:cursor-not-allowed ${FOCUS_RING}`}
            />
            <Button
              variant="secondary"
              onClick={submitSelect}
              disabled={busy || selected}
              className="w-full border-selected text-selected-strong hover:bg-selected-subtle"
            >
              <span aria-hidden="true" className="mr-1.5 font-mono">●</span>
              {selected ? t('state.selected') : t('decision.adopt')}
            </Button>
            {selected && (
              <p className="text-xs text-ink-3">{t('decision.adoptAppendOnly')}</p>
            )}
          </div>

          {/* ---- 动作二：批准（放行决定，绿）—— 与上面是**两个独立按钮** ---- */}
          <div className="space-y-1.5 border-t border-line pt-3">
            <label className="block text-xs text-ink-2" htmlFor={`apr-by-${mediaVersionId}`}>
              {t('decision.approverLabel')}
            </label>
            <input
              id={`apr-by-${mediaVersionId}`}
              value={approver}
              onChange={(e) => setApprover(e.target.value)}
              disabled={busy}
              placeholder={t('decision.approverPlaceholder')}
              className={`w-full rounded border border-line bg-surface-2 px-2.5 py-1.5 text-sm text-ink-1 placeholder:text-ink-3 disabled:opacity-60 disabled:cursor-not-allowed ${FOCUS_RING}`}
            />
            <Button
              variant="secondary"
              onClick={submitApprove}
              // ⚠️ 这里**不能**把失效态一起 disabled：按钮上写着
              //    「批准已失效，需重新批准」却点不动，等于把用户指到一条死路上
              //    （看得见出路、走不过去）。文案与可点性同源：
              //    只有「当前持有有效批准」才禁用，失效态必须允许重新批准。
              disabled={busy || approvedNow}
              className="w-full border-approved text-approved-strong hover:bg-approved-subtle"
            >
              <span aria-hidden="true" className="mr-1.5 font-mono">✓</span>
              {approvedNow
                ? t('state.approved')
                : approvalInvalid ? t('decision.approveInvalid') : t('decision.approve')}
            </Button>
            {/* MASTER invariant 6：disabled 必须配相邻原因 */}
            {!approvedNow && !approvalInvalid && !selected && (
              <p className="text-xs text-ink-3">{t('decision.approveNeedsSelect')}</p>
            )}
          </div>
        </>
      )}

      {(localError || select.error || approve.error) && (
        <p role="alert" className="rounded border border-danger bg-danger-subtle px-2.5 py-1.5 text-xs text-danger-strong">
          {(localError
            || (select.error instanceof Error ? select.error.message : '')
            || (approve.error instanceof Error ? approve.error.message : '')
          )}
        </p>
      )}
    </div>
  );
}

export default DirectorTakeAdoption;
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
  // 已批准/失效/无法验证都说明「曾经批准过」，此时不再重复给批准按钮
  const hasAnyApproval = safe.state === 'approved' || safe.state === 'approved_stale'
    || safe.state === 'approved_unverified';

  const submitSelect = async () => {
    setLocalError('');
    // 客户端也拦一道：后端会 400，但先给出即时反馈，
    // 免得用户提交后才发现自己没写理由（且提示要过一轮网络往返）。
    if (!reason.trim()) {
      setLocalError('采用必须写明理由，否则无法回答「这镜为什么用这一版」。');
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
      setLocalError('批准必须填写人工授权主体；机器检查通过不等于批准。');
      return;
    }
    if (!selected) {
      // 后端同样会 409；这里提前一步，避免把一次必然失败的请求发出去。
      setLocalError('尚未采用，不能批准。先点「采用」再批准 —— 采用不等于批准。');
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
        <span className="text-sm font-medium text-ink-1">决策</span>
        <DecisionStateBadge decision={safe} />
      </div>

      {/* 已采用未批准：这是最容易被误读成「可以交付」的状态，必须显式说明 */}
      {selected && !approved && <NotApprovedNotice />}

      {!readOnly && (
        <>
          {/* ---- 动作一：采用（创作决定，青） ---- */}
          <div className="space-y-1.5">
            <label className="block text-xs text-ink-2" htmlFor={`sel-reason-${mediaVersionId}`}>
              采用理由（必填）
            </label>
            <input
              id={`sel-reason-${mediaVersionId}`}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={busy}
              placeholder="例如：九宫格第 4 格构图最稳，视线朝向正确"
              className={`w-full rounded border border-line bg-surface-2 px-2.5 py-1.5 text-sm text-ink-1 placeholder:text-ink-3 disabled:opacity-60 disabled:cursor-not-allowed ${FOCUS_RING}`}
            />
            <Button
              variant="secondary"
              onClick={submitSelect}
              disabled={busy || selected}
              className="w-full border-selected text-selected-strong hover:bg-selected-subtle"
            >
              <span aria-hidden="true" className="mr-1.5 font-mono">●</span>
              {selected ? '已采用' : '采用这一版'}
            </Button>
            {selected && (
              <p className="text-xs text-ink-3">已采用过。再次采用会追加一条新的采用记录（决策是追加式留痕）。</p>
            )}
          </div>

          {/* ---- 动作二：批准（放行决定，绿）—— 与上面是**两个独立按钮** ---- */}
          <div className="space-y-1.5 border-t border-line pt-3">
            <label className="block text-xs text-ink-2" htmlFor={`apr-by-${mediaVersionId}`}>
              人工授权主体（必填）
            </label>
            <input
              id={`apr-by-${mediaVersionId}`}
              value={approver}
              onChange={(e) => setApprover(e.target.value)}
              disabled={busy}
              placeholder="你的署名（机器账号无权批准）"
              className={`w-full rounded border border-line bg-surface-2 px-2.5 py-1.5 text-sm text-ink-1 placeholder:text-ink-3 disabled:opacity-60 disabled:cursor-not-allowed ${FOCUS_RING}`}
            />
            <Button
              variant="secondary"
              onClick={submitApprove}
              disabled={busy || approved || hasAnyApproval}
              className="w-full border-approved text-approved-strong hover:bg-approved-subtle"
            >
              <span aria-hidden="true" className="mr-1.5 font-mono">✓</span>
              {approved ? '已批准' : hasAnyApproval ? '批准已失效，需重新批准' : '批准放行'}
            </Button>
            {/* MASTER invariant 6：disabled 必须配相邻原因 */}
            {!approved && !hasAnyApproval && !selected && (
              <p className="text-xs text-ink-3">需先采用才能批准（后端同样会 409）。</p>
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
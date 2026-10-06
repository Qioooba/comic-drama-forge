/**
 * 整集镜头板（Episode Shot Board）—— Shot Studio 的主视图。
 *
 * 三栏骨架（MASTER §3）
 *   左：镜头导航（ShotNavigator，超过 50 项虚拟化）
 *   中：媒体舞台（bg-media，比 UI 更深）
 *   右：检查器（意图 / 站位 / 首尾帧 / 候选 / 决策态）
 *
 * 为什么这一层只做**编排**
 * ----------------------
 * 取数一律走 `api/queries`（ADR-0010），决策态一律走 `DecisionStateBadge`
 * （ADR-0002），本文件不自己发请求、不自己推导状态 —— 一旦在这里再写一套
 * 轮询或状态映射，就等于把刚收敛掉的两样东西又复制了一份。
 */
import React, { useState } from 'react';
import { ShotNavigator, type NavigatorShot } from './ShotNavigator';
import { DirectorIntentEditor, type DirectorIntent } from './DirectorIntentEditor';
import { StagingBoard, type StagedCharacter } from './StagingBoard';
import { FrameBridgeControls } from './FrameBridgeControls';
import { CandidateCompareDialog } from './CandidateCompareDialog';
import { DecisionStateBadge } from './DecisionStateBadge';
import type { ProductionDecisionState } from '@/types';

export interface EpisodeShotBoardProps {
  projectKey: string;
  episodeNo: number | null;
  shots: NavigatorShot[];
  selectedShotId?: string | null;
  onSelectShot: (shotId: string) => void;
  /** 当前镜的导演意图 */
  intent: DirectorIntent;
  onIntentChange: (patch: Partial<DirectorIntent>) => void;
  onDeriveIntent?: () => void;
  /** 当前镜出场角色与站位 */
  staged?: StagedCharacter[];
  onStagedChange?: (characterId: string, patch: Partial<StagedCharacter>) => void;
  /** 当前镜候选的生成意图 id（用于候选并排对比） */
  intentId?: string;
  /** 当前镜的决策态（若有） */
  decision?: ProductionDecisionState;
  /** 导航器是否收起（MASTER §3：导航器 285px，可收） */
  navigatorOpen?: boolean;
  onToggleNavigator?: () => void;
}

export function EpisodeShotBoard(props: EpisodeShotBoardProps): JSX.Element {
  const [compareOpen, setCompareOpen] = useState(false);
  const {
    projectKey, episodeNo, shots, selectedShotId, onSelectShot,
    intent, onIntentChange, onDeriveIntent,
    staged = [], onStagedChange,
    intentId, decision,
    navigatorOpen = true, onToggleNavigator,
  } = props;

  const current = shots.find((s) => s.shot_id === selectedShotId) || null;

  return (
    <div className="flex min-h-0 flex-col gap-3">
      {/* ---- 常驻上下文条：项目 → 季 → 集 → 镜（MASTER §3） ---- */}
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-line bg-surface px-2.5 py-1.5 text-sm">
        <span className="text-ink-2">{projectKey}</span>
        <span aria-hidden="true" className="text-ink-3">→</span>
        {/* 集号用等宽数字：跨镜头逐位核对时不能左右跳动 */}
        <span className="font-mono tabular-nums text-ink-1">
          第 {episodeNo ?? '—'} 集
        </span>
        <span aria-hidden="true" className="text-ink-3">→</span>
        <span className="font-mono tabular-nums text-ink-1">
          {current?.seq != null ? `第 ${current.seq} 镜` : '未选镜'}
        </span>

        <span className="ml-auto flex items-center gap-2">
          {decision && <DecisionStateBadge decision={decision} compact />}
          <button
            onClick={() => setCompareOpen(true)}
            disabled={!intentId}
            title={intentId ? '并排比较候选' : '本镜尚无生成意图'}
            className="control-compact rounded border border-line px-2 py-1 text-xs text-ink-2 hover:border-brand hover:text-brand disabled:cursor-not-allowed disabled:opacity-60"
          >
            候选对比
          </button>
          {onToggleNavigator && (
            <button
              onClick={onToggleNavigator}
              aria-expanded={navigatorOpen}
              className="control-compact rounded border border-line px-2 py-1 text-xs text-ink-2 hover:border-brand hover:text-brand"
            >
              {navigatorOpen ? '收起镜头' : '展开镜头'}
            </button>
          )}
        </span>
      </div>

      {/* ---- 三栏 ---- */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[285px_1fr_350px]">
        {/* 左：导航器（可收） */}
        {navigatorOpen && (
          <ShotNavigator
            shots={shots}
            selectedShotId={selectedShotId}
            onSelect={onSelectShot}
            className="min-h-[320px] lg:min-h-0"
          />
        )}

        {/* 中：媒体舞台 */}
        <div className="flex min-h-[280px] min-w-0 flex-col gap-3 rounded-lg border border-line bg-media p-3">
          <h2 className="font-serif text-lg text-ink-1">
            {current?.description || '选择一个镜头'}
          </h2>
          <FrameBridgeControls
            startFrameUrl={undefined}
            endFrameUrl={undefined}
            disabledReason={current ? undefined : '请先选择镜头'}
          />
        </div>

        {/* 右：检查器 */}
        <div className="min-w-0 space-y-3">
          <DirectorIntentEditor
            intent={intent}
            onChange={onIntentChange}
            onDerive={onDeriveIntent}
          />
          {onStagedChange && (
            <StagingBoard
              characters={staged}
              onChange={onStagedChange}
            />
          )}
        </div>
      </div>

      <CandidateCompareDialog
        isOpen={compareOpen}
        onClose={() => setCompareOpen(false)}
        intentId={intentId}
        project={projectKey}
        episode={episodeNo != null ? String(episodeNo) : undefined}
        shotKey={selectedShotId || undefined}
      />
    </div>
  );
}

export default EpisodeShotBoard;
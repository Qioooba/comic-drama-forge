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
 *
 * ⚠️ 布局**不自建**：三栏与常驻上下文条由 `@/layouts` 的 `AppShell` 承担。
 *    本文件原先自己写了一份「上下文条 + lg:grid-cols-[285px_1fr_350px]」，
 *    与 `AppShell` 逐字重复 —— 两份实现各自演化，正是 MASTER §3 那张
 *    三栏图永远对不上的原因。现在只有一处布局实现。
 */
import React, { useState } from 'react';
import { AppShell, type ResourceStatus } from '@/layouts';
import { ShotNavigator, type NavigatorShot } from './ShotNavigator';
import { DirectorIntentEditor, type DirectorIntent } from './DirectorIntentEditor';
import { StagingBoard, type StagedCharacter } from './StagingBoard';
import { FrameBridgeControls } from './FrameBridgeControls';
import { CandidateCompareDialog } from './CandidateCompareDialog';
import { DecisionStateBadge } from './DecisionStateBadge';
import { t } from '@/i18n';
import type { ProductionDecisionState } from '@/types';

/**
 * 只读模式的空回调。
 *
 * ⚠️ 必须是**同一个引用**：每次渲染新建 `() => {}` 会让
 * `DirectorIntentEditor` / `StagingBoard` 的 props 永远不等，
 * 任何 memo 化的子组件都会被白白重渲。
 */
const NOOP_INTENT = () => { /* 只读模式：不接受编辑 */ };
const NOOP_STAGE = () => { /* 只读模式：不接受编辑 */ };

export interface EpisodeShotBoardProps {
  projectKey: string;
  episodeNo: number | null;
  shots: NavigatorShot[];
  selectedShotId?: string | null;
  onSelectShot: (shotId: string) => void;
  /** 当前镜的导演意图 */
  intent: DirectorIntent;
  /**
   * 意图变更回调。
   *
   * ⚠️ **可缺省**：缺省时编辑器转只读展示。这不是省事，而是当前后端的实情：
   *   剧本字段里 `camera_motion` / `emotion` 存的是中文散文（「固定」「紧张」），
   *   而本编辑器的下拉是英文枚举（`static` / `tense`）；把英文 token 写回那两列
   *   会污染出图提示词。而真正的「派生新意图」入口
   *   （`POST /production_facts/intents/{id}/derive`）强制要求 `reason`，
   *   编辑器没有承接它的输入框。**宁可只读，也不写一个存不住的假编辑口。**
   */
  onIntentChange?: (patch: Partial<DirectorIntent>) => void;
  onDeriveIntent?: () => void;
  /** 当前镜出场角色与站位 */
  staged?: StagedCharacter[];
  onStagedChange?: (characterId: string, patch: Partial<StagedCharacter>) => void;
  /**
   * 站位只读：后端剧本 PUT 的字段白名单里**没有** `blocking`
   * （见 `app/api/storyboard.py::api_update_episode` 的 `_ALLOWED`），
   * 站位改动无处落盘，因此只显示不改。
   */
  stagedReadOnly?: boolean;
  /** 当前镜候选的生成意图 id（用于候选并排对比） */
  intentId?: string;
  /** 当前镜的决策态（若有） */
  decision?: ProductionDecisionState;
  /** 首帧 / 尾帧图 URL；缺省即该端未生成（由画布卡片给出真实地址） */
  startFrameUrl?: string | null;
  endFrameUrl?: string | null;
  /** 队列 / GPU / 磁盘；取不到时由 EpisodeContextBar 显示「未知」而非隐藏 */
  resourceStatus?: ResourceStatus;
  className?: string;
}

export function EpisodeShotBoard(props: EpisodeShotBoardProps): JSX.Element {
  const [compareOpen, setCompareOpen] = useState(false);
  const {
    projectKey, episodeNo, shots, selectedShotId, onSelectShot,
    intent, onIntentChange, onDeriveIntent,
    staged = [], onStagedChange, stagedReadOnly,
    intentId, decision,
    startFrameUrl, endFrameUrl,
    resourceStatus, className = '',
  } = props;

  const current = shots.find((s) => s.shot_id === selectedShotId) || null;
  const intentReadOnly = !onIntentChange;

  return (
    <>
      <AppShell
        className={className}
        context={{
          projectKey,
          episodeNo,
          // 季在本项目没有独立概念（产物按「项目/epNN」落盘），不硬造一档
          shotSeq: current?.seq ?? null,
        }}
        status={resourceStatus}
        // 决策态与上下文同处一条常驻带：用户必须能确认「第 37 镜已采用·未批准」
        // 说的是哪一镜的决定（ADR-0002 在 UI 上的落点）
        actions={<DecisionStateBadge decision={decision} compact />}
        navigator={
          <ShotNavigator
            shots={shots}
            selectedShotId={selectedShotId}
            onSelect={onSelectShot}
            className="min-h-[320px] lg:min-h-0"
          />
        }
        stage={
          <div className="flex min-h-0 flex-col gap-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-serif text-lg text-ink-1">
                {current?.description || t('studio.selectShot')}
              </h2>
              <button
                onClick={() => setCompareOpen(true)}
                disabled={!intentId}
                title={intentId ? t('studio.compareHint') : t('studio.noIntent')}
                className="control-compact rounded border border-line px-2 py-1 text-xs text-ink-2 hover:border-brand hover:text-brand active:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {t('studio.compare')}
              </button>
            </div>
            <FrameBridgeControls
              startFrameUrl={startFrameUrl}
              endFrameUrl={endFrameUrl}
              disabledReason={current ? undefined : t('studio.selectShotFirst')}
            />
          </div>
        }
        inspector={
          <div className="min-w-0 space-y-3">
            <DirectorIntentEditor
              intent={intent}
              onChange={onIntentChange ?? NOOP_INTENT}
              onDerive={onDeriveIntent}
              busy={intentReadOnly}
            />
            <StagingBoard
              characters={staged}
              onChange={onStagedChange ?? NOOP_STAGE}
              readOnly={stagedReadOnly || !onStagedChange}
            />
          </div>
        }
      />

      <CandidateCompareDialog
        isOpen={compareOpen}
        onClose={() => setCompareOpen(false)}
        intentId={intentId}
        project={projectKey}
        episode={episodeNo != null ? String(episodeNo) : undefined}
        shotKey={selectedShotId || undefined}
      />
    </>
  );
}

export default EpisodeShotBoard;
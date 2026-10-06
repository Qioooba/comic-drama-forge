/**
 * Shot Studio 出口（ADR-0009 + 评估 §4.3）。
 *
 * 组件一律从 `@/features/shot-studio` 取，不要深入具体文件。
 * 其中 `DecisionStateBadge` 是 ADR-0002「采用 ≠ 批准」在 UI 上的**唯一**渲染入口；
 * 任何地方要显示决策态都必须用它 —— 自己写一份 badge 就是复制一次
 * 「用 approved 的颜色表达 selected」的机会，而那正是 MASTER §2.4
 * 点名的头号不变量（目前仅靠 code review 保证，静态校验只是提醒级）。
 */
export { DecisionStateBadge, NotApprovedNotice } from './DecisionStateBadge';
export type { DecisionStateBadgeProps } from './DecisionStateBadge';

export { DirectorTakeAdoption } from './DirectorTakeAdoption';
export type { DirectorTakeAdoptionProps } from './DirectorTakeAdoption';

export { ShotNavigator, VIRTUALIZE_THRESHOLD } from './ShotNavigator';
export type { ShotNavigatorProps, NavigatorShot } from './ShotNavigator';

export { DirectorIntentEditor, CAMERA_MOVES, EMOTIONS, GAZE_TARGETS } from './DirectorIntentEditor';
export type {
  DirectorIntent, DirectorIntentEditorProps, CameraMove, Emotion, GazeTarget,
} from './DirectorIntentEditor';

export { EpisodeShotBoard } from './EpisodeShotBoard';
export type { EpisodeShotBoardProps } from './EpisodeShotBoard';

export { CandidateCompareDialog } from './CandidateCompareDialog';
export type { CandidateCompareDialogProps } from './CandidateCompareDialog';

export { FrameBridgeControls } from './FrameBridgeControls';
export type { FrameBridgeControlsProps } from './FrameBridgeControls';

export { StagingBoard } from './StagingBoard';
export type { StagingBoardProps, StagedCharacter } from './StagingBoard';
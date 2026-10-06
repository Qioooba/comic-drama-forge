/**
 * 工作台查询出口。
 *
 * 组件一律从 `@/api/queries` 取，不要深入具体模块 ——
 * 这样 queryClient 的默认策略、key 形状与轮询节奏只有这一处可改。
 */
export { queryClient, clearProjectCache, DEFAULT_STALE_TIME } from '../queryClient';
export { queryKeys } from './keys';
export { POLL_FAST, POLL_SLOW, POLL_CANVAS, keepPreviousOptions } from './options';
export type { KeepPreviousOptions } from './options';

export { useProjectAssets, assetSrc } from './assets';
export type { AssetItem, ProjectAssets } from './assets';

export { sanitizeError } from './errors';

export { useAutopilotStatus } from './autopilot';

export { useEpisodes, useEpisodeScript, useStoryboardCanvas, useKeyframePlan } from './storyboard';

export { useGenerationStatus } from './jobs';
export type { GenerationTaskStatus } from './jobs';

export { useUpscaleTask } from './upscale';

export {
  useMediaVersions,
  useMediaDecision,
  useSelectMediaVersion,
  useApproveMediaVersion,
  useShotIntents,
  latestIntentOf,
  normalizeDecisionState,
  mediaVersionsOf,
} from './productionFacts';
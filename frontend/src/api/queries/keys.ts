/**
 * Query Key 单一真源（ADR-0010）。
 *
 * 为什么 key 要集中在这里写
 * ----------------------
 * key 写错**不会报错**，只会让两个本该不同的资源共享同一份缓存 ——
 * 「A 项目的候选显示成 B 项目的」这种 bug 没有日志、没有报错、难复现。
 * 把 key 收敛成一个工厂后，key 的形状只有一处能改，review 时只需看这一个文件。
 *
 * 约定
 * ----
 * 1. 项目级资源一律以 `['project', projectKey, ...]` 开头 —— 这样
 *    `clearProjectCache(projectKey)` 能一次性丢掉整个项目的缓存（切项目不留脏数据）。
 * 2. 一切会换 key 的资源（切集、切镜）把可变部分放在最后，
 *    `keepPreviousData` 才有意义（见 `queries/options.ts`）。
 * 3. key 里**不放**刷新间隔：那不是身份的一部分。放进去会让同一个资源的
 *    不同调用方各自持一份缓存 —— 正是本次要消除的「重复打后端」。
 */
export const queryKeys = {
  /** 项目资产（跨 Tab 共享的那一份：总览统计、交付验收、关系图都读它） */
  assets: (projectKey: string) => ['project', projectKey, 'assets'] as const,

  /** 项目配置（含 video_mode 等生成方式设置） */
  projectConfig: (projectKey: string) => ['project', projectKey, 'config'] as const,

  /** 剧集列表（按小说维度） */
  episodes: (novelId: string) => ['episodes', novelId] as const,

  /** 单集剧本正文（Shot Studio 的站位 blocking 只在这里） */
  episodeScript: (novelId: string, episodeNo: number | null | undefined) =>
    ['episodes', novelId, 'script', episodeNo ?? 'latest'] as const,

  /** 分镜画布（按集） */
  storyboardCanvas: (projectKey: string, episodeNo: number | null | undefined) =>
    ['project', projectKey, 'storyboard', 'canvas', episodeNo ?? 'latest'] as const,

  /** 分镜首尾帧计划（按集） */
  keyframePlan: (projectKey: string, episodeNo: number | null | undefined) =>
    ['project', projectKey, 'keyframes', 'plan', episodeNo ?? 'latest'] as const,

  /** 托管生产状态（工作台进度条 + 总控状态卡共用同一份） */
  autopilotStatus: (projectKey: string) => ['project', projectKey, 'autopilot', 'status'] as const,

  /** 质检配置（按项目） */
  qcConfig: (projectKey: string) => ['project', projectKey, 'qc', 'config'] as const,

  /** 超分链路自检 */
  upscaleEnv: () => ['upscale', 'env'] as const,

  /** 超分产物列表（按项目） */
  upscaleArtifacts: (projectKey: string) => ['project', projectKey, 'upscale', 'artifacts'] as const,

  /** 超分任务进度（按 taskId） */
  upscaleTask: (taskId: string) => ['upscale', 'task', taskId] as const,

  /** 生成候选版本列表（ADR-0002：采用/批准的载体） */
  mediaVersions: (intentId: string) => ['production-facts', 'media', intentId] as const,

  /** 单个候选的采用/批准三态 */
  mediaDecision: (mediaVersionId: string) => ['production-facts', 'decision', mediaVersionId] as const,

  /**
   * 冻结的生成意图（按 项目/集/镜 定位「这一镜最近一次意图」）。
   * 可变部分放最后，切集切镜即换 key。
   */
  productionIntents: (projectKey: string, episode: string, shotKey: string) =>
    ['production-facts', 'intents', projectKey, episode, shotKey] as const,
} as const;

export type QueryKeys = typeof queryKeys;
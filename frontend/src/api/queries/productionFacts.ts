/**
 * 采用 / 批准的查询与变更（ADR-0002，ADR-0010）。
 *
 * 这个模块存在的理由
 * ------------------
 * 改造前前端对 `production_facts` / `decision_state` / `selected_not_approved`
 * **零引用** —— 后端把「采用 ≠ 批准」做成了 fail-closed 的铁律，界面上却完全
 * 看不到，用户只能从「质检全绿」误读成「可以交付」。本模块是那两档在 UI 上的
 * 唯一数据来源。
 *
 * 两条铁律在客户端侧的落法
 * ----------------------
 * 1. `select` 与 `approve` 是两个独立 mutation，**没有**任何合并入口。
 *    写一个 `adopt()` 同时调两个端点，就是把「采用隐式升级为批准」
 *    从服务端搬到客户端 —— 后端 409/403 的闸门就此形同虚设。
 * 2. 采用 / 批准后**只 invalidate，不做乐观更新**。
 *    这两类记录有审计留痕，前端乐观插一个「已批准」再回滚，
 *    会让界面短暂显示一个从未发生过的决策状态 —— 那正是本 ADR 要消除的错觉。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { productionFactsApi } from '@/api/client';
import type { ProductionDecisionState, ProductionIntent, ProductionMediaVersion } from '@/types';
import { queryKeys } from './keys';

/**
 * 某一镜最近一次冻结的生成意图。
 *
 * 为什么需要它
 * ------------
 * Shot Studio 的「候选对比 / 采用 / 批准」全部挂在 `intent_id` 上：
 * 候选列表按意图取，后端也只在意图存在时才谈得上「这一版」。
 * 而工作台的分镜画布（`StoryboardShot`）**不带 intent_id** —— 它是剧本 +
 * 磁盘产物的视图，不含生成事实。要把两者接起来，只能按
 * 「项目 + 集 + 镜号」回查意图：这正是后端 `list_intents` 的过滤维度。
 *
 * ⚠️ 取不到就取不到：`enabled` 要求四项齐全（项目 / 集 / 镜号），
 * 缺一项即不发请求，绝不用「拉全量再前端过滤」把噪音带进来。
 */
export function useShotIntents(
  projectKey: string,
  episodeNo: number | null | undefined,
  shotKey: string | null | undefined,
) {
  return useQuery({
    queryKey: queryKeys.productionIntents(projectKey, String(episodeNo ?? ''), shotKey ?? ''),
    queryFn: () =>
      productionFactsApi.listIntents({
        project: projectKey,
        episode: String(episodeNo ?? ''),
        shot_key: shotKey ?? '',
      }),
    enabled: !!projectKey && episodeNo != null && !!shotKey,
    // 意图是**冻结**事实（ADR-0002 铁律 2）：写入后不会原地变，
    // 只会新增一条派生。因此不需要轮询，staleTime 给足。
    staleTime: 60_000,
  });
}

/** 该镜最近一次意图（后端按 created_at DESC 返回，取首条）。 */
export function latestIntentOf(q: { data?: { intents?: ProductionIntent[] } }): ProductionIntent | null {
  const list = q.data?.intents;
  return Array.isArray(list) && list.length > 0 ? list[0] : null;
}

/** 候选版本列表（候选并排对比的数据源）。 */
export function useMediaVersions(intentId?: string) {
  return useQuery({
    queryKey: queryKeys.mediaVersions(intentId || ''),
    queryFn: () => productionFactsApi.listMedia(intentId),
    enabled: !!intentId,
    staleTime: 5_000,
  });
}

/** 单个候选的三态。徽标按这个渲染，不做二次推导。 */
export function useMediaDecision(mediaVersionId: string) {
  return useQuery({
    queryKey: queryKeys.mediaDecision(mediaVersionId),
    queryFn: () => productionFactsApi.getMedia(mediaVersionId),
    enabled: !!mediaVersionId,
    staleTime: 5_000,
  });
}

/**
 * 采用（创作决定）。
 *
 * `reason` 必填且**不做默认填充**：后端强制要求（否则 400），
 * 因为没有理由就无法回答「这镜为什么用这一版」。
 */
export function useSelectMediaVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      mediaVersionId: string;
      reason: string;
      decided_by?: string;
      project?: string;
      episode?: string;
      shot_key?: string;
    }) =>
      productionFactsApi.select(vars.mediaVersionId, {
        reason: vars.reason,
        decided_by: vars.decided_by,
        project: vars.project,
        episode: vars.episode,
        shot_key: vars.shot_key,
      }),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: queryKeys.mediaDecision(vars.mediaVersionId) });
      void qc.invalidateQueries({ queryKey: ['production-facts', 'media'] });
    },
  });
}

/**
 * 批准（放行决定）。
 *
 * ⚠️ `authorized_by` 由调用方**显式**提供，本模块不代填、不缓存、不预填。
 *    代填一个人名等于替用户伪造人工授权，让审批记录看起来有人负责、实际没有 ——
 *    后端拒绝 `system:` / `bot` / `auto` 是有道理的，客户端绕过它同样不可接受。
 */
export function useApproveMediaVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      mediaVersionId: string;
      authorized_by: string;
      reason?: string;
      authorization_ref?: string;
      note?: string;
    }) =>
      productionFactsApi.approve(vars.mediaVersionId, {
        authorized_by: vars.authorized_by,
        reason: vars.reason,
        authorization_ref: vars.authorization_ref,
        note: vars.note,
      }),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: queryKeys.mediaDecision(vars.mediaVersionId) });
      void qc.invalidateQueries({ queryKey: ['production-facts', 'media'] });
    },
  });
}

/**
 * 把后端 decision 结构**降级**成一个可渲染的保守态。
 *
 * 为什么需要它（fail-closed）
 * -------------------------
 * 后端契约里 `state` 只有 6 个取值，且 `selection_implies_approval` 恒为 false。
 * 但前端拿到的是一个网络响应 —— 它可能来自旧版本后端、被代理改写、或字段缺失。
 * 此时若 `switch` 落到 default 分支就渲染成「已批准」，等于在契约破裂时
 * **fail-open**：用户会看到一个绿色的「已批准」，而实际从未有人批准过。
 *
 * 因此：无法识别的一律降级成 `selected_not_approved`（青 + 警示），
 * 那是最接近事实的保守读法 —— 它说「还不能交付」，而 fail-open 会说「可以交付」。
 */
export function normalizeDecisionState(
  decision: ProductionDecisionState | undefined | null,
): ProductionDecisionState {
  const NONE: ProductionDecisionState = {
    state: 'none',
    selected: false,
    approved: false,
    selection_implies_approval: false,
    media_sha256_now: '',
    media_verified: false,
  };
  if (!decision || typeof decision !== 'object') return NONE;

  const known: ProductionDecisionState['state'][] = [
    'none', 'selected', 'selected_not_approved',
    'approved', 'approved_stale', 'approved_unverified',
  ];
  const state = decision.state;

  // 契约破裂：state 不是已知取值，或声称「采用蕴含批准」
  // → 一律降级，绝不按 approved 渲染。
  if (!known.includes(state) || decision.selection_implies_approval === true) {
    return { ...NONE, state: 'selected_not_approved', selected: !!decision.selected };
  }

  // approved / approved_stale 必须有 media_verified 与 approved 支撑；
  // 缺任一即降级 —— 服务端说「已批准」但自己都没验证磁盘现状，前端不能替它背书。
  if ((state === 'approved' || state === 'approved_stale')
      && (decision.approved !== true || decision.media_verified === false)) {
    return { ...decision, state: 'selected_not_approved' };
  }

  return decision;
}

/** 候选版本数组的安全解包（后端可能给 null）。 */
export function mediaVersionsOf(
  res: { media_versions?: ProductionMediaVersion[] } | undefined,
): ProductionMediaVersion[] {
  return Array.isArray(res?.media_versions) ? res.media_versions : [];
}
/**
 * 托管生产状态查询（ADR-0010 收敛的第二处）。
 *
 * 改造前有两个**各自独立**的轮询打同一个端点：
 *   - `ProductionProgress` 里的 `setInterval(poll, 3000)`（固定 3s）
 *   - `useProductionStatus` 里的自适应 setTimeout（有活 3s / 空闲 12s）
 * 两者同时挂载时同一份 `/api/autopilot/status` 每 3 秒被打两遍，
 * 屏幕上的内容却几乎相同 —— 这是「同一资源多处重复打后端」的典型浪费。
 * 现在共用一份缓存：同一时刻只有一个请求在飞，两个消费点读同一份数据。
 *
 * 节奏保持不变：有活儿 3s、空闲 12s 慢轮询兜底等新任务起来。
 * 全局开 3s 会在空闲时空转打接口（改造前注释里明确点过这个代价）。
 */
import { useQuery } from '@tanstack/react-query';
import { autopilotApi } from '@/api/client';
import type { AutopilotCurrent } from '@/types';
import { queryKeys } from './keys';
import { POLL_FAST, POLL_SLOW } from './options';

/** 有生产任务 → 3s；空闲 → 12s。取自 query 上一次的结果，自适应无需额外状态。 */
function adaptiveInterval(current: AutopilotCurrent | null | undefined): number {
  return current ? POLL_FAST : POLL_SLOW;
}

/**
 * 托管生产状态。
 *
 * ⚠️ **不**开 keepPreviousData（ADR-0010 §关键设计：轮询不换 key）。
 *    生产状态 key 只含 projectKey，切 Tab 回来直接命中缓存，
 *    不需要「保留旧数据」——这条路径由缓存命中而不是 stale 态解决。
 */
export function useAutopilotStatus(projectKey: string) {
  return useQuery({
    queryKey: queryKeys.autopilotStatus(projectKey),
    queryFn: async () => {
      const st = await autopilotApi.status(projectKey);
      return (st.current as AutopilotCurrent) || null;
    },
    enabled: !!projectKey,
    placeholderData: undefined,
    staleTime: 0,
    // 函数式 interval：按**上一次结果**决定下一次节奏，
    // 「有活就密、空闲就疏」的自适应逻辑因此从组件里消失了。
    refetchInterval: (q) => adaptiveInterval(q.state.data as AutopilotCurrent | null),
  });
}
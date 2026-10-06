/**
 * 任务状态查询（ADR-0005 Job / Attempt；ADR-0010 收敛用）。
 *
 * 为什么这类「按需启动、跑完自停」的轮询最适合 query
 * ----------------------------------------------
 * 手写实现要维护三样东西：一个 interval ref、一个 `mounted` 标志防卸载后 setState、
 * 以及「任务结束时停机 + 触发刷新」的收尾逻辑 —— 三者任何一处漏掉，
 * 表现都是「卸载后还在打后端」或「任务结束了还在轮询」。
 * query 把这三件事折成一条声明：`refetchInterval` 返回 `false` 即停机，
 * 停机与卸载都由生命周期托管。
 *
 * ⚠️ 任务进度**不**开 keepPreviousData（ADR-0010：轮询不换 key）。
 */
import { useQuery } from '@tanstack/react-query';
import { POLL_FAST } from './options';

/** `/api/generation/status/<task_id>` 的响应形状（只取用到的字段）。 */
export interface GenerationTaskStatus {
  status?: string;
  live?: Record<string, any> | null;
  [key: string]: any;
}

async function fetchGenerationStatus(taskId: string): Promise<GenerationTaskStatus> {
  const r = await fetch(`/api/generation/status/${encodeURIComponent(taskId)}`);
  return (await r.json()) as GenerationTaskStatus;
}

/** 终态：不是 running 就该停机（原实现也是这个判据）。 */
function isTerminal(status: string | undefined): boolean {
  return !!status && status !== 'running';
}

/**
 * 单个生成任务的状态。
 *
 * @param taskId 传 `null` 即禁用（还没有任务在跑）。
 *                「按需启动」因此退化成「给个 id」—— 不再需要手动 start/stop。
 */
export function useGenerationStatus(taskId: string | null) {
  return useQuery({
    queryKey: ['generation', 'status', taskId || ''] as const,
    queryFn: () => fetchGenerationStatus(taskId as string),
    enabled: !!taskId,
    placeholderData: undefined,
    staleTime: 0,
    // running 才轮询；拿到终态后 interval 变成 false，自动停机
    refetchInterval: (q) => {
      const st = (q.state.data as GenerationTaskStatus | undefined)?.status;
      return isTerminal(st) ? false : POLL_FAST;
    },
  });
}
/**
 * 超分域查询（ADR-0010 收敛用）。
 *
 * 只收敛「任务进度」这一处 —— `env`（链路自检）与 `artifacts`（产物列表）
 * 在本轮仍走原有的一问一答取数，因为它们是「打开面板取一次」的数据，
 * 不是需要持续跟踪的进度；把它们塞进 query 只会增加一层间接。
 * 轮询类才值得收敛，这正是 ADR-0010 的判据。
 */
import { useQuery } from '@tanstack/react-query';
import { upscaleApi } from '@/api/client';
import type { UpscaleTask } from '@/types';
import { queryKeys } from './keys';

/** 2.5s —— 与改造前 `setInterval(..., 2500)` 节奏一致。 */
const UPSCALE_POLL_MS = 2_500;

function isTerminal(status: string | undefined): boolean {
  return status === 'done' || status === 'error';
}

/**
 * 单个超分任务进度。
 *
 * @param taskId 传 `null` 即禁用（还没有提交任务）。终态自动停机。
 */
export function useUpscaleTask(taskId: string | null) {
  return useQuery({
    queryKey: queryKeys.upscaleTask(taskId || ''),
    queryFn: () => upscaleApi.status(taskId as string),
    enabled: !!taskId,
    placeholderData: undefined,
    staleTime: 0,
    refetchInterval: (q) => {
      const st = (q.state.data as UpscaleTask | undefined)?.status;
      return isTerminal(st) ? false : UPSCALE_POLL_MS;
    },
  });
}
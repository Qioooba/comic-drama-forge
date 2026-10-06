/**
 * 分镜 / 剧集查询（ADR-0010 收敛的第三、四处）。
 *
 * 为什么这两处**必须**开 keepPreviousData
 * ------------------------------------
 * 分镜画布与首尾帧计划都是**按集落盘**的（`storyboards/<项目>/epNN/`）。
 * 改造前切集时先清空再填充，用户看到的是「列表清空了」而不是「正在加载下一集」——
 * 内容高度从 0 跳到 N 行把下方内容顶下去（布局跳动），且很容易被误读为数据丢失，
 * 于是重复点击。
 *
 * 代价（必须一起承担）：保留期间显示的是**上一集**的数据。
 * 所以两个 hook 都导出 `isPreviousData`，调用方必须把它渲染成 stale 标识，
 * 并在 stale 期间**禁用**会写入的操作按钮。
 * 「保留旧数据」与「标 stale」是一套，只取其一是 ADR-0010 点名要避免的错误。
 */
import { useQuery } from '@tanstack/react-query';
import { episodesApi, storyboardApi, keyframesApi } from '@/api/client';
import { queryKeys } from './keys';
import { POLL_CANVAS, keepPreviousOptions } from './options';

/** 剧集列表。key 只含 novelId —— 换小说换 key，切集不换 key。 */
export function useEpisodes(novelId?: string) {
  return useQuery({
    queryKey: queryKeys.episodes(novelId || ''),
    queryFn: () => episodesApi.list(novelId as string),
    enabled: !!novelId,
    staleTime: 30_000,
  });
}

/**
 * 单集剧本正文（Shot Studio 的站位来源）。
 *
 * ⚠️ 为什么分镜画布不夠用：canvas 卡片带 `characters_in_shot`（角色**名字**），
 *    但**站位** `blocking: [{name, x, depth, facing}]` 只在剧本正文里
 *    （`app/novel_to_script.py::_norm_blocking` 归一后才落到 row 上）。
 *    少这一次请求，站位板就只能靠猜 —— 那正是它作为「生成前可核对事实」
 *    最不该有的状态。
 *
 * 不轮询：剧本只在用户显式编辑时变，且那条路径已自行失效画布缓存。
 */
export function useEpisodeScript(novelId?: string, episodeNo?: number | null) {
  return useQuery({
    queryKey: queryKeys.episodeScript(novelId || '', episodeNo),
    queryFn: () => episodesApi.get(novelId as string, episodeNo as number),
    enabled: !!novelId && episodeNo != null,
    staleTime: 30_000,
  });
}

/**
 * 分镜画布（按集）。
 *
 * ⚠️ 调用方**必须**处理 `isPreviousData`：为真时表示屏幕上是上一集的分镜，
 *    此时应给出 stale 标识并禁用「重生成」等写操作。
 */
export function useStoryboardCanvas(projectKey: string, episodeNo: number | null | undefined) {
  return useQuery({
    queryKey: queryKeys.storyboardCanvas(projectKey, episodeNo),
    queryFn: () => storyboardApi.canvas(projectKey, episodeNo ?? undefined),
    enabled: !!projectKey,
    ...keepPreviousOptions(POLL_CANVAS),
  });
}

/**
 * 首尾帧计划（按集）。同样必须配 stale 标识 —— 计划是「写」的前置条件，
 * 在上一集的计划上点「生成首尾帧」会写错集。
 */
export function useKeyframePlan(projectKey: string, episodeNo: number | null | undefined) {
  return useQuery({
    queryKey: queryKeys.keyframePlan(projectKey, episodeNo),
    queryFn: () => keyframesApi.plan(projectKey, episodeNo ?? undefined),
    enabled: !!projectKey,
    ...keepPreviousOptions(false),
  });
}
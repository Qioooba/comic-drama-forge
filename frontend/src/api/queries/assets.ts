/**
 * 项目资产查询（ADR-0010 收敛的第一处）。
 *
 * 改造前：总览统计卡、交付验收、关系图各自发请求拉**同一个**
 * `/api/projects/<key>/assets`，三个组件三份 `useState`，切 Tab 各拉一次
 * —— 这正是 ADR-0010 §背景第 5 点「重复打后端」的实例。
 * 现在一份缓存，多个组件复用；同一时刻只有一个请求在飞。
 */
import { useQuery } from '@tanstack/react-query';
import { queryKeys } from './keys';
import { POLL_SLOW } from './options';

export interface AssetItem {
  name: string;
  url?: string;
  file?: string;
  size?: number;
  /** 文件 mtime（后端下发的版本令牌）：拼进图片 URL 做缓存击穿，保证重生成后前端必刷新 */
  mtime?: number;
  [key: string]: any;
}

export interface ProjectAssets {
  success: boolean;
  project: string;
  gallery: Record<string, AssetItem[]>;
  storyboards: AssetItem[];
  videos: AssetItem[];
  final: AssetItem[];
  dub: AssetItem[];
  counts: Record<string, number>;
}

/** 12s —— 与改造前 `setInterval(tick, 12000)` 节奏一致，不改变可见行为。 */
const ASSETS_POLL_MS = POLL_SLOW;

async function fetchProjectAssets(projectKey: string): Promise<ProjectAssets> {
  const r = await fetch(`/api/projects/${encodeURIComponent(projectKey)}/assets`);
  // ⚠️ 失败必须**抛**出去，不能返回 null。
  //    改造前这里 `if (!r.ok) return null`：TanStack Query 会把「HTTP 失败」
  //    当成一次**成功**的结果（data === null），于是
  //      - 统计卡把 `assets?.counts?.x || 0` 画成 0 —— 与「真的没有资产」无法区分；
  //      - 监控面板看不到任何 error，重试逻辑（含全局 retry）完全不触发。
  //    「不阻塞页面」这个诉求由**调用方**承接：页面用 `isError` 显示一条
  //    可重试的错误条，其余内容照常渲染，而不是把整页打成错误态。
  //    用 null 冒充成功等于把「我不知道」说成「我知道了，是 0」。
  if (!r.ok) {
    throw new Error(`加载项目资产失败：HTTP ${r.status}`);
  }
  return (await r.json()) as ProjectAssets;
}

/**
 * 项目资产。跨 Tab 共享同一份缓存。
 *
 * @param projectKey 传空串即禁用（`enabled: false`）—— 切项目期间不发请求。
 */
export function useProjectAssets(projectKey: string) {
  return useQuery({
    queryKey: queryKeys.assets(projectKey),
    queryFn: () => fetchProjectAssets(projectKey),
    enabled: !!projectKey,
    // 不 keepPreviousData：资产按项目维度缓存，切项目必须显示新项目的真实状态。
    // 沿用上一项目的资产数去「展示」新项目，是比闪一下空态更糟的错误。
    placeholderData: undefined,
    staleTime: 0,
    refetchInterval: ASSETS_POLL_MS,
    // 页面不可见（切到别的标签页 / 窗口最小化）就**停**轮询。
    // 默认 true 会在用户根本没看这个页时继续打后端；本查询已有 12s 节奏，
    // 回到页面时立刻可见的那一次由 query 自身补上，不需要后台空转。
    refetchIntervalInBackground: false,
  });
}

/**
 * 资源地址解析。
 *
 * ⚠️ 逐字搬运自原页面实现，**不要**顺手"优化"：
 *    - 协议判定用 `http://` / `https://` 显式比对（不是 `startsWith('http')`）；
 *    - 非绝对路径才补 `/api/`，已是 `/` 开头的原样保留；
 *    - 版本令牌用 `encodeURIComponent` 编码。
 *    任何一处"等价简化"都会改变生成的 URL，而资产 URL 正是靠 ?v=<mtime>
 *    做缓存击穿的 —— 变了就等于用户静默地看到旧图，且没有任何报错。
 */
export function assetSrc(url?: string | null, version?: number | string | null): string | null {
  if (!url) return null;
  let out = url;
  if (!(out.startsWith('http://') || out.startsWith('https://'))) {
    out = out.startsWith('/') ? out : `/api/${out}`;
  }
  if (version !== undefined && version !== null && `${version}` !== '') {
    out += (out.includes('?') ? '&' : '?') + 'v=' + encodeURIComponent(String(version));
  }
  return out;
}
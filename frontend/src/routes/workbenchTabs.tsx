/**
 * 项目工作台的路由契约。
 *
 * 单一真源：路由表（router.tsx）、旧入口 301 层（routes/legacyRedirect.tsx）
 * 与二级导航都从这里取 tab 清单与顺序，改一处即可，不会出现「路由认了但
 * 导航没有」这种半改状态。
 *
 * ⚠️ 顺序即生产顺序（评估 §4.1）。改造前 Tab 按**功能模块**排
 * （overview/storyboard/qc/upscale/relation/audio/output），`upscale` 排在
 * `qc` 之后、`relation` 夹在中间，与「先出图 → 再质检 → 再超分 → 再合成」
 * 的实际流程不符。现在按生产顺序重排。
 */
import type { ReactNode } from 'react';
import {
  BarChart3, CheckCircle2, Clapperboard, Music, Network, Play, Share2, ZoomIn,
} from '@/components/ui/icons';

/** 项目级路由 segment。`autopilot` 是托管生产主路径（2026-10-07 恢复独立入口）。 */
export type WorkbenchTab =
  | 'overview'
  | 'autopilot'
  | 'storyboard'
  | 'qc'
  | 'upscale'
  | 'relation'
  | 'audio'
  | 'output';

/** 生产顺序的 tab 清单。索引即导航顺序，路由表按它生成。 */
export const WORKBENCH_TABS: WorkbenchTab[] = [
  'overview',    // 总览
  'storyboard',  // 镜头（Shot Studio）
  'autopilot',   // 生产
  'qc',          // 质检
  'upscale',     // 超分
  'audio',       // 声音
  'output',      // 交付
  'relation',    // 关系（关系图是**资产**的附属视图，排在交付之后而不是夹在分镜与质检中间）
];

const TAB_SET = new Set<string>(WORKBENCH_TABS);

/** 段名是否合法 —— 非法段一律重定向到 overview，不静默渲染空白页。 */
export function isWorkbenchTab(value: string | undefined): value is WorkbenchTab {
  return !!value && TAB_SET.has(value);
}

export interface WorkbenchTabMeta {
  id: WorkbenchTab;
  /** i18n key。沿用既有 wb.* 文案，一个字都不改（R2）。 */
  labelKey: string;
  icon: (cls: string) => ReactNode;
}

/** 导航用的展示元数据。i18n key 全部取自既有 wb.*，不新增文案。 */
export const WORKBENCH_TAB_META: Record<WorkbenchTab, WorkbenchTabMeta> = {
  overview: { id: 'overview', labelKey: 'wb.overview', icon: (c) => <BarChart3 className={c} /> },
  storyboard: { id: 'storyboard', labelKey: 'wb.storyboardHub', icon: (c) => <Clapperboard className={c} /> },
  autopilot: { id: 'autopilot', labelKey: 'wb.autopilot', icon: (c) => <Play className={c} /> },
  qc: { id: 'qc', labelKey: 'wb.qc', icon: (c) => <CheckCircle2 className={c} /> },
  upscale: { id: 'upscale', labelKey: 'wb.upscale', icon: (c) => <ZoomIn className={c} /> },
  audio: { id: 'audio', labelKey: 'wb.audio', icon: (c) => <Music className={c} /> },
  output: { id: 'output', labelKey: 'wb.output', icon: (c) => <Share2 className={c} /> },
  relation: { id: 'relation', labelKey: 'wb.relation', icon: (c) => <Network className={c} /> },
};

/** 生成单个 tab 的工作台深链。旧 hash 退役层也复用它，保证 301 目标与路由表一致。 */
export function workbenchPath(projectKey: string, tab: WorkbenchTab = 'overview'): string {
  return `/projects/${encodeURIComponent(projectKey)}/${tab}`;
}

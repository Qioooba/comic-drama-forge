// 画幅预设的 i18n 映射 + 端点不可用时的兜底字面量。
//
// 事实源是后端 app/style_catalog.json 的 aspect_presets（8 项）。
// 这里只保留两样前端才需要的东西：
//   1) aspect_id -> i18n key（文案归 i18n 管，不从后端取展示文案）；
//   2) 兜底字面量（/api/styles 不可用时仍能选画幅，不能阻断新建项目）。
//
// ⚠️ value 是**写入 config.aspect_ratio 的字面值**，必须与历史数据逐字一致
//   （style_kit.aspect_ratio 显式比例优先，中文后缀仅供人读），故 \u 转义书写。
import type { AspectPreset } from './types';

/** aspect_id -> i18n key。 */
export const ASPECT_LABEL_KEYS: Record<string, string> = {
  aspect_1x1: 'project.aspect1x1',
  aspect_2x3: 'project.aspect2x3',
  aspect_3x2: 'project.aspect3x2',
  aspect_3x4: 'project.aspect3x4',
  aspect_4x3: 'project.aspect4x3',
  aspect_9x16: 'project.aspect9x16',
  aspect_16x9: 'project.aspect16x9',
  aspect_21x9: 'project.aspect21x9',
};

/** 默认画幅（后端 default_aspect_id，与 style_kit.DEFAULT_RATIO 一致）。 */
export const DEFAULT_ASPECT_ID = 'aspect_16x9';

/** 默认画幅的 value 字面量（16:9 横屏）。 */
export const DEFAULT_ASPECT_VALUE = '16:9 \u6a2a\u5c4f';

/**
 * 兜底画幅预设：仅在 /api/styles 失败时使用，顺序与后端 catalog 一致。
 * ⚠️ value 逐字照抄后端，不能改中文后缀（历史项目里存的就是这些字符串）。
 */
export const FALLBACK_ASPECT_PRESETS: AspectPreset[] = [
  { aspect_id: 'aspect_1x1', ratio: '1:1', label: '1:1 \u65b9\u5f62', value: '1:1 \u65b9\u5f62', orientation: 'square', comfy_widget: '1:1 (Square)' },
  { aspect_id: 'aspect_2x3', ratio: '2:3', label: '2:3 \u7ad6\u5e45\uff08\u7167\u7247\uff09', value: '2:3 \u7ad6\u5e45', orientation: 'portrait', comfy_widget: '2:3 (Portrait Photo)' },
  { aspect_id: 'aspect_3x2', ratio: '3:2', label: '3:2 \u6a2a\u5e45\uff08\u7167\u7247\uff09', value: '3:2 \u6a2a\u5e45', orientation: 'landscape', comfy_widget: '3:2 (Photo)' },
  { aspect_id: 'aspect_3x4', ratio: '3:4', label: '3:4 \u7ad6\u5e45\uff08\u6807\u51c6\uff09', value: '3:4 \u7ad6\u5e45', orientation: 'portrait', comfy_widget: '3:4 (Portrait Standard)' },
  { aspect_id: 'aspect_4x3', ratio: '4:3', label: '4:3 \u6a2a\u5e45\uff08\u6807\u51c6\uff09', value: '4:3 \u6a2a\u5e45', orientation: 'landscape', comfy_widget: '4:3 (Standard)' },
  { aspect_id: 'aspect_9x16', ratio: '9:16', label: '9:16 \u7ad6\u5c4f\uff08\u77ed\u89c6\u9891\uff09', value: '9:16 \u7ad6\u5c4f', orientation: 'portrait', comfy_widget: '9:16 (Portrait Widescreen)' },
  { aspect_id: 'aspect_16x9', ratio: '16:9', label: '16:9 \u6a2a\u5c4f\uff08\u5bbd\u5c4f\uff09', value: '16:9 \u6a2a\u5c4f', orientation: 'landscape', comfy_widget: '16:9 (Widescreen)' },
  { aspect_id: 'aspect_21x9', ratio: '21:9', label: '21:9 \u8d85\u5bbd\uff08\u7535\u5f71\u611f\uff09', value: '21:9 \u8d85\u5bbd', orientation: 'ultrawide', comfy_widget: '21:9 (UltraWide)' },
];
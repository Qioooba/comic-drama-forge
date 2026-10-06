// 风格目录类型（对齐 GET /api/styles 的响应体）。
// 后端是唯一事实源（app/style_catalog.json），这里的类型只是它的镜像。
// ⚠️ 本文件零 CJK 约定（与 pages/ProjectsPage.tsx 同规则）：中文一律 \u 转义。

/** 风格分类（后端 categories[].id）。 */
export type StyleCategoryId = '2d' | '3d' | 'real';

/** 画廊分类筛选项：三个分类 + 'all'。 */
export type StyleCatFilter = 'all' | StyleCategoryId;

/** 一个风格条目。 */
export interface StyleEntry {
  /** 稳定标识（不含中文、改名不变）；历史与 URL 都用它，不用 label。 */
  style_id: string;
  /** 展示名；同时是写入 config.style 的旧自由文本字面值。 */
  label: string;
  /** === label，向后兼容字段。 */
  value: string;
  category: StyleCategoryId;
  /** 缩略图文件名（不含目录）；URL 仍由前端 import 决定。 */
  thumbnail: string;
  /** 该风格建议的默认画幅（'9:16' 等），仅作解析回退。 */
  aspect_default: string;
  /** 注入链路的风格后缀正文（不含画幅 token 与画质收尾）。 */
  positive_suffix: string;
  /** 该风格需要压制的负向词。 */
  negative_suffix: string[];
  /** 历史上出现过的其它写法（同样能解析回本条目）。 */
  aliases: string[];
}

/** 风格分类。 */
export interface StyleCategory {
  id: StyleCategoryId;
  label: string;
  order: number;
}

/** 画幅预设（8 种比例，与 ComfyUI ResolutionSelector 下拉一致）。 */
export interface AspectPreset {
  aspect_id: string;
  ratio: string;
  label: string;
  /** 写入 config.aspect_ratio 的字面值（与历史数据逐字一致）。 */
  value: string;
  orientation: string;
  comfy_widget: string;
}

/** GET /api/styles 响应体。 */
export interface StylesResponse {
  success: boolean;
  schema_version: number;
  catalog_version?: string;
  default_style_id: string;
  default_aspect_id: string;
  categories: StyleCategory[];
  styles: StyleEntry[];
  aspect_presets: AspectPreset[];
  counts: Record<string, number>;
  error?: string;
}

/** 自定义风格选项的哨兵值（沿用既有约定，勿改）。 */
export const CUSTOM_STYLE = '__custom__';
// 风格库 feature 出口：消费 GET /api/styles 的画廊组件与数据 hook。
export { fetchStyles } from './api';
export { StyleGallery } from './StyleGallery';
export type { StyleGalleryProps } from './StyleGallery';
export { useStyles } from './useStyles';
export type { UseStylesResult } from './useStyles';
export { thumbFor, STYLE_THUMBNAILS, THUMBNAIL_COUNT } from './thumbnails';
export {
  ASPECT_LABEL_KEYS,
  DEFAULT_ASPECT_ID,
  DEFAULT_ASPECT_VALUE,
  FALLBACK_ASPECT_PRESETS,
} from './aspectPresets';
export { CUSTOM_STYLE } from './types';
export type {
  AspectPreset,
  StyleCatFilter,
  StyleCategory,
  StyleCategoryId,
  StyleEntry,
  StylesResponse,
} from './types';
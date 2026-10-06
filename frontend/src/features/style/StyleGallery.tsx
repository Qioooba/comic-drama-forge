// 风格画廊：分类标签 + 缩略图卡片 + 自定义风格卡。
//
// 2026-10-07 起数据来自 GET /api/styles（后端 app/style_catalog.json 为唯一事实源），
// 页面不再内联 61 条中文文案。本组件只负责**渲染与交互**，不持有目录数据。
//
// ⚠️ 交互与文案约定（勿改，改了会破坏既有 UI 一致性）：
//   - 卡片选中态判据是 **value**（旧自由文本字面值），不是 style_id ——
//     config.style 至今仍存自由文本，写 style_id 会打断既有生成链路；
//     style_id 的用途是稳定标识 / 历史对照（见 style_kit.resolve_style_id）。
//   - 自定义风格哨兵值 '__custom__' 沿用既有约定。
//   - 分类顺序：全部 → 2D → 3D → 真人（对标 pavo 风格库）。
import React from 'react';
import { useApp } from '@/context/AppContext';
import { Input, Skeleton } from '@/components/ui';
import { Check, Plus } from '@/components/ui/icons';
import { thumbFor } from './thumbnails';
import { CUSTOM_STYLE } from './types';
import type { StyleCatFilter, StyleEntry } from './types';

/** 分类筛选项（labelKey 走 i18n，顺序对标 pavo 风格库）。 */
const STYLE_CATS: { id: StyleCatFilter; labelKey: string }[] = [
  { id: 'all', labelKey: 'project.styleCatAll' },
  { id: '2d', labelKey: 'project.styleCat2d' },
  { id: '3d', labelKey: 'project.styleCat3d' },
  { id: 'real', labelKey: 'project.styleCatReal' },
];

export interface StyleGalleryProps {
  styles: StyleEntry[];
  counts: Record<string, number>;
  cat: StyleCatFilter;
  onCatChange: (cat: StyleCatFilter) => void;
  /** 当前选中的 value（自定义模式为 CUSTOM_STYLE）。 */
  selected: string;
  onSelect: (value: string) => void;
  customStyle: string;
  onCustomChange: (value: string) => void;
  loading: boolean;
  /** 目录取数失败时的可读报错（空串表示正常）。 */
  error: string;
}

export function StyleGallery({
  styles,
  counts,
  cat,
  onCatChange,
  selected,
  onSelect,
  customStyle,
  onCustomChange,
  loading,
  error,
}: StyleGalleryProps) {
  const { t } = useApp();
  const visible = cat === 'all' ? styles : styles.filter((s) => s.category === cat);
  const countOf = (id: StyleCatFilter) =>
    id === 'all' ? (counts.all ?? styles.length) : (counts[id] ?? 0);

  return (
    <div>
      {/* 分类标签：全部 / 2D / 3D / 真人，后缀显示该分类风格数 */}
      <div className="mb-2 flex flex-wrap gap-1.5">
        {STYLE_CATS.map((c) => (
          <button
            key={c.id}
            type="button"
            onClick={() => onCatChange(c.id)}
            aria-pressed={cat === c.id}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ${
              cat === c.id
                ? 'bg-ink-1 text-white'
                : 'border border-line text-ink-2 hover:bg-surface-2'
            }`}
          >
            {t(c.labelKey)}
            <span className="ml-1 opacity-60">{countOf(c.id)}</span>
          </button>
        ))}
      </div>

      {/* 目录取数失败：显式报错但**不阻断**自定义风格（新建项目仍要走得通） */}
      {error && (
        <p className="mb-2 rounded-md border border-line px-2 py-1 text-[11px] text-ink-3">
          {error}
        </p>
      )}

      {loading && styles.length === 0 ? (
        <div className="max-h-72 grid grid-cols-3 gap-2 pr-1 sm:grid-cols-4 md:grid-cols-5">
          {Array.from({ length: 10 }).map((_, i) => (
            <Skeleton key={i} className="aspect-[3/4] w-full" />
          ))}
        </div>
      ) : (
        /* 缩略图卡片网格：自定义卡在最前（对标 pavo 风格库「自定义风格」首位卡片）。
           61 张会很高 → 限高滚动，保持弹窗整体可操作（pavo 风格库弹层同理）。 */
        <div className="max-h-72 grid grid-cols-3 gap-2 overflow-y-auto pr-1 sm:grid-cols-4 md:grid-cols-5">
          <button
            type="button"
            onClick={() => onSelect(CUSTOM_STYLE)}
            aria-pressed={selected === CUSTOM_STYLE}
            title={t('project.styleCustom')}
            className={`flex aspect-[3/4] flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed p-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ${
              selected === CUSTOM_STYLE
                ? 'border-brand bg-brand-subtle text-ink-1 ring-2 ring-brand/30'
                : 'border-line-strong text-ink-3 hover:bg-surface-2'
            }`}
          >
            <Plus className="h-5 w-5" />
            <span className="px-0.5 text-center text-[11px] leading-tight">
              {t('project.styleCustom')}
            </span>
          </button>
          {visible.map((s) => {
            const thumb = thumbFor(s.style_id);
            const isSelected = selected === s.value;
            return (
              <button
                key={s.style_id}
                type="button"
                onClick={() => onSelect(s.value)}
                aria-pressed={isSelected}
                title={`${s.label} · ${s.aspect_default}`}
                className={`group relative aspect-[3/4] overflow-hidden rounded-lg border transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ${
                  isSelected
                    ? 'border-brand ring-2 ring-brand/40'
                    : 'border-line hover:border-line-strong'
                }`}
              >
                {thumb ? (
                  <img
                    src={thumb}
                    alt={s.label}
                    className="absolute inset-0 h-full w-full object-cover"
                    loading="lazy"
                  />
                ) : (
                  /* 映射漏图时的兜底（不崩，且能看出是哪一项） */
                  <span className="absolute inset-0 flex items-center justify-center px-1 text-center text-[10px] text-ink-3">
                    {s.label}
                  </span>
                )}
                {/* 底部文字遮罩：沿用 Modal 遮罩 slate-900 的例外约定（照片上保证可读） */}
                <span className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-slate-900/85 via-slate-900/30 to-transparent px-1.5 pb-1 pt-5 text-left text-[11px] font-medium leading-tight text-white">
                  {s.label}
                </span>
                {isSelected && (
                  <span className="absolute right-1 top-1 flex h-5 w-5 items-center justify-center rounded-full bg-brand text-white">
                    <Check className="h-3 w-3" />
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      {selected === CUSTOM_STYLE && (
        <div className="mt-2">
          <Input
            value={customStyle}
            onChange={onCustomChange}
            placeholder={t('project.styleCustomPlaceholder')}
          />
        </div>
      )}
    </div>
  );
}
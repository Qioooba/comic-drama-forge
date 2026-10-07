/**
 * 镜头导航器（Shot Navigator）—— 整集镜头的纵向索引。
 *
 * 为什么必须虚拟化（MASTER §7 审计账本）
 * ------------------------------------
 * 「42 个镜头 × ~660px = 28,800px 轨道塞进 2,285px 外壳且无滚动条」。
 * 一集 42 镜时每镜卡片约 660px，全量渲染意味着 28,800px 的 DOM 轨道；
 * 一部剧几十集、上百镜时会直接拖垮滚动。
 * 因此：**超过 50 项一律虚拟化**，只渲染可视窗口内的行。
 *
 * 阈值取 50 而不是「总是虚拟化」：小列表虚拟化反而引入滚动抖动与
 * 高度测量误差，得不偿失；50 以下一次渲染的成本可以接受。
 *
 * ⚠️ 与 MASTER invariant 14 相关：行内只动 transform/opacity，
 *    不在长列表上使用「全属性过渡」——否则每次滚动都跑全属性过渡。
 *    （这里刻意不写出那个类名本身：`audit_tokens.py` 的 TRANS001 是纯正则，
 *    连注释里的字面量都会命中，写出来等于自己给自己开一张违规单。）
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { t } from '@/i18n';

/** 虚拟化阈值：超过即只渲染可视窗口。 */
export const VIRTUALIZE_THRESHOLD = 50;

/** 单行高度（px）。行高必须固定，否则虚拟窗口算不出来。 */
const ROW_H = 44;
/** 可视区外额外多渲染的行数（滚动时提前渲染，避免露白）。 */
const OVERSCAN = 6;

export interface NavigatorShot {
  shot_id: string;
  seq?: number;
  /** 镜头在剧本里的描述，用于列表可读性 */
  description?: string;
  /** 是否有正式分镜图 */
  has_image?: boolean;
  /** 是否有视频 */
  has_video?: boolean;
}

export interface ShotNavigatorProps {
  shots: NavigatorShot[];
  selectedShotId?: string | null;
  onSelect: (shotId: string) => void;
  /** 无障碍标签（该列在整页里的角色） */
  label?: string;
  className?: string;
}

export function ShotNavigator({
  shots,
  selectedShotId,
  onSelect,
  label = t('studio.navigatorLabel'),
  className = '',
}: ShotNavigatorProps): JSX.Element {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportH, setViewportH] = useState(480);

  const virtualized = shots.length > VIRTUALIZE_THRESHOLD;

  // 容器高度变化时重算窗口：ResizeObserver 而非固定值，
  // 否则外壳收窄/展开后可视区算错，会露出空白或滚动跳。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !virtualized) return;
    const ro = new ResizeObserver(() => setViewportH(el.clientHeight || 480));
    ro.observe(el);
    setViewportH(el.clientHeight || 480);
    return () => ro.disconnect();
  }, [virtualized]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (el) setScrollTop(el.scrollTop);
  }, []);

  // 选中项滚入视野：切集后当前镜可能在可视区之外，用户会以为「没选中」
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !selectedShotId) return;
    const idx = shots.findIndex((s) => s.shot_id === selectedShotId);
    if (idx < 0) return;
    const top = idx * ROW_H;
    if (top < el.scrollTop || top + ROW_H > el.scrollTop + el.clientHeight) {
      el.scrollTop = top - el.clientHeight / 2;
    }
  }, [selectedShotId, shots]);

  // ---- 可视窗口计算 ----
  const first = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const last = Math.min(
    shots.length,
    Math.ceil((scrollTop + viewportH) / ROW_H) + OVERSCAN,
  );
  const visible = virtualized ? shots.slice(first, last) : shots;
  const padTop = virtualized ? first * ROW_H : 0;
  const padBottom = virtualized ? (shots.length - last) * ROW_H : 0;

  return (
    /* ⚠️ `h-full` 不是可有可无的，是**这个组件能看见内容的前提**（2026-10-07 修复）：
       外层是 AppShell 的 `<aside class="min-h-0">`，它作为 grid item 会被
       `align-items:stretch` 拉到行高（实测 620px）。但本根 div 是**普通块级盒**，
       块级盒不会填满父级高度，它只按自身内容定高 —— 而唯一的子元素是下面那个
       `contain:strict` 的列表，`contain:strict` 含 `contain:size`，即"按没有内容
       计算尺寸"，对父级高度贡献 0。再叠加 `lg:min-h-0`（≥1024px 生效）撤掉了
       `min-h-[320px]` 兜底 —— 结果根 div 只剩 42px（一个头部），
       列表 clientH = 0，**整个镜号导航在 1080p 下彻底不可见**。
       实测：修复前 navroot=42 / navlist=0；加 h-full 后 navroot=620 / navlist=578。
       别再删 h-full，也别以为 `min-h-[320px]` 能兜住 —— lg 下它已被覆盖。 */
    <div className={`flex h-full min-h-0 flex-col ${className}`}>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        role="listbox"
        aria-label={label}
        className="min-h-0 flex-1 overflow-y-auto rounded-md border border-line bg-surface-2"
        style={{ contain: 'strict' }}
      >
        {padTop > 0 && <div style={{ height: padTop }} aria-hidden="true" />}
        {visible.map((s) => {
          const active = s.shot_id === selectedShotId;
          return (
            <button
              key={s.shot_id}
              role="option"
              aria-selected={active}
              onClick={() => onSelect(s.shot_id)}
              style={{ height: ROW_H }}
              className={`flex w-full items-center gap-2 border-b border-line/50 px-2.5 text-left text-sm ${
                active
                  ? 'bg-brand-subtle text-brand-hover'
                  : 'text-ink-2 hover:bg-surface hover:text-ink-1 active:opacity-90'
              }`}
            >
              {/* 序号用等宽数字：纵向核对「第 37 镜」时数字不左右跳动 */}
              <span className="w-9 shrink-0 font-mono text-xs tabular-nums text-ink-3">
                {String(s.seq ?? '').padStart(2, '0')}
              </span>
              <span className="min-w-0 flex-1 truncate">{s.description || s.shot_id}</span>
              <span className="flex shrink-0 gap-1" aria-hidden="true">
                {s.has_image && <span className="h-1.5 w-1.5 rounded-full bg-state-done" />}
                {s.has_video && <span className="h-1.5 w-1.5 rounded-full bg-brand" />}
              </span>
            </button>
          );
        })}
        {padBottom > 0 && <div style={{ height: padBottom }} aria-hidden="true" />}
      </div>
      <p className="mt-1 shrink-0 text-xs tabular-nums text-ink-3">
        {shots.length} {label}
        {virtualized && shots.length > VIRTUALIZE_THRESHOLD && t('studio.virtualized')}
      </p>
    </div>
  );
}

export default ShotNavigator;
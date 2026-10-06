/**
 * 首尾帧桥接控制（Frame Bridge）。
 *
 * 首尾帧是「镜头能不能对上」的关键：上一镜的尾帧与下一镜的首帧接不上，
 * 成片就会有跳切。桥接控制把两端并排放在一起，让用户在生成前就能发现不接。
 *
 * ⚠️ 为什么用 `bg-media` 而不是普通 surface：图片/视频在深底上才不刺眼，
 *    也避免浅色皮肤里出现一块突兀的深色区域（MASTER §2.1 明确两套皮肤同值）。
 */
import React from 'react';
import { Button } from '@/components/ui';
import { assetSrc } from '@/api/queries';

export interface FrameBridgeControlsProps {
  /** 上一镜尾帧 URL（可空） */
  startFrameUrl?: string | null;
  /** 本镜首帧 URL（可空） */
  endFrameUrl?: string | null;
  /** 本镜当前选中的首帧版本（用于「重生成首帧」） */
  startVersion?: number | string | null;
  endVersion?: number | string | null;
  onRegenerateStart?: () => void;
  onRegenerateEnd?: () => void;
  onCommit?: () => void;
  busy?: boolean;
  disabledReason?: string;
}

export function FrameBridgeControls({
  startFrameUrl,
  endFrameUrl,
  startVersion,
  endVersion,
  onRegenerateStart,
  onRegenerateEnd,
  onCommit,
  busy = false,
  disabledReason,
}: FrameBridgeControlsProps): JSX.Element {
  const startSrc = assetSrc(startFrameUrl, startVersion);
  const endSrc = assetSrc(endFrameUrl, endVersion);
  // disabled 必须配相邻原因（MASTER invariant 6）
  const blocked = busy || !!disabledReason;

  return (
    <div className="space-y-2 rounded-lg border border-line bg-surface p-3">
      <h3 className="text-sm font-semibold text-ink-1">首尾帧桥接</h3>

      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2">
        <FrameSlot label="首帧" src={startSrc} onRegenerate={onRegenerateStart} disabled={blocked} />
        {/* 桥接符号：把「两端接不接得上」这件事画出来 */}
        <span aria-hidden="true" className="px-1 font-mono text-lg text-ink-3">⇄</span>
        <FrameSlot label="尾帧" src={endSrc} onRegenerate={onRegenerateEnd} disabled={blocked} />
      </div>

      {disabledReason && (
        <p className="text-xs text-ink-3">{disabledReason}</p>
      )}

      {onCommit && (
        <Button
          variant="secondary"
          onClick={onCommit}
          disabled={blocked}
          title={disabledReason}
          className="w-full"
        >
          确认桥接
        </Button>
      )}
    </div>
  );
}

function FrameSlot({
  label,
  src,
  onRegenerate,
  disabled,
}: {
  label: string;
  src: string | null;
  onRegenerate?: () => void;
  disabled: boolean;
}): JSX.Element {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-ink-2">{label}</span>
        {onRegenerate && (
          <button
            onClick={onRegenerate}
            disabled={disabled}
            title={disabled ? '当前不可重生成' : `重生成${label}`}
            className="rounded border border-line px-1.5 py-0.5 text-xs text-ink-2 hover:border-brand hover:text-brand disabled:opacity-50 disabled:cursor-not-allowed"
          >
            重生成
          </button>
        )}
      </div>
      {/* 媒体舞台面：比 UI 更深，图片在其上不被界面色干扰 */}
      <div className="flex aspect-video items-center justify-center overflow-hidden rounded bg-media">
        {src ? (
          <img src={src} alt={label} className="h-full w-full object-contain" loading="lazy" />
        ) : (
          <span className="text-xs text-ink-3">未生成{label}</span>
        )}
      </div>
    </div>
  );
}

export default FrameBridgeControls;
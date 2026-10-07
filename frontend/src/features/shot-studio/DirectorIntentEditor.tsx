/**
 * 导演意图编辑器（运镜 / 情绪 / 视线）。
 *
 * 为什么意图要「冻结后不可改」（ADR-0002 铁律 3）
 * --------------------------------------------
 * 意图是一次生成的**输入**，不是它的输出。改输入却不派生新 intent，
 * 事后就无法回答「这一版到底是按什么生成的」—— 那正是 ADR-0002 要根治的问题。
 * 因此本组件的交互语义是：**编辑草稿 → 派生新意图**，而不是就地保存覆盖。
 * 界面上把这一点写出来，避免用户以为「改了就是改了」。
 */
import React, { useEffect, useState } from 'react';
import { Button } from '@/components/ui';
import { t } from '@/i18n';

/** 运镜：短剧最常用的几种，取值与后端 motion 字段对齐。 */
export const CAMERA_MOVES = [
  'static', 'push_in', 'pull_out', 'pan_left', 'pan_right',
  'tilt_up', 'tilt_down', 'handheld', 'tracking',
] as const;
export type CameraMove = typeof CAMERA_MOVES[number];

/** 情绪档位。 */
export const EMOTIONS = ['calm', 'tense', 'warm', 'cold', 'melancholy', 'urgent'] as const;
export type Emotion = typeof EMOTIONS[number];

/** 视线方向：角色看哪里，直接影响构图。 */
export const GAZE_TARGETS = ['camera', 'subject', 'off_screen', 'down', 'up'] as const;
export type GazeTarget = typeof GAZE_TARGETS[number];

export interface DirectorIntent {
  /** 意图 id（冻结态）。空 = 尚未创建 */
  intentId?: string;
  cameraMove?: string;
  emotion?: string;
  gaze?: string;
  promptZh?: string;
  /** 冻结后不可原地改：任何编辑都必须派生新 intent */
  frozen?: boolean;
}

export interface DirectorIntentEditorProps {
  intent: DirectorIntent;
  onChange: (patch: Partial<DirectorIntent>) => void;
  /** 派生新意图（冻结态下唯一合法的保存方式） */
  onDerive?: () => void;
  busy?: boolean;
}

/**
 * 枚举取值 → 标签的 i18n 键。取值本身是后端约定的英文 token，不翻译；
 * 只有下拉里显示给用户的标签走语言包。缺键时原样回显取值，不静默隐藏。
 */
const LABEL_KEYS: Record<string, string> = {
  static: 'studio.enum.camera.static', push_in: 'studio.enum.camera.pushIn', pull_out: 'studio.enum.camera.pullOut',
  pan_left: 'studio.enum.camera.panLeft', pan_right: 'studio.enum.camera.panRight',
  tilt_up: 'studio.enum.camera.tiltUp', tilt_down: 'studio.enum.camera.tiltDown',
  handheld: 'studio.enum.camera.handheld', tracking: 'studio.enum.camera.tracking',
  calm: 'studio.enum.emotion.calm', tense: 'studio.enum.emotion.tense', warm: 'studio.enum.emotion.warm',
  cold: 'studio.enum.emotion.cold', melancholy: 'studio.enum.emotion.melancholy', urgent: 'studio.enum.emotion.urgent',
  camera: 'studio.enum.gaze.camera', subject: 'studio.enum.gaze.subject', off_screen: 'studio.enum.gaze.offScreen',
  down: 'studio.enum.gaze.down', up: 'studio.enum.gaze.up',
};

const enumLabel = (value: string): string => (LABEL_KEYS[value] ? t(LABEL_KEYS[value]) : value);

const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

export function DirectorIntentEditor({
  intent,
  onChange,
  onDerive,
  busy = false,
}: DirectorIntentEditorProps): JSX.Element {
  // 本地草稿与提交分离：每次 keystroke 都写回上游会让父级整树重渲染
  const [draft, setDraft] = useState(intent.promptZh || '');
  useEffect(() => { setDraft(intent.promptZh || ''); }, [intent.promptZh]);

  return (
    <div className="space-y-3 rounded-lg border border-line bg-surface p-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-ink-1">{t('studio.intentTitle')}</h3>
        {intent.frozen && (
          <span className="rounded-full border border-state-skipped bg-surface-2 px-2 py-0.5 text-xs text-ink-2">
            {t('studio.frozen')}
          </span>
        )}
      </div>

      {intent.frozen && (
        // invariant 1（Never silently fall back）：冻结不是静默失效，
        // 降级路径必须告知用户，并给出恢复动作。
        <p className="rounded border border-warning/40 bg-warning-subtle px-2.5 py-1.5 text-xs text-warning-strong">
          {t('studio.frozenHint')}
        </p>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <SelectField
          id="intent-camera"
          label={t('studio.cameraMove')}
          value={intent.cameraMove || ''}
          options={CAMERA_MOVES}
          disabled={busy}
          onChange={(v) => onChange({ cameraMove: v })}
        />
        <SelectField
          id="intent-emotion"
          label={t('studio.emotion')}
          value={intent.emotion || ''}
          options={EMOTIONS}
          disabled={busy}
          onChange={(v) => onChange({ emotion: v })}
        />
        <SelectField
          id="intent-gaze"
          label={t('studio.gaze')}
          value={intent.gaze || ''}
          options={GAZE_TARGETS}
          disabled={busy}
          onChange={(v) => onChange({ gaze: v })}
        />
      </div>

      <div>
        <label className="mb-1 block text-xs text-ink-2" htmlFor="director-intent-prompt">
          {t('studio.promptLabel')}
        </label>
        <textarea
          id="director-intent-prompt"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => onChange({ promptZh: draft })}
          disabled={busy}
          rows={3}
          placeholder={t('studio.promptPlaceholder')}
          className={`w-full rounded border border-line bg-surface-2 px-2.5 py-2 text-sm text-ink-1 placeholder:text-ink-3 disabled:opacity-60 ${FOCUS_RING}`}
        />
        {/* 正文可读宽度上限（--measure）：长文本铺满 2100px 外壳会难以阅读 */}
        <p className="mt-1 max-w-measure text-xs text-ink-3">
          {t('studio.promptHint')}
        </p>
      </div>

      {intent.frozen && onDerive && (
        <Button variant="secondary" onClick={onDerive} disabled={busy} className="w-full">
          {t('studio.derive')}
        </Button>
      )}
    </div>
  );
}

function SelectField({
  id,
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  /** 显式传入 id：label 现在会随语言变化，不能再拿它拼 DOM id */
  id: string;
  label: string;
  value: string;
  options: readonly string[];
  onChange: (v: string) => void;
  disabled?: boolean;
}): JSX.Element {
  return (
    <div>
      <label className="mb-1 block text-xs text-ink-2" htmlFor={id}>{label}</label>
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={`h-control-compact w-full rounded border border-line bg-surface-2 px-2 text-sm text-ink-1 disabled:opacity-60 disabled:cursor-not-allowed ${FOCUS_RING}`}
      >
        <option value="">{t('studio.unspecified')}</option>
        {options.map((o) => (
          <option key={o} value={o}>{enumLabel(o)}</option>
        ))}
      </select>
    </div>
  );
}

export default DirectorIntentEditor;
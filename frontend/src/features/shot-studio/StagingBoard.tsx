/**
 * 站位板（Staging Board）—— 谁在画面里、在哪、看向哪。
 *
 * 多人镜出图崩掉的常见原因是站位没定：三个人都在画面中央，
 * 或者说话的人在背后。站位板把「角色 → 位置 → 视线」显式画出来，
 * 让它在生成前成为可核对的事实，而不是留给出图碰运气。
 *
 * ⚠️ 它**不**替代分镜：站位是导演意图的一部分，写进 intent 而非
 *    覆盖已生成的图（ADR-0002 铁律 3：意图冻结、变更派生新意图）。
 */
import React from 'react';
import { assetSrc } from '@/api/queries';

export interface StagedCharacter {
  /** 角色 id */
  characterId: string;
  name: string;
  /** 画面位置 */
  stage?: 'left' | 'center' | 'right';
  /** 视线 */
  gaze?: string;
  /** 站位参考图 */
  portraitUrl?: string | null;
  portraitVersion?: number | string | null;
}

export interface StagingBoardProps {
  characters: StagedCharacter[];
  onChange: (characterId: string, patch: Partial<StagedCharacter>) => void;
  /** 只读审片模式：不可编辑但仍要显示（invariant 6 的反面：可看不可改） */
  readOnly?: boolean;
}

const STAGES: { id: NonNullable<StagedCharacter['stage']>; label: string }[] = [
  { id: 'left', label: '左' },
  { id: 'center', label: '中' },
  { id: 'right', label: '右' },
];

/** 站位 → 网格列（1/3、2/3、3/3），用 grid 而不是绝对定位，便于响应式 */
const STAGE_COL: Record<string, string> = {
  left: 'col-start-1',
  center: 'col-start-2',
  right: 'col-start-3',
};

export function StagingBoard({
  characters,
  onChange,
  readOnly = false,
}: StagingBoardProps): JSX.Element {
  if (characters.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-surface p-4 text-center text-sm text-ink-2">
        这一镜没有出场角色。
      </div>
    );
  }

  return (
    <div className="space-y-2 rounded-lg border border-line bg-surface p-3">
      <h3 className="text-sm font-semibold text-ink-1">站位</h3>
      <ul className="space-y-2">
        {characters.map((c) => {
          const src = assetSrc(c.portraitUrl, c.portraitVersion);
          return (
            <li
              key={c.characterId}
              className="grid grid-cols-[auto_1fr] items-center gap-2 rounded border border-line bg-surface-2 px-2 py-1.5"
            >
              <div className="flex h-9 w-9 items-center justify-center overflow-hidden rounded-full bg-media">
                {src ? (
                  <img src={src} alt="" className="h-full w-full object-cover" loading="lazy" />
                ) : (
                  <span aria-hidden="true" className="text-xs text-ink-3">?</span>
                )}
              </div>

              <div className="min-w-0">
                <p className="truncate text-sm text-ink-1">{c.name}</p>
                <div className="mt-1 flex flex-wrap items-center gap-1">
                  {STAGES.map((s) => {
                    const active = (c.stage || 'center') === s.id;
                    return (
                      <button
                        key={s.id}
                        disabled={readOnly}
                        onClick={() => onChange(c.characterId, { stage: s.id })}
                        aria-pressed={active}
                        title={readOnly ? '只读模式' : `移到${s.label}侧`}
                        className={`min-w-hit-target rounded px-2 py-0.5 text-xs ${
                          active
                            ? 'bg-brand-subtle text-brand'
                            : 'text-ink-3 hover:bg-surface hover:text-ink-1'
                        } disabled:cursor-not-allowed disabled:opacity-60`}
                      >
                        {s.label}
                      </button>
                    );
                  })}
                  {c.gaze && (
                    <span className="rounded bg-surface px-1.5 py-0.5 text-xs text-ink-2">
                      视线：{c.gaze}
                    </span>
                  )}
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      {/* 舞台俯视图：把站位关系一眼看清，而不是只看三行文字 */}
      <div className="grid grid-cols-3 gap-2 rounded border border-line bg-media p-2">
        {STAGES.map((s) => {
          const here = characters.filter((c) => (c.stage || 'center') === s.id);
          return (
            <div
              key={s.id}
              className={`flex min-h-[72px] flex-col items-center justify-center gap-1 rounded border border-line/60 ${STAGE_COL[s.id]}`}
            >
              <span className="text-xs text-ink-3">{s.label}</span>
              {here.length === 0 ? (
                <span className="text-xs text-ink-3">—</span>
              ) : (
                here.map((c) => (
                  <span key={c.characterId} className="max-w-full truncate text-xs text-ink-1">
                    {c.name}
                  </span>
                ))
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default StagingBoard;
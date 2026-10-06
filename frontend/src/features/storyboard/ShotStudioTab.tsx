/**
 * Shot Studio 接线层（分镜域 → Shot Studio）。
 *
 * 这个文件存在的唯一理由
 * ----------------------
 * `features/shot-studio/**` 里 9 个组件、约 1300 行，此前**没有任何人导入**：
 * 代码正确、能编译，但用户永远看不到。ADR-0002 的「采用 ≠ 批准」因此只活在
 * 领域层与 API 层，`docs/release/g0-contract.json` 记的
 * `selected-not-approved-ui-gap` 一直没关掉。
 *
 * 本文件把它接到**真实数据**上，且不造任何假数据源
 * ------------------------------------------------
 *   分镜画布 cards ──▶ ShotNavigator（镜号 / 描述 / 有无图、有无视频）
 *   剧本 blocking ──▶ StagingBoard（角色左右站位）
 *   画布 keyframe ─▶ FrameBridgeControls（首帧 / 尾帧真实地址）
 *   冻结意图     ──▶ 候选对比（后端 `GET /production_facts/intents`）
 *   候选决策态   ──▶ DecisionStateBadge（ADR-0002 的唯一渲染入口）
 *
 * 取数全部走 `api/queries`（ADR-0010）：本文件不发一个 `fetch`，
 * 也不起一个定时器。分镜画布与「分镜序列」子页共用同一份 query key，
 * 因此这里**不新增**任何请求 —— 只是同一份缓存的第二个读者。
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  EpisodeShotBoard,
  type DirectorIntent,
  type NavigatorShot,
  type StagedCharacter,
} from '@/features/shot-studio';
import type { StoryboardShot } from '@/types';
import {
  latestIntentOf,
  mediaVersionsOf,
  normalizeDecisionState,
  useEpisodeScript,
  useMediaVersions,
  useShotIntents,
  useStoryboardCanvas,
} from '@/api/queries';

/** 画布卡片 → 镜头导航行。字段一一对应，无一是编的。 */
function toNavigatorShots(cards: StoryboardShot[]): NavigatorShot[] {
  return cards.map((c) => ({
    // 后端按字符串存镜号（`str(shot.get("shot_id"))`），这里统一成字符串，
    // 否则 shot_id=2 与 shot_id="2" 会在选中态与 shot_key 查询上对不上。
    shot_id: String(c.shot_id),
    seq: c.seq,
    description: c.description,
    has_image: !!c.storyboard?.exists,
    has_video: !!c.video?.exists,
  }));
}

/** 剧本镜行的 blocking（后端已归一成英文枚举）→ 站位板。 */
function toStaged(card: StoryboardShot | null, shotRow: any): StagedCharacter[] {
  const cast: string[] = Array.isArray(card?.characters_in_shot)
    ? card!.characters_in_shot!
    : [];
  const blocking: any[] = Array.isArray(shotRow?.blocking) ? shotRow.blocking : [];
  const byName = new Map<string, any>();
  for (const b of blocking) {
    if (b && typeof b.name === 'string') byName.set(b.name, b);
  }
  return cast.map((name) => {
    const b = byName.get(name);
    const x = typeof b?.x === 'string' ? b.x : '';
    return {
      characterId: name,
      name,
      // 后端 `_norm_blocking` 只把 left/center/right 认成合法 x；
      // 其余（含空）一律不猜 —— 站位板对未给出的位置按「未指定」渲染。
      stage: x === 'left' || x === 'center' || x === 'right' ? x : undefined,
      gaze: typeof b?.facing === 'string' && b.facing ? b.facing : undefined,
    };
  });
}

export interface ShotStudioTabProps {
  projectKey: string;
  novelId?: string;
  episodeNo: number | null;
}

/**
 * 「镜头工作台」子页。
 *
 * 取数失败一律显式提示，不静默退化成空列表 —— 空列表和「没数据」在
 * 这个界面上是两件完全不同的事（invariant 1）。
 */
export function ShotStudioTab({ projectKey, novelId, episodeNo }: ShotStudioTabProps) {
  const canvasQ = useStoryboardCanvas(projectKey, episodeNo);
  const scriptQ = useEpisodeScript(novelId, episodeNo);

  const cards = ((canvasQ.data as any)?.cards || []) as StoryboardShot[];
  // 换集后上一集的选中镜号可能不存在于新集 —— 显式回落，不留悬空选中；
  // 没有有效选中时落到本集第一镜：否则「决策态徽标 / 候选对比」在用户
  // 点第一下之前永远缺席，等于把 ADR-0002 的入口藏起来。
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null);
  useEffect(() => {
    setSelectedShotId((prev) => {
      if (prev && cards.some((c) => String(c.shot_id) === prev)) return prev;
      return cards.length > 0 ? String(cards[0].shot_id) : null;
    });
  }, [cards]);

  const shots = useMemo(() => toNavigatorShots(cards), [cards]);
  const current = useMemo(
    () => cards.find((c) => String(c.shot_id) === selectedShotId) || null,
    [cards, selectedShotId],
  );
  const currentShotId = current ? String(current.shot_id) : null;

  // ---- 生成事实（ADR-0002）：本镜最近一次冻结意图 → 候选 → 决策态 ----
  const intentsQ = useShotIntents(projectKey, episodeNo, currentShotId);
  const intent = latestIntentOf(intentsQ);
  const mediaQ = useMediaVersions(intent?.intent_id);
  const versions = mediaVersionsOf(mediaQ.data);

  /**
   * 本镜的决策态 = **被采用那一版**自己的后端三态。
   *
   * ⚠️ 这里刻意不做「多候选取并集」：ADR-0002 的批准是**逐版本**的，
   *    把三个候选合成一个「已批准」等于凭空制造一次不存在的批准。
   *    一个候选都没被采用时，`normalizeDecisionState(undefined)` 按 none
   *    渲染「未采用」—— 这与 DecisionStateBadge 自身的契约一致
   *    （「缺失时按 none 渲染」），是**保守**方向：说「还不能交付」。
   */
  const decision = useMemo(() => {
    const adopted = versions.find((v) => v.selected === true);
    const d = adopted ? (adopted as Record<string, unknown>).decision : null;
    // 没有候选、或候选一个都没被采用 → 一律按 none 渲染「未采用」。
    // 这是 DecisionStateBadge 与 normalizeDecisionState 共同的契约（缺失即 none），
    // 且方向是**保守**的：任何情况下都不会显示成「已批准」。
    return normalizeDecisionState(d && typeof d === 'object' ? (d as any) : null);
  }, [versions]);

  // ---- 导演意图 / 站位：真实剧本字段 ----
  const shotRow = useMemo(() => {
    const rows = scriptQ.data?.script?.shots;
    if (!Array.isArray(rows)) return null;
    return rows.find((r: any) => String(r?.shot_id) === currentShotId) || null;
  }, [scriptQ.data, currentShotId]);

  const directorIntent: DirectorIntent = useMemo(
    () => ({
      intentId: intent?.intent_id,
      promptZh: current?.description,
      frozen: !!intent,
    }),
    [intent?.intent_id, current?.description, !!intent],
  );

  const staged = useMemo(() => toStaged(current, shotRow), [current, shotRow]);

  if (canvasQ.isPending) {
    return (
      <p role="status" aria-live="polite" className="rounded-md border border-line bg-surface p-4 text-sm text-ink-2">
        正在加载本集镜头…
      </p>
    );
  }

  if (canvasQ.isError) {
    return (
      <div role="alert" className="rounded-md border border-danger bg-danger-subtle p-4 text-sm text-danger-strong">
        <p>分镜画布加载失败：{canvasQ.error instanceof Error ? canvasQ.error.message : String(canvasQ.error)}</p>
        <button
          onClick={() => void canvasQ.refetch()}
          className="mt-2 rounded border border-danger px-2 py-1 text-xs"
        >
          重试
        </button>
      </div>
    );
  }

  if (cards.length === 0) {
    return (
      <p className="rounded-md border border-line bg-surface p-4 text-sm text-ink-2">
        本集还没有镜头数据 —— 先在「分镜序列」里生成或编辑本集剧本。
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <EpisodeShotBoard
        projectKey={projectKey}
        episodeNo={episodeNo}
        shots={shots}
        selectedShotId={selectedShotId}
        onSelectShot={setSelectedShotId}
        intent={directorIntent}
        intentId={intent?.intent_id}
        decision={decision}
        staged={staged}
        // 站位与意图本轮均无写入路径（见 EpisodeShotBoardProps 的注释），
        // 组件据此转只读，而不是给一个点了存不住的按钮。
        stagedReadOnly
        startFrameUrl={current?.keyframe?.start ? current?.storyboard?.url : undefined}
        endFrameUrl={current?.keyframe?.end_exists ? current?.keyframe?.end_url : undefined}
      />

      {/* 只读模式与「本镜尚无意图」都必须**说明原因**（invariant 6 的反面） */}
      {!intent && (
        <p className="text-xs text-ink-3">
          本镜还没有登记过冻结的生成意图，因此没有候选版本、也没有决策态可显示 ——
          采用与批准是挂在候选版本上的决定，没有候选就无从谈起。
        </p>
      )}
      <p className="text-xs text-ink-3">
        导演意图与站位当前为只读：剧本里 <code>camera_motion</code> / <code>emotion</code>{' '}
        存的是中文散文，而 Shot Studio 编辑器是英文枚举；站位 <code>blocking</code>{' '}
        也不在剧本 PUT 的可写字段白名单里。要改请到「分镜序列」子页的提示词编辑器。
      </p>
    </div>
  );
}

export default ShotStudioTab;
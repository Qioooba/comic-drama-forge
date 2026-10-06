// ===== storyboard 域 =====
// 分镜域（Shot Studio 数据层）：集切换器、分镜画布、首尾帧计划、九宫格候选构图。
//
// 本文件由机械切片从 `pages/ProjectWorkbenchPage.tsx` 搬出（ADR-0010 配套的
// 按域拆分）。代码逐字搬运：报错文案、提示语、空态措辞一律未改（R2）。

import React, { useState, useEffect, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { t } from '@/i18n';
import { projectsApi, keyframesApi, storyboardApi, videoApi, episodesApi, generationApi, type VideoMode } from '@/api/client';
import { Button, ConfirmDialog, EmptyState, ErrorState, Loading, Skeleton, Modal, Select } from '@/components/ui';
import { useToast } from '@/components/ui/toast';
import { GridPage } from '@/pages/GridPage';
import { Clapperboard, ImageIcon, Target, X } from '@/components/ui/icons';
import { useEpisodes, useKeyframePlan, useGenerationStatus, useStoryboardCanvas } from '@/api/queries';
import type { ShotGridStatusResponse, ShotGridTaskState } from '@/types';

// 焦点环：与 components/ui/index.tsx 里的 FOCUS_RING 逐字一致。
// index.css 有全局 :focus-visible outline 兜底，这里显式加 focus:outline-none 把它压掉，
// 否则 outline + ring 会叠成双环。凡因形状/类型原因换不成共享组件的原生控件，统一补这一串。
const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';


// ========== 分镜管理（九宫格 + 关键帧 + 分镜序列 三合一） ==========
// 三者本是同一工序的三个阶段：先出构图草案 → 再定首尾关键帧 → 最后成型分镜序列。
// 拆成 3 个顶级标签会让用户在标签间来回跳，这里收成一个标签页 + 3 个子标签。
//
// ⭐ 集级隔离（2026-09-25）：同一部小说会产多集，而分镜图/尾帧/视频**按集落盘**
// （第 1 集平铺，第 2 集起 `epNN/`）。此前这里**完全不选集** ——
// `storyboardApi.canvas(projectKey)` 不带 episode_no，后端 `_load_script_for` 就
// 只看「已生成的集里最新的那集」，于是无论用户在哪一集，看到的永远是最近一集的分镜；
// 关键帧同理。这里统一加一个集切换器，把选中集号透传给下面三个子页。
// ⚠️ ADR-0010：原先这里是 `useState` + `useEffect` + `alive` 标志的一问一答取数，
//    失败时清空列表。现在收敛为 query：切 Tab 回来命中缓存不再重拉，
//    且失败/空列表由 query 的三态统一承载，不再是「静默变成空数组」。
function useEpisodeList(novelId?: string) {
  const { data } = useEpisodes(novelId);
  return (data?.episodes || []) as any[];
}

function EpisodeSwitcher({
  episodes,
  value,
  onChange,
}: {
  episodes: any[];
  value: number | null;
  onChange: (ep: number) => void;
}) {
  const { t } = useApp();
  if (episodes.length === 0) return null;
  const cur = episodes.find((e) => e.episode_no === value);
  return (
    <div className="flex flex-wrap items-center gap-2 bg-surface-2 rounded-lg px-3 py-2">
      <span className="text-sm text-ink-2 shrink-0">{t('sb.episode')}</span>
      <div className="flex flex-wrap gap-1.5">
        {episodes.map((e) => {
          const active = e.episode_no === value;
          const shots = e.shots ?? e.shot_count ?? 0;
          return (
            <button
              key={e.episode_no}
              onClick={() => onChange(e.episode_no)}
              title={`${e.episode_title || e.title || ''}${e.chapter_index ? ` · ${t('sb.chapterN', { n: e.chapter_index })}` : ''}`}
              className={`px-2.5 py-1 rounded-md text-xs transition-colors ${FOCUS_RING} ${
                active ? 'bg-brand text-white shadow' : 'bg-surface text-ink-2 hover:bg-line hover:text-ink-1 border border-line'
              }`}
            >
              {t('sb.episodeN', { n: e.episode_no })}
              <span className={active ? ' text-white/80' : ' text-ink-3'}>{` · ${shots}`}</span>
            </button>
          );
        })}
      </div>
      {cur && (
        <span className="text-xs text-ink-3 ml-auto truncate max-w-[16rem]">
          {cur.episode_title || cur.title || ''}
        </span>
      )}
    </div>
  );
}


// =====================================================================
// 分镜提示词编辑器（2026-09-30）
// 每行 shot 的 description + motion 可编辑，保存调 PUT /api/episodes/...
// 保存成功后可触发单镜重新生成分镜图
// =====================================================================
function ShotPromptEditor({ shot, novelId, episodeNo, onSaved }: {
  shot: any; novelId?: string; episodeNo: number; onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [desc, setDesc] = useState(shot.description || '');
  const [motion, setMotion] = useState(shot.motion || '');
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const toast = useToast();

  // G10b（2026-09-30）：质检改写的提示词**实时渲染** —— 出图/重试循环每改写一次 prompt，
  // 后端任务状态里的 live 字段就更新一次；本组件启动「重新生成分镜」后每 3s 轮询一次，
  // 把「当前正在用的提示词」渲染到本镜头卡片下（不再等任务收尾才可见）。
  //
  // ⚠️ ADR-0010：原先这里是 `pollRef` + `mountedRef` + `stopPolling()` 三件套
  //    （手写轮询的典型成本：漏掉任一处就是「卸载后还在打后端」）。
  //    现在「按需启动 / 跑完自停」退化成「手里有没有 taskId」：
  //    给 id 就轮询，拿到终态 query 自动停机，卸载由生命周期托管。
  const [taskId, setTaskId] = useState<string | null>(null);
  const taskQ = useGenerationStatus(taskId);
  const [live, setLive] = useState<any>(null);
  const [liveDone, setLiveDone] = useState(false);

  // 后端 live 帧 → 卡片展示；只认属于本镜头的那一帧
  useEffect(() => {
    const lv = taskQ.data?.live;
    if (lv && lv.shot === shot.shot_id && lv.prompt) {
      setLive(lv);
      setLiveDone(false);
    }
  }, [taskQ.data, shot.shot_id]);

  // 任务结束：若最后一帧 live 还没标记定稿，补上；并刷新剧本数据
  //（原实现在 pollTask 的 tick 里做；这里改成对终态的响应，语义相同）
  const finishedRef = useRef(false);
  useEffect(() => {
    const st = taskQ.data?.status;
    if (st && st !== 'running' && !finishedRef.current) {
      finishedRef.current = true;
      setLive((prev: any) => (prev && prev.shot === shot.shot_id && !prev.done
        ? { ...prev, done: true } : prev));
      onSaved();
    }
  }, [taskQ.data?.status, shot.shot_id, onSaved]);

  // 换一次任务就重置「已收尾」标志（否则新任务的终态不会触发刷新）
  const startPoll = (id: string) => {
    finishedRef.current = false;
    setTaskId(id);
  };

  const save = async () => {
    setSaving(true);
    try {
      const r = await fetch(
        `/api/episodes/${encodeURIComponent(novelId || '')}/${episodeNo}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ shots: [{ shot_id: shot.shot_id, description: desc.trim() || null, motion: motion.trim() || null }] }),
        }
      );
      const d = await r.json();
      if (!d.success) throw new Error(d.error || '保存失败');
      toast.success(`镜头 ${shot.shot_id} 提示词已保存`);
      onSaved();
    } catch (e: any) {
      toast.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const regenerate = async () => {
    setRegenerating(true);
    try {
      const r = await fetch('/api/storyboards/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_name: novelId || '',
          shots: [{ shot_id: shot.shot_id }],
          limit: 1,
        }),
      });
      const d = await r.json();
      if (d.task_id) {
        setLive(null);
        startPoll(d.task_id);
        toast.info(`分镜 ${shot.shot_id} 重新生成任务已启动（提示词随质检实时刷新）`);
      } else {
        throw new Error(d.error || '启动失败');
      }
    } catch (e: any) {
      toast.error(e.message || '重新生成失败');
    } finally {
      setRegenerating(false);
    }
  };

  // G10b：live 面板 —— 正在跑的出图任务实时暴露「当前提示词」（含质检改写后的版本）
  const livePanel = live && live.shot === shot.shot_id ? (
    <div className={`mt-2 rounded border p-2 ${live.done ? 'border-success/40' : 'border-brand/40'}`}>
      <div className="flex items-center gap-2 text-xs">
        <span className={`font-medium ${live.done ? 'text-success' : 'text-brand'}`}>
          {live.done
            ? '定稿提示词（本轮出图实际使用）'
            : `QC 实时改写 · 第 ${(live.attempt ?? 0) + 1} 次尝试 · ${live.phase === 'regenerating' ? '改写后重新生成中' : live.phase === 'checking' ? '质检判定中' : '生成中'}`}
        </span>
        {!live.done && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />}
      </div>
      <pre className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap text-[11px] leading-4 text-ink-2">{live.prompt}</pre>
    </div>
  ) : null;

  if (!editing) {
    return (
      <div className="mt-1 space-y-1">
        <div className="flex items-start gap-2">
          <p className="text-sm text-ink-1 flex-1">{shot.description}</p>
          <button
            className="text-xs text-brand hover:underline shrink-0 cursor-pointer"
            onClick={() => { setDesc(shot.description || ''); setMotion(shot.motion || ''); setEditing(true); }}
          >
            编辑
          </button>
        </div>
        {shot.motion && <p className="text-xs text-ink-3 ml-2">{shot.motion}</p>}
        {livePanel}
      </div>
    );
  }

  return (
    <div className="mt-2 bg-surface rounded border border-line p-3 space-y-2">
      <label className="text-xs font-medium text-ink-2 block">镜头描述（提示词）</label>
      <textarea
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        rows={2}
        className="w-full text-sm bg-surface border border-line rounded p-2 text-ink-1 resize-y"
        placeholder="中近景（腰部以上取景），赵天霸右手食指伸出指向右前方…"
      />
      <label className="text-xs font-medium text-ink-2 block">画面内动作（motion）</label>
      <textarea
        value={motion}
        onChange={(e) => setMotion(e.target.value)}
        rows={2}
        className="w-full text-sm bg-surface border border-line rounded p-2 text-ink-1 resize-y"
        placeholder="【摄影机】无；【画面内】赵天霸右手食指前伸，左手丹丸位于胸前"
      />
      {livePanel}
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={save} disabled={saving || !desc.trim()}>
          {saving ? '保存中…' : '保存提示词'}
        </Button>
        <Button size="sm" variant="secondary" onClick={regenerate} disabled={regenerating || !desc.trim()}>
          {regenerating ? '生成中…' : '重新生成分镜'}
        </Button>
        <button className="text-xs text-ink-3 hover:text-ink-1 cursor-pointer" onClick={() => setEditing(false)}>取消</button>
      </div>
    </div>
  );
}

function StoryboardHubTab({ projectKey, novelId }: { projectKey: string; novelId?: string }) {
  const { t } = useApp();
  const [sub, setSub] = useState<'storyboard' | 'ninegrid' | 'keyframes'>('storyboard');
  // 选中集号：null = 未指定（沿用后端「最新一集」的兜底，兼容无剧集数据的纯项目）
  const [selectedEpisode, setSelectedEpisode] = useState<number | null>(null);
  const episodes = useEpisodeList(novelId);

  // 剧集列表到位后默认落到第 1 集：必须显式传集号，否则后端会漂到「最新一集」，
  // 与用户在概览页看到的选集不一致（同一部小说不同页显示不同集）。
  useEffect(() => {
    if (episodes.length > 0 && selectedEpisode === null) {
      setSelectedEpisode(episodes[0].episode_no);
    }
    // 选集被删（重跑时清过产物）时回落到第一个可用集
    if (episodes.length > 0 && selectedEpisode !== null
        && !episodes.some((e) => e.episode_no === selectedEpisode)) {
      setSelectedEpisode(episodes[0].episode_no);
    }
  }, [episodes, selectedEpisode]);

  const subs: { id: 'storyboard' | 'ninegrid' | 'keyframes'; icon: React.ReactNode; label: string; hint: string }[] = [
    { id: 'storyboard', icon: <Clapperboard className="h-4 w-4" />, label: t('wb.subStoryboard'), hint: t('sb.subStoryboardHint') },
    { id: 'ninegrid', icon: <Target className="h-4 w-4" />, label: t('wb.subNinegrid'), hint: t('sb.subNinegridHint') },
    { id: 'keyframes', icon: <ImageIcon className="h-4 w-4" />, label: t('wb.subKeyframes'), hint: t('sb.subKeyframesHint') },
  ];

  return (
    <div className="space-y-5">
      {/* 子标签导航 */}
      <div className="flex flex-wrap gap-2">
        {subs.map((s) => (
          <button
            key={s.id}
            onClick={() => setSub(s.id)}
            title={s.hint}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm transition-colors ${FOCUS_RING} ${
              sub === s.id
                ? 'bg-brand text-white shadow'
                : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1'
            }`}
          >
            <span>{s.icon}</span>
            <span>{s.label}</span>
          </button>
        ))}
      </div>

      {/* 集切换器：分镜 / 关键帧两类产物按集隔离，必须先在集之间分流 */}
      {sub !== 'ninegrid' && (
        <EpisodeSwitcher
          episodes={episodes}
          value={selectedEpisode}
          onChange={setSelectedEpisode}
        />
      )}

      {sub === 'storyboard' && <StoryboardTab projectKey={projectKey} episodeNo={selectedEpisode} />}
      {sub === 'ninegrid' && <GridPage projectKey={projectKey} />}
      {sub === 'keyframes' && <KeyframesTab projectKey={projectKey} episodeNo={selectedEpisode} />}
    </div>
  );
}

// ========== Keyframes Tab ==========
function KeyframesTab({ projectKey, episodeNo }: { projectKey: string; episodeNo?: number | null }) {
  const { t } = useApp();
  const toast = useToast();
  const [generating, setGenerating] = useState(false);

  // ⚠️ ADR-0010：原先是 `useState` + `fetchPlan()` 一问一答，切集时先清空再填充
  //    （用户看到「列表清空了」而不是「正在加载下一集」，且内容高度从 0 跳到 N 行）。
  //    现在 keepPreviousData：切集时保留上一集的计划直到新数据到达。
  //
  //    ⚠️ 代价必须一起承担：保留期间 `isPreviousData` 为真，屏幕上的是**上一集**的计划。
  //        此刻「生成首尾帧」必须禁用 —— 在上一集的计划上生成会写错集。
  const planQuery = useKeyframePlan(projectKey, episodeNo);
  const plan = (planQuery.data as any) ?? null;
  const loading = planQuery.isPending || planQuery.isFetching;
  // ⚠️ ADR-0010 §关键设计 里写的标识是 `isPreviousData`，但那是 v4 的名字：
  //    **v5 已移除 `isPreviousData`**，同一语义由 `isPlaceholderData` 承担
  //    （本仓库装的是 @tanstack/react-query 5.104.1）。两者语义相同，
  //    这里用 v5 的名字；ADR 里的措辞待下次修订时同步。
  const isStale = planQuery.isPlaceholderData || planQuery.isFetching;
  const error = planQuery.error
    ? (planQuery.error instanceof Error ? planQuery.error.message : t('sb.fetchFailed'))
    : '';

  const fetchPlan = async () => { await planQuery.refetch(); };

  const handleGenerate = async () => {
    if (!projectKey) return;
    // stale 期间拒写：宁可按钮点不动，也不能把任务发到上一集去
    if (isStale) return;
    setGenerating(true);
    try {
      const result = await keyframesApi.generate({
        project_name: projectKey,
        episode_no: episodeNo ?? undefined,
      });
      toast.success(`${t('keyframes.generateStarted')}：${result.task_id}`);
      setTimeout(fetchPlan, 3000);
    } catch (err) {
      const msg = err instanceof Error ? err.message : t('sb.generateFailed');
      // 错误态由 query 的 error 承载；生成失败是一次性动作的失败，
      // 不能把它塞进 query 的错误里（那会让整个计划区都变成失败态），
      // 因此这里只弹 toast，与改造前一致。
      toast.error(msg);
    } finally {
      setGenerating(false);
    }
  };

  // ⚠️ ADR-0010：原先这里有 `useEffect(() => { fetchPlan(); }, [projectKey, episodeNo])`
  //    —— 换集就无条件重拉、切 Tab 回来再拉一遍。现在由 query 自动按 key 取数，
  //    切 Tab 命中缓存不重发请求；换集换 key 自动重取。

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">{t('keyframes.title')}</h3>
        <Button size="sm" onClick={fetchPlan} disabled={loading}>{t('common.refresh')}</Button>
      </div>

      {/* stale 标识（MASTER invariant 9 + ADR-0010）：keepPreviousData 期间屏幕上
          是上一集的计划。必须显式说明，且下方「生成」按钮在此时禁用 ——
          只保留旧数据而不标 stale，用户会在旧数据上点操作，这比闪一下空态更危险。 */}
      {isStale && (
        <div role="status" className="rounded-md border border-warning/50 bg-warning-subtle px-2.5 py-1.5 text-xs text-warning-strong">
          {t('common.loading')}…（正在切换到本集的数据，以上为上一集内容）
        </div>
      )}

      {/* 加载态（此前首屏只剩标题栏，无任何反馈）：对齐真实区块的 4 张统计卡 */}
      {loading && !plan && (
        <div role="status" aria-live="polite" aria-label={t('common.loading')}>
          <div className="grid grid-cols-4 gap-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-24 rounded-lg" />
            ))}
          </div>
        </div>
      )}

      {/* 硬失败：整块拿不到 plan（无数据可展示）→ ErrorState；
          已有 plan 时仅是刷新/生成失败 → 降级为下面那条紧凑行内提示，不吃掉已展示内容 */}
      {error && !plan && (
        <ErrorState
          title={t('project.loadingFailed')}
          description={error}
          onRetry={fetchPlan}
        />
      )}
      {error && plan && (
        <div className="p-4 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong">{error}</div>
      )}

      {plan && (
        <div className="grid grid-cols-4 gap-4">
          <div className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className="text-3xl font-bold text-brand">{plan.shot_count}</div>
            <div className="text-sm text-ink-2">{t('keyframes.totalShots')}</div>
          </div>
          <div className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className="text-3xl font-bold text-success-strong">{plan.start_frames_ready}</div>
            <div className="text-sm text-ink-2">{t('keyframes.startReady')}</div>
          </div>
          <div className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className="text-3xl font-bold text-info-strong">{plan.end_frames_ready}</div>
            <div className="text-sm text-ink-2">{t('keyframes.endReady')}</div>
          </div>
          <div className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className="text-3xl font-bold text-warning-strong">{plan.to_generate}</div>
            <div className="text-sm text-ink-2">{t('keyframes.toGenerate')}</div>
          </div>
        </div>
      )}

      {plan && plan.to_generate > 0 && (
        <Button
          onClick={handleGenerate}
          // stale 期间禁用：MASTER invariant 6（disabled 必须配相邻原因）
          disabled={generating || isStale}
          title={isStale ? '正在切换到本集数据，暂不能生成' : undefined}
          className="w-full bg-warning hover:bg-warning-strong"
        >
          {generating ? t('common.generating') : t('keyframes.generate')}
        </Button>
      )}

      {plan && (
        <div className="space-y-2">
          <h4 className="font-semibold text-ink-1 mb-3">{t('keyframes.shotList')}</h4>
          {plan.plan?.map((shot: any) => (
            <div
              key={shot.seq}
              className={`flex items-center gap-4 p-3 rounded-lg ${
                shot.need_gen ? 'bg-warning-subtle border border-warning/30' :
                shot.has_end ? 'bg-success-subtle border border-success/30' :
                'bg-surface-2'
              }`}
            >
              <span className="w-12 font-mono text-ink-2">#{shot.seq}</span>
              <span className="flex-1">{shot.status || shot.shot_id}</span>
              <div className="flex gap-2">
                {shot.has_start && <span className="px-2 py-1 bg-success/20 text-success-strong rounded text-xs">{t('keyframes.startFrame')}</span>}
                {shot.has_end && <span className="px-2 py-1 bg-info/20 text-info-strong rounded text-xs">{t('keyframes.endFrame')}</span>}
                {shot.need_gen && !shot.has_end && <span className="px-2 py-1 bg-warning/20 text-warning-strong rounded text-xs">{t('keyframes.toGenerate')}</span>}
              </div>
              {shot.url && (
                <a href={shot.url} target="_blank" rel="noopener noreferrer" className={`text-brand hover:text-brand rounded-sm ${FOCUS_RING}`}>{t('common.view')}</a>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ========== Storyboard Tab ==========
// 单镜重做闭环：后端 /api/storyboard/retry-shot（分镜图）与 /api/video/retry-shot（视频）
// 早已实现，但前端此前**零入口** —— 用户对某一镜不满意只能整集重跑。
// 这里把两个入口放到每张分镜卡上，并在视频重做成功后提示「同集成片已过期」。
// 项目级视频生成方式（写入 config.video_mode）。
// ⚠️ 2026-10-02 修复：后端 `config.norm_video_mode` 已**只保留「整集一次生成」**
//   （per_shot / keyframe 两种模式废弃，任何入口一律归一成 episode，见 config.py:384）。
//   这里原先仍列 3 项并注释「取值与后端 config.VIDEO_MODES 一致」——**注释与事实不符**
//   （自我背书的错误注释），用户选 per_shot/keyframe 会被后端**静默归一**，
//   界面却仍显示所选值，属静默降级。现与后端同源收敛为单值。
//   注意与「每镜重做模式」（reference / keyframe，只管这一镜怎么重做）是两件事。
const PROJECT_VIDEO_MODE_OPTIONS: { value: VideoMode; labelKey: string }[] = [
  { value: 'episode', labelKey: 'project.videoModeEpisode' },
];

// 批量重生成单次上限：与后端 /api/video/retry-shots-batch 的硬上限（≤12）一致
const BATCH_RETRY_LIMIT = 12;

function StoryboardTab({ projectKey, episodeNo }: { projectKey: string; episodeNo?: number | null }) {
  const { t } = useApp();
  const toast = useToast();
  // cards / summary / loading / error 已随 ADR-0010 收敛为 query（见下方 canvasQ），
  // 原先的四个 useState 随之删除 —— 同一份数据不可能既在 state 又在 query 里各存一份。
  /** 正在重做的镜头：`${shot_id}:image` / `${shot_id}:video` */
  const [busy, setBusy] = useState<string | null>(null);
  /** 每镜的视频重做模式（reference=分镜图驱动 / keyframe=首尾帧插值） */
  const [videoMode, setVideoMode] = useState<Record<string, 'reference' | 'keyframe'>>({});
  /** 正在内嵌预览视频的镜头 id（null=全部收起）：按镜头单开，避免多卡同时播放 */
  const [playingVideo, setPlayingVideo] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [shotError, setShotError] = useState('');
  /** 整集一次提交（mode=episode）：触发中标记 + 返回的 task_id */
  const [episodeGenerating, setEpisodeGenerating] = useState(false);
  const [episodeTaskId, setEpisodeTaskId] = useState<string | null>(null);
  /** 项目级视频生成方式（新建项目时选的 video_mode）：本页「生成视频」按它执行。
   *  ⚠️ 不要与上面每镜的 videoMode（reference/keyframe 单镜重做）混用，两者不是一个东西。 */
  const [projectVideoMode, setProjectVideoMode] = useState<VideoMode>('episode');

  // ---- 批量重生成（POST /api/video/retry-shots-batch，单次 ≤12 个）----
  /** 已勾选待批量重生成的镜头 id（String(card.shot_id)） */
  const [selectedShots, setSelectedShots] = useState<string[]>([]);
  /** 批量请求进行中：同步端点可能耗时数分钟，期间防重复提交 */
  const [batchRunning, setBatchRunning] = useState(false);
  /** 批量重生成确认弹窗 */
  const [batchConfirmOpen, setBatchConfirmOpen] = useState(false);
  /** 最近一次批量结果（成功 N/共 M + 失败明细），展示在工具栏下方 */
  const [batchResult, setBatchResult] = useState<{
    total: number;
    ok: number;
    failures: { shot_id: string; error: string }[];
  } | null>(null);

  // ---- 分镜九宫格候选构图（P2-1：grid-candidates 生成 3x3 候选 → 点选某格 → grid-apply 裁切入库）----
  /** 正在操作九宫格的镜头（sid + 展示用序号）；null = 弹窗关闭 */
  const [gridTarget, setGridTarget] = useState<{ sid: string; seq: number } | null>(null);
  /** 弹窗内阶段：generating=候选图生成中 / ready=可点选（或已失败可重试）/ applying=裁切入库中 */
  const [gridPhase, setGridPhase] = useState<'generating' | 'ready' | 'applying'>('generating');
  /** 后端生成的 3x3 候选网格图地址（generation 任务完成态的 grid_url） */
  const [gridImgUrl, setGridImgUrl] = useState('');
  /** 用户点选的格号（1-9，行优先：1=左上 … 9=右下，与后端 crop_grid_cell 的等分切分一致） */
  const [gridCell, setGridCell] = useState<number | null>(null);
  /** 候选生成失败原因（弹窗内展示，可原地重试） */
  const [gridError, setGridError] = useState('');
  /** 进行中的候选生成任务（sid → task_id）：弹窗被提前关掉后重开可续接轮询，不重复烧卡 */
  const gridTaskRef = useRef<{ sid: string; taskId: string } | null>(null);
  /** ⚠️ ADR-0010：原先的 `gridPollRef`（setInterval 句柄）已删除。
   *  「有没有任务在跑」直接用 taskId 表达 —— 给 id 即轮询，置空即停机。 */
  const [gridTaskId, setGridTaskId] = useState<string | null>(null);
  const gridTaskQ = useGenerationStatus(gridTaskId);

  // ---- 分镜图点击放大（lightbox）----
  /** 正在放大的分镜图：正式图或生成中 scratch 图；null = 弹窗关闭。
   *  分镜缩略图原先没有任何点击交互（点了没反应），这里补上全屏放大查看。 */
  const [zoomImg, setZoomImg] = useState<{ src: string; seq: number } | null>(null);

  // ---- 分镜画布自动刷新（ADR-0010：原手写 setInterval → query）----
  /**
   * ⚠️ 改造前这里是 `canvasPollRef` + `stopCanvasPoll()` + `canvasPollStateRef`
   *    + 一个「静默 vs 非静默」的 fetchCanvas({silent}) 分叉，共约 45 行。
   *    其中 `canvasPollStateRef` 记录「是否值得继续轮询」，但 stepActive 从未被读取，
   *    是一段**死记账** —— 现在整段随手写轮询一起删除。
   *
   * 保留的行为：5s 自动刷新（分镜整步 20+ 镜、每镜 2-3 分钟，5s 接近实时又不压后端）。
   *
   * ⚠️ keepPreviousData 的代价必须一起承担：换集期间屏幕上仍是**上一集**的分镜，
   *    下方 isCanvasStale 会显式标出，且写操作（重生成 / 批量重生成）在此时禁用。
   */
  const canvasQ = useStoryboardCanvas(projectKey, episodeNo);
  const cards = ((canvasQ.data as any)?.cards || []) as any[];
  const summary = ((canvasQ.data as any)?.summary || null) as any;
  const loading = canvasQ.isPending;
  const error = canvasQ.error
    ? ((canvasQ.error as any)?.message?.includes('404')
        ? 'no-data'
        : (canvasQ.error instanceof Error ? canvasQ.error.message : t('sb.fetchFailed')))
    : '';
  // 换集/重取期间显示的是上一集内容 —— 必须让用户看见，且期间禁写
  const isCanvasStale = canvasQ.isPlaceholderData;

  const fetchCanvas = async () => { await canvasQ.refetch(); };

  // 换集必须重拉：cards 是按集落盘的分镜图/视频，混用会张冠李戴。
  // ⚠️ ADR-0010：原先这里还有一个 `useEffect` 起 5s 定时器 + 卸载时 clearInterval。
  //    现在换 key 由 query 自动重取、轮询随生命周期启停，无需任何手动清理。
  //
  //    ⚠️ 关于原来的 `silent` 分叉：静默轮询是为了「不翻转 loading，否则每 5 秒
  //    闪一次骨架屏」。query 天然满足这一点 —— 后台重取只让 `isFetching` 变真，
  //    `isPending` 保持 false，因此不会翻转骨架屏。分叉随之删除。

  // 项目级视频生成方式：进页面就回显（用户「新建项目」时选的），改成什么就按什么生成
  useEffect(() => {
    if (!projectKey) return;
    projectsApi.getConfig(projectKey)
      .then((d) => setProjectVideoMode(((d.config?.video_mode as VideoMode) || 'episode')))
      .catch(() => null);
  }, [projectKey]);

  const handleRetryImage = async (card: any) => {
    // ⚠️ stale 期间拒写：card 来自上一集，此时重生成会写到上一集去
    if (isCanvasStale) return;
    const key = `${card.shot_id}:image`;
    setBusy(key);
    setShotError('');
    setNotice('');
    try {
      await storyboardApi.retryShot({
        project_name: projectKey,
        shot_id: String(card.shot_id),
        // 单镜重跑也带集号：后端据此决定写 <项目>/ 还是 <项目>/epNN/，
        // 漏传会把第 2 集的图写进第 1 集目录（覆盖第 1 集同号镜头的图）。
        episode_no: episodeNo ?? undefined,
      });
      setNotice(t('sb.imageRedone', { seq: card.seq }));
      await fetchCanvas();
    } catch (e) {
      setShotError(e instanceof Error ? e.message : t('sb.imageRedoFailed'));
    } finally {
      setBusy(null);
    }
  };

  const handleRetryVideo = async (card: any) => {
    // ⚠️ stale 期间拒写：card 来自上一集，此时重生成会写到上一集去
    if (isCanvasStale) return;
    const key = `${card.shot_id}:video`;
    const mode = videoMode[String(card.shot_id)] || 'reference';
    if (mode === 'keyframe' && !card.keyframe?.end_exists) {
      setShotError(t('sb.noKeyframeForInterp', { seq: card.seq }));
      return;
    }
    setBusy(key);
    setShotError('');
    setNotice('');
    try {
      const r = await videoApi.retryShot({
        project_name: projectKey,
        shot_id: card.shot_id,
        mode,
        episode_no: episodeNo ?? undefined,
      });
      setNotice(
        t('sb.videoRedone', {
          seq: card.seq,
          mode: r.mode === 'keyframe' ? t('sb.modeKeyframe') : t('sb.modeReference'),
          refCount: r.ref_count,
          duration: r.duration,
        }) + (r.deliverable_marked_stale ? t('sb.videoRedoneStale') : '')
      );
      await fetchCanvas();
    } catch (e) {
      setShotError(e instanceof Error ? e.message : t('sb.videoRedoFailed'));
    } finally {
      setBusy(null);
    }
  };

  /** 改项目级视频生成方式：写进项目配置，之后「生成视频」与托管生产都按它执行 */
  const handleProjectVideoModeChange = async (v: string) => {
    if (!projectKey) return;
    const prev = projectVideoMode;
    setProjectVideoMode(v as VideoMode);
    try {
      const d = await projectsApi.updateConfig(projectKey, { video_mode: v });
      const saved = (d.config?.video_mode as VideoMode) || (v as VideoMode);
      setProjectVideoMode(saved);
      toast.success(t('sb.videoModeSaved'));
    } catch (e) {
      // 保存失败必须回滚 UI，否则界面显示的模式与实际生成方式不一致（最难查的一类漂移）
      setProjectVideoMode(prev);
      toast.error(e instanceof Error ? e.message : t('sb.videoModeSaveFailed'));
    }
  };

  // 生成该集视频：方式取**项目级设定**（episode 整集一次出连续片 / per_shot 逐镜 /
  // keyframe 首尾帧插值）。旧实现把 mode 写死成 episode，用户在新建设置里选什么都无效。
  const handleGenerateEpisode = async () => {
    if (!projectKey) return;
    setEpisodeGenerating(true);
    setShotError('');
    try {
      const r = await videoApi.generateEpisode({
        project_name: projectKey,
        episode_no: episodeNo ?? undefined,
        mode: projectVideoMode,
      });
      setEpisodeTaskId(r.task_id);
      toast.success(t('sb.episodeGenerateStarted', { total: r.total }));
    } catch (e) {
      setShotError(e instanceof Error ? e.message : t('sb.videoRedoFailed'));
    } finally {
      setEpisodeGenerating(false);
    }
  };

  // ---- 批量重生成 ----
  // 刷新/切集后清掉已不在当前列表里的勾选（防止带着旧集的镜头去批量重做）
  useEffect(() => {
    const ids = new Set(cards.map((c: any) => String(c.shot_id)));
    setSelectedShots((prev) => prev.filter((id) => ids.has(id)));
  }, [cards]);

  const toggleShotSelected = (sid: string) => {
    setSelectedShots((prev) =>
      prev.includes(sid) ? prev.filter((s) => s !== sid) : [...prev, sid]
    );
  };

  // 确认弹窗里点「确认」后执行。与单镜重跑一样是同步等待（逐镜等 ComfyUI 出片，
  // 可能耗时数分钟）：ConfirmDialog loading 期间不可关闭，按钮 loading 防重复提交。
  const runBatchRetry = async () => {
    if (batchRunning || selectedShots.length === 0) return;
    setBatchRunning(true);
    setShotError('');
    setNotice('');
    try {
      const r = await videoApi.retryShotsBatch(projectKey, episodeNo ?? undefined, selectedShots);
      const results = Array.isArray(r.results) ? r.results : [];
      const ok = typeof r.ok_count === 'number'
        ? r.ok_count
        : results.filter((x) => x.success).length;
      const failures = results
        .filter((x) => !x.success)
        .map((x) => ({ shot_id: String(x.shot_id), error: x.error || t('video.batchRetry.unknownError') }));
      const total = results.length || selectedShots.length;
      setBatchResult({ total, ok, failures });
      if (failures.length === 0) {
        toast.success(t('video.batchRetry.done', { ok, total }));
      } else {
        toast.warning(t('video.batchRetry.partial', { ok, total }));
      }
      setSelectedShots([]);
      await fetchCanvas();
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('video.batchRetry.submitFailed');
      setShotError(msg);
      toast.error(msg);
    } finally {
      setBatchRunning(false);
      setBatchConfirmOpen(false);
    }
  };

  // ---- 分镜九宫格候选构图（P2-1 前端入口） ----
  /** 九宫格轮询句柄：⚠️ ADR-0010 —— 原先的 `gridPollRef` + `stopGridPoll()` 已删除，
   *     「停轮询」现在是「把 gridTaskId 置空」。 */
  const stopGridPoll = () => setGridTaskId(null);

  /** 关闭弹窗：停轮询并复位弹窗内状态（服务端任务继续跑；同镜重开时续接轮询，不重复发起） */
  const closeGridModal = () => {
    stopGridPoll();
    setGridTarget(null);
    setGridPhase('generating');
    setGridImgUrl('');
    setGridCell(null);
    setGridError('');
  };

  /** 轮询九宫格生成任务（3s）：完成取 grid_url 展示；失败/取消把原因留在弹窗内可重试。
   *  ⚠️ /api/generation/status 历史上存在 {success, task:{…}} 信封与顶层平铺两种返回形态，
   *     这里按 `task ?? 顶层` 兼容读取，不依赖其中一种。
   *
   *  ⚠️ ADR-0010：原先是 `gridPollRef` + `stopGridPoll()` + 一个卸载时的
   *     `useEffect(() => () => stopGridPoll(), [])`，共约 25 行手写轮询。
   *     现在「停轮询」退化成「把 gridTaskId 置空」，query 随之禁用；
   *     终态（completed / failed / cancelled）由 refetchInterval 判定自动停机。 */
  useEffect(() => {
    const st = gridTaskQ.data ? (((gridTaskQ.data as any)?.task ?? gridTaskQ.data) as ShotGridTaskState) : null;
    if (!st) return;
    if (st.status === 'completed') {
      setGridTaskId(null);
      gridTaskRef.current = null;
      setGridImgUrl(st.grid_url || '');
      if (!st.grid_url) setGridError(t('sb.grid.generateFailed'));
      setGridPhase('ready');
    } else if (st.status === 'failed' || st.status === 'cancelled') {
      setGridTaskId(null);
      gridTaskRef.current = null;
      setGridError(st.error || t('sb.grid.generateFailed'));
      setGridPhase('ready');
    }
  }, [gridTaskQ.data]);

  /** 发起轮询（兼容两种返回形态的读取逻辑已上移到 effect 里） */
  const pollGridTask = (taskId: string) => { setGridTaskId(taskId); };

  /** 发起九宫格候选生成（打开弹窗与弹窗内「重新生成」共用）：异步任务 + 轮询，防重复提交 */
  const runGridCandidates = async (target: { sid: string; seq: number }) => {
    setGridTarget(target);
    setGridPhase('generating');
    setGridImgUrl('');
    setGridCell(null);
    setGridError('');
    stopGridPoll();
    // 同一镜头已有候选任务在跑（上次弹窗被提前关掉）：直接续接轮询，不重复烧一次 GPU
    const running = gridTaskRef.current;
    if (running && running.sid === target.sid) {
      pollGridTask(running.taskId);
      return;
    }
    try {
      const r = await storyboardApi.gridCandidates(projectKey, episodeNo ?? 1, target.sid);
      gridTaskRef.current = { sid: target.sid, taskId: r.task_id };
      pollGridTask(r.task_id);
    } catch (e) {
      // 发起失败（镜号不存在 / 无可用参考图等 4xx）：留在弹窗内展示原因，可关闭或重试
      setGridError(e instanceof Error ? e.message : t('sb.grid.generateFailed'));
      setGridPhase('ready');
    }
  };

  /** 应用所选格：后端把该格从九宫格图裁切为该镜正式分镜图（人工定稿），成功后刷新画布 */
  const handleGridApply = async () => {
    if (!gridTarget || gridCell == null || gridPhase === 'applying') return;
    setGridPhase('applying');
    try {
      const r = await storyboardApi.gridApply(projectKey, episodeNo ?? 1, gridTarget.sid, gridCell);
      toast.success(t('sb.grid.applied', { seq: gridTarget.seq, cell: r.cell ?? gridCell }));
      closeGridModal();
      await fetchCanvas();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('sb.grid.applyFailed'));
      setGridPhase('ready');
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">{t('wb.storyboardHub')}</h3>
        <div className="flex items-center gap-2">
          {/* 视频生成方式（项目级）：默认取「新建项目」时选的值，这里可改并即刻落盘 */}
          <Select
            value={projectVideoMode}
            onChange={handleProjectVideoModeChange}
            options={PROJECT_VIDEO_MODE_OPTIONS.map(o => ({ value: o.value, label: t(o.labelKey) }))}
            disabled={loading || episodeGenerating}
            className="w-40"
          />
          <Button
            size="sm"
            onClick={handleGenerateEpisode}
            disabled={loading || episodeGenerating || isCanvasStale}
            title={isCanvasStale ? '正在切换到本集数据，暂不能生成' : undefined}
            className="bg-brand hover:bg-brand-strong"
          >
            {episodeGenerating ? t('common.generating') : t('sb.generateVideo')}
          </Button>
          {/* 批量重生成：无选中禁用；超上限在复选框层已挡，这里再兜底。
              ⚠️ isCanvasStale 时同样禁用：选中的镜头号来自上一集，直接跑会写错集。 */}
          {selectedShots.length > 0 && (
            <span className="text-xs text-ink-2 whitespace-nowrap">
              {t('video.batchRetry.selectedCount', { n: selectedShots.length, max: BATCH_RETRY_LIMIT })}
            </span>
          )}
          <Button
            size="sm"
            variant="secondary"
            onClick={() => { setBatchResult(null); setBatchConfirmOpen(true); }}
            disabled={loading || batchRunning || isCanvasStale || selectedShots.length === 0 || selectedShots.length > BATCH_RETRY_LIMIT}
            title={isCanvasStale ? '正在切换到本集数据，暂不能批量重生成' : t('video.batchRetry.buttonHint')}
          >
            {batchRunning ? t('video.batchRetry.running') : t('video.batchRetry.button')}
          </Button>
          <Button size="sm" onClick={fetchCanvas} disabled={loading}>{t('common.refresh')}</Button>
        </div>
      </div>

      {/* stale 标识（MASTER invariant 9 + ADR-0010「保留旧数据与 stale 标识是一套」）：
          换集期间屏幕上仍是**上一集**的分镜。必须显式说明，且上方写操作已禁用 ——
          只留旧数据不标 stale，用户会在旧集上点重生成，写错集还不报错。 */}
      {isCanvasStale && (
        <div role="status" className="rounded-md border border-warning/50 bg-warning-subtle px-2.5 py-1.5 text-xs text-warning-strong">
          {t('common.loading')}…（正在切换到本集的分镜，以上为上一集内容，暂不可操作）
        </div>
      )}

      {/* 加载态（此前首屏只剩标题栏，无任何反馈）：对齐真实区块的三列分镜卡 */}
      {loading && cards.length === 0 && (
        <div role="status" aria-live="polite" aria-label={t('common.loading')}>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-56 rounded-lg" />
            ))}
          </div>
        </div>
      )}

      {summary && (
        <div className="flex flex-wrap gap-4 text-sm text-ink-2">
          <span>{t('sb.shotCount', { n: summary.shot_count })}</span>
          <span>{t('sb.storyboardCount', { n: summary.storyboard_ready ?? 0 })}</span>
          <span>{t('sb.videoCount', { n: summary.video_ready ?? 0 })}</span>
          <span>{t('sb.keyframeCount', { n: summary.keyframe_end_ready ?? 0 })}</span>
          {(summary.qc_blocked ?? 0) > 0 && (
            <span className="text-warning-strong">{t('storyboard.qcBlocked')} {summary.qc_blocked}</span>
          )}
        </div>
      )}

      {notice && (
        <div className="p-3 bg-success-subtle border border-success/30 rounded-lg text-success-strong text-sm">
          {notice}
        </div>
      )}
      {/* 软失败：重做单镜失败时卡片仍在展示，只能用紧凑行内条，不能顶掉内容 */}
      {shotError && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
          {shotError}
        </div>
      )}

      {/* 批量重生成结果：成功 N/共 M + 失败明细（错误可能整段话，行内展示比 toast 从容） */}
      {batchResult && (
        <div className={`p-3 rounded-lg border text-sm ${
          batchResult.failures.length === 0
            ? 'bg-success-subtle border-success/30 text-success-strong'
            : 'bg-warning-subtle border-warning/30 text-warning-strong'
        }`}>
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">
              {batchResult.failures.length === 0
                ? t('video.batchRetry.done', { ok: batchResult.ok, total: batchResult.total })
                : t('video.batchRetry.partial', { ok: batchResult.ok, total: batchResult.total })}
            </span>
            <button
              type="button"
              onClick={() => setBatchResult(null)}
              aria-label={t('common.close')}
              className={`shrink-0 opacity-60 hover:opacity-100 ${FOCUS_RING}`}
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          {batchResult.failures.length > 0 && (
            <ul className="mt-2 space-y-1 text-xs">
              {batchResult.failures.map((f) => (
                <li key={f.shot_id} className="text-danger-strong break-all">
                  {t('video.batchRetry.failedItem', { shot_id: f.shot_id, error: f.error })}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {error === 'no-data' && (
        <EmptyState
          icon={<Clapperboard className="h-10 w-10" />}
          title={t('sb.noData')}
          description={t('sb.noDataHint')}
        />
      )}

      {/* 硬失败：分镜数据整体没取到且无卡片可展示 → ErrorState；
          已有卡片时的刷新失败 → 行内条（软失败） */}
      {error && error !== 'no-data' && cards.length === 0 && (
        <ErrorState
          title={t('project.loadingFailed')}
          description={error}
          onRetry={fetchCanvas}
        />
      )}
      {error && error !== 'no-data' && cards.length > 0 && (
        <div className="p-4 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong">{error}</div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards.map((card: any) => {
          const sid = String(card.shot_id);
          const imgBusy = busy === `${card.shot_id}:image`;
          const vidBusy = busy === `${card.shot_id}:video`;
          const mode = videoMode[sid] || 'reference';
          const isSelected = selectedShots.includes(sid);
          return (
            <div key={card.seq} className="bg-surface rounded-lg border border-line p-4 flex flex-col">
              <div className="flex items-center justify-between mb-2">
                <label className="flex items-center gap-1.5 cursor-pointer">
                  {/* 批量重生成勾选：已达上限时未勾选的复选框禁用（原生 checkbox，与 QcTab 一致） */}
                  <input
                    type="checkbox"
                    checked={isSelected}
                    disabled={batchRunning || (!isSelected && selectedShots.length >= BATCH_RETRY_LIMIT)}
                    onChange={() => toggleShotSelected(sid)}
                    aria-label={t('video.batchRetry.pickShot', { seq: card.seq })}
                    className={`rounded ${FOCUS_RING}`}
                  />
                  <span className="font-mono text-sm text-ink-2">#{card.seq}</span>
                </label>
                <span className="text-xs text-ink-2">{card.camera}</span>
              </div>

              <div className="flex gap-3 mb-2">
                {(() => {
                  // 有可放大查看的图（正式图优先，其次生成中 scratch）才让缩略框可点击
                  const zoomSrc = (card.storyboard?.exists && card.storyboard?.url)
                    || (card.storyboard?.generating && card.storyboard?.scratch_url);
                  return (
                <div
                  role={zoomSrc ? 'button' : undefined}
                  tabIndex={zoomSrc ? 0 : undefined}
                  aria-label={zoomSrc ? t('sb.zoomTitle') : undefined}
                  onClick={zoomSrc ? () => setZoomImg({ src: zoomSrc, seq: card.seq }) : undefined}
                  onKeyDown={(e) => {
                    if (zoomSrc && (e.key === 'Enter' || e.key === ' ')) {
                      e.preventDefault();
                      setZoomImg({ src: zoomSrc, seq: card.seq });
                    }
                  }}
                  className={`w-24 h-24 shrink-0 rounded bg-surface-2 border overflow-hidden flex items-center justify-center text-xs text-ink-3 ${
                    zoomSrc ? `cursor-zoom-in ${FOCUS_RING}` : ''
                  } ${
                    card.storyboard?.generating && !card.storyboard?.exists
                      ? 'border-brand/50'
                      : 'border-line'
                  }`}
                >
                  {card.storyboard?.exists && card.storyboard?.url ? (
                    <img src={card.storyboard.url} alt={t('sb.imageAlt', { seq: card.seq })} className="w-full h-full object-cover" />
                  ) : card.storyboard?.generating && card.storyboard?.scratch_url ? (
                    // 生成中：正式产物尚未落盘，但已有中间产物 → 显示实时缩略图 + 角标
                    // ⚠️ 中间产物可能被后续 try 覆盖 → 加时间戳查询参数绕过浏览器缓存
                    <div className="relative w-full h-full" title={t('sb.generatingHint')}>
                      <img
                        src={`${card.storyboard.scratch_url}${card.storyboard.scratch_url.includes('?') ? '&' : '?'}t=${Date.now()}`}
                        alt={t('sb.imageAlt', { seq: card.seq })}
                        className="w-full h-full object-cover"
                      />
                      <span className="absolute inset-x-0 bottom-0 bg-brand/85 text-white text-[10px] leading-4 text-center">
                        {t('sb.generating')}
                      </span>
                    </div>
                  ) : card.storyboard?.generating ? (
                    <span className="flex flex-col items-center gap-1 text-brand">
                      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />
                      <span>{t('sb.generating')}</span>
                    </span>
                  ) : (
                    <span>{t('sb.noImage')}</span>
                  )}
                </div>
                  );
                })()}
                <div className="min-w-0 flex-1 text-xs space-y-1">
                  <p className="text-ink-1 line-clamp-3">{card.description}</p>
                  {card.dialogue_text && (
                    <p className="text-ink-2 italic line-clamp-2">{card.dialogue_text}</p>
                  )}
                  <p className={card.video?.exists ? 'text-success-strong' : 'text-warning-strong'}>
                    {t('sb.video')}：{card.video?.exists ? t('sb.generated') : t('sb.notGenerated')}
                  </p>
                  {card.consistency?.score != null && (
                    <p className="text-ink-2">{t('sb.consistency')}：{card.consistency.score}</p>
                  )}
                </div>
              </div>

              {/* 内嵌播放器：插在信息区之后、操作区（带 mt-auto）之前，
                  使按钮仍被压到卡底，且与卡片内 mb-2 的间距风格一致。
                  ⚠️ 不写死高度、不加 aspect-video —— 重跑/超分后分辨率会变，交给浏览器按元数据自适应。 */}
              {playingVideo === sid && card.video?.url && (
                <video src={card.video.url} controls className="w-full mt-2 rounded-lg bg-black" />
              )}

              <div className="flex flex-wrap items-center gap-2 mt-auto pt-2">
                {card.video?.exists && card.video?.url && (
                  <>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => setPlayingVideo(playingVideo === sid ? null : sid)}
                    >
                      {playingVideo === sid ? t('common.close') : t('common.play')}
                    </Button>
                    {/* 端点未设 as_attachment，只能靠 HTML download 属性触发下载（同源，有效） */}
                    <a
                      href={card.video.url}
                      download
                      className={`text-xs text-brand hover:text-brand rounded-sm ${FOCUS_RING}`}
                    >
                      {t('common.download')}
                    </a>
                  </>
                )}
                <select
                  value={mode}
                  onChange={(e) =>
                    setVideoMode((prev) => ({ ...prev, [sid]: e.target.value as 'reference' | 'keyframe' }))
                  }
                  className={`text-xs rounded border border-line bg-surface text-ink-1 px-1 py-1 ${FOCUS_RING}`}
                  title={t('sb.modeHint')}
                >
                  <option value="reference">{t('sb.modeReference')}</option>
                  <option value="keyframe">{t('sb.modeKeyframe')}</option>
                </select>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => handleRetryImage(card)}
                  disabled={!!busy}
                >
                  {imgBusy ? t('sb.redoing') : t('sb.redoImage')}
                </Button>
                <Button
                  size="sm"
                  onClick={() => handleRetryVideo(card)}
                  disabled={!!busy}
                >
                  {vidBusy ? t('sb.redoing') : t('sb.redoVideo')}
                </Button>
                {/* 九宫格候选构图：一次生成 3x3 候选，弹窗内点选某格裁切为该镜分镜图。
                    生成中全站九宫格按钮禁用（防重复提交），弹窗内有进度提示 */}
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => runGridCandidates({ sid, seq: card.seq })}
                  loading={gridTarget?.sid === sid && gridPhase === 'generating'}
                  disabled={!!busy || gridPhase === 'generating' || gridPhase === 'applying'}
                  title={t('sb.grid.buttonHint')}
                >
                  {t('sb.grid.button')}
                </Button>
              </div>
            </div>
          );
        })}
      </div>

      {/* 批量重生成确认：与 QcTab 清空配置同款模式 —— loading 期间不可关闭，防重复提交 */}
      <ConfirmDialog
        isOpen={batchConfirmOpen}
        onClose={() => setBatchConfirmOpen(false)}
        onConfirm={runBatchRetry}
        title={t('video.batchRetry.confirmTitle')}
        message={
          <>
            <p>{t('video.batchRetry.confirmBody', { n: selectedShots.length, shots: selectedShots.join(', ') })}</p>
            <p className="mt-2 text-xs text-ink-3">{t('video.batchRetry.confirmHint')}</p>
          </>
        }
        confirmText={batchRunning ? t('video.batchRetry.running') : t('video.batchRetry.button')}
        loading={batchRunning}
      />

      {/* 九宫格候选构图弹窗：生成中显示进度提示；完成后在候选图上按 3x3 等分覆盖 9 个
          透明选格按钮（行优先 1-9，与后端 crop_grid_cell 的整图等分切分逐格对齐），
          点格 → 「应用所选格」即裁切为该镜正式分镜图并刷新画布 */}
      <Modal
        isOpen={gridTarget !== null}
        onClose={closeGridModal}
        preventClose={gridPhase === 'applying'}
        title={t('sb.grid.modalTitle', { seq: gridTarget?.seq ?? '' })}
        description={t('sb.grid.pickHint')}
        size="lg"
        footer={
          <>
            <span className="mr-auto text-xs text-ink-2">
              {gridCell != null ? t('sb.grid.pickedCell', { n: gridCell }) : ''}
            </span>
            <Button variant="secondary" onClick={closeGridModal} disabled={gridPhase === 'applying'}>
              {t('common.close')}
            </Button>
            <Button
              onClick={handleGridApply}
              disabled={gridPhase !== 'ready' || gridCell == null || !!gridError || !gridImgUrl}
              loading={gridPhase === 'applying'}
            >
              {gridPhase === 'applying' ? t('sb.grid.applying') : t('sb.grid.apply')}
            </Button>
          </>
        }
      >
        {/* 生成中：ComfyUI 出图可能耗时一两分钟，给明确进度文案，防用户以为卡死 */}
        {gridPhase === 'generating' && (
          <Loading size="md" label={t('sb.grid.generating')} />
        )}

        {/* 生成失败 / 取消：原因留在弹窗内，可原地重试（异常不打断页面） */}
        {gridPhase !== 'generating' && gridError && (
          <div className="space-y-3">
            <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm break-all">
              {gridError}
            </div>
            <Button
              variant="secondary"
              onClick={() => gridTarget && runGridCandidates(gridTarget)}
              disabled={gridPhase === 'applying'}
            >
              {t('sb.grid.regenerate')}
            </Button>
          </div>
        )}

        {/* 候选图 + 3x3 选格覆盖层：划分与后端裁切同口径（整图等分三行三列，行优先） */}
        {gridPhase !== 'generating' && !gridError && gridImgUrl && (
          <div className="relative select-none">
            <img
              src={gridImgUrl}
              alt={t('sb.grid.imageAlt', { seq: gridTarget?.seq ?? '' })}
              className="block w-full rounded-md border border-line bg-surface-2"
            />
            <div className="absolute inset-0 grid grid-cols-3 grid-rows-3">
              {Array.from({ length: 9 }, (_, i) => i + 1).map((n) => (
                <button
                  key={n}
                  type="button"
                  aria-label={t('sb.grid.cellN', { n })}
                  aria-pressed={gridCell === n}
                  title={t('sb.grid.cellN', { n })}
                  onClick={() => setGridCell(n)}
                  className={`relative cursor-pointer transition-colors ${FOCUS_RING} ${
                    gridCell === n
                      ? 'border-2 border-brand bg-brand/25'
                      : 'border border-transparent hover:border-brand/70 hover:bg-brand/10'
                  }`}
                >
                  <span
                    className={`absolute left-1 top-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                      gridCell === n ? 'bg-brand text-white' : 'bg-slate-900/70 text-white'
                    }`}
                  >
                    {n}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
      </Modal>

      {/* 分镜图点击放大（lightbox）：正式图 / 生成中 scratch 图都支持；原尺寸展示，ESC / 点遮罩关闭 */}
      <Modal
        isOpen={zoomImg !== null}
        onClose={() => setZoomImg(null)}
        title={t('sb.zoomTitle')}
        description={zoomImg ? `#${zoomImg.seq}` : ''}
        size="full"
      >
        {zoomImg && (
          <div className="flex items-center justify-center">
            <img
              src={zoomImg.src}
              alt={t('sb.imageAlt', { seq: zoomImg.seq })}
              className="max-w-full max-h-[78vh] rounded-lg border border-line bg-surface-2 object-contain"
            />
          </div>
        )}
      </Modal>
    </div>
  );
}

// ShotPromptEditor 一并导出：总览域的镜头卡片要内嵌它（跨域复用，
// 不复制第二份 —— 两份会各自漂移，改一处就出现行为不一致）。
export { StoryboardHubTab, ShotPromptEditor };

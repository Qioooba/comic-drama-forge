// ===== overview 域 =====
// 总览域：项目统计、剧本面板、资产上传 / 预览 / 沉淀、角色衣柜、生产进度。
//
// 本文件由机械切片从 `pages/ProjectWorkbenchPage.tsx` 搬出（ADR-0010 配套的
// 按域拆分）。代码逐字搬运：报错文案、提示语、空态措辞一律未改（R2）。

import React, { useState, useEffect, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { t } from '@/i18n';
import { videoApi, autopilotApi, episodesApi, novelsSplitPlanApi, preflightApi, screenplayApi, characterOutfits, characterSheetUpload, assetPrecipitation, generationApi, assetsApi, type EpisodeScenesResponse, type ChapterPreflightResult, type AssetPrecipitationResponse, type PrecipitationStatus } from '@/api/client';
import { Button, Input, EmptyState, ErrorState, Loading, Skeleton, Modal, Select } from '@/components/ui';
import { useToast } from '@/components/ui/toast';
import { useComfyProgress } from '@/hooks/useComfyProgress';
import { AlertTriangle, Box, Check, CheckCircle2, ClipboardList, FileText, FolderOpen, ImageIcon, Mountain, Upload, User, X } from '@/components/ui/icons';
import { assetSrc, useAutopilotStatus, type AssetItem, type ProjectAssets } from '@/api/queries';
// 跨域依赖：镜头卡片内嵌的分镜提示词编辑器属于分镜域，由该域导出
import { ShotPromptEditor } from '@/features/storyboard/StoryboardTab';
import type { AutopilotCurrent, CharacterOutfit } from '@/types';

// 焦点环：与 components/ui/index.tsx 里的 FOCUS_RING 逐字一致。
// index.css 有全局 :focus-visible outline 兜底，这里显式加 focus:outline-none 把它压掉，
// 否则 outline + ring 会叠成双环。凡因形状/类型原因换不成共享组件的原生控件，统一补这一串。
const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';


// ========== Overview Tab ==========

// 生产流水线步骤序列：与后端 pipeline.STEP_SEQUENCE 对齐（技术标识符 → i18n 键）。
const PRODUCTION_STEPS: { id: string; labelKey: string }[] = [
  { id: 'script', labelKey: 'wb.stepScript' },
  { id: 'tts_pre', labelKey: 'wb.stepTtsPre' },
  { id: 'assets', labelKey: 'wb.stepAssets' },
  { id: 'storyboard', labelKey: 'wb.stepStoryboard' },
  { id: 'keyframe', labelKey: 'wb.stepKeyframe' },
  { id: 'video', labelKey: 'wb.stepVideo' },
  { id: 'upscale', labelKey: 'wb.stepUpscale' },
  { id: 'final', labelKey: 'wb.stepFinal' },
];

/** 生产进度卡片：轮询 /api/autopilot/status，把「当前正在生产哪一集、当前步骤、百分比、
 *  超时告警」实时展示出来。此前这些数据后端都有，但前端从未渲染 —— 用户生产时只能干等。
 *  （报告 P1-5「无进度反馈」+ 建议 3「生产过程可视化」的落地）
 *
 *  ⚠️ ADR-0010：原先这里是手写 `setInterval(poll, 3000)` + `alive` 标志，
 *    与总控面板的 `useProductionStatus` 轮询**同一个端点**——两处同时挂载时
 *    每 3 秒打两遍后端，显示的内容却几乎相同。现在两处共用同一份 query 缓存，
 *    同一时刻只有一个请求在飞。失败仍静默（状态刷新是锦上添花）。
 */
function ProductionProgress({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const cur = useAutopilotStatus(projectKey).data ?? null;

  // ComfyUI 采样级实时进度：有生产任务时才轮询 comfyui 日志源（tqdm N/M）
  const comfy = useComfyProgress(!!cur);

  if (!cur) return null;

  const percent = Math.max(0, Math.min(100, Number(cur.percent) || 0));
  const stepsDone = Array.isArray(cur.steps_done) ? cur.steps_done : [];
  const stalled = Number(cur.step_stalled_sec) || 0;
  const stalledMin = Math.floor(stalled / 60);
  const stalledSec = stalled % 60;
  // step 可能是环节内子阶段（如 outline 提炼大纲属 script 步骤）——子阶段
  // 不在步骤表里，此时不点亮任何环节（百分比与描述行仍准确），避免步骤链错位
  const _rawStep = (cur.step || '').split(':')[0];
  const currentStepId = PRODUCTION_STEPS.some(s => s.id === _rawStep) ? _rawStep : null;
  const currentStep = PRODUCTION_STEPS.find(s => s.id === currentStepId);

  return (
    <div
      className="bg-surface rounded-lg border border-line p-4 mb-4"
      role="status"
      aria-live="polite"
      aria-label={t('wb.productionProgress')}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-semibold text-ink-1 flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-info animate-pulse" />
          {t('wb.productionProgress')}
        </span>
        {cur.episode != null && (
          <span className="text-sm text-ink-2">{t('wb.producingEpisode', { n: cur.episode })}</span>
        )}
      </div>

      {/* 进度条 */}
      <div className="w-full bg-line rounded-full h-2 mb-3">
        <div
          className="progress-fill h-2 rounded-full transition-colors"
          style={{ width: `${percent}%` }}
        />
      </div>

      {/* 步骤链 */}
      <div className="flex flex-wrap items-center gap-1.5 mb-2">
        {PRODUCTION_STEPS.map((s, i) => {
          const done = stepsDone.includes(s.id);
          const active = s.id === currentStepId;
          return (
            <React.Fragment key={s.id}>
              {i > 0 && <span className="text-ink-3 text-xs">→</span>}
              <span
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs ${
                  done
                    ? 'bg-success-subtle text-success-strong'
                    : active
                      ? 'bg-brand-subtle text-brand font-medium'
                      : 'bg-surface-2 text-ink-3'
                }`}
              >
                {done && <Check className="h-3 w-3" />}
                {t(s.labelKey)}
              </span>
            </React.Fragment>
          );
        })}
      </div>

      {/* 当前步骤消息 + 超时告警 */}
      {cur.message && <p className="text-xs text-ink-2 mb-1">{cur.message}</p>}

      {/* ComfyUI 采样进度（如「比例分镜 10/19」）：解析 comfyui 日志的 tqdm 行 */}
      {comfy.active && comfy.total > 0 && (
        <div className="mt-2 flex items-center gap-2 rounded-md border border-line bg-surface-2 px-2.5 py-1.5 text-xs">
          <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-accent" />
          <span className="shrink-0 text-ink-2">{t('live.comfySampling')}</span>
          <span className="shrink-0 font-medium tabular-nums text-ink-1">
            {comfy.current}/{comfy.total}
          </span>
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-line">
            <div className="progress-fill h-full rounded-full transition-colors duration-300" style={{ width: `${comfy.percent}%` }} />
          </div>
          <span className="shrink-0 tabular-nums text-ink-2">{comfy.percent}%</span>
        </div>
      )}
      {stalled >= 900 && (
        <div className="flex items-start gap-2 mt-2 p-2.5 rounded bg-warning-subtle text-warning-strong text-xs">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
          <div>
            <p className="font-medium">
              {t('wb.currentStep')}
              {currentStep ? `：${t(currentStep.labelKey)}` : ''} ·{' '}
              {t('wb.stepStalled', { min: stalledMin, sec: stalledSec })}
            </p>
            <p className="text-ink-2 mt-0.5">{t('wb.stallHint')}</p>
          </div>
        </div>
      )}
    </div>
  );
}

function OverviewTab({
  assets,
  projectKey,
  novelId,
  onRefreshAssets,
}: {
  assets: ProjectAssets | null;
  projectKey: string;
  novelId?: string;
  onRefreshAssets: () => Promise<void> | void;
}) {
  const { t } = useApp();
  const toast = useToast();
  const [preview, setPreview] = useState<{ item: AssetItem; type: 'character' | 'item' | 'scene' } | null>(null);
  // 上传形象图 → 三视图（零 GPU 本地切分）
  const [uploadOpen, setUploadOpen] = useState(false);
  // 手动一条龙：一键批量生成角色/物品/场景资产。三类顺序提交并逐任务轮询，
  // 避免同时把多个 ComfyUI 资产任务压满。
  const [batchAssetsBusy, setBatchAssetsBusy] = useState(false);
  const [batchAssetsNotice, setBatchAssetsNotice] = useState('');
  const [batchAssetsNoticeKind, setBatchAssetsNoticeKind] = useState<'success' | 'error'>('success');
  const batchAssetsReqRef = useRef(0);
  // 可上传的角色名：优先取已有角色资产（用户大概率是给已抽出的角色换图），
  // 没有资产时也给个空列表让用户手填 —— 支持「先上传形象图再跑剧本」的用法。
  const uploadCharacters = React.useMemo(
    () => (assets?.gallery?.characters || []).map((c) => c.name).filter(Boolean),
    [assets]
  );

  // 剧本相关状态
  const [episodes, setEpisodes] = useState<any[]>([]);
  const [totalEpisodes, setTotalEpisodes] = useState(0);
  const [scriptLoading, setScriptLoading] = useState(true);
  const [scriptError, setScriptError] = useState('');
  const [selectedEpisode, setSelectedEpisode] = useState<number | null>(null);
  const [episodeDetail, setEpisodeDetail] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  /** 记下最近一次请求的集号：详情加载失败后 ErrorState 的「重试」要能定位回同一集 */
  const lastDetailEpRef = React.useRef<number | null>(null);

  // P2-2 分集断点提议（与生产口径同源的只读预览，不触发任何生成）
  const [splitPlanOpen, setSplitPlanOpen] = useState(false);
  const [splitPlan, setSplitPlan] = useState<any | null>(null);
  const [splitPlanLoading, setSplitPlanLoading] = useState(false);
  const [splitPlanError, setSplitPlanError] = useState('');
  // 前置解析（chapter pre-flight）：人物档案 + 故事梗概 + 关键事件 + 情绪基线
  const [preflightDone, setPreflightDone] = useState<number[]>([]);
  const [preflightRunning, setPreflightRunning] = useState<number | null>(null);
  const [preflightDetail, setPreflightDetail] = useState<ChapterPreflightResult | null>(null);
  const [preflightOpen, setPreflightOpen] = useState(false);

  // --- 按场次生成（2026-10-03）：集行可展开「第1场/第2场/…」场次层级 ---
  // 视频生产已改为按场次生成：每场一个 scene_XX.mp4，全部完成后拼成 epNN_full.mp4。
  // 展开时拉 GET /api/episode/scenes（每次展开都重新拉：分镜/视频状态随生产推进变化）。
  const [scenesOpenEps, setScenesOpenEps] = useState<number[]>([]);
  const [scenesByEp, setScenesByEp] = useState<Record<number, EpisodeScenesResponse | null>>({});
  const [scenesLoadingEp, setScenesLoadingEp] = useState<number | null>(null);
  const [scenesErrByEp, setScenesErrByEp] = useState<Record<number, string>>({});
  /** 「重做本场」进行中的键（"集:场"）；非空时所有重做按钮禁用，防止并发占满 ComfyUI */
  const [redoBusy, setRedoBusy] = useState<string | null>(null);
  const redoReqRef = useRef(0);

  const runPreflight = async (chIdx: number) => {
    if (!novelId) return;
    setPreflightRunning(chIdx);
    try {
      const d = await preflightApi.analyze(novelId, chIdx);
      if (d.success && d.result) {
        setPreflightDetail(d.result);
        setPreflightDone(prev => [...new Set([...prev, chIdx])].sort((a, b) => a - b));
        toast.success(t('wb.preflightDone', { n: chIdx, chars: d.result.characters?.length ?? 0 }));
      } else {
        toast.error(t('wb.preflightFailed', { err: (d as any).error ?? '' }));
      }
    } catch (err: any) {
      toast.error(t('wb.preflightFailed', { err: err?.message ?? '' }));
    } finally {
      setPreflightRunning(null);
    }
  };

  const openPreflightDetail = async (chIdx: number) => {
    if (!novelId) return;
    const d = await preflightApi.get(novelId, chIdx);
    if (d.exists && d.result) {
      setPreflightDetail(d.result);
      setPreflightOpen(true);
    }
  };

  // --- 文学剧本（人审层，两段式第一步）：1章=1集，episode_no 即章号 ---
  const [spOpen, setSpOpen] = useState(false);
  const [spEpisode, setSpEpisode] = useState<number | null>(null);
  const [spContent, setSpContent] = useState('');
  const [spLoading, setSpLoading] = useState(false);
  const [spGenerating, setSpGenerating] = useState(false);
  const [spRewriting, setSpRewriting] = useState(false);
  // 2026-10-04 改写已自动化：本标志仅用于弹窗底部状态条展示「✅ 分镜剧本已生成」，
  // 打开弹窗/重新生成时重置（回看已有剧本永远走不到该状态）
  const [spRewritten, setSpRewritten] = useState(false);
  const [spError, setSpError] = useState('');
  // 标签切换会卸载本组件：卸载后让任务轮询让位，不再 setState
  const spAliveRef = useRef(true);
  useEffect(() => {
    spAliveRef.current = true;
    return () => { spAliveRef.current = false; };
  }, []);
  // 请求令牌：A 章生成中用户改点 B 章时，丢弃 A 的迟到结果，防止跨章串内容
  const spReqRef = useRef(0);

  /** 轮询 generation 任务至终态（3s 一次，15 分钟兜底）。
   *  ⚠️ /api/generation/status 历史上有 {success, task:{…}} 信封与顶层平铺两种形态，
   *  按 `task ?? 顶层` 兼容读取（与九宫格候选轮询同口径）。 */
  /** failMsg/timeoutMsg：场次重做等非剧本场景传入各自的错误文案（缺省回落剧本文案） */
  const pollGenerationTask = async (taskId: string, isCurrent: () => boolean, failMsg?: string, timeoutMsg?: string) => {
    const deadline = Date.now() + 15 * 60 * 1000;
    for (;;) {
      await new Promise(r => setTimeout(r, 3000));
      if (!spAliveRef.current || !isCurrent()) throw new Error(failMsg || t('wb.screenplayGenerateFailed'));
      const d: any = await generationApi.status(taskId);
      const task = (d?.task ?? d) || {};
      if (task.status === 'completed') return;
      if (task.status === 'failed' || task.status === 'cancelled') {
        throw new Error(task.error || failMsg || t('wb.screenplayGenerateFailed'));
      }
      if (Date.now() > deadline) throw new Error(timeoutMsg || t('wb.screenplayTimeout'));
    }
  };

  /** 每章行「文学剧本」：已有则直接弹窗展示；没有则发起生成（异步任务轮询）后再展示 */
  const openScreenplay = async (chapterNo: number) => {
    if (!novelId) return;
    const token = ++spReqRef.current;
    const isCurrent = () => token === spReqRef.current;
    setSpEpisode(chapterNo);
    setSpContent('');
    setSpError('');
    setSpGenerating(false);
    // 上一章迟到的改写请求被令牌作废后可能遗留 true：不许把改写态带进本章
    setSpRewriting(false);
    setSpRewritten(false);
    setSpOpen(true);
    setSpLoading(true);
    try {
      const doc = await screenplayApi.get(novelId, chapterNo, projectKey);
      if (!isCurrent()) return;
      if (doc.exists && doc.markdown) {
        // 回看已有剧本：只展示，不自动改写（避免回看场景重复烧 LLM）
        setSpContent(doc.markdown);
        setSpLoading(false);
        return;
      }
      // 不存在 → 生成（异步任务 + 轮询），完成后回读展示
      const r = await screenplayApi.generate(novelId, chapterNo, { projectName: projectKey });
      if (!r?.task_id) throw new Error(t('wb.screenplayStartFailed'));
      setSpLoading(false);
      setSpGenerating(true);
      await pollGenerationTask(r.task_id, isCurrent);
      if (!isCurrent()) return;
      const done = await screenplayApi.get(novelId, chapterNo, projectKey);
      if (!done.exists || !done.markdown) throw new Error(t('wb.screenplayNotGenerated'));
      setSpContent(done.markdown);
      // 2026-10-04 需求：文学剧本无需人工确认 —— 生成（轮询到 completed）后立即
      // 自动改写为分镜剧本，等价于旧版自动点「确认无误」按钮。先落 spGenerating
      // 再进改写，弹窗底部状态条才能依次显示「生成中 → 自动改写中 → 已完成」。
      // 改写失败由 runScreenplayRewrite 内部落在 spError，不进本层 catch。
      setSpGenerating(false);
      await runScreenplayRewrite(chapterNo, isCurrent);
    } catch (err) {
      if (isCurrent()) {
        setSpError(err instanceof Error ? err.message : t('wb.screenplayGenerateFailed'));
      }
    } finally {
      if (isCurrent()) {
        setSpLoading(false);
        setSpGenerating(false);
      }
    }
  };

  /** 两段式第二步：以文学剧本为原文重写成结构化分镜剧本。
   *  2026-10-04 起无需人工确认：「生成文学剧本」轮询到 completed 后由 openScreenplay
   *  自动调用本函数（等价于旧版自动点「确认无误」按钮）；弹窗底部确认按钮已改为
   *  流程状态展示，不再有手动入口。
   *  - chapter 显式传参：自动链路里同批 setState 尚未落地，读 spEpisode 会拿到旧章号；
   *  - stillCurrent 令牌守卫：自动链路传 openScreenplay 的 isCurrent，用户切章后迟到的
   *    改写结果不再动 UI；防重入由 spRewriting 兜底（改写中不允许再次触发）。 */
  const runScreenplayRewrite = async (chapter: number, stillCurrent?: () => boolean) => {
    if (!novelId || spRewriting) return;
    const alive = stillCurrent ?? (() => spAliveRef.current);
    setSpRewriting(true);
    setSpError('');
    setSpRewritten(false);
    try {
      const r = await episodesApi.generate(novelId, { chapters: [chapter], use_screenplay: true });
      // 兼容同步/异步两种返回：带 task_id 则轮询到完成
      const taskId = r?.task_id;
      if (taskId) {
        await pollGenerationTask(String(taskId), alive, t('wb.screenplayRewriteFailed'), t('wb.screenplayRewriteTimeout'));
      }
      if (!alive()) return;
      toast.success(t('wb.screenplayRewriteDone'));
      // 成功后弹窗保持打开，底部状态条显示「✅ 分镜剧本已生成」；剧集列表在背后刷新
      setSpRewritten(true);
      fetchEpisodes();
    } catch (err) {
      if (alive()) setSpError(err instanceof Error ? err.message : t('wb.screenplayRewriteFailed'));
    } finally {
      setSpRewriting(false);
    }
  };

  // 加载剧集列表
  // 抽成具名函数：错误态需要「重试」入口，而 useEffect 无法被手动重新触发。
  // 取数逻辑与原实现逐字一致，仅补一次错误清理，避免重试成功后旧的失败文案残留。
  const fetchEpisodes = React.useCallback(() => {
    if (!novelId) return;
    setScriptLoading(true);
    setScriptError('');
    episodesApi.list(novelId)
      .then(data => {
        setEpisodes(data.episodes || []);
        setTotalEpisodes(data.total || 0);
      })
      .catch(err => {
        setScriptError(err instanceof Error ? err.message : t('project.loadingFailed'));
      })
      .finally(() => {
        setScriptLoading(false);
      });
  }, [novelId, t]);

  useEffect(() => { fetchEpisodes(); }, [fetchEpisodes]);

  // 拉取分集断点提议：默认不带参数，与生产 autopilot.episode_units 完全同源，
  // 保证「提议 ≡ 实际生成」，不会提议说 1 集、真生成拆 3 集。
  const fetchSplitPlan = React.useCallback(() => {
    if (!novelId) return;
    setSplitPlanLoading(true);
    setSplitPlanError('');
    novelsSplitPlanApi.get(novelId)
      .then(data => {
        setSplitPlan(data);
        setSplitPlanOpen(true);
      })
      .catch(err => {
        setSplitPlan(null);
        setSplitPlanError(err instanceof Error ? err.message : t('project.loadingFailed'));
        setSplitPlanOpen(true);
      })
      .finally(() => setSplitPlanLoading(false));
  }, [novelId, t]);

  // --- 场次层级（按场次生成）：展开拉取 + 重做本场 ---
  const fetchScenes = React.useCallback(async (episodeNo: number, silent = false) => {
    if (!projectKey) return;
    if (!silent) setScenesLoadingEp(episodeNo);
    try {
      const d = await episodesApi.scenes(projectKey, episodeNo);
      setScenesByEp(prev => ({ ...prev, [episodeNo]: d }));
      setScenesErrByEp(prev => { const next = { ...prev }; delete next[episodeNo]; return next; });
    } catch (err) {
      // 静默刷新失败时保留旧数据（展开着的面板不该因一次轮询失败闪成报错）
      if (!silent) {
        setScenesErrByEp(prev => ({ ...prev, [episodeNo]: err instanceof Error ? err.message : t('wb.scenesLoadFailed') }));
      }
    } finally {
      if (!silent) setScenesLoadingEp(null);
    }
  }, [projectKey, t]);

  const toggleScenes = (episodeNo: number) => {
    const willOpen = !scenesOpenEps.includes(episodeNo);
    setScenesOpenEps(prev => (willOpen ? [...prev, episodeNo] : prev.filter(n => n !== episodeNo)));
    if (willOpen) void fetchScenes(episodeNo);
  };

  /** 「重做本场」：只重生成这一场（POST /videos/generate only_scenes=[X] overwrite=true），
   *  轮询 generation/status 至终态后静默刷新场次状态。直接执行不加确认 ——
   *  与资产「重新生成」同款项目习惯（误重做可再重做一次，非不可逆操作）。 */
  const redoScene = async (episodeNo: number, sceneNo: number) => {
    if (redoBusy) return;
    const key = `${episodeNo}:${sceneNo}`;
    const token = ++redoReqRef.current;
    const isCurrent = () => spAliveRef.current && token === redoReqRef.current;
    setRedoBusy(key);
    try {
      const r = await videoApi.generateEpisode({
        project_name: projectKey,
        episode_no: episodeNo,
        only_scenes: [sceneNo],
        overwrite: true,
      });
      if (r?.task_id) {
        await pollGenerationTask(String(r.task_id), isCurrent, t('wb.sceneRedoFailed'), t('wb.sceneRedoTimeout'));
      }
      if (isCurrent()) toast.success(t('wb.sceneRedoDone', { n: sceneNo }));
    } catch (err) {
      if (isCurrent()) toast.error(err instanceof Error ? err.message : t('wb.sceneRedoFailed'));
    } finally {
      // 与 openScreenplay 同款：组件已卸载（切标签页）就不再 setState / 刷新
      if (isCurrent()) {
        setRedoBusy(null);
        void fetchScenes(episodeNo, true);
      }
    }
  };

  // 集行「整集已拼接」徽标：列表加载完后对每集静默拉一次场次状态，
  // 未展开也能看到哪些集已拼出整集成片。只读、失败静默（徽标是锦上添花）。
  useEffect(() => {
    if (scriptLoading || episodes.length === 0 || !projectKey) return;
    let alive = true;
    Promise.all(
      episodes.map((ep: any) =>
        episodesApi.scenes(projectKey, ep.episode_no)
          .then(d => ({ no: ep.episode_no as number, d }))
          .catch(() => null)
      )
    ).then(results => {
      if (!alive) return;
      const ok = results.filter(Boolean) as { no: number; d: EpisodeScenesResponse }[];
      if (ok.length === 0) return;
      setScenesByEp(prev => {
        const next = { ...prev };
        for (const { no, d } of ok) next[no] = d;
        return next;
      });
    });
    return () => { alive = false; };
  }, [scriptLoading, episodes, projectKey]);

  /** 一键批量生成资产：遍历项目全部剧集读取剧本 characters/items/scenes，
   *  三类顺序提交 /api/assets/generate 并等待任务终态；已有资产由后端断点续跑跳过。 */
  const batchGenerateAssets = async () => {
    if (batchAssetsBusy || scriptLoading || !novelId || episodes.length === 0) return;
    const token = ++batchAssetsReqRef.current;
    setBatchAssetsBusy(true);
    setBatchAssetsNotice('');
    try {
      const byKey = {
        characters: new Map<string, any>(),
        items: new Map<string, any>(),
        scenes: new Map<string, any>(),
      };
      for (const ep of episodes) {
        const detail = await episodesApi.get(novelId, ep.episode_no);
        if (token !== batchAssetsReqRef.current || !spAliveRef.current) return;
        const script = ((detail as any)?.script || {}) as Record<string, any[]>;
        (script.characters || []).forEach((x: any) => x?.name && byKey.characters.set(String(x.name), x));
        (script.items || []).forEach((x: any) => x?.name && byKey.items.set(String(x.name), x));
        (script.scenes || []).forEach((x: any) => x?.name && byKey.scenes.set(String(x.name), x));
      }
      const groups: { type: 'character' | 'item' | 'scene'; key: string }[] = [
        { type: 'character', key: 'characters' },
        { type: 'item', key: 'items' },
        { type: 'scene', key: 'scenes' },
      ];
      let started = 0;
      for (const g of groups) {
        if (token !== batchAssetsReqRef.current || !spAliveRef.current) return;
        const list = Array.from((byKey as any)[g.key].values());
        if (list.length === 0) continue;
        const r = await assetsApi.generate({
          asset_type: g.type,
          project_name: projectKey,
          assets: list,
          overwrite: false,
        });
        if (r?.task_id) {
          await pollGenerationTask(
            String(r.task_id),
            () => token === batchAssetsReqRef.current && spAliveRef.current,
            t('tts.batchAssetsFailed'),
            t('tts.batchAssetsTimeout')
          );
        }
        started += 1;
      }
      if (token !== batchAssetsReqRef.current || !spAliveRef.current) return;
      setBatchAssetsNoticeKind('success');
      setBatchAssetsNotice(started ? t('tts.batchAssetsDone') : t('tts.batchAssetsNothing'));
    } catch (err) {
      if (token === batchAssetsReqRef.current && spAliveRef.current) {
        setBatchAssetsNoticeKind('error');
        setBatchAssetsNotice(err instanceof Error ? err.message : t('tts.batchAssetsFailed'));
      }
    } finally {
      if (token === batchAssetsReqRef.current && spAliveRef.current) setBatchAssetsBusy(false);
    }
  };

  // 加载单集详情
  // ⚠️ 后端 /api/episodes/<novel>/<ep> 的剧本正文嵌在 `script` 对象下（shots/characters/items/scenes），
  // 且列表行才带 status/completed_shots（_episode_progress 推导），详情接口本身不返回这两个字段。
  // 这里摊平成视图直接可读的结构，并从已加载的剧集列表补进度字段，避免详情恒显「暂无剧本内容」。
  const loadEpisodeDetail = async (episodeNo: number) => {
    if (!novelId) return;
    lastDetailEpRef.current = episodeNo;
    setDetailLoading(true);
    setDetailError('');
    try {
      const detail = await episodesApi.get(novelId, episodeNo);
      const script = (detail as any).script || {};
      const shots: any[] = script.shots || [];
      // 从剧集列表找本行进度（status / completed_shots / created_at）
      const row = episodes.find((e: any) => e.episode_no === episodeNo);
      const normalized: any = {
        ...detail,
        ...script,
        episode_no: detail.episode_no ?? episodeNo,
        title: detail.episode_title || detail.project_name || script.title,
        chapter_title: detail.episode_title,
        shots,
        shot_count: (detail as any).stats?.shot_count || script.shot_count || shots.length,
        status: row?.status ?? (detail as any).status ?? 'pending',
        completed_shots: row?.completed_shots ?? (detail as any).completed_shots ?? 0,
        created_at: row?.generated_at ?? (detail as any).created_at,
      };
      setEpisodeDetail(normalized);
      setSelectedEpisode(episodeNo);
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : t('wb.loadDetailFailed'));
    } finally {
      setDetailLoading(false);
    }
  };

  // 返回列表
  const goBack = () => {
    setSelectedEpisode(null);
    setEpisodeDetail(null);
    setDetailError('');
  };

  const groups: { key: 'characters' | 'items' | 'scenes'; label: string; icon: React.ReactNode; type: 'character' | 'item' | 'scene' }[] = [
    { key: 'characters', label: t('wb.characters'), icon: <User className="h-4 w-4" />, type: 'character' },
    { key: 'items', label: t('wb.items'), icon: <Box className="h-4 w-4" />, type: 'item' },
    { key: 'scenes', label: t('wb.scenes'), icon: <Mountain className="h-4 w-4" />, type: 'scene' },
  ];

  const total = groups.reduce((n, g) => n + (assets?.gallery?.[g.key]?.length || 0), 0);

  // 显示单集详情
  if (selectedEpisode !== null && episodeDetail && !detailError) {
    return (
      <div className="space-y-4">
        <Button variant="ghost" onClick={goBack}>
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
          </svg>
          {t('wb.backToList')}
        </Button>

        <div className="bg-surface rounded-lg border border-line p-6">
          <div className="flex items-start justify-between">
            <div>
              <h3 className="text-xl font-bold text-ink-1">
                {t('wb.episodeNo', { n: episodeDetail.episode_no })}
                {episodeDetail.title && <span className="ml-2 text-lg font-normal text-ink-2">{episodeDetail.title}</span>}
              </h3>
              {episodeDetail.chapter_title && (
                <p className="text-sm text-ink-2 mt-1">{t('wb.chapterLabel')}{episodeDetail.chapter_title}</p>
              )}
            </div>
            <span className={`px-3 py-1 rounded-full text-xs font-medium inline-flex items-center gap-1 ${
              episodeDetail.status === 'done' ? 'bg-success-subtle text-success-strong' :
              episodeDetail.status === 'producing' ? 'bg-info-subtle text-info-strong' :
              episodeDetail.status === 'failed' ? 'bg-danger-subtle text-danger-strong' :
              'bg-surface-2 text-ink-1'
            }`}>
              {episodeDetail.status === 'done' ? (<><Check className="h-3.5 w-3.5" /> {t('wb.done')}</>) :
               episodeDetail.status === 'producing' ? t('wb.producing') :
               episodeDetail.status === 'failed' ? (<><X className="h-3.5 w-3.5" /> {t('episodes.failed')}</>) :
               t('ep.pending')}
            </span>
          </div>

          <div className="mt-4 flex items-center gap-4 text-sm text-ink-2">
            <span>{t('wb.shotProgress', { done: episodeDetail.completed_shots, total: episodeDetail.shot_count })}</span>
            {episodeDetail.created_at && (
              <span>{t('wb.createdAt', { time: episodeDetail.created_at.split('T')[0] })}</span>
            )}
          </div>

          {episodeDetail.shot_count > 0 && (
            <div className="mt-3 w-full bg-line rounded-full h-2">
              <div
                className={`h-2 rounded-full transition-colors ${
                  episodeDetail.status === 'done' ? 'bg-success' :
                  episodeDetail.status === 'failed' ? 'bg-danger' :
                  'bg-brand'
                }`}
                style={{ width: `${(episodeDetail.completed_shots / episodeDetail.shot_count) * 100}%` }}
              ></div>
            </div>
          )}
        </div>

        {/* 文学剧本（人审层）：默认收起，展开才拉取；novel_id 拿不到时整个面板不渲染。
            面板标题旁注明「此为人审稿」—— 下方「剧本内容」区块即改写后的结构化分镜剧本。 */}
        <ScreenplayPanel novelId={novelId} episodeNo={selectedEpisode} />

        <div className="bg-surface rounded-lg border border-line p-6">
          <h4 className="font-semibold text-ink-1 mb-4">{t('wb.scriptContent')}</h4>

          {(episodeDetail.shots && episodeDetail.shots.length > 0) ? (
            <div className="space-y-4">
              {episodeDetail.shots.map((shot: any, idx: number) => (
                // 用 shot_id 作 key：剧本重新生成会改镜数与顺序（列表**可增删可重排**），
                // 用 idx 会让已渲染的节点被复用成另一镜。shot_id 缺失时退回 idx，
                // 行为与改前一致（这里的 shots 是 any，取不到 id 时不能凭空造一个）。
                <div key={shot.shot_id ?? idx} className="border-l-4 border-brand pl-4 py-2">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <span className="px-2 py-0.5 bg-brand-subtle text-brand text-xs font-medium rounded">
                      {t('wb.shotN', { n: shot.shot_id ?? idx + 1 })}
                    </span>
                    {shot.camera && (
                      <span className="text-xs text-ink-2">{shot.camera}</span>
                    )}
                    {shot.location && (
                      <span className="text-xs text-ink-2">· {shot.location}</span>
                    )}
                    {shot.duration != null && (
                      <span className="text-xs text-ink-3">· {shot.duration}s</span>
                    )}
                  </div>
                  <ShotPromptEditor
                      shot={shot}
                      novelId={novelId}
                      episodeNo={selectedEpisode!}
                      onSaved={() => loadEpisodeDetail(selectedEpisode!)}
                    />
                  {shot.dialogue_text && (
                    <p className="text-sm text-ink-1 mt-1 pl-2 border-l-2 border-line-strong">
                      {shot.dialogue_text}
                    </p>
                  )}
                  {shot.visual_detail && (
                    <p className="text-xs text-ink-2 mt-1">
                      {t('wb.visualDetail', { text: shot.visual_detail })}
                    </p>
                  )}
                  {shot.audio_cues && (
                    <p className="text-xs text-ink-3 mt-1">
                      {t('wb.audioCues', { text: shot.audio_cues })}
                    </p>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-ink-2 text-sm">
              {t('wb.noShotsInEpisode')}
            </p>
          )}
        </div>
      </div>
    );
  }

  if (detailError) {
    // 硬失败：单集详情整块取不到数据，用 ErrorState 顶掉内容区（不是把已渲染内容盖掉）
    return (
      <div className="space-y-4">
        <ErrorState
          title={t('project.loadingFailed')}
          description={detailError}
          onRetry={() => {
            const n = lastDetailEpRef.current;
            if (n != null) loadEpisodeDetail(n);
          }}
        />
        <Button variant="link" className="text-sm" onClick={goBack}>
          {t('wb.backToList')}
        </Button>
      </div>
    );
  }

  if (detailLoading) {
    return (
      <div className="space-y-4" role="status" aria-live="polite" aria-label={t('common.loading')}>
        <Skeleton className="h-9 w-28" />
        <Skeleton className="h-40 rounded-lg" />
        <Skeleton className="h-64 rounded-lg" />
      </div>
    );
  }

  if (total === 0 && episodes.length === 0) {
    return (
      <EmptyState
        icon={<FolderOpen className="h-10 w-10" />}
        title={t('wb.noAssets')}
        description={t('wb.noAssetsHint')}
        action={
          <p className="text-sm text-brand">
            {t('wb.noAssetsAction')}
          </p>
        }
      />
    );
  }

  return (
    <div className="space-y-8">
      {/* 生产进度（实时）：把 autopilot 当前正在生产的一集/步骤/百分比/超时告警展示出来 */}
      <ProductionProgress projectKey={projectKey} />

      {/* 资产展示 */}
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-lg font-semibold text-ink-1">{t('wb.assetsTitle')}</h3>
        <div className="flex items-center gap-2">
          <Button
            variant="brand"
            onClick={batchGenerateAssets}
            disabled={batchAssetsBusy || scriptLoading || !novelId || episodes.length === 0}
            aria-describedby="batch-assets-reason"
            title={
              batchAssetsBusy ? t('tts.batchAssetsBusy')
              : !novelId || episodes.length === 0 ? t('tts.batchAssetsNoScript')
              : t('tts.batchAssetsHint')
            }
          >
            {batchAssetsBusy ? t('common.generating') : t('tts.batchAssetsGenerate')}
          </Button>
          <span id="batch-assets-reason" className="sr-only">
            {batchAssetsBusy ? t('tts.batchAssetsBusy') : !novelId || episodes.length === 0 ? t('tts.batchAssetsNoScript') : t('tts.batchAssetsHint')}
          </span>
          <Button variant="secondary" onClick={() => setUploadOpen(true)}>
            <Upload className="h-4 w-4 mr-1.5" />
            {t('uploadSheet.entry')}
          </Button>
        </div>
      </div>
      {batchAssetsNotice && (
        <div className={`p-3 rounded-lg border text-sm ${batchAssetsNoticeKind === 'error' ? 'bg-danger-subtle border-danger/30 text-danger-strong' : 'bg-success-subtle border-success/30 text-success-strong'}`}>
          {batchAssetsNotice}
        </div>
      )}
      {total > 0 && (
        <>
          {groups.map((g) => {
            const list = assets?.gallery?.[g.key] || [];
            if (list.length === 0) return null;
            return (
              <div key={g.key}>
                <h3 className="text-sm font-semibold text-ink-2 mb-3 flex items-center gap-1.5">
                  {g.icon} {g.label} · {list.length}
                </h3>
                <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
                  {list.map((item, idx) => (
                    <AssetCard
                      key={`${g.key}-${idx}`}
                      item={item}
                      type={g.type}
                      onClick={() => setPreview({ item, type: g.type })}
                    />
                  ))}
                </div>
              </div>
            );
          })}
          <AssetPreviewModal
            preview={preview}
            projectKey={projectKey}
            onClose={() => setPreview(null)}
            onRegenerated={onRefreshAssets}
          />
        </>
      )}
      {/* 上传形象图模态挂在 total>0 之外：空项目也能先上传角色形象图再跑剧本 */}
      <UploadSheetModal
        isOpen={uploadOpen}
        onClose={() => setUploadOpen(false)}
        projectKey={projectKey}
        characters={uploadCharacters}
        onUploaded={onRefreshAssets}
      />

      {/* 剧本概览 */}
      <div className="border-t border-line pt-8">
        <h3 className="text-lg font-semibold text-ink-1 mb-4 flex items-center gap-2">
          <FileText className="h-5 w-5" /> {t('wb.script')}
        </h3>

        {scriptLoading ? (
          // 骨架对齐真实区块：4 张统计卡 → 进度条 → 剧集列表卡
          <div role="status" aria-live="polite" aria-label={t('common.loading')}>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-20 rounded-lg" />
              ))}
            </div>
            <Skeleton className="mt-4 h-16 rounded-lg" />
            <Skeleton className="mt-4 h-48 rounded-lg" />
          </div>
        ) : scriptError ? (
          <ErrorState
            title={t('project.loadingFailed')}
            description={scriptError}
            onRetry={fetchEpisodes}
          />
        ) : episodes.length === 0 ? (
          <EmptyState
            icon={<ClipboardList className="h-10 w-10" />}
            title={t('episodes.noEpisodes')}
            description={t('wb.startAutoFirst')}
          />
        ) : (
          <>
            {/* 统计卡片 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
              <div className="bg-surface rounded-lg border border-line p-4">
                <div className="text-2xl font-bold text-ink-1">{totalEpisodes}</div>
                <div className="text-sm text-ink-2">{t('wb.totalEpisodes')}</div>
              </div>
              {(() => {
                const stats = episodes.reduce((acc: any, ep: any) => {
                  acc[ep.status] = (acc[ep.status] || 0) + 1;
                  return acc;
                }, {} as Record<string, number>);
                return [
                  { label: t('episodes.done'), count: stats['done'] || 0, color: 'text-success-strong' },
                  { label: t('episodes.producing'), count: stats['producing'] || 0, color: 'text-info-strong' },
                  { label: t('episodes.failed'), count: stats['failed'] || 0, color: 'text-danger-strong' },
                  { label: t('episodes.pending'), count: stats['pending'] || 0, color: 'text-ink-2' },
                ].map(s => (
                  <div key={s.label} className="bg-surface rounded-lg border border-line p-4">
                    <div className={`text-2xl font-bold ${s.color}`}>{s.count}</div>
                    <div className="text-sm text-ink-2">{s.label}</div>
                  </div>
                ));
              })()}
            </div>

            {/* 进度条 */}
            {totalEpisodes > 0 && (
              <div className="bg-surface rounded-lg border border-line p-4 mb-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm font-medium text-ink-1">{t('wb.overallProgress')}</span>
                  <span className="text-sm text-ink-2">{Math.round(((episodes.filter((e: any) => e.status === 'done').length) / totalEpisodes) * 100)}%</span>
                </div>
                <div className="w-full bg-line rounded-full h-2">
                  <div
                    className="bg-success h-2 rounded-full transition-colors"
                    style={{ width: `${((episodes.filter((e: any) => e.status === 'done').length) / totalEpisodes) * 100}%` }}
                  ></div>
                </div>
              </div>
            )}

            {/* 前置解析卡片：人物档案 + 故事梗概 + 关键事件 + 情绪基线 */}
            {preflightOpen && (
              <div className="bg-surface rounded-lg border border-line p-4 mb-4">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <h4 className="font-semibold text-ink-1">{t('wb.preflightTitle')}</h4>
                    <p className="text-xs text-ink-3 mt-0.5">{t('wb.preflightDesc')}</p>
                  </div>
                  <button onClick={() => setPreflightOpen(false)}
                    className={`p-1.5 rounded-md text-ink-3 hover:bg-surface-2 transition-colors ${FOCUS_RING}`}>
                    <X className="h-4 w-4" />
                  </button>
                </div>
                {preflightDetail ? (
                  <div className="space-y-3">
                    <div className="text-sm text-ink-2">
                      <strong className="text-ink-1">{t('wb.preflightSummary')}</strong>
                      <p className="mt-1 text-ink-2">{preflightDetail.story_summary}</p>
                    </div>
                    {preflightDetail.key_events?.length > 0 && (
                      <div>
                        <strong className="text-sm text-ink-1">{t('wb.preflightKeyEvents')}</strong>
                        <ol className="mt-1 space-y-0.5 text-xs text-ink-2 list-decimal list-inside">
                          {preflightDetail.key_events.map((ev, i) => <li key={i}>{ev}</li>)}
                        </ol>
                      </div>
                    )}
                    {preflightDetail.characters?.length > 0 && (
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                        {preflightDetail.characters.map(c => (
                          <div key={c.name} className="p-2.5 rounded-lg bg-surface-2 border border-line">
                            <div className="flex items-center gap-1.5 mb-1">
                              <span className="text-sm font-medium text-ink-1">{c.name}</span>
                              {c.gender && <span className="text-xs text-ink-3">{c.gender}</span>}
                              {c.identity && <span className="text-xs text-ink-3">· {c.identity}</span>}
                            </div>
                            {c.personality && (
                              <p className="text-xs text-ink-2"><strong>{t('wb.preflightPersonality')}：</strong>{c.personality}</p>
                            )}
                            {c.emotions?.length > 0 && (
                              <p className="text-xs text-ink-2 mt-0.5">
                                <strong>{t('wb.preflightEmotion')}</strong>{' '}
                                {c.emotions.slice(0, 4).map(e => e.emotion).join(' / ')}
                              </p>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="text-xs text-ink-3">{t('wb.preflightNoData')}</div>
                )}
              </div>
            )}
            {/* 剧集列表 */}
            <div className="bg-surface rounded-lg border border-line">
              <div className="p-4 border-b border-line">
                <div className="flex items-center justify-between">
                  <h4 className="font-semibold text-ink-1">{t('wb.episodeList')}</h4>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => (splitPlanOpen ? setSplitPlanOpen(false) : fetchSplitPlan())}
                      title={t('wb.splitPlanHint')}
                      className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${FOCUS_RING} ${
                        splitPlanOpen
                          ? 'bg-brand text-white'
                          : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1 border border-line'
                      }`}
                    >
                      {splitPlanOpen ? t('wb.splitPlanCollapse') : t('wb.splitPlan')}
                    </button>
                    <button
                      onClick={() => setPreflightOpen(v => !v)}
                      title={t('wb.preflightHint')}
                      className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${FOCUS_RING} ${
                        preflightOpen
                          ? 'bg-success text-white'
                          : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1 border border-line'
                      }`}
                    >
                      {t('wb.preflight')}
                    </button>
                  </div>
                </div>
                <p className="text-xs text-ink-2 mt-1">{t('wb.clickEpisodeHint')}</p>
              </div>

              {/* P2-2 分集断点提议卡片（与生产口径同源的只读预览） */}
              {splitPlanOpen && (
                <div className="px-4 py-4 border-b border-line bg-surface-2/50">
                  {splitPlanLoading && (
                    <div className="text-sm text-ink-2 flex items-center gap-2">
                      <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
                      {t('wb.splitPlanLoading')}
                    </div>
                  )}
                  {!splitPlanLoading && splitPlanError && (
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-sm text-danger-strong">{splitPlanError}</span>
                      <button
                        onClick={fetchSplitPlan}
                        className={`px-2.5 py-1 rounded-md text-xs bg-surface-2 text-ink-2 hover:bg-line ${FOCUS_RING}`}
                      >
                        {t('wb.splitPlanRetry')}
                      </button>
                    </div>
                  )}
                  {!splitPlanLoading && !splitPlanError && splitPlan && splitPlan.chapters?.length > 0 && (
                    <div className="space-y-3">
                      {/* 汇总行 */}
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="font-medium text-ink-1">
                          {t('wb.splitPlanTotal', { ep: splitPlan.total_episodes ?? 0, ch: splitPlan.chapter_count ?? splitPlan.chapters.length })}
                        </span>
                        {(splitPlan.needs_confirm_chapters?.length ?? 0) > 0 && (
                          <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-warning-subtle text-warning-strong">
                            {t('wb.splitPlanNeedsConfirm', { n: splitPlan.needs_confirm_chapters.length })}
                          </span>
                        )}
                      </div>
                      {/* 逐章断点 */}
                      <div className="space-y-2">
                        {splitPlan.chapters.map((ch: any) => (
                          <div key={ch.index} className="bg-surface rounded-lg border border-line p-3">
                            <div className="flex flex-wrap items-center gap-2 mb-2">
                              <span className="text-sm font-semibold text-ink-1">
                                {t('wb.splitPlanChapter', { n: ch.index })}
                                {ch.title && <span className="ml-1.5 text-sm text-brand font-normal">{t('wb.splitPlanChapterTitle', { title: ch.title })}</span>}
                              </span>
                              <span className="text-xs text-ink-2">{t('wb.splitPlanParts', { n: ch.total_parts })}</span>
                              {ch.needs_confirm && (
                                <span className="px-1.5 py-0.5 rounded text-[11px] font-medium bg-warning-subtle text-warning-strong">
                                  {t('wb.splitPlanConfirmNote')}
                                </span>
                              )}
                            </div>
                            {ch.units?.length > 0 && (
                              <div className="space-y-1">
                                {ch.units.map((u: any) => (
                                  <div key={u.part} className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
                                    <span className="font-medium text-ink-1 w-24 shrink-0">
                                      {t('wb.splitPlanPart', { part: u.part, total: ch.total_parts })}
                                    </span>
                                    <span className="tabular-nums">{t('wb.splitPlanShots', { n: u.est_shots ?? '—' })}</span>
                                    <span className="tabular-nums">{t('wb.splitPlanSec', { sec: u.est_sec ?? '—' })}</span>
                                    {u.over_redline && (
                                      <span className="px-1.5 py-0.5 rounded text-[11px] font-medium bg-danger-subtle text-danger-strong">
                                        {t('wb.splitPlanOverRedline')}
                                      </span>
                                    )}
                                    {u.preview && (
                                      <span className="text-ink-3 truncate max-w-[14rem]">{u.preview}</span>
                                    )}
                                  </div>
                                ))}
                              </div>
                            )}
                            {ch.message && (
                              <p className="mt-1.5 text-xs text-ink-3">{ch.message}</p>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {!splitPlanLoading && !splitPlanError && splitPlan && (splitPlan.chapters?.length ?? 0) === 0 && (
                    <p className="text-sm text-ink-3">{t('wb.splitPlanNoChapters')}</p>
                  )}
                </div>
              )}

              {/* P2-15：补列表语义 —— 此前是一串裸 div/button，读屏不会播报
                  「列表，共 N 项」。这里刻意**不用** <table>：它是可点击的导航列表，
                  不是行列数据，套表格语义反而会误导读屏。 */}
              <ul className="divide-y divide-line">
                {episodes.map((ep: any) => (
                  <li key={ep.episode_no}>
                    <button
                      onClick={() => loadEpisodeDetail(ep.episode_no)}
                      className={`w-full p-4 hover:bg-surface-2 transition-colors text-left ${FOCUS_RING}`}
                    >
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          {/* 场次层级（按场次生成）：展开/收起「第1场/第2场/…」；行本身仍点开单集详情 */}
                          <button
                            onClick={(e) => { e.stopPropagation(); toggleScenes(ep.episode_no); }}
                            aria-expanded={scenesOpenEps.includes(ep.episode_no)}
                            title={t('wb.scenesToggle')}
                            className={`shrink-0 rounded-md p-1 text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink-1 ${FOCUS_RING}`}
                          >
                            <svg className={`w-4 h-4 transition-transform ${scenesOpenEps.includes(ep.episode_no) ? 'rotate-90' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                            </svg>
                          </button>
                          <span className="flex items-center justify-center w-8 h-8 rounded-full bg-brand-subtle text-brand text-sm font-semibold">
                            {ep.episode_no}
                          </span>
                          <div>
                            <div className="flex items-center gap-1.5">
                              <p className="font-medium text-ink-1">
                                {t('wb.episodeNo', { n: ep.episode_no })}
                                {ep.chapter_title && <span className="ml-2 text-sm text-brand">《{ep.chapter_title}》</span>}
                              </p>
                              {/* 前置解析状态：绿点=已完成（点击查看） / 灰点=可运行 / 转圈=运行中 */}
                              {preflightDone.includes(ep.episode_no) && (
                                <button
                                  onClick={e => { e.stopPropagation(); openPreflightDetail(ep.episode_no); }}
                                  title={t('wb.preflightView')}
                                  className="h-2.5 w-2.5 rounded-full bg-success border-0 p-0 cursor-pointer"
                                />
                              )}
                              {preflightRunning === ep.episode_no && (
                                <div className="w-3 h-3 border-2 border-brand border-t-transparent rounded-full animate-spin" />
                              )}
                              {!preflightDone.includes(ep.episode_no) && preflightRunning !== ep.episode_no && (
                                <button
                                  onClick={e => { e.stopPropagation(); runPreflight(ep.episode_no); }}
                                  title={t('wb.preflightRun')}
                                  className="h-2.5 w-2.5 rounded-full bg-line border border-ink-3 cursor-pointer hover:bg-brand transition-colors"
                                />
                              )}
                            </div>
                            <p className="text-xs text-ink-2 mt-0.5">
                              {t('wb.chapterNo', { n: ep.chapter_index ?? ep.episode_no })}
                            </p>
                          </div>
                        </div>

                        <div className="flex items-center gap-4">
                          {/* 文学剧本（人审层）：查看/生成本章文学剧本，确认后可改写为分镜剧本。
                              行本身是可点击的大按钮，这里必须 stopPropagation 防止误开详情 */}
                          <button
                            onClick={(e) => { e.stopPropagation(); openScreenplay(ep.episode_no); }}
                            disabled={spGenerating && spEpisode === ep.episode_no}
                            title={t('wb.screenplayRowHint')}
                            className={`inline-flex shrink-0 items-center gap-1 rounded-md border border-line bg-surface-2 px-2 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-line hover:text-ink-1 disabled:opacity-60 ${FOCUS_RING}`}
                          >
                            <FileText className="h-3.5 w-3.5" />
                            {t('wb.screenplay')}
                          </button>
                          <div className="text-right">
                            <div className="text-sm text-ink-2">
                              {t('wb.shotsRatio', { done: ep.completed_shots, total: ep.shot_count })}
                            </div>
                            {ep.created_at && (
                              <div className="text-xs text-ink-3">{ep.created_at.split('T')[0]}</div>
                            )}
                          </div>

                          <span className={`px-2 py-1 rounded-full text-xs font-medium inline-flex items-center gap-1 ${
                            ep.status === 'done' ? 'bg-success-subtle text-success-strong' :
                            ep.status === 'producing' ? 'bg-info-subtle text-info-strong' :
                            ep.status === 'failed' ? 'bg-danger-subtle text-danger-strong' :
                            'bg-surface-2 text-ink-1'
                          }`}>
                            {ep.status === 'done' ? (<><Check className="h-3.5 w-3.5" /> {t('wb.done')}</>) :
                             ep.status === 'producing' ? t('wb.producing') :
                             ep.status === 'failed' ? (<><X className="h-3.5 w-3.5" /> {t('episodes.failed')}</>) :
                             t('ep.pending')}
                          </span>

                          {/* 按场次生成：该集全部场次视频已拼接成整集成片（epNN_full.mp4） */}
                          {scenesByEp[ep.episode_no]?.full_video_ready && (
                            <span className="inline-flex items-center gap-1 rounded-full bg-success-subtle px-2 py-1 text-xs font-medium text-success-strong">
                              <Check className="h-3.5 w-3.5" /> {t('wb.fullVideoReady')}
                            </span>
                          )}

                          <svg className="w-5 h-5 text-ink-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                          </svg>
                        </div>
                      </div>

                      {ep.shot_count > 0 && (
                        <div className="mt-3 w-full bg-line rounded-full h-1.5">
                          <div
                            className={`h-1.5 rounded-full transition-colors ${
                              ep.status === 'done' ? 'bg-success' :
                              ep.status === 'failed' ? 'bg-danger' :
                              'bg-brand'
                            }`}
                            style={{ width: `${(ep.completed_shots / ep.shot_count) * 100}%` }}
                          ></div>
                        </div>
                      )}
                    </button>

                    {/* 场次子列表（按场次生成）：第1场/第2场/… + 分镜/视频状态 + 重做本场。
                        既有「点集名展开逐镜详情」不受影响 —— 本面板是新增的场次层级。 */}
                    {scenesOpenEps.includes(ep.episode_no) && (
                      <div className="border-t border-line bg-surface-2/50 px-4 py-3">
                        {scenesLoadingEp === ep.episode_no && !scenesByEp[ep.episode_no] ? (
                          <div className="flex items-center gap-2 py-1 text-sm text-ink-2">
                            <div className="h-4 w-4 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                            {t('wb.scenesLoading')}
                          </div>
                        ) : scenesErrByEp[ep.episode_no] ? (
                          <div className="flex items-center justify-between gap-3 py-1">
                            <span className="text-sm text-danger-strong">{scenesErrByEp[ep.episode_no]}</span>
                            <button
                              onClick={() => fetchScenes(ep.episode_no)}
                              className={`rounded-md bg-surface-2 px-2.5 py-1 text-xs text-ink-2 transition-colors hover:bg-line ${FOCUS_RING}`}
                            >
                              {t('wb.scenesRetry')}
                            </button>
                          </div>
                        ) : (scenesByEp[ep.episode_no]?.scenes?.length ?? 0) === 0 ? (
                          <p className="py-1 text-sm text-ink-3">{t('wb.scenesEmpty')}</p>
                        ) : (
                          <ul className="divide-y divide-line">
                            {(scenesByEp[ep.episode_no]?.scenes || []).map((sc) => {
                              const redoKey = `${ep.episode_no}:${sc.scene_no}`;
                              return (
                                <li key={sc.scene_no} className="flex flex-wrap items-center gap-2 py-2.5">
                                  <span className="shrink-0 text-sm font-medium text-ink-1">
                                    {t('wb.sceneNo', { n: sc.scene_no })}
                                  </span>
                                  {sc.heading && (
                                    <span className="max-w-[18rem] truncate text-xs text-ink-2" title={sc.heading}>{sc.heading}</span>
                                  )}
                                  {sc.location && (
                                    <span className="text-xs text-ink-3">{sc.location}</span>
                                  )}
                                  <span className="tabular-nums text-xs text-ink-2">{t('wb.sceneShotCount', { n: sc.shot_count })}</span>
                                  {/* 分镜状态：齐了亮绿「分镜完成」，否则显示「分镜 k/N」 */}
                                  {sc.shot_count > 0 && sc.storyboard_ok >= sc.shot_count ? (
                                    <span className="rounded bg-success-subtle px-1.5 py-0.5 text-[11px] font-medium text-success-strong">
                                      {t('wb.sceneStoryboardDone')}
                                    </span>
                                  ) : (
                                    <span className="rounded bg-warning-subtle px-1.5 py-0.5 text-[11px] font-medium text-warning-strong">
                                      {t('wb.sceneStoryboardPart', { done: sc.storyboard_ok, total: sc.shot_count })}
                                    </span>
                                  )}
                                  {/* 视频状态：该场 scene_XX.mp4 是否已生成 */}
                                  {sc.video_ready ? (
                                    <span className="rounded bg-success-subtle px-1.5 py-0.5 text-[11px] font-medium text-success-strong">
                                      ✓ {t('wb.sceneVideoReady')}
                                    </span>
                                  ) : (
                                    <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[11px] font-medium text-ink-3">
                                      {t('wb.sceneVideoMissing')}
                                    </span>
                                  )}
                                  <button
                                    onClick={() => redoScene(ep.episode_no, sc.scene_no)}
                                    disabled={redoBusy !== null}
                                    title={t('wb.sceneRedoHint')}
                                    className={`ml-auto inline-flex shrink-0 items-center gap-1 rounded-md border border-line bg-surface px-2 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-line hover:text-ink-1 disabled:opacity-60 ${FOCUS_RING}`}
                                  >
                                    {redoBusy === redoKey && (
                                      <span className="h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                                    )}
                                    {redoBusy === redoKey ? t('wb.sceneRedoBusy') : t('wb.sceneRedo')}
                                  </button>
                                </li>
                              );
                            })}
                          </ul>
                        )}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
      </div>

      {/* 文学剧本弹窗（人审层）：展示/生成本章文学剧本；生成完成后自动改写为分镜剧本
          （2026-10-04 起无需人工确认，弹窗底部为流程状态条而非确认按钮） */}
      <ScreenplayModal
        isOpen={spOpen}
        episodeNo={spEpisode}
        content={spContent}
        loading={spLoading}
        generating={spGenerating}
        rewriting={spRewriting}
        rewritten={spRewritten}
        error={spError}
        onClose={() => setSpOpen(false)}
      />
    </div>
  );
}

// ========== 文学剧本弹窗（人审层，两段式第一步） ==========
// 展示 / 生成本章文学剧本。正文为 Markdown 文本：项目无 markdown 渲染依赖
// （package.json 仅 react/react-dom/react-router），按约定用 whitespace-pre-wrap
// 的正文样式直接展示，不引入新依赖。
// 2026-10-04 起两段式第二步自动化：生成完成后由 OverviewTab 自动触发改写，
// 底部不再是「确认无误」按钮，而是流程状态条（生成中 → 自动改写中 → 已完成）；
// 失败在正文上方红色块展示。回看已有剧本不自动改写，底部只留「关闭」。
function ScreenplayModal({
  isOpen,
  episodeNo,
  content,
  loading,
  generating,
  rewriting,
  rewritten,
  error,
  onClose,
}: {
  isOpen: boolean;
  episodeNo: number | null;
  content: string;
  loading: boolean;
  generating: boolean;
  rewriting: boolean;
  rewritten: boolean;
  error: string;
  onClose: () => void;
}) {
  const { t } = useApp();
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      preventClose={rewriting}
      title={t('wb.screenplayModalTitle', { n: episodeNo ?? '' })}
      description={t('wb.screenplayModalDesc')}
      size="xl"
      footer={
        <div className="flex w-full flex-wrap items-center gap-2">
          {/* 流程状态条：正在生成文学剧本… → 正在自动改写为分镜剧本… → ✅ 已生成
              （三个状态互斥；失败走正文上方红色错误块，此处恢复只读「关闭」） */}
          {generating && (
            <span role="status" className="inline-flex items-center gap-1.5 text-sm text-ink-2">
              <span className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" />
              {t('wb.screenplayAutoGenerating')}
            </span>
          )}
          {rewriting && (
            <span role="status" className="inline-flex items-center gap-1.5 text-sm text-ink-2">
              <span className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" />
              {t('wb.screenplayAutoRewriting')}
            </span>
          )}
          {!generating && !rewriting && rewritten && (
            <span role="status" className="inline-flex items-center gap-1.5 text-sm font-medium text-success-strong">
              <CheckCircle2 className="h-4 w-4 shrink-0" />
              {t('wb.screenplayAutoDone')}
            </span>
          )}
          <Button variant="secondary" onClick={onClose} disabled={rewriting} className="ml-auto">
            {t('common.close')}
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        {error && (
          <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm break-words">
            {error}
          </div>
        )}
        {loading ? (
          <Loading size="md" label={t('common.loading')} />
        ) : generating ? (
          <Loading size="md" label={t('wb.screenplayGenerating')} />
        ) : content ? (
          <pre className="max-h-[60vh] overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-surface-2 p-4 text-sm leading-6 text-ink-1">
            {content}
          </pre>
        ) : (
          <p className="text-sm text-ink-2">{t('wb.screenplayNotGenerated')}</p>
        )}
      </div>
    </Modal>
  );
}

// ========== 文学剧本折叠面板（单集详情，人审层） ==========
// 默认收起、展开才拉取（与资产沉淀/服装变体面板同款 fail-open：接口异常降级为
// 一行提示，绝不打断详情主体）。novel_id 拿不到时整个面板不渲染，零报错。
// 下方既有「剧本内容」区块即改写后的结构化分镜剧本，两者关系在标题旁注明。
function ScreenplayPanel({ novelId, episodeNo }: { novelId?: string; episodeNo: number | null }) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  const [markdown, setMarkdown] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // 换集重置：上一集的剧本绝不能带进下一集
  useEffect(() => {
    setOpen(false);
    setMarkdown('');
    setError('');
  }, [episodeNo]);

  useEffect(() => {
    if (!open || !novelId || episodeNo == null) return;
    let alive = true;
    setLoading(true);
    setError('');
    screenplayApi.get(novelId, episodeNo)
      .then((d) => {
        if (!alive) return;
        if (d?.exists && d.markdown) setMarkdown(d.markdown);
        else setError(t('wb.screenplayNotGenerated'));
      })
      .catch(() => { if (alive) setError(t('wb.screenplayLoadFailed')); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // t 是 i18n 稳定引用；按 open/novelId/episodeNo 触发即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, novelId, episodeNo]);

  if (!novelId || episodeNo == null) return null;

  return (
    <div className="bg-surface rounded-lg border border-line p-6">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className={`flex w-full items-center justify-between gap-2 rounded text-left ${FOCUS_RING}`}
      >
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <FileText className="h-4 w-4 shrink-0 text-ink-2" />
          <span className="font-semibold text-ink-1">{t('wb.screenplay')}</span>
          <span className="text-xs font-normal text-ink-3">{t('wb.screenplayHumanTag')}</span>
        </span>
        <span className="shrink-0 text-xs text-ink-3">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="mt-3">
          {loading ? (
            <div className="text-sm text-ink-3">{t('common.loading')}</div>
          ) : error ? (
            <div className="text-sm text-ink-2">{error}</div>
          ) : (
            <pre className="max-h-[50vh] overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-surface-2 p-4 text-sm leading-6 text-ink-1">
              {markdown}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

// ========== Asset Card ==========
function AssetCard({
  item,
  type,
  onClick,
}: {
  item: AssetItem;
  type: 'character' | 'item' | 'scene';
  onClick?: () => void;
}) {
  const { t } = useApp();
  const _v = item.thumb || item.views?.[0];
  const imageUrl = assetSrc(_v?.url, _v?.mtime ?? _v?.size);
  const [broken, setBroken] = useState(false);
  const FallbackIcon = type === 'character' ? User : type === 'item' ? Box : Mountain;
  // 缩略图容器比例必须跟随资产实际画幅（后端 style_kit.ASSET_BASE_RATIO 写死）：
  // 角色三视图设定图与道具图是 1:1、场景原画是 16:9。此前一律 aspect-video + object-cover，
  // 一张 1:1（或更早的 3:4 竖幅）三视图放进 16:9 容器会被裁掉上下两边 ——
  // 人物头顶与脚底同时被切，看起来就是「三视图比例不对」。
  const thumbAspect = type === 'scene' ? 'aspect-video' : 'aspect-square';

  return (
    <button
      type="button"
      onClick={onClick}
      className={`text-left bg-surface rounded-xl border border-line overflow-hidden hover:shadow-lg transition-shadow ${FOCUS_RING}`}
    >
      <div className={`${thumbAspect} bg-surface-2 flex items-center justify-center overflow-hidden`}>
        {imageUrl && !broken ? (
          <img
            src={imageUrl}
            alt={item.name}
            loading="lazy"
            className="w-full h-full object-cover"
            onError={() => setBroken(true)}
          />
        ) : (
          <FallbackIcon className="h-9 w-9 text-ink-3" />
        )}
      </div>
      <div className="p-3">
        <h4 className="font-medium text-ink-1 text-sm truncate">{item.name}</h4>
        <p className="text-xs text-ink-2 mt-1">
          {item.category || (item.view_count ? t('wb.viewCount', { n: item.view_count }) : type === 'character' ? t('wb.characters') : type === 'item' ? t('wb.items') : t('wb.scenes'))}
        </p>
      </div>
    </button>
  );
}

// ========== Upload Character Sheet Modal（上传形象图 → 三视图） ==========
// 设计要点：上传的图**直接落 base.png** 再本地切分（零 GPU、零质检），所以
//   · 界面必须先把「期望版式」画清楚 —— 用户按版式出图才切得开；
//   · 切分失败不能报模糊错误，要把后端给的 layout_hint 原样透出来；
//   · 角色名给下拉（从项目剧本/已有资产取），避免手打错字落错目录。
function UploadSheetModal({
  isOpen,
  onClose,
  projectKey,
  characters,
  onUploaded,
}: {
  isOpen: boolean;
  onClose: () => void;
  projectKey: string;
  characters: string[];
  onUploaded?: () => void;
}) {
  const { t } = useApp();
  const toast = useToast();
  const [character, setCharacter] = useState('');
  const [customName, setCustomName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState('');
  const [busy, setBusy] = useState(false);
  const [overwrite, setOverwrite] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // 每次打开重置：上一次的文件/错误绝不能带进下一次（会误传给另一个角色）
  useEffect(() => {
    if (!isOpen) return;
    setFile(null);
    setPreview('');
    setBusy(false);
    setOverwrite(false);
    setCustomName('');
    setCharacter((prev) => (prev && characters.includes(prev) ? prev : characters[0] || ''));
  }, [isOpen, characters]);

  useEffect(() => {
    if (!file) {
      setPreview('');
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const finalName = (character === '__custom__' ? customName : character).trim();

  const submit = async () => {
    if (!finalName) {
      toast.error(t('uploadSheet.needCharacter'));
      return;
    }
    if (!file) {
      toast.error(t('uploadSheet.needFile'));
      return;
    }
    setBusy(true);
    try {
      const r = await characterSheetUpload.upload({
        project_name: projectKey,
        character: finalName,
        file,
        overwrite,
      });
      if (r.skipped) {
        toast.info(r.message || t('uploadSheet.skipped'));
      } else {
        // 2026-10-02 不裁剪：views 恒为空，改用后端返回的准确文案（否则会显示「0 张视角图」）
        toast.success(r.message || t('uploadSheet.ok', { n: Object.keys(r.views || {}).length }));
      }
      onUploaded?.();
      onClose();
    } catch (e) {
      // 后端把「版式不符」的可读原因 + layout_hint 都放在 error 里，直接展示
      toast.error(e instanceof Error ? e.message : t('uploadSheet.failed'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={t('uploadSheet.title')}>
      <div className="space-y-4">
        {/* 版式示意：这是本功能唯一的使用门槛，必须一眼看懂 */}
        <div className="rounded-lg border border-line bg-surface-2 p-3">
          <p className="text-xs font-medium text-ink-1 mb-2">{t('uploadSheet.layoutTitle')}</p>
          <div className="flex items-center gap-2">
            <div className="flex flex-col gap-1">
              <div className="flex gap-1">
                {['front', 'left', 'back'].map((k) => (
                  <div
                    key={k}
                    className="w-12 h-16 rounded border border-dashed border-ink-3 bg-surface flex items-center justify-center text-[10px] text-ink-3"
                  >
                    {t(`uploadSheet.view.${k}`)}
                  </div>
                ))}
              </div>
              <div className="w-12 h-16 rounded border border-dashed border-ink-3 bg-surface flex items-center justify-center text-[10px] text-ink-3">
                {t('uploadSheet.view.half')}
              </div>
            </div>
            <p className="text-xs text-ink-2 flex-1">{t('uploadSheet.layoutHint')}</p>
          </div>
        </div>

        {/* 角色选择 */}
        <div>
          <label className="block text-sm font-medium text-ink-1 mb-1">
            {t('uploadSheet.character')}
          </label>
          <Select
            value={character === '__custom__' || !characters.includes(character) ? '__custom__' : character}
            onChange={(v) => setCharacter(v)}
            options={[
              ...characters.map((c) => ({ value: c, label: c })),
              { value: '__custom__', label: t('uploadSheet.customName') },
            ]}
            className="w-full"
          />
          {(character === '__custom__' || characters.length === 0) && (
            <Input
              className="mt-2 w-full"
              value={customName}
              placeholder={t('uploadSheet.characterPlaceholder')}
              onChange={(v) => setCustomName(v)}
            />
          )}
        </div>

        {/* 文件选择 */}
        <div>
          <label className="block text-sm font-medium text-ink-1 mb-1">
            {t('uploadSheet.file')}
          </label>
          <input
            ref={fileRef}
            type="file"
            accept=".png,.jpg,.jpeg,.webp"
            className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
          />
          <div className="flex items-center gap-3">
            <Button variant="secondary" onClick={() => fileRef.current?.click()} disabled={busy}>
              {file ? t('uploadSheet.reselect') : t('uploadSheet.choose')}
            </Button>
            <span className="text-xs text-ink-2 truncate">
              {file ? `${file.name}（${(file.size / 1024).toFixed(0)} KB）` : t('uploadSheet.noFile')}
            </span>
          </div>
          {preview && (
            <div className="mt-2 rounded-lg border border-line overflow-hidden bg-surface-2">
              <img src={preview} alt="preview" className="w-full max-h-56 object-contain" />
            </div>
          )}
        </div>

        <label className="flex items-center gap-2 text-sm text-ink-2">
          <input
            type="checkbox"
            checked={overwrite}
            onChange={(e) => setOverwrite(e.target.checked)}
            className="rounded border-line"
          />
          {t('uploadSheet.overwrite')}
        </label>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} disabled={busy || !file || !finalName}>
            {busy ? t('uploadSheet.uploading') : t('uploadSheet.submit')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// ========== Asset Preview Modal ==========
// 薄封装：遮罩、头部、动画、ESC / 遮罩关闭、滚动锁定、焦点陷阱、层级全部由共享 Modal
// 负责（方案 P1-7）。此前这里是一份独立的自建弹层（bg-black/60 + p-4 头部 + z-modal），
// 与全站 Modal 的观感和层级都对不上。本组件只保留资产预览自己的业务：多视角切换 + 下载。
function AssetPreviewModal({
  preview,
  projectKey,
  onClose,
  onRegenerated,
}: {
  preview: { item: AssetItem; type: 'character' | 'item' | 'scene' } | null;
  projectKey: string;
  onClose: () => void;
  onRegenerated?: () => void;
}) {
  const { t } = useApp();
  const toast = useToast();
  const [active, setActive] = useState(0);
  const isOpen = !!preview;

  // —— 提示词：打开时拉取详情接口（/api/projects/<pid>/asset-detail），展示可编辑的
  //    生成提示词，支持「改提示词 → 单点重新生成」。此前详情接口后端早已返回
  //    meta.prompt_zh 与 regenerate 模板，但前端从未渲染，用户只能看整图无从改起。
  const [promptZh, setPromptZh] = useState('');
  const [promptEn, setPromptEn] = useState('');
  const [promptLoading, setPromptLoading] = useState(false);
  const [regenerating, setRegenerating] = useState(false);

  useEffect(() => {
    setActive(0);
    setPromptZh('');
    setPromptEn('');
    if (!preview) return;
    let alive = true;
    setPromptLoading(true);
    fetch(
      `/api/projects/${encodeURIComponent(projectKey)}/asset-detail` +
        `?kind=${encodeURIComponent(preview.type)}&name=${encodeURIComponent(preview.item.name)}`
    )
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        if (!alive) return;
        setPromptZh(d?.meta?.prompt_zh || '');
        setPromptEn(d?.meta?.prompt_en || '');
      })
      .catch(() => {
        if (alive) {
          setPromptZh('');
          setPromptEn('');
        }
      })
      .finally(() => { if (alive) setPromptLoading(false); });
    return () => { alive = false; };
  }, [preview, projectKey]);

  const gallery = React.useMemo(() => {
    if (!preview) return [] as { url: string; view?: string; size?: number; mtime?: number }[];
    return [
      preview.item.thumb,
      ...(preview.item.views || []),
    ].filter(Boolean) as { url: string; view?: string; size?: number; mtime?: number }[];
  }, [preview]);

  // ← / → 在多个视角之间切换（图片浏览器的最低预期）
  useEffect(() => {
    if (!isOpen || gallery.length <= 1) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight') setActive((i) => (i + 1) % gallery.length);
      else if (e.key === 'ArrowLeft') setActive((i) => (i - 1 + gallery.length) % gallery.length);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, gallery.length]);

  // 共享 Modal 已处理 isOpen=false 时不渲染，这里只需容忍 preview 为空时的取值
  const item = preview?.item;
  const current = gallery[active];
  const src = assetSrc(current?.url, current?.mtime ?? current?.size);

  const downloadCurrent = () => {
    if (!src) return;
    const a = document.createElement('a');
    a.href = src;
    a.download = `${item?.name || 'asset'}${current?.view ? '_' + current.view : ''}.png`;
    a.click();
  };

  // 单点重新生成：用当前编辑后的提示词覆盖重新出图（overwrite=true）。
  const regenerate = async () => {
    if (!preview || regenerating) return;
    const zh = promptZh.trim();
    if (!zh) {
      toast.warning(t('wb.assetPromptRequired'));
      return;
    }
    setRegenerating(true);
    try {
      const res = await fetch('/api/assets/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          asset_type: preview.type,
          project_name: projectKey,
          overwrite: true,
          assets: [{
            name: item?.name,
            reference_prompt_zh: zh,
            reference_prompt_en: promptEn.trim(),
          }],
        }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || !d.task_id) {
        toast.error(d?.error || t('wb.assetRegenerateFailed'));
      } else {
        toast.success(t('wb.assetRegenerateStarted'));
        onRegenerated?.();
      }
    } catch {
      toast.error(t('wb.assetRegenerateFailed'));
    } finally {
      setRegenerating(false);
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={item?.name || t('wb.assetPreview')}
      size="xl"
      footer={
        <div className="flex w-full items-center gap-2">
          <Button size="sm" variant="secondary" onClick={downloadCurrent}>
            {t('wb.downloadCurrent')}
          </Button>
          <Button
            size="sm"
            variant="brand"
            onClick={regenerate}
            disabled={regenerating || promptLoading || !promptZh.trim()}
          >
            {regenerating ? t('wb.assetRegenerating') : t('wb.assetRegenerate')}
          </Button>
          {current?.size && (
            <span className="text-xs text-ink-3">{(current.size / 1024).toFixed(0)} KB</span>
          )}
          {gallery.length > 1 && (
            <span className="ml-auto text-xs text-ink-3">{t('wb.switchView')}</span>
          )}
        </div>
      }
    >
        <div className="space-y-4">
          {src ? (
            <img
              src={src}
              alt={`${item?.name || ''}${current?.view ? ` - ${current.view}` : ''}`}
              className="w-full rounded-md bg-surface-2"
            />
          ) : (
            <EmptyState icon={<ImageIcon className="h-10 w-10" />} title={t('wb.imageUnavailable')} />
          )}

          {gallery.length > 1 && (
            <div className="flex flex-wrap gap-2" role="tablist" aria-label={t('wb.viewSwitch')}>
              {gallery.map((g, i) => {
                const thumb = assetSrc(g.url, g.mtime ?? g.size);
                return (
                  <button
                    key={i}
                    type="button"
                    role="tab"
                    aria-selected={i === active}
                    aria-label={g.view || t('wb.viewN', { n: i + 1 })}
                    onClick={() => setActive(i)}
                    className={`h-14 w-20 overflow-hidden rounded border-2 ${FOCUS_RING} ${
                      i === active ? 'border-brand' : 'border-transparent hover:border-line-strong'
                    }`}
                  >
                    {thumb && <img src={thumb} alt="" className="h-full w-full object-cover" />}
                  </button>
                );
              })}
            </div>
          )}

          {/* 生成提示词：可编辑 + 单点重新生成 */}
          <div className="border border-line rounded-lg p-3 space-y-2">
            <h4 className="text-sm font-semibold text-ink-1">{t('wb.assetPrompt')}</h4>
            {promptLoading ? (
              <div className="text-xs text-ink-3">{t('common.loading')}</div>
            ) : (
              <>
                <textarea
                  value={promptZh}
                  onChange={(e) => setPromptZh(e.target.value)}
                  rows={4}
                  placeholder={t('wb.assetPromptPlaceholder')}
                  className={`w-full rounded-md border border-line bg-surface px-3 py-2 text-sm text-ink-1 resize-y ${FOCUS_RING}`}
                />
                <p className="text-[11px] text-ink-3">{t('wb.assetPromptHint')}</p>
              </>
            )}
          </div>

          {/* 资产沉淀过程（时间线）：抽取 → 提示词 → 出图 → 质检 → 教训 → 切分 → 入库 */}
          {item?.name && (
            <AssetPrecipitationSection
              projectKey={projectKey}
              kind={preview?.type || 'character'}
              name={item.name}
            />
          )}

          {/* 服装变体（衣柜）：仅角色资产显示 —— 列表 + 新增入口 */}
          {preview?.type === 'character' && item?.name && (
            <CharacterOutfitsSection projectKey={projectKey} character={item.name} />
          )}
        </div>
    </Modal>
  );
}

// ========== 资产沉淀过程（时间线，2026-10-06） ==========
// 需求：资产不是「一下就有的」，用户在界面上只能看到「最后那张图」，看不到
// 「它被改了几次、为什么改、学到了什么」。后端 /api/projects/<pid>/asset-precipitation
// 把散落在质检历史（QC_DIR）、产物旁路元数据（*.meta.json）、教训库
// （output/lessons/lessons.jsonl）三处的痕迹按资产聚合成 7 步时间线。
//
// 与 CharacterOutfitsSection 同款 fail-open：默认折叠、点开才拉取；接口异常降级成
// 「无法读取」一行字，绝不打断预览主体（后端零回归约束对齐）。
function AssetPrecipitationSection({
  projectKey,
  kind,
  name,
}: {
  projectKey: string;
  kind: string;
  name: string;
}) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<AssetPrecipitationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    setData(null);
    setError(false);
    if (!open || !projectKey || !name) return;
    let alive = true;
    setLoading(true);
    assetPrecipitation
      .get(projectKey, kind, name)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setError(true); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [open, projectKey, kind, name]);

  // 状态 → 圆点样式 + 文案。skipped/pending 视觉上明显弱于 done/failed，
  // 因为它们是「本来就没有这一步」而非「出错了」，不该让用户误以为有问题。
  const dotOf = (status: PrecipitationStatus) => {
    switch (status) {
      case 'done':    return 'bg-success-strong border-success-strong';
      case 'failed':  return 'bg-danger-strong border-danger-strong';
      case 'pending': return 'bg-surface border-line-strong';
      default:        return 'bg-surface-2 border-line';
    }
  };
  const badgeOf = (status: PrecipitationStatus) => {
    switch (status) {
      case 'done':    return 'bg-success-subtle text-success-strong';
      case 'failed':  return 'bg-danger-subtle text-danger-strong';
      default:        return 'bg-surface-2 text-ink-3';
    }
  };
  const statusText = (status: PrecipitationStatus) =>
    t(`precip.status.${status}`);

  const summary = data?.summary;

  return (
    <div className="border border-line rounded-lg p-3 space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`flex w-full items-center justify-between rounded text-sm font-semibold text-ink-1 hover:text-brand transition-colors ${FOCUS_RING}`}
      >
        <span className="flex items-center gap-2">
          <span>{t('precip.title')}</span>
          {summary && (
            <span className="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] font-normal text-ink-3">
              {t('precip.summary.done', { done: summary.done, total: summary.total_steps })}
            </span>
          )}
        </span>
        <span className="text-xs text-ink-3">{open ? '−' : '+'}</span>
      </button>

      {open && (
        <div className="space-y-3">
          {loading ? (
            <div className="text-xs text-ink-3">{t('common.loading')}</div>
          ) : error || !data ? (
            <div className="text-xs text-ink-3">{t('precip.loadFailed')}</div>
          ) : (
            <>
              {/* 概览：质检次数 / 命中教训 / 视角数 / 是否用户上传 */}
              <div className="flex flex-wrap gap-1.5">
                {summary?.is_user_upload && (
                  <span className="rounded-full bg-brand-subtle px-2 py-0.5 text-[11px] font-medium text-brand-strong">
                    {t('precip.badge.userUpload')}
                  </span>
                )}
                <span className="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-ink-2">
                  {t('precip.stat.qc', { n: summary?.qc_attempts ?? 0 })}
                </span>
                <span className="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-ink-2">
                  {t('precip.stat.lessons', { n: summary?.lessons ?? 0 })}
                </span>
                {!!summary?.views?.length && (
                  <span className="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-ink-2">
                    {t('precip.stat.views', { n: summary.views.length })}
                  </span>
                )}
              </div>

              {/* 步骤时间线 */}
              <ol className="space-y-0">
                {data.steps.map((s, i) => (
                  <li key={s.id} className="flex gap-2.5">
                    {/* 竖线 + 圆点 */}
                    <span className="flex flex-col items-center pt-1.5">
                      <span className={`h-2.5 w-2.5 shrink-0 rounded-full border-2 ${dotOf(s.status)}`} />
                      {i < data.steps.length - 1 && <span className="w-px flex-1 bg-line" />}
                    </span>
                    <div className="min-w-0 flex-1 pb-3">
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-ink-1">{s.label}</span>
                        <span
                          className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${badgeOf(s.status)}`}
                        >
                          {statusText(s.status)}
                        </span>
                      </div>
                      {s.detail && (
                        <p className="mt-0.5 text-xs text-ink-3 break-words">{s.detail}</p>
                      )}

                      {/* 质检逐次尝试：哪一次、多少分、因为什么被打回 */}
                      {s.id === 'qc' && s.items.length > 0 && (
                        <ul className="mt-1.5 space-y-1">
                          {s.items.map((it, j) => (
                            <li
                              key={j}
                              className="rounded-md border border-line bg-surface-2 px-2 py-1.5 text-[11px]"
                            >
                              <div className="flex items-center gap-2">
                                <span className="text-ink-2">
                                  {t('precip.qc.attempt', { n: it.attempt ?? j + 1 })}
                                </span>
                                {it.score != null && (
                                  <span className="text-ink-3">
                                    {t('precip.qc.score', { score: it.score })}
                                  </span>
                                )}
                                <span className={it.passed ? 'text-success-strong' : 'text-danger-strong'}>
                                  {it.passed ? t('precip.qc.passed') : t('precip.qc.failed')}
                                </span>
                                {it.seed != null && (
                                  <span className="ml-auto text-ink-3">seed {it.seed}</span>
                                )}
                              </div>
                              {it.reason && (
                                <p className="mt-0.5 text-ink-3 break-words">{it.reason}</p>
                              )}
                              {!!it.issues?.length && (
                                <ul className="mt-0.5 list-disc pl-4 text-ink-3">
                                  {it.issues.map((x, k) => (
                                    <li key={k} className="break-words">{x}</li>
                                  ))}
                                </ul>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}

                      {/* 教训：下次重画时会被召回用于改写提示词 */}
                      {s.id === 'lesson' && s.items.length > 0 && (
                        <ul className="mt-1.5 space-y-1">
                          {s.items.map((it, j) => (
                            <li
                              key={j}
                              className="rounded-md border border-line bg-surface-2 px-2 py-1.5 text-[11px]"
                            >
                              <div className="flex items-start gap-2">
                                <span className="min-w-0 flex-1 text-ink-2 break-words">{it.issue}</span>
                                {it.category && (
                                  <span className="shrink-0 rounded-full bg-surface px-1.5 py-0.5 text-[10px] text-ink-3">
                                    {it.category}
                                  </span>
                                )}
                                {it.priority && (
                                  <span className="shrink-0 rounded-full bg-surface px-1.5 py-0.5 text-[10px] text-ink-3">
                                    {it.priority}
                                  </span>
                                )}
                              </div>
                              {it.project && (
                                <p className="mt-0.5 text-ink-3">
                                  {t('precip.lesson.from', { project: it.project })}
                                </p>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}

                      {/* 通用键值条目（抽取字段 / 提示词 / seed / 视角清单 / 目录） */}
                      {s.id !== 'qc' && s.id !== 'lesson' && s.items.filter((x) => x.key).length > 0 && (
                        <dl className="mt-1 space-y-0.5">
                          {s.items.filter((x) => x.key).map((it, j) => (
                            <div key={j} className="flex gap-2 text-[11px]">
                              <dt className="shrink-0 text-ink-3">{it.key}</dt>
                              <dd className="min-w-0 flex-1 text-ink-2 break-words whitespace-pre-wrap">
                                {it.value}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ========== 服装变体（衣柜，2026-10-02） ==========
// 角色资产预览弹层里的小入口：展开显示已生成的服装变体列表
// （GET /api/assets/character/outfits），并提供「+ 新增服装变体」表单
// （服装名 + 服装描述 → POST /api/assets/character/outfit，后端复用资产生成
// 全链路，异步进度可看任务列表）。仅角色类型显示；任何接口异常都降级为空列表 /
// toast 提示，绝不打断预览主体（fail-open，与后端零回归约束对齐）。
function CharacterOutfitsSection({ projectKey, character }: { projectKey: string; character: string }) {
  const { t } = useApp();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [outfits, setOutfits] = useState<CharacterOutfit[]>([]);
  const [loading, setLoading] = useState(false);
  const [outfitKey, setOutfitKey] = useState('');
  const [outfitDesc, setOutfitDesc] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const d = await characterOutfits.list(projectKey, character);
      setOutfits(Array.isArray(d?.outfits) ? d.outfits : []);
    } catch {
      setOutfits([]);   // 目录不存在 / 读取失败 → 空列表，不打断预览
    } finally {
      setLoading(false);
    }
  }, [projectKey, character]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const submit = async () => {
    if (submitting) return;
    if (!outfitKey.trim() || !outfitDesc.trim()) {
      toast.warning(t('assets.outfit.required'));
      return;
    }
    setSubmitting(true);
    try {
      const d = await characterOutfits.generate({
        project_name: projectKey,
        character,
        outfit_key: outfitKey.trim(),
        outfit_desc: outfitDesc.trim(),
      });
      if (d?.skipped) {
        toast.info(d.message || t('assets.outfit.skipped'));
      } else {
        toast.success(t('assets.outfit.started'));
      }
      setOutfitKey('');
      setOutfitDesc('');
      setOpen(true);
      void load();
    } catch (err: any) {
      toast.error(err?.message || t('assets.outfit.failed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="border border-line rounded-lg p-3 space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`flex w-full items-center justify-between rounded text-sm font-semibold text-ink-1 hover:text-brand transition-colors ${FOCUS_RING}`}
      >
        <span>{t('assets.outfit.title')}</span>
        <span className="text-xs text-ink-3">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="space-y-2">
          {loading ? (
            <div className="text-xs text-ink-3">{t('common.loading')}</div>
          ) : outfits.length === 0 ? (
            <div className="text-xs text-ink-3">{t('assets.outfit.empty')}</div>
          ) : (
            <ul className="space-y-1.5">
              {outfits.map((o) => (
                <li key={o.outfit_key}
                    className="flex items-center justify-between gap-2 rounded-md border border-line bg-surface-2 px-2.5 py-1.5">
                  <span className="min-w-0">
                    <span className="block truncate text-sm text-ink-1">{o.outfit_key}</span>
                    {o.desc && (
                      <span className="block truncate text-xs text-ink-3">{o.desc}</span>
                    )}
                  </span>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      o.ready ? 'bg-success-subtle text-success-strong' : 'bg-surface-2 text-ink-3'
                    }`}
                  >
                    {o.ready ? t('assets.outfit.ready') : t('assets.outfit.generating')}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {/* 新增服装变体：服装名 + 服装描述（⚠️ ui.Input 的 onChange 直接传 string 值） */}
          <div className="space-y-1.5 pt-1">
            <Input
              value={outfitKey}
              onChange={(v) => setOutfitKey(v)}
              placeholder={t('assets.outfit.keyPlaceholder')}
            />
            <Input
              value={outfitDesc}
              onChange={(v) => setOutfitDesc(v)}
              placeholder={t('assets.outfit.descPlaceholder')}
            />
            <Button size="sm" variant="secondary" onClick={submit} loading={submitting}>
              {t('assets.outfit.add')}
            </Button>
          </div>
          <p className="text-[11px] text-ink-3">{t('assets.outfit.hint')}</p>
        </div>
      )}
    </div>
  );
}

export { OverviewTab };

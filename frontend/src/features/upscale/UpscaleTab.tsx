// ===== upscale 域 =====
// 超分域：链路自检、候选输入、任务提交与进度、产物列表。
//
// 本文件由机械切片从 `pages/ProjectWorkbenchPage.tsx` 搬出（ADR-0010 配套的
// 按域拆分）。代码逐字搬运：报错文案、提示语、空态措辞一律未改（R2）。

import React, { useState, useEffect, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { t } from '@/i18n';
import { autopilotApi, upscaleApi } from '@/api/client';
import { Button, EmptyState, Skeleton } from '@/components/ui';
import { Check, X, ZoomIn } from '@/components/ui/icons';
import { useUpscaleTask } from '@/api/queries';
import { useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/api/queries/keys';
import type { UpscaleEnv, UpscaleSource, UpscaleTask, UpscaleArtifact } from '@/types';

// 焦点环：与 components/ui/index.tsx 里的 FOCUS_RING 逐字一致。
// index.css 有全局 :focus-visible outline 兜底，这里显式加 focus:outline-none 把它压掉，
// 否则 outline + ring 会叠成双环。凡因形状/类型原因换不成共享组件的原生控件，统一补这一串。
const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';


// ========== 超分（FlashVSR） ==========
// 接口：
// GET /api/upscale/env 链路自检（ComfyUI 在线 / 模型 / 节点）
// GET /api/upscale/sources 候选输入视频（成片 / 片段 / 已有超分 / ComfyUI）
// POST /api/upscale/video {project_name, video_path, scale, attach_audio} -> task_id
// GET /api/upscale/status/<id> 轮询进度
// GET /api/upscale/list 该项目已生成的超分产物
//
// ⚠️ attach_audio 必须传 true：后端 TE-Speed 链路默认 attach_audio=False，
// 对「成片」超分会把已合成的 TTS 配音整轨丢掉（backend 侧该参数此前也不在白名单，
// 已一并补上）。
//
// ⚠️ 可下载性取决于 URL 前缀：只有 /api/upscale/<project>/<name> 支持 ?download=1；
// ComfyUI 侧来源走 /api/upscale/comfyview 是 302 重定向，不能直接当附件下载。
function UpscaleTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const queryClient = useQueryClient();
  const [env, setEnv] = useState<UpscaleEnv | null>(null);
  const [sources, setSources] = useState<UpscaleSource[]>([]);
  const [artifacts, setArtifacts] = useState<UpscaleArtifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const [scale, setScale] = useState<2 | 3 | 4>(2);
  // task 状态已随 ADR-0010 收敛为 query（见下方 taskQ），原 useState 随之删除
  const [submitting, setSubmitting] = useState(false);
  const [playing, setPlaying] = useState<string | null>(null);
  /**
   * 是否把 ComfyUI 侧素材也列进候选。
   *
   * 后端 /api/upscale/sources 会把 **全局** COMFYUI_OUTPUT_DIR 下所有 mp4 都返回
   * （不分项目，实测该项目能列出 300 条），混进下拉框会把本项目的成片淹没，
   * 原生 select 里 300+ 选项也几乎没法选。故默认只显示项目自己的产物。
   */
  const [showComfy, setShowComfy] = useState(false);
  /** 自动生产是否带超分（项目级计划，存于 autopilot plan） */
  const [planOn, setPlanOn] = useState<boolean | null>(null);
  const [planScale, setPlanScale] = useState<2 | 3 | 4>(2);
  const [savingPlan, setSavingPlan] = useState(false);
  /** ⚠️ ADR-0010：原先的 `pollRef` / `stopPoll` 已删除 ——
   *  「有没有任务在跑」改由 taskId 表达（见下方 taskQ）。 */
  const [taskId, setTaskId] = useState<string | null>(null);

  const loadArtifacts = React.useCallback(async () => {
    try {
      const d = await upscaleApi.list(projectKey);
      setArtifacts(d.items || []);
    } catch {
      setArtifacts([]);
    }
  }, [projectKey]);

  const load = React.useCallback(async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      // env 失败不视为致命：把原因展示出来比整页报错更有用
      const [envRes, srcRes, planRes] = await Promise.allSettled([
        upscaleApi.env(),
        upscaleApi.sources(projectKey),
        autopilotApi.plan(projectKey),
      ]);
      if (envRes.status === 'fulfilled') setEnv(envRes.value);
      else setEnv(null);
      if (planRes.status === 'fulfilled') {
        const pl = (planRes.value?.plan || {}) as Record<string, unknown>;
        setPlanOn(pl.enable_upscale !== false);
        const s = Number(pl.upscale_scale);
        setPlanScale(s === 3 || s === 4 ? (s as 3 | 4) : 2);
      } else {
        setPlanOn(null);
      }
      if (srcRes.status === 'fulfilled') {
        const items = srcRes.value.items || [];
        setSources(items);
        // 默认优先选「成片」（自动生产刚出的成品），省掉一次手动选择
        setSelected((prev) => prev || (items.find((i) => i.kind === '成片') || items[0])?.path || '');
      } else {
        setError(srcRes.reason instanceof Error ? srcRes.reason.message : t('upscale.loadFailed'));
        setSources([]);
      }
      await loadArtifacts();
    } finally {
      setLoading(false);
    }
  }, [projectKey, loadArtifacts, t]);

  useEffect(() => { void load(); }, [load]);

  // ⚠️ ADR-0010：原先这里是 `pollRef` + `stopPoll()` + 一个卸载时的
  //    `useEffect(() => () => stopPoll(), [stopPoll])`，约 20 行手写轮询。
  //    现在「有没有任务在跑」用 taskId 表达，终态由 refetchInterval 判定自动停机。
  const taskQ = useUpscaleTask(taskId);
  const task = (taskQ.data as UpscaleTask | null) ?? null;

  // 任务到达终态：done 时刷新产物列表（原实现在 poll 的 tick 里做）
  const finishedRef = React.useRef(false);
  useEffect(() => {
    if (!task) return;
    if (task.status === 'done' && !finishedRef.current) {
      finishedRef.current = true;
      void loadArtifacts();
    }
  }, [task, loadArtifacts]);

  // 查询失败：与原实现同一口径（停轮询 + 展示错误文案）
  React.useEffect(() => {
    if (taskQ.error) {
      setError(taskQ.error instanceof Error ? taskQ.error.message : t('upscale.statusFailed'));
    }
  }, [taskQ.error, t]);

  /** 开始轮询某个任务；done/error 时自动停机并刷新产物列表 */
  const startPoll = React.useCallback((taskId: string) => {
    finishedRef.current = false;
    setError('');
    setTaskId(taskId);
  }, []);

  const submit = async () => {
    if (!selected) return;
    setSubmitting(true);
    setError('');
    setPlaying(null);
    try {
      const res = await upscaleApi.submit({
        project_name: projectKey,
        video_path: selected,
        scale,
        // 成片含配音，必须保留音轨
        attach_audio: true,
      });
      // 立刻显示「已提交」：原实现是 setTask({status:'pending'})，按钮当场翻转成
      // 「生成中」，不必等第一次轮询回来。现在改为**给 query 缓存播种**同一条记录，
      // 这样「立即反馈」与「之后由轮询更新」共用同一份数据，不会出现两个真相。
      queryClient.setQueryData(queryKeys.upscaleTask(res.task_id), {
        task_id: res.task_id, status: 'pending', progress: 0, message: '',
      });
      startPoll(res.task_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('upscale.submitFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  /** 保存「自动生产时是否带超分」到项目计划（后端只认 PLAN_DEFAULTS 里的字段） */
  const savePlan = async (patch: { enable_upscale?: boolean; upscale_scale?: 2 | 3 | 4 }) => {
    setSavingPlan(true);
    setError('');
    try {
      const res = await autopilotApi.setPlan(projectKey, patch);
      const pl = (res?.plan || {}) as Record<string, unknown>;
      setPlanOn(pl.enable_upscale !== false);
      const s = Number(pl.upscale_scale);
      setPlanScale(s === 3 || s === 4 ? (s as 3 | 4) : 2);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('upscale.planSaveFailed'));
    } finally {
      setSavingPlan(false);
    }
  };

  if (loading) return (
    // 骨架对齐真实区块：标题行 → 链路自检卡 → 计划卡 → 源选择卡
    <div className="space-y-4" role="status" aria-live="polite" aria-label={t('common.loading')}>
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 space-y-2">
          <Skeleton className="h-5 w-28" />
          <Skeleton className="h-4 w-56" />
        </div>
        <Skeleton className="h-8 w-20" />
      </div>
      <Skeleton className="h-32 rounded-lg" />
      <Skeleton className="h-32 rounded-lg" />
      <Skeleton className="h-44 rounded-lg" />
    </div>
  );

  const ready = !!env?.available;
  const busy = task?.status === 'pending' || task?.status === 'running';
  /** 本项目自身产物（成片 / 片段 / 已有超分）；ComfyUI 侧是全局素材池，默认折叠 */
  const visibleSources = showComfy
    ? sources
    : sources.filter((s) => !s.kind.startsWith('ComfyUI'));
  const source = sources.find((s) => s.path === selected);
  const srcUrl = source?.url || '';
  // 仅 /api/upscale/<project>/<name> 支持 ?download=1（comfyview 是 302，不能当附件）
  const downloadUrl = (u?: string) =>
    u && u.startsWith('/api/upscale/') && !u.includes('comfyview') ? `${u}?download=1` : '';

  const statusText = () => {
    if (!task) return '';
    if (task.status === 'pending') return t('upscale.queued');
    if (task.status === 'running') return t('upscale.running');
    if (task.status === 'done') return t('upscale.done');
    return t('upscale.failed');
  };

  const res = task?.result;
  const fmtResolution = (v?: { width?: number; height?: number }) =>
    v?.width && v?.height ? `${v.width}×${v.height}` : '—';

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-lg font-semibold text-ink-1">{t('upscale.title')}</h3>
          <p className="text-sm text-ink-2 mt-0.5">{t('upscale.subtitle')}</p>
        </div>
        <Button size="sm" variant="secondary" onClick={load} disabled={loading || busy}>
          {t('common.refresh')}
        </Button>
      </div>

      {/* 链路自检 */}
      <div
        className={`p-3 rounded-lg border text-sm ${
          ready
            ? 'bg-success-subtle border-success/30 text-success-strong'
            : 'bg-warning-subtle border-warning/30 text-warning-strong'
        }`}
      >
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium">
            {ready ? t('upscale.envOk') : t('upscale.envBad')}
          </span>
          <span className="text-xs">
            {t('upscale.engine')}:{' '}
            {env?.default_engine === 'legacy-flashvsr'
              ? t('upscale.engineLegacy')
              : t('upscale.engineTe')}
          </span>
        </div>
        {env && (
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
            <span className="inline-flex items-center gap-1">{t('upscale.comfyOnline')}: {env.comfy_online ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
            <span className="inline-flex items-center gap-1">{t('upscale.modelReady')}: {env.model_ready ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
            <span className="inline-flex items-center gap-1">{t('upscale.teReady')}: {env.te_ready ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
            <span className="inline-flex items-center gap-1">{t('upscale.legacyReady')}: {env.legacy_ready ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
          </div>
        )}
        {!ready && (env?.reasons?.length ?? 0) > 0 && (
          <div className="mt-2">
            <p className="text-xs opacity-90">{t('upscale.envHint')}</p>
            <ul className="mt-1 list-disc list-inside text-xs opacity-90 space-y-0.5">
              {(env?.reasons || []).map((r) => <li key={r}>{r}</li>)}
            </ul>
          </div>
        )}
        {/* 说明流水线默认开启超分且失败即跳过，避免用户以为「没超分 = 坏了」 */}
        <p className="mt-2 text-xs opacity-80">{t('upscale.pipelineTip')}</p>
      </div>

      {/* 自动生产是否带超分 —— 超分默认开启，且单集耗时会明显变长，
          必须给一个真正的关闭入口（之前 enable_upscale 不在 PLAN_DEFAULTS 里，
          接口会把该字段过滤掉，等于关不掉）。 */}
      {planOn !== null && (
        <div className="bg-surface rounded-lg border border-line p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="text-sm font-medium text-ink-1">
                {t('upscale.planToggle')}
              </p>
              <p className="text-xs text-ink-2 mt-1">
                {t('upscale.planToggleHint')}
              </p>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <span className={`text-xs font-medium ${planOn ? 'text-success-strong' : 'text-ink-2'}`}>
                {planOn ? t('upscale.on') : t('upscale.off')}
              </span>
              <button
                role="switch"
                aria-checked={planOn}
                disabled={savingPlan}
                onClick={() => savePlan({ enable_upscale: !planOn })}
                className={`relative w-11 h-6 rounded-full transition-colors disabled:opacity-50 ${FOCUS_RING} ${
                  planOn ? 'bg-brand' : 'bg-line-strong'
                }`}
              >
                <span
                  className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-surface shadow transition-transform ${
                    planOn ? 'translate-x-5' : ''
                  }`}
                />
              </button>
            </div>
          </div>

          {/* 自动生产的超分倍率（与手工超分独立配置） */}
          <div className="mt-3 pt-3 border-t border-line flex items-center gap-3">
            <span className="text-xs text-ink-2">{t('upscale.planScale')}</span>
            <div className="flex gap-2">
              {([2, 3, 4] as const).map((n) => (
                <button
                  key={n}
                  disabled={savingPlan || !planOn}
                  onClick={() => savePlan({ upscale_scale: n })}
                  className={`px-3 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-50 ${FOCUS_RING} ${
                    planScale === n
                      ? 'bg-brand text-white'
                      : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1'
                  }`}
                >
                  {t('upscale.scaleTimes', { n })}
                </button>
              ))}
            </div>
            <span className="text-xs text-ink-3">{t('upscale.planScaleHint')}</span>
          </div>
        </div>
      )}

      {error && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-sm text-danger-strong">
          {error}
        </div>
      )}

      {/* 选择源 + 倍率 + 发起 */}
      <div className="bg-surface rounded-lg border border-line p-4 space-y-4">
        {sources.length === 0 ? (
          <EmptyState
            icon={<ZoomIn className="h-10 w-10" />}
            title={t('upscale.sourceEmpty')}
            description={t('upscale.sourceEmptyTip')}
          />
        ) : (
          <>
            <div className="grid md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-ink-2 mb-1.5">
                  {t('upscale.selectSource')}
                </label>
                <select
                  value={selected}
                  onChange={(e) => { setSelected(e.target.value); setPlaying(null); }}
                  disabled={busy}
                  className={`w-full px-3 py-2 text-sm border border-line rounded-lg bg-surface text-ink-1 ${FOCUS_RING}`}
                >
                  {Array.from(new Set(visibleSources.map((s) => s.kind))).map((kind) => (
                    <optgroup key={kind} label={kind}>
                      {visibleSources.filter((s) => s.kind === kind).map((s) => (
                        <option key={s.path} value={s.path}>
                          {s.name} · {s.size_mb} MB
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
                {/* ComfyUI 侧是全局素材池（不分项目），默认折叠避免淹没本项目成片 */}
                <label className="mt-2 flex items-center gap-2 text-xs text-ink-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={showComfy}
                    onChange={(e) => setShowComfy(e.target.checked)}
                    className={`rounded border-line accent-brand ${FOCUS_RING}`}
                  />
                  {t('upscale.showComfy', {
                    n: sources.length - sources.filter((s) => !s.kind.startsWith('ComfyUI')).length,
                  })}
                </label>
                {source && (
                  <p className="text-xs text-ink-2 mt-1.5">
                    {source.mtime} · {source.size_mb} MB
                  </p>
                )}
              </div>

              <div>
                <label className="block text-xs font-medium text-ink-2 mb-1.5">
                  {t('upscale.scale')}
                </label>
                <div className="flex gap-2">
                  {([2, 3, 4] as const).map((n) => (
                    <button
                      key={n}
                      onClick={() => setScale(n)}
                      disabled={busy}
                      className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${FOCUS_RING} ${
                        scale === n
                          ? 'bg-brand text-white shadow-lg'
                          : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1'
                      }`}
                    >
                      {t('upscale.scaleTimes', { n })}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-ink-2 mt-1.5">
                  {t('upscale.scaleHint')}
                </p>
              </div>
            </div>

            {/* 源预览：确认选中的是哪一个视频 */}
            {srcUrl && playing === 'source' && (
              <video src={srcUrl} controls className="w-full rounded-lg bg-black" />
            )}

            <div className="flex gap-2 flex-wrap">
              <Button onClick={submit} disabled={!ready || !selected || submitting || busy}>
                {busy ? t('upscale.running') : t('upscale.start')}
              </Button>
              {srcUrl && (
                <Button
                  variant="secondary"
                  onClick={() => setPlaying(playing === 'source' ? null : 'source')}
                >
                  {playing === 'source' ? t('common.close') : t('upscale.preview')}
                </Button>
              )}
              {!ready && (
                <span className="text-xs text-warning-strong self-center">
                  {t('upscale.notReadyTip')}
                </span>
              )}
            </div>
          </>
        )}
      </div>

      {/* 任务进度 / 结果 */}
      {task && (
        <div className="bg-surface rounded-lg border border-line p-4 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold text-ink-1">{statusText()}</span>
            <span className="text-xs text-ink-2">
              {t('upscale.progress')} {task.progress || 0}%
            </span>
          </div>

          <div className="h-2 rounded-full bg-surface-2 overflow-hidden">
            <div
              className={`h-full transition-colors ${
                task.status === 'error' ? 'bg-danger' : 'bg-brand'
              }`}
              style={{ width: `${Math.min(100, task.progress || 0)}%` }}
            />
          </div>

          {task.message && (
            <p className="text-xs text-ink-2">{task.message}</p>
          )}
          {task.status === 'error' && task.error && (
            <p className="text-xs text-danger-strong break-all">{task.error}</p>
          )}

          {task.status === 'done' && res && (
            <div className="pt-3 border-t border-line space-y-3">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                <div>
                  <div className="text-ink-2">{t('upscale.before')}</div>
                  <div className="font-medium text-ink-1">{fmtResolution(res.before)}</div>
                </div>
                <div>
                  <div className="text-ink-2">{t('upscale.after')}</div>
                  <div className="font-medium text-ink-1">{fmtResolution(res.after)}</div>
                </div>
                <div>
                  <div className="text-ink-2">{t('upscale.elapsed')}</div>
                  <div className="font-medium text-ink-1">
                    {res.elapsed_sec != null ? `${res.elapsed_sec}s` : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-ink-2">{t('upscale.resolution')}</div>
                  <div className="font-medium text-ink-1">
                    {res.after?.size_mb != null ? `${res.after.size_mb} MB` : '—'}
                  </div>
                </div>
              </div>

              {/* 音轨保留情况：无声超分是这里最容易踩的坑 */}
              <p className={`text-xs ${res.after?.has_audio ? 'text-success-strong' : 'text-warning-strong'}`}>
                {res.after?.has_audio ? t('upscale.audioKept') : t('upscale.audioLost')}
              </p>

              {res.output_url && (
                <>
                  <video src={res.output_url} controls className="w-full rounded-lg bg-black" />
                  <div className="flex gap-2">
                    <a
                      href={downloadUrl(res.output_url) || res.output_url}
                      className={`inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-line text-ink-1 hover:bg-surface-2 transition-colors ${FOCUS_RING}`}
                    >
                      {t('common.download')}
                    </a>
                    <span className="text-xs text-ink-2 self-center">
                      {t('upscale.confirmClose')}
                    </span>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {/* 已生成的超分产物 */}
      {artifacts.length > 0 && (
        <div className="bg-surface rounded-lg border border-line p-4">
          <h4 className="text-sm font-semibold text-ink-1 mb-3">
            {t('upscale.artifacts')}（{artifacts.length}）
          </h4>
          <div className="space-y-2">
            {artifacts.map((a) => (
              <div
                key={a.name}
                className="flex items-center justify-between gap-3 py-2 border-b border-line last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-sm text-ink-1 truncate">{a.name}</p>
                  <p className="text-xs text-ink-2">{a.mtime} · {a.size_mb} MB</p>
                </div>
                <div className="flex gap-2 shrink-0">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setPlaying(playing === a.name ? null : a.name)}
                  >
                    {playing === a.name ? t('common.close') : t('upscale.preview')}
                  </Button>
                  {downloadUrl(a.url) && (
                    <a
                      href={downloadUrl(a.url)}
                      className={`inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-line text-ink-1 hover:bg-surface-2 transition-colors ${FOCUS_RING}`}
                    >
                      {t('common.download')}
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
          {playing && artifacts.some((a) => a.name === playing) && (
            <video
              src={artifacts.find((a) => a.name === playing)?.url}
              controls
              className="w-full mt-3 rounded-lg bg-black"
            />
          )}
        </div>
      )}
    </div>
  );
}

export { UpscaleTab };

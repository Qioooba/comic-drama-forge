import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { autopilotApi } from '@/api/client';
import { Button, Skeleton } from '@/components/ui';
import { Play, Pause, RefreshCw, AlertTriangle, Check, ClipboardCheck, Clock, Settings } from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';

/**
 * 托管生产（无人值守自动生产）面板
 * ============================================================
 *
 * 为什么要有这个组件（2026-10-07）
 * --------------------------------
 * 托管是本系统**唯一的 24/7 无人值守生产主路径** —— 后端 autopilot.py 有完整守护线程、
 * 断点续跑、失败隔离、自检门禁，`enable/pause/resume/status/plan` 等端点全部就绪，
 * 但前端**一个调用点都没有**（`autopilotApi.enable/disable/pause/resume` 零引用）。
 * 「自动生产」此前还被从独立标签页移进了 AI 总控聊天面板里
 * （见 ProjectWorkbenchPage 的 tabs 定义注释），等于主生产路径在界面上彻底消失。
 *
 * 后果很具体：用户以为「必须逐个生成资产、逐镜生成视频」，实际完全可以配置好计划后
 * 交给机器跑一夜；反过来，想做无人值守的人根本找不到开关。
 *
 * 设计要点
 * --------
 * 1. **自检前置**：直接消费 `/api/autopilot/ready` 的四项检查（文本模型 / AI 质检 /
 *    FFmpeg / ComfyUI），缺什么、去哪修，后端已经把 hint 写好了，直接显示。
 *    不在前端另写一套判断 —— 那必然与后端漂移（历史上已经有过多套口径）。
 * 2. **门禁错误可读**：`/api/autopilot/enable` 前置 `_ai_gate_or_400`，未配置 AI 时
 *    返回 400 + `ai_selfcheck` 结构。这里原样展示 message/hint，而不是弹一句
 *    「请求失败」。
 * 3. **失败必须回滚 UI**：所有写操作先乐观更新，失败回滚并toast，避免界面显示与
 *    实际托管状态漂移（最难查的一类问题）。
 * 4. **只在标签页可见时轮询**：切走即停，避免常驻请求（与工作台其它标签一致）。
 */

// 与后端 autopilot.PLAN_DEFAULTS 的键一一对应。
// ⚠️ 后端 `api_autopilot_plan_set` 按 `k in PLAN_DEFAULTS` 过滤入参，
//    不在表里的键会被静默丢弃。新增项必须同时加进 PLAN_DEFAULTS。
interface AutopilotPlan {
  enabled?: boolean;
  episodes?: unknown;
  priority?: number;
  max_episode_attempts?: number;
  style?: string;
  target_shots?: number;
  enable_assets?: boolean;
  enable_video?: boolean;
  enable_final?: boolean;
  enable_tts?: boolean;
  enable_tts_pre?: boolean;
  enable_mix?: boolean;
  enable_upscale?: boolean;
  upscale_scale?: number;
  coverage_min_percent?: number;
  consistency_min_score?: number;
  require_consistency?: boolean;
  step_max_retries?: number;
  auto_repair?: boolean;
  overwrite_script?: boolean;
  auto_revive_hours?: number;
  auto_accept?: boolean;
  novel_id?: string;
  [key: string]: unknown;
}

interface ReadyCheck {
  key: string;
  label: string;
  ok: boolean;
  hint?: string;
}

interface ReadyInfo {
  ok: boolean;
  checks: ReadyCheck[];
}

interface AutopilotStatusLike {
  running?: boolean;
  paused?: boolean;
  pause_reason?: string;
  current?: {
    describe?: string;
    step?: string;
    episode?: number;
    percent?: number;
    message?: string;
    stall_warning?: string;
    step_stalled_sec?: number;
  } | null;
  last_run?: Record<string, unknown>;
  last_error?: string;
  pending_review?: number;
  delivered_total?: number;
  exceptions?: unknown;
  enabled_count?: number;
  plan_count?: number;
  other_project_running?: boolean;
  [key: string]: unknown;
}

const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

/** 数字输入的统一样式（与工作台其它原生控件同款，避免引入新组件带来的样式分叉） */
const NUM_INPUT =
  'w-24 rounded-lg border border-line bg-surface-2 px-2 py-1.5 text-sm text-ink-1 tabular-nums ' + FOCUS_RING;

export function AutopilotPanel({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const toast = useToast();

  const [status, setStatus] = useState<AutopilotStatusLike | null>(null);
  const [plan, setPlan] = useState<AutopilotPlan | null>(null);
  const [readyInfo, setReadyInfo] = useState<ReadyInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [gateHint, setGateHint] = useState('');

  // 草稿：输入过程中不立刻回写 plan，避免每敲一个数字就打一次接口
  const [draft, setDraft] = useState<Record<string, string>>({});

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const load = useCallback(async () => {
    if (!projectKey) return;
    const [st, pl] = await Promise.allSettled([
      autopilotApi.status(projectKey),
      autopilotApi.plan(projectKey),
    ]);
    if (!alive.current) return;
    if (st.status === 'fulfilled') {
      setStatus((st.value as unknown as AutopilotStatusLike) || null);
      setError('');
    }
    if (pl.status === 'fulfilled') {
      const p = ((pl.value as { plan?: AutopilotPlan })?.plan || null) as AutopilotPlan | null;
      setPlan(p);
      setDraft({});
    }
    setLoading(false);
  }, [projectKey]);

  // ready（自检）单独拉一次：它会真实探测质检接口连通性（timeout 20s），
  // 不适合放进 5s 轮询里，否则每轮都打一次外部接口。
  const loadReady = useCallback(async () => {
    if (!projectKey) return;
    try {
      const r = (await autopilotApi.ready()) as unknown as ReadyInfo;
      if (alive.current) setReadyInfo(r);
    } catch {
      if (alive.current) setReadyInfo(null);
    }
  }, [projectKey]);

  useEffect(() => { void load(); void loadReady(); }, [load, loadReady]);

  // 状态轮询：只在面板挂着时跑，切走由 alive=false 停掉
  useEffect(() => {
    const timer = setInterval(() => {
      if (!alive.current) return;
      void (async () => {
        try {
          const st = (await autopilotApi.status(projectKey)) as unknown as AutopilotStatusLike;
          if (alive.current) setStatus(st);
        } catch { /* 轮询失败不打断界面；下一轮自动重试 */ }
      })();
    }, 5000);
    return () => clearInterval(timer);
  }, [projectKey]);

  const enabled = !!plan?.enabled;

  const savePlan = async (patch: Record<string, unknown>, labelKey: string) => {
    if (!projectKey) return;
    setBusy(labelKey);
    setGateHint('');
    try {
      const d = await autopilotApi.setPlan(projectKey, patch);
      const p = ((d as { plan?: AutopilotPlan })?.plan || null) as AutopilotPlan | null;
      if (p) setPlan(p);
      setDraft({});
      toast.success(t('ap.planSaved'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('common.failed'));
      void load(); // 回滚到服务端真实值，避免界面显示与实际不一致
    } finally {
      setBusy(null);
    }
  };

  const start = async () => {
    if (!projectKey) return;
    setBusy('start');
    setGateHint('');
    setError('');
    try {
      const d = (await autopilotApi.enable(projectKey)) as {
        plan?: AutopilotPlan; warning?: string; message?: string; hint?: string;
        ai_selfcheck?: { blocked_labels?: string[] };
      };
      if (d?.plan) setPlan(d.plan);
      // 后端在「项目没关联小说」时返回 warning（开了但没活干）——必须显示，
      // 否则用户以为托管已经开起来了，实际一集也不会产。
      if (d?.warning) toast.error(d.warning);
      else if (d?.hint) toast.error(d.hint);
      else toast.success(t('ap.started'));
      void load();
    } catch (e) {
      // 门禁 400 会带 message/hint/ai_selfcheck：原样呈现，别退化成「请求失败」
      const err = e as { message?: string; hint?: string; ai_selfcheck?: { blocked_labels?: string[] } };
      const msg = err?.message || (e instanceof Error ? e.message : t('common.failed'));
      setGateHint([err?.hint, err?.ai_selfcheck?.blocked_labels?.length
        ? t('ap.blockedModules', { list: err.ai_selfcheck.blocked_labels.join('、') })
        : ''].filter(Boolean).join(' '));
      setError(msg);
      toast.error(msg);
    } finally {
      setBusy(null);
    }
  };

  const stop = async () => {
    setBusy('stop');
    try {
      const d = await autopilotApi.disable(projectKey);
      const p = ((d as { plan?: AutopilotPlan })?.plan || null) as AutopilotPlan | null;
      if (p) setPlan(p);
      toast.success(t('ap.stopped'));
      void load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('common.failed'));
      void load();
    } finally {
      setBusy(null);
    }
  };

  const togglePause = async (nextPaused: boolean) => {
    setBusy('pause');
    try {
      if (nextPaused) await autopilotApi.pause();
      else await autopilotApi.resume();
      toast.success(nextPaused ? t('ap.paused') : t('ap.resumed'));
      void load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('common.failed'));
    } finally {
      setBusy(null);
    }
  };

  const numField = (key: string, fallback: number, min: number, max: number, step = 1) => {
    const cur = draft[key] !== undefined
      ? draft[key]
      : String((plan as Record<string, unknown>)?.[key] ?? fallback);
    return (
      <input
        type="number"
        className={NUM_INPUT}
        value={cur}
        min={min}
        max={max}
        step={step}
        onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
        onBlur={() => {
          const v = Number(cur);
          if (!Number.isFinite(v) || v === Number((plan as Record<string, unknown>)?.[key])) return;
          const clamped = Math.min(max, Math.max(min, v));
          void savePlan({ [key]: clamped }, key);
        }}
      />
    );
  };

  const boolField = (key: string, fallback: boolean, labelKey: string, hintKey: string) => {
    const on = !!((plan as Record<string, unknown>)?.[key] ?? fallback);
    return (
      <label className="flex items-start gap-2 cursor-pointer">
        <input
          type="checkbox"
          className="mt-1 accent-[var(--color-brand)]"
          checked={on}
          disabled={busy !== null}
          onChange={(e) => void savePlan({ [key]: e.target.checked }, key)}
        />
        <span className="min-w-0">
          <span className="block text-sm text-ink-1">{t(labelKey)}</span>
          <span className="block text-xs text-ink-2">{t(hintKey)}</span>
        </span>
      </label>
    );
  };

  if (loading) {
    return (
      <div className="space-y-4" role="status" aria-live="polite">
        <Skeleton className="h-24 w-full rounded-xl" />
        <Skeleton className="h-40 w-full rounded-xl" />
      </div>
    );
  }

  const paused = !!status?.paused;
  const cur = status?.current || null;
  const checks = readyInfo?.checks || [];
  const readyOk = checks.length > 0 && checks.every((c) => c.ok);

  return (
    <div className="space-y-4">
      {/* ---------- 主卡片：状态 + 主控制 ---------- */}
      <section className="rounded-xl border border-line bg-surface p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="flex items-center gap-2 text-lg font-semibold text-ink-1">
              <Play className="h-5 w-5" /> {t('ap.title')}
            </h3>
            <p className="mt-1 text-sm text-ink-2">{t('ap.desc')}</p>
          </div>
          <span
            className={
              'shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ' +
              (enabled
                ? paused ? 'bg-warning/15 text-warning-strong' : 'bg-success/15 text-success-strong'
                : 'bg-surface-2 text-ink-2')
            }
          >
            {enabled ? (paused ? t('ap.statePaused') : t('ap.stateRunning')) : t('ap.stateOff')}
          </span>
        </div>

        {/* 当前在做什么 */}
        {cur && (
          <div className="mt-3 rounded-lg border border-line bg-surface-2 p-3">
            <div className="flex items-center gap-2 text-sm text-ink-1">
              <RefreshCw className="h-4 w-4 shrink-0 animate-spin" />
              <span className="min-w-0 truncate">
                {cur.describe || cur.message || t('ap.working')}
                {typeof cur.episode === 'number' ? ` · ${t('ap.episode', { n: cur.episode })}` : ''}
              </span>
            </div>
            {typeof cur.percent === 'number' && (
              <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-surface">
                <div className="h-full rounded-full bg-brand" style={{ width: `${Math.min(100, Math.max(0, cur.percent))}%` }} />
              </div>
            )}
            {cur.stall_warning && (
              <p className="mt-2 flex items-start gap-1.5 text-xs text-warning-strong">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                {cur.stall_warning}
              </p>
            )}
          </div>
        )}

        {/* 未开启时的引导：告诉用户它能干什么 */}
        {!enabled && (
          <p className="mt-3 rounded-lg border border-line bg-surface-2 p-3 text-xs text-ink-2">
            {t('ap.hintManual')}
          </p>
        )}

        {error && (
          <div className="mt-3 rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger-strong">
            <p className="font-medium">{error}</p>
            {gateHint && <p className="mt-1 text-xs opacity-90">{gateHint}</p>}
          </div>
        )}

        {/* 主控制 */}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          {!enabled ? (
            <Button onClick={() => void start()} disabled={busy !== null}>
              <Play className="mr-1.5 h-4 w-4" />
              {busy === 'start' ? t('common.saving') : t('ap.start')}
            </Button>
          ) : (
            <>
              {paused ? (
                <Button onClick={() => void togglePause(false)} disabled={busy !== null}>
                  <Play className="mr-1.5 h-4 w-4" /> {t('ap.resume')}
                </Button>
              ) : (
                <Button variant="secondary" onClick={() => void togglePause(true)} disabled={busy !== null}>
                  <Pause className="mr-1.5 h-4 w-4" /> {t('ap.pause')}
                </Button>
              )}
              <Button variant="secondary" onClick={() => void stop()} disabled={busy !== null}>
                {busy === 'stop' ? t('common.saving') : t('ap.stop')}
              </Button>
            </>
          )}
        </div>
        {status?.pause_reason && paused && (
          <p className="mt-2 text-xs text-ink-2">{t('ap.pauseReason', { reason: status.pause_reason })}</p>
        )}
      </section>

      {/* ---------- 前置自检 ---------- */}
      <section className="rounded-xl border border-line bg-surface p-4">
        <h4 className="flex items-center gap-2 font-semibold text-ink-1">
          <ClipboardCheck className="h-4 w-4" />
          {t('ap.readiness')}
          {readyInfo && (
            <span className={readyOk ? 'ml-auto text-xs text-success-strong' : 'ml-auto text-xs text-warning-strong'}>
              {readyOk ? t('ap.ready') : t('ap.notReady')}
            </span>
          )}
        </h4>
        <p className="mt-1 text-xs text-ink-2">{t('ap.readinessDesc')}</p>
        <ul className="mt-3 space-y-2">
          {checks.map((c) => (
            <li key={c.key} className="flex items-start gap-2">
              {c.ok
                ? <Check className="mt-0.5 h-4 w-4 shrink-0 text-success-strong" />
                : <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning-strong" />}
              <span className="min-w-0">
                <span className="block text-sm text-ink-1">{c.label}</span>
                {c.hint && <span className="block text-xs text-ink-2">{c.hint}</span>}
              </span>
            </li>
          ))}
          {checks.length === 0 && (
            <li className="text-sm text-ink-2">{t('ap.readinessUnavailable')}</li>
          )}
        </ul>
      </section>

      {/* ---------- 交付进度 ---------- */}
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          { k: 'delivered_total', label: t('ap.delivered') },
          { k: 'pending_review', label: t('ap.pendingReview') },
          { k: 'exceptions', label: t('ap.exceptions') },
          { k: 'plan_count', label: t('ap.planCount') },
        ].map((s) => (
          <div key={s.k} className="rounded-lg border border-line bg-surface p-3">
            <div className="text-xl font-bold tabular-nums text-ink-1">
              {String((status as Record<string, unknown>)?.[s.k] ?? 0)}
            </div>
            <div className="text-xs text-ink-2">{s.label}</div>
          </div>
        ))}
      </section>

      {/* ---------- 生产计划 ---------- */}
      <section className="rounded-xl border border-line bg-surface p-4">
        <h4 className="flex items-center gap-2 font-semibold text-ink-1">
          <Settings className="h-4 w-4" /> {t('ap.planTitle')}
        </h4>
        <p className="mt-1 text-xs text-ink-2">{t('ap.planDesc')}</p>

        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <label className="flex items-center justify-between gap-3 text-sm">
            <span className="text-ink-1">{t('ap.targetShots')}</span>
            {numField('target_shots', 12, 4, 60)}
          </label>
          <label className="flex items-center justify-between gap-3 text-sm">
            <span className="text-ink-1">{t('ap.coverage')}</span>
            {numField('coverage_min_percent', 95, 0, 100)}
          </label>
          <label className="flex items-center justify-between gap-3 text-sm">
            <span className="text-ink-1">{t('ap.consistency')}</span>
            {numField('consistency_min_score', 80, 0, 100)}
          </label>
          <label className="flex items-center justify-between gap-3 text-sm">
            <span className="text-ink-1">{t('ap.stepRetries')}</span>
            {numField('step_max_retries', 2, 0, 10)}
          </label>
          <label className="flex items-center justify-between gap-3 text-sm">
            <span className="text-ink-1">{t('ap.episodeAttempts')}</span>
            {numField('max_episode_attempts', 2, 1, 20)}
          </label>
          <label className="flex items-center justify-between gap-3 text-sm">
            <span className="text-ink-1">{t('ap.reviveHours')}</span>
            {numField('auto_revive_hours', 6, 0, 168, 0.5)}
          </label>
        </div>

        <div className="mt-4 space-y-3 border-t border-line pt-4">
          {boolField('auto_repair', true, 'ap.autoRepair', 'ap.autoRepairHint')}
          {boolField('require_consistency', true, 'ap.requireConsistency', 'ap.requireConsistencyHint')}
          {boolField('auto_accept', false, 'ap.autoAccept', 'ap.autoAcceptHint')}
          {boolField('enable_upscale', true, 'ap.enableUpscale', 'ap.enableUpscaleHint')}
        </div>
      </section>

      <p className="flex items-start gap-1.5 text-xs text-ink-3">
        <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        {t('ap.footNote')}
      </p>
    </div>
  );
}
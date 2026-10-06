import React, { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { Badge, Button, Card, Select } from '@/components/ui';
import { AlertTriangle, RefreshCw } from '@/components/ui/icons';
import { actualParamsApi, comfyuiModelsApi, trtEngineApi } from '@/api/client';
import type {
  ComfyUIModelSlot,
  ComfyUIModelsResponse,
  TrtEngineCheckResponse,
  ActualParamsSnapshot,
} from '@/types';

/**
 * ComfyUI 生成模型（全局设置）
 *
 * 为什么不直接在工作流模板里写死模型文件名：ComfyUI 某个节点下拉框的**合法值**
 * 取决于模型文件放在哪个目录（放进 diffusion_models/minimax-h3/ 之后，报出的名字
 * 会带 `minimax-h3\` 前缀）。模板里一旦写的是裸文件名，与实际磁盘布局不符时，
 * 该节点会被校验失败 —— 而且往往是**静默丢弃产出**，不报显式错误，非常难查。
 *
 * 因此这里直接向 ComfyUI 查询权威候选值，由用户手动指定，并即时生效于后续提交。
 */
export function ComfyUIPage() {
  const { t } = useApp();
  const [data, setData] = useState<ComfyUIModelsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [flash, setFlash] = useState<{ key: string; ok: boolean; msg: string } | null>(null);
  const [showPlugins, setShowPlugins] = useState(false);
  const [trt, setTrt] = useState<TrtEngineCheckResponse | null>(null);
  const [trtProbing, setTrtProbing] = useState(false);
  const [actual, setActual] = useState<ActualParamsSnapshot | null>(null);

  const loadActual = useCallback(async () => {
    try {
      const res = await actualParamsApi.latest(1);
      setActual(res.items?.[0] || null);
    } catch {
      setActual(null);
    }
  }, []);

  const checkTrt = useCallback(async (probe: boolean) => {
    if (probe) setTrtProbing(true);
    try {
      setTrt(await trtEngineApi.check(probe));
    } catch (e) {
      setTrt({
        success: false,
        status: '未测试',
        reason: e instanceof Error ? e.message : String(e),
      });
    } finally {
      if (probe) setTrtProbing(false);
    }
  }, []);

  const scan = useCallback(async (refresh: boolean) => {
    setLoading(true);
    setError('');
    try {
      setData(await comfyuiModelsApi.scan(refresh));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void scan(false);
    void checkTrt(false);
    void loadActual();
  }, [scan, checkTrt, loadActual]);

  const handleChange = async (slot: ComfyUIModelSlot, value: string) => {
    setSavingKey(slot.key);
    setFlash(null);
    try {
      const res = await comfyuiModelsApi.select({ [slot.key]: value || null });
      if (res?.success) {
        setData(prev =>
          prev
            ? {
                ...prev,
                slots: prev.slots.map(s =>
                  s.key === slot.key
                    ? { ...s, selected: value, selected_valid: !value || s.values.includes(value) }
                    : s
                ),
              }
            : prev
        );
        setFlash({ key: slot.key, ok: true, msg: t('wb.cm.saved') });
      } else {
        setFlash({ key: slot.key, ok: false, msg: t('wb.cm.saveFailed') });
      }
    } catch (e) {
      setFlash({ key: slot.key, ok: false, msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setSavingKey(null);
      window.setTimeout(() => setFlash(null), 2500);
    }
  };

  const slots = data?.slots || [];
  const offline = data ? !data.success : false;

  return (
    <div className="space-y-6 p-6">
      {/* ===== Header ===== */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold text-ink-1">{t('wb.cm.title')}</h2>
          <p className="mt-1 max-w-3xl text-sm text-ink-3">{t('wb.cm.subtitle')}</p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          disabled={loading}
          onClick={() => scan(true)}
        >
          <RefreshCw className={`mr-2 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          {loading ? t('wb.cm.scanning') : t('wb.cm.scan')}
        </Button>
      </div>

      {/* ===== Stats ===== */}
      {data?.success && (
        <div className="flex flex-wrap items-center gap-2">
          {typeof data.node_type_count === 'number' && (
            <Badge variant="default">
              {t('wb.cm.nodeTypes')}: {data.node_type_count}
            </Badge>
          )}
          {typeof data.core_node_count === 'number' && (
            <Badge variant="default">
              {t('wb.cm.coreNodes')}: {data.core_node_count}
            </Badge>
          )}
          <Badge variant="default">
            {t('wb.cm.customPkgs')}: {data.plugins?.length ?? 0}
          </Badge>
          {data.scanned_at && (
            <span className="text-xs text-ink-3">
              {t('wb.cm.scannedAt')}: {data.scanned_at}
            </span>
          )}
        </div>
      )}

      {/* ===== Error / Offline ===== */}
      {(error || offline) && (
        <div className="flex items-start gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4">
          <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-500" />
          <div className="text-sm">
            <p className="font-medium text-warning-strong">
              {t('wb.cm.offline')}
            </p>
            <p className="mt-1 text-ink-3">{error || data?.error || t('wb.cm.offlineHint')}</p>
          </div>
        </div>
      )}

      {/* ===== TRT engine status (P0-3) ===== */}
      <Card bodyClassName="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold text-ink-1">{t('wb.cm.trtTitle')}</h3>
            <p className="mt-1 text-xs text-ink-3">{t('wb.cm.trtHint')}</p>
          </div>
          <div className="flex items-center gap-2">
            {trt && (
              <Badge variant={
                trt.status === '可用' ? 'success'
                  : trt.status === '不兼容' ? 'danger' : 'warning'
              }>
                {trt.status}
              </Badge>
            )}
            <Button
              variant="secondary"
              size="sm"
              disabled={trtProbing}
              onClick={() => void checkTrt(true)}
            >
              <RefreshCw className={`mr-2 h-4 w-4 ${trtProbing ? 'animate-spin' : ''}`} />
              {trtProbing ? t('wb.cm.trtTesting') : t('wb.cm.trtTest')}
            </Button>
          </div>
        </div>
        {trt?.reason && (
          <p className="mt-3 rounded border border-warning/30 bg-warning-subtle p-2 text-xs text-warning-strong">
            {trt.reason}
          </p>
        )}
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {(trt?.static?.engines || []).map(engine => (
            <div key={engine.key} className="rounded border border-border px-3 py-2 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-ink-2">{engine.key}</span>
                <Badge variant={engine.ok ? 'success' : 'danger'}>
                  {engine.ok ? t('wb.cm.trtFileOk') : t('wb.cm.trtFileMissing')}
                </Badge>
              </div>
              <p className="mt-1 truncate font-mono text-[11px] text-ink-3">{engine.path}</p>
              <p className="text-[11px] text-ink-3">{(engine.size / 1024 / 1024).toFixed(1)} MB</p>
            </div>
          ))}
        </div>
        {trt?.environment && (
          <p className="mt-3 text-[11px] text-ink-3">
            {trt.environment.name} · {t('wb.cm.trtDriver')} {trt.environment.driver || '-'} ·
            {' '}{t('wb.cm.trtCompute')} {trt.environment.compute_cap || '-'}
            {trt.checked_at ? ` · ${t('wb.cm.trtCheckedAt')} ${trt.checked_at}` : ''}
          </p>
        )}
      </Card>

      {/* ===== Actual submitted params (P0-5) ===== */}
      <Card bodyClassName="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold text-ink-1">{t('wb.cm.actualTitle')}</h3>
            <p className="mt-1 text-xs text-ink-3">{t('wb.cm.actualHint')}</p>
          </div>
          <Button variant="secondary" size="sm" onClick={() => void loadActual()}>
            <RefreshCw className="mr-2 h-4 w-4" />
            {t('wb.cm.actualRefresh')}
          </Button>
        </div>
        {!actual ? (
          <p className="mt-3 text-sm text-ink-3">{t('wb.cm.actualEmpty')}</p>
        ) : (
          <div className="mt-3 space-y-3">
            <div className="flex flex-wrap gap-2">
              <Badge variant="default">{t('wb.cm.actualNodes')}: {actual.node_count}</Badge>
              <Badge variant="default">{t('wb.cm.actualSeed')}: {actual.seed ?? '-'}</Badge>
              <Badge variant="default">
                {t('wb.cm.actualSize')}: {actual.width ?? '-'}×{actual.height ?? '-'}
              </Badge>
              <Badge variant="default">{t('wb.cm.actualFps')}: {actual.fps ?? '-'}</Badge>
              <Badge variant="default">
                {t('wb.cm.actualSegments')}: {actual.segment_count ?? '-'}
              </Badge>
              <Badge variant="default">
                {t('wb.cm.actualFrames')}: {actual.total_frames ?? '-'}
              </Badge>
            </div>
            <div className="grid gap-2 text-xs sm:grid-cols-2">
              <div className="rounded border border-border px-3 py-2">
                <p className="font-medium text-ink-2">{t('wb.cm.actualRefine')}</p>
                <p className="mt-1 text-ink-3">
                  {t('wb.cm.actualTemplate')}: {actual.workflow_file || '-'}
                </p>
                <p className="text-ink-3">
                  {t('wb.cm.actualSwitch')}:
                  {' '}{actual.refine_expected ? t('wb.cm.switchOn') : t('wb.cm.switchOff')}
                </p>
                <p className={actual.refine_present ? 'text-success-strong' : 'text-ink-2'}>
                  {t('wb.cm.actualResult')}:
                  {' '}{actual.refine_present
                    ? t('wb.cm.actualRefineOn')
                    : actual.workflow_file === 'minimax_h3_director_二采_加速.json'
                      ? t('wb.cm.actualRefineSelectedOff')
                      : t('wb.cm.actualRefineOff')}
                </p>
              </div>
              <div className="rounded border border-border px-3 py-2">
                <p className="font-medium text-ink-2">{t('wb.cm.actualDlss')}</p>
                <p className="mt-1 text-ink-3">
                  {t('wb.cm.actualTemplate')}:
                  {' '}{actual.workflow_file === 'minimax_h3_director_二采_加速.json'
                    ? t('wb.cm.actualDlssTemplateOn')
                    : t('wb.cm.actualDlssTemplateOff')}
                </p>
                <p className="text-ink-3">
                  {t('wb.cm.actualSwitch')}:
                  {' '}{actual.dlss_bypassed ? t('wb.cm.actualDlssBypass') : t('wb.cm.switchOn')}
                </p>
                <p className={actual.dlss_bypassed ? 'text-warning-strong' : 'text-ink-2'}>
                  {t('wb.cm.actualResult')}:
                  {' '}{actual.dlss_present
                    ? t('wb.cm.actualDlssOn')
                    : actual.dlss_bypassed
                      ? t('wb.cm.actualDlssBypass')
                      : t('wb.cm.actualDlssAbsent')}
                </p>
              </div>
            </div>
            <div className="rounded border border-border px-3 py-2 text-[11px]">
              <p className="truncate font-mono text-ink-2">{actual.workflow_path || '-'}</p>
              <p className="mt-1 truncate font-mono text-ink-3">
                {t('wb.cm.actualWorkflowHash')}: {actual.workflow_hash || '-'}
                {actual.submitted_at ? ` · ${actual.submitted_at}` : ''}
              </p>
            </div>
          </div>
        )}
      </Card>

      {/* ===== Model slots ===== */}
      <div className="space-y-4">
        {slots.length === 0 && !loading && !offline && (
          <Card>
            <p className="p-4 text-sm text-ink-3">{t('wb.cm.empty')}</p>
          </Card>
        )}

        {slots.map(slot => {
          const mismatch = Boolean(slot.selected) && !slot.selected_valid;
          return (
            <Card key={slot.key} bodyClassName="p-5">
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-base font-semibold text-ink-1">{slot.label}</h3>
                  <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-ink-3">
                    {slot.node_type}.{slot.field}
                  </span>
                  {slot.available ? (
                    <Badge variant="default">
                      {t('wb.cm.candidates')}: {slot.values.length}
                    </Badge>
                  ) : (
                    <Badge variant="danger">{t('wb.cm.notFound')}</Badge>
                  )}
                  {mismatch && (
                    <Badge variant="danger">{t('wb.cm.mismatchBadge')}</Badge>
                  )}
                </div>

                <p className="text-xs text-ink-3">{slot.hint}</p>

                <Select
                  label={slot.label}
                  value={slot.selected || ''}
                  onChange={v => void handleChange(slot, v)}
                  disabled={!slot.available || savingKey === slot.key}
                  options={[
                    { value: '', label: t('wb.cm.inherit') },
                    ...slot.values.map(v => ({ value: v, label: v })),
                  ]}
                />

                {mismatch && (
                  <div className="flex items-start gap-2 rounded border border-danger/40 bg-danger-subtle p-2 text-xs text-danger-strong">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
                    <span>
                      {t('wb.cm.mismatch')}
                      <span className="ml-1 font-mono">{slot.selected}</span>
                    </span>
                  </div>
                )}

                {flash?.key === slot.key && (
                  <p className={`text-xs ${flash.ok ? 'text-success-strong' : 'text-danger-strong'}`}>
                    {flash.msg}
                  </p>
                )}
              </div>
            </Card>
          );
        })}
      </div>

      {/* ===== Plugins ===== */}
      {data?.success && (data.plugins?.length ?? 0) > 0 && (
        <Card bodyClassName="p-5">
          <button
            type="button"
            className="flex w-full items-center justify-between text-left"
            onClick={() => setShowPlugins(v => !v)}
          >
            <div>
              <h3 className="text-base font-semibold text-ink-1">{t('wb.cm.pluginsTitle')}</h3>
              <p className="mt-0.5 text-xs text-ink-3">{t('wb.cm.pluginsHint')}</p>
            </div>
            <Badge variant="default">{data.plugins!.length}</Badge>
          </button>
          {showPlugins && (
            <ul className="mt-4 max-h-80 space-y-1 overflow-y-auto pr-1">
              {data.plugins!.map(p => (
                <li
                  key={p.id}
                  className="flex items-center justify-between gap-3 rounded border border-border px-3 py-1.5 text-sm"
                >
                  <span className="truncate font-mono text-xs text-ink-2">{p.id}</span>
                  <span className="flex-shrink-0 text-xs text-ink-3">
                    {t('wb.cm.nodeCount')}: {p.node_count}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      {/* ===== Why ===== */}
      <Card bodyClassName="p-5">
        <h3 className="text-sm font-semibold text-ink-1">{t('wb.cm.whyTitle')}</h3>
        <p className="mt-2 text-xs leading-relaxed text-ink-3">{t('wb.cm.whyText')}</p>
      </Card>
    </div>
  );
}

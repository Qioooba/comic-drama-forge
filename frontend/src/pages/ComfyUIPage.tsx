import React, { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { Badge, Button, Card, Select } from '@/components/ui';
import { AlertTriangle, RefreshCw } from '@/components/ui/icons';
import { comfyuiModelsApi } from '@/api/client';
import type { ComfyUIModelSlot, ComfyUIModelsResponse } from '@/types';

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
  }, [scan]);

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
            <p className="font-medium text-amber-600 dark:text-amber-400">
              {t('wb.cm.offline')}
            </p>
            <p className="mt-1 text-ink-3">{error || data?.error || t('wb.cm.offlineHint')}</p>
          </div>
        </div>
      )}

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
                  <span className="rounded bg-black/5 px-1.5 py-0.5 font-mono text-[11px] text-ink-3 dark:bg-white/10">
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
                  <div className="flex items-start gap-2 rounded border border-red-500/40 bg-red-500/10 p-2 text-xs text-red-600 dark:text-red-400">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
                    <span>
                      {t('wb.cm.mismatch')}
                      <span className="ml-1 font-mono">{slot.selected}</span>
                    </span>
                  </div>
                )}

                {flash?.key === slot.key && (
                  <p className={`text-xs ${flash.ok ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
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

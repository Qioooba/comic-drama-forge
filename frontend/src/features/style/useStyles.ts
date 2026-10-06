// 风格目录数据 hook：拉 /api/styles，模块级缓存（画廊会被反复开关，不能每次重拉）。
//
// 为什么自带缓存而不用 TanStack Query：查询数据层由并行改造的另一位 agent 负责，
// 本 feature 不引入新依赖。待 queryClient 落地后可换实现，调用方签名保持不变。
import { useCallback, useEffect, useState } from 'react';
import { fetchStyles } from './api';
import { FALLBACK_ASPECT_PRESETS } from './aspectPresets';
import { THUMBNAIL_COUNT } from './thumbnails';
import type { StylesResponse } from './types';

let cache: StylesResponse | null = null;
// 仅作「并发去重」标志用，不取返回值，故类型放宽到 Promise<unknown>
let inflight: Promise<unknown> | null = null;

export interface UseStylesResult {
  styles: StylesResponse['styles'];
  aspectPresets: StylesResponse['aspect_presets'];
  counts: Record<string, number>;
  defaultStyleId: string;
  loading: boolean;
  error: string;
  /** 强制重新拉取（后端目录热更新后用）。 */
  reload: () => void;
}

/**
 * 取风格目录。
 *
 * ⚠️ 端点失败时**不抛异常**、只置 error：风格库是新建项目的增强项，
 *    拿不到目录也必须让用户能填自定义风格把项目建出来（fail-open）。
 */
export function useStyles(): UseStylesResult {
  const [data, setData] = useState<StylesResponse | null>(cache);
  const [loading, setLoading] = useState(!cache);
  const [error, setError] = useState('');

  const load = useCallback((force = false) => {
    if (force) {
      cache = null;
      inflight = null;
    }
    if (inflight) return;
    setLoading(true);
    inflight = fetchStyles()
      .then((res) => {
        cache = res;
        setData(res);
        setError('');
        // 缩略图表与后端目录对不上属于**前端映射漏了**，必须响亮（dev 直接可见）
        if (import.meta.env.DEV && res.styles.length !== THUMBNAIL_COUNT) {
          console.warn(
            `[style] 缩略图 ${THUMBNAIL_COUNT} 张与后端目录 ${res.styles.length} 项不一致`,
          );
        }
      })
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        setLoading(false);
        inflight = null;
      });
  }, []);

  useEffect(() => {
    if (!cache) load();
  }, [load]);

  return {
    styles: data?.styles ?? [],
    // 目录没到手时给兜底画幅，保证「新建项目」的画幅下拉始终可用
    aspectPresets: data?.aspect_presets?.length ? data.aspect_presets : FALLBACK_ASPECT_PRESETS,
    counts: data?.counts ?? {},
    defaultStyleId: data?.default_style_id ?? '',
    loading,
    error,
    reload: () => load(true),
  };
}
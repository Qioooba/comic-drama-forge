import React, { createContext, useContext, useState, useEffect, useCallback, useMemo } from 'react';
import { loadLocale, t, getLocaleVersion } from '@/i18n';
import { useToast } from '@/components/ui/toast';

interface AppContextType {
  lang: string;
  setLang: (lang: string) => void;
  t: (key: string, params?: Record<string, string | number>) => string;
  loading: boolean;
  error: string | null;
  showError: (msg: string) => void;
  clearError: () => void;
}

const AppContext = createContext<AppContextType>({
  lang: 'zh-CN',
  setLang: () => {},
  t,
  loading: false,
  error: null,
  showError: () => {},
  clearError: () => {},
});

export function AppProvider({ children }: { children: React.ReactNode }) {
  // 审计 P2-38（2026-09-29）：语言选择持久化到 localStorage —— 旧实现只写内存，
  // 用户选了英文、刷新即丢回 navigator.language。
  const [lang, setLangState] = useState(() => {
    try {
      const saved = window.localStorage.getItem('mjscxt.lang');
      if (saved === 'zh-CN' || saved === 'en-US') return saved;
    } catch { /* 隐私模式等 localStorage 不可用：按浏览器语言回退 */ }
    return navigator.language.startsWith('zh') ? 'zh-CN' : 'en-US';
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 语言包版本号 —— 见 i18n/index.ts 的说明。
  // 关键点：t 必须是「随语言包变化而改变引用」的函数，否则组件里
  // useMemo(() => t('x'), [t]) 会在语言包加载前算出原始 key 并永久缓存。
  const [localeVersion, setLocaleVersion] = useState(getLocaleVersion());

  useEffect(() => {
    loadLocale(lang).then(() => {
      setLocaleVersion(getLocaleVersion());
      setLoading(false);
    }).catch((e) => {
      console.error('Failed to load locale:', e);
      setLoading(false);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setLang = useCallback(async (newLang: string) => {
    await loadLocale(newLang);
    setLangState(newLang);
    setLocaleVersion(getLocaleVersion());
    try {
      window.localStorage.setItem('mjscxt.lang', newLang);
    } catch { /* localStorage 不可用时静默：仅本次会话生效 */ }
  }, []);

  // 引用随 localeVersion 变化 → 依赖 t 的 useMemo/useCallback 会在语言包就绪后重算
  const translate = useCallback(
    (key: string, params?: Record<string, string | number>) => t(key, params),
    [localeVersion]
  );

  // showError 此前是**死 API**：导出了却没人调用、也没人渲染 error，
  // 页面只能各自用原生 alert / console.error 兜着。现在接到全局 Toast 上，
  // 任何地方调 showError 都能真正被用户看到（error 状态保留以兼容旧取值）。
  const toast = useToast();
  const showError = useCallback((msg: string) => {
    setError(msg);
    if (msg) toast.error(msg);
  }, [toast]);
  const clearError = useCallback(() => setError(null), []);

  const value = useMemo(
    () => ({ lang, setLang, t: translate, loading, error, showError, clearError }),
    [lang, setLang, translate, loading, error, showError, clearError]
  );

  return (
    <AppContext.Provider value={value}>
      {children}
    </AppContext.Provider>
  );
}

export function useApp() {
  return useContext(AppContext);
}

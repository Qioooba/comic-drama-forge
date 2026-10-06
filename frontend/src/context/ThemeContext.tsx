import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

/**
 * 界面主题（浅色 / 深色 / 跟随系统）
 *
 * 设计要点：
 * - 三档 mode，**默认 dark**；持久化在 localStorage。key 与判定逻辑必须和
 *   index.html 的防闪烁脚本保持一致（两边改一边要同步另一边）。
 * - 2026-10-07 起**深色是默认主题**（ADR-0003）：`:root` 直接承载深色工作站取值，
 *   浅色降为可选皮肤放在 `:root.light`。所以这里只在浅色时给 <html> 加 `.light`，
 *   深色**不加任何类** —— 「默认什么都不加」就是深色。
 *   ⚠️ 因此默认主题下首屏不会有 `.dark` 类，别再按「有没有 .dark」判断当前主题。
 * - 组件层不感知主题，禁止散落 dark: 变体类。
 * - mode=system 时监听 prefers-color-scheme，系统切换外观实时跟随。
 * - 监听 storage 事件，多标签页之间保持一致。
 */

export type ThemeMode = 'light' | 'dark' | 'system';

/** localStorage key：index.html 防闪烁脚本读取的是同一个 */
const STORAGE_KEY = 'theme-mode';

interface ThemeContextType {
  /** 用户选择的三档偏好 */
  mode: ThemeMode;
  /** 实际生效的主题（mode=system 解析后的结果） */
  resolved: 'light' | 'dark';
  setMode: (mode: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeContextType>({
  mode: 'dark',
  resolved: 'dark',
  setMode: () => {},
});

function readStoredMode(): ThemeMode {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === 'light' || raw === 'dark' || raw === 'system') return raw;
  } catch {
    // localStorage 不可用（隐私模式等）：退回默认深色
  }
  // 2026-10-07：默认深色。原先默认 system（跟随系统），对浅色系统偏好的用户
  // 会在长时创作工作台里得到浅底 + 白卡片，工作台改为低眩光深色更合适。
  return 'dark';
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(readStoredMode);
  // 系统偏好单独存一份：mode=system 时跟随它实时变化
  const [sysDark, setSysDark] = useState(() => window.matchMedia('(prefers-color-scheme: dark)').matches);

  const resolved: 'light' | 'dark' = mode === 'system' ? (sysDark ? 'dark' : 'light') : mode;

  // .light 挂在 <html> 上；index.css 的 :root.light 覆盖层随之生效。
  // 深色是默认（不加类），浅色才加 .light —— 与 index.html 的防闪烁脚本一致。
  useEffect(() => {
    document.documentElement.classList.toggle('light', resolved === 'light');
  }, [resolved]);

  // Electron 桌面壳：标题栏覆盖层是主进程画的，切主题要同步过去；
  // 浏览器/exe 版没有 window.mjscxt，这里直接跳过。
  useEffect(() => {
    const shell = (window as unknown as { mjscxt?: { window?: { setTitleBarTheme?: (t: string) => void } } }).mjscxt;
    shell?.window?.setTitleBarTheme?.(resolved);
  }, [resolved]);

  // 系统主题变化实时跟随（始终监听，mode 非 system 时只是暂不使用）
  useEffect(() => {
    const mql = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (e: MediaQueryListEvent) => setSysDark(e.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, []);

  // 其他标签页改了主题 → 本页跟进
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setModeState(readStoredMode());
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const setMode = useCallback((m: ThemeMode) => {
    setModeState(m);
    try {
      localStorage.setItem(STORAGE_KEY, m);
    } catch {
      // 存不进去就只在当前页生效
    }
  }, []);

  const value = useMemo(() => ({ mode, resolved, setMode }), [mode, resolved, setMode]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  return useContext(ThemeContext);
}

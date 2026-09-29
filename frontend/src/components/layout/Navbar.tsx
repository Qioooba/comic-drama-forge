import React, { useState } from 'react';
import { useApp } from '@/context/AppContext';
import { useTheme, type ThemeMode } from '@/context/ThemeContext';

/** 键盘焦点环：与 ui/index.tsx 的 FOCUS_RING 保持一致 */
const FOCUS_RING =
'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

/** 主题三档的图标（currentColor 描边，随文字色变化） */
const THEME_ICONS: Record<ThemeMode, React.ReactNode> = {
  light: (
    <svg className="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41m11.32-11.32 1.41-1.41" />
    </svg>
  ),
  dark: (
    <svg className="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  ),
  system: (
    <svg className="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <path d="M8 21h8m-4-4v4" />
    </svg>
  ),
};

const THEME_MODES: ThemeMode[] = ['light', 'dark', 'system'];

export function Navbar() {
  const { t, lang, setLang } = useApp();
  const { mode: themeMode, setMode: setThemeMode } = useTheme();
  const [showLangMenu, setShowLangMenu] = useState(false);
  const [showThemeMenu, setShowThemeMenu] = useState(false);

  return (
    <nav className="sticky top-0 z-sticky flex items-center justify-between gap-2 border-b border-line bg-surface/80 px-4 py-3 backdrop-blur-xl sm:px-6">
      <div className="flex min-w-0 items-center gap-2 sm:gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand text-sm font-bold text-white shadow-sm">
          {t('brand.mark')}
        </div>
        <h1 className="truncate text-lg font-bold text-ink-1">
          {t('app.title')}
        </h1>
      </div>

      <div className="flex shrink-0 items-center gap-2 sm:gap-4">
        {/* Status indicator：窄屏只留呼吸圆点，文字让位给标题 */}
        <div className="flex shrink-0 items-center gap-2 rounded-full border border-success/20 bg-success-subtle px-2.5 py-1.5 text-xs text-success-strong sm:px-3">
          <span className="h-2 w-2 animate-pulse rounded-full bg-success" />
          <span className="hidden sm:inline">Online</span>
        </div>

        {/* Theme selector：与语言选择器同款交互，窄屏只留图标 */}
        <div className="relative">
          <button
            onClick={() => setShowThemeMenu(!showThemeMenu)}
            aria-label={t('theme.label')}
            title={t('theme.label')}
            className={`flex shrink-0 items-center gap-2 rounded-md border border-brand/20 bg-brand-subtle px-2.5 py-1.5 text-brand transition-all duration-200 hover:bg-brand/10 hover:shadow-md sm:px-3 ${FOCUS_RING}`}
          >
            {THEME_ICONS[themeMode]}
            <span className="hidden text-sm font-medium sm:inline">{t(`theme.${themeMode}`)}</span>
          </button>
          {showThemeMenu && (
            <div className="absolute right-0 top-full z-dropdown mt-2 w-36 overflow-hidden rounded-xl border border-line bg-surface/95 shadow-md backdrop-blur-xl">
              {THEME_MODES.map((m) => (
                <button
                  key={m}
                  onClick={() => { setThemeMode(m); setShowThemeMenu(false); }}
                  aria-pressed={themeMode === m}
                  className={`flex w-full items-center gap-2.5 px-4 py-2.5 text-left text-sm transition-colors hover:bg-brand-subtle ${
                    themeMode === m ? 'font-medium text-brand' : 'text-ink-1'
                  } ${FOCUS_RING}`}
                >
                  {THEME_ICONS[m]}
                  {t(`theme.${m}`)}
                  {themeMode === m && (
                    <svg className="ml-auto h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                      <path d="M5 13l4 4L19 7" />
                    </svg>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Language selector：窄屏只留国旗 + 箭头 */}
        <div className="relative">
          <button
            onClick={() => setShowLangMenu(!showLangMenu)}
            className={`flex shrink-0 items-center gap-2 rounded-md border border-brand/20 bg-brand-subtle px-2.5 py-1.5 text-brand transition-all duration-200 hover:bg-brand/10 hover:shadow-md sm:px-3 ${FOCUS_RING}`}
          >
            <span>{lang === 'zh-CN' ? '🇨🇳' : '🇺🇸'}</span>
            <span className="hidden text-sm font-medium sm:inline">{lang === 'zh-CN' ? t('lang.zh-CN') : 'English'}</span>
            <svg className="h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          {showLangMenu && (
            <div className="absolute right-0 top-full z-dropdown mt-2 w-32 overflow-hidden rounded-xl border border-line bg-surface/95 shadow-md backdrop-blur-xl">
              <button
                onClick={() => { setLang('zh-CN'); setShowLangMenu(false); }}
                className={`w-full px-4 py-2.5 text-left text-sm text-ink-1 transition-colors hover:bg-brand-subtle ${FOCUS_RING}`}
              >
                🇨🇳 {t('lang.simplified')}
              </button>
              <button
                onClick={() => { setLang('en-US'); setShowLangMenu(false); }}
                className={`w-full px-4 py-2.5 text-left text-sm text-ink-1 transition-colors hover:bg-brand-subtle ${FOCUS_RING}`}
              >
                🇺🇸 English
              </button>
            </div>
          )}
        </div>

        {/* User avatar */}
        <button
          type="button"
          aria-label={t('nav.userMenu')}
          className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand text-sm font-medium text-white transition-transform hover:scale-105 hover:shadow-lg ${FOCUS_RING}`}
        >
          A
        </button>
      </div>
    </nav>
  );
}

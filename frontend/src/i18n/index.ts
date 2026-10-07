// ============================================
// i18n 模块（TypeScript）
// ============================================
import { i18nApi } from '../api/client';

let currentLang = 'zh-CN';
let messages: Record<string, string> = {};
// 语言包版本号：每次 loadLocale 成功都会 +1。
// 用途：t() 本身是模块级函数、引用永不变化，若组件把 t 放进 useMemo/useCallback 依赖，
// 会在语言包尚未加载时算出「原始 key」并被永久缓存（页面出现 project.create 这种字样）。
// AppContext 依赖本版本号重建 t 的引用，从根上修掉这一类问题。
let localeVersion = 0;

// 语言包加载完成的订阅者集合。
// ⚠️ 为什么需要它：`t()` 是**模块级函数、引用永不变化**，所以任何用模块级
//    `t` 渲染文案的组件，在语言包到达前算出的「原始 key」会被永久缓存 ——
//    它不会因为 messages 变了就自己重算（见本文件顶部注释里记的那个坑）。
//    AppProvider 用 localeVersion 重建了 useApp().t 的引用来救自己那份，
//    但**不订阅**语言包的组件（例如 ToastProvider：它在 AppProvider **之外**，
//    拿不到 context）仍然会永久停在原始 key 上。
//    实测症状：读屏把字面量 "toast.regionLabel" 当作区域名念出来。
//    这里给一条显式订阅通道，让这类组件能在语言包就绪后重算一次。
const localeListeners = new Set<() => void>();

/** 订阅语言包加载。返回取消订阅函数。用法见 ToastProvider。 */
export function subscribeLocale(listener: () => void): () => void {
  localeListeners.add(listener);
  return () => {
    localeListeners.delete(listener);
  };
}

function notifyLocaleChanged(): void {
  localeListeners.forEach((fn) => fn());
}

export function getLocaleVersion(): number {
  return localeVersion;
}

export async function loadLocale(lang: string): Promise<void> {
  try {
    const data = await i18nApi.get(lang);
    // Flatten nested messages
    const flat: Record<string, string> = {};
    function flatten(obj: Record<string, unknown>, prefix = '') {
      for (const [key, val] of Object.entries(obj)) {
        const fullKey = prefix ? `${prefix}.${key}` : key;
        if (val && typeof val === 'object' && !Array.isArray(val)) {
          flatten(val as Record<string, unknown>, fullKey);
        } else {
          flat[fullKey] = String(val);
        }
      }
    }
    flatten(data.messages);
    messages = flat;
    currentLang = lang;
    localeVersion += 1;
    document.documentElement.lang = lang;
    // 通知不订阅 context 的组件（ToastProvider 等）重算文案。
    notifyLocaleChanged();
  } catch (error) {
    console.error('Failed to load locale:', error);
  }
}

export function t(key: string, params?: Record<string, string | number>): string {
  let value = messages[key] || key;
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      value = value.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v));
    });
  }
  return value;
}

export function getCurrentLang(): string {
  return currentLang;
}

export async function switchLang(lang: string): Promise<void> {
  await loadLocale(lang);
  window.location.reload();
}

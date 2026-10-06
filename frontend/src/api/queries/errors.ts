/**
 * 错误文案脱敏（缺陷 D5）—— 从工作台页面文件搬到这里。
 *
 * 为什么放在查询层
 * --------------
 * 查询收敛之后，「把一次失败变成用户能看懂的一句话」这件事只发生在一个地方：
 * query 抛错 → 消费方渲染错误态。它原先住在页面文件顶部，而页面文件里
 * 有 6 个域各写一遍调用；搬到 `api/queries/` 后所有域共用同一口径，
 * 也就不必再各自复制一份正则。
 *
 * ⚠️ 搬运必须逐字一致：这里改的是**用户看到的报错**，
 *    属于不可编程契约（R2）。正则、截断长度、fallback 全部保持原样。
 */
import { t } from '@/i18n';

/**
 * 前端错误框只展示人话：丢掉 traceback / 模块名 / 文件路径等实现细节。
 *
 * 判定为「泄漏实现细节」的信号：残留 `.py`、`import`、`module`、
 * `site-packages`，或任何路径分隔符（`/` `\`）—— 说明后端把堆栈直接抛了出来。
 */
export function sanitizeError(err: unknown, fallback = t('wb.actionFailed')): string {
  const raw = typeof err === 'string' ? err : (err as any)?.message || '';
  let text = String(raw || '').trim();
  if (!text) return fallback;
  const tb = text.indexOf('Traceback (most recent call last)');
  if (tb !== -1) text = text.slice(0, tb).trim();
  const lines = text.split(/\r?\n/).filter(Boolean);
  text = (lines[lines.length - 1] || '').trim();
  text = text
    .replace(/File\s+"[^"]*",\s*line\s*\d+/g, '')
    .replace(/\s*from\s+'[^']*'/g, '')
    .replace(/\s*\([^()]*\.py[^()]*\)/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (/\.py\b|\bimport\b|\bmodule\b|site-packages|[\\/]/.test(text)) return fallback;
  return text.length > 160 ? `${text.slice(0, 160)}…` : text;
}
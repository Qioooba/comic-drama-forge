// 风格目录取数（消费 GET /api/styles）。
//
// ⚠️ 为什么不加到 api/client.ts：那份文件属并行改造的另一位 agent（仅允许追加），
//   风格库取数只有本 feature 用，故自包含一个 30 行的请求函数，避免交叉改动。
//   待 TanStack Query 数据层落地后，可整段替换为 query 版，调用方签名不变。
import type { StylesResponse } from './types';

const API_BASE = '/api';

/** 优先展示后端给的可读报错（与 client.ts 的 readError 同一取向）。 */
async function readError(response: Response): Promise<string> {
  let detail = '';
  try {
    const data = await response.clone().json();
    const raw = data?.error || data?.message || data?.detail;
    if (typeof raw === 'string' && raw.trim()) detail = raw.trim();
    else if (raw) detail = JSON.stringify(raw);
  } catch {
    try {
      const text = (await response.text()).trim();
      if (text) detail = text;
    } catch {
      /* 响应体不可读，退化为状态码 */
    }
  }
  return detail || `HTTP ${response.status} ${response.statusText || ''}`.trim();
}

/** 拉取整份风格目录（61 风格 + 8 画幅预设）。 */
export async function fetchStyles(): Promise<StylesResponse> {
  const response = await fetch(`${API_BASE}/styles`, {
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error(await readError(response));
  const data = await response.json();
  if (!data?.success) throw new Error(data?.error || 'HTTP ' + response.status);
  return data as StylesResponse;
}
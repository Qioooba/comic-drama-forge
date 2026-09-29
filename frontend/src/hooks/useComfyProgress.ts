import { useEffect, useRef, useState } from 'react';
import { logsApi } from '@/api/client';
import type { LogsResponse } from '@/types';

/**
 * ComfyUI 实时进度（解析其 stdout 日志流）
 *
 * 数据链路：ComfyUI 进程的 stdout 被重定向到 .workbuddy/test/_out/comfyui_stdout.log，
 * 后端 /api/logs?source=comfyui 按字节偏移增量尾随（app/log_viewer.py，GBK 解码在后端完成）。
 *
 * ComfyUI 用 tqdm 打采样进度，`\r` 原地刷新在 log_viewer 的 splitlines() 下成为独立行：
 *   52%|█████▏  | 10/19 [00:04<00:03, 2.34it/s]
 * 正则取最后的 N/M 即「执行到第几步 / 共几步」；"Prompt executed in ..." 表示
 * 一次提交跑完 → 置为空闲。30 秒没有新进度也视为空闲（任务已换走 / 出错）。
 *
 * ⚠️ 只在 enabled=true（有生产任务在跑）时轮询；日志文件不存在（ComfyUI 非本目录
 * 启动、桌面版路径不同）时退避到 15s 慢轮询，绝不打接口风暴。
 */

export interface ComfyProgress {
  /** 最近 30s 内有采样进度输出 */
  active: boolean;
  /** 当前步 / 总步数（tqdm 的 N/M） */
  current: number;
  total: number;
  percent: number;
  /** 最近一次进度更新的 epoch ms */
  updatedAt: number;
  /** 最近一行原始日志（悬浮展示 / 排查用），截到 120 字符 */
  lastLine: string;
}

const IDLE: ComfyProgress = { active: false, current: 0, total: 0, percent: 0, updatedAt: 0, lastLine: '' };

const TQDM_RE = /(\d+)%\|[^|]*\|\s*(\d+)\s*\/\s*(\d+)/;
const DONE_RE = /Prompt executed in/i;
const ACTIVE_WINDOW_MS = 30_000;

export function useComfyProgress(enabled: boolean, intervalMs = 2500): ComfyProgress {
  const [progress, setProgress] = useState<ComfyProgress>(IDLE);
  const offsetRef = useRef(0);
  const lastRef = useRef<ComfyProgress>(IDLE);
  const failRef = useRef(0);

  useEffect(() => {
    if (!enabled) {
      offsetRef.current = 0;
      lastRef.current = IDLE;
      setProgress(IDLE);
      return;
    }
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      let next = intervalMs;
      try {
        const res: LogsResponse = await logsApi.tail({ source: 'comfyui', since: offsetRef.current || 0, tail: 200 });
        if (!alive) return;
        if (!res?.success) {
          // 文件不存在 / 读取失败：连续失败后退避慢轮询
          failRef.current += 1;
          next = failRef.current > 2 ? 15000 : intervalMs;
        } else {
          failRef.current = 0;
          offsetRef.current = res.offset ?? offsetRef.current;
          let last = lastRef.current;
          for (const ln of res.lines || []) {
            const m = TQDM_RE.exec(ln);
            if (m) {
              const current = Number(m[2]);
              const total = Number(m[3]);
              if (total > 0) {
                last = {
                  active: true,
                  current,
                  total,
                  percent: Math.round((current / total) * 100),
                  updatedAt: Date.now(),
                  lastLine: ln.trim().slice(0, 120),
                };
              }
            } else if (DONE_RE.test(ln)) {
              last = { ...last, active: false, updatedAt: Date.now(), lastLine: ln.trim().slice(0, 120) };
            }
          }
          if (last.active && Date.now() - last.updatedAt > ACTIVE_WINDOW_MS) {
            last = { ...last, active: false };
          }
          lastRef.current = last;
          setProgress(last);
        }
      } catch {
        if (!alive) return;
        failRef.current += 1;
        next = 15000;
      }
      if (alive) timer = setTimeout(tick, next);
    };
    tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [enabled, intervalMs]);

  return progress;
}

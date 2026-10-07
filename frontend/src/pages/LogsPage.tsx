import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { Badge, Button, Card, Input, Select } from '@/components/ui';
import { logsApi } from '@/api/client';
import type { LogSource, LogsResponse } from '@/types';

/** 内存中最多保留多少行（防止长时间挂着拖垮浏览器） */
const MAX_KEEP = 2000;
const REFRESH_MS = 3000;

function fmtSize(n: number): string {
  if (!n) return '0 B';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

/**
 * 取日志级别，判据与后端 `log_viewer._level_of` 一致。
 *
 * ⚠️ 这里**不能**用 `startsWith('WARNING')` / `includes('WARNING:')` 这类写法：
 * 2026-10-07 统一日志格式后，行首变成了时间戳、级别被方括号包住：
 *   `2026-10-07 14:13:57.386 [WARNING] script_generator | ...`
 * `startsWith('WARNING')` 因行首是数字而永不成立，`includes('WARNING:')` 又匹配不到
 * `[WARNING] ` 的形态 → 所有行都落到默认灰，整页日志看起来「全是一个颜色」。
 * （旧格式 `WARNING:app:msg` 仍需认，因为历史日志文件里大量存在，故保留第 2 条兜底。）
 *
 * 另外，判据**只扫行首窗口**，不全文搜索级别词 —— 正文里出现「ERROR:」字样的普通
 * INFO 行不应被染成红色。
 */
const LEVEL_SCAN_WINDOW = 64;
const BRACKET_LEVEL_RE = /\[(CRITICAL|FATAL|ERROR|WARNING|WARN|INFO|DEBUG)\]/i;
/** 旧格式 `LEVEL:name:message`；`WARN`/`FATAL` 归一到 `WARNING`/`CRITICAL` */
const LEVEL_ALIAS: Record<string, string> = { WARN: 'WARNING', FATAL: 'CRITICAL' };

function levelOf(line: string): string | null {
  const head = line.slice(0, LEVEL_SCAN_WINDOW);
  const m = BRACKET_LEVEL_RE.exec(head);
  if (m) return LEVEL_ALIAS[m[1].toUpperCase()] ?? m[1].toUpperCase();
  const legacy = head.split(':', 1)[0].trim().toUpperCase();
  if (legacy === 'CRITICAL' || legacy === 'ERROR' || legacy === 'WARNING'
    || legacy === 'INFO' || legacy === 'DEBUG') {
    return LEVEL_ALIAS[legacy] ?? legacy;
  }
  return null;
}

/**
 * 行着色。
 *
 * `inherit` 是**上一行已识别出的级别**：ERROR 的 traceback、多行消息的后续行本身不带
 * 级别前缀，各自都返回 null，若一律按「无级别 → 灰」处理，堆栈就会与错误首行断开、
 * 读起来像另一条无关信息。子日志行跟住父行的颜色才符合阅读预期 —— 仅对错误级
 * 生效，WARNING 的续行仍回落默认色，避免长告警把整屏染黄。
 */
function lineClass(line: string, inherit?: string | null): string {
  const lv = levelOf(line);
  if (lv) {
    switch (lv) {
      case 'CRITICAL':
      case 'ERROR':
        return 'text-danger-strong';
      case 'WARNING':
        return 'text-warning-strong';
      case 'DEBUG':
        return 'text-ink-3';
      default:
        return 'text-ink-2';
    }
  }
  // 续行（无级别前缀）：仅在**错误级**下跟住父行的红色系，但降一档。
  // 实测一个 HTTPConnectionPool 的 traceback 就带 34~75 行，若续行也用
  // danger-strong，4767 行日志里会有 1500+ 行标红，真正需要一眼定位的
  // ERROR 首行反而被淹没；WARNING 的续行则一律回落默认色，避免长告警染黄整屏。
  if (inherit === 'CRITICAL' || inherit === 'ERROR') return 'text-danger';
  return 'text-ink-2';
}

/**
 * 后台服务日志
 *
 * 服务通过计划任务以后台进程运行，stdout/stderr 被重定向到磁盘上的日志文件，
 * 没有终端窗口可看 —— 这里把它接到 Web 上，支持实时跟随、按级别过滤与关键字搜索。
 *
 * 跟随策略：首屏按 tail 取尾部若干行，之后每 3 秒只请求 **since 之后的新增部分**
 * (AoT offset)，既省流量也省 CPU（后端是 seek 追加读，不重扫整个文件）。
 */
export function LogsPage() {
  const { t } = useApp();
  const [sources, setSources] = useState<LogSource[]>([]);
  const [source, setSource] = useState('serve');
  const [tail, setTail] = useState(300);
  const [level, setLevel] = useState('');
  const [query, setQuery] = useState('');
  const [auto, setAuto] = useState(true);
  const [lines, setLines] = useState<string[]>([]);
  const [meta, setMeta] = useState<LogsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const offsetRef = useRef(0);
  const boxRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  const load = useCallback(async (incremental: boolean) => {
    setLoading(true);
    setError('');
    try {
      const res = await logsApi.tail({
        source,
        tail,
        since: incremental ? offsetRef.current : 0,
        q: query || undefined,
        level: level || undefined,
      });
      if (!res?.success) {
        setError(res?.error || t('wb.logs.loadFailed'));
        return;
      }
      offsetRef.current = res.offset ?? 0;
      setMeta(res);
      setLines(prev => {
        const next = incremental ? prev.concat(res.lines || []) : (res.lines || []);
        return next.length > MAX_KEEP ? next.slice(next.length - MAX_KEEP) : next;
      });
      if (stickRef.current) {
        requestAnimationFrame(() => {
          const el = boxRef.current;
          if (el) el.scrollTop = el.scrollHeight;
        });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [source, tail, query, level, t]);

  // 日志源清单（大小 / 更新时间）
  useEffect(() => {
    let alive = true;
    logsApi.sources()
      .then(r => { if (alive) setSources(r.sources || []); })
      .catch(() => { /* 清单拉不到不阻塞主体 */ });
    return () => { alive = false; };
  }, []);

  // 来源 / 行数 / 过滤条件变化 → 重新全量取，并把跟随偏移归零
  useEffect(() => {
    offsetRef.current = 0;
    void load(false);
  }, [load]);

  // 定时增量跟随
  useEffect(() => {
    if (!auto) return;
    const id = window.setInterval(() => { void load(true); }, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [auto, load]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    stickRef.current = atBottom;
  };

  const cur = sources.find(s => s.key === source);
  const counts = meta?.counts || {};

  // 逐行定色：需要「上一行的级别」，故在渲染前一次算完（顺带保住原来的 React key）
  const rendered = useMemo(() => {
    const out: { key: string; text: string; cls: string }[] = [];
    let prev: string | null = null;
    lines.forEach((ln, i) => {
      const lv = levelOf(ln);
      const cls = lineClass(ln, lv ? null : prev);
      if (lv) prev = lv;
      out.push({ key: `${i}-${ln.slice(0, 24)}`, text: ln, cls });
    });
    return out;
  }, [lines]);

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold text-ink-1">{t('wb.logs.title')}</h2>
          <p className="mt-1 max-w-3xl text-sm text-ink-3">{t('wb.logs.subtitle')}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant={auto ? 'primary' : 'secondary'} size="sm" onClick={() => setAuto(v => !v)}>
            {auto ? t('wb.logs.following') : t('wb.logs.paused')}
          </Button>
          <Button variant="secondary" size="sm" disabled={loading} onClick={() => void load(false)}>
            {t('wb.logs.refresh')}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setLines([])}>
            {t('wb.logs.clear')}
          </Button>
        </div>
      </div>

      {/* 控制条 */}
      <Card bodyClassName="p-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
          <Select
            label={t('wb.logs.source')}
            value={source}
            onChange={setSource}
            options={(sources.length ? sources : [{ key: source, label: source } as unknown as LogSource])
              .map(s => ({ value: s.key, label: s.label }))}
          />
          <Select
            label={t('wb.logs.level')}
            value={level}
            onChange={setLevel}
            options={[
              { value: '', label: t('wb.logs.levelAll') },
              { value: 'ERROR', label: 'ERROR' },
              { value: 'WARNING', label: 'WARNING' },
              { value: 'INFO', label: 'INFO' },
              { value: 'DEBUG', label: 'DEBUG' },
            ]}
          />
          <Select
            label={t('wb.logs.tail')}
            value={String(tail)}
            onChange={v => setTail(Number(v) || 300)}
            options={[
              { value: '100', label: '100' },
              { value: '300', label: '300' },
              { value: '1000', label: '1000' },
              { value: '2000', label: '2000' },
            ]}
          />
          <Input
            label={t('wb.logs.search')}
            value={query}
            onChange={setQuery}
            placeholder={t('wb.logs.searchPh')}
          />
        </div>
      </Card>

      {/* 状态信息 */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {cur && (
          <>
            <Badge variant="default">{t('wb.logs.size')}: {fmtSize(cur.size)}</Badge>
            <Badge variant="default">{t('wb.logs.modified')}: {cur.modified_at || '—'}</Badge>
          </>
        )}
        {meta?.encoding && <Badge variant="default">{t('wb.logs.encoding')}: {meta.encoding}</Badge>}
        {counts.ERROR ? <Badge variant="danger">ERROR: {counts.ERROR}</Badge> : null}
        {counts.WARNING ? <Badge variant="warning">WARNING: {counts.WARNING}</Badge> : null}
        {loading && <span className="text-ink-3">{t('wb.logs.loading')}</span>}
        {meta?.truncated && <Badge variant="warning">{t('wb.logs.truncated')}</Badge>}
      </div>

      {error && (
        <div className="rounded-lg border border-danger/40 bg-danger-subtle p-3 text-sm text-danger-strong">
          {error}
        </div>
      )}

      {/* 日志正文 */}
      <Card>
        <div
          ref={boxRef}
          onScroll={onScroll}
          className="max-h-[60vh] min-h-[320px] overflow-auto bg-surface-2 p-4 font-mono text-[12px] leading-relaxed"
        >
          {lines.length === 0 && !loading && (
            <p className="text-ink-3">{t('wb.logs.empty')}</p>
          )}
          {rendered.map(({ key, text, cls }) => (
            <div key={key} className={`whitespace-pre-wrap break-all ${cls}`}>
              {text}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

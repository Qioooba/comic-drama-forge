// ============================================
// Markdown 渲染（AI 文本展示统一出口）
// ============================================
// 起因（2026-10-07 用户反馈）：AI 总控面板把回复当纯文本吐出来 —— 模型返回的是
// Markdown 源码（## 标题、- 列表、**加粗**、``` 代码块、表格），界面却原样显示
// 标记符号。此前 ChatPanel 用 `<p className="whitespace-pre-wrap">{content}</p>`，
// 剧本弹窗/折叠面板用 `<pre>`，两者都只能"看"不能"读"。
//
// 依赖：react-markdown + remark-gfm + remark-breaks。
//   · react-markdown —— 不注入 raw HTML（默认把原始 HTML 当文本转义），无 XSS 面；
//   · remark-gfm     —— 表格 / 删除线 / 任务列表 / 自动链接，LLM 输出高频用到；
//   · remark-breaks  —— 单换行渲染成 <br>。模型写回复时习惯用单换行分段（不是空行），
//                       CommonMark 默认把软换行折叠成空格，会把两行并成一行，读起来像
//                       一坨。开了 breaks 才和 GitHub 评论区、Typora 的直觉一致。
// ⚠️ 不装 @tailwindcss/typography：它自带一套字号/配色，与本项目 tailwind.config.js 的
//    语义 token（surface / ink-1 / line / brand）冲突，等于两套设计语言打架。这里按元素
//    逐个覆盖 class，样式永远跟着 token 走，深色主题自动适配。
//
// 窄栏适配：右侧总控面板只有 300~600px，代码块 / 表格 / 长路径一律横向滚动 + 断词，
// 绝不撑破面板把整条消息顶出可视区。
import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';

const cx = (...parts: Array<string | false | null | undefined>): string =>
  parts.filter(Boolean).join(' ');

/** 从 ```lang 围栏的 className（language-ts）里取出语言名 */
function langOf(className?: string): string | null {
  const m = /language-([\w+-]+)/.exec(className || '');
  return m ? m[1] : null;
}

/**
 * 段落/列表的统一垂直节奏：相邻块之间留白，同一容器首尾不额外撑开。
 * 用 [&>*]:my-* 一次性覆盖所有直接子块，避免每个元素各写一套 margin。
 */
const BLOCK_RHYTHM =
  '[&>*+*]:mt-2 [&>p]:leading-relaxed';

export function Markdown({ children, className }: { children: string; className?: string }) {
  const text = (children ?? '').trim();
  // 空回复（例如总控只跑了工具没出话）不留空气泡
  if (!text) return null;

  return (
    <div className={cx('text-sm break-words', BLOCK_RHYTHM, className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          // ── 标题：模型爱用 ## / ### 当小节标题，压成 1~3 级，避免撑破窄栏 ──
          h1: ({ children: c }) => (
            <h1 className="text-base font-semibold leading-snug text-ink-1">{c}</h1>
          ),
          h2: ({ children: c }) => (
            <h2 className="text-sm font-semibold leading-snug text-ink-1">{c}</h2>
          ),
          h3: ({ children: c }) => (
            <h3 className="text-sm font-semibold leading-snug text-ink-2">{c}</h3>
          ),
          h4: ({ children: c }) => <h4 className="text-sm font-medium text-ink-2">{c}</h4>,
          h5: ({ children: c }) => <h5 className="text-xs font-medium text-ink-2">{c}</h5>,
          h6: ({ children: c }) => <h6 className="text-xs font-medium text-ink-3">{c}</h6>,

          p: ({ children: c }) => <p className="text-ink-1">{c}</p>,

          // ── 列表：嵌套层级靠缩进 + 更小圆点区分，避免深层嵌套缩到看不见 ──
          ul: ({ children: c, className: cn }) => (
            <ul
              className={cx(
                'list-disc space-y-1 pl-5 text-ink-1 marker:text-ink-3',
                '[&>li::marker]:text-[0.9em]',
                // task list（- [ ] / - [x]）自带 checkbox，隐藏项目符号由 remark 处理
                cn?.includes('contains-task-list') && '[&>li]:list-none [&>li]:pl-0',
              )}
            >
              {c}
            </ul>
          ),
          ol: ({ children: c }) => (
            <ol className="list-decimal space-y-1 pl-5 text-ink-1 marker:text-ink-3">{c}</ol>
          ),
          li: ({ children: c }) => <li className="leading-relaxed [&>ul]:mt-1 [&>ol]:mt-1">{c}</li>,

          // 任务列表复选框：只读展示，禁用指针避免看起来能点。
          // 必须摘掉 node —— react-markdown v9+ 会把 hast 节点当 prop 传进来，
          // 直接 {...props} 展开会把 node 甩到 DOM 上，React 报未知属性警告。
          input: ({ node: _node, ...props }) => (
            <input
              {...props}
              disabled
              className="mr-1 h-3 w-3 shrink-0 translate-y-[1px] accent-[rgb(var(--brand))]"
            />
          ),

          strong: ({ children: c }) => <strong className="font-semibold text-ink-1">{c}</strong>,
          em: ({ children: c }) => <em className="italic">{c}</em>,
          del: ({ children: c }) => <del className="text-ink-3 line-through">{c}</del>,

          // 外链一律新标签页打开 + noopener：模型偶尔甩个文档链接，不能让它替换掉
          // 单页应用当前页（刷新后整个工作台状态就没了）
          a: ({ children: c, href }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-brand underline underline-offset-2 hover:no-underline break-all"
            >
              {c}
            </a>
          ),

          // ── 代码：pre 只做透传，块级/行内的样式判定全交给 code ──
          // （react-markdown v9+ 不再给 code 传 inline 属性，靠 unwrap pre + 语言类名/换行
          //   判定块级，见下方 code）
          pre: ({ children: c }) => <>{c}</>,

          code: ({ className: cn, children: c }) => {
            const raw = Array.isArray(c) ? c.join('') : String(c ?? '');
            const isBlock = !!langOf(cn || undefined) || raw.includes('\n');
            if (!isBlock) {
              return (
                <code className="rounded border border-line bg-surface-2 px-1 py-px font-mono text-[0.92em] text-brand">
                  {c}
                </code>
              );
            }
            const lang = langOf(cn || undefined);
            return (
              // 整块可横向滚动；不加 min-w-0 会让长行把气泡撑破面板
              <div className="min-w-0 overflow-hidden rounded-md border border-line bg-surface-2">
                {lang && (
                  <div className="border-b border-line px-2.5 py-1 font-mono text-[10px] uppercase tracking-wide text-ink-3">
                    {lang}
                  </div>
                )}
                <pre className="min-w-0 overflow-x-auto px-2.5 py-2 font-mono text-[12px] leading-relaxed text-ink-1">
                  <code className="font-mono">{c}</code>
                </pre>
              </div>
            );
          },

          blockquote: ({ children: c }) => (
            <blockquote className="border-l-2 border-brand/40 pl-2.5 text-ink-2">{c}</blockquote>
          ),

          hr: () => <hr className="border-line" />,

          img: ({ src, alt }) => (
            <img src={src} alt={alt || ''} loading="lazy" className="max-w-full rounded-md border border-line" />
          ),

          // ── 表格：remark-gfm 产出 <table>，外面套横向滚动容器，
          //    否则窄栏里两列就撑破布局 ──
          table: ({ children: c }) => (
            <div className="min-w-0 overflow-x-auto rounded-md border border-line">
              <table className="w-full border-collapse text-[12px]">{c}</table>
            </div>
          ),
          thead: ({ children: c }) => <thead className="bg-surface-2">{c}</thead>,
          th: ({ children: c }) => (
            <th className="whitespace-nowrap border-b border-line px-2 py-1.5 text-left font-semibold text-ink-1">
              {c}
            </th>
          ),
          td: ({ children: c }) => (
            <td className="border-b border-line/60 px-2 py-1.5 align-top text-ink-2">{c}</td>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

export default Markdown;
# comic-drama-forge 设计系统（MASTER）

> 权威运行时令牌：`frontend/src/index.css` + `frontend/tailwind.config.js`
> 自动化校验：`scripts/audit_tokens.py`（CI 门禁）
> 一致性审计脚本把本文档的规则变成可执行检查 —— 文档不落地成检查就会腐化。
>
> **扫描范围（2026-10-07 更正）**：审计覆盖 `frontend/**`（排除
> `node_modules` / 产物目录），**含 `index.html` 与 `tailwind.config.js`**。
> 原先只扫 `frontend/src/**`，而白名单里却列了这两个在扫描根之外的文件 ——
> 看着在管、实际一次都没读过。扫描根已扩，并由 `WHITELIST001` 断言白名单条目
> 必须真实存在且落在扫描根内，杜绝「死条目」复活。
> 例外（白名单）：`index.css`（令牌定义本体）、`index.html`（防闪烁底色，
> **已由 `BG001` 与 `--bg-canvas` 交叉校验**）、`context/ThemeContext.tsx`
> （主题判定）、`public/te_3d_render/render.html`（独立 Three.js 出图探针，
> 颜色是出图输入参数而非 UI 样式）。

---

## 1. 产品气质

**漫剧工坊**是一台本地 AI 漫剧生产工作站。用户在**同一屏内**完成：从剧本到分镜、
逐镜审片、配音字幕、质检、合成交付。典型单次会话时长 30 分钟到数小时。

因此设计的**第一目标不是"好看"，是"长时间不累"**。这决定了下面每一条取舍：

| 决策 | 取值 | 理由 |
|---|---|---|
| 默认主题 | **深色工作站** | 长时间盯屏审片，浅底 + 白卡片是持续视觉噪声 |
| 装饰预算 | 近乎为零 | 分辨率是用户的认知预算，要留给内容而非特效 |
| 动效 | 150–220ms，只动 transform/opacity | 长列表里每次重排都跑 transition 会拖垮帧率 |
| 状态表达 | 色 + 文本 + 图标**三重** | 色觉障碍用户约占男性 8%、女性 0.5%；颜色不能是唯一载体 |

### 1.1 被否决的方案（记录理由以防回潮）

| 否决项 | 理由 |
|---|---|
| 浅色为默认 | 审片场景下长时间浅底刺眼；深色 + 低眩光更适合长时间工作 |
| `.glass` backdrop-blur 用于长列表 | 滚动时逐帧重绘滤镜，帧率崩。原注释早已承认，改名 `.glass-chrome` 强制其只用于常驻 chrome |
| 品牌→青渐变文字 / 辉光 | 装饰性；且渐变标题在深浅两套皮肤上都需额外 fallback，标题应是稳定纯色 |
| 科技感辉光 + 64px 网格默认开启 | 「氛围不是夜店」的自认知，但对工作台是**持续的**噪声。降级为 `data-skin="tech"` opt-in |
| 进度条渐变填充 | 进度是**信息**不是装饰；渐变导致不同进度下颜色不可比 |
| 卡片 hover 悬浮上移 | 纯装饰，且在密集列表里造成视觉抖动 |

---

## 2. 令牌表（唯一真源）

所有色值令牌一律存 **RGB 三元组**（`79 70 229`），**不存 hex**。

> ⚠️ **不可违反**：Tailwind v3 的透明度修饰符（`bg-brand/10`）靠 `<alpha-value>`
> 占位符生成 `rgb(var(--x) / .1)`。若令牌存成 `#4f46e5`，`bg-brand/10` 会产出
> **无效 CSS 而静默失效** —— 不报错，只是那一层背景永远不出现。
> 在 CSS 里直接使用请写 `rgb(var(--brand))`。

### 2.1 承载与分层

| 语义 | 令牌 | 深色（默认） | 浅色（`.light`） |
|---|---|---|---|
| 页面画布 | `--bg-canvas` | `#020617` | `#F7F8FA` |
| 卡片 / 面板 | `--bg-surface` | `#0F172A` | `#FFFFFF` |
| 次级区块 | `--bg-surface-2` | `#1E293B` | `#F1F5F9` |
| 媒体舞台面 | `--bg-media` | `#05070B` | `#05070B`（**两套皮肤同值**） |

> 媒体面刻意在浅色皮肤下仍是深色：图片/视频在深底上才不刺眼，也避免
> 浅色皮肤里出现一块突兀的深色区域。

### 2.2 描边与文字

| 语义 | 令牌 | 深色 | 浅色 |
|---|---|---|---|
| 常规描边 | `--border` | `#334155` | `#E2E8F0` |
| 强调描边 | `--border-strong` | `#475569` | `#CBD5E1` |
| 主文字 | `--text-primary` | `#E2E8F0` | `#0F172A` |
| 次文字 | `--text-secondary` | `#94A3B8` | `#475569` |
| 弱化 / 禁用 | `--text-tertiary` | `#64748B` | `#94A3B8` |

### 2.3 生产状态语义色（6 档）

| 状态 | 令牌 | 深色 | 浅色 |
|---|---|---|---|
| 待办 | `--state-pending` | `#94A3B8` | `#94A3B8` |
| 进行中 | `--state-running` | `#06B6D4` | `#06B6D4` |
| 完成 | `--state-done` | `#10B981` | `#10B981` |
| 失败 | `--state-failed` | `#EF4444` | `#EF4444` |
| 跳过 | `--state-skipped` | `#A1A1AA` | `#A1A1AA` |
| 需注意 | `--state-attention` | `#F59E0B` | `#F59E0B` |

### 2.4 采用 / 批准（**两档独立**）—— 本项目最重要的语义

| 决策 | 令牌 | 深色 | 浅色 | 含义 |
|---|---|---|---|---|
| **已采用** | `--selected` | `#36D6C5` 青 | `#0D9488` | 创作决定：这版被采用为工作版本 |
| **已批准** | `--approved` | `#50D890` 绿 | `#16A34A` | 放行决定：人工批准，可进入交付 |

> **不变量：`selected` 永不显示为 `approved`。**
> 组件层不得用 approved 的色/文案/图标表达 selected 状态，反之亦然。
> ⚠️ **校验现状（2026-10-07 更正，别把这条当成已被机器守住）**：
> `scripts/audit_tokens.py` 的 `SEL001` 只是**提醒级**检查 —— 它列出「同一文件里
> 同时出现 selected 与 approved 类名」的文件，**不计入退出码**，且判据无法证明
> 两者指向同一元素（需要运行时）。所以这条不变量目前**仍靠 code review 保证**，
> 机器只能提示你去review。原文写的「静态校验这条」比实现强，是错的，已更正。
>
> 为什么单列：改造前只有 6 档 `state-*`，其中 `state-done` 同时承担"已选中"和
> "已批准"两种含义 —— 这正是"为什么这镜用了这一版"无法回答的**视觉根因**。
> 机器检查通过 ≠ 人工批准，两者是不同性质的决策，必须有两档视觉。

### 2.5 尺寸

| 用途 | 令牌 | 值 |
|---|---|---|
| 标准控件高 | `--control-h` | 40px |
| 紧凑控件高 | `--control-h-compact` | 36px |
| 纯图标触达区 | `--hit-target` | 44×44 |
| 正文可读宽度 | `--measure` | 70ch |
| 外壳上限 | `--shell-max` | 2100px |
| 字段宽 | `--control-field` | 320px |
| 宽字段 | `--control-field-lg` | 520px |

**间距**：4px 基网格 → `--space-1..12` = 4 / 8 / 12 / 16 / 24 / 32 / 48

**圆角**：控件 6 / 字段 8 / 面板 10 / 容器 12

**字号七档**：12 / 13 / 14 / 16 / 18 / 24 / 32

**字体三族**：`--font-sans`（UI 正文，中文优先）/ `--font-serif`（编辑性标题）/
`--font-mono`（数值：seed、时长、哈希、路径）。**不发外部字体请求**（离线可用）。

**等宽数字**：时长 / 种子 / 队列 / 资源值**必须**加 `tabular-nums`（Tailwind
内置工具类）。数字宽度一致才能逐位核对，否则 `01:05` 与 `01:55` 左右跳动
无法比对。
> ⚠️ 2026-10-07 更正：原文写「必须加 `tabular` 类」，但 `tailwind.config.js`
> 里那个产出 `.tabular` 的 `fontVariantNumeric` 配置**零命中**（全仓 13 处用的
> 都是内置 `tabular-nums`）—— 死配置已删除，文档随实现改为 `tabular-nums`。

### 2.6 尺寸令牌的收敛进度（**未完成，如实记录**）

尺寸令牌（`--control-h` / `--hit-target` / `--control-field`…）**目前只在共享
组件层落地**：`components/ui/index.tsx` 的 Button（`h-control` /
`h-control-compact` / `h-hit-target`）、Input / Select（`FIELD_H` =
`h-control-compact`）与 ErrorState 重试按钮。Textarea 刻意**不给固定高度**
（随内容自适应），因此复用同一基线但不套高度类。

**页面层尚未收敛**：`features/**` 与 `pages/**` 里仍有 55 处硬编码
`h-7` / `h-8` / `h-9` / `h-10` / `h-11` / `h-12`，分布在 14 个文件里，数值
恰好与令牌巧合相等（如 `h-9`=36px=`--control-h-compact`），所以视觉无回归，
但**改令牌不会传导到页面层**。这一层属于后续批次，登记在此以免被误认为已完成。

---

## 3. 应用框架

```
┌─────────────────────────────────────────────────────────┐
│ EpisodeContextBar  项目 → 季 → 集 → 镜 + 队列/GPU/磁盘  │  常驻
├──────────┬──────────────────────────────┬───────────────┤
│ 导航器   │        媒体舞台              │   检查器      │
│ 285px    │        1fr                   │   350px       │
│ (可收)   │   bg-media，比 UI 更深        │  意图/参数/   │
│          │                              │  候选/参考    │
├──────────┴──────────────────────────────┴───────────────┤
│ AI 总控：**可切换抽屉**（快捷键呼出），非常驻右栏       │
└─────────────────────────────────────────────────────────┘
```

三栏的由来：短剧工作台最需要的右侧栏是**镜头检查器**，不是 AI 总控。
AI 总控从常驻右栏降为抽屉，保留轨迹可视化与急停开关，但不再挤占检查器空间。

**响应式三档**：

| 断点 | 布局 |
|---|---|
| ≥1440 | 三栏全展开 |
| 1280 | 检查器收窄，导航器可折叠 |
| ≤1024 | 单列，检查器转为抽屉 |

---

## 4. 组件规则

### 4.1 按钮语义四层（**不可混用**）

| 层级 | 类 | 用法 |
|---|---|---|
| `.primary-action` | 每屏唯一 | 当前屏的主创作动作，一屏最多一个 |
| `.secondary` | 可重复 | 次要动作 |
| `.danger-action` | 红边红字 | **破坏性动作**：删除项目、重置整集、重置资产 |
| `.ghost-action` | 无边框 | 工具栏、取消 |

> **破坏性动作绝不与只读动作共享轮廓。** 项目里有"删除项目（移入 `_trash`）"
> "重置整集""重置资产"这类工具，红边红字使 删除/归档 不被误点成 查看。

### 4.2 状态表达三重律

状态**不得只靠颜色**。每个状态徽标必须同时具备：

1. **色**（语义令牌）
2. **文本**（明确的中文状态词）
3. **图标或形状**

改造前 `state-*` 只有颜色，且只有 `role="alert"` 一处。

### 4.3 七态页面状态

每个数据区域必须实现：`normal` / `loading` / `empty` / `error` / `blocked` /
`running` / `success`。刷新时**保留旧数据**（不闪空）。

### 4.4 表单状态机

| 属性 | 样式 |
|---|---|
| `[aria-busy="true"]` | 自带 spinner，光标 `progress` |
| `[aria-invalid="true"]` | 红边 + 红字说明 |
| `[readonly]` | 虚线边框（区别于 disabled 的灰化） |
| `disabled` | 降透明 + `not-allowed` 光标 + **必须配相邻原因文案** |

---

## 5. 无障碍

- **焦点可见**：base 层 `:focus-visible` 统一兜底（项目里仍有约 50 处原生
  button/input/select/textarea 未补 focus 样式，逐个补不现实）。
- **减少动效**：`prefers-reduced-motion: reduce` 下所有动画停止或显著放慢。
- **触达区**：纯图标触达区 ≥ 44×44。
- **对比度**：正文 ≥ 4.5:1。⚠️ 不要拿 `--success` 等主色直接当文字色
  （浅底上仅 ~2.5:1），用 `-strong` 档。

---

## 6. Interaction invariants（14 条，祈使句形式）

> 格式照抄自对标项目的 quick-create 页面规范。这些是**可执行的**不变量 ——
> 每条都在 `scripts/audit_tokens.py` 或代码 review 中被检查。

### 身份与决策

1. **Never silently fall back.** 降级必须告知用户，不得静默。
2. **Never collapse "selected" into "approved".** 采用不等于批准，永不自动升级。
3. **Never show a machine check as human approval.** 机器检查 ≠ 人工批准。
4. **Never mark a preview artifact as deliverable.** 预演产物永不可交付。

### 破坏性动作

5. **Never style a destructive action like a read-only action.** 破坏性动作必须红边红字。
6. **Never disable without a reason.** disabled 必须带相邻原因或恢复链接。
7. **Never delete without confirming the target.** 破坏性确认必须指名对象。

### 状态与反馈

8. **Never rely on color alone.** 状态必须色 + 文本 + 图标三重表达。
9. **Never blank the view during a refresh.** 刷新保留旧数据，不闪空。
10. **Never leave an error without a next action.** 报错必须给原因 + request ID + 重试 + 诊断链接。

### 布局与导航

11. **Never lose selection state on navigation.** 同一集的 view / shot / filters /
    selection / scroll 保持在 URL 或 session。
12. **Never let a raw router change silently break old deep links.** 改路由必须留 301 层并保留
    query/hash。
13. **Never require keyboard-only knowledge to be undiscoverable.** 快捷键必须有可见提示。
14. **Never use `transition-all` on long lists.** 长列表只动 transform/opacity。

---

## 7. 审计账本（可追溯的设计债务）

每个设计修正都记录度量数字 + 失败原因 + 修正选择。这是 `density.css` 做法，
让设计债务可回归验证而不只是"听说改好了"。

| 度量 | 发现于 | 修正 |
|---|---|---|
| 188 处裸 `border` 依赖 v3 默认 gray-200 | tailwind.config.js 注释 | **锁 v3 不迁 v4**（v4 改成 currentColor 会全站回归） |
| 27 处 ring/outline-none 在 v4 已改名 | 同上 | 同上 |
| 619 处 `dark:` 类不可达（`.dark` 从未注入） | 2026-09 审计 | 整体移除，改为 token 覆盖 |
| 86 个控件高度不足 24px | 2026-10-07 审计 | 统一 `--control-h: 40px` / 紧凑 36px |
| 197 个 disabled 按钮与 enabled 视觉不可区分 | 同上 | 降透明 + not-allowed + 必配原因 |
| 1,324 个按钮全程停在 12px | 同上 | 七档字号规范 |
| 42 个镜头 × ~660px = 28,800px 轨道塞进 2,285px 外壳且无滚动条 | 同上 | 长列表 > 50 项虚拟化 |
| `.glass` 用于长列表导致逐帧重绘 | 同上 | 降级重命名为 `.glass-chrome` |
| `state-done` 同时表达"采用"与"批准" | 同上 | 拆 `--selected` / `--approved` 两档 |

---

*本文档由 2026-10-07 对标 LocalDramaStudio 改造抽出。此前规范全部埋在
`index.css` 注释里 —— 写得认真，但工具读不到。现在令牌在 CSS、规则在本文件、
校验在 `scripts/audit_tokens.py`、决策在 `docs/decisions/ADR-*.md`。*
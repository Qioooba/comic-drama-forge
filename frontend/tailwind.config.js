/** Tailwind 配置（v3，与原先 CDN 版本保持一致，避免 v3→v4 的视觉回归）
 *
 * 背景：此前 index.html 通过 <script src="https://cdn.tailwindcss.com"> 在运行时
 * 现场生成工具类，构建产物里 0 个 Tailwind 类。后果：
 *   1) 断网 / 内网 / Electron 离线打包 → 整个界面失去样式；
 *   2) 每个页面加载都要在浏览器里做一次 JIT 扫描；
 *   3) Tailwind 官方明确禁止在生产环境使用该 CDN。
 * 这里改为构建期编译：postcss + tailwindcss(v3) 产出静态 CSS。
 *
 * 刻意选择 v3 而非原先 devDependencies 里的 v4：本项目有 188 处裸 `border`
 * （v3 默认 gray-200、v4 变成 currentColor）与 27 处 ring/outline-none
 * （v4 已重命名），直接迁 v4 会造成整站视觉回归。v3 能 1:1 复刻既有观感。
 *
 * ── 2026-09-23 设计体系收敛 ──
 * theme.extend 里新增的语义色**全部指向 index.css 的 CSS 变量**，
 * 让 token 成为唯一真源：改一处变量即可整站生效，组件层不再写死 hex/rgba。
 *   canvas  → 页面画布     surface / surface-2 → 卡片与次级区块
 *   line    → 描边         ink-1/2/3           → 三级文字
 *   brand   → 品牌主色     accent              → 青蓝强调
 *   action  → 主按钮实心底
 *   success / warning / danger / info → 通用语义色（-subtle 浅底 / -strong 深字）
 *   state-* → 生产状态语义色（pending/running/done/failed/skipped/attention）
 * 圆角 / 阴影 / 字号 / 动效时长同样在此统一，避免各页自带一套数值。
 *
 * ⚠️ 颜色统一写成 `rgb(var(--x) / <alpha-value>)`：
 *    index.css 里的 token 存的是 RGB 三元组，靠 `<alpha-value>` 占位符才能让
 *    `bg-brand/10`、`ring-brand/40` 这类透明度修饰符产出合法 CSS。
 *    写成 `var(--x)` 的话透明度修饰符会静默失效（生成无效声明）。
 * ⚠️ 这里的 borderRadius / fontSize / boxShadow 是**覆盖** Tailwind 默认值：
 *    既有页面用到的 rounded-lg、text-base、shadow-sm 等会随之改变观感，
 *    这是收敛为单一设计语言的预期代价，不是回归。
 *
 * 注：package.json 未声明 "type": "module"，故此处使用 CommonJS 写法，
 * 避免 Node 每次都要重新解析模块类型产生告警。
 */

/** 把「--token」映射成支持透明度修饰符的 Tailwind 颜色 */
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

module.exports = {
  // ⚠️ 刻意**不设** darkMode（2026-10-07 修正）。
  //   原先写的是 `darkMode: 'class'`，但注入 <html> 的类名是 `.light`（浅色皮肤），
  //   深色是 `:root` 默认 —— 项目里**不存在** `.dark` 类，该配置纯属误导：
  //   它让人以为「主题靠 dark: 变体类切换」，而真实机制是 index.css 的
  //   `:root` / `:root.light` **变量覆盖**。
  //   不设 darkMode 时 Tailwind 默认 'media'；一旦有人写下 `dark:` 类，
  //   产出的是 `@media (prefers-color-scheme: dark)`（会真的生效、跟着系统走），
  //   而不是像 'class' 那样悄悄失效 —— 失效比响亮地错好排查。
  //   且 `scripts/audit_tokens.py` 的 DARK001 会直接拦下任何 `dark:` 类。
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['PingFang SC', 'Microsoft YaHei', 'system-ui', 'sans-serif'],
        // 编辑性标题（项目名 / 集名）：与正文分族，给「作品」一点编辑感
        serif: ['Georgia', 'Songti SC', 'SimSun', 'serif'],
        // 数值与技术值：seed / 时长 / 哈希 / 路径。等宽让数字等宽可逐位核对
        mono: ['Cascadia Code', 'SFMono-Regular', 'Consolas', 'monospace'],
      },
      colors: {
        canvas: token('bg-canvas'),
        surface: {
          DEFAULT: token('bg-surface'),
          2: token('bg-surface-2'),
        },
        // 媒体舞台面：比 canvas 更深，用于图片/视频查看器与候选对比
        media: token('bg-media'),
        line: {
          DEFAULT: token('border'),
          strong: token('border-strong'),
        },
        ink: {
          1: token('text-primary'),
          2: token('text-secondary'),
          3: token('text-tertiary'),
        },
        brand: {
          DEFAULT: token('brand'),
          hover: token('brand-hover'),
          subtle: token('brand-subtle'),
        },
        accent: {
          DEFAULT: token('accent'),
          subtle: token('accent-subtle'),
        },
        action: token('action'),
        // ⚠️ 数据可视化专用（SVG / 图表分类色），UI 层不要引用，详见 index.css
        viz: {
          rose: token('viz-rose'),
          violet: token('viz-violet'),
          teal: token('viz-teal'),
          amber: token('viz-amber'),
        },
        success: {
          DEFAULT: token('success'),
          subtle: token('success-subtle'),
          strong: token('success-strong'),
        },
        warning: {
          DEFAULT: token('warning'),
          subtle: token('warning-subtle'),
          strong: token('warning-strong'),
        },
        danger: {
          DEFAULT: token('danger'),
          subtle: token('danger-subtle'),
          strong: token('danger-strong'),
        },
        info: {
          DEFAULT: token('info'),
          subtle: token('info-subtle'),
          strong: token('info-strong'),
        },
        // ⚠️ 「采用 ≠ 批准」两档**独立**语义色（2026-10-07 新增）。
        //   selected = 创作决定（这版被采用）；approved = 放行决定（人工批准）。
        //   不变量：selected 永不显示为 approved，组件层不得混用两档的色/文案/图标。
        //   原先 `state-done` 同时承担这两种含义，是「为什么这镜用了这一版」
        //   无法回答的视觉根因，故拆开。
        selected: {
          DEFAULT: token('selected'),
          subtle: token('selected-subtle'),
          strong: token('selected-strong'),
        },
        approved: {
          DEFAULT: token('approved'),
          subtle: token('approved-subtle'),
          strong: token('approved-strong'),
        },
        state: {
          pending: token('state-pending'),
          'pending-subtle': token('state-pending-subtle'),
          'pending-strong': token('state-pending-strong'),
          running: token('state-running'),
          'running-subtle': token('state-running-subtle'),
          'running-strong': token('state-running-strong'),
          done: token('state-done'),
          'done-subtle': token('state-done-subtle'),
          'done-strong': token('state-done-strong'),
          failed: token('state-failed'),
          'failed-subtle': token('state-failed-subtle'),
          'failed-strong': token('state-failed-strong'),
          skipped: token('state-skipped'),
          'skipped-subtle': token('state-skipped-subtle'),
          'skipped-strong': token('state-skipped-strong'),
          attention: token('state-attention'),
          'attention-subtle': token('state-attention-subtle'),
          'attention-strong': token('state-attention-strong'),
        },
      },
      borderRadius: {
        sm: '6px',   // 控件（按钮 / 徽标）
        DEFAULT: '8px', // 字段（input / select / textarea）—— 原先缺这一档
        md: '10px',  // 面板
        lg: '12px',  // 容器
        xl: '16px',  // 大容器（弹窗等）
      },
      // 控件高度与触达区（§5.4）。改造前有 86 个控件不足 24px，
      // 纯图标按钮的触达区小于 24px —— 这类目标在触屏与桌面都难以稳定命中。
      height: {
        control: 'var(--control-h)',         // 40px 标准
        'control-compact': 'var(--control-h-compact)', // 36px 紧凑
        'hit-target': 'var(--hit-target)',   // 44×44 纯图标触达区
      },
      minWidth: {
        'hit-target': 'var(--hit-target)',
      },
      maxWidth: {
        measure: 'var(--measure)',           // 70ch 正文可读宽度
        shell: 'var(--shell-max)',           // 2100px 外壳上限
        'control-field': 'var(--control-field)',
        'control-field-lg': 'var(--control-field-lg)',
      },
      // 4px 基网格：4/8/12/16/24/32/48
      spacing: {
        1: 'var(--space-1)',
        2: 'var(--space-2)',
        3: 'var(--space-3)',
        4: 'var(--space-4)',
        6: 'var(--space-6)',
        8: 'var(--space-8)',
        12: 'var(--space-12)',
      },
      // 阴影指向 index.css 的 --shadow-* 变量：浅色取值与原先写死的完全一致，
      // 深色主题在 :root.dark 里覆盖为纯黑高不透明度（token 唯一真源）。
      boxShadow: {
        xs: 'var(--shadow-xs)',
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)',
      },
      fontSize: {
        // 七档，与 design-system MASTER.md 的字号规范一致
        xs: ['12px', '18px'],
        sm: ['13px', '20px'],
        base: ['14px', '22px'],
        lg: ['16px', '24px'],
        // 原先 xl=20 / 3xl=30 两档偏大且与正文层级拉不开；按 §5.4 对齐为
        // 18 / 24 / 32（面板标题 18、页面标题 24、概览大标题 32）
        xl: ['18px', '28px'],
        '2xl': ['24px', '32px'],
        '3xl': ['32px', '40px'],
      },
      // ⚠️ 刻意**不再**定义 fontVariantNumeric.tabular（2026-10-07 修正）。
      //   它产出的是 `.tabular` 工具类，但全仓 13 处等宽数字用的都是 Tailwind
      //   **内置**的 `tabular-nums`，`.tabular` 零命中 —— 死配置。
      //   死配置比没有配置更糟：它让 MASTER「必须加 tabular 类」这句话
      //   看起来有落地，实则没人用。文档已改为要求 `tabular-nums`。
      //   （tailwind.config.js 现在被 audit_tokens.py 的 HEX001 真实覆盖，
      //     因为扫描根已从 frontend/src 扩到 frontend/。）
      transitionDuration: {
        DEFAULT: '160ms',
      },
      zIndex: {
        sticky: 'var(--z-sticky)',
        dropdown: 'var(--z-dropdown)',
        drawer: 'var(--z-drawer)',
        modal: 'var(--z-modal)',
        toast: 'var(--z-toast)',
        preview: 'var(--z-preview)',
      },
    },
  },
  plugins: [],
};

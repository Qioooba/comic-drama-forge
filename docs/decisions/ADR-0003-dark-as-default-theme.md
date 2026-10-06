# ADR-0003：深色为默认主题，浅色降为可选皮肤

- 状态：**已采纳**（2026-10-07 实施）
- 相关：`frontend/src/index.css`、`design-system/comicdrama/MASTER.md`

## 背景

项目原本是「浅色为主 + 深色覆盖层」：`:root` 是浅色，`:root.dark` 由
`ThemeContext` 注入 `.dark` 类切换，默认跟随系统。同时存在三类装饰：

| 项 | 位置 | 问题 |
|---|---|---|
| `.glass` / `.glass-strong` | `index.css` backdrop-blur(18–24px) | 长列表滚动时逐帧重绘，帧率崩 |
| `body::before` 科技感辉光 + 64px 网格 | 铺满视口，两团 radial-gradient | 持续视觉噪声 |
| `.text-gradient` / `.glow-brand` | 品牌→青渐变文字 / 品牌外发光 | 纯装饰；且渐变标题在深浅两套皮肤都需额外 fallback |

## 决策

### 1. 深色提升为默认，浅色降为可选皮肤

`:root` 承载**深色工作站**取值，浅色移入 `:root.light`。`ThemeContext` 改为
只在 `resolved === 'light'` 时加 `.light` 类；`index.html` 的防闪烁脚本同步反转。

**理由**：这是长时间盯屏的创作工作台 —— 典型单次会话 30 分钟到数小时，逐镜审片。
浅底 + 白卡片 + 辉光叠在一起，在这个时长下是**持续的**视觉噪声。
深色 + 低眩光 + 靠描边分层更适合长时间工作，也更省眼。

**不是一刀切删浅色**：浅色仍是完整可用的一套皮肤，用户可随时切。

### 2. 装饰降级与删除

| 原类 | 处置 | 理由 |
|---|---|---|
| `.glass` | 重命名 `.glass-chrome` | 名字收窄后，"误用在长列表上"在 review 时一眼可辨 —— 靠人记住的规矩守不住 |
| `.glow-brand` | **删除** | 与「运行中」状态徽标争夺注意力；真状态已有 `state-running` 专用色 |
| `.text-gradient` | **删除** | 标题应有稳定纯色底；渐变标题在两套皮肤上都需 fallback |
| `.progress-fill` 渐变 | **改品牌单色** | 进度是**信息**不是装饰；渐变导致不同进度下颜色不可比 |
| `body::before` 辉光 | 降级为 `data-skin="tech"` opt-in | 氛围不是夜店；且它唯一的正当理由（给 `.glass` 提供可模糊内容）已随 `.glass` 降级消失 |

### 3. 必须完整保留的四件事

1. **RGB 三元组 + `<alpha-value>` 占位符** —— 令牌存 hex 会让 `bg-brand/10`
   产出**无效 CSS 而静默失效**。这是全项目最有价值的样式工程约束。
2. **主题走令牌覆盖，组件层零 `dark:` 变体** —— 本轮清掉 8 处真实存在的
   `dark:text-amber-600` 之类（这些变体此前**不可达**：`.dark` 从未在组件层生效）。
3. **`-subtle` / `-strong` 分档** —— 不要拿 `--success` 主色直接当文字色（浅底上仅 ~2.5:1）。
4. **`prefers-reduced-motion` + `:focus-visible` base 层兜底** —— 项目仍有约 50 处
   原生 button/input 未补 focus 样式，base 兜底是极划算的做法。

## 后果

**正面**
- 长时间使用的视觉负担下降；
- 装饰类从 4 个降到 1 个（`.glass-chrome`），且命名自带使用边界；
- 新增 `selected` / `approved` 两档后，状态语义在视觉上可区分。

**负面**
- 浅色用户（若系统偏好浅色且未手动设置）首次打开会看到深色 —— 这是**有意的默认值变更**；
- `:root` 与 `:root.light` 两套令牌必须同步维护，漏一个 = 该语义在浅色下静默沿用深色值。
  已由 `scripts/audit_tokens.py` 的 `TOKEN001` 静态校验兜住。

## 配套门禁

`scripts/audit_tokens.py` 把规范变成可执行检查（CI 用 `--strict`）：

| 规则 | 内容 | 本轮实测 |
|---|---|---|
| `HEX001` | 组件层禁止 hex/rgba 字面量 | PASS |
| `DARK001` | 组件层禁止 `dark:` 变体 | 修掉 8 处后 PASS |
| `TRANS001` | 长列表禁用 `transition-all` | 收敛 21 处后 PASS |
| `TOKEN001` | 深浅两套皮肤令牌集对齐 | PASS |

配套 `scripts/fix_transition_all.py` 做批量收敛（并跳过确属 transform/opacity 语义的 5 处）。
# ADR-0013：设计令牌与交互状态 —— 组件层持有 spinner / 表单状态机 / 决策徽标

- 状态：**已采纳**（2026-10-07 实施，W6 设计系统）
- 相关：`frontend/src/index.css`、`frontend/tailwind.config.js`、
  `frontend/src/components/ui/index.tsx`、`design-system/comicdrama/MASTER.md` §4.4/§4.5、
  ADR-0002（采用 ≠ 批准）、ADR-0003（深色默认）、ADR-0011（令牌审计门禁）

## 背景

对标评估 §5.3b/§5.4 指出三条缺口：① 共享组件缺 `aria-busy` / `aria-invalid`
表单状态机，各页面各自手写且常漏；② 控件高度需 44px / 36px 两档令牌化；
③ 状态表达必须「色 + 文本 + 图标」三重，而 selected / approved 两档决策色
没有共享组件承载。2026-10-07 首轮改造已落地令牌与 Button，但静态复核发现
base 层与组件层的职责划分仍有三处实错（见 MASTER §7 审计账本）。

## 决策

1. **spinner 由组件层持有，base 层只给「忙」语义。**
   `[aria-busy='true']::after` 伪元素对 input/select/textarea（替换元素）不渲染，
   对自带 spinner 的 Button 会叠成双转圈，且配套的 `color:transparent` 被
   utilities 层 `text-ink-*` 覆盖而从未生效。修正后 base 层仅保留
   `cursor: progress`；Button / Input / Select / Textarea 各自渲染真实 spinner
   节点（`FieldSpinner`），字段用 `busy` prop 驱动 `aria-busy`。

2. **表单状态机完整落在共享组件。**
   Input / Select / Textarea 统一：`error` → `aria-invalid` + 红边 +
   `aria-describedby` 关联原因文案；`busy` → `aria-busy` + spinner；
   焦点环收敛为 `focus-visible:`（点击不闪环，键盘 Tab 必然可见）。
   Select 此前完全没有 error 态，本轮补齐（与 Input/Textarea 同款）。

3. **决策徽标两档独立，三重律组件化。**
   `DecisionBadge` 固化 ADR-0002 的 UI 契约：selected（青，单勾）与
   approved（绿，印章勾）各自持有 `-subtle` / `-strong` 令牌类、固定图标与
   必填 `label`。`label` 必填的原因：语言包尚无 `state.selected` /
   `state.approved` 键，而 `t()` 缺键会原样返回 key；宁可把「补语言包」登记为
   债务，也不把 key 字符串渲染给用户。语言包补齐后可改为可选并回落 `t()`。

4. **字体令牌唯一真源回 `index.css`。**
   `tailwind.config.js` 的 `fontFamily` 改为引用 `var(--font-sans/serif/mono)`，
   消除与 CSS 令牌的字面量漂移（上一版 tailwind sans 少了
   -apple-system / BlinkMacSystemFont 等回退项）。

5. **reduced-motion 对转圈「放慢」而非「停止」。**
   `.animate-spin` 在 `prefers-reduced-motion: reduce` 下降到 2.4s：完全停转
   会丢掉「正在处理」的信号，前庭敏感用户要的是降低频闪，不是信息消失。

## 后果

**正面**：提交中 / 校验失败 / 只读 / 失焦可见在全站只有一处实现；
selected / approved 的视觉区分不再依赖调用方自觉；令牌漂移面缩小。

**代价**：新增 props（`busy` / `error` / `disabled`）需后续接入调用点
（本轮按契约只改设计系统层，features 归属其他文件所有权）；
`DecisionBadge` 在语言包补齐前必须显式传 label。

## 验证方式（静态，本轮不运行）

- `python scripts/audit_tokens.py`：HEX001 / DARK001 / TOKEN001 应保持 PASS
  （本 ADR 的改动不引入 hex 字面量 / dark: 变体 / 令牌集分叉）；
- 人工 review：`ui/index.tsx` 无色值字面量、无 `dark:`；决策徽标两档类/图标/文案互斥。

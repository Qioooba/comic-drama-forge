# ADR-0011：设计规范文档化 + 自动化校验

- 状态：**已采纳**（2026-10-07 实施）
- 决策者：项目维护者
- 相关：`design-system/comicdrama/MASTER.md`、`design-system/comicdrama/pages/{quick-create,diagnostics}.md`、`scripts/audit_tokens.py`、`scripts/fix_transition_all.py`

## 背景

§P2-8 的第一件事：**把不变量写成可测试的祈使句**。

参照物是自研 `pages/quick-create.md` 里的 "Interaction invariants" 14 条，
形如 «Never silently fallback or collapse selection into approval» ——
**祈使句、否定式、可判定**。而 comic 的不变量散在 MASTER 式的长篇注释与
README 表格里，形态是「应���」「建议」「不要」。

这两种写法的差别不在文风，在**可判定性**：

| 写法 | 能不能进 CI |
|---|---|
| 「状态徽标不要只用颜色表达」 | ❌ 「不要」没有边界，脚本无法判定 |
| «Never express status by color alone — always pair color with text and icon» | ✅ 可判定：扫 `.text-\w+` 是否同时有文本节点 |

`apps` 里也确实存在一批**真实违规**，证明「写在文档里」不管用
（见下方实测表）。

## 决策

两层落地，缺一不可：

**第一层：`design-system/comicdrama/MASTER.md` + `pages/{quick-create,diagnostics}.md`**
把不变量写成祈使句，每条给出**为什么**与**怎么判定**。

**第二层：`scripts/audit_tokens.py` 把规则变成 CI 门禁。**

### 关键设计：规范埋在注释里 = 工具读不到

这是本 ADR 的核心判断。写在 `.md` 里的祈使句**给人看**，
写在代码注释里的也**给人看**。只有脚本里的规则能被 CI 强制执行。

所以每条不变量必须落成**两条**：

1. 一条祈使句 —— 写清楚**为什么**（让人在被要求时理解，而不是机械遵守）；
2. 一条可执行规则 —— 写清楚**怎么查**（让 CI 能拦）。

只有 (1) 没有 (2)：规范会随时间腐化，因为没人验证它还被遵守。
只有 (2) 没有 (1)：规则会被当成「门禁挑刺」，然后开始加白名单。

### 规则表（`--strict` 为 CI 模式，违规退出码 1）

| 规则 | 内容 | 状态 |
|---|---|---|
| `HEX001` | 组件层禁止 hex / rgba 字面量（令牌唯一真源） | 硬失败 |
| `DARK001` | 组件层禁止 `dark:` 变体（主题走令牌覆盖） | 硬失败 |
| `TRANS001` | 长列表禁用 `transition-all`（只动 transform / opacity） | 硬失败 |
| `TOKEN001` | 深浅两套皮肤的令牌集必须对齐 | 硬失败 |
| `SEL001` | 提醒：同一文件同时出现 `selected` 与 `approved`，请确认语义未混淆 | **仅警告** |

`SEL001` 刻意**不是**硬失败：ADR-0002 要求 UI 上「采用」与「批准」是**两个独立按钮**，
它们**合法地**出现在同一个文件里。只有语义被**混淆**（比如把 selected 显示成 approved）
才是问题，而这件事脚本判不了。所以它只提醒。

硬报错一个合法写法，会逼人去加白名单——**白名单一旦存在就会被滥用**。
这是规则集设计的通用教训：宁可少一条硬规则，也不要一条会被白名单掏空的硬规则。

配套 `scripts/fix_transition_all.py` 做批量收敛，并**跳过确属
transform/opacity 语义的 5 处**（自动改写不等于无脑替换）。

### 三条必须完整保留的样式工程约束

（详见 ADR-0003，此处只列它们为什么要有门禁）

1. **RGB 三元组 + `<alpha-value>` 占位符** —— 令牌存 hex 会让 `bg-brand/10`
   产出**无效 CSS 而静默失效**。全项目最有价值的样式约束。
2. **主题走令牌覆盖、组件层零 `dark:` 变体** —— 本轮清掉 8 处真实存在的
   `dark:` 变体（此前**不可达**：`.dark` 从未在组件层生效）。
3. **`-subtle` / `-strong` 分档** —— 浅底上 `--success` 主色直接当文字色仅约 2.5:1。

## 为什么不选另一条路

**只写 MASTER.md，不做脚本。** 最省事。问题是规范会腐化：违规的人不会去看文档，
看了也会忘。ADR-0003 的实测就是证据——`DARK001` 一跑就出 8 处违规，
`TRANS001` 出 21 处。**这些违规在文档里早就被禁了。**

**只做脚本，不写文档。** 规则能跑，但没人知道**为什么**。结果是门禁被当成障碍，
然后加白名单，然后白名单膨胀，然后规则失效。

**用 ESLint / Stylelint 配置表达规则。** 工具链更标准。但本项目的问题不在
「lint 能力不足」，而在「规则内容不存在」——把同样的规则写进 ESLint 配置，
和维护一份 Python 脚本的工作量一样，且 ESLint 配置对本项目自定义的令牌语义
（`TOKEN001` 的深浅对齐、`SEL001` 的语义提醒）表达力更弱。

**用注释式 lint（`eslint-disable` 满天飞）。** 见上：白名单会被滥用。

## 后果

**正面**

- 规范从「文档」变成**可执行门禁**；
- 令牌唯一真源（`HEX001`）与深浅对齐（`TOKEN001`）由 CI 兜住，
  不再依赖「记得同步维护两套 `:root`」；
- 违规在提交时就暴露，而不是在 review 时靠眼睛；
- 规范条目与门禁规则**一一对应**，可追溯。

**负面 / 代价**

- **多一个必须维护的脚本**，规则集会随设计演进（加令牌、加状态档都要改脚本）。
- 误报需要白名单机制，而白名单机制本身有被滥用的风险（`SEL001` 刻意做成
  仅警告，就是这个原因）。
- `TRANS001` 这类规则依赖「是否长列表」这类语义判断，脚本只能靠路径模式近似，
  所以才有 `fix_transition_all.py` 里那 5 处人工豁免。**规则集不可能完备。**

## 验证方式

```powershell
python scripts/audit_tokens.py --strict   # expect_exit_code 0
```

### 当前门禁状态（2026-10-07 实测）：FAIL，1 处违规

```
[HEX001]    PASS
[DARK001]   PASS
[TRANS001]  FAIL (1)  frontend/src/features/style/StyleGallery.tsx:125
[TOKEN001]  PASS
结果：1 处违规  →  exit 1
```

这一处 `transition-all` 在**长列表缩略图网格**上（`features/style/StyleGallery.tsx`），
属 W4 文件范围，**本 ADR 不代修**。它也说明门禁是活的：`TRANS001` 在 W6 收敛 21 处
之后 PASS，随后 W4 新建的组件又引入 1 处——**静态门禁只对已提交的代码负责，
新代码进来就会被拦**。这正是它存在的意义。

修法二选一（由 W4 决定）：改成 `transition-[transform,opacity]`；
或按 `fix_transition_all.py` 的既有约定确认属 transform/opacity 语义后豁免。

## 关联决策

- ADR-0003 深色为默认主题（`HEX001` / `DARK001` / `TOKEN001` 是该 ADR 的兜底）
- ADR-0002 采用 ≠ 批准（`SEL001` 提醒 selected / approved 语义未混淆）
- ADR-0009 前端真路由（规则集同样约束新路由层的写法）
- `docs/release/g0-contract.json`（`quality_gates.token-audit` 是本 ADR 的落点）

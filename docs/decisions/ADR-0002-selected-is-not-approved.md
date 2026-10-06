# ADR-0002：「采用 ≠ 批准」—— 生成 / 采用 / 批准三态分离

- 状态：**已采纳**（2026-10-07 实施）
- 相关：`app/domain/production_facts.py`、`design-system/comicdrama/MASTER.md` §2.4

## 背景

改造前的「生成事实」只有两样东西：**磁盘上的产物文件** 和 `quality_stage` 上
绑哈希的四层质量状态（A technical_render / B content_qa / C editorial_review /
D release_approval）。

这导致三个具体问题：

1. **无法回答「这镜为什么用这一版」**。候选比较（best-of-N、九宫格选格）没有统一模型，
   「采用」只能隐含在"磁盘上现在这个文件"里。
2. **重跑覆盖文件即丢历史**。没有版本概念，上一版还在不在完全取决于有没有手动备份。
3. **「采用」与「批准」被压成一档**。`state-done` 一个绿档同时表达"我选了这版"
   和"我批准放行"，但这两件事**性质完全不同**：前者是创作决定，后者是放行决定。

第 3 点最危险：机器检查通过 ≠ 人工批准。混淆二者会让「质检全绿」被误读成
「可以交付」，而实际上从来没有人看过第 37 集的第 12 镜。

## 决策

引入四类实体 + **两条独立记录**：

| 实体 | 作用 | 可变性 |
|---|---|---|
| `GenerationIntent` | 这一次生成想做什么：提示词、参考图槽位、seed、Profile、Workflow 版本 | **冻结后不可改**，改动必须派生新 intent |
| `MediaVersion` | 一次生成产出的候选，带父 intent、sha256 指纹、ffprobe 事实 | 追加，不修改 |
| `SelectionDecision` | **采用**：这版被选为工作版本 | 追加 |
| `ApprovalDecision` | **批准**：人工放行，可进交付 | 追加，可 revoke |
| `CapabilityProfileVersion` | 能力档案版本化 | 追加 |

### 铁律（已在代码里 fail-closed）

1. **采用永不隐式升级为批准。**
   `approve()` 强制要求 `selection_id`（必须先有对应采用记录）+ 三元组哈希绑定；
   拒绝 `system:` / `bot` / `auto` 等机器账号作为授权者。
   读侧 `decision_state` **恒返** `selection_implies_approval=False`。

2. **批准绑定内容哈希。**
   `bound_hash = sha256(media_sha256 + intent_hash + selection_hash + scope + authorized_by)`。
   产物内容一变，批准**自动失效**（哈希不匹配）。这防止"批准过 A，交付了 B"。

3. **Intent 冻结不可原地改。**
   `derive_intent()` 只允许 9 个内容字段；仓库层**没有 UPDATE 入口**；
   路由表**没有 PUT/PATCH**。改意图 = 派生新 intent，保留完整血缘。

4. **TimelineRevision 不可变。**
   仓库层只 INSERT，DELETE/PUT/PATCH 一律 405。

5. **合法静音必须显式声明。**
   `undeclared_silence` 在**预检阶段**阻断，而不是渲染后才发现
   （借鉴自研 `RenderManifest.declare_silence`）。

## 为什么哈希用满 64 位

`comfyui_job_store.workflow_hash` 历史上截断到 32 位。本轮新增的
`intent_hash` / `bound_hash` 用**满 64 位**。

理由：`bound_hash` 是法律意义上的"这一版是谁批准的"。32 位只有约 43 亿种可能，
**截断后可被人为构造碰撞** —— 构造一个与已批准产物哈希相同的新文件，
就能绕过批准闸门。这不是理论洁癖，是授权门禁的前提。

> 遗留：`workflow_hash` 的 32 位口径未统一（不在本轮文件范围内）。
> 建议后续统一到 64 位，见遗留议题。

## UI 侧同步

状态语义色从 6 档扩到 8 档，新增**独立**的：
- `--selected` 青 `#36D6C5` = 采用
- `--approved` 绿 `#50D890` = 批准

Shot Studio 的「采用」与「批准」是**两个独立按钮**，不是同一个。
且按 MASTER §4.2 三重律，状态必须色 + 文本 + 图标表达，不能只靠颜色。

## 后果

**正面**
- 能回答「这镜为什么用这一版」；
- 候选比较有统一模型；
- 重跑保留历史版本；
- 「质检全绿」与「可交付」在数据模型与视觉上都不再混淆。

**代价**
- 每次生成多写 2–3 条记录；
- 批准需要额外的显式动作（多一步操作）—— 这是**有意的摩擦**，用摩擦换误交付。

## 关联决策

- ADR-0001 Blueprint 拆分
- ADR-0004 契约优先
- ADR-0006 交付包需 SHA-256 与人工批准
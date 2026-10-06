# 改造总览 · 2026-10-07（对标 LocalDramaStudio）

> 一轮并行改造的**决策与门禁落盘**。8 个 worker 在文件所有权互斥的前提下各自落地
> （见 `docs/decisions/改造契约_并行协调书.md`），本文件是这一轮的**收口视图**。
>
> 范围声明：本轮**只实现 + 审核，不写测试**（协调书 R4）；验证全部为**静态检查 / 只读诊断**，
> 铁律「不启动服务、不连 ComfyUI、不碰 GPU」（R3）在本轮所有产物里都守住了。

---

## 1. 本轮做了什么

| 域 | 交付 | ADR |
|---|---|---|
| 地基 | `app/app.py` 16,844 → 105 行，10 个 Blueprint + `pkgutil` 自动发现注册 | ADR-0001 |
| 语义 | 生成意图 / 媒体版本 / **采用** / **批准** 四类实体；`selected ≠ approved` | ADR-0002 |
| 视觉 | 深色默认、浅色降为可选皮肤；装饰类 4 → 1 | ADR-0003 |
| 契约 | OpenAPI 3.1 导出 + 生成客户端 + 过期门禁 | ADR-0004 |
| 任务 | 三套任务语义收敛为 Job / Attempt | ADR-0005 |
| 交付 | 交付预设 + SHA-256 + 人工批准闸门 | ADR-0006 |
| 合规 | 模型 / 素材 / 品牌授权登记，fail-closed | ADR-0007 |
| 风格库 | 61 风格 + 8 画幅唯一真源化到后端 | ADR-0008 |
| 前端 | 真路由 + 旧 hash 301 退役层；TanStack Query 收敛轮询 | ADR-0009 / ADR-0010 |
| 规范 | 祈使句不变量 + `audit_tokens.py` 变成 CI 门禁 | ADR-0011 |
| 后期 | 不可变时间线 `TimelineRevision` + 合法静音前置声明 | ADR-0012 |
| 音频 | 频谱 + 波形 → 多模态语义复核（AI 质检层） | — |
| 门禁 | `docs/release/g0-contract.json` 版本化规格 | 本文件 §3 |

---

## 2. 这一轮的核心不变量（跨 agent 共同遵守）

1. **采用 ≠ 批准**（协调书 §4）。机器校验（`verified`）永不隐式升级为人工批准（`approved`）。
   读侧恒返 `selection_implies_approval=False`；UI 上是两个**独立按钮**、两档**独立令牌**
   （青 `--selected` / 绿 `--approved`）。
2. **批准绑定内容哈希**。产物内容一变，批准**自动失效**——且失效判定按**磁盘现状重算**，
   不是清单基线。
3. **fail-closed 优于猜**。未登记即拦住；`minimax-h3` 宁可 `verified:false` 也不猜
   ——「不知道」必须可表达。
4. **生成物禁止手改**。改契约的唯一流程是 改后端 → 重跑 `generate_client.py`。
5. **磁盘产物为准**。这是唯一在状态库损坏时仍然成立的续跑判据，收敛语义时**必须保留**。
6. **URL 逐字不变**。前端 1508 行靠字符串拼 URL，改一个字符就 404。

---

## 3. 门禁：`docs/release/g0-contract.json`

把门禁从 `HANDOFF_P0_20261006.md`（49 KB 人工叙述）变成**可 diff 的数据**。
顶层结构：

```
spec_version / contract_version / openapi_version
w3_contract_and_delivery      ← W3 原样保留（契约 / 交付 / 授权）
spec_note / generated_artifacts_note / quality_gates_note
required_components           27 项，按 owner(W1..W8) + adr 标注
generated_artifacts           6 项，全部 hand_editable:false
required_schema_versions      facts_repo / timeline_repo / jobs_repo / style_catalog / delivery / model_licensing
migration_heads               expected_heads（库内登记表）+ constant_heads（常量式）
quality_gates                 5 道门禁，含可执行 steps
decision_index                决策 → 门禁反向索引
reserved_for_w8               pending_components(6) + open_items(7)
```

### 门禁命令

| id | 命令 | 实测 |
|---|---|---|
| `url-contract` | `MJSCXT_AUTOPILOT=0 python scripts/snapshot_routes.py BASE --diff NOW` | 未跑（需路由基线快照） |
| `token-audit` | `python scripts/audit_tokens.py --strict` | **FAIL**（1 处，属 W4） |
| `generated-fresh` | `python scripts/check_generated_client.py` | PASS |
| `frontend-types` | `cd frontend && npx tsc --noEmit` | **FAIL**（15 处，属 W3） |
| `readonly-diagnose` | `MJSCXT_AUTOPILOT=0 python scripts/diagnose_readonly.py` | PASS（exit 0） |

> `MJSCXT_AUTOPILOT=0` **必设**：否则 `import app` 会触发 autopilot 托管恢复，
> **会真的续跑生产**（协调书 §5）。

---

## 4. 只读诊断链

新增 `scripts/diagnose_readonly.py`（对应评估文档 §P2-8 第 4 件）。
以 SQLite `mode=ro` URI + `PRAGMA query_only=1` **双保险**打开 5 个库，
打印各域登记状态与 `schema_version`。

**它刻意在最后打印「诊断不等于验收」**，并逐条列出它替代不了什么：
授权门禁判定、人工批准有效性、`workflow_hash` 免重渲判据、成片质量。
真正的验收是上面那 5 道门禁。

**库不存在 / 被锁 / 表缺失一律只报告、不阻断，退出码恒为 0** ——
诊断工具不该因「数据还没生产出来」而失败，否则在 CI 里会变成噪音。

---

## 5. 已知问题与未完成项

> **当前工作树收口复核**：以下表格保留首轮门禁记录；后续工作树已出现部分修复（路由/features、静态 bundle、生成客户端导出去重、交付 release-check），但本轮审核**没有重跑任何门禁命令**，因此不能把历史 PASS/FAIL 当作当前验收结论。

门禁的首轮诚实结论：**5 道里 2 道 FAIL，两处当时未代修。**

| id | 归属 | 内容 |
|---|---|---|
| `generated-client-duplicate-export` | W3 | 首轮记录：`generated/index.ts` 15 处 `Duplicate identifier`；当前生成器已加入 `sorted(set(...))` 去重，生成物当前也未见重复导出，但未重跑 `frontend-types` / `generated-fresh` 复核 |
| `token-audit-transition-all-stylegallery` | W4 | `StyleGallery.tsx:125` 长列表 `transition-all` |
| `safe-suite-vs-hardware-split` | **无人认领** | §P2-8 第 3 件「安全集 / 真实硬件集分离」本轮**未实现** |
| `workflow-hash-64` | — | `workflow_hash` 32 位截断与 `bound_hash` 64 位口径不一致，可构造碰撞 |
| `shared-py-split` | — | `_shared.py` 当前约 11,144 行（本轮明确不写测试，不具备动它的条件） |
| `timeline-revision-retention` | — | `TimelineRevision` 无清理 / 归档策略 |
| `style-label-migration` | — | `style_id` 与旧 label 路由两条路径并存 |

还有一处**有意的功能倒退**：`minimax-h3` 登记为 `verified:false`，
**商业交付会被 `LIC-LICENSE-UNVERIFIED` 拦下**，直到有人核实其 LICENSE 并更新登记表。
这是 ADR-0007 有意承担的摩擦，不是 bug。

### 前端两块（首轮记录 vs 当前工作树）

首轮记录中 `frontend/src/routes/` 与 `layouts/` 为空；当前工作树已存在
`router.tsx`、`routes/legacyRedirect.tsx`、`routes/routeElements.tsx`、
`layouts/AppShell.tsx`、`layouts/EpisodeContextBar.tsx`，并由 `App.tsx` 消费。
ADR-0010 的 query 层也已存在。**但本轮未运行浏览器/类型门禁**，所以这里只确认源码接线，不宣称运行时验收。

---

## 6. 关联文件

- 决策：`docs/decisions/ADR-000{1..12}-*.md`
- 协作契约：`docs/decisions/改造契约_并行协调书.md`
- 评估依据：`docs/改造评估_对标LocalDramaStudio.md`（§P0-3 / §P0-4 / §P1-5 / §P1-6 / §P2-8）
- 门禁规格：`docs/release/g0-contract.json`
- 设计规范：`design-system/comicdrama/MASTER.md` + `pages/*.md`

---

## 7. 当前收口复核（P0/P1 摘要）

- **P0｜生产事实已接入托管主链（best-effort）**：`app/pipeline.py` 在分镜/视频产物完成后登记 `GenerationIntent` + `MediaVersion`；历史存量产物仍需回填。
- **P1｜契约启动闸门已接线**：`ApiCompatibilityGate` 启动比对 `/api/contracts/version` 与生成物版本/摘要，不一致时阻断写操作。
- **P1｜导出与交付包发布门禁已分两段统一**：生成导出走集级质检与 C/D 哈希绑定，下载/取回再走交付包授权、机器校验、人工批准与磁盘摘要；服务端重新评估授权，客户端不能伪造。
- **P1｜Job/Attempt 已接入整集生产**：`run_episode` 创建 Job/Attempt 并登记步骤单元；`task_store` 继续负责细粒度断点续跑，状态映射仍需持续审计。
- **P0/P1｜TimelineRevision 保持 opt-in**：冻结计划渲染与 `EpisodeRenderVersion` 落库/查询已实现；旧 `generate_final_video()` 仍是默认兼容路径，切换需真实样片验证。

以上为静态审核与 Vite 打包收口；本轮未运行 tsc/pytest/FFmpeg/生产任务，未调用 GPU/ComfyUI。

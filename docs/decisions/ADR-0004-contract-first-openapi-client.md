# ADR-0004：契约优先 —— OpenAPI 导出 + 生成客户端

- 状态：**已采纳**（2026-10-07 实施）
- 决策者：项目维护者
- 相关：`app/contracts/openapi.py`、`scripts/generate_client.py`、`scripts/check_generated_client.py`、`frontend/src/api/generated/**`

## 背景

本项目此前的「契约」是**接口文档 + 运行时穿墙钉**：

- 后端 `app/app.py` 16.8k 行 / 242 条路由，返回结构只有运行时穿墙钉；
- 前端 `frontend/src/api/client.ts` 1,508 行**全靠字符串拼 URL**。

后端一改返回结构，前端只在**用户点击的那一刻**才发现——而且是在用户正在做的
那一集上。这不是「体验不好」，这是**用生产数据做集成测试**。

§P2-8 与 §P1-6 的判断：项目没有一份「可导出的数据」。契约优先的第一步不是写文档，
是先把契约变成**可导出的数据**。

## 决策

`app/contracts/openapi.py` 把接口长度成三段式：

1. **反射**：从 Flask `url_map` 反射出全量路径与方法，叠加*手工补充的* schema，
   产出 OpenAPI 3.1 字典；
2. `GET /api/contracts/openapi.json` 对外导出，前端与 CI 消费同一份；
3. `scripts/generate_client.py` 读它生成 TS 客户端，
   `scripts/check_generated_client.py` 在 CI 查**生成物是否过期**。

### 关键设计：两源并存，且**刻意冗余**

这是本决策最容易被质疑的一点——「既然有反射，为什么还要手工 schema？」答案是
**两者覆盖的是两类完全不同的路由**：

| 源 | 覆盖 | 标注 | 理由 |
|---|---|---|---|
| 反射 | 既有 242 条路由 | `x-contract-source: reflected` | 这 242 条本轮是**纯搬迁**（协调书 R5）。逐条手写 schema 当时写不实，会立刻失真。**先把「键」钉在规格里**（新增路径会被 CI 发现），schema 逐域补 |
| 手工 | `contracts` / `delivery` / `licensing` 三个新域 | `x-contract-source: manual` | 这三条是本轮**新增**的，schema 与实现同时写、同时生效，不会搬迁 |

哪个路径属于手工补充的，这信息本身就是契约：`build_spec` 返回的
`x-manual-paths` 供 CI 断言「手工 schema 必须**真的**覆盖了它声称覆盖的路径」。

### 铁律：生成物禁止手改

**改契约的唯一流程是：改后端 → 重跑 `generate_client.py`。**

理由很直白：手改生成物 = 造出第二份真源。两份真源必然分叉，而分叉方向永远是
「人直接改的那个更省事」。`manifest.json` 里带 `contract_version` 与 `spec_hash`，
前端启动闸门据此判断客户端是否过期。

`frontend/src/api/generated/**` 下 5 个文件全部 `hand_editable: false`。
其中 `client.ts` 的错误脱敏逻辑（`readError`，源自旧 `client.ts:37-40`）随生成物
一并迁移——它是这个文件里唯一「不是类型」的东西，迁移时不能丢。

## 为什么不选另一条路

**纯手工 schema 单源。** 241 条没人写得完。更糟的是：写了却不更新，契约就变成
**第二份谎言**——比没有契约更坏，因为它看起来可信。

**只反射、不生成。** 反射出的 spec 没有 response schema（`openapi.py` 刻意只给
路径 + 方法），据此生成的 TS 只是一堆路径常量，类型全是 `any`。契约优先会落空成
「路径优先」，而路径本来就是前端拼字符串拼出来的——等于什么都没换。

**继续字符串拼 URL（现状）。** 最省事也最贵的选项：破窗成本由用户在点击那一刻支付。

**换 FastAPI 自动拿全量 schema。** 242 端点迁移成本远超收益，且
`agent_core` 的 37 个工具走**进程内 Flask `test_client`**（不走网络、不占端口），
换框架要重做这一层。见 ADR-0001。

**上 swagger-ui / 红oc 作为唯一入口。** 本项目是单机 Electron 工作台，不是对外
API 产品；给内部工具加一层文档站是净成本。导出 JSON 已足够。

## 后果

**正面**

- 契约从「文档」变成**可 diff 的数据**，CI 能判断「这次改动有没有破坏契约」；
- 前端不再手拼 URL，后端改路径会被 `check_generated_client.py` 拦下；
- 新增路由漏写 schema 会被 `contract-coverage` 门禁发现；
- 安全边界清晰：生成器**不 import `app`**（绕开副作用，`app/app.py` 可能半小时
  才完成 import），**不启动服务 / 不连 ComfyUI / 不碰 GPU**。

**负面 / 代价**

- **两源并存本身就是债。** 反射路由的 schema 逐步过期是必然的；`reflected`
  标注至少让它**可见**，但消不掉。真正解决要等每个域拿到完整 schema。
- 生成物入库会增加 diff 噪音（每次改后端都带一坨生成物变更）。
- `openapi.py` 里的正则/映射逻辑是**不可测试的字符串工程**——本轮明确不写测试
  （协调书 R4），这部分质量靠门禁兜，不靠测试兜。

## 验证方式

```powershell
$env:MJSCXT_AUTOPILOT='0'   # 必须：否则 import 会触发 autopilot 托管恢复，会真的续跑生产
python scripts/generate_client.py      # 改后端后重新生成
python scripts/check_generated_client.py   # expect_exit_code 0
```

### 门禁互补：`generated-fresh` 抓不到生成器本身的 bug

2026-10-07 实测：`check_generated_client.py` **PASS**（5 个文件逐字匹配），
而 `cd frontend && npx tsc --noEmit` **FAIL** —— `generated/index.ts:14-30` 有 15 处
`Duplicate identifier`（`contractsApi` ×3、`deliveryApi` ×8、`licensingApi` ×4）。

根因：`scripts/generate_client.py` 按**每个 operation** 追加导出名，没有去重。

这条要留在 ADR 里，因为它证明**两道门禁不冗余**：
`check_generated_client.py` 校验的是「生成物 == 规格」，而一个**稳定产出非法 TS** 的
生成器同样满足这个等式。只有类型门禁能抓住它。删掉任何一道都会漏。

> 该缺陷属 W3 文件范围（`scripts/generate_client.py`），本 ADR 不代修；
> 修法是生成时 `sorted(set(...))` 去重。已记入 `g0-contract.json` 的 `open_items`。

## 关联决策

- ADR-0001 Blueprint 拆分（反射的 242 条路由来自拆分后的蓝图）
- ADR-0002 采用 ≠ 批准（`approved` 语义由生成类型带到前端）
- ADR-0006 交付包 SHA-256 与人工批准（`delivery` 域的 schema 是手工源之一）
- ADR-0007 授权门禁（`licensing` 域的 schema 是手工源之一）
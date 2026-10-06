# ADR-0008：风格库唯一真源化到后端，`style_id` 与图片文件名刻意解耦

- 状态：**已采纳**（2026-10-07 实施）
- 决策者：项目维护者
- 相关：`app/style_catalog.json`、`app/domain/style_catalog.py`、`app/infrastructure/style_catalog_repo.py`、`app/api/styles.py`、`frontend/src/features/style/**`

## 背景

风格库此前是**前端内联常量**（`ProjectsPage.tsx` 里的 `STYLE_LIBRARY`），
后端 `app/style_kit.py` 另有一套解析。同一份「61 项风格」在两个地方各写一遍，
且两边对「什么算一个风格」「画幅有哪些」的理解并不完全一致。

具体后果：

1. 改一个风格名要改两处，漏一处就前后端不一致；
2. 后端无法校验前端传来的风格值是否有效；
3. 画幅（aspect）只有前端知道，后端渲染时拿不到结构化信息。

## 决策

`app/style_catalog.json` 是风格库的**后端唯一事实源**：
61 项可视觉化风格 + 8 个画幅，`schema_version: 1`、`catalog_version: 2026.10.07`。

- `GET /api/styles` 对外暴露，前端 `frontend/src/features/style/**` 消费 API；
- `app/style_kit.py` 只**追加** `style_id` 解析函数（不改既有函数体），
  保留「按 label 自由文本路由」的旧链路向后兼容；
- 前端 `ProjectsPage.tsx` 只把内联 `STYLE_LIBRARY` 换成 API 消费。

### 关键设计：`style_id` 与图片文件名**刻意解耦**

目录里每项风格长这样（`app/style_catalog.json:27-30`）：

```json
{
  "style_id": "style_2d_urban_romance",
  "label": "2D现代都市风",
  "category": "2d",
  "thumbnail": "style_2d_urban_romance.jpg"
}
```

**`thumbnail` 是一个独立字段，不是从 `style_id` 派生的。** 刻意不派生的理由：

`style_id` 会出现在**历史产物里**——每一张生成图、每一条 `MediaVersion`、
每一个 `GenerationIntent` 都记着它。历史产物一旦写下就不能改（ADR-0002 铁律 3：
Intent 冻结不可原地改）。

如果 `style_id` 绑在图片文件名上，那么**美术换一张缩略图**就等于
**全量历史产物失配**——而换缩略图是纯装饰需求，发生频率远高于换风格本身。
解耦之后：换图 = 改 `thumbnail` 字段，`style_id` 不动，历史全对。

`thumbnail_dir` 也提到目录级（`frontend/src/assets/styles`），避免每项重复。

### 关键设计：label 可改，`style_id` 永久不变

两者职责不同，不能混：

| 字段 | 角色 | 可变性 |
|---|---|---|
| `style_id` | 稳定标识符（纯 ASCII、不含中文、不随文案变化）；历史产物的锚点 | **永不改变** |
| `label` | 展示名；**同时**是写进 `config.style` 的旧自由文本路由值（向后兼容） | **可随时改** |

label 必须可改，因为它承担了向后兼容职责——历史 `config.style` 里存的是中文
label 而非 `style_id`，解析链路要先接住 label（`positive_suffix` /
`negative_suffix` 经 `app/style_kit.py` 注入）。

默认项用 id 表达（`default_style_id: style_2d_urban_romance`、
`default_aspect_id: aspect_16x9`），**不用 label 表达**——默认值也必须抗改名。

`aspect_default` 仅作解析回退，不作真源。

## 为什么不选另一条路

**继续前端内联，后端读一份 JSON 副本。** 零改动，但两份数据必然漂移，
且后端无法校验前端传入的风格值。这是现状，也是要解决的问题本身。

**`style_id` 直接用中文 label。** 最省事，且省掉了映射表。但用户改一次风格显示名，
所有历史产物的锚点就断了——而且中文 label 必然要改（文案优化是常态）。

**`style_id` = 图片文件名（去掉后缀）。** 看起来很自然，少一个字段。
但正如上面所述，换图 = 全量历史失配。**字段少一个，代价是不可逆的。**

**风格库放数据库。** 运行时可编辑听起来更灵活，但它会让风格库变成**有状态数据**：
需要迁移、需要备份、需要在崩溃后恢复。对一份几乎不变的常量清单，这三样成本
全是净负担。JSON 文件 + `catalog_version` 已经够。

**风格 id 用 UUID / 哈希。** 稳定但不可读。审片时人工核对风格，
`style_2d_urban_romance` 远优于 `a3f9c2e1`。

## 后果

**正面**

- 风格与画幅**只有一份真源**，改一处即全端生效；
- 后端能校验前端传入的 `style_id` 是否有效；
- 换缩略图 / 改风格显示名**不影响任何历史产物**；
- 默认项用 id 表达，抗改名。

**负面 / 代价**

- 前端首屏要等 `/api/styles` 返回，风格库不再内联即时可见
  （用 `useStyles` 收敛，配 loading 态）；
- `app/style_kit.py` 的旧 label 路由链路**保留**，等于**两条解析路径并存**。
  这是向后兼容的必要代价：历史 `config.style` 里是 label，不能直接断。
  两条路径的收敛需要迁完历史数据后再做，不在本轮。
- `style_catalog.json` 的 schema 演进要靠 `schema_version` + `catalog_version` 兜。

## 关联决策

- ADR-0002 采用 ≠ 批准（`style_id` 是 `GenerationIntent` 的字段之一，Intent 冻结不可改）
- ADR-0004 契约优先（`/api/styles` 契约与生成类型）
- ADR-0011 设计规范文档化 + 自动化校验
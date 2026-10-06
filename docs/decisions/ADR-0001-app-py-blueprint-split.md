# ADR-0001：把 18,844 行的 `app/app.py` 按域拆成 Flask Blueprint

- 状态：**已采纳**（2026-10-07 实施）
- 决策者：项目维护者
- 相关：`docs/decisions/改造契约_并行协调书.md`、`scripts/snapshot_routes.py`

## 背景

`app/app.py` 曾是 **16,844 行 / 990 KB / 242 条路由**的单文件。所有 `@app.route`
挤在一起，外加 212 个模块级 helper 和 59 个模块级赋值。任何新功能都要改这个文件 ——
这是「不改就没法继续加功能」的地基问题。

## 决策

保持 Flask（**不换 FastAPI**），按域拆成 10 个 Blueprint + 一个 `_shared.py`：

```
app/
  app.py              # composition root：create_app + 注册蓝图 + 错误处理 + SPA 回退（105 行）
  api/
    __init__.py       # 自动发现 + fail-closed 注册器
    _shared.py        # 共享 runtime：app 实例 / 212 helper / config 常量
    projects.py novels.py script.py assets.py storyboard.py
    video.py audio.py qc.py agent.py ops.py
```

`url_prefix` 一律留空，路径原样写死在每个 `@bp` 上 —— 这样搬迁时不存在
「前缀 + 路径」拼接出错的可能。

### 关键设计：自动发现注册

`app/api/__init__.py` 用 `pkgutil` 扫描本包，凡暴露模块级 `bp` 的模块即自动注册。
**新增一个域 = 丢一个文件进来，不需要改 `app.py`**。这正是本次拆分要换来的收益 ——
否则拆完之后，每加一个域还是要去改中心文件，只是把 16k 行换成了另一个中心文件。

这个模式沿用仓库里已有的 `app/plugin_registry.py`（同样是 `pkgutil` 自动发现 +
暴露 `register(reg)`），不是新发明的机制。

### 加载失败策略：fail-closed

任一域模块导入失败即**抛出并阻止启动**。理由：端点缺失必须响亮，不能静默变成 404 ——
用户会以为"这个功能本来就没有"，而真实原因是某个模块 import 炸了。
需要本地排障时可设 `MJSCXT_API_STRICT=0` 降级，该开关不得进生产。

## 为什么不换 FastAPI

评估文档已给出结论，这里落盘：

- 242 个端点手工迁移的收益低于成本；
- `agent_core` 的 37 个工具走**进程内 Flask `test_client`**（不走网络、不占端口），
  这是 Agent 子系统能工作的基石，换框架要重做这一层；
- waitress + Flask 的部署链路已在 Electron 打包链里验证过。

**分层 ≠ 换框架。**

## 后果

**正面**
- `app.py` 16,844 → 105 行；
- 新增域不再需要改中心文件；
- 路由可按域加测试；与自研项目 `api/routes/` 结构对齐。

**负面 / 代价**
- `_shared.py` 当前约 11,144 行代码（首轮记录为 8,884 行，后续新增 helper）。**这是本决策的已知遗留**：212 个 helper 里大量是
  跨域共用的历史工具函数，继续拆需要逐个判定归属域，且必须保证「改 A 域时
  不漏掉 B 域的调用方」。本轮不拆，因为那需要真实测试覆盖兜底，
  而本轮明确「只审核不测试」——在没有测试护栏的情况下动 212 个函数的归属，
  风险远大于收益。留作独立议题。
- `from api._shared import *` 是宽导入，静态分析友好度下降。

## 验证方式

`scripts/snapshot_routes.py` 把「全部 rule + methods」冻结成契约文件。
拆分前后逐字 diff，只比对**路径 + 方法**（endpoint 名允许随蓝图改名而变）。

实测结果：**基线 252 条全部保留，0 丢失**；新增 54 条来自本轮新增的域蓝图。

```powershell
$env:MJSCXT_AUTOPILOT='0'   # 必须：否则 import 会触发 autopilot 托管恢复，会真的续跑生产
python scripts/snapshot_routes.py _route_snapshot_after.json
python scripts/snapshot_routes.py _route_snapshot_before.json --diff _route_snapshot_after.json
```

## 关联决策

- ADR-0002 采用 ≠ 批准
- ADR-0003 深色为默认主题
- ADR-0004 契约优先（OpenAPI）
- ADR-0005 保留磁盘产物为准的续跑判据

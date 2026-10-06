# 诊断（Diagnostics）—— 交互不变量

> 页面：全局导航 → 日志 / ComfyUI / AI 配置
> 相关后端：`/api/deps/check`、`/api/logs`、`app/deps_check.py`
> 规范出处：`design-system/comicdrama/MASTER.md` §6 Interaction invariants

---

## 只读诊断链的边界

对标评估 §P2-8 指出：自研项目的 `diagnose_production_readonly.py` 以 SQLite
`mode=ro` + `query_only` 读取已登记状态，**不启动服务、不探显卡、不提交任务**。

本项目的 `/api/deps/check` 已经是雏形，但**会碰真实运行时**（读 ComfyUI
`/object_info`）。因此必须明确区分三件事：

| 层级 | 含义 | 允许的动作 |
|---|---|---|
| **只读诊断** | 读已登记状态 | 读本地文件/SQLite，不连外部 |
| **运行时探测** | 连 ComfyUI/模型服务 | GET `/object_info`，只读但会访问外部进程 |
| **验收** | 判断系统是否可交付 | **人做，不是工具做** |

> **诊断不等于验收。** 依赖检查全绿 ≠ 成片可交付。UI 不得用绿色勾暗示「可交付」。

---

## Interaction invariants

### 诊断可信度

1. **Never present an offline degraded scan as a full check.**
   在线以 `/object_info` 为准；离线退化扫描必须**标注「不可靠」**并说明缺了什么。

2. **Never imply readiness from a green check.**
   依赖检查通过只说明「依赖在」，不说明「产物对」。

3. **Never hide a partial result.**
   12 个插件里过了 8 个 —— 必须显示「8/12」而不是只显示通过的 8 个。

4. **Never let a diagnostic write production state.**
   诊断不得提交任务、不得改配置、不得清 ComfyUI history。
   （注意：`CLEAR_COMFYUI_HISTORY` 是独立开关，不随诊断触发。）

### 失败可归因

5. **Never show an error without a failure code.**
   用 `failure_codes.py` 的稳定错误码，不给裸异常文案。

6. **Never make the user guess which layer failed.**
   失败必须标明层级：ComfyUI 进程 / 节点注册 / 模型权重 / 前端插件。

7. **Never let a stale check look current.**
   检查结果带时间戳；超过阈值要标注「可能过期」。

### 破坏性动作

8. **Never style "清空 ComfyUI 历史" like a read-only action.**
   这是破坏性动作（会丢生成历史），红边红字 + 二次确认。

9. **Never delete without confirming the target.**
   确认弹窗指名对象与**影响范围**（清多少条历史）。

### 布局与键盘

10. **Never require hovering to read an error.**
    错误信息必须常驻可见，不能只在 `title` 里（触屏拿不到）。

11. **Never scroll the log list on every poll.**
    日志列表 3s 轮询；用户正在往上翻看时**必须暂停自动滚动**，
    否则新行会把正在读的内容顶走。

12. **Never render unbounded log output.**
    长列表 > 50 行虚拟化；单行超长要截断并可展开。

13. **Never use `transition-all` on the log stream.**
    每 3s 一次全属性过渡会与文本重排叠加成明显抖动。

14. **Never hide the request ID behind a hover.**
    报错要能被复制：request ID 必须在正文里，不是 tooltip。

---

## 错误展示的必备五要素

每个错误展示必须同时给（对应 MASTER §4.3 七态中的 `error`）：

1. **原因** —— 发生了什么（用后端中文文案，`client.ts:37-40` 优先透出）
2. **失败层** —— 哪一层出的问题
3. **request ID** —— 可复制，用于查 `output/agent/audit-<date>.jsonl`
4. **重试** —— 能重试的动作按钮
5. **诊断链接** —— 跳到本页或日志页并带上过滤条件

---

## 状态语义档位

诊断页的状态色必须区分「设备在不在」与「东西对不对」：

| 语义 | 令牌 | 含义 |
|---|---|---|
| 待办 / 未探测 | `--state-pending` | 还没查 |
| 探测中 | `--state-running` | 正在查 |
| 通过 | `--state-done` | 查了，通过 |
| 失败 | `--state-failed` | 查了，不通过 |
| 跳过 | `--state-skipped` | 本机不适用（如非 Windows 的注册表项） |
| 需注意 | `--state-attention` | 通过但有风险（如版本漂移） |

> 注意区分 `--state-done`（**技术检查通过**）与 `--approved`（**人工批准**）。
> 诊断页永远不该出现 `approved` —— 这里没有人做批准决策。
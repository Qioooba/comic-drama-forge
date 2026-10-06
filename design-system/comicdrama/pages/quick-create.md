# 快速创建（Quick Create）—— 交互不变量

> 页面：`/projects` → 新建项目
> 规范出处：`design-system/comicdrama/MASTER.md` §6 Interaction invariants
> 状态：**祈使句**形式 —— 每条都是可检查的断言，不是描述性建议。

---

## 决策压缩（本页唯一的产品判断）

新建项目表单曾是 11 个字段的平铺：风格、集数、每集镜头数、分辨率、画幅、帧率、
每镜时长、质检开关、集时长、目标镜头数、voice_map。

对标评估 §4.1 的结论是：**普通用户不该逐项配置**。把它们收敛成三档质量档位，
分辨率/画幅/帧率/模型留在项目设置里，不在每次新建时重复填。

| 档位 | 预设 | 适用 |
|---|---|---|
| 预览 | 低分辨率 + 质检全开 + 禁视频 | 快速验证剧本与分镜，**产物不可交付** |
| 标准 | 768p + 正常质检 | 默认 |
| 精品 | 1080p + 全质检 + 超分 | 交付级 |

> 「预览档产物不可交付」是硬约束，不是提示文案。`preview_gate.py` 已在后端
> 强制（preview 产物永不进交付包），UI 不得给出可交付的暗示。

---

## Interaction invariants

### 决策语义

1. **Never silently fall back.**
   若用户选的模型/风格在当前 ComfyUI 不可用，必须**明确告知并给出替代项**，
   不得静默换一个能跑的。

2. **Never collapse "selected" into "approved".**
   风格卡上的「选中」只表示"用于这个项目"，不表示"内容已审"。两档视觉独立
   （`selected` 青 / `approved` 绿），文案也不同。

3. **Never show a machine check as human approval.**
   质检通过率是机器结论，不得以「已通过」呈现为「已批准」。

### 破坏性动作

4. **Never style a destructive action like a read-only action.**
   「删除项目」（移入 `_trash`）、「重置整集」、「重置资产」一律红边红字，
   与「查看」在视觉上不可混淆。

5. **Never delete without confirming the target.**
   确认弹窗必须**指名对象**（「删除项目《XXX》？」），不得只说「确定删除吗？」。

6. **Never disable without a reason.**
   「整集生产」在质检未过时被禁用，必须在按钮旁给出原因 + 修复链接。

### 状态与反馈

7. **Never rely on color alone.**
   风格分类 Tab（全部/2D/3D/真人）与质量档位必须色 + 文本 + 图标三重表达。

8. **Never blank the view during a refresh.**
   重新拉取项目列表时保留旧列表，不闪空骨架。

9. **Never leave an error without a next action.**
   报错必须给：原因 + request ID + 重试 + 诊断链接。

### 布局与导航

10. **Never lose selection state on navigation.**
    进入工作台再返回本页，保留上次的筛选与滚动位置。

11. **Never let a raw router change silently break old deep links.**
    旧 hash 路由（`#projects?p=xxx`）必须经 301 层重定向并保留 query。

12. **Never require keyboard-only knowledge to be undiscoverable.**
    若支持 Enter 提交，提交按钮上必须有可见提示。

13. **Never use `transition-all` on the style gallery.**
    61 张缩略图卡片的 grid，任何全属性过渡都会在缩放/换筛选时重排卡顿。

---

## 风格画廊（承接 §★2.1 改造）

- 数据源：`GET /api/styles`（后端单一事实源 `app/style_catalog.json`）
- 缩略图：仍由前端 `import`（Flask 只挂了 `/assets` 路由，61 张图不搬后端）
- `style_id` 是稳定标识，**label 可改名而不断历史**
- 分类 Tab：全部 / 2D（24）/ 3D（11）/ 真人（26）

> 排序、搜索与分类筛选是纯前端状态，**不写进 URL** ——
> 它们是一次性的筛选，不是可分享的深链。但「已选风格」必须写进项目 config。

---

## 验收状态

| 状态 | 要求 |
|---|---|
| normal | 表单可用 |
| loading | 拉风格库时显示 Skeleton，**不阻断表单其余字段** |
| empty | 自定义风格时显示空态 + 跳转链接 |
| error | `/api/styles` 失败时**降级到自由文本输入**，并明确告知降级原因 |
| blocked | ComfyUI 不可用时禁用视频相关档位 + 原因 |
| running | 提交中按钮 `aria-busy`，防重复提交 |
| success | 创建后跳转工作台 overview |
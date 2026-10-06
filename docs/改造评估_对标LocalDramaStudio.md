# comic-drama-forge 改造评估：对标 LocalDramaStudio 的短剧能力与架构

> 评估对象：`H:\github\comic-drama-forge`（下载自 GitHub，origin `Qioooba/comic-drama-forge`，upstream `xianjing2000/comic-drama-forge`，仓库仅 15 个 commit，2026-10-05/06）
> 对标基准：`F:\AI_Projects\h3\local_drama_studio`（自研，LocalDramaStudio 0.1.0）
> 目标：判断**哪些部分值得改造**，以及**向自研项目借什么**——功能 / 界面 / 样式三条线。
> 结论不含代码改动，只给评估与方案。

---

## 0. 结论速览

**一句话**：这个项目不是"半成品"，它是一套**质量工程极其出色、但工程架构严重欠债**的单机 GPU 漫剧流水线。它在"AI 生成链条的质量与可控性"上有很多自研项目都没有的东西；但它的**后端是一个 18,205 行的 app.py、前端是一个 5,405 行的单页**，没有版本化生产事实、没有时间线、没有契约、没有前端测试。所以改造方向非常清楚：

| 判断 | 对象 |
|---|---|
| **值得保留并做强（稀缺资产）** | 61 项可视化风格库 · 生成前提示词确定性预检+自愈 · 音频客观质检+频谱喂多模态 · 生产状态语义色 `state-*` · 双层 design token 架构 · 崩溃免重渲台账+文件租约+GPU 闸门 · 37 工具 Agent+六道护栏 · 九宫格候选构图 · 四层质量状态机（哈希绑定批准） · Electron 增量更新+GH Actions 发布 |
| **必须改造（对齐自研）** | 后端 Blueprint 拆分 · 前端路由化与 5405 行页面拆解 · 引入 GenerationIntent/MediaVersion/CapabilityProfileVersion 版本化事实 · 引入 TimelineRevision 不可变时间线 · 任务系统收敛为 Job/Attempt · 契约优先（OpenAPI+生成客户端） · 交付包与 SHA-256 校验 · 前端测试从 0 到 1 |
| **反向值得自研借鉴** | 61 项风格库的产品化（自研目前只有自由文本风格字段） · 提示词级 lint（自研是"编译"而非"校验"，各有优势） · 音频 AI 层（自研客观层 `ebur128` 更严，缺频谱→VLM） · `<alpha-value>` 三元组令牌写法 · z-index/transitionDuration 令牌化 |

**最大的单点风险**：`app/app.py` = 18,205 行 / 242 个路由；`frontend/src/pages/ProjectWorkbenchPage.tsx` = 5,405 行 / 245 KB。任何新功能都要改这两个文件，这是改造的地基。

---

## 1. 两个项目的本质差异

### 1.1 形态与定位

| | comic-drama-forge | LocalDramaStudio |
|---|---|---|
| 自称 | 本地漫剧自动化生成系统（单线：漫剧） | 本地优先 AI 视频创作工作台（双线：短剧 + 解说） |
| 交付形态 | Flask Web(:5210) / PyInstaller exe / Electron 桌面，三形态 | Web + Go Runtime Host（`cmd/runtime-host`）统一生命周期 |
| 后端框架 | **Flask + waitress** | **FastAPI** |
| 前端 | React 18 + Vite 5 + Tailwind 3，无状态库、无 UI 库、无图标库（手写 SVG） | React 19 + React Router + TanStack Query + 领域 `features/` |
| 存储 | `output/tasks.db` + JSON/JSONL + Fernet 密文 | SQLite WAL（事实/状态/版本/审计/outbox）+ 项目文件系统 |

### 1.2 规模对照（实测）

| 维度 | comic-drama-forge | LocalDramaStudio | 倍数 |
|---|---|---|---|
| 后端模块 | 95 个 `.py` / 4.11 MB，**无分层** | `api/application/domain/infrastructure/model_platform` 六层 | — |
| 路由 | **243 个 `@app.route` 全在 `app/app.py`**（990 KB / 18,205 行） | **902 个装饰器 / 65 个路由文件** | 3.7× |
| 前端页面 | 7 个页面 + 1 个 5,405 行工作台 | ~40 个页面 + 40 个 `features/` 域 | ~5× |
| 前端测试 | **0 个** | 244 个 vitest 单元 + 39 个 Playwright e2e spec（核心/2K/字幕/系统中心/无障碍多套 config） | ∞ |
| 后端测试 | 9 个基础设施单测（创作链路 0 覆盖） | **575 个** `test_*.py`；安全默认集 1710 用例 / 真实硬件集 4 用例，用 pytest marker 分离 | 64× |
| 架构决策记录 | 无 ADR | `docs/decisions/` ADR-0001…0110（含"modular monolith no microservices""qc evidence human authority""router migration url contract"） | — |
| 数据库演进 | 无迁移体系 | **152 个 Alembic 迁移** / ~270 张表，手写 SQL + 24 个 repository（无 ORM） | — |
| API 契约 | **无 OpenAPI**，手写 `client.ts` 1,583 行 | `contracts/` + `docs/openapi/` + `generate_client.py` + `ApiCompatibilityGate` 启动校验 | — |

### 1.3 短剧主链路功能覆盖

| 环节 | comic-drama-forge | LocalDramaStudio |
|---|---|---|
| 原稿解析 | txt/md/docx/pdf/epub/html + LLM 归纳章节标题 | 同 + 段落分页 + 来源片段哈希 |
| 故事规划 | `book_outline.py` + 改编草稿 | 改编计划预检→分析→审批→物化（幂等） |
| 剧本 | 两段式：文学剧本(人审 MD) → 分镜 JSON | 剧本 revision + Shot 表 + draft 审批 |
| 资产 | 角色多视图 / 物品 3D 多视角 / 场景 3 类 + 衣柜 + sha256 形象指纹 | **资产圣经** 四类（CHARACTER/SCENE/PROP/COSTUME）+ Identity Pack 版本化 + 跨项目授权 |
| 分镜 | 九宫格候选 + 关键帧 + 画布卡片 | Episode/Scene/**Shot 表** + 镜头组 + 分镜工作区 |
| 镜头 | 分镜卡片（内联在 5,405 行页面里） | **Shot Studio**：镜头导航器 / 意图编辑器 / 站位板 / 候选对比 / 首尾帧桥接 / 参考指南 / 口型同步 |
| 生成事实 | 文件产物 + `actual_params` 快照 | **GenerationIntent + MediaVersion + CapabilityProfileVersion**，生成/采用/批准三态分离 |
| 时间线 | **无**（FFmpeg 直接合并） | **TimelineRevision 不可变时间线** + 合成预检 + 分段渲染 |
| 交付 | 导出 SRT / 剪映 / FCPXML / 帧清单 | 交付包 + 平台规格 + SHA-256 校验 + **人工批准** |
| 质量 | ★四层状态机 A/B/C/D + **哈希绑定批准** | 机器检查 ≠ 人工批准（ReviewDecision 独立） |
| 授权 | **无 license 门禁** | `model-licensing.json` + 音乐库授权 + 品牌/合规版本 |

> 关键结论：**comic-drama-forge 的"生成前"和"生成中"比 LocalDramaStudio 更细；LocalDramaStudio 的"生成后"（版本/时间线/交付/审计）比它完整得多。** 这正是改造的着力点。

---

## 2. 值得保留并做强的稀缺资产（★）

这些是 comic-drama-forge **比自研项目更强或自研没有**的部分，改造时应作为"不可退让资产"保留。

### ★2.1 61 项可视化风格库 —— 单项产品价值最高

- 位置：`frontend/src/pages/ProjectsPage.tsx:13-73`（61 张 `import`）、`:97-159`（`STYLE_LIBRARY`）、`:162-167`（分类 Tab）、`frontend/src/assets/styles/`（61 张缩略图）。
- 分类：**2D 24 项 / 3D 11 项 / 真人写实 26 项**；另有 8 项画幅预设（1:1…21:9，与后端 `style_kit._KEYWORD_RATIO` 对齐）。
- 后端闭环：`app/style_kit.py`（879 行）是风格注入**唯一入口**——`normalize_style` / `with_style` / `apply_asset_style` / `style_emphasis`，分镜提示词、资产参考提示词、尾帧 keyframe、视频重试全部注入同一风格后缀；`qc_client._apply_style_gate` 是**硬闸门**（风格不达标强制 `passed=False`）。
- **自研项目的缺口**：LocalDramaStudio 的风格是自由文本（`character_style`/`scene_style`/`video_style`，见 `features/production-settings-v2/ProjectCreativeDefaultsEditor.tsx:90-92`）+ 少量下拉（`features/pipeline/OneClickPipelineWorkbench.tsx:31` 的 `VISUAL_STYLES`）+ 资产圣经 Style 分类。**没有可视化风格画廊**。
- **改造建议**：
  1. 把 `STYLE_LIBRARY` 从页面内联搬到**后端单一事实源** `style_catalog.json` + `GET /api/styles`（前端下拉/画廊只消费，不再 import 61 张图）；
  2. 补 `style_id` 稳定标识（现在 `value` 就是中文自由文本，改名即断历史），并为每个风格存 `positive_suffix / negative_suffix / aspect_default`；
  3. 加"风格参考图"入口（可从参考图反推风格草案）——自研已有此能力（`features/explainers/ReferenceStyleDraft.tsx`），comic 没有。

### ★2.2 生成前提示词确定性预检 + 自愈（`app/prompt_qc.py`，1,415 行）

- 与"质检"不同：**不依赖质检接口、零模型、零成本**，在出图/出片**之前**跑确定性检查。覆盖 5 类 `kind`：`storyboard` / `h3` / `asset` / `keyframe` / `audio`（`prompt_qc.py:407/557/781/847/947`）。
- 检查内容举例：骨架完整性、风格声明、参考图用途、台词泄漏、字幕类指令、质量空词、H3 六段结构与时间码、尾帧的"锚定参考图/取景连贯/链式承接"语义、TTS 台词里的说话人前缀/语言标记/零宽字符/emoji。
- 三模式：`warn` / `repair`（默认，可修的当场自愈）/ `block`（`qc_config.json` 的 `prompt_enabled`、`prompt_mode`）；接入生成链路 **7 处**，手动链路与 autopilot 一致。
- 对外接口：`POST /api/qc/prompt`（`kind`+`prompt`+`style`+`context`+`ref_count` → `verdict` + 自愈后 `prompt` + `repairs` + `gate`）。
- **自研项目的差异**：LocalDramaStudio 的 `preflight` 是**计划/哈希层**（冻结输入、`plan_hash`、`DomainRuleError` 重预检），另有 `_compile_prompt` 从类型化契约**编译**提示词（`application/explainers/visual_generation.py:1641`）。**没有对自由文本提示词的 lint+repair**。
- **改造建议**：这一层**不要动**，且应升级为契约级资产——把 `check_prompt`/`repair_prompt` 抽成**纯函数**（当前已基本是），补 `tests/` 覆盖（现在创作链路 0 单测），并把它注册进插件表（见 ★2.7）成为可开关的 stage。

### ★2.3 音频客观质检 + 频谱喂多模态（`app/audio_qc.py`）

- 客观层：**一次 ffmpeg 解码取全量指标**（时长/平均电平/峰值/静音时长→有声占比），硬闸（时长 < 0.15s、有声占比 < 15%、平均电平 < -50dB）+ 软扣分。关键设计："**解析不出平均电平即判失败**——无声轨的 mp4 不能因为读得出 Duration 就被放行"。
- AI 层：`showspectrumpic` + `showwavespic` 渲染**频谱图 + 波形图**喂多模态模型；两层**单向收紧**（客观层判死，AI 层说好也翻不回来）。
- 接口 `POST /api/qc/audio`，`with_ai=false` 只跑客观层；整轨口径**自动关闭有声占比判定**（避免成片留白满屏误报）。
- **自研项目的差异**：LocalDramaStudio 客观层**更严**（`ebur128` 综合响度 + true peak + `silencedetect`，`media.py:1642 audio_qc_metrics`，`g8_audio_qc_v1` 策略，-16±1 LUFS / ≤-1 dBTP，`showwavespic` 已用于波形图 `media.py:1594`）。**缺的是"频谱图 + 波形图 → VLM 语义判定"这一层**。
- **改造建议**：把 AI 层补到自研的 `media.audio_qc_metrics` 之后，作为**可选**的语义复核；保持"单向收紧"和 fail-open 两条铁律。

### ★2.4 双层 design token 架构（详见 §5.1）

`index.css` 定义令牌（RGB 三元组）→ `tailwind.config.js:38` 的 `token()` 映射为语义色（`rgb(var(--x) / <alpha-value>)`）→ `:root.dark` 整体覆盖 → **组件层零 `dark:` 变体**。自研项目没有 Tailwind，但**这套"令牌 + 语义层 + 主题覆盖"的三段式值得完整移植**（见 §5）。

### ★2.5 崩溃免重渲台账 + 跨进程文件租约 + GPU 闸门

- `comfyui_job_store.py`：稳定任务键 → prompt_id → 产物；重启后三情形分流（已完成/在跑中/不可复用），安全阀是 **workflow_hash 必须一致** + 产物非空校验。
- `task_lease.py`：`O_EXCL` 原子创建的文件租约 + 心跳 + TTL 过期回收。模块头注释把"进程内 `threading.Lock` 跨进程无效、崩溃后无痕迹"两个真问题写得很清楚——**这正是自研项目"集级互斥/租约"的同类设计**。
- `gpu_task_gate.py`：Semaphore 并发闸 + `/free` 互斥守卫；`cancellation.py` 协作式中止（秒级响应但不打断在飞 GPU 渲染）。
- **改造建议**：与自研的 Job/Attempt + GPU runtime coordinator 收敛为**一套**语义（见 §3.5），不要保留两套。

### ★2.6 37 工具 Agent + 六道护栏（`app/agent_core.py`，1,129 行）

- 37 个工具全部带 `risk` / `expensive` 元数据；护栏：`MAX_STEPS=12`、`MAX_EXPENSIVE=4`（单轮最多 4 次烧 GPU 动作）、`MAX_TURN_SEC`、`COOLDOWN_SEC=120`（同参数复用上次结果）、全局急停 `POST /api/agent/kill` + 单项目互斥、`output/agent/audit-<date>.jsonl` 全量审计。
- 工程细节：工具调用**走进程内 Flask `test_client`**，不走网络不占端口；**降级模式**——模型不支持 tools 协议时从正文里抠 JSON 动作块（`agent_core.py:914`）。
- 前端：右侧常驻可拖宽面板（300–600px，落 localStorage），Agent 轨迹可视化 + 工具计数 + 急停开关，`getAgentSession()` 让面板卸载重挂后**续跟同一 job**。
- **改造建议**：这套 Agent UX **值得整块移植**；但要把它接进自研的持久化 Job 与审计事件模型（现在是内存态 + JSONL）。

### ★2.7 其余值得保留的工程做法

| 资产 | 位置 | 为什么值钱 |
|---|---|---|
| 九宫格候选构图 | `nine_grid_storyboard.py` + `/api/storyboard/grid-candidates` / `grid-apply` + 3×3 选格弹窗 | 一次生成 9 机位构图 → 选格裁切，比 best-of-N 更省卡 |
| 场景九宫格机位预览 | `scene_grid.py` | 复用 `SCENE_VIEW_ANGLE_ZH` 9 条机位文案 |
| 四层质量状态机 | `quality_stage.py` A technical_render / B content_qa / C editorial_review / D **release_approval**，批准**绑定哈希** | 与自研"机器检查 ≠ 人工批准"同源，且用 `label` 而非 `passed` 措辞，值得保留 |
| 两级生产 | `preview_gate.py` 低分辨率预演 → 人工批准 → 正式生产，铁律"预演产物永不可交付" | 省钱且防误交付 |
| 形象指纹跨项目复用 | `asset_library.py`（sha256，零渲染复用） | 避免重复烧卡 |
| 镜号归一 | `shot_key.py`（收敛 6 套实现的语义漂移） | 反面教材的正面修复，值得作为改造范式 |
| 失败码归类 | `failure_codes.py` 从既有异常文案**推导**稳定错误码，不改任何现有报错文案 | 零回归地引入可编程错误分类 |
| 参数快照 | `actual_params.py`（模板 ≠ 实际提交值） | 模板与实际执行漂移的兜底证据 |
| 稳定参数锁 | `stable_profile.py`（把 3090Ti 实测稳定参数写成可机器核对的 `EXPECTED`，漂移即启动告警 + 单测失败） | 硬件回归防呆 |
| 依赖自检 | `deps_check.py` + `GET /api/deps/check`（插件节点 + 模型权重双通道，在线以 `/object_info` 为准，离线退化扫描并标注"不可靠"，只读不阻断） | 换机/交付时的第一道自查 |
| 插件注册表 | `plugin_registry.py` 登记 12 个环节 + 依赖拓扑排序，外部插件放 `app/plugins/*.py` 暴露 `register(reg)` 即自动加载，加载失败只记日志 | 生产环节可替换 |
| Provider 抽象 | `providers/base.py` 三接口（Image/Video/TTS）+ 6 个云端骨架类已声明 | 本地/云端可切换 |
| 成本与耗时看板 | `analytics.py`（ComfyUI 调用计数 + 按部署档案估电费） | 自研没有 |
| 桌面分发链 | Electron `main.js`（41 KB，端口复用/spawn 后端/退出 SIGINT→10s→taskkill 兜底）+ `updater.js`（资源增量包 + SHA256 强校验）+ `.github/workflows/desktop-release.yml`（3 个 ASCII 命名资产，注释记录了 GitHub 吞中文文件名导致 updater 空转的坑） | 分发包工程可直接复用 |

---

## 3. 必须改造的部分（对齐自研的短剧架构）

按优先级排序，**P0 是"不改就没法继续加功能"的地基**。

### ★P0-1 后端：`app.py` 18,205 行 / 243 路由 → Blueprint 分层

**现状**：所有 `@app.route` 在一个文件（243 个装饰器；990 KB），另 9 个 SPA 路由 + 通配回退在 `app.py:18067-18079`。

**改造**（保持 Flask，不换框架——换框架是收益最低的改造）：

```
app/
  app.py                    # 只剩 create_app() + 蓝图注册 + 错误处理（目标 < 300 行）
  api/
    projects.py  novels.py  script.py  assets.py  storyboard.py
    video.py     audio.py   qc.py      agent.py   ops.py       # 按域拆 Blueprint
  application/              # 新增：编排层（现在 pipeline/autopilot/autonomous 散在顶层）
  domain/                   # 新增：不变量与纯函数（shot_key / failure_codes / prompt_qc 已是纯函数，直接归位）
  infrastructure/           # 已有的 client/store/lease 归位
```

**收益**：新功能不再需要改一个 18k 行文件；路由可按域加测试；与自研 `api/routes/` 结构对齐。

**风险**：242 个端点的 URL 必须**逐字不变**（前端 `client.ts` 手写 1,583 行全靠字符串拼接）。建议先做纯搬迁，用"路由快照测试"（`GET /api` 导出全部 rule → 做 golden 文件）守住。

### ★P0-2 前端：5,405 行工作台 → 路由化 + `features/` 域

**现状**：`ProjectWorkbenchPage.tsx` 245 KB，内含 7 个主 Tab 的全部实现（`OverviewTab` / `StoryboardHubTab` / `QcTab` / `AudioTab` / `OutputReviewTab` / `UpscaleTab` / `RelationGraphTab`）+ `ChatPanel` + 8 个内联子组件；`react-router-dom` 已装但 `App.tsx:28-41` 是**手写 hash 解析**，路由库实际未用。

**改造**：

1. **换真路由**：`/projects/:projectKey/overview|storyboard|qc|upscale|relation|audio|output`。现在 `?p=<key>` 承担全部深链，Tab 切换**不换 URL**——刷新回 Overview、无法分享到某 Tab、无法后退。
2. **按域拆 `features/`**（对齐自研 `apps/web/src/features/*`）：`features/storyboard/`、`features/qc/`、`features/audio/`、`features/output/`、`features/asset/`、`features/agent/`。
3. **引入数据层**：现在全是手写 `useState` + `setInterval` 轮询（分镜画布 5s、九宫格 3s、日志 3s）与手写 loading/error。自研用 TanStack Query + 事件失效 + `keepPreviousData`（刷新时保留旧数据）。改造后：轮询收敛为 query 的 `refetchInterval`，跨 Tab 共享的资产数据不再重复拉取。
4. **补状态保持**：自研的规矩是"同一集保持 view/shot/filters/selection/scroll 在 URL/session"（`MASTER.md:68`）。comic 现在换集靠 `useEffect` 重拉，选中态易丢。
5. **补测试**：从 0 到 1 先覆盖 `sanitizeError`（已是有价值的纯函数，`:295`）、`assetSrc`（`:276`）、九宫格格号映射、`pickRoute`。

### ★P0-3 引入版本化生产事实（GenerationIntent / MediaVersion / CapabilityProfileVersion）

**现状**：comic 的"生成"是**文件产物 + 台账**。没有"这一次生成意图是什么、产出了哪些候选版本、哪一个被采用、哪一个被批准"的第一类实体。后果：

- "采用/批准"只能挂在 `quality_stage` 的哈希上，无法回答"这镜为什么用这一版"；
- 候选比较（best-of-N、九宫格选格）无统一模型；
- 重跑只能覆盖文件，无法保留历史版本。

**改造**：引入三张表/三个实体（可照抄自研 `Shot Studio` 的语义）：

| 实体 | 作用 |
|---|---|
| `GenerationIntent` | 这一次生成想做什么（提示词、参考图槽位、seed、Profile、Workflow 版本），**冻结后不可改** |
| `MediaVersion` | 一次生成产出的候选（图/视频/音），带父 intent、指纹、ffprobe 事实 |
| `SelectionDecision` + `ApprovalDecision` | **采用 ≠ 批准**：采用是创作决定，批准是放行决定，两者独立记录 |

**收益**：改造后 `OutputReviewTab`、`QcTab`、`StoryboardTab` 的状态语义立刻收敛；"生成/采用/批准"三态分离是短剧产品成熟的标志。

### ★P0-4 引入不可变时间线（TimelineRevision）

**现状**：**comic 完全没有时间线概念**（grep `TimelineRevision` = 0 命中）。后期只是"FFmpeg 合并 + 字幕"（`video_postprocess.py`）。这意味着：

- 不能在不重渲全部镜头的前提下调整顺序/时长/转场；
- 不能冻结"这一版剪辑"再渲染；
- 不能做"分段渲染 / 只重渲改动段"。

**改造**：这是**从漫剧流水线升级为短剧工作台最关键的一步**。最小可用形态：

1. `TimelineRevision` 冻结：镜头序列 + 每镜采用的 MediaVersion id + 时长 + 转场；
2. `compose:preflight` 返回渲染计划（含 `compose_fingerprint`）；
3. FFmpeg 渲染器只消费冻结计划，产出 `EpisodeRenderVersion`；
4. 字幕 revision 与时间线解耦（现在字幕在 `video_postprocess` 里混着）。
5. 参考自研：`application/timeline.py` + `application/composition/manifest.py`（`RenderManifest` 把"合法静音必须显式 `declare_silence`"这类规则**前置到计划里**，而不是渲染后才发现）。

### P1-5 任务系统收敛

**现状**：comic 有三套并存的任务语义——`task_store.py`（SQLite 全生命周期 + 单元级进度 + 串行队列 + 启动 `recycle_interrupted()`）、`comfyui_job_store.py`（ComfyUI 台账，workflow_hash 复用）、`task_lease.py`（跨进程租约）。断点续跑判据是"**磁盘产物存在且非空**"（产物为准，状态表丢了也能跳）——这个判据很好，但三套语义会让"任务失败到底算谁的"变得含糊。

**改造**：收敛为 **Job / Attempt** 两级（对齐自研）：Job = 一次用户意图，Attempt = 一次执行尝试。保留 comic 的两个优点：① 磁盘产物为准的续跑判据；② ComfyUI `workflow_hash` 复用（**免重渲**是它最值钱的性能特性之一）。

### P1-6 契约优先

**现状**：无 OpenAPI、无 schema 校验；`frontend/src/api/client.ts` 1,583 行手写，`types/index.ts` 1,111 行手写类型；后端返回结构变了，前端只靠运行时崩（README 自述 P3-2「分镜 schema 运行时校验」才刚补上 canvas 边界校验）。

**改造**：后端加 OpenAPI 导出 → 生成 TS 客户端 → CI 里跑"生成物是否过期"检查 → 前端启动时做契约版本校验（自研的 `ApiCompatibilityGate.tsx` + `scripts/generate_client.py` + `contracts/` 三段式可直接照搬）。`client.ts` 里那 1,583 行里**最有价值的一段**是错误脱敏（`client.ts:37-40` 优先透出后端中文错误），迁移时别丢。

### P1-7 交付与授权

- **交付包**：现在只有导出（SRT / 剪映 draft_content.json / FCPXML / 帧清单，`nle_export.py`）。缺：交付预设（竖屏/横屏/3:4）、交付文件清单 + **SHA-256 校验**、人工批准闸门。
- **授权门禁**：**grep `license` = 0 命中**。缺模型授权登记、音乐/素材授权、品牌与水印版本。自研的 `config/model-licensing.json` + 音乐库授权（CC BY 署名可复制到简介）+ `BrandKitPanel` 是成熟参照。
- 顺带补：**i18n 只有中英两套 locales**（`locales/zh-CN.json`、`en-US.json`），且是 `data-i18n` 键值替换式；自研的 i18n 基础设施更完整。

### P2-8 工程实践：五件低成本高回报的事

这几件都不需要重构，但能显著提高"改得动"的概率：

| 事项 | 自研的做法 | comic 的现状 |
|---|---|---|
| **把不变量写成可测试的祈使句** | `design-system/localdramastudio/pages/quick-create.md` 的 "Interaction invariants" 14 条，形如 «Never silently fallback or collapse selection into approval» | 不变量散在 `MASTER` 式的长篇注释与 README 表格里，不可执行 |
| **旧入口退役层** | 三个 `Legacy*Redirect` 组件 + `legacyRoute.test.tsx` + `legacyRetirementAudit.test.ts`，保留 query/hash | hash 路由手写解析（`App.tsx:28-41`），一旦改路由必然产死链且无人报警 |
| **安全集 / 真实硬件集分离** | `test_api_safe.ps1` 只设 `LOCAL_DRAMA_COMFY_ACCESS=disabled` + 追加 `-m 'not comfyui and not video_upscale_gpu'`，退出时恢复原值 | 9 个测试本来就是纯逻辑（做对了），但没有"CI 能跑什么 / 真机才能跑什么"的显式边界 |
| **只读诊断链** | `diagnose_production_readonly.py` 以 SQLite `mode=ro` + `query_only` 读取已登记状态，不启动服务/不探显卡/不提交任务，并明确"诊断不等于验收" | `GET /api/deps/check` + `/api/logs` + `LogsPage` 已是雏形，但会碰真实运行时 |
| **门禁写成版本化规格** | `docs/release/g0-contract.json` 把 `required_components` / `generated_artifacts` / `required_schema_versions` / `migration_heads.expected_heads` / `quality_gates` 全部数据化 | 没有门禁文件；`HANDOFF_P0_20261006.md`（49 KB）是人工维护的验收叙述 |


---

## 4. 界面改造建议

### 4.1 信息架构：从"5 + 7 Tab"到"按生产顺序的项目二级导航"

**现状**（comic）：全局 5 项（项目中心 / AI 配置 / AI 记忆 / ComfyUI / 日志）+ 项目工作台 7 个 Tab（overview / storyboard / qc / upscale / relation / audio / output）。Tab 顺序是**按功能模块**排的，不是按生产顺序。

**问题**：`upscale`（超分）排在 `qc` 之后、`relation`（角色关系图）夹在中间——这与"先出图、再质检、再超分、再合成"的实际流程不符；而"整集生产"没有独立入口（藏在 overview 里）。

**改造**（对齐自研 `MASTER.md:67`）：项目二级导航按**生产顺序**：

```
总览 → 剧本 → 资产 → 分集策划 → 镜头(Shot Studio) → 生产 → 后期(声音/编辑/审核) → 交付
```

其中 `AI 配置 / 记忆 / ComfyUI / 日志` 归入全局"系统中心"，与创作页分离（自研的做法：`/system/*` 独立）。

**更进一步：自研项目做了一次产品决策值得直接采纳**（`docs/product/episode-agent-workspace.md`）——把"分集策划"与"整集生产"合并为**一个「本集制作」工作台**，普通用户不再逐项配置角色/场景/镜头类型/首尾帧/模型/分辨率，而是由 Agent 分五个创作者可见阶段推进：

```
方案与设定 → 关键画面 → 动态视频 → 声音字幕 → 合成质检
```

**唯一的启动决策只有三项**：质量档位（预览/标准/精品）、确认方式（仅异常时暂停 / 批量生成视频前确认）、是否生成人声。分辨率/画幅/帧率/模型留在**项目设置**，不在每集重复填。文档还立了两条真实性约束：

> - 这些阶段是对**现有生产事实的聚合视图**，不新增第二套状态或任务系统；
> - Agent 不伪造人工批准，不把"已选中"显示为"已批准"；启动前仍执行真实预检，失败不创建生产任务。

这对 comic 最有价值的地方是：**它给"7 个按模块排的 Tab"提供了一个明确的目标形态**——不是把 Tab 重新排序，而是把"逐项配置"降级为"Agent 推进 + 只在歧义/质量/人工检查点请求处理"，高级用户再下钻到 Shot Studio。comic 的 `autopilot` 已经有这个野心，但缺"聚合视图 + 唯一三决策启动 + 不新增第二套状态"这三条纪律，所以最终长成了"每个 Tab 都有一堆自己的状态"。

### 4.2 工作台布局：三栏 → 明确"导航 / 主舞台 / 检查器"

**现状**：工作台 = 左列（头 + 统计 + Tab 内容）+ 右侧常驻 AI 总控面板（可拖宽 300–600）。AI 总控**永久占用**右侧栏。

**问题**：短剧工作台最需要的右侧栏是**镜头检查器**（意图/参数/候选/参考），现在被 Agent 占了；Tab 内容区只能横向铺开，导致分镜卡片信息密度不足。

**改造**（对齐自研 `AppShell` + `EpisodeTaskDrawer` 模式）：

- 主区三栏：**场景/镜头导航器（左，可收） + 媒体舞台（中） + 检查器（右）**；
- AI 总控从"常驻右栏"改为**可切换的右侧抽屉/轨道**（快捷键呼出），保留它的轨迹可视化与急停开关；
- 下方常驻 **EpisodeContextBar**（自研 `layouts/EpisodeContextBar.tsx`）：项目 → 季 → 集 → 镜 面包屑 + 队列/GPU/磁盘状态（自研 `MASTER.md:65` 的 topbar 规范）。

### 4.3 分镜卡片 → Shot Studio

**现状**：`StoryboardTab`（`:3496-4274`，约 780 行）是一个内联组件，约 20 个 `useState`（`cards/summary/loading/error/busy/videoMode/playingVideo/notice/shotError/episodeGenerating/episodeTaskId/projectVideoMode/selectedShots/batchRunning/batchConfirmOpen/batchResult/gridTarget/gridPhase/gridImgUrl/gridCell/gridError/zoomImg`）+ 4 个轮询 ref。功能其实**不少**（单镜重跑、批量重跑 ≤12、九宫格选格、lightbox、分集切换），但：

- 无镜头导航器（只能滚长列表）；
- 无候选对比（多版本只能顺序看）；
- **"采用"与"批准"没有独立按钮**；
- 无首尾帧桥接的显式 UI（有 `videoMode: reference|keyframe` 但没有帧级绑定）；
- 无站位板（`StagingBoard`）、无口型同步面板、无参考指南面板。

**改造**（拆成自研 `features/director-v2/` 同构的组件集）：

| 目标组件 | 对标自研 |
|---|---|
| `ShotNavigator` | `features/director-v2/ShotNavigator.tsx`（窗口化镜头导航） |
| `DirectorIntentEditor` | 同名的意图编辑器（含运镜/情绪/微表情/视线） |
| `EpisodeShotBoard` | 整集镜头板（缩略图网格 + 完成度） |
| `CandidateCompareDialog` | 候选并排对比 |
| `DirectorTakeAdoption` | **采用 vs 批准两个独立动作** |
| `FrameBridgeControls` | 首尾帧桥接（前后镜帧复用） |
| `StagingBoard` | 站位板（人物位置 → 视线建议） |
| `DirectorSoundInspector` + `ShotLipsyncPanel` | 声音检查器 + 口型同步 |
| `VideoReferenceGuidesPanel` | 参考指南（运动掩码/向量/关键帧） |

### 4.4 状态语义：补齐 `selected` / `approved` 两档

comic 的 `state-*` 只有 6 档：`pending / running / done / failed / skipped / attention`。自研的 `styles.css` 有 **`--selected: #36d6c5`（青）与 `--approved: #50d890`（绿）两档独立**，并在 `MASTER.md:43-44` 明确"`selected` 永不标记为 approved"。

**改造**：把 `state-done` 拆成 `state-selected`（青）与 `state-approved`（绿），并在所有徽标上配合**文本 + 图标**（自研 `MASTER.md:108` 的规矩：状态不得只靠颜色）。这是"采用 ≠ 批准"在视觉上的落地，与 §P0-3 的数据模型改造是同一件事的两面。

### 4.5 其它界面改造点

| 问题 | 现状证据 | 改造 |
|---|---|---|
| 长列表不虚拟化 | 无 `react-window`/`react-virtual` | > 50 项虚拟化（自研 `MASTER.md:112`）；分镜画布、日志、记忆库首当其冲 |
| 无键盘操作 | `onKeyDown` 仅 4 处 | 键盘优先：镜头切换、候选采纳、代理选优（自研 `MASTER.md:127`） |
| 错误展示 | 已有 `sanitizeError` + `role="alert"` 仅 1 处 | 补"就近报错 + 原因 + request ID + 重试 + 诊断链接"（自研 `MASTER.md:110`） |
| disabled 无原因 | — | disabled 必须带相邻原因或恢复链接（自研 `MASTER.md:107`） |
| 无空/加载/错误态规范 | 已有 `Skeleton`/`EmptyState`/`ErrorState`（`ui/index.tsx`） | 好，保留；补"刷新时保留旧数据"（现在静默轮询已做对，其余请求没有） |
| 无响应式规范 | — | 补 1440/1280/1024 三档（自研 `MASTER.md:116-121`） |

---

## 5. 样式改造建议

### 5.1 令牌对照表（这是最有价值的可直接挪用部分）

**comic-drama-forge（浅色为主 + 深色覆盖层）**：

| 语义 | 令牌 | 浅色值 | 深色值 |
|---|---|---|---|
| 画布 | `--bg-canvas` | `#F7F8FA` | `#020617` |
| 卡片 | `--bg-surface` | `#FFFFFF` | `#0F172A` |
| 次级区块 | `--bg-surface-2` | `#F1F5F9` | `#1E293B` |
| 描边 | `--border` / `--border-strong` | `#E2E8F0` / `#CBD5E1` | `#334155` / `#475569` |
| 文字 | `--text-primary/secondary/tertiary` | `#0F172A` / `#475569` / `#94A3B8` | `slate-200/400/500` |
| 品牌 | `--brand` | `#4F46E5` indigo-600 | `#6366F1` indigo-500 |
| 强调青 | `--accent` | `#06B6D4` | `#06B6D4` |
| 主按钮实心底 | `--action` | `#0F172A` | `#334155` |
| 生产状态 | `--state-pending/running/done/failed/skipped/attention` | 6 档 + `-subtle`/`-strong` | 同左反转 |
| 图表分类 | `--viz-rose/violet/teal/amber` | 仅 SVG 用 | 不变 |
| 动效 | `transitionDuration` | 统一 `160ms` | 同 |
| 层级 | `--z-sticky…--z-preview` | 6 档 | 同 |

**LocalDramaStudio（深色工作站）**：`--chrome #080a10` / `--canvas #0b0e15` / `--surface #121620` / `--surface-muted #191e2a` / `--media-surface #05070b` / `--text #f4f6fb` / `--text-muted #9aa4b5` / `--border rgba(255,255,255,.11)` / `--creative #7c5cff` / `--selected #36d6c5` / `--approved #50d890` / `--danger #ff6b7a` / `--radius-sm|md|lg 6|10|14px` / 4px 网格 / 控件 40px / 图标热区 44px。

**建议**：**保留 comic 的令牌架构（三段式），把取值向 LocalDramaStudio 的生产工作站语义靠**——即"dark chrome + 低眩光工作区"，而不是"浅色卡片 + 科技辉光"。理由见 5.3。

### ★5.2 必须完整保留的四件事

1. **RGB 三元组 + `<alpha-value>` 占位符**（`index.css:15-19` + `tailwind.config.js:38`）。注释已经把坑写透：令牌存 hex 会让 `bg-brand/10` 产出**无效 CSS 而静默失效**。这是全项目最有价值的样式工程约束。
2. **主题走令牌覆盖，组件层零 `dark:` 变体**（删掉了 619 处不可达的 `dark:` 类）。任何新增组件都不得写 `dark:`。
3. **`-subtle`（徽标浅底）/ `-strong`（同色系深字）分档**，并明确"不要拿 `--success` 主色直接当文字色：白底上仅 ~2.5:1"。
4. **`prefers-reduced-motion` 兜底 + `:focus-visible` base 层统一兜底**（`index.css:273-279, 417-426`）——后者在"还有 ~50 处原生 button/input 没补 focus 样式"的现实下是极划算的做法，直接照搬。

### ★5.3 必须删 / 降级的三件事

自研 `MASTER.md:18` 的硬规矩是：**"No decorative gradients, glow, glassmorphism, giant marketing hero, or card-on-card nesting."** comic 当前恰好三条全中：

| 项 | 位置 | 处理建议 |
|---|---|---|
| `.glass` / `.glass-strong` backdrop-blur(18–24px) | `index.css:291-301` | **降级**。代码注释自己已承认"长列表卡片**不要**加 backdrop-blur：滚动时逐帧重绘会把帧率拖垮"——既然如此，与其留一条只能靠人记住的规矩，不如把它限制到一个类名 `.glass-chrome` 并加注释白名单 |
| `body::before` 科技感辉光 + 64px 细网格 | `index.css:245-258` | **降级为可选皮肤**（`data-skin="tech"`）。这是"氛围不是夜店"的自我认知，但对长时间创作工作台属于持续视觉噪声 |
| `.text-gradient` / `.glow-brand` / `.progress-fill` 品牌→青渐变 | `index.css:303-316` | **删 `.text-gradient` 与 `.glow-brand`**；`.progress-fill` 保留但改实色/单色（进度条是信息，不是装饰）。`a` 标签当前也带品牌色渐变观感，需一并检查 |

**注意**：不要一刀切删掉浅色主题。comic 的深色层（`:root.dark`）本身是完整且经过反色推理的（subtle 翻 950、strong 翻 300、brand 取 indigo-500 作"实心底 + 文字色"双职责折中）。**自研项目本身是纯深色**（`apps/web/src/styles.css:3` `color-scheme: dark`，无 light token 集，`MASTER.md:20-24` 还专门记录了"全深色剪辑套件"与"全暖色生产纸"两个被否决方案的理由）。所以建议：**直接把 `:root.dark` 提升为默认主题，浅色降为可选皮肤**——这比重新设计一套深色省事得多，且能立刻与自研的视觉语言对齐。

### ★5.3b 自研项目里额外值得一并搬过来的三样

1. **「每屏一个主创作动作」的按钮语义四层**（`styles.css:200-229`）：`.secondary` / `.primary-action` / **`.danger-action`**（注释：破坏性动作绝不与只读动作共享轮廓，"红边红字，使 删除/归档 不会被误点成 查看"）/ `.ghost-action`。comic 现在只有 `Button` 的 `variant: secondary`，破坏性动作与普通次要动作同形——这在有"删除项目（移入 `_trash`）""重置整集""重置资产"这类工具的面板上是真实风险。
2. **`[aria-busy="true"]` 自带 spinner**（`studio-components.css:239-251`）+ **`[aria-invalid="true"]` / `[readonly]`（虚线边框）等完整表单状态机**（`:310-378`）。comic 的 `Input`/`Select`/`Textarea`（`ui/index.tsx:301-462`）没有这一层，导致"提交中"和"校验失败"在各页面各写一遍。
3. **把审计结论写进 CSS/文档**（`density.css` 是最佳范例）：里面直接记着"86 controls under 24px"、"1,324 buttons stayed at 12px through the first pass"、"197 disabled buttons indistinguishable from enabled ones"、"42 shots at ~660px each produced a 28,800px track inside a 2,285px shell with no scrollbar"。**这让设计债务可追溯、可回归验证**。comic 的 `index.css` 注释已经有这个味道（如"619 处不可达 `dark:` 类"、`animation-fill-mode: backwards` 导致弹窗跑到屏幕外），值得把它制度化：每个设计修正都记度量数字 + 失败原因 + 修正选择。


### 5.4 必须补齐的规范

| 缺什么 | 具体值（取自研） | 落到哪 |
|---|---|---|
| 控件高度 | 组件层实际用 **44px**（`studio-components.css` 的 `--control-height:44px` / `--control-height-compact:36px`），规范底线 40px；纯图标触达区 **44×44** | 加令牌 `--control-h: 44px` / `--control-h-compact: 36px` / `--hit-target: 44px`，`ui/index.tsx` 的 Button/Input/Select 统一消费 |
| 内容宽度上限 | `--control-max:640px` / `--control-field:320px` / `--control-field-lg:520px` / `--measure:70ch` / `--shell-max:2100px`，注释明说"按钮/字段按内容尺寸，只有容器、进度条、表格外框允许撑满父级" | 加同名令牌；**不要用全局宽度上限**——自研 `density.css:30-31` 记录了教训："全局上限会压窄全宽编辑器并扭曲行内标签"，改成 `.control-compact` 显式 opt-in |
| 间距网格 | **4px 基网格**：4/8/12/16/24/32/48 | 加 `--space-*` 令牌，替换页面里散落的 `p-3/p-4/gap-4` 组合 |
| 圆角 | 控件 6 / 字段 8 / 面板 10 / 容器 12 | 现在 `borderRadius` 覆盖为 sm6/md10/lg12/xl16，**缺"面板 10"与"字段 8"的区分**，且 `xl:16` 偏大 |
| 字体 | UI/正文 `Inter, Segoe UI, Microsoft YaHei`（**不发外部字体请求**）；编辑性标题 `Georgia, Songti SC, serif`；等宽 `Cascadia Code, SFMono-Regular, Consolas` | comic 用的是 `PingFang SC, Microsoft YaHei`（`index.css:122`），中文优先是对的，但**缺等宽族**与"标题/正文分族"的规则 |
| 等宽数字 | 时长/种子/队列/资源值**必须** tabular-nums | comic 已在统计卡用了 `tabular-nums`（`:201`），需提升为全局规范 |
| 字号档位 | 12 / 13 / 14 / 16 / 18 / 24 / 32（七档） | comic 是 12/13/14/16/20/24/30，**建议对齐**（页面标题 24、概览标题 32） |
| 阴影 | 阴影**罕见**：level1 `0 1px 2px rgba(32,36,43,.06)`，level2 仅弹层 | comic 有 4 档 + 卡片 `hover:-translate-y-0.5 hover:shadow-md`（`:200`）悬浮动效，属于"过度装饰"，收敛掉 |
| 动效 | 150–220ms，只动 transform/opacity | comic 已是 160ms，但 `transition-all` 出现频繁，建议收为 `transition-colors` / `transition-transform` |
| 虚化/模糊 | 媒体查看器/多视频对比/时间线用 `#05070B` 面 | 补 `--media-surface` 令牌（当前无） |

### 5.5 文档化与自动化校验

comic 的设计规范**全部埋在 `index.css` 的注释里**（写得很认真，但工具读不到）。建议：

1. 抽出 `design-system/comicdrama/MASTER.md`，结构照抄自研（产品气质 / 选定方向与被拒方案 / 令牌表 / 应用框架 / 组件规则 / 响应式 / 无障碍与 QA 七节）；
2. 加**令牌一致性审计脚本**（照抄自研 `scripts/audit_*.py` 的思路）：扫描 `frontend/src/**` 里组件层的 hex/rgba 字面量与裸 `dark:` 类，超出白名单即失败；
3. 把"normal/loading/empty/error/blocked/running/success 七态 × 1440×900 / 1280×800 / 1024×768 三档"做成 Playwright 截图基线（comic 现在前端 0 测试，这一步同时补上测试缺口）。

---

## 6. 路线图建议

### 阶段一（地基，2–3 周）——"能继续加功能"
1. 路由快照 golden 文件 → `app.py` 拆 Blueprint（URL 逐字不变）
2. 前端换真路由 + 按域拆 `features/` + 引入 TanStack Query
3. 补前端测试基建（Vitest + Playwright 截图基线）+ 令牌审计脚本
4. **风格库唯一真源化**（`STYLE_LIBRARY` → 后端 `style_catalog.json` + `/api/styles`）
5. **同时启动 ADR 制度**（`docs/decisions/ADR-0001-….md`）：把"为什么拆 Blueprint""为什么深色为默认""为什么采用≠批准"逐条落盘。自研项目有 ADR-0001…0110，这是它能做到"改造不动摇"的底层原因——comic 现在的决策全散在 `HANDOFF_P0_20261006.md`（49 KB）和代码注释里

### 阶段二（产品模型，3–5 周）——"从流水线变成工作台"
5. `GenerationIntent` / `MediaVersion` / `SelectionDecision`+`ApprovalDecision` 三件套
6. `TimelineRevision` 不可变时间线 + `compose:preflight` + 分段渲染
7. 任务系统收敛为 Job/Attempt（保留磁盘产物续跑判据与 workflow_hash 免重渲）
8. Shot Studio 组件集（导航器 / 意图编辑器 / 候选对比 / 采用-批准 / 帧桥接 / 站位板）

### 阶段三（质量与交付，2–3 周）
9. 契约优先：OpenAPI 导出 + 生成客户端 + 启动兼容校验
10. 交付包 + SHA-256 + 平台预设 + 人工批准
11. 授权门禁（模型授权 / 音乐素材授权 / 品牌水印版本）
12. 音频 AI 层（频谱+波形 → VLM）接入客观层之后
13. 设计系统文档化 + 深色为默认主题

---

## 7. 明确不建议照搬的（避免过度工程）

| 项 | 理由 |
|---|---|
| 把 Flask 换成 FastAPI | 收益低于成本。242 个端点的手工迁移 + waitress/Flask 生态（`test_client` 进程内直连是 Agent 的基石）都要重做。**分层 ≠ 换框架** |
| 全盘照搬自研的六层 DDD | comic 是 95 模块的单进程应用，套六层会先付出巨大迁移成本。**先做"Blueprint + application 层"两层就够** |
| 自研的"解说 explainer"双产品线 | comic 定位是单线漫剧，加第二产品线会稀释。除非要合并两个产品 |
| 自研的 `model_platform`（ModelRoot/完整性/Profile/Workflow 逐级发布门禁） | comic 现有的"以 ComfyUI `/object_info` 为唯一权威枚举模型"（`comfyui_models.py`）+ `deps_check` + `workflow_integrity` 已覆盖大部分场景，全套门禁是另一个量级的工程 |
| 无限画布 / 节点式编排 | 自研已明确决策"不做无限画布"，comic 也没这个包袱 |
| comic 的 `text-gradient` / 辉光 / 玻璃卡片 | 见 §5.3 |
| comic 现有 9 个基础设施单测的**覆盖范围** | 它们是"修复项回归"，不是架构测试。要新写，不要以为已有测试就安全 |

---

## 8. 证据索引（关键路径）

**comic-drama-forge**
- 定位与能力清单：`README.md:1-180`（架构图、能力增强表、三类资产规范）、`README.md:46-145`（对标开源项目后的优化落地 + 顺带修复的真实缺陷 15 条）
- 对标调研：`docs/开源对标与升级实施方案.md`（对标 story-claw/BigBanana/Toonflow/CineGen 等 9 个项目；结论"流水线内部质量工程领先，缺口在 4 处"）、`docs/开源项目对比分析.md`、`docs/小说到视频全流程竞品对比.md`
- 单文件后端：`app/app.py`（18,205 行 / 242 路由）、`app/serve.py`（waitress 说明）
- 质量工程：`app/prompt_qc.py`(1,415 行)、`app/audio_qc.py`、`app/qc_client.py`(4,107 行)、`app/quality_stage.py`、`app/preview_gate.py`
- 风格：`app/style_kit.py`(879 行)、`frontend/src/pages/ProjectsPage.tsx:13-184`
- 生成链：`app/comfyui_client.py`(4,567 行)、`app/h3_*_builder.py`、`app/model_capabilities.py`、`app/keyframe.py`、`app/nine_grid_storyboard.py`、`app/scene_grid.py`
- Agent：`app/agent_core.py`(1,129 行，TOOLS 在 `:110-505`)、`app/autopilot.py`(1,531 行)、`app/autonomous.py`、`app/pipeline.py`
- 工程基建：`app/task_lease.py`、`app/gpu_task_gate.py`、`app/comfyui_job_store.py`、`app/stable_profile.py`、`app/deps_check.py`、`app/plugin_registry.py`、`app/failure_codes.py`
- 前端：`frontend/src/index.css`(457 行)、`frontend/tailwind.config.js`、`frontend/src/App.tsx`(106 行)、`frontend/src/pages/ProjectWorkbenchPage.tsx`(5,405 行)、`frontend/src/api/client.ts`(1,583 行)、`frontend/src/components/ui/index.tsx`
- 分发：`electron-app/main.js`(41 KB)、`electron-app/updater.js`、`build_exe.py`、`.github/workflows/desktop-release.yml`

**LocalDramaStudio（对标基准）**
- 产品与架构：`README.md:1-130`（双产品线、原稿→交付链路、13 条工作区路由表、已实现能力、架构概览、LOCAL_ONLY 边界）
- 设计系统：`design-system/localdramastudio/MASTER.md`（130 行，七节规范）、`design-system/localdramastudio/pages/{quick-create,diagnostics}.md`（含 "Interaction invariants" 格式）、`design-system/localdramastudio/figma-state.json`（**注意：这是被 rate limit 中断的状态检查点，不是可用设计稿**）、`apps/web/src/styles.css:1-68`（权威运行时令牌）、`studio-theme.css`(670 行)、`studio-components.css`(466 行)、`density.css`(318 行，审计账本)、`v2-pages.css`(562 行)
- 分层：`apps/api/local_drama/{domain,application,infrastructure,model_platform,api,bootstrap,entrypoints}`、`apps/api/local_drama/api/routes/`（73 个模块 / 902 装饰器）、`api/schemas/`（56 个 Pydantic 契约）、`infrastructure/database/`（24 个 repository）
- 前端：`apps/web/src/{app,features,layouts,pages,generated}`、`app/router.tsx:107-205`（路由表 + 旧路由 301 层）、`app/routeRegistry.ts`、`app/projectNavigation.ts`、`features/director-v2/*`（Shot Studio 组件集 + `director-desk.css` 2,640 行，三栏 `grid-template-columns: 285px 1fr 350px` + 可拖拽分栏）、`layouts/EpisodeContextBar.tsx`、`features/drama-workflow/DramaWorkflowSteps.tsx:38-46`（六步生产顺序）
- 产品决策：`docs/product/episode-agent-workspace.md`（五阶段合并工作台 + 唯一三决策启动 + 真实性约束）
- 数据模型：`apps/api/alembic/versions/0001_g2_core.py`（核心表 + `_audit_columns()` 全表统一审计列）、`0061`（`shot_working_media_slots` 采用≠批准）、`0096`（`production_sessions` 带 `plan_hash`/`checkpoint_policy`）
- 质量工程：`apps/api/local_drama/application/composition/manifest.py`（`declare_silence` 把合法静音前置到计划）、`application/explainers/media_qc.py`（ebur128 + silencedetect）、`application/media.py:1642`（`audio_qc_metrics`）、`application/gpu_runtime.py`（120s 租约 / 20s 心跳 / `DEGRADED` 审计态）、`scripts/check.ps1`（13 步门禁链）、`scripts/audit_*.py`（十余个真实模型/成片审计）、`docs/release/g0-contract.json`（把门禁写成版本化规格）
- 变更即规格：`docs/short-drama/optimization-implementation-20261004.md`（23 项编号 → 变化 → 实现位置）
- 决策可追溯：`docs/decisions/` ADR-0001…0110
- 视觉审计：`docs/visual_audit/FULL_UI_UX_AUDIT_REPORT.md`(48 KB) + 按 `NN_page_控件名_序号_标题.png` 命名的证据截图库

---

*报告基于对两个仓库的只读勘察：comic-drama-forge 侧覆盖 95 个后端模块、`app/app.py`、全部前端页面与样式文件；LocalDramaStudio 侧覆盖 README、设计系统三件套（MASTER.md / pages / 五个 CSS）、路由表、41 个 `features/` 域、核心数据模型迁移与质量工程脚本。行号取自勘察当时的工作树，后续改动可能漂移。*

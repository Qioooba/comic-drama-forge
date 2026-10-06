# ADR-0012：不可变时间线 TimelineRevision

- 状态：**决策已采纳，领域层已落地，成片链路已接入（新增旁路，默认关闭）**（2026-10-07）
- 决策者：项目维护者
- 相关：`app/domain/timeline.py`、`app/infrastructure/timeline_repo.py`、`app/application/timeline_service.py`、`app/api/timeline.py`、`app/video_postprocess.py`

> ## ⚠️ 实施状态声明（2026-10-07 收口审核补记 → 当日补接入）
>
> ### 上一轮的状态（保留作对照）
>
> **本轮落地的是时间线的领域层与 API，还没有被任何成片链路消费。**
> 收口审核实测：
>
> - ✅ 已有：`TimelineRevision` 冻结语义、`compose:preflight`、`compose_fingerprint`、
>   合法静音前置声明、`/api/timeline/*` 共 10 条端点。
> - ❌ **未接入**：`app/video_postprocess.py` 对 `timeline` / `revision` /
>   `fingerprint` 的引用数为 **0**。FFmpeg 渲染器**仍然不消费冻结计划**，
>   字幕也仍与后期混在一起（本 ADR「决策」第 4、5 点未实现）。
>
> 换句话说：当时这条时间线是一条**孤岛** —— 数据能冻结、能预检，
> 但成片仍然走「按磁盘上现有文件直接合并」的老路。
>
> ### 当前状态（2026-10-07 补接入后）
>
> **成片链路已能消费冻结计划，但走的是**新增旁路**、默认关闭**，
> 旧的「按磁盘现有文件合并」路径的**既有函数体一行都没改**。
>
> ⚠️ 关于「本轮对 `app/video_postprocess.py` 的改动范围」，准确说法是
> **新增为主、并非严格「仅追加」**：以第一轮修复之前的提交为基线，
> `git diff --numstat` 累计为 **297 added / 1 deleted**。那 1 行删除是第 11 行
> `from typing import List, Optional, Dict`（补上 `Any`）。
> `git diff` 只有两个 hunk：这一个 import 行，和文件末尾的纯追加，
> **没有任何既有函数体被修改**，故「既有行为不变」成立；但把它写成「仅追加」是不准确的。
> （两轮修复中另改过 `_verify_plan_duration` 的 docstring，不改其逻辑。）
>
> - ✅ **决策第 3 点已实现**：
>   `app/video_postprocess.py` 新增 `render_from_plan()` / `validate_render_plan()`，
>   只消费 `ComposePlan`，不接受「磁盘上有什么就合什么」；
>   产出 `EpisodeRenderVersion` 登记（`app/domain/timeline.py`），
>   把 `compose_fingerprint` + `plan_fingerprint` + 产物 sha256 三者绑死。
> - ✅ **决策第 4 点（字幕解耦）已实现**：`ComposePlan` 里**没有**任何字幕步骤，
>   `subtitle_revision` 仅作为引用挂在计划上；合成路径（`compose_plan_to_ffmpeg_args`）
>   不产出 `subtitles=` 滤镜参数，字幕仍由既有 `add_subtitles` 在成片渲完后单独消费。
> - ✅ 计划 → FFmpeg 参数的转换是**纯函数**
>   （`application.timeline_service.compose_plan_to_ffmpeg_args`，无 I/O、无子进程）。
> - ✅ 渲染前置预检（`TimelineService.compose_preflight`）四个阻断点全部 fail-closed：
>   `undeclared_silence`、指纹自校验失败、版本未登记/与磁盘现状对不上、版本未批准。
> - ✅ 新增 `POST /api/timeline/revisions/<id>/render`，`authorized_by` 必填，
>   机器账号 **403**（复用 `require_human_authorization`，与 `/api/delivery/*` 同一口径）。
>
> **仍遗留（不是"已接入"能盖过去的）**：
>
> - ⚠️ **旧路径仍是默认**：`generate_final_video()` 未改，`/api/final/video` 等
>   既有入口仍走它。切到新路径需要一次显式的迁移决定（前端按钮 / 托管链路改指新端点），
>   本轮**刻意不做** —— `video_postprocess.py` 是在产的生产代码。
> - ⚠️ **`compose_plan_to_ffmpeg_args` 未经真实渲染验证**：两轮修复都明确禁止跑 FFmpeg，
>   `xfade` / `acrossfade` / `concat` / `atrim` 的 offset 与长度递推只有**参数串断言**与
>   **由图反推的算术验证**。首次启用**必须**渲一支短样片核对时长、转场边界与音画同步。
> - ✅ **后续补修已关闭该项**：`episode_render_versions` 已在 `timeline_repo` schema v2 落库；
>   `record_render()` 必须携带 `output_path + output_sha256`，并提供
>   `/api/timeline/revisions/<id>/renders`、`/api/timeline/renders/<id>`、
>   `/api/timeline/renders/by-fingerprint/<fingerprint>` 查询端点。
>   `dry_run=true` 不再伪造登记。
>
> ### 2026-10-07 缺陷修复轮（6 处，静态验证 + 纯逻辑探针）
>
> 独立审计查出 6 处真实缺陷，已全部修复。**未跑过 ffmpeg**，全部结论来自
> 参数串的纯逻辑断言。
>
> - **时长基线漏算转场重叠**：`total_duration_sec` 原为各镜时长求和
>   （4×10s + dissolve 1s + fade 0.5s = 40.0s），真实成片 38.5s。
>   差 1.5s > 0.5s 容差 ⇒ 每次带转场的渲染都在**渲完之后**才被判失败。
>   现由唯一权威函数 `domain.timeline.expected_total_duration_sec()`
>   （扣掉每个转场吃掉的重叠）同时供给「计划字段 / ffmpeg 预期 /
>   渲后校验基线」三处，并在 `ComposePlan.validate()` 里 fail-closed 复核。
> - **音轨 `atrim` 截断**：无转场分支原用「上一镜自身时长」裁累计音轨，
>   `acrossfade=d=1.0` 之后的 19s 音轨被裁成 10s，**静默丢 9 秒**
>   （画面 38.5s / 声音 19s，ffmpeg 不报错）。终点改为累计值。
> - **`output_path` 零校验** → 现做扩展名白名单 + 显式拒 `..` +
>   `realpath` 后复查包含关系（挡符号链接逃逸），缺省落到项目输出根。
> - **审批门可被请求体关闭**：`require_approved: false` 曾能跳过人工批准
>   却照样落进 `final/`。现改为服务端决定；预览走 `preview: true`，
>   **强制**落 `preview/` 并在返回里打 `preview` / `deliverable=false`。
> - **文件名把集号塞进版本号槽**：`f"ep{ep:02d}_final_tl{ep:02d}.mp4"`
>   曾用同一个变量填两个槽，第 2/3 版剪辑 `-y` 覆盖第 1 版（违反铁律 4）。
>   现 `ep` = 集号、`tl` = 版本号，两个独立具名变量。
> - **`plan_fingerprint` 漏覆盖两项**：原载荷不含 `total_duration_sec` 与
>   `compose_fingerprint`，单独篡改任一个 `verify_plan_fingerprint()` 仍返回 True。
>   现两者都进载荷 —— 「成片 ↔ 剪辑 ↔ 计划」三者绑死补齐了这一环。
>   ⚠️ **这会改变指纹值**。该路径是 opt-in 新端点、**从未有任何生产渲染走过**，
>   故无历史指纹需要迁移，也无已落库成片需要重渲。
>
> ### 2026-10-07 缺陷修复轮第二轮（审计 6 处 + 自查 1 处）
>
> 同样是**未跑过 ffmpeg**：参数串断言 + 由**图本身**反推流长度的算术探针。
>
> - **concat 分支从不混入本镜音轨**（P0）：无转场分支原只对**已累计**的音轨
>   `apad,atrim`，`[a_i]` 从未进入音频链 —— 末几秒是静音而画面正在放有声镜头，
>   而**渲后时长校验照样通过**（时长对得上）。现改为
>   `[ax<i>]aformat` + `[a<i>]aformat` + `concat=n=2:v=0:a=1`，
>   本镜音轨真的进链；concat 前显式统一采样参数，不靠「恰好一致」。
> - **音轨链与权威成片时长分歧**：音频递推原来对「整条和」取 `max(0, …)`，
>   权威函数只封顶**重叠量**。现统一为 `cursor + d_i - min(overlap, cursor)`，
>   并在合成函数里 **fail-closed**：音轨链长与权威值不等即拒绝出参数。
>   探针穷举 27900 组输入（2~3 镜 × 5 种时长 × 6 种转场）：
>   **preflight 放行的 9300 组零分歧**，其余 18600 组非法输入全部被
>   领域层预检或该 fail-closed 守卫拒掉。返回体另给
>   `expected_audio_duration_sec`，**每条流一个数**。
> - **预览失败返回体缺标记**：`rr.update({preview…})` 原本在失败 `return` **之后**，
>   失败响应里没有 `preview`/`deliverable`/`output_kind`，而失败时文件可能已建了一半。
>   现先打标记再判成败，成功与失败响应形状一致。
> - **成片文件名撞名**（`-y` ⇒ 静默覆盖已渲成片）：从自由文本抠整数集号**必然有损**
>   （`S01E02` 与 `S01E03` 都得到 1；`第一集` 与 `第2集` 也撞）。文件名现为
>   `epNN_<可读集标识>-<集身份原文短哈希>_final_tlNN.mp4`：可读性保留，
>   唯一性不再依赖「抠数字猜得对」。
> - **预检与渲染各报一个总时长**：`TimelineRevision.total_duration_sec()`
>   原为朴素求和，同一计划预检报 40.0、渲染声明并校验 38.5。现它只转发到
>   `expected_total_duration_sec()`，预检 / 渲染清单 / `verify_render` /
>   渲后校验基线读的是**同一个数**。
> - **路径与指纹的窄口子**：新增 Windows 保留设备名拒绝（`NUL.mp4` 写不进任何文件
>   却不报错）与长度上限（超长路径原先由 `os.makedirs` 抛 `OSError` ⇒ 500，
>   现在是一条干净的 400）；落盘前复查一次包含关系以**收窄** TOCTOU 窗口
>   （**未关闭**，见 residual）；`silence_declarations` 纳入 `plan_fingerprint`
>   —— ⚠️ **指纹值第二次变更**。
> - **自查发现：输入下标算错导致每镜都读上一镜的文件**（不在原 6 处内，
>   但同属「静默出错且验证层看不见」那一类，故一并修）：
>   视频输入下标原写成 `len(inputs) // 2`，只有视频输入时输入数按 1 递增，
>   于是第 2 镜读第 1 镜的文件、第 3 镜读第 2 镜的 —— 渲出来
>   「顺序看着对、素材全是错的那一版」，计划声明的 `media_version_id`
>   与真正喂给 ffmpeg 的字节对不上，而时长校验照样通过。现按已收集输入个数取下标。
>
> **本轮验证的诚实边界**：以上全部是**参数串与算术**层面的验证。
> `compose_plan_to_ffmpeg_args()` **从未被真实 ffmpeg 执行过一次**，
> 图反推出的流长度依赖「源片音轨确有计划声明那么长」这一前提（见 residual）。


## 背景

**本项目此前没有「时间线」这个概念**（`grep TimelineRevision` = 0 命中）。
后期只是 `video_postprocess.py` 里的 FFmpeg 合并 + 字幕——把已有镜头按顺序拼起来。

三个能力缺口：

1. 不能在不重渲全部镜头的前提下调整顺序 / 时长 / 转场；
2. 不能冻结「这一版剪辑」再渲染（渲染完才知道顺序错了，改了就得全重跑）；
3. 不能分段渲染、只重渲改动段。

第 2 点最贵：GPU 时间是本项目最稀缺的资源，而「顺序错了」恰恰是**渲染之后才发现**
的高频情况（声音对不上、口型对不上、转场突兀）。没有时间线概念，
发现顺序错了只能全量重渲。

## 决策

三个部件：

| 部件 | 作用 |
|---|---|
| `TimelineRevision` | **冻结**镜头序列：每镜采用的 `MediaVersion` id、时长、转场 |
| `compose_fingerprint` | 渲染计划的内容指纹：不变 ⇒ 渲染器可复用；变 ⇒ 必须重渲 |
| `RenderManifest` | 参考自研 `RenderManifest.declare_silence`：把「合法静音」前置到计划里 |

### 铁律 4：TimelineRevision 不可变 —— 改内容 = 建新 revision

**为什么「不改就没法继续加功能」**：若允许原地改顺序，**历史成片将永远对不上它的计划**。

时间线是「这一版剪辑」的**合同**。渲完的成片必须能对上「当时批准的那一版剪辑」——
这是 ADR-0002「批准绑定内容哈希」在时间线侧的延伸：`ApprovalDecision` 批准的是
某一版内容，而那一版内容由 `revision_no` 标识。

一旦允许原地改：
- 上周交付的成片，其 `revision_no = 7`；今天 `revision_no = 7` 的内容已经变了；
- 追溯链断掉——**交付时的追溯链断掉**（无法回答「交付的那一版剪辑当时长什么样」）。

实现上三重锁：
- `frozen dataclass`（内存层不可变）；
- `revision_no` 单调递增 + `parent_revision_id` 血缘（血缘不断）；
- `derive_revision()` 是**唯一**改内容的合法路径；仓库层 DELETE / PUT / PATCH
  一律 **405**（不是 403——方法不存在，不是无权限）；
- 落库前校验 `compose_fingerprint` 自洽（防库被旁路改写）。

`timeline_repo` 与 `facts_repo` **同库不同表**（都在 `facts.db`）：
批准记录也在 `facts.db` 里，分库会让「预检时确认这一镜已批准」变成跨库查询，
而预检是最需要快的路径。`schema_version.module` 区分两者的迁移
（`facts_repo` / `timeline_repo` 各自维护）。

### 铁律 5：合法静音必须**前置**到计划里声明

借鉴自研 `RenderManifest.declare_silence`。

生成阶段**刻意静音是合法状态**——`quality_stage.technical_checks` 里
「音轨只记录、不判失败」就是对它的承认。但**没有音轨**和**声明静音**是两件事：

| | 含义 | 处置 |
|---|---|---|
| 没音轨，也未声明 | **缺陷** | 报错 |
| 没音轨，但已 `declare_silence` | **计划** | 合法 |

所以 `undeclared_silence` 在**预检阶段**阻断，而不是渲染后才发现。
理由很直接：渲完 30 分钟才发现第 12 镜静音是计划外的，整集白渲。
**这条铁律把「渲完才发现」的成本从 30 分钟 GPU 时间降到 0 秒。**

## 为什么不选另一条路

**允许原地改时间线（改了就改了）。** 最省事，也是多数剪辑软件的默认。
但它让「历史成片 ↔ 当时的计划」这个对应关系**永久失去**，交付追溯链断掉。
对本项目更直接的问题是：改顺序后 `compose_fingerprint` 变了，
渲染器无法判断「只是顺序变了」还是「内容也变了」，只能全量重渲——
**于是第 1 个能力缺口（不重渲全部镜头）没有解决，反而被固化。**

**不建新 revision，直接改 + 记个日志。** 有历史但没有可引用的版本号。
追溯时要从日志重建，而日志是「事后叙述」，不保证完整。

**在渲染完成后才校验音轨。** 顺序没错，但成本是**整集 GPU 时间**。
预检只需几毫秒。

**每集一条时间线，不做多版本。** 第一版够用。问题是「顺序错了改一下」是高频操作，
没有多版本就只能原地改，于是回到第一个选项。

**时间线独立成库（不与 facts.db 同库）。** 边界更干净。但预检需要同时确认
「这一镜已采用」（facts）+「这一镜的转场」（timeline）+「整集已批准」（facts），
分库让这个高频查询变成跨库连接。

## 后果

**正面**

- 改顺序 / 时长 / 转场只需重渲改动段（`compose_fingerprint` 指明范围）；
- 成片能对上「当时批准的那一版剪辑」，追溯链完整；
- 静音等「计划性例外」在预检暴露，把 30 分钟 GPU 浪费降到 0；
- `revision_no` + `parent_revision_id` 让「为什么改成这样」可回答。

**负面 / 代价**

- **每改一次顺序就多一个 revision**。改 10 次 = 10 条记录。
  这是**有意的**（历史即证据），但确实需要定期清理策略（本轮未做）。
- 不可变意味着用户**不能「就地撤销」**——只能 `derive_revision` 出新版本。
  交互上需要把「派生新版本」做成一个显式且易理解的按钮。
- `declare_silence` 是**额外的负担**：用户必须为每一个静音镜声明原因。
  没有它，这些镜会被预检拦下。**这是有意的摩擦**——「这镜本来就没声音」
  是需要说清楚的事，不该由系统替用户猜。

**遗留**

- revision 无清理 / 归档策略，长剧项目会累积大量 revision。
- `RenderManifest` 目前只覆盖静音；字幕缺失、分段渲染计划未纳入声明体系。

## 关联决策

- ADR-0002 采用 ≠ 批准（`bound_hash` 与 `compose_fingerprint` 是同一套思路的两处落地）
- ADR-0005 Job / Attempt（`compose_fingerprint` 是免重渲判据在时间线侧的版本）
- ADR-0004 契约优先（`/api/timeline` 契约与生成客户端）

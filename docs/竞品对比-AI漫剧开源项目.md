# AI 漫剧 / 小说转视频开源项目对比

> 核查时间：2026-09-29（Asia/Shanghai）
> **方法**：GitHub REST API 核对仓库存在性与活跃度（stars / forks / 主语言 / 创建与最后推送时间 / 许可证 / 仓库体积），
> 再以 `raw.githubusercontent.com` 拉取各仓库 **README、requirements.txt、生产标准文档** 做技术栈实证。
> **未取到的信息一律显式标注，不做推测。**

## 可信度分档

| 档位 | 项目 | 依据 |
| --- | --- | --- |
| **高（社区验证充分）** | ComfyUI-Novel-Director（212★）、KT-AI-Studio（90★）、comfyui-auto-drama（48★） | 星数/分叉多，README + 源码可验证 |
| **中** | lumenx-comfyui（18★） | 自身星少且 2026-05-14 后停更，但上游是 1315★ 的 [alibaba/lumenx](https://github.com/alibaba/lumenx) |
| **低（个人新项目，无社区信号）** | NiliX（2★）、grokbot-ai-manju（1★）、ai-manga-factory（0★） | 均为 2026-08 后新建，需自行审码 |

## 对比总表

| 项目 | 来源（已核实） | 核心特点 | 真实技术栈（实证） | 适合人群 | 活跃度 |
| --- | --- | --- | --- | --- | --- |
| **comfyui-auto-drama** | [qiukaihui/comfyui-auto-drama](https://github.com/qiukaihui/comfyui-auto-drama) ✅ | 剧本→改写→规范提示词→参考资产（角色四视图/场景/分镜）→批量视频（T2V/I2V/R2V）→自动质检→一键合成；链式衔接（上段末帧接下段首帧）；失败码体系；自动裁掉 H3 开头杂音；README 自述"演示/教学用途" | ComfyUI + **MiniMax H3**（ref2va）+ TurboLoRA/SageAttention；文本走 LM Studio/oMLX/云端；文生图走 Boogu/云端；控制台**纯 Python 标准库** + SQLite | 想用远程 ComfyUI 跑 H3、要轻量控制台的技术用户 | ⭐48 · 14 forks · 2026-08-16 建 · 最后推送 2026-08-26 · MIT |
| **lumenx-comfyui** | [brokenmoonbeam/lumenx-comfyui](https://github.com/brokenmoonbeam/lumenx-comfyui) ✅ | **LumenX Studio 本地化改造版**：完整保留原版 **6 阶段**流程与 UI，把底层生成从阿里云百炼换成 ComfyUI API；工作流映射可配置 | 前端 Next.js 14 + React 18 + TS + Tailwind；后端 FastAPI + Python 3.11；ComfyUI 工作流 API；LLM 走 OpenAI 兼容接口。视频工作流实为 **Wan2.2 / LTX2.3**，生图 Qwen/Z-Image | 想要 LumenX 但必须全本地自建 ComfyUI 的团队 | ⭐18 · 4 forks · 2026-05-14 建并当日停更 · MIT |
| **ComfyUI-Novel-Director** | [Work-Fisher/ComfyUI-Novel-Director](https://github.com/Work-Fisher/ComfyUI-Novel-Director) ✅ | **纯节点包**：多角色选角（单节点 6 人可串联）、三类 JSON 脚本解析（Audio/Visual/Video）、Scene Iterator 自动批量、"一键 100 集"；按音频时长算帧 + Buffer Frames；自动合并 Final_Movie.mp4 回传 | ComfyUI 自定义节点（Python）+ TTS（Qwen-TTS 角色名前缀 / GPT-SoVITS 音色库）+ IP-Adapter 锁脸 + ffmpeg/moviepy；**与视频模型解耦** | 已在用 ComfyUI、做有声小说/动态漫的创作者 | ⭐**212**（最高）· 17 forks · 2026-01-26 建 · 最后推送 2026-02-27（约 7 个月未更新）· **无 license 文件** |
| **NiliX** | [YiIimini/NiliX](https://github.com/YiIimini/NiliX) ✅ | **Go 单服务桌面工作台**：小说管理（书架/阅读器/续写/一键转剧本）+ 六阶段流水线（方案→资产→编码→渲染→质检→合成）+ **智能体审片返工**（剧本师复核→VLM 逐镜判分→修复师定点返工→终审）+ 断点续跑/条件缓存 + 剪映草稿导出 + 自包含 ComfyUI 一键迁移 + 云端 2K 定稿 | **Go** 单服务 + ComfyUI（**MiniMax H3 R2V**）+ MiniMax `/v2/video_regeneration` 云端升 2K + ONNX YuNet 人脸裁剪 + pyJianYingDraft | 单机（自述 RTX 5090 Laptop）跑全流程、不愿打开 ComfyUI 界面的进阶用户 | ⭐2 · 2026-08-17 建 · 最后推送 2026-09-08（活跃）· **无 license 文件** |
| **KT-AI-Studio** | [oskey/kt-ai-Studio](https://github.com/oskey/kt-ai-Studio) ✅ · 官网 [x-kt.com](https://www.x-kt.com) | LLM 负责规划与提示词、ComfyUI 负责本地出图出片；**人物/场景/风格一致性且无需训练 LoRA**；多页面控制台（首页/项目/人物/场景/视频/日志/设置） | **`requirements.txt` 实证**：fastapi + uvicorn + jinja2 + sqlalchemy + openai SDK；ComfyUI API；模型 Qwen-Image / Z-Image / Wan2.2 / LTX2 | 批量出漫画/漫剧分镜的本地创作者（官方建议 LLM 走 API 以免显存打架） | ⭐90 · 17 forks · 2026-02-09 建 · 最后推送 2026-05-26 · **已被 [oskey/Go-Ai-Studio](https://github.com/oskey/Go-Ai-Studio) 取代** · 无 license 文件 |
| **grokbot-ai-manju** | [wicm84266964/grokbot-ai-manju](https://github.com/wicm84266964/grokbot-ai-manju) ✅ | Grok Bot 以**自然语言全流程接管**：整本小说→bible/分集 map→角色三视图+声线→剧本/分镜→关键帧→本地 ComfyUI 出带音频视频→FFmpeg 导出；单一交付出口 `shows/<show>/epXX/export/`；QC + 按 shot_id 单镜重渲 | Grok Bot Skill（ai-manju-pipeline）+ 文件夹契约/轻调度 + 本地 ComfyUI HTTP API + **PowerShell** + FFmpeg + Wan/MiniMax H3 | 想"只说话、丢文件、做审美否决"的 AI 实践者 | ⭐1 · 2026-09-06 建并停更 · **仓库仅 35KB：实质是文档 + prompts + Skill，非完整引擎** · MIT |
| **ai-manga-factory** | [DeanChen85/ai-manga-factory](https://github.com/DeanChen85/ai-manga-factory) ✅ | 单集 V3 / **整季 V4**（主题+梗概+集数+每集秒数→整季圣经/人物声线/世界/场景 + 连续 state chain）；**确定性 H3 导演编译器**产出官方结构提示词；低成本"预演(proof)→哈希绑定内容 QA + 人工晋级→正式生产"，预演永不可交付；SQLite 断点恢复 | Python + **Streamlit** + ComfyUI（H3）+ MiniMax（Anthropic 兼容协议 / MiniMax-M2.7 生成合同）+ FFmpeg/ffprobe + SQLite | 要求"可发布级"交付与人工门禁的严肃本地工作室 | ⭐0 · 2026-08-30 建 · 最后推送 2026-08-31 · Apache-2.0 |

## 与用户原表的出入（需修正的认知）

1. **lumenx-comfyui 不是 MiniMax H3 链路**：它的视频工作流是 **Wan2.2 / LTX2.3**，图像是 Qwen/Z-Image。"6 阶段流程"继承自上游 LumenX Studio，不是自创。
2. **KT-AI-Studio 的"一致性"不靠 LoRA**：靠 LLM 规划 + 资产/提示词约束；且作者已迁到 Go-Ai-Studio，本仓处于维护末期。（"FastAPI + Web 控制台"经 `requirements.txt` 证实 ✅）
3. **ai-manga-factory 的"284 个离线测试"数字不可复现**——但项目**确有大量离线测试**：
   - 仓库简介写 "284 offline tests"，而 README 明确写"**测试总数以 CI 运行结果为唯一事实来源，不在文档手填易过期数字**"，全文搜不到 284；
   - 实测 `tests/` 下 25 个测试文件共 **329** 个 `def test_*`（另有 `pipeline/test_full_user_flow.py`）→ 数量级对得上，具体数字很可能已过期。
   - 但它的"**四层 QA 门禁**"**已证实**：[SHORT_DRAMA_PRODUCTION_STANDARD.md](https://github.com/DeanChen85/ai-manga-factory/blob/main/SHORT_DRAMA_PRODUCTION_STANDARD.md) 第 5 节"四层质量状态"＝ **A. Technical render / B. Automated content QA / C. Editorial review / D. Release approval**。
4. **ComfyUI-Novel-Director 与具体视频模型解耦**：它是节点包，出片模型由你自己的工作流决定——强项在"音画对齐 + 批量"，不自带 H3。
5. **grokbot-ai-manju 不是完整引擎**：仓库仅 35KB，是"文档 + prompts + Grok Bot Skill"的编排契约，落地要靠你自己的 ComfyUI/FFmpeg。

## 命名混淆警告（检索时务必限定词）

- **NiliX** → 会命中 Rust 内核 [Zero-kernel/Nilix](https://github.com/Zero-kernel/Nilix)（23★）以及 [nilix.ai](https://www.nilix.ai/)、[nilix.app](https://nilix.app/) 等无关商业站。本项目是 [YiIimini/NiliX](https://github.com/YiIimini/NiliX)（Go）。
- **KT-AI-Studio** → 会被韩国 KT 电信结果淹没，请用 `kt-ai-Studio` 或 `oskey` 限定。
- **ai-manga-factory** → 至少 3 个同名仓库（[duolaAmengweb3/manga-ai-factory](https://github.com/duolaAmengweb3/manga-ai-factory)、[zkuniii123/ai-manga-factory](https://github.com/zkuniii123/ai-manga-factory)），描述逐字吻合的只有 DeanChen85。
- **comfyui-auto-drama** → 另有 1★ 同名 fork [chenbuting/comfyui-auto-drama](https://github.com/chenbuting/comfyui-auto-drama)，主仓是 qiukaihui。

## 选型建议

- **只想最快跑通"小说→成片"** → comfyui-auto-drama（链路最直、控制台零第三方依赖；README 自述教学用途）。
- **要现成 Web UI 与可替换工作流** → lumenx-comfyui（但 2026-05 停更，需自己接工作流）。
- **已手搓 ComfyUI 工作流，只缺"对齐+批量"** → ComfyUI-Novel-Director（社区最热，212★；注意停更 7 个月）。
- **要独立桌面级管理台、单机一条龙** → NiliX（Go 单服务，但极新、社区几乎为 0）。
- **要批量且风格锁定** → KT-AI-Studio（注意已被 Go-Ai-Studio 取代）。
- **要严格质量门禁、可审计的生产线** → ai-manga-factory（门禁最重，但 0★，需自担风险）。
- **想要对话式操作** → grokbot-ai-manju（极小，适合当思路参考而非生产依赖）。

## 与本仓库（漫剧生成系统）的对照

本仓库同属"ComfyUI + MiniMax H3 全自动漫剧"这一类，差异点：

| 维度 | 本仓库 | 最接近的开源项目 |
| --- | --- | --- |
| 整集一次出片 | **H3 Director 一次提交整集**（`minimax_h3_director_二采_加速.json`，段间原生引导），并保留逐镜/首尾帧两种模式 | NiliX（六阶段）、ai-manga-factory |
| 质量门禁 | 视频/音频/资产逐环质检 + 教训库回写改写提示词 | **ai-manga-factory**（四层 QA：A/B/C/D）最接近 |
| 多集托管生产 | autopilot 24/7 托管、断点续跑、死信挂起与自动复活 | comfyui-auto-drama |
| 离线回归 | `.workbuddy/test/` 下大量 `verify_*.py` 纯离线守卫（本次"视频生成方式"改动即含 33 项断言） | ai-manga-factory（`tests/` 约 329 个测试函数） |
| 视频生成方式 | **项目级单一事实来源**：新建项目时选择，`config.json:video_mode` 一处定义，生成/托管/UI 全部跟随 | 上述项目多数写死在代码或计划里 |

## 本次未覆盖

各项目**成片质量**均为作者自述；本核查只验证**存在性、特性描述、技术栈与活跃度**，未运行任何项目。

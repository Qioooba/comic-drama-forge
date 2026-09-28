"""
小说 → A 版剧本转换器

核心思路（长篇小说必须分块，但**不得删减原小说内容**，只做小说→剧本的体裁改写）：
1. 分块：优先按章节把全文聚合成约 CHUNK_CHARS 字的块；无章节则按段落聚合
2. 全量覆盖：分块只用于控制单次 prompt 体量，**所有块都必须产出结果并合并**，
   块与块连续无缝、不跳段、不抽样（旧的「首尾保留 + 中间均匀抽样」逻辑已取消）
3. 三段式生成：
   ① 逐块提炼（每块一次调用）→ 剧情摘要 / 人物 / 物品 / 场景 / 情节要点
   ② 汇总设定（一次调用）→ title / theme / style / characters[] / items[] / scenes[]（含参考图提示词）
   ③ 逐块写分镜（每块一次调用）→ shots[]（A 版字段，含 prompt_h3 草稿）
4. 组装为系统 A 版剧本 Schema 并落盘 output/scripts
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from datetime import datetime

from llm_client import LLMError, LLMTruncatedError, LLMGatewayUnavailable
from dialogue_utils import dialogue_text as _dlg_text, normalize_lines as _dlg_lines
import fs_atomic
import style_kit
import h3_prompt_kit
import asset_prompt_kit

logger = logging.getLogger(__name__)

# ⚠️ 2026-09-25 实测修正：3000 字 × (1/CHARS_PER_SHOT=120) ≈ 25 镜/块，
#    **正好越过 MAX_SHOTS_PER_CHUNK=24 的单次调用响应体红线** → 预劈半几乎每块都触发
#    （实测日志 32 次「目标 N 镜超过单块上限 24，预拆为 2 个子块」），
#    把「一次调用」变成「先拆再调」，既慢又平白多一层失败面。
#    改为 2400 字 → 约 20 镜/块，首次调用即落在红线内（预劈半退回真正的兜底而非常态）。
#    原文字字不丢：只是块数变多、每块更短，总覆盖不变。
CHUNK_CHARS = 2400
MAX_CHUNKS = 8
MIN_CHUNK_CHARS = 300
MAX_CHARS_PER_CHUNK_PROMPT = 3400
COVERAGE_MAX_ROUNDS = 1          # 原文覆盖率不足时自动补生成轮次上限（与 continuity.COVERAGE_MAX_ROUNDS 对齐）
COVERAGE_THRESHOLD = 0.70        # 原文覆盖率阈值（coverage.py 读取，缺省同值；下方会被 env 覆盖）

# ===================== 每集时长口径（产品需求，2026-09-26） =====================
# ⚠️ 这是**产品口径**，不是技术红线 —— 与下方两条技术红线（H3 资源红线 / LLM 响应体红线）
#    是三个独立维度，别混：
#      · EPISODE_MAX_SEC   = 用户要的「一集多长」（本块）
#      · MAX_SHOTS_PER_EPISODE = H3 连续渲染的资源红线（78 段会崩）
#      · MAX_SHOTS_PER_CHUNK   = 单次 LLM 调用的响应体红线（24 镜）
#
# 需求：**每集 1-2 分钟，最长不超过 3 分钟**。故取
#   - EPISODE_MAX_SEC = 180（3 分钟）＝ 硬上限，拆集判据用它；
#   - EPISODE_TARGET_SEC = 90（1.5 分钟）＝ 期望中位，仅用于日志/展示与质检目标值；
#   - EPISODE_MIN_SEC = 60（1 分钟）＝ 下限参考，低于它的集不被判缺陷（内容自然就这么长）。
#
# ⚠️ 为什么拆集判据用「时长」而不是「镜数」：
#   历史实现用 `ceil(预估镜数 / MAX_SHOTS_PER_EPISODE=78)`，而 78 镜 ≈ 6.5 分钟成片 ——
#   从「集」的产品定义（1-2 分钟一集）看这个阈值形同虚设：实测《逆天系统》每章 1766 字
#   ≈ 15 镜 ≈ 70 秒，**永远触发不了拆集**，一本 42 章的小说会产出 42 集、每集都超出/逼近
#   产品口径。改成按预估成片秒数判据后，集数才真正由「一章能拍几分钟」决定。
#
# 换算依据（用本仓库实测数据标定，不是拍脑袋）：
#   第 1 集实测 63 镜 / 372 秒成片 / 原文 2361 字 → **约 6.35 字原文 = 1 秒成片**，
#   即约 5.9 秒/镜、约 37 字原文/镜。为留出画面细节余量，下方按**保守的**
#   EST_SEC_PER_SHOT=6.0 秒/镜、SEC_PER_SRC_CHAR=1/6 换算：
#     180 秒 ≈ 30 镜 ≈ 1080 字原文
#   比实测密度更保守 → 算出的份数只会偏多（更碎），不会偏少（更不碎）。
#   拆碎是安全的（原文不丢、每集仍完整叙事），拆不够才危险（超出产品口径）。
def _env_pos_int(name: str, default: int, floor: int = 1) -> int:
    """读一个**正整数**环境变量，非法/缺失/≤0 一律回落到 `default`。

    ⚠️ 为什么不写成 `int(os.environ.get(name, default) or default)`：
    那样在 env 设为 **"0"** 时 `"0" or default` 走的是 `"0"`（非空字符串为真）
    → `int("0")` = 0 → 再被 `max(floor, ...)` 抬到 floor，**悄悄变成 floor 而不是默认值**
    （实测 `MJSCXT_EPISODE_TARGET_SEC=0` 会得到 10 而不是 90）。
    这里显式判「解析失败或 ≤0 → 用默认」，语义可预期、可被守卫断言。
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val >= floor else default


def _env_float(name: str, default: float, floor: float = 0.0, ceiling: float = 1.0) -> float:
    """读一个 [floor, ceiling] 区间的浮点环境变量，非法/缺失/越界一律回落 `default`。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if not (floor <= val <= ceiling):
        return default
    return val


EPISODE_MAX_SEC = _env_pos_int("MJSCXT_EPISODE_MAX_SEC", 180, floor=30)
EPISODE_TARGET_SEC = _env_pos_int("MJSCXT_EPISODE_TARGET_SEC", 90, floor=10)
EPISODE_MIN_SEC = _env_pos_int("MJSCXT_EPISODE_MIN_SEC", 60, floor=5)

#: 单镜成片秒数的**拆集规划**用估值（实测 7.28 → 取 7.5）。
#: ⚠️ 与 `estimate_shot_duration`（按台词/画面内容逐镜精算）**不是一回事**：
#:    那个要读镜头内容、用于生成期对账；这个是**不读正文**的前置规划估值，用于算集数。
#: ⚠️ 为什么是 7.5 而不是早先的 6.0：
#:    旧口径第 1 集（2361 字 / 63 镜）平均 5.9 秒/镜，但那次有 **24 个覆盖率补生成镜头**
#:    稀释了密度（补生成镜头承载原文少、时长短）。拆集改造后每集变小、覆盖率一次通过、
#:    无补生成 —— 纯主生成实测（第 1 部分 823 字 / 29 镜）是 **7.28 秒/镜**。
#:    用 6.0 会系统性低估成片时长 → 拆集不足（实测第 1 部分估 138 秒不拆，实际 211 秒）。
try:
    EPISODE_PLAN_SEC_PER_SHOT = max(
        1.0, float(os.environ.get("MJSCXT_EPISODE_PLAN_SEC_PER_SHOT", "7.5") or 7.5))
except (TypeError, ValueError):
    EPISODE_PLAN_SEC_PER_SHOT = 7.5

#: 🔴 **规划用的「每镜承载原文」实测标定值**（2026-09-26 二次修正，本次最关键的一处）。
#:
#: 背景：`CHARS_PER_SHOT=120` 是**模型提示词侧的软引导**（「约每 120 字原文写 1 镜」），
#: 实测**模型根本不遵守** —— 真实生成密度约是它的 **4 倍**：
#:     第 1 部分实测：原文 823 字 → 实际产出 29 镜（≈ **28.4 字/镜**），
#:     而 `estimate_shots_for_chars(823)` 只给出 7 镜 —— 规划值只有实际的 1/4。
#: 后果（本次实测踩到两次）：「按预估秒数拆集」的判据因为预估值腰斩而**永远不触发**：
#:     42 章全部估算 102~162 秒（都在 180 秒上限内）→ 42 章仍是 42 集。
#:     即便第一版改到 36 字/镜，第 1 部分（823 字）仍估 138 秒 < 180 秒不拆，
#:     实际却生成 211 秒（3.5 分钟）—— 因为 36 字/镜来自**旧口径**（含 24 个补生成镜头
#:     的稀释），不是纯主生成的密度。
#:
#: 处置：拆集**规划**改用本常量（纯主生成实测密度）而不是 `CHARS_PER_SHOT`。
#: 为什么不在估计器里直接改 `CHARS_PER_SHOT`：它同时被喂给模型的提示词
#: （`REWRITE_RULES.format(chars_per_shot=...)`）与覆盖率容量校验
#: （`build_chapter_coverage_meta(capacity=n*chars_per_shot)`）消费，
#: 改它会连带改提示词语义与覆盖率口径 —— 那不是这次要动的东西。
#: 因此**只把规划口径独立出来**：两个数字服务两个目的，各自有各自的依据。
#:
#: 取值：实测 823/29 = 28.4 字/镜。取 **26**（略密 = 略偏多估镜数 = 拆得更保险）。
#: 组合密度 = 26 字/镜 ÷ 7.5 秒/镜 ≈ **3.47 字/秒**，比实测 3.9 字/秒保守约 11%，
#: 确保「预估秒数只会偏高、拆集只会偏多」，绝不会拆不够（拆不够会超出 3 分钟上限）。
try:
    EPISODE_PLAN_CHARS_PER_SHOT = _env_pos_int(
        "MJSCXT_EPISODE_PLAN_CHARS_PER_SHOT", 26, floor=1)
except (TypeError, ValueError):
    EPISODE_PLAN_CHARS_PER_SHOT = 26

SHOT_FIELDS_DEFAULT = {
    "episode": 1,
    "duration": 5,
    "camera": "中景",
    "location": "",
    "scene_lighting": "",
    "description": "",
    "dialogue": [],
    "dialogue_text": "",
    "emotion": "平静",
    "edit_reason": "",
    "audio_cues": "",
    "prompt_h3": "",
    "characters_in_shot": [],
    "items_in_shot": [],
    # P0-1：分镜「首帧/末帧/运动」三段结构（借鉴 ViMax decompose_visual_description）。
    # 模型逐镜把单段画面描述拆成「运动起点→终点→运动类型」，下游 build_storyboard_prompt
    # 据此给模型显式锚点，减少动作画崩。旧剧本无此字段 → 空串，下游回落单段 description。
    "first_frame": "",
    "last_frame": "",
    "motion": "",
}

#: **已废弃分镜字段登记表**（结构化，替代注释里的君子协定）。
#: 键 = 字段名；值中的 ``deprecated=True`` 供代码/回归脚本做机器可判定的守卫。
#: 口径：这些字段**不得再写入剧本**，``_norm_shots`` 的字段白名单在标准化时会显式丢弃
#: 模型越界输出的对应字段并留痕；读取侧（coverage / h3_prompt_kit / tts_client）仅为兼容
#: 旧剧本做 legacy 记账，是唯一的合法消费方。
DEPRECATED_SHOT_FIELDS = {
    "narration": {
        "deprecated": True,
        "since": "2026-09-19",
        "reason": (
            "旁白通道已关闭（2026-09-19 产品决策）：剧本阶段不写、配音链路不念、成片不产出旁白。"
            "历史缺陷：narration 曾被当成「心理活动 + 背景补叙 + 环境描写」的公共出口，"
            "实测 ep04 旁白 2231 字 ≈ 496 秒铺在 100 秒画面上（4.93x 溢出），尾部被成片 -shortest "
            "静默截断。现在 _norm_shots 不再透传模型越界输出的 narration，"
            "保证「成片无旁白」是硬不变量；旧剧本残留字段由读取侧按需兼容。"
        ),
    },
}

SYSTEM_BIBLE = ("你是资深漫剧编剧与 AI 绘画提示词工程师，精通把长篇小说改编成可拍摄的漫剧分镜脚本，"
                "并输出严格合法的 JSON。严禁在 content 中输出任何思考过程、英文推理、分析或解释文字，"
                "只允许输出一个可被 json.loads 直接解析的 JSON 对象。")

# ===================== 全量覆盖策略常量（改编 ≠ 缩写，严禁删减原文） =====================

CHARS_PER_SHOT = 120          # 每个镜头承载的原文字数基准（镜头数随内容体量自动扩展，收紧以承载细节）
SHOTS_PER_CHUNK_MIN = 6       # 单块分镜数下限（再短的块也至少这么多镜）

# ---- 单集镜头数硬上限（H3 连续渲染的资源红线，2026-09-24 实测）----
# ⚠️ 背景：整集视频走 H3 连续工作流（84 段一个 prompt，全程约 7 小时）。实测渲染到
# **第 78 段**时 ComfyUI 崩溃：
#     aimdo: xfer_file_read_at: GetOverlappedResult failed error=1450
#     RuntimeError: HostBuffer.read_file_slice failed
# error=1450 = Windows ERROR_NO_SYSTEM_RESOURCES —— 长跑把系统资源（尤其是 ComfyUI 默认
# pin 住的 ram*0.40 ≈ 12.9GB 锁定页）耗尽，异步重叠 I/O 读不进模型权重。
# 根因是**长跑累积**（时间相关，不是段号本身），但表现为「跑到 78 段左右必崩」。
# 因此在剧本阶段就设硬上限：单集分镜数 ≤ MAX_SHOTS_PER_EPISODE，从源头不让任务长到会崩。
# 配套：ComfyUI 侧可加 --disable-pinned-memory 进一步降低资源压力（见项目记忆）。
# 超限处理：不丢原文 —— 按该集总字数等比收紧**每块的镜头额度**（每镜承载更多原文），
# 全部子块仍然逐块送模型、原文覆盖率不受影响，只是镜头密度变稀。
MAX_SHOTS_PER_EPISODE = 78

# ---- 单块镜头数上限（LLM 响应体红线，2026-09-25 实测）----
# ⚠️ 与 MAX_SHOTS_PER_EPISODE 是**两条独立的红线**，别混：
#   · MAX_SHOTS_PER_EPISODE = H3 连续渲染的资源红线（78 段会崩，见上）；
#   · 本常量 = **单次 LLM 调用的响应体红线**。
# ⚠️ 背景（实测）：一次调用要出 49 镜时，agnes 网关 ReadTimeout ——
#     ReadTimeout: host='api.agnes-ai.cn' read timeout=1200
#     等了整整 20 分钟读不完响应，整块失败。
# 根因：`CHARS_PER_SHOT=120` + `CHUNK_CHARS=3000` → 一块 ~25 镜；两章聚合即 49 镜。
# 而 `shots_cap = min(120, target*2+3)` 让模型**被允许**输出到 101 镜 → 响应体更大。
# 处置：单块目标镜数超过本阈值时，**先二分再送模型**（原文字字不丢，只是拆成多次调用）。
# 之所以要「预劈半」而不是「等出错再二分」：劈半原本只挂在 LLMTruncatedError 上，
# 而超时抛的是 LLMError/LLMGatewayUnavailable → **不触发劈半**，直接整块失败（本次即此坑）。
# 设为 0 = 关闭（退回旧行为，只靠截断兜底）。
try:
    MAX_SHOTS_PER_CHUNK = max(0, int(os.environ.get("MJSCXT_MAX_SHOTS_PER_CHUNK", "24") or 24))
except (TypeError, ValueError):
    MAX_SHOTS_PER_CHUNK = 24

# ---- 每章**至少**拆成的集数（默认 1 = 不强制拆，纯按内容判断）----
# ⚠️ 2026-09-25 策略变更：由「一章固定拆 2 集」改为**纯按内容自动判断**。
#
# 旧行为（=2）：MAX_SHOTS_PER_EPISODE 只是「兜底硬上限」，章节短时它根本不触发，
#   所以曾再加一条固定份数规则强行拆开。实测后果：本项目 42 章平均 1766 字、
#   预估 15 镜/章，**远低于 78 镜上限**，却在旧规则下被硬拆成 2 集 ——
#   每集只剩 7~8 镜（约 35 秒），集与集之间还在句中断开，
#   既不是「一集」该有的体量，也破坏了叙事完整性（用户实测反馈）。
#
# 新行为（=1）：集数**完全由内容体量决定** ——
#   `parts = ceil(预估镜头数 / MAX_SHOTS_PER_EPISODE)`，
#   只有预估镜头数真的超过单集硬上限时才拆，且拆出的每段都不超上限。
#   这与业界漫剧/短剧项目的通行做法一致（一集 = 一个完整叙事单元，
#   约 1.5~3 分钟；内容不够就不硬凑集数）。
#
# 本常量现在退化为**「每章至少拆 N 集」的下限**，仅供需要「无论长短都拆」的场景
# 通过 env 显式开启（MJSCXT_EPISODES_PER_CHAPTER=2）。
# 取更碎的那个：parts = max(本下限, 内容算出的份数)，因此调大它只可能更碎，不会更碎不动。
try:
    EPISODES_PER_CHAPTER = max(1, int(os.environ.get("MJSCXT_EPISODES_PER_CHAPTER", "1") or 1))
except (TypeError, ValueError):
    EPISODES_PER_CHAPTER = 1

# ---- 分镜阶段的 token 预算（必须给「思考」留预留量）----
# ⚠️ always-on reasoning 模型（agnes-3.0-flash / GLM 系）在写分镜前会先输出一大段思考，
# 实测该任务的思考量 ≈16K token。若 max_tokens 低于思考量，模型会「只吐思考、正文为空」，
# 表现为整集卡死（2026-09-23 端到端复测的核心阻塞）。此前按 shots_target*300+1200 给
# （12 镜 → 4800）远低于水位，故改为「固定思考预留 + 每镜正文额度」。
SHOTS_THINKING_RESERVE = 16384   # 思考预留（与 llm_client.REASONING_ONLY_TOKEN_FLOOR 对齐）
SHOTS_TOKENS_PER_SHOT = 300      # 单镜正文额度（description+visual_detail+dialogue+audio_cues 实测够用）
COVERAGE_THRESHOLD = _env_float("MJSCXT_COVERAGE_THRESHOLD", 0.70, floor=0.0, ceiling=1.0)  # 原文覆盖率阈值：低于该值自动补生成缺失片段（压缩提炼后只需覆盖情节单元，不再要求逐句 95%）

# ===================== 提炼 / 分镜阶段的断点缓存（对抗网关偶发挂起） =====================
# 背景（2026-09-24 实测）：网关上游偶发挂起 —— 连 max_tokens=3600 的小请求也会挂满
# 1200s read timeout（而同一时刻 16384 额度的大请求 258s 就返回了），说明**与请求体量无关**。
# 而本阶段单个请求体量本来就大（思考预留 16384 + 每镜 300），一集完整生成要 20+ 分钟；
# pipeline 的重试又是**整个 script 步骤重来**（`_run_step_with_retry`）——没有缓存时
# 每次重试都要把 提炼 + 设定 + 全部分镜 重新跑一遍，网关一抖就永远跑不完。
#
# 这里按「prompt 内容指纹」做**内容寻址**落盘，重跑时命中即跳过模型调用：
#   - 任意输入（原文 / 镜头额度 / 风格 / 提示词模板本身）变化 → 指纹随之变化 → 自动失效，
#     绝不会用旧结果冒充新结果（把 prompt 整体入指纹，是防止「改了提示词却命中旧缓存」的关键）；
#   - 网关抖动的净效果从「永远跑不完」变成「多跑几次总能跑完」。
# 缓存只写不读回主链路语义 —— 未命中时行为与加缓存前**完全一致**。
# 用 MJSCXT_SHOTS_CACHE=0 可关闭（离线测试 / 需要强制重新生成时）。
SHOTS_CACHE_ENV = "MJSCXT_SHOTS_CACHE"


def _shots_cache_enabled() -> bool:
    """缓存开关：默认开启，环境变量置 0/false/no/off 时关闭。"""
    return str(os.environ.get(SHOTS_CACHE_ENV, "1")).strip().lower() not in (
        "0", "false", "no", "off")


def _cache_file(cache_dir: str, kind: str, prompt: str) -> str:
    """内容寻址缓存路径：文件名 = sha1(kind + prompt) 前 20 位。"""
    digest = hashlib.sha1(f"{kind}\x00{prompt}".encode("utf-8")).hexdigest()[:20]
    return os.path.join(cache_dir, f"{kind}_{digest}.json")


def _shots_cache_root(continuity_dir: str, key: str) -> str:
    """**整本路径**的断点缓存目录（``<continuity>/<项目>/shots_cache/whole``）。

    ⚠️ 与「按章分集」路径的 ``continuity.shots_cache_dir(...)``（``.../shots_cache/epNN``）
    刻意分开放：两条路径的 prompt 体量、块划分、shots_target 完全不同，
    混在一个目录里虽然因内容寻址不会串味，但排查「这次为什么没命中」时无法区分来源。

    缓存是纯加速手段：目录不可用（权限/磁盘）时返回 ""，调用方按「不落缓存」继续跑，
    **绝不让缓存问题阻断主链路**（与 `_shots_cache_enabled` 的取舍一致）。
    """
    if not continuity_dir or not key:
        return ""
    try:
        root = os.path.join(continuity_dir, safe_project_name(key), "shots_cache", "whole")
        os.makedirs(root, exist_ok=True)
        return root
    except Exception as e:  # noqa: BLE001
        logger.warning(f"整本路径缓存目录不可用（本次不落缓存）：{e}")
        return ""


def _cache_read(path: str):
    """读缓存：不存在 / 损坏 / 非 dict 一律当作**未命中**。

    缓存是「只读加速视图」，不是主链路状态 —— 因此损坏时按未命中重新生成，
    而不是 fail-loud 中断整集（与 `fs_atomic` 对主链路文件的 fail-loud 口径区分开）。
    """
    try:
        data = fs_atomic.read_json_strict(path, None)
    except Exception as e:  # noqa: BLE001 —— 缓存损坏仅降级为未命中
        logger.warning(f"提炼/分镜缓存不可用，按未命中重新生成：{os.path.basename(path)}：{e}")
        return None
    return data if isinstance(data, dict) else None


def _cache_write(path: str, payload: dict) -> None:
    """写缓存：失败只告警，绝不影响本次产出（缓存是加速手段，不是必需产物）。"""
    try:
        fs_atomic.atomic_write_json(path, payload)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"写提炼/分镜缓存失败（忽略，不影响本次产出）：{e}")


def _cache_get(cache_dir: str, kind: str, prompt: str, events: list, label: str):
    """命中则返回缓存 dict，否则 None（未命中不产生任何副作用）。"""
    if not cache_dir or not _shots_cache_enabled():
        return None
    path = _cache_file(cache_dir, kind, prompt)
    hit = _cache_read(path)
    if hit is None:
        return None
    logger.info(f"{label} 命中断点缓存，跳过模型调用：{os.path.basename(path)}")
    if events is not None:
        events.append({"label": label, "event": "cache_hit", "kind": kind})
    return hit


def _cache_put(cache_dir: str, kind: str, prompt: str, payload: dict) -> None:
    if not cache_dir or not _shots_cache_enabled():
        return
    _cache_write(_cache_file(cache_dir, kind, prompt), payload)


REWRITE_RULES = (
    "【改写规则（这是压缩提炼，不是逐句照搬：只保留推动剧情的信息，删掉纯背景铺陈）】\n"
    "1) 先提炼：把本段原文压缩成 3~5 句剧情梗概，只保留「冲突、转折、关键动作、金句」四类信息；"
    "世界观、势力背景、环境补叙、器物来历等**不推进剧情**的描写，一律不逐句复述、不单独成镜；\n"
    "2) 再落镜：把梗概里的关键情节改写成镜头。心理活动→可拍的表情/动作或该角色第一人称自语，"
    "叙述→画面动作，环境→画面与音效；**禁止**把第三人称背景补叙、世界观说明原样写进 description 当画面；\n"
    "3) 【信息密度·单镜 ≤3 条】每个镜头只承载 ≤3 条核心信息（人物动作 / 台词 / 关键环境 各算 1 条）；"
    "一条信息用一个画面能讲清的，绝不拆成两镜；**纯环境空镜**（无人、无动作、无信息推进）禁止生成；\n"
    "4) 【描述/对白比】剧情主要由人物台词与动作推进，画面描述只做必要补充："
    "全块所有镜头 description+visual_detail 的合计字数不得超过 dialogue 合计字数的 3 倍；"
    "无台词的镜头必须有明确的人物动作或情绪变化，禁止写成静态环境铺陈；\n"
    "5) 原文对话尽量原样写进对应角色的 dialogue.text，禁止改写成概括式引述；\n"
    "6) 镜头按原文时间顺序排列，块首镜头自然衔接上一块结尾，不得跳段、不得重复；\n"
    "7) 【本系统不产出旁白】成片没有画外音解说：背景补叙、环境描写一律靠画面承载（且只保留推进剧情的部分），"
    "心理活动靠神态动作或第一人称角色自语承载；**禁止**把第三人称叙述/背景补叙硬转成角色开口的台词——"
    "dialogue 只承载两种内容：原文的对话，以及原文明确的心理活动/独白改写的**第一人称**角色自语。"
    "判断标准：这句话由该角色以第一人称自然说出才可进 dialogue；"
    "全知视角的交代句（世界观、势力背景、环境说明、「XX大陆人人习武」这类）"
    "只能写进 description / audio_cues（且仅保留推进剧情的部分），绝不进 dialogue；\n"
    "8) 自检：写完一块后回看梗概，确认每个关键情节都有对应镜头；"
    "纯背景补叙、纯环境描写若未推进剧情，应当已删去，**不要求「逐句覆盖原文」**。\n"
    "9) 【镜头语言克制】运镜以固定为主（约 75%），推/拉/摇/手持/跟随仅在情绪递进或空间转换时用；"
    "景别以中景/中近景/近景为主（约 80%），全景与特写做情绪锚点（各 ≤ 3%），"
    "局部近景用于情感道具回环；每个镜头的 edit_reason 字段写一句剪辑动机（15 字以内，"
    "如「用背影暂缓解释」「情绪停在等待而非眼泪」「道具回环推进信任弧线」），"
    "不解释给观众，是给构图与取舍的依据。\n"
    "10) 【视觉锚点/道具回环】识别原文中反复出现的关键道具（如印章、信物、武器、食物、书信等），"
    "把它作为跨镜头视觉锚点：每次该道具出现时，在 description 的构图描述中明确写道具"
    "在画面中的位置与状态变化（如「纸币被折叠/展开/递出/攥紧」），"
    "让道具成为观众追踪情感或信任弧线的视觉线索；"
    "同一道具全段出现 ≥ 2 次时，在首次出现的镜头 description 末尾加「（视觉锚点）」标注，"
    "后续每次出现的镜头 description 末尾加「（视觉锚点·第 N 次）」，N 从 2 起算。\n"
    "11) 【节拍识别·节奏分层】先判断本段属于哪种叙事节拍，再把节拍落成镜头切分与时长：\n"
    "    · 节拍四段：开场（建立情境/人物/悬念）→ 触发（矛盾出现/目标确立）→ 高潮（冲突爆发/反转/关键动作，"
    "      全段情绪与张力的最高点）→ 收尾（结果落地/钩子留白/接下一段）；本段不一定四段齐全，按原文实际节奏取舍。\n"
    "    · 节拍边界**强制切段**：从一个节拍切换到下一个节拍（尤其进入/离开「高潮」）处，"
    "      镜头边界必须落在节拍切换点上——绝不让「高潮爆发」与「收尾」挤在同一镜内，"
    "      也不让铺垫（开场/触发）与高潮并镜。\n"
    "    · 爆点镜时长加成：处于「高潮」节拍的镜头（冲突爆发/反转/关键动作/金句落点）"
    "      应取该档位**上限附近**的时长（节奏上需要停留，让观众看清动作与反应）；"
    "      「触发/开场/收尾」等过渡与铺垫镜则取较短时长，快切推进。"
    "      具体档位由程序按本镜内容自动估算，你只需把高潮镜的画面信息写足、把过渡镜写紧凑。"
)


# ===================== 分块与全量覆盖 =====================

def build_chunks(text: str, chapters: list, chunk_chars: int = CHUNK_CHARS) -> list:
    """把全文切成若干块（优先按章节边界聚合）"""
    text = text or ""
    if not text.strip():
        return []

    chunks = []
    if chapters:
        buf_text, buf_start, buf_chars, from_ch, to_ch = [], None, 0, None, None
        for ch in chapters:
            seg = text[ch["start"]:ch["end"]]
            if buf_start is None:
                buf_start, from_ch = ch["start"], ch["index"]
            buf_text.append(seg)
            buf_chars += len(seg)
            to_ch = ch["index"]
            if buf_chars >= chunk_chars:
                chunks.append({
                    "text": "".join(buf_text).strip(),
                    "char_count": buf_chars,
                    "from_chapter": from_ch,
                    "to_chapter": to_ch,
                })
                buf_text, buf_start, buf_chars = [], None, 0
        if buf_text and buf_chars > 0:
            chunks.append({
                "text": "".join(buf_text).strip(),
                "char_count": buf_chars,
                "from_chapter": from_ch,
                "to_chapter": to_ch,
            })
    else:
        paras = [p for p in re.split(r"\n{1,}", text)]
        buf, buf_chars = [], 0
        for p in paras:
            buf.append(p)
            buf_chars += len(p) + 1
            if buf_chars >= chunk_chars:
                chunks.append({"text": "\n".join(buf).strip(), "char_count": buf_chars,
                               "from_chapter": None, "to_chapter": None})
                buf, buf_chars = [], 0
        if buf:
            chunks.append({"text": "\n".join(buf).strip(), "char_count": buf_chars,
                           "from_chapter": None, "to_chapter": None})

    # 零丢弃：过短的块并入相邻块（原实现用过滤丢弃，会把整章/整段正文静默舍弃）
    if len(chunks) >= 2:
        merged = []
        for c in chunks:
            if merged and len(c["text"]) < MIN_CHUNK_CHARS:
                prev = merged[-1]
                prev["text"] = (prev["text"] + "\n" + c["text"]).strip()
                prev["char_count"] += c["char_count"]
                prev["to_chapter"] = c.get("to_chapter") or prev.get("to_chapter")
            else:
                merged.append(c)
        if len(merged) >= 2 and len(merged[0]["text"]) < MIN_CHUNK_CHARS:
            # 首块过短：并入后一块，保证正文开头不被丢弃
            merged[1]["text"] = (merged[0]["text"] + "\n" + merged[1]["text"]).strip()
            merged[1]["char_count"] += merged[0]["char_count"]
            merged[1]["from_chapter"] = (merged[0].get("from_chapter")
                                         or merged[1].get("from_chapter"))
            merged.pop(0)
        chunks = merged
    for i, c in enumerate(chunks):
        c["index"] = i + 1
        c["total"] = len(chunks)
        c["title"] = _chunk_title(c)
    return chunks


def _chunk_title(c: dict) -> str:
    first = (c.get("text") or "").split("\n")[0].strip()
    if c.get("from_chapter"):
        if c["from_chapter"] == c.get("to_chapter"):
            return f"第{c['from_chapter']}章"
        return f"第{c['from_chapter']}-{c['to_chapter']}章"
    return first[:24] or f"第{c['index']}块"


def sample_chunks(chunks: list, max_chunks: int = MAX_CHUNKS):
    """均匀抽样（必含首块与末块），返回 (抽样块列表, 抽样下标 1-based)"""
    n = len(chunks)
    if n <= max_chunks:
        return list(chunks), [c["index"] for c in chunks]
    step = (n - 1) / (max_chunks - 1)
    idxs = sorted({int(round(i * step)) for i in range(max_chunks)})
    idxs[0], idxs[-1] = 0, n - 1
    if len(idxs) > max_chunks:  # 极端情况去重后再补
        idxs = idxs[:max_chunks - 1] + [n - 1]
    return [chunks[i] for i in idxs], [chunks[i]["index"] for i in idxs]


# ===================== 三段式生成 =====================

def _as_dict(data) -> dict:
    """模型返回容错：兼容部分模型把 JSON 对象包在数组里返回（[{"...": ...}]）的情况。"""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                return it
    return {}


def _bare_list_to_bible(rows) -> dict:
    """模型只返回了某个数组（未包成完整对象）时的兜底归位。

    按首元素的字段特征判断该数组属于 characters / items / scenes 中的哪一类，
    避免整链路因为“少了一层对象”而中断。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return {}
    sample = rows[0]
    if any(k in sample for k in ("category", "owner")):
        return {"items": rows}
    if "location" in sample and "appearance" in sample:
        return {"scenes": rows}
    return {"characters": rows}


def _normalize_bible(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return _bare_list_to_bible(raw)
    return {}


# ===================== 截断自适应（A 项③） =====================
# 单块送模型时若输出被截断（finish_reason=length），除了自动提高 max_tokens 重试外，
# 仍失败则把该块再二分（最多 CHAPTER_SPLIT_MAX_DEPTH 层）后分别生成再合并，保证不静默失败。
CHAPTER_SPLIT_MAX_DEPTH = 3
CHAPTER_SPLIT_MIN_CHARS = 400     # 低于该字数的子块不再继续二分（避免无意义碎片）


def _robust_json(client, prompt: str, system: str, temperature: float, max_tokens: int,
                 events: list = None, label: str = "",
                 max_attempts: int = 3, token_ladder=None) -> dict:
    """统一 JSON 调用入口：走 chat_json_robust（截断自动提额重试）并记录重试事件

    max_attempts / token_ladder 可按阶段覆盖默认重试策略（如 bible 汇总阶段输出更长，
    需要更高的提额上限与更多尝试次数）。
    """
    robust = getattr(client, "chat_json_robust", None)
    if robust is None:                      # 兼容旧客户端
        return client.chat_json(prompt, system=system, temperature=temperature,
                                max_tokens=max_tokens)
    kw = {}
    if token_ladder:
        kw["token_ladder"] = tuple(token_ladder)
    if max_attempts:
        kw["max_attempts"] = int(max_attempts)

    def _on_event(h):
        if events is not None and int(h.get("attempt") or 1) > 1:
            events.append({"label": label, "attempt": h.get("attempt"),
                           "max_tokens": h.get("max_tokens"),
                           "finish_reason": h.get("finish_reason"),
                           "truncated": bool(h.get("truncated"))})

    try:
        return robust(prompt, system=system, temperature=temperature,
                      max_tokens=max_tokens, on_event=_on_event, **kw)
    finally:
        meta = getattr(client, "last_json_meta", None)
        if events is not None and isinstance(meta, dict) and int(meta.get("attempts") or 0) > 1:
            seen = {(e.get("label"), e.get("attempt")) for e in events}
            if (label, meta.get("attempts")) not in seen:
                events.append({"label": label, "attempt": meta.get("attempts"),
                               "max_tokens": meta.get("max_tokens"),
                               "finish_reason": meta.get("finish_reason"),
                               "truncated": bool(meta.get("truncated"))})


def _gateway_down_fallback(e, chunk: dict, per_chunk: int, bible: dict,
                           produced: list) -> list:
    """「LLM 网关不可用」时的统一处置（不是普通的单块失败）

    实测教训：网关上游没算力时，每一块都会失败 → 每块都按原文兜底 →
    整集 6/6 兜底，脚本却仍记成「生成成功」。这种剧本没有分镜设计、没有台词，
    配音链路读不到 dialogue 就只出 1 句，用户根本无从判断哪一镜有问题。
    所以分两种情况：
    - **还没产出任何真实镜头** → 直接失败。产出兜底剧本比报错更糟。
    - **已有真实产出** → 剩余块兜底，保住已完成部分，并在 warnings 里如实标注降级。
    """
    if not produced:
        hint = getattr(e, "hint", "") or "请到「AI 设置」检查 base_url / model 是否可用"
        raise LLMError(
            "LLM 网关不可用，已中止生成：继续下去只会得到一份「无分镜、无台词」的"
            f"原文兜底剧本（该剧本配音只能出 0 句）。原因：{e}　处置建议：{hint}"
        ) from e
    return _fallback_shots_for_chunk(chunk, per_chunk, bible)


def _gateway_down_warning(chunk: dict, n_fb: int) -> str:
    return (f"⚠ LLM 网关不可用：第 {chunk['index']} 块已按原文兜底生成 {n_fb} 镜。"
            "本集为**降级产出**（该块无分镜设计、无台词），网关恢复后建议重跑本集。")


def _split_chunk_in_half(chunk: dict) -> list:
    """把子块按中点附近的句末标点一分为二（保证不丢字），用于截断后的进一步切分"""
    text = str(chunk.get("text") or "")
    if len(text) < CHAPTER_SPLIT_MIN_CHARS * 2:
        return []
    mid = len(text) // 2
    cut = mid
    for m in re.finditer(r"[。！？；!?;\n]", text):
        if m.end() >= mid:
            cut = m.end()
            break
    parts = [text[:cut].strip(), text[cut:].strip()]
    out = []
    for i, p in enumerate(parts):
        if len(p) < CHAPTER_SPLIT_MIN_CHARS // 2:
            continue
        out.append({**chunk, "text": p, "char_count": len(p),
                    "title": f"{chunk.get('title') or '块'}·{i + 1}",
                    "_split_from": chunk.get("title")})
    return out if len(out) == 2 else []


def _merge_outlines(outlines: list, chunk: dict) -> dict:
    """把同一原始子块的多个子-提炼结果合并为一个 outline（人物/物品/场景按名去重）"""
    merged = {"summary": "", "characters": [], "items": [], "scenes": [], "key_beats": []}
    summaries = []
    seen = {k: set() for k in ("characters", "items", "scenes")}
    for o in outlines:
        if not isinstance(o, dict):
            continue
        if o.get("summary"):
            summaries.append(str(o["summary"]))
        for key in ("characters", "items", "scenes"):
            for row in (o.get(key) or []):
                if not isinstance(row, dict):
                    continue
                nm = str(row.get("name") or "").strip()
                if not nm or nm in seen[key]:
                    continue
                seen[key].add(nm)
                merged[key].append(row)
        for b in (o.get("key_beats") or []):
            if str(b).strip():
                merged["key_beats"].append(str(b))
    merged["summary"] = "；".join(summaries)[:200]
    # 全量覆盖：情节要点不再压到 5 条（避免把长块内容压缩丢失），容纳整段全部节点
    merged["key_beats"] = merged["key_beats"][:14]
    merged["_chunk"] = {"index": chunk.get("index"), "title": chunk.get("title"),
                        "char_count": len(str(chunk.get("text") or "")), "sub_split": True}
    return merged


def extract_chunk_outline(client, chunk: dict, novel_title: str,
                          events: list = None, depth: int = 0,
                          cache_dir: str = "") -> dict:
    """① 单块提炼（截断时自动提高 max_tokens；仍截断则把该块再二分后合并）

    cache_dir 非空时启用断点缓存：命中即跳过模型调用（见文件上方缓存说明）。
    """
    body = chunk["text"][:MAX_CHARS_PER_CHUNK_PROMPT]
    prompt = f"""【任务】下面是长篇小说《{novel_title}》的第 {chunk['index']}/{chunk['total']} 段原文（{chunk.get('title')}，约 {len(body)} 字），请提炼改编漫剧所需的辅助信息（人物 / 物品 / 场景 / 剧情摘要 / 关键情节节点）。本提炼只作分镜阶段的辅助索引：分镜阶段会拿到本段完整原文，因此这里**不需要逐句复述原文**，但也不得删改原意。
【原文开始】
{body}
【原文结束】
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释文字，结构如下：
{{
  "summary": "本段剧情摘要，120 字以内",
  "characters": [{{"name": "人物名", "role": "主角/配角/反派", "gender": "性别，只填「男」或「女」（必须按原文称谓/代词推断给出）", "appearance": "静态外貌（发色/瞳色/脸型/体格等**不随剧情变化的定妆特征**，**不含会随集变化的服饰/配饰**），35 字以内", "personality": "性格，20 字以内"}}],
  "items": [{{"name": "物品名", "category": "武器/法宝/道具/服饰", "appearance": "外观，30 字以内", "importance": "重要/临时。判定三问（任一答案为「是」即判临时、宁缺勿滥）：①删掉它剧情还成立吗？②它只是随手用的日常物品吗？③它只是场景陈设吗？只有推动剧情且后续会反复出现/被反复指认的关键道具才判「重要」"}}],
  "scenes": [{{"name": "场景名", "location": "地点类型", "appearance": "环境特征，30 字以内"}}],
  "key_beats": ["按原文顺序列出本段关键情节节点，每条 30 字以内，最多 12 条（分镜阶段会读取完整原文，这里只做索引，不要逐句复述、不要写成英文）"]
}}
【道具三问过滤】items 最多输出 3 条（宁缺勿滥）：只保留通过三问的重要道具（见上方 importance 判定），临时道具一律不进 items。"""
    label = f"outline#{chunk.get('index')}"
    hit = _cache_get(cache_dir, "outline", prompt, events, label)
    if hit is not None and hit.get("_chunk"):
        return hit
    try:
        data = _as_dict(_robust_json(client, prompt, system=SYSTEM_BIBLE, temperature=0.35,
                                     max_tokens=3000, events=events, label=label,
                                     max_attempts=3, token_ladder=(4096, 8192, 16384)))
    except LLMTruncatedError:
        subs = _split_chunk_in_half(chunk) if depth < CHAPTER_SPLIT_MAX_DEPTH else []
        if not subs:
            raise LLMTruncatedError(
                f"{label}（{chunk.get('title')}，{len(chunk.get('text') or '')} 字）"
                f"在自动提高 max_tokens 后仍被截断，且已无法继续二分（depth={depth}）") from None
        logger.warning(f"{label} 提炼输出被截断，自动二分为 {len(subs)} 个子块重试（depth={depth}）")
        if events is not None:
            events.append({"label": label, "event": "sub_split", "depth": depth,
                           "parts": [len(s.get("text") or "") for s in subs]})
        return _merge_outlines(
            [extract_chunk_outline(client, s, novel_title, events, depth + 1, cache_dir)
             for s in subs], chunk)
    data["_chunk"] = {
        "index": chunk["index"],
        "title": chunk.get("title"),
        "char_count": len(body),
    }
    _cache_put(cache_dir, "outline", prompt, data)
    return data


def _ctx_block(ctx, key: str) -> str:
    """取跨集连贯性上下文块（A/B/C 方案的 prompt 注入片段），无则返回空串"""
    if not isinstance(ctx, dict):
        return ""
    val = ctx.get(key)
    return str(val).strip() if val else ""


def _ctx_line(ctx, key: str) -> str:
    v = _ctx_block(ctx, key)
    return (v + "\n") if v else ""


def _ctx_asset_names_block(ctx) -> str:
    """P3 名称归一化去重：把「已登记资产名单」投影进 ②汇总 prompt。

    取 ``continuity_ctx['bible']``（项目级设定库，A① 持久化）里已锁定的角色/物品/场景规范名，
    让 LLM 在汇总时**复用这些规范名**、不要另造近名（如已有「古月方源」就别再写「方源」，
    否则下游会重复生成参考图）。纯 prompt-only：不新增 import（``continuity.py`` 已
    ``import novel_to_script``，反向会循环导入），也不动 ``align_script_assets`` 的确定性
    兜底（它仍是最后一道归一闸，本块只是让 LLM 在前置阶段就收敛到规范名）。

    ``ctx`` 为 None / 无 ``bible`` / 名单全空时返回空串 —— 首集（无跨集上下文）行为零变化。
    """
    if not isinstance(ctx, dict):
        return ""
    bible = ctx.get("bible")
    if not isinstance(bible, dict):
        return ""

    def _names(key):
        rows = []
        for r in (bible.get(key) or []):
            if isinstance(r, dict):
                nm = str(r.get("name") or "").strip()
                if nm:
                    rows.append(nm)
        return rows[:12]

    chars, items, scenes = _names("characters"), _names("items"), _names("scenes")
    if not (chars or items or scenes):
        return ""
    lines = ["【已登记资产名单（跨集锁定，必须复用规范名、禁止另造近名）】"]
    if chars:
        lines.append("角色：" + "、".join(chars))
    if items:
        lines.append("物品：" + "、".join(items))
    if scenes:
        lines.append("场景：" + "、".join(scenes))
    lines.append("硬约束：characters[].name / items[].name / scenes[].name 若与上方名单指向同一对象，"
                 "必须逐字复制该规范名（含姓氏/全称，不得写简称或去姓别名）；"
                 "只有原文出现名单之外的确凿新对象时才新增条目，且不得与已有近名重复。")
    return "\n".join(lines)


def _prev_tail_block(prev_chunk: dict, prev_outline: dict) -> str:
    """P1-2 长文「接缝重叠」注入（借鉴 ViMax novel_compressor 的 overlap 思路，
    按本系统架构落地为「集内相邻块接缝上下文」）。

    写第 i 块分镜时，注入**上一块（i-1）的结尾**：已 LLM 提炼好的剧情摘要 + 末 2 条
    情节要点 + 末场景。让 LLM 知道「上段发生到哪了」，使本块开头能**承接**而非
    凭空重启——治「章节衔接硬 / 长文丢主线」（相邻块各自独立写、互不知道对方结尾）。

    纯 prompt-only、零新增 LLM 调用：上块 outline 在 ① 提炼阶段已生成，此处只取用。
    向后兼容：``prev_chunk`` / ``prev_outline`` 为 None（首块 i=0、或递归子块不传）时
    返回空串 → prompt 不出现该段、行为与旧版完全一致。

    关键措辞：这是**衔接锚点，不是重演指令**——显式要求「承接上段结尾，勿重演上段
    已发生的事件」（与「上一集已发生事件禁止重演」同口径，避免 LLM 把上段再写一遍）。
    """
    if not isinstance(prev_chunk, dict) and not isinstance(prev_outline, dict):
        return ""
    lines = []
    summary = str((prev_outline or {}).get("summary") or "").strip()
    beats = list((prev_outline or {}).get("key_beats") or [])
    tail_beats = beats[-2:] if beats else []
    prev_title = str((prev_chunk or {}).get("title") or "上一段")
    if summary or tail_beats:
        lines.append(f"【承接上段（{prev_title}结尾）——只用于让本段开头衔接自然，"
                     "切勿重演上段已发生的事件】")
        if summary:
            lines.append("上段剧情摘要：" + summary)
        if tail_beats:
            lines.append("上段最后情节要点：" + "；".join(
                str(b).strip() for b in tail_beats if str(b).strip()))
        prev_loc = str((prev_chunk or {}).get("to_chapter") or "").strip()
        if prev_loc:
            lines.append("上段所在章节：第" + prev_loc + "章")
    return "\n".join(lines)


def build_bible(client, outlines: list, novel_title: str, style: str, episodes: int,
                target_shots: int, events: list = None, continuity_ctx: dict = None,
                cache_dir: str = "") -> dict:
    """② 汇总全剧设定，产出 A 版 characters/items/scenes

    continuity_ctx 非空时（跨集连贯性方案 A①②③）：注入项目级设定库（角色外观锁定）、
    上集摘要卡与衔接契约，使本集设定与既有跨集设定保持一致。
    """
    digest = []
    for o in outlines:
        if not isinstance(o, dict):
            continue
        digest.append({
            "段": o.get("_chunk", {}).get("index"),
            "摘要": o.get("summary", ""),
            "人物": [{"name": c.get("name"), "role": c.get("role"), "gender": c.get("gender"), "appearance": c.get("appearance")}
                     for c in (o.get("characters") or [])[:6] if isinstance(c, dict)],
            "物品": [{"name": i.get("name"), "category": i.get("category"), "appearance": i.get("appearance"),
                     "importance": i.get("importance", "")}
                    for i in (o.get("items") or [])[:3] if isinstance(i, dict)],
            "场景": [{"name": s.get("name"), "appearance": s.get("appearance")}
                     for s in (o.get("scenes") or [])[:6] if isinstance(s, dict)],
            "情节要点": (o.get("key_beats") or [])[:5],
        })
    prompt = f"""【任务】以下是长篇小说《{novel_title}》各段落的提炼结果（JSON）。请把它们整合成一份可直接用于漫剧生产的「全剧设定集」。
【风格要求】{style}
【集数】{episodes} 集  【预计总镜头数】{target_shots}
【分段提炼结果】
{json.dumps(digest, ensure_ascii=False)}
{_ctx_asset_names_block(continuity_ctx)}
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字，结构如下：
{{
  "title": "剧名（4-12 字）",
  "theme": "一句话主题/卖点（30 字以内）",
  "style": "{style}",
  "characters": [{{"name": "姓名", "gender": "性别，只允许「男」或「女」两个值；必须按原文的人物称谓/代词/姓名线索推断后明确给出，禁止留空或写「未知」", "age": "年龄", "identity": "身份/阵营（15 字以内）", "appearance": "静态外貌定妆（含发色/瞳色/脸型/体格/标志特征等**不随剧情变化的特征**，**必须包含性别（如「女性」「男子」**，**不含会逐集变化的服饰/配饰——那些写进 outfit**），60 字以内；若上方设定库已锁定则该字段必须与锁定值逐字一致", "outfit": "本集服装状态（**动态特征：逐集可变的服饰/配饰，与静态 appearance 解耦**——appearance 是定妆照约束的静态外貌，outfit 是分镜画面约束的本集服装），20 字以内，与上集结尾一致；若本集确有换装必须体现原因", "personality": "性格（30 字以内）", "voice_style": "配音风格（15 字以内）", "reference_prompt_zh": "中文参考图提示词：角色三视图设定图，60 字以内，**必须写明角色性别（如开头写「女性角色，」「男性角色，」）**，只写画面可见的具体特征——发色发型、瞳色、脸型、服装款式与材质配色、标志配饰、三视图版式（**必须写明「正面、侧面、背面三张全身视图横排，从头到脚完整入画、同一角色身高比例一致」**，不要写成半身/胸像）；**严禁写任何风格词/画风词/质量词**（如「国漫」「3D渲染」「电影级」「高清」「精致」）", "reference_prompt_en": "English prompt for a character reference sheet with three full-body views (front, side, back laid out horizontally, head-to-toe, consistent body proportions), under 45 words, must explicitly state the character's gender (e.g. 'a woman,' / 'a man,'), comma-separated CONCRETE visual keywords (hair color and style, eye color, face shape, outfit material and colors, signature accessories, view layout). It MUST be an accurate translation of reference_prompt_zh. Never romanize Chinese concepts into invented pinyin (「国漫」 must become 'Chinese animated style', NOT 'xuanxuan'); never write style or quality words — the program appends them"}}],
  "items": [{{"name": "物品名", "category": "武器/法宝/道具/服饰", "appearance": "外观（50 字以内）", "owner": "持有人", "importance": "重要/临时。判定三问（任一答案为「是」即判临时）：①删掉它剧情还成立吗？②它只是随手用的日常物品吗？③它只是场景陈设吗？", "reference_prompt_zh": "中文参考图提示词，50 字以内，只写形制、材质、颜色、纹样与磨损状态；**严禁写风格词/画风词/质量词**", "reference_prompt_en": "English prompt for an item prop sheet, under 40 words, comma-separated concrete visual keywords (shape, material, color, pattern, wear). Accurate translation of reference_prompt_zh; no invented pinyin, no style or quality words"}}],
  "scenes": [{{"name": "场景名", "location": "地点类型", "appearance": "环境与氛围（60 字以内）", "scene_lighting": "该场景的**统一光影基调**（30 字以内）：整个场景所有镜头共享的主光源/时间/色温（如「黄昏暖调逆光」「冷蓝月光」「正午顶光」），用于消除同场景内逐镜光影漂移；无明确光源倾向时写「自然漫射光」。此字段只写光影，不写风格词/画风词/质量词，也不要出现人物", "reference_prompt_zh": "中文参考图提示词，50 字以内，只写空间结构、建筑形制、时间天气、光源方向与色调；**严禁写风格词/画风词/质量词**（且不要出现人物）", "reference_prompt_en": "English prompt for an environment concept art sheet, under 40 words, comma-separated concrete visual keywords (spatial layout, architecture, time of day and weather, light direction, color palette, no people). Accurate translation of reference_prompt_zh; no invented pinyin, no style or quality words"}}],
  "production_notes": {{"style_guide": "画面与叙事风格说明（60 字以内）"}}
}}
【硬性约束】characters 最多 6 个（只保留主要角色，按戏份排序）；items 最多 3 个（**宁缺勿滥**，本集 0-3 个都可：只有通过三问过滤的重要道具才保留——①删掉它剧情还成立吗？②只是随手用的日常物品吗？③只是场景陈设吗？任一答案为「是」即剔除；临时道具不得进 items）；scenes 最多 6 个；不要输出示例里的占位文字。若上方提供了「项目级设定库」，则已登记角色的 name / gender / appearance / personality 必须与该库完全一致（禁止改名、禁止改性别、禁止改外观），只允许更新 outfit（当前服装状态）。
【风格红线·重要变更】风格词由**程序在生成前统一追加**（幂等，不会重复），不再由你写。因此 characters / items / scenes 三个数组里每一条 reference_prompt_zh 与 reference_prompt_en **都不得自行写风格词、画风词或质量词**——自己写了会导致风格在提示词里出现两遍（实测就是「中国古风玄幻漫剧风格。风格：中国古风玄幻漫剧，画面精致…」这种重复），属于不合格输出。你只需专注描述画面里看得见的具体特征，把风格判断交给程序。
【格式红线】直接以 {{ 作为输出的第一个字符；严禁输出任何推理过程、思考草稿、英文说明、markdown 代码块标记或前后缀解释文字；整个 JSON 输出控制在 1200 字以内（字段描述能短则短）。"""
    # 断点缓存：命中则跳过模型汇总（未命中时行为与加缓存前完全一致）。
    # 只缓存**模型成功产出**的结果；下面的确定性兜底不缓存，好让下次仍有机会走模型。
    hit = _cache_get(cache_dir, "bible", prompt, events, "bible")
    if hit is not None and hit.get("characters"):
        return hit
    bible_retry_kw = {"max_attempts": 4, "token_ladder": (6000, 8192, 16384, 24576)}
    data = {}
    for tag, p in (("bible", prompt),
                   ("bible-repair", prompt + "\n\n【重要·格式修复】上一次调用未产出完整合规 JSON。"
                    "请重新输出**一个完整、紧凑的 JSON 对象**，必须同时包含 characters、items、scenes、"
                    "production_notes 四个键，不要只输出其中某个数组，不要输出任何解释文字。"),
                   ("bible-slim", prompt + "\n\n【重要·精简模式】模型输出连续被截断。请只保留最核心信息："
                    "characters 最多 4 个（name / gender / identity / appearance / outfit 五个字段，每个不超过 20 字），"
                    "items 最多 3 个（name / category / appearance），scenes 最多 3 个（name / appearance），"
                    "其余字段全部省略。直接输出 JSON，不要解释。")):
        try:
            raw = _robust_json(client, p, system=SYSTEM_BIBLE, temperature=0.3, max_tokens=6000,
                               events=events, label=tag, **bible_retry_kw)
        except LLMError as e:
            logger.warning(f"bible 阶段 {tag} 调用失败：{e}")
            continue
        data = _normalize_bible(raw)
        if data.get("characters"):
            _cache_put(cache_dir, "bible", prompt, data)
            return data
        logger.warning(f"bible {tag} 返回缺少 characters（原类型 {type(raw).__name__}）：{str(raw)[:200]}")

    # 兜底：模型连续失败时用分块提炼结果做确定性聚合，保证整集生成不中断
    logger.warning("bible 汇总失败，降级为分块设定确定性聚合（未使用模型汇总）")
    data = _fallback_bible(outlines, novel_title, style)
    if events is not None:
        events.append({"label": "bible-fallback", "attempt": 0, "max_tokens": 0,
                       "finish_reason": "n/a", "truncated": False,
                       "note": "模型汇总连续失败，已降级为分块设定聚合"})
    return data


def _fallback_bible(outlines: list, novel_title: str, style: str) -> dict:
    """bible 兜底：从各分块提炼结果确定性聚合角色 / 物品 / 场景（不调用模型）"""
    chars, items, scenes = {}, {}, {}

    def _pick(src: dict, key: str, fields: list) -> None:
        name = str(src.get("name") or "").strip()
        if not name or name in key:
            return
        key[name] = {"name": name, **{f: str(src.get(f) or "") for f in fields}}

    for o in outlines or []:
        if not isinstance(o, dict):
            continue
        for c in (o.get("characters") or [])[:6]:
            if isinstance(c, dict):
                _pick(c, chars, ["identity", "gender", "appearance", "outfit", "personality", "voice_style"])
        for i in (o.get("items") or [])[:3]:
            if isinstance(i, dict):
                _pick(i, items, ["category", "appearance", "owner"])
        for s in (o.get("scenes") or [])[:6]:
            if isinstance(s, dict):
                _pick(s, scenes, ["location", "appearance"])
    out_chars, out_items, out_scenes = (list(chars.values())[:6], list(items.values())[:3],
                                        list(scenes.values())[:6])
    # 兜底路径同样要带风格：否则一旦 bible 汇总失败，资产提示词又回到「零风格词」老样子
    eff = style_kit.normalize_style(style)
    if eff:
        for group in (out_chars, out_items, out_scenes):
            style_kit.apply_asset_style_all(group, eff)
    return {
        "title": (novel_title or "")[:20],
        "theme": "",
        "style": eff,
        "characters": out_chars,
        "items": out_items,
        "scenes": out_scenes,
        "production_notes": {"style_guide": eff},
        "_degraded": True,
    }


def build_shots_for_chunk(client, bible: dict, outline: dict, chunk: dict, shots_target: int,
                          events: list = None, depth: int = 0,
                          continuity_ctx: dict = None, cache_dir: str = "",
                          shots_hard_cap: int = 0, prev_tail: str = "") -> list:
    """③ 单块写分镜（截断时自动提高 max_tokens；仍截断则把该块再二分后合并）

    prev_tail（P1-2 接缝重叠）：上一块的「结尾上下文」预渲染文本（由调用方经
    :func:`_prev_tail_block` 构造），非空时注入 prompt 让本块开头**承接**上段而非
    凭空重启。默认 ""（首块 / 递归子块 / 旧调用方）→ prompt 不出现该段，零变化。

    continuity_ctx 非空时（跨集连贯性方案 A②③ / C⑦⑧）：注入上集摘要卡、衔接契约、
    项目级风格指南、人物口吻词典、金句保留清单与运镜术语表。
    cache_dir 非空时启用断点缓存：命中即跳过模型调用（见文件上方缓存说明）。
    shots_hard_cap > 0 时收紧本块镜头数上限（单集硬上限 MAX_SHOTS_PER_EPISODE 的分配额度）——
    只压上限、不动下限，避免与 SHOTS_PER_CHUNK_MIN 打架（下限由调用方按预算调低）。

    ⚠️ 单块镜数超过 MAX_SHOTS_PER_CHUNK 时**先二分再送模型**（见该常量注释）：
    一次性要几十镜会让响应体大到读超时（实测 49 镜 ReadTimeout 1200s）。
    这里必须在**调用前**劈半 —— 劈半原本只挂在 LLMTruncatedError 上，而超时抛的是
    LLMError/LLMGatewayUnavailable，**不会**触发劈半，直接整块失败。
    """
    # ---- 预劈半：单块镜数过大 → 先拆成子块，避免单次响应体过大导致读超时 ----
    if (MAX_SHOTS_PER_CHUNK and int(shots_target) > MAX_SHOTS_PER_CHUNK
            and depth < CHAPTER_SPLIT_MAX_DEPTH):
        _subs = _split_chunk_in_half(chunk)
        if _subs:
            logger.warning(
                "块 %s 目标 %d 镜超过单块上限 %d，预拆为 %d 个子块（原文不丢，改为多次调用）",
                chunk.get("title"), int(shots_target), MAX_SHOTS_PER_CHUNK, len(_subs))
            if events is not None:
                events.append({"label": f"shots#{chunk.get('index')}",
                               "event": "pre_split", "depth": depth,
                               "target": int(shots_target),
                               "parts": [len(s.get("text") or "") for s in _subs]})
            # 镜数按原文字数比例分配（不是均分）—— 短的那半不该拿同样的镜数
            _lens = [max(1, len(s.get("text") or "")) for s in _subs]
            _tot = float(sum(_lens))
            _out = []
            _beats = list(outline.get("key_beats") or [])
            for _i, _s in enumerate(_subs):
                _target_i = max(1, int(round(int(shots_target) * _lens[_i] / _tot)))
                _sub_outline = dict(outline)
                if _beats:
                    _n = len(_beats)
                    _a = _i * _n // len(_subs)
                    _b = max(_a + 1, (_i + 1) * _n // len(_subs))
                    _sub_outline["key_beats"] = _beats[_a:_b]
                _out.extend(build_shots_for_chunk(
                    client, bible, _sub_outline, _s, _target_i,
                    events=events, depth=depth + 1,
                    continuity_ctx=continuity_ctx, cache_dir=cache_dir,
                    shots_hard_cap=shots_hard_cap))
            return _out

    char_brief = [
        {"name": c.get("name"), "gender": c.get("gender") or "", "appearance": (c.get("appearance") or "")[:40]}
        for c in (bible.get("characters") or [])[:6] if isinstance(c, dict)
    ]
    item_brief = [{"name": i.get("name"), "appearance": (i.get("appearance") or "")[:30]}
                  for i in (bible.get("items") or [])[:3] if isinstance(i, dict)]
    scene_brief = [{"name": s.get("name"), "appearance": (s.get("appearance") or "")[:40]}
                   for s in (bible.get("scenes") or [])[:6] if isinstance(s, dict)]
    _hard = int(shots_hard_cap or 0)
    shots_target = int(shots_target)
    if _hard > 0:
        # 单集镜头数硬上限是**权威**：目标也不得超过它。否则 prompt 会自相矛盾
        #（「至少 25 个、上限 6 个」），模型只会照目标超额产出 → 硬上限形同虚设。
        shots_target = max(1, min(shots_target, _hard))
    shots_cap = max(shots_target, min(120, shots_target * 2 + 3))
    if _hard > 0:
        shots_cap = min(shots_cap, max(shots_target, _hard))
    speech_budget = SHOT_SPEECH_BUDGET_CHARS
    prompt = f"""【任务】为漫剧《{bible.get('title') or ''}》的「{chunk.get('title')}」（第 {chunk['index']}/{chunk['total']} 段）编写分镜：至少 {shots_target} 个、上限 {shots_cap} 个。把下方原文**压缩提炼**成可拍摄的镜头，只保留推动剧情的关键情节（冲突/转折/关键动作/金句），纯背景铺陈直接删去、勿逐句照搬。
{REWRITE_RULES.format(chars_per_shot=CHARS_PER_SHOT)}
【全剧风格】{bible.get('style') or ''}　【画面风格指南】{_ctx_block(continuity_ctx, 'style_guide_text') or (bible.get('production_notes') or {}).get('style_guide') or ''}
{_ctx_line(continuity_ctx, 'prev_block')}{_ctx_line(continuity_ctx, 'bible_block')}{_ctx_line(continuity_ctx, 'contract_block')}{_ctx_line(continuity_ctx, 'style_block')}{_ctx_line(continuity_ctx, 'camera_block')}{prev_tail}【可用角色】{json.dumps(char_brief, ensure_ascii=False)}
【可用物品】{json.dumps(item_brief, ensure_ascii=False)}
【可用场景】{json.dumps(scene_brief, ensure_ascii=False)}
【本段原文（先压缩提炼：只保留冲突/转折/关键动作/金句，纯背景铺陈直接删去，勿逐句照搬）】
{chunk.get('text') or ''}
【本段剧情摘要】{outline.get('summary', '')}
【本段情节要点】{json.dumps(outline.get('key_beats') or [], ensure_ascii=False)}
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字，结构如下：
{{"shots": [{{"camera": "景别+运镜（必须取自上方运镜术语表，如 中景跟拍/特写推入，10 字以内）", "location": "所属场景名（必须来自可用场景）", "description": "画面内容描述（80 字以内，只写人物动作过程与关键构图：谁做了什么、怎么做的、在画面什么位置；外貌衣着/环境光线只在推动剧情或首次出场时写，不逐句铺陈，禁止写背景陈述/世界观/来历评述）", "visual_detail": "画面补充细节（可选；当 description 之外还有更细的关键动作过程/环境细节时写在这里，80 字以内；没有多余细节时写空字符串）", "dialogue": [{{"speaker": "说话角色名（必须与可用角色完全一致）", "text": "该角色台词（≤30 字；原文对话尽量原样保留；角色的自语/心声写成该角色本人的台词）"}}], "emotion": "情绪（8 字以内）", "edit_reason": "剪辑动机（15字以内，为什么切到这一镜/承担什么叙事功能，如：用背影暂缓解释/情绪停在等待而非眼泪/道具回环推进信任弧线）", "beat": "叙事节拍（本镜所处节拍，只填「开场」「触发」「高潮」「收尾」四值之一；拿不准填「触发」）", "audio_cues": "音效/配乐提示（60 字以内，只写环境音/音效/配乐，不写人声）", "characters_in_shot": ["出场角色名"], "first_frame": "首帧画面（运动开始前那一刻的静态快照：画面主体与构图，40字以内；无明显运动变化写空字符串）", "last_frame": "末帧画面（运动结束后的终态，40字以内；与首帧相同或无运动时写空字符串）", "motion": "运动描述（严格区分【摄影机运动】推拉摇移跟升降 与【画面内运动】人物/物体自身动作；30字以内；静止镜头写空字符串）", "items_in_shot": ["出场物品名"]}}]}}
【禁止输出 prompt_h3 字段】视频提示词由程序在生成阶段按 H3 规范自动构建（它会结合当次实际传入的参考图，生成 subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music 六段）。你在剧本阶段并不知道最终配几张参考图，写出来的英文提示词缺少 <Picture N> 标签，反而会覆盖规范提示词导致出片偏离设定。因此**不要写 prompt_h3、不要写英文提示词**；把画面信息全部写进 description 即可。
【台词要求】dialogue 必须是数组，数组元素为 {{"speaker": 角色名, "text": 台词}}；speaker 必须精确等于「可用角色」中的名字，禁止写“旁白/众人”等未登记角色；无台词的镜头 dialogue 写 []（空数组），禁止写成字符串或 null。角色的心理活动改写成该角色**本人**的自语台词时，speaker 仍写角色名（不要写成「旁白」，本系统没有旁白角色）。dialogue **只承载**：原文对话、以及原文明确心理活动/独白改写的第一人称自语——第三人称叙述与背景补叙**禁止**写成任何角色开口的台词（改写规则 8）。
【台词预算（防成片截断）】单个镜头的 dialogue **合计不超过 {speech_budget} 字**（≈6.7 秒配音）。台词过多时**先精简冗余语气词与重复表述**，仍超预算才拆成相邻镜头——配音是按镜头时间轴铺的，单镜台词超出镜头时长会被成片尾部静默截掉。
【音轨说明（本系统不产出旁白）】成片没有画外音解说，配音链路**只读 dialogue**：audio_cues 里写「雨声」「风声」这类音效**不会产生人声**。因此：① 有对话或自语的镜头必须写 dialogue，禁止把台词塞进 description / visual_detail / audio_cues；② 纯画面/纯动作镜头允许没有台词（该镜成片留白，由音效与配乐铺底），但**必须**在 audio_cues 写明音效/配乐提示；③ **严禁**凭空编造原文里没有的台词来「凑人声」——宁可留白，也不要无中生有。
【硬性约束】shots 数组元素个数必须在 {shots_target} ~ {shots_cap} 之间：只把原文里**推动剧情的冲突/转折/关键动作/金句**落到镜头里，纯背景补叙、纯环境描写（不推进剧情）**直接删去、不单独成镜**；name 字段必须与上面「可用角色/物品/场景」中的名字完全一致，不要新造名字。若上方给出「本集必须出现的原文金句」，必须把每句**原样**写进对应角色的 dialogue.text（不得改写、不得拆分、不得省略）。上一集已发生的事件禁止在本集重演。
【关键情节自检】写完回看上方「剧情摘要/情节要点」，确认每个关键情节都有对应镜头；纯背景补叙、纯环境描写若未推进剧情应当已删去，**不要求逐句覆盖原文**。记住：本系统没有旁白，背景补叙与环境描写靠画面承载、绝不写成台词，心理活动靠神态动作或第一人称角色自语承载。"""
    label = f"shots#{chunk.get('index')}"
    hit = _cache_get(cache_dir, "shots", prompt, events, label)
    if hit is not None and isinstance(hit.get("shots"), list) and hit["shots"]:
        return [s for s in hit["shots"] if isinstance(s, dict)]
    try:
        # ⚠️ 起始额度必须已包含「思考水位」：agnes-3.0-flash 这类 always-on reasoning 模型
        # 在本任务的思考量实测 ≈16K token（见 llm_client.REASONING_ONLY_TOKEN_FLOOR 注释）。
        # 此前按 shots_target*300+1200 给（12 镜 → 4800），低于思考水位 → 正文恒为空，
        # 表现为「模型只吐思考内容」并把整集卡死。这里按「思考预留 + 每镜正文」给足。
        _budget = SHOTS_THINKING_RESERVE + int(shots_target) * SHOTS_TOKENS_PER_SHOT
        data = _robust_json(client, prompt, system=SYSTEM_BIBLE, temperature=0.6,
                            max_tokens=max(8192, min(24000, _budget)),
                            events=events, label=label,
                            max_attempts=4,
                            token_ladder=(16384, 24576, 32768))
    except (LLMTruncatedError, LLMError) as _e:
        # ⚠️ 不只截断要二分：**超时/网关故障也要**。
        # 实测坑：一次要 49 镜 → 网关 ReadTimeout 1200s，抛的是 LLMError（非截断）
        #  → 旧代码不二分 → 整块失败、整集中断。既然「镜数太多」正是病因，
        #  二分后每块镜数减半、响应体也减半，大概率能过。
        # 但 LLMGatewayUnavailable（上游没算力/熔断）要**原样上抛** ——
        # 那是网关整体挂了，二分多少次都没用，正确处置是立刻停下告诉用户。
        if isinstance(_e, LLMGatewayUnavailable):
            raise
        _tag = "输出被截断" if isinstance(_e, LLMTruncatedError) else "调用失败"
        subs = _split_chunk_in_half(chunk) if depth < CHAPTER_SPLIT_MAX_DEPTH else []
        if not subs or int(shots_target) <= 1:
            if isinstance(_e, LLMTruncatedError):
                raise LLMTruncatedError(
                    f"{label}（{chunk.get('title')}，{len(chunk.get('text') or '')} 字，目标 {shots_target} 镜）"
                    f"在自动提高 max_tokens 后仍被截断，且已无法继续二分（depth={depth}）") from None
            # 非截断且无法再二分 → 保留原异常类型与原错误信息（含网关诊断），别吞成 Unknown
            raise
        logger.warning(
            "%s 分镜%s，自动二分为 %d 个子块重试（depth=%d，原错误：%s）",
            label, _tag, len(subs), depth, str(_e)[:160])
        if events is not None:
            events.append({"label": label, "event": "sub_split", "depth": depth,
                           "reason": _tag,
                           "parts": [len(s.get("text") or "") for s in subs]})
        per = max(1, int(round(int(shots_target) / float(len(subs)))))
        beats = list(outline.get("key_beats") or [])
        merged = []
        for i, s in enumerate(subs):
            sub_outline = dict(outline)
            if beats:
                n = len(beats)
                a = i * n // len(subs)
                b = max(a + 1, (i + 1) * n // len(subs))
                sub_outline["key_beats"] = beats[a:b]
            merged.extend(build_shots_for_chunk(client, bible, sub_outline, s, per,
                                                events=events, depth=depth + 1,
                                                continuity_ctx=continuity_ctx,
                                                cache_dir=cache_dir))
        return merged
    if isinstance(data, dict):
        shots = data.get("shots")
    elif isinstance(data, list):
        # 兜底：模型直接把 shots 数组作为顶层返回
        shots = [x for x in data if isinstance(x, dict)
                 and any(k in x for k in ("description", "camera", "prompt_h3"))] or None
    else:
        shots = None
    if not isinstance(shots, list):
        return []
    out = [s for s in shots if isinstance(s, dict)]
    if out:
        _cache_put(cache_dir, "shots", prompt, {"shots": out})
    return out


def _fallback_shots_for_chunk(chunk: dict, shots_target: int = 1, bible: dict = None) -> list:
    """分镜阶段模型失败时的兜底：按原文逐句生成「原文承载镜头」，保证该段内容不丢。

    与覆盖率补生成同源（只增不删）：原文措辞写进 description（超长部分由 _norm_shots
    拆进 visual_detail，故正文不会因为 200 字截断而丢失），标记 fallback=True 供前端提示需人工润色。
    这类镜头没有台词（本系统不产出旁白），成片该段留白 —— dialogue_utils.audit_script 会显式告警。
    """
    text = str((chunk or {}).get("text") or "")
    if not text.strip():
        return []
    bible = bible or {}
    chars = [c.get("name") for c in (bible.get("characters") or [])
             if isinstance(c, dict) and c.get("name")]
    scenes = [s.get("name") for s in (bible.get("scenes") or [])
              if isinstance(s, dict) and s.get("name")]
    total = int(chunk.get("char_count") or len(text))
    per = max(CHARS_PER_SHOT, total // max(1, int(shots_target)))
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?…；;])\s*|\n+", text) if s.strip()]
    if not sentences:
        sentences = [text.strip()]
    groups, buf = [], ""
    for s in sentences:
        if buf and len(buf) + len(s) > per:
            groups.append(buf)
            buf = s
        else:
            buf += s
    if buf:
        groups.append(buf)
    out = []
    for g in groups:
        out.append({
            "camera": "中景", "location": (scenes[0] if scenes else ""),
            "description": g[:200], "dialogue": [], "emotion": "平静", "audio_cues": "",
            "characters_in_shot": chars[:1], "items_in_shot": [], "prompt_h3": "",
            "fallback": True,
            "fallback_reason": f"第 {chunk.get('index')} 段模型分镜失败，按原文逐句承载",
        })
    return out


# ===================== 组装 =====================

def _norm_list(items, limit, keys):
    out = []
    for it in (items or [])[:limit]:
        if not isinstance(it, dict):
            continue
        row = {}
        for k in keys:
            v = it.get(k)
            row[k] = "" if v is None else (v if isinstance(v, (int, float)) else str(v))
        if any(str(row.get(k, "")).strip() for k in keys):
            out.append(row)
    return out


# ===================== 剧本 Schema：镜头数 / 每集时长（自动判定） =====================
# AI 转剧本阶段自动判定并写入，后续分镜 / 视频 / 配音链路直接引用，无需人工配置。
SHOT_DURATION_MIN = 3.0          # 单镜头最短秒数
SHOT_DURATION_MAX = 12.0         # 单镜头最长秒数
SHOT_DURATION_SILENT = 3.0       # 无台词的纯画面镜头基准秒数
CHARS_PER_SECOND = 4.5           # 中文配音语速基准（字/秒），用于按台词长度推算镜头时长
#: 单镜台词合计字数建议上限（≈6.7 秒配音）。这是「剧本阶段」的软预算：写超了应当拆成更多镜头。
#: 「生成期」另有一道硬兜底 —— required_shot_duration() 超 SHOT_DURATION_MAX 的镜头会被 _norm_shots 自动拆镜，
#: 所以即使模型没遵守预算，也不会让配音溢出到下一镜（溢出的尾部会被成片 -shortest 静默截掉）。
SHOT_SPEECH_BUDGET_CHARS = 30

# 动作镜识别词：description 命中任意一个即给画面停留时间加成（动作戏观感不仓促）
_ACTION_MARKERS = (
    "冲", "扑", "挥", "劈", "斩", "刺", "砍", "击", "踢", "打", "斗", "抓", "抛", "掷",
    "跳", "跃", "翻", "滚", "追", "逃", "跑", "奔", "飞", "坠", "落", "闪", "避", "挡",
    "拔", "抽", "掷", "掐", "捏", "撕", "扯", "推", "撞", "擒", "锁", "绞", "轰", "炸",
    "施法", "结印", "御剑", "掐诀", "催动", "爆发", "猛冲", "疾驰",
)

# ---- 叙事节拍（改写规则 11，P1 节拍识别切段）----
#: 节拍四段白名单。模型输出越界/未识别值一律归一到「触发」（最通用的中性节拍）。
_BEAT_WHITELIST = ("开场", "触发", "高潮", "收尾")
_DEFAULT_BEAT = "触发"
#: 「高潮」节拍镜的画面停留加成（秒）：爆点镜需要停留让观众看清动作与反应，
#: 故在基准时长上额外加成、把时长向档位上限（SHOT_DURATION_MAX=12）顶住。
#: ⚠️ 不加到全局 SHOT_DURATION_MAX（QC 侧 SHOT_DURATION_MAX_OK 共用同口径）——
#: 加成只抬高 required/estimate，最终仍被 estimate_shot_duration 夹在 [MIN, MAX]，
#: 所以是「更常贴着 12s 上限」而非突破上限，不会引入 QC「时长过长」误报。
BEAT_CLIMAX_BONUS_SEC = 2.0


def _norm_beat(raw) -> str:
    """把模型给的节拍值归一到四值白名单；空/越界值取默认「触发」。"""
    v = str(raw or "").strip()
    return v if v in _BEAT_WHITELIST else _DEFAULT_BEAT


def required_shot_duration(shot: dict) -> float:
    """该镜头「装得下内容」所需的时长（秒），**不封顶**。

    与 estimate_shot_duration 同源，只是不做 [MIN, MAX] 夹取：返回值 > SHOT_DURATION_MAX
    就说明这个镜头的台词/画面塞不进一个镜头，配音铺到时间轴上会溢出到下一镜、尾部被
    成片 `-shortest` 静默截掉（历史缺陷：ep04 旁白 496 秒铺在 100 秒画面上）。

    生成期用它做对账：超限即拆镜（见 _norm_shots），而不是事后告警。
    """
    # 台词合计时长：_dlg_text 会把同镜多条台词拼起来，正是配音链路的实际喂入量。
    spoken = _dlg_text(shot.get("dialogue"))
    spoken = re.sub(r"^[^：:]{1,12}[：:]", "", spoken)               # 去掉“角色名：”前缀
    if not spoken:
        spoken = str(shot.get("dialogue_text") or "").strip()
    speak_sec = len(spoken) / CHARS_PER_SECOND if spoken else 0.0

    desc = " ".join(x for x in (
        str(shot.get("description") or ""),
        str(shot.get("visual_detail") or ""),
    ) if x).strip()
    desc_sec = min(2.0, len(desc) / 60.0)
    # 动作复杂度加成：description 里动作过程词/动词越多，画面越需要停留时间。
    # 历史缺陷：动作镜与静景镜一律 3 秒基准，动作戏（打斗/追逐/施法）观感仓促。
    action_sec = 0.0
    if desc:
        action_hits = sum(1 for kw in _ACTION_MARKERS if kw in desc)
        if action_hits:
            action_sec = min(1.5, 0.4 + action_hits * 0.15)
    # 节拍加成（P1）：高潮/爆点镜额外停留，让节奏分层落地；不影响过渡镜的快切。
    beat_bonus = BEAT_CLIMAX_BONUS_SEC if str(shot.get("beat") or "").strip() == "高潮" else 0.0
    return SHOT_DURATION_SILENT + speak_sec + desc_sec + action_sec + beat_bonus


def estimate_shot_duration(shot: dict) -> float:
    """按画面 + 台词长度自动推算单镜头时长（秒），保证同一剧本多次运行结果稳定。

    台词兼容两种写法：结构化 [{"speaker","text"}] / 旧字符串（含 "角色名：台词" 前缀）。

    2026-09-19 优化：纯动作/无台词镜头不再一律给 3 秒 ——
    - description 里动作要素越多（动词/动作过程词），画面停留越久（动作镜需要呈现完整过程）；
    - 动作描写长的镜头（描述超 60 字）给更高基准，避免「动作才做一半就切走」。

    ⚠️ 值被夹在 [SHOT_DURATION_MIN, SHOT_DURATION_MAX]：返回值**无法**表达「装不下」。
    需要判断是否溢出请用 required_shot_duration()。
    """
    return round(max(SHOT_DURATION_MIN,
                     min(SHOT_DURATION_MAX, required_shot_duration(shot))) * 2) / 2.0


def build_episode_stats(shots: list) -> dict:
    """由分镜列表生成「镜头数 / 每集时长（秒）」统计（含分集明细 episode_plan）。"""
    per_ep: dict = {}
    for sh in shots or []:
        if not isinstance(sh, dict):
            continue
        ep = int(sh.get("episode") or 1)
        per_ep.setdefault(ep, []).append(sh)

    plan = []
    for ep in sorted(per_ep):
        rows = per_ep[ep]
        total = round(sum(float(s.get("duration") or SHOT_DURATION_SILENT) for s in rows), 2)
        plan.append({
            "episode_no": ep,
            "shot_count": len(rows),
            "duration_sec": total,
            "duration_per_shot_sec": round(total / len(rows), 2) if rows else 0.0,
            "shot_ids": [s.get("shot_id") for s in rows],
        })

    total_shots = sum(r["shot_count"] for r in plan)
    total_sec = round(sum(r["duration_sec"] for r in plan), 2)
    primary = plan[0] if len(plan) == 1 else None
    return {
        "shot_count": primary["shot_count"] if primary else total_shots,
        "duration_sec": primary["duration_sec"] if primary else total_sec,
        "duration_per_shot_sec": (primary["duration_per_shot_sec"] if primary
                                  else (round(total_sec / total_shots, 2) if total_shots else 0.0)),
        "episode_count": len(plan),
        "total_shot_count": total_shots,
        "total_duration_sec": total_sec,
        "episode_plan": plan,
    }


def apply_episode_schema(script: dict, duration_per_shot: float = None) -> dict:
    """把自动判定的镜头数 / 每集时长写入剧本 Schema（顶层 + production_notes + metadata）。

    - 顶层：shot_count / episode_duration_sec / duration_per_shot_sec / episode_plan
    - production_notes：total_shots / estimated_duration（兼容旧字段）+ 新字段
    - metadata：episode_stats（供前端与下游链路直接读取）
    """
    shots = script.get("shots") or []
    if duration_per_shot:
        for sh in shots:
            if isinstance(sh, dict):
                sh["duration"] = round(float(duration_per_shot) * 2) / 2.0
    stats = build_episode_stats(shots)
    script["shot_count"] = stats["shot_count"]
    script["episode_duration_sec"] = stats["duration_sec"]
    script["duration_per_shot_sec"] = stats["duration_per_shot_sec"]
    script["episode_plan"] = stats["episode_plan"]

    notes = script.setdefault("production_notes", {})
    notes["shot_count"] = stats["shot_count"]
    notes["episode_duration_sec"] = stats["duration_sec"]
    notes["total_shots"] = stats["total_shot_count"]
    notes["estimated_duration"] = stats["total_duration_sec"]
    notes["episode_plan"] = stats["episode_plan"]

    meta = script.setdefault("metadata", {})
    meta["episode_stats"] = stats
    meta["shot_count"] = stats["shot_count"]
    meta["episode_duration_sec"] = stats["duration_sec"]
    return script


def build_chapter_coverage_meta(shots: list, chars_per_shot: int = CHARS_PER_SHOT) -> dict:
    """按「镜头数 × 每镜承载字数」估算内容承载量（覆盖率校验前的预估值）

    仅用于覆盖率校验未执行时给出「预计承载字数 / 是否可能遗漏」的提示；
    真实覆盖率以 coverage.py 的逐句校验结果为准。
    """
    n = len([s for s in (shots or []) if isinstance(s, dict)])
    capacity = n * int(chars_per_shot or CHARS_PER_SHOT)
    return {"shots": n, "chars_per_shot": int(chars_per_shot or CHARS_PER_SHOT),
            "capacity_chars": capacity, "verified": False}


def _keep_valid_h3(raw) -> str:
    """只保留结构合规的 H3 提示词，其余丢弃

    合规 = 六段式（Ref2VA）或三段式（base）齐全，见 :mod:`h3_prompt_kit`。
    剧本阶段模型写的裸英文描述必然不合规，会被丢弃；提示词分析器产出的
    规范文本会被保留，用户的「重新生成提示词」成果不会被下一次 script
    归一化抹掉。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return text if h3_prompt_kit.validate(text)["valid"] else ""
    except Exception:  # noqa: BLE001 —— 校验失败不应影响剧本生成主流程
        return ""


def _overflow_detail(raw, limit: int) -> str:
    """把超长描述的「超出部分」拆出来（不丢画面细节，供 visual_detail 使用）。

    - 空串 / 未超长 → 返回空串（visual_detail 不重复 description）；
    - 超长 → 从第 limit 个字符开始截取，去头尾空白，限 400 字。
    """
    s = str(raw or "").strip()
    if len(s) <= int(limit):
        return ""
    return s[int(limit):].strip()[:400]


def _match_known_names(raw_names, known: list, field: str) -> list:
    """把镜头声明的角色/物品名收敛到 bible 名单内（**禁止静默 take-first**）。

    P1-16 修复：旧实现用 `[...] if c in chars] or chars[:1]` —— 名字与 bible 对不上时
    静默填入首个角色（通常是主角），使整集以**错误角色**为外观锚点（日志/界面看不出来），
    并把下游 S6「禁止静默 take-first」的 `_no_reference` 分支彻底架空（列表恒非空）。
    现在只保留**精确命中 bible** 的名字（去重保序）；匹配不到即返回空列表，交由下游
    `_allocate_storyboard_refs` 的 no_reference 分支显式告警/跳过，并在「有输入但全未
    命中」时记 warning，保证排障可见。
    """
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    names = [n for n in (raw_names or []) if n]
    if not names:
        return []
    known_set = set(known or [])
    seen, out = set(), []
    for n in names:
        if n in known_set and n not in seen:
            seen.add(n)
            out.append(n)
    if not out:
        logger.warning(
            "镜头 %s 声明的名字 %s 均未命中 bible（可用：%s）→ 保留空列表，"
            "交由下游 no_reference 显式告警/跳过（禁止静默兜底取错角色）",
            field, "、".join(names)[:80],
            "、".join(x for x in (known or []) if x)[:120])
    return out


# 实质台词文本判据（P0-2 补修 / task#9）：
# 一条台词只有当它含「实质字符」（CJK / 字母 / 数字）才算"有台词内容"。
# 纯标点（"。！？"）、空 text（[{"text":""}]）、结构化空壳（[{"text":""}]）
# 一律视为**无实质台词**——与下游 prompt_qc「缺少画面描述」的硬不变量对齐。
_DLGM_SUBSTANTIVE_RE = re.compile(r"[\u4e00-\u9fff\w]")


def _has_meaningful_dlg(raw, chars: list) -> bool:
    """归一化后是否含**实质**台词文本（单一判据，供「丢弃闸门」与「画面补齐」同源使用）。

    task#9 修复的根因：旧实现里丢弃闸门用**原始值真值**（``bool([{"text":""}])`` /
    ``bool("。！？".strip())`` 都为 True），而画面补齐条件用**归一化后真值**（这两类
    归一化后 dialogue 为 []）→ 两处口径不一致 → 「结构化空壳 / 纯标点」镜头**既不被丢弃
    也不被补齐** → 空壳镜在分镜步永久卡死。本函数以「归一化后是否含实质字符」为唯一
    判据，让两处共用，消除分歧。
    """
    for line in _dlg_lines(raw, chars, chars):
        if _DLGM_SUBSTANTIVE_RE.search(str(line.get("text") or "")):
            return True
    return False


def _norm_shots(raw_shots: list, bible: dict, episodes: int, start_id: int = 1) -> list:
    scenes = [s.get("name") for s in (bible.get("scenes") or []) if isinstance(s, dict)]
    chars = [c.get("name") for c in (bible.get("characters") or []) if isinstance(c, dict)]
    items = [i.get("name") for i in (bible.get("items") or []) if isinstance(i, dict)]
    # P4-A 场景共享光影：把「场景名 → 该场景统一光影基调」建成映射，落到每个镜头上。
    # 同场景所有镜头共享同一 base，下游 comfyui_client 光影推断时优先用它，消除场景内逐镜漂移。
    # 旧剧本 / bible 无 scene_lighting 字段 → 映射值为空串，下游走原有逐镜+情绪兜底（零变化）。
    scene_light_map = {
        str(s.get("name") or "").strip(): str(s.get("scene_lighting") or "").strip()
        for s in (bible.get("scenes") or [])
        if isinstance(s, dict) and str(s.get("name") or "").strip()
    }
    # 风格：镜头级落一次 style，下游（分镜图 / 视频提示词）才有值可用。
    # 历史缺陷：这里不写 style，导致 comfyui_client 里 shot.get("style", "3D动漫渲染")
    # 永远回落硬编码默认值 —— 用户与总控敲定的风格一个镜头都传不到。
    shot_style = style_kit.normalize_style(bible.get("style"))
    shots = []
    sid = start_id
    dropped_empty = []
    dropped_deprecated = []   # 已废弃字段（DEPRECATED_SHOT_FIELDS）被模型越界输出的次数
    for s in raw_shots:
        if not isinstance(s, dict):
            continue
        # 已废弃字段可见化（见 DEPRECATED_SHOT_FIELDS）：模型若仍吐出 narration 等已废弃字段，
        # 这里显式记账并在本函数末尾打一条 warning —— 让「重新写旁白」被**可见地拒绝/告警**，
        # 而不是靠注释里的君子协定蒙混过关。字段本身仍照旧丢弃，不改变任何业务行为。
        for _dep_field in DEPRECATED_SHOT_FIELDS:
            if str(s.get(_dep_field) or "").strip():
                dropped_deprecated.append(_dep_field)
        # 空壳镜头（画面描述 / 补充细节 / 台词 三者皆空）**必须在这里丢掉**。
        # 历史缺陷：模型偶尔会吐出一条只有 camera/location/emotion 的幽灵镜头
        #（实测《蛊真人》ep02 shot_02：description/visual_detail/dialogue/audio_cues 全空），
        # 本函数原样收下 → 生成期 prompt_qc 判「镜头缺少画面描述」**致命缺陷且不可自愈**
        # → 该镜永远出不了图 → probe_storyboard 永远缺 1 镜 → 整集在分镜步永久卡死，
        # 且用户在界面上拿不到任何可操作的补救入口。
        # 这类镜头不承载任何原文内容（原文覆盖率校验不会因此丢句），丢掉是零损失；
        # 若确实有原文没被承载，后续 coverage 补生成会按原文补回一条**有内容**的镜头。
        # 注意：audio_cues 不参与判定 —— 只有音效没有画面的镜头同样出不了图。
        _has_vis = bool(str(s.get("description") or "").strip()
                        or str(s.get("visual_detail") or "").strip()
                        or str(s.get("storyboard_prompt_zh") or "").strip())
        # task#9 补修：台词判据与下方「画面补齐」同源（归一化后是否含实质文本），
        # 不再用原始值真值——否则 `[{"text":""}]` / `"。！？"` 既骗过闸门又不被补齐。
        _has_dlg = _has_meaningful_dlg(s.get("dialogue"), chars)
        if not (_has_vis or _has_dlg):
            dropped_empty.append(s.get("camera") or s.get("location") or "?")
            continue
        loc = str(s.get("location") or "").strip()
        if scenes and loc and loc not in scenes:
            # P1-16 修复（场景侧）：匹配不到时**保留原 loc** 并记 warning，绝不静默回落
            # `scenes[0]`。旧行为会把「破败的大殿」这类不在 bible 里的场景名换成「后山」
            # 这类首个场景 —— 环境锚点整集级错位，且日志/界面看不出来。
            _hit = next((n for n in scenes if n and n in loc), None)
            if _hit:
                loc = _hit
            else:
                logger.warning(
                    "镜头场景名未命中 scenery bible：%r（可用：%s）→ 保留原文，"
                    "不静默回落首个场景", loc, "、".join(x for x in scenes if x)[:120])
        if not loc and scenes:
            loc = scenes[0]
        row = {
            "shot_id": sid,
            "duration": 5,
            "camera": str(s.get("camera") or "中景").strip()[:20] or "中景",
            "location": loc,
            # P4-A 场景共享光影：按解析后的 loc 取该场景统一光影基调（同场景镜头同源，
            # 消除逐镜漂移）。旧剧本 loc 未命中或场景无该字段 → 空串，下游走逐镜/情绪兜底。
            "scene_lighting": scene_light_map.get(loc, ""),
            # description：限长 200 字（前端展示与 prompt 体量控制用）。
            # 但画面细节不丢：原始描述若超长，把超出的部分拆进 visual_detail（分镜图/视频
            # 提示词会把它并回画面主体）。历史缺陷：description 截断 200 字后剩余细节
            # 直接丢失，导致「动作完整、光影明确」的要求只能靠模型猜。
            "description": str(s.get("description") or "").strip()[:200],
            # visual_detail：优先用模型直接输出的字段（分镜 schema 已要求模型把超出
            # description 的更细画面细节写这里）；模型没给时用 _overflow_detail 兜底
            #（description 截断 200 字后的剩余部分）。供 build_storyboard_prompt /
            # h3_prompt_kit 等下游取用。
            "visual_detail": (str(s.get("visual_detail") or "").strip()[:400]
                              or _overflow_detail(s.get("description"), 200)),
            # narration：**已废弃字段（登记于 DEPRECATED_SHOT_FIELDS），此处显式丢弃**；
            # 模型若越界输出会在本函数末尾打一条 warning（可见地拒绝，不是注释君子协定）。
            # 历史缺陷：narration 被当成「心理活动 + 背景补叙 + 环境描写」的公共出口，
            # 再叠加当时的「每镜必须有人声」约束，导致原著所有叙述性文字都变成画外音解说
            #（实测 ep04 旁白 2231 字 ≈ 496 秒，铺在 100 秒画面上 → 4.93x 溢出，尾部被
            # 成片 -shortest 静默截断）。现在剧本阶段不再产出旁白，这里也不再透传模型的
            # 越界输出，保证「成片无旁白」是硬不变量而不是提示词君子协定。
            # 旧剧本文件里残留的 narration 由读取侧（coverage / h3_prompt_kit / tts_client）
            # 按需兼容，这是 DEPRECATED_SHOT_FIELDS 里 narration 唯一的合法消费方。
            # 台词：结构化 [{"speaker","text"}]（分镜阶段直接写明说话人，配音链路直接读取）
            "dialogue": _dlg_lines(s.get("dialogue"), chars, chars),
            "emotion": str(s.get("emotion") or "平静").strip()[:20],
            # edit_reason：剪辑动机（「为什么切到这一镜/承担什么叙事功能」，改写规则 9 新增字段）。
            # 它不解释给观众，是给构图与取舍的依据。限长 50 字（防模型越界输出塞一长段）。
            "edit_reason": str(s.get("edit_reason") or "").strip()[:50],
            # beat：叙事节拍（开场/触发/高潮/收尾），改写规则 11 新增。
            # 只保留四值白名单，越界/未识别值归一到「触发」；供下游节奏分层与时长加成取用。
            "beat": _norm_beat(s.get("beat")),
            "audio_cues": str(s.get("audio_cues") or "").strip()[:60],
            # 视频提示词：**剧本阶段不再信任模型自写的文本**。
            # 历史缺陷：这里原样保留模型写的「英文画面描述（60 词以内）」，一句无
            # <Picture N> 标签的裸英文，会在生成期把结构化 H3 构建器整个顶掉
            #（app.py 原写法 `shot.get('prompt_h3') or _build_h3_prompt(...)`），
            # 实测全项目 200+ 镜头的结构化提示词数量为 0。
            # 现在只保留「本身已合规」的提示词（例如提示词分析器产出的六段式），
            # 其余一律丢弃，交由生成期 h3_prompt_kit 按当次参考图规范重建。
            "prompt_h3": _keep_valid_h3(s.get("prompt_h3")),
            "style": shot_style,          # ← 风格注入：分镜图/视频提示词的风格来源
            # P1-16 修复（角色侧）：**去掉 `or chars[:1]` 兜底**。旧行为在「镜头角色名与
            # bible 对不上」时静默填入首个角色（通常是主角），使整集以错误角色为外观锚点，
            # 且日志/界面看不出来 —— 同时把下游 S6「禁止静默 take-first」的 `_no_reference`
            # 分支彻底架空（列表恒非空）。现在只保留**精确命中 bible** 的角色，匹配不到即留空，
            # 由下游 `_allocate_storyboard_refs` 的 no_reference 分支显式告警/跳过。
            "characters_in_shot": _match_known_names(
                s.get("characters_in_shot"), chars, "characters_in_shot"),
            "items_in_shot": [i for i in (s.get("items_in_shot") or []) if i in items],
            # P0-1：首帧/末帧/运动三段（借鉴 ViMax）。限长避免模型越界输出塞一长段；
            # 旧剧本/模型未输出 → 空串，下游 build_storyboard_prompt 回落单段 description，零变化。
            "first_frame": str(s.get("first_frame") or "").strip()[:40],
            "last_frame": str(s.get("last_frame") or "").strip()[:40],
            "motion": str(s.get("motion") or "").strip()[:30],
        }
        # 覆盖率补生成镜头：保留其承载的原文单元编号，便于覆盖率校验与前端回溯
        src_ids = s.get("source_unit_ids")
        if isinstance(src_ids, (list, tuple)) and src_ids:
            row["source_unit_ids"] = [int(x) for x in src_ids
                                      if isinstance(x, (int, float))][:80]
        if s.get("supplement"):
            row["supplement"] = True
        # 模型失败后的原文兜底镜头：保留标记，前端可提示需人工润色
        if s.get("fallback"):
            row["fallback"] = True
            row["fallback_reason"] = str(s.get("fallback_reason") or "")[:120]
        # 兼容展示字段：台词纯文本（有说话人时 "角色：台词"），前端/提示词按需读取
        row["dialogue_text"] = " ".join(
            (f"{d['speaker']}：{d['text']}" if d.get("speaker") else d.get("text") or "")
            for d in row["dialogue"]
        ).strip()
        # P0-2 修复（上游补齐）：只有台词、没有画面描述的镜头，在生成期提示词预检里会命中
        #   「镜头缺少画面描述（description / visual_detail / storyboard_prompt_zh 均为空）」
        # 这条**致命且不可自愈**的缺陷（prompt_qc._check_storyboard → fatal）→ 该镜永远
        # 出不了图 → probe_storyboard 永远缺 1 镜 → **整集在分镜步永久卡死**。
        # 这里在上游用**台词上下文**补齐一条画面描述，使该镜带「画面内容」进入生成。
        # ⚠️ 刻意**不写入台词原文**：台词进画面提示词会被模型渲染成字幕（prompt_qc 的硬原则），
        #    且会被 description 复用方（H3/尾帧/图片质检）当成「镜头内容」——那是以台词冒充
        #    画面，属于新缺陷。故只描述「说话人物的表演」，不含任何台词文本。
        # 真·四字段全空（含 task#9 的「结构化空壳 / 纯标点」形状）的幽灵镜头已在上方丢弃。
        # 补齐判据与上方丢弃闸门**同源**（都走 _has_meaningful_dlg），避免两处口径再次漂移：
        # 只有「归一化后确有实质台词文本」才补画面描述；纯标点/空壳既已被丢弃，此处恒假。
        if not (row["description"] or row["visual_detail"]) \
                and _has_meaningful_dlg(s.get("dialogue"), chars):
            _spk = []
            for _d in row["dialogue"]:
                _nm = (_d.get("speaker") or "").strip() if isinstance(_d, dict) else ""
                if _nm and _nm not in _spk:
                    _spk.append(_nm)
            _who = "、".join(_spk) or "人物"
            row["description"] = (
                f"{_who}开口说话（本镜以人物台词表演为主，画面聚焦说话人物的口型与神情）"
            )[:200]
        # 单镜头时长：取「模型给的时长」与「内容实际需要的时长」的**较大值**。
        # 历史缺陷：原实现只要模型给了合法值就直接采用（4~5 秒），完全不看这镜有多少台词
        #   → 长台词硬贴在短画面上，配音沿时间轴溢出到后面几镜，成片尾部被 `-shortest` 静默截掉。
        #   （实测《蛊真人》ep04：21/21 镜都直接采用模型值，与 estimate_shot_duration 的返回值全部不一致）
        auto_dur = estimate_shot_duration(row)
        model_dur = None
        try:
            model_dur = float(s.get("duration"))
        except (TypeError, ValueError):
            model_dur = None
        if model_dur and SHOT_DURATION_MIN <= model_dur <= SHOT_DURATION_MAX:
            row["duration"] = round(max(model_dur, auto_dur) * 2) / 2.0
        else:
            row["duration"] = auto_dur
        # 生成期对账：内容确实塞不进单镜上限时**显式记账**，不静默吞咽。
        # 不在这里私自抬高 SHOT_DURATION_MAX —— QC 侧的 SHOT_DURATION_MAX_OK 是同一个口径，
        # 单方面拉高会让成片被剧本质检判「时长过长」。溢出部分由 dub_mix 的 max_line_sec 变速兜底，
        # 该字段供 dialogue_utils.audit_script 提示用户「这一镜台词写多了，建议拆镜」。
        need = required_shot_duration(row)
        if need > SHOT_DURATION_MAX:
            row["duration_overflow_sec"] = round(need - SHOT_DURATION_MAX, 2)
        shots.append(row)
        sid += 1
    if dropped_empty:
        logger.warning("已丢弃 %d 条空壳镜头（无画面描述/细节/台词，出不了图且会卡死整集）：%s",
                       len(dropped_empty), dropped_empty[:12])
    if dropped_deprecated:
        logger.warning(
            "分镜标准化丢弃了模型越界输出的已废弃字段 %s（共 %d 处，见 DEPRECATED_SHOT_FIELDS）："
            "旁白通道已关闭，剧本阶段不再产出 narration；如有叙述性内容，请改写为角色自语台词"
            "（dialogue）或画面描述（description）。",
            "、".join(sorted(set(dropped_deprecated))), len(dropped_deprecated))
    # 分配集数
    n = len(shots)
    if n:
        per = max(1, math.ceil(n / max(1, episodes)))
        for i, sh in enumerate(shots):
            sh["episode"] = min(episodes, i // per + 1)
    return shots


def _run_full_coverage_check(client, novel_text: str, script: dict, reports=None,
                             warnings: list = None, continuity_dir: str = None,
                             project_key: str = None, max_rounds: int = COVERAGE_MAX_ROUNDS,
                             total_steps: int = 0) -> dict:
    """整本剧本的原文覆盖率校验：逐句核对原文是否被镜头承载 → 遗漏补生成 → 复检。

    只增不删：补生成只向 shots 追加镜头，不改写、不替换既有镜头。
    校验失败时降级（记 warning 并沿用原覆盖率摘要），不阻断剧本落盘。
    """
    warnings = warnings if warnings is not None else []
    try:
        import coverage as coverage_mod
        if reports:
            reports("coverage", total_steps, total_steps,
                    f"原文覆盖率校验（逐句核对 {len(novel_text or '')} 字是否被镜头承载）…", 98)
        rep = coverage_mod.run_coverage_check(
            client, novel_text, script, episode_no=1, threshold=None,
            max_rounds=max_rounds, events=None,
            continuity_dir=continuity_dir, project_key=project_key, save=True)
        if rep.get("supplement_shots"):
            apply_episode_schema(script)   # 补生成镜头后刷新镜头数 / 时长
        if reports:
            reports("coverage", total_steps, total_steps,
                    f"原文覆盖率：情节级 {rep.get('plot_coverage_percent')}%、"
                    f"细节级 {rep.get('detail_coverage_percent')}%，遗漏 "
                    f"{rep.get('missing_count')} 条、补生成 {rep.get('supplement_shots')} 镜", 99)
        # ---- P0-3 剧本↔原著一致性：整本单集路径（1 集 = 整本小说）无单章比较基准，
        #      锚定与要素覆盖显式跳过，只做元信息泄漏扫描 + 命中镜头定向重写。
        try:
            import script_consistency as sc_mod
            sc_rep = sc_mod.run_script_consistency_check(
                client, script, novel_meta=None, chapter_text="", chapter=None,
                episode_no=1, auto_fix=True, events=None,
                continuity_dir=continuity_dir, project_key=project_key, save=True,
                skip_anchor=True, skip_elements=True)
            if reports:
                reports("consistency", total_steps, total_steps,
                        f"剧本一致性：元信息泄漏 "
                        f"{(sc_rep.get('leak') or {}).get('hit_count')} 处，"
                        f"定向修复 {sc_rep.get('fix_rounds')} 轮"
                        f"（{'已修复' if sc_rep.get('fixed') else '留告警'}）", 99.5)
        except Exception as e:  # noqa: BLE001
            note = (f"剧本一致性校验失败（已跳过，剧本仍按原结果落盘）："
                    f"{type(e).__name__}: {str(e)[:200]}")
            warnings.append(note)
            logger.warning(note)
        return coverage_mod.summary_for_meta(rep, rep.get("report_path")) \
            or (script.get("metadata") or {}).get("coverage") or {}
    except Exception as e:  # noqa: BLE001
        note = (f"原文覆盖率校验失败（已跳过，剧本仍按全量分块生成）："
                f"{type(e).__name__}: {str(e)[:200]}")
        warnings.append(note)
        logger.warning(note)
        return (script.get("metadata") or {}).get("coverage") or {}


def convert_novel_to_script(client, novel_meta: dict, novel_text: str, style: str = "3D动漫渲染",
                            episodes: int = 1, target_shots: int = 12, progress_cb=None,
                            check_coverage: bool = True, continuity_dir: str = None,
                            project_key: str = None, cache_dir: str = "",
                            coverage_max_rounds: int = COVERAGE_MAX_ROUNDS) -> dict:
    """完整转换流程，返回 A 版剧本 dict（含 metadata）

    check_coverage=True（默认）时，整本路径同样执行「原文覆盖率校验 → 遗漏自动补生成 →
    复检 → 报告落盘」，保证长篇小说既不删减原文，也不因分块而丢内容。

    ⚠️ cache_dir（2026-09-25 补）：整本路径此前**完全不落缓存** ——
    提炼 42 章要跑 40 次调用、约 40 分钟，只要有一次网关抖动，
    pipeline 的 script 步骤整体重试就要**从头再烧一遍**。
    这正是 `shots_cache_dir` 注释里写的那个坑，但当时只接到「按章分集」路径
    （convert_chapter_to_script），整本路径漏了。现补上：口径与分集路径一致
    （内容寻址 sha1(prompt)，命中即跳过模型调用）。
    """
    def report(phase, current, total, message, percent):
        if progress_cb:
            try:
                progress_cb(phase, current, total, message, percent)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"进度回调异常：{e}")

    t0 = time.time()
    cache_events = []          # 缓存命中轨迹（写入 metadata，便于排查「为什么这么快」）
    novel_title = novel_meta.get("title") or novel_meta.get("name") or "未命名小说"
    chapters = novel_meta.get("chapters") or []
    chunks = build_chunks(novel_text, chapters)
    if not chunks:
        raise LLMError("小说正文为空，无法转换")
    # 全量覆盖：不再抽样，所有块都送模型（分块仅用于控制单次 prompt 体量）
    sampled = list(chunks)
    sampled_idx = [c["index"] for c in chunks]

    total_steps = len(sampled) + 2 + len(sampled)
    report("prepare", 0, total_steps,
           f"全文 {novel_meta.get('char_count', 0)} 字，切分为 {len(chunks)} 块，"
           f"全量覆盖送模型 {len(sampled)} 块（不抽样、不丢内容）",
           3)

    # ① 逐块提炼
    outlines, warnings = [None] * len(sampled), []
    for i, chunk in enumerate(sampled):
        report("outline", i + 1, total_steps, f"提炼第 {chunk['index']} 块（{chunk.get('title')}）…",
               int(5 + (i + 1) / total_steps * 55))
        try:
            outlines[i] = extract_chunk_outline(client, chunk, novel_title,
                                                events=cache_events, cache_dir=cache_dir)
        except LLMError as e:
            # 大纲仅用于辅助提示，失败不丢原文（分镜阶段仍全量送该块正文）
            warnings.append(f"第 {chunk['index']} 块大纲提炼失败（改用空大纲，原文仍全量送模型）：{e}")
            logger.warning(f"第 {chunk['index']} 块提炼失败：{e}")
    if not any(outlines):
        raise LLMError("所有分块提炼均失败：" + ("；".join(warnings) or "未知错误"))

    # ② 汇总设定
    report("bible", len(sampled) + 1, total_steps, "汇总全剧人物 / 物品 / 场景设定…", 65)
    bible = _as_dict(build_bible(client, outlines, novel_title, style, episodes, target_shots,
                                 events=cache_events, cache_dir=cache_dir))

    characters = _norm_list(bible.get("characters"), 8,
                            ["name", "age", "gender", "appearance", "personality", "voice_style",
                             "reference_prompt_zh", "reference_prompt_en"])
    items = _norm_list(bible.get("items"), 3,
                       ["name", "category", "appearance", "owner", "importance",
                        "reference_prompt_zh", "reference_prompt_en"])
    scenes = _norm_list(bible.get("scenes"), 8,
                        ["name", "location", "appearance", "scene_lighting", "reference_prompt_zh", "reference_prompt_en"])
    if not characters:
        raise LLMError("模型未返回有效角色设定，转换中止")
    # 风格：以调用方传入的 style 为准 + 确定性补写（模型不得自写风格，统一由此收尾）
    eff_style = style_kit.normalize_style(style) or style_kit.normalize_style(bible.get("style"))
    style_filled = style_kit.apply_asset_style_all(characters, eff_style) \
        + style_kit.apply_asset_style_all(items, eff_style) \
        + style_kit.apply_asset_style_all(scenes, eff_style)
    if eff_style and style_filled:
        logger.info("全剧设定：已为 %d 条资产参考提示词补写风格「%s」", style_filled, eff_style)
    bible = {"title": str(bible.get("title") or novel_title)[:40],
             "theme": str(bible.get("theme") or "")[:200],
             "style": eff_style[:60],
             "characters": characters, "items": items, "scenes": scenes,
             "production_notes": bible.get("production_notes") or {}}

    # ③ 逐块写分镜（镜头数按每块原文体量自动扩展，不再按 target_shots 摊薄砍内容）

    # ③ 逐块写分镜（镜头数按每块原文体量自动扩展，不再按 target_shots 摊薄砍内容）
    all_shots = []
    for i, chunk in enumerate(sampled):
        per_chunk = estimate_shots_for_chars(chunk.get("char_count") or len(chunk.get("text") or ""))
        # P1-2 接缝重叠（借 ViMax overlap 思路、按本架构落地）：i>0 时把「上一块结尾」
        # （已 LLM 提炼的 outline，零新增调用）注入本块，让开头承接上段而非凭空重启；
        # i=0（首块）→ prev_chunk/prev_outline 均 None → prev_tail="" → prompt 不出现该段（零变化）。
        _prev_chunk = sampled[i - 1] if i > 0 else None
        _prev_outline = (outlines[i - 1] or {}) if i > 0 else None
        _prev_tail = _prev_tail_block(_prev_chunk, _prev_outline)
        report("shots", len(sampled) + 2 + i, total_steps,
               f"编写第 {chunk['index']} 块分镜（{chunk.get('title')}，{per_chunk} 镜）…",
               int(70 + (i + 1) / total_steps * 25))
        try:
            all_shots.extend(build_shots_for_chunk(client, bible, outlines[i] or {"summary": "", "key_beats": []},
                                                   chunk, per_chunk,
                                                   events=cache_events, cache_dir=cache_dir,
                                                   prev_tail=_prev_tail))
        except LLMGatewayUnavailable as e:
            # 网关问题不是「这一块运气不好」，重试无用 —— 见 _gateway_down_fallback 说明
            fb = _gateway_down_fallback(e, chunk, per_chunk, bible, all_shots)
            all_shots.extend(fb)
            warnings.append(_gateway_down_warning(chunk, len(fb)))
            logger.error(f"第 {chunk['index']} 块因网关不可用降级兜底 {len(fb)} 镜：{e}")
        except LLMError as e:
            # 兜底：按原文逐句生成承载镜头，绝不静默丢弃该段原文
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 块分镜失败，已按原文兜底生成 {len(fb)} 镜"
                            f"（内容未丢，建议人工润色）：{e}")
            logger.warning(f"第 {chunk['index']} 块分镜失败，已兜底 {len(fb)} 镜：{e}")

    shots = _norm_shots(all_shots, bible, episodes)
    if not shots:
        raise LLMError("模型未返回有效分镜：" + ("；".join(warnings) or "未知错误"))

    # 外形一致性收敛（2026-09-24）：资产**出图依据**是 reference_prompt_zh，而分镜
    # 提示词 / 质检口径取 appearance —— 两者漂移会让「文字说黑寸头、参考图却是黑发高冠」
    # 两条互斥指令同时生效（分镜是 cfg=1.0 的参考图编辑型 → 参考图压过文字），模型每次
    # 随机倒向一边、质检必然抓到另一边 → 无限重试、整集跑不过。落盘前按 appearance 收敛。
    asset_prompt_kit.reconcile_script({"characters": characters, "shots": shots})

    script = {
        "title": bible["title"],
        "theme": bible["theme"],
        "style": bible["style"],
        "characters": characters,
        "items": items,
        "scenes": scenes,
        "shots": shots,
        "production_notes": {
            "total_shots": len(shots),
            "estimated_duration": len(shots) * 5,
            "style_guide": str((bible.get("production_notes") or {}).get("style_guide") or "")[:300],
        },
        "metadata": {
            "source": "novel_to_script",
            "source_novel": {
                "novel_id": novel_meta.get("novel_id"),
                "name": novel_meta.get("name"),
                "char_count": novel_meta.get("char_count"),
                "chapter_count": novel_meta.get("chapter_count"),
            },
            "chunks_total": len(chunks),
            "chunks_used": len(sampled),
            # 全量覆盖：sampled_chunks 语义 = 全部块下标（保留旧字段名兼容前端）
            "sampled_chunks": sampled_idx,
            "coverage_mode": "full",
            "chars_per_shot": CHARS_PER_SHOT,
            "estimated_shots": sum(estimate_shots_for_chars(c.get("char_count") or 0) for c in chunks),
            "shots_per_chunk": [estimate_shots_for_chars(c.get("char_count") or 0) for c in chunks],
            "chunk_chars": CHUNK_CHARS,
            "style": style,
            "episodes": episodes,
            "target_shots": target_shots,
            "model": client.model,
            "base_url": client.base_url,
            "elapsed_sec": round(time.time() - t0, 1),
            "warnings": warnings,
            # 断点缓存命中轨迹：重跑时据此判断「这次为什么这么快 / 哪几步还在烧模型」
            "cache_enabled": _shots_cache_enabled(),
            "cache_hits": sum(1 for e in cache_events if e.get("event") == "cache_hit"),
            "cache_events": cache_events[:200],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    }
    # ⑤ AI 转剧本阶段自动判定「每集镜头数 / 每集时长（秒）」并写入 Schema（下游链路直接引用）
    apply_episode_schema(script)
    script["metadata"]["coverage"] = build_chapter_coverage_meta(shots)
    # ⑥ 原文覆盖率校验：逐句核对原文是否被镜头承载，有遗漏即补生成镜头并复检（只增不删）
    if check_coverage:
        script["metadata"]["coverage"] = _run_full_coverage_check(
            client, novel_text, script, reports=report, warnings=warnings,
            continuity_dir=continuity_dir,
            project_key=project_key or (novel_meta or {}).get("name"),
            max_rounds=coverage_max_rounds, total_steps=total_steps)
    report("done", total_steps, total_steps,
           f"转换完成：{len(characters)} 角色 / {len(items)} 物品 / {len(scenes)} 场景 / "
           f"{len(script.get('shots') or shots)} 镜头 / 本集约 {script['episode_duration_sec']}s", 100)
    return script


# ===================== 按章节分集生成（每章一集） =====================

CHAPTER_MIN_CHARS = 300          # 过短章节阈值（低于此值仅提示，不做合并）
# ⚠️ 2026-09-25 实测修正（与上方 CHUNK_CHARS 同因，两条路径必须同口径）：
#    3000 字 / CHARS_PER_SHOT(120) ≈ 25 镜/块 > MAX_SHOTS_PER_CHUNK(24) 的单次调用
#    响应体红线 → 预劈半变成常态。改为 2400 字 ≈ 20 镜/块，首次调用即合规。
CHAPTER_CHUNK_CHARS = 2400       # 单章二次分块粒度（字符）
CHAPTER_MAX_SUBCHUNKS = 12       # [兼容保留] 旧「单章最多子块数」上限；全量覆盖后不再抽样，仅前端旧字段展示
CHAPTER_MIN_SUBCHUNK_CHARS = 200 # 章内子块最小字数（过短的尾块并入前一块，保证不丢正文）


class EpisodeNotFoundError(Exception):
    """指定剧集尚未生成"""


def _split_oversized(unit: str, limit: int) -> list:
    """把超长子单元按句末标点/换行再切，仍超限则按固定长度硬切（保证不丢字）"""
    parts, buf = [], ""
    for piece in re.split(r"(?<=[。！？；!?;])|\n", unit or ""):
        if not piece:
            continue
        if len(buf) + len(piece) <= limit:
            buf += piece
            continue
        if buf:
            parts.append(buf)
            buf = ""
        while len(piece) > limit:
            parts.append(piece[:limit])
            piece = piece[limit:]
        buf = piece
    if buf:
        parts.append(buf)
    return [p for p in parts if p.strip()] or [unit]


def build_chapter_chunks(chapter_text: str, chunk_chars: int = CHAPTER_CHUNK_CHARS,
                         max_subchunks: int = CHAPTER_MAX_SUBCHUNKS):
    """单章过长时做二次分块（按段落聚合 → 超长段落按句硬切 → 尾部并块 → 全量覆盖）。

    与整本分块（build_chunks）不同：本函数不做 MIN_CHUNK_CHARS 过滤，
    保证章节正文首尾不被丢弃。

    **全量覆盖**：分块只用于控制单次 prompt 体量，块与块连续无缝，
    所有子块都会送模型并合并，不做首尾保留式抽样（max_subchunks 仅作兼容保留）。

    返回 (全部子块, 送模型的子块, 送模型子块下标 1-based)：后两者在语义上等于全部子块。
    """
    text = (chapter_text or "").strip()
    if not text:
        return [], [], []

    units = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        units.extend([para] if len(para) <= chunk_chars else _split_oversized(para, chunk_chars))
    if not units:
        units = _split_oversized(text, chunk_chars)

    chunks, buf = [], ""
    for u in units:
        if buf and len(buf) + len(u) + 1 > chunk_chars:
            chunks.append(buf)
            buf = u
        else:
            buf = f"{buf}\n{u}" if buf else u
    if buf:
        chunks.append(buf)
    # 尾部过短则并入前一块：避免产生无意义碎片，同时不丢正文
    if len(chunks) >= 2 and len(chunks[-1]) < max(CHAPTER_MIN_SUBCHUNK_CHARS, chunk_chars // 3):
        chunks[-2] = chunks[-2] + "\n" + chunks[-1]
        chunks.pop()

    out = [{"text": c.strip(), "char_count": len(c.strip()),
            "from_chapter": None, "to_chapter": None} for c in chunks if c.strip()]
    for i, c in enumerate(out):
        c["index"] = i + 1
        c["total"] = len(out)
        c["title"] = f"第{i + 1}子块"
    # 全量覆盖：所有子块连续无缝地送模型并合并，不做任何抽样（旧 sample_chunks 调用已移除）
    all_idx = [c["index"] for c in out]
    return out, list(out), all_idx


def estimate_subchunks(char_count, chunk_chars: int = CHAPTER_CHUNK_CHARS,
                       max_subchunks: int = CHAPTER_MAX_SUBCHUNKS) -> int:
    """不读正文即可估算的单章二次分块数量（与 build_chapter_chunks 的尾部并块行为对齐）

    全量覆盖改造后不再设子块数上限：返回值即实际需要送模型的子块数。
    """
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    est = int(math.ceil(n / float(chunk_chars)))
    if est >= 2 and (n - (est - 1) * chunk_chars) < max(CHAPTER_MIN_SUBCHUNK_CHARS, chunk_chars // 3):
        est -= 1
    return max(1, est)


def estimate_shots_for_chars(char_count) -> int:
    """按内容体量估算镜头数（全量覆盖：约每 CHARS_PER_SHOT 字 1 个镜头，不为凑时长砍内容）"""
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    return max(SHOTS_PER_CHUNK_MIN, int(math.ceil(n / float(CHARS_PER_SHOT))))


def estimate_episode_shots(char_count) -> int:
    """按**章字数**预估整集镜头数（不读正文；口径必须与 convert_chapter_to_script 对齐）。

    用于「这一章要不要拆成多集」的前置规划。口径不对齐就会出问题：
    规划说「不超」，真生成却超 → 白拆；规划说「要拆」，真生成远不到 → 无谓拆碎。

    真实生成的口径是：先按 CHAPTER_CHUNK_CHARS 把本章切成子块，再对**每块**取
    `estimate_shots_for_chars(块字数)`，最后各块求和。这里用 `estimate_subchunks`
    直接拿到子块数（它刻意与 build_chapter_chunks 的尾部并块行为对齐），
    再按平均块长估算每块镜头数。
    """
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    n_chunks = estimate_subchunks(n) or 1
    return n_chunks * estimate_shots_for_chars(max(1, n // n_chunks))


def estimate_episode_sec(shot_count) -> float:
    """按预估镜头数换算**预估成片秒数**（不读正文，规划用）。

    `shots * EPISODE_PLAN_SEC_PER_SHOT`。与 `build_episode_stats` 的 duration 口径
    刻意分开：后者汇总的是**逐镜精算**后的真实秒数（要读镜头内容），
    这个只是「还没生成正文时」的用量级估值，够用来算集数即可。
    """
    return round(max(0, int(shot_count or 0)) * EPISODE_PLAN_SEC_PER_SHOT, 2)


def plan_shots_for_chars(char_count) -> int:
    """**拆集规划专用**的镜头数预估（按实测密度 `EPISODE_PLAN_CHARS_PER_SHOT`）。

    ⚠️ 与 `estimate_shots_for_chars` 的区别是本函数存在的全部理由，别混用：
      · `estimate_shots_for_chars` = 按 `CHARS_PER_SHOT=120`（**提示词引导值**）。
        它喂给模型写提示词、也用作覆盖率容量基准 —— **偏乐观**（实测只有实际的 1/3）。
      · 本函数 = 按 `EPISODE_PLAN_CHARS_PER_SHOT=36`（**实测密度**）。
        只用于「这一章要拆几集」的前置规划 —— **必须贴近实际**，否则拆集判据失效。

    实测依据见 `EPISODE_PLAN_CHARS_PER_SHOT` 的注释（2361 字 → 实测 63 镜）。
    """
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    return max(SHOTS_PER_CHUNK_MIN, int(math.ceil(n / float(EPISODE_PLAN_CHARS_PER_SHOT))))


def estimate_episode_plan_sec(char_count) -> float:
    """按章字数给出的**预估成片秒数**（规划口径，实测密度）。

    这是「按章节内容动态调整集数」的判据来源：`estimate_episode_parts` 用它对比
    `EPISODE_MAX_SEC` 决定拆几集。
    """
    return estimate_episode_sec(plan_shots_for_chars(char_count))


def estimate_episode_parts(char_count, max_sec: int = None) -> int:
    """一章需要拆成几集（**按预估成片时长**判断，2026-09-26 起）。

    规则：`parts = ceil(预估秒数 / max_sec)`，`max_sec` 默认 `EPISODE_MAX_SEC`（180 秒）。
    预估不超上限就是 1 集 —— 集数完全由「这一章能拍几分钟」决定。

    ⚠️ 预估秒数走 `estimate_episode_plan_sec`（**实测密度 36 字/镜**），
    而不是 `estimate_episode_shots`（提示词口径 120 字/镜）。用错口径会让判据失效 ——
    本次实测踩到：按 120 字/镜估算，42 章全部落在 180 秒内 → 集数永远拆不动，
    而实际每集约 6 分钟。详见 `EPISODE_PLAN_CHARS_PER_SHOT` 注释。

    ⚠️ 与旧的「按镜数 / MAX_SHOTS_PER_EPISODE(78)」判据的区别见 EPISODE_MAX_SEC 上方注释。
    `MAX_SHOTS_PER_EPISODE` 现在退化为**另一条独立的技术红线**（H3 资源），
    不再承担「拆集」职责；两者取更碎的那个（在 `convert_chapter_to_script` 里有兜底）。

    ⚠️ 短章不拆：字数低于 `CHAPTER_MIN_CHARS` 时退回 1 集 —— 拆开只会得到
    「每集几十字、几秒成片」的碎片，破坏叙事完整性（历史缺陷，见 EPISODES_PER_CHAPTER 注释）。
    """
    n = max(0, int(char_count or 0))
    if not n or n < CHAPTER_MIN_CHARS:
        return 1
    cap_sec = int(max_sec or EPISODE_MAX_SEC)
    if cap_sec <= 0:
        return 1
    est_sec = estimate_episode_plan_sec(n)
    if est_sec <= cap_sec:
        return 1
    return max(2, int(math.ceil(est_sec / float(cap_sec))))


#: 拆章时切点吸附的候选边界（按优先级从高到低：段落空行 → 句末 → 换行）
_CUT_MARKERS = ("\n\n", "\n", "。", "！", "？", "；", "…", "”", "』", "」")


def _snap_cut(text: str, ideal: int, lo: int, hi: int, prev: int) -> int:
    """把理想切点吸附到最近的语义边界（段落空行 > 换行 > 句末标点）。

    约束：返回值必须严格落在 `(prev, hi)` 内，否则跨段重叠或丢字。
    找不到合适边界时原样返回 `ideal`（同样被夹进合法区间）。
    """
    ideal = int(ideal)
    ideal = max(int(prev) + 1, min(ideal, int(hi) - 1))
    if not text:
        return ideal
    span = max(1, int(hi) - int(lo))
    window = max(80, span // 12)
    w_lo = max(int(prev) + 1, ideal - window)
    w_hi = min(int(hi) - 1, ideal + window)
    if w_hi <= w_lo:
        return ideal
    chunk = text[w_lo:w_hi]
    for marker in _CUT_MARKERS:
        best = None
        pos = chunk.find(marker)
        while pos != -1:
            cut = w_lo + pos + len(marker)      # 切在标记**之后**：标记留在前一段
            if prev < cut < hi:
                if best is None or abs(cut - ideal) < abs(best - ideal):
                    best = cut
            pos = chunk.find(marker, pos + 1)
        if best is not None:
            return best
    return ideal


def split_chapter_for_episodes(chapter: dict, text: str = "",
                               max_shots: int = None,
                               fixed_parts: int = None,
                               max_sec: int = None) -> list:
    """把一章拆成 1..N 个「拍摄单元」（一个单元 = 一集）。

    拆分规则：**按预估成片时长自动判断**（2026-09-26 起）
    ---------------------------------------------------
    1. **内容份数（主规则）**：按 `estimate_episode_parts` —— 先由章字数预估镜头数，
       再乘 `EPISODE_PLAN_SEC_PER_SHOT` 得到预估成片秒数，除以 `max_sec`
       （默认 ``EPISODE_MAX_SEC``=180 秒 = 3 分钟）向上取整。
       预估不超上限就是 1 集，超限才拆。集数因此**完全由「这一章能拍几分钟」决定**。
    2. **固定下限（可选）**：``fixed_parts``（默认取全局 ``EPISODES_PER_CHAPTER``，
       当前 =1 即关闭）作为「每章至少拆 N 集」的兜底下限。设为 2 可强制短章也拆；
       取 ``max(本下限, 内容份数)``，因此调大它只可能更碎，绝不会更碎不动。

    ⚠️ 为什么不再按镜数判据（历史实现）：
        旧实现是 `ceil(预估镜数 / max_shots)`，`max_shots` 默认 `MAX_SHOTS_PER_EPISODE`=78。
        78 镜 ≈ 6.5 分钟成片，而产品口径是「一集 1-2 分钟、最长 3 分钟」——
        实测 1766 字/章的《逆天系统》只估 15 镜，**永远触发不了拆集**，
        于是「按章节内容动态调整集数」形同虚设（42 章 → 42 集，每集 6 分钟）。
        改按时长判据后同一本书变成每章 2~3 集。
        `max_shots` 参数**保留但仅作为兜底**（时长口径算不出时回退），旧调用方不致报错。

    设计要点
    --------
    - **不拆时零影响**：预估不超限且下限为 1 → 返回单段且 `start/end` 原样不变，
      老项目（一章一集）行为完全不变。
    - **过短章不拆**：章字数 < ``CHAPTER_MIN_CHARS``（300）时拆开没有意义（每段
      只剩一两百字），直接退回 1 集。
    - **不丢字、不重叠**：各段首尾相接，`[start, end)` 逐段连续覆盖原章区间。
    - **切点吸附语义边界**（段落空行 > 换行 > 句末标点，在理想切点 ±span/12 内找），
      避免把一句话 / 一段动作劈到两集（跨集是独立生成，劈开会导致语义断裂）。
    - `text` 为空时退化为按字数等分硬切 —— 仍然不丢字。

    返回 `[{"part": 1..N, "parts": N, "start": int, "end": int, "char_count": int}]`。
    """
    seg_start = int(chapter.get("start") or 0)
    seg_end = int(chapter.get("end") or 0)
    if seg_end < seg_start:
        seg_end = seg_start
    n_chars = int(chapter.get("char_count") or (seg_end - seg_start) or 0)

    # 主规则：按预估成片时长（见上方注释）。max_sec 未显式给时用全局口径。
    if max_sec:
        parts = estimate_episode_parts(n_chars, max_sec=max_sec)
    else:
        parts = estimate_episode_parts(n_chars)
    # 兜底：时长口径因常量异常算不出时，退回旧的镜数判据（保证「总会拆」不会变成「永不拆」）。
    if parts <= 1 and max_shots:
        cap = int(max_shots or 0)
        if cap > 0:
            est = estimate_episode_shots(n_chars)
            if est > cap:
                parts = max(2, int(math.ceil(est / float(cap))))
    # 可选下限（默认 1 = 关闭）：仅当显式要求「每章至少 N 集」时才抬高。
    # 取更碎的那个，保证每段仍 ≤ 上限。
    n_fixed = EPISODES_PER_CHAPTER if fixed_parts is None else int(fixed_parts or 0)
    if n_fixed > 1 and n_chars >= CHAPTER_MIN_CHARS:
        parts = max(parts, n_fixed)
    total = seg_end - seg_start
    if parts <= 1 or total <= 0:
        return [{"part": 1, "parts": 1, "start": seg_start, "end": seg_end,
                 "char_count": n_chars}]

    # ⚠️ 等分算出的 parts 是「理想份数」，但切点要**吸附到语义边界**（段落/换行/句末），
    #    吸附会让各段长度不均匀 —— 实测出现过 14269 字拆 23 段时，有 8 段被顶到
    #    210 秒（超过 180 秒上限），即**理想份数不够**。
    #    处置：切完**实测**每段秒数，**按越限段数**加份重切（一轮就大致补齐），
    #    循环到没有越限段为止（设安全上限防死循环）。
    #    为什么不做「事后修剪越限段」：修剪会在句中劈开句子，破坏「跨集不劈句」不变量。
    #    加份数重切则保持所有不变量（首尾相接、不劈句、不丢字），只是集数更碎 ——
    #    更碎是安全的（原文不丢、每集仍是完整叙事单元），不拆够才危险。
    cap_sec = int(max_sec or EPISODE_MAX_SEC)
    #: 段长低于该字数时语义吸附会**无法收敛**（切点挤到同一句末、加份指数爆炸）——
    #:   实测 max_sec=10 的极端探针下，400 字理想段长 33 字 < 40，吸附窗口(80 字)远大于
    #:   段长，每轮加份后 ideal 间隔更小但句号只有 18 个，切点无法细分 → parts 涨到 5889
    #:   仍越限。此时放弃语义吸附、改**按字数硬切**（保证每段字数均匀、不越界）。
    _SNAP_MIN_CHARS = 40
    _max_parts = max(parts, 4096)  # 安全上限（正常远达不到，防死循环）
    while parts < _max_parts:
        if total / float(parts) < _SNAP_MIN_CHARS:
            break                   # 段长太小，语义吸附无法收敛，交给下面的硬切兜底
        _b = [seg_start]
        for i in range(1, parts):
            ideal = seg_start + int(round(total * i / float(parts)))
            _b.append(_snap_cut(text, ideal, seg_start, seg_end, _b[-1]))
        _b.append(seg_end)
        if cap_sec <= 0:
            break
        _over = sum(
            1 for i in range(parts)
            if estimate_episode_plan_sec(max(0, _b[i + 1] - _b[i])) > cap_sec)
        if _over == 0:
            break
        parts += max(1, _over)     # 越限几段就加几份，快速收敛（而不是每次只 +1）

    # ⚠️ 用**最终确定的 parts** 重新生成一次 bounds：上面的循环可能在「段长 < 阈值」或
    #    「parts 超上限」时直接退出，此时局部 `_b` 对应旧 parts（比最终 parts 少切点）
    #    → 直接切片会 IndexError（实测踩到）。这里重算保证两者严格同步。
    #    段长 >= 阈值走语义吸附（正常场景）；< 阈值走硬切（极端场景，保不崩溃）。
    _final_seg = total / float(parts)
    bounds = [seg_start]
    for i in range(1, parts):
        ideal = seg_start + int(round(total * i / float(parts)))
        if _final_seg >= _SNAP_MIN_CHARS:
            bounds.append(_snap_cut(text, ideal, seg_start, seg_end, bounds[-1]))
        else:
            bounds.append(ideal)   # 硬切：不吸附语义边界，按字数均分
    bounds.append(seg_end)

    out = []
    for i in range(parts):
        s, e = bounds[i], bounds[i + 1]
        out.append({"part": i + 1, "parts": parts, "start": s, "end": e,
                    "char_count": max(0, e - s)})
    return out


def chapter_advice(char_count: int, subchunk_count: int = 0) -> dict:
    """给单章的规模提示（过短只提示不合并；全量覆盖：内容体量决定镜头数）

    2026-09-26：提示语改为按**产品时长口径**给出——直接告诉用户「这一章会拆成几集、
    每集大约几分钟」，而不是只说镜数（用户关心的是集数与时长，不是镜数）。
    """
    n = int(char_count or 0)
    too_short = n < CHAPTER_MIN_CHARS
    too_long = n > CHAPTER_CHUNK_CHARS
    est_shots = estimate_shots_for_chars(n)
    # ⚠️ 展示给用户的集数/时长必须用**实测密度**（plan_shots_for_chars），
    #    否则界面会告诉用户「这一章 2 分钟」而实际出 6 分钟（本次实测踩到的坑）。
    plan_shots = plan_shots_for_chars(n)
    est_sec = estimate_episode_sec(plan_shots)
    est_parts = estimate_episode_parts(n)
    _minutes = f"{est_sec / 60.0:.1f} 分钟"
    if too_short:
        msg = (f"本章仅 {n} 字，内容偏短，成片约 {est_sec:g} 秒 / 约 {plan_shots} 个镜头"
               f"（按需求不做自动合并）")
    elif est_parts > 1:
        msg = (f"本章 {n} 字，预估成片约 {_minutes}（{est_sec:g} 秒 / 约 {plan_shots} 镜），"
               f"超过单集上限 {EPISODE_MAX_SEC / 60.0:g} 分钟 → 将自动拆成 {est_parts} 集"
               f"（每集约 {est_sec / est_parts / 60.0:.1f} 分钟）")
    elif too_long and subchunk_count >= 2:
        msg = (f"本章 {n} 字，超过 {CHAPTER_CHUNK_CHARS} 字，将自动二次分块为 "
               f"{subchunk_count} 个子块全量覆盖送模型（不抽样、不丢内容），"
               f"预计约 {plan_shots} 个镜头 / 约 {est_sec:g} 秒成片")
    elif too_long:
        msg = (f"本章 {n} 字，略超 {CHAPTER_CHUNK_CHARS} 字，按单块全量处理，"
               f"预计约 {plan_shots} 个镜头 / 约 {est_sec:g} 秒成片")
    else:
        msg = (f"本章 {n} 字，单块即可完成，预计约 {plan_shots} 个镜头 / "
               f"约 {est_sec:g} 秒成片（镜头数随内容体量自动扩展）")
    return {"char_count": n, "too_short": too_short, "too_long": too_long,
            "subchunks": subchunk_count, "estimated_shots": est_shots,
            "estimated_plan_shots": plan_shots,
            "estimated_sec": est_sec, "estimated_parts": est_parts,
            "message": msg}


def episode_project_name(novel_name: str, episode_no) -> str:
    """每集独立项目名：后续资产/分镜/视频按「某一集」继续走通，互不覆盖"""
    return f"{safe_project_name(novel_name)}_第{int(episode_no)}集"


def convert_chapter_to_script(client, novel_meta: dict, novel_text: str, chapter: dict,
                              style: str = "3D动漫渲染", target_shots: int = 12,
                              episode_no: int = 1, chunk_chars: int = CHAPTER_CHUNK_CHARS,
                              max_subchunks: int = CHAPTER_MAX_SUBCHUNKS,
                              progress_cb=None, continuity_ctx: dict = None,
                              cache_dir: str = "") -> dict:
    """把「一章」转成一集 A 版剧本（每章一集，独立落盘）

    continuity_ctx（跨集连贯性方案 A/B/C）：由 continuity.build_continuity_context 组装，
    包含项目级设定库、上集摘要卡、衔接契约、项目级风格指南、金句清单、口吻词典与运镜术语表；
    传入后本集生成将带着跨集上下文，且 style_guide 改用项目级唯一配置。

    cache_dir 非空时，提炼 / 分镜两步启用断点缓存：重跑时命中已完成的块即跳过模型调用，
    使「网关偶发挂起 → pipeline 整步重试」不再每次都从头烧 20+ 分钟（见文件上方缓存说明）。
    """
    def report(phase, current, total, message, percent):
        if progress_cb:
            try:
                progress_cb(phase, current, total, message, percent)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"进度回调异常：{e}")

    t0 = time.time()
    novel_title = novel_meta.get("title") or novel_meta.get("name") or "未命名小说"
    chapter_title = (chapter.get("title") or f"第{chapter.get('index')}章").strip()
    seg = (novel_text or "")[int(chapter.get("start") or 0):int(chapter.get("end") or 0)]
    if not seg.strip():
        raise LLMError(f"章节正文为空：{chapter_title}")
    if len(seg) < CHAPTER_MIN_CHARS:
        logger.warning(f"章节偏短（{len(seg)} 字）：{chapter_title}")

    all_chunks, sampled, sampled_idx = build_chapter_chunks(seg, chunk_chars, max_subchunks)
    if not sampled:
        raise LLMError(f"章节无法分块：{chapter_title}")
    advice = chapter_advice(len(seg), len(all_chunks))

    warnings = []
    trunc_events = []          # 截断自动提额 / 二次二分事件（写入 metadata，便于排查）
    if advice["too_short"]:
        warnings.append(advice["message"])
    # 全量覆盖：镜头数随内容体量自动扩展（约每 CHARS_PER_SHOT 字 1 镜），不再为凑固定时长砍内容
    est_shots_total = sum(estimate_shots_for_chars(c.get("char_count") or 0) for c in all_chunks)

    # ---- 单集镜头数硬上限（见 MAX_SHOTS_PER_EPISODE 的注释）----
    # 超过上限时不删原文，只按比例收紧**每块的镜头额度**：全部子块照样送模型、原文覆盖率不变，
    # 只是每镜承载的原文变多（镜头密度变稀）。真正的保底在下方对最终 shots 的硬截断。
    per_chunk_cap = 0                      # 0 = 不限制
    if est_shots_total > MAX_SHOTS_PER_EPISODE:
        _n_sampled = max(1, len(sampled))
        per_chunk_cap = max(1, -(-MAX_SHOTS_PER_EPISODE // _n_sampled))   # 向上取整均分
        warnings.append(
            f"本章预计 {est_shots_total} 镜，超过单集硬上限 {MAX_SHOTS_PER_EPISODE} 镜"
            f"（整集 H3 连续渲染跑到约 {MAX_SHOTS_PER_EPISODE} 段后会因系统资源耗尽崩溃，"
            f"已实测）→ 每块镜头额度收紧为 {per_chunk_cap} 镜。原文不删减，但镜头密度变稀；"
            f"**建议把本章拆成 2 集分别生成**以保留画面细节。")
        logger.warning("第%s集：镜头预算超限（预计 %d > 上限 %d），每块额度收紧为 %d：%s",
                       episode_no, est_shots_total, MAX_SHOTS_PER_EPISODE, per_chunk_cap,
                       chapter_title)

    total_steps = len(sampled) + 2 + len(sampled)
    report("prepare", 0, total_steps,
           f"第{episode_no}集《{chapter_title}》：{len(seg)} 字，二次分块 {len(all_chunks)} 块，"
           f"全量送模型 {len(sampled)} 块（不抽样），预计 {est_shots_total} 镜", 3)

    # ① 逐块提炼（按 chunk.index 建立映射，避免与 sampled 错位）
    outlines, outline_by_chunk = [], {}
    for i, chunk in enumerate(sampled):
        report("outline", i + 1, total_steps,
               f"第{episode_no}集 提炼子块 {chunk['index']}/{chunk['total']}…",
               int(5 + (i + 1) / total_steps * 55))
        try:
            ol = extract_chunk_outline(client, chunk, novel_title, events=trunc_events,
                                       cache_dir=cache_dir)
        except LLMTruncatedError as e:
            warnings.append(f"第 {chunk['index']} 子块提炼被截断失败（已自动提额并尝试二次切分）：{e}")
            logger.warning(f"第 {chunk['index']} 子块提炼被截断失败：{e}")
            continue
        except LLMError as e:
            warnings.append(f"第 {chunk['index']} 子块提炼失败：{e}")
            logger.warning(f"第 {chunk['index']} 子块提炼失败：{e}")
            continue
        outlines.append(ol)
        outline_by_chunk[chunk["index"]] = ol
    if not outlines:
        raise LLMError("本章所有子块提炼均失败（每块均已自动提高 max_tokens，必要时二次切分）："
                       + ("；".join(warnings) or "未知错误"))

    # ② 汇总本集设定
    report("bible", len(sampled) + 1, total_steps, f"第{episode_no}集 汇总人物 / 物品 / 场景设定…", 65)
    bible = _as_dict(build_bible(client, outlines, f"{novel_title}·{chapter_title}", style, 1,
                                 target_shots, events=trunc_events,
                                 continuity_ctx=continuity_ctx, cache_dir=cache_dir))

    characters = _norm_list(bible.get("characters"), 8,
                            ["name", "age", "gender", "identity", "appearance", "outfit", "personality",
                             "voice_style", "reference_prompt_zh", "reference_prompt_en"])
    items = _norm_list(bible.get("items"), 3,
                       ["name", "category", "appearance", "owner", "importance",
                        "reference_prompt_zh", "reference_prompt_en"])
    scenes = _norm_list(bible.get("scenes"), 8,
                        ["name", "location", "appearance", "scene_lighting", "reference_prompt_zh", "reference_prompt_en"])
    if not characters:
        raise LLMError("模型未返回有效角色设定，转换中止")
    # 风格：以调用方传入的 style 为准（模型的返回值可能是自我发挥，用户意图优先）
    eff_style = style_kit.normalize_style(style) or style_kit.normalize_style(bible.get("style"))
    # 确定性补写：模型不得自写风格词（写了会重复），统一在这里收尾，中英双语都补
    style_filled = style_kit.apply_asset_style_all(characters, eff_style) \
        + style_kit.apply_asset_style_all(items, eff_style) \
        + style_kit.apply_asset_style_all(scenes, eff_style)
    if eff_style and style_filled:
        logger.info("第%s集：已为 %d 条资产参考提示词补写风格「%s」", episode_no, style_filled, eff_style)
    bible = {"title": str(bible.get("title") or novel_title)[:40],
             "theme": str(bible.get("theme") or "")[:200],
             "style": eff_style[:60],
             "characters": characters, "items": items, "scenes": scenes,
             "production_notes": bible.get("production_notes") or {}}

    # ③ 逐块写分镜（每块镜头数按该块原文体量自动扩展，不再按 target_shots 摊薄砍内容）
    all_shots = []
    for i, chunk in enumerate(sampled):
        chunk_chars_i = chunk.get("char_count") or len(chunk.get("text") or "")
        per_chunk = estimate_shots_for_chars(chunk_chars_i)
        if per_chunk_cap:
            # 单集硬上限生效：本块额度不得超预算（原文不丢，只是每镜承载更多）
            per_chunk = max(1, min(int(per_chunk), int(per_chunk_cap)))
        report("shots", len(sampled) + 2 + i, total_steps,
               f"第{episode_no}集 编写分镜（子块 {chunk['index']}/{chunk['total']}，"
               f"{chunk_chars_i} 字 → {per_chunk} 镜）…",
               int(70 + (i + 1) / total_steps * 25))
        ol = outline_by_chunk.get(chunk["index"])
        if not ol:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块缺少提炼结果，已按原文兜底生成 {len(fb)} 镜"
                            f"（内容未丢，建议人工润色）")
            logger.warning(f"第 {chunk['index']} 子块缺少提炼结果，已兜底 {len(fb)} 镜")
            continue
        # P1-2 接缝重叠（借 ViMax overlap 思路、按本架构落地）：i>0 时把「上一子块结尾」
        # 注入本块，让开头承接上段。上一子块 = sampled[i-1]，其 outline 按 index 取；
        # i=0 → 上子块 None → prev_tail="" → prompt 不出现该段（零变化）。
        _prev_chunk = sampled[i - 1] if i > 0 else None
        _prev_outline = outline_by_chunk.get(_prev_chunk["index"]) if _prev_chunk else None
        _prev_tail = _prev_tail_block(_prev_chunk, _prev_outline)
        try:
            all_shots.extend(build_shots_for_chunk(client, bible, ol, chunk, per_chunk,
                                                   events=trunc_events,
                                                   continuity_ctx=continuity_ctx,
                                                   cache_dir=cache_dir,
                                                   shots_hard_cap=per_chunk_cap,
                                                   prev_tail=_prev_tail))
        except LLMTruncatedError as e:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块分镜被截断失败（已自动提额并尝试二次切分），"
                            f"已按原文兜底生成 {len(fb)} 镜（内容未丢，建议人工润色）：{e}")
            logger.warning(f"第 {chunk['index']} 子块分镜被截断失败，已兜底 {len(fb)} 镜：{e}")
        except LLMGatewayUnavailable as e:
            fb = _gateway_down_fallback(e, chunk, per_chunk, bible, all_shots)
            all_shots.extend(fb)
            warnings.append(_gateway_down_warning(chunk, len(fb)))
            logger.error(f"第 {chunk['index']} 子块因网关不可用降级兜底 {len(fb)} 镜：{e}")
        except LLMError as e:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块分镜失败，已按原文兜底生成 {len(fb)} 镜"
                            f"（内容未丢，建议人工润色）：{e}")
            logger.warning(f"第 {chunk['index']} 子块分镜失败，已兜底 {len(fb)} 镜：{e}")

    shots = _norm_shots(all_shots, bible, 1)

    # ---- 单集镜头数硬上限（保底截断）----
    # 上面的额度收紧只是「引导」，模型仍可能超产。这里做**最终保证**：整集镜头数绝不超过
    # MAX_SHOTS_PER_EPISODE，否则整集 H3 连续渲染会跑到崩溃点（见常量注释）。
    # 截断会丢失尾部情节 → 必须响亮记录（写进 metadata.warnings + error 日志），
    # 并明确指引「拆成 2 集重新生成」，不能静默吞掉。
    if len(shots) > MAX_SHOTS_PER_EPISODE:
        _dropped = len(shots) - MAX_SHOTS_PER_EPISODE
        shots = shots[:MAX_SHOTS_PER_EPISODE]
        warnings.append(
            f"⚠ 本集生成 {len(shots) + _dropped} 镜，超过单集硬上限 {MAX_SHOTS_PER_EPISODE} 镜，"
            f"已截断末尾 {_dropped} 镜（**尾部情节会缺失**）。请把本章拆成 2 集重新生成，"
            f"以完整覆盖原文。")
        logger.error("第%s集：镜头数 %d 超上限 %d，已硬截断 %d 镜（%s）—— 建议拆章分集",
                     episode_no, len(shots) + _dropped, MAX_SHOTS_PER_EPISODE, _dropped,
                     chapter_title)

    for sh in shots:
        sh["episode"] = int(episode_no)
    if not shots:
        raise LLMError("模型未返回有效分镜（每块均已自动提高 max_tokens，必要时二次切分）："
                       + ("；".join(warnings) or "未知错误"))

    # 外形一致性收敛（2026-09-24）：资产**出图依据**是 reference_prompt_zh，而分镜
    # 提示词 / 质检口径取 appearance —— 两者漂移会让「文字说黑寸头、参考图却是黑发高冠」
    # 两条互斥指令同时生效（分镜是 cfg=1.0 的参考图编辑型 → 参考图压过文字），模型每次
    # 随机倒向一边、质检必然抓到另一边 → 无限重试、整集跑不过。落盘前按 appearance 收敛。
    asset_prompt_kit.reconcile_script({"characters": characters, "shots": shots})

    project_name = episode_project_name(novel_meta.get("name") or novel_title, episode_no)
    script = {
        "title": bible["title"],
        "episode_no": int(episode_no),
        "episode_title": chapter_title,
        "theme": bible["theme"],
        "style": bible["style"],
        "characters": characters,
        "items": items,
        "scenes": scenes,
        "shots": shots,
        "production_notes": {
            "total_shots": len(shots),
            "estimated_duration": len(shots) * 5,
            # C⑥：style_guide 提升为项目级唯一配置（continuity 提供时优先，不再每集各写一套）
            "style_guide": str(_ctx_block(continuity_ctx, "style_guide_text")
                               or (bible.get("production_notes") or {}).get("style_guide") or "")[:300],
            "style_guide_source": "project" if _ctx_block(continuity_ctx, "style_guide_text") else "episode",
            "bible_locked": bool(continuity_ctx),
            "continuity_version": str((continuity_ctx or {}).get("version") or "") or None,
        },
        "metadata": {
            "source": "novel_chapter_to_script",
            "source_novel": {
                "novel_id": novel_meta.get("novel_id"),
                "name": novel_meta.get("name"),
                "title": novel_meta.get("title"),
                "char_count": novel_meta.get("char_count"),
                "chapter_count": novel_meta.get("chapter_count"),
            },
            "episode_no": int(episode_no),
            "episode_title": chapter_title,
            "chapter_index": chapter.get("index"),
            "chapter_title": chapter_title,
            "chapter_char_count": len(seg),
            "chunks_total": len(all_chunks),
            "chunks_used": len(sampled),
            # 全量覆盖：sampled_chunks 语义 = 全部子块下标（保留旧字段名兼容前端）
            "sampled_chunks": sampled_idx,
            "coverage_mode": "full",
            "chars_per_shot": CHARS_PER_SHOT,
            "estimated_shots": est_shots_total,
            "shots_per_chunk": [estimate_shots_for_chars(c.get("char_count") or 0) for c in all_chunks],
            "subchunk_chars": chunk_chars,
            "style": style,
            "target_shots": target_shots,
            "project_name": project_name,
            "model": client.model,
            "base_url": client.base_url,
            "elapsed_sec": round(time.time() - t0, 1),
            "warnings": warnings,
            "truncation_events": trunc_events,
            "truncation_retries": len([e for e in trunc_events if e.get("attempt")]),
            "auto_sub_splits": len([e for e in trunc_events if e.get("event") == "sub_split"]),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    }
    # ⑤ AI 转剧本阶段自动判定「本集镜头数 / 本集时长（秒）」并写入 Schema（下游链路直接引用）
    apply_episode_schema(script)
    report("done", total_steps, total_steps,
           f"第{episode_no}集完成：{len(characters)} 角色 / {len(items)} 物品 / "
           f"{len(scenes)} 场景 / {script['shot_count']} 镜头 / 本集约 {script['episode_duration_sec']}s", 100)
    return script


def episode_script_path(script_dir: str, novel_name: str, episode_no, project_key: str = None) -> str:
    """按集组织落盘路径：output/scripts/<项目键>/第N集.json

    project_key 由项目注册表（project_store）统一分配，保证「一部小说一个独立目录」；
    未提供时退回按小说名安全化，兼容旧行为。
    """
    folder = project_key or safe_project_name(novel_name)
    return os.path.abspath(os.path.join(
        script_dir, folder, f"第{int(episode_no)}集.json"))


def save_episode_script(script: dict, script_dir: str, novel_name: str, episode_no,
                        project_key: str = None) -> str:
    """按集落盘（覆盖同名集号），返回绝对路径"""
    path = episode_script_path(script_dir, novel_name, episode_no, project_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    meta = script.setdefault("metadata", {})
    meta["script_path"] = path
    meta["project_name"] = meta.get("project_name") or episode_project_name(novel_name, episode_no)
    meta["project_key"] = project_key or safe_project_name(novel_name)
    meta["episode_no"] = int(episode_no)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    logger.info(f"第{episode_no}集剧本已落盘：{path}")
    return path


def list_episodes(script_dir: str, novel_name: str, project_key: str = None) -> list:
    """列出某小说已生成的剧集（按集号排序）"""
    folder = os.path.join(script_dir, project_key or safe_project_name(novel_name))
    if not os.path.isdir(folder):
        return []
    out = []
    for fn in os.listdir(folder):
        m = re.match(r"^第(\d+)集\.json$", fn)
        if not m:
            continue
        path = os.path.abspath(os.path.join(folder, fn))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"剧集读取失败 {path}：{e}")
            continue
        meta = data.get("metadata") or {}
        shots = data.get("shots") or []
        stats = meta.get("episode_stats") or build_episode_stats(shots)
        cov = meta.get("coverage") or {}
        out.append({
            "episode_no": int(m.group(1)),
            "file": fn,
            "path": path,
            "title": data.get("title"),
            "episode_title": data.get("episode_title") or meta.get("chapter_title"),
            "chapter_index": meta.get("chapter_index"),
            "chapter_char_count": meta.get("chapter_char_count"),
            "characters": len(data.get("characters") or []),
            "items": len(data.get("items") or []),
            "scenes": len(data.get("scenes") or []),
            "shots": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
            "shot_count": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
            "episode_duration_sec": float(data.get("episode_duration_sec") or stats.get("duration_sec") or 0),
            "duration_per_shot_sec": float(data.get("duration_per_shot_sec")
                                           or stats.get("duration_per_shot_sec") or 0),
            "episode_stats": stats,
            "prompt_ready": sum(1 for s in shots if isinstance(s, dict) and s.get("prompt_h3")),
            "project_name": meta.get("project_name") or episode_project_name(novel_name, m.group(1)),
            "generated_at": meta.get("generated_at"),
            "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
            "warnings": meta.get("warnings") or [],
            # ④⑤ 原文覆盖率（落盘可查；未跑过新流程的旧剧集为 None）
            "coverage_percent": cov.get("coverage_percent"),
            "coverage_plot_percent": cov.get("plot_coverage_percent", cov.get("coverage_percent")),
            "coverage_detail_percent": cov.get("detail_coverage_percent", cov.get("char_coverage_percent")),
            "coverage_detail_passed": cov.get("detail_passed"),
            "coverage_zero_omission": cov.get("zero_omission"),
            "coverage_passed": cov.get("passed"),
            "coverage_threshold_percent": cov.get("threshold_percent"),
            "coverage_missing_count": cov.get("missing_count"),
            "coverage_supplement_shots": cov.get("supplement_shots"),
            "coverage_supplement_rounds": cov.get("supplement_rounds"),
            "coverage_report_path": meta.get("coverage_report_path"),
            "coverage_verified": bool(cov.get("checked_at")),
        })
    out.sort(key=lambda r: r["episode_no"])
    return out


def load_episode_script(script_dir: str, novel_name: str, episode_no, project_key: str = None) -> dict:
    """读取已落盘的单集剧本（供前端切集预览 / 载入后续步骤）"""
    path = episode_script_path(script_dir, novel_name, episode_no, project_key)
    if not os.path.isfile(path):
        raise EpisodeNotFoundError(f"第{int(episode_no)}集尚未生成：{path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_project_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip())
    return name[:40] or "novel_project"


def save_generated_script(script: dict, script_dir: str, project_name: str = None,
                          project_key: str = None) -> str:
    """落盘剧本 JSON。

    - project_key 为空：沿用旧行为，落在 output/scripts/<项目名>_<时间戳>.json
    - project_key 非空：落在 output/scripts/<项目键>/整本_<时间戳>.json，
      与项目隔离目录结构一致（项目键由 project_store 统一分配）
    """
    meta = script.setdefault("metadata", {})
    if project_key:
        pname = str(project_key)
        folder = os.path.join(script_dir, pname)
        os.makedirs(folder, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.abspath(os.path.join(folder, f"整本_{ts}.json"))
    else:
        os.makedirs(script_dir, exist_ok=True)
        pname = safe_project_name(project_name or script.get("title") or "novel_project")
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.abspath(os.path.join(script_dir, f"{pname}_{ts}.json"))
    meta["script_path"] = path
    meta["project_name"] = pname
    meta["project_key"] = pname
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    logger.info(f"剧本已落盘：{path}")
    return path

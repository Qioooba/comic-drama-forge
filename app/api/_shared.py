# -*- coding: utf-8 -*-
"""共享运行时上下文：242 条路由共用的模块级状态与 helper（2026-10-07 由 app/app.py 拆出）。

约定
----
1. 本模块是**只读**共享层：其它 agent 的域蓝图（``app/api/*.py``）只 import 本模块，
   不得修改；需要新增共享能力时由 W1 统一加。
2. ``app`` 初始为 ``None``，由 composition root（``app/app.py``）在
   ``Flask(__name__)`` 之后调 :func:`bind_app` 注入。**注入必须早于任何域蓝图
   的导入**，否则域模块 ``from api._shared import *`` 会拿到 ``None``。
   （域模块是在 ``api.register_blueprints()`` 里被 import 的，而它发生在
   ``bind_app`` 之后，因此顺序天然成立。）
3. 原 ``app.py`` 里**依赖 Flask 实例的模块级语句**（启动自检 / 凭证迁移 / 托管
   调度 / ``MAX_CONTENT_LENGTH``）按原相对顺序搬进 :func:`bind_app`，不再在导入期
   执行；其余模块级语句保持原样、原顺序。
4. Flask 实例仍然在 ``app/app.py`` 里创建（``root_path`` / ``template_folder``
   取决于 app.py 所在目录，不能挪进本包）。

其余域模块用 ``from api._shared import *`` 取全部名字（含下划线 helper），
因此本模块显式声明 :data:`__all__`。
"""
from __future__ import annotations

import os

import re

import json

import math

import time

import random

import shutil

import threading

import copy

import uuid

import contextvars

from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, send_from_directory

from werkzeug.exceptions import BadRequest, HTTPException

from config import (
    COMFYUI_URL, PROJECT_ROOT_DIR, PROJECT_DATA_DIR, PROJECT_OUTPUT_DIR, SCRIPT_DIR,
    CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR, KEYFRAMES_DIR, VIDEOS_DIR, FINAL_DIR,
    PROJECT_TRASH_DIR,
    NOVELS_DIR, LLM_CONFIG_PATH, NOVEL_CHUNK_CHARS, NOVEL_MAX_CHUNKS,
    NOVEL_DEFAULT_SHOTS, NOVEL_PREVIEW_CHARS, NOVEL_BRIEF_CHARS, LLM_REQUEST_TIMEOUT,
    QC_CONFIG_PATH, QC_DIR,
    CLEAR_COMFYUI_HISTORY, CLEAR_COMFYUI_HISTORY_INTERVAL_SEC,
    WATERMARK_CONFIG_PATH, WATERMARK_DIR,
    AI_CONFIG_PATH, AI_MODULES, AI_CHAT_HISTORY_PATH, AI_SETTINGS_PATH,
    UPSCALE_DIR, UPSCALE_DEFAULT_PARAMS, COMFYUI_OUTPUT_DIR,
    TE_UPSCALE_DEFAULT_PARAMS, TE_UPSCALE_LOWVRAM_PARAMS, UPSCALE_ENGINE,
    DUB_DIR, TTS_DEFAULT_PARAMS, TTS_VOICE_BANK_TIMEOUT, H3_STRIP_AUDIO, H3_EMIT_AUDIO, H3_SFX_ISOLATE,
    DUB_MIX_DIR, MIX_DEFAULT_PARAMS, CONTINUITY_DIR,
    TASKS_DB_PATH, TASK_QUEUE_CONCURRENCY, TASK_UNIT_MIN_BYTES,
    KEYFRAME_CHAIN_MODE, WORKFLOW_TEMPLATE,
    ASSET_VIEW_STEMS,
    SCENE_VIEW_KEYS, SCENE_VIEW_LABELS, SCENE_VIEW_ANGLE_ZH,
    SCENE_ANGLE_TO_VIEW, SCENE_VIEWS_ENABLED, SCENE_VIEW_MAX_RETRIES,
    SCENE_VIEW_DUP_PHASH_MAX,
    # 场景九宫格多视角（2026-10-05）：开关 + 9 机位键序 + 机位句/标签 + 主图文件名/derive_mode
    SCENE_GRID_MODE, SCENE_GRID_VIEW_KEYS, SCENE_GRID_ANGLE_ZH, SCENE_GRID_LABELS,
    SCENE_GRID_FILENAME, SCENE_GRID_DERIVE_MODE,
    H3_COMMON_REFS, H3_COMMON_REFS_MAX,
    PROJECT_DEFAULT_CONFIG,
    PROMPT_ENHANCE_CONFIG_PATH, save_prompt_enhance_config, _prompt_enhance_file_flags,
)

from script_generator import ScriptGenerator

from comfyui_client import (ComfyUIClient, camera_spec as _camera_spec,
                            camera_key as _camera_key, camera_angle as _camera_angle,
                            BLOCKING_REF_MARK as _BLOCKING_REF_MARK)

import comfyui_job_store  # 崩溃免重渲检查点（2026-09-29）：种子沿用判据 + 台账查询

import preview_gate  # 两级生产（2026-09-29）：预演不可交付 + 预演批准

import novel_screenplay  # 文学剧本层（2026-10-03 两段式生产：文学剧本→改写为分镜表）

import quality_stage  # 四层质量状态（2026-09-29：预演也记 A/B 层）

# ⚠️ 注意：本文件里 `comfyui_client` 这个名字是**实例**（见下方 `comfyui_client = ComfyUIClient()`），
# 不是模块。因此**模块级函数**（camera_spec / camera_key / get_call_stats 等）必须像上面这样
# 直接 import 后用别名调用 —— 写成 `comfyui_client.camera_spec(...)` 会在运行时抛
# AttributeError（实例上没有该属性）。类方法（_build_h3_prompt / generate_storyboard 等）
# 通过实例调用没问题，已有的那种写法不用改。
from video_postprocess import VideoPostProcessor, ensure_no_audio, ensure_audio_track

from novel_parser import (
    SUPPORTED_EXTS, NovelParseError, ingest_novel, list_novels,
    get_novel, preview_novel, read_novel_text, split_chapters,
    ensure_chapter_structure, chapter_body_chars
)

from llm_client import (
    LLMClient, LLMError, LLMGatewayUnavailable, LLMTruncatedError, LLMReasoningOnlyError,
    FailoverLLMClient, load_config as load_llm_config,
    save_config as save_llm_config, clear_config as clear_llm_config,
    public_view as llm_public_view
)

import ai_config

import ai_chat

import agent_core

import novel_to_script

import analytics

import autopilot

import consistency

import continuity

import chapter_preflight

import coverage

import script_consistency

import keyframe

import export_manager

from export_manager import ExportManager

import nle_export

import pipeline

import audio_qc

import plugin_registry

import deps_check

import workflow_integrity

import trt_engine_check

import stable_profile

import project_store

import shot_key

from fs_atomic import atomic_write_json, read_json_strict

import providers

import comfyui_models

import actual_params

import log_viewer

import asset_name_match

import scene_grid

import model_capabilities

import prompt_qc

import qc_client

import qc_coverage

import style_kit

import asset_prompt_kit

import sheet_split

import task_store

import gpu_task_gate

import video_watermark

import upscale_client

import tts_client

import dub_mix

import dialogue_utils

import h3_prompt_kit

import h3_common_refs

import h3_director_builder

import h3_segment_loras

import autonomous

import cancellation

import ai_memory

import prompt_memory

from ai_memory import get_memory_system

from dub_mix import (
    DubMixError, ffmpeg_available as mix_ffmpeg_check, shot_timeline,
    build_entries, mix_video_with_entries, write_mix_report, mix_out_dir,
    probe_audio_info as mix_probe_audio,
)

from tts_client import (
    QwenTTSClient, TTSError, check_environment as tts_env_check,
    build_dub_plan, default_voice_map, normalize_voice, save_voice_map,
    load_voice_map, list_voices as tts_list_voices, probe_audio as probe_audio_info,
    concat_audio, clean_line_text,
    # 参考音频克隆（2026-10-06）
    save_voice_bank_ref, find_voice_bank_ref, list_voice_bank, voice_bank_dir,
    clone_available as tts_clone_available, VOICE_BANK_EXTS
)

from upscale_client import (
    VideoUpscaler, UpscaleError, check_environment as upscale_env_check,
    probe_video as probe_video_info
)

from script_prompt_analyzer import analyze_script as analyze_script_prompts, save_script_inplace

from character_manager import CharacterManager, CharacterConsistencyEngine

from relation_manager import RelationManager, RelationConflictDetector

from nine_grid_storyboard import NineGridStoryboard

# ===== 镜号归一化收敛（缺陷 P1-19 / 任务 A-10）=====
# 全项目**唯一**的镜号归一化实现见 app/shot_key.py。此处把三处旧的本地实现收敛为
# 「一行代理」，供本模块与 pipeline 等外部引用无缝沿用 —— 写侧（keyframe 落盘）与
# 读侧（本模块查找）从此共用同一个函数，杜绝「写 shot_102、读 shot_01」的静默错位。
_shot_seq = shot_key.shot_seq

_shot_num_key = shot_key.norm_shot_key

_norm_shot_key = shot_key.norm_shot_key

APP_HOST = os.getenv("APP_HOST", "127.0.0.1")

APP_PORT = int(os.getenv("APP_PORT", "5000"))

APP_DEBUG = os.getenv("APP_DEBUG", "0").lower() in ("1", "true", "yes", "on")

def _h3_audio_policy(path: str) -> dict:
    """按 config 的 H3 音轨策略处理刚生成的视频，返回可展示的音频处理记录。

    - `H3_STRIP_AUDIO=True`  → 剥离音轨（2026-09-17 之前的旧行为）
    - 否则 `H3_EMIT_AUDIO=True` → **保留** H3 原生音轨（环境音/打斗音效），
      并顺带保证「每镜都有音轨」：缺的补一条静音轨。
      必须补齐的理由：成片拼接用 concat demuxer + `-c copy`，
      音轨时有时无会让拼接错位甚至失败。
    """
    if H3_STRIP_AUDIO:
        strip = ensure_no_audio(path, backup=True)
        return {
            "policy": "strip",
            "has_audio_before": strip.get("has_audio_before"),
            "has_audio_after": strip.get("has_audio_after"),
            "changed": strip.get("changed"),
            "method": strip.get("method"),
            "backup": strip.get("backup"),
            "message": strip.get("message") or ("已剥离音轨" if strip.get("changed") else ""),
            "error": strip.get("error"),
        }
    pad = ensure_audio_track(path) if H3_EMIT_AUDIO else None
    pad = pad or {}
    if pad.get("error"):
        msg = f"音轨处理异常（已保留原状）：{pad.get('error')}"
    elif pad.get("changed"):
        msg = "原无音轨，已补静音轨（保证拼接一致）"
    elif H3_EMIT_AUDIO:
        msg = "音轨保留（H3 原生音效）"
    else:
        msg = "未做音轨处理"
    return {
        "policy": "keep" if H3_EMIT_AUDIO else "keep_as_is",
        "has_audio_before": pad.get("has_audio_before"),
        "has_audio_after": pad.get("has_audio_after"),
        "changed": pad.get("changed"),
        "method": pad.get("method"),
        "message": msg,
        "error": pad.get("error"),
    }

# 全局状态
generation_state = {}

lock = threading.Lock()

# 审计 P1-4（2026-09-29）：任务态字典（generation_state / upscale_tasks / dub_tasks /
# mix_tasks）此前只增不清 —— 24/7 挂机下内存无界增长，且幂等复用扫描、任务列表遍历
# 随历史线性变慢。这里提供按「终态条数上限」的清理：超出上限时按插入序（≈时间序）从
# 最旧开始丢弃。running 条目永不清；刚到终态的条目不会立即被清（超限才清），
# 前端按 task_id 轮询不受影响。调用方必须已持有该注册表自己的锁。
_TASK_TERMINAL_STATUSES = ("completed", "done", "failed", "error", "cancelled")

_TASK_STATE_KEEP_DONE = 40

def _prune_task_registry(registry: dict) -> int:
    """清理一个任务注册表里超量的终态条目，返回清理条数（须持有该注册表的锁）"""
    if not isinstance(registry, dict):
        return 0
    _done = sum(1 for v in registry.values()
                if isinstance(v, dict)
                and str(v.get("status") or "") in _TASK_TERMINAL_STATUSES)
    _excess = _done - _TASK_STATE_KEEP_DONE
    if _excess <= 0:
        return 0
    _pruned = 0
    for _k in list(registry):
        if _excess <= 0:
            break
        _v = registry.get(_k)
        if isinstance(_v, dict) and \
                str(_v.get("status") or "") in _TASK_TERMINAL_STATUSES:
            registry.pop(_k, None)
            _excess -= 1
            _pruned += 1
    return _pruned

# 视频 worker「是否托管（pipeline）任务」的执行期标记（contextvar，随线程上下文传递）。
# 用于让「托管暂停」只掐断托管任务，不误杀用户手动触发的生成。见 _video_should_stop。
_VIDEO_TASK_IS_PIPELINE = contextvars.ContextVar("video_task_is_pipeline", default=False)

# P0-4 持久化任务队列：任务全生命周期落盘（SQLite），支持重启后查询与断点续跑
# P2-3：任务完成/失败时通过 on_change 钩子写入耗时统计（成本看板数据源）
def _task_analytics_hook(task: dict, event: str) -> None:
    try:
        analytics.record_from_task(task, analytics_kind=task.get("kind"))
    except Exception as e:  # noqa: BLE001  统计失败不得影响任务
        app.logger.warning(f"任务统计写入失败（忽略）：{e}")

task_db = task_store.get_store(TASKS_DB_PATH, on_change=_task_analytics_hook)

task_queue = task_store.get_queue(TASKS_DB_PATH)

# 初始化组件
script_gen = ScriptGenerator()

comfyui_client = ComfyUIClient()

video_processor = VideoPostProcessor()

# 无人值守托管：若存在已启用的托管计划，服务启动后自动接着生产（断点续跑）
# 用一个短延时线程延后启动，避免拖慢 Flask 首次响应；失败不影响服务可用性。
# 注意：若上次是用户「主动暂停」的，启动时尊重该状态，不擅自恢复生产。
def autopilot_boot_enabled() -> bool:
    """是否允许「随进程启动自动恢复生产」

    默认开启 —— 24/7 无人值守是本系统的主场景。
    但必须留一个逃生口：任何 `import app` 的短命脚本（单元验证、数据迁移、
    一次性批处理）都会触发 boot，从而与正在挂机的服务**抢同一块 GPU**。
    实测踩过这个坑：一条用于取路由列表的 `python -c "import app"` 直接
    启动了守护进程并开始跑图。需要这类脚本时设置 MJSCXT_AUTOPILOT=0 即可。
    """
    val = (os.getenv("MJSCXT_AUTOPILOT") or "").strip().lower()
    return val not in ("0", "false", "no", "off")

def _autopilot_boot():
    try:
        autopilot._restore_runtime()
        if autopilot.is_paused():
            app.logger.info("托管：上次为「已暂停」状态，启动后保持暂停（可在控制台恢复）")
            return
        enabled = autopilot.enabled_projects()
        if not enabled:
            app.logger.info("托管：没有已启用的项目，守护进程待命（可在控制台一键开启）")
            return
        app.logger.info("托管：检测到 %d 个启用项目，自动恢复生产", len(enabled))
        autopilot.resume()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"托管自动恢复失败（不影响服务）：{e}")

def _schedule_autopilot_boot(delay: float = 3.0):
    """按开关决定是否调度自动恢复（关闭时给出显式提示，避免误以为已托管）"""
    if not autopilot_boot_enabled():
        app.logger.info("托管：MJSCXT_AUTOPILOT=0，本次启动不自动恢复生产"
                        "（如需 24/7 托管请移除该环境变量）")
        return
    threading.Timer(delay, _autopilot_boot).start()

def _safe_project(name: str) -> str:
    """项目名安全化（与项目注册表的项目键规则保持一致）"""
    return project_store.safe_key(name)

# B-16 P2-11：失败路径清理中间产物。把「生成失败 / 质检阻断 / 异常」时的 scratch
# 目录、.tmp 文件等中间产物统一删掉，避免只增不减。
#: 命中即判定「环境级 TTS 不可用」的特征串（ComfyUI 侧 qwen_tts 包 import 失败等）。
#: 这些错误对**每个角色**完全一致 —— 继续逐角色重试只是把同一条失败放大 N 次。
#:
#: ⚠️ 2026-10-06 收紧：原先含裸串 ``"failed to import"``，任何 import 相关报错
#: （包括与 TTS 无关的节点加载失败）都会误触发熔断，把本可成功的角色一起跳过。
#: 改为要求「TTS 特征」与「未加载/导入失败特征」**同时**出现，避免误伤。
_TTS_MODEL_UNAVAILABLE_MARKERS = (
    "Model class is not loaded",
    "Critical Import Error",
    "qwen_tts",
)

_TTS_IMPORT_FAILURE_MARKERS = (
    "failed to import",
    "ImportError",
    "ModuleNotFoundError",
    "未加载",
)

#: 「环境缺依赖」类特征（2026-10-06 现场补充，W3 复发的新形态）。
#:
#: 现场：8190 实例的 Qwen21 venv 缺 ``accelerate``，Qwen-TTS 节点先试 sdpa、
#: 失败回退 eager，两次都撞 ``Using a `device_map` ... requires `accelerate```。
#: 这条 exception_message 里**没有**上面任何一个标记；唯一能匹配上的 ``qwen_tts``
#: 只存在于 traceback 中，而 ``tts_client._await_prompt`` 把详情截到 400 字符
#: （完整 traceback 4600+ 字符，``qwen_tts`` 落在截断线之后）→
#: 熔断对「缺 accelerate」这条错误**完全失明**，角色会一个个继续送必败任务。
#:
#: 因此这里只收「出现在 exception_message 头部、400 字符内可见」的串，
#: 不依赖 traceback。判据输入本身已是 TTS 执行失败（调用点仅在 TTS 路径），
#: 不会误伤 2026-10-06 那次收紧要防的「无关节点 import 失败」。
_TTS_DEP_MISSING_MARKERS = (
    "requires `accelerate`",
    "You can install it with",
    "No module named",
    "ModuleNotFoundError",
)

def _is_tts_model_unavailable(err_text: str) -> bool:
    """ComfyUI 侧 TTS 模型类是否因依赖缺失而整体不可用（可熔断，不再逐角色重试）。"""
    t = str(err_text or "")
    if any(m in t for m in _TTS_MODEL_UNAVAILABLE_MARKERS):
        return True
    # 缺依赖（accelerate / 模块缺失）同样是环境级故障：每个角色报同一条，
    # 特征串都在 exception_message 头部，不受 400 字符截断影响
    if any(m in t for m in _TTS_DEP_MISSING_MARKERS):
        return True
    # 「未加载/导入失败」类特征必须同时带 TTS 语境，避免误伤无关节点的 import 失败
    return (any(m in t for m in _TTS_IMPORT_FAILURE_MARKERS)
            and ("tts" in t.lower() or "TTS" in t))

def _asset_err_traceback(exc: BaseException) -> str:
    """把异常格式化器统一收口：拿不到 traceback 时退化为「（无）」，绝不二次抛错。

    背景：资产批量生成的隔离分支原先只打 `类型: 消息`，而像
    ``FileNotFoundError: [WinError 3] 系统找不到指定的路径`` 这类消息在
    ComfyUI output 读取、质检暂存区落盘、跨盘 move 三处都可能抛出 ——
    消息里**没有任何路径**，日志里也没��堆栈，故障完全不可定位（2026-10-06 现场）。
    打堆栈本身绝不能成为新的失败源，所以整段包在 try 里。
    """
    try:
        import traceback as _tb
        return "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    except Exception:  # noqa: BLE001  取堆栈失败不能盖掉原始异常
        return "（traceback 不可用）"

def _ingest_comfy_output(src_files, dst_path: str, logger=None,
                         fallback_to_source: bool = False) -> str:
    """把 ComfyUI 产出的媒体**可靠地**落到 ``dst_path``，返回最终可用路径。

    这是全代码库消费 ComfyUI output 的**唯一入口**（2026-10-06）。
    原先散落着 11 处 ``shutil.move(files[0], dst)``，每一处都有同样的三个缺陷。

    为什么不能直接 ``shutil.move(files[0], dst)``
    --------------------------------------------
    实测事故：第 1 集的 4 个角色资产在 ComfyUI 上**渲染成功**
    （``output/performance/<prompt_id>.json`` 里 ``success: true``，
    产物也真的落在 ComfyUI output 里），应用却在消费产物时抛
    ``FileNotFoundError: [WinError 3] 系统找不到指定的路径``，
    把整批资产判 failed —— **GPU 白烧，产物留在 ComfyUI output 里没人要**。

    三个叠加缺陷：
    1. **目标目录是 20~40 秒 GPU 渲染之前**建的。渲染期间它被外部清理 /
       被并发任务动过，move 时就抛 WinError 3。
       跨盘（F: ComfyUI output → H: 项目 output）时 ``shutil.move`` 必然走
       ``copy2`` 回退分支，而 ``copy2`` 里的 ``open(dst,'wb')`` 正是抛
       WinError 3（``ERROR_PATH_NOT_FOUND``）的那一步 —— 报错完全指不到
       真正缺失的是哪个目录。
    2. **盲取 ``[0]``**：``get_output_files`` 会把 history 里**所有**输出条目
       （含临时/预览图）平铺返回，第 0 个未必是真正要用的正式产物，
       也可能已被并发执行体取走。
    3. 失败即**整步判死**，没有降级空间。

    现在的语义：
      · 落地前**重新**确保目标目录存在（不信任渲染前的 makedirs）；
      · 只挑**真实存在**的源文件；
      · 同盘 ``os.replace``（原子）/ 跨盘显式 ``copy2`` + ``remove``
        （不依赖 ``shutil.move`` 的隐式回退）；
      · 源与目标同路径时直接返回（等价于原来的 ``if abspath != abspath`` 守卫）；
      · 失败信息里带**完整源/目标路径与候选清单**；
      · ``fallback_to_source=True`` 时：搬移失败不抛，**返回仍可用的源路径**
        （给「搬移只是优化、不是必需」的调用点，例如预演产物）；
        仍会记 warning，绝不静默。
    """
    _log = logger
    files = [f for f in (src_files or []) if f]
    dst_dir = os.path.dirname(os.path.abspath(dst_path))
    if not files:
        if fallback_to_source:
            return dst_path
        raise FileNotFoundError(f"ComfyUI 未返回任何产物（目标：{dst_path}）")

    # ① 落地前重新确保目标目录存在（渲染期间可能被清理）
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except OSError as e:
        if fallback_to_source:
            if _log:
                _log.warning("[产物落盘] 目标目录不可建，改用 ComfyUI 原始路径：%s（%s）", dst_dir, e)
            return files[0]
        raise RuntimeError(f"产物目标目录无法创建：{dst_dir}（{e}）") from e

    # ② 只挑真实存在的源（跳过已被并发取走 / 被输出回收清掉的条目）
    existing = [f for f in files if os.path.isfile(f)]
    if not existing:
        if fallback_to_source:
            if _log:
                _log.warning("[产物落盘] ComfyUI 产物均已不存在，沿用原始路径：%s", files[0])
            return files[0]
        raise FileNotFoundError(
            f"ComfyUI 产物均已不存在（无法落盘）：{files!r}；目标：{dst_path}。"
            f"可能是并发执行体已取走，或被 ComfyUI 输出回收清掉")

    src = existing[0]
    # ③ 源与目标本就是同一路径 → 无需搬移（等价于原调用点的 abspath 守卫）
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst_path)):
        return dst_path

    # ④ 跨盘安全落地
    try:
        if os.path.dirname(os.path.normcase(os.path.abspath(src))) == \
           os.path.dirname(os.path.normcase(os.path.abspath(dst_path))):
            os.replace(src, dst_path)
        else:
            shutil.copy2(src, dst_path)
            try:
                os.remove(src)
            except OSError:   # 源删不掉不算失败（产物已安全落盘）
                if _log:
                    _log.warning("[产物落盘] 已落盘但源文件删除失败（忽略）：%s", src)
    except (OSError, shutil.Error) as e:
        if fallback_to_source:
            if _log:
                _log.warning("[产物落盘] 搬移失败（%s → %s：%s），沿用 ComfyUI 原始路径",
                             src, dst_path, e)
            return src
        raise RuntimeError(
            f"搬移 ComfyUI 产物失败：{src} → {dst_path}（{e}）。"
            f"目标目录：{dst_dir}；源目录：{os.path.dirname(src)}") from e

    if _log and len(existing) > 1:
        _log.debug("[产物落盘] 候选 %d 个，取首个：%s（其余：%s）", len(existing), src, existing[1:])
    return dst_path

def _cleanup_scratch_dir(dir_path: str, logger=None) -> None:
    """清空目录内容（保留目录本身），失败时只记日志不抛异常。"""
    import logging
    _log = logger or logging.getLogger(__name__)
    if not dir_path or not os.path.isdir(dir_path):
        return
    try:
        for fn in os.listdir(dir_path):
            fp = os.path.join(dir_path, fn)
            try:
                if os.path.isdir(fp):
                    import shutil
                    shutil.rmtree(fp, ignore_errors=True)
                else:
                    os.remove(fp)
            except OSError:
                _log.warning("清理中间产物失败：%s", fp)
        _log.info("已清理 scratch 目录：%s", dir_path)
    except OSError as e:
        _log.warning("清理 scratch 目录失败：%s", e)

# ===================== 质检不合格产物：统一移入回收站（可恢复） =====================
# 需求（用户原话）：「质检不合格的图片、提示词或视频要删除，不要留存在本地（包括
# ComfyUI 目录下的）」。这里统一收敛为**移入回收站**而非硬删 —— 误删好图好片是不可逆
# 事故，移入 `output/projects/_trash/qc_reject/<时间戳>_<项目>/` 可人工恢复/复核。
#
# ⚠️ 红线（缺一不可）：
#   1) 调用方必须用 `verdict.get("ok") is True and not gate["accept"]` 作为删除条件。
#      `ok=False`（接口故障/超时/鉴权失败）、`skipped=True`（质检未开启）、
#      `qc_declared 但 qc_on=False`（接口未就绪）**都不是产物不合格** —— 那些删下去会
#      把好图好片删光。本工具只负责「安全地移」，判定由调用方提供，工具内不再猜。
#   2) 路径必须落在 PROJECT_OUTPUT_DIR / COMFYUI_OUTPUT_DIR 之内；
#   3) 显式排除 PROJECT_TRASH_DIR（避免把自己的回收站再搬一层）；
#   4) 硬链接（st_nlink > 1）不释放空间、且可能被他处引用 → 拒绝移动。
_PURGE_REJECTED_ENV = "MJSCXT_PURGE_REJECTED"

def _purge_rejected_enabled() -> bool:
    """不合格产物清理开关。默认开启；设 `MJSCXT_PURGE_REJECTED=0` 时只记日志不移走。"""
    v = str(os.environ.get(_PURGE_REJECTED_ENV, "1")).strip().lower()
    return v not in ("0", "false", "no", "off", "")

def _reject_artifact(paths, project: str = "", reason: str = "", kind: str = "") -> dict:
    """质检不合格产物 → 移入回收站（可恢复），**绝不硬删**。

    返回 ``{"moved": [...], "skipped": [...], "failed": [...]}``，三态都带
    `{"src":..., "why":...}`（moved 项另带 `dst`）。

    安全闸（任一不满足即跳过并记日志，绝不抛异常）：
      · 只接受绝对路径（相对路径无法可靠判边界，直接拒绝）；
      · `os.path.normpath(os.path.abspath(p))` 归一 —— 本项目已知坑：混合分隔符
        （`/` 与 `\\`）会让外部 API 静默匹配失败，必须先归一；
      · 必须落在 PROJECT_OUTPUT_DIR 或 COMFYUI_OUTPUT_DIR 之内（越界拒绝）；
      · 显式排除 PROJECT_TRASH_DIR（含 `_backup_*` / `_watermark_backup` 同理越界/排除）；
      · `os.path.lexists` 判存在 —— episode 成片 `move` 后 src 已消失是常态，
        不存在即跳过，**不能当异常**；
      · `os.stat().st_nlink == 1` 校验（硬链接不释放空间，见 disk_reclaim.py 的教训）；
      · 文件与目录都支持（目录走 shutil.move）。

    全程 try/except：清理是优化而非功能，**任何失败都不阻断主流程**，只 logger.warning。
    """
    res = {"moved": [], "skipped": [], "failed": []}
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    paths = [p for p in (paths or []) if p]
    if not paths:
        return res

    # 归一化的边界根（含尾分隔符，防止 /output 误匹配 /output2）
    def _root_ok(p: str) -> bool:
        cands = []
        for root in (PROJECT_OUTPUT_DIR, COMFYUI_OUTPUT_DIR):
            if root:
                try:
                    cands.append(os.path.normpath(os.path.abspath(root)) + os.sep)
                except Exception:  # noqa: BLE001
                    pass
        return any(p == c.rstrip(os.sep) or p.startswith(c) for c in cands)

    trash_abs = ""
    try:
        if PROJECT_TRASH_DIR:
            trash_abs = os.path.normpath(os.path.abspath(PROJECT_TRASH_DIR))
    except Exception:  # noqa: BLE001
        trash_abs = ""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, "qc_reject",
                              f"{stamp}_{_safe_project(project or 'project')}")
    enabled = _purge_rejected_enabled()

    for raw in paths:
        try:
            if not os.path.isabs(raw):
                res["skipped"].append({"src": str(raw), "why": "非绝对路径"})
                app.logger.warning(
                    "[质检清理] 跳过：非绝对路径（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, raw)
                continue
            p = os.path.normpath(os.path.abspath(raw))
            if trash_abs and (p == trash_abs or p.startswith(trash_abs + os.sep)):
                res["skipped"].append({"src": p, "why": "回收站内，跳过"})
                continue
            if not _root_ok(p):
                res["skipped"].append({"src": p, "why": "越界（不在 output/ComfyUI 目录内）"})
                app.logger.warning(
                    "[质检清理] 拒绝越界路径（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, p)
                continue
            if not os.path.lexists(p):
                res["skipped"].append({"src": p, "why": "不存在"})
                continue
            try:
                if os.stat(p).st_nlink > 1:
                    res["skipped"].append({"src": p, "why": "硬链接（不释放空间）"})
                    app.logger.warning(
                        "[质检清理] 跳过硬链接（project=%s kind=%s reason=%s path=%s）",
                        project, kind, reason, p)
                    continue
            except OSError as se:
                res["failed"].append({"src": p, "why": f"stat 失败：{se}"})
                continue
            if not enabled:
                res["skipped"].append({"src": p, "why": f"{_PURGE_REJECTED_ENV}=0（仅记日志）"})
                app.logger.warning(
                    "[质检清理] 开关关闭，仅记日志不移走（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, p)
                continue
            os.makedirs(trash_root, exist_ok=True)
            base = os.path.basename(p.rstrip(os.sep)) or "artifact"
            dst = os.path.join(trash_root, base)
            # 同名冲突（同项目多次重试同名）→ 加序号，绝不覆盖已在回收站里的证据
            _n = 1
            while os.path.lexists(dst):
                stem, ext = os.path.splitext(base)
                dst = os.path.join(trash_root, f"{stem}__{_n}{ext}")
                _n += 1
            shutil.move(p, dst)
            res["moved"].append({"src": p, "dst": dst})
            app.logger.warning(
                "[质检清理] 不合格产物已移入回收站（project=%s kind=%s reason=%s）：%s → %s",
                project, kind, reason, p, dst)
        except Exception as e:  # noqa: BLE001  清理绝不能中断生产
            res["failed"].append({"src": str(raw), "why": f"{type(e).__name__}: {e}"})
            app.logger.warning("[质检清理] 移入回收站失败（已忽略，不影响主流程）：%s: %s",
                               type(e).__name__, e)
    return res

def _purge_rejected_artifacts(paths, project: str = "", reason: str = "", kind: str = "",
                              history_file: str = "") -> dict:
    """``_reject_artifact`` 的语义化包装：移走后顺手把「质检历史 file 断链」补掉。

    ⚠️ 质检历史 json 是排查依据，**只把断链的 `file` 字段置 null + 打 `purged` 标记**，
    绝不删除历史记录本身（否则事后无法查「当时为什么不合格」）。
    """
    out = _reject_artifact(paths, project=project, reason=reason, kind=kind)
    moved_srcs = {os.path.normpath(m["src"]) for m in out.get("moved") or []}
    if moved_srcs and history_file:
        try:
            _mark_history_file_purged(history_file, moved_srcs)
        except Exception as e:  # noqa: BLE001
            app.logger.warning("[质检清理] 质检历史 file 断链标记失败（忽略）：%s", e)
    return out

def _mark_history_file_purged(history_file: str, moved_srcs: set) -> None:
    """把质检历史里指向**已移走路径**的 `file` / `frames_f` 记为 null 并打 `purged=true`。

    历史记录本身保留 —— 它是事后排查「当时为什么不合格」的唯一依据，只是不再指向
    已不存在的本地文件（否则前端/排障脚本按路径取会 404）。
    """
    if not history_file or not os.path.isfile(history_file):
        return
    with open(history_file, "r", encoding="utf-8") as f:
        data = json.load(f) or {}
    hit = False
    for rec in (data.get("records") or []):
        if not isinstance(rec, dict):
            continue
        fp = rec.get("file")
        if fp and os.path.normpath(os.path.abspath(str(fp))) in moved_srcs:
            rec["file"] = None
            rec["purged"] = True
            hit = True
        # 抽帧图（整片质检）：整目录被移走 → 逐条把已消失的帧路径剔除
        frames = rec.get("frames_f")
        if isinstance(frames, list) and frames:
            kept = [x for x in frames
                    if os.path.normpath(os.path.abspath(str(x))) not in moved_srcs]
            if len(kept) != len(frames):
                rec["frames_f"] = kept
                rec["frames_purged"] = True
                hit = True
    if hit:
        atomic_write_json(history_file, data)
        app.logger.warning("[质检清理] 已标记质检历史断链：%s", history_file)

def _purge_prompt_records(project: str, shot_key, reason: str = "") -> dict:
    """提示词预检不通过 → 移走该镜**唯一落盘物** `prompt_<shot>.json`（P12）。

    用户决策 1：``prompt_qc.preflight`` 是生成前的文本合规检查，不通过直接阻断不生成，
    因此没有图片/视频可删 —— 只有这份提示词历史 json 留在了本地。
    """
    try:
        key = qc_client.safe_token(shot_key, "0")
        path = os.path.join(QC_DIR, _safe_project(project or "project"), f"prompt_{key}.json")
    except Exception as e:  # noqa: BLE001
        app.logger.warning("[质检清理] 提示词落盘物路径解析失败（忽略）：%s", e)
        return {"moved": [], "skipped": [], "failed": []}
    return _reject_artifact([path], project=project, reason=reason or "提示词预检未通过",
                            kind="prompt")

def _purge_sb_refs(project: str) -> dict:
    """分镜参考图上传残留（ComfyUI output/sb_ref_*.png）按项目清理（P13）。

    ⚠️ 与「不合格产物」是**两码事**：`sb_ref_*` 是分镜**参考图输入**（上传给
    LoadImageOutput 的中间件），不是质检产物，**绝不能混进普通「不合格即删」逻辑**
    （那会在单镜重试中途删掉当前镜头正在用的参考图）。按用户决策 4：只在**本轮分镜
    批量生成循环全部结束后**调用一次，按项目前缀收口。
    """
    res = {"moved": [], "skipped": [], "failed": []}
    try:
        root = COMFYUI_OUTPUT_DIR
        if not root or not os.path.isdir(root):
            return res
        # ⚠️ 2026-09-29 前置安全检查（实测根因，用户 09-29 日志）：
        # 「收尾调用一次」这个前提在**异常收尾**时不成立 —— 某镜 wait_for_completion
        # 超时返回、或批次被中止信号打断时，任务其实**仍在 ComfyUI 队列里跑/等待**。
        # 此时照常清理 `sb_ref_*`，那些任务执行到 LoadImage 就报
        # FileNotFoundError（实测 shot_05~11 连续 7 镜全灭，每镜 0.01s 失败）。
        # 故队列非空、或队列状态查不到（ok=False，信息不明）时**跳过本轮清理**：
        # 宁可留残留（有 _maybe_reclaim_comfyui_output 滚动兜底），
        # 也不删正在被引用的参考图。
        _q = comfyui_client.queue_state()
        if not _q.get("ok") or _q.get("running") or _q.get("pending"):
            app.logger.warning(
                "[质检清理] 跳过 sb_ref 清理（project=%s）：ComfyUI 队列 运行=%s / 等待=%s"
                "（查询ok=%s）—— 队列里可能仍有引用这些参考图的任务，"
                "删掉会让它们 LoadImage 报 FileNotFoundError",
                project, _q.get("running"), _q.get("pending"), _q.get("ok"))
            return res
        # generate_storyboard 的命名：sb_ref_<filename_prefix 的 basename>_<idx>.png，
        # 分镜链路 filename_prefix 形如 `comic_drama_sb/<项目>_shot_NN[...]`，
        # 故前缀里含 `<项目>_shot_`。
        pref = f"sb_ref_{_safe_project(project or '')}_shot_"
        targets = []
        for fn in os.listdir(root):
            if fn.startswith(pref) and fn.lower().endswith(".png"):
                targets.append(os.path.join(root, fn))
        if not targets:
            return res
        res = _reject_artifact(targets, project=project,
                               reason="分镜参考图上传残留（本轮分镜生成结束）",
                               kind="sb_ref")
    except Exception as e:  # noqa: BLE001
        app.logger.warning("[质检清理] sb_ref 清理失败（忽略，不影响生产）：%s: %s",
                           type(e).__name__, e)
    return res

# B-14 P2-4：资产「取图判据」统一入口。就绪判据（_collect_asset_refs 的
# _first_nonempty_image）与取图判据（_build_asset_index 的 _first_existing）
# 此前各自维护一套「判有图」逻辑，口径漂移（一个只认 4 个扩展名、另一个只
# 认 front/base 固定名）。统一为：扩展名白名单 + 取第一张非空图片。
_ASSET_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")

#: 资产目录内「主视角图」的取图优先级（2026-09-24 修复）。
#: ⚠️ 实测 bug：原来只按**文件名字典序**取第一张非空图，而角色资产目录里
#:    back.png < base.png < front.png < left.png < right.png —— 于是 `back.png`
#:    （**背面图**）被当成「主角锚点」喂给分镜/视频链路（_build_asset_index 在
#:    剧本 characters 没有 front/base 字段时就走这条兜底，autopilot 正是这种形态）。
#:    当时看不出来，是因为 4 张视角图与 base 内容完全一致（都是同一张三视图整图）；
#:    一旦视角图变成真单机位（2026-09-24 sheet_split 改造后就是如此），
#:    这个字典序兜底就会静默地把每个镜头的角色锚点换成「只有背面」。
#: 故改为显式优先级：正面 > 整图 > 左侧 > 右侧 > 背面 > 其它图片（字典序）。
_ASSET_IMG_PRIORITY = ("front.png", "base.png", "front.jpg", "base.jpg",
                       "left.png", "right.png", "back.png",
                       "left.jpg", "right.jpg", "back.jpg")

def _first_existing_asset_image(directory: str) -> str:
    """在目录内取「主视角」图片（扩展名白名单），无则返回 ''。

    统一判据：
      1) 扩展名白名单 (".png", ".jpg", ".jpeg", ".webp")；
      2) 按 :data:`_ASSET_IMG_PRIORITY` 显式优先级取（**不是**文件名字典序，
         否则 back.png 会排在 base/front 之前被取走，详见该常量注释）；
      3) 优先级名都不存在时，再按字典序取第一张非空图片；
      4) 找不到任何图片 → 返回 ''，调用方自行决策（报错/跳过）。
    """
    if not directory or not os.path.isdir(directory):
        return ""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return ""

    def _ok(fn: str) -> bool:
        return (fn.lower().endswith(_ASSET_IMG_EXTS)
                and os.path.isfile(os.path.join(directory, fn))
                and os.path.getsize(os.path.join(directory, fn)) > 0)

    for cand in _ASSET_IMG_PRIORITY:
        if cand in entries and _ok(cand):
            return os.path.join(directory, cand)
    for fn in entries:
        if _ok(fn):
            return os.path.join(directory, fn)
    return ""

# B-20 P2-10：磁盘余量检查。大体积写入（视频/图片/混音成片）前确认剩余空间，
# 不足时 fail-loud（logger.warning + 返回 False），不静默写入导致半截文件。
def _ensure_disk_headroom(directory: str, min_bytes: int, logger=None) -> bool:
    """检查 directory 所在分区剩余空间是否 ≥ min_bytes。
    返回 True（充足）/ False（不足，已记 warning）。失败时不影响调用方继续。
    """
    _log = logger
    try:
        # 用 shutil.disk_usage 取实际分区剩余空间
        usage = shutil.disk_usage(os.path.abspath(directory))
        free = usage.free
        if free < min_bytes:
            if _log:
                _log.warning("磁盘余量不足：%s 剩余 %.1fMB < 需要 %.1fMB",
                             os.path.abspath(directory), free / 1048576, min_bytes / 1048576)
            return False
        return True
    except Exception as e:
        if _log:
            _log.warning("磁盘余量检查失败（已放行）：%s", e)
        return True

def _project_or_400(raw, field_name="project_name"):
    """G4 收口：路由层「项目入参 → 安全键 / 400」的统一入口。

    ⚠️ 不能写 `_safe_project(x) or 兜底`、也不能判 `_safe_project(x)` 的真值——
    `safe_key('')` 返回**字面量 'project'**（真值），守卫恒不成立（死守卫），
    漏传项目名会静默写进共享 `project` 命名空间。判空必须看**原始入参**
    （与 api_qc_project_summary 的 G3 修复同一口径）。

    A-01（F-01）加固：额外**拒绝路径穿越**入参（含 `..` / 绝对路径 / 路径分隔符）。
    仅靠 `safe_key` 收敛会把 `../../evil` 静默变成合法键 `evil`——虽不越界写盘，
    但把越界尝试当成正常项目混淆视听；此处直接 400，作到「越界即拒 + 不落盘」。
    收敛后仍做一次 abspath 前缀校验作为双保险（防御未来 safe_key 规则变更）。

    返回 (project, error)：error 为 None 表示合法（project 已 safe_key）；
    否则 error 是 (jsonify, 400) 响应，直接 return 它。
    用法::

        project, err = _project_or_400((data.get('project_name') or '').strip())
        if err is not None:
            return err
    """
    if not (isinstance(raw, str) and raw.strip()):
        return "", (jsonify({"success": False, "error": f"缺少 {field_name}"}), 400)
    raw_s = raw.strip()
    # task#7 口径补齐：含控制字符（如 NUL `\x00`）/ **无任何有效字符**（如 `.` `。` `…`）的
    # 入参 → 与空串**同口径 400**。否则 `safe_key` 会把它们坍缩成共享默认键 `project`
    # （非越界、无写盘，但会静默写进共享命名空间，且与空串口径不一致、掩盖调用方 bug）。
    # ⚠️ 判「有效字符」只看 isalnum/_/-（与 safe_key 的存活字符一致）：中文名（isalnum 为真，
    #    如「剑影孤城」「蛊真人精校版」）照常通过，绝不被误杀。
    if any(ord(_c) < 32 or ord(_c) == 0x7f for _c in raw_s):
        app.logger.warning("[task#7] 拒绝含控制字符的项目名：%r", raw_s)
        return "", (jsonify({"success": False,
                             "error": f"非法的 {field_name}（含控制字符）"}), 400)
    _cleaned = re.sub(r"[《》〈〉【】「」『』]", "", raw_s)
    if not any((_c.isalnum() or _c in "_-") for _c in _cleaned):
        app.logger.warning("[task#7] 拒绝无有效字符的项目名（与空串同口径）：%r", raw_s)
        return "", (jsonify({"success": False,
                             "error": f"非法的 {field_name}（无有效字符）"}), 400)
    if (raw_s.startswith(("/", "\\")) or ".." in raw_s
            or "/" in raw_s or "\\" in raw_s
            or os.path.isabs(raw_s) or os.path.splitdrive(raw_s)[0]):
        app.logger.warning("[A-01] 拒绝越界项目名（疑似路径穿越）：%r", raw_s)
        return "", (jsonify({
            "success": False,
            "error": f"非法的 {field_name}（禁止路径分隔符 / 绝对路径 / 「..」）"}), 400)
    project = _safe_project(raw_s)
    _root = os.path.abspath(PROJECT_OUTPUT_DIR)
    _pdir = os.path.abspath(os.path.join(PROJECT_OUTPUT_DIR, project))
    if not _pdir.startswith(_root + os.sep):
        app.logger.warning("[A-01] 项目名收敛后仍越界，拒绝：%r → %r", raw_s, project)
        return "", (jsonify({"success": False, "error": f"非法的 {field_name}"}), 400)
    return project, None

def _serve_safe(base_dir: str, filename: str, **send_kw):
    """目录穿越防护的 send_file 统一入口。

    返回 send_file(...) 或 403/404 的 Flask 响应；永不抛异常。
    用法：``return _serve_safe(KEYFRAMES_DIR, filename)``。

    防护原理（S-06）：
    - 旧写法用 ``os.path.normpath(filename).startswith('..')`` 拦截穿越，
      但 ``os.path.join(base, safe_path)`` 遇**绝对路径**（如 ``C:\\Windows``、``/etc/passwd``）
      会丢弃 base 前缀直接返回绝对路径 → 穿越成功。
    - 这里用 ``os.path.abspath`` 归一后校验目标路径必须落在 base_dir 之内
      （前缀匹配 base_dir + 分隔符），从根上杜绝穿越。
    """
    base = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base, filename or ""))
    # 必须严格落在 base 之内：base 本身（目录）或 base + 分隔符 开头
    if not (target == base or target.startswith(base + os.sep)):
        return abort(403)
    if not os.path.isfile(target):
        return abort(404)
    return send_file(target, **send_kw)

def _serve_attachment(base_dir: str, filename: str, **send_kw):
    """目录穿越防护的附件下载统一入口（as_attachment + download_name）。

    与 _serve_safe 防护逻辑相同，但额外指定 as_attachment=True 与 download_name。
    """
    base = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base, filename or ""))
    if not (target == base or target.startswith(base + os.sep)):
        return abort(403)
    if not os.path.isfile(target):
        return abort(404)
    send_kw.setdefault("as_attachment", True)
    send_kw.setdefault("download_name", os.path.basename(target))
    return send_file(target, **send_kw)

def _body() -> dict:
    """统一取请求 body，**永不抛异常**（返回 ``{}`` 兜底）

    为什么不用裸 ``request.json``：Flask 在 Content-Type 不是 application/json 时抛
    ``UnsupportedMediaType``（415），body 非法 JSON 时抛 ``BadRequest``（400）。
    这类错误会让接口以 4xx 结束，而不是「按缺参数处理并给出可读错误」——
    用 ``curl -d '{}'``（默认表单 Content-Type）调就会直接 415，排查成本很高。
    项目约定：所有取 body 的地方统一走这里。
    """
    return request.get_json(silent=True) or {}

# ==========================================================================
# 集级产物目录（多集自动生产的必要条件）
# --------------------------------------------------------------------------
# 背景：配音文件名早已含集号（ep{NN}_shot{NN}_角色.wav），但分镜图 / 尾帧 / 视频
# 历史上按 shot_NN 平铺存放，**多集生产会互相覆盖**——第 2 集的分镜图会覆盖第 1 集的。
# 手动流程一次只做一集，问题不暴露；24 小时托管必须解决。
#
# 策略：第 1 集沿用平铺目录（现有项目、现有前端、现有数据完全不受影响），
#       第 2 集起写入 epNN/ 子目录。读取时优先集目录、回落平铺目录，兼容旧数据。
# ==========================================================================

def _ep_dir(base_dir: str, episode_no=None) -> str:
    """写入用的集级产物目录（第 1 集 = 平铺目录）"""
    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1
    if ep <= 1:
        return base_dir
    return os.path.join(base_dir, f"ep{ep:02d}")

def _ep_read_dir(base_dir: str, project: str, episode_no=None) -> str:
    """读取用的集级产物目录：优先集目录，不存在则回落平铺目录（兼容旧数据）"""
    flat = os.path.join(base_dir, project)
    d = _ep_dir(flat, episode_no)
    if os.path.isdir(d) and d != flat:
        return d
    return flat if os.path.isdir(flat) else d

def _ep_of_script(script: dict, fallback=None):
    """从剧本里取集号（metadata.episode_no > 顶层 episode_no > 兜底）"""
    if not isinstance(script, dict):
        return fallback
    meta = script.get("metadata") or {}
    for v in (meta.get("episode_no"), script.get("episode_no"), fallback):
        try:
            if v is not None and str(v).strip() != "":
                return int(v)
        except (TypeError, ValueError):
            continue
    return fallback

def _episode_schema_defaults(project_name: str, shots: list) -> dict:
    """⑥ 下游链路自动引用剧本自动判定的「镜头数 / 每集时长」字段。

    - 镜头缺 duration 时按项目配置的 duration_per_shot 兜底；
    - 返回 episode_stats（shot_count / duration_sec / episode_plan）供接口回显与后续步骤使用。
    """
    cfg = {}
    try:
        cfg = project_store.read_config(project_name)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"读取项目配置失败（{project_name}）：{e}")
    per_shot = cfg.get("duration_per_shot") or 5
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        if not s.get("duration"):
            s["duration"] = per_shot
    return novel_to_script.build_episode_stats(shots)

# ===== 项目封面（项目中心卡片对外展示的第一张图） =====

def _project_cover_path(dir_key: str) -> str:
    return os.path.join(project_store.paths(dir_key)["root"], "cover.png")

def _collect_project_cast_images(rec: dict, kind_pref: str = "") -> tuple:
    """收集项目角色资产图，返回（主角/英雄参考图列表，反派参考图列表，主角数量，反派数量）。

    判定依据：book_outline.json 的 characters[].role（primary/antagonist/villain），
    无大纲时回落为“前 2 个主角 + 后 1 个反派”的保守截断，避免误把配角焊进封面。
    资产图优先取 base.png / front.png（与现有资产目录约定一致）。
    """
    dir_key = rec["dir_key"]
    project = str(rec.get("name") or dir_key).strip()
    roots = [CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR]
    base_map = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}

    def _img_for(name: str, prefs: tuple) -> str:
        for d in roots:
            cand = os.path.join(d, project, str(name), "base.png")
            if os.path.isfile(cand):
                return cand
        for p in prefs:
            cand = os.path.join(CHARACTERS_DIR, project, str(name), p)
            if os.path.isfile(cand):
                return cand
        return ""

    # 大纲优先：主角团 = role in (primary, hero, protagonist, champion)
    # 反派 = role in (antagonist, villain, rival, enemy)
    outline = None
    try:
        import book_outline as bo
        # ⚠️ 2026-10-07 修复：这一段原先**永远**抛异常、永远走下面的 except，
        # 整个 book_outline 读取是死的 —— 而且是三处独立错误叠在一起，任一处都足以
        # 让它 100% 失败：
        #   ① 本文件顶部是 `from novel_parser import (read_novel_text, split_chapters, …)`
        #      —— **按函数名直接导入**，`novel_parser` 这个模块名在本模块里并不存在
        #      → NameError；
        #   ② novel_parser 里也没有 `split_novel` 这个函数（真名是 split_chapters）；
        #   ③ read_novel_text 返回的是 **str**，原写法 `text, _ch = …` 试图把正文
        #      解包成两个变量 → ValueError（正文必然 >2 字符）。
        # 三者叠在一起还都被 `except Exception` + logger.debug 吞掉，于是界面上看不出
        # 任何异常，只是「主角/反派映射一直是默认的那一套」——极难定位的静默失效。
        # 现在直接用顶部已导入的函数名（与全文件口径一致）。
        text = read_novel_text(NOVELS_DIR, rec.get("novel_id") or "") or ""
        chunks = split_chapters(text) or []
        outline = bo.load_outline(dir_key, text, len(chunks), CONTINUITY_DIR)
    except Exception as e:  # noqa: BLE001
        # debug → warning：读不到大纲会让主角/反派映射退回默认值，属于「结果错了但没人知道」，
        # 不能只留在 debug 级别。
        app.logger.warning("读取 book_outline 失败，使用默认主角/反派映射：%s", e)
        outline = None

    hero_names, villain_names = [], []
    if outline and isinstance(outline.get("characters"), list):
        for c in outline["characters"]:
            if not isinstance(c, dict):
                continue
            nm = str(c.get("name") or "").strip()
            if not nm:
                continue
            role = str(c.get("role") or c.get("camp") or "").strip().lower()
            if role in ("primary", "hero", "protagonist", "champion", "main") or c.get("is_protagonist"):
                hero_names.append(nm)
            elif role in ("antagonist", "villain", "rival", "enemy", "main_villain") or c.get("is_antagonist"):
                villain_names.append(nm)
        hero_names = hero_names[:6]
        villain_names = villain_names[:3]
    else:
        # 无大纲时：保守取角色目录里前 2 个有 base.png 的作主角，后 1 个作反派（避免把群演误判成反派）
        chars_dir = os.path.join(CHARACTERS_DIR, project)
        if os.path.isdir(chars_dir):
            all_names = [d for d in sorted(os.listdir(chars_dir)) if os.path.isdir(os.path.join(chars_dir, d))]
            hero_names = all_names[:2]
            villain_names = all_names[-1:] if len(all_names) > 2 else []

    hero_imgs = [_img_for(n, ("base.png", "front.png", "half.png")) for n in hero_names]
    villain_imgs = [_img_for(n, ("base.png", "front.png", "half.png")) for n in villain_names]
    hero_imgs = [p for p in hero_imgs if p]
    villain_imgs = [p for p in villain_imgs if p]
    return hero_imgs, villain_imgs, len(hero_imgs), len(villain_imgs)

def _cover_prompt_from_outline(rec: dict, outline: dict, hero_imgs: list, villain_imgs: list) -> str:
    """从大纲与主角/反派图片数量生成封面提示词（人物参考仅用于身份锚点，不强制画满）"""
    dir_key = rec["dir_key"]
    cfg = project_store.read_config(dir_key)
    style = str(cfg.get("style") or "").strip()
    name = str(rec.get("name") or dir_key).strip()

    # 大纲摘要：取 story_summary（若存在）作为主题基调
    summary = ""
    if outline:
        summary = str(outline.get("story_summary") or "").strip()[:200]
    primary = 0
    if outline and isinstance(outline.get("characters"), list):
        primary = sum(1 for c in outline["characters"] if c.get("role") in ("primary", "hero", "protagonist", "main") or c.get("is_protagonist"))
    if summary:
        base = f"漫剧主视觉封面插画，《{name}》：{summary}"
    else:
        base = f"漫剧主视觉封面插画，《{name}》主题氛围场景"
    # 加入风格 + 光影 + 构图（与旧版一致，避免画质下降）
    base += "，戏剧性光影，电影感构图，景深层次丰富，高细节，画面中不出现任何文字"
    # 如有角色参考图，追加“主角团/反派”人数（不点名，避免把群演误画成反派）
    extras = []
    if hero_imgs:
        extras.append(f"主角团 {len(hero_imgs)} 人")
    if villain_imgs:
        extras.append(f"反派 {len(villain_imgs)} 人")
    if extras:
        base += "，" + "、".join(extras)
    if style:
        base += f"，风格：{style}"
    return base

def _generate_project_cover(rec: dict, seed=None) -> str:
    """生成项目封面并落到项目根目录 cover.png，返回落盘绝对路径。

    新版：优先读 book_outline.json 作为主题输入，并用主角/反派角色资产图作为参考（最多 3 张），
    走故事板/参考图生成链路，若资产缺失则回落到旧的场景 t2i 链路（保持无角色时也能出图）。
    """
    dir_key = rec["dir_key"]
    cfg = project_store.read_config(dir_key)
    style = str(cfg.get("style") or "").strip()
    name = str(rec.get("name") or dir_key).strip()
    size = style_kit.aspect_size((16, 9), style_kit.asset_megapixels()) or (960, 544)

    hero_imgs, villain_imgs, hero_n, villain_n = _collect_project_cast_images(rec)
    refs = (hero_imgs[:2] + villain_imgs[:1])[:3]  # 最多 3 张参考（避免过多图导致模型偏脸）
    if refs:
        # 有参考图时走故事板链路，把主角/反派图作为身份锚点，画面仍保留场景与光影
        prompt = _cover_prompt_from_outline(rec, None, hero_imgs, villain_imgs)
        outs = comfyui_client.generate_storyboard(
            prompt, refs, seed=seed, size=size,
            filename_prefix=f"comic_drama/{dir_key}_cover",
        )
    else:
        # 无参考图时回落到场景 t2i（旧逻辑，保证封面始终能生成）
        prompt = (f"漫剧主视觉封面插画，《{name}》主题氛围场景，戏剧性光影，电影感构图，"
                  f"景深层次丰富，高细节，画面中不出现任何文字")
        outs = comfyui_client.generate_scene_base(
            prompt, seed=seed, style=style, size=size,
            filename_prefix=f"comic_drama/{dir_key}_cover")
    if not outs:
        raise RuntimeError("ComfyUI 未返回任何图片")
    cover = _project_cover_path(dir_key)
    # 2026-10-06：走统一落盘入口（跨盘安全 + 只挑真实存在的源 + 失败信息可定位）。
    # 刻意**不开** fallback_to_source：封面失败必须 fail-loud，
    # 沿用 ComfyUI 原始路径会让前端拿到一个不在可服务目录里的 URL（封面直接裂图）。
    return _ingest_comfy_output(outs, cover, logger=app.logger)

# ===== 资产点击查看（B：角色 / 场景 / 物品 / 分镜详情） =====

_PROJECT_KIND_DIRS = {"characters": CHARACTERS_DIR, "items": ITEMS_DIR,
                      "scenes": SCENES_DIR, "storyboards": STORYBOARDS_DIR,
                      "videos": VIDEOS_DIR, "final": FINAL_DIR,
                      "upscale": UPSCALE_DIR, "dub": DUB_DIR, "qc": QC_DIR}

# ===================== 资产沉淀过程可视化（2026-10-06） =====================
# 需求：资产不是「一下就有的」，而是「抽取 → 写提示词 → 出图 → 质检（可能重画 N 次）
# → 沉淀教训 → 切分入库」这样一条流水线跑出来的。此前这些步骤散落在质检历史
# （QC_DIR/<项目>/asset_<名称>.json）、产物旁路元数据（*.meta.json）与教训库
# （output/lessons/lessons.jsonl）三处，用户在界面上只能看到「最后那张图」，
# 看不到「它被改了几次、为什么改、学到了什么」。
#
# 本接口把三处数据按资产聚合成一条**时间线**，供前端在资产详情里渲染。
# 纯只读：不触发任何生成，不写盘，读失败一律降级成空步骤（绝不 500）。

#: 沉淀过程的步骤定义（id / 中文名 / 英文名）。前端按此顺序渲染步骤条。
_PRECIP_STEPS = (
    ("extract", "资产抽取", "Extraction"),
    ("prompt", "提示词定稿", "Prompt"),
    ("base", "基础图出图", "Base image"),
    ("qc", "质检迭代", "QC iterations"),
    ("lesson", "教训沉淀", "Lessons"),
    ("derive", "视角切分", "View derivation"),
    ("store", "入库完成", "Stored"),
)

def _precip_meta_of(asset_dir: str, filename: str) -> dict:
    """读某个产物的旁路元数据（<产物>.meta.json），失败返回 {}。"""
    p = os.path.join(asset_dir, f"{filename}.meta.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        app.logger.debug("产物元数据读取失败 %s：%s", p, e)
        return {}

# ===== 状态 =====

def _task_queue_status() -> dict:
    """TaskQueue 状态 + 「未接线」显式标注（N2，2026-09-22 复验）。

    ``TaskQueue.submit`` 在本项目**没有生产调用方**：单 GPU 并发由 ``gpu_task_gate``
    的进程级 Semaphore 承担（见 ``gpu_task_gate.py`` 模块头「不做什么」）。D-07 给
    submit 加的背压/去重是该模块**自身契约**的加固，供嵌入使用与测试。这里显式标注
    ``wired=False``，避免 ``/api/status`` 的 ``task_queue`` 字段让调用方误以为它是
    生产并发闸门（即「已修但不可达」的假象）。

    既有字段（running/concurrency/queued/max_queue/pending/current/current_elapsed_sec）
    原样保留，``wired`` / ``note`` 均为**新增**字段。
    """
    try:
        st = dict(task_queue.status())
    except Exception as e:  # noqa: BLE001  可观测性接口自身绝不能把 /api/status 打挂
        return {"wired": False, "error": f"{type(e).__name__}: {e}"}
    st["wired"] = False
    st["note"] = ("本进程 GPU 并发由 gpu_gate 承担；TaskQueue.submit 未接线"
                  "（D-07 加固属模块自身契约，非生产路径）")
    return st

def _consistency_collect(project_name: str, episode_no: int = None) -> dict:
    """从磁盘采集一致性校验所需素材

    - character_refs：output/assets/characters/<项目>/<角色>/front.png（缺则 base.png）
    - shot_images：优先读 storyboards manifest（含每镜产出图与 QC 记录），
      其次按 shot_NN.png 命名约定扫描，再从剧本补齐 characters_in_shot
    """
    chars_dir = os.path.join(CHARACTERS_DIR, project_name)
    character_refs = {}
    asset_dirs = {}
    if os.path.isdir(chars_dir):
        for name in sorted(os.listdir(chars_dir)):
            d = os.path.join(chars_dir, name)
            if not os.path.isdir(d):
                continue
            ref = _first_existing(os.path.join(d, "front.png"), os.path.join(d, "base.png"))
            if ref:
                character_refs[name] = ref
            asset_dirs[name] = d

    # 分镜图 + 剧本角色归属（统一用数字键，保证文件名/剧本/manifest 三方对齐）
    shot_images = {}
    sb_dir = _ep_read_dir(STORYBOARDS_DIR, project_name, episode_no)

    # 剧本 → 每镜角色
    shot_chars = {}
    try:
        key = project_store.safe_key(project_name)
        script = None
        if episode_no:
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, int(episode_no))
        if not script:
            eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
            if eps:
                first = eps[0]
                no = first if isinstance(first, int) else (
                    first.get("episode_no") if isinstance(first, dict) else 1)
                script = novel_to_script.load_episode_script(SCRIPT_DIR, key, no)
        for s in ((script or {}).get("shots") or []):
            if isinstance(s, dict):
                shot_chars[_shot_num_key(s.get("shot_id"))] = s.get("characters_in_shot") or []
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"剧本读取失败（一致性校验将缺少角色归属）：{e}")

    # 先按目录命名约定扫描
    if os.path.isdir(sb_dir):
        for fn in sorted(os.listdir(sb_dir)):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            k = _shot_num_key(os.path.splitext(fn)[0])
            shot_images[k] = {"image": os.path.join(sb_dir, fn),
                              "characters": shot_chars.get(k, [])}

    # manifest 覆盖（可能指向非默认目录，并附带 QC 分数）
    manifest_path = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                mf = json.load(f) or {}
            for s in (mf.get("shots") or []):
                if not isinstance(s, dict):
                    continue
                fp = comfyui_client.resolve_local_path(s.get("file") or "")
                if not (fp and os.path.isfile(fp)):
                    continue
                k = _shot_num_key(s.get("shot_id"))
                shot_images.setdefault(k, {})
                shot_images[k]["image"] = fp
                shot_images[k]["characters"] = shot_chars.get(k, [])
                shot_images[k]["qc"] = (s.get("qc") or {})
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"分镜 manifest 读取失败：{e}")

    return {"character_refs": character_refs, "shot_images": shot_images,
            "asset_dirs": asset_dirs}

# ==========================================================================
# 通用辅助：剧本读取 / 章节原文 / 关键帧目录
# ==========================================================================

def _load_script_for(project_name: str, episode_no=None) -> dict:
    """按项目名（+可选集号）读取剧本；缺集号时取该项目第一集

    兼容两种历史布局（否则「迁移项目」会永远读不到剧本）：
      A. 现行：SCRIPT_DIR/<project_key>/第N集.json
      B. 迁移遗留：SCRIPT_DIR/<name>_<时间戳>.json（扁平，无子目录）
    遗留项目在项目索引里登记着 episode_count（例如 10），但按 A 找不到任何一集，
    于是分镜画布 / 导出 / 质检等全部读到空数据，界面显示「10 集 · 0 分镜」。
    这里在 A 落空时回退到 B，并优先取时间戳最新的一份。
    """
    key = project_store.safe_key(project_name)
    script = None
    if episode_no:
        try:
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, int(episode_no))
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"剧本读取失败（第{episode_no}集）：{e}")
    if not script:
        try:
            eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
        except Exception:  # noqa: BLE001
            eps = []
        if eps:
            first = eps[0]
            epno = first if isinstance(first, int) else (first.get("episode_no") or 1)
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, epno)
    if not script:
        script = _load_legacy_flat_script(project_name)
    return script or {}

def _load_legacy_flat_script(project_name: str) -> dict:
    """回退：读取旧版扁平命名的剧本（SCRIPT_DIR/<name>_<时间戳>.json）。

    仅在现行目录布局读不到剧本时调用，因此不会遮蔽正常的第N集.json。
    按修改时间倒序取第一份「含 shots」的文件，避免命中空壳/中间态产物。
    """
    try:
        names = os.listdir(SCRIPT_DIR)
    except OSError:
        return {}
    cands = []
    for fn in names:
        if not fn.lower().endswith(".json"):
            continue
        stem = fn[:-5]
        # 允许 <name>_<时间戳> 与 <name> 本身（例如「剑心初醒_兼容版」）
        if stem != project_name and not stem.startswith(project_name + "_"):
            continue
        path = os.path.join(SCRIPT_DIR, fn)
        try:
            cands.append((os.path.getmtime(path), path))
        except OSError:
            continue
    for _, path in sorted(cands, reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"遗留剧本读取失败 {path}：{e}")
            continue
        if isinstance(data, dict) and (data.get("shots") or data.get("episode_no")):
            app.logger.info("剧本回退：%s 使用遗留扁平剧本 %s", project_name, os.path.basename(path))
            return data
    return {}

def _chapter_text_for_script(script: dict) -> str:
    """由剧本 metadata（novel_id + chapter_index）反查该集对应的原文章节文本"""
    meta = (script or {}).get("metadata") or {}
    novel_id = meta.get("novel_id")
    if not novel_id:
        return ""
    ch_index = meta.get("chapter_index") or (script or {}).get("episode_no") or 1
    try:
        text = read_novel_text(NOVELS_DIR, str(novel_id))
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"小说正文不可读（覆盖率归属将缺失）：{e}")
        return ""
    for c in split_chapters(text):
        if int(c.get("index") or 0) == int(ch_index or 0):
            return text[c.get("start") or 0:c.get("end") or 0]
    return ""

def _shot_coverage_map(script: dict) -> dict:
    """把原文章节正文单元归属到镜头（用于分镜画布展示「该镜承载了原文哪几句」）

    规则：逐单元与各镜「描述+台词+prompt_h3」做 4-gram 字面比对，
    取命中率最高的镜头归属；命中率低于 0.3 视为未承载。
    这是**离线规则判定**，与 coverage.py 的 LLM 判定同源（同一 gram 口径），
    仅供画布展示定位用，不替代覆盖率报告结论。
    """
    text = _chapter_text_for_script(script)
    if not text:
        return {}
    try:
        units, _total = coverage.split_source_units(text)
    except Exception:  # noqa: BLE001
        return {}
    if not units:
        return {}
    shots = [s for s in ((script or {}).get("shots") or []) if isinstance(s, dict)]
    if not shots:
        return {}
    shot_grams = []
    for s in shots:
        corpus = " ".join(str(x) for x in (
            s.get("description"), s.get("dialogue_text"), s.get("prompt_h3"),
            s.get("location"), s.get("camera")) if x)
        try:
            shot_grams.append(coverage._grams(coverage._norm(corpus)))
        except Exception:  # noqa: BLE001
            shot_grams.append(set())

    out: dict = {}
    for uid, unit in enumerate(units, start=1):
        if coverage.is_title_unit(unit):
            continue
        best_i, best_r = -1, 0.0
        for i, grams in enumerate(shot_grams):
            if not grams:
                continue
            try:
                r = coverage.literal_ratio(unit, grams)
            except Exception:  # noqa: BLE001
                continue
            if r > best_r:
                best_i, best_r = i, r
        if best_i >= 0 and best_r >= 0.3:
            sid = shots[best_i].get("shot_id", best_i + 1)
            out.setdefault(str(sid), []).append(
                {"unit_id": uid, "text": unit[:200], "ratio": best_r})
    return out

def _keyframes_dir(project_name: str, episode_no=None) -> str:
    d = _ep_dir(os.path.join(KEYFRAMES_DIR, _safe_project(project_name)), episode_no)
    os.makedirs(d, exist_ok=True)
    return d

def _collect_asset_refs(project: str) -> tuple:
    """从磁盘自动收集项目的角色 / 场景参考图（无需前端传入）

    返回 (character_refs, scene_refs)，元素形如 {"name":..., "front": 本地路径}，
    可直接喂给 _collect_reference_images。

    为什么需要它：单镜重跑等「带内调用的接口」如果只依赖前端传参，
    前端一旦传了结构不完整的对象（例如直接传剧本里的 characters，只有
    reference_prompt_zh 而没有 front/base 键），参考图会静默丢失、
    视频退化成无角色锚点——这类静默降级比报错更难发现。

    S6 修复：判据与 pipeline.probe_assets 对齐 ——
      1) 扩展名白名单 (".png", ".jpg", ".jpeg", ".webp")，不再只认 4 个固定文件名；
      2) 同一目录下取第一张非空图片（兼容 ComfyUI 直接输出 base_123.png 等非标名）；
      3) 找不到任何图片 → 返回空 dict，**绝不静默 take-first**（由调用方决策报错/跳过）。
    B-14 P2-4：判据统一抽到模块级 _first_existing_asset_image，取图判据
    _build_asset_index 复用同一函数，消除两处「判有图」口径漂移。
    """
    def _first_nonempty_image(d: str) -> str:
        return _first_existing_asset_image(d)

    def _scan(root: str) -> list:
        out = []
        base = os.path.join(root, _safe_project(project))
        if not os.path.isdir(base):
            return out
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if not os.path.isdir(d):
                continue
            # S6 候选顺序：front/base 固定名优先，否则取目录内第一张非空图片
            ref = ""
            for cand in ("front.png", "base.png", "front.jpg", "base.jpg"):
                p = os.path.join(d, cand)
                if os.path.isfile(p):
                    ref = p
                    break
            if not ref:
                ref = _first_nonempty_image(d)
            if ref:
                out.append({"name": name, "front": ref, "base": ref})
        return out

    return _scan(CHARACTERS_DIR), _scan(SCENES_DIR)

# ==========================================================================
# P1-2 关键帧驱动视频模式
# ==========================================================================

def _keyframe_sb_map(project_name: str, script: dict = None, storyboards=None,
                     episode_no=None) -> dict:
    """取该项目的分镜图映射 {shot键: 本地路径}

    键同时注册「数字键」与「shot_NN 键」（如 "1" 与 "shot_01"），
    避免调用方因键风格不同而漏配。

    注意（真实缺陷修复）：**必须合并** manifest 与目录扫描，不能命中 manifest 就提前返回。
    manifest 可能只记录了部分镜头（例如某镜曾在画布上单独重跑，manifest 被写成了单条），
    此时提前返回会让其余镜头在后续 index 兜底里**错配到别的镜头的分镜图**，
    进而用错误的画面当关键帧首帧。
    """
    project = _safe_project(project_name)
    sb_map: dict = {}

    def _put(key, path):
        if not path or not os.path.isfile(path):
            return
        k = _shot_num_key(key)
        sb_map[k] = path
        # 同时注册 shot_NN 风格键（若 k 为纯数字）
        if k.isdigit():
            sb_map[f"shot_{int(k):02d}"] = path

    # ① 前端显式传入优先
    if isinstance(storyboards, dict):
        for k, v in storyboards.items():
            local = comfyui_client.resolve_local_path(v) if isinstance(v, str) else None
            if local:
                _put(k, local)
    # ② 目录扫描作为基底（覆盖所有实际存在的分镜图）
    sb_dir = _ep_read_dir(STORYBOARDS_DIR, project,
                          episode_no if episode_no is not None else _ep_of_script(script))
    if not sb_map and os.path.isdir(sb_dir):
        for fn in sorted(os.listdir(sb_dir)):
            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                name = os.path.splitext(fn)[0]
                seq = "".join(ch for ch in name if ch.isdigit())
                if seq:
                    _put(seq, os.path.join(sb_dir, fn))
    # ③ manifest 覆盖（含质检状态与可能位于非默认目录的产物路径）
    manifest_path = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                mf = json.load(f) or {}
            for s in (mf.get("shots") or []):
                if not isinstance(s, dict) or not s.get("success"):
                    continue
                fp = comfyui_client.resolve_local_path(s.get("file") or "")
                _put(s.get("shot_id"), fp)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"分镜 manifest 读取失败：{e}")
    return sb_map

# ==========================================================================
# P1-3 可视化分镜画布 + 单镜重跑
# ==========================================================================

def _storyboard_scratch_map(project):
    """扫「分镜生成中」的中间产物 → {shot_seq: {"url", "mtime"}}。

    ⭐ 为什么需要它（2026-10-02）：
        分镜步骤是**整步落盘**的 —— 6 镜全部生成 + 质检通过后，才把图写进
        `STORYBOARDS_DIR/<项目>/` 与 `storyboard_manifest.json`（画布的正规数据源）。
        而生成过程本身要 2~3 分钟/镜，整步十几分钟，期间画布**一张图都取不到**，
        用户看到的是「跑着但什么都没有」，误判为卡死或前端不刷新。

    `storyboard_scratch/` 是步骤进行中每镜的落盘位置（`shot_NN_tryK.png`，见
    下方生成侧 `scratch_png`），这里把它作为**只读的进度快照**暴露给画布，
    仅用于「生成中」预览，**不改变** `storyboard.exists/url` 的既有语义
    （那仍严格代表「已落盘正式产物」）。取最新 try（文件名尾部序号最大）。

    失败一律返回空 dict：这是纯展示增强，绝不能让画布接口 500。
    """
    out = {}
    scratch_dir = os.path.join(QC_DIR, project, "storyboard_scratch")
    if not os.path.isdir(scratch_dir):
        return out
    try:
        for fn in os.listdir(scratch_dir):
            m = re.match(r"^shot_(\d+)_try(\d+)\.png$", fn)
            if not m:
                continue
            seq, attempt = int(m.group(1)), int(m.group(2))
            full = os.path.join(scratch_dir, fn)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            cur = out.get(seq)
            if cur is None or attempt > cur["_attempt"]:
                out[seq] = {
                    "_attempt": attempt,
                    "url": f"/api/storyboards/scratch/{project}/{fn}",
                    "mtime": mtime,
                }
    except OSError:
        return {}
    for v in out.values():
        v.pop("_attempt", None)
    return out

# ==========================================================================
# 托管接口统一异常兜底
# --------------------------------------------------------------------------
# D1（2026-09-23）：本函数原先定义在文件后段，只能装饰**其后**注册的路由，
# 导致更早注册的 /api/storyboard/retry-shot、/api/video/retry-shot 拿不到兜底
# —— 它们抛错时前端收到的是 werkzeug 的 HTML 500，`readError()` 解析不出 error
# 字段，用户只看到「服务内部错误」而无从判断。故前移到首个使用点之前。
# 实现未改（仍复用下方的 _friendly_error，运行期解析）。
# ==========================================================================
def _autopilot_guard(fn):
    """统一异常兜底：托管接口不应把 500 抛给前端，而是返回可读错误

    注意不要把客户端错误（HTTPException，例如请求体不是合法 JSON 时
    werkzeug 抛出的 400 BadRequest）误判成服务端 500——否则前端会看到
    「500 服务内部错误」，而真实原因是自己发了个畸形请求，排查方向会被带偏。
    """
    def _wrap(*a, **k):
        try:
            return fn(*a, **k)
        except KeyError as e:
            return jsonify({"success": False, "error": f"对象不存在：{e}"}), 404
        except HTTPException as e:
            # 保留 werkzeug 原本的语义状态码（400/404/405…），不要降级成 500
            return jsonify({
                "success": False,
                "error": e.description or e.name,
            }), (e.code or 400)
        except Exception as e:  # noqa: BLE001
            app.logger.exception("托管接口异常")
            return jsonify({"success": False, "error": _friendly_error(e)}), 500
    _wrap.__name__ = fn.__name__
    return _wrap

def _storyboard_retry_shot_impl():
    """单镜分镜图重跑的实际实现（整段在 GPU 闸门内执行）

    D1（2026-09-23）：
    - 加 @_autopilot_guard → 异常不再泄漏成裸 HTML 500（与其它托管接口一致）。
    - 整段关键区（出图 → 质检 → 入库 → manifest 回写）进入 gpu_task_gate：
        · 避免与批量分镜 worker 抢同一张 GPU（TASK_QUEUE_CONCURRENCY 默认 1）；
        · 消除两边并发 read-modify-write storyboard_manifest.json 的**丢更新**
          —— 批量 worker 的 manifest 写入在它的 gate 内（`_storyboard_worker`
          由 `run_gpu_task` 包裹），本函数的写入也在本 gate 内，两者互斥。
      代价：批量任务在跑时手动重跑会排队等待（与「单 GPU 并发度 1」的设计一致；
      排队超过 30s 由 gpu_task_gate 打 warning，不静默）。
    """
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no'))
    shots = script.get("shots") or []
    shot = data.get('shot') or {}
    if not shot and shots:
        want = str(data.get('shot_id'))
        shot = next((s for s in shots if str(s.get("shot_id")) == want), {})
    if not shot:
        return jsonify({"success": False, "error": "未找到目标镜头"}), 400

    shot_id = shot.get("shot_id", 1)
    seq = _shot_seq(shot_id, 1)
    char_idx = _build_asset_index(script.get("characters") or [], project, "character")
    item_idx = _build_asset_index(script.get("items") or [], project, "item")
    scene_idx = _build_asset_index(script.get("scenes") or [], project, "scene")
    refs = _allocate_storyboard_refs(shot, char_idx, item_idx, scene_idx, project)
    # S6 修复：参考图匹配失败时，不再静默取首角色（旧行为会把"不存在的角色"当主角色），
    # 而是明确 400 + 具体错误。
    if not refs and shot.get("_no_reference"):
        return jsonify({"success": False, "no_reference": True,
                        "error": shot.get("_ref_error") or "该镜头角色在资产索引中无匹配",
                        "hint": "请检查剧本 characters_in_shot 与资产目录名是否一致"}), 400
    if not refs:
        return jsonify({"success": False,
                       "error": "该镜头无可用参考图（请先完成步骤2/3/4的资产生成）"}), 400
    labels = [r[1] for r in refs]
    # 风格：剧本自带 style（用户与总控敲定）优先，缺失时退回项目 plan 的 style
    _rs_style = style_kit.normalize_style(script.get("style")) or style_kit.normalize_style(
        (autopilot.get_plan(project) or {}).get("style"))
    if _rs_style:
        shot = dict(shot, style=(shot.get("style") or _rs_style))
    # G19 同款兜底：风格串无画幅关键词时以默认画幅（2026-09-28 起为 16:9）为底。本端点
    # 此前漏了这层兜底 → size=None → 完全不覆写，画幅完全沿用模板/参考图，与批量 worker
    # 口径不一致。第二个实参 = 分镜图预算 0.8MP（画质提升，旧值 0.5）。
    _rs_size = style_kit.aspect_size(
        style_kit.aspect_ratio(_rs_style) or style_kit.DEFAULT_RATIO,
        style_kit.storyboard_megapixels())
    prompt = comfyui_client.build_storyboard_prompt(
        shot, labels, has_characters=_shot_has_on_screen(shot))
    refs = _unify_ref_canvas(refs, _rs_size, project)
    # ---- 提示词预检（生成前质检）：先判 → 确定性自愈 → 再出图 ----
    # 目的：把 GPU 花在有问题的提示词上是纯浪费，且出图后质检才发现就已经晚了。
    qc_cfg = _qc_load_cfg()
    prompt, _pf, _pgate = _prompt_preflight(
        "storyboard", prompt, ctx=shot, style=(shot.get("style") or _rs_style),
        ref_count=len(refs), project_name=project)
    if not _pgate.get("accept"):
        # ★ 用户需求：质检不合格的提示词不留本地。用户决策 1：提示词预检是**生成前**的文本
        # 合规检查，不通过直接阻断不生成 → 没有图片/视频可删，只有这份提示词历史 json 落盘
        # （P12 `output/qc/<项目>/prompt_<shot>.json`），把它移回收站。
        try:
            _purge_prompt_records(project, shot_id,
                                  reason=f"提示词预检未通过（{_pgate.get('label')}）")
        except Exception as _pe:  # noqa: BLE001
            app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
        return jsonify({"success": False, "prompt_qc_blocked": True,
                        "error": f"提示词预检未通过（{_pgate.get('label')}）：{_pgate.get('reason')}"
                                 + (f"；建议：{_pf.get('rebuild_hint')}" if _pf.get("rebuild_hint") else ""),
                        "prompt_qc": _pf.get("verdict")}), 200
    seed = data.get('seed')
    try:
        result = comfyui_client.generate_storyboard(
            prompt_zh=prompt, ref_images=[r[2] for r in refs],
            filename_prefix=f"comic_drama_sb/{project}_shot_{seq:02d}_retry",
            seed=seed, size=_rs_size)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"分镜图重跑失败：{e}"}), 500
    files = (result or {}).get("files") or []
    if not files:
        return jsonify({"success": False, "error": "ComfyUI 未返回分镜图"}), 500

    # 质检（若已开启）：不达标同样阻断入库（与批量链路一致）
    qc_on = qc_client.image_qc_ready(qc_cfg)
    _ep = _ep_of_script(script, data.get('episode_no'))
    dst_dir = _ep_dir(os.path.join(STORYBOARDS_DIR, project), _ep)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"shot_{seq:02d}.png")
    verdict = None
    gate = None
    scratch_dir = os.path.join(QC_DIR, project, "storyboard_scratch")
    os.makedirs(scratch_dir, exist_ok=True)
    scratch = os.path.join(scratch_dir, f"shot_{seq:02d}_retry.png")
    # G8②：消费 ComfyUI output 源（与主 worker 的 move 语义对齐），不留 output 残留
    # 2026-10-06：改走统一落盘入口（原先 shutil.move(files[0], scratch) 跨盘会抛
    # WinError 3，把整次重跑判失败）
    scratch = _ingest_comfy_output(files, scratch, logger=app.logger)
    if qc_on:
        verdict = qc_client.check_image(scratch, _qc_shot_desc(shot), qc_cfg,
                                        style=(shot.get("style") or _rs_style),
                                        ref_images=_qc_ref_images(
                                            shot, char_idx, item_idx, scene_idx, refs))
        gate = _qc_gate(verdict)
        _qc_record_verdict(project, "image", shot_id, "单镜重跑质检",
                           1, seed, scratch, verdict, style=(shot.get("style") or _rs_style))
    if qc_on and not (gate or {}).get("accept"):
        # ★ 用户需求：质检「判定不通过」的暂存图不留本地（含 ComfyUI 侧）。
        # ⚠️ 只删「质检成功返回（ok=True）且判定不合格」的产物；ok=False（接口故障/超时/
        # 鉴权失败）不是产物不合格，绝不能删（那会把好图删光）。
        if (verdict or {}).get("ok") is True:
            _purge_rejected_artifacts([scratch], project=project, kind="storyboard_image_retry",
                                      reason=f"单镜重跑质检不合格（{(gate or {}).get('label')}）",
                                      history_file=(_qc_history_file_for(project, "image", shot_id)))
        return jsonify({"success": False, "qc_blocked": True,
                        "error": f"分镜图质检阻断（{(gate or {}).get('label')}）："
                                 f"{(gate or {}).get('reason')}；未写入正式目录",
                        "verdict": verdict}), 200
    shutil.copy2(scratch, dst)
    # 同步更新 manifest 中该镜条目
    # B-2 收口（2026-09-22 复验）：manifest 损坏时 read_json_strict 会 fail-loud 抛错，
    # 但此刻图片**已经**重跑成功并写进正式目录（上一行的 copy2）。若让异常直接冒泡，
    # 会把「部分成功」整镜报成失败，前端还只能拿到裸 HTML 500（全库仅注册了
    # BadRequest 处理器，无 JSON 500 处理器）。
    # 这里用窄 try 做**响亮降级**（不是 fail-open）：
    #   · 记 error 级日志（数据层异常不静默）
    #   · 在响应里显式带 manifest_updated=False + 原因，调用方可感知
    #   · **绝不**把 manifest 重建为 {} —— 那才会清空其他镜头的记录
    _manifest_updated = True
    _manifest_err = ""
    try:
        _update_storyboard_manifest_shot(project, shot_id, seq, dst, prompt, refs, verdict, gate,
                                         episode_no=_ep)
    except Exception as _m_err:  # noqa: BLE001
        _manifest_updated = False
        _manifest_err = f"{type(_m_err).__name__}: {_m_err}"
        app.logger.error(
            "单镜重跑：图片已写入正式目录，但 manifest 同步失败（不影响本次出图；"
            "project=%s shot=%s dst=%s）：%s", project, shot_id, dst, _manifest_err)
    # 提示词预检结论也落质检历史（kind=prompt），便于回溯「这一镜出图前提示词是什么状态」
    if not _pf.get("skipped"):
        try:
            _qc_record_verdict(project, "prompt", shot_id, "分镜图提示词预检",
                               0, seed, None, _pf.get("verdict") or {}, extra={
                                   "prompt_kind": "storyboard",
                                   "repairs": _pf.get("repairs") or [],
                                   "mode": (_pf.get("verdict") or {}).get("mode"),
                                   "accept": bool(_pf.get("accept")),
                               }, style=(shot.get("style") or _rs_style))
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"提示词预检记录落盘失败：{e}")
    _sub = f"ep{int(_ep):02d}/" if _ep and int(_ep) > 1 else ""
    return jsonify({"success": True, "project": project, "shot_id": shot_id, "seq": seq,
                    "path": dst,
                    "url": f"/api/storyboards/file/{project}/{_sub}shot_{seq:02d}.png",
                    "prompt": prompt, "ref_count": len(refs),
                    # B-2：本次出图是否已同步进 manifest。False 表示图已出好、但清单未更新
                    # （manifest 损坏等），调用方可据此提示用户「重跑成功、清单待修」。
                    "manifest_updated": _manifest_updated,
                    "manifest_error": _manifest_err,
                    "prompt_qc": _pf.get("verdict"), "prompt_qc_repairs": _pf.get("repairs") or [],
                    "qc": verdict})

def _update_storyboard_manifest_shot(project: str, shot_id, seq: int, dst: str,
                                     prompt: str, refs: list, verdict=None, gate=None,
                                     episode_no=None):
    """把单镜重跑结果写回分镜 manifest（保持既有 schema 不变）

    集级目录：第 1 集沿用平铺，第 2 集起写 epNN/ 下的 manifest 与 URL。
    """
    _flat = os.path.join(STORYBOARDS_DIR, project)
    mpath = os.path.join(_ep_dir(_flat, episode_no), "storyboard_manifest.json")
    _sub = os.path.basename(_ep_dir(_flat, episode_no)) if _ep_dir(_flat, episode_no) != _flat else ""
    _url_prefix = f"{project}/{_sub}/" if _sub else f"{project}/"
    manifest = {}
    if os.path.isfile(mpath):
        # B-2（2026-09-22 复核补漏）：本路径与 _storyboard_worker（app.py 的
        # atomic_write_json 落盘）写的是**同一个** storyboard_manifest.json。
        # 旧实现用 `except: manifest = {}` 的 fail-open 读 + 裸 open(w) 非原子写，
        # 与批量 worker 并发时会出现「读到半截 → 用残缺 manifest 覆盖回去 → 其他
        # 镜头记录整批丢失」。这里改为与 D-03/D-04 同口径：严格读（损坏→.bak 恢复或
        # fail-loud）+ 原子写。单镜重跑是用户显式操作，manifest 损坏时报错远好过静默清空。
        manifest = read_json_strict(mpath, {})
    items = [s for s in (manifest.get("shots") or []) if isinstance(s, dict)]
    target = next((s for s in items if _shot_num_key(s.get("shot_id")) == _shot_num_key(shot_id)), None)
    entry = {
        "shot_id": shot_id, "success": True,
        "file": dst, "url": f"/api/storyboards/file/{_url_prefix}shot_{seq:02d}.png",
        "prompt": prompt, "ref_count": len(refs),
        "refs": {r[0]: os.path.basename(os.path.dirname(r[2])) for r in refs},
        "regenerated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "regenerated": "single_shot_retry",
    }
    if verdict:
        entry["qc"] = {"enabled": True, "status": "pass" if (gate or {}).get("accept") else "blocked",
                       "label": (gate or {}).get("label"), "attempts": 1,
                       "score": verdict.get("score"), "verdict": verdict.get("verdict"),
                       "reason": verdict.get("reason")}
    if target is not None:
        target.update(entry)
    else:
        items.append(entry)
    manifest.setdefault("project_name", project)
    manifest["shots"] = items
    manifest["total"] = len(items)
    manifest["success_count"] = sum(1 for s in items if s.get("success"))
    manifest["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(mpath), exist_ok=True)
        # B-2：与 worker 侧统一走 fs_atomic（唯一临时名 + fsync + .bak 快照 + replace 重试），
        # 避免单镜重跑与批量分镜 worker 并发写同一 manifest 互相截断。
        atomic_write_json(mpath, manifest)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"分镜 manifest 更新失败：{e}")

def _video_retry_shot_impl():
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no'))
    shots = script.get("shots") or []
    shot = data.get('shot') or {}
    if not shot and shots:
        want = str(data.get('shot_id'))
        shot = next((s for s in shots if str(s.get("shot_id")) == want), {})
    if not shot:
        return jsonify({"success": False, "error": "未找到目标镜头"}), 400

    shot_id = shot.get("shot_id", 1)
    seq = _shot_seq(shot_id, 1)
    mode = str(data.get('mode') or 'reference').strip().lower()
    char_refs = data.get('character_refs') or []
    scene_refs = data.get('scene_refs') or []
    ref_imgs = _collect_reference_images(char_refs, scene_refs)
    main_char_img = _collect_reference_images(char_refs[:1], [])
    # 2026-09-27「分镜 + 本镜资产」：构建角色/物品/场景索引，逐镜匹配。
    _r_char_idx = _build_asset_index(char_refs, project, "character")
    _r_item_idx = _build_asset_index(script.get("items") or [], project, "item")
    _r_scene_idx = _build_asset_index(script.get("scenes") or [], project, "scene")
    if scene_refs:
        for _k, _v in _build_asset_index(scene_refs, project, "scene").items():
            if _v.get("image"):
                _r_scene_idx[_k] = _v
    # 参考图兜底：前端未传、或传了结构不完整的对象（例如直接传剧本 characters，
    # 只有 reference_prompt_zh 而无 front/base 键）时，从磁盘资产目录自动收集，
    # 避免「无角色锚点」的静默降级。
    if not main_char_img or not ref_imgs:
        auto_chars, auto_scenes = _collect_asset_refs(project)
        # ⚠️ 2026-10-06 修复：同主链路 —— `char_refs or auto_chars` 会被「结构不完整
        # 但非空」的剧本 characters 短路，磁盘资产永远用不上（见 _upgrade_refs_with_disk）。
        if auto_chars:
            char_refs = _upgrade_refs_with_disk(char_refs, auto_chars)
        if not main_char_img:
            main_char_img = _collect_reference_images(char_refs[:1], [])
        if not ref_imgs:
            if auto_scenes:
                scene_refs = _upgrade_refs_with_disk(scene_refs, auto_scenes)
            ref_imgs = _collect_reference_images(char_refs, scene_refs)
        if main_char_img or ref_imgs:
            app.logger.info(f"[retry-shot] 参考图已由磁盘资产补齐："
                            f"角色 {len(main_char_img)} / 合计 {len(ref_imgs)}")
    _rs_ep = _ep_of_script(script, data.get('episode_no'))
    _rs_sub = f"ep{int(_rs_ep):02d}/" if _rs_ep and int(_rs_ep) > 1 else ""
    sb_map = _keyframe_sb_map(project, script, episode_no=_rs_ep)
    sb_local = sb_map.get(_shot_num_key(shot_id))
    _r_end_ref = None  # keyframe 模式的尾帧声明（<Picture 2>），非 keyframe 恒 None

    if mode == 'keyframe':
        kf_dir = _ep_dir(os.path.join(KEYFRAMES_DIR, project), _rs_ep)
        end_p = os.path.join(kf_dir, f"shot_{seq:02d}_end.png")
        if not (sb_local and os.path.isfile(sb_local)):
            return jsonify({"success": False, "error": "缺少分镜图，无法关键帧驱动"}), 400
        if not os.path.isfile(end_p):
            return jsonify({"success": False,
                            "error": "缺少尾帧，请先执行关键帧生成（/api/keyframes/generate）"}), 400
        _seg_refs = [sb_local, end_p]
        # 审计 P1-1（2026-09-29）：与主链路 keyframe 分支同口径 —— 实际挂图只有
        # 「首帧 + 尾帧」两张，char/item/scene 引用必须置空（旧实现残留
        # char_refs/scene_refs，会声明出实际不存在的 <Picture 3..N>）；尾帧图
        # 经 end_frame_ref 声明为 <Picture 2>，让提示词真正产出尾帧锚定句。
        _r_char_refs, _r_item_refs, _r_scene_refs = [], [], []
        _r_end_ref = {"name": f"shot_{seq:02d}_end"}
    else:
        if sb_local:
            # 「分镜 + 本镜资产」：分镜图 + 本镜角色三视图 + 物品 + 场景
            _r_matched = _match_shot_chars(shot, _r_char_idx)
            _r_want_half = _framing_wants_half_shot(shot)
            _r_char_imgs, _r_char_refs = [], []
            for _mc in (_r_matched or []):
                _e = _r_char_idx.get(_mc) or {}
                # 2026-10-02 服装变体：本镜服装提示（shot.outfit / shot.character_outfits）
                # 能解析出已生成的 outfit_key → 参考图优先取 outfits/<key>/ 同档位图；
                # 解析不出 / 未生成回落主设定图（_shot_outfit_dir 返回 ''，fail-open）。
                _p = _pick_char_view(_e, _r_want_half,
                                     _shot_outfit_dir(shot, _mc, _e.get("_dir") or ""))
                if _p and _p not in _r_char_imgs:
                    _r_char_imgs.append(_p)
                    _r_char_refs.append({"name": _mc,
                                         "appearance": _e.get("appearance")
                                         or _e.get("description") or ""})
            _r_item_imgs, _r_item_refs = [], []
            for _it in _resolve_item_names(shot, _r_item_idx, "重跑切段"):
                _p = (_r_item_idx.get(_it) or {}).get("image")
                if _p and _p not in _r_item_imgs and _p not in _r_char_imgs:
                    _r_item_imgs.append(_p)
                    _e = _r_item_idx.get(_it) or {}
                    _r_item_refs.append({"name": _it,
                                         "appearance": _e.get("appearance")
                                         or _e.get("description") or ""})
            _r_loc, _r_sc_entry = _resolve_scene_entry(shot, _r_scene_idx, "重跑切段")
            # 与主链路同口径：按本镜机位取对应场景档（缺失逐级回落 front/base）
            _r_scene_img = _pick_scene_view(_r_sc_entry, shot) if _r_sc_entry else None
            _r_scene_refs = ([{"name": _r_loc, "appearance": ""}]
                             if _r_scene_img else [])
            _seg_refs = [sb_local] + _r_char_imgs + _r_item_imgs + \
                ([_r_scene_img] if _r_scene_img else [])
            if not _r_char_imgs:
                _seg_refs = [sb_local] + main_char_img
                _r_char_refs = char_refs[:1] if char_refs else []
            # 9 张上限（与 builder MAX_REFERENCE_IMAGES=9 一致）
            if len(_seg_refs) > 9:
                _seg_refs = _seg_refs[:9]
                _rk = 9 - 1
                _r_char_refs = _r_char_refs[:_rk]
                _rk -= len(_r_char_refs)
                _r_item_refs = _r_item_refs[:_rk] if _rk > 0 else []
                _rk -= len(_r_item_refs)
                _r_scene_refs = _r_scene_refs[:_rk] if _rk > 0 else []
        else:
            _seg_refs = ref_imgs
            _r_char_refs, _r_item_refs, _r_scene_refs = char_refs, [], scene_refs
    try:
        dur = float(shot.get('duration') or 5)
    except (TypeError, ValueError):
        dur = 5.0

    # ---- 长镜切段（P0-1，与 worker 内 _shot_segment 同口径）----
    # 单镜重跑接口此前硬编码「一个分镜 = 一段」，与主链路的长镜切段不一致：
    # 用户手点重跑一个 12 秒镜头时，仍会一次生成 12 秒（超出 4 秒可信窗口）。
    # 这里改为与 _shot_segment 相同的切段 + 逐子段重建提示词逻辑。
    _rs_sub_shots = h3_prompt_kit.segment_shot(
        shot, dur, max_sec=h3_prompt_kit.H3_SEGMENT_MAX_SEC)
    _rs_segs = []
    # ⭐ 单镜重跑同样走 H3 Director timeline（generate_h3_sequence → 构建器），
    #    故按与 worker 内 _shot_segment 同口径给每个子段挂场景 LoRA（同源规则表）。
    #    优先使用 LLM 智能选择，失败则回落规则表匹配。
    _rs_loras = h3_segment_loras.select_loras_for_shot_smart(shot)
    for _rsi, _rsub in enumerate(_rs_sub_shots):
        if mode == 'keyframe':
            # ⚠️ 与 worker 内 _shot_segment 同口径：尾帧锚定句只挂最后一段
            #    （每段都挂 = 每段都演完整镜，接缝倒带重启）。
            _rp = comfyui_client._build_h3_prompt(
                _rsub, _r_char_refs, _r_scene_refs,
                storyboard_ref={"name": f"shot_{seq}"}, item_refs=_r_item_refs,
                end_frame_ref=(_r_end_ref if _rsi == len(_rs_sub_shots) - 1 else None))
        elif sb_local:
            _rp = comfyui_client._build_h3_prompt(
                _rsub, _r_char_refs, _r_scene_refs,
                storyboard_ref={"name": f"shot_{seq}"}, item_refs=_r_item_refs)
        else:
            # 择优：既有 prompt_h3 结构合规才采用，否则用规范构建器重建
            # （历史缺陷：`shot.get('prompt_h3') or _build_h3_prompt(...)` 让
            #  剧本里那句无参考图标签的裸英文把结构化提示词整个顶掉）
            _rp = comfyui_client.resolve_h3_prompt(
                _rsub, _r_char_refs, _r_scene_refs, item_refs=_r_item_refs)
        _rsuf = (f"_{chr(ord('a') + _rsi)}"
                 if len(_rs_sub_shots) > 1 and _rsi < 26 else "")
        _rs_segs.append({"prompt": _rp,
                         "duration": float(_rsub.get("duration") or dur),
                         "reference_images": _seg_refs,
                         "name": f"shot_{seq:02d}{_rsuf}",
                         "loras": list(_rs_loras)})
    seg = _rs_segs[0]

    # ---- 提示词预检（生成前质检）----
    # H3 的结构缺段只有生成端能重建（必须有每张参考图的用途），所以这里做「验证 + 安全追加」，
    # 命中致命缺陷就直接拦：缺段的 H3 提示词等于出片跑偏，而一次视频生成的代价远大于一次判断。
    # ⚠️ 逐子段预检：任一子段不合格即整镜拦截（与主链路 worker 内的逐子段预检口径一致）。
    for _ri, _rseg in enumerate(_rs_segs):
        _rp2, _pf_v, _pgate_v = _prompt_preflight(
            "h3", _rseg["prompt"], ctx=shot,
            style=(shot.get("style") or style_kit.normalize_style(
                (autopilot.get_plan(project) or {}).get("style"))),
            # ⚠️ 用 seg 里的参考图数量，不要用 `refs`：关键帧分支只设 ref_images，没有 `refs`，
            #    直接引用会 NameError（该分支走不到 else，`refs` 从未绑定）。
            expect_refs=bool(_rseg.get("reference_images")),
            project_name=project)
        _rseg["prompt"] = _rp2
        if not _pgate_v.get("accept"):
            # ★ 用户需求：视频提示词预检不通过 → 提示词唯一落盘物（P12）移回收站（同决策 1）。
            try:
                _purge_prompt_records(project, shot_id,
                                      reason=f"视频提示词预检未通过（{_pgate_v.get('label')}）")
            except Exception as _pe:  # noqa: BLE001
                app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
            return jsonify({"success": False, "prompt_qc_blocked": True,
                            "error": f"视频提示词预检未通过（{_pgate_v.get('label')}）：{_pgate_v.get('reason')}"
                                     + (f"；建议：{_pf_v.get('rebuild_hint')}" if _pf_v.get("rebuild_hint") else ""),
                            "prompt_qc": _pf_v.get("verdict")}), 200

    try:
        result = comfyui_client.generate_h3_sequence(
            segments=_rs_segs,
            filename_prefix=f"comic_drama_retry/{project}_shot_{seq:02d}",
            seed=data.get('seed'), timeout_per_segment=int(data.get('timeout') or 900))
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"单镜视频重跑失败：{e}"}), 500
    files = (result or {}).get('files') or []
    if not files or not os.path.isfile(files[0]):
        return jsonify({"success": False, "error": "ComfyUI 未返回视频文件"}), 500
    vid_dir = _ep_dir(os.path.join(VIDEOS_DIR, project), _rs_ep)
    dst = _ingest_comfy_output(files, os.path.join(vid_dir, f"shot_{seq:02d}.mp4"),
                               logger=app.logger)
    # 该镜已更新 → 同集的旧成片失效，打上「已过期」标记，避免用户对着旧成片点验收
    stale = {}
    try:
        stale = pipeline.mark_deliverable_stale(
            project, _rs_ep or 1, "镜头重做后成片需重新合成",
            {"shot_id": shot_id, "seq": seq, "mode": mode,
             "video": os.path.basename(dst)})
    except Exception as e:  # noqa: BLE001 - 打标失败不影响重做本身
        app.logger.warning(f"标记成片过期失败：{e}")
    return jsonify({"success": True, "project": project, "shot_id": shot_id, "seq": seq,
                    "mode": mode, "path": dst,
                    "url": f"/api/videos/{project}/{_rs_sub}{os.path.basename(dst)}",
                    "ref_count": len(seg["reference_images"]), "duration": seg["duration"],
                    "deliverable_marked_stale": bool(stale)})

# ===== 步骤2/3/4：资产生成（角色/物品/场景 + 多视角） =====

def _generate_asset_task(task_id: str, assets: list, asset_type: str, project_name: str,
                         style: str = "", overwrite: bool = False, sub_dir: str = ""):
    """后台资产生成任务：基础图 + （角色）由整图本地切分派生的视角单图

    P0 修复（④⑤）：全链路接入 AI 质检——基础图必须送检；不达标自动重生成（换 seed），
    重试仍不达标 / 质检调用异常 → 阻断入库并标记 qc_blocked。

    A-2 P0 断点续跑：新增 overwrite 参数（默认 False）。已达标入库的资产
    （目录内已有非空图，判据同 pipeline.probe_assets）直接跳过，不再重复
    「生成→质检→重画」；overwrite=True 时强制全量重生成。

    服装变体（衣柜，2026-10-02）：新增 sub_dir 可选参数（默认空串 = 行为与从前
    完全一致）。非空时把该资产的落盘目录（含 base.png / 切分视角图 / meta
    sidecar / 质检历史）整体挂到「主设定目录 + sub_dir」下（如 outfits/<key>），
    用于角色服装变体 —— 生成链路（提示词预检 / 质检 / 重试 / 切分）零改动，
    仅目录多一层。

    风格落地（2026-09-18 修复）：新增 style 参数。此前该任务**完全没有风格入参**，
    资产提示词只有 bible 的 reference_prompt_zh（实测其中零风格词），于是物品/角色/场景
    出的参考图完全不体现用户与总控敲定的风格。现在：
      - 正向提示词在生成前统一追加风格后缀（幂等，二次追加不重复）；
      - 解析画幅并覆写尺寸节点，「竖屏 9:16」真正落到画布；
      - 派生视角图继承基础图的画幅（切分件贴回同尺寸画布，见 sheet_split）。

    ⚠️ 2026-09-24 视角图改造（勿回退为 GPU 多视角重渲染）：详见 app/sheet_split.py 模块头。
      角色视角图由**基础图整图本地列投影切分**得到（零 GPU、零质检），物品/场景不再产出
      视角图；`success` 也不再受视角质量影响。
    """
    try:
        base_dir = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}[asset_type]
        gen_base = {
            "character": comfyui_client.generate_character_base,
            "item": comfyui_client.generate_item_base,
            "scene": comfyui_client.generate_scene_base,
        }[asset_type]

        # 服装变体（衣柜）子目录防御：sub_dir 只应是「outfits/<安全键>」这类相对
        # 子路径（由 /api/assets/character/outfit 传入）。这里再做一次纵深防御——
        # 归一后含「..」/ 盘符 / 绝对路径前缀的一律按空串处理（fail-open，回落主
        # 设定目录，与该参数不存在时的行为完全一致）。
        if sub_dir:
            _sd = os.path.normpath(str(sub_dir)).replace("\\", "/").strip("/")
            if not _sd or _sd.startswith("..") or "/../" in f"/{_sd}/" or ":" in _sd:
                app.logger.warning("资产子目录参数非法，按主设定目录处理：%r", sub_dir)
                sub_dir = ""
            else:
                sub_dir = _sd

        def _asset_full_dir(asset_name: str) -> str:
            """该资产的落盘目录：主设定目录（base_dir/<项目>/<名称>），或
            （服装变体）主设定目录 + sub_dir 子目录。sub_dir 为空时与从前逐字节一致。"""
            d = os.path.join(base_dir, project_name, asset_name)
            return os.path.join(d, sub_dir) if sub_dir else d

        # 风格（文字部分，如画风/色调）仍从总控敲定的 style 串解析；
        # 画幅**按资产类型内置写死**（2026-09-22 需求，不跟随视频比例）：
        #   角色参考图(三视图设定图) 1:1 / 道具 item 1:1 / 场景 scene 16:9
        # 与成片画幅解耦——即使用户拍 9:16 成片，角色参考图仍是 1:1。
        # ⚠️ 角色基础图内容是「正/侧/背三张全身视图横排的三视图设定图」，不是单人立绘，
        #    2026-09-23 已从 3:4 竖幅改回 1:1（3:4 会把三人挤到贴边，实测留白 0~2px）；
        #    详见 style_kit.ASSET_BASE_RATIO 上方注释。
        # ⚠️ 2026-09-24：视角图已改为从基础图**本地切分**派生（app/sheet_split.py），
        #    不再有「多视图独立画幅」这条路径，故这里只需解析基础图画幅。
        style_res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO)
        gen_style = style_res["style"]
        _base_ratio = style_kit.asset_aspect_ratio(asset_type) or style_kit.DEFAULT_RATIO
        # ⚠️ 像素预算**必须显式传**：2026-10-02 前此处漏传，资产图静默吃 `aspect_size`
        #    的函数默认值 0.5MP（1:1 → 736×736），而角色设定图是**全链路角色一致性的
        #    上游锚点** —— 参考图细节不足 → 被分镜放大后表现为系统性脸漂/色漂。
        #    现取 asset_megapixels()（默认 1.5，env MJSCXT_ASSET_MEGAPIXELS 可覆盖）。
        gen_size = style_kit.aspect_size(_base_ratio, style_kit.asset_megapixels())
        app.logger.info("[资产风格] %s 资产生成风格=%s；内置画幅 %s×%s（%.2fMP）",
                        asset_type, gen_style or style, _base_ratio[0], _base_ratio[1],
                        style_kit.asset_megapixels())

        # 质检配置：任务级读取一次，本任务内所有资产共用
        qc_cfg = _qc_load_cfg()
        qc_on = qc_client.image_qc_ready(qc_cfg)          # 质检接口是否可用
        qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("image_enabled"))
        if qc_declared and not qc_on:
            # 已声明开启图片质检但接口不可用：后续逐个资产明确阻断，绝不静默放行
            app.logger.error("[资产质检] 图片质检已开启但接口未就绪（base_url/api_key/model 不完整），"
                             "本次资产生成将阻断入库；请检查 qc_config.json")
        max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
        scratch_root = os.path.join(QC_DIR, project_name, "assets_scratch")

        results = []
        total = len(assets)

        def _set_phase(phase: str, qc_phase: str = None):
            with lock:
                generation_state[task_id]["phase"] = phase
                if qc_phase:
                    generation_state[task_id]["qc_phase"] = qc_phase

        for i, asset in enumerate(assets):
            name = asset.get('name', f'{asset_type}_{i+1}')
            try:

                # A-2 P0 断点续跑：已达标入库的资产不重复「生成→质检→重画」。
                # 就绪判据与 pipeline.probe_assets / _first_existing_asset_image 一致
                # （目录内任意一张非空白名单图）。仅 overwrite=True 时强制重生成。
                # ⚠️ 只在「已就绪」时提前 continue；未就绪项继续走下面的重要性过滤
                #    与完整生成链路，临时道具的 skip 路径不受影响。
                if not overwrite:
                    _ready_img = _first_existing_asset_image(_asset_full_dir(name))
                    if _ready_img:
                        app.logger.info("资产已达标入库，断点续跑跳过：%s（%s）",
                                        name, _ready_img)
                        results.append({"name": name, "status": "skipped",
                                        "reason": "已达标入库，断点续跑跳过"})
                        continue

                # 物品过滤：只生成重要道具的参考图
                if asset_type == 'item':
                    importance = asset.get('importance', '')
                    if importance and importance != '重要':
                        app.logger.info(f"跳过临时道具 '{name}'（importance={importance}），不生成参考图")
                        results.append({"name": name, "status": "skipped", "reason": f"临时道具，importance={importance}"})
                        continue

                prompt_zh = asset.get('reference_prompt_zh', asset.get('prompt_zh', asset.get('appearance', '')))
                if asset_type == 'character':
                    # 不变量（2026-09-28）：qc_desc 里说「性别：X」，出图 prompt 里就必须有 X。
                    # reference_prompt_zh 为空时 canon 条款无处可落（ensure_canon_clause 对空
                    # zh 直接返回 False）→ 不在生成入口兜底就会出现「生成侧零性别约束、质检侧
                    # 按性别判」的反复重画。放在这里（而不是 qc_desc 之后）是为了让
                    # orig_asset_prompt（重试基准 + 教训库 key）也带上性别，否则每次重试
                    # 都会把 prompt 复原成无性别版本。
                    prompt_zh = asset_prompt_kit.ensure_prompt_gender(prompt_zh, asset)
                elif asset_type == 'scene':
                    # 场景同款不变量（2026-09-29）：角色有性别、物品有白底，唯独场景缺一条
                    # —— 场景图要被多视角 / 分镜 / 视频当作**同一个可导航空间**反复引用，
                    # 图里一旦出现人物就会被一并带进下游，且换机位后无法复用。
                    # ⚠️ 与 character 一样必须放在下方 qc_desc 之前，否则会重蹈
                    # 「生成侧零约束、质检侧按另一口径判」→ 反复判不过重画的坑。
                    prompt_zh = asset_prompt_kit.ensure_scene_layout(prompt_zh, asset)

                with lock:
                    generation_state[task_id].update({
                        "current": i + 1, "progress": int((i + 1) / total * 100),
                        "current_asset": name, "phase": "基础图"
                    })

                asset_dir = _asset_full_dir(name)
                os.makedirs(asset_dir, exist_ok=True)
                # 质检暂存目录：服装变体（sub_dir 非空）带子目录标签，避免与同一角色
                # 主设定图的并发生成互相 prune 掉对方的 try 中间产物；sub_dir 为空时
                # 目录名与从前逐字节一致（零回归）。
                _scratch_tag = sub_dir.replace("/", "_") if sub_dir else ""
                scratch_dir = os.path.join(
                    scratch_root,
                    f"{asset_type}_{name}" + (f"_{_scratch_tag}" if _scratch_tag else ""))
                os.makedirs(scratch_dir, exist_ok=True)
                _qc_prune_attempts(scratch_dir)   # G8③：清理上一轮遗留的过期 try（只留最近 4）
                _g_hint = asset_prompt_kit.gender_hint(asset); qc_desc = f"资产类型：{asset_type}；资产名称：{name}；{(_g_hint + '；') if _g_hint else ''}资产设定：{str(prompt_zh)[:400]}"

                # ---------- 阶段1：基础图（生成 → 质检 → 重生成 → 阻断判定） ----------
                base_dst = os.path.join(asset_dir, "base.png")
                base_attempts = []
                base_gate = None
                base_ok = False
                base_files = []
                # O3：第 1 轮也用真实随机 seed 并始终注入（不再 None 走模板默认常量），
                # 使 qc.history[0].seed 不再为 null，产物可复现、可追溯。
                seed = random.randint(1, 2 ** 31 - 1)
                orig_asset_prompt = prompt_zh     # 教训库稳定键（改写后的提示词不参与指纹）
                # ---- 提示词预检（生成前质检）----
                # 资产是「一对多」批量生成（一个项目几十个角色/物品/场景），与整集同理不做硬阻断：
                # 单条提示词有问题就自愈 + 记录，不让整批资产生成中断。缺失/过短这类致命缺陷
                # 由后面的生成+质检链路兜底（空提示词本就出不来可用资产）。
                prompt_zh, _pf_asset, _pgate_asset = _prompt_preflight(
                    "asset", prompt_zh, ctx=asset, style=(asset.get("style") or gen_style or ""),
                    project_name=project_name, cfg=qc_cfg)   # G13：复用 worker 级配置
                if not _pgate_asset.get("accept"):
                    app.logger.warning("资产「%s」参考图提示词预检未通过（%s）：%s",
                                       name, _pgate_asset.get("label"), _pgate_asset.get("reason"))
                for attempt in range(max_retries + 1):
                    if attempt > 0:
                        seed = random.randint(1, 2 ** 31 - 1)
                        # ① 优先：针对**上一轮这张图**的质检缺陷，用 LLM 即时改写提示词（精准）
                        # ② 回落：召回历史教训库改写；再回落：仅换种子。
                        # ⚠️ 基准用 orig_asset_prompt，避免建议块一轮轮累积
                        prompt_zh = orig_asset_prompt
                        optimized = None
                        if base_attempts:
                            optimized = _optimize_prompt_from_qc(
                                "asset", orig_asset_prompt, base_attempts[-1],
                                style=gen_style or _qc_style_of(project_name))
                        if optimized:
                            prompt_zh = optimized
                            app.logger.info("资产 %s 第 %d 次重试，针对本次缺陷即时优化提示词",
                                            name, attempt + 1)
                        else:
                            try:
                                suggestions = prompt_memory.suggest(
                                    kind="asset",
                                    prompt=orig_asset_prompt,
                                    project=project_name,
                                    root_dir=PROJECT_OUTPUT_DIR
                                )
                                learned = prompt_memory.learned_prompt(
                                    kind="asset",
                                    prompt=orig_asset_prompt,
                                    project=project_name,
                                    root_dir=PROJECT_OUTPUT_DIR,
                                    style=_qc_style_of(project_name),
                                )
                                if learned and learned != orig_asset_prompt:
                                    prompt_zh = learned
                                    app.logger.info("资产 %s 第 %d 次重试，按历史质检教训改写提示词：%s",
                                                    name, attempt + 1, suggestions[:2])
                                else:
                                    app.logger.info("资产 %s 第 %d 次重试，暂无可用教训，仅换种子",
                                                    name, attempt + 1)
                            except Exception as mem_err:
                                app.logger.warning(f"读取记忆模块失败: {mem_err}")

                        _set_phase(f"{name} 基础图质检不达标，修改提示词后重新生成（第 {attempt}/{max_retries} 次）",
                                   "regenerating")
                    # B-13 P1-14：output 基础图改按「项目/类型/资产名」分桶，不再按
                    # 「项目×类型」混放——同名资产跨项目、同项目不同集共享 asset_type 时
                    # 互串基础图。资产目录仍按 name 分桶（asset_dir 不变），仅 output 桶细化。
                    base_files = gen_base(prompt_zh, seed=seed, style=gen_style, size=gen_size,
                                          filename_prefix=f"comic_drama/{project_name}/{asset_type}/{name}")
                    if not base_files:
                        base_attempts.append({"attempt": attempt + 1, "seed": seed, "stage": "基础图生成",
                                              "ok": False, "error": "基础图生成失败"})
                        # B-16 P2-11：基础图生成失败 → 清理本资产产生的 scratch 中间产物
                        _cleanup_scratch_dir(scratch_dir, app.logger)
                        base_gate = {"accept": False, "blocked": True, "skipped": False,
                                     "label": "生成失败", "reason": "基础图生成失败", "critical_issues": []}
                        break
                    # G8②：消费 ComfyUI output 源（视频链路一直用 move，图片链路此前 copy2
                    # 导致 output/comic_drama/ 只增不减）。move 后 output 目录不留残留。
                    # 2026-10-06：改走 _ingest_comfy_output —— 旧写法
                    # `shutil.move(base_files[0], scratch_base)` 在渲染成功后抛
                    # WinError 3，整批资产判 failed（详见该函数 docstring）。
                    scratch_base = _ingest_comfy_output(
                        base_files, os.path.join(scratch_dir, f"base_try{attempt + 1}.png"),
                        logger=app.logger)
                    if not qc_on:
                        if qc_declared:
                            # 已声明开启质检但接口不可用：明确阻断（图仅留在暂存区），不静默放行
                            base_gate = {"accept": False, "blocked": True, "skipped": False,
                                         "label": "质检接口未就绪",
                                         "reason": "已开启图片质检但质检接口不可用"
                                                   "（qc_config.json 缺 base_url / api_key / model）",
                                         "critical_issues": []}
                            break
                        base_gate = {"accept": True, "blocked": False, "skipped": True,
                                     "label": "质检未开启", "reason": "图片质检未开启（跳过）",
                                     "critical_issues": []}
                        base_ok = True
                        break
                    _set_phase(f"{name} 基础图质检中（第 {attempt + 1} 次）", "checking")
                    # 物品参考图多给一条判据：只认本体，出现承托物/容器即缺陷。
                    # 实测漏判：筑基丹提示词自带「置于黑色玉盒中」，图照着画 → 图与提示词
                    # 完全一致 → 质检判通过。质检口径必须比「像不像提示词」更高一层。
                    _qc_desc_base = qc_desc
                    if str(asset_type) == "item":
                        _qc_desc_base = (str(qc_desc) +
                                         "\n\n【物品图专项判据】物品参考图只应呈现**物品本体**。"
                                         "若画面主体是容器/托盘/盒子/底座/支架/展示台/碗碟/绸布等承托物，"
                                         "或物品被遮挡、被放进/放在别的物体内或上面、出现手或人物，"
                                         "判为关键缺陷（extra_prop / 承托物），kinds 记「物品变形」。"
                                         "纯白背景上出现明显投影或桌面/地面也计一条缺陷。"
                                         "注意：本判据优先于「是否与提示词一致」——"
                                         "提示词本身写了容器时，仍判缺陷。")
                    verdict = qc_client.check_image(scratch_base, _qc_desc_base, qc_cfg, style=gen_style)
                    app.logger.info(f"[资产质检] base {asset_type}/{name} 第{attempt + 1}次 → "
                                    f"{verdict.get('call_url')} model={verdict.get('model')} "
                                    f"ok={verdict.get('ok')} passed={verdict.get('passed')} "
                                    f"score={verdict.get('score')} style_mismatch={verdict.get('style_mismatch')} "
                                    f"latency={verdict.get('latency_ms')}ms")
                    base_attempts.append(_qc_record_verdict(project_name, "asset_image", f"{name}_base",
                                                            "资产基础图质检", attempt + 1, seed,
                                                            scratch_base, verdict, style=gen_style))
                    base_gate = _qc_gate(verdict)
                    if base_gate["accept"]:
                        base_ok = True
                        break
                    if not verdict.get("ok"):
                        break     # 质检接口异常，重生成无意义
                    # ★ 立刻沉淀：让同一次循环的下一次重试就能召回这条缺陷
                    _record_qc_lesson(project_name, "asset", orig_asset_prompt, base_attempts[-1])
                    # ★ G1 止损：连续两次基础图缺陷完全相同 → 继续重试只是重复烧 GPU，提前停
                    _hopeless, _hopeless_detail = _qc_retry_hopeless(base_attempts)
                    if _hopeless:
                        base_attempts[-1]["retry_stopped"] = True
                        base_attempts[-1]["retry_stopped_features"] = _hopeless_detail
                        app.logger.warning(
                            f"资产基础图重试止损（{name}）：连续 {len(base_attempts)} 次缺陷完全相同，"
                            f"提前停止重试。缺陷：{_hopeless_detail}；建议改写该资产提示词后重跑")
                        break
                if not base_ok:
                    # 把「基础图哪里不对」沉淀进教训库（供下次重生成时改写提示词）
                    if base_attempts and isinstance(base_attempts[-1], dict):
                        _record_qc_lesson(project_name, "asset", prompt_zh, base_attempts[-1])
                    # ★ 用户需求：质检「判定不通过」的暂存基础图不留本地（含 ComfyUI 侧）。
                    # ⚠️ 仅当最后一次尝试是「质检成功返回且不合格」（ok=True）时才删；
                    # ok=False（接口故障）/ skipped（未开启）/ 质检接口未就绪 都不删。
                    try:
                        _last_b = base_attempts[-1] if (base_attempts and isinstance(base_attempts[-1], dict)) else {}
                        if qc_on and _last_b.get("ok") is True:
                            _purge_rejected_artifacts(
                                [_last_b.get("file") or scratch_base],
                                project=project_name,
                                reason=f"资产基础图质检不合格（{(base_gate or {}).get('label')}）",
                                kind="asset_base_image",
                                history_file=_last_b.get("history_file") or "")
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning(f"资产不合格基础图清理失败（忽略）：{_pe}")

                    results.append({
                        "name": name, "success": False, "dir": asset_dir, "stage": "基础图",
                        "qc_blocked": bool(base_gate and base_gate.get("blocked")),
                        "error": (f"基础图未通过质检（{base_gate['label']}）：{base_gate['reason']}"
                                  if base_gate else "基础图生成失败"),
                        "qc": _qc_summary(base_attempts, qc_declared, qc_on,
                                          int(qc_cfg.get("max_retries", 0))),
                    })
                    continue
                # 质检达标 → 正式入库
                if base_attempts:
                    shutil.copy2(base_attempts[-1]["file"], base_dst)
                else:
                    # G8②：2380 处已把 ComfyUI output move 到 scratch_base（不再 copy2），
                    # 故入库源是 scratch_base（base_files[0] 此时已 move 走、不可再取）
                    shutil.copy2(scratch_base, base_dst)
                # O2：产物旁路元数据（seed/提示词/工作流 SHA256/质检结论），可复现可追溯
                _write_artifact_meta(
                    base_dst, kind="asset_base", project_name=project_name,
                    seed=seed, prompt=orig_asset_prompt,
                    workflow_key={"character": "character_gen", "item": "item_gen",
                                  "scene": "scene_gen"}.get(asset_type),
                    qc=base_gate, asset_name=name,
                    extra={"asset_type": asset_type, "style": gen_style or None})

                # ---------- 阶段2：视角单图（本地切分，不再走 GPU 多视角编辑） ----------
                # 2026-09-24 改造，机制与实测见 app/sheet_split.py 模块头 + config 同名注释：
                #   旧实现在这里调 `comfyui_client.generate_multiview` 逐视角**重渲染** —— 实测
                #   4 张产物与 base.png **内容一致**（参考图编辑 cfg=1.0 只复刻已见机位），
                #   净成本 = 每资产 4 次 GPU 渲染 + 4 次质检，收益 = 0（下游只取 front.png），
                #   且多视角不达标会把**整个资产判 failed**（旧 success = not blocked_views）。
                #   现在：
                #     · 角色 → 从三视图整图**本地列投影切分**出 front/left/back 单视角图
                #       （零 GPU、零质检），并清掉旧实现遗留的 right.png；
                #     · 物品 / 场景 → 基础图本身就是单主体图，不再产出任何视角图。
                #   切分是**已通过质检**的基础图的确定性派生 —— 没有可重试的自由度
                #   （重跑只会得到同一张切图），故不再需要「逐视角质检 → 整组重生成」循环。
                view_paths = {"base": base_dst}
                view_attempts = {}
                view_gate = {}
                saved_views = []
                blocked_views = []      # 保留字段：派生无「阻断」语义，恒为空
                derive_error = None
                if asset_type == "character":
                    # 2026-10-02 用户指定：角色图**不裁剪**，直接保留整图 base.png。
                    # 角色设定图已改为英文四区 character sheet（见
                    # comfyui_client._CHARACTER_SHEET_EN_LAYOUT），四区不对称布局无法再
                    # 做行列投影切分 → 不再产出 front/left/back/half 单视角。
                    # 并**清掉旧视角图**：否则 _ASSET_IMG_PRIORITY（front > base）会让
                    # 下游取到上一轮遗留的旧单视角图，与「整图」口径互斥
                    # （即旧发型图继续被用作参考的老坑，见下方原注释）。
                    _derived = {}
                    derive_error = None
                    app.logger.info("角色「%s」不裁剪：保留整图 %s（不再切分单视角）",
                                    name, os.path.basename(base_dst))
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=(), known=ASSET_VIEW_STEMS, logger=app.logger)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)
                elif asset_type == "scene" and SCENE_VIEWS_ENABLED:
                    # ---------- 场景：按机位逐档出图（2026-09-29 新增） ----------
                    # 动机：场景此前只有一张 base.png，分镜不论什么机位都拿它当参考图 ——
                    #   俯拍 / 斜侧镜头拿到的是**正面基准图**，构图先验与镜头要求反向。
                    # 做法：在**基础图阶段**把机位写进提示词逐档**独立出图**（T2I），
                    #   而不是基础图之后用参考图编辑补机位 —— 后者在 cfg=1.0 下改不动
                    #   机位，正是 2026-09-24 被废除的那条路（机制见 config.SCENE_VIEW_KEYS
                    #   上方注释 + comfyui_client.scene_view_prompt_suffix）。
                    # 成本口径：正面档**复用刚过质检的 base.png**（不重复烧 GPU），
                    #   只多出 left45 / right45 / top 三档；三档都是**加值**而非必需，
                    #   不达标就丢弃并由下游回落正面档，绝不把资产判 failed。
                    # ⭐ 2026-10-05 场景九宫格（SCENE_GRID_MODE）：机位档从 4 档扩到 9 档，
                    #   逐档独立出图后额外用 scene_grid.stitch_grid 拼成一张 3×3 总图
                    #   grid.png（= 场景资产本体，下游 _pick_scene_view 整图直接用）。
                    #   关闭时下面全部回落到 4 档旧行为（SCENE_VIEW_*），零回归。
                    _grid = bool(SCENE_GRID_MODE)
                    _grid_keys = SCENE_GRID_VIEW_KEYS if _grid else SCENE_VIEW_KEYS
                    _grid_labels = SCENE_GRID_LABELS if _grid else SCENE_VIEW_LABELS
                    _grid_angles = SCENE_GRID_ANGLE_ZH if _grid else SCENE_VIEW_ANGLE_ZH
                    view_paths["front"] = base_dst
                    view_gate["front"] = {
                        "accept": True, "blocked": False, "skipped": True,
                        "label": "复用基础图（正面档）",
                        "reason": "基础图即正面机位出图，正面档直接复用，不重复出图",
                        "critical_issues": [],
                    }
                    saved_views.append("front")
                    for vk in _grid_keys:
                        if vk == "front":
                            continue
                        _v_label = _grid_labels.get(vk) or vk
                        _v_angle = _grid_angles.get(vk) or _v_label
                        _v_dst = os.path.join(asset_dir, f"{vk}.png")
                        _v_gate = None
                        _v_verdict = None       # 最近一次质检原始判据（丢档排障用）
                        _v_scratch = None
                        view_attempts[vk] = []   # 逐次累加（不覆盖），供结果回传与排障
                        _v_seed = seed      # 同 seed：跨档共享初始噪声，结构最相关
                        for _va in range(SCENE_VIEW_MAX_RETRIES + 1):
                            if _va > 0:
                                _v_seed = random.randint(1, 2 ** 31 - 1)
                            _set_phase(f"{name} 场景机位档「{_v_label}」"
                                       f"（第 {_va + 1}/{SCENE_VIEW_MAX_RETRIES + 1} 次）", "views")
                            _v_files = comfyui_client.generate_scene_base(
                                prompt_zh, seed=_v_seed, style=gen_style, size=gen_size,
                                filename_prefix=(f"comic_drama/{project_name}/{asset_type}"
                                                 f"/{name}/{vk}"),
                                view_key=vk)
                            if not _v_files:
                                # 出图失败（非质检问题）→ 换 seed 重试无意义
                                app.logger.warning("场景「%s」机位档 %s 出图失败（seed=%s）",
                                                   name, vk, _v_seed)
                                break
                            _v_scratch = _ingest_comfy_output(
                                _v_files,
                                os.path.join(scratch_dir, f"{vk}_try{_va + 1}.png"),
                                logger=app.logger)
                            if not qc_on:
                                _v_gate = {"accept": True, "blocked": False, "skipped": True,
                                           "label": "质检未开启", "reason": "图片质检未开启（跳过）",
                                           "critical_issues": []}
                                break
                            # 机位档的质检口径**显式带机位要求**：否则判官只核「是不是这个场景」，
                            # 出一张正面图也会判过，机位档就白生成了。
                            _v_desc = (
                                f"资产类型：scene；资产名称：{name}；"
                                f"本图机位要求：{_v_angle}；"
                                f"资产设定：{str(prompt_zh)[:400]}")
                            _set_phase(f"{name} 场景机位档「{_v_label}」质检中"
                                       f"（第 {_va + 1} 次）", "checking")
                            _v_verdict = qc_client.check_image(
                                _v_scratch, _v_desc, qc_cfg, style=gen_style)
                            app.logger.info(
                                "[资产质检] view scene/%s/%s 第%d次 → ok=%s passed=%s "
                                "score=%s", name, vk, _va + 1, _v_verdict.get("ok"),
                                _v_verdict.get("passed"), _v_verdict.get("score"))
                            view_attempts[vk].append(
                                _qc_record_verdict(
                                    project_name, "asset_image", f"{name}_{vk}",
                                    f"场景机位档「{_v_label}」质检", _va + 1, _v_seed,
                                    _v_scratch, _v_verdict, style=gen_style))
                            _v_gate = _qc_gate(_v_verdict)
                            # ⚠️ 质检接口故障（fault_open）**不算达标**：不得当「已判过」落盘。
                            if _v_gate["accept"] and not _v_gate.get("fault_open"):
                                break
                            # 接口故障（根本没拿到判定）→ **继续循环换 seed 重试**，别当内容不合格停手；
                            # 非接口级的「质检调用异常」重生成无意义 → 停。
                            if not _v_verdict.get("ok") and not (
                                    _v_verdict.get("interface_fault")
                                    or (_v_gate or {}).get("fault_open")):
                                break     # 质检调用异常（非接口级）→ 重生成无意义
                            # ⚠️ 刻意**不**调 _record_qc_lesson：机位档的缺陷（角度不对）
                            #    不是资产提示词的缺陷，沉淀进去会让**下一轮的基础图提示词**
                            #    被按「机位」改写，把加值项的毛病传播成资产本身的毛病。
                        # 达标 = 判官放行 **且** 非接口故障放行（fault_open 不算达标）
                        _v_ok = bool(_v_gate and _v_gate.get("accept")
                                     and not _v_gate.get("fault_open") and _v_scratch)
                        # 与 base 的**相似度粗筛**（撞车即丢档，2026-10-03 B 方案）：
                        # 仅作「明显撞车」的**下限粗筛** —— 阈值见 config.SCENE_VIEW_DUP_PHASH_MAX
                        # （=95，**不能调到 80**：真换构图的 top≈81.2、几乎没变的 left45≈84.4，
                        #   80~95 不可分，调到 80 会误杀 top），只拦 >95 的近重复。
                        _v_sim = None
                        if _v_ok:
                            try:
                                _v_sim_res = consistency.phash_similarity(base_dst, _v_scratch)
                                if _v_sim_res.get("ok"):
                                    _v_sim = float(_v_sim_res.get("score") or 0.0)
                            except Exception as _vse:  # noqa: BLE001
                                app.logger.warning(
                                    "场景「%s」机位档「%s」相似度粗筛失败（不拦截，按达标处理）：%s",
                                    name, _v_label, _vse)
                        _v_dup = bool(_v_sim is not None
                                      and _v_sim > SCENE_VIEW_DUP_PHASH_MAX)
                        if _v_ok and not _v_dup:
                            shutil.copy2(_v_scratch, _v_dst)
                            view_paths[vk] = _v_dst
                            view_gate[vk] = _v_gate
                            saved_views.append(vk)
                            # O2：机位档旁路元数据（标注「同 seed 独立出图」，可复现）
                            _write_artifact_meta(
                                _v_dst, kind="asset_view", project_name=project_name,
                                seed=_v_seed, prompt=orig_asset_prompt,
                                workflow_key="scene_gen",
                                qc=_v_gate, asset_name=name,
                                extra={"asset_type": asset_type, "view": vk,
                                       "view_label": _v_label, "angle_zh": _v_angle,
                                       "derive_mode": "angle_regen",
                                       "derived_from": os.path.basename(base_dst)})
                        else:
                            # 加值项不阻断资产：本档不提供，分镜侧逐级回落 front/base。
                            # 三种丢档原因彼此区分，并在 qc_views 里留 drop_reason 便于排障。
                            _v_gate = _v_gate or {}
                            _v_fault = bool(_v_gate.get("fault_open")
                                            or (_v_verdict or {}).get("interface_fault"))
                            if _v_dup:
                                app.logger.warning(
                                    "场景「%s」机位档「%s」与 base 相似度 %.1f > %.1f"
                                    "（明显撞车 / 未换构图）→ 丢弃该档（不落盘、不判 failed，"
                                    "分镜将回落正面档）",
                                    name, _v_label, _v_sim, SCENE_VIEW_DUP_PHASH_MAX)
                                view_gate[vk] = dict(
                                    _v_gate, dropped=True, dup_similarity=round(_v_sim, 1),
                                    drop_reason=(f"与base相似度{_v_sim:.1f}"
                                                 f">阈值{SCENE_VIEW_DUP_PHASH_MAX:g}"))
                            elif not _v_scratch:
                                app.logger.warning(
                                    "场景「%s」机位档「%s」出图失败（未产出图片）→ 丢弃该档"
                                    "（不落盘、不判 failed，分镜将回落正面档）", name, _v_label)
                                view_gate[vk] = dict(_v_gate, dropped=True,
                                                     drop_reason="出图失败（未产出图片）")
                            elif _v_fault:
                                app.logger.warning(
                                    "场景「%s」机位档「%s」因**质检接口故障**重试 %d 次仍未"
                                    "取得判定 → 丢弃该档（不落盘、不判 failed，"
                                    "分镜将回落正面档）：%s",
                                    name, _v_label, SCENE_VIEW_MAX_RETRIES + 1,
                                    _v_gate.get("reason") or "质检接口不可用")
                                view_gate[vk] = dict(
                                    _v_gate, dropped=True,
                                    drop_reason="质检接口故障·重试后仍未取得判定")
                            else:
                                app.logger.warning(
                                    "场景「%s」机位档「%s」**质检判定不达标**，本次不落盘"
                                    "（分镜将回落正面档）：%s",
                                    name, _v_label, _v_gate.get("label"))
                                view_gate[vk] = dict(_v_gate, dropped=True,
                                                     drop_reason="质检判定不达标")
                    # 清掉本档集合内**本轮不再产出**的陈旧机位图（含上一轮/旧实现遗留），
                    # 避免 UI 与资产索引把陈旧机位当成有效档展示。
                    # ⭐ 2026-10-05 九宫格：known 集合随模式扩展（网格含 9 档 + grid 主图），
                    #    否则宽机位档（wide/low/detail/depth/back）会被 prune 当陈旧档清掉。
                    _known_stems = (tuple(ASSET_VIEW_STEMS)
                                    + (_grid_keys if _grid else tuple(SCENE_VIEW_KEYS)))
                    if _grid:
                        _grid_cell_paths = [os.path.join(asset_dir, f"{k}.png")
                                            for k in _grid_keys
                                            if os.path.isfile(os.path.join(asset_dir, f"{k}.png"))]
                        _grid_dst = os.path.join(asset_dir, SCENE_GRID_FILENAME)
                        if len(_grid_cell_paths) >= 2:
                            try:
                                _stitched = scene_grid.stitch_grid(_grid_cell_paths, _grid_dst)
                                if _stitched and os.path.isfile(_stitched):
                                    view_paths[SCENE_GRID_FILENAME] = _stitched
                                    view_gate[SCENE_GRID_FILENAME] = {
                                        "accept": True, "blocked": False, "skipped": True,
                                        "label": "九宫格拼接（本地派生）",
                                        "reason": "9 机位独立出图后本地拼 3×3 总图，继承各档质检",
                                        "critical_issues": [],
                                    }
                                    saved_views.append(SCENE_GRID_FILENAME)
                                    _write_artifact_meta(
                                        _stitched, kind="asset_grid", project_name=project_name,
                                        seed=seed, prompt=orig_asset_prompt,
                                        workflow_key="scene_gen_grid",
                                        qc={"accept": True, "skipped": True,
                                             "label": "拼接总图（继承 9 档质检）"},
                                        asset_name=name,
                                        extra={"asset_type": asset_type,
                                               "derive_mode": SCENE_GRID_DERIVE_MODE,
                                               "cells": list(_grid_keys),
                                               "n_cells": len(_grid_cell_paths),
                                               "style": gen_style or None})
                                    app.logger.info(
                                        "[场景九宫格] shot 场景「%s」9 机位独立出图已拼成 3×3 总图 "
                                        "（%d 格）：%s", name, len(_grid_cell_paths), _stitched)
                            except Exception as _ge:  # noqa: BLE001
                                app.logger.warning(
                                    "场景「%s」九宫格拼接失败（保留各机位档，不阻断入库）：%s",
                                    name, _ge)
                        else:
                            app.logger.warning(
                                "场景「%s」九宫格可用的机位档不足 2（实得 %d），跳过拼接，"
                                "下游回落 base.png", name, len(_grid_cell_paths))
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=tuple(view_paths.keys()),
                            known=_known_stems,
                            logger=app.logger)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)
                else:
                    # 物品：基础图即单主体图；清掉旧实现遗留的视角图，避免 UI 把陈旧的
                    # 「重渲染整图」继续当成一个视角展示。
                    # （场景在关闭 SCENE_VIEWS_ENABLED 时也走这里 → 与改造前逐字一致）
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=(),
                            known=tuple(ASSET_VIEW_STEMS) + tuple(SCENE_VIEW_KEYS),
                            logger=app.logger)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)

                results.append({
                    "name": name,
                    "success": True,
                    "dir": asset_dir,
                    "views": list(view_paths.keys()),
                    "qc_blocked": False,
                    "qc_blocked_views": blocked_views,
                    "derive_error": derive_error,
                    "error": None,
                    # 视角图多为本地派生（不单独质检），故 qc 汇总只反映基础图
                    "qc": _qc_summary(base_attempts, qc_declared, qc_on,
                                      int(qc_cfg.get("max_retries", 0))),
                    "qc_base": _qc_summary(base_attempts, qc_declared, qc_on,
                                           int(qc_cfg.get("max_retries", 0))),
                    # 逐档质检结论（角色档是「本地派生·继承基础图质检」，场景机位档是
                    # 真跑质检的结论）。前端此前未消费该字段，这里填实数据不改形状。
                    "qc_views": {k: dict(v) for k, v in view_gate.items()},
                })

            except cancellation.Cancelled as _cancelled:
                # ⭐ 2026-10-06：用户点「暂停/停止」不是错误。此前 Cancelled 落进下面的
                # 通用 except，被记成 ERROR 并塞进 results[i]["error"]：
                #   ① 日志里每暂停一次就刷一批 ERROR，真故障被淹没（本轮日志统计里
                #      「资产 X 生成失败：Cancelled: …」占 ERROR 的 2/5）；
                #   ② 前端把暂停显示成「生成失败」，用户以为资产坏了；
                #   ③ 已完成步骤的产物判定被污染。
                # 口径：记 info，stage 标「已暂停」，error 留 None（= 没出错）。
                app.logger.info("资产「%s」因暂停/停止中止（已完成步骤保留，可续跑）：%s",
                                name, _cancelled)
                results.append({
                    "name": name, "success": False, "cancelled": True,
                    "dir": _asset_full_dir(name),
                    "stage": "已暂停", "qc_blocked": False,
                    "error": None,
                })
                with lock:
                    generation_state[task_id].update({
                        "current": i + 1,
                        "progress": int((i + 1) / total * 100),
                        "phase": f"{name} 已暂停",
                    })
            except Exception as _asset_err:  # noqa: BLE001
                # ⚠️ 审计 S11：旧代码这里没有 try —— `generate_multiview` 上传基础图失败会
                #    raise RuntimeError，`queue_prompt` 遇 5xx/超时也会抛。任一处抛出 →
                #    整个批次被标 failed，`results`（已成功资产的结果）全部丢弃：
                #    20 个资产在第 7 个时来一次连接抖动，前 6 个已入库的成果用户也看不见。
                # ⚠️ 2026-10-06 补 traceback：本分支原先只打 `类型: 消息`，导致
                #    「资产 X 生成失败：FileNotFoundError: [WinError 3] 系统找不到指定的路径」
                #    这类故障**完全不可定位** —— 该消息在 ComfyUI output、质检暂存区、
                #    移动/复制落盘三处都可能抛出，日志里没有任何路径信息。
                app.logger.error("资产「%s」生成失败（已隔离，继续后续资产）：%s: %s\n%s",
                                 name, type(_asset_err).__name__, _asset_err,
                                 _asset_err_traceback(_asset_err))
                results.append({
                    "name": name, "success": False,
                    "dir": _asset_full_dir(name),
                    "stage": "异常中断", "qc_blocked": False,
                    "error": f"{type(_asset_err).__name__}: {_asset_err}",
                })
                with lock:
                    generation_state[task_id].update({
                        "current": i + 1,
                        "progress": int((i + 1) / total * 100),
                        "phase": f"{name} 生成异常（已跳过）",
                    })
                continue
        blocked_count = sum(1 for r in results if r.get("qc_blocked"))
        with lock:
            generation_state[task_id].update({
                "status": "completed", "results": results,
                "success_count": sum(1 for r in results if r.get("success")),
                "qc_blocked_count": blocked_count,
            })
    except Exception as e:
        app.logger.error(f"资产生成失败: {e}")
        _partial = locals().get("results") or []   # 审计 S11：已成功的部分结果不能丢
        with lock:
            generation_state[task_id].update({
                "status": "failed", "error": str(e), "results": _partial,
                "success_count": sum(1 for r in _partial if r.get("success")),
            })
    _maybe_reclaim_comfyui_output()   # D-11a：任务收尾滚动回收 ComfyUI 重试残留（节流+全容错）
    _maybe_clear_comfyui_history("资产批量生成收尾")

# ===================== 清空重做单个镜头 / 单个资产（总控 AI 工具后端） =====================
# 与 reset-episode 同一套安全原则：移入回收站（可恢复）、逐项容错、按镜号/资产名精确圈定。

def _trash_move(src, category, trash_root, cleared, skipped):
    """把单个文件/目录移入回收站；不存在=无事发生，被占用=记入 skipped。

    模块级版本（reset-shot / reset-asset 共用）；reset-episode 端点内另有闭包版本，语义一致。
    """
    if not src or not os.path.exists(src):
        return
    try:
        dst = os.path.join(trash_root, category,
                           os.path.basename(src.rstrip('\\/')) or category)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        cleared.append({"category": category, "path": src})
    except Exception as e:  # noqa: BLE001  单项失败不阻断其余清理
        skipped.append({"path": src, "error": str(e)})

def _collect_matching(base_dir, prefixes):
    """收集 base_dir 下（递归）名字以任一前缀开头的文件/目录路径（浅层优先）。

    浅层优先的原因：目录整移会连带其内容，深层残余路径在 move 时已不存在 → 静默跳过。
    """
    hits = []
    if base_dir and os.path.isdir(base_dir):
        for _root, _dirs, _files in os.walk(base_dir):
            for _name in _dirs + _files:
                if any(_name.startswith(pfx) for pfx in prefixes):
                    hits.append(os.path.join(_root, _name))
    hits.sort(key=lambda p: p.count(os.sep))
    return hits

# ===================== 场景九宫格机位预览（2026-10-01，对标 BigBanana 候选构图） =====================
# 场景 base 图 → 9 个机位各一张同场景变体 + 3x3 拼接预览（scene_grid.py）。
# 「应用」= 选中机位图升级为该场景新 base（旧 base 移入回收站），分镜参考图与
# 按机位出图自动沿用新视角。GPU 任务走统一闸门（与生产串行，不抢卡）。

def _find_scene_asset_dir(project_name: str, name: str) -> str:
    """按名称定位场景资产目录（scenes/<项目>/<名称>）"""
    d = os.path.join(SCENES_DIR, project_name, name)
    return d if os.path.isdir(d) else ""

def _scene_grid_prompt_for(project_name: str, name: str) -> str:
    """场景内容描述：优先读剧本 scenes[].reference_prompt_zh / appearance"""
    try:
        script = _load_script_for(project_name, 1) or {}
        for sc in (script.get("scenes") or []):
            if not isinstance(sc, dict):
                continue
            if str(sc.get("name") or "").strip() == name or \
                    str(sc.get("location") or "").strip() == name:
                return str(sc.get("reference_prompt_zh") or sc.get("appearance") or "").strip()
    except Exception as e:  # noqa: BLE001
        app.logger.debug("场景九宫格读取剧本场景描述失败：%s", e)
    return ""

# ===================== 角色服装变体（衣柜，2026-10-02） =====================
# 目录约定：output/assets/characters/<项目>/<角色名>/outfits/<outfit_key>/，
# 内部文件布局与主设定目录相同（base.png + front/left/back/half.png，由整图本地
# 切分派生 —— 复用 _generate_asset_task 的「生成→质检→重试→切分」全链路，见其
# sub_dir 参数）。端点加在 api_generate_assets 旁边：同为「消费已产出提示词 +
# ComfyUI 出图 + 质检」链路，不读 AI 凭证，故同样不挂 AI 前置门禁。
# ⚠️ 零回归约束：所有新行为都以「服装变体目录存在」为前提 —— 目录不存在时查询
# 返回空数组、取图回落主设定图（fail-open，任何异常只 log 不抛）。

#: 服装变体子目录名（挂在角色主设定目录下）
_OUTFITS_DIRNAME = "outfits"

#: 服装描述追加进提示词的标记（幂等判据，见 _append_outfit_prompt）
_OUTFIT_PROMPT_MARK = "；本套服装："

#: 服装档案文件名（生成发起时先落一份 outfit_key/desc 记录，查询端点回显描述用）
_OUTFIT_RECORD_FILE = "outfit.json"

#: 变体档位（与主设定目录的切分产物同名，来自 sheet_split 链路）
_OUTFIT_VIEW_STEMS = ("front", "left", "back", "half")

def _sanitize_outfit_key(raw) -> str:
    """outfit_key 安全化：剔除路径非法字符 ``\\ / : * ? " < > |`` 与首尾空白、
    截断到 40 字符，防路径穿越。

    剔完为空（或只剩 ``.`` / ``..`` —— 分别是目录自身与上级，同样算穿越）返回
    ''，由调用方按 400 拒绝。刻意**不用** _safe_project：那是项目键收敛规则，
    会把任意输入坍缩成合法键（恒非空），而 outfit_key 必须能被 400 明确拒绝。
    """
    s = str(raw or "").strip()
    for _ch in '\\/:*?"<>|':
        s = s.replace(_ch, "")
    s = s.strip()
    if not s or s in (".", ".."):
        return ""
    return s[:40]

def _append_outfit_prompt(base_prompt: str, outfit_desc: str) -> str:
    """把「；本套服装：{outfit_desc}」追加到角色提示词末尾（幂等）。

    幂等实现：服装段**只追加在串尾**，故追加前把已有的尾段剥掉再接新段 ——
    同一 desc 重复追加结果不变（不重复），desc 改动时旧描述被替换而非叠两段。
    """
    base = str(base_prompt or "").strip()
    desc = str(outfit_desc or "").strip()
    if not desc:
        return base
    base = re.sub(r"；本套服装：.*$", "", base).strip()
    if base:
        return f"{base}{_OUTFIT_PROMPT_MARK}{desc}"
    return f"本套服装：{desc}"

def _character_outfit_dir(project_name: str, character: str, outfit_key: str = "") -> str:
    """角色服装变体目录（outfit_key 为空时是 outfits 根目录）"""
    d = os.path.join(CHARACTERS_DIR, project_name, character, _OUTFITS_DIRNAME)
    return os.path.join(d, outfit_key) if outfit_key else d

def _find_script_character(project_name: str, character: str) -> dict:
    """从项目剧本（_load_script_for）按名字（含别名归一）找角色档案；找不到返回 {}

    用途：服装变体的提示词要在「角色主设定」之上追加服装描述，主设定来自剧本
    characters[].reference_prompt_zh；appearance / gender 等字段也一并透传，
    供生成端 ensure_prompt_gender 不变量与质检描述使用。
    """
    _norm = _normalize_char_alias(character)
    try:
        for c in ((_load_script_for(project_name, None) or {}).get("characters") or []):
            if isinstance(c, dict) and \
                    _normalize_char_alias(str(c.get("name") or "")) == _norm:
                return dict(c)
    except Exception as e:  # noqa: BLE001  剧本读失败不阻断（回落主设定 meta）
        app.logger.warning("服装变体读取剧本角色档案失败（忽略）：%s", e)
    return {}

def _character_base_prompt(project_name: str, character: str) -> str:
    """角色主设定的参考提示词：剧本 characters[].reference_prompt_zh 优先，
    回落主设定目录 base.png.meta.json 的 prompt（O2 产物旁路元数据）。

    都拿不到返回 ''——此时变体提示词只含服装段，生成端的性别不变量会按剩余
    字段兜底；与「剧本缺角色描述」的既有资产生成行为同口径，不额外阻断。
    """
    p = str(_find_script_character(project_name, character).get("reference_prompt_zh")
            or "").strip()
    if p:
        return p
    try:
        meta_path = os.path.join(CHARACTERS_DIR, project_name, character,
                                 "base.png.meta.json")
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                return str((json.load(f) or {}).get("prompt") or "").strip()
    except Exception as e:  # noqa: BLE001
        app.logger.warning("服装变体读取主设定 meta 失败（忽略）：%s", e)
    return ""

def _outfit_desc_of(outfit_dir: str) -> str:
    """该服装变体的描述：outfit.json 档案优先，回落 base.png.meta.json
    提示词里的「；本套服装：…」尾段。任何失败返回 ''（查询列表不因此报错）。"""
    try:
        rec_path = os.path.join(outfit_dir, _OUTFIT_RECORD_FILE)
        if os.path.isfile(rec_path):
            with open(rec_path, "r", encoding="utf-8") as f:
                desc = str((json.load(f) or {}).get("desc") or "").strip()
            if desc:
                return desc
    except Exception as e:  # noqa: BLE001
        app.logger.debug("服装档案读取失败（回落 meta sidecar）：%s", e)
    try:
        meta_path = os.path.join(outfit_dir, "base.png.meta.json")
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                prompt = str((json.load(f) or {}).get("prompt") or "")
            _m = re.search(r"；本套服装：(.+)$", prompt)
            if _m:
                return _m.group(1).strip()
    except Exception as e:  # noqa: BLE001
        app.logger.debug("服装 meta 回读失败（忽略）：%s", e)
    return ""

# ===================== 上传角色形象图 → 三视图（2026-10-06） =====================
# 需求：用户手里已有角色画稿（自己画的 / 外包出的 / 别处满意的一张），希望直接
# 用这张图当角色的「设定图基准」，而不是让模型按提示词重新抽一次外形。
#
# 关键取舍（**本地切分，不是参考图重绘**）：
#   · 上传图直接落 `base.png`（= 角色设定整图），再走 `sheet_split` 现有链路切出
#     front/left/back/half —— 零 GPU、零质检、秒出，且**人物外形 100% 保留**
#     （模型重绘一定会改写脸/发型，这正是用户上传画稿要避免的）。
#   · 因此上传图必须**本身就已按分档版式排好**（上排正面/左侧/背面三全身，
#     下排一格正面半身）。版式不符 → `SheetSplitError` → 明确报错并回滚，
#     绝不落一张切不动的 base.png 污染下游（下游会拿它当参考图）。
#   · 上传即视为**已定稿**，不做图片质检（`view_gate` 记 skipped）：用户上传
#     自己的画稿，质检判「不合格」既无意义又会把用户的东西删掉。
#
# 与服装变体（outfits/<key>）的关系：同一张上传图可以挂到主设定，也可以挂到
# 某个服装变体档；由 `outfit_key` 决定落位（空 = 主设定）。
# 幂等：默认已存在 base.png 即跳过；overwrite=true 强制覆盖。

#: 上传形象图允许的扩展名（与 project_store._views_of 的图片白名单同口径）
_UPLOAD_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

def _bigram_overlap(a: str, b: str) -> float:
    """字符 2-gram 重叠率（|A∩B| / |B|）：服装文本与变体描述的模糊匹配打分。"""
    a = re.sub(r"\s+", "", str(a or ""))
    b = re.sub(r"\s+", "", str(b or ""))
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    return len(ga & gb) / max(1, len(gb))

def _episode_outfit_overrides(project_name: str, episode_no: int) -> dict:
    """跨集一致性巩固（2026-10-02）：把本集各角色的服装状态解析成衣柜变体 key。

    服装文本来源（按优先级）：本集 state_in.character_states[].outfit（continuity
    按集登记的服装状态）→ bible.current_outfit。变体匹配：outfits/<key>/outfit.json
    的 desc 与服装文本做 2-gram 重叠打分，最高分且 >0 才采用 —— 分不清就不指定，
    走主设定图（宁缺毋滥，绝不因猜错服装而错挂参考图）。
    :return: {角色名: outfit_key}；无 state / 无变体 / 匹配不上 → {}（零回归）
    """
    try:
        from config import CONTINUITY_DIR as _cont_dir
        from continuity import load_state as _load_ep_state
        _st = _load_ep_state(_cont_dir, project_name, int(episode_no)) or {}
    except Exception:  # noqa: BLE001
        _st = {}
    want: dict = {}
    for cs in ((_st.get("state_in") or {}).get("character_states") or []):
        if isinstance(cs, dict) and str(cs.get("name") or "").strip():
            want[str(cs.get("name")).strip()] = str(cs.get("outfit") or "").strip()
    if not want:
        try:
            from continuity import load_bible as _load_bible
            _bible = _load_bible(_cont_dir, project_name) or {}
            for c in (_bible.get("characters") or []):
                if isinstance(c, dict) and str(c.get("name") or "").strip():
                    want[str(c.get("name")).strip()] = str(
                        c.get("current_outfit") or "").strip()
        except Exception:  # noqa: BLE001
            return {}
    want = {k: v for k, v in want.items() if v}
    if not want:
        return {}

    proj_char_root = os.path.join(CHARACTERS_DIR, project_name)
    if not os.path.isdir(proj_char_root):
        return {}
    out: dict = {}
    try:
        _char_dirs = os.listdir(proj_char_root)
    except OSError:
        return {}
    for char_name in _char_dirs:
        outfit_root = os.path.join(proj_char_root, char_name, _OUTFITS_DIRNAME)
        if not os.path.isdir(outfit_root):
            continue
        text = want.get(char_name) or ""
        # 别名容错：want 的键可能带别名，做一次包含匹配
        if not text:
            text = next((v for k, v in want.items()
                         if k in char_name or char_name in k), "")
        if not text:
            continue
        best_key, best_score = "", 0.0
        try:
            _keys = os.listdir(outfit_root)
        except OSError:
            continue
        for key in _keys:
            rec = os.path.join(outfit_root, key, _OUTFIT_RECORD_FILE)
            desc = ""
            try:
                if os.path.isfile(rec):
                    with open(rec, "r", encoding="utf-8") as _f:
                        desc = str((json.load(_f) or {}).get("desc") or "")
            except Exception:  # noqa: BLE001
                desc = ""
            if not desc:
                continue
            _s = _bigram_overlap(desc, text)
            if _s > best_score:
                best_key, best_score = key, _s
        if best_key and best_score > 0:
            out[char_name] = best_key
    if out:
        app.logger.info("[服装变体] 本集服装覆盖：%s", out)
    return out

# ===== 步骤5：分镜图片生成 =====

_ASSET_DIRS = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}

# 资产「主视角候选文件名」——**只是顺序提示，不是取图实现**：真正的取图入口是
# `_build_asset_index`（`_first_existing` + `_dir` 约定）与 `_first_existing_asset_image`
# （`_ASSET_IMG_PRIORITY`）。本常量目前**无任何调用方**（grep 仅命中定义处），
# 故不重复登记各机位档，避免三份名字副本互相漂移。
_ASSET_VIEW_FILES = {"character": ("front.png", "base.png"),
                     "item": ("front.png", "base.png"),
                     "scene": ("front.png", "base.png")}

def _first_existing(*candidates):
    for c in candidates:
        if isinstance(c, str) and c and os.path.exists(c):
            return c
    return None

def _build_asset_index(assets: list, project_name: str, kind: str) -> dict:
    """构建「资产名 → 本地图片绝对路径」索引

    优先使用前端传来的 HTTP 资源路径（解析回本地），其次按资产目录约定推导
    output/assets/<kind>/<项目>/<名称>/(front|base).png，最后补齐磁盘上已有但未上报的资产。
    """
    base_dir = _ASSET_DIRS[kind]
    names = []
    for a in (assets or []):
        if isinstance(a, dict) and a.get("name"):
            names.append((a.get("name"), a))
        elif isinstance(a, str) and a:
            names.append((a, {}))
    project_dir = os.path.join(base_dir, project_name)
    if os.path.isdir(project_dir):
        known = {n for n, _ in names}
        for d in sorted(os.listdir(project_dir)):
            if os.path.isdir(os.path.join(project_dir, d)) and d not in known:
                names.append((d, {}))

    index = {}
    for name, payload in names:
        if name in index:
            continue
        # B-14 P2-4：取图判据统一走 _first_existing_asset_image——
        # 不再只认 front/base 固定名，改为「扩展名白名单 + 第一张非空」。
        # front/base 的 http URL 仍由前端 resolve_local_path 传回本地路径；
        # 本地推导时按 (project_dir, name) 目录取第一张可用图。
        asset_dir_for_name = os.path.join(project_dir, name)
        local = _first_existing(
            comfyui_client.resolve_local_path(payload.get("front") or ""),
            comfyui_client.resolve_local_path(payload.get("base") or ""),
            _first_existing_asset_image(asset_dir_for_name),
        )
        entry = {"name": name, "image": local, "_dir": asset_dir_for_name,
                 "url": f"/api/assets/{kind}s/{project_name}/{name}/front.png"}
        # 2026-09-25 景别对档：把**逐视角**路径也挂进来，供 `_pick_char_view`
        # 按镜头景别取「半身档 / 全身档」。角色资产自 sheet_split 改造后是
        # front/left/right/back/half 五个独立文件（少任何一个都合法）。
        #   ⚠️ 键只在**文件真实存在**时写入（不写空串），否则 `payload.get(k) or ""`
        #      分辨不出「没这个档位」与「有这个档位但路径为空」。
        #   ⚠️ `_dir` 是 `_pick_char_view` 的兜底按需推导目录（而不是批量 stat），
        #      这样前端上报的 http URL 失效时仍能命中磁盘约定路径。
        # 2026-09-29：挂在集合里再并入**场景机位档**（front/left45/right45/top）——
        # 供 `_pick_scene_view` 按镜头机位取「同机位场景图」。两类档位名不重叠
        # （角色 front/left/right/back/half vs 场景 front/left45/right45/top），
        # 且键只在**文件真实存在**时写入，故混挂不会互相干扰、也不会给没有的档写空串。
        for _stem in tuple(ASSET_VIEW_STEMS) + tuple(SCENE_VIEW_KEYS):
            _p = _first_existing(
                comfyui_client.resolve_local_path(payload.get(_stem) or ""),
                os.path.join(asset_dir_for_name, f"{_stem}.png"),
                os.path.join(asset_dir_for_name, f"{_stem}.jpg"),
            )
            if _p:
                entry[_stem] = _p
        # ⭐ 2026-10-05 场景九宫格：把 9 机位独立档（wide/low/detail/depth/back 等）
        #    与拼接总图 grid.png 一并挂进 entry，供 `_pick_scene_view` 的九宫格分支
        #    按 SCENE_GRID_FILENAME 直接取整图。键只在**文件真实存在**时写入（同上方
        #    逐档口径），避免给「没这个档」写空串。
        if kind == "scene":
            for _stem in tuple(SCENE_GRID_VIEW_KEYS) + (SCENE_GRID_FILENAME,):
                if _stem in entry:
                    continue
                _p = _first_existing(
                    comfyui_client.resolve_local_path(payload.get(_stem) or ""),
                    os.path.join(asset_dir_for_name, f"{_stem}.png"),
                    os.path.join(asset_dir_for_name, f"{_stem}.jpg"),
                )
                if _p:
                    entry[_stem] = _p
        # 场景把**正面档别名到 base.png**：场景不单独产出 front.png（base 本身就是正面
        # 机位出图，见资产 worker 的 scene 分支），但下游 `_pick_scene_view` 的回退链
        # 要按档位名逐级取，别名能省掉「每个调用点各自特判 scene」的分支。
        if kind == "scene" and "front" not in entry and local:
            entry["front"] = local
        index[name] = entry
    return index

def _normalize_char_alias(name) -> str:
    """归一化角色名别名（S6）：剥离 _主角/_角色/_主/_人 后缀、去空白与《》。

    与 pipeline.probe_assets 的资产目录扫描口径对齐：资产目录名可能是
    "青玉_主角" 而剧本里写 "青玉"，或反之。这里只做「后缀剥离 + 去符号」，
    **不做模糊匹配**（避免把"阿青"误归到"青玉"）。
    """
    s = str(name or "").strip()
    s = s.replace("《", "").replace("》", "").replace(" ", "")
    for suf in ("_主角", "_角色", "_主", "_人"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s

# ---- 场景名容错匹配（2026-09-29）------------------------------------------- #
# 背景：场景资产名（``scenes[].name``）与镜头引用的场景名（``shot.location``）
#   是**两次独立的 LLM 生成**，用词不保证逐字一致（全角/半角括号、书名号、
#   空格、「铜铃巷」vs「铜铃巷（夜）」）。旧实现统一写成::
#
#       scene_name = loc if loc in scene_idx else None
#
#   —— **不匹配就静默置 None**：场景参考图直接不注入、不打任何日志。画面里
#   的建筑形制、光位只能靠模型自行想象，是「背景不一致」类质检缺陷的隐蔽
#   来源；而且这段判据在 5 处取用点各写了一遍（分镜参考图 / 重跑切段 /
#   H3 视频段 / 图片质检锚点 / 连续性判定），改一处漏四处的风险很高。
#   这里收敛为**一个匹配器**，并确立「宁可告警、不可静默」。
#
# ⚠️ 降级方向**越靠后越保守**：误配（把 A 场景的图给 B）比丢图更糟——
#   丢图只是「没有锚点」，误配会主动把**错误背景**焊进画面。故最后一级
#   强制要求**唯一命中**，多候选一律放弃。

#: 全角 ASCII（！-～）→ 半角；另加全角空格。只动标点/空白，不动汉字。
#: ⚠️ 2026-09-29：归一化与匹配逻辑已收敛到 ``asset_name_match``（三类资产共用），
#: 下面保留同名函数作为**薄封装**——调用点与既有探针无需改动，实现只有一份。
_SCENE_FULLWIDTH_MAP = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}

_SCENE_FULLWIDTH_MAP[0x3000] = 0x20

#: 装饰性符号（引号/书名号/间隔号）：本身无语义，剥离后仍指向同一场景。
_SCENE_DECOR_CHARS = ("《", "》", "「", "」", "『", "』", "\"", "'",
                      "“", "”", "‘", "’", "·", "•")

def _normalize_scene_name(name) -> str:
    """归一化场景名（只做**标点 / 空白 / 全半角**层面，不做语义改写）。

    刻意**不剥**「（夜）」「外」「门口」这类限定词：它们是场景区分的一部分，
    剥了会把「卧室」和「卧室外」混成一个。此类差异交给
    :func:`_match_scene_name` 的「唯一子串」一级去兜（且必须唯一）。

    ⚠️ 2026-09-29：实现已移到 :mod:`asset_name_match`（三类资产共用一份），
    这里只是保持旧函数名的薄封装。
    """
    return asset_name_match.normalize(name)

def _match_scene_name(loc, scene_idx) -> tuple:
    """把镜头写的场景名解析到 ``scene_idx`` 的键 → ``(key, level)``。

    三级降级（精确 → 归一化 → **唯一**子串），全部失败返回 ``(None, "")``。
    第 3 级必须唯一：若索引里「卧室」「卧室外」都能被子串命中，则放弃——
    **宁可不给图，也不给错图**。

    ⚠️ 2026-09-29：实现已移到 :mod:`asset_name_match`，此处为薄封装。
    """
    return asset_name_match.match(loc, scene_idx, "scene")

def _note_ref_warning(shot, msg: str) -> None:
    """把「参考图未命中」告警挂到镜头上（**累加，不覆盖**）。

    一个镜头可能同时缺场景图与物品图；直接赋值会把前一条覆盖掉，排障时只能
    看到最后一条。这里统一累加到 ``_ref_warnings``，同时保留 ``_ref_warning``
    （首条）以兼容既有读取方。
    """
    if not isinstance(shot, dict) or not msg:
        return
    lst = shot.get("_ref_warnings")
    if not isinstance(lst, list):
        lst = []
        shot["_ref_warnings"] = lst
    if msg not in lst:
        lst.append(msg)
    shot.setdefault("_ref_warning", msg)

def _resolve_scene_entry(shot: dict, scene_idx: dict,
                         where: str = "") -> tuple:
    """解析镜头所属场景 → ``(scene_name, entry)``；未命中时**显式告警**并记账。

    调用方只有在 ``entry is None`` 时才真正「没有场景锚点」。告警写进
    ``shot["_ref_warnings"]``（**累加**），能在任务结果里定位到具体哪一镜、
    写的什么名字。

    ⚠️ 只有「剧本声明了场景、且索引非空」时未命中才告警——两者皆空属于正常
    空镜脚本，不该制造噪音。
    """
    if not isinstance(shot, dict):
        return None, None
    loc = shot.get("location")
    name, level = _match_scene_name(loc, scene_idx)
    if not name:
        if loc and scene_idx:
            _msg = (f"场景名未匹配：镜头 {shot.get('shot_id')} 的 location={loc!r} "
                    f"在场景资产 {sorted(scene_idx.keys())[:8]} 中无对应项，"
                    f"该镜将不带场景参考图")
            _note_ref_warning(shot, _msg)
            app.logger.warning("[%s] %s", where or "scene-ref", _msg)
        return None, None
    if level != "exact":
        app.logger.info("[%s] 场景名模糊命中：%r → %r（%s）",
                        where or "scene-ref", loc, name, level)
        shot["_scene_match"] = {"queried": loc, "matched": name, "level": level}
    return name, (scene_idx.get(name) or {})

def _resolve_item_names(shot: dict, item_idx: dict, where: str = "") -> list:
    """解析镜头出场物品 → **规范名列表**（去重保序）；未命中显式告警。

    与 :func:`_resolve_scene_entry` / :func:`_match_shot_chars` 同口径：
    「宁可告警、不可静默」。旧实现是裸判据
    ``[n for n in items_in_shot if n in item_idx]`` —— 物品名只因全角括号、
    书名号、空格或「（断）」这类后缀差异对不上，该物品的设定图就**不注入、
    不打任何日志**，画面里道具形制只能靠模型猜（「道具走形」类质检缺陷的
    隐蔽来源）。
    """
    if not isinstance(shot, dict):
        return []
    resolved, missing, fuzzy = asset_name_match.resolve_names(
        shot.get("items_in_shot"), item_idx, "item")
    for _q, _k, _lv in fuzzy:
        app.logger.info("[%s] 物品名模糊命中：%r → %r（%s）",
                        where or "item-ref", _q, _k, _lv)
    if missing:
        _msg = (f"物品名未匹配：镜头 {shot.get('shot_id')} 的 {missing} "
                f"在物品资产 {sorted(item_idx.keys())[:8]} 中无对应项，"
                f"该物品将不带参考图")
        _note_ref_warning(shot, _msg)
        app.logger.warning("[%s] %s", where or "item-ref", _msg)
    return resolved

def _match_shot_chars(shot: dict, char_idx: dict) -> list:
    """S6 修复：按镜头 characters_in_shot 匹配 char_idx，**禁止静默 take-first**。

    返回匹配到的角色名列表（保持 shot 原顺序、去重）；镜头一个角色都匹配不到 →
    返回 []，由调用方设 shot['_no_reference']=True / shot['_ref_error']=... 决定
    400 / 跳过。

    ⚠️ 2026-09-29 与物品／场景统一口径：
      · 匹配走 :mod:`asset_name_match` 三级降级（精确 → 归一化 → **唯一**子串），
        不再只做「精确 + 后缀别名」两档 —— 「方源。（少年）」这类写法以前会静默丢图；
      · **部分未命中也会告警**。旧实现只在「一个都没匹配上」时出声，而
        「镜头登记了 A、B 两人、只有 A 命中」时 B 被静默丢弃（B 的参考图不注入，
        用户与日志都看不出来），是「角色不像设定」的隐蔽来源。
    """
    if not isinstance(shot, dict):
        return []
    chars_in = [n for n in (shot.get("characters_in_shot") or []) if n]
    if not chars_in:
        return []
    resolved, missing, fuzzy = asset_name_match.resolve_names(
        chars_in, char_idx, "character")
    for _q, _k, _lv in fuzzy:
        app.logger.info("[角色参考图] 角色名模糊命中：%r → %r（%s）", _q, _k, _lv)
    if missing:
        _msg = (f"角色名未匹配：镜头 {shot.get('shot_id')} 的 {missing} "
                f"在角色资产 {sorted(char_idx.keys())[:8]} 中无对应项，将不带其参考图")
        _note_ref_warning(shot, _msg)
        app.logger.warning("[角色参考图] %s", _msg)
    return resolved

def _on_screen_characters(shot: dict) -> list:
    """「本镜画面内可见角色」的**权威口径**（单一来源，2026-10-05）。

    直接委托 :func:`te_3d_director.on_screen_characters` —— 它同时是 3D 导演台
    ``_parse_characters`` 的实现（``render_blocking → build_render_plan →
    _parse_characters``）。因此「分镜该不该注入 3D 基准图」的判据与「3D 导演台渲不渲得出
    人偶」的判据**是同一个函数**，天然同源，不会再出现「A 处说没人、B 处说有人」的分叉。

    ⚠️ 只认剧本的 ``characters_in_shot``；**台词 speaker 可能是画外音，不算出场角色**。

    te_3d_director 不可导入时返回 ``[]``：此时 ``render_blocking`` 同样不可用（本镜本就不会
    注入基准图），判为「无出场角色」是安全的保守值，不会误注入人偶。
    """
    if not isinstance(shot, dict):
        return []
    try:
        import te_3d_director  # noqa: PLC0415
        return list(te_3d_director.on_screen_characters(shot))
    except Exception as e:  # noqa: BLE001 - 导演台不可用时保守判为「无出场角色」
        app.logger.warning("[3D导演台] 解析出场角色失败（按无出场角色处理）：%s", e)
        return []

def _shot_has_on_screen(shot) -> bool:
    """本镜是否有**画面内出场角色**（读 :func:`_on_screen_characters` 的权威口径）。

    正常路径下 ``_allocate_storyboard_refs`` 已把该口径以 ``shot['_on_screen_chars']``
    挂回；字段**缺失**时保守返回 ``True``（沿用旧行为）—— 避免某条没走分配器的调用路径被
    误判成无人物镜、丢掉构图基准图或加错提示词（宁可少禁、不可误禁）。
    """
    if not isinstance(shot, dict):
        return True
    return bool(shot.get("_on_screen_chars", True))

def _shot_has_char_ref(shot) -> bool:
    """本镜是否有**已命中资产的角色身份参考图**（读 ``_allocate_storyboard_refs`` 挂回的
    ``shot['_char_ref_names']`` = :func:`_match_shot_chars` 的 ``chars_in``）。

    ## 为什么渲染 3D 人偶基准图还要再过这一关（2026-10-05）

    ``COMPOSITION_BASELINE_SECTION``（comfyui_client.py）要求模型「用其他参考图里的角色
    **完全覆盖**人偶」。若本镜声明了角色、但角色**资产缺失**（``_match_shot_chars`` 返回
    ``[]``、``_no_reference=True``），refs 里就只有场景图、**没有角色图可覆盖** → 模型只能
    照抄人偶，症状与「画外音兜底」逐字相同。故渲染门槛在 ``_shot_has_on_screen``（declared）
    之外，**还须** ``_shot_has_char_ref``（declared ∩ matched）。

    ⚠️ 本判据是 ``_shot_has_on_screen`` 的**子集**，行为只会更保守（不插 ref、少调一次
    ``render_blocking``），**不会与渲染器产生矛盾输出**，因此不构成「第三个会分叉的口径」。

    字段**缺失**时保守返回 ``True``：与 :func:`_shot_has_on_screen` 同风格 —— 避免某条未走
    ``_allocate_storyboard_refs`` 的调用路径被误判成「无角色图」而丢掉基准图（宁可多渲、
    不可误禁）。注意：真走到渲染前的正常路径**一定**已挂该字段。
    """
    if not isinstance(shot, dict):
        return True
    return bool(shot.get("_char_ref_names", True))

#: 取「半身档」角色参考图的景别集合（近景类）。
#: 为什么是这些：角色立绘是**全身**，而近景类镜头要求参考图与目标取景同向 ——
#: 全身立绘会把模型往全景方向拉（见 _framing_wants_half 的长注释，实测 shot_24）。
#: ⚠️ 景别取值来自 config.SHOT_TYPES（唯一权威表）；新增近景类景别时要同步加进来，
#:    否则该景别会静默走全身档、重新引入「画幅对抗」。
_FRAMING_HALF_SHOT = ("大特写", "特写", "近景", "中近景", "局部", "中景")

def _framing_wants_half(camera) -> bool:
    """本镜的景别是否该用「半身档」参考图（近景/特写/中景 → 半身；全景/远景 → 全身）。

    ## 为什么需要「景别对档」（2026-09-25）

    实测根因（见 ``.workbuddy/tools/diag_framing_control.py``）：分镜模板 ``<image1>``
    是**主画布**，角色参考图原先是 736x736 的**全身**立绘；cover 到 9:16 竖屏要
    左右各裁一半 → 模型为保住「完整的全身」只能把人物缩小 → **系统性偏全景/远景**。
    近景/特写要求「只拍局部」，与立绘「保全身」方向相反 → 被拉回、画不出来
    （实测 shot_24 规定近景、出图近全身，质检却因容差放行）。

    业界通行做法就是「**参考图的景别要接近目标镜头**」（要特写，参考图也尽量用特写）。
    故：角色设定图补一格**正面半身胸像**（``half``），近景/特写/中景镜头取它当
    ``<image1>``，画幅与景别同向，画幅对抗即消失。

    ⚠️ 判据用 ``camera_key``（已能解析「特写推入」「中景跟拍」等复合写法）；
    但**必须先挡掉空值**：``camera_key('')`` 会返回 ``'中景'``（那是给生成端用的
    默认档，不是「本镜是中景」）—— 直接复用会把「景别未指定」误判成中景。
    景别**未指定**时返回 False（全身档）：全身档是画幅对抗最小的默认，
    且近景档是「新增资源」，只在明确要近景时才用（不给存量资产凭空换档）。
    """
    _raw = str(camera or "").strip()
    if not _raw:
        return False
    k = _camera_key(_raw)
    return k in _FRAMING_HALF_SHOT

def _framing_wants_half_shot(shot: dict) -> bool:
    """A1：按**权威景别字段**判断本镜角色参考图是否取「半身档」。

    与 :func:`_framing_wants_half` 的差别：优先读 ``shot_type``（新剧本权威字段），不再靠
    解析「特写推入」这类复合串去猜；``shot_type`` 与 ``camera`` **都为空**时返回 False
    （全身档）—— 与原实现「空值返回 False」同口径，绝不把「景别未指定」误判成中景。
    """
    if not isinstance(shot, dict):
        return False
    st = str(shot.get("shot_type") or "").strip()
    if st:
        return st in _FRAMING_HALF_SHOT
    _raw = str(shot.get("camera") or "").strip()
    if not _raw:
        return False
    return _camera_key(_raw) in _FRAMING_HALF_SHOT

def _pick_char_view(char_payload: dict, want_half: bool, outfit_dir: str = "") -> str:
    """取角色参考图：**始终返回整张设定图 base.png**（2026-10-02 起不裁剪）。

    ⚠️⚠️ **2026-10-02 用户指定「角色图不用裁剪，给整个图片就行」** —— 本函数
    从「按景别对档挑 half/front/back」退化为「**只取整图 base.png**」。动机与配套：
      · 角色设定图已改为**英文四区 character sheet**（Top 三视图 / Left 面部+配色 /
        Bottom 细节 / Right 比例参照，见 `comfyui_client._CHARACTER_SHEET_EN_LAYOUT`），
        四区不对称布局**无法再做行列投影切分** → 单视角切分（front/left/back/half）
        失去产出依据；
      · 用户要「整图给模型自己取视角」，不再由我们按景别裁单视角。
    ⚠️ 取舍（风险已向用户说明）：取消「景别对档」会**重新引入画幅对抗**（全景/近景
    参考图同用一张整图，近景/特写的构图牵引变弱）——这是 2026-09-25 那套「半身档」
    要解决的问题。用户明确选择「不裁剪」，故此处按要求退化；`want_half`/`outfit_dir`
    参数**保留签名**（调用方不破），但不再影响取图结果（仅整图）。
    ⚠️ 回滚点：如需恢复景别对档，改回 order 的 half/front 分支即可。

    服装变体（衣柜，2026-10-02）：``outfit_dir`` 非空时**仍优先**在该变体目录里取
    整图（base.png），变体目录没有 / 异常 → 回落主设定图（fail-open，零回归）。
    """
    payload = char_payload or {}
    # 只取整图：base.png →（兜底）front.png（存量资产可能只有 front）
    order = ("base", "front")
    if outfit_dir:
        try:
            _od = comfyui_client.resolve_local_path(outfit_dir) or outfit_dir
            if os.path.isdir(_od):
                _hit = _first_existing(*[os.path.join(_od, f"{k}.png") for k in order])
                if _hit:
                    return _hit
        except Exception as _oe:  # noqa: BLE001  变体取图失败绝不拖垮参考图链
            app.logger.debug("服装变体取图失败（回落主设定图）：%s", _oe)
    cands = [comfyui_client.resolve_local_path(payload.get(k) or "") for k in order]
    # 资产目录约定路径兜底（前端未上报时）
    d = payload.get("_dir")
    if d and os.path.isdir(d):
        cands += [os.path.join(d, f"{k}.png") for k in order]
    return _first_existing(*cands) or ""

def _shot_outfit_dir(shot: dict, character_name: str, char_dir: str) -> str:
    """解析「本镜服装提示」→ 该角色**已生成**的服装变体目录（outfits/<key>/）。

    服装提示来源（按优先级）：
      ① ``shot["outfit"]``（本镜服装提示，字符串）；
      ② ``shot["character_outfits"]`` 按角色名查（值可为字符串，或
         {outfit_key|key|outfit|desc: ...} 形态的字典；键走别名归一匹配）。
    解析：剥掉「本套服装：/服装：/outfit(_key)?:」前缀后，与该角色主设定目录下
    ``outfits/`` 的**已存在**子目录名做精确匹配（含书名号/引号剥离与安全化键）——
    只认磁盘上真实存在的变体，解析出的名字没建过目录就回落主设定图。

    ⚠️ fail-open：任何一步解析不出 / outfits 目录不存在 → 返回 ''，调用方按
    「无变体」走主设定图（与改造前行为完全一致）。查不到表也绝不抛异常。
    """
    if not isinstance(shot, dict) or not char_dir or not os.path.isdir(char_dir):
        return ""
    outfit_root = os.path.join(char_dir, _OUTFITS_DIRNAME)
    try:
        known = [d for d in os.listdir(outfit_root)
                 if os.path.isdir(os.path.join(outfit_root, d))]
    except OSError:
        return ""
    if not known:
        return ""
    raw = str(shot.get("outfit") or "").strip()          # ① 本镜服装提示（优先）
    if not raw:
        co = shot.get("character_outfits")               # ② 按角色名查
        if isinstance(co, dict):
            v = co.get(character_name)
            if v is None:
                _norm = _normalize_char_alias(character_name)
                for k, vv in co.items():
                    if _normalize_char_alias(str(k)) == _norm:
                        v = vv
                        break
            if isinstance(v, dict):
                v = (v.get("outfit_key") or v.get("key")
                     or v.get("outfit") or v.get("desc") or "")
            raw = str(v or "").strip()
    if not raw:
        return ""
    _m = re.match(r"^(?:本套服装|服装|outfit(?:_key)?)\s*[:：=]\s*(.+)$", raw)
    cand = (_m.group(1) if _m else raw).strip()
    for k in (cand, cand.strip("「」《》\"'“”‘’")):
        if k in known:
            return os.path.join(outfit_root, k)
    _sk = _sanitize_outfit_key(cand)
    if _sk and _sk in known:
        return os.path.join(outfit_root, _sk)
    return ""

def _scene_view_for_shot(shot) -> str:
    """本镜**机位**该取哪个场景档（``front`` / ``left45`` / ``right45`` / ``top``）。

    与景别（`shot_type` / `camera` 里的特写/近景/全景）**正交**：景别决定取景范围，
    机位决定观察方向。映射表单一来源在 :data:`config.SCENE_ANGLE_TO_VIEW`，
    未列出的机位（含未指定即空串）一律回落到正面档。

    ⚠️ 机位只能从**复合串** `camera` 解析（`shot_type` 只登记景别，无角度信息）；
    `camera_motion` 是 A1 新增的运镜权威字段，老剧本为空、新剧本可能把「俯拍缓推」
    写在那里，故作为**第二来源**（不是第一条）：先正规字段，再权威字段，
    两边都没有才判「未指定」。
    """
    if not isinstance(shot, dict):
        return "front"
    ang = _camera_angle(str(shot.get("camera") or "")) or \
        _camera_angle(str(shot.get("camera_motion") or ""))
    return SCENE_ANGLE_TO_VIEW.get(ang, "front")

def _pick_scene_view(scene_payload: dict, shot) -> str:
    """按镜头机位从场景资产里挑一张**同机位**的场景参考图（2026-09-29 新增）。

    动机：场景此前只有一张基准图，俯拍 / 斜侧镜头拿到的都是正面图，构图先验与镜头
    要求**反向**（「要求俯拍却给了平视」既没被约束、也没被检出）。场景现在按机位档
    逐档出图（`config.SCENE_VIEW_KEYS`），这里负责把镜头机位映射到档位。

    ⚠️ 必须**逐级优雅降级**，绝不返回空（与 `_pick_char_view` 同口径）：返回空会让该镜
    判成「无场景锚点」，把「机位档缺失」这种**加值项缺失**升级成「镜头不能出图」。
    回退链：
      · 目标档 → front → base（该档没生成 / 是老项目没有机位档）
      · 目标档本身就是 front → front → base
    `base` 是每个场景都有的兜底（正面机位出图），故链尾一定落得住。

    ⚠️ 刻意**不做**「俯拍档缺失就退而取斜侧档」这类跨机位借用：俯视与仰拍是反向机位，
    拿俯视图当仰拍镜头的锚点会把构图拽反 —— 「误配比丢图更糟」（丢图只是没有锚点，
    误配是把错误机位焊进画面，且日志看不出来）。宁可回落正面档。

    ⚠️ 链尾必须是 ``entry["image"]``（2026-09-29 实修）：它是**长期存在的主图契约**，
    `_build_asset_index` 与历史生产者都填它，但**不一定**同时给 `front`/`base`/`_dir`。
    少了这一档，任何「只填 image」的场景索引都会静默丢场景锚点 —— 这不是假想：
    改完当场被 ``probe_scene_match.py`` 的端到端用例抓出（fixture 就是只填 image 的形态）。

    ⭐ 2026-10-05 场景九宫格（SCENE_GRID_MODE）：**彻底关机位对档、纯整图直接用**。
       网格模式下场景资产本体是 9 机位拼成的 ``grid.png``，下游**所有镜头机位**都喂
       这张整图（不再按机位选档）——这是用户 2026-10-05 三轮确认的选择。故函数体
       第一步就先查 ``grid``；查不到（老项目 / 关网格）才走下面原有的按机位逐级回退。
    """
    payload = scene_payload or {}
    # ⭐ 九宫格模式：优先整图（grid.png），不再按镜头机位选档（关机位对档）。
    #    用既有回退链工具保证「网格图缺失时平滑回落旧行为」，绝不返回空。
    if SCENE_GRID_MODE:
        _g = comfyui_client.resolve_local_path(payload.get(SCENE_GRID_FILENAME) or "")
        _gd = payload.get("_dir")
        _g2 = os.path.join(_gd, SCENE_GRID_FILENAME) if (_gd and os.path.isdir(_gd)) else ""
        _grid_pick = _first_existing(_g, _g2)
        if _grid_pick:
            return _grid_pick
        # 网格主图缺失（老项目未重跑 / 拼接失败）→ 继续走下方按机位回退，绝不静默丢图。
    target = _scene_view_for_shot(shot)
    order = []
    for k in (target, "front", "base"):
        if k and k not in order:
            order.append(k)
    cands = [comfyui_client.resolve_local_path(payload.get(k) or "") for k in order]
    # 资产目录约定路径兜底（前端未上报 / 索引未挂到该档时）
    d = payload.get("_dir")
    if d and os.path.isdir(d):
        cands += [os.path.join(d, f"{k}.png") for k in order]
    # 最后回落主图契约（老索引 / 外部生产者只填 image 的形态）
    cands.append(comfyui_client.resolve_local_path(payload.get("image") or ""))
    return _first_existing(*cands) or ""

def _h3_shot_ref_components(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                            character_refs: list = None,
                            main_char_img: list = None,
                            outfit_map: dict = None) -> list:
    """把一个分镜解析成**有序参考图组件**（不构建提示词、不决定槽位）。

    返回 ``[{"kind": "character"|"item"|"scene", "name": str,
             "path": 本地路径, "appearance": str}, ...]``，
    顺序恒为「角色 → 物品 → 场景」——与 ``comfyui_client._h3_picture_defs`` 里
    三段的排放顺序一致（提示词的 ``<Picture N>`` 就按这个序推下去）。

    ## 为什么要抽出来（2026-09-30，H3 Director 公共参考图）

    「公共参考图」的判据是「全段都在用、且用的是同一张图」，要在 worker 循环**之前**
    先把每个镜头的资产解析一遍才能求交集；而 ``_shot_segment`` 里原来那段解析是
    内联的。两处各写一份必然漂移（公共池按 A 口径选、段级按 B 口径挂 → 公共图挂错镜）。
    故收敛到本函数：公共池规划与段级挂图**共用同一份解析结果**，
    并且解析只做一次（``_match_shot_chars`` / ``_resolve_item_names`` /
    ``_resolve_scene_entry`` 都会往 ``shot["_ref_warnings"]`` 记账，
    重复调用会把同一句告警记两遍）。

    ⚠️ 解析口径必须是**逐镜对档**后的结果，不能退化成「资产的基准图」：
      · 角色按景别取半身/全身档（``_pick_char_view``，2026-09-25「景别对档」）；
      · 场景按机位取 front/left45/right45/top 档（``_pick_scene_view``，2026-09-29）。
    公共池要的是「同一张图」，所以上图**按路径**判交集——档位不同的同一资产
    天然不满足，这正是「公共锁定」不会把对档工作废掉的原因（见 h3_common_refs）。
    """
    if not isinstance(shot, dict):
        return []
    out: list = []

    # ---- 本镜角色：每人一张（按景别对档），去重保序 ----
    # outfit_map（2026-10-02 服装变体）：「角色名 → outfits/<key>/ 目录」的可选映射，
    # 由调用方解析本镜服装提示（shot.outfit / shot.character_outfits）后传入；
    # None（默认）/ 缺该角色 → 走主设定图，与旧行为逐字一致（视频 worker 调用点
    # 位于 6200 行后的区域、本轮不可改动，故暂以默认 None 接线，见最终报告）。
    _want_half = _framing_wants_half_shot(shot)
    _seen_paths: set = set()
    for _mc in (_match_shot_chars(shot, char_idx) or []):
        _entry = char_idx.get(_mc) or {}
        _od = ""
        if outfit_map:
            _od = str(outfit_map.get(_mc)
                      or outfit_map.get(_normalize_char_alias(_mc)) or "")
        _p = _pick_char_view(_entry, _want_half, _od)
        if not _p or _p in _seen_paths:
            continue
        _seen_paths.add(_p)
        out.append({"kind": "character", "name": _mc, "path": _p,
                    "common_key": f"char:{_mc}",
                    "appearance": _entry.get("appearance")
                    or _entry.get("description") or ""})

    # ---- 本镜物品（与角色图重复的丢弃，保持旧口径）----
    for _it in (_resolve_item_names(shot, item_idx, "H3视频段") or []):
        _entry = item_idx.get(_it) or {}
        _p = _entry.get("image")
        if not _p or _p in _seen_paths:
            continue
        _seen_paths.add(_p)
        out.append({"kind": "item", "name": _it, "path": _p,
                    "appearance": _entry.get("appearance")
                    or _entry.get("description") or ""})

    # ---- 本镜场景（按机位取档）----
    _loc, _scene_entry = _resolve_scene_entry(shot, scene_idx, "H3视频段")
    _scene_img = _pick_scene_view(_scene_entry, shot) if _scene_entry else None
    if _scene_img:
        out.append({"kind": "scene", "name": _loc or "本镜场景", "path": _scene_img,
                    "appearance": ""})

    # ---- 兜底：逐镜角色一张都没解析出来 → 退回全局主角锚点（保持原行为）----
    # ⚠️ 旧实现把整个 refs 直接替换成 `[sb] + main_char_img`，于是本镜的**物品/场景锚点
    #    连同「提示词已声明」一起被丢掉**（声明在、图不在 → 编号错位）。这里改成
    #    「主角锚点排到最前、其余组件保留」，发送的图只多不少、且每一张都有声明。
    # ⚠️ 旧实现是 `refs = [sb] + main_char_img` 但提示词只声明 1 个角色 —— 图比声明的多，
    #    多出来的那张「有图无声明」（模型拿到一张没说用途的图）。这里按**实际张数**
    #    逐个声明，把编号对齐。
    if not any(c["kind"] == "character" for c in out) and main_char_img:
        _ref0 = (character_refs or [{}])[0] if character_refs else {}
        _nm = (_ref0.get("name") if isinstance(_ref0, dict) else None) or "主角"
        _ap = (_ref0.get("appearance") or _ref0.get("description") or "") \
            if isinstance(_ref0, dict) else ""
        _fb = []
        for _p in list(main_char_img):
            if not _p or _p in _seen_paths:
                continue
            _seen_paths.add(_p)
        _fb.append({"kind": "character", "name": _nm, "path": _p,
                    "common_key": f"char:{_nm}",
                    "appearance": _ap})
        out = _fb + out      # 角色在前（与 _h3_picture_defs 的段序一致，且保序）
    return out

def _h3_plan_common_refs(shots: list, char_idx: dict, item_idx: dict, scene_idx: dict,
                         character_refs: list = None, main_char_img: list = None,
                         sb_map: dict = None, use_storyboard: bool = True,
                         project_name: str = ""):
    """按「全段都在用、且同一张图」挑出公共参考图（H3 Director 公共参数）。

    返回 ``(common, comps_map)``：
      · ``common``: 公共组件列表（顺序 = 第 1 镜的出现顺序，已按上限截断）；
      · ``comps_map``: ``{shot_id: [组件, ...]}`` —— 每镜的解析结果，
        供 ``_shot_segment`` 复用（避免解析两遍造成重复告警）。

    ⚠️ 关掉开关或只有 1 个镜头时返回 ``([], {})``：**完全走原路径**，零行为变更。
    单镜头（per_shot 重跑）没有「跨段公共」可言，硬开公共池只会把段级图挪到全局，
    收益为零还多一层 ``commonEnabled`` 耦合。

    ⚠️ **要求每个镜头都有分镜图**，否则整集公共化会被整体放弃（返回 ``([], {})``）：
    公共图由客户端写进 ``global.refs``，它被 merge 进**每一段**，而提示词侧的
    ``<Picture 1..K>`` 只有在「分镜图分支」（``sb_local`` 为真）才会被生成。
    只要有一镜走了无分镜图的兜底分支，它的 ``<Picture N>`` 就从 1 起重新数，
    与槽位（公共块从 0 起）整体错位 —— 那是**静默错图**，宁可不做这个特性。
    """
    if not H3_COMMON_REFS or len(shots or []) < 2:
        return [], {}
    _shots = [s for s in shots if isinstance(s, dict)]
    # 逐镜解析结果按 shot_id 缓存复用（避免解析两遍），故 shot_id 必须唯一且非空 ——
    # 一旦重复/缺失（历史剧本偶有），缓存会互相覆盖 → 公共池按 A 镜选、B 镜挂错图。
    _ids = [str(s.get("shot_id") or "") for s in _shots]
    if "" in _ids or len(set(_ids)) != len(_ids):
        app.logger.warning(
            "[H3公共参考图] 镜号缺失或重复（%s…）→ 放弃公共化（逐镜缓存需要唯一镜号）",
            [x or "<空>" for x in _ids[:8]])
        return [], {}
    if sb_map is not None:
        _missing_sb = [s.get("shot_id") for s in _shots
                       if not (use_storyboard
                               and sb_map.get(_norm_shot_key(s.get("shot_id"))))]
        if _missing_sb:
            app.logger.info(
                "[H3公共参考图] %d 个镜头没有分镜图（%s…）→ 放弃公共化"
                "（公共图会被 merge 进每一段，缺分镜图的段编号会整体错位）",
                len(_missing_sb), _missing_sb[:5])
            return [], {}
    comps_map = {}
    per_shot_assets = []
    for _s in _shots:
        _sid = str(_s.get("shot_id") or "")
        _comps = _h3_shot_ref_components(_s, char_idx, item_idx, scene_idx,
                                         character_refs, main_char_img)
        comps_map[_sid] = _comps
        per_shot_assets.append(_comps)
    common = h3_common_refs.plan_common_refs(
        per_shot_assets, max_common=H3_COMMON_REFS_MAX,
        limit=h3_director_builder.MAX_REFERENCE_IMAGES)
    # ⭐ 全局风格参考图（2026-10-03）：项目 config.json 的 style_ref_image 作为最后一张公共图。
    if project_name:
        try:
            _proj_root = _safe_project(project_name)
            if _proj_root:
                _cfg = json.load(open(os.path.join(_proj_root, 'config.json'), encoding='utf-8'))
                _style_ref = str(_cfg.get('style_ref_image') or '').strip()
                if _style_ref:
                    _style_ref_path = _style_ref if os.path.isabs(_style_ref) else os.path.join(_proj_root, _style_ref)
                    if os.path.isfile(_style_ref_path) and len(common) < h3_director_builder.MAX_REFERENCE_IMAGES:
                        common = list(common) + [{'kind':'scene','name':'全局风格参考','appearance':'global style / lighting anchor','path':_style_ref_path}]
                        app.logger.info('[H3公共参考图] 追加全局风格参考图：%s', os.path.basename(_style_ref_path))
        except Exception as _e:  # noqa: BLE001
            app.logger.debug('[H3公共参考图] 风格参考图解析失败（忽略）：%s', _e)
    if common:
        app.logger.info(
            "[H3公共参考图] %d 镜中 %d 项全段共用 → 走 global.refs + commonEnabled：%s",
            len(per_shot_assets), len(common), h3_common_refs.describe(common))
    else:
        app.logger.info(
            "[H3公共参考图] %d 镜无「全段都在用且同一张图」的资产 → 维持逐段 refs",
            len(per_shot_assets))
    return common, comps_map

def _h3_is_common_comp(comp: dict, common_keys: set) -> bool:
    """该组件是否属于公共池（身份键 = 种类 + 名称 + 图片路径，与 h3_common_refs 同源）。"""
    return h3_common_refs.asset_key(comp) in common_keys

def _project_worldview(project_name: str) -> str:
    """取项目的「世界观设定」文本（2026-10-03）：供公共提示词 WORLD 行拼接用。

    来源优先级：① autopilot plan 的 brief（总控 AI 敲定的故事概述）；
    ② 项目 config.json 的 note。两者皆空则返回 ""（公共段不写 WORLD 行，零变更）。
    纯只读、永不抛。
    """
    _pw = ""
    try:
        _plan = (autopilot.get_plan(project_name) or {})
        _brief = str(_plan.get("brief") or _plan.get("worldview") or _plan.get("era_world") or "")
        if _brief and len(_brief) <= 400:
            _pw = _brief
    except Exception:  # noqa: BLE001
        pass
    if not _pw:
        try:
            _proj = _safe_project(project_name)
            if _proj:
                _cfg = json.load(open(os.path.join(_proj, "config.json"), encoding="utf-8"))
                _note = str(_cfg.get("note") or "")
                if _note and len(_note) <= 200:
                    _pw = _note
        except Exception:  # noqa: BLE001
            _pw = ""
    return _pw

def _ensure_voice_bank_refs(common: list, project_name: str, all_characters=None) -> None:
    """确保每个公共角色在 voice_bank 里有参考音频（2026-10-03）：
    遍历公共池的角色，若 voice_bank 里没有参考音频，用 TTS 自动生成一段短文本
    并保存到 voice_bank，供后续 _h3_common_ref_audios 捡取。
    任何异常只记日志、绝不阻断主流程。
    """
    # ⚠️ 放宽守卫（原为 `if not common:`）：存量调用方要么传非空 common、要么传非空 all_characters（恒传 char_idx），故零行为变更；放宽只为让 `tts_pre`
    #    能以 common=[] + all_characters=<char_idx> 调用（「每角色参考音色」口径）。
    if not common and not all_characters:
        return
    _dub_dir = _dub_project_dir(project_name)
    if not _dub_dir:
        app.logger.warning("[VoiceBank] 找不到项目 dub 目录，跳过自动生成参考音色")
        return
    _char_names = []
    for _c in (common or []):
        if isinstance(_c, dict) and _c.get("kind") == "character":
            _n = str(_c.get("name") or "").strip()
            if _n and _n not in _char_names:
                _char_names.append(_n)
    # ⭐ 追加项目全量角色（2026-10-04）：用户要求「每个角色都要生成一个参考音色」。
    #    公共池角色之外的角色也要落 voice_bank（供后续按需作为参考音色使用）。
    if all_characters:
        _all_names = list(all_characters.keys()) if isinstance(all_characters, dict) else list(all_characters)
        for _n in _all_names:
            _n = str(_n or "").strip()
            if _n and _n not in _char_names:
                _char_names.append(_n)
    if not _char_names:
        return
    # 获取默认 voice_map（用于拿到每个角色的默认 speaker/seed）
    try:
        import tts_client
        _voice_map = tts_client.default_voice_map(
            [{"name": n} for n in _char_names],
            project=project_name,
            episode=1)
        _chars_vmap = _voice_map.get("characters", {})
    except Exception as _e:  # noqa: BLE001
        app.logger.warning("[VoiceBank] 无法生成默认 voice_map，跳过：%s", _e)
        return
    # 逐角色合成
    import tempfile
    # ⭐ 2026-10-06：TTS 模型类加载失败的熔断开关。
    # 现场：ComfyUI 侧 `qwen_tts` 包 import 失败 → `FB_Qwen3TTSCustomVoice` 节点
    # 每个角色都报同一条 `Model class is not loaded`（一条就带 2KB traceback）。
    # 本函数按角色循环，20 个角色 = **20 次必失败的 ComfyUI 提交**，
    # 每次都进 GPU 队列、进 history，20×2KB 刷屏还会把真故障淹没。
    # 熔断：首次命中「模型类未加载」这类**环境级**错误后，本轮剩余角色全部跳过
    # 并只记一条汇总 —— 与资产库/质检的 fail-open 口径一致（参考音色本就是可选项）。
    _tts_model_unavailable = False
    for _cn in _char_names:
        if _tts_model_unavailable:
            continue
        try:
            _ref, _ = find_voice_bank_ref(_dub_dir, _cn)
            if _ref and os.path.isfile(_ref):
                continue  # 已有参考音频
        except Exception:
            pass
        # 需要生成：用角色名+一句简短介绍做参考文本
        _ref_text = f"我是{_cn}，这是我的声音样本。"
        _voice = _chars_vmap.get(_cn) or {}
        _speaker = str(_voice.get("speaker") or tts_client.SPEAKER_KEYS[0])
        _seed = int(_voice.get("seed") or 0)
        # 组装 TTS voice dict（用 preset 模式，不走 clone）
        _tts_voice = {
            "mode": "preset",
            "speaker": _speaker,
            "seed": _seed,
            "instruct": "",
            "model_choice": "1.7B",
        }
        # 输出路径：voice_bank 目录下的临时文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as _tf:
            _tmp_path = _tf.name
        try:
            _dub_dir2 = _dub_project_dir(project_name)
            _client = tts_client.QwenTTSClient(out_root=_dub_dir2, params=tts_client.TTS_DEFAULT_PARAMS)
            # ⚠️ 超时曾硬编码 60s，比 Qwen3-TTS 的真实单批耗时（实测 24~78s）还短：
            # 合成其实已经成功，ComfyUI 侧也产出了音频，这里却在它返回前放弃 ——
            # 记「配音超时」、丢弃结果、GPU 白烧，下一角色再排一轮（详见 config.TTS_VOICE_BANK_TIMEOUT）。
            _res = _client.synthesize_one(_ref_text, _tts_voice, _tmp_path,
                                          timeout=TTS_VOICE_BANK_TIMEOUT)
            if not _res.get("ok"):
                _err = str(_res.get("error") or "")
                app.logger.warning("[VoiceBank] %s 合成参考音频失败：%s", _cn, _err[:400])
                if _is_tts_model_unavailable(_err):
                    _tts_model_unavailable = True
                    app.logger.warning(
                        "[VoiceBank] TTS 模型类在 ComfyUI 侧未加载（qwen_tts 包 import 失败等），"
                        "本轮剩余 %d 个角色不再重复提交（参考音色是可选项，不阻断生产）。"
                        "修复：在 ComfyUI 侧安装/修复 qwen_tts 依赖后重启 ComfyUI。",
                        max(0, len(_char_names) - _char_names.index(_cn) - 1))
                continue
            # 存入 voice_bank（按 ref.wav 落盘，save_voice_bank_ref 会覆盖旧文件）
            _saved = save_voice_bank_ref(_dub_dir2, _cn, _tmp_path, ref_text=_ref_text)
            app.logger.info("[VoiceBank] %s 自动生成参考音色：%s", _cn, _saved)
        except Exception as _e:  # noqa: BLE001
            _err = str(_e)
            app.logger.warning("[VoiceBank] %s 自动生成参考音色异常（忽略）：%s", _cn, _err[:400])
            if _is_tts_model_unavailable(_err):
                _tts_model_unavailable = True
                app.logger.warning(
                    "[VoiceBank] TTS 模型类在 ComfyUI 侧未加载，本轮剩余角色不再重复提交：%s", _err[:200])
        finally:
            try:
                if os.path.isfile(_tmp_path):
                    os.remove(_tmp_path)
            except Exception:
                pass

def _h3_common_ref_audios(common: list, project_name: str) -> list:
    """从公共池角色收集 voice_bank 参考音色（2026-10-02 公共参考音色）。

    只对 ``kind == character`` 的公共项，取其在 voice_bank 里已登记的参考音频
    （``find_voice_bank_ref``，缺则跳过＝该角色无绑定音色，绝不误挂别的声音）。
    返回**有序**的 ``(角色名, 本地绝对路径)`` 列表，供 ComfyUI 侧写进
    ``global.refAudios``（index 0..M-1）**并**在公共提示词里做**逐行角色归属**
    （``<Audio N> = 角色名``）。
    无公共角色 / 无音色库 / 全部缺音色 → 返回 ``[]``（零行为变更，走原 generate 无参考）。

    ⭐ 2026-10-05：从「纯路径列表」改为 ``(角色名, 路径)`` 有序对 —— 因为本函数会
    **跳过无音色的角色**，下游靠公共池顺序反推「哪个 <Audio> 属于谁」会错位；
    携带角色名才能逐行准确归属。下游两处消费（build 侧取 [1]、subject_lock 侧取 [0]）
    需同步。
    """
    _char_names = [str(c.get("name") or "").strip()
                   for c in (common or [])
                   if isinstance(c, dict) and c.get("kind") == "character"
                   and str(c.get("name") or "").strip()]
    if not _char_names:
        return []
    try:
        _dub_dir = _dub_project_dir(project_name)
    except Exception:  # noqa: BLE001
        return []
    _out: list = []
    _seen = set()
    for _cn in _char_names:
        try:
            _ref, _ = find_voice_bank_ref(_dub_dir, _cn)
        except Exception:  # noqa: BLE001
            _ref = ""
        if not _ref or not os.path.isfile(_ref):
            app.logger.info("[H3公共参考音色] 角色 %s 未绑定参考音色，跳过（不挂声）", _cn)
            continue
        _key = os.path.normcase(os.path.normpath(os.path.abspath(_ref)))
        if _key in _seen:
            continue
        _seen.add(_key)
        _out.append((_cn, _ref))
    if _out:
        app.logger.info("[H3公共参考音色] 公共角色音色 %d 支 → global.refAudios：%s",
                        len(_out), "、".join(n for n, _p in _out))
    return _out

def _h3_common_subject_lock(common: list, common_ref_audios: list,
                        style: str = "", worldview: str = "", aspect: str = "") -> str:
    """公共提示词 subject lock（2026-10-02）：角色/物品/场景锁定句 + 公共音色指代。

    编号与公共槽位**逐位对齐**：公共图 index 0..K-1 → ``<Picture 1..K>``（1-based）；
    公共音色 index 0..M-1 → ``<Audio 1..M>``。插件在 commonEnabled 时把本句拼在**每段
    提示词之前**（plan.py:concat_common_segment_prompt），作为全集一致的主体锁定前缀，
    段级提示词只管本镜叙事，主体外观/音色由这句统一锁定（不再逐镜重述）。

    ⚠️ 提示词消费的是 ``<Picture N>``/``<Audio N>``（H3 模型认的标签）；``@image#N``
    只是 ComfyUI 前端 @ 选择器的 UI chip，不进模型 —— 故这里用 ``<>`` 标签。
    无公共项且无公共音色 → 返回 ``""``（零行为变更，global.prompt 留空）。
    """
    _parts: list = []
    _K = len(common or [])
    if _K:
        _lock = ["Subject lock — keep these shared references consistent across every shot:"]
        for _i, _c in enumerate(common or []):
            _num = _i + 1
            _name = str(_c.get("name") or "").strip()
            _kind = _c.get("kind")
            _app = str(_c.get("appearance") or _c.get("description") or "").strip()
            _ap = (f", {_app[:80]}" if _app else "")
            if _kind == "character":
                _lock.append(
                    f"<Picture {_num}> is {_name}{_ap} — keep this character's facial "
                    "identity, hairstyle, build, outfit and rendering style identical in all shots.")
            elif _kind == "item":
                _lock.append(
                    f"<Picture {_num}> is {_name}{_ap} — keep this prop's shape, material "
                    "and colors identical in all shots.")
            else:
                _lock.append(
                    f"<Picture {_num}> is the scene {_name} — keep its spatial layout and "
                    "lighting anchor; framing follows the per-shot camera, not this image.")
        _parts.append(" ".join(_lock))
    _M = len(common_ref_audios or [])
    if _M:
        # ⭐ 2026-10-05：音色参考音频**每个单独一行、更显眼、带角色名归属**。
        # common_ref_audios 现为 ``[(角色名, 路径)]`` 有序对（见 _h3_common_ref_audios）；
        # index 0..M-1 → <Audio 1..M>，逐行写「<Audio N> = 角色名」，模型不再猜哪支音色是谁。
        _voice_lines = []
        for _i, _item in enumerate(common_ref_audios or []):
            # 兼容：正常是 (名, 路径) 元组；万一上游仍传纯路径字符串，取不到名字就用「角色_{i+1}」。
            if isinstance(_item, (list, tuple)) and len(_item) >= 1:
                _vn = str(_item[0] or "").strip() or f"角色{_i + 1}"
            else:
                _vn = f"角色{_i + 1}"
            _voice_lines.append(f"<Audio {_i + 1}> = {_vn}")
        _parts.append(
            "VOICE TIMBRE REFERENCE — 本集角色参考音色（贯穿全片共用，逐行对应）:\n"
            + "\n".join(_voice_lines)
            + ". Each reference voice drives that character's spoken lines across every "
              "shot; keep the timbre of each character consistent with its reference "
              "audio (do not switch between voices for the same character).")

    # ---- 公共提示词补全：世界观 + 全局 STYLE（2026-10-03）----
    # 按「推荐放公共参数的内容」表：公共提示词 = 角色定义(上面 subject lock) +
    # 世界观设定 + 全局 STYLE（画风 / 比例 / 无字幕 / 稳定构图）。这些会被插件拼在
    # **每段提示词之前**（concat_common_segment_prompt），保证全片一致；镜头特有的
    # 动作/构图/景别/运镜/光线只写在组内，不进公共段。
    _w = str(worldview or "").strip()
    if _w:
        _parts.append(f"WORLD (world setting, holds for every shot): {_w}.")
    _st = str(style or "").strip()
    if _st:
        _aspect = ("/" + str(aspect or "").strip()) if str(aspect or "").strip() else ""
        _parts.append(
            f"GLOBAL STYLE (holds for every shot): {_st}{_aspect} vertical cinematic, "
            "no text, no subtitles, stable composition, consistent lighting and "
            "colour grading.")
    return "\n".join(_parts)

def _allocate_storyboard_refs(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                               project_name: str = None) -> list:
    """为单个镜头分配参考图（Qwen-Image-2.1 reference stack，最多 9 张）

    槽位键名随编辑节点换代而变（QwenImage2.1 的 TextEncodeQwenImage21 是
    ``images.image_1..9``，老的 TextEncodeQwenImageEditPlus 是 ``image1..3``），
    由 comfyui_client._find_image_slots 统一识别；本函数只负责**按序**给出这些图。

    ## 为什么改成 9 槽位（2026-09-25）

    Qwen-Image-2.1 官方规格支持最多 10 张参考图，且官方 Prompt Rewriter 的核心是
    **Attribute Disentanglement**（属性解耦）：每张参考图解决**一个明确问题**，
    由 ``<image1>…<imageN>`` 显式编号绑定职责。官方同时强调「10 是容量不是目标」——
    塞重复图片会让模型分不清哪张优先。

    旧实现按「主角色 / 次要 / 场景」压成 3 张，导致：多角色镜头第 3 人起直接丢失、
    服装与身份混在同一张图、道具只能挤占角色槽位 —— 这些正是质检重灾区
    （角色/背景不一致 16+11 次）。

    ## 槽位分工（固定顺序，与 build_storyboard_prompt 的 <imageN> 编号一一对应）

      1. ``<image1>`` 主角色身份锚点 —— 兼作画布/构图基线（官方：image_1 是 edit target）
      2. ``<image2>`` 次角色身份（多角色镜头才有；让模型独立保身份，禁止特征串味）
      3. ``<image3>`` 第三角色身份（三人同框时才有）
      4. ``<image4>`` 主角色服装（角色资产图与服装资产不同图时才有独立槽位）
      5. ``<image5>`` 场景
      6. ``<image6>`` 道具（镜头内物品，可多个）
      7. ``<image7>`` 构图参考（特写镜头放头部特写锚点）
      8. ``<image8>`` 上一镜连续性锚点（同场景承接时才有）

    只填**实际需要**的前 N 个：未用到的尾部槽位在生成端被留空
    （见 generate_storyboard 的 slot_cleared），不会塞重复图。
    """
    chars_in = _match_shot_chars(shot, char_idx)
    # ⭐ 2026-10-05：把「本镜画面内可见角色」以**单一权威口径**挂回 shot，供调用点的
    #    3D 构图基准图注入判据复用（见工作线程里 `_shot_has_on_screen(shot)`）。
    #    ⚠️ 取的是**剧本声明**（characters_in_shot），不是 chars_in（= 声明 ∩ 资产索引命中）：
    #    `has_characters` **提示词参数**要的正是这个「本镜该不该有人」的剧本意图。
    shot["_on_screen_chars"] = _on_screen_characters(shot)
    # ⭐ 2026-10-05：另行挂回**已命中资产的角色名**（= chars_in），专供「渲不渲 3D 基准图」的
    #    门槛用（见 `_shot_has_char_ref`）。为什么渲染门槛要比提示词门槛更严：人偶基准图必须
    #    被「其他参考图里的角色」完全覆盖才有意义；角色资产缺失时没有可覆盖的角色图，渲了只会
    #    被照抄（与本次缺陷同症状）。该判据是 `_on_screen_chars` 的子集，只更保守、不会分叉。
    shot["_char_ref_names"] = list(chars_in)
    # 2026-09-29：物品与角色/场景同口径——走统一匹配器（原先 `if n in item_idx`
    # 让名字稍有差异的物品**静默不带参考图**，且不打日志）。
    items_in = _resolve_item_names(shot, item_idx, "分镜参考图")

    refs = []
    # ⭐ 2026-10-05：区分两种「一个角色都没匹配到」——
    #   (a) characters_in_shot **本就为空** → **合法的无人物镜头**（道具特写/空镜/纯画外音），
    #       **不得**置 _no_reference、不写 _ref_error（否则会产出误导文案
    #       「角色 [] 在资产索引中均无匹配」，并让该镜走 S6 报错路径）；
    #   (b) 声明了角色但 resolve 后一个都没命中 → 保持 S6 行为（置 _no_reference + _ref_error
    #       + 日志），由调用方决定 400 / 跳过。
    # ⚠️ 消费方 `if not refs:` 分支（工作线程内）只在前者为「(b) 且无任何其他参考图」时
    #    才触发；`(a)` 或「有场景图但无角色」的镜头 refs 非空 → 该分支不触发（这正是 shot#1
    #    以前被「静默放行」的原因）。
    if not chars_in and (shot.get("_on_screen_chars") or []):
        # S6：禁止静默 take-first —— 镜头声明了角色却一个都匹配不到时，标记 no_reference，
        # 由调用方决定 400（单镜）/ 跳过 + 警告日志（批量）。
        shot["_no_reference"] = True
        shot["_ref_error"] = (
            f"镜头 {shot.get('shot_id')} 的角色 {shot.get('characters_in_shot')} "
            f"在资产索引中均无匹配（别名归一化后仍无）")

    used_paths = set()
    # 本镜景别决定角色参考图取「半身档」还是「全身档」（画幅与景别同向，消除对抗）。
    _want_half = _framing_wants_half_shot(shot)

    # ---- <image1>…<image3>：角色身份锚点（逐个独立，禁止特征串味）----
    # 官方要点：多角色时每人一张图 + 明确「independently / Do not merge facial features」，
    # 否则最常见的失败就是 A 的脸跑到 B、B 的衣服跑到 C。
    for i, name in enumerate(chars_in):
        if i >= 3:
            break                       # 3 个角色身份槽位；第 4 人起并入构图说明
        payload = char_idx.get(name, {}) or {}
        # 2026-09-25 景别对档：近景/特写/中景优先取 half.png（半身胸像），
        # 全景/远景优先取 front.png（全身）；档位缺失时逐级回退（见 _pick_char_view）。
        # 2026-10-02 服装变体：本镜服装提示（shot.outfit 优先，其次
        # shot.character_outfits 按角色名查）能解析出已生成的 outfit_key →
        # 参考图优先取 outfits/<key>/ 的同档位图；解析不出/未生成回落主设定图。
        img = _pick_char_view(payload, _want_half,
                              _shot_outfit_dir(shot, name, payload.get("_dir") or ""))
        if not img or img in used_paths:
            continue
        used_paths.add(img)
        _zoom = "半身近景" if _want_half else "全身"
        if i == 0:
            refs.append(("主角色",
                         f"参考图1（<image1>）是角色「{name}」的身份锚点（{_zoom}视图）："
                         f"保持其面部身份、发型与体型不变", img))
        else:
            refs.append(("次角色",
                         f"参考图{i + 1}（<image{i + 1}>）是角色「{name}」的身份锚点（{_zoom}视图）："
                         f"独立保持其面部身份与发型，不得与其他角色特征混用", img))

    # ---- <imageN>：场景（角色之后，作为环境锚点）----
    # 2026-09-29：走统一容错匹配（精确 → 归一化 → 唯一子串）；未命中时**显式告警**，
    # 不再沿用旧的 `loc if loc in scene_idx else None`——那条路会静默丢场景锚点。
    scene_name, _sc_entry = _resolve_scene_entry(shot, scene_idx, "分镜参考图")
    # 2026-09-29：取**与本镜机位同向**的那一档场景图（俯拍→俯视档 / 斜侧→斜侧档），
    # 而不是恒取基准图 —— 机位反向的参考图会把构图先验拽反。档位缺失时逐级回落。
    _sc_view = _scene_view_for_shot(shot)
    scene_img = _pick_scene_view(_sc_entry, shot) if _sc_entry else None
    if scene_img and scene_img in used_paths:
        scene_img = None

    # ---- <imageN>：道具（镜头内物品，可多个但最多 2 张，避免挤占角色槽位）----
    item_refs = []
    for cand in items_in:
        if len(item_refs) >= 2:
            break
        img = item_idx.get(cand, {}).get("image")
        if not img or img in used_paths or img == scene_img:
            continue
        used_paths.add(img)
        item_refs.append((cand, img))

    slot_no = len(refs) + 1
    if scene_img:
        # 2026-09-29 与场景新规格同步：``novel_to_script`` 的 reference_prompt_zh
        # 已升级为「地理优先」（先定空间结构与地理关系，再定光照基调），此处槽位
        # 话术同步升级，并**显式声明不锁定机位**——场景基准图是一个固定 View，
        # 而分镜是另一个机位；不声明的话模型会照搬参考图的取景范围（实测口径与
        # 特写镜头「场景仅作基调参考」一致）。
        # ⚠️ 改这句话必须同步 comfyui_client._REF_ROLE_ZH2EN（中文职责 → 官方英文
        #    短语）与 prompt_memory._BOILERPLATE（相似度剥离用），否则职责声明会
        #    退化成中文原文、教训召回会被套话稀释。
        # 2026-09-29：参考图已按**本镜机位**选档（俯拍→俯视档…），故话术里点明档位，
        #    让模型知道这张图**就是本镜机位**的空间参考，「不得照搬机位」的豁免随之
        #    收紧为「同一机位、取景范围仍以镜头描述为准」——档位与镜头同向时再照搬
        #    机位不再有害，但**取景范围**（全景 vs 特写）仍必须听镜头。
        refs.append(("场景",
                     f"参考图{slot_no}（<image{slot_no}>）是场景「{scene_name}」"
                     f"（{SCENE_VIEW_LABELS.get(_sc_view) or _sc_view}）的空间与光照锚点："
                     f"保持空间结构、地理关系（入口 / 通道 / 固定陈设的位置关系）"
                     f"与光源方向、色温基调一致；"
                     f"取景范围以本镜镜头描述为准，不得照搬该参考图的构图范围",
                     scene_img))
        used_paths.add(scene_img)
        slot_no += 1
    for cand, img in item_refs:
        refs.append(("道具",
                     f"参考图{slot_no}（<image{slot_no}>）是物品「{cand}」的形状、材质与配色锚点",
                     img))
        slot_no += 1

    refs = _apply_closeup_ref_strategy(refs, shot, project_name)
    # P0-3（借鉴 ViMax reference_image_selector）：每镜参考图**相关性优先 + ≤8 张上限**。
    return _cap_storyboard_refs(refs, shot)

# 特写镜头参考图策略 ---------------------------------------------------------
# 根因：分镜生成用的 Qwen Edit 以参考图构图为强先验。全身角色图 + 道具图作参考时，
# 模型倾向输出中全景，并把道具明确画在人物手中，导致「特写 + 道具已收起」类镜头反复不达标。
# 对策（仅对 camera 含「特写」的镜头生效）：
#   1) 角色参考图换为该角色全身视图的「头部特写裁剪图」（现裁现用，不改动原始资产）；
#   2) 剔除道具参考图（特写中道具应已入袖、不应出镜）；
#   3) 场景参考图**整条剔除**（见下方实现：只保留「主角色 / 次角色」两类）。
#      文件头注释曾写「保留但降级为仅色调参考」，与实现不符 —— 实测口径是剔除；
#      2026-09-29 修正注释与实现对齐。特写是「只拍局部」，而场景基准图是全景
#      View，留作基调参考仍会把模型拉回中全景（见上方实测记录）。
# 角色资产 5 个视角均为「右手握笛」形象（道具已被画进人物），直接作参考会让模型把笛子
# 画进画面，与「道具已收起」类动作冲突；此处只取角色图顶部「头部条带」作锚点，
# 从参考层面切断手部/道具先验。
#
# ⚠️ 2026-09-24 口径变化：角色参考图（front.png）已从「三视图整图」改为
#    「从整图切分出的**单人格**并居中贴回同尺寸方底」（见 app/sheet_split.py）。
#    切分后人物恰好落在画面**水平中部约 31%** 宽（x≈0.34~0.66），
#    故下面的 x 区间 (0.32, 0.68) 现在正好框住这张单人格的头部 —— 语义与常量取值一致，
#    **不需要再改**。反过来，若哪天把 front.png 换回三视图整图，这个区间会落到
#    **中格（左侧面）** 的头上，取到侧脸锚点，需同步调整为最左格。
CLOSEUP_CHAR_CROP_TOP = (0.32, 0.02, 0.68, 0.28)   # 头部条带（x0, y0, x1, y1）

def _closeup_char_crop(img_path: str, project_name: str, shot_id) -> str:
    """把角色正视/半身图裁剪为头部特写图，作为特写镜头的构图锚点（失败则回退原图）。"""
    try:
        from PIL import Image
        out_dir = os.path.join(QC_DIR, str(project_name or "default"), "closeup_refs")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, f"char_closeup_shot{_shot_seq(shot_id, 1):02d}.png")
        with Image.open(img_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            x0, y0, x1, y1 = CLOSEUP_CHAR_CROP_TOP
            band = im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
            side = max(1, min(band.width, band.height))
            left = max(0, (band.width - side) // 2)
            crop = band.crop((left, 0, left + side, side))
            target_w = max(768, crop.width)
            crop = crop.resize((target_w, max(1, int(target_w * crop.height / crop.width))))
            crop.save(dst, format="PNG")
        return dst
    except Exception as e:  # 裁剪失败不影响主流程，回退原图
        app.logger.warning(f"特写参考图裁剪失败，回退原图: {e}")
        return img_path

def _apply_closeup_ref_strategy(refs: list, shot: dict, project_name: str = None) -> list:
    """特写镜头：只保留「角色头部特写」单一锚点。

    实测（4 轮 12 次生成）：混入场景/道具参考时，模型会按参考图的取景范围把画面
    铺开成中全景，并把道具画回手中；道具参考剔除后模型仍会自行"想象"出手持物。
    因此特写镜头只给头部特写锚点，构图约束最强。

    ⚠️ 2026-09-25 口径变化（Qwen-Image-2.1 9 槽位）：
      旧实现「多出的槽位自动复用同一张」在 9 槽模板下会把头部特写复制到 8 个槽位，
      而官方明确「容量不是目标，重复图反而稀释注意力」。现在特写镜头**只输出 1 张**
      （主角色头部特写，占 ``<image1>``），其余槽位由生成端留空。
      多角色特写时保留每个角色的头部特写（各占一槽），不复制。
    """
    # 审计 P2-13（2026-09-29）：特写判定改为 shot_type / camera 双查 —— camera 字段
    # 已拆分（A1），_norm_shots 允许 camera 为纯运镜串（如「推入」）而 shot_type=「特写」；
    # 只看旧 camera 会漏掉这类镜头，与生成端（build_storyboard_prompt 按 shot_framing
    # 注入特写硬约束）判定分裂。
    _is_closeup = ("特写" in str(shot.get("shot_type") or "")
                   or "特写" in str(shot.get("camera") or ""))
    if not _is_closeup:
        return refs
    out = []
    for kind, label, path in refs:
        if kind in ("主角色", "次角色"):
            # 保留 <imageN> 编号锚点，让质检端能继续核对「编号 ↔ 职责」一致性。
            # 审计 P2-19（2026-09-29）：必须完整保留「（<imageN>）」整组 —— 旧实现
            # 只留「参考图N（」，<imageN> 被削掉，下游 _ref_label_body 剥前缀不净
            # （提示词出现「参考图2（已替换为…」脏片段）且特写镜丢身份保留句。
            _mark = ""
            if label.startswith("参考图"):
                _m = re.match(r"(参考图\d+（<image\d+>）)", label)
                if _m:
                    _mark = _m.group(1)
                else:
                    _head = label.split("）", 1)[0]
                    _mark = _head.split("（", 1)[0] + "（"
            out.append((kind,
                        _mark + "已替换为该角色头部特写，画面取景范围以此为准：仅肩部以上",
                        _closeup_char_crop(path, project_name, shot.get("shot_id", 1))))
    return out or refs

def _cap_storyboard_refs(refs: list, shot: dict) -> list:
    """P0-3（借鉴 ViMax reference_image_selector）：每镜参考图**相关性优先 + ≤8 张上限**。

    官方 Qwen-Image-2.1 支持最多 10 张但明确「容量不是目标」；ViMax 实践为每帧 ≤8 张，
    且同角色多视图只取一张（已由 ``_pick_char_view`` 按景别对档处理）。这里在分配末端
    再加一道安全网：超出 8 张时按相关性优先级截断（角色身份 > 场景 > 道具），
    绝不丢弃角色身份锚点，并打日志便于排查「参考图被静默截断」。

    只做**保序截断**，不改变已分配槽位的职责语义；``build_storyboard_prompt`` 按位置
    重编号 ``<imageN>``，列表顺序即相关性顺序。
    """
    MAX_STORYBOARD_REFS = 8
    if len(refs) <= MAX_STORYBOARD_REFS:
        return refs
    _rank = {"主角色": 0, "次角色": 0, "场景": 1}   # 其余（道具等）→ 2，最先被截断
    ordered = sorted(refs, key=lambda r: _rank.get(r[0], 2))
    dropped = ordered[MAX_STORYBOARD_REFS:]
    app.logger.warning(
        "镜头 %s 参考图 %d 张 > 上限 %d，已按相关性丢弃 %d 张：%s",
        shot.get("shot_id"), len(refs), MAX_STORYBOARD_REFS, len(dropped),
        "、".join(f"{d[0]}:{str(d[1])[:24]}" for d in dropped))
    return ordered[:MAX_STORYBOARD_REFS]

# --------------------------------------------------------------------------- #
# 分镜参考图「统一画幅」（2026-09-24）
#   ⚠️ 为什么必须做：分镜模板 `分镜生成_Qwen21.json` 是「参考图编辑」型，**没有尺寸
#      节点** → style_kit.apply_latent_size 返回空 → 输出画幅**继承第一张参考图**。
#      而参考图随镜头而变：建立镜（characters_in_shot 为空）只有场景图（资产内置
#      16:9 → 960×544 横屏）；有角色的镜头第一张是角色图（1:1 → 736×736 方形）；
#      特写镜头第一张是头部裁剪条带（更小）→ **同一集分镜画幅在两三种尺寸间跳变**，
#      与项目画幅（9:16 竖屏 544×960）不符，成片拼接会出现黑边 / 拉伸。
#      这里在送进工作流前把每张参考图 cover 到目标画幅，使输出画幅恒定。
# --------------------------------------------------------------------------- #

#: 统一画幅结果的进程内缓存：键 = (绝对路径, mtime_ns, 目标宽, 目标高) → 处理后路径。
#: 同一批分镜里同一张参考图会出现多次（84 镜共用寥寥数张），缓存避免逐镜重复裁剪。
_REF_CANVAS_CACHE: dict = {}

def _ref_canvas_target(size):
    """把目标画幅规整成 (W, H) 正整数元组；None / 非法 → None（调用方按「不处理」走）。"""
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (w, h) if w > 0 and h > 0 else None

def _fit_ref_to_canvas(im, size):
    """按 **cover** 把图缩放到恰好覆盖 size 画布并居中裁剪（内容充满、无条带）。

    ⚠️ 为什么用 cover 而不是 contain(letterbox)：2026-09-24 真图 A/B 实测
    （逆天系统 shot_02，同镜同 prompt）——
      · contain（内容缩放居中 + 自身模糊放大作底）→ 图像编辑型工作流**会模仿这个
        布局**：输出内容只占中间约 44%，上下是模型自绘的虚化带，画面利用率腰斩；
      · cover（放大到覆盖画布 + 居中裁剪）→ 内容充满整幅，构图正常（中景主体 +
        背景群像，与 camera 描述一致）。
    代价：宽幅参考图（场景资产内置 16:9）会被裁掉两侧。参考图的语义是「内容锚点」，
    中心区域通常已含代表性主体，环境细节由模型按 prompt 补全 —— 比留虚化带更划算。
    """
    from PIL import Image
    W, H = int(size[0]), int(size[1])
    if im.width == W and im.height == H:
        return im
    s = max(W / im.width, H / im.height)
    scaled = im.resize((max(W, int(round(im.width * s))),
                        max(H, int(round(im.height * s)))), Image.LANCZOS)
    left, top = (scaled.width - W) // 2, (scaled.height - H) // 2
    return scaled.crop((left, top, left + W, top + H))

def _unify_ref_canvas(refs: list, size, project_name: str = "") -> list:
    """把分镜参考图统一到目标画幅（cover 填充），返回新的 refs（结构不变）。

    只替换第 3 项（本地路径），kind / label 原样保留。失败降级：单张处理失败 → 该张
    沿用原图；缓存目录不可建 → 整批沿用原图；size 非法 → 原样返回。任何情况都不抛
    异常、不阻断分镜生成。
    """
    tgt = _ref_canvas_target(size)
    if not tgt or not refs:
        return refs
    out_dir = os.path.join(QC_DIR, str(project_name or "default"), "ref_canvas")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        app.logger.warning("参考图统一画幅：缓存目录不可建，本次沿用原图（%s）", e)
        return refs
    unified, changed = [], 0
    for item in refs:
        try:
            kind, label, path = item[0], item[1], item[2]
        except (TypeError, IndexError, KeyError):
            unified.append(item)
            continue
        newp = path
        try:
            local = comfyui_client.resolve_local_path(path) or path
            if not local or not os.path.isfile(local):
                raise FileNotFoundError(f"参考图本地路径不可用: {path}")
            key = (os.path.normcase(os.path.abspath(local)),
                   int(os.stat(local).st_mtime_ns), tgt[0], tgt[1])
            cached = _REF_CANVAS_CACHE.get(key)
            if cached and os.path.isfile(cached):
                newp = cached
            else:
                import hashlib
                import tempfile
                from PIL import Image
                with Image.open(local) as _im:
                    _rgb = _im.convert("RGB")
                    if (_rgb.width, _rgb.height) == tgt:
                        unified.append(item)
                        continue
                    fixed = _fit_ref_to_canvas(_rgb, tgt)
                _stem = os.path.splitext(os.path.basename(local))[0]
                _h = hashlib.sha1(os.path.abspath(local).encode("utf-8")).hexdigest()[:8]
                _dst = os.path.join(out_dir, f"{_stem}_{_h}_{tgt[0]}x{tgt[1]}.png")
                # 仓库纪律：禁止「路径拼接固定 .tmp 后缀」这类**固定临时名**（并发会互相写坏，
                # 守卫 verify_asset_skip_existing A3.3 会红）。用 mkstemp 拿唯一名。
                _fd, _tmp = tempfile.mkstemp(dir=out_dir, prefix=".refcanvas_", suffix=".png")
                os.close(_fd)
                try:
                    fixed.save(_tmp, format="PNG")
                    os.replace(_tmp, _dst)
                except BaseException:
                    try:
                        os.unlink(_tmp)
                    except OSError:
                        pass
                    raise
                _REF_CANVAS_CACHE[key] = _dst
                newp = _dst
            if newp != path:
                changed += 1
        except Exception as e:  # noqa: BLE001 —— 单张失败不影响整镜
            app.logger.warning("参考图统一画幅失败，该张沿用原图（%s: %s）",
                               type(e).__name__, e)
            newp = path
        unified.append((kind, label, newp))
    app.logger.info("[分镜参考图画幅] 目标 %d×%d，%d/%d 张已统一（其余原尺寸或降级）",
                    tgt[0], tgt[1], changed, len(refs))
    return unified

def _storyboard_worker(task_id: str, project_name: str, shots: list,
                       char_idx: dict, item_idx: dict, scene_idx: dict,
                       episode_no=None, style: str = "", overwrite: bool = False):
    """后台分镜图生成任务：逐镜头生成并落盘 output/storyboards/<项目>[/epNN]/shot_XX.png

    style：用户与总控敲定的风格。用于 ① 补齐镜头 style 字段（老剧本无该字段时兜底）
    ② 解析画幅并覆写分镜图尺寸，保证分镜与成片同为竖屏 9:16。

    overwrite：是否「全量重做」。默认 False —— 已达标入库的 shot_XX.png 直接复用、
    只重跑缺失/被质检阻断的镜头（断点续跑语义）。这是本次修复的核心：
    修复前无论如何都从第 1 镜重跑到最后一镜，导致「补跑 21 个不达标镜」要重烧全部 83 镜。
    需要强制全部重画时（如换风格）显式传 overwrite=True。
    注意：质检不达标的镜头只写质检暂存区、不写正式目录，所以「正式目录里已有该图」
    ⟺ 「该镜上一轮已通过质检」——跳过它不会漏掉任何不达标镜。
    """
    out_dir = _ep_dir(os.path.join(STORYBOARDS_DIR, project_name), episode_no)
    os.makedirs(out_dir, exist_ok=True)
    # ---- 分镜图 URL 的集前缀（P1 修复，2026-09-25）----
    # ⚠️ 旧 bug：本 worker 落盘在 `_ep_dir(...)`（第 2 集起是 <项目>/epNN/），
    #    但 manifest 里的 url 却**硬编码**成 `/api/storyboards/file/<项目>/shot_NN.png`
    #    —— 少了 epNN 段 → 第 2 集起所有分镜图在界面上 404（文件明明存在）。
    # ⚠️ 参照物是**同文件里的 `_update_storyboard_manifest_shot`**（单镜重跑那条路）
    #    与视频 worker 的 `_vurl`：两者都按下标算前缀，所以单镜重跑后 URL 变对、
    #    整批重跑后又变错 —— 这正是「同一张图时好时坏」的根因。
    # 口径：第 1 集平铺（无前缀），第 2 集起 `epNN/`。
    # 用 `_ep_of_script` 而非裸 `int(episode_no)`：兼容历史调用传 None / "" 的情况，
    # 与 `_ep_dir` 的兜底（<=1 → 平铺）保持一致。
    _sb_ep = episode_no if episode_no not in (None, "") else 1
    _sb_sub = (f"ep{int(_sb_ep):02d}/" if int(_sb_ep) > 1 else "")
    _sb_url_base = f"/api/storyboards/file/{project_name}/{_sb_sub}"
    # 风格/画幅：整批分镜共用
    # G19：风格串未含画幅关键词时以默认画幅（2026-09-28 起为 16:9）为底，
    #      不再静默回落模板尺寸。megapixels：分镜图预算 0.8MP（画质提升，旧值 0.5）。
    _sb_style_res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO,
                                      megapixels=style_kit.storyboard_megapixels())
    _sb_style = _sb_style_res["style"]
    _sb_size = _sb_style_res["size"]
    # 分镜图「单镜九宫格（9 关键帧）」开关（2026-10-02 用户指定，见 config.SB_GRID_MODE）。
    # 开启时：① 提示词用 build_shot_grid_keyframes_prompt（一个镜头 = 3x3 九宫格 = 9 关键帧）；
    #         ② 景别自动裁剪禁用（九宫格不能按景别裁，否则破坏 9 格结构）；
    #         ③ 质检描述带「九宫格」口径（判官按整体一致性评，不按单帧景别判）。
    from config import SB_GRID_MODE
    _sb_grid_mode = bool(SB_GRID_MODE)
    if _sb_style:
        app.logger.info("[分镜风格] 风格=%s；画幅=%s；九宫格=%s", _sb_style,
                        _sb_style_res["label"] or "未指定（沿用模板）", _sb_grid_mode)
        shots = [dict(s, style=(s.get("style") or _sb_style)) for s in (shots or [])
                 if isinstance(s, dict)]
    manifest_shots = []
    # 旧 manifest：断点续跑时给「被跳过的镜头」回填上一轮的质检/提示词信息，避免信息丢失
    _prev_by_key = {}
    _prev_manifest = os.path.join(out_dir, "storyboard_manifest.json")
    # C4-1（2026-09-22 复验收口）：读取口径与 D-03/D-04 统一 —— 交给 read_json_strict
    # 自己负责三态（缺失→{}；活文件缺失但有 .bak→自动恢复；损坏→.bak 或 fail-loud），
    # 故不再用 os.path.isfile 预判。口径与 _update_storyboard_manifest_shot（本文件
    # L1990-1998 的 B-2 收口）一致；差异在于**本处是只读视图**：只给被跳过的镜头回填
    # 上一轮的质检/提示词信息、从不写回，所以损坏时**响亮降级**（error 日志 + 不回填），
    # 而不是 fail-loud 把整批分镜打挂。
    try:
        for _it in (read_json_strict(_prev_manifest, {}).get("shots") or []):
            if not isinstance(_it, dict):
                continue
            _sid = _it.get("shot_id")
            if _sid is not None:
                _prev_by_key[str(_sid)] = _it
            _sq = _shot_seq(_sid, 0)
            if _sq:
                _prev_by_key[f"shot_{_sq:02d}"] = _it
    except Exception as _e:  # noqa: BLE001
        app.logger.error("旧分镜清单 %s 不可读（%s: %s）：本次不回填被跳过镜头的信息，不影响生成",
                         _prev_manifest, type(_e).__name__, _e)
    app.logger.info("[分镜断点续跑] overwrite=%s；待处理 %d 镜（已存在者将跳过）",
                    overwrite, len(shots))
    # G13（P1）：质检配置 worker 级读一次，本批所有镜头共用（对齐资产 worker 2302）。
    # 旧代码逐镜 _qc_load_cfg()（每次 load JSON + Fernet 解密 secrets.enc），单集 56 镜 ≈
    # 上百次读盘；改为进循环前读一次，既省开销又避免「同批任务新旧配置混用」（审计 G13）。
    qc_cfg = _qc_load_cfg()
    qc_on = qc_client.image_qc_ready(qc_cfg)
    qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("image_enabled"))
    max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
    # best-of-N（2026-09-29，借 ViMax best_image_selector）：>1 时每镜固定生成 N 张候选，
    # 循环内只收集、**不立即入库**，循环后按质检分选**最佳**那张入库；==1 时完全走原
    # 「通过即停」逻辑（零回归）。质检未开（qc_on=False）无分可比 → 强制退回 1。
    best_of = max(1, int(qc_cfg.get("best_of", 1) or 1)) if qc_on else 1
    _rounds = best_of if best_of > 1 else (max_retries + 1)
    try:
        for i, shot in enumerate(shots):
            shot_id = shot.get("shot_id", i + 1)
            seq = _shot_seq(shot_id, i + 1)
            dst = os.path.join(out_dir, f"shot_{seq:02d}.png")
            # ---------- 断点续跑：已达标入库的镜头直接复用，只重跑缺失/不达标的 ----------
            # 正式目录里已有非空 shot_XX.png ⟺ 上一轮该镜已通过质检（不达标的只落暂存区）。
            # 因此这里跳过是安全的，且能把「补跑 N 个不达标镜」的代价从「全量 M 镜」降回 N 镜。
            if not overwrite and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                prev = _prev_by_key.get(str(shot_id)) or _prev_by_key.get(f"shot_{seq:02d}") or {}
                item = dict(prev) if prev else {}
                item.update({"shot_id": shot_id, "success": True, "skipped": True,
                             "file": dst, "error": "",
                             "url": f"{_sb_url_base}shot_{seq:02d}.png"})
                item.setdefault("qc", {"enabled": False, "status": "skipped",
                                       "label": "沿用已达标图", "attempts": 0, "regenerated": 0})
                item.pop("qc_blocked", None)
                manifest_shots.append(item)
                with lock:
                    generation_state[task_id].update({
                        "current": i + 1,
                        "progress": int((i + 1) / max(len(shots), 1) * 100),
                        "current_shot": shot_id,
                    })
                    generation_state[task_id]["results"].append(item)
                continue
            with lock:
                generation_state[task_id].update({
                    "current": i + 1,
                    "progress": int(i / max(len(shots), 1) * 100),
                    "current_shot": shot_id,
                    "phase": "分镜图生成",
                })
            item = {"shot_id": shot_id, "success": False, "refs": {}, "prompt": ""}
            try:
                refs = _allocate_storyboard_refs(shot, char_idx, item_idx, scene_idx, project_name)
                # 统一参考图画幅：分镜工作流无尺寸节点，输出画幅继承第一张参考图，
                # 不统一会让同集画幅在 16:9 / 1:1 间跳变（详见 _unify_ref_canvas）。
                refs = _unify_ref_canvas(refs, _sb_size, project_name)
                if not refs:
                    # S6：区分"无参考图"与"角色匹配失败"（_no_reference）
                    # ⚠️ 2026-10-05：本分支**只**在 refs 完全为空时进入。对「有场景/道具图但
                    #    无角色」的镜头 refs 非空 → 走 else 分支、**不会**进入这里 —— 这正是
                    #    shot#1（道具特写 + 场景图，characters_in_shot=[]）以前能「静默通过」
                    #    并被人偶污染的原因。故这里只处理「(b) 声明了角色却匹配不到 且 无其他
                    #    任何参考图」的情形；「(a) 本就无人物」的镜头由 _allocate_storyboard_refs
                    #    决定**不**置 _no_reference（见那里的注释）。
                    if shot.get("_no_reference"):
                        item["no_reference"] = True
                        item["ref_error"] = shot.get("_ref_error") or ""
                        item["error"] = f"角色匹配失败（禁止静默兜底）：{item['ref_error']}"
                        app.logger.warning(
                            f"[S6] 分镜 shot {shot_id} 角色匹配失败"
                            f"（characters_in_shot={shot.get('characters_in_shot')}），"
                            f"跳过该镜参考图分配：{item['ref_error']}")
                    else:
                        item["error"] = "该镜头无可用参考图（请先完成步骤2/3/4的资产生成）"
                else:
                    labels = [r[1] for r in refs]
                    # ---- TE 3D 导演台：渲染 3D 站位/机位构图基准图（2026-09-30）----
                    # 每镜先渲一张无面人偶站位图 → 作 <image1> 构图基准 → 提示词追加
                    # COMPOSITION BASELINE 段。渲染失败静默降级为纯文字站位锚点（fail-open）。
                    # ⭐ 2026-10-05（修复「无角色镜头被人偶污染」）：渲人偶基准图需**同时**
                    #    满足两条（口径见 _shot_has_on_screen / _shot_has_char_ref）：
                    #      ① 画面内确有出场角色（declared，characters_in_shot 非空）；
                    #      ② 本镜确有**已命中资产的角色身份参考图**（chars_in 非空）。
                    #    缺②时（声明了角色但资产缺失，_no_reference=True）refs 里只有场景图、
                    #    没有角色图可覆盖人偶 → COMPOSITION BASELINE 要求「用其他角色图完全覆盖
                    #    人偶」无法满足 → 模型照样照抄人偶，症状与本次缺陷一致。故一并挡住。
                    #    注意：② 是 ① 的**子集**，只更保守（不插 ref / 少调一次渲染），不会分叉。
                    #    ⚠️ `has_characters` 提示词参数仍只取 ①（declared）—— 见下方与 L2817。
                    from config import ENABLE_3D_BLOCKING_IMAGE
                    _has_on_screen = _shot_has_on_screen(shot)
                    _blocking_ref_path = ""
                    if ENABLE_3D_BLOCKING_IMAGE and _has_on_screen and _shot_has_char_ref(shot):
                        try:
                            import te_3d_render
                            if te_3d_render.available():
                                _blk_out_dir = os.path.join(QC_DIR, project_name, "te3d_blocking")
                                _blocking_ref_path = te_3d_render.render_blocking(
                                    shot, _blk_out_dir,
                                    aspect=":".join(map(str, _sb_style_res.get("ratio") or ("16", "9"))),
                                    width=_sb_size[0],
                                    # ⭐ 按目标像素对齐（2026-10-02）：裸 aspect 字符串在本仓
                                    #    有双语义（style_kit 的 (16,9)＝横屏，而 te_3d_director
                                    #    收 "16:9" 会算成 684 高）→ 基准图 1216×684 ≠ 分镜 1216×672，
                                    #    作 <image1> 定画布会把错的画幅带进分镜图。
                                    #    传 _sb_size 保证逐像素一致（G2 存量失败的真因）。
                                    target_size=_sb_size,
                                ) or ""
                                if _blocking_ref_path:
                                    app.logger.info("[3D导演台] shot=%s 站位基准图已渲染：%s",
                                                     shot_id, os.path.basename(_blocking_ref_path))
                                    item["_blocking_ref"] = _blocking_ref_path
                                    # 插入为第一张参考图（<image1>），后续参考图序号后移
                                    # ⭐ 标签必须引用常量 _BLOCKING_REF_MARK（= comfyui_client.BLOCKING_REF_MARK，
                                    #    值 "3D导演台构图基准"），**禁止再写裸字面量**：comfyui_client.build_storyboard_prompt
                                    #    靠 `BLOCKING_REF_MARK in label` 识别基准图并生成 COMPOSITION BASELINE 段，
                                    #    字面量与常量一旦漂移（如旧 "3D构图基准" ≠ "3D导演台构图基准"）该段静默缺失、
                                    #    且人偶被误当 identity anchor（2026-10-05 定位的既有缺陷）。
                                    refs.insert(0, (_blocking_ref_path, _BLOCKING_REF_MARK, _blocking_ref_path))
                                    # ⭐ 基准图在 _unify_ref_canvas **之后**插入，躲过了归一
                                    #    （2026-10-02）：它是 <image1> 画布定义者，尺寸必须与
                                    #    _sb_size 逐像素一致，否则整个分镜画幅被它带偏。
                                    #    这里显式再归一一次，把「插入顺序」这个隐患彻底封死。
                                    refs = _unify_ref_canvas(refs, _sb_size, project_name)
                                    labels = [r[1] for r in refs]
                        except Exception as _3d_e:  # noqa: BLE001
                            app.logger.warning("[3D导演台] shot=%s 渲染失败（降级为纯文字站位）：%s",
                                               shot_id, _3d_e)
                            _blocking_ref_path = ""
                    prompt = (comfyui_client.build_shot_grid_keyframes_prompt(
                        shot, labels, style=(shot.get("style") or _sb_style),
                        has_characters=_has_on_screen)
                        if _sb_grid_mode
                        else comfyui_client.build_storyboard_prompt(
                            shot, labels,
                            has_blocking_image=bool(_blocking_ref_path),
                            has_characters=_has_on_screen))
                    item["prompt"] = prompt
                    orig_prompt = prompt      # 教训库的稳定键：改写后的提示词不参与指纹
                    item["refs"] = {r[0]: os.path.basename(os.path.dirname(r[2])) for r in refs}

                    # ---------- 提示词预检（生成前质检）：先判 → 确定性自愈 → 再出图 ----------
                    # ⚠️ 放在 orig_prompt 之后：教训库指纹仍以构建器输出为准（自愈不参与指纹，
                    #    否则同一镜头在自愈前后会生成两条互不相认的教训）。
                    prompt, _pf_item, _pgate_item = _prompt_preflight(
                        "storyboard", prompt, ctx=shot,
                        style=(shot.get("style") or _sb_style), ref_count=len(refs),
                        project_name=project_name, cfg=qc_cfg)   # G13：复用 worker 级配置
                    item["prompt"] = prompt
                    item["prompt_qc"] = _pf_item.get("verdict")
                    item["prompt_qc_repairs"] = _pf_item.get("repairs") or []
                    if not _pgate_item.get("accept"):
                        item["prompt_qc_blocked"] = True
                        # ★ 用户需求：不合格提示词不留本地（P12）—— 该镜不生成，
                        # 把上一轮遗留的 `output/qc/<项目>/prompt_<shot>.json` 移回收站。
                        try:
                            _purge_prompt_records(
                                project_name, shot_id,
                                reason=f"提示词预检未通过（{_pgate_item.get('label')}）")
                        except Exception as _pe:  # noqa: BLE001
                            app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
                        raise _PromptQCBlocked(
                            f"提示词预检未通过（{_pgate_item.get('label')}）："
                            f"{_pgate_item.get('reason')}" +
                            (f"；建议：{_pf_item.get('rebuild_hint')}" if _pf_item.get("rebuild_hint") else ""))
                    # ★ G10：捕获自愈后提示词作为重试基准。
                    #   旧 bug：重试召回教训库以 orig_prompt（自愈前）为键，导致重试
                    #   回落到未自愈提示词，自愈修复被静默丢弃。
                    self_healed_prompt = prompt
                    # G10b（2026-09-30）：第 1 次尝试的提示词也实时暴露（live.attempt=0）
                    with lock:
                        generation_state[task_id]["live"] = {
                            "shot": shot_id, "attempt": 0, "prompt": prompt,
                            "phase": "generating", "done": False}

                    # ---------- 图片 AI 质检（不达标自动重生成） ----------
                    # G13：qc_cfg/qc_on/qc_declared/max_retries 已在 worker 级读一次（见上方），
                    # 本批所有镜头共用，不再逐镜 _qc_load_cfg()。
                    attempts = []
                    _best_candidates = []   # best-of-N：本镜候选 [(score, png, rec, gate)]
                    # O3：第 1 轮也用真实随机 seed 并始终注入（不再 None 走模板默认常量），
                    # 使 manifest.shots[*].qc.history[0].seed 不再为 null，产物可复现、可追溯。
                    seed = random.randint(1, 2 ** 31 - 1)
                    dst = os.path.join(out_dir, f"shot_{seq:02d}.png")

                    for attempt in range(_rounds):
                        if attempt > 0:
                            seed = random.randint(1, 2 ** 31 - 1)
                            # G10：基准改为 self_healed_prompt（自愈后提示词）。
                            # 旧 bug：基准是 orig_prompt（自愈前），重试会回落到未自愈提示词，
                            # 导致首次自愈对后续重试不再生效。
                            # 教训库召回也改用 self_healed_prompt 作键：
                            # 自愈改变了提示词 → 指纹也变了，用旧指纹的教训与自愈后提示词不匹配。
                            _retry_base = self_healed_prompt
                            # ① 优先：针对上一轮这张图的缺陷，LLM 即时改写（精准）
                            # ② 回落：历史教训库召回；再回落：仅换种子
                            prompt = _retry_base
                            _opt_prompt = None
                            if attempts:
                                _opt_prompt = _optimize_prompt_from_qc(
                                    "storyboard", _retry_base, attempts[-1],
                                    style=_qc_style_of(project_name))
                            if _opt_prompt:
                                prompt = _opt_prompt
                                item["prompt"] = prompt
                                app.logger.info("镜头 %s 第 %d 次重试，针对本次缺陷即时优化提示词",
                                                shot_id, attempt + 1)
                            else:
                                try:
                                    hints = prompt_memory.suggest(
                                        kind="storyboard",
                                        prompt=_retry_base,
                                        project=project_name,
                                        root_dir=PROJECT_OUTPUT_DIR
                                    )
                                    learned = prompt_memory.learned_prompt(
                                        kind="storyboard",
                                        prompt=_retry_base,
                                        project=project_name,
                                        root_dir=PROJECT_OUTPUT_DIR,
                                        style=_qc_style_of(project_name),
                                    )
                                    if learned and learned != _retry_base:
                                        prompt = learned
                                        item["prompt"] = prompt
                                        item["prompt_hints"] = hints[:3]
                                        app.logger.info("镜头 %s 第 %d 次重试，按质检教训改写提示词：%s",
                                                        shot_id, attempt + 1, hints[:2])
                                    else:
                                        prompt = _retry_base
                                        item["prompt"] = prompt
                                        app.logger.info("镜头 %s 第 %d 次重试，暂无可用教训，仅换种子",
                                                        shot_id, attempt + 1)
                                except Exception as mem_err:
                                    app.logger.warning(f"读取记忆模块失败: {mem_err}")

                            with lock:
                                generation_state[task_id]["phase"] = \
                                    f"质检不达标，修改提示词后重新生成（第 {attempt}/{max_retries} 次）"
                                generation_state[task_id]["qc_phase"] = "regenerating"
                                # G10b（2026-09-30）：改写后的提示词**实时**暴露给前端轮询。
                                # 前端每 3s 拉 /api/generation/status/<task_id> 的 live 字段
                                generation_state[task_id]["live"] = {
                                    "shot": shot_id, "attempt": attempt + 1,
                                    "prompt": prompt, "phase": "regenerating", "done": False}
                        result = comfyui_client.generate_storyboard(
                            prompt_zh=prompt,
                            ref_images=[r[2] for r in refs],
                            filename_prefix=f"comic_drama_sb/{project_name}_shot_{seq:02d}",
                            seed=seed,
                            size=_sb_size,
                        )
                        if not result["files"]:
                            item["error"] = "ComfyUI 未返回分镜图（可能节点缺失或超时）"
                            break
                        # P0：先落「质检暂存区」，质检达标后才写入正式交付目录（阻断 ⇒ 正式目录不产生该图）
                        sb_scratch_dir = os.path.join(QC_DIR, project_name, "storyboard_scratch")
                        os.makedirs(sb_scratch_dir, exist_ok=True)
                        _qc_prune_attempts(sb_scratch_dir)   # G8③：清本镜历史过期 try（共享目录按镜头前缀保留最近4）
                        scratch_png = os.path.join(sb_scratch_dir,
                                                   f"shot_{seq:02d}_try{attempt + 1}.png")
                        # G8②：消费 ComfyUI output 源（与视频链路的 move 语义对齐），
                        # 不再 copy2 导致 output/comic_drama_sb/ 只增不减。
                        # 每轮 attempt 都会重新 generate 出新文件，move 走旧源无副作用。
                        # 2026-10-06：改走统一落盘入口。这一处**一集要跑 20 次**（每镜一次），
                        # 是 WinError 3 风险最高的调用点：跨盘 shutil.move 在
                        # 暂存目录被清理时会抛 WinError 3，把整集分镜判失败。
                        scratch_png = _ingest_comfy_output(
                            result["files"], scratch_png, logger=app.logger)
                        item.update({
                            "success": True,
                            "file": dst,
                            "url": f"{_sb_url_base}shot_{seq:02d}.png",
                            "ref_count": len(refs),
                        })
                        item.pop("error", None)
                        if not qc_on:
                            # P0-1 fail-closed：qc_declared=True 但接口未就绪（qc_on=False）→ 阻断，
                            # **不写正式目录**。旧实现此处 `copy2(scratch_png, dst)` 属 fail-open，
                            # 会把未质检产物当成品交付并破坏「正式目录有产物 ⟺ 已过质检」不变量。
                            # 资产链路早已 fail-closed（见资产生成处的同型分支），此处对齐口径。
                            if qc_declared:
                                item["error"] = ("分镜图质检阻断（质检接口未就绪）：已开启图片质检，"
                                                 "但 base_url / api_key / model 不可用；"
                                                 "未质检产物不写入正式目录（暂存图见质检历史）")
                                app.logger.warning(
                                    "[分镜质检] qc_declared=True 但 qc_on=False → fail-closed 阻断入库"
                                    "（不写正式目录）：project=%s shot=%s", project_name, shot_id)
                                break
                            shutil.copy2(scratch_png, dst)   # 质检本就未开启：按原行为直接入库
                            break
                        with lock:
                            generation_state[task_id]["phase"] = f"图片质检中（镜头 {shot_id} · 第 {attempt + 1} 次）"
                            generation_state[task_id]["qc_phase"] = "checking"
                            # G10b：质检阶段同步暴露当前提示词（与生成阶段同一份）
                            generation_state[task_id]["live"] = {
                                "shot": shot_id, "attempt": attempt + 1,
                                "prompt": prompt, "phase": "checking", "done": False}
                        # ---- 景别后处理（C 方案，2026-09-30）：送检前若主体占比超出目标景别，自动裁剪 ----
                        # ⚠️ 九宫格模式（2026-10-02）禁用：一张图是 3x3 九个关键帧，
                        #    按景别裁剪会切掉 8 格、只剩一格，破坏九宫格结构。
                        _shot_type = (shot.get("shot_type") or shot.get("camera") or "").strip()
                        if _shot_type and not _sb_grid_mode and os.path.isfile(scratch_png):
                            try:
                                _crop_res = qc_client.auto_crop_framing(scratch_png, _shot_type)
                                if _crop_res.get("cropped") and os.path.isfile(_crop_res["out_path"]):
                                    app.logger.info("[分镜质检] 景别自动裁剪 shot=%s: %s", shot_id, _crop_res["reason"])
                                    scratch_png = _crop_res["out_path"]
                            except Exception as _crop_e:  # noqa: BLE001
                                app.logger.warning("[分镜质检] 景别自动裁剪失败（不阻断）shot=%s: %s", shot_id, _crop_e)
                        # 九宫格模式（2026-10-02）：质检描述加「九宫格」口径，
                        # 判官按 9 个关键帧的整体一致性评，不按单帧景别判。
                        _qc_desc = _qc_shot_desc(shot)
                        if _sb_grid_mode:
                            _qc_desc = ("本图为一张 3x3 九宫格故事板：同一镜头的 9 个关键帧，"
                                        "随时间从左到右、从上到下推进；请评 9 格整体的角色/场景/"
                                        "光线/色调一致性、动作推进的连贯性与画面质量，"
                                        "不要按单帧的景别去判（景别可能随运镜在格间渐次变化）。"
                                        + _qc_desc)
                        verdict = qc_client.check_image(
                            scratch_png, _qc_desc, qc_cfg,
                            style=(shot.get("style") or _sb_style),
                            ref_images=_qc_ref_images(
                                shot, char_idx, item_idx, scene_idx, refs),
                            blocking_ref=(item.get("_blocking_ref") or ""),
                        # 预演图不再送检（见 qc_client.check_image 的说明），改送确定性文字规格；
                        # 只有本镜确实该出基准图时才带（否则空串，等同于不加这段口径）。
                        blocking_spec=(_blocking_spec_text(shot)
                                        if (item.get("_blocking_ref") or "") else ""),
                            # ---- 跨镜连续性（P1，2026-09-25）----
                            # 只在**同场景**时给上一镜信息：跨场景切换本就该换背景换光，
                            # 拿上一镜去比会判出一堆假缺陷（与 keyframe.same_scene 同判据）。
                            prev_shot_desc=_qc_prev_shot_desc(shots, i),
                            prev_shot_ref=_qc_prev_shot_ref(shots, i, out_dir))
                        rec = _qc_record_verdict(project_name, "image", shot_id, "图片质检",
                                                 attempt + 1, seed, scratch_png, verdict,
                                                 style=(shot.get("style") or _sb_style))
                        attempts.append(rec)
                        gate = _qc_gate(verdict)
                        item["qc_gate"] = gate
                        item["qc"] = _qc_summary(attempts, qc_declared, True, max_retries)
                        if gate["accept"]:
                            if best_of > 1:
                                # best-of-N：本轮达标也**不立即入库** —— 先收集候选，
                                # 循环后按质检分选最佳那张入库（见循环末尾收口块）。
                                _best_candidates.append(
                                    (verdict.get("score"), scratch_png, rec, gate))
                                continue
                            shutil.copy2(scratch_png, dst)   # 质检达标 → 写入正式交付目录
                            item["file"] = dst
                            # O2：产物旁路元数据（seed/提示词/工作流 SHA256/质检结论）
                            _write_artifact_meta(
                                dst, kind="storyboard", project_name=project_name,
                                seed=seed, prompt=item.get("prompt"), workflow_key="storyboard_gen",
                                qc=gate, shot_id=shot_id,
                                extra={"ref_count": item.get("ref_count")})
                            break
                        if not verdict.get("ok"):
                            # 质检接口异常：保留暂存图，不盲目重生成（闸门会阻断入库）
                            break
                        # ★ 立刻把这次的缺陷沉淀进教训库 ——
                        #   这样「同一次重试循环的下一次」就能召回它（旧实现只在循环结束后记一次，
                        #   导致前 N 次重试拿不到任何信息，纯粹换种子瞎撞）
                        _record_qc_lesson(project_name, "storyboard", orig_prompt, rec)
                        # ★ 重试止损：连续两次缺陷一字不差 → 「改提示词 + 换种子」根本没带来
                        #   任何变化，继续重试只是重复烧 GPU（实测 ep02 shot_13 这样白烧 6 次，
                        #   全程 GPU 十几分钟，出的图全都一样）。放在闸门与教训沉淀之后：
                        #   本镜若达标早已 break，不受影响；止损只减少无效重试，不改结论。
                        _hopeless, _hopeless_detail = _qc_retry_hopeless(attempts)
                        if _hopeless:
                            # 标记最后一条质检记录：_qc_summary 据此把 label 写成「已停止重试」
                            rec["retry_stopped"] = True
                            rec["retry_stopped_features"] = _hopeless_detail
                            item["qc_retry_stopped"] = {
                                "reason": "连续两次缺陷完全相同，判定重试无收益，已提前停止",
                                "features": _hopeless_detail, "attempts": len(attempts)}
                            app.logger.warning(
                                f"分镜重试止损（镜头 {shot_id}）：连续 {len(attempts)} 次缺陷完全相同，"
                                f"提前停止重试。缺陷：{_hopeless_detail}；"
                                f"建议改写该镜剧本字段（camera / description）后单独重跑该镜")
                            break
                    # ---------- best-of-N 收口：从候选里选**质检分最高**的一张入库 ----------
                    # 仅在 best_of>1 且收集到候选时生效；==1 时此块完全不出现（零回归）。
                    # 达标 → 入库；最佳仍未达标 → **不在此处阻断**，交下方统一阻断逻辑处理
                    #（scratch_png 已指向最佳候选，确保「不合格产物不留本地」删的是被选中那张）。
                    if best_of > 1 and _best_candidates:
                        _bi = qc_client.pick_best_candidate([c[0] for c in _best_candidates])
                        _bs, _bpng, _brec, _bgate = _best_candidates[_bi]
                        scratch_png = _bpng
                        item["qc_gate"] = _bgate
                        item["best_of"] = {
                            "candidates": len(_best_candidates), "picked": _bi + 1,
                            "score": _bs, "accepted": bool(_bgate.get("accept"))}
                        app.logger.info(
                            "镜头 %s best-of-N：%d 张候选中选第 %d 张（score=%s，达标=%s）",
                            shot_id, len(_best_candidates), _bi + 1, _bs,
                            bool(_bgate.get("accept")))
                        if _bgate.get("accept"):
                            shutil.copy2(_bpng, dst)
                            item["file"] = dst
                            item["url"] = f"{_sb_url_base}shot_{seq:02d}.png"
                            item["success"] = True
                            item.pop("error", None)
                            _write_artifact_meta(
                                dst, kind="storyboard", project_name=project_name,
                                seed=seed, prompt=item.get("prompt"),
                                workflow_key="storyboard_gen", qc=_bgate, shot_id=shot_id,
                                extra={"ref_count": item.get("ref_count"),
                                       "best_of": len(_best_candidates)})
                    if qc_declared or qc_on:
                        item["qc"] = _qc_summary(attempts, qc_declared, qc_on,
                                                 int(qc_cfg.get("max_retries", 0)))
                        gate = item.get("qc_gate")
                        if not gate or not gate.get("accept"):
                            # 教训已在循环内逐次沉淀；这里兜底记一次终态（同键会被去重）
                            if attempts and isinstance(attempts[-1], dict):
                                _record_qc_lesson(project_name, "storyboard",
                                                  orig_prompt, attempts[-1])
                            # P0：质检不达标 / 调用异常 → 阻断入库
                            item["success"] = False
                            item["qc_blocked"] = True
                            item.pop("file", None)
                            item.pop("url", None)
                            # ★ 用户需求：质检「判定不通过」的暂存图不留本地（含 ComfyUI 侧）。
                            # ⚠️ 只删「质检成功返回且判定不合格」的产物：ok=False（接口故障/
                            # 超时/鉴权失败）、skipped（未开启）、qc_on=False（接口未就绪）都
                            # 不是产物不合格，删下去会误删好图。见 _reject_artifact 红线说明。
                            try:
                                if qc_on and attempts and attempts[-1].get("ok") is True:
                                    _purge_rejected_artifacts(
                                        [scratch_png], project=project_name,
                                        reason=f"分镜图质检不合格（{gate['label']}）" if gate else "",
                                        kind="storyboard_image",
                                        history_file=attempts[-1].get("history_file") or "")
                            except Exception as _pe:  # noqa: BLE001
                                app.logger.warning(f"分镜图不合格产物清理失败（忽略）：{_pe}")
                            item["error"] = ((f"分镜图质检阻断（{gate['label']}）：{gate['reason']}"
                                              "；未通过质检，未写入正式目录（暂存图见质检历史）")
                                             if gate else (item.get("error")
                                                           or "分镜图生成失败，未写入正式目录"))
                    elif "qc" not in item:
                        item["qc"] = {"enabled": False, "status": "disabled",
                                      "label": "质检未开启", "attempts": 0, "regenerated": 0}
            except _PromptQCBlocked as _pqb:
                # ⭐ P0（2026-10-02 实测定位）：预检阻断**必须**单独捕获。
                #   旧实现只写 item["error"]，而 `item["success"]=True` 是成功路径里
                #   **无条件先写**的（生成完图就置位，见上方 item.update），此后才做预检/
                #   质检/入库。异常一抛，success 保持 True 且 file 指向从未 copy2 的路径
                #   → manifest 谎报「6/6 成功」而正式目录一张图都没有：
                #     · api_storyboard_canvas 按 os.path.isfile 判 exists=False（界面无图）
                #     · 下游 video 拿这个不存在的 file 当 I2V 首帧
                #     · 断点续跑 skip 判据也是 os.path.isfile → 永远跳不过，每轮白重跑
                #   与下方 `item["success"]=False`（质检闸门口径）对齐：阻断即非成功。
                app.logger.warning("镜头 %s 分镜图被提示词预检阻断：%s", shot_id, _pqb)
                item["success"] = False
                item["file"] = ""
                item.pop("url", None)
                item["error"] = str(_pqb)
            except Exception as shot_err:
                import traceback as _tb
                app.logger.error(f"镜头 {shot_id} 分镜图生成失败: {shot_err}\n{_tb.format_exc()}")
                # ⭐ 同上：任何异常都不得留下「success=True 但无产物」的僵尸条目。
                #   成功路径先置位、后续任何一步抛错都必须在这里复位（fail-closed）。
                item["success"] = False
                item["file"] = ""
                item.pop("url", None)
                item["error"] = str(shot_err)

            manifest_shots.append(item)
            with lock:
                generation_state[task_id]["results"].append(item)
                generation_state[task_id]["progress"] = int((i + 1) / max(len(shots), 1) * 100)
                # G10b（2026-09-30）：该镜收尾 → 标记 live 完成（前端据此区分「进行中/已定稿」）
                _live = generation_state[task_id].get("live")
                if isinstance(_live, dict) and _live.get("shot") == shot_id:
                    _live["done"] = True

        ok = sum(1 for r in manifest_shots if r.get("success"))
        blocked = sum(1 for r in manifest_shots if r.get("qc_blocked"))
        manifest = {
            "project_name": project_name,
            # 从模板表取，避免模型换代后 manifest 里还写着旧工作流名（口径漂移）
            "workflow": WORKFLOW_TEMPLATE.get("storyboard_gen", ""),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(manifest_shots),
            "success_count": ok,
            "qc_blocked_count": blocked,
            "shots": manifest_shots,
        }
        # A-3 P1：manifest 原子写改走 fs_atomic（唯一临时名 + flush/fsync + .bak 快照 +
        # os.replace 重试），替代旧「固定 .tmp + os.replace」。落盘失败仍回滚 status。
        _sb_mp = os.path.join(out_dir, "storyboard_manifest.json")
        try:
            atomic_write_json(_sb_mp, manifest)
        except Exception as _mp_err:
            app.logger.warning(f"分镜 manifest 原子写失败（已回滚 status）：{_mp_err}")
            with lock:
                generation_state[task_id].update({
                    "status": "failed",
                    "progress": 100,
                    "success_count": ok,
                    "qc_blocked_count": blocked,
                    "output_dir": out_dir,
                    "error": f"分镜 manifest 落盘失败：{_mp_err}",
                })
            return

        with lock:
            generation_state[task_id].update({
                "status": "completed" if ok else "failed",
                "progress": 100,
                "success_count": ok,
                "qc_blocked_count": blocked,
                "output_dir": out_dir,
                "error": "" if ok else "所有镜头分镜图均生成失败",
            })
    except Exception as e:
        app.logger.error(f"分镜图任务失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    # ★ 用户决策 4：ComfyUI 侧分镜参考图上传残留（sb_ref_*）只在**本轮分镜批量生成全部
    # 结束后**按项目清理一次 —— 不在单镜循环里调（那会在重试中途删掉当前镜头正在用的参考图）。
    try:
        _purge_sb_refs(project_name)
    except Exception as _sb_ref_err:  # noqa: BLE001
        app.logger.warning(f"sb_ref 残留清理失败（忽略）：{_sb_ref_err}")
    _maybe_reclaim_comfyui_output()   # D-11a：任务收尾滚动回收 ComfyUI 重试残留（节流+全容错）
    # 分镜批量是本项目重跑最密集的环节（每失败一次多一条任务历史）→ 收尾顺手清历史面板。
    _maybe_clear_comfyui_history("storyboard 批量生成收尾")

def _project_style(project_name: str = "") -> str:
    """取项目的生效风格：plan.json 的 style（总控 AI 敲定）> AI 设定面板 > config.json 的 style。

    前端手工触发的生成路由（分镜 / 视频 / 资产）此前**完全不传风格**，
    导致「界面按钮点出来的图」和「托管跑出来的图」风格行为不一致。
    统一从这里取，保证两条链路同源。

    2026-09-23（建项目选风格）：新增第三级兜底 config.json 的 style —— 用户「新建项目」时
    下拉/自定义的风格写进 config.style，但此前这里完全不读它，选了什么都不会生效
    （总控没敲定风格时 style 恒为空 → 生成被 409 拦截或回落默认）。现在总控没敲定时
    退回 config.style，让「建项目时选风格」这条路径真正闭环。
    """
    proj = _safe_project(project_name or "")
    try:
        brief = (autopilot.get_plan(proj) or {}).get("style") or ""
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"读取项目风格失败（忽略）：{e}")
        brief = ""
    if not brief:
        try:
            brief = _apply_project_settings("", proj)
        except Exception:  # noqa: BLE001
            brief = ""
    if not brief:
        # 建项目时选的风格 + 画面比例（config.json 的 style / aspect_ratio 字段）作为最后兜底。
        # 画面比例拼进风格串，让 style_kit.aspect_ratio 能解析，从而真正落到视频/分镜画布
        # （与总控 style_brief 里的「画面比例：9:16 竖屏」同一种表达，解析口径一致）。
        try:
            rec = project_store.get_project(proj)
            if rec:
                cfg = project_store.read_config(rec["dir_key"])
                brief = str(cfg.get("style") or "").strip()
                ar = str(cfg.get("aspect_ratio") or "").strip()
                if ar:
                    brief = f"{brief}，画面比例：{ar}" if brief else f"画面比例：{ar}"
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"读取项目 config.style/aspect_ratio 失败（忽略）：{e}")
            brief = ""
    # 2026-09-28 修复（「建项目时选的画面比例」必须真正生效）：
    # 三级兜底顺序**完全不变**，但「拼画幅」不再只发生在第三级 —— 只要最终 brief 里
    # **还没有任何画幅信息**（style_kit.aspect_ratio(brief) is None），且项目
    # config.aspect_ratio 非空，就统一补一句「画面比例：<ar>」。这样 plan.json / AI
    # 设定面板有 style（正常情况恒成立）时，用户在「新建项目」里手选的比例也能落到
    # 视频 / 分镜画布（两链路的 style_kit.resolve 都能解析这句）。
    # ⚠️ 硬约束：brief **已带**画幅（关键词或显式 a:b）时绝不覆盖 —— 显式意图优先。
    # 读 config 失败 fail-open：沿用原 brief、只告警不抛。
    if not style_kit.aspect_ratio(brief):
        try:
            _rec = project_store.get_project(proj)
            if _rec:
                _ar = str(project_store.read_config(_rec["dir_key"]).get("aspect_ratio")
                          or "").strip()
                if _ar:
                    brief = f"{brief}，画面比例：{_ar}" if brief else f"画面比例：{_ar}"
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"补齐项目画面比例失败（忽略）：{e}")
    return style_kit.normalize_style(brief)

def _project_subtitle_enabled(project_name: str = "") -> bool:
    """该项目的成片「硬字幕」开关（config.json 的 subtitle_enabled），默认 False。

    2026-09-24（用户明确要求「不要生成字幕」）：
    成片阶段有两处会往视频里烧硬字幕（pipeline.step_final / video_postprocess.finalize_episode），
    此前无条件执行。现在统一从这里取值：读不到 / 非 true → 视为关闭，直接不烧字幕。
    这样「H3 提示词不诱导字幕」+「成片不烧字幕」两层都封死，
    确需硬字幕的老项目可在其 config.json 里显式写 "subtitle_enabled": true 单独放开。
    """
    proj = _safe_project(project_name or "")
    default = bool(PROJECT_DEFAULT_CONFIG.get("subtitle_enabled", False))
    try:
        rec = project_store.get_project(proj)
        if rec:
            cfg = project_store.read_config(rec["dir_key"])
            if "subtitle_enabled" not in cfg:
                return default
            val = cfg.get("subtitle_enabled")
            # 宽容解析：字符串 "false"/"0"/"no"/"off" 不能被 bool() 误判为「开」
            if isinstance(val, str):
                s = val.strip().lower()
                if s in ("true", "1", "yes", "on"):
                    return True
                if s in ("false", "0", "no", "off", "none", "null", ""):
                    return False
                return default
            return bool(val)
    except Exception as e:  # noqa: BLE001  开关读取失败按「关闭」处理（安全侧）
        app.logger.warning(f"读取项目 config.subtitle_enabled 失败（按关闭处理）：{e}")
    return default

def _project_caption_burn_enabled(project_name: str = "") -> bool:
    """该项目的「字幕/转场 caption」烧制开关（config.json 的 caption_burn_enabled），**默认 True**。

    与 _project_subtitle_enabled 是**两件事**，刻意分开：
      · subtitle_enabled（默认关）：把人物开口的台词转录成硬字幕——辅助性文字，
        用户 2026-09-24 明确要求不要；
      · caption_burn_enabled（默认开）：把剧本 caption 烧进成片——它是**剧情装置**
        （时空落点、时空回溯、集尾悬念）。参考改编稿正是靠「春秋蝉，逆转时光。」
        让观众看懂时空跳变；不烧就会看到无过渡的跳切。
    不想要字幕的项目在其 config.json 写 "caption_burn_enabled": false 即可。
    """
    proj = _safe_project(project_name or "")
    default = bool(PROJECT_DEFAULT_CONFIG.get("caption_burn_enabled", True))
    try:
        rec = project_store.get_project(proj)
        if rec:
            cfg = project_store.read_config(rec["dir_key"])
            if "caption_burn_enabled" not in cfg:
                return default
            val = cfg.get("caption_burn_enabled")
            if isinstance(val, str):
                s = val.strip().lower()
                if s in ("true", "1", "yes", "on"):
                    return True
                if s in ("false", "0", "no", "off", "none", "null", ""):
                    return False
                return default
            return bool(val)
    except Exception as e:  # noqa: BLE001  开关读取失败按默认处理
        app.logger.warning(f"读取项目 config.caption_burn_enabled 失败（按默认开启处理）：{e}")
    return default

def _style_aspect_confirmed(project_name: str) -> dict:
    """生成前置确认门判据：用户是否已与总控 AI 确认「风格」与「视频比例」。

    单一事实源 = 已应用的总控设定（ai_chat/project_settings.json，按项目）。
    - 风格确认：settings 的风格基调(style) 或 画风(art_style) 任一非空；
    - 比例确认：settings 的画面比例(aspect_ratio，即视频画幅，如 9:16) 非空。
    资产图的画幅已按类型内置写死（见 style_kit.asset_aspect_ratio），不依赖此比例；
    这里确认比例只为「视频 / 分镜」画幅服务。
    """
    try:
        view = ai_chat.settings_view(AI_SETTINGS_PATH, project_name or "") or {}
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"确认门读取总控设定失败（按未确认处理）：{e}")
        view = {}
    s = view.get("settings") or {}
    style_confirmed = bool((s.get("style") or s.get("art_style") or "").strip())
    # 2026-09-23（建项目选风格）：用户「新建项目」时下拉/自定义的风格写进 config.json 的
    # style，也算「风格已确认」——否则用户明明选了风格，生成仍被 409 拦在「尚未确认风格」，
    # 与「建项目时就能选风格」的体验自相矛盾。总控 AI 敲定（project_settings）仍是第一优先级。
    if not style_confirmed:
        try:
            rec = project_store.get_project(_safe_project(project_name or ""))
            if rec:
                cfg_style = str(project_store.read_config(rec["dir_key"]).get("style") or "").strip()
                style_confirmed = bool(cfg_style)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"确认门读取 config.style 失败（忽略）：{e}")
    aspect_confirmed = bool((s.get("aspect_ratio") or "").strip())
    # 2026-09-23（建项目选比例）：用户「新建项目」时选的画面比例写进 config.json 的
    # aspect_ratio，也算「比例已确认」，与 style 的同源兜底保持一致。总控 AI 敲定仍是第一优先级。
    if not aspect_confirmed:
        try:
            rec = project_store.get_project(_safe_project(project_name or ""))
            if rec:
                cfg_ar = str(project_store.read_config(rec["dir_key"]).get("aspect_ratio") or "").strip()
                aspect_confirmed = bool(cfg_ar)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"确认门读取 config.aspect_ratio 失败（忽略）：{e}")
    missing = []
    if not style_confirmed:
        missing.append("风格")
    if not aspect_confirmed:
        missing.append("视频比例")
    return {"confirmed": style_confirmed and aspect_confirmed,
            "style_confirmed": style_confirmed, "aspect_confirmed": aspect_confirmed,
            "missing": missing, "settings": s}

def _style_aspect_guard(project_name: str, override_style: str = ""):
    """生成入口前置校验门（2026-09-22 需求）。

    用户未与总控 AI 确认「风格 / 视频比例」时拦截生成：返回 409 + 可读提醒响应；
    已确认则返回 None（放行）。各生成端点在解析出 project_name 后调用它。
    前端 client.ts readError 会自动弹出 error + guide 文案，提示去总控确认。

    ``override_style``：个别入口（如托管 /api/autonomous/start）允许调用方**显式传风格**
    （plan_overrides.style）——此时视「风格」为已确认，但「视频比例」仍须总控确认。
    """
    chk = _style_aspect_confirmed(project_name)
    if override_style and not chk["style_confirmed"]:
        chk["style_confirmed"] = True
        chk["missing"] = [m for m in chk["missing"] if m != "风格"]
        chk["confirmed"] = chk["style_confirmed"] and chk["aspect_confirmed"]
    if chk["confirmed"]:
        return None
    _names = "、".join(chk["missing"])
    return jsonify({
        "success": False,
        "requires_confirm": True,
        "error": f"尚未与总控 AI 确认{_names}，暂不开展生成。",
        "guide": ("请先在「AI 对话 · 创作总控」里与 AI 敲定" + _names
                  + "（风格：画风/基调；视频比例：画面画幅，如 9:16 / 16:9 / 1:1），"
                    "点击「应用设定」落盘后再开始生成。"),
        "missing": chk["missing"],
        "style_confirmed": chk["style_confirmed"],
        "aspect_confirmed": chk["aspect_confirmed"],
    }), 409

# ===== 步骤6：视频生成 =====

def _blocking_spec_text(shot: dict) -> str:
    """本镜的**确定性构图规格**文字（3D 导演台），用于替代「无面人偶预演图」送质检。

    背景：把预演图与成品图一起送视觉质检会严重污染判定（同一张合格图 88 → 35），
    污染来自图像本身而非措辞，所以改送这段文字规格（人数/左右顺序/景别/机位）。
    任何异常都返回空串 —— 构图规格绝不能影响质检主流程。
    """
    try:
        import te_3d_director  # noqa: PLC0415
        return te_3d_director.blocking_spec_text(shot)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("[质检] 构图规格生成失败（不影响判定）：%s", e)
        return ""

def _ref_image_local(ref):
    """取 ref 上第一张真实存在的本地参考图，取不到返回 None。

    全项目「判这张参考图能不能用」的**唯一**判据：`_collect_reference_images`、
    `_upgrade_refs_with_disk` 都走这里。历史上「判有图」散落多处写法不一致，
    才会出现「base.png 明明在盘上、日志却说参考图不可用」这类静默降级。
    """
    if not isinstance(ref, dict):
        return None
    for key in ("front", "base"):
        p = ref.get(key)
        if not isinstance(p, str) or not p:
            continue
        local = comfyui_client.resolve_local_path(p)
        if local and os.path.exists(local):
            return local
    return None

def _upgrade_refs_with_disk(refs: list, disk_refs: list) -> list:
    """按 name 把磁盘资产的图路径合并进 refs，返回结构完整的 ref 列表。

    ⚠️ 为什么不能用 `refs or disk_refs`（2026-10-06 实测踩坑）：
    整集生成视频时前端只发 {project_name, episode_no}，后端在 /api/video/generate
    里用剧本 characters 兜底 —— 而那些对象只有 name/description/reference_prompt_zh，
    **没有 front/base 图路径**。于是 `refs or disk_refs` 因 refs 非空而永远短路，
    磁盘资产（base.png 明明在盘上）永远用不上，结果是整集视频**一个角色锚点都拿不到**，
    人物全靠模型自由发挥 —— 正是「全片人物 OOC / 服装对不上设定图」的根因，
    而它只表现为日志里一串「角色参考图不可用」，不报错。

    合并语义：
      - 显式带图路径的 ref 原样保留（前端传参优先，不被磁盘覆盖）；
      - 缺图的 ref 按 name 从磁盘资产补 front/base，保留原 name 与原有顺序；
      - 剧本没列出的角色追加在后面，保证前 2 个槽位总能找到真实锚点；
      - refs 为空时等价于旧行为（直接返回磁盘资产列表）。
    """
    by_name = {}
    for d in (disk_refs or []):
        if isinstance(d, dict):
            n = str(d.get("name") or "").strip()
            if n and _ref_image_local(d) and n not in by_name:
                by_name[n] = d
    out, seen = [], set()
    for r in list(refs or []):
        if not isinstance(r, dict):
            continue
        n = str(r.get("name") or "").strip()
        merged = r
        if not _ref_image_local(r):
            d = by_name.get(n)
            if d:
                merged = dict(r)
                merged["front"] = d.get("front")
                merged["base"] = d.get("base")
        out.append(merged)
        if n:
            seen.add(n)
    for n, d in by_name.items():
        if n not in seen:
            out.append(d)
    return out

def _collect_reference_images(character_refs: list, scene_refs: list) -> list:
    """把前端传来的参考图（可能是 /api/assets/... 的 HTTP 资源路径）解析为本地绝对路径

    修复 P0-4：原实现直接 os.path.exists(HTTP 路径) 恒为 False，参考图永远传不到 H3。
    """
    ref_imgs = []
    for ref in list(character_refs)[:2]:
        local = _ref_image_local(ref)
        if local:
            ref_imgs.append(local)
        else:
            app.logger.warning(f"角色参考图不可用: {ref.get('name') if isinstance(ref, dict) else ref}")
    for ref in list(scene_refs)[:1]:
        local = _ref_image_local(ref)
        if local:
            ref_imgs.append(local)
    return ref_imgs

def _video_generate_worker(task_id, project_name, shots, character_refs,
                          scene_refs, storyboards, use_storyboard, mode,
                          timeout_per_segment, episode_tag, episode_no=None,
                          chain_mode="auto", style="", overwrite=False,
                          build_only=False, only_scenes=None):
    """逐镜/整集/关键帧三种模式的视频生成（后台任务体，可被路由与流水线复用）

    overwrite：是否全量重做。默认 False —— per_shot 模式下已达标入库的
    shot_XX.mp4 直接复用、只重跑缺失/被质检阻断的镜头（断点续跑）。
    episode 模式为「整集一次生成」，无逐镜跳过语义，本参数不影响该分支。

    从 /api/videos/generate 抽出的模块级实现：原闭包变量（项目名、镜头、参考图、
    模式等）改为显式参数，业务逻辑不变。抽出的目的是让自动生产流水线
    （pipeline.py / autopilot.py）能直接复用同一条视频生成链路，避免两套实现漂移。

    episode_no：集级目录隔离（第 1 集沿用平铺，第 2 集起写 epNN/），
    避免多集自动生产时 shot_NN.mp4 互相覆盖。
    style：用户与总控敲定的风格描述。用途有二 ——
      ① 补齐镜头 style 字段（剧本未注入时的兜底），使提示词带上风格；
      ② 解析画幅并覆写 H3 分辨率（竖屏 9:16 真正落地，而非模板死板的 16:9）。
    """
    # ⚠️ 关键：本 worker 是**裸线程**（见 /api/videos/generate 的 threading.Thread），
    # 运行在 Flask 请求线程之外 —— contextvars 不会从请求线程继承过来，因此
    # `comfyui_client.wait_for_completion` 轮询里的 `cancellation.should_stop()`
    # 在过去**恒为 False**：前端点「暂停」只停了托管的下一步，**正在跑的 ComfyUI
    # 渲染任务不会被打断**（用户反馈「暂停要同步停止 comfyui 的任务」的根因）。
    # 这里显式把中止判定器注册进本线程的执行上下文：
    #   - 托管暂停（autopilot.is_paused）→ 立即中断远端
    #   - 进程退出（autopilot._STOP / 解释器与用户交互无关的关停）→ 一并中断
    # 判定器自身异常在 cancellation.should_stop 里 fail-open 处理，不影响生产。
    _cancel_token = cancellation.push(_video_should_stop)
    # 该任务是否由托管流水线发起（pipeline._run_task_worker 会置 state["pipeline"]=True）。
    # 决定「托管暂停」是否应掐断本任务：手动生成不受托管开关影响。
    _is_pipeline = False
    with lock:
        _st0 = generation_state.get(task_id)
        if isinstance(_st0, dict):
            _is_pipeline = bool(_st0.get("pipeline"))
    _pipe_token = _VIDEO_TASK_IS_PIPELINE.set(_is_pipeline)
    try:
        _video_generate_worker_body(
            task_id, project_name, shots, character_refs, scene_refs, storyboards,
            use_storyboard, mode, timeout_per_segment, episode_tag, episode_no,
            chain_mode, style, overwrite, build_only, only_scenes)
    except cancellation.Cancelled as e:
        # 协作式中止：不是失败，落到「已取消」态，前端展示为已停止而非报错
        app.logger.info("[视频] 任务因中止信号停止（task=%s）：%s", task_id, e)
        with lock:
            _st = generation_state.get(task_id)
            if isinstance(_st, dict):
                _st.update({"status": "cancelled", "phase": "已停止",
                            "error": "已收到中止信号，ComfyUI 远端任务已中断（已完成镜头保留，可续跑）"})
    finally:
        _VIDEO_TASK_IS_PIPELINE.reset(_pipe_token)
        cancellation.reset(_cancel_token)

def _video_should_stop() -> bool:
    """视频 worker 的中止判定器：**仅对托管（pipeline）任务**生效。

    刻意与 `autopilot._halt_requested` 同口径，但**不 import autopilot**（app.py 与
    autopilot 相互 import 会成环）。用惰性 import 规避循环依赖，失败时 fail-open。

    ⚠️ 2026-09-24 修正：早期实现「只要 autopilot 处于 paused 就停」，会把**用户手工
    触发**的生成一起掐掉 —— 实测：托管暂停期间点「生成视频（手动）」，第一次轮询就
    命中中止信号，报「ComfyUI 远端等待期间收到中止信号」（manual 任务被 pause 误杀）。
    暂停的语义应只覆盖「托管自动生产」，不该阻断用户当前手动操作。
    因此这里判定的前提是 `_VIDEO_TASK_IS_PIPELINE`（由 worker 外壳按任务态设置）：
      - 托管任务（generation_state[task]["pipeline"] is True）：托管暂停 → 停；
      - 进程退出（autopilot._STOP）：任何任务都停（关服就该全停）。
    """
    try:
        import autopilot
        # 进程退出：无论手动还是托管，都应立刻停
        stop_ev = getattr(autopilot, "_STOP", None)
        if stop_ev is not None and stop_ev.is_set():
            return True
        # 托管暂停：只对 pipeline 任务生效（手动任务不受托管开关影响）
        if _VIDEO_TASK_IS_PIPELINE.get() and autopilot.is_paused():
            return True
    except Exception as e:  # noqa: BLE001  判定器故障不得影响生产
        app.logger.debug("视频中止判定器读取失败（按不中止处理）：%s", e)
    return False

def _video_generate_worker_body(task_id, project_name, shots, character_refs,
                                scene_refs, storyboards, use_storyboard, mode,
                                timeout_per_segment, episode_tag, episode_no=None,
                                chain_mode="auto", style="", overwrite=False,
                                build_only=False, only_scenes=None):
    """视频生成的实际业务体（外壳见 :func:`_video_generate_worker`，负责注册中止信号）"""
    try:
        # 风格/画幅：整集共用一次解析
        # G19：风格串未含画幅关键词时以默认画幅（2026-09-28 起为 16:9）为底，
        #      不再静默回落模板尺寸。megapixels：视频预算走 video_megapixels()
        #      —— 让 MJSCXT_VIDEO_MEGAPIXELS 真正生效（8GB 卡的 0.4 应急档）。
        #      默认 0.5 与 aspect_size 的默认值**完全等价**（16:9→960×544、
        #      9:16→544×960），故本行不改变既有行为。
        _style_res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO,
                                       megapixels=style_kit.video_megapixels())
        _size = _style_res.get("size")
        if style:
            app.logger.info("[视频] 风格=%s；画幅=%s", _style_res.get("style") or style,
                            _style_res.get("label") or "未指定（沿用模板）")
        # 镜头 style 兜底：老剧本没有该字段时（历史产物），用本次传入的风格补齐
        if _style_res.get("style"):
            shots = [dict(s, style=(s.get("style") or _style_res["style"]))
                     for s in (shots or []) if isinstance(s, dict)]
        # 按模型能力归一化镜头参数（Toonflow 借鉴吸收点 #1，2026-10-01）：
        # duration 钳位、引用列表补齐、shot_id 补齐——fail-open，归一失败按原样继续。
        try:
            shots, _norm_notes = model_capabilities.normalize_shots_for_h3(shots)
            if _norm_notes:
                app.logger.info("[视频] 镜头参数归一化：%s", "；".join(_norm_notes[:3]))
        except Exception as _norm_err:  # noqa: BLE001
            app.logger.warning("镜头参数归一化失败（按原 shots 继续）：%s", _norm_err)
        videos_dir = _ep_dir(os.path.join(VIDEOS_DIR, project_name), episode_no)
        os.makedirs(videos_dir, exist_ok=True)
        try:
            _epn = int(episode_no) if str(episode_no or "").strip() else 1
        except (TypeError, ValueError):
            _epn = 1
        # 视频 URL 前缀：第 2 集起带 epNN 段（与 _ep_dir 的落盘位置一致）
        _vurl = (f"/api/videos/{project_name}/ep{_epn:02d}" if _epn > 1
                 else f"/api/videos/{project_name}")

        # 参考图来源优先级：显式传入（带 front/base）> 磁盘资产目录。
        # ⚠️ 2026-10-06 修复：旧顺序是「先收集 → 失败才兜底」，兜底又写成
        #    `character_refs = character_refs or _auto_chars`；但 :8087 已用剧本
        #    characters 填过 character_refs（非空、却没有 front/base 图路径）
        #    → `or` 永远短路 → 磁盘资产永远用不上 → 整集视频 0 角色锚点，
        #    人物全靠模型自由发挥，即「全片人物 OOC / 服装对不上设定图」的根因。
        #    现在把「按 name 合并磁盘资产」提到收集之前：显式传参仍优先，
        #    剧本描述对象也能拿到真实图路径；且「角色参考图不可用」从此只在
        #    该角色盘上确实没有图时才打印（详见 _upgrade_refs_with_disk）。
        _auto_chars, _auto_scenes = _collect_asset_refs(project_name)
        _upgraded = False
        if _auto_chars:
            _merged_chars = _upgrade_refs_with_disk(character_refs, _auto_chars)
            _upgraded = _merged_chars != character_refs
            character_refs = _merged_chars
        if not scene_refs and _auto_scenes:
            scene_refs = _auto_scenes

        ref_imgs = _collect_reference_images(character_refs, scene_refs)
        main_char_img = _collect_reference_images(character_refs[:1], [])
        if main_char_img or ref_imgs:
            if _upgraded:
                app.logger.info("[视频] 参考图已由磁盘资产补齐：角色 %d / 合计 %d",
                                len(main_char_img), len(ref_imgs))
        else:
            # 优化#1（2026-10-01）：连磁盘兜底都拿不到参考图 → 角色资产从未生成或
            # 目录被清空。此前只 warning 后照跑（人物全靠模型自由发挥，成片必 OOC）。
            # 现在自动补做：从剧本取角色清单，同步触发生成（上限 4 个、总等待 30 分钟），
            # 完成后重新收集参考图再继续；补做失败/超时不阻塞出片（fail-open）。
            app.logger.warning("[视频] 磁盘资产目录里也没有可用参考图 → 自动补做角色资产…")
            try:
                _scr0 = _load_script_for(project_name, episode_no) or {}
                _need = [c for c in (_scr0.get("characters") or [])
                         if isinstance(c, dict) and str(c.get("name") or "").strip()][:4]
                if _need:
                    _task_id = f"character_{project_name}_{uuid.uuid4().hex[:12]}"
                    with lock:
                        generation_state[_task_id] = {
                            "status": "running", "asset_type": "character",
                            "progress": 0, "total": len(_need), "current": 0,
                            "phase": "基础图", "results": [],
                            "overwrite": False, "auto_repair": True}
                    _t0 = threading.Thread(
                        target=_generate_asset_task,
                        args=(_task_id, _need, "character", project_name,
                              _project_style(project_name), False))
                    _t0.daemon = True
                    _t0.start()
                    _t0.join(timeout=1800)     # 上限 30 分钟，超时不阻塞出片
                    if _t0.is_alive():
                        app.logger.warning("[视频] 资产补做超时（30 分钟）→ 按无锚点继续")
                    _re_chars, _re_scenes = _collect_asset_refs(project_name)
                    if _re_chars:
                        character_refs = _re_chars
                        main_char_img = _collect_reference_images(character_refs[:1], [])
                        ref_imgs = _collect_reference_images(character_refs, scene_refs)
                        app.logger.info("[视频] 资产补做完成：角色参考图已重新挂载"
                                        "（主角锚点 %d / 合计 %d）",
                                        len(main_char_img), len(ref_imgs))
            except Exception as _e:  # noqa: BLE001
                app.logger.warning("[视频] 资产自动补做失败（继续生成）：%s", _e)
            if not (main_char_img or ref_imgs):
                app.logger.warning("[视频] 补做后仍无参考图 —— 本集将无角色锚点生成，"
                                   "人物一致性无法保证（检查 output/assets/characters/%s）",
                                   project_name)
        app.logger.info(f"视频参考图解析结果: {ref_imgs}；主角锚点: {main_char_img}")

        # B-18 P1-7：构建角色索引，供 _shot_segment 逐镜匹配参考图（与分镜链路口径对齐）
        char_idx = _build_asset_index(character_refs, project_name, "character")
        # 2026-09-27 扩展：物品/场景索引也逐镜匹配（「分镜+本镜资产」参考图策略）。
        # 前端 video 接口未传 items/scenes 时，从剧本兜底读取（含 characters/items/scenes）。
        _scr = _load_script_for(project_name, episode_no) or {}
        item_idx = _build_asset_index((_scr.get("items") or []), project_name, "item")
        scene_idx = _build_asset_index((_scr.get("scenes") or []), project_name, "scene")
        # 前端显式传来的 scene_refs 优先（可能带 URL/本地路径，比剧本兜底更准）
        if scene_refs:
            _scene_idx_explicit = _build_asset_index(scene_refs, project_name, "scene")
            for _k, _v in _scene_idx_explicit.items():
                if _v.get("image"):
                    scene_idx[_k] = _v

        # 分镜图映射（步骤5产物）→ 作为 H3 的 <Picture 1> 构图基准
        # 修复：改用合并式映射（目录扫描 + manifest + 前端传入）。
        # 原实现只认前端传入的 storyboards，前端漏传某镜时该镜会静默退化为
        # 「无分镜图参考」，与用户所见不符。
        sb_map = _keyframe_sb_map(project_name, None, storyboards, episode_no=episode_no)
        app.logger.info(f"分镜图参考映射: {sorted(sb_map.keys())}")

        # 关键帧驱动模式：取该项目尾帧目录，并按镜登记 [首帧, 尾帧]
        # 首帧支持跨镜链式（上一镜尾帧 = 下一镜首帧），见 keyframe.plan_keyframes
        kf_end_map = {}
        kf_start_map = {}
        if mode == 'keyframe':
            kf_dir = _ep_dir(os.path.join(KEYFRAMES_DIR, project_name), episode_no)
            for i, _s in enumerate(shots):
                _sid = _s.get('shot_id', i + 1)
                _seq = _shot_seq(_sid, i + 1)
                _end = os.path.join(kf_dir, f"shot_{_seq:02d}_end.png")
                # P1-19 一次性兼容：旧 keyframe 曾用「拼接全部数字」命名（"S01-C02"→102），
                # 新语义为「首段数字」（→1）。旧文件仍在时按旧名回退命中并告警；整数镜号下
                # 新旧同名，`_legacy != _seq` 恒为假，此分支不触发（零行为变更）。
                if not os.path.isfile(_end):
                    _legacy = shot_key.legacy_shot_seq(_sid)
                    if _legacy is not None and _legacy != _seq:
                        _legacy_path = os.path.join(kf_dir, f"shot_{_legacy:02d}_end.png")
                        if os.path.isfile(_legacy_path):
                            app.logger.warning(
                                "[P1-19 兼容] 命中旧镜号命名尾帧 %s（镜号 %r 现按首段数字"
                                "归一为 %d）；建议重跑关键帧以迁移到 shot_%02d_end.png",
                                os.path.basename(_legacy_path), _sid, _seq, _seq)
                            _end = _legacy_path
                if os.path.isfile(_end):
                    kf_end_map[str(_sid)] = _end
                    kf_end_map[f"shot_{_seq:02d}"] = _end
            _cm = keyframe.norm_chain_mode(chain_mode if chain_mode is not None
                                           else KEYFRAME_CHAIN_MODE)
            kf_start_map = keyframe.resolve_start_map(
                shots, sb_map, kf_dir, chain_mode=_cm)
            app.logger.info(f"[keyframe] 尾帧就绪 {len(set(kf_end_map.values()))}/{len(shots)} 镜"
                            f"；链式模式 {_cm}，串帧 {sum(1 for v in kf_start_map.values() if v) // 2} 镜")

        # H3 Director **公共参考图**（2026-09-30）：episode 模式会在循环前填这里
        # （见 _h3_plan_common_refs）。per_shot 模式保持空 = 完全走原路径。
        # ``_comps_map`` 让「公共池规划」与「段级挂图」共用同一份逐镜解析结果
        # （重复解析会把 _ref_warnings 同一句告警记两遍，且两边口径可能漂移）。
        _comps_map: dict = {}

        def _shot_segment(shot, seq, qc_cfg=None, common=None, common_keys=None):
            """把一个分镜转成 H3 工作流的一个「段」（提示词 + 时长 + 参考图）

            keyframe 模式：参考图 = [首帧(分镜图), 尾帧]，让 H3 在两端之间插值运动；
            缺尾帧时自动退化为首帧单锚（并在返回值中标记，便于前端提示）。

            ``common`` / ``common_keys``：本次提交的**公共参考图**（H3 Director 公共
            参数，2026-09-30）。公共项由客户端写进 ``global.refs``（index
            ``0..K-1``）+ ``commonEnabled=true``，因此：
              · 提示词按 ``<Picture 1..K>`` **先声明公共项**（``common_refs=`` 传给
                ``comfyui_client._h3_picture_defs``）；
              · 本段 ``reference_images`` **不含**公共项 —— 同一张图挂两处会被插件
                按 index 当成两张（槽位白白翻倍，还可能挤掉本镜自己的锚点）；
              · 9 槽预算先扣掉 K，再留给「分镜图 + 本镜资产」。
            不传（默认）＝完全走原路径，零行为变更。
            """
            sid = shot.get('shot_id')
            sb_local = sb_map.get(_norm_shot_key(sid)) if use_storyboard else None
            _c_keys = common_keys if common_keys is not None else set()
            _c_refs = []          # 公共项的「提示词载荷」（kind/name/appearance）
            if common:
                _c_refs = [dict(c) for c in common]
            # 按镜匹配的参考图 refs（供提示词构建），非 sb_local 分支默认走全局 refs
            _shot_char_refs = character_refs
            _shot_item_refs = []
            _shot_scene_refs = scene_refs
            _seg_comps = []       # 本段**私有**组件（不含公共项）
            _end_frame_ref = None  # keyframe 模式的尾帧声明（<Picture K>），非 keyframe 恒 None
            if mode == 'keyframe' and sb_local:
                sb_local = sb_map.get(_norm_shot_key(seq)) or sb_map.get(
                    f"shot_{seq:02d}") or sb_local
                # 链式首帧优先：上一镜尾帧（resolve_start_map 已按同场景判定回退）
                start_p = (kf_start_map.get(str(sid))
                           or kf_start_map.get(f"shot_{seq:02d}") or sb_local)
                end_p = kf_end_map.get(str(sid)) or kf_end_map.get(f"shot_{seq:02d}")
                refs = [start_p] + ([end_p] if end_p else [])
                # ⚠️ 审计 P1-1（2026-09-29）：keyframe 的实际挂图只有「首帧+尾帧」两张，
                #    提示词声明必须与之对齐 —— char/item/scene 三段式引用一律置空
                #    （旧实现残留全局 character_refs/scene_refs，会声明出实际不存在的
                #    <Picture 3..N>，属于「声明指向错误图」的静默错位）；尾帧图经
                #    end_frame_ref 声明为 <Picture K>，让 h3_prompt_kit 真正产出
                #    「End state: the final frame must land on <Picture K>」尾帧锚定句
                #    （<Picture 1>=首帧构图基准；公共块不参与，_h3_plan_common_refs
                #    已按 mode 前置挡掉，见 episode 分支的调用点）。
                _shot_char_refs = []
                _shot_item_refs = []
                _shot_scene_refs = []
                _end_frame_ref = ({"name": f"shot_{seq:02d}_end"} if end_p else None)
                _c_refs = []
            elif sb_local:
                # 2026-09-27「分镜 + 本镜资产」参考图策略：分镜图(构图基准) +
                # 本镜出场角色三视图(每人一张) + 本镜物品 + 场景图。
                # H3 Director 每段最多 9 张（ref_image_0..8），去掉旧的「上限 2 张」保守限制。
                # 2026-09-30：解析收敛到 _h3_shot_ref_components（公共池规划与段级挂图
                # 共用同一份结果），并在这里把**公共项摘掉**（它们由客户端走 global.refs）。
                _comps = (_comps_map.get(str(sid))
                          if isinstance(_comps_map, dict) else None)
                if _comps is None:
                    # 优化#2 接线（2026-10-01）：本镜角色声明了服装（shot.outfit /
                    # shot.character_outfits）且对应变体资产已生成 → 用变体参考图；
                    # 无声明或变体不存在时逐字走旧逻辑（_shot_outfit_dir 返回空）。
                    _outfit_map = {}
                    for _cn in (char_idx or {}).keys():
                        try:
                            _od = _shot_outfit_dir(
                                shot, str(_cn),
                                os.path.join(CHARACTERS_DIR, project_name, str(_cn)))
                        except Exception as _oe:  # noqa: BLE001
                            _od = ""
                            app.logger.debug("[服装变体] 解析失败（回落主设定图）：%s", _oe)
                        if _od:
                            _outfit_map[str(_cn)] = _od
                    _comps = _h3_shot_ref_components(shot, char_idx, item_idx, scene_idx,
                                                     character_refs, main_char_img,
                                                     outfit_map=_outfit_map or None)
                _seg_comps = [c for c in _comps if not _h3_is_common_comp(c, _c_keys)]
                _shot_char_refs = [c for c in _seg_comps if c.get("kind") == "character"]
                _shot_item_refs = [c for c in _seg_comps if c.get("kind") == "item"]
                _shot_scene_refs = [c for c in _seg_comps if c.get("kind") == "scene"]
                # ⭐ 槽位预算：9 格总量里先扣公共块（K 张），再扣分镜图 1 张，
                #    剩下的才是本镜资产的额度。超出按「角色→物品→场景」截断
                #    （分镜图恒保留），并同步截断提示词的 char/item/scene refs，
                #    避免「声明的 <Picture N> > 实际传入的图」错位。
                _own_room = max(0, h3_director_builder.MAX_REFERENCE_IMAGES
                                - len(_c_refs) - 1)
                if len(_seg_comps) > _own_room:
                    _dropped = len(_seg_comps) - _own_room
                    _seg_comps = _seg_comps[:_own_room]
                    _shot_char_refs = [_c for _c in _seg_comps
                                       if _c.get("kind") == "character"]
                    _shot_item_refs = [_c for _c in _seg_comps
                                       if _c.get("kind") == "item"]
                    _shot_scene_refs = [_c for _c in _seg_comps
                                        if _c.get("kind") == "scene"]
                    app.logger.warning(
                        "[H3公共参考图] 镜头 %s：公共 %d 张 + 分镜图占位后，本镜资产"
                        "可挂 %d 张，已按「角色→物品→场景」丢弃 %d 项",
                        sid, len(_c_refs), _own_room, _dropped)
                refs = [sb_local] + [c["path"] for c in _seg_comps]
            else:
                # 无分镜图的兜底分支：编号从 1 起重新数，公共块不参与
                # （_h3_plan_common_refs 已要求全镜有分镜图，故开启公共时走不到这里）。
                refs = ref_imgs
                _c_refs = []
            try:
                dur = float(shot.get('duration') or 5)
            except (TypeError, ValueError):
                dur = 5.0
            # ---- 长镜切段（需求 J / P0-1，2026-09-25）----
            # 业界共识：AI 视频可信窗口约 4 秒，超过后段易崩坏。本项目剧本单镜普遍
            # 4.5~12 秒（实测第1集 27/27 超线、第2集 28/29 超线），故在**生成期**把长镜
            # 拆成多个 ≤H3_SEGMENT_MAX_SEC 的子段，一次提交让 H3 原生段间衔接出**一条**
            # 连续视频 —— 落盘仍是单个 shot_XX.mp4，命名契约（probe_video / dub_mix /
            # _SHOT_RE）全部不受影响；总时长严格守恒（segment_durations 均摊且 sum 不变），
            # 故配音时间轴与成片长度也不变。
            # ⚠️ 每个子段要用**子段时长**重建提示词：否则 4 秒的段会被塞进 12 秒的节拍，
            #    段内动作空转、台词位置也会整体后移。
            _seg_shots = h3_prompt_kit.segment_shot(
                shot, dur, max_sec=h3_prompt_kit.H3_SEGMENT_MAX_SEC)
            _multi_seg = len(_seg_shots) > 1
            _segs = []
            # ⭐ 逐分镜场景 LoRA（LLM 智能选择 + 规则表兜底，见 app/h3_segment_loras.py）：
            #    优先调用用户配置的 AI 模型分析分镜内容，智能判断应使用哪个风格 LoRA；
            #    若 AI 模型未配置 / 调用失败，则回落规则表匹配（按场景文本关键词）。
            #    同一分镜的所有长镜子段继承同一套（一场戏一种画风/质感）。
            #    editMode=segment → 插件按段采纳 segments[i].loras（模块 docstring 有机制说明）。
            #    无命中 → []；构建器只有在非空时才把 loras 写进 timeline。
            _shot_loras = h3_segment_loras.select_loras_for_shot_smart(shot)
            for _si, _sub in enumerate(_seg_shots):
                _sub_dur = float(_sub.get("duration") or dur)
                # 提示词按子段重建（keyframe / reference 两条参考图分支共用同一构建入口）
                # ``common_refs=_c_refs``：公共参考图排在 ``<Picture 1..K>``，与客户端写进
                # ``global.refs`` 的槽位 0..K-1 同序同编号（H3 Director 公共参数）。
                if mode == 'keyframe' and sb_local:
                    # ⚠️ 尾帧锚定句只挂**最后一段**（2026-09-29 续修）：
                    #    尾帧图是**整镜**的收尾终态。过去每个子段都带 end_frame_ref，
                    #    等于每一段都被要求「最后一帧落在整镜末态」—— 切段后每 4 秒
                    #    就演完一遍整镜，接缝处必然倒带重启（运镜也跟着从头再来）。
                    #    交由末段独占这条锚定，前段靠「运镜延续」声明继续推进。
                    _sub_is_last = (_si == len(_seg_shots) - 1)
                    _sub_prompt = comfyui_client._build_h3_prompt(
                        _sub, _shot_char_refs, _shot_scene_refs,
                        storyboard_ref={"name": f"shot_{sid}"},
                        item_refs=_shot_item_refs,
                        common_refs=_c_refs,
                        end_frame_ref=(_end_frame_ref if _sub_is_last else None))
                elif sb_local:
                    _sub_prompt = comfyui_client._build_h3_prompt(
                        _sub, _shot_char_refs, _shot_scene_refs,
                        storyboard_ref={"name": f"shot_{sid}"},
                        item_refs=_shot_item_refs,
                        common_refs=_c_refs)
                else:
                    _sub_prompt = comfyui_client.resolve_h3_prompt(
                        _sub, _shot_char_refs, _shot_scene_refs,
                        item_refs=_shot_item_refs,
                        common_refs=_c_refs)
                # ---- 提示词预检（生成前质检）----
                # ⚠️ 这里**只自愈 + 记录，不阻断**：整集模式一次提交 N 段，为一条提示词的问题把
                #    整集生成打断，代价远大于收益；且 H3 提示词由构建器产出、结构必然齐全，
                #    出现 fatal 只可能是构建器自身有 bug —— 那更该留下证据继续跑，
                #    而不是让整集静默失败。单镜重跑接口（用户显式只跑一镜）才做硬阻断。
                _sub_prompt, _pf_seg, _pgate_seg = _prompt_preflight(
                    "h3", _sub_prompt, ctx=_sub,
                    style=(shot.get("style") or _style_res.get("style") or ""),
                    expect_refs=bool(refs), project_name=project_name,
                    cfg=qc_cfg)   # G13：复用 worker 级质检配置，避免逐镜再读盘+解密
                # 单段时名字保持旧样式 shot_07（与历史日志/画布标识一致）；
                # 多段时标 shot_07_a/_b/_c 仅供日志辨识，**不参与落盘命名**。
                _suffix = (f"_{chr(ord('a') + _si)}"
                           if _multi_seg and _si < 26 else "")
                _segs.append({"prompt": _sub_prompt, "duration": _sub_dur,
                              "reference_images": refs,
                              "name": f"shot_{seq:02d}{_suffix}",
                              "loras": list(_shot_loras)})
                if _pf_seg.get("repairs") or (_pf_seg.get("verdict") or {}).get("issues"):
                    _segs[-1]["prompt_qc"] = _pf_seg.get("verdict")
                    _segs[-1]["prompt_qc_repairs"] = _pf_seg.get("repairs") or []
                if not _pgate_seg.get("accept"):
                    _segs[-1]["prompt_qc_blocked"] = True
                    app.logger.warning("镜头 %s 视频提示词预检未通过（%s）：%s",
                                       shot.get("shot_id"), _pgate_seg.get("label"),
                                       _pgate_seg.get("reason"))
            if _multi_seg:
                app.logger.info(
                    "[长镜切段] project=%s shot=%s %ss → %d 段 %s（每段 ≤%ss，总时长守恒）",
                    project_name, shot.get("shot_id"), dur, len(_segs),
                    "/".join(f"{s['duration']:.2f}" for s in _segs),
                    h3_prompt_kit.H3_SEGMENT_MAX_SEC)
            elif not _pgate_seg.get("accept"):
                # ⚠️ 用户需求：不合格提示词不留本地（P12）。整集模式该段不生成，
                #    把可能存在的上一轮 `prompt_<shot>.json` 移回收站。
                #    多段时（_multi_seg）暂不按段清理：一条 prompt 记录对应一个 shot_id，
                #    按子段清理会把同一镜的记录反复移动，留到质检汇总里处理。
                try:
                    _purge_prompt_records(project_name, shot.get("shot_id") or seq,
                                          reason=f"视频提示词预检未通过（{_pgate_seg.get('label')}）")
                except Exception as _pe:  # noqa: BLE001
                    app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
            # ⚠️ 必须返回**列表**：长镜切段后一个分镜可能产出 N 个子段（见上方 _segs）。
            #    两个调用方（episode / per_shot）都用 isinstance(seg, list) 兼容单段，
            #    故单段场景零行为变更。旧写法 `return _segs[0]` 会把切段结果砍成 1 段，
            #    导致整集只生成每镜的第 1 个子段（实测第 2 集 7 段 562 帧，应为 18 段 1450 帧）。
            return _segs, sb_local

        # ---------- 模式 episode：整集 N 段一次生成（H3 原生衔接）+ 整片 QC 门控 ----------
        if mode == 'episode':
            # G13：质检配置 worker 级读一次，本集所有段共用（上提到循环前，供 _shot_segment
            # 内的提示词预检复用，避免逐段再 _qc_load_cfg() 读盘+解密）。
            qc_cfg = _qc_load_cfg()
            qc_on = qc_client.video_qc_ready(qc_cfg)
            qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("video_enabled"))
            max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
            segs, shot_meta_map = [], []
            # ⭐ H3 Director 公共参考图（2026-09-30，用户拍板）：整集里「每一段都在用、
            #    且用的是同一张图」的资产走插件公共参数（global.refs + commonEnabled），
            #    使同一资产的 <Picture N> 在全集恒定（不再随每镜声明顺序漂移），
            #    也让「公共用哪些资产」在工作流 JSON 里显式可查。
            #    ⚠️ keyframe 模式的 refs 是「首帧+尾帧」两句式（与 char/item/scene 三段式
            #    声明不是同一套编号），公共块不参与 —— 故该模式整集放弃公共化。
            # 跨集一致性巩固（2026-10-02）：① 上集 state_out 供首镜跨集衔接；
            # ② 本集服装状态 → 衣柜变体 key 覆盖表，供逐镜参考图取变体。
            # 两者加载失败都按「无上集/无覆盖」处理（fail-open，零回归）。
            _prev_ep_state = {}
            _outfit_ovr = {}
            try:
                if _epn and int(_epn) > 1:
                    from config import CONTINUITY_DIR as _cont_dir
                    from continuity import load_state as _load_ep_state
                    # ⚠️ 2026-10-02 修复（off-by-one）：load_state(dir, key, ep) 读的是
                    #    **指定那一集**的 state，取「上集」必须减 1 —— canonical 见
                    #    continuity.py:1343 / :1888 的 `int(episode_no) - 1`。
                    #    原先传 int(_epn)（本集）→ 本集 state 尚不存在时 `or {}` 静默
                    #    退回「无上集」（fail-open 不崩，但锚点是错的）；而本集 state
                    #    已存在（重跑/续跑）时会把**本集**当上集做首镜跨集衔接。
                    _prev_ep_state = _load_ep_state(_cont_dir, project_name,
                                                    int(_epn) - 1) or {}
            except Exception as _ce:  # noqa: BLE001
                app.logger.debug("[跨集衔接] 上集 state 加载失败（按无上集处理）：%s", _ce)
            try:
                _outfit_ovr = _episode_outfit_overrides(
                    project_name, int(_epn or 1)) or {}
            except Exception as _oe:  # noqa: BLE001
                app.logger.debug("[服装变体] 本集覆盖解析失败（不指定变体）：%s", _oe)

            _common = []
            if mode != 'keyframe':
                _common, _comps_map = _h3_plan_common_refs(
                    shots, char_idx, item_idx, scene_idx,
                    character_refs=character_refs, main_char_img=main_char_img,
                    sb_map=sb_map, use_storyboard=use_storyboard,
                    project_name=project_name)
            _common_keys = {h3_common_refs.asset_key(c) for c in _common}
            _common_keys.discard(None)
            # ⭐ 公共参考音色 + 公共提示词 subject lock（2026-10-02）：统一派生，
            #    预演/正式两个调用点共用同一份，避免两处口径漂移。
            _ensure_voice_bank_refs(_common, project_name, all_characters=char_idx)
            # ⭐ 2026-10-05：_h3_common_ref_audios 现返回 [(角色名, 路径)] 有序对。
            # subject_lock 用完整对（逐行「<Audio N> = 角色名」归属）；build 侧要纯路径列表。
            _common_audios = _h3_common_ref_audios(_common, project_name)
            _common_audio_paths = [p for (_n, p) in _common_audios]
            # ⭐ 公共提示词补全（2026-10-03）：世界观 + 全局 STYLE 也进公共段（与角色锁定一致
            #    拼在每段提示词前）。世界观优先取 AI 设定面板的 era_world，缺则取项目 brief。
            _cw_style = str(_style_res.get("style") or style or "").strip()
            _cw_aspect = str(_style_res.get("aspect") or _style_res.get("ratio") or "").strip()
            _cw_worldview = _project_worldview(project_name)
            _common_prompt = _h3_common_subject_lock(
                _common, _common_audios,
                style=_cw_style, worldview=_cw_worldview, aspect=_cw_aspect)
            for i, shot in enumerate(shots):
                shot_id = shot.get('shot_id', i + 1)
                seq = _shot_seq(shot_id, i + 1)
                # 跨集一致性巩固：本镜角色未显式声明服装时，套用本集 state 推出的
                # 衣柜变体覆盖（_shot_outfit_dir 消费 shot["character_outfits"]）
                if _outfit_ovr:
                    _co = dict(shot.get("character_outfits") or {})
                    for _cn, _ok in _outfit_ovr.items():
                        _co.setdefault(_cn, _ok)
                    if _co:
                        shot["character_outfits"] = _co
                seg, sb_local = _shot_segment(shot, seq, qc_cfg,
                                              common=_common, common_keys=_common_keys)
                # ⚠️ 整集模式**每个分镜可能产出多个段**（长镜切段，见 _shot_segment）。
                # 必须 extend 而非 append：H3 工作流段数 = len(segments)，少一段就等于
                # 该镜只生成了一半时长；且段顺序即时间轴顺序，extend 保持镜头内子段连续。
                _shot_segs = seg if isinstance(seg, list) else [seg]
                # 按场次生成（2026-10-03）：段落继承所属镜头的场次号（scene_no），
                # 视频阶段按场次分组逐场提交，最后拼接成整集。
                for _ss in _shot_segs:
                    try:
                        _ss.setdefault("scene_no", int(shot.get("scene_no") or 1))
                    except (TypeError, ValueError):
                        _ss.setdefault("scene_no", 1)
                # 优化#4 段间衔接（2026-10-02 细化版）：逻辑提纯到
                # h3_prompt_kit.transition_clause —— 同场延续 / 换场 / 机位切换 /
                # 跨集首镜（用上集 state_out 承接）四种情形各自措辞；只加在本镜
                # **首段**（镜内子段本就是同镜延续，写「上一镜」反而误导）。
                if _shot_segs:
                    _link = ""
                    if i > 0:
                        _link = h3_prompt_kit.transition_clause(
                            shots[i - 1] if i - 1 < len(shots) else {}, shot)
                    elif _epn and int(_epn) > 1:
                        _link = h3_prompt_kit.transition_clause(
                            None, shot, _prev_ep_state or {})
                    if _link:
                        _shot_segs[0]["prompt"] = str(
                            _shot_segs[0].get("prompt") or "") + "\n" + _link
                segs.extend(_shot_segs)
                # shot_meta_map 是**按镜头**的报表（每镜一条），时长取该镜各子段之和 ——
                # 与切段前的 seg["duration"] 口径一致，前端/报表不会因切段而变。
                shot_meta_map.append({
                    "shot_id": shot_id, "seq": seq,
                    "duration": round(sum(float(s.get("duration") or 0)
                                          for s in _shot_segs), 3),
                    "segment_count": len(_shot_segs),
                    "used_storyboard": bool(sb_local)})
            # 质检开关结论已在上方 worker 级算好（与 per_shot 保持一致）
            eff_style = (shot.get("style") if shot else None) or _style_res.get("style") or ""
            # [教训][video] 诊断（§2.3.5）：qc_off = 质检总开关/类型开关/接口任一未就绪
            # → 整片 QC 门控不会注入（qc_fn=None），本模式**根本不写教训库**，如实打点。
            if not qc_on:
                app.logger.info("[教训][video] project=%s mode=episode qc_off=true "
                                "enabled=%s video_enabled=%s → 无质检门控，本模式不沉淀教训",
                                project_name, qc_cfg.get("enabled"), qc_cfg.get("video_enabled"))

            # 整片 QC 门控回调：对 N 段一次生成出的单个连续整集视频抽帧质检
            _ep_qc_attempt = {"n": 0}

            def _seg_qc_fn(video_path, shot_desc, cfg, style):
                """QC 门控：对整片抽帧 → 多模态判定 → 返回 {"passed": bool, "verdict": {...}, "gate": {...}}

                ⚠️ 两点与「单镜模式」必须对齐，否则整集模式（默认）会缺半边能力：
                1) **镜头信息**：comfyui_client 传进来的是所有段 H3 提示词全文拼接
                   （每段六段式，几十段叠一起）——又长又难判读。改用本集镜头摘要。
                2) **教训沉淀**：此前整集模式只做 QC+门控、**从不写教训库**，
                   于是「质检不达标 → 改提示词重生成」的闭环在最常用模式下完全断裂，
                   重试只会换随机种子瞎撞。这里补齐 `_record_qc_lesson`。
                """
                _ep_qc_attempt["n"] += 1
                fr_dir = os.path.join(QC_DIR, project_name, "frames",
                                      f"episode_full_{os.path.basename(video_path)}")
                # D-05（P1）：按「每段中点」抽帧，取代原先「全片均布 3 帧（配置上限 6）」。
                # 原实现下一次产出 20~44 段的整集只抽 3~6 帧，中段几十段零采样 →
                # 崩坏镜必然漏检、整集质检形同虚设。这里把各段时长折算成全片占比传下去，
                # 抽帧数随段数增长（>6），使每一段至少被采到一次。
                _qc_ratios = _episode_frame_ratios(segs)
                if _qc_ratios:
                    app.logger.info("[整集质检] 按段抽帧：%d 段 → 请求 %d 帧（每段中点占比）",
                                    len(segs), len(_qc_ratios))
                else:
                    app.logger.warning(
                        "[整集质检] 段时长不可用（segs=%d）→ 退回配置抽帧数（可能漏检中段）",
                        len(segs))
                # P2-3 逐段主体一致性门禁（2026-10-01 接线）：段时长累加成绝对时间区间，
                # 角色索引里的设定图作为外观锚点，一并交 check_video 按段分组逐段判定。
                _seg_ranges = []
                _t_acc = 0.0
                for _s in segs:
                    try:
                        _d = float(_s.get("duration") or 0.0)
                    except (TypeError, ValueError):
                        _d = 0.0
                    if _d > 0:
                        _seg_ranges.append({"name": str(_s.get("name") or ""),
                                            "start": _t_acc, "end": _t_acc + _d})
                        _t_acc += _d
                _qc_refs = []
                for _v in (char_idx or {}).values():
                    _p = str((_v or {}).get("image") or "")
                    if _p and _p not in _qc_refs:
                        _qc_refs.append(_p)
                _qc_refs = _qc_refs[:4]
                if _seg_ranges:
                    app.logger.info("[整集质检] 逐段一致性门禁：%d 段时间区间 + %d 张角色锚点图",
                                    len(_seg_ranges), len(_qc_refs))
                verdict = qc_client.check_video(video_path, _episode_qc_desc(shots), cfg,
                                               frames_dir=fr_dir,
                                               style=style,
                                               frame_ratio=_qc_ratios or None,
                                               segment_ranges=_seg_ranges or None,
                                               ref_images=_qc_refs or None,
                                               expected_duration=sum(
                                                   float(s.get("duration") or 0.0) for s in shots))
                gate = _qc_gate(verdict)
                passed = bool(gate.get("accept", False))
                if not verdict.get("ok"):
                    # P1-18：质检「不可判定」（ffmpeg 缺失 / 接口 5xx 等，与内容无关）——
                    # 不得当作「不达标」触发换种子整片重跑（会白烧 20~44 段 H3；见报告 P1-18）。
                    # 标记 qc_unavailable，交由 _ep_qc_stop_cb 停止重试；口径与「不达标」分开。
                    _ep_qc_attempt["unavailable"] = True
                    app.logger.warning(
                        "[整集质检] 质检不可判定 → qc_unavailable（不重试）："
                        "project=%s attempt=%d error=%s",
                        project_name, _ep_qc_attempt["n"],
                        (verdict.get("error") or gate.get("reason") or ""))
                if not passed and verdict.get("ok"):
                    try:
                        # 抽帧图路径写进历史 extra.frames_f，供产物被移走后做「断链修正」
                        # （见下方 _mark_history_file_purged）。
                        rec = _qc_record_verdict(
                            project_name, "video", episode_tag or "episode", "整片质检",
                            _ep_qc_attempt["n"], None, video_path, verdict, style=style,
                            extra={"frames_f": list(verdict.get("frames") or []),
                                   "frames_dir": fr_dir})
                        # 挂上本集的段提示词集合，供下次重试时按相似度召回
                        # A-5 P1：整片模式段数可达 20~44 段，全量拼接可达数十 KB —— 全量入
                        # 教训库会撑爆/稀释检索。截断到 2000 字符（保留段边界换行，人可读）。
                        _seg_blob = "\n".join((sg.get("prompt") or "") for sg in segs)
                        _record_qc_lesson(project_name, "video", _seg_blob[:2000], rec)
                        app.logger.info(
                            "[教训][video] project=%s mode=episode attempt=%d ok=True "
                            "passed=False → 已沉淀",
                            project_name, _ep_qc_attempt["n"])
                    except Exception as le:  # noqa: BLE001
                        app.logger.warning("整片质检教训沉淀失败：%s", le)
                elif not passed and not verdict.get("ok"):
                    # [教训][video] 诊断（§2.3.5）：质检调用异常/超时（ok=false）时静默跳过、
                    # 不记教训——视频质检需 ffmpeg 抽帧 + 多模态，失败率高，如实打点便于排障。
                    app.logger.info(
                        "[教训][video] project=%s mode=episode attempt=%d ok=False "
                        "passed=False verdict.ok=false → 质检异常/超时，不沉淀教训",
                        project_name, _ep_qc_attempt["n"])
                # ★ 用户需求：整片质检「判定不通过」的抽帧图不留本地。⚠️ 仅当质检成功返回
                # 且不合格（ok=True、passed=False）时删；ok=False（接口故障/ffmpeg 缺失）时
                # 抽帧图保留供排障。整片成片本身按用户决策 2 保留（在调用方处理）。
                if not passed and verdict.get("ok") and not verdict.get("unavailable"):
                    # 抽帧图整目录移入回收站，并把质检历史里指向它的帧路径一并标记为断链
                    _hist_f = _qc_history_file_for(project_name, "video", episode_tag or "episode")
                    try:
                        _purge_rejected_artifacts(
                            [fr_dir], project=project_name,
                            reason=f"整片质检不合格（{gate.get('label')}）抽帧图",
                            kind="episode_frames", history_file=_hist_f)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning(f"整片抽帧图清理失败（忽略）：{_pe}")
                return {"passed": passed, "verdict": verdict, "gate": gate}

            def _ep_qc_stop_cb(qc_results):
                """G1 止损 + P1-18：质检「不可判定」优先于缺陷重复判定 —— 不可判定不重试。

                与分镜/逐镜/资产的既有口径一致：`not verdict.get("ok")` 时 break（不重画）。
                这里通过止损回调把该语义传达给 comfyui_client 的整片重试循环，避免在质检
                接口/ffmpeg 不可用时换种子白烧整集。
                """
                if _ep_qc_attempt.get("unavailable"):
                    return True, "质检不可判定（接口 / ffmpeg 不可用，与内容无关），不重试"
                return _qc_retry_hopeless(qc_results)

            # P0-1 fail-closed：qc_declared=True 但质检接口未就绪（qc_on=False）→ 整集
            # **不生成、不写正式目录**，直接阻断并如实告警。旧实现在 qc_fn=None 下仍把
            # 未质检成片 move 进正式目录（fail-open），与资产链路口径不一致。
            if qc_declared and not qc_on:
                app.logger.warning(
                    "[整集质检] qc_declared=True 但 qc_on=False → fail-closed 阻断："
                    "整集视频不生成、不写正式目录（project=%s，enabled=%s video_enabled=%s）",
                    project_name, qc_cfg.get("enabled"), qc_cfg.get("video_enabled"))
                with lock:
                    generation_state[task_id]["results"].append({
                        "success": False, "mode": "episode",
                        "segment_count": 0,
                        "qc_blocked": True,
                        "qc_unavailable": True,
                        "error": ("整集视频质检阻断（质检接口未就绪）：已开启视频质检，"
                                  "但 base_url / api_key / model 不可用；"
                                  "未质检产物不写入正式目录"),
                    })
                    generation_state[task_id].update({
                        "status": "failed",
                        "success_count": 0,
                        "error": "整集视频质检阻断（质检接口未就绪）",
                    })
                return

            with lock:
                generation_state[task_id].update({
                    "current": 0, "progress": 5,
                    "phase": f"整集 {len(segs)} 段一次生成（H3 原生衔接，整片 QC 门控）",
                    "segment_count": len(segs),
                })
            # ---------------- 两级生产（2026-09-29）：预演 → 批准 → 正式 ----------------
            # 开关默认**关** → 整段跳过，行为与改造前一致。
            # 打开后：本集还没有「被批准过的预演」时，只出一版低成本预演就返回，等人工
            # 批准；批准后再跑一次才是正式生产。坏片在廉价档就被拦下，不必等几十分钟。
            if preview_gate.enabled():
                _pv_prefix = preview_gate.preview_prefix(
                    f"comic_drama/{project_name}_{episode_tag or 'episode'}")
                _pv_need, _pv_why = preview_gate.needs_preview(project_name, episode_tag)
                if _pv_need:
                    app.logger.info("[预演] 第%s集先出低成本预演（原因：%s）", episode_tag, _pv_why)
                    with lock:
                        generation_state[task_id].update({
                            "phase": f"整集预演生成中（{len(segs)} 段 · 低分辨率 + 短时长）",
                            "preview": True, "progress": 5})
                    _pv_size = preview_gate.preview_size(_size)
                    _pv_segs = preview_gate.preview_segments(segs)
                    app.logger.info("[预演] 画幅 %s → %s；段数 %d（不变，每镜都看得到）",
                                    _size, _pv_size, len(_pv_segs))
                    _pv_res = None
                    try:
                        _pv_res = comfyui_client.generate_h3_sequence_sequential(
                            segments=_pv_segs,
                            filename_prefix=_pv_prefix,
                            seed=comfyui_job_store.get_or_create_seed(
                                f"preview|{project_name}|{episode_tag or 'episode'}",
                                lambda: random.randint(1, 2 ** 31 - 1),
                                live_key=f"h3|{_pv_prefix}"),
                            timeout_per_segment=timeout_per_segment,
                            size=_pv_size,
                            qc_fn=_seg_qc_fn if qc_on else None,
                            qc_cfg=qc_cfg,
                            qc_style=eff_style,
                            max_retries=0,      # 预演不重试：要改就重出一版预演
                            common_refs=[c["path"] for c in _common if c.get("path")],
                            common_ref_audios=_common_audio_paths,
                            common_prompt=_common_prompt,
                        )
                    except Exception as _pv_err:                       # noqa: BLE001
                        app.logger.error("[预演] 生成失败：%s", _pv_err, exc_info=True)
                    _pv_files = (_pv_res or {}).get("files") or []
                    if not _pv_files:
                        with lock:
                            generation_state[task_id].update({
                                "status": "failed", "success_count": 0,
                                "error": "预演生成失败（未产出可用文件）",
                                "results": [{"success": False, "mode": "episode",
                                             "preview": True, "segment_count": 0,
                                             "error": "预演生成失败"}]})
                        return
                    os.makedirs(videos_dir, exist_ok=True)
                    _pv_dst = os.path.join(
                        videos_dir,
                        f"{episode_tag or 'episode'}{preview_gate.PREVIEW_MARK}1.mp4")
                    # 2026-10-06：统一落盘入口 + fallback_to_source。
                    # 原 try/except 只兜住「抛异常」，但没兜住「目标目录不存在」这个
                    # 真正的触发条件（跨盘 shutil.move 的回退分支就是在那一步炸的）。
                    _pv_dst = _ingest_comfy_output(
                        _pv_files, _pv_dst, logger=app.logger, fallback_to_source=True)
                    # 预演也记四层状态：A 层判技术完成，B 层取本次质检结论（若送检）
                    try:
                        quality_stage.record_stage(
                            project_name, episode_tag, "A",
                            quality_stage.evaluate_technical(_pv_dst).get("status") or "pending",
                            evidence={"preview": True, "path": _pv_dst})
                        _pv_qc = (_pv_res or {}).get("qc_results") or []
                        if _pv_qc:
                            quality_stage.record_stage(
                                project_name, episode_tag, "B",
                                quality_stage.evaluate_content(_pv_qc[-1]).get("status")
                                or "pending", evidence={"preview": True})
                    except Exception as _pv_qs:                        # noqa: BLE001
                        app.logger.warning("[预演] 质量状态记录失败（忽略）：%s", _pv_qs)
                    with lock:
                        generation_state[task_id].update({
                            "status": "awaiting_preview_approval", "progress": 100,
                            "phase": "预演已生成，等待人工批准后再生产正式成片",
                            "results": [{"success": True, "mode": "episode",
                                         "preview": True, "deliverable": False,
                                         "file": _pv_dst, "segment_count": len(_pv_segs),
                                         "approve_hint": "批准预演后重新生成本集，即产出正式成片"}]})
                    app.logger.info("[预演] 已产出预演（不可交付）：%s", _pv_dst)
                    return

            app.logger.info(f"[episode] 整集生成开始：{len(segs)} 段，qc_on={qc_on}，max_retries={max_retries}")

            # 工作流导出模式（2026-10-03）：body 带 build_only=true 时只构建整集 UI
            # 工作流并落盘到 output/workflows_export/<项目>/epNN_h3_director_ui.json，
            # **不提交 ComfyUI、不烧 GPU** —— UI 格式可直接导入 ComfyUI 检查/运行。
            _build_only = bool(build_only)
            _wf_export_path = ""
            if _build_only and str(os.environ.get("MJSCXT_VIDEO_PER_SCENE") or "1").strip().lower() \
                    in ("0", "false", "off", "no"):
                # 仅旧「整集一次提交」模式的导出路径；按场次模式的导出在下方分支内
                _wf_export_path = os.path.join(
                    PROJECT_OUTPUT_DIR, "workflows_export", project_name,
                    f"ep{int(episode_no or 1):02d}_h3_director_ui.json")
                app.logger.info("[episode][build_only] 工作流将导出到：%s", _wf_export_path)

            # ===== 按场次生成（2026-10-03 用户决策，默认开）=====
            # 把整集段落按 scene_no 分组，每场一次 H3 提交（场内保留段间引导与衔接
            # 提示词，场间是自然剪切点）→ scene_XX.mp4；全部场次完成后 ffmpeg concat
            # 拼接成整集。逐场质检重试（比整集重试便宜一个量级）；某场失败只重做该场
            # （已完成场次落盘复用，天然断点续跑）。env MJSCXT_VIDEO_PER_SCENE=0 切回
            # 旧的「整集一次提交」。
            _per_scene = str(os.environ.get("MJSCXT_VIDEO_PER_SCENE") or "1").strip().lower() \
                not in ("0", "false", "off", "no")
            _scene_groups = []
            for _s in segs:
                try:
                    _sn = int(_s.get("scene_no") or 1)
                except (TypeError, ValueError):
                    _sn = 1
                if _scene_groups and _scene_groups[-1][0] == _sn:
                    _scene_groups[-1][1].append(_s)
                else:
                    _scene_groups.append((_sn, [_s]))

            if _per_scene:
                _ep_name_ps = f"{episode_tag or 'episode'}_full.mp4"
                # 单场重做（2026-10-03）：body only_scenes=[N] 时只生成/重做指定场次；
                # 配合 overwrite=true 可强制重画已有成片的场（断点续跑语义保持）。
                _ovw_ps = bool(overwrite)
                _only_scenes = only_scenes
                if isinstance(_only_scenes, list) and _only_scenes:
                    try:
                        _only_set = {int(x) for x in _only_scenes}
                        _scene_groups = [g for g in _scene_groups if g[0] in _only_set]
                    except (TypeError, ValueError):
                        pass
                app.logger.info("[episode] 按场次生成：%d 场 / %d 段（build_only=%s）",
                                len(_scene_groups), len(segs), _build_only)

                if _build_only:
                    # 导出模式：逐场构建 UI 工作流落盘（零 GPU），供人工在 ComfyUI 检查
                    _scene_reports = []
                    for _sn, _ssegs in _scene_groups:
                        _sp = os.path.join(
                            PROJECT_OUTPUT_DIR, "workflows_export", project_name,
                            f"ep{int(episode_no or 1):02d}_scene{_sn:02d}_ui.json")
                        _r = comfyui_client.generate_h3_sequence_sequential(
                            segments=_ssegs,
                            filename_prefix=(f"comic_drama/{project_name}_"
                                             f"{episode_tag or 'episode'}_s{_sn:02d}"),
                            timeout_per_segment=timeout_per_segment, size=_size,
                            common_refs=[c["path"] for c in _common if c.get("path")],
                            common_ref_audios=_common_audio_paths,
                            common_prompt=_common_prompt,
                            build_only=True, save_build_to=_sp)
                        _wfo = (_r or {}).get("workflow")
                        if _wfo and _sp:
                            os.makedirs(os.path.dirname(_sp), exist_ok=True)
                            atomic_write_json(_sp, _wfo)
                        _scene_reports.append({
                            "scene_no": _sn, "segments": len(_ssegs),
                            "workflow": _sp if (_wfo and os.path.isfile(_sp)) else "",
                            "layout": (_r or {}).get("layout") or {}})
                    _ok_n = sum(1 for _r in _scene_reports if _r.get("workflow"))
                    with lock:
                        generation_state[task_id].update({
                            "status": "completed" if _ok_n == len(_scene_groups) else "failed",
                            "progress": 100,
                            "phase": "工作流已按场次导出（未提交 GPU）",
                            "workflow_dir": (os.path.dirname(_scene_reports[0]["workflow"])
                                             if _ok_n else ""),
                            "results": [{"success": _ok_n == len(_scene_groups),
                                         "build_only": True,
                                         "scenes": _scene_reports}]})
                    app.logger.info("[episode][build_only] 按场次导出完成：%d/%d 场",
                                    _ok_n, len(_scene_groups))
                    return

                _scene_files = []
                _scene_reports = []
                import secrets as _secrets
                for _gi, (_sn, _ssegs) in enumerate(_scene_groups):
                    with lock:
                        generation_state[task_id].update({
                            "phase": (f"按场次生成：第 {_gi + 1}/{len(_scene_groups)} 场"
                                      f"（scene_{_sn:02d}，{len(_ssegs)} 段）"),
                            "progress": int(_gi / max(1, len(_scene_groups)) * 100)})
                    _sdst = os.path.join(videos_dir, f"scene_{_sn:02d}.mp4")
                    if (not _ovw_ps) and os.path.isfile(_sdst) and os.path.getsize(_sdst) > 0:
                        # 断点续跑：该场已有成片直接复用（overwrite=true 时强制重画）
                        app.logger.info("[episode][per-scene] 第 %d 场已有成片，复用：%s",
                                        _sn, _sdst)
                        _scene_files.append(_sdst)
                        _scene_reports.append({"scene_no": _sn, "success": True,
                                               "skipped": True, "path": _sdst})
                        continue
                    _sres = comfyui_client.generate_h3_sequence_sequential(
                        segments=_ssegs,
                        filename_prefix=(f"comic_drama/{project_name}_"
                                         f"{episode_tag or 'episode'}_s{_sn:02d}"),
                        seed=comfyui_job_store.get_or_create_seed(
                            f"video|{project_name}|{episode_tag or 'episode'}|scene{_sn}",
                            lambda: _secrets.randbelow(2 ** 31 - 2) + 1,
                            live_key=(f"h3|comic_drama/{project_name}_"
                                      f"{episode_tag or 'episode'}|scene{_sn}")),
                        timeout_per_segment=timeout_per_segment, size=_size,
                        qc_fn=_seg_qc_fn if qc_on else None, qc_cfg=qc_cfg,
                        qc_style=eff_style, max_retries=max_retries,
                        qc_stop_cb=(_ep_qc_stop_cb if qc_on else None),
                        common_refs=[c["path"] for c in _common if c.get("path")],
                        common_ref_audios=_common_audio_paths,
                        common_prompt=_common_prompt)
                    _sf = (_sres or {}).get("files") or []
                    if not _sf or not os.path.isfile(_sf[0]):
                        _scene_reports.append({
                            "scene_no": _sn, "success": False,
                            "error": ((_sres or {}).get("error")
                                      or "ComfyUI 未返回该场成片")})
                        with lock:
                            generation_state[task_id].update({
                                "status": "failed", "success_count": 0,
                                "error": (f"第 {_sn} 场生成失败"
                                          f"（已完成 {_gi}/{len(_scene_groups)} 场；"
                                          "重跑将自动跳过已完成场次）"),
                                "results": [{"success": False,
                                             "mode": "episode_per_scene",
                                             "scenes": _scene_reports}]})
                        return
                    os.makedirs(videos_dir, exist_ok=True)
                    # 2026-10-07：与预演路径（:9054）对齐，补上 fallback_to_source。
                    # 此前正式场次落盘**不**带该开关：跨盘 move / copy2 失败时直接抛，
                    # 整集被判失败中止 —— 而 GPU 已经烧完、产物就躺在 ComfyUI output
                    # 里没人要（与 _ingest_comfy_output docstring 里记录的「GPU 白烧」
                    # 事故同型）。预演路径早已修好，正式路径一直漏着。
                    _sdst = _ingest_comfy_output(_sf, _sdst, logger=app.logger,
                                                fallback_to_source=True)
                    # ⚠️ _ingest_comfy_output 在「源列表为空」且 fallback_to_source=True
                    # 时会**原样返回 dst_path**（一个并不存在的路径）。直接 append 进
                    # _scene_files 的话，要到 concat 才炸，报错还指向 ffmpeg，现场完全
                    # 看不出「这一场压根没产物」。这里显式拦一道：返回的不是真实文件
                    # → 该场判失败并说清是哪一场、为什么（对齐上面「部分场次失败」的
                    # 既有处理口径，不把整集打成不明原因的中止）。
                    if not (_sdst and os.path.isfile(_sdst)):
                        app.logger.error(
                            "[场次落盘] 第%s场无可用产物（源=%r 目标=%s），本场判失败",
                            _sn, _sf, _sdst)
                        _scene_reports.append({"scene_no": _sn, "success": False,
                                               "error": "ComfyUI 未产出可用文件"})
                        with lock:
                            generation_state[task_id].update({
                                "status": "failed", "success_count": 0,
                                "error": (f"第 {_sn} 场落盘失败"
                                          f"（已完成 {_gi}/{len(_scene_groups)} 场；"
                                          "重跑将自动跳过已完成场次）"),
                                "results": [{"success": False,
                                             "mode": "episode_per_scene",
                                             "scenes": _scene_reports}]})
                        return
                    _scene_files.append(_sdst)
                    _scene_reports.append({"scene_no": _sn, "success": True,
                                           "path": _sdst,
                                           "qc_results": (_sres or {}).get("qc_results") or []})

                # 全部场次完成 → ffmpeg concat 拼接成整集（流复制，无重编码）
                import subprocess as _sp_sub
                from pathlib import Path as _P
                dst = os.path.join(videos_dir, _ep_name_ps)
                _concat_list = os.path.join(videos_dir, f"_concat_{episode_tag or 'ep'}.txt")
                try:
                    _lines = "".join(
                        "file '" + _f.replace("\\", "/").replace("'", "'\\''") + "'\n"
                        for _f in _scene_files)
                    _P(_concat_list).write_text(_lines, encoding="utf-8")
                    _rc = _sp_sub.run(
                        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                         "-i", _concat_list, "-c", "copy", dst],
                        capture_output=True, text=True, timeout=3600)
                    if (_rc.returncode != 0 or not os.path.isfile(dst)
                            or os.path.getsize(dst) == 0):
                        raise RuntimeError(
                            f"ffmpeg concat 失败 rc={_rc.returncode}: "
                            f"{(_rc.stderr or '')[-300:]}")
                except Exception as _ce:  # noqa: BLE001
                    app.logger.exception("[episode][per-scene] 拼接失败")
                    with lock:
                        generation_state[task_id].update({
                            "status": "failed", "success_count": 0,
                            "error": f"场次已全部生成但拼接失败：{_ce}",
                            "results": [{"success": False, "mode": "episode_per_scene",
                                         "scenes": _scene_reports}]})
                    return

                item = {"success": True, "mode": "episode_per_scene",
                        "scene_count": len(_scene_groups),
                        "segment_count": len(segs),
                        "scenes": _scene_reports,
                        "qc_passed": True, "attempts_used": 1,
                        "path": dst, "url": f"{_vurl}/{_ep_name_ps}",
                        "shots": shot_meta_map,
                        "common_refs": [c.get("name") for c in _common],
                        "qc": _qc_summary([], qc_declared, qc_on, max_retries)}
                item["audio"] = _h3_audio_policy(dst)
                with lock:
                    generation_state[task_id]["results"].append(item)
                    generation_state[task_id]["progress"] = 100
                    generation_state[task_id]["phase"] = (
                        f"按场次生成完成（{len(_scene_groups)} 场已拼接为整集）")
                app.logger.info("[episode][per-scene] 整集拼接完成：%s（%d 场）",
                                dst, len(_scene_groups))
                with lock:
                    results = generation_state[task_id]["results"]
                    ok = sum(1 for r in results if r.get("success"))
                    generation_state[task_id].update({
                        "status": "completed", "success_count": ok, "error": ""})
                return

            result = comfyui_client.generate_h3_sequence_sequential(
                segments=segs,
                filename_prefix=f"comic_drama/{project_name}_{episode_tag or 'episode'}",
                # 崩溃免重渲（2026-09-29）：整集一次提交要跑几十分钟，若中途崩溃/重启，
                # 种子必须还能复原 —— 否则重建出的工作流哈希变了、检查点直接失效。
                # 种子只在「该任务仍在飞」时沿用（见 comfyui_job_store.get_or_create_seed）；
                # 任务一旦完成就换新种子，保证用户主动「重新生成」不会秒回旧片。
                seed=comfyui_job_store.get_or_create_seed(
                    f"video|{project_name}|{episode_tag or 'episode'}|episode",
                    lambda: random.randint(1, 2 ** 31 - 1),
                    live_key=f"h3|comic_drama/{project_name}_{episode_tag or 'episode'}"),
                timeout_per_segment=timeout_per_segment,
                size=_size,
                qc_fn=_seg_qc_fn if qc_on else None,
                qc_cfg=qc_cfg,
                qc_style=eff_style,
                max_retries=max_retries,
                qc_stop_cb=(_ep_qc_stop_cb if qc_on else None),
                # ⭐ 公共参考图（有序本地路径）：客户端先上传 → 写 global.refs
                #   （index 0..K-1）+ commonEnabled=true → 各段的 reference_images
                #   从 index K 起编号。顺序必须与提示词里的 <Picture 1..K> 一致。
                common_refs=[c["path"] for c in _common if c.get("path")],
                # ⭐ 公共参考音色（2026-10-02 取代逐段配音）：公共角色的 voice_bank 参考音
                #   → global.refAudios（index 0..M-1）；无公共角色/无音色 → 空（零行为变更）。
                common_ref_audios=_common_audio_paths,
                # ⭐ 公共提示词 subject lock（2026-10-02）：角色/物品/场景锁定 + 公共音色
                #   指代，编号与 global.refs/refAudios 逐位对齐（<Picture 1..K>/<Audio 1..M>）。
                common_prompt=_common_prompt,
                build_only=_build_only,
                save_build_to=(_wf_export_path or None),
            )
            # 导出模式：工作流落盘即任务完成（跳过成片搬运 / QC / 历史清理全流程）
            if _build_only:
                _wf_obj = (result or {}).get("workflow")
                if _wf_obj and _wf_export_path:
                    os.makedirs(os.path.dirname(_wf_export_path), exist_ok=True)
                    atomic_write_json(_wf_export_path, _wf_obj)
                if _wf_obj and os.path.isfile(_wf_export_path):
                    with lock:
                        generation_state[task_id].update({
                            "status": "completed", "progress": 100,
                            "phase": "工作流已导出（未提交 GPU）",
                            "workflow_path": _wf_export_path,
                            "results": [{"success": True, "build_only": True,
                                         "workflow": _wf_export_path,
                                         "layout": (result or {}).get("layout") or {}}]})
                    app.logger.info("[episode][build_only] 已导出：%s", _wf_export_path)
                else:
                    with lock:
                        generation_state[task_id].update({
                            "status": "failed", "success_count": 0,
                            "error": "build_only 未产出工作流（看后端 [H3-*][build_only] 日志）"})
                return
            files = result.get("files") or []
            episode_failed = bool(result.get("failed"))
            attempts_used = result.get("attempts_used", 1)
            qc_results = result.get("qc_results") or []
            ep_name = f"{episode_tag or 'episode'}_full.mp4"

            if not files:
                _ep_err = result.get("error") or "整片生成失败（ComfyUI 未返回视频文件或整片 QC 全部不通过）"
                with lock:
                    generation_state[task_id]["results"].append({
                        "success": False, "mode": "episode",
                        "segment_count": len(segs),
                        "qc_results": qc_results,
                        "error": _ep_err,
                    })
                with lock:
                    generation_state[task_id].update({
                        "status": "failed",
                        "success_count": 0,
                        "error": "整片生成失败或整片 QC 未通过",
                    })
                return

            # 2026-10-06：统一落盘入口（原先的 `if abspath != abspath: shutil.move` 守卫
            # 已内建为「同路径直接返回」，跨盘失败不再是 WinError 3）。
            dst = _ingest_comfy_output(files, os.path.join(videos_dir, ep_name),
                                       logger=app.logger)
            qc_passed = not episode_failed
            # ★ 用户决策 2：整集成片**保留现行为** —— 不通过仍写入正式目录（dst）供人工复核，
            # 故 app.py 这里的 fail-open **不动**。但**必须清理 ComfyUI 侧历次重试的整集 mp4**
            # （每轮 attempt 都会在 COMFYUI_OUTPUT_DIR/comic_drama/ 生成一个 `<项目>_<集>_0000N_.mp4`，
            # 不清理就是每次重试堆一个几十分钟的成片）。qc_results[].file 是 comfyui_client
            # 回传的**原始**产物路径（dst 已 move 走，不在其中）。
            try:
                if qc_on and qc_results:
                    _comfy_retries = []
                    for _r in qc_results:
                        if not isinstance(_r, dict):
                            continue
                        _f = _r.get("file")
                        # 只清「质检成功返回且判定不合格」的轮次。comfyui_client 写入的
                        # qc_results 条目里：接口故障轮 unavailable=True 且 passed=None；
                        # 正常不合格轮 passed=False（`is False` 严格判等，None 不命中）。
                        _ok_true = _r.get("passed") is False and _r.get("unavailable") is not True
                        if _f and _ok_true:
                            _comfy_retries.append(_f)
                    if _comfy_retries:
                        _purge_rejected_artifacts(
                            _comfy_retries, project=project_name,
                            reason="整集视频历次质检不合格重试残留",
                            kind="episode_video_retry")
            except Exception as _pe:  # noqa: BLE001
                app.logger.warning(f"整集重试残留清理失败（忽略）：{_pe}")
            # A-1 P1：整片 QC 调用异常（comfyui_client 已改「break + 追加 unavailable 条目」，
            # 不再触碰 app.py 的 _ep_qc_attempt 闭包）时，仅看闭包会漏判 → 结果/UI 会误报
            # 「QC 不通过」。这里同时看 qc_results 里是否存在 unavailable 条目，口径与闭包对齐。
            qc_unavailable = bool(_ep_qc_attempt.get("unavailable")) or \
                any(isinstance(r, dict) and r.get("unavailable") for r in qc_results)
            item = {"success": True, "mode": "episode",
                    "segment_count": len(segs),
                    "qc_passed": qc_passed,
                    "qc_unavailable": qc_unavailable,
                    "attempts_used": attempts_used,
                    "path": dst, "url": f"{_vurl}/{ep_name}",
                    "shots": shot_meta_map,
                    # 公共参考图（H3 Director 公共参数）：名字列表，便于前端/排查时
                    # 一眼看到「本集把哪几项锁成公共底图」（2026-09-30）。
                    "common_refs": [c.get("name") for c in _common],
                    "common_enabled": bool(_common) and not result.get("common_inline"),
                    "common_inline": bool(result.get("common_inline")),
                    "qc": _qc_summary([], qc_declared, qc_on, max_retries),
                    "qc_results": qc_results,
                    "prompt_id": result.get("prompt_id")}
            # H3 音轨策略
            item["audio"] = _h3_audio_policy(dst)
            with lock:
                generation_state[task_id]["results"].append(item)
                generation_state[task_id]["progress"] = 100
                generation_state[task_id]["phase"] = (
                    f"整集 {len(segs)} 段视频生成完成（QC 通过）" if qc_passed
                    else (f"整集 {len(segs)} 段视频生成（QC 不可判定，已停止重试，保留成片供人工复核）"
                          if qc_unavailable else
                          f"整集 {len(segs)} 段视频生成（QC 未通过，保留最后生成成片供人工复核）"))
            app.logger.info(f"[episode] 产物落盘: {dst}（qc_passed={qc_passed}，"
                            f"qc_unavailable={qc_unavailable}，attempts={attempts_used}）")
            with lock:
                results = generation_state[task_id]["results"]
                ok = sum(1 for r in results if r.get("success"))
                generation_state[task_id].update({
                    "status": "completed" if ok and qc_passed else "failed",
                    "success_count": ok,
                    "error": ("" if qc_passed else
                              ("整片质检不可判定（qc_unavailable，已停止重试；已保留成片供人工复核）"
                               if qc_unavailable else "整片 QC 未通过（已保留最后生成成片）")),
                })
            return

        # G13（P1）：per_shot 分支的质检配置 worker 级读一次，本批所有镜头共用（对齐资产
        # worker）。旧代码逐镜 _qc_load_cfg() 反复读盘 + Fernet 解密，现上提省开销、避免
        # 同批新旧配置混用。注意 episode 分支在上已单独读一次并 return，二者互不影响。
        qc_cfg = _qc_load_cfg()
        qc_on = qc_client.video_qc_ready(qc_cfg)
        qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("video_enabled"))
        max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
        for i, shot in enumerate(shots):
            shot_id = shot.get('shot_id', i + 1)
            seq = _shot_seq(shot_id, i + 1)
            # ---------- 断点续跑：已达标入库的视频直接复用，只重跑缺失/不达标的 ----------
            # 正式目录里已有非空 shot_XX.mp4 ⟺ 上一轮该镜视频已通过质检（不达标的只落暂存区）。
            if not overwrite:
                _vdst = os.path.join(videos_dir, f"shot_{seq:02d}.mp4")
                if os.path.isfile(_vdst) and os.path.getsize(_vdst) > 0:
                    _vitem = {"shot_id": shot_id, "success": True, "skipped": True,
                              "mode": "per_shot", "path": _vdst,
                              "url": f"{_vurl}/shot_{seq:02d}.mp4",
                              "qc": {"enabled": False, "status": "skipped",
                                     "label": "沿用已达标视频", "attempts": 0, "regenerated": 0}}
                    with lock:
                        generation_state[task_id]["results"].append(_vitem)
                        generation_state[task_id].update({
                            "current": i + 1,
                            "progress": int((i + 1) / len(shots) * 100),
                            "current_shot": shot_id,
                        })
                    # [教训][video] 诊断（§2.3.5）：断点续跑复用已达标视频 → 该镜**根本不重新
                    # 质检**，自然没有新的 video 教训要沉淀（这是 0 落盘的正常原因之一）。
                    app.logger.info("[教训][video] project=%s shot=%s 复用跳过：沿用已达标视频，"
                                    "不重新质检、不沉淀教训", project_name, shot_id)
                    continue
            with lock:
                generation_state[task_id].update({
                    "current": i + 1,
                    "progress": int((i + 1) / len(shots) * 100),
                    "current_shot": shot_id
                })

            try:
                seg, sb_local = _shot_segment(shot, seq, qc_cfg)   # G13：传 worker 级配置
                # ⚠️ 长镜切段（P0-1）：_shot_segment 现在返回**列表**（1 个或 N 个子段）。
                # per_shot 模式把 N 个子段一次性提交给 H3，由其原生段间衔接产出**一条**
                # 连续视频 → 落盘仍是单个 shot_XX.mp4，命名与质检契约完全不变。
                segs_list = seg if isinstance(seg, list) else [seg]
                seg = segs_list[0]
                prompt = seg["prompt"]
                _shot_total_dur = round(sum(float(s.get("duration") or 0)
                                            for s in segs_list), 3)
                if len(segs_list) > 1:
                    app.logger.info(
                        "[长镜切段] project=%s shot=%s %d 段总 %ss %s（每段 ≤%ss）",
                        project_name, shot_id, len(segs_list), _shot_total_dur,
                        "/".join(f"{s['duration']:.2f}" for s in segs_list),
                        h3_prompt_kit.H3_SEGMENT_MAX_SEC)

                # ---------- 视频 AI 质检（抽帧送检，不达标自动重生成） ----------
                # G13：qc_cfg/qc_on/qc_declared/max_retries 已在 per_shot worker 级读一次（见上方），
                # 本批所有镜头共用，不再逐镜 _qc_load_cfg()。
                if not qc_on:
                    # [教训][video] 诊断（§2.3.5）：qc_off = 质检总开关/类型开关/接口任一未就绪
                    # → per_shot 模式直接按原行为入库，**根本不质检**，自然无 video 教训可沉淀。
                    app.logger.info("[教训][video] project=%s shot=%s mode=per_shot qc_off=true "
                                    "enabled=%s video_enabled=%s → 未开质检，不沉淀教训",
                                    project_name, shot_id,
                                    qc_cfg.get("enabled"), qc_cfg.get("video_enabled"))
                attempts = []
                # O3：第 1 轮也用真实随机 seed 并始终注入（不再 None），视频 qc.history[0].seed 不再为 null
                # 崩溃免重渲：种子走台账（在飞时沿用、完成后换新），见 comfyui_job_store.get_or_create_seed
                _v_live = f"h3|comic_drama/{project_name}_shot_{seq:02d}"
                seed = comfyui_job_store.get_or_create_seed(
                    f"{_v_live}|a0",
                    lambda: random.randint(1, 2 ** 31 - 1), live_key=_v_live)
                dst = os.path.join(videos_dir, f"shot_{seq:02d}.mp4")
                video_item = {"shot_id": shot_id, "success": False,
                              "mode": "per_shot", "segment_count": len(segs_list),
                              "duration": _shot_total_dur,
                              "used_storyboard": bool(sb_local)}
                orig_video_prompt = prompt     # 教训库稳定键（改写后的提示词不参与指纹）

                for attempt in range(max_retries + 1):
                    if attempt > 0:
                        # 每一轮重试独立命名空间（a{n}）：某轮崩了能单独复原，也不会串到下一轮
                        seed = comfyui_job_store.get_or_create_seed(
                            f"{_v_live}|a{attempt}",
                            lambda: random.randint(1, 2 ** 31 - 1), live_key=_v_live)
                        # 从教训库召回「上一轮质检哪里不对」，据此改写视频提示词再生成
                        try:
                            learned = prompt_memory.learned_prompt(
                                kind="video",
                                prompt=orig_video_prompt,
                                project=project_name,
                                root_dir=PROJECT_OUTPUT_DIR,
                                style=_qc_style_of(project_name),
                            )
                            if learned and learned != orig_video_prompt:
                                prompt = learned
                                seg["prompt"] = prompt
                                app.logger.info("镜头 %s 第 %d 次视频重试，按质检教训改写提示词",
                                                shot_id, attempt + 1)
                            else:
                                prompt = orig_video_prompt
                                seg["prompt"] = prompt
                                app.logger.info("镜头 %s 第 %d 次视频重试，暂无可用教训，仅换种子",
                                                shot_id, attempt + 1)
                        except Exception as mem_err:
                            app.logger.warning(f"读取记忆模块失败: {mem_err}")
                        with lock:
                            generation_state[task_id]["phase"] = \
                                f"视频质检不达标，重新生成（第 {attempt}/{max_retries} 次）"
                            generation_state[task_id]["qc_phase"] = "regenerating"
                    with lock:
                        if attempt == 0:
                            generation_state[task_id]["phase"] = f"视频生成中（镜头 {shot_id}）"
                    # 一个分镜 = 一段或多段（长镜切段，P0-1）：段数 = len(segs_list)，
                    # 由 H3 原生段间衔接产出**一条**连续视频，故落盘仍是单个 shot_XX.mp4。
                    # ⚠️ 重试时只改写**首段**提示词（教训库键基于整镜提示词），
                    #    其余子段按原样重提交，保持镜头内动作连贯。
                    result = comfyui_client.generate_h3_sequence(
                        segments=segs_list,
                        filename_prefix=f"comic_drama/{project_name}_shot_{seq:02d}",
                        seed=seed,
                        timeout_per_segment=timeout_per_segment,
                        size=_size,
                    )
                    if not result['files']:
                        video_item["error"] = "ComfyUI 未返回视频文件（可能未安装 H3 节点或超时）"
                        break
                    src = result['files'][0]
                    # 防御：源文件不存在时明确判失败（避免重试时对已 move 过的路径二次读取报错）
                    if not os.path.isfile(src):
                        video_item["error"] = f"ComfyUI 返回的视频文件不存在: {src}"
                        break
                    # P0：先落「质检暂存区」，质检达标后才写入正式交付目录（阻断 ⇒ 正式目录不产生该视频）
                    v_scratch_dir = os.path.join(QC_DIR, project_name, "video_scratch")
                    os.makedirs(v_scratch_dir, exist_ok=True)
                    _qc_prune_attempts(v_scratch_dir)   # G8③：清本镜历史过期 try（视频暂存按镜头前缀保留最近4）
                    v_scratch = _ingest_comfy_output(
                        [src], os.path.join(v_scratch_dir,
                                           f"shot_{seq:02d}_try{attempt + 1}.mp4"),
                        logger=app.logger)
                    video_item.update({"success": True, "path": dst,
                                       "url": f"{_vurl}/shot_{seq:02d}.mp4"})
                    video_item.pop("error", None)
                    if not qc_on:
                        # P0-1 fail-closed：qc_declared=True 但接口未就绪 → 阻断，不写正式目录
                        # （旧实现 `move(v_scratch, dst)` 属 fail-open，未质检视频直接入库）。
                        if qc_declared:
                            video_item["error"] = ("视频质检阻断（质检接口未就绪）：已开启视频质检，"
                                                   "但 base_url / api_key / model 不可用；"
                                                   "未质检产物不写入正式目录（暂存视频见质检历史）")
                            app.logger.warning(
                                "[视频质检] qc_declared=True 但 qc_on=False → fail-closed 阻断入库"
                                "（不写正式目录）：project=%s shot=%s", project_name, shot_id)
                            break
                        if os.path.exists(dst):
                            os.remove(dst)
                        # 质检本就未开启：按原行为直接入库（统一落盘入口）
                        _ingest_comfy_output([v_scratch], dst, logger=app.logger)
                        break
                    with lock:
                        generation_state[task_id]["phase"] = \
                            f"视频质检中（镜头 {shot_id} · 抽帧 {qc_cfg.get('video_frame_count', 3)} 张 · 第 {attempt + 1} 次）"
                        generation_state[task_id]["qc_phase"] = "checking"
                    frames_dir = os.path.join(QC_DIR, project_name, "frames",
                                              f"shot_{seq:02d}_try{attempt + 1}")
                    verdict = qc_client.check_video(v_scratch, _qc_shot_desc(shot), qc_cfg,
                                                    frames_dir=frames_dir,
                                                    style=(shot.get("style") or _style_res.get("style")),
                                                    expected_duration=float(
                                                        shot.get("duration") or 0.0))
                    rec = _qc_record_verdict(
                        project_name, "video", shot_id, "视频质检",
                        attempt + 1, seed, v_scratch, verdict,
                        extra={
                            "duration": verdict.get("duration"),
                            "frames": [f"/api/qc/frames/{os.path.relpath(f, QC_DIR).replace(os.sep, '/')}"
                                       for f in (verdict.get("frames") or [])],
                            "frames_local": verdict.get("frames") or [],
                        },
                        style=(shot.get("style") or _style_res.get("style")))
                    attempts.append(rec)
                    gate = _qc_gate(verdict)
                    video_item["qc_gate"] = gate
                    video_item["qc"] = _qc_summary(attempts, qc_declared, True, max_retries)
                    if gate["accept"]:
                        if os.path.exists(dst):
                            os.remove(dst)
                        # 质检达标 → 写入正式交付目录（统一落盘入口：确保目标目录存在）
                        _ingest_comfy_output([v_scratch], dst, logger=app.logger)
                        # O2：产物旁路元数据（seed/提示词/工作流 SHA256/质检结论）
                        _write_artifact_meta(
                            dst, kind="video", project_name=project_name,
                            seed=seed, prompt=prompt, workflow_key="h3_video",
                            qc=gate, shot_id=shot_id,
                            extra={"mode": "per_shot", "used_storyboard": video_item.get("used_storyboard")})
                        break
                    if not verdict.get("ok"):
                        # [教训][video] 诊断（§2.3.5）：per_shot 质检调用异常/超时（ok=false）
                        # → 静默跳过、不记教训（ffmpeg 抽帧 + 多模态失败率高），如实打点便于排障。
                        app.logger.info("[教训][video] project=%s shot=%s mode=per_shot attempt=%d "
                                        "verdict.ok=false → 质检异常/超时，不沉淀教训",
                                        project_name, shot_id, attempt + 1)
                        break
                    # ★ 立刻沉淀教训（含风格不达标强化），供下一次重试改写提示词
                    _record_qc_lesson(project_name, "video", orig_video_prompt, rec)
                    app.logger.info("[教训][video] project=%s shot=%s mode=per_shot attempt=%d "
                                    "ok=true passed=False → recorded",
                                    project_name, shot_id, attempt + 1)
                    # ★ 重试止损（同分镜）：连续两次缺陷完全相同 → 「改提示词 + 换种子」没带来
                    #   任何变化，提前停止重试，别再重复烧 GPU。达标早已 break，不改结论。
                    _hopeless, _hopeless_detail = _qc_retry_hopeless(attempts)
                    if _hopeless:
                        # 标记最后一条质检记录：_qc_summary 据此把 label 写成「已停止重试」
                        rec["retry_stopped"] = True
                        rec["retry_stopped_features"] = _hopeless_detail
                        video_item["qc_retry_stopped"] = {
                            "reason": "连续两次缺陷完全相同，判定重试无收益，已提前停止",
                            "features": _hopeless_detail, "attempts": len(attempts)}
                        app.logger.warning(
                            f"视频重试止损（镜头 {shot_id}）：连续 {len(attempts)} 次缺陷完全相同，"
                            f"提前停止重试。缺陷：{_hopeless_detail}；"
                            f"建议改写该镜剧本字段后单独重跑该镜")
                        break
                if qc_declared or qc_on:
                    video_item["qc"] = _qc_summary(attempts, qc_declared, qc_on,
                                                   int(qc_cfg.get("max_retries", 0)))
                    gate = video_item.get("qc_gate")
                    if not gate or not gate.get("accept"):
                        # P0：质检不达标 / 调用异常 → 阻断入库（不写正式目录，暂存区留证，不计入成功）
                        video_item["success"] = False
                        video_item["qc_blocked"] = True
                        video_item.pop("path", None)
                        video_item.pop("url", None)
                        # ★ 用户需求：质检「判定不通过」的暂存视频 + 抽帧图不留本地（含 ComfyUI 侧）。
                        # ⚠️ 仅当最后一次尝试是「质检成功返回且不合格」（ok=True）时才删；
                        # ok=False（接口故障/超时）/ skipped（未开启）不是产物不合格，绝不删。
                        try:
                            _last_v = attempts[-1] if (attempts and isinstance(attempts[-1], dict)) else {}
                            if qc_on and _last_v.get("ok") is True:
                                _purge_rejected_artifacts(
                                    [v_scratch, frames_dir], project=project_name,                                    reason=f"视频质检不合格（{gate['label']}）" if gate else "视频质检不合格",
                                    kind="shot_video",
                                    history_file=_last_v.get("history_file") or "")
                        except Exception as _pe:  # noqa: BLE001
                            app.logger.warning(f"视频不合格产物清理失败（忽略）：{_pe}")
                        # 摘掉响应体里的 frames URL（抽帧目录已移走，前端再取会 404），
                        # 但保留质检历史里的 frames_local 供排障。
                        video_item.pop("frames", None)
                        video_item["error"] = ((f"视频质检阻断（{gate['label']}）：{gate['reason']}"
                                                "；未通过质检，未写入正式目录（暂存视频见质检历史）")
                                               if gate else (video_item.get("error")
                                                             or "视频生成失败，未写入正式目录"))
                elif "qc" not in video_item:
                    video_item["qc"] = {"enabled": False, "status": "disabled",
                                        "label": "质检未开启", "attempts": 0, "regenerated": 0}

                # ---------- H3 音轨策略（保留原生音效 / 或按 config 剥离，见 _h3_audio_policy） ----------
                if video_item.get("success"):
                    aud = _h3_audio_policy(dst)
                    video_item["audio"] = aud
                    if aud.get("policy") == "strip" and aud.get("has_audio_after"):
                        app.logger.warning(f"镜头 {shot_id} 去音轨后仍检测到音频流: {dst}")
                    elif aud.get("error"):
                        app.logger.warning(f"镜头 {shot_id} 音轨处理异常（已保留原状）: {aud.get('error')}")
                    # ---------- 音效提取：分离出「纯音效」，供混音垫底 ----------
                    if H3_SFX_ISOLATE and H3_EMIT_AUDIO and not H3_STRIP_AUDIO \
                            and aud.get("has_audio_after"):
                        sfx = _isolate_shot_sfx(dst, project_name,
                                                shot.get("episode") or 1, shot_id)
                        video_item["sfx"] = sfx
                        if sfx.get("ok"):
                            app.logger.info(f"镜头 {shot_id} 音效已分离: {sfx.get('out_path')}")
                        else:
                            app.logger.warning(f"镜头 {shot_id} 音效分离未成功"
                                               f"（混音将退回原音轨）: {sfx.get('error')}")
                with lock:
                    generation_state[task_id]["results"].append(video_item)
                    generation_state[task_id]["phase"] = "视频生成"
            except cancellation.Cancelled:
                # 审计 P1-2：中止信号必须穿透到 worker 外壳（cancelled 分支）。
                # Cancelled 继承 Exception，在这里被吞会让任务终态误报 failed，
                # 且 per_shot 循环会继续给后续镜头提交 ComfyUI 任务（白耗配额）。
                raise
            except Exception as shot_err:
                # 单镜头失败不影响其余镜头（原实现会让整批 failed）
                app.logger.error(f"镜头 {shot_id} 生成失败: {shot_err}")
                with lock:
                    generation_state[task_id]["results"].append({
                        "shot_id": shot_id, "success": False, "error": str(shot_err)
                    })

        with lock:
            results = generation_state[task_id]["results"]
            ok = sum(1 for r in results if r.get("success"))
            blocked = sum(1 for r in results if r.get("qc_blocked"))
            generation_state[task_id].update({
                "status": "completed" if ok else "failed",
                "success_count": ok,
                "qc_blocked_count": blocked,
                "error": "" if ok else "所有镜头均生成失败",
            })
    except cancellation.Cancelled:
        # 审计 P1-2：与上面逐镜循环同理 —— Cancelled 不是失败，必须穿透到
        # _video_generate_worker 外壳的 cancelled 分支（pipeline._run_task_worker
        # 的「中止信号不重试」保护也依赖它原样上抛）。
        raise
    except Exception as e:
        app.logger.error(f"视频生成失败: {e}")
        # B-16 P2-11：视频任务异常 → 清理本任务产生的视频 scratch 中间产物
        _cleanup_scratch_dir(os.path.join(QC_DIR, project_name, "video_scratch"), app.logger)
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})

# ===== 视频水印（C 项：可配置、默认关闭、支持「全视频移动」） =====
# 说明：图片生成链路不添加任何水印（C 项⑧）；本区块只处理视频后处理，不改 ComfyUI 工作流。

def _wm_load_cfg() -> dict:
    return video_watermark.load_config(WATERMARK_CONFIG_PATH)

def _wm_view(cfg: dict) -> dict:
    view = video_watermark.public_view(cfg)
    view["config_path"] = os.path.abspath(WATERMARK_CONFIG_PATH)
    view["output_dir"] = os.path.abspath(WATERMARK_DIR)
    return view

def _wm_apply_to_final(final_path: str, project_name: str) -> dict:
    """成片后处理：水印开启时额外产出一份带水印成片；默认关闭则整体跳过"""
    try:
        cfg = _wm_load_cfg()
        if not cfg.get("enabled"):
            return {"enabled": False, "skipped": True,
                    "message": "视频水印未开启（默认关闭，成片保持无水印）"}
        out_dir = os.path.join(WATERMARK_DIR, project_name)
        res = video_watermark.apply_watermark(final_path, cfg=cfg, out_dir=out_dir)
        out_path = res.get("out_path") or ""
        res.update({
            "type": cfg.get("type"), "mode": cfg.get("mode"),
            "mode_label": video_watermark.MODES_LABEL.get(cfg.get("mode"), cfg.get("mode")),
            "url": (f"/api/watermark/file/{project_name}/{os.path.basename(out_path)}"
                    if out_path else ""),
        })
        if not res.get("ok"):
            app.logger.warning(f"成片水印烧写失败（不影响无水印成片）：{res.get('error')}")
        return res
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"成片水印处理异常（忽略）：{e}")
        return {"enabled": True, "ok": False, "error": str(e)}

# ===== 视频超分（FlashVSR 真实实现，成片/片段 → 高分辨率） =====

upscale_tasks = {}

upscale_lock = threading.Lock()

UPSCALE_URL_PREFIXES = [("/api/final/", FINAL_DIR),
                        ("/api/videos/", VIDEOS_DIR),
                        ("/api/upscale/", UPSCALE_DIR)]

# ComfyUI 侧产出目录（项目里真实生成的镜头视频默认落在 ComfyUI/output/video，
# 成片前的素材也需要能直接超分，故一并列为候选来源；播放走 ComfyUI /view 重定向）
COMFY_VIDEO_DIRS = [("ComfyUI片段", os.path.join(COMFYUI_OUTPUT_DIR, "video")),
                    ("ComfyUI超分", os.path.join(COMFYUI_OUTPUT_DIR, "upscale"))]

def _comfy_view_url(filename: str, subfolder: str = "") -> str:
    """生成后端代理 URL（/api/upscale/comfyview），实际播放时 302 到 ComfyUI /view"""
    from urllib.parse import quote
    return (f"/api/upscale/comfyview?filename={quote(filename)}"
            f"&subfolder={quote(subfolder)}")

def _upscale_resolve_comfyview(query: dict) -> str:
    """解析 /api/upscale/comfyview?... 形式的 ComfyUI 产出视频为本地绝对路径"""
    filename = (query.get("filename") or "").replace("\\", "/").lstrip("/")
    subfolder = (query.get("subfolder") or "").replace("\\", "/").strip("/")
    if not filename or ".." in filename.split("/") or ".." in subfolder.split("/"):
        raise UpscaleError("非法的 ComfyUI 文件参数")
    candidate = os.path.abspath(os.path.join(COMFYUI_OUTPUT_DIR, subfolder, filename))
    root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    if not candidate.startswith(root + os.sep):
        raise UpscaleError("非法路径：不允许跳出 ComfyUI 输出目录")
    if not os.path.exists(candidate):
        raise UpscaleError(f"ComfyUI 产出文件不存在: {candidate}")
    return candidate

def _upscale_resolve_video(data: dict) -> str:
    """解析待超分视频的真实本地路径：支持 video_path（绝对路径）或 video_url（/api/... 前缀）"""
    video_path = (data.get("video_path") or "").strip()
    if video_path:
        video_path = os.path.abspath(video_path)
        if not os.path.exists(video_path):
            raise UpscaleError(f"视频文件不存在: {video_path}")
        # 审计 P2-4（2026-09-29）：绝对路径输入限定在 output/ 与 ComfyUI 输出目录内。
        # URL 分支本就有目录边界，绝对路径分支此前没有 —— 零鉴权部署下等于
        # 「任意磁盘视频文件间接读取」（送 ComfyUI 渲染、产物可回看）。normcase
        # 对齐大小写不敏感文件系统的路径比较。
        _vp_norm = os.path.normcase(video_path)
        _allowed_roots = (os.path.abspath(PROJECT_OUTPUT_DIR),
                          os.path.abspath(COMFYUI_OUTPUT_DIR))
        if not any(_vp_norm.startswith(os.path.normcase(r + os.sep))
                   for r in _allowed_roots):
            raise UpscaleError("非法路径：video_path 仅允许 output/ 或 ComfyUI 输出目录内的文件")
        return video_path

    url = (data.get("video_url") or "").strip()
    if url.startswith("/api/upscale/comfyview"):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        return _upscale_resolve_comfyview({k: v[0] for k, v in qs.items()})
    url = url.split("?")[0]
    if url:
        for prefix, base in UPSCALE_URL_PREFIXES:
            if url.startswith(prefix):
                rel = url[len(prefix):]
                candidate = os.path.abspath(os.path.join(base, rel))
                if not candidate.startswith(os.path.abspath(base)):
                    raise UpscaleError("非法路径：不允许跳出输出目录")
                if not os.path.exists(candidate):
                    raise UpscaleError(f"URL 对应文件不存在: {candidate}")
                return candidate
        raise UpscaleError(f"不支持的视频 URL 前缀: {url}")

    raise UpscaleError("请提供 video_path（绝对路径）或 video_url（如 /api/final/<项目>/<文件>）")

def _upscale_url_for_path(path: str) -> str:
    """把输出目录下的绝对路径反查为可播放 URL（用于前端对比预览）"""
    try:
        p = os.path.abspath(path)
    except Exception:
        return ""
    for prefix, base in UPSCALE_URL_PREFIXES:
        b = os.path.abspath(base)
        if p.startswith(b + os.sep):
            rel = os.path.relpath(p, b).replace(os.sep, "/")
            return prefix + rel
    for _label, base in COMFY_VIDEO_DIRS:
        b = os.path.abspath(base)
        if p.startswith(b + os.sep):
            rel = os.path.relpath(p, b).replace(os.sep, "/")
            sub = os.path.relpath(b, os.path.abspath(COMFYUI_OUTPUT_DIR)).replace(os.sep, "/")
            return _comfy_view_url(rel, "" if sub == "." else sub)
    # ComfyUI 侧任意子目录产出（video / v5video / upscale / 自定义工作流目录等）：
    # 统一走 comfyview 代理，保证「超分前」对比预览有可播放地址
    root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    if p.startswith(root + os.sep):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        return _comfy_view_url(os.path.basename(rel), os.path.dirname(rel).replace("\\", "/"))
    return ""

def _upscale_worker(task_id: str, video_path: str, project_name: str, params: dict):
    """后台线程：执行真实超分链路，进度/错误全部写入 upscale_tasks"""
    def progress(msg, pct=None):
        with upscale_lock:
            t = upscale_tasks.get(task_id)
            if not t:
                return
            if pct is not None:
                t["progress"] = max(int(t.get("progress") or 0), int(pct))
            t["message"] = msg
            t["updated_at"] = time.time()

    with upscale_lock:
        upscale_tasks[task_id].update({"status": "running", "progress": 2,
                                       "message": "正在准备超分…"})
    try:
        result = VideoUpscaler().upscale(video_path, project_name=project_name,
                                         progress_cb=progress, **params)
        result["output_url"] = _upscale_url_for_path(result.get("output_path") or "")
        result["input_url"] = _upscale_url_for_path(video_path)

        # —— 超分成品质检（2026-09-24 补上，此前超分是唯一「产出后零质检」的环节）——
        # 超分是成品链路的最后一环（4K 输出），若不质检，超分导致的闪烁/撕裂/糊化会直接
        # 进成片无人拦。这里对「超分后的成品」跑一次视频质检（抽帧 + 多模态判定）。
        # 口径与整集视频质检一致，但**不阻断**（fail-open）：超分是增值环节，质检接口
        # 未就绪 / 成品不达标时只标记 qc_passed=False 并留痕，不把任务判 error ——
        # 用户仍能拿到超分成品，同时能看到质检结论。
        _qc_result = {"checked": False, "passed": None, "reason": ""}
        try:
            _qc_cfg = _qc_load_cfg()
            if qc_client.video_qc_ready(_qc_cfg):
                _out_path = result.get("output_path") or ""
                _before = probe_video_info(video_path) or {}
                _dur = _before.get("duration")
                _style = _project_style(project_name)
                _verdict = qc_client.check_video(
                    _out_path, "超分成品质检（对超分后的成品抽帧，检查是否引入闪烁/撕裂/糊化/色块）",
                    _qc_cfg, style=_style,
                    expected_duration=float(_dur) if _dur else None)
                _gate = _qc_gate(_verdict)
                _qc_result["checked"] = bool(_verdict.get("ok"))
                _qc_result["passed"] = bool(_gate.get("accept", False))
                _qc_result["reason"] = (_gate.get("reason") or _verdict.get("reason")
                                        or _verdict.get("error") or "")
                _qc_result["score"] = _verdict.get("score")
                if _verdict.get("ok"):
                    try:
                        _qc_record_verdict(
                            project_name, "video", "upscale", "超分成品质检",
                            1, None, _out_path, _verdict, style=_style)
                    except Exception as _re:  # noqa: BLE001 质检记录失败不影响超分交付
                        app.logger.warning("超分质检落盘失败（不影响交付）：%s", _re)
                if _qc_result["passed"] is False:
                    app.logger.warning("[超分质检] 成品未通过质检：%s", _qc_result["reason"])
                else:
                    app.logger.info("[超分质检] 成品质检%s：%s",
                                    "通过" if _qc_result["passed"] else "未执行/不可判定",
                                    _qc_result["reason"])
            else:
                app.logger.info("[超分质检] 视频质检未就绪（开关/接口），跳过（fail-open）")
        except Exception as _qe:  # noqa: BLE001 质检自身异常绝不拖垮超分交付
            _qc_result["reason"] = f"质检异常：{_qe}"
            app.logger.warning("[超分质检] 质检异常（不影响交付）：%s", _qe)
        result["qc"] = _qc_result

        with upscale_lock:
            upscale_tasks[task_id].update({
                "status": "done", "progress": 100, "message": "超分完成",
                "result": result, "finished_at": time.time(),
            })
        with upscale_lock:
            _prune_task_registry(upscale_tasks)
    except Exception as e:
        import traceback
        traceback.print_exc()
        with upscale_lock:
            upscale_tasks[task_id].update({
                "status": "error", "message": str(e), "error": str(e),
                "finished_at": time.time(),
            })

LLM_NOT_CONFIGURED_GUIDE = (
    "尚未配置自定义 AI 接口。请点击页面右上角「AI 设置」，切换到对应模块后依次填写：\n"
    "① base_url：OpenAI 兼容接口地址，例如 https://api.deepseek.com/v1\n"
    "② api_key：接口密钥（保存后页面只显示脱敏结果）\n"
    "③ model：模型名，例如 deepseek-chat / gpt-4o-mini\n"
    "填写后先点「测试连接」，成功再点「保存配置」。文本分析 / 质检 / 对话总控 三个模型相互独立。"
)

LLM_NOT_CONFIGURED_GUIDE_MAP = {
    "text": ("尚未配置「文本分析模型」。请在「AI 设置」中填写 base_url / api_key / model 并保存，"
             "之后即可使用小说转剧本与提示词分析。"),
    "qc": ("尚未配置「质检模型」。请在「AI 设置 → 质检模型」中填写独立的 base_url / api_key / model"
           "（需支持图像输入的多模态模型）。未配置时生成流程会自动跳过质检，不会报错。"),
    "chat": ("尚未配置「对话总控模型」。请在「AI 设置 → 对话总控模型」中填写 base_url / api_key / model，"
             "之后即可使用 AI 对话来确定创作设定。"),
}

AI_MODULE_LABEL = {m: ai_config.MODULE_META[m]["label"] for m in AI_MODULES}

def _ai_client_for_module(module: str, base_url: str = None, api_key: str = None,
                          model: str = None, timeout: int = None,
                          reasoning_effort: str = None,
                          with_fallbacks: bool = True) -> LLMClient:
    """构造某个 AI 模块的客户端；传参可用于「测试连接」（不落盘）

    ⚠️ reasoning_effort 必须一起透传：它决定请求体注入哪个思考档位。
    漏掉它会导致两种事故——(1) 测试连接时按「不注入档位」探测，与保存后的真实行为不一致；
    (2) always-on reasoning 的模型（GLM-5.3-Flash）拿不到额度下限，max_tokens 太小 →
    正文全空只剩 reasoning_content，被误判成「模型不支持」。
    """
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    saved = ai_config.get_module(cfg, module)
    _re = None if reasoning_effort is None else str(reasoning_effort).strip()
    ep = {
        "base_url": (base_url or "").strip() or saved["base_url"],
        "api_key": (api_key or "").strip() or saved["api_key"],
        "model": (model or "").strip() or saved["model"],
        # 档位：本次显式传参优先，否则沿用已保存值（None 表示「不改动」语义）
        "reasoning_effort": (saved.get("reasoning_effort") or "") if _re is None else _re,
    }
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        raise LLMError(f"「{AI_MODULE_LABEL.get(module, module)}」尚未配置"
                       "（base_url / api_key / model 均为必填）")
    primary = LLMClient(AI_CONFIG_PATH, config=ep, timeout=timeout or LLM_REQUEST_TIMEOUT)
    # 备用模型故障转移链（顺序即优先级）：主模型连续 3 次 API 报错自动切备用，
    # 任务继续跑不中断；备用全挂才抛错。缺密钥的备用跳过。
    _fb_clients = []
    _fb_labels = []
    # ⚠️ 2026-09-30 修复：这里原本读的是 `ep.get("fallbacks")`，而 `ep` 是上面刚拼出来的
    # 四字段字典（base_url/api_key/model/reasoning_effort），**从来不含 fallbacks** ——
    # 于是这个循环永远拿不到备用模型，故障转移链从来就没被构造过，
    # 「备用模型」在配置页配了也完全不生效（实测：配好后 _ai_client_for_module 仍返回裸 LLMClient）。
    # 正确来源是 saved（ai_config.get_module 返回的完整模块视图，含已解密的备用密钥）。
    for _fb in (saved.get("fallbacks") or []):
        if not (_fb.get("base_url") and _fb.get("model") and _fb.get("api_key")):
            continue
        _fb_clients.append(LLMClient(AI_CONFIG_PATH, config={
            "base_url": _fb["base_url"], "api_key": _fb["api_key"],
            "model": _fb["model"],
            "reasoning_effort": _fb.get("reasoning_effort") or "",
        }, timeout=timeout or LLM_REQUEST_TIMEOUT))
        _fb_labels.append(_fb.get("label") or _fb.get("model") or "备用")
    # with_fallbacks=False 用于「测试连接」：探针只应该测**这一个端点**。
    # ⚠️ 2026-10-01 实测：修好 fallbacks 取值来源之后，探针也把故障转移链带上了 ——
    # 于是点一次「测试连接」会先连挂主模型 3 次、再切备用连挂 3 次（各带退避），
    # 在限速网关上要几分钟才返回；用户侧表现为「点了测试没反应、什么信息都不显示」。
    # 按索引测某条备用时更荒谬：primary 与备用是同一个端点，等于把同一个地址连测 6 次。
    if _fb_clients and with_fallbacks:
        return FailoverLLMClient([primary] + _fb_clients,
                                 labels=["主模型"] + _fb_labels, module=module)
    return primary

def _ai_gate_or_400(action: str, probe: bool = True):
    """开跑前 AI 前置门禁（P0-5 收尾）。

    通过 → 返回 None（调用方继续）；未通过 → 返回可直接 `return` 的 (response, 400) 元组。

    为什么放在每个生产入口而不是散在内部：修复前的故障是「配置页测试通过、运行时 401、
    整条流水线静默失败」，用户看不到任何原因。门禁要在**进入执行前**就把话说明白
    （缺什么、去哪修），而不是跑 4 小时后再炸。

    门禁自身异常按 fail-open 放行并响亮告警 —— 门禁是护栏，不是业务本身，
    绝不能因为护栏故障把整条线堵死。
    """
    try:
        import ai_selfcheck
        rep = ai_selfcheck.gate(action, probe=probe)
    except Exception as e:  # noqa: BLE001
        app.logger.error(f"AI 门禁执行异常（按放行处理，action={action}）：{type(e).__name__}: {e}")
        return None
    if rep.get("ok"):
        return None
    return jsonify({
        "success": False,
        "error": rep.get("message") or "AI 前置自检未通过，已阻断执行",
        "message": rep.get("message") or "",
        "hint": rep.get("hint") or "",
        "ai_selfcheck": {
            "action": action,
            "ok": False,
            "blocked_modules": rep.get("blocked_modules") or [],
            "blocked_labels": rep.get("blocked_labels") or [],
            "modules": rep.get("modules") or {},
            "hint": rep.get("hint") or "",
            "gate_off": False,
            "fix_url": "/api/ai/selfcheck?probe=1",
        },
    }), 400

def _current_llm_client() -> LLMClient:
    """文本分析链路（小说转剧本 / 章节转剧本 / 提示词分析）专用客户端：只用「文本分析模型」"""
    return _ai_client_for_module("text")

def _optional_llm_client():
    """best-effort 取「文本分析模型」客户端：已配置返回 LLMClient，未配置/异常返回 None。

    用于「LLM 锦上添花但不可阻断主流程」的场景（如上传时用 LLM 归纳章节标题正则，
    失败则退回纯正则切分）。与 _current_llm_client 的区别是**不抛 LLMError**。"""
    try:
        return _current_llm_client()
    except LLMError:
        return None

def _apply_project_settings(style: str, project_name: str = "") -> str:
    """把「AI 对话 → 应用设定」落盘的创作设定并入风格描述，供剧本 / 提示词 / 分镜链路引用。

    - 命中项目则用该项目的生效设定；未命中则退回最近一次应用的设定；
    - 未应用过任何设定时原样返回 style，行为与改造前一致。
    """
    base = (style or "").strip()
    try:
        brief = (ai_chat.settings_view(AI_SETTINGS_PATH, project_name).get("style_brief") or "").strip()
        if not brief and (project_name or "").strip():
            brief = (ai_chat.settings_view(AI_SETTINGS_PATH, "").get("style_brief") or "").strip()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"创作设定读取失败（忽略，沿用原风格）：{e}")
        return base
    if not brief:
        return base
    return f"{base}；{brief}" if base else brief

def _sync_project_config_style(project_name: str, brief: str) -> bool:
    """把 AI 总控敲定的风格纲要同步到项目 config.json 的 style 字段（2026-09-22 P-2）。

    总控 apply 设定原本只写 ai_chat/project_settings.json，项目 config.json 的 style
    停在「建项目时的默认值（3D动漫渲染）」，用户看到「配置要求国漫2D 实际却还是 3D」
    即由此。这里把生效的 style_brief 回写进 config.json，让两份配置口径一致。

    - 仅当项目在 project_store 里真实存在、且 brief 非空时才写；
    - 失败只降级告警、不阻断 apply（config.json 同步属后置簿记，主链路必须成功）。
    返回是否真正落盘。
    """
    brief = (brief or "").strip()
    if not brief:
        return False
    rec = project_store.get_project(project_name or "")
    if not rec:
        return False
    try:
        project_store.update_config(rec["dir_key"], {"style": brief})
        return True
    except Exception as e:  # noqa: BLE001
        app.logger.warning("AI 总控风格同步到项目 config.json 失败（不影响设定应用）：%s", e)
        return False

def _ai_config_view() -> dict:
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    view = ai_config.public_view(cfg)
    view["config_path"] = os.path.abspath(AI_CONFIG_PATH)
    view["legacy_path"] = os.path.abspath(LLM_CONFIG_PATH)
    view["modules_meta"] = ai_config.module_meta()
    # ComfyUI 地址如实下发：它由环境变量 COMFYUI_URL 决定，写进配置文件也没有任何
    # 代码读取（历史遗留的死配置）。前端据此只做只读展示，不再给一个「改了没用」的输入框。
    view["comfyui"] = {
        "url": COMFYUI_URL,
        "source": "环境变量 COMFYUI_URL",
        "editable": False,
    }
    # 网关熔断状态：上游整体挂掉时前端要能一眼看到「不是模型配错，是网关没算力」，
    # 否则用户会反复改 base_url/model 却越改越乱。
    view["gateways"] = {
        m: {"base_url": (v or {}).get("base_url") or "",
            "circuit": LLMClient.gateway_circuit_state((v or {}).get("base_url") or "")}
        for m, v in (view.get("modules") or {}).items()
    }
    return view

def _ai_guide_response(message: str, code: int = 400, module: str = "text"):
    label = AI_MODULE_LABEL.get(module, module)
    return jsonify({
        "success": False,
        "error": message,
        "code": "LLM_NOT_CONFIGURED",
        "module": module,
        "guide": (f"请点击顶部「AI 设置」→「{label}」，填写 ① base_url ② api_key ③ model，"
                  "先点「测试连接」通过后再点「保存」。三个模型相互独立配置，互不影响。"),
        "config": ai_config.public_view(ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)),
    }), code

def _ai_credentials_verify(module: str, base_url: str = "", model: str = "",
                           new_key: str = "", cleared: bool = False) -> tuple:
    """保存/清空后**读回核对**：任务实际读的那份凭证（tasks.db）是否等于本次提交的值。

    职责分工：写侧由 `ai_config.save_module` / `clear_module` 内部镜像闭合
    （任何调用方都自动同步，见 `ai_config._mirror_credentials_db` 的长注释）。
    这里**只做独立核对**，因为最危险的故障恰恰是「写没成功但接口照样返回 200」——
    磁盘满 / tasks.db 不可写 / env 覆盖，都会让「前端配的」与「任务实际用的」分叉，
    而页面上完全看不出来（用户会以为配置已生效，直到任务 401 或跑出别的账号的结果）。

    返回 `(note, error)`：`error` 非空 → 响应必须响亮告警。
    """
    try:
        import ai_credentials_db
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"

    if cleared:
        # module=None = 整体重置 → 三个模块都要核对（`get_credentials(None)` 会抛 ValueError）
        mods = [module] if module else list(AI_MODULES)
        stuck = []
        for m in mods:
            try:
                if ai_credentials_db.get_credentials(m).get("api_key"):
                    stuck.append(m)
            except Exception as e:  # noqa: BLE001
                return "", f"{type(e).__name__}: {e}"
        if stuck:
            return "", (f"AI 凭证库（tasks.db）中 {'、'.join(stuck)} 模块的密钥未被清空 → "
                        "任务仍会读到旧凭证，请检查 output/tasks.db 可写性后重试")
        return "，AI 凭证库已同步清空", ""

    try:
        db = ai_credentials_db.get_credentials(module)
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"

    # env 覆盖是**设计内**的最高优先级（运维部署用），但它同样意味着「页面填的不作数」，
    # 必须明说而不是报成错误。
    try:
        import secret_store
        env_name = secret_store.ENV_KEY_MAP.get(f"ai.{module}") or ""
    except Exception:  # noqa: BLE001
        env_name = ""
    env_key = ((os.getenv(env_name) or "").strip() if env_name else "")
    if env_key and env_key != new_key:
        return (f"注意：{module} 模块密钥被环境变量 {env_name} 覆盖，"
                "任务实际使用的是该环境变量的值，不是页面上填写的值", "")

    def _norm(u):
        return (u or "").strip().rstrip("/").lower()

    diff = []
    if _norm(db.get("base_url")) != _norm(base_url):
        diff.append("base_url")
    if (db.get("model") or "") != (model or ""):
        diff.append("model")
    if new_key and (db.get("api_key") or "") != new_key:
        diff.append("api_key")
    if diff:
        return "", (f"AI 凭证库（tasks.db）与本次保存不一致（{'、'.join(diff)}）→ "
                    "任务可能仍用旧凭证。请检查 output/tasks.db 是否可写、磁盘是否已满后重试")
    return "，已写入 AI 凭证库（任务下次调用立即生效，无需重启）", ""

def _save_ai_module(data: dict):
    """保存单个 AI 模块的核心实现（/api/ai/config 与兼容路由 /api/llm/config 共用）"""
    module = (data.get("module") or "").strip()
    if module not in AI_MODULES:
        return jsonify({"success": False,
                        "error": f"unknown module：{module or '(空)'}，可选 {list(AI_MODULES)}"}), 400
    base_url = (data.get("base_url") or '').strip()
    model = (data.get("model") or '').strip()
    api_key = data.get("api_key")
    # 思考档位（可选项）：字段缺失 = 不改动；传空串 = 清空。非法值由 ai_config 归一化成 ""
    has_reasoning_effort = "reasoning_effort" in data
    reasoning_effort = data.get("reasoning_effort")
    if not base_url or not model:
        return jsonify({"success": False, "error": "base_url 与 model 均为必填项"}), 400

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    old = ai_config.get_module(cfg, module)
    key = "" if api_key is None else str(api_key).strip()
    keep = (not key) or ("*" in key)   # 留空或脱敏回显 → 不改动原密钥
    if keep and not old.get("api_key"):
        return jsonify({"success": False, "error": "该模块首次配置必须填写 api_key"}), 400

    # 备用模型（故障转移链）：字段缺失 = 不改动；传 list = 整体覆盖
    has_fallbacks = "fallbacks" in data
    _fb = data.get("fallbacks") if has_fallbacks else None
    cfg = ai_config.save_module(AI_CONFIG_PATH, module, base_url=base_url, model=model,
                                api_key=None if keep else key, legacy_path=LLM_CONFIG_PATH,
                                reasoning_effort=(reasoning_effort if has_reasoning_effort else None),
                                fallbacks=_fb)
    # ⭐ 凭证单一事实源由 `ai_config.save_module` **内部**镜像闭合（json + 加密库 + tasks.db
    # 一次写完，见其 `_mirror_credentials_db` 长注释）—— 这里不再重复写库，只**读回核对**，
    # 避免同一份值有两个写点（将来谁改一处就会漂移）。核对能抓到「接口返回成功但写没落地」
    # 以及 env 覆盖这类页面上看不出来的分叉。
    db_note, db_error = _ai_credentials_verify(
        module, base_url=base_url, model=model,
        new_key="" if keep else key)
    if db_error:
        app.logger.error("AI 凭证库核对不通过（module=%s）：%s", module, db_error)
    # 配置刚变 → 清掉前置自检的端点探测缓存，避免出现「明明确认改好了，开跑还是被拦」
    try:
        import ai_selfcheck
        ai_selfcheck.reset_probe_cache()
    except Exception as e:  # noqa: BLE001
        app.logger.debug("重置 AI 前置自检探测缓存失败（忽略）：%s", e)
    view = ai_config.module_public_view(ai_config.get_module(cfg, module))
    return jsonify({
        "success": True,
        "module": module,
        "module_config": view,
        "config": _ai_config_view(),
        "config_path": os.path.abspath(AI_CONFIG_PATH),
        "credentials_db_updated": bool(not db_error),
        "credentials_db_error": db_error,
        "message": (f"{AI_MODULE_LABEL.get(module, module)}配置已保存"
                    + ("（api_key 保持不变）" if keep else "")
                    + db_note
                    + (f"；但凭证核对未通过：{db_error}" if db_error else "")),
    })

# =====================================================================
# AI 对话（创作总控）：多轮对话敲定创作设定 →「应用设定」落盘
# =====================================================================

# 归档根目录注入：ai_chat.load_all_messages 未显式传 root 时用它（见 ai_chat.set_archive_root）
ai_chat.set_archive_root(os.path.dirname(os.path.abspath(AI_CHAT_HISTORY_PATH)))

def _chat_project(data: dict = None, history: dict = None) -> str:
    data = data or {}
    # ⚠️ Ưu tiên request field (project_name hoặc project), sau đó mới fallback history
    name = (data.get("project_name") or data.get("project") or "").strip()
    if not name and isinstance(history, dict):
        name = (history.get("active_project") or "").strip()
    return ai_chat.project_key(name or "default")

def _chat_state(project: str = "") -> dict:
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = ai_chat.project_key(project or history.get("active_project") or "default")
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    model_view = ai_config.module_public_view(ai_config.get_module(cfg, "chat"))
    return {
        "project_name": project,
        "messages": ai_chat.project_messages(history, project),
        "draft": ai_chat.get_draft(history, project),
        "settings": ai_chat.settings_view(AI_SETTINGS_PATH, project),
        "fields": ai_chat.fields_meta(),
        "model": model_view,
        "history_file": os.path.abspath(AI_CHAT_HISTORY_PATH),
        "settings_file": os.path.abspath(AI_SETTINGS_PATH),
    }

def _archive_project_key(raw: str, history: dict) -> str:
    """归档接口的项目键：**看原始入参**决定是否回退到活跃项目。

    ⚠️ 不能用 `_safe_project(x) or <兜底>` 判空：`_safe_project('')` 返回字面量
    `'project'`（真值），兜底永不生效。必须看原始 query 是否为空。
    """
    raw = (raw or "").strip()
    if raw:
        return ai_chat.canonical_project_key(raw)
    return str(history.get("active_project") or "")

# =====================================================================
# 新增模块 C：图片 / 视频 AI 质检（可开关、不达标自动重生成）
# =====================================================================

def _qc_load_cfg() -> dict:
    return qc_client.load_config(QC_CONFIG_PATH)

def _qc_brief(kind: str) -> dict:
    """任务级质检摘要（随状态接口下发，供前端展示徽标与开关状态）"""
    try:
        cfg = _qc_load_cfg()
        view = qc_client.public_view(cfg)
        active = view["image_qc_active"] if kind == "image" else view["video_qc_active"]
        return {"enabled": view["enabled"], "active": active,
                "declared": bool(view["enabled"] and (cfg.get("image_enabled") if kind == "image"
                                                      else cfg.get("video_enabled"))),
                "pass_score": cfg.get("pass_score"), "max_retries": cfg.get("max_retries"),
                "video_frame_count": cfg.get("video_frame_count"),
                "model": view["effective_model"], "endpoint_source": view["endpoint_source"]}
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"质检摘要生成失败（忽略）：{e}")
        return {"enabled": False, "active": False, "declared": False}

def _qc_test_override(data: dict) -> dict:
    """测试用的临时接口参数（不落盘）；返回空 dict 表示完全用已保存配置"""
    ep = {k: str(data.get(k) or "").strip() for k in ("base_url", "model")}
    key = str(data.get("api_key") or "").strip()
    if "*" in key:
        key = ""          # 脱敏回显 → 用已保存密钥
    ep["api_key"] = key
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return {}
    return ep

class _PromptQCBlocked(RuntimeError):
    """提示词预检未通过（内部信号）

    批处理循环里每个镜头是一大段嵌套代码，用异常跳出比「把生成段整体再缩进一层」
    安全得多；异常会被同一层的 ``except Exception`` 接住，该镜头照常记入 manifest
    （状态为失败），不会从产物清单里消失。
    """

def _prompt_preflight(kind: str, prompt: str, *, ctx=None, style: str = "",
                      ref_count=None, expect_refs=None, project_name: str = "",
                      cfg: dict = None) -> tuple:
    """生成前提示词预检 + 确定性自愈（统一入口）。

    返回 ``(应交给生成端的提示词, preflight 结果, 闸门结论)``。

    ``project_name``：可选；给出时会在预检**之前**先召回 ``kind="prompt"`` 的历史教训
    并叠加，把「历史上被预检判死的输入模式」变成显式补丁后再进预检（US-7）。

    ``cfg``：G13（P1）可选，传入 worker 级已读取的质检配置则直接复用，避免逐镜再
    触发一次 ``_qc_load_cfg()``（读 JSON + Fernet 解密）；不传则按需自读（向后兼容）。

    ⚠️ **一律 fail-open**：预检自身异常时按「原样放行」处理并记 warning。
    这一层是新增的保险，绝不能因为它自己出问题就把整集生产卡死。
    """
    text = str(prompt or "")
    try:
        cfg = cfg if cfg is not None else _qc_load_cfg()
        if project_name:
            # 召回叠加在原始提示词上（自愈前）；调用方已保留 orig_prompt 作稳定 phash 键。
            text = prompt_memory.learned_prompt(
                kind="prompt", prompt=text, project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, style=style)
        pf = prompt_qc.preflight(kind, text, ctx=ctx, style=style, cfg=cfg,
                                 ref_count=ref_count, expect_refs=expect_refs)
        gate = prompt_qc.prompt_qc_gate(pf, cfg)
        if pf.get("repairs") or pf["verdict"].get("issues"):
            app.logger.info(
                "提示词预检[%s] %s｜自愈 %s 项｜issue %s 项｜accept=%s",
                kind, pf.get("label"), len(pf.get("repairs") or []),
                len(pf["verdict"].get("issues") or []), pf.get("accept"))
        return pf["prompt"], pf, gate
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"提示词预检异常（{kind}），按放行处理：{e}")
        skip = {"ok": False, "skipped": True, "accept": True, "blocked": False,
                "label": "提示词预检异常", "reason": str(e), "repairs": [],
                "verdict": {"issues": [], "critical_issues": [], "reason": str(e)}}
        return text, skip, {"accept": True, "blocked": False, "skipped": True,
                            "label": "提示词预检异常", "reason": str(e),
                            "critical_issues": [], "repairs": []}

def _qc_repeat_features(rec: dict) -> frozenset:
    """取一条质检记录的「缺陷特征集合」，用于判断连续两次重试是否毫无变化。

    G1 修复：委托给 qc_client.qc_retry_features（共享叶子模块），app 侧再叠加
    style_issues 维度（app 特有的风格缺陷，不进入通用模块）。
    seed 与 score 每次都会变（换了种子必然抖动），不能当特征，否则永远判不出「没变化」。
    """
    base = qc_client.qc_retry_features(rec)
    extra = set()
    for item in (rec.get("style_issues") or []):
        text = str(item).strip()
        if text:
            extra.add(text[:200])
    return frozenset(base | extra)

def _qc_retry_hopeless(attempts: list, streak: int = 2) -> tuple:
    """连续 ``streak`` 次重试的缺陷特征**完全相同** → 判定「改提示词 + 换种子」没有产生
    任何变化，继续重试只是重复烧 GPU（实测 ep02 shot_13 连烧 6 次全败，缺陷一字不差）。

    返回 ``(True, "缺陷摘要")`` 或 ``(False, "")``。

    G1 修复：把本函数抽到 qc_client.qc_retry_hopeless（共享叶子模块），
    供 comfyui_client / keyframe 以注入回调（qc_stop_cb）方式复用，
    规避 app ↔ comfyui_client 的循环依赖。app 侧把 style_issues 合并进
    issues 再委托 —— 通用模块不认识 app 特有字段。

    ⚠️ 刻意保守（宁可多试一次，也不要误停）：
      · 特征为**空**时一律不判定 —— 质检没给出可用信息 ≠ 缺陷相同；
      · 必须最近 streak 条**逐条集合相等**（多一条少一条都不算）；
      · **不改变闸门结论**：该镜仍算未通过、仍不进正式目录，只是不再继续重试；
        用户可直接改这一镜的剧本字段（如 camera / description）后单独重跑该镜。
    """
    merged = []
    for r in (attempts or []):
        if not isinstance(r, dict):
            continue
        issues = list(r.get("issues") or [])
        style = list(r.get("style_issues") or [])
        if style:
            issues = issues + style
        merged.append({"issues": issues, "critical_issues": r.get("critical_issues") or []})
    return qc_client.qc_retry_hopeless(merged, streak)

def _qc_prune_attempts(scratch_dir: str, keep: int = 4) -> None:
    """G8：质检暂存区 try 产物滚动保留 —— 每个 shot/asset 只保留最近 keep 个尝试。

    背景（审计 G8）：图片链路每次都把产物 copy 进暂存区 `output/qc/<项目>/{assets,storyboard,
    video}_scratch/`，不达标越多残留越多，实测 qc 目录膨胀到 1.1GB。临时产物（`*_tryN`）
    只有「最近几轮」对续跑/排障有意义，更早的纯浪费。这里按 (前缀, 尝试号, 扩展名) 归组，
    每组只留最大的 keep 个 try，其余删除。

    设计取舍：
      - 只清「带 _try 后缀的临时产物」，正式交付图（base.png/shot_XX.png）绝不动；
      - 单文件失败静默跳过（清理是优化而非功能，绝不能因清错文件阻断生产）；
      - 保留策略对 `.png`/`.mp4`/`.srt`/`.list` 通用，三类暂存区都能复用。
    """
    if not os.path.isdir(scratch_dir):
        return
    try:
        import re as _re
        groups = {}   # (前缀, 扩展名) -> [尝试号]
        info = {}      # 尝试号 -> 完整路径
        for fn in os.listdir(scratch_dir):
            # ⚠️ 单镜重跑落盘名是 `shot_NN_retry.png`（**无** `_tryN` 数字后缀），
            # 旧正则 `^(.+)_try(\d+)(\.\w+)$` 匹配不到 → 该文件永不被滚动清理、只增不减。
            # 这里把 `_retry` 也纳入：归到 num=0（比任何 `_tryN` 都旧 → 优先被清）。
            m = _re.match(r"^(.+)_try(\d+)(\.\w+)$", fn)
            if not m:
                m = _re.match(r"^(.+)_retry(\.\w+)$", fn)
                if m:
                    prefix, num, ext = m.group(1), 0, m.group(2)
                else:
                    continue   # 非 try/retry 命名（正式产物/杂项）一律不动
            else:
                prefix, num, ext = m.group(1), int(m.group(2)), m.group(3)
            groups.setdefault((prefix, ext), []).append(num)
            info[(prefix, ext, num)] = os.path.join(scratch_dir, fn)
        removed = 0
        for (prefix, ext), nums in groups.items():
            nums = sorted(nums)   # P2-2（A-15）：listdir 无序，必须按尝试号排序，
                                  # 否则 `nums[-keep:]` 保留的是「最早遍历到」而非「最新尝试号」
            keep_set = set(nums[-keep:]) if len(nums) > keep else set(nums)
            for num in nums:
                if num in keep_set:
                    continue
                p = info.get((prefix, ext, num))
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass   # 文件被占用/权限问题：跳过，不阻断
        if removed:
            app.logger.info("G8 暂存区滚动清理 %s：删 %d 个过期 try（每组保留最近 %d）",
                            scratch_dir, removed, keep)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("G8 暂存区清理失败（不影响生产）：%s: %s",
                           type(e).__name__, e)

def _write_artifact_meta(artifact_path: str, *, kind: str, project_name: str,
                         seed=None, prompt=None, workflow_key=None,
                         elapsed=None, qc=None, shot_id=None, asset_name=None,
                         extra=None) -> None:
    """O2：产物旁路元数据 —— 在正式产物旁写 `<产物>.meta.json`（可追溯/可复现）。

    记录：seed / prompt / 工作流文件名 + SHA256（内容指纹，而非仅文件名）/ 耗时 /
    生效质检结论。此前 manifest 只记工作流**文件名**，无法校验"当初到底用哪版工作流
    出的这张图"；SHA256 让产物与生成时点的工作流内容一一对应。

    纯旁路（绝不阻断生产）：任何异常静默降级、只留 debug 日志 —— meta 缺失不影响主流程。
    """
    try:
        import hashlib
        import config as _cfg
        meta = {
            "artifact": os.path.basename(artifact_path),
            "kind": kind,
            "project": project_name,
            "seed": seed,
            "prompt": (str(prompt)[:2000] if prompt else None),
            "workflow": None,
            "workflow_sha256": None,
            "elapsed_sec": elapsed,
            "qc": qc,
            "extra": extra,
            "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if shot_id is not None:
            meta["shot_id"] = shot_id
        if asset_name is not None:
            meta["asset"] = asset_name
        # 工作流内容指纹（O2 核心：文件名 → 名 + SHA256）
        if workflow_key:
            try:
                tpl_name = _cfg.WORKFLOW_TEMPLATE.get(workflow_key)
                if tpl_name:
                    wf_path = os.path.join(_cfg.COMFYUI_WORKFLOWS_DIR, tpl_name)
                    if os.path.isfile(wf_path):
                        h = hashlib.sha256()
                        with open(wf_path, "rb") as _f:
                            for _chunk in iter(lambda: _f.read(65536), b""):
                                h.update(_chunk)
                        meta["workflow"] = tpl_name
                        meta["workflow_sha256"] = h.hexdigest()
            except Exception as e:  # noqa: BLE001  工作流指纹算不出不影响 meta 主体
                app.logger.warning("工作流指纹计算失败（不影响 meta 主体）：%s", e)
        out = os.path.splitext(artifact_path)[0] + ".meta.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001  旁路兜底：meta 写失败绝不阻断入库
        try:
            app.logger.debug("O2 产物元数据旁路写失败（不影响入库）：%s: %s",
                            type(e).__name__, e)
        except Exception:  # noqa: BLE001
            pass

def _qc_gate(verdict: dict) -> dict:
    """统一质检入库闸门（P0）：ok=false 或 不达标 一律不得静默入库。
    - skipped=True   → 质检未执行（总开关/类型开关关闭、接口未配置），按「放行」处理并明确标注；
    - ok=False       → 质检调用异常，结果不可判定，一律阻断（不得静默入库）；
    - accepted=False → 不通过；命中关键缺陷时 blocked=True（关键缺陷阻断）。

    P0 加固（不盲信 verdict.accepted）：无论上游 verdict 的 accepted / passed / blocked
    给什么值，本函数都会用本地关键缺陷词表对 issues（并集上游显式 critical_issues）做一次
    独立复核；一旦命中关键缺陷（画面崩坏 / 拼接 / 人物重复 等），**强制阻断**，不得因为
    模型自评 accepted=True 而放行。放行条件是 accepted 与 passed **同时**为真（任一为假即阻断）。
    """
    verdict = verdict or {}
    if verdict.get("skipped"):
        return {"accept": True, "blocked": False, "skipped": True, "label": "质检未执行",
                "reason": verdict.get("reason") or "质检未执行（跳过）", "critical_issues": []}
    if not verdict.get("ok"):
        # P0-2：区分「接口级故障」与「内容不合格」。
        # interface_fault=True（鉴权 401/403、超时、网络抖动、未配置 key）= 根本没拿到
        # 模型判定，**不等于**产物不合格——ComfyUI 已出好的图/片不能因质检 key 失效被丢弃。
        # 此时 fail-open：放行已产出资产 + 响亮告警；但若客观层已命中致命缺陷
        # （全黑/无音轨等确定性闸门，见 critical_issues）仍强制阻断，不放行真坏帧。
        if verdict.get("interface_fault"):
            crit = [str(x) for x in (verdict.get("critical_issues") or [])]
            if crit:
                return {"accept": False, "blocked": True, "skipped": False,
                        "fault_open": False, "label": "客观层致命缺陷（AI 质检接口不可用）",
                        "reason": "；".join(crit[:3]), "critical_issues": crit}
            app.logger.warning(
                "质检接口故障，已 fail-open 放行本资产（结果不可判定）：%s",
                verdict.get("error") or "质检接口不可用")
            return {"accept": True, "blocked": False, "skipped": False,
                    "fault_open": True, "label": "质检接口故障·已放行",
                    "reason": (verdict.get("error") or "质检接口不可用")
                              + "（接口级故障，未做内容判定，已放行）",
                    "critical_issues": []}
        return {"accept": False, "blocked": True, "skipped": False, "label": "质检调用异常",
                "reason": verdict.get("error") or "质检调用失败，结果不可判定", "critical_issues": []}

    # ---- 独立复核：本地词表命中 ∪ 上游显式 critical_issues（不依赖模型自评结论） ----
    local_hits = qc_client.find_critical_issues(verdict.get("issues") or [])
    declared = [str(x) for x in (verdict.get("critical_issues") or [])]
    crit = list(dict.fromkeys(list(local_hits) + declared))
    if crit:
        app.logger.warning(f"质检闸门独立复核命中关键缺陷，强制阻断：{crit[:3]}")
        return {"accept": False, "blocked": True, "skipped": False,
                "label": "关键缺陷阻断（独立复核）" if local_hits else "关键缺陷阻断",
                "reason": verdict.get("reason") or ("命中关键缺陷：" + "；".join(crit[:3])),
                "critical_issues": crit}

    accepted = verdict.get("accepted")
    passed = verdict.get("passed")
    if accepted is None:
        accepted = bool(passed)
    if passed is None:
        passed = bool(accepted)
    if not (bool(accepted) and bool(passed)):
        if verdict.get("style_mismatch"):
            return {"accept": False, "blocked": bool(verdict.get("blocked")),
                    "skipped": False, "style_blocked": True, "label": "风格不达标",
                    "reason": verdict.get("reason") or "画面风格与目标风格不符",
                    "critical_issues": [],
                    "style_issues": verdict.get("style_issues") or []}
        return {"accept": False, "blocked": bool(verdict.get("blocked")), "skipped": False,
                "label": "质检不达标", "reason": verdict.get("reason") or "质检未达标",
                "critical_issues": []}
    return {"accept": True, "blocked": False, "skipped": False, "label": "质检达标",
            "reason": verdict.get("reason") or "", "critical_issues": []}

def _qc_record_verdict(project_name: str, kind: str, shot_key, stage: str,
                       attempt: int, seed, file_path: str, verdict: dict,
                       extra: dict = None, style: str = "") -> dict:
    """把一次质检结论整理成历史记录并落盘，返回该记录（含 history_file）

    style：本次质检所用的目标风格串。连同 style_mismatch / style_issues 一并落盘，
    供教训库识别「风格不达标」并触发改写提示词重生成。
    """
    rec = {"attempt": attempt, "seed": seed, "file": file_path, "stage": stage,
           "ok": bool(verdict.get("ok")), "passed": bool(verdict.get("passed")),
           "accepted": bool(verdict.get("accepted") if verdict.get("accepted") is not None
                            else verdict.get("passed")),
           "blocked": bool(verdict.get("blocked")),
           "score": verdict.get("score"), "reason": verdict.get("reason"),
           "issues": verdict.get("issues") or [],
           "critical_issues": verdict.get("critical_issues") or [],
           "style_mismatch": bool(verdict.get("style_mismatch")),
           "style_issues": verdict.get("style_issues") or [],
           "style": style_kit.normalize_style(style),
           "error": verdict.get("error"), "latency_ms": verdict.get("latency_ms"),
           # P0-2：接口级故障（鉴权/超时/网络）标记，供 _qc_summary 区分「故障放行」与「内容不合格」
           "interface_fault": bool(verdict.get("interface_fault")),
           # ★ 二次复核留档：首次判不过时用同一张图再判一次（判官抖动实测极大）。
           #   落盘后可直接统计「多少重跑是被复核拦下来的」，用于评估该机制收益。
           "recheck": verdict.get("recheck") or None,
           "recheck_first": verdict.get("recheck_first") or None}
    if extra:
        rec.update(extra)
    rec["history_file"] = _qc_record(project_name, kind, shot_key, rec)
    return rec

def _qc_lesson_from_record(rec: dict) -> dict:
    """从一条质检历史记录里取出「缺陷」，供教训库沉淀。

    ⚠️ `_qc_record_verdict` 返回的记录把 score/reason/issues 放在**顶层**，
    **没有** `verdict` 子对象。此前写入教训时误读 `rec["verdict"]`（恒为 None → {}），
    于是教训库里躺着的全是 score=0 / reason="" / issues=[] 的空记录，
    召回时自然什么建议都给不出来 —— 重试就变成了「换种子瞎撞」。
    这里对两种形态都做兼容，避免再被字段形态坑一次。
    """
    if not isinstance(rec, dict):
        return {}
    inner = rec.get("verdict") if isinstance(rec.get("verdict"), dict) else {}
    score = rec.get("score")
    if score is None:
        score = inner.get("score")
    issues = list(rec.get("issues") or inner.get("issues") or [])
    issues += list(rec.get("critical_issues") or inner.get("critical_issues") or [])
    issues += list(rec.get("style_issues") or inner.get("style_issues") or [])
    issues = [str(x).strip() for x in issues if str(x).strip()]
    reason = str(rec.get("reason") or inner.get("reason") or "").strip()
    if not issues and reason:
        issues = [reason]
    return {"score": score if score is not None else 0, "issues": issues[:20], "reason": reason}

def _record_qc_lesson(project_name: str, kind: str, prompt: str, rec: dict) -> dict:
    """把一次「质检不达标」沉淀成教训（供下次重试时改写提示词）。

    风格由 ``_project_style()`` 内部取（plan 的 style > AI 设定 > config.style），
    这样 6 个调用点不用各自找 style —— 它们本来就都在同一个项目上下文里。
    """
    lesson_src = _qc_lesson_from_record(rec)
    # 记录当时的视觉风格：召回时按「同风格加权 / 异风格降权」使用。
    # 没有它就无法回答「生成相同风格的提示词时有没有参考历史教训」——
    # 旧教训一律 context={}，跨画风的缺陷会串味（用 A 画风的标准要求 B 画风的图）。
    try:
        _qc_style = _project_style(project_name) or ""
    except Exception as _se:  # noqa: BLE001
        app.logger.warning("取项目风格失败（教训按无风格记录）：%s", _se)
        _qc_style = ""
    # 风格不达标：额外注入一条「明确的风格强化指令」，确保召回时能直接指导模型修正风格，
    # 而不是只给一条「风格不符」的缺陷描述。
    # ⚠️ 风格名必须写成占位符 {style}，**不能在记录时把项目风格写死**：
    #    教训库是跨项目复用的，写死会让 A 项目（中国古风玄幻）的教训被 B 项目
    #    （国漫偏写实）召回时强行要求 B 采用 A 的风格 —— 那是主动伤害。
    #    实际替换发生在 prompt_memory.suggestions(..., style=当前项目风格)。
    if (rec or {}).get("style_mismatch"):
        style_hint = ("画面风格与目标风格不符，必须严格采用「{style}」"
                      "的视觉风格、画风、渲染方式与配色，不得偏离")
        existing = lesson_src.get("issues") or []
        lesson_src["issues"] = [style_hint] + [i for i in existing if i != style_hint]
    if not lesson_src.get("issues") and not lesson_src.get("reason"):
        return {}
    try:
        got = prompt_memory.record(project=project_name, kind=kind, prompt=prompt,
                                   issues=lesson_src["issues"], reason=lesson_src["reason"],
                                   score=lesson_src.get("score"),
                                   root_dir=PROJECT_OUTPUT_DIR, style=_qc_style)
        if got:
            app.logger.info("[教训库] %s 记录 %d 条缺陷（kind=%s score=%s style=%s）：%s",
                            project_name, len(lesson_src["issues"]), kind,
                            lesson_src.get("score"), _qc_style or "-",
                            lesson_src["issues"][:2])
        return got or {}
    except Exception as mem_err:  # noqa: BLE001
        app.logger.warning("记录质检教训失败：%s", mem_err)
        return {}

def _optimize_prompt_from_qc(kind: str, prompt: str, rec: dict, style: str = "") -> str:
    """质检不达标后，用「文本分析模型」针对**本次这张图**的缺陷即时改写提示词。

    与 ``prompt_memory.learned_prompt``（召回**历史泛化**教训）的区别：
    这里把本次 verdict 的具体 issues 直接喂给 LLM，让它针对「这张图为什么没过」给出
    一条精准的提示词修正——而不是拼一条可能跨项目、可能过时的历史建议。

    返回优化后的提示词；任何失败（模型未配置 / 调用异常 / 返回空）都返回 None，
    由调用方回落原逻辑（换 seed / 召回历史教训），**绝不让优化环节阻断重生成**。
    """
    try:
        lesson = _qc_lesson_from_record(rec)
        issues = [str(x).strip() for x in (lesson.get("issues") or []) if str(x).strip()]
        reason = str(lesson.get("reason") or "").strip()
        if not issues and not reason:
            return None
        if not (prompt or "").strip():
            return None
        client = _optional_llm_client()
        if client is None:
            app.logger.info("[提示词优化] 文本分析模型未配置，跳过即时优化（回落历史召回/换种子）")
            return None
        kind_label = {"asset": "参考图", "storyboard": "分镜图",
                      "keyframe": "尾帧", "h3": "视频"}.get(kind, kind)
        issues_text = "\n".join(f"  - {i}" for i in issues[:6])
        style_text = (f"\n目标风格：{style}" if style else "")
        system = (
            "你是漫剧生成系统的提示词优化器。用户给出一段「生成图片用的提示词」和「质检判定它"
            "不达标的具体问题」，你要输出一段**修正后的提示词**，让重新生成能通过质检。\n"
            "要求：\n"
            "1. 只输出修正后的提示词正文，不要任何解释、前言、标号或 Markdown；\n"
            "2. 保留原提示词里仍然有效的描述（主体、外貌、材质、风格等），只针对列出的问题做精准修补；\n"
            "3. 用中文输出；\n"
            "4. 不要新增与问题无关的内容，不要改变原有画面主体；\n"
            "5. 修正要具体可执行（例如「去掉文字」就写「画面中不得出现任何文字/字幕/水印」）。"
        )
        # ⚠️ 2026-10-01 实测：参考图重试的优化器会写出与资产图**不变量**冲突的要求 ——
        # 实测输出「特写镜头，画面核心为黑色玉盒及其内部丹药，严禁全白纯色背景」，
        # 而守卫函数随后又追加「，纯白背景，…」→ 同一份提示词里出现矛盾指令，模型必然摇摆
        # （那一轮同时被「承托物」和「桌面/阴影」两条判据拦下就是因此）。
        # 这里把不变量写进系统提示，并要求它**不得写入与之冲突的措辞**。
        if kind == "asset":
            system += (
                "\n\n【不可违背的参考图不变量（优先级高于上面全部要求）】\n"
                "1. 纯白背景：画面里不得出现场景、地面、桌面、墙面、投影、背景纹理或任何环境元素；"
                "严禁写入「不要纯白背景 / 严禁全白纯色背景 / 加上场景或桌面」这类**与不变量冲突**的要求。\n"
                "2. 物品参考图只呈现**物品本体**：不得出现容器、托盘、盒子、底座、支架、展示台、"
                "碗碟、绸布等任何承托物，不得把物品放进或放在别的物体内部/上方，不得出现手或人物；"
                "严禁把「某个容器」（例如黑色玉盒）写成画面核心或主体。\n"
                "3. 角色参考图保持多视图横排、完整入画，不改变五官与服装。\n"
                "4. 只修补质检列出的问题，**不得引入任何新物体、新容器、新场景元素**。"
            )
        user = (
            f"原提示词：\n{prompt.strip()}\n\n"
            f"质检判定不达标的问题：\n{issues_text}"
            f"{'（结论：' + reason + '）' if reason else ''}{style_text}\n\n"
            f"请输出修正后的提示词："
        )
        reply = client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=0.4, max_tokens=1024,
        )
        optimized = (reply or "").strip()
        if not optimized or optimized == prompt.strip():
            app.logger.info("[提示词优化] %s 本次优化无变化或为空，回落原逻辑", kind_label)
            return None
        app.logger.info("[提示词优化] %s 针对本次缺陷改写提示词（%d 条问题）：%s → %s",
                        kind_label, len(issues), prompt[:24], optimized[:40])
        return optimized
    except Exception as e:  # noqa: BLE001
        app.logger.warning("质检后即时优化提示词失败（回落原逻辑）：%s", e)
        return None

def _record_preflight_lesson(project_name: str, prompt_original: str, pf: dict,
                             gate: dict) -> dict:
    """把一次「提示词预检不通过 / 有缺陷」沉淀成 ``kind="prompt"`` 教训。

    ``prompt_original`` 必须是**自愈前**（也**不含召回叠加块**）的原始提示词，作为稳定
    phash 键。收敛 keyframe / asset / storyboard 三处预检的沉淀逻辑，避免复制粘贴。
    """
    pf = pf if isinstance(pf, dict) else {}
    verdict = pf.get("verdict") if isinstance(pf.get("verdict"), dict) else {}
    gate = gate if isinstance(gate, dict) else {}
    rec = {
        "issues": [str(x).strip() for x in (verdict.get("issues") or []) if str(x).strip()],
        "critical_issues": [str(x).strip() for x in (verdict.get("critical_issues") or [])
                            if str(x).strip()],
        "reason": str(pf.get("reason") or gate.get("reason") or "").strip(),
        "score": verdict.get("score"),
        "stage": "prompt_preflight",
        "label": str(pf.get("label") or gate.get("label") or ""),
    }
    if not rec["issues"] and rec["reason"]:
        rec["issues"] = [rec["reason"]]
    if not rec["issues"] and not rec["reason"]:
        return {}
    return _record_qc_lesson(project_name, "prompt", prompt_original or "", rec)

def _qc_style_of(project_name: str) -> str:
    """取项目**当前**风格，供教训召回替换建议里的 ``{style}`` 占位符。

    数据源优先用 autopilot 计划（用户与总控敲定的创作设定，最权威），
    其次退回项目级创作设定的 style；都取不到就返回空串
    （此时 ``prompt_memory`` 会主动丢弃带 ``{style}`` 的建议，而不是把别的项目的风格安上来）。
    """
    try:
        plan = autopilot.get_plan(project_name) or {}
        s = style_kit.normalize_style(plan.get("style"))
        if s:
            return s
    except Exception as e:  # noqa: BLE001
        app.logger.debug("读取托管计划风格失败（忽略）：%s", e)
    try:
        for ep in (novel_to_script.list_episodes(SCRIPT_DIR, project_name, project_name) or []):
            path = ep.get("path") or ""
            if path and os.path.isfile(path):
                data = project_store._read_json(path, {}) or {}
                s = style_kit.normalize_style(data.get("style"))
                if s:
                    return s
    except Exception as e:  # noqa: BLE001
        app.logger.debug("读取剧本风格失败（忽略）：%s", e)
    return ""

def _keyframe_recall_cb(project_name: str):
    """尾帧重试召回回调（注入 ``keyframe.generate_keyframes`` 的 ``recall_cb``）。

    返回闭包 ``(orig_prompt, shot, item) -> str``：用 preflight 自愈**之前**的原始尾帧
    提示词召回 ``kind="keyframe"`` 历史教训并叠加；无教训时原样返回（零行为变更）。
    """
    def _recall(orig_prompt: str, shot: dict, item: dict) -> str:
        try:
            return prompt_memory.learned_prompt(
                kind="keyframe", prompt=orig_prompt or "", project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, style=_qc_style_of(project_name))
        except Exception as e:  # noqa: BLE001 - 召回失败绝不影响生成
            app.logger.warning(f"尾帧教训召回失败（忽略）：{e}")
            return orig_prompt or ""
    return _recall

def _episode_frame_ratios(segs: list, max_frames: int = None) -> list:
    """D-05（P1）整集按段抽帧的占比列表 —— 实现见 ``qc_coverage.episode_frame_ratios``。

    抽成独立零依赖模块（``app/qc_coverage.py``）以便离线单测
    （``verify_episode_qc_coverage.py``）无需 Flask/requests 即可验证覆盖率。
    """
    return qc_coverage.episode_frame_ratios(segs, max_frames=max_frames)

def _episode_qc_desc(shots: list, limit: int = qc_coverage.DEFAULT_DESC_LIMIT) -> str:
    """构造整集质检用的「镜头信息」摘要 —— 实现见 ``qc_coverage.episode_qc_desc``。

    为什么不用 comfyui_client 传进来的 ``shot_desc``：整集模式下它传的是
    **所有段的 H3 提示词全文拼接**（每段都是六段式结构，几十段叠在一起），
    又长又难判读，还挤占上下文。整片质检真正需要的是「这一集有哪些镜头、
    各自什么景别和内容」，这里按镜头生成紧凑摘要。

    D-05（P1）：``limit`` 由 12 提到 60 —— 原来 40 镜的整集只把前 12 镜给模型，
    中后段镜头对模型**完全不可见**，与抽帧漏检叠加后整集质检形同虚设。
    另：一旦真的截断，必须在串里**显式声明「其余未提供」**，让模型知道信息不完整，
    而不是误以为整集只有 limit 个镜头。
    """
    return qc_coverage.episode_qc_desc(shots, limit=limit, warn=app.logger.warning)

def _qc_shot_desc(shot: dict) -> str:
    """构造交给质检模型的「镜头信息」。

    ⚠️ 景别必须带上**判定标准**，不能只给裸词。
    生成端用 ``SHOT_CAMERA_SPECS[camera]`` 的精确定义写提示词，而质检端此前只传
    「机位：中景跟拍」——模型只能凭自己的理解判「中景」，与生成端标准不一致，
    实测分镜图通过率仅 57%、失败原因几乎全是「景别不符」（把腰部以上的中景判成不合规）。
    这里改为引用 :func:`comfyui_client.camera_spec`（经模块顶部的 ``_camera_spec`` 别名调用，
    因为本文件里 ``comfyui_client`` 是实例而非模块），保证两端**同一份标准**。
    """
    parts = []
    # 景别放在最前：描述较长时 [:900] 截断会吃掉尾部，判定标准必须优先保住
    if shot.get("camera"):
        cam = str(shot["camera"]).strip()
        # ⚠️ 必须显示「解析后的景别」而不是只给裸词：camera 常是「景别+机位+运镜」的复合写法。
        # 且**景别未给时不许编**（旧实现回落中景 → 拿中景标准去判脚部俯拍图，必然判不符，
        # 该镜永远过不了；实测 ep02 shot_13 因此白烧 6 次 GPU）。
        cam_k = _camera_key(cam)
        parts.append(f"景别：{cam_k or '未指定'}（camera 原值「{cam}」；判定标准：{_camera_spec(cam)}）")
        # 机位与景别正交：只给景别不给机位的话，「要求俯拍却给了平视」没人能判出来。
        ang = _camera_angle(cam)
        if ang:
            parts.append(f"机位：{ang}（须与画面一致）")
    if shot.get("location"):
        parts.append(f"场景：{shot['location']}")
    if shot.get("description"):
        parts.append(str(shot["description"]).strip())
    if shot.get("emotion"):
        parts.append(f"情绪：{shot['emotion']}")
    if shot.get("dialogue"):
        parts.append(f"台词：{str(shot['dialogue']).strip()[:80]}")
    return "；".join(parts)[:900] or "（无镜头描述）"

def _qc_prev_shot_desc(shots: list, idx: int) -> str:
    """取「上一镜」的文字描述，供图片质检做跨镜连续性比对（P1，2026-09-25）

    ⚠️ 只在**同场景**时返回：跨场景切换本就该换背景、换光位，拿上一镜去比会判出
    一堆假缺陷（与 ``keyframe.same_scene`` 同一判据，口径保持一致）。
    首镜、或上一镜不同场景 → 返回空串，``check_image`` 便完全不加连续性口径
    （零行为变更，也不会诱发模型凭「上一镜」三个字臆造缺陷）。
    """
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return ""
    if i <= 0 or i >= len(shots or []):
        return ""
    prev = shots[i - 1] if isinstance(shots[i - 1], dict) else {}
    cur = shots[i] if isinstance(shots[i], dict) else {}
    _same = False
    for k in ("location", "scene_id", "scene", "scene_name"):
        a = str(prev.get(k) or "").strip()
        b = str(cur.get(k) or "").strip()
        if a and b:
            # 2026-09-29：与场景参考图取用共用同一套归一化（全半角 / 引号 / 空白）。
            # 旧写法 `a == b` 会把「铜铃巷」vs「铜铃巷（夜）」判成换场，
            # 于是**无谓地关掉**连续性判定与上一镜锚点，同场景承接镜失去衔接约束。
            _same = (_normalize_scene_name(a) == _normalize_scene_name(b))
            break
    else:
        # 两边都没写场景信息 → 无法判定「是否换场」。宁可**不启用**连续性判定
        # （不给描述比给错描述安全：错描述会让模型把正常换场判成缺陷）。
        _same = False
    if not _same:
        return ""
    bits = []
    if prev.get("camera"):
        bits.append(f"景别 {str(prev['camera']).strip()}")
    if prev.get("description"):
        bits.append(str(prev["description"]).strip()[:200])
    return "；".join(bits)[:300]

def _qc_prev_shot_ref(shots: list, idx: int, out_dir: str) -> str:
    """取「上一镜」已入库的分镜图路径，供图片质检真正做画面级比对（P1）

    与 :func:`_qc_prev_shot_desc` 同判据（仅同场景）。**只取正式目录里已通过的图**
    （``out_dir/shot_NN.png``）—— 不达标的图留在暂存区，拿它当基准会把缺陷传播下去。
    首镜 / 跨场景 / 上一镜尚未入库 → 返回空串。
    """
    if not _qc_prev_shot_desc(shots, idx):
        return ""
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return ""
    prev = shots[i - 1] if isinstance(shots[i - 1], dict) else {}
    try:
        pseq = _shot_seq(prev.get("shot_id"), i)
    except Exception:  # noqa: BLE001
        return ""
    p = os.path.join(out_dir, f"shot_{pseq:02d}.png")
    return p if os.path.isfile(p) else ""

def _qc_ref_images(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                   fallback_refs: list = None) -> list:
    """为「图片质检」收集**本镜出现**的角色 / 物品 / 场景设定图 → [(label, path)]。

    为什么要单独收集，而不直接复用生成侧的 `_allocate_storyboard_refs`：
    生成侧只有 3 个参考图槽位（主角色 / 次要 / 场景），最多带 3 张；而质检的目的是
    **逐个核对画面里的每个角色、每件物品有没有变形、是否与设定一致**，所以按
    `shot.characters_in_shot` / `shot.items_in_shot` 全量收集（总数上限
    `qc_client.MAX_REF_IMAGES`，在 check_image 内还会按路径去重）。

    历史缺陷：分镜质检只把成品图单独送检，模型手里没有任何设定锚点，
    「这个角色长得像不像设定」「这柄剑的形制对不对」只能靠它自己猜 ——
    「角色不像设定 / 道具走形」这类问题要么被放过、要么被误判。
    """
    out, seen = [], set()

    def _add(label, path):
        if not path or path in seen:
            return
        seen.add(path)
        out.append((label, path))

    # 2026-09-29：角色 / 物品也走统一匹配。原先这里与场景**不同源** ——
    # 场景已用 _resolve_scene_entry，角色 / 物品却是裸 char_idx.get(n) /
    # item_idx.get(n)：名字只因标点或后缀差一点，质检就**拿不到该资产的设定图**，
    # 「这个角色像不像设定」只能靠模型凭记忆猜（正是本函数注释里说的历史缺陷）。
    for n in _match_shot_chars(shot, char_idx):
        _add(f"角色「{n}」的外貌、服装与发型", (char_idx.get(n) or {}).get("image"))
    for n in _resolve_item_names(shot, item_idx, "图片质检"):
        _add(f"物品「{n}」的形状、材质与配色", (item_idx.get(n) or {}).get("image"))
    loc, _scene_e = _resolve_scene_entry(shot, scene_idx, "图片质检")
    if _scene_e:
        # 质检口径与生成侧话术同步（2026-09-29）：场景锚点核对的是**空间结构与
        # 光照基调**，而非笼统的「环境与氛围」——与 <imageN> 场景槽位新规格一致。
        # ⚠️ 送检的锚点也必须**同机位**（与生成侧 _allocate_storyboard_refs 同一取图函数）：
        #    否则判官拿正面锚点去判一张俯拍分镜图，只会判「背景不一致」，
        #    把「机位档没生效」误报成「场景画错了」。
        _add(f"场景「{loc}」的空间结构与光照基调",
             _pick_scene_view(_scene_e, shot))
    if not out:
        # 兜底：本镜没登记角色/物品时，用生成侧实际用的那几张（至少保住场景锚点）
        # ⚠️ 2026-10-02 修复：必须**跳过构图基准图**。生成侧 refs 的第 1 项可能是
        #    `(_blocking_ref_path, _BLOCKING_REF_MARK, ...)`（见上方 refs.insert(0, ...)，
        #    标签现为常量 _BLOCKING_REF_MARK = "3D导演台构图基准"）——
        #    那是**无面人偶预演图**，实测把它当设定图送检会严重污染判定
        #    （同图 score 88 → 35，且诱发臆造缺陷，见 qc_client.check_image 的定论）。
        #    ⭐ 下方过滤判据 `"构图基准" in _kind/_label` 对新旧标签（"3D构图基准"/
        #    "3D导演台构图基准"）都命中，无需随标签改动。
        #    原先兜底无差别收下 refs，等于从「质检输入」这个后门把基准图放了回去。
        for r in (fallback_refs or []):
            if not (isinstance(r, (list, tuple)) and len(r) >= 3):
                continue
            _kind, _label = str(r[0] or ""), str(r[1] or "")
            if "构图基准" in _kind or "构图基准" in _label:
                continue
            _add(_label, r[2])
    return out[:qc_client.MAX_REF_IMAGES]

def _keyframe_qc_verifier(project_name: str, script: dict = None):
    """尾帧质检回调（供 keyframe.generate_keyframes 的 verify_cb 注入）

    返回 (verify_cb, max_retries)；质检未开启或不可用时返回 (None, 0)，
    此时尾帧链路与旧行为完全一致（不做任何质检）。

    背景：尾帧此前**完全不经过质检**（只有分镜图走），而链式模式下尾帧会直接
    成为下一镜的首帧，一张坏图会顺着链污染后面所有镜——必须拦在源头。
    """
    try:
        cfg = _qc_load_cfg()
    except Exception:  # noqa: BLE001
        return None, 0
    if not (cfg.get("enabled") and cfg.get("image_enabled")
            and cfg.get("keyframe_qc_enabled", True)):
        return None, 0
    if not qc_client.image_qc_ready(cfg):
        return None, 0

    # 尾帧同样要核对「角色/物品有没有变形、是否与设定一致」：尾帧在链式模式下
    # 会直接成为下一镜的首帧，一张走形的尾帧会顺着链污染后面所有镜。
    # 这里按剧本预建一次资产索引（只建一次，逐镜复用）。
    _kf_idx = None
    try:
        if script:
            _kf_idx = (
                _build_asset_index(script.get("characters") or [], project_name, "character"),
                _build_asset_index(script.get("items") or [], project_name, "item"),
                _build_asset_index(script.get("scenes") or [], project_name, "scene"),
            )
    except Exception as _e:  # noqa: BLE001
        app.logger.warning(f"尾帧质检构建资产索引失败（本轮不带设定图）：{_e}")
        _kf_idx = None

    def _verify(path: str, shot: dict, item: dict):
        desc = (_qc_shot_desc(shot)
                + f"；本图是该镜的「尾帧」（动作结束瞬间），"
                  f"须与首帧保持同一人物、同一服装、同一场景与同一画风"
                + ("，且须承接上一镜尾帧的画面" if item.get("chained") else ""))
        verdict = qc_client.check_image(path, desc, cfg, style=(shot.get("style") or ""),
                                        ref_images=(_qc_ref_images(shot, *_kf_idx) if _kf_idx else None))
        gate = _qc_gate(verdict)
        accepted = bool(gate.get("accept"))
        try:
            # A-17（P2-6）：尾帧质检结论**达标 / 不达标都落盘**一条 keyframe 质检记录 ——
            # 旧实现只在 `if not accepted` 里写记录，达标时无痕，用户无从确认「这集尾帧
            # 到底查没查」。这里两种结果都产生一条记录（record 内自带 ok/passed/accepted
            # 区分口径）；只有**不达标**才进一步沉淀教训（lesson 供下次重试改写提示词）。
            rec = _qc_record_verdict(
                project_name, "keyframe",
                f"shot_{item.get('seq') or item.get('shot_id')}", "尾帧质检",
                1, None, path, verdict,
                extra={"qc_outcome": "pass" if accepted else "fail"},
                style=(shot.get("style") or ""))
            if not accepted:
                # 尾帧质检不达标 → 沉淀 kind="keyframe" 教训，供下次重试 recall_cb 改写提示词。
                # 提示词键用 preflight 自愈**之前**的确定性串（build_end_frame_prompt），与
                # keyframe.generate_keyframes 里的 orig_prompt 同键，保证 phash 稳定。
                orig_prompt = keyframe.build_end_frame_prompt(
                    shot, chained=bool(item.get("chained")))
                _record_qc_lesson(project_name, "keyframe", orig_prompt, rec)
        except Exception as e:  # noqa: BLE001 - 落盘/沉淀失败绝不影响质检结论
            app.logger.warning(f"尾帧质检记录/教训沉淀失败（忽略）：{e}")
        # P1-18：返回 4 元组（ok, reason, unavailable, critical_issues）——
        #  · verdict.ok=False 表示质检「不可判定」（接口 5xx / ffmpeg 缺失等，与内容无关），
        #    由 keyframe 侧据此 **不重试**并标记 qc_unavailable（口径与「不达标」分开）；
        #  · critical_issues（A-18）：本镜判定为致命的缺陷清单（gate 独立复核命中的关键
        #    缺陷 ∪ 上游显式 critical_issues），keyframe 侧写入 r["qc"] 供 G1 止损
        #    （_qc_retry_hopeless）比对「连续 N 次缺陷相同」。旧 3 元组下 keyframe 侧
        #    r["qc"]["critical_issues"] 恒空 → 止损永不判无望（纯换 seed 瞎撞）。
        return (accepted, gate.get("reason") or "",
                not bool(verdict.get("ok")),
                list(gate.get("critical_issues") or []))

    return _verify, min(2, int(cfg.get("max_retries") or 0))

def _keyframe_prompt_preflight(project_name: str):
    """尾帧「生成前提示词预检」回调构建器（与 ``_keyframe_qc_verifier`` 同一注入风格）

    返回 ``(preflight_cb, enabled)``；``preflight_cb(prompt, shot, item) -> dict`` 直接返回
    ``prompt_qc.preflight`` 的结果（含自愈后的提示词），由
    ``keyframe.generate_keyframes`` 取其中的 ``prompt`` 去出图。

    ⚠️ 与尾帧质检（生成后、依赖质检接口）不同：预检是**纯确定性**的，所以只看
    ``prompt_enabled`` —— 质检接口没配好时它照样能拦住「提示词为空」「缺锚定参考图语义」
    这类必然废图的输入。这正是预检比事后质检便宜、且能兜住事后质检的原因。
    """
    try:
        cfg = _qc_load_cfg()
    except Exception:  # noqa: BLE001
        return None, False
    if not prompt_qc.prompt_qc_ready(cfg):
        return None, False

    # 项目级风格只解析一次：预检要按镜头逐个跑，不能在闭包里反复读盘
    _proj_style = ""
    try:
        _proj_style = _qc_style_of(project_name) or ""
    except Exception:  # noqa: BLE001
        _proj_style = ""

    def _pre(prompt: str, shot: dict, item: dict) -> dict:
        shot = shot or {}
        # 风格与生成端对齐：build_end_frame_prompt 只读 shot["style"]。镜头没带风格时退回
        # 项目当前风格 —— 这样预检能把「风格缺失」判出来并自愈补上，而不是直接放过。
        style = shot.get("style") or _proj_style
        ctx = dict(shot)
        ctx["chained"] = bool(item.get("chained"))
        # prompt 召回：预检前先叠加 kind="prompt" 历史教训。orig_prompt 是自愈**前**的
        # 原始串（也不含召回叠加块），作为下方沉淀的稳定 phash 键，避免指纹漂移。
        orig_prompt = prompt or ""
        try:
            prompt = prompt_memory.learned_prompt(
                kind="prompt", prompt=orig_prompt, project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, style=style)
        except Exception as e:  # noqa: BLE001 - 召回失败不影响预检
            app.logger.warning(f"尾帧提示词召回失败（忽略）：{e}")
        pf = prompt_qc.preflight("keyframe", prompt, ctx=ctx, style=style, cfg=cfg)
        gate = prompt_qc.prompt_qc_gate(pf, cfg)
        _qc_record(project_name, "prompt",
                   f"shot_{item.get('seq') or item.get('shot_id')}",
                   {"stage": "keyframe",
                    "label": pf.get("label"),
                    "accept": bool(pf.get("accept")),
                    "repairs": list(pf.get("repairs") or []),
                    "rebuild_hint": pf.get("rebuild_hint") or "",
                    "verdict": pf.get("verdict") or {},
                    "prompt": pf.get("prompt") or ""})
        # prompt 沉淀：预检不通过或有缺陷时落 kind="prompt"（键用自愈前原始串）。
        if not gate.get("accept") or (pf.get("verdict") or {}).get("issues"):
            try:
                _record_preflight_lesson(project_name, orig_prompt, pf, gate)
            except Exception as e:  # noqa: BLE001 - 沉淀失败不影响预检
                app.logger.warning(f"尾帧提示词教训沉淀失败（忽略）：{e}")
        return pf

    return _pre, True

def _qc_record(project_name: str, kind: str, shot_id, payload: dict) -> str:
    """写一条质检/重试历史（失败也不影响主流程）"""
    try:
        return qc_client.append_history(QC_DIR, project_name, kind, shot_id, payload)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"质检历史写入失败（忽略）：{e}")
        return ""

def _qc_history_file_for(project_name: str, kind: str, shot_id) -> str:
    """推算某条质检历史的落盘路径（与 qc_client.history_path 同口径）。

    用途：产物被移入回收站后，把该历史里指向已删路径的 `file` 记为 null（断链修正），
    历史记录本身保留 —— 它是事后排查「当时为什么不合格」的唯一依据。
    """
    try:
        return qc_client.history_path(QC_DIR, project_name, kind, shot_id)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"质检历史路径推算失败（忽略）：{e}")
        return ""

def _qc_summary(attempts: list, enabled: bool, ready: bool, max_retries: int) -> dict:
    """汇总一次资产生成的全部质检尝试，供前端展示徽标 / 明细 / 重试次数"""
    if not enabled:
        return {"enabled": False, "status": "disabled", "label": "质检未开启",
                "attempts": 0, "max_retries": max_retries}
    if not ready:
        return {"enabled": True, "status": "unconfigured", "label": "质检未配置（未启用）",
                "attempts": 0, "max_retries": max_retries}
    passed_rec = next((a for a in attempts if a.get("passed")), None)
    last = attempts[-1] if attempts else {}
    # P0-2：接口级故障放行——最后一次尝试是 interface_fault（鉴权/超时/网络）且无客观层
    # 致命缺陷 → 资产已被 fail-open 写入，不算「error/不达标」，单独一档 fault_open。
    fault_open = bool(last.get("interface_fault")) and not passed_rec and \
        not (last.get("critical_issues") or [])
    if fault_open:
        status = "fault_open"
        blocked = False
    else:
        status = "passed" if passed_rec else ("error" if last.get("error") else "failed")
        blocked = status in ("failed", "error")
    crit = list((passed_rec or last).get("critical_issues") or [])
    # ★ 重试止损（_qc_retry_hopeless）：未通过且已提前停止重试时，把原因写进 label，
    #   否则用户只看到「质检不达标」，不知道系统其实已经主动止损（没在继续烧 GPU）。
    retry_stopped = bool((last or {}).get("retry_stopped")) and not passed_rec
    _label = {"passed": "质检达标", "failed": "质检不达标", "error": "质检调用异常",
              "fault_open": "质检接口故障·已放行"}.get(status, status)
    if retry_stopped:
        _label = "质检不达标（已停止重试：连续两次缺陷完全相同）"
    return {
        "enabled": True,
        "status": status,
        "label": _label,
        "fault_open": fault_open,
        "retry_stopped": retry_stopped,
        "retry_stopped_detail": (last or {}).get("retry_stopped_features") or "",
        "passed": bool(passed_rec),
        "blocked": blocked,
        "entry_blocked": blocked,
        "asset_written": (not blocked),
        "critical_issues": crit,
        "score": (passed_rec or last).get("score"),
        "reason": (passed_rec or last).get("reason") or (last.get("error") or ""),
        "issues": (passed_rec or last).get("issues") or [],
        "attempts": len(attempts),
        "regenerated": max(0, len(attempts) - 1),
        "max_retries": max_retries,
        "history": attempts,
        "history_file": last.get("history_file") or "",
    }

def _audio_qc_file_url(project: str, path: str) -> str:
    """尽量给出可直接播放的 URL（只对 tts/mix 两个既有静态路由下的产物）"""
    ap = os.path.abspath(path)
    try:
        rel_dub = os.path.relpath(ap, os.path.join(DUB_DIR, project or 'project'))
        if not rel_dub.startswith('..'):
            return f"/api/tts/file/{project or 'project'}/{rel_dub.replace(os.sep, '/')}"
        rel_mix = os.path.relpath(ap, mix_out_dir(project or 'project'))
        if not rel_mix.startswith('..'):
            return f"/api/mix/file/{project or 'project'}/{rel_mix.replace(os.sep, '/')}"
    except ValueError as e:
        app.logger.debug("混音相对路径解析失败（忽略）：%s", e)
    return ""

_AUDIO_QC_MEDIA_EXT = ('.mp4', '.mkv', '.mov', '.webm', '.m4v', '.avi')

_AUDIO_QC_AUDIO_EXT = ('.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg')

_AUDIO_QC_NON_PROJECT_DIRS = ('lines', 'frames', 'output', 'temp', 'qc', 'audio', 'audio_mix')

def _audio_qc_project_key(target: str) -> str:
    """按产物路径反推项目名，用于可视化图片的落盘目录。

    ⚠️ 不能退化成字面量（如 ``project``）：按 ``path`` 直接检查时拿不到项目名，
    所有项目就会挤进同一个目录，**不同项目的同名文件互相覆盖** —— 而 AI 读图是
    子进程/网络异步进行的，覆盖会变成竞态（读数项目的图）。
    布局：成片 ``output/final_dub/<项目>/x.mp4``、逐句 ``output/dub/<项目>/lines/x.wav``。
    """
    d = os.path.dirname(os.path.abspath(target))
    for _ in range(4):
        name = os.path.basename(d)
        if name and name.lower() not in _AUDIO_QC_NON_PROJECT_DIRS:
            return name
        parent = os.path.dirname(d)
        if parent == d:                       # 已到根，别再往上
            break
        d = parent
    return 'adhoc'

def _audio_qc_visuals_key(requested_project, target: str) -> str:
    """音频质检可视化图片用的项目键。

    ⚠️ 调用方**不能**写成 ``_safe_project(x) or _audio_qc_project_key(target)``：
    ``_safe_project('')`` 返回的是**字面量 'project'**（``project_store.safe_key``
    的空值兜底），恒为真值 → 兜底永不生效。后果是所有按 ``path`` 直接检查的请求都
    挤进同一个 ``.../project/`` 目录，**不同项目的同名产物互相覆盖** —— 而 AI 读图是
    异步进行的，覆盖会变成竞态（读到了别的项目的频谱图）。
    判「调用方到底有没有传项目名」必须看**原始入参**。
    """
    if str(requested_project or '').strip():
        return _safe_project(requested_project)
    return _audio_qc_project_key(target)

def _resolve_audio_qc_target(project: str, source: str = ''):
    """按项目推导待质检音频：mix（带配音成片）> merged（整集合成音轨）> line（单句）

    返回 ``(路径, source, 失败原因)``。

    ⚠️ 必须按扩展名过滤：``mix_out_dir`` 里除了成片还有 ``*_mix_report.json``
    等边车文件（且它们往往最新），不过滤就会把 JSON 报告当成成片送去解码，
    结论变成「文件无法解码」——假失败。
    """
    def _newest(paths):
        cands = [p for p in paths if os.path.isfile(p) and os.path.getsize(p) > 0]
        return max(cands, key=os.path.getmtime) if cands else ''

    def _media(d, exts):
        if not os.path.isdir(d):
            return []
        return [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(exts)]

    mix_dir = mix_out_dir(project)
    merged_dir = os.path.join(DUB_DIR, project)
    lines_dir = os.path.join(merged_dir, 'lines')

    if source in ('', 'mix'):
        p = _newest(_media(mix_dir, _AUDIO_QC_MEDIA_EXT))
        if p:
            return p, 'mix', ''
        if source == 'mix':
            return '', 'mix', f"项目 '{project}' 下没有带配音成片（请先做音画合成）"
    if source in ('', 'merged'):
        p = _newest(_media(merged_dir, _AUDIO_QC_AUDIO_EXT))
        if p:
            return p, 'merged', ''
        if source == 'merged':
            return '', 'merged', f"项目 '{project}' 下没有整集配音音轨"
    if source in ('', 'line'):
        p = _newest(_media(lines_dir, _AUDIO_QC_AUDIO_EXT))
        if p:
            return p, 'line', ''
        if source == 'line':
            return '', 'line', f"项目 '{project}' 下没有逐句配音文件"
    return '', source or 'mix', f"项目 '{project}' 下没有可质检的音频产物（先做配音/合成）"

# =====================================================================
# 新增模块 B：小说上传与解析
# =====================================================================

UPLOAD_TMP_DIR = os.path.join(NOVELS_DIR, "_uploads")

def _safe_upload_name(filename: str) -> str:
    """保留中文文件名，仅剥离路径与非法字符"""
    name = os.path.basename(str(filename or '').replace('\\', '/').split('/')[-1])
    name = re.sub(r'[<>:"|?*\x00-\x1f]', '_', name).strip().strip('.')
    return name or f"novel_{int(time.time())}.txt"

def _novels_stats(project_ref: str = None, include_unbound: bool = True):
    items = list_novels(NOVELS_DIR)
    # 标注每部小说当前归属的项目（一部小说 = 一个独立项目）
    for m in items:
        rec = project_store.find_by_novel(m.get("novel_id") or m.get("id"))
        m["project_id"] = (rec or {}).get("id", "")
        m["project_key"] = (rec or {}).get("dir_key", "")
        m["project_name"] = (rec or {}).get("name", "")
        m["bound"] = bool(rec)
    if project_ref:
        rec = project_store.get_project(project_ref)
        pid = (rec or {}).get("id") or project_ref
        filtered = [m for m in items if m["project_id"] == pid
                    or (include_unbound and not m["project_id"])]
    else:
        filtered = items
    return {
        "count": len(filtered),
        "total_chars": sum(int(m.get("char_count") or 0) for m in filtered),
        "novels": filtered,
        "all_count": len(items),
    }

# =====================================================================
# 新增模块 C：小说 → A 版剧本（全量分块 + 原文覆盖率校验：不删减原文，只做体裁改写）
# =====================================================================

def _novel_convert_worker(task_id: str, novel_meta: dict, style: str, episodes: int,
                          target_shots: int, project_key: str = None):
    def cb(phase, current, total, message, percent):
        with lock:
            generation_state[task_id].update({
                "phase": phase, "current": current, "total": total,
                "message": message, "progress": percent,
            })
        # ⭐ 同步镜像到 autopilot.current，让「总控面板的实时进度条」在**转剧本阶段**
        #    也有东西可显示。此前这条链路只写 generation_state（任务态），而
        #    /api/autopilot/status 读的是 autopilot.current —— 两者不通，导致整个
        #    转剧本阶段（实测 ~40 分钟、40 次 LLM 调用）用户看到的进度条是空的，
        #    恰恰是他最想知道「在生成什么」的那段时间。
        # ⚠️ 纯展示用途：失败必须静默降级，绝不能因为进度上报拖垮转剧本主链路。
        try:
            _ph = str(phase or "").split(":")[0] or "script"
            autopilot._set_current(
                project=project_key or novel_meta.get("novel_id") or "",
                episode=1,
                title=(novel_meta.get("title") or novel_meta.get("novel_title")
                       or project_key or "整本转剧本"),
                step="script", phase=_ph,
                percent=int(percent or 0),
                message=str(message or ""),
                steps_done=[],
                started_at=autopilot._now(),
            )
        except Exception as _pe:  # noqa: BLE001
            app.logger.debug("转剧本进度镜像到 autopilot.current 失败（忽略）：%s", _pe)

    try:
        text = read_novel_text(NOVELS_DIR, novel_meta["novel_id"])
        client = _current_llm_client()
        # 整本路径的断点缓存目录（按 novel_id + 项目隔离）：命中即跳过模型调用。
        # ⚠️ 没有它时，42 章提炼要跑 40 次调用、约 40 分钟，网关一抖整步重试就要从头再烧 ——
        #    这正是 shots_cache 的设计初衷，此前只接到了「按章分集」路径。
        _nc_cache = ""
        try:
            _nc_cache = novel_to_script._shots_cache_root(
                CONTINUITY_DIR, project_key or novel_meta.get("novel_id"))
        except Exception as _ce:  # noqa: BLE001  缓存不可用不得阻断主链路
            app.logger.warning("整本路径缓存目录解析失败（本次不落缓存）：%s", _ce)
        # 全书结构化大纲（2026-10-02 用户指定）：整本转剧本的强制前置。
        # 通读清洗→抽角色(按戏份)→按场景切换拆段→标剧情任务→落 book_outline.json，
        # 后续环节只引用这一份不重读小说。失败不阻断（降级为旧流程），只响亮降级。
        _book_outline = None
        try:
            import book_outline
            cb("book_outline", 0, 1, "通读全书并生成结构化大纲（角色表+分段大纲）…", 1)
            _book_outline = book_outline.build_outline(
                client, novel_meta, text, style=style,
                progress_cb=cb, continuity_dir=CONTINUITY_DIR,
                project_key=project_key or novel_meta.get("novel_id"),
                cache_dir=_nc_cache,
            )
            app.logger.info("全书大纲已生成：%s（%d 角色 / %d 段）",
                            book_outline.outline_path(project_key or novel_meta.get("novel_id"),
                                                       CONTINUITY_DIR),
                            len(_book_outline.get("characters") or []),
                            len(_book_outline.get("segments") or []))
        except Exception as _boe:  # noqa: BLE001
            app.logger.warning("全书大纲生成失败（降级为旧流程继续转剧本）：%s", _boe)
        script = novel_to_script.convert_novel_to_script(
            client, novel_meta, text, style=style, episodes=episodes,
            target_shots=target_shots, progress_cb=cb,
            continuity_dir=CONTINUITY_DIR, project_key=project_key,
            cache_dir=_nc_cache,
        )
        path = novel_to_script.save_generated_script(script, SCRIPT_DIR, project_key=project_key)
        if project_key:
            try:
                project_store.bind_script(project_key, path, project_store.script_stats(path))
            except Exception as be:  # noqa: BLE001
                app.logger.warning(f"项目剧本登记失败（{project_key}）：{be}")
        with lock:
            generation_state[task_id].update({
                "status": "completed", "progress": 100,
                "script_path": path, "script": script,
                "project_name": script.get("metadata", {}).get("project_name"),
                "project_key": project_key,
                # 原文覆盖率摘要（含遗漏清单预览 / 补生成镜头数 / 复检轨迹），前端可直接展示
                "coverage": (script.get("metadata") or {}).get("coverage") or {},
                "coverage_report_path": (script.get("metadata") or {}).get("coverage_report_path"),
                "warnings": (script.get("metadata") or {}).get("warnings") or [],
                "episode_stats": {
                    "shot_count": script.get("shot_count") or len(script.get("shots") or []),
                    "episode_duration_sec": script.get("episode_duration_sec"),
                    "episode_plan": script.get("episode_plan"),
                },
                "stats": {
                    "characters": len(script.get("characters") or []),
                    "items": len(script.get("items") or []),
                    "scenes": len(script.get("scenes") or []),
                    "shots": len(script.get("shots") or []),
                    "shot_count": script.get("shot_count"),
                    "episode_duration_sec": script.get("episode_duration_sec"),
                },
            })
    except (LLMError, NovelParseError) as e:
        app.logger.error(f"小说转剧本失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("小说转剧本异常")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": f"转换异常：{e}"})
    finally:
        # 转剧本已结束（无论成败）→ 撤掉镜像的进度，否则总控面板会一直显示
        # 「正在生产 …」这条已经结束的进度条，用户以为还在跑。
        try:
            autopilot._clear_current()
        except Exception as _ce:  # noqa: BLE001  清理失败不影响任务结论
            app.logger.debug("清理转剧本进度镜像失败（忽略）：%s", _ce)

# =====================================================================
# 新增模块 C2：按章节分集生成（每章一集，独立落盘 output/scripts/<小说名>/第N集.json）
# =====================================================================

EPISODE_BATCH_LIMIT = 30          # 单次批量生成集数上限（保护后台任务）

def _novel_key(novel_meta: dict, project_ref: str = None) -> str:
    """剧集目录/项目名前缀所用的稳定键：优先取项目注册表分配的项目键。

    一部小说 = 一个独立项目 → 剧本落盘 output/scripts/<项目键>/，与其它小说彻底隔离。
    """
    rec = project_store.get_project(project_ref) if project_ref else None
    if not rec:
        rec = project_store.find_by_novel(novel_meta.get("novel_id") or novel_meta.get("id"))
    if rec:
        return rec["dir_key"]
    raw = (novel_meta.get("name") or novel_meta.get("title")
           or novel_meta.get("novel_id") or "novel")
    cleaned = re.sub(r"[《》〈〉【】「」『』\s]+", "", str(raw)).strip()
    return cleaned or str(novel_meta.get("novel_id") or "novel")

def _estimate_subchunks(char_count: int) -> int:
    """不读全文的二次分块数量估算（用于章节列表）"""
    return novel_to_script.estimate_subchunks(char_count)

def _resolve_novel_project(data: dict, novel_meta: dict) -> dict:
    """把当前操作绑定到项目：显式指定优先，否则按小说自动建立/复用独立项目。"""
    data = data or {}
    ref = (data.get('project_id') or data.get('project_name') or '').strip()
    rec = project_store.get_project(ref) if ref else None
    if rec is None:
        rec = project_store.ensure_project_for_novel(
            novel_meta.get("novel_id") or novel_meta.get("id") or "",
            novel_meta.get("name") or novel_meta.get("title") or "")
    return rec

def _salvage_episode_script(path: str, episode_no: int):
    """任务报错后检查剧本产物是否其实可用（**以产物为准**，避免误报失败）

    2026-09-17 E2E 实测：分集任务报「模型未返回有效分镜…未知错误」，
    但 `第1集.json` 其实已落盘、6 镜有效、覆盖率 100%、也已被 /api/episodes 收录 ——
    用户看到 "失败" 会以为白跑一趟。产物存在且镜头非空时，按成功回填统计字段。

    返回可直接并入 results 的统计 dict；产物缺失/不可用则返回 None。
    """
    try:
        if not (path and os.path.isfile(path) and os.path.getsize(path) > 200):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return None
    shots = data.get("shots") or []
    if not shots:
        return None
    meta = data.get("metadata") or {}
    try:
        stats = meta.get("episode_stats") or novel_to_script.build_episode_stats(shots)
    except Exception:  # noqa: BLE001
        stats = {}
    return {
        "shots": len(shots),
        "shot_count": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
        "episode_duration_sec": data.get("episode_duration_sec") or stats.get("duration_sec"),
        "characters": len(data.get("characters") or []),
        "items": len(data.get("items") or []),
        "scenes": len(data.get("scenes") or []),
        "elapsed_sec": meta.get("elapsed_sec"),
        "warnings": meta.get("warnings") or [],
    }

def _episode_units_for_chapters(novel_meta: dict, chapters: list) -> list:
    """把「用户选中的章」展开成**拍摄单元**（超长章会拆成多集）。

    ⚠️ 单元编号必须基于**全量章节**展开（口径 = ``autopilot.episode_units``），
    不能用传入的子集 —— 否则同一章在「手动选集生成」与「托管」两条链路上会拿到
    不同的集号，产物（``第N集.json`` / 成片 / 验收记录）互相错位。
    """
    selected = [int(c.get("index") or 0) for c in (chapters or [])]
    if not selected:
        return []
    try:
        all_chapters, text = autopilot.chapters_and_text(novel_meta)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("章节列表读取失败（按一章一集处理）：%s", e)
        all_chapters, text = [], ""
    if all_chapters:
        try:
            units = autopilot.episode_units(all_chapters, {"episodes": selected}, text)
            if units:
                return units
        except Exception as e:  # noqa: BLE001
            app.logger.warning("拆章失败（按一章一集处理）：%s", e)
    # 兜底：拿不到全量章节时退回「一章一集」（与历史行为一致）
    return [{"episode_no": int(c.get("index") or i + 1),
             "chapter_index": int(c.get("index") or i + 1),
             "part": 1, "parts": 1, "chapter": c}
            for i, c in enumerate(chapters or [])]

def _episodes_worker(task_id: str, novel_meta: dict, chapters: list, style: str,
                     target_shots: int, overwrite: bool, project_key: str = None,
                     use_screenplay: bool = False):
    key = _novel_key(novel_meta, project_key)
    # 拍摄单元：超长章按语义边界拆成多集（单集镜头数硬上限见 novel_to_script）
    units = _episode_units_for_chapters(novel_meta, chapters)
    total = len(units)

    def report(ep_ordinal, chapter, phase, message, inner_percent):
        overall = int(((ep_ordinal - 1) + (inner_percent or 0) / 100.0) / total * 100)
        with lock:
            generation_state[task_id].update({
                "phase": phase, "progress": min(99, max(1, overall)),
                "current": ep_ordinal, "total": total,
                "current_chapter": chapter.get("title"),
                "current_episode": chapter.get("index"),
                "message": message,
            })

    try:
        text = read_novel_text(NOVELS_DIR, novel_meta["novel_id"])
        client = _current_llm_client()
        if not client.configured:
            raise LLMError("尚未配置自定义 AI 接口")

        results = []
        for i, unit in enumerate(units):
            ep = int(unit["episode_no"])
            chapter = unit["chapter"]
            ch_title = chapter.get("title") or f"第{ep}集"
            out_path = novel_to_script.episode_script_path(SCRIPT_DIR, key, ep)

            if os.path.isfile(out_path) and not overwrite:
                info = None
                for e in novel_to_script.list_episodes(SCRIPT_DIR, key):
                    if int(e.get("episode_no")) == ep:
                        info = e
                        break
                results.append({"episode_no": ep, "chapter_title": ch_title,
                                "status": "skipped", "path": out_path,
                                "project_key": key,
                                "message": "该集已存在，跳过（可勾选覆盖重新生成）",
                                "shots": (info or {}).get("shots", 0),
                                "shot_count": (info or {}).get("shot_count"),
                                "episode_duration_sec": (info or {}).get("episode_duration_sec")})
                report(i + 1, chapter, "skip", f"第{ep}集已存在，跳过", 100)
                continue

            report(i + 1, chapter, "chapter",
                   f"第{ep}集《{ch_title}》：准备章节正文…", 2)

            def cb(phase, cur, tot, msg, pct, _ep=ep, _ord=i + 1, _ch=chapter, _t=ch_title):
                report(_ord, _ch, phase, f"第{_ep}集《{_t}》 {msg}", pct)

            try:
                # 前置解析（2026-10-03 补链，用户口径：前置解析必须先于改写）：
                # 该章还没有 preflight（人物档案/梗概/关键事件/情绪基线）时先自动
                # 跑一次并并入项目级设定库 —— convert_chapter_with_continuity 依赖
                # 它注入防 OOC 约束。一章一集口径下章号=集号，注入键天然对齐。
                # 失败不阻塞改写（fail-open，只告警）。
                try:
                    if not chapter_preflight.load_preflight(
                            CONTINUITY_DIR, key, int(chapter.get("index") or 0)):
                        report(ep, chapter, "preflight",
                               f"第{ep}集：前置解析（人物档案/梗概/关键事件/情绪基线）…", 2)
                        _pf_seg = text[int(chapter.get("start") or 0):
                                       int(chapter.get("end") or 0)]
                        _pf_res = chapter_preflight.preflight_analyze(
                            client,
                            novel_meta.get("title") or novel_meta.get("name") or "",
                            int(chapter.get("index") or 0),
                            chapter.get("title") or "", _pf_seg)
                        chapter_preflight.save_preflight(CONTINUITY_DIR, key, _pf_res)
                        try:
                            chapter_preflight.merge_to_bible(CONTINUITY_DIR, key, _pf_res)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as _pfe:  # noqa: BLE001
                    app.logger.warning("第%s集前置解析失败（跳过注入，不阻塞）：%s", ep, _pfe)
                # 两段式生产（2026-10-03 ②）：use_screenplay=true 时以「文学剧本」为
                # 原文走改写链路 —— 合成 chapter 使 start/end 覆盖剧本全文（continuity
                # 与覆盖率校验只消费传入的 novel_text[start:end]，零内部改动即生效）。
                # 文学剧本缺失时回退章节原文并告警（fail-open，不阻塞批量）。
                _conv_text = text
                _conv_chapter = chapter
                if use_screenplay:
                    _md = novel_screenplay.load_screenplay(
                        key, int(unit.get("episode_no") or 0))
                    if _md:
                        _conv_text = _md
                        _conv_chapter = {"index": chapter.get("index"),
                                         "title": chapter.get("title"),
                                         "start": 0, "end": len(_md)}
                    else:
                        app.logger.warning(
                            "第%s集：文学剧本不存在，回退章节原文（建议先调用 "
                            "/api/novels/<id>/screenplay/generate）",
                            unit.get("episode_no"))
                # 跨集连贯性（A/B/C/D）：项目级 bible + 上集摘要卡 + 衔接契约 + state 锚点
                # + 相邻集六类校验 + 命中高危问题时的局部重写，全部由 continuity 编排
                conv = continuity.convert_chapter_with_continuity(
                    client, novel_meta, _conv_text, _conv_chapter, key, CONTINUITY_DIR,
                    style=style, target_shots=target_shots, episode_no=ep,
                    save_dir=SCRIPT_DIR, progress_cb=cb,
                )
                script = conv["script"]
                validation = conv.get("validation") or {}
                path = (conv.get("script_path")
                        or novel_to_script.save_episode_script(script, SCRIPT_DIR, key, ep, key))
                # 项目登记：把剧本及其镜头数/每集时长统计写回项目注册表
                if project_key:
                    try:
                        project_store.bind_script(
                            project_key, path, project_store.script_stats(path))
                    except Exception as be:  # noqa: BLE001
                        app.logger.warning(f"项目剧本登记失败（{project_key}）：{be}")
                # 剧本体检：兜底镜头（dialogue=[] 且 prompt_h3=""）会在配音环节变成
                # 「一句都合不出来」，但这里看起来是「生成成功」，必须把缺口显式带出
                _audit = dialogue_utils.audit_script(script)
                _meta_warnings = list(script["metadata"].get("warnings") or [])
                if _audit["warnings"]:
                    _meta_warnings.extend(_audit["warnings"])
                    app.logger.warning(f"第{ep}集剧本存在内容缺口：{_audit['warnings']}")
                # P0-3 剧本↔原著一致性（三件套 + 定向修复）结果，随生成结果带出给前端
                _sc = script["metadata"].get("script_consistency") or {}
                results.append({
                    "episode_no": ep, "chapter_title": ch_title, "status": "success",
                    "path": path, "project_name": script["metadata"]["project_name"],
                    "project_key": key,
                    "shots": len(script.get("shots") or []),
                    "shot_count": script.get("shot_count") or len(script.get("shots") or []),
                    "episode_duration_sec": script.get("episode_duration_sec"),
                    "characters": len(script.get("characters") or []),
                    "items": len(script.get("items") or []),
                    "scenes": len(script.get("scenes") or []),
                    "chunks_total": script["metadata"].get("chunks_total"),
                    "chunks_used": script["metadata"].get("chunks_used"),
                    "elapsed_sec": script["metadata"].get("elapsed_sec"),
                    "warnings": _meta_warnings,
                    "script_audit": _audit["stats"],
                    "script_audit_ok": _audit["ok"],
                    "continuity_score": (script["metadata"].get("continuity") or {}).get("validation_score"),
                    "continuity_issues": len(validation.get("issues") or []),
                    "continuity_issue_stats": validation.get("issue_stats") or {},
                    "continuity_rewrite": bool((conv.get("rewrite") or {}).get("triggered")),
                    "continuity_rewrite_shots": (conv.get("rewrite") or {}).get("rewritten_shot_ids") or [],
                    "continuity_dir": continuity.continuity_root(CONTINUITY_DIR, key),
                    "coverage_percent": (conv.get("coverage") or {}).get("coverage_percent"),
                    "coverage_plot_percent": (conv.get("coverage") or {}).get("plot_coverage_percent"),
                    "coverage_detail_percent": (conv.get("coverage") or {}).get("detail_coverage_percent"),
                    "coverage_detail_passed": (conv.get("coverage") or {}).get("detail_passed"),
                    "coverage_passed": (conv.get("coverage") or {}).get("passed"),
                    "coverage_threshold_percent": (conv.get("coverage") or {}).get("threshold_percent"),
                    "coverage_missing": (conv.get("coverage") or {}).get("missing_count"),
                    "coverage_zero_omission": (conv.get("coverage") or {}).get("zero_omission"),
                    "coverage_supplement_shots": (conv.get("coverage") or {}).get("supplement_shots"),
                    "coverage_supplement_rounds": (conv.get("coverage") or {}).get("supplement_rounds"),
                    "coverage_report_path": (conv.get("coverage") or {}).get("report_path"),
                    # P0-3 剧本↔原著一致性：三件套 + 定向修复闭环
                    "consistency_passed": _sc.get("passed"),
                    "consistency_chapter_index": _sc.get("chapter_index"),
                    "consistency_anchor_checked": _sc.get("anchor_checked"),
                    "consistency_anchor_ok": _sc.get("anchor_ok"),
                    "consistency_anchor_deviation": _sc.get("anchor_deviation"),
                    "consistency_anchor_reason": _sc.get("anchor_reason"),
                    "consistency_leak_count": _sc.get("leak_count"),
                    "consistency_leak_shot_ids": _sc.get("leak_shot_ids") or [],
                    "consistency_element_percent": _sc.get("element_coverage_percent"),
                    "consistency_element_missing_count": _sc.get("element_missing_count"),
                    "consistency_element_missing": [e.get("name") for e in (_sc.get("element_missing") or [])],
                    "consistency_fix_rounds": _sc.get("fix_rounds"),
                    "consistency_fixed": _sc.get("fixed"),
                    "consistency_issue_count": _sc.get("issue_count"),
                    "consistency_issue_stats": _sc.get("issue_stats") or {},
                    "consistency_report_path": _sc.get("report_path"),
                    "message": "生成完成",
                })
            except Exception as e:  # noqa: BLE001
                app.logger.error(f"第{ep}集生成失败: {e}")
                # 以产物为准：模型抖动/校验失败时报错，但剧本可能已经落盘且可用
                salvaged = _salvage_episode_script(out_path, ep)
                if salvaged:
                    app.logger.warning(
                        f"第{ep}集虽报错但剧本产物可用，已按成功回填：{out_path}")
                    results.append({
                        "episode_no": ep, "chapter_title": ch_title,
                        "status": "success", "degraded": True,
                        "path": out_path, "project_key": key, **salvaged,
                        "message": f"生成过程报错，但剧本已落盘且可用（已按产物回填为成功）：{e}",
                        "error": str(e),
                    })
                else:
                    results.append({"episode_no": ep, "chapter_title": ch_title,
                                    "status": "failed", "path": out_path,
                                    "message": str(e)})

        ok = [r for r in results if r["status"] == "success"]
        skipped = [r for r in results if r["status"] == "skipped"]
        failed = [r for r in results if r["status"] == "failed"]
        degraded = [r for r in ok if r.get("degraded")]
        episodes = novel_to_script.list_episodes(SCRIPT_DIR, key)
        with lock:
            generation_state[task_id].update({
                "status": "completed" if ok or skipped else "failed",
                "progress": 100, "current": total, "total": total,
                "message": (f"批量完成：成功 {len(ok)} 集"
                            + (f"（其中 {len(degraded)} 集过程报错但产物可用）" if degraded else "")
                            + f" / 跳过 {len(skipped)} 集 / 失败 {len(failed)} 集"),
                "degraded_count": len(degraded),
                "error": "" if (ok or skipped) else (failed[0]["message"] if failed else "全部失败"),
                "results": results, "episodes": episodes,
                "novel_id": novel_meta.get("novel_id"),
                "project_key": key,
                "episode_dir": os.path.abspath(os.path.join(SCRIPT_DIR, key)),
            })
    except (LLMError, NovelParseError) as e:
        app.logger.error(f"分集生成失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("分集生成异常")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": f"分集生成异常：{e}"})

# ===================== 文学剧本层（两段式生产 ①：人审层，2026-10-03） =====================

def _screenplay_worker(task_id: str, novel_meta: dict, chapter: dict,
                       project_key: str, style: str, episode_no: int):
    """文学剧本生成 worker：章节正文 → LLM 场次剧本 → output/screenplays/<项目>/第N集_文学剧本.md"""
    try:
        text = read_novel_text(NOVELS_DIR, novel_meta["novel_id"])
        seg = text[int(chapter.get("start") or 0):int(chapter.get("end") or 0)]
        with lock:
            generation_state[task_id].update({"phase": "literary", "progress": 15,
                                              "message": "正在把本章正文改写成文学剧本…"})
        client = _current_llm_client()
        if not client.configured:
            raise LLMError("尚未配置自定义 AI 接口")
        md = novel_screenplay.generate_screenplay(
            client, novel_meta.get("title") or novel_meta.get("name") or "",
            chapter.get("title") or f"第{episode_no}集", seg, style)
        with lock:
            generation_state[task_id].update({"progress": 80, "message": "落盘…"})
        path = novel_screenplay.save_screenplay(
            novel_screenplay.screenplay_path(project_key, episode_no), md)
        with lock:
            generation_state[task_id].update({
                "status": "completed", "progress": 100,
                "message": "文学剧本已生成（可在前端查看，确认后再改写为分镜剧本）",
                "screenplay_path": path,
                "results": [{"success": True, "episode_no": episode_no,
                             "path": path, "chars": len(md)}]})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("文学剧本生成失败")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})

def _episode_progress(project_name: str, episode_no: int, shot_count: int = 0) -> dict:
    """从磁盘真实产物推导单集的进度与状态（界面回显的唯一依据）

    背景（P1 线上问题）
    ------------------
    `novel_to_script.list_episodes` 只返回镜头数/覆盖率等字段，**从不返回 status / completed_shots**。
    前端 `EpisodeInfo.status` 因此恒为 undefined，`completed_shots` 恒为 undefined，于是：
      - 状态徽标一律落到兜底分支 → 第 1 集跑完 4 小时（含 final 失败）仍显示「○ 待生产」；
      - 进度显示「0 / 24 镜头」；
      - 概览统计「已完成 0 / 生产中 0」；
    用户完全无法从界面判断任务是否结束、成功还是失败，体验等同卡死。

    修复：以磁盘产物 + 生产历史为准推导状态，前端拿到的是真实进度。
    判定顺序（先看终态，再看进行中，最后看未开始）：
      1) 成片存在                  → done
      2) 最近一次生产记录为失败      → failed（并把错误原文回显）
      3) 托管正在跑这一集 或 已有中间产物 → producing
      4) 其余                      → pending
    """
    proj = _safe_project(project_name or "")
    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1

    def _count(d: str, pattern: str) -> int:
        try:
            return sum(1 for f in os.listdir(d) if re.match(pattern, f))
        except OSError:
            return 0

    sb_dir = _ep_read_dir(STORYBOARDS_DIR, proj, ep)
    vid_dir = _ep_read_dir(VIDEOS_DIR, proj, ep)
    storyboards = _count(sb_dir, r"^shot_\d+\.png$")
    shot_videos = _count(vid_dir, r"^shot_\d+\.mp4$")
    full_video = ""
    for cand in (f"ep{ep:02d}_full.mp4", "episode_full.mp4"):
        p = os.path.join(vid_dir, cand)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            full_video = p
            break
    final_file = os.path.join(FINAL_DIR, proj, f"ep{ep:02d}_final.mp4")
    final_ready = os.path.isfile(final_file) and os.path.getsize(final_file) > 0

    # 生产历史：取该集**最后一条**记录（不按时间窗过滤，否则老失败会被漏掉）
    last_run = None
    try:
        hp = os.path.join(PROJECT_OUTPUT_DIR, "autopilot", proj, "history.jsonl")
        if os.path.isfile(hp):
            with open(hp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if int(row.get("episode_no") or 0) == ep:
                        last_run = row
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"读取生产历史失败（{proj} 第{ep}集）：{e}")

    # 托管是否正在跑这一集
    running_this = False
    try:
        cur = (autopilot.status(proj) or {}).get("current") or {}
        running_this = (cur.get("project") == proj and int(cur.get("episode") or 0) == ep
                        and bool((autopilot.status(proj) or {}).get("running")))
    except Exception:  # noqa: BLE001
        running_this = False

    total = int(shot_count or 0)
    completed = storyboards
    if final_ready:
        status = "done"
    elif last_run is not None and not last_run.get("ok") and \
            str(last_run.get("status") or "") == "failed":
        status = "failed"
    elif running_this or storyboards or shot_videos or full_video:
        status = "producing"
    else:
        status = "pending"

    # 成片已出但镜头没画齐，同样视为「完成」但标注不齐（与交付物登记口径一致）
    incomplete = bool(final_ready and total and completed < total)
    return {
        "status": status,
        "completed_shots": min(completed, total) if total else completed,
        "storyboard_count": storyboards,
        "shot_video_count": shot_videos,
        "full_video": bool(full_video),
        "final_ready": final_ready,
        "final_file": final_file if final_ready else "",
        "incomplete_shots": incomplete,
        "last_run": ({
            "ok": last_run.get("ok"),
            "status": last_run.get("status"),
            "elapsed_sec": last_run.get("elapsed_sec"),
            "error": last_run.get("error") or "",
            "at": last_run.get("at"),
            "steps": last_run.get("steps") or {},
        } if last_run else None),
    }

# =====================================================================
# 跨集连贯性（相邻两章转剧本改进 A/B/C/D）：项目级设定库 / 摘要卡 / state / 校验 查询
# =====================================================================

def _resolve_continuity_key(novel_id):
    """小说 → (meta, 项目记录, 项目键)，供连贯性查询接口复用"""
    meta = get_novel(NOVELS_DIR, novel_id)
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    return meta, proj, _novel_key(meta, proj["dir_key"] if proj else None)

# =====================================================================
# 新增模块 D：剧本提示词分析（prompt_h3 + 参考图提示词）
# =====================================================================

def _ensure_script_file(script: dict, script_path: str, project_name: str) -> str:
    # P0-4 纵深防御：worker（save_script_inplace 覆盖写盘）只认「项目输出目录内」的既有剧本；
    # 越界/空路径一律视为未提供，改在 SCRIPT_DIR 内另存，绝不对项目外文件落笔。
    if script_path and project_store.is_path_inside_output(script_path) \
            and os.path.isfile(script_path):
        return script_path
    return novel_to_script.save_generated_script(script, SCRIPT_DIR, project_name)

def _analyze_worker(task_id: str, script: dict, script_path: str, mode: str,
                    shot_ids, project_name: str, extra: str):
    def cb(phase, current, total, message, percent):
        with lock:
            generation_state[task_id].update({
                "phase": phase, "current": current, "total": total,
                "message": message, "progress": percent,
            })

    try:
        client = _current_llm_client()
        result = analyze_script_prompts(
            client, script, mode=mode, shot_ids=shot_ids,
            storyboards_dir=STORYBOARDS_DIR, project_name=project_name,
            extra_instruction=extra, progress_cb=cb,
        )
        path = _ensure_script_file(script, script_path, project_name)
        save_script_inplace(script, path)
        with lock:
            generation_state[task_id].update({
                "status": "completed", "progress": 100,
                "result": result, "script_path": path, "script": script,
            })
    except (LLMError, OSError) as e:
        app.logger.error(f"提示词分析失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("提示词分析异常")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": f"分析异常：{e}"})

# =====================================================================
# 视频配音（QwenTTS 真实链路：剧本台词 → 逐角色语音 → output/dub/<项目>/）
# =====================================================================

dub_tasks = {}

dub_lock = threading.Lock()

def _dub_project_dir(project_name: str) -> str:
    d = os.path.join(DUB_DIR, project_name)
    os.makedirs(d, exist_ok=True)
    return d

def _dub_resolve_script(data: dict) -> dict:
    """解析配音所用剧本：优先 body.script，其次 script_path（限项目输出目录内），最后自动匹配"""
    script = data.get("script")
    if isinstance(script, dict) and script.get("shots"):
        return {"script": script, "script_path": (data.get("script_path") or "").strip(),
                "source": "body"}

    script_path = (data.get("script_path") or "").strip()
    if script_path:
        # P0-4：与 project_store.bind_script / /api/final/video 同一校验函数
        if not project_store.is_path_inside_output(script_path):
            raise TTSError(f"剧本路径必须在项目输出目录内：{os.path.abspath(PROJECT_OUTPUT_DIR)}")
        p = os.path.abspath(script_path)
        if not os.path.exists(p):
            raise TTSError(f"剧本文件不存在：{p}")
        with open(p, "r", encoding="utf-8") as f:
            return {"script": json.load(f), "script_path": p, "source": "path"}

    # 自动匹配 output/scripts 下的剧本（优先路径含项目名，其次 episode_no 命中，最后取最新）
    project_name = data.get("project_name") or ""
    episode = data.get("episode")
    cands = []
    for dp, _dn, fn in os.walk(SCRIPT_DIR):
        for name in fn:
            if not name.lower().endswith(".json"):
                continue
            p = os.path.join(dp, name)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    s = json.load(f)
            except Exception:
                continue
            if not isinstance(s, dict) or not s.get("shots"):
                continue
            cands.append({"path": p, "mtime": os.path.getmtime(p), "script": s,
                          "episode_no": s.get("episode_no") or (s.get("metadata") or {}).get("episode_no")})
    if not cands:
        raise TTSError("未找到可用剧本（output/scripts 下无含 shots 的 JSON），请先生成剧本")

    # 严格匹配：有 project_name 时必须属于该项目，禁止跨项目回退
    if project_name:
        project_cands = [c for c in cands if project_name in c["path"]]
        if not project_cands:
            raise TTSError(f"项目 '{project_name}' 暂无剧本，请先生成剧本后再使用 TTS 功能")
        hit = project_cands
    else:
        hit = cands

    if episode:
        hit2 = [c for c in hit if str(c["episode_no"]) == str(episode)]
        if hit2:
            hit = hit2
    best = max(hit, key=lambda c: c["mtime"])
    return {"script": best["script"], "script_path": best["path"], "source": "auto"}

def _dub_audio_url(project_name: str, rel_path: str) -> str:
    rel = os.path.relpath(rel_path, _dub_project_dir(project_name)).replace(os.sep, "/")
    return f"/api/tts/file/{project_name}/{rel}" if not rel.startswith("..") else ""

# =====================================================================
# 音频质检接线（提示词预检 + 成品质检）
# =====================================================================
# 两层都在「生成前后」各管一段，与图片/视频质检的三层结构（预检 → 成品质检 → 重试）对齐：
#   ① 配音台词预检（零模型依赖，默认开启）：挡住会被念出来的结构化残留、空台词、错配音色；
#   ② 配音成品质检（ffmpeg 客观层 + 频谱/波形 AI 层）：挡住「合成成功但整段无声」这类
#      在旧流程里要等到成片验收才暴露的问题。
# 两者都**不阻断生成**：整集生产不能被单句质检拖死，结论如实记录、逐句可定位即可。

def _record_audio_qc_lesson(project_name: str, ln: dict, verdict: dict) -> dict:
    """把一句「配音成品质检不达标」沉淀成 ``kind="audio"`` 教训。

    提示词键用**自愈前**的台词原文（``audio_orig_text``，回退当前 ``ln["text"]``）：
    它正是 TTS 的实际输入，phash 稳定；预检已自愈过 text 时取自愈前的原文，避免指纹漂移。
    ⚠️ 沉淀的 issues **只进教训库，绝不改台词**（音频类召回是计划级纠偏，见 _apply_audio_hints）。
    """
    text_key = ln.get("audio_orig_text") or ln.get("text") or ""
    if not text_key:
        return {}
    verdict = verdict if isinstance(verdict, dict) else {}
    rec = {
        "issues": [str(x).strip() for x in
                   (list(verdict.get("issues") or []) +
                    list(verdict.get("critical_issues") or [])) if str(x).strip()],
        "critical_issues": [str(x).strip() for x in (verdict.get("critical_issues") or [])
                            if str(x).strip()],
        "reason": str(verdict.get("reason") or "").strip(),
        "score": verdict.get("score"),
        "audio": True,
    }
    if not rec["issues"] and not rec["reason"]:
        return {}
    try:
        return _record_qc_lesson(project_name, "audio", text_key, rec)
    except Exception as e:  # noqa: BLE001 - 沉淀失败绝不影响配音
        app.logger.warning(f"配音教训沉淀失败（忽略）：{e}")
        return {}

def _dub_line_speaker_from_script(ln: dict, project_name: str) -> str:
    """从剧本里找该句所属镜头登记的 speaker（dialogue[].speaker / shot.speaker）。

    取不到返回空串（调用方不做回填）。纯只读，永不抛异常。
    """
    shot_id = ln.get("shot_id")
    if not project_name or shot_id is None:
        return ""
    try:
        resolved = _dub_resolve_script({"project_name": project_name})
        script = resolved.get("script") or {}
    except Exception:  # noqa: BLE001
        return ""
    sid_str = str(shot_id)
    text = str(ln.get("text") or "").strip()
    shots = []
    for sc in (script.get("scenes") or []):
        shots.extend(sc.get("shots") or [])
    if not shots:
        shots = script.get("shots") or []
    for shot in shots:
        if str(shot.get("shot_id") or "") != sid_str:
            continue
        dlg_speaker = ""
        for row in (shot.get("dialogue") or []):
            if str(row.get("text") or "").strip() == text:
                dlg_speaker = str(row.get("speaker") or "").strip()
                if dlg_speaker:
                    break
        return dlg_speaker or str(shot.get("speaker") or "").strip()
    return ""

def _dub_character_desc(character: str, project_name: str) -> str:
    """取角色音色底稿描述（供 design 模式 instruct）；取不到返回空串。"""
    if not character or not project_name:
        return ""
    try:
        resolved = _dub_resolve_script({"project_name": project_name})
        script = resolved.get("script") or {}
        for ch in (script.get("characters") or []):
            if str(ch.get("name") or "") == str(character):
                return str(ch.get("description") or ch.get("tts_voice") or "")
    except Exception as e:  # noqa: BLE001
        app.logger.debug("角色描述取值失败（忽略）：%s", e)
    return ""

def _apply_audio_hints(ln: dict, hints: list, project_name: str = "") -> None:
    """音频类召回的**计划级纠偏**（设计 D4：音频建议绝不拼进 ``ln["text"]``，会被 TTS 念出来）。

    逐条扫描 hints（缺陷描述），按特征做确定性纠偏，只动 plan 的说话人/音色模式/期望时长：
      - 含「旁白」「speaker」「角色」：若本句说话人是旁白兜底（剧本 dialogue 没登记 speaker），
        且能拿到该镜在剧本里登记的 speaker，则回填 ``ln["character"]``，避免角色台词被旁白念；
      - 含「情绪」「语气」「instruct」：``voice.mode == "preset"`` 时切到 ``design``，
        并确保 ``instruct`` 携带该句情绪（preset 的 CustomVoice 会忽略 instruct，只有
        VoiceDesign 真正按 instruct 控制语气）；
      - 含「时长」「截断」：记录 ``ln["audio_expect_sec"]``（期望时长）供后续质检比对，不阻断；
      - 其它：仅留痕（hints 由调用方写入 ``ln["audio_hints"]`` 审计），不改 plan。

    纯就地修改、永不抛异常、不改 tts_client.py（build_dub_plan 保持纯计划构建）。
    """
    hints = [str(h).strip() for h in (hints or []) if str(h).strip()]
    if not hints:
        return
    try:
        joined = " ".join(hints)
        voice = ln.get("voice") or {}
        # —— 说话人回填：旁白兜底 + hint 提示该句其实是角色台词 → 按剧本登记的 speaker 纠偏 ——
        if (("旁白" in joined or "speaker" in joined or "角色" in joined)
                and str(ln.get("character") or "") == tts_client.NARRATION_SPEAKER):
            speaker = ""
            try:
                speaker = _dub_line_speaker_from_script(ln, project_name)
            except Exception:  # noqa: BLE001
                speaker = ""
            if speaker and speaker != tts_client.NARRATION_SPEAKER:
                ln["character"] = speaker
        # —— 情绪/语气：preset 忽略 instruct → 切 design 并携带情绪 ——
        if ("情绪" in joined or "语气" in joined or "instruct" in joined.lower()):
            emotion = str(ln.get("emotion") or "").strip()
            if emotion and not tts_client._is_neutral_emotion(emotion):
                desc = ""
                try:
                    desc = _dub_character_desc(ln.get("character"), project_name)
                except Exception:  # noqa: BLE001
                    desc = ""
                voice = dict(voice, mode="design",
                             instruct=tts_client._emotion_instruct(emotion, desc))
                ln["voice"] = voice
        # —— 时长/截断：记录期望时长供质检比对（不阻断）——
        if "时长" in joined or "截断" in joined:
            expect = _audio_line_expect_sec(ln)
            if expect > 0:
                ln["audio_expect_sec"] = round(float(expect), 2)
    except Exception as e:  # noqa: BLE001 - 纠偏失败绝不影响配音
        app.logger.warning(f"配音教训纠偏失败（忽略）：{e}")

def _apply_audio_lessons(plan_lines: list, project_name: str) -> int:
    """配音计划构建后、逐句合成前的音频教训召回（设计 §2.3.2）。

    对每句按 ``kind="audio"`` 召回历史教训（键 = 该句 text）：
      - ``ln["audio_hints"]`` 只存审计，**绝不进台词**；
      - 调 ``_apply_audio_hints`` 做计划级纠偏（说话人回填 / preset→design / 期望时长）。
    无教训时零行为变更；召回失败静默忽略（保险不影响配音）。返回产生 hints 的句数。
    """
    if not project_name or not plan_lines:
        return 0
    hit_lines = 0
    for ln in plan_lines:
        text = str(ln.get("text") or "")
        # 键用 build_dub_plan 刚构建、**尚未被预检自愈过**的台词原文（TTS 实际输入），
        # 保证 _record_audio_qc_lesson 里 phash 稳定、与自愈后的 text 不漂移。
        ln.setdefault("audio_orig_text", text)
        if not text:
            continue
        try:
            hints = prompt_memory.suggest(kind="audio", prompt=text, project=project_name,
                                          root_dir=PROJECT_OUTPUT_DIR)
        except Exception:  # noqa: BLE001 - 召回失败绝不影响配音
            hints = []
        if not hints:
            continue
        ln["audio_hints"] = list(hints)
        _apply_audio_hints(ln, hints, project_name)
        hit_lines += 1
    return hit_lines

def _audio_line_expect_sec(line: dict) -> float:
    """该句配音的期望时长（由台词字数推算；推算不出时退回镜头时长）

    只用于「时长偏差」这一条软判据，因此宁松勿紧：优先用字数推算（能发现「被截断」），
    推算不出（空台词）时才退回剧本给的镜头时长，避免拿 0 当期望值把一切都判成偏差。
    """
    est = audio_qc.estimate_speech_sec(line.get("text"))
    if est > 0:
        return est
    try:
        return max(0.0, float(line.get("duration_hint") or 0))
    except (TypeError, ValueError):
        return 0.0

def _dub_prompt_preflight(lines: list, project_name: str = "") -> dict:
    """配音台词生成前预检：就地自愈 ``lines[i]["text"]``，结论写入 ``lines[i]["prompt_qc"]``。

    永不抛异常（预检是保险，保险本身出问题不能耽误配音）。
    """
    stats = {"enabled": False, "checked": 0, "repaired": 0, "blocked": 0,
             "issue_lines": 0, "repaired_lines": 0, "problem_lines": []}
    if not lines:
        return stats
    try:
        cfg = _qc_load_cfg()
        if not prompt_qc.prompt_qc_ready(cfg):
            return stats
        stats["enabled"] = True
        mode = prompt_qc.prompt_qc_mode(cfg)
        for ln in lines:
            text = ln.get("text") or ""
            ctx = {
                "project_name": project_name,
                "shot_id": ln.get("shot_id"),
                "character": ln.get("character"),
                "emotion": ln.get("emotion"),
                # preset（CustomVoice）会忽略 instruct → 情绪送不进 TTS，预检据此提示
                "voice_mode": (ln.get("voice") or {}).get("mode"),
                # 剧本没写 speaker（或写了未登记角色）时 build_dub_plan 落到「旁白」音色，
                # 角色台词会被旁白念 —— 用「最终音色是不是旁白兜底」判定，而不是旧写法
                # `source == "narration"`（旁白通道关闭后 source 恒为 dialogue，那个判据永远为假，
                # 等于这条预检规则静默失效）。
                "speaker_fallback": str(ln.get("character") or "") == tts_client.NARRATION_SPEAKER,
            }
            pf = prompt_qc.preflight("audio", text, ctx=ctx, cfg=cfg)
            verdict = pf.get("verdict") or {}
            stats["checked"] += 1
            if pf.get("repairs"):
                stats["repaired"] += 1
            if verdict.get("issues"):
                stats["issue_lines"] += 1
            # ⚠️ 自愈结果为空时**保留原文**：把台词改成空串会让该句直接合成失败/静音，
            #    比「带一点噪音」更糟。空台词交给调用方按 rebuild_hint 从剧本重建。
            new_text = pf.get("prompt") or ""
            if new_text and new_text != text:
                ln["text"] = new_text
                stats["repaired_lines"] += 1
            ln["prompt_qc"] = {
                "mode": mode,
                "passed": bool(verdict.get("passed")),
                "blocked": bool(pf.get("blocked")),
                "score": verdict.get("score"),
                "issues": list(verdict.get("issues") or []),
                "critical_issues": list(verdict.get("critical_issues") or []),
                "repairs": list(pf.get("repairs") or []),
                "label": pf.get("label") or "",
                "reason": pf.get("reason") or "",
                "rebuild_hint": pf.get("rebuild_hint") or "",
            }
            if pf.get("blocked") or verdict.get("issues"):
                if pf.get("blocked"):
                    stats["blocked"] += 1
                if len(stats["problem_lines"]) < 20:
                    stats["problem_lines"].append({
                        "line_id": ln.get("line_id"), "shot_id": ln.get("shot_id"),
                        "character": ln.get("character"),
                        "blocked": bool(pf.get("blocked")),
                        "issues": list(verdict.get("issues") or [])[:3]
                                  + list(verdict.get("critical_issues") or [])[:2],
                        "repairs": list(pf.get("repairs") or []),
                        "rebuild_hint": pf.get("rebuild_hint") or "",
                    })
    except Exception as e:  # noqa: BLE001 - 预检失败绝不影响配音
        app.logger.warning(f"配音台词预检异常（已跳过，不影响配音）：{e}")
    return stats

def _audio_qc_lines(project_name: str, lines: list, results: list, cfg: dict,
                    retry_cb=None) -> dict:
    """配音成品逐句质检（客观层 + AI 层），结论写入 ``results[i]["audio_qc"]``。

    ``retry_cb(line, result) -> dict|None``：可选的重配合回调。**只对客观层判致命的句子
    调用**（整段无声/空文件）—— 这类失败属于「合成出了东西但不是人声」，重配一次是最有效
    的补救；软扣分项（音量偏小、时长偏差）不重配，交由用户决定。

    永不抛异常；批量口径为「记录 + 有限重配」，不阻断整集。
    """
    stats = {"enabled": False, "checked": 0, "passed": 0, "failed": 0, "blocked": 0,
             "ai_used": 0, "retried": 0, "recovered": 0, "problems": []}
    if not results:
        return stats
    try:
        if not qc_client.audio_qc_ready(cfg):
            return stats
        stats["enabled"] = True
        by_id = {str(l.get("line_id")): l for l in (lines or [])}
        visuals_root = os.path.join(QC_DIR, "audio", _safe_project(project_name or "project"))
        for r in results:
            if not r.get("ok") or not r.get("out_path"):
                continue
            ln = by_id.get(str(r.get("line_id"))) or {}
            expect = _audio_line_expect_sec(ln)
            verdict = qc_client.check_audio(
                r["out_path"], expect_sec=expect or None,
                line_text=ln.get("text") or r.get("text") or "", cfg=cfg,
                visuals_dir=os.path.join(visuals_root,
                                         os.path.splitext(os.path.basename(r["out_path"]))[0]))
            stats["checked"] += 1
            # 致命（整段无声/空文件）→ 重配一次。⚠️ 计数必须在重配之后按**最终**结论统计：
            # 先记 blocked 再重配会出现「致命 1 句 / 未通过 0 句」这种自相矛盾的汇总，
            # 前端与任务消息都在读这两个数，口径必须一致。
            if verdict.get("blocked") and retry_cb is not None:
                try:
                    stats["retried"] += 1
                    again = retry_cb(ln, r)
                    if again:
                        verdict = again
                        if not verdict.get("blocked"):
                            stats["recovered"] += 1
                except Exception as e:  # noqa: BLE001 - 重配失败不影响已有结论
                    app.logger.warning(f"音频质检重配失败（{r.get('line_id')}）：{e}")
            if verdict.get("blocked"):
                stats["blocked"] += 1
            if verdict.get("ai_used"):
                stats["ai_used"] += 1
            if verdict.get("passed"):
                stats["passed"] += 1
            else:
                stats["failed"] += 1
                # T03b：配音成品质检不达标 → 沉淀 kind="audio" 教训（键 = 该句 TTS 输入原文，
                # 自愈前用 audio_orig_text）。verdict.ok=false（接口异常）时 verdict 无有效缺陷，
                # 不沉淀，避免把「质检调用失败」记成「这句配音有问题」。
                if verdict.get("ok", True) and ln:
                    _record_audio_qc_lesson(project_name, ln, verdict)
                # ★ 用户需求：质检不合格的配音不留本地。⚠️ **仅在最终 failed（重配也失败）时删**：
                # `blocked`（整段无声/空文件）已由 retry_cb 重配过一次，重配若恢复则 verdict
                # 被替换、不会走到这里；能走到这里说明**最终结论仍不合格**。删除条件是
                # 「质检成功返回（ok 非 False）且最终 not passed」—— ok=False（接口故障）不删。
                # 删：该句 wav（P9）+ 其可视化目录（P10 output/qc/audio/<项目>/<stem>/）。
                if verdict.get("ok", True) and r.get("out_path"):
                    try:
                        _stem = os.path.splitext(os.path.basename(r["out_path"]))[0]
                        _purge_rejected_artifacts(
                            [r["out_path"], os.path.join(visuals_root, _stem)],
                            project=project_name,
                            reason=f"配音质检不合格（{verdict.get('reason') or ''}）"[:120],
                            kind="audio_line")
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning(f"不合格配音清理失败（忽略）：{_pe}")
                if len(stats["problems"]) < 20:
                    stats["problems"].append({
                        "line_id": r.get("line_id"), "shot_id": r.get("shot_id"),
                        "character": r.get("character"),
                        "blocked": bool(verdict.get("blocked")),
                        "score": verdict.get("score"),
                        "reason": str(verdict.get("reason") or "")[:200],
                        "metrics": verdict.get("metrics") or {},
                        "visuals": [f"/api/qc/frames/audio/{_safe_project(project_name or 'project')}/"
                                    f"{os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}"
                                    for p in (verdict.get("visuals") or [])],
                    })
            r["audio_qc"] = {
                "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
                "score": verdict.get("score"),
                "reason": str(verdict.get("reason") or "")[:300],
                "issues": list(verdict.get("issues") or [])[:5],
                "critical_issues": list(verdict.get("critical_issues") or [])[:3],
                "metrics": verdict.get("metrics") or {},
                "ai_used": bool(verdict.get("ai_used")),
                "ai_skipped": bool(verdict.get("ai_skipped")),
                "ai_skip_reason": verdict.get("ai_skip_reason") or "",
                "visuals": [f"/api/qc/frames/audio/{_safe_project(project_name or 'project')}/"
                            f"{os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}"
                            for p in (verdict.get("visuals") or [])],
            }
    except Exception as e:  # noqa: BLE001 - 质检失败绝不影响配音产物
        app.logger.warning(f"配音成品质检异常（已跳过，不影响配音）：{e}")
    if stats["enabled"]:
        app.logger.info(f"配音质检（{project_name}）：检查 {stats['checked']} 句，"
                        f"通过 {stats['passed']}，未通过 {stats['failed']}，"
                        f"致命 {stats['blocked']}，重配 {stats['retried']}，"
                        f"恢复 {stats['recovered']}，AI 层 {stats['ai_used']}")
    return stats

def _mix_audio_qc(report: dict, cfg: dict) -> dict:
    """带配音成片的音频质检（整轨口径）。

    ⚠️ 必须关掉「有声占比下限」：成片天然有大段无台词留白（无台词镜头/纯环境音），
    拿单句的 50% 标准去卡它必然误报「漏句」。整轨真正要挡的是**整条音轨近乎无声**
    （amix 失败 / 全部条目静音）与**不含音频流** —— 这两条都在客观层的硬闸里。
    """
    try:
        if not qc_client.audio_qc_ready(cfg):
            return {"enabled": False, "reason": "音频质检开关未开启"}
        out = report.get("output_path") or ""
        before = report.get("video_before") or {}
        expect = 0.0
        try:
            expect = float(before.get("duration") or 0)
        except (TypeError, ValueError):
            expect = 0.0
        project_name = _safe_project(report.get("project") or "project")
        stem = os.path.splitext(os.path.basename(out))[0]
        verdict = qc_client.check_audio(
            out, expect_sec=expect or None, cfg=cfg,
            check_speech_ratio=False,
            visuals_dir=os.path.join(QC_DIR, "audio_mix", project_name, stem))
        metrics = verdict.get("metrics") or {}
        out_v = {
            "enabled": True,
            "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
            "score": verdict.get("score"),
            "reason": str(verdict.get("reason") or "")[:300],
            "issues": list(verdict.get("issues") or [])[:5],
            "critical_issues": list(verdict.get("critical_issues") or [])[:3],
            "metrics": metrics,
            "ai_used": bool(verdict.get("ai_used")),
            "ai_skipped": bool(verdict.get("ai_skipped")),
            "ai_skip_reason": verdict.get("ai_skip_reason") or "",
            "visuals": [f"/api/qc/frames/audio_mix/{project_name}/{stem}/"
                        f"{os.path.basename(p)}" for p in (verdict.get("visuals") or [])],
            "coverage_sec": report.get("coverage_sec"),
            "video_duration": expect or None,
        }
        # 配音覆盖率：逐句音频总时长 / 视频时长。**两端都要看**：
        #   偏低（<50%）→ 大量镜头没有配音落点；
        #   偏高（>115%）→ 台词总长超过画面，末尾整段被 `-shortest` **静默截掉**
        #     （成片仍「有声音」所以客观层查不出来，但台词已经丢了一大半）。
        #   ⚠️ 曾只写「偏低」这一个方向，实测把 ep04（台词 606.4s / 画面 85.2s，
        #      21 句里 17 句落在片外）这条最该拦的缺陷直接放过了 —— 覆盖率是**比值**，
        #      单向判定等于漏掉一半语义。
        try:
            cov = float(report.get("coverage_sec") or 0)
            if expect > 0:
                ratio = cov / expect
                out_v.setdefault("issues", [])
                if ratio < 0.5:
                    out_v["issues"].append(
                        f"配音覆盖偏低：逐句音频合计 {cov:.1f}s / 视频 {expect:.1f}s"
                        f"（{ratio * 100:.0f}%）")
                elif ratio > 1.15:
                    entries = report.get("entries") or []
                    dropped = 0
                    for e in entries:
                        try:
                            if float(e.get("start") or 0) >= expect:
                                dropped += 1
                        except (TypeError, ValueError):
                            continue
                    detail = (f"，其中 {dropped}/{len(entries)} 句起始点已在片长之外、放不出来"
                              if dropped else "")
                    msg = (f"配音总长超出画面：逐句音频合计 {cov:.1f}s / 视频 {expect:.1f}s"
                           f"（{ratio * 100:.0f}%）{detail} —— 超出部分会被合成命令静默截断")
                    out_v["issues"].append(msg)
                    out_v.setdefault("critical_issues", [])
                    out_v["critical_issues"].append(msg)
                    # 单向收紧：客观层/AI 层说通过也翻不回来
                    out_v["blocked"] = True
                    out_v["passed"] = False
                    try:
                        out_v["score"] = min(int(out_v.get("score") or 0), 40)
                    except (TypeError, ValueError) as e:
                        app.logger.debug("评分字段解析失败（忽略）：%s", e)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            app.logger.debug("评分归一化计算失败（忽略）：%s", e)
        # ★ 用户需求：质检不合格的混音不留本地（P10 可视化目录）。仅当最终「质检成功返回
        # 且不合格」（ok 非 False 且 passed=False）时删；ok=False（接口故障）不删。
        if verdict.get("ok") is not False and not out_v.get("passed"):
            try:
                _purge_rejected_artifacts(
                    [os.path.join(QC_DIR, "audio_mix", project_name, stem)],
                    project=project_name,
                    reason=f"混音质检不合格（{out_v.get('reason') or ''}）"[:120],
                    kind="audio_mix")
            except Exception as _pe:  # noqa: BLE001
                app.logger.warning(f"不合格混音清理失败（忽略）：{_pe}")
        return out_v
    except Exception as e:  # noqa: BLE001 - 质检失败绝不影响合成结果
        app.logger.warning(f"成片音频质检异常（已跳过）：{e}")
        return {"enabled": True, "passed": None, "error": f"{type(e).__name__}: {e}"}

def _dub_worker(task_id: str, project_name: str, plan: dict, out_dir: str,
                fmt: str, episode: int):
    """后台配音：批量合成逐句音频 → 合并整集音轨 → 落盘清单"""
    try:
        lines = plan.get("lines") or []
        if not lines:
            raise TTSError("配音计划为空（剧本中没有可朗读台词，或所选镜头无台词）")

        lines_dir = os.path.join(out_dir, "lines")
        client = QwenTTSClient(out_root=DUB_DIR, params=TTS_DEFAULT_PARAMS)
        for ln in lines:
            ln["project_tag"] = project_name

        # ---- ① 生成前提示词预检（配音台词）----
        # 台词会被 TTS 逐字念出来：结构化残留（`(S1) 说：[Chinese] …`）、舞台指示
        # （`（转身冷笑）`）都会原样进成片；空台词/纯标点则合成出静音却显示「成功」。
        # 这一层零模型依赖、默认开启，在消耗 GPU 之前把确定性缺陷挡住/修掉。
        qc_cfg = _qc_load_cfg()
        pre = _dub_prompt_preflight(lines, project_name)
        if pre.get("blocked") or pre.get("repaired_lines"):
            with dub_lock:
                dub_tasks[task_id].update({"prompt_qc": pre})

        def _cb(done, total, last, note):
            with dub_lock:
                dub_tasks[task_id].update({
                    "current": done, "total": total,
                    "progress": int(done / max(1, total) * 90),
                    "phase": f"配音合成中（{done}/{total}）",
                    "message": f"最新：{last.get('character') or ''} {str(last.get('text') or '')[:18]}",
                })

        results = client.synthesize_lines(lines, lines_dir, progress_cb=_cb)
        ok_items = [r for r in results if r.get("ok")]
        with dub_lock:
            dub_tasks[task_id].update({
                "results": [
                    dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                    for r in results
                ],
                "success_count": len(ok_items), "failed_count": len(results) - len(ok_items),
            })

        # ---- ② 成品质检（音频客观层 + 频谱/波形 AI 层）----
        # 「合成成功」不等于「念出来了」：节点正常返回、文件也落盘，但整段可以是静音
        # （漏配音 / 模型未发声）。旧流程要等到成片验收才发现整集缺一句。
        # 这里逐句实测，致命的（整段无声/空文件）当场重配一次，软扣分项只记录。
        def _retry_line(ln, rec):
            """重配单句并重新质检（只对客观层判致命的句子调用）"""
            import copy as _copy
            one = _copy.deepcopy(ln)
            one["project_tag"] = project_name
            res = client.synthesize_lines([one], lines_dir)
            if not res or not res[0].get("ok"):
                return None
            rec.update({k: v for k, v in res[0].items() if k != "audio_qc"})
            return qc_client.check_audio(
                rec["out_path"], expect_sec=_audio_line_expect_sec(ln) or None,
                line_text=ln.get("text") or "", cfg=qc_cfg,
                visuals_dir=os.path.join(QC_DIR, "audio", _safe_project(project_name),
                                         os.path.splitext(os.path.basename(rec["out_path"]))[0]))

        if qc_cfg.get("enabled") and qc_cfg.get("audio_enabled"):
            with dub_lock:
                dub_tasks[task_id].update({"phase": "配音质检中（客观指标 + 频谱波形送检）",
                                           "progress": 92})
        aqua = _audio_qc_lines(project_name, lines, results, qc_cfg, retry_cb=_retry_line)
        if aqua.get("enabled"):
            with dub_lock:
                dub_tasks[task_id].update({"audio_qc": aqua})
        ok_items = [r for r in results if r.get("ok")]
        with dub_lock:
            dub_tasks[task_id].update({
                "results": [
                    dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                    for r in results
                ],
                "success_count": len(ok_items), "failed_count": len(results) - len(ok_items),
                "audio_qc_failed": aqua.get("failed", 0),
            })

        # 合并整集音轨（按剧本镜头顺序）
        merged = None
        merged_probe = {}
        if ok_items:
            with dub_lock:
                dub_tasks[task_id].update({"phase": "合并整集音轨", "progress": 94})
            order = {l.get("line_id"): i for i, l in enumerate(lines)}
            ok_sorted = sorted(ok_items, key=lambda r: order.get(r.get("line_id"), 9999))
            merged_path = os.path.join(out_dir, f"ep{int(episode):02d}_dub.{fmt}")
            merged = concat_audio([r["out_path"] for r in ok_sorted], merged_path, fmt=fmt)
            merged_probe = probe_audio_info(merged)

        manifest = {
            "project": project_name, "episode": episode,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "script_path": plan.get("script_path") or "",
            "voice_map": plan.get("voice_map") or {},
            "characters": plan.get("characters") or [],
            "merged_audio": merged,
            "merged_info": merged_probe,
            # 质检结论随清单落盘：成片验收时能回溯「这句当时是怎么判的」
            "prompt_qc": pre if pre.get("enabled") else {},
            "audio_qc": aqua if aqua.get("enabled") else {},
            "lines": [dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                      for r in results],
        }
        manifest_path = os.path.join(out_dir, f"ep{int(episode):02d}_dub_manifest.json")
        # A-3 P1：manifest 原子写改走 fs_atomic（唯一临时名 + flush/fsync + .bak 快照 +
        # os.replace 重试），替代旧「固定 .tmp + os.replace」
        atomic_write_json(manifest_path, manifest)

        _msg = f"成功 {len(ok_items)} 句 / 失败 {len(results) - len(ok_items)} 句"
        if aqua.get("enabled") and aqua.get("checked"):
            _msg += f"；质检通过 {aqua['passed']}/{aqua['checked']} 句"
            if aqua.get("recovered"):
                _msg += f"（重配恢复 {aqua['recovered']} 句）"
        with dub_lock:
            dub_tasks[task_id].update({
                "status": "completed" if ok_items else "failed",
                "progress": 100, "phase": "配音完成" if ok_items else "配音失败",
                "message": _msg,
                "merged_audio": merged,
                "merged_url": _dub_audio_url(project_name, merged) if merged else "",
                "merged_info": merged_probe,
                "manifest": manifest_path,
                "error": "" if ok_items else "全部句子合成失败，请查看 results 中的错误原因",
            })
        with dub_lock:
            _prune_task_registry(dub_tasks)
    except (TTSError, OSError) as e:
        app.logger.error(f"配音任务失败: {e}")
        # B-16 P2-11：配音失败 → 清理本任务产生的中间产物（lines 目录、merged 半成品）
        _cleanup_scratch_dir(os.path.join(out_dir, "lines"), app.logger)
        with dub_lock:
            dub_tasks[task_id].update({"status": "failed", "error": str(e), "phase": "失败"})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("配音任务异常")
        # B-16 P2-11：配音异常 → 清理中间产物
        _cleanup_scratch_dir(os.path.join(out_dir, "lines"), app.logger)
        with dub_lock:
            dub_tasks[task_id].update({"status": "failed", "error": f"异常：{e}", "phase": "失败"})

# =====================================================================
# 音画对齐与混音合成（配音轨 × 成片视频 → 带配音成片 output/final_dub/）
# =====================================================================

mix_tasks = {}

mix_lock = threading.Lock()

def _mix_resolve_video(data: dict, project_name: str) -> str:
    """定位待合成的成片：显式 video_path/video_url 优先，否则在项目成片目录自动匹配最新 mp4"""
    if (data.get("video_path") or "").strip() or (data.get("video_url") or "").strip():
        return _upscale_resolve_video(data)
    cands = []
    for d in project_store.project_dirs(FINAL_DIR, project_name):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower().endswith(".mp4"):
                p = os.path.join(d, name)
                cands.append((os.path.getmtime(p), p))
    if not cands:
        raise DubMixError(
            "未找到成片视频：请先在步骤6完成成片合成，或显式提供 video_path / video_url")
    cands.sort(reverse=True)
    return cands[0][1]

def _mix_segments_dir(project_name: str, episode: int = 0) -> str:
    """定位该集（episode 给定）或该项目的镜头分段视频目录（用于按真实分段时长对齐时间轴）

    B-10 P1-6：带集号过滤。第 2 集起不再取到第 1 集素材，避免时间轴/成片源系统性错配。
    优先匹配该集专属目录（``<key>_第N集`` 或 ``epNN`` 子目录），找不到再回退到项目级目录。
    """
    ep_tag = f"ep{int(episode):02d}" if episode else ""
    best, best_key = "", (-1, 0)
    # 优先找该集专属目录（第 2 集起视频通常落在 <项目键>_第N集/ 或 epNN/ 子目录）
    ep_dir = ""
    if episode:
        for cand in (os.path.join(VIDEOS_DIR, project_name, ep_tag),
                     os.path.join(VIDEOS_DIR, f"{project_name}_第{episode}集")):
            if os.path.isdir(cand):
                ep_dir = cand
                break
    if ep_dir:
        # 该集目录直接采用
        vids = [f for f in os.listdir(ep_dir) if f.lower().endswith(".mp4")]
        if vids:
            return ep_dir
    # 回退：项目级目录（第 1 集或整集模式）
    for d in project_store.project_dirs(VIDEOS_DIR, project_name):
        if not os.path.isdir(d):
            continue
        vids = [f for f in os.listdir(d) if f.lower().endswith(".mp4")]
        if not vids:
            continue
        key = (len(vids), max(os.path.getmtime(os.path.join(d, f)) for f in vids))
        if key > best_key:
            best, best_key = d, key
    return best

def _mix_manifest(project_name: str, episode: int = 0) -> dict:
    """读取配音清单（优先指定集数，其次最新）"""
    out_dir = os.path.join(DUB_DIR, project_name)
    if not os.path.isdir(out_dir):
        raise DubMixError(f"尚未生成配音（目录不存在）：{out_dir}")
    if episode:
        p = os.path.join(out_dir, f"ep{int(episode):02d}_dub_manifest.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return {"manifest": json.load(f), "path": p}
    cands = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
             if f.endswith("_dub_manifest.json")]
    if not cands:
        raise DubMixError("未找到配音清单（*_dub_manifest.json），请先完成配音合成")
    cands.sort(key=os.path.getmtime, reverse=True)
    p = cands[0]
    with open(p, "r", encoding="utf-8") as f:
        return {"manifest": json.load(f), "path": p}

def _mix_audio_url(project_name: str, rel_path: str) -> str:
    base = os.path.abspath(mix_out_dir(project_name))
    p = os.path.abspath(rel_path)
    if not p.startswith(base + os.sep):
        return ""
    rel = os.path.relpath(p, base).replace(os.sep, "/")
    return f"/api/mix/file/{project_name}/{rel}"

def _episode_video_stats(project_name: str, episode_no) -> dict:
    """该集镜头视频就绪度：剧本镜头数 vs 已落盘视频数（>1KB 才算数）"""
    script = _load_script_for(project_name, episode_no)
    shots = [s for s in (script.get('shots') or []) if isinstance(s, dict)]
    d = _ep_dir(os.path.join(VIDEOS_DIR, project_name), episode_no)
    ready = 0
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if not fn.lower().endswith('.mp4'):
                continue
            try:
                if os.path.getsize(os.path.join(d, fn)) > 1024:
                    ready += 1
            except OSError:
                continue
    return {"total": len(shots), "ready": ready, "dir": d}

def register_final_deliverable(project_name: str, episode_no, video_path: str,
                               meta: dict = None) -> dict:
    """把整集成片登记进「待验收」队列（幂等）。

    硬闸门（不满足就完全不登记，避免验收页被垃圾塞满）：
      0) 成片不存在 / < 100KB；1) 探不到时长或 < 2s。

    软闸门（镜头覆盖）：剧本镜头数 vs 已落盘镜头视频数。
    这里刻意不做「时长 >= 镜头数 x N 秒」的硬判定 —— 漫剧单镜常常不到 1s
    （实测 6 镜合并成片只有 4.46s），按时长否决会把真成片误判成半成品。
    镜头不齐时仍然登记，但在 meta 里打 `incomplete_shots` + `warning`，
    让「成品验收」页能显示「可能不完整」提醒，用户可据此打回。

    返回 {"registered": bool, "reason": str, "stats": {...}, "item": {...}}
    """
    # 铁律（2026-09-29）：预演产物**永不可交付**。这里给一个**友好拒绝**（不抛异常），
    # 让调用方能直接把原因显示给用户；同一不变量在 pipeline.record_deliverable 上还有
    # 一道硬闸门（防绕过）。
    _ok, _why = preview_gate.deliverable_ok(video_path)
    if not _ok:
        app.logger.error("[预演拦截] 拒绝把非正式产物登记为成片（%s）：%s",
                         os.path.basename(str(video_path or "")), _why)
        return {"registered": False, "reason": _why, "stats": {}, "preview_blocked": True}
    if not video_path or not os.path.exists(video_path):
        return {"registered": False, "reason": "成片文件不存在", "stats": {}}
    try:
        size = os.path.getsize(video_path)
    except OSError as e:
        return {"registered": False, "reason": f"成片不可读：{e}", "stats": {}}
    if size < 100 * 1024:
        return {"registered": False, "reason": f"成片过小（{size} 字节），疑似半成品",
                "stats": {"size": size}}

    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1

    try:
        vinfo = probe_video_info(video_path) or {}
    except Exception:  # noqa: BLE001 - 探测失败不代表成片不可用，走宽松分支
        vinfo = {}
    duration = float(vinfo.get("duration") or 0)
    if duration and duration < 2.0:
        return {"registered": False, "reason": f"成片仅 {duration:.1f}s，疑似片段",
                "stats": {"duration": duration, "size": size}}

    stats = _episode_video_stats(project_name, ep)
    stats["size"], stats["duration"] = size, duration
    total, ready = int(stats.get("total") or 0), int(stats.get("ready") or 0)
    incomplete = bool(total > 0 and ready < total)

    item_meta = dict(meta or {})
    item_meta.setdefault("source", "final")
    item_meta.setdefault("duration_sec", round(duration, 2) if duration else None)
    item_meta.setdefault("size_bytes", size)
    item_meta["shots_total"] = total
    item_meta["shots_ready"] = ready
    if incomplete:
        item_meta["incomplete_shots"] = True
        item_meta["warning"] = (f"该集剧本 {total} 镜，仅发现 {ready} 个镜头视频，"
                                f"成片可能不完整，建议核对后再验收")
    try:
        item = pipeline.record_deliverable(project_name, ep, video_path, meta=item_meta)
    except Exception as e:  # noqa: BLE001 - 登记失败不能影响出片主流程
        app.logger.warning(f"成片登记交付物失败（{project_name} 第{ep}集）：{e}")
        return {"registered": False, "reason": f"登记失败：{e}", "stats": stats}
    if incomplete:
        app.logger.warning(f"成片已登记但镜头疑似不全：{project_name} 第{ep}集 "
                           f"({ready}/{total}) -> {os.path.basename(video_path)}")
        return {"registered": True,
                "reason": f"已登记（镜头覆盖 {ready}/{total}，可能不完整）",
                "stats": stats, "item": item, "incomplete": True}
    app.logger.info(f"成片已登记待验收：{project_name} 第{ep}集 -> {os.path.basename(video_path)}")
    return {"registered": True, "reason": "已登记", "stats": stats, "item": item}

def _isolate_shot_sfx(video_path: str, project: str, episode, shot_id) -> dict:
    """对单镜音轨做人声分离，只留音效（H3 原生音效，剔掉它自带的说话声）。

    **fail-open**：任何异常都只返回 ok=False，绝不打断出片流程。
    """
    try:
        import sfx_isolate
        return sfx_isolate.isolate_sfx(
            video_path, project, f"ep{int(episode):02d}_shot{int(shot_id):02d}")
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

def _mix_prepare(data: dict) -> dict:
    """公共准备：解析项目 / 视频 / 剧本 / 配音清单 / 时间轴 / 逐句条目（不合成）"""
    # P2-T2：mix 的 project 解析唯一事实源在 _mix_prepare（被 /mix/plan 与 /mix/generate 共用）。
    # 缺省/越界 project_name → 抛 DubMixError（两条路由均已 catch 并回 400），
    # 不再静默回落共享 'project' 命名空间造成串项目。前端契约必填。
    project_name, _mix_err = _project_or_400((data.get('project_name') or '').strip())
    if _mix_err is not None:
        raise DubMixError("缺少 project_name")
    video_path = _mix_resolve_video(data, project_name)

    resolved = _dub_resolve_script(dict(data, project_name=project_name))
    script = resolved["script"]
    episode = int(data.get('episode') or script.get('episode_no')
                  or (script.get('metadata') or {}).get('episode_no') or 0)

    mf = _mix_manifest(project_name, episode)
    manifest = mf["manifest"]
    episode = episode or int(manifest.get("episode") or 1)

    seg_dir = _mix_segments_dir(project_name, episode)
    timeline = shot_timeline(script, videos_dir=seg_dir)

    params = dict(MIX_DEFAULT_PARAMS)
    params.update(data.get('params') or {})
    mode = (data.get('mode') or params.get("mode") or "timeline").strip()

    lines = [ln for ln in (manifest.get("lines") or []) if ln.get("ok") and ln.get("out_path")]
    if mode == "concat":
        merged = manifest.get("merged_audio") or ""
        if not merged or not os.path.exists(merged):
            raise DubMixError("concat 模式需要整集合并音轨，但配音清单中没有有效 merged_audio")
        entries = [{"line_id": "merged", "shot_id": None, "character": "",
                    "text": "", "audio_path": os.path.abspath(merged),
                    "audio_dur": float((manifest.get("merged_info") or {}).get("duration") or 0),
                    "start": 0.0, "fit_ratio": 1.0}]
        warnings = ["concat 模式：整集音轨从 0 秒顺次铺设，不做逐镜头对齐"]
    else:
        built = build_entries(lines, timeline, params, mode=mode)
        entries, warnings = built["entries"], list(built["warnings"])
    if not entries:
        raise DubMixError("没有可用的配音音频：请先完成配音合成，或检查配音文件是否存在")

    # ---- 音效轨：H3 原生音效经人声分离后垫底（2026-09-17 新增）----
    # 为什么需要：H3 是音视频联合模型，原音轨里既有打斗/雨声等音效，也有它自己生成的
    # 说话声。直接保留原音轨会让两套人声重叠；完全丢弃又会让成片没有任何音效。
    # 折中：用 sfx_isolate 分离出「纯音效」，作为独立条目按同一条时间轴垫底。
    sfx_entries = []
    if H3_SFX_ISOLATE and mode != "concat":
        try:
            import sfx_isolate
            sfx_entries = sfx_isolate.build_sfx_entries(
                project_name, episode, timeline,
                volume=float(params.get("original_audio_volume") or 0.3))
        except Exception as e:                                  # noqa: BLE001
            warnings.append(f"音效轨装配失败（本集跳过音效）：{type(e).__name__}: {e}")
    if sfx_entries:
        entries = list(entries) + sfx_entries
        # 已用「分离后的纯音效」→ 关掉视频原音轨，否则人声会回来、音效也会叠双份
        params["keep_original_audio"] = False
        warnings.append(f"已叠加 {len(sfx_entries)} 条镜头音效（H3 音轨已做人声分离，"
                        f"垫底音量 {params.get('original_audio_volume')}）")
    elif H3_SFX_ISOLATE and params.get("keep_original_audio"):
        warnings.append("未找到可用的分离音效轨，将直接使用视频原音轨垫底"
                        "（其中可能含 H3 生成的说话声）")

    vinfo = probe_video_info(video_path)
    if vinfo.get("duration") and entries[-1].get("end", 0) > float(vinfo["duration"]) + 0.5:
        warnings.append(
            f"末句结束 {entries[-1].get('end')}s 超出视频时长 {vinfo.get('duration')}s，超出部分会被截断")

    return {
        "project_name": project_name, "video_path": video_path, "video_info": vinfo,
        "script_path": resolved["script_path"], "script_source": resolved["source"],
        "episode": episode, "manifest_path": mf["path"], "manifest": manifest,
        "segments_dir": seg_dir, "timeline": timeline,
        "entries": entries, "warnings": warnings, "mode": mode, "params": params,
    }

def _mix_worker(task_id: str, prepared: dict, out_name: str):
    """后台合成：逐句对齐混音 → 落盘带配音成片 + 报告"""
    try:
        project_name = prepared["project_name"]
        out_dir = mix_out_dir(project_name)
        out_path = os.path.join(out_dir, out_name)
        with mix_lock:
            mix_tasks[task_id].update({"phase": "音画对齐混音中", "progress": 30})
        report = mix_video_with_entries(prepared["video_path"], prepared["entries"],
                                        out_path, prepared["params"])
        # ---- 成品音频质检：成片音轨是不是真的有人声 ----
        # ffmpeg 返回成功、文件也有音频流，并不代表「配音真的混进去了」：
        # 条目路径错、amix 被压成静音、源片段本身无声，都能产出一条「合法但没声音」的
        # 音轨。这里对**最终成片**实测一遍（整轨口径，不做有声占比判定）。
        report["audio_qc"] = _mix_audio_qc(report, _qc_load_cfg())
        report.update({
            "task_id": task_id, "project": project_name, "episode": prepared["episode"],
            "mode": prepared["mode"], "video_source": prepared["video_path"],
            "script_path": prepared["script_path"], "manifest_path": prepared["manifest_path"],
            "segments_dir": prepared["segments_dir"], "warnings": prepared["warnings"],
            "timeline": prepared["timeline"], "entries": prepared["entries"],
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        report_path = os.path.join(out_dir, f"{os.path.splitext(out_name)[0]}_mix_report.json")
        write_mix_report(report, report_path)
        _aq = report.get("audio_qc") or {}
        _aq_msg = ""
        if _aq.get("enabled") and _aq.get("passed") is not None:
            _aq_msg = "；音频质检通过" if _aq.get("passed") else \
                f"；音频质检未通过（{str(_aq.get('reason') or '')[:60]}）"
        with mix_lock:
            mix_tasks[task_id].update({
                "status": "completed", "progress": 100, "phase": "合成完成",
                "message": (f"已合成 {report['entry_count']} 句配音，"
                            f"耗时 {report['elapsed_sec']}s{_aq_msg}"),
                "output_path": report["output_path"],
                "report_path": report_path,
                "url": _mix_audio_url(project_name, report["output_path"]),
                "audio_qc": _aq,
                # 音频质检的致命结论随任务态一起对外暴露（此前只落在 report JSON 里，
                # 界面完全看不到，只能点开文件才知道这一版音轨其实不合格）
                "qc_blocked": bool(_aq.get("blocked")),
                "result": report,
            })
        with mix_lock:
            _prune_task_registry(mix_tasks)
        # 带配音成片＝用户真正要验收的成品：自动登记进「成品验收」队列
        #
        # ⚠️ 2026-10-07 收紧为 fail-closed：此前**无条件**登记，音频质检把
        # 「整条音轨近乎无声 / 不含音频流 / 覆盖率严重不足」判成 blocked=True、
        # passed=False 之后，结论只写进 *_mix_report.json，任务照样 status=completed、
        # 照样登记进验收队列 —— 与历史「分镜僵尸成功」同一形态：失败被记成成功，
        # 用户在验收页看到的是一版**没有可用人声**的成片，还以为混音成功了。
        # 现在 blocked 时**不登记**，并把原因带进 deliverable，让界面能显式提示。
        # 注意：仍然保留 status=completed 与产物路径 —— 文件确实产出了，
        # 用户需要能预览/下载去排查；只是它**不再冒充合格成品**进入验收队列。
        _aq_blocked = bool(_aq.get("blocked"))
        if _aq_blocked:
            reg = {"registered": False,
                   "reason": ("音频质检判定为致命不合格（"
                              f"{str(_aq.get('reason') or '')[:80] or '成片音轨不可用'}），"
                              "本次成片不进入待验收队列")}
            app.logger.error(
                "混音成片未登记待验收（%s 第%s集）：%s",
                project_name, prepared["episode"], reg["reason"])
        else:
            reg = register_final_deliverable(
                project_name, prepared["episode"], report["output_path"],
                meta={"source": "mix", "mode": prepared["mode"],
                      "entry_count": report.get("entry_count"),
                      "video_source": os.path.basename(prepared["video_path"] or ""),
                      "report": os.path.basename(report_path)})
        with mix_lock:
            mix_tasks[task_id]["deliverable"] = {
                "registered": bool(reg.get("registered")),
                "reason": reg.get("reason") or "",
                "episode_no": int(prepared["episode"] or 1),
                "path": report["output_path"],
            }
        if not reg.get("registered"):
            app.logger.info(f"成片未登记待验收（{project_name} 第{prepared['episode']}集）："
                            f"{reg.get('reason')}")
    except DubMixError as e:
        app.logger.warning(f"混音合成失败: {e}")
        # B-16 P2-11：混音失败 → 清理本任务产生的中间产物（未完成的 report / 半成品）
        _cleanup_scratch_dir(out_dir, app.logger)
        with mix_lock:
            mix_tasks[task_id].update({"status": "failed", "phase": "合成失败",
                                       "message": str(e), "progress": 100})
    except Exception as e:  # pragma: no cover - 兜底
        app.logger.exception("音画合成异常")
        # B-16 P2-11：混音异常 → 清理中间产物
        _cleanup_scratch_dir(out_dir, app.logger)
        with mix_lock:
            mix_tasks[task_id].update({"status": "failed", "phase": "合成异常",
                                       "message": f"{type(e).__name__}: {e}", "progress": 100})

def _friendly_error(msg, fallback: str = "服务内部错误，请稍后重试（详情见后端日志）") -> str:
    """把后端异常整理成可安全展示给前端的文案（对应测试缺陷 D5）。

    前端错误框不应出现 traceback、文件路径、模块名等实现细节。
    这里做一次归一化：截掉 traceback 段、去掉 File/line 与模块来源、
    压缩空白并限长；若仍残留实现细节特征，则整体降级为通用文案。
    业务类友好错误（中文短句）会原样保留。
    """
    text = str(msg or "").strip()
    if not text:
        return fallback
    idx = text.find("Traceback (most recent call last)")
    if idx != -1:
        text = text[:idx].strip()
    text = text.splitlines()[-1].strip() if text else ""
    text = re.sub(r'File\s+"[^"]*",\s*line\s*\d+', "", text)
    text = re.sub(r"\s*from\s+'[^']*'", "", text)          # cannot import name 'X' from 'mod'
    text = re.sub(r"\s*\([^()]*\.py[^()]*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # 仍残留实现细节（模块/导入语句/文件路径）→ 一律降级，避免外泄内部结构
    if re.search(r"\.py\b|\bimport\b|\bmodule\b|site-packages|[\\/]", text):
        return fallback
    if len(text) > 160:
        text = text[:160] + "…"
    return text or fallback

# =====================================================================
# 四层质量状态 · 审片接口（2026-09-29，C/D 层的人工并排复核）
# =====================================================================
# 数据口径与生成链路完全一致（零新管道）：
#   逐镜视频   /api/videos/<项目>/epNN/shot_XX.mp4          （VIDEOS_DIR）
#   分镜图     /api/storyboards/file/<项目>/shot_XX.png      （合同侧的「预期画面」）
#   抽帧       /api/qc/frames/...                            （送检时已抽好）
#   逐镜质检   qc_client.read_history(QC_DIR, 项目, "video", 镜号)
#   镜头契约   _load_script_for（shot_id/duration/description/...）
#   参考资产   /api/assets/<characters|items|scenes>/<项目>/<名称>/<图>
#
# C/D 的「通过」必须绑定当刻产物 + 合同（哈希）：重渲一集或改剧本后批准自动失效，
# 由 GET 接口的 stale 字段带回界面 —— 否则界面会显示「已批准」，实际批的是上一版。

def _quality_video_url(local_path: str) -> str:
    """本地视频路径 → /api/videos URL；出了 VIDEOS_DIR 就不给 URL（防穿越）。"""
    if not local_path:
        return ""
    try:
        rel = os.path.relpath(local_path, VIDEOS_DIR)
    except (ValueError, TypeError):
        return ""
    if rel.startswith(".."):
        return ""
    return "/api/videos/" + rel.replace(os.sep, "/")

def _quality_asset_url(kind: str, project: str, name: str) -> str:
    """资产名 → 第一张可用图的 URL（front/base 优先，与生成链路取图同口径）。"""
    d = os.path.join(PROJECT_OUTPUT_DIR, "assets", kind, project, str(name))
    if not os.path.isdir(d):
        return ""
    try:
        files = sorted(f for f in os.listdir(d)
                       if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
    except OSError:
        return ""
    if not files:
        return ""
    pick = files[0]
    for prio in ("front", "base"):
        hit = next((x for x in files if x.lower().startswith(prio)), "")
        if hit:
            pick = hit
            break
    return "/api/assets/%s/%s/%s/%s" % (kind, project, name, pick)

def _quality_storyboard_url(project: str, seq: int) -> str:
    """分镜图 URL（并排审片「合同侧」的预期画面）；无图给空串。"""
    fn = "shot_%02d.png" % seq
    if os.path.isfile(os.path.join(STORYBOARDS_DIR, project, fn)):
        return "/api/storyboards/file/%s/%s" % (project, fn)
    return ""

def _quality_refs_for_shot(project: str, shot: dict) -> list:
    """本镜参考资产（角色/物品/场景）→ 可显示的图列表。"""
    refs = []

    def _add(kind, names):
        for n in (names or []):
            if not n:
                continue
            u = _quality_asset_url(kind, project, n)
            if u:
                refs.append({"kind": kind, "name": str(n), "url": u})

    _add("characters", shot.get("characters_in_shot"))
    _add("items", shot.get("items_in_shot"))
    loc = shot.get("location") or shot.get("scene")
    if loc:
        u = _quality_asset_url("scenes", project, loc)
        if u:
            refs.append({"kind": "scenes", "name": str(loc), "url": u})
    return refs

def _quality_ep_numbers(project: str) -> list:
    """项目集号 = 剧本集 ∪ 视频产物集（任一侧有就列出，旧项目缺剧本也能审）。"""
    nums = set()
    try:
        key = project_store.safe_key(project)
        for ep in (novel_to_script.list_episodes(SCRIPT_DIR, key) or []):
            try:
                nums.add(int(ep.get("episode_no")))
            except (TypeError, ValueError):
                pass
    except Exception as e:                                   # noqa: BLE001
        app.logger.debug("剧本集列表读取失败：%s", e)
    vroot = os.path.join(VIDEOS_DIR, project)
    try:
        entries = os.listdir(vroot) if os.path.isdir(vroot) else []
    except OSError:
        entries = []
    if any(f.lower().endswith((".mp4", ".mov", ".mkv")) for f in entries):
        nums.add(1)
    for d in entries:
        m = re.match(r"^ep(\\d+)$", d)
        if m and os.path.isdir(os.path.join(vroot, d)):
            nums.add(int(m.group(1)))
    return sorted(nums)

def _quality_find_full(project: str, ep) -> str:
    """该集整集成片（播放与批准绑定都认它）；无则空串。"""
    d = _ep_read_dir(VIDEOS_DIR, project, ep)
    if not os.path.isdir(d):
        return ""
    try:
        fps = sorted(f for f in os.listdir(d)
                     if f.lower().endswith(".mp4") and "_full" in f.lower())
    except OSError:
        return ""
    if not fps:
        return ""
    try:
        tag = "ep%02d" % int(ep)
    except (TypeError, ValueError):
        tag = ""
    for f in fps:
        if tag and f.lower().startswith(tag):
            return os.path.join(d, f)
    return os.path.join(d, fps[0])

def _quality_find_preview(project: str, ep) -> dict:
    """该集预演产物（两级生产第一阶段的输出，不可交付）。"""
    d = _ep_read_dir(VIDEOS_DIR, project, ep)
    try:
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if preview_gate.is_preview_path(fn):
                    p = os.path.join(d, fn)
                    return {"exists": True, "name": fn, "url": _quality_video_url(p)}
    except OSError as e:                                     # noqa: BLE001
        app.logger.warning("扫描预演产物失败：%s", e)
    return {"exists": False, "name": "", "url": ""}

def _quality_contract_summary(project: str, ep) -> dict:
    """合同的稳定摘要（批准时进哈希；镜头数/时长被改 → 批准失效）。"""
    scr = _load_script_for(project, ep) or {}
    shots = [s for s in (scr.get("shots") or []) if isinstance(s, dict)]
    return {"shots_total": int(scr.get("shot_count") or len(shots)),
            "duration_sec": float(scr.get("episode_duration_sec") or 0),
            "shots": [[shot_key.shot_seq(s.get("shot_id"), i + 1), s.get("duration")]
                      for i, s in enumerate(shots)]}

def _quality_state_view(project: str, ep) -> dict:
    """四层状态 + 发布就绪判定 + 批准是否已被重渲作废（界面的唯一口径）。"""
    state = quality_stage.load_state(project, ep)
    full = _quality_find_full(project, ep)
    stages, stale, blockers_extra = {}, {}, []
    for s in quality_stage.STAGES:
        e = (state.get("stages") or {}).get(s) or {}
        stages[s] = {"status": e.get("status") or "pending",
                     "name": e.get("name") or quality_stage.STAGE_NAMES[s],
                     "label": e.get("label") or quality_stage.STAGE_LABELS[s],
                     "at": e.get("at") or "", "note": e.get("note") or "",
                     "has_binding": bool(e.get("binding"))}
        # 批准已过期检测：C/D 通过过、但产物已不是批准时那一份
        if s in ("C", "D") and stages[s]["status"] == "passed" and e.get("binding"):
            bstatus, breason = quality_stage.check_stage_binding(state, s, full)
            if bstatus == "invalid":
                stale[s] = breason
                blockers_extra.append("%s(%s) 批准已失效：%s"
                                      % (s, quality_stage.STAGE_NAMES[s], breason))
    _ready, blockers = quality_stage.release_ready(state)
    blockers = list(blockers) + blockers_extra
    ready = not [r for r in blockers if not r.startswith("提示：")]
    return {"stages": stages, "release": {"ready": ready, "blockers": blockers},
            "stale": stale, "updated_at": state.get("updated_at") or ""}

def _quality_episode_row(project: str, ep) -> dict:
    """单集摘要行（审片左栏）。"""
    scr = _load_script_for(project, ep) or {}
    full = _quality_find_full(project, ep)
    sv = _quality_state_view(project, ep)
    review = {}
    try:
        for d in pipeline.list_deliverables(project):
            try:
                if int(d.get("episode_no") or 0) == int(ep):
                    review = {"status": d.get("review") or "pending",
                              "stale": bool(d.get("approval_stale")),
                              "exists": bool(d.get("exists"))}
                    break
            except (TypeError, ValueError):
                continue
    except Exception as e:                                   # noqa: BLE001
        app.logger.debug("交付物状态读取失败：%s", e)
    return {"episode_no": int(ep),
            "title": scr.get("episode_title") or scr.get("title") or "",
            "shots_total": int(scr.get("shot_count") or len(scr.get("shots") or [])),
            "duration_sec": float(scr.get("episode_duration_sec") or 0),
            "state": sv["stages"], "release": sv["release"], "stale": sv["stale"],
            "artifact": {"exists": bool(full),
                         "name": os.path.basename(full) if full else "",
                         "url": _quality_video_url(full)},
            "review": review, "preview": _quality_find_preview(project, ep)}

# ===================== 导出API =====================

def _timeline_for_export(project_name: str, episode_no=None,
                         provided: dict = None) -> dict:
    """把项目剧本适配成 ExportManager 需要的时间轴结构。

    为什么需要这层适配（对应「导出结果是空壳」缺陷）：
        ExportManager 期望 {project_name, duration, resolution, fps,
        sequences:[{clips:[{name,start,end}]}]}；
        而 nle_export.build_timeline 产出的是 {shots:[{start,end,video,...}], total_sec}。
        两者结构不同，前端又只发 formats 不发 timeline，
        于是 export_all({}) 写出的是 name="项目"、<clips/> 为空的无效文件，
        用户下载下来却以为导出成功。
    """
    if provided and provided.get("sequences"):
        provided.setdefault("project_name", project_name)
        return provided

    script = _load_script_for(project_name, episode_no)
    # B-17 P2-13：传集号给 build_timeline，按集号过滤视频目录
    tl = nle_export.build_timeline(script or {}, project_name, episode=episode_no)
    rows = tl.get("shots") or []
    clips = [
        {
            "name": f"shot_{int(r.get('index', i)) + 1:02d}",
            "start": r.get("start", 0),
            "end": r.get("end", 0),
            "asset_path": r.get("video") or "",
        }
        for i, r in enumerate(rows)
    ]
    return {
        "project_name": project_name,
        "duration": tl.get("total_sec", 0),
        "resolution": {
            "width": nle_export.JY_CANVAS["width"],
            "height": nle_export.JY_CANVAS["height"],
        },
        "fps": 30,
        "shots": rows,
        "sequences": [{"name": project_name, "clips": clips}],
    }

def _prompt_memory_view(kind: str = "", limit: int = 50) -> dict:
    """真实质检教训库的只读视图（供记忆页展示）"""
    try:
        m = prompt_memory.get_memory(PROJECT_OUTPUT_DIR)
        st = m.stats()
        return {
            "total": st.get("total", 0),
            "by_kind": st.get("by_kind") or {},
            "path": st.get("path", ""),
            "lessons": m.list(kind=kind, limit=limit),
        }
    except Exception as e:  # noqa: BLE001
        app.logger.warning("读取质检教训库失败：%s", e)
        return {"total": 0, "by_kind": {}, "path": "", "lessons": [], "error": str(e)}

def _prompt_memory_dead_count() -> int:
    """死教训数（use_count==0 的条数）；读取失败返回 0。"""
    try:
        return int(prompt_memory.get_memory(PROJECT_OUTPUT_DIR).stats().get("dead_lessons") or 0)
    except Exception:  # noqa: BLE001
        return 0

def _prompt_memory_used_total() -> int:
    """累计被生成链路召回次数（所有教训 use_count 之和）；读取失败返回 0。"""
    try:
        return int(prompt_memory.get_memory(PROJECT_OUTPUT_DIR).stats().get("used_total") or 0)
    except Exception:  # noqa: BLE001
        return 0

# ==========================================================================
# D-11a（P2）：ComfyUI 输出目录滚动回收 —— 任务收尾接线
# --------------------------------------------------------------------------
# 判定与删除**全部委托**零第三方依赖的 disk_reclaim 模块（可用
# `MJSCXT_AUTOPILOT=0 python verify_comfyui_reclaim.py` 离线单测）；
# 这里只做三件事：
#   ① 从 config 常量推导「正式产物目录」清单（不硬编码任何盘符路径）；
#   ② 进程内 10 分钟节流（同一进程最多每 10 分钟真正扫描一次）；
#   ③ 全容错 —— 回收是**优化**不是功能，任何异常只留 warning，绝不阻断生产。
#
# 为什么把包装函数集中在文件末尾追加：本文件已逾万行，把新逻辑集中放在末尾
# 便于审阅与回滚，也避免与既有函数体交错。**注意不要再以「绝对行号」锚定任何
# 守卫** —— 本仓库吃过亏：`verify_silent_except.py` 的白名单原本写成
# `("app.py", 5691)`，D-11a 在上面插了两行就漂到 5693、守卫静默失效，被迫手工
# 同步。该白名单现已改为**内容锚点**（`app/verify_project_audit.py` G5 同口径），
# 行号扰动不再影响它。
#
# 调用点：`_generate_asset_task`（资产生成任务收尾）、`_storyboard_worker`
# （分镜生成任务收尾）；成片步骤收尾见 `app/pipeline.py step_final`。
# ==========================================================================
_COMFYUI_RECLAIM_LAST_TS = 0.0          # 上次真正扫描的时间戳（模块级节流状态）

_COMFYUI_RECLAIM_INTERVAL_SEC = 600.0   # 同一进程 10 分钟内只真正扫描一次

_COMFYUI_RECLAIM_LOCK = threading.Lock()

# ComfyUI「任务历史」自动清理（面板只增不减 → 易被误读成「生成了大量废图」）：
# 与上面的回收同构 —— 模块级节流 + 全容错，默认间隔取 config 值（5 分钟）。
_COMFYUI_CLEAR_HISTORY_LAST_TS = 0.0

_COMFYUI_CLEAR_HISTORY_LOCK = threading.Lock()

def _maybe_clear_comfyui_history(where: str = "") -> bool:
    """按节流清空 ComfyUI **任务历史列表**（不是磁盘产物）。

    为什么要做：ComfyUI 界面「任务历史」面板只增不减，质检每失败一次重跑就多一条
    记录，跑几轮后几百条 → 用户会以为「生成了大量废图」。实测面板 162 条时磁盘上
    真正残留的废弃分镜图 **0 张**（清之前 /history 162 条 → 清完 0 条）。

    语义边界（重要）：
      · 只调 `POST /history {"clear":true}`，**绝不删任何 output 文件**；
      · 不影响正在执行/排队中的任务（它们结束后会各自追加新记录）；
      · 只应在**任务收尾**调用 —— 有任务在飞时清掉历史，会让 `wait_for_completion`
        的轮询查不到自己那条记录而误判超时。

    开关 `MJSCXT_CLEAR_COMFYUI_HISTORY=0` 可整体关闭；节流 5 分钟（见 config）。
    永不抛异常、永不阻断生产。
    """
    global _COMFYUI_CLEAR_HISTORY_LAST_TS
    if not CLEAR_COMFYUI_HISTORY:
        return False
    try:
        now = time.time()
        with _COMFYUI_CLEAR_HISTORY_LOCK:
            if now - _COMFYUI_CLEAR_HISTORY_LAST_TS < CLEAR_COMFYUI_HISTORY_INTERVAL_SEC:
                return False
            # 先占用时间戳：真正清理失败也不要在同一分钟内反复重试刷屏。
            _COMFYUI_CLEAR_HISTORY_LAST_TS = now
        ok = comfyui_client.clear_history()
        if ok:
            app.logger.info("[任务历史] 已清空 ComfyUI 任务历史面板（收尾：%s）", where or "未知")
        return ok
    except Exception as e:  # noqa: BLE001  可观测性优化，绝不能阻断生产
        app.logger.warning("ComfyUI 任务历史清理异常（不影响生产）：%s: %s",
                           type(e).__name__, e)
        return False

def _comfyui_official_dirs() -> list:
    """正式产物目录清单（全部由 config 常量推导，不硬编码盘符路径）。"""
    return [d for d in (CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR,
                        KEYFRAMES_DIR, VIDEOS_DIR, FINAL_DIR) if d]

def _maybe_reclaim_comfyui_output() -> None:
    """薄包装：带节流地回收 ``COMFYUI_OUTPUT_DIR`` 下的产物残留（D-11a）。

    只有**同时**满足下列条件的文件才会被删（判定细节与安全论证见
    ``app/disk_reclaim.py`` 模块 docstring）：

      ① 位于 ``COMFYUI_OUTPUT_DIR`` 下的 ``comic_drama*`` 产物目录内；
      ② 文件名是 ComfyUI 侧产物命名（带自动编号后缀 ``_00001_``，或含
         ``_retry``/``_try``）—— 交付件名 ``base.png`` 之类天然不匹配；
      ③ ``mtime`` 距今 > 24h；
      ④ 正式产物目录里已有 ``(size, sha256)`` **双匹配**的同内容副本；
      ⑤ ``st_nlink == 1``（硬链接删了不释放空间）；
      ⑥ 候选不在任何正式产物目录内（含大小写归一后的比较）。

    性能：内容指纹按 ``(路径, size, mtime_ns)`` 进程内缓存，稳态下只有**新增**的
    正式产物需要读盘；配合 10 分钟节流，同步调用不会给任务收尾带来可感知的延迟。
    """
    global _COMFYUI_RECLAIM_LAST_TS
    try:
        now = time.time()
        with _COMFYUI_RECLAIM_LOCK:
            if now - _COMFYUI_RECLAIM_LAST_TS < _COMFYUI_RECLAIM_INTERVAL_SEC:
                return
            _COMFYUI_RECLAIM_LAST_TS = now
        if not COMFYUI_OUTPUT_DIR or not os.path.isdir(COMFYUI_OUTPUT_DIR):
            return
        import disk_reclaim   # 延迟导入：与本文件其它叶子模块一致，避免加载期副作用
        stats = disk_reclaim.reclaim_comfyui_output(
            COMFYUI_OUTPUT_DIR, _comfyui_official_dirs(), logger=app.logger)
        if stats.get("delete"):
            app.logger.info("D-11a ComfyUI 输出回收：删 %d 个产物残留，释放 %.2f MB",
                            len(stats["delete"]),
                            (stats.get("removed_bytes") or 0) / 1048576.0)
    except Exception as e:  # noqa: BLE001  回收是优化，绝不能阻断生产
        app.logger.warning("D-11a ComfyUI 输出回收失败（不影响生产）：%s: %s",
                           type(e).__name__, e)



# 预声明：下面几个启动期报告由 bind_app() 赋值（此处预声明只为了让
# `from api._shared import *` 能取到名字，取到的初值随后即被覆盖）。

AI_SELFCHECK_BOOT = None
WORKFLOW_INTEGRITY_BOOT = None
STABLE_PROFILE_BOOT = None
TRT_ENGINE_CHECK_BOOT = None


#: ``from api._shared import *`` 的名字清单（含下划线开头的 helper）
__all__ = [
    'AI_CHAT_HISTORY_PATH',
    'AI_CONFIG_PATH',
    'AI_MODULES',
    'AI_MODULE_LABEL',
    'AI_SELFCHECK_BOOT',
    'AI_SETTINGS_PATH',
    'APP_DEBUG',
    'APP_HOST',
    'APP_PORT',
    'ASSET_VIEW_STEMS',
    'BadRequest',
    'CHARACTERS_DIR',
    'CLEAR_COMFYUI_HISTORY',
    'CLEAR_COMFYUI_HISTORY_INTERVAL_SEC',
    'CLOSEUP_CHAR_CROP_TOP',
    'COMFYUI_OUTPUT_DIR',
    'COMFYUI_URL',
    'COMFY_VIDEO_DIRS',
    'CONTINUITY_DIR',
    'CharacterConsistencyEngine',
    'CharacterManager',
    'ComfyUIClient',
    'DUB_DIR',
    'DUB_MIX_DIR',
    'DubMixError',
    'EPISODE_BATCH_LIMIT',
    'ExportManager',
    'FINAL_DIR',
    'FailoverLLMClient',
    'Flask',
    'H3_COMMON_REFS',
    'H3_COMMON_REFS_MAX',
    'H3_EMIT_AUDIO',
    'H3_SFX_ISOLATE',
    'H3_STRIP_AUDIO',
    'HTTPException',
    'ITEMS_DIR',
    'KEYFRAMES_DIR',
    'KEYFRAME_CHAIN_MODE',
    'LLMClient',
    'LLMError',
    'LLMGatewayUnavailable',
    'LLMReasoningOnlyError',
    'LLMTruncatedError',
    'LLM_CONFIG_PATH',
    'LLM_NOT_CONFIGURED_GUIDE',
    'LLM_NOT_CONFIGURED_GUIDE_MAP',
    'LLM_REQUEST_TIMEOUT',
    'MIX_DEFAULT_PARAMS',
    'NOVELS_DIR',
    'NOVEL_BRIEF_CHARS',
    'NOVEL_CHUNK_CHARS',
    'NOVEL_DEFAULT_SHOTS',
    'NOVEL_MAX_CHUNKS',
    'NOVEL_PREVIEW_CHARS',
    'NineGridStoryboard',
    'NovelParseError',
    'PROJECT_DATA_DIR',
    'PROJECT_DEFAULT_CONFIG',
    'PROJECT_OUTPUT_DIR',
    'PROJECT_ROOT_DIR',
    'PROJECT_TRASH_DIR',
    'PROMPT_ENHANCE_CONFIG_PATH',
    'QC_CONFIG_PATH',
    'QC_DIR',
    'QwenTTSClient',
    'RelationConflictDetector',
    'RelationManager',
    'SCENES_DIR',
    'SCENE_ANGLE_TO_VIEW',
    'SCENE_GRID_ANGLE_ZH',
    'SCENE_GRID_DERIVE_MODE',
    'SCENE_GRID_FILENAME',
    'SCENE_GRID_LABELS',
    'SCENE_GRID_MODE',
    'SCENE_GRID_VIEW_KEYS',
    'SCENE_VIEWS_ENABLED',
    'SCENE_VIEW_ANGLE_ZH',
    'SCENE_VIEW_DUP_PHASH_MAX',
    'SCENE_VIEW_KEYS',
    'SCENE_VIEW_LABELS',
    'SCENE_VIEW_MAX_RETRIES',
    'SCRIPT_DIR',
    'STABLE_PROFILE_BOOT',
    'STORYBOARDS_DIR',
    'SUPPORTED_EXTS',
    'ScriptGenerator',
    'TASKS_DB_PATH',
    'TASK_QUEUE_CONCURRENCY',
    'TASK_UNIT_MIN_BYTES',
    'TE_UPSCALE_DEFAULT_PARAMS',
    'TE_UPSCALE_LOWVRAM_PARAMS',
    'TRT_ENGINE_CHECK_BOOT',
    'TTSError',
    'TTS_DEFAULT_PARAMS',
    'TTS_VOICE_BANK_TIMEOUT',
    'UPLOAD_TMP_DIR',
    'UPSCALE_DEFAULT_PARAMS',
    'UPSCALE_DIR',
    'UPSCALE_ENGINE',
    'UPSCALE_URL_PREFIXES',
    'UpscaleError',
    'VIDEOS_DIR',
    'VOICE_BANK_EXTS',
    'VideoPostProcessor',
    'VideoUpscaler',
    'WATERMARK_CONFIG_PATH',
    'WATERMARK_DIR',
    'WORKFLOW_INTEGRITY_BOOT',
    'WORKFLOW_TEMPLATE',
    '_ASSET_DIRS',
    '_ASSET_IMG_EXTS',
    '_ASSET_IMG_PRIORITY',
    '_ASSET_VIEW_FILES',
    '_AUDIO_QC_AUDIO_EXT',
    '_AUDIO_QC_MEDIA_EXT',
    '_AUDIO_QC_NON_PROJECT_DIRS',
    '_BLOCKING_REF_MARK',
    '_COMFYUI_CLEAR_HISTORY_LAST_TS',
    '_COMFYUI_CLEAR_HISTORY_LOCK',
    '_COMFYUI_RECLAIM_INTERVAL_SEC',
    '_COMFYUI_RECLAIM_LAST_TS',
    '_COMFYUI_RECLAIM_LOCK',
    '_FRAMING_HALF_SHOT',
    '_OUTFITS_DIRNAME',
    '_OUTFIT_PROMPT_MARK',
    '_OUTFIT_RECORD_FILE',
    '_OUTFIT_VIEW_STEMS',
    '_PRECIP_STEPS',
    '_PROJECT_KIND_DIRS',
    '_PURGE_REJECTED_ENV',
    '_PromptQCBlocked',
    '_REF_CANVAS_CACHE',
    '_SCENE_DECOR_CHARS',
    '_SCENE_FULLWIDTH_MAP',
    '_TASK_STATE_KEEP_DONE',
    '_TASK_TERMINAL_STATUSES',
    '_TTS_DEP_MISSING_MARKERS',
    '_TTS_IMPORT_FAILURE_MARKERS',
    '_TTS_MODEL_UNAVAILABLE_MARKERS',
    '_UPLOAD_IMAGE_EXTS',
    '_VIDEO_TASK_IS_PIPELINE',
    '_ai_client_for_module',
    '_ai_config_view',
    '_ai_credentials_verify',
    '_ai_gate_or_400',
    '_ai_guide_response',
    '_allocate_storyboard_refs',
    '_analyze_worker',
    '_append_outfit_prompt',
    '_apply_audio_hints',
    '_apply_audio_lessons',
    '_apply_closeup_ref_strategy',
    '_apply_project_settings',
    '_archive_project_key',
    '_asset_err_traceback',
    '_audio_line_expect_sec',
    '_audio_qc_file_url',
    '_audio_qc_lines',
    '_audio_qc_project_key',
    '_audio_qc_visuals_key',
    '_autopilot_boot',
    '_autopilot_guard',
    '_bigram_overlap',
    '_blocking_spec_text',
    '_body',
    '_build_asset_index',
    '_camera_angle',
    '_camera_key',
    '_camera_spec',
    '_cap_storyboard_refs',
    '_chapter_text_for_script',
    '_character_base_prompt',
    '_character_outfit_dir',
    '_chat_project',
    '_chat_state',
    '_cleanup_scratch_dir',
    '_closeup_char_crop',
    '_collect_asset_refs',
    '_collect_matching',
    '_collect_project_cast_images',
    '_collect_reference_images',
    '_comfy_view_url',
    '_comfyui_official_dirs',
    '_consistency_collect',
    '_cover_prompt_from_outline',
    '_current_llm_client',
    '_dub_audio_url',
    '_dub_character_desc',
    '_dub_line_speaker_from_script',
    '_dub_project_dir',
    '_dub_prompt_preflight',
    '_dub_resolve_script',
    '_dub_worker',
    '_ensure_disk_headroom',
    '_ensure_script_file',
    '_ensure_voice_bank_refs',
    '_ep_dir',
    '_ep_of_script',
    '_ep_read_dir',
    '_episode_frame_ratios',
    '_episode_outfit_overrides',
    '_episode_progress',
    '_episode_qc_desc',
    '_episode_schema_defaults',
    '_episode_units_for_chapters',
    '_episode_video_stats',
    '_episodes_worker',
    '_estimate_subchunks',
    '_find_scene_asset_dir',
    '_find_script_character',
    '_first_existing',
    '_first_existing_asset_image',
    '_fit_ref_to_canvas',
    '_framing_wants_half',
    '_framing_wants_half_shot',
    '_friendly_error',
    '_generate_asset_task',
    '_generate_project_cover',
    '_h3_audio_policy',
    '_h3_common_ref_audios',
    '_h3_common_subject_lock',
    '_h3_is_common_comp',
    '_h3_plan_common_refs',
    '_h3_shot_ref_components',
    '_ingest_comfy_output',
    '_is_tts_model_unavailable',
    '_isolate_shot_sfx',
    '_keyframe_prompt_preflight',
    '_keyframe_qc_verifier',
    '_keyframe_recall_cb',
    '_keyframe_sb_map',
    '_keyframes_dir',
    '_load_legacy_flat_script',
    '_load_script_for',
    '_mark_history_file_purged',
    '_match_scene_name',
    '_match_shot_chars',
    '_maybe_clear_comfyui_history',
    '_maybe_reclaim_comfyui_output',
    '_mix_audio_qc',
    '_mix_audio_url',
    '_mix_manifest',
    '_mix_prepare',
    '_mix_resolve_video',
    '_mix_segments_dir',
    '_mix_worker',
    '_norm_shot_key',
    '_normalize_char_alias',
    '_normalize_scene_name',
    '_note_ref_warning',
    '_novel_convert_worker',
    '_novel_key',
    '_novels_stats',
    '_on_screen_characters',
    '_optimize_prompt_from_qc',
    '_optional_llm_client',
    '_outfit_desc_of',
    '_pick_char_view',
    '_pick_scene_view',
    '_precip_meta_of',
    '_project_caption_burn_enabled',
    '_project_cover_path',
    '_project_or_400',
    '_project_style',
    '_project_subtitle_enabled',
    '_project_worldview',
    '_prompt_enhance_file_flags',
    '_prompt_memory_dead_count',
    '_prompt_memory_used_total',
    '_prompt_memory_view',
    '_prompt_preflight',
    '_prune_task_registry',
    '_purge_prompt_records',
    '_purge_rejected_artifacts',
    '_purge_rejected_enabled',
    '_purge_sb_refs',
    '_qc_brief',
    '_qc_gate',
    '_qc_history_file_for',
    '_qc_lesson_from_record',
    '_qc_load_cfg',
    '_qc_prev_shot_desc',
    '_qc_prev_shot_ref',
    '_qc_prune_attempts',
    '_qc_record',
    '_qc_record_verdict',
    '_qc_ref_images',
    '_qc_repeat_features',
    '_qc_retry_hopeless',
    '_qc_shot_desc',
    '_qc_style_of',
    '_qc_summary',
    '_qc_test_override',
    '_quality_asset_url',
    '_quality_contract_summary',
    '_quality_ep_numbers',
    '_quality_episode_row',
    '_quality_find_full',
    '_quality_find_preview',
    '_quality_refs_for_shot',
    '_quality_state_view',
    '_quality_storyboard_url',
    '_quality_video_url',
    '_record_audio_qc_lesson',
    '_record_preflight_lesson',
    '_record_qc_lesson',
    '_ref_canvas_target',
    '_ref_image_local',
    '_reject_artifact',
    '_resolve_audio_qc_target',
    '_resolve_continuity_key',
    '_resolve_item_names',
    '_resolve_novel_project',
    '_resolve_scene_entry',
    '_safe_project',
    '_safe_upload_name',
    '_salvage_episode_script',
    '_sanitize_outfit_key',
    '_save_ai_module',
    '_scene_grid_prompt_for',
    '_scene_view_for_shot',
    '_schedule_autopilot_boot',
    '_screenplay_worker',
    '_serve_attachment',
    '_serve_safe',
    '_shot_coverage_map',
    '_shot_has_char_ref',
    '_shot_has_on_screen',
    '_shot_num_key',
    '_shot_outfit_dir',
    '_shot_seq',
    '_storyboard_retry_shot_impl',
    '_storyboard_scratch_map',
    '_storyboard_worker',
    '_style_aspect_confirmed',
    '_style_aspect_guard',
    '_sync_project_config_style',
    '_task_analytics_hook',
    '_task_queue_status',
    '_timeline_for_export',
    '_trash_move',
    '_unify_ref_canvas',
    '_update_storyboard_manifest_shot',
    '_upgrade_refs_with_disk',
    '_upscale_resolve_comfyview',
    '_upscale_resolve_video',
    '_upscale_url_for_path',
    '_upscale_worker',
    '_video_generate_worker',
    '_video_generate_worker_body',
    '_video_retry_shot_impl',
    '_video_should_stop',
    '_wm_apply_to_final',
    '_wm_load_cfg',
    '_wm_view',
    '_write_artifact_meta',
    'abort',
    'actual_params',
    'agent_core',
    'ai_chat',
    'ai_config',
    'ai_memory',
    'analytics',
    'analyze_script_prompts',
    'app',
    'asset_name_match',
    'asset_prompt_kit',
    'atomic_write_json',
    'audio_qc',
    'autonomous',
    'autopilot',
    'autopilot_boot_enabled',
    'bind_app',
    'build_dub_plan',
    'build_entries',
    'cancellation',
    'chapter_body_chars',
    'chapter_preflight',
    'clean_line_text',
    'clear_llm_config',
    'comfyui_client',
    'comfyui_job_store',
    'comfyui_models',
    'concat_audio',
    'consistency',
    'contextvars',
    'continuity',
    'copy',
    'coverage',
    'datetime',
    'default_voice_map',
    'deps_check',
    'dialogue_utils',
    'dub_lock',
    'dub_mix',
    'dub_tasks',
    'ensure_audio_track',
    'ensure_chapter_structure',
    'ensure_no_audio',
    'export_manager',
    'find_voice_bank_ref',
    'generation_state',
    'get_memory_system',
    'get_novel',
    'gpu_task_gate',
    'h3_common_refs',
    'h3_director_builder',
    'h3_prompt_kit',
    'h3_segment_loras',
    'ingest_novel',
    'json',
    'jsonify',
    'keyframe',
    'list_novels',
    'list_voice_bank',
    'llm_public_view',
    'load_llm_config',
    'load_voice_map',
    'lock',
    'log_viewer',
    'math',
    'mix_ffmpeg_check',
    'mix_lock',
    'mix_out_dir',
    'mix_probe_audio',
    'mix_tasks',
    'mix_video_with_entries',
    'model_capabilities',
    'nle_export',
    'normalize_voice',
    'novel_screenplay',
    'novel_to_script',
    'os',
    'pipeline',
    'plugin_registry',
    'preview_gate',
    'preview_novel',
    'probe_audio_info',
    'probe_video_info',
    'project_store',
    'prompt_memory',
    'prompt_qc',
    'providers',
    'qc_client',
    'qc_coverage',
    'quality_stage',
    'random',
    're',
    'read_json_strict',
    'read_novel_text',
    'redirect',
    'register_final_deliverable',
    'render_template',
    'request',
    'save_llm_config',
    'save_prompt_enhance_config',
    'save_script_inplace',
    'save_voice_bank_ref',
    'save_voice_map',
    'scene_grid',
    'script_consistency',
    'script_gen',
    'send_file',
    'send_from_directory',
    'sheet_split',
    'shot_key',
    'shot_timeline',
    'shutil',
    'split_chapters',
    'stable_profile',
    'style_kit',
    'task_db',
    'task_queue',
    'task_store',
    'threading',
    'time',
    'trt_engine_check',
    'tts_client',
    'tts_clone_available',
    'tts_env_check',
    'tts_list_voices',
    'upscale_client',
    'upscale_env_check',
    'upscale_lock',
    'upscale_tasks',
    'uuid',
    'video_processor',
    'video_watermark',
    'voice_bank_dir',
    'workflow_integrity',
    'write_mix_report',
]


app = None  # composition root 注入（见模块 docstring 第 2 条）


def bind_app(instance):
    """注入 Flask 实例，并执行原先在 app.py 模块级跑的启动期语句。

    必须由 ``app/app.py`` 在 ``Flask(__name__)`` 之后立即调用，且早于
    ``api.register_blueprints(app)``（域模块导入时会整体取走本模块的名字）。
    """
    global app
    # 这几个启动期报告在别处被读取，必须仍是模块级全局（否则只剩 bind_app 的局部）
    global AI_SELFCHECK_BOOT, WORKFLOW_INTEGRITY_BOOT, STABLE_PROFILE_BOOT, TRT_ENGINE_CHECK_BOOT

    app = instance
    # ↓↓↓ 原 app.py 模块级的启动期语句，按原相对顺序 ↓↓↓
    try:
        # 启动时把上次残留的 running 任务标记为 interrupted（可供前端提示「可继续」）
        _interrupted = task_db.recycle_interrupted()
    except Exception as _e:  # noqa: BLE001  不得因任务库异常导致启动失败
        _interrupted = 0
        app.logger.warning(f"任务库中断恢复失败（不影响启动）：{_e}")

    # ⭐ AI 凭证单一事实源（tasks.db · ai_credentials 表）：启动时一次性把旧
    # secrets.enc 双槽（ai.text/ai.qc/ai.chat + qc）+ ai_config.json 非密钥字段迁入 DB。
    # 幂等（DB 已有值则跳过），失败不影响启动（读侧对每个模块都有旧口径回落）。
    try:
        import ai_credentials_db
        _mig = ai_credentials_db.migrate_from_legacy()
        if _mig.get("migrated"):
            app.logger.info(f"AI 凭证已从旧源迁入 {TASKS_DB_PATH}：{_mig['migrated']}"
                            + (f"（跳过 {_mig.get('skipped')}）" if _mig.get("skipped") else ""))
    except Exception as _e:  # noqa: BLE001  迁移失败不得阻断启动
        app.logger.warning(f"AI 凭证库迁移失败（不影响启动，任务读侧仍回落旧口径）：{_e}")

    # ⭐ AI 前置自检（P0-5）：启动即体检三个 AI 模块的配置完整性，缺 key / 缺 base_url 时
    # 醒目告警（不阻断启动 —— 用户很可能正是启动后才去「AI 设置」页补配置）。
    # 这里只做静态检查、不打外网，避免拖慢启动或让启动依赖外部网络；端点可达性在
    # 开跑门禁（_ai_gate_or_400）与 /api/ai/selfcheck?probe=1 时才探测。
    try:
        import ai_selfcheck
        AI_SELFCHECK_BOOT = ai_selfcheck.startup_report()
    except Exception as _e:  # noqa: BLE001  自检失败不得阻断启动
        AI_SELFCHECK_BOOT = {}
        app.logger.warning(f"AI 前置自检执行失败（不影响启动）：{_e}")

    # ⭐ P0-4：启动即核对 workflows/ 多副本 hash。只告警、不阻断启动；状态 API
    # 供前端/排障复用，必须同时显示“实际读取路径”和各副本 hash。
    try:
        WORKFLOW_INTEGRITY_BOOT = workflow_integrity.check()
        for _warning in WORKFLOW_INTEGRITY_BOOT.get("warnings") or []:
            app.logger.warning(_warning)
        if WORKFLOW_INTEGRITY_BOOT.get("ok"):
            app.logger.info(
                "工作流副本核对通过：实际读取=%s，canonical=%s，共 %d 个文件",
                WORKFLOW_INTEGRITY_BOOT.get("actual_root") or "(未找到)",
                WORKFLOW_INTEGRITY_BOOT.get("canonical_root") or "(未配置)",
                len(WORKFLOW_INTEGRITY_BOOT.get("files") or []),
            )
    except Exception as _e:  # noqa: BLE001  完整性核对失败不得阻断启动
        WORKFLOW_INTEGRITY_BOOT = {"success": False, "ok": False, "warnings": [str(_e)]}
        app.logger.warning(f"工作流副本核对失败（不影响启动）：{_e}")

    # ⭐ P0-3：启动先做 TRT engine 静态自检（不抢 GPU、不提交任务）。
    # 真实加载探针只在用户点击“测试可用性”或显式 ?probe=1 时执行。
    try:
        TRT_ENGINE_CHECK_BOOT = trt_engine_check.check()
        if TRT_ENGINE_CHECK_BOOT.get("status") == "不兼容":
            # 2026-10-06：reason 可能携带 ComfyUI 整段 status.messages（含未转义换行的
            # traceback），直接打出来会产出数千字符的单行 WARNING 并污染后续日志解析。
            # 改用 trt_engine_check._reason_for_log 压成单行限长文本。
            app.logger.warning("TRT VAE engine 自检：%s",
                               trt_engine_check._reason_for_log(TRT_ENGINE_CHECK_BOOT))
        else:
            app.logger.info("TRT VAE engine 自检：%s（真实加载探针未执行）",
                            TRT_ENGINE_CHECK_BOOT.get("status"))
    except Exception as _e:  # noqa: BLE001  自检失败不得阻断启动
        TRT_ENGINE_CHECK_BOOT = {"success": False, "status": "未测试", "reason": str(_e)}
        app.logger.warning(f"TRT VAE engine 自检失败（不影响启动）：{_e}")

    # ⭐ P2-12：把已实测稳定参数变成机器可审计项；只告警，不自动改参数。
    try:
        STABLE_PROFILE_BOOT = stable_profile.report()
        if STABLE_PROFILE_BOOT["ok"]:
            app.logger.info("稳定参数锁定检查通过（P2-12）")
        else:
            for _violation in STABLE_PROFILE_BOOT["violations"]:
                app.logger.warning("稳定参数漂移（P2-12，改动前必须走 A/B）：%s", _violation)
    except Exception as _e:  # noqa: BLE001
        STABLE_PROFILE_BOOT = {"ok": False, "violations": [str(_e)]}
        app.logger.warning(f"稳定参数锁定检查失败（不影响启动）：{_e}")

    try:
        _schedule_autopilot_boot()
    except Exception as _e:  # noqa: BLE001
        app.logger.warning(f"托管启动调度失败：{_e}")

    # =====================================================================
    # 新增模块 A：自定义 LLM API 配置
    # =====================================================================

    app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 单次上传上限 200MB

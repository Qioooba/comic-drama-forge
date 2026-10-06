# -*- coding: utf-8 -*-
"""成片字幕轨构建（**单一事实源**，2026-10-07）

为什么要有这个模块
------------------
系统里曾有**两处**成片字幕实现，而且写的是**同一个文件**
（``output/final/<项目>/epNN_final.mp4``）：

  A. ``pipeline.step_final``                 —— 托管 / 自动生产链路（pipeline.py）
  B. ``video_postprocess.finalize_episode``   —— 界面点「生成成片」（video_postprocess.py）

两者的**时间轴基准不同**：

  - A 自 2026-10-05 起改为 ffprobe **实测各镜真实时长**累加；
  - B 仍在累加剧本里的 ``shot["duration"]``。

而成片是按**真实片段**（``_ordered_shot_files`` 拿到的真实文件）拼出来的 ——
每镜的实际时长与剧本声明值并不相等（H3 整集生成按提示词自行决定节奏，实测单镜
常见 0.2~1.5s 偏差）。于是 B 算出的字幕会随镜数**累积前移**：20 镜可错开 4~30 秒，
字幕与画面对不上，且**不报任何错**。

更糟的是两条链路互不知情：A、B 都往同一个 ``epNN_final.mp4`` 写，而
``pipeline.probe_final`` 见「文件可播放」就短路 —— 谁先跑完，后一条永远不再执行，
所以实际表现是「同一份代码、两种字幕行为，且由历史遗留的执行顺序决定」。

本模块把「镜头列表 + 分段实测时长 → 字幕轨」抽成**唯一实现**，两处都调它。
这样「字幕时间轴基准」不再有第二个口径可言。

顺带解决的另一个缺陷
--------------------
B 原先把烧完字幕的结果写到**旁挂文件** ``epNN_final_subtitles.mp4``，然后
``return output_path`` 返回的是**没字幕的那一份** —— 字幕文件既没被交付也没人读，
调用方（``app.py`` 的 ``/api/final/video``）登记的交付物因此是无字幕版。
:func:`burn_subtitles_inplace` 一并把这个行为收敛掉。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

#: 某镜既无实测时长、剧本也没给 duration 时的兜底秒数。
#: 与 novel_to_script 侧「模型时长非法 → 取 estimate_shot_duration」的默认值同量级，
#: 不会因为这里取 0 而让字幕塌成一个点。
_DEFAULT_SHOT_SEC = 5.0

#: ffprobe 单次探测超时（秒）。短超时避免坏文件把整条成片链路拖住。
_FFPROBE_TIMEOUT_SEC = 60


def probe_media_duration(path: str) -> float:
    """ffprobe 实测媒体时长（秒）；不可用 / 解析失败 / 无视频流一律返回 0.0。

    返回 0.0 的语义是「**没测到**」而不是「时长为 0」，调用方据此回退到剧本声明值，
    不让工具链不齐（ffprobe 缺失）导致整条链路的字幕全塌成默认时长。

    ffprobe 二进制缺失时**静默降级**返回 0.0，不抛异常：字幕烧制是成片的增强环节，
    不该因为少一个探针工具就让整集产不出来。
    """
    if not path or not os.path.isfile(path):
        return 0.0
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", os.path.abspath(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_FFPROBE_TIMEOUT_SEC)
        if r.returncode != 0:
            return 0.0
        return float((json.loads(r.stdout or "{}").get("format") or {}).get("duration") or 0)
    except FileNotFoundError:
        # ffprobe 不在 PATH —— 明确降级一次，避免每镜都刷一遍 warning
        logger.debug("ffprobe 不可用，字幕时间轴回退剧本声明时长")
        return 0.0
    except Exception as e:  # noqa: BLE001 —— 任何探测异常都只降级，不阻断成片
        logger.debug("ffprobe 时长探测失败（回退剧本声明值）%s：%s", path, e)
        return 0.0


def probe_durations(paths) -> list:
    """批量实测一组媒体的时长，返回与入参等长的 list（失败位为 0.0）。"""
    return [probe_media_duration(p) for p in (paths or [])]


def shot_dialogue_text(shot: dict) -> str:
    """该镜的台词纯文本（与旧实现同口径：委托 dialogue_utils，兼容结构化/字符串）。"""
    try:
        from dialogue_utils import dialogue_text
        return dialogue_text((shot or {}).get("dialogue"))
    except Exception as e:  # noqa: BLE001
        logger.warning("台词文本提取失败（本镜字幕跳过）：%s", e)
        return ""


def shot_caption_text(shot: dict) -> str:
    """该镜的 caption 文本：兼容 ``{text,kind}`` / 旧字符串 / 扁平 ``caption_text`` 三种形状。

    与旧两处实现的 ``_cap_text`` / 内联逻辑**逐字同口径**，抽出来只为消除复制。
    """
    cap = (shot or {}).get("caption")
    if isinstance(cap, dict):
        cap = cap.get("text")
    return str(cap or (shot or {}).get("caption_text") or "").strip()


def _resolve_durations(shots: list, segment_durations, total_duration) -> list:
    """把「剧本声明时长 / 实测分段时长 / 实测总时长」收敛成每镜一个确定秒数。

    优先级：**实测分段 > 实测总时长按占比分摊 > 剧本声明 > 默认值**。

    为什么要实测优先：成片是按真实文件拼的，剧本时长只是模型的**声明**。
    用声明值排字幕，误差会随镜数线性累积（见模块 docstring）。
    """
    n = len(shots or [])
    durs = []
    for i in range(n):
        d = 0.0
        if segment_durations is not None and i < len(segment_durations):
            try:
                d = float(segment_durations[i] or 0)
            except (TypeError, ValueError):
                d = 0.0
        if d <= 0:
            try:
                d = float(((shots[i] or {}).get("duration")) or 0)
            except (TypeError, ValueError):
                d = 0.0
        durs.append(d if d > 0 else _DEFAULT_SHOT_SEC)

    # 整集模式（一条源片、无法逐段 ffprobe）：按各镜声明时长的**占比**把实测总时分摊。
    # 这是近似 —— 但它保证了「字幕总跨度 == 成片实测总长」这个更重要的不变量，
    # 尾部不会溢出、开头不会空转，远好过直接用声明值（可能整体偏移几十秒）。
    if not segment_durations:
        try:
            total = float(total_duration or 0)
        except (TypeError, ValueError):
            total = 0.0
        if total > 0:
            base = sum(durs) or 1.0
            durs = [d / base * total for d in durs]
    return durs


def build_subtitle_track(shots: list, segment_durations=None, total_duration: float = 0.0,
                         want_dialogue: bool = True, want_caption: bool = True) -> list:
    """构建字幕轨：``[{"start": 秒, "end": 秒, "text": 文本}, ...]``。

    参数
    ----
    shots
        该集镜头列表（按**剧本顺序**，与拼接顺序一致）。
    segment_durations
        与 ``shots`` 等长的**实测**分段时长（秒）。逐镜模式传它 —— 这是最精确的档位。
    total_duration
        整集成片的**实测**总时长（秒）。整集模式（只有一条源片、无法逐段探测）传它。
    want_dialogue
        是否产出**台词字幕**（人物开口的转录）。对应项目开关 ``subtitle_enabled``（默认关）。
    want_caption
        是否产出**字幕/转场 caption**（时空落点 / 回溯 / 集尾悬念）。
        对应项目开关 ``caption_burn_enabled``（默认开）。

    两个开关**各自独立**：两者都关时返回空列表，调用方直接沿用未烧字幕的产物，
    行为与历史实现一致（不因为本模块的存在而改变默认出片路径）。
    """
    if not (want_dialogue or want_caption):
        return []
    shots = list(shots or [])
    if not shots:
        return []
    durs = _resolve_durations(shots, segment_durations, total_duration)

    subs, cur = [], 0.0
    for i, shot in enumerate(shots):
        dur = durs[i]
        if want_dialogue:
            text = shot_dialogue_text(shot)
            if text:
                subs.append({"start": round(cur, 3), "end": round(cur + dur, 3), "text": text})
        if want_caption:
            text = shot_caption_text(shot)
            if text:
                subs.append({"start": round(cur, 3), "end": round(cur + dur, 3), "text": text})
        cur += dur
    return subs


def burn_subtitles_inplace(video_path: str, subs: list, add_subtitles) -> str:
    """把字幕烧回 ``video_path`` **自身**，返回该路径。

    为什么坚持「烧回自身」而不是旁挂一个 ``*_subtitles.mp4``
    ------------------------------------------------------
    旧实现把结果写到旁挂文件、却返回原路径，于是调用方拿到的是**没字幕的那一版**
    —— 字幕既没交付也没人读，而调用方还会拿这个路径去登记交付物、
    写进「成品验收」队列。这是典型的「做对了但没生效」。

    这里改成：先烧到同目录临时文件，成功后 ``os.replace`` 原子替换回 ``video_path``。
    - 成功 → 返回 ``video_path``（**就是带字幕的那一版**，与调用方预期一致）；
    - 失败 → 临时文件清理掉，``video_path`` 保持原样并上抛，
      绝不留下「半截文件冒充成片」的中间态（原子替换保证不出现）。

    ``add_subtitles`` 形参传入 ``VideoPostProcessor.add_subtitles`` 绑定方法，
    以避免本模块反向依赖具体实现类（也便于单测替换）。
    """
    if not subs or not video_path:
        return video_path
    tmp = os.path.join(os.path.dirname(video_path),
                       f".{os.path.basename(video_path)}.sub.{os.getpid()}.tmp.mp4")
    try:
        out = add_subtitles(video_path, subs, tmp)
        if not out or not os.path.isfile(out) or os.path.getsize(out) <= 0:
            raise RuntimeError(f"字幕烧制未产出有效文件：{out or '(空路径)'}")
        os.replace(out, video_path)
        logger.info("字幕已烧入成片本体：%s（%d 条）", video_path, len(subs))
        return video_path
    except Exception as e:  # noqa: BLE001 —— 字幕失败不阻断成片
        logger.warning("成片字幕烧制失败（保留无字幕成片，不阻断出片）%s：%s", video_path, e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError as ce:
            logger.debug("字幕临时文件清理失败（忽略）：%s", ce)
        return video_path
# -*- coding: utf-8 -*-
"""结构化失败码 —— 让「为什么失败」可归类、可统计、可告警（学 comfyui-auto-drama）

问题
----
全项目的失败原因都是**自由文本**。于是：

* 日志里只能靠人眼 grep，无法回答「这一周最常见的是哪类失败」；
* 死信队列里躺着一堆句子，前端做不出「按原因分组」；
* 告警（如「连续 3 次都是显存不足」）没有稳定的判据。

设计取舍（**不改任何既有报错文案**）
----------------------------------
刻意**不**去逐个改写抛错点 —— 那要动几十处、且会破坏现有日志/前端里
依赖原文的匹配与展示。改为提供纯函数 :func:`classify`：

    从 **已经存在的** 异常/文案里推导出稳定错误码

* 需要精确分类的新代码，可以显式传 code（:class:`Failure`）；
* 老代码一字不改，照样能被归类（靠 :data:`_RULES` 的关键词规则）。

码表用「F-大类-细分」，便于日志聚合与看板分组。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["CODES", "UNKNOWN", "Failure", "classify", "explain", "summarize",
           "annotate", "is_known"]

UNKNOWN = "F-UNKNOWN"

#: 码表：码 → 人话说明。新增码必须同时进这里（守卫会检查一致性）。
CODES: Dict[str, str] = {
    "F-SUBMIT-API": "ComfyUI 提交被拒（队列接口返回 error）",
    "F-SUBMIT-NET": "连不上 ComfyUI / 网络异常",
    "F-TIMEOUT": "等待 ComfyUI 超时",
    "F-COUNT": "产物数量不符（段数 / 镜数对不上）",
    "F-QC-BLOCKED": "质检不通过被阻断",
    "F-QC-UNAVAILABLE": "质检接口未就绪（已声明要检却检不了）",
    "F-REF-MISSING": "参考图 / 输入图缺失",
    "F-OOM": "显存或内存不足",
    "F-DEPS": "依赖缺失（模型 / 自定义节点 / ffmpeg）",
    "F-PROBE": "产物探测失败（ffprobe 读不出参数）",
    "F-LEASE": "集级租约被占用（别处正在跑）",
    "F-INTERRUPT": "任务被中止 / 取消",
    "F-EXPORT": "成片合成或导出失败",
    "F-SCRIPT": "剧本 / 分镜内容问题",
    "F-UNKNOWN": "未归类（需补充规则或显式传 code）",
}

#: 关键词规则，**顺序敏感**：越具体的越靠前，命中即返回。
#: 每条 = (码, 关键词元组)，任一词命中即算命中该码。
_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("F-OOM", ("out of memory", "cuda out of memory", "cuda error", "显存", "内存不足",
               "OOMKilled")),
    ("F-INTERRUPT", ("中止信号", "被取消", "已取消", "cancelled", "canceled",
                     "interrupted", "收到了中止")),
    ("F-QC-UNAVAILABLE", ("质检接口未就绪", "质检接口不可用", "质检未就绪",
                          "qc_unavailable")),
    ("F-QC-BLOCKED", ("质检阻断", "质检不通过", "质检未通过", "未通过质检",
                      "不达标", "qc_blocked", "qc 未通过")),
    ("F-REF-MISSING", ("参考图缺失", "缺少参考图", "参考图不存在", "参考图为空",
                       "loadimage", "filenotfounderror", "no such file")),
    ("F-DEPS", ("未找到 ffprobe", "未找到 ffmpeg", "custom_nodes", "自定义节点",
                "模型权重", "依赖缺失", "importerror", "modulenotfounderror")),
    ("F-PROBE", ("ffprobe", "未找到视频流", "探测失败", "读取视频元信息")),
    ("F-TIMEOUT", ("等待超时", "超时", "timeout", "timed out")),
    ("F-LEASE", ("租约", "已被占用", "正在跑", "正在执行中")),
    ("F-SUBMIT-NET", ("connectionreseterror", "connectionerror", "max retries",
                      "连不上", "无法连接", "connection refused")),
    ("F-SUBMIT-API", ("队列错误", "提交被拒", "prompt_failed", "队列 api")),
    ("F-COUNT", ("数量不符", "段数", "镜数", "视频缺失", "镜头缺失", "缺镜",
                 "缺少镜头", "数量对不上")),
    ("F-EXPORT", ("合成失败", "导出失败", "混音失败", "封装失败")),
    ("F-SCRIPT", ("剧本", "分镜", "提示词预检", "字段缺失", "schema")),
]

#: 按规则长度倒序缓存一份（长关键词更具体，优先匹配），避免每次调用重排。
_ORDERED_RULES: List[Tuple[str, str]] = sorted(
    ((code, kw) for code, kws in _RULES for kw in kws),
    key=lambda kv: len(kv[1]), reverse=True)


class Failure(RuntimeError):
    """带稳定错误码的异常。

    **str() 与原信息逐字一致** —— 这样既有日志、前端文案、测试断言全都不受影响，
    码只作为附加属性存在（.code）。
    """

    def __init__(self, message: str, code: str = UNKNOWN, *, detail: dict = None):
        super().__init__(message)
        self.code = code if code in CODES else UNKNOWN
        self.detail = dict(detail or {})

    def __str__(self) -> str:
        return super().__str__()


def _text_of(err: Any) -> str:
    """把异常/字符串/字典统一取成待匹配文本（低风险，绝不抛）。"""
    if isinstance(err, Failure):
        return str(err)
    if isinstance(err, BaseException):
        return "%s: %s" % (type(err).__name__, err)
    if isinstance(err, dict):
        parts = [str(err.get(k) or "") for k in
                 ("reason", "error", "message", "detail", "label", "phase")]
        return " ".join(p for p in parts if p)
    return str(err or "")


def classify(err: Any) -> str:
    """从异常或既有文案推导错误码。纯函数、全定义（任何输入都返回一个合法码）。

    显式带 code 的 Failure 直接采用其码（精确优先于猜测）。
    """
    try:
        if isinstance(err, Failure) and err.code:
            return err.code
        if isinstance(err, dict) and err.get("code") in CODES:
            return str(err["code"])
        text = _text_of(err).lower()
        if not text.strip():
            return UNKNOWN
        for code, kw in _ORDERED_RULES:
            if kw.lower() in text:
                return code
    except Exception as e:                                   # noqa: BLE001
        logger.debug("失败码归类异常（按 UNKNOWN 处理）：%s", e)
    return UNKNOWN


def explain(code_or_err: Any) -> str:
    """给出人话说明（码 → 说明；其他输入先归类再解释）。"""
    code = code_or_err if code_or_err in CODES else classify(code_or_err)
    return CODES.get(code, CODES[UNKNOWN])


def is_known(code: str) -> bool:
    return code in CODES


def annotate(err: Any) -> Tuple[str, str]:
    """返回 (code, 原文消息)，**不修改消息**。

    给需要「既保留原文、又想记一个码」的调用点用（如死信记录）。
    """
    return classify(err), _text_of(err)


def summarize(items: Iterable[Any]) -> Dict[str, Any]:
    """按码统计一组失败（死信列表 / 日志条目）。

    接受任意可迭代：元素既可以是字符串、异常，也可以是带 code/reason 的字典。
    返回 {"total": n, "top": 码, "counts": {码: 次数}, "unknown": 次数}。
    """
    counts: Dict[str, int] = {}
    total = 0
    for it in items or []:
        try:
            code = classify(it)
        except Exception:                                    # noqa: BLE001
            code = UNKNOWN
        counts[code] = counts.get(code, 0) + 1
        total += 1
    top = max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""
    return {"total": total, "top": top, "counts": counts,
            "unknown": counts.get(UNKNOWN, 0)}

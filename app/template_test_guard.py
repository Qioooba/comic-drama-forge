# -*- coding: utf-8 -*-
"""P1-6：正式提交前拦截“模板测试 timeline”。

测试模板曾经内嵌 seed=1001/43 帧与猴子跑动案例。Director Builder 正常会重建
timeline，但直接提交旧模板会把测试数据带进生产。这里在 ``queue_prompt`` 的最终
提交点做 fail-closed 校验：识别已知测试 timeline 的规范化 hash 与语义组合。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

# 2026-10-06 清理前两个生产模板 timeline 的规范化 SHA256（sort_keys + 紧凑 JSON）。
KNOWN_TEST_TIMELINE_HASHES = {
    "e748a8281ff724b7e6933890ee62a1fd7de8863a03353983abf8c4cbe955ff83",
    "2631437d3739144b15e456f23e339d73972b137c993eaf1de9465229c2acdd38",
}
_TEST_IMAGES = {"0059.png", "0060.png", "0062.png", "0086.png"}


class TemplateTestDataError(ValueError):
    """正式提交携带了模板测试数据。"""


def _canonical_hash(value: Any) -> str:
    try:
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return None


def _has_test_semantics(timeline: Dict, node: Dict) -> bool:
    shots = timeline.get("shots") or []
    keyframes = timeline.get("keyframes") or []
    segments = timeline.get("segments") or []
    if not (shots or keyframes or segments):
        return False

    named = node.get("widgets_values_named") or {}
    if named.get("seed") == 1001 and named.get("total_frames") == 43:
        return True

    text_parts = [str(named.get("global_prompt") or "")]
    for item in list(shots) + list(segments) + list(keyframes):
        if isinstance(item, dict):
            text_parts.append(str(item.get("prompt") or ""))
            start = item.get("startImage")
            if isinstance(start, dict):
                text_parts.append(str(start.get("imageFile") or ""))
    text = "\n".join(text_parts).lower()
    monkey = ("猴子" in text) or ("monkey" in text)
    test_image = any(name in text for name in _TEST_IMAGES)
    return monkey and (test_image or named.get("seed") == 666)


def find_violations(api_prompt: Dict[str, Any]) -> List[str]:
    violations: List[str] = []
    for node_id, node in (api_prompt or {}).items():
        if not isinstance(node, dict) or node.get("class_type") != "MiniMaxH3Director":
            continue
        inputs = node.get("inputs") or {}
        raw = inputs.get("timeline_data")
        timeline = _parse(raw)
        if isinstance(timeline, dict):
            digest = _canonical_hash(timeline)
            if digest in KNOWN_TEST_TIMELINE_HASHES:
                violations.append(f"节点 {node_id} 命中已知模板测试 timeline hash")
                continue
            if _has_test_semantics(timeline, node):
                violations.append(f"节点 {node_id} 命中模板测试语义（seed/帧数/测试素材）")
    return violations


def assert_no_template_test_data(api_prompt: Dict[str, Any]) -> None:
    found = find_violations(api_prompt)
    if found:
        raise TemplateTestDataError(
            "拒绝提交：正式任务携带了模板测试数据。"
            + "；".join(found)
            + "。请先通过 Director Builder 注入正式 timeline。"
        )

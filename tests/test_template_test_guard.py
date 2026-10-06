# -*- coding: utf-8 -*-
"""P1-6 模板测试数据清理与提交前拦截测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import comfyui_client  # noqa: E402
import template_test_guard  # noqa: E402


def _api_prompt_from_ui(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    prompt = {}
    for node in data.get("nodes") or []:
        inputs = dict(node.get("widgets_values_named") or {})
        prompt[str(node.get("id"))] = {
            "class_type": node.get("type"),
            "inputs": inputs,
        }
    return prompt


class TemplateCleanTests(unittest.TestCase):
    def test_pre_clean_examples_are_blocked(self):
        examples = ROOT / "examples" / "h3_template_test_cases"
        for name in ("h3_director_r2v_单采.pre_clean.json",
                     "minimax_h3_director_二采_加速.pre_clean.json"):
            with self.subTest(name=name):
                prompt = _api_prompt_from_ui(examples / name)
                found = template_test_guard.find_violations(prompt)
                self.assertTrue(found, f"{name} 应被识别为模板测试数据")

    def test_clean_production_templates_have_no_test_timeline(self):
        for name in ("h3_director_r2v_单采.json",
                     "minimax_h3_director_二采_加速.json"):
            with self.subTest(name=name):
                prompt = _api_prompt_from_ui(ROOT / "workflows" / name)
                self.assertEqual(template_test_guard.find_violations(prompt), [])

    def test_queue_prompt_rejects_before_network_call(self):
        example = (ROOT / "examples" / "h3_template_test_cases" /
                   "h3_director_r2v_单采.pre_clean.json")
        prompt = _api_prompt_from_ui(example)
        client = comfyui_client.ComfyUIClient()
        with patch.object(client, "_post") as post:
            with self.assertRaises(template_test_guard.TemplateTestDataError):
                client.queue_prompt(prompt)
        post.assert_not_called()

    def test_legitimate_monkey_story_without_test_assets_is_allowed(self):
        prompt = {"12": {"class_type": "MiniMaxH3Director", "inputs": {
            "seed": 123, "total_frames": 96,
            "timeline_data": json.dumps({
                "totalFrames": 96,
                "segments": [{"prompt": "猴子在森林里奔跑"}],
                "shots": [{"prompt": "猴子在森林里奔跑",
                           "startImage": {"imageFile": "project_shot_01.png"}}],
                "keyframes": [],
            }, ensure_ascii=False),
        }}}
        self.assertEqual(template_test_guard.find_violations(prompt), [])


if __name__ == "__main__":
    unittest.main()

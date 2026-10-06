# -*- coding: utf-8 -*-
"""P0-3 TRT engine 自检测试（不访问真实 ComfyUI）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import trt_engine_check  # noqa: E402


OK_STATIC = {
    "engines": [
        {"key": "decoder", "name": trt_engine_check.ENGINE_SPECS[0]["name"],
         "path": "/x/decoder.engine", "exists": True, "size": 10, "ok": True},
        {"key": "encoder", "name": trt_engine_check.ENGINE_SPECS[1]["name"],
         "path": "/x/encoder.engine", "exists": True, "size": 10, "ok": True},
    ],
    "ok": True,
    "reason": "",
}


def _object_info():
    return {
        "MiniMaxH3TRTVAELoader": {
            "input": {"required": {
                "decoder": [[trt_engine_check.ENGINE_SPECS[0]["name"], "None"], {}],
                "encoder": [[trt_engine_check.ENGINE_SPECS[1]["name"], "None"], {}],
            }}
        }
    }


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class TrtEngineCheckTests(unittest.TestCase):
    def test_static_missing_engine_is_incompatible(self):
        static = {"engines": [], "ok": False, "reason": "decoder 缺失"}
        with patch.object(trt_engine_check, "_static_report", return_value=static), \
             patch.object(trt_engine_check, "_gpu_info", return_value={}):
            report = trt_engine_check.check()
        self.assertEqual(report["status"], "不兼容")
        self.assertIn("decoder", report["reason"])

    def test_combo_value_matches_semantic_file_name(self):
        self.assertEqual(
            trt_engine_check._combo_value(_object_info(),
                                          trt_engine_check.ENGINE_SPECS[1]["name"]),
            trt_engine_check.ENGINE_SPECS[1]["name"],
        )

    def test_probe_success_marks_available(self):
        def fake_get(url, **kwargs):
            if url.endswith("/object_info"):
                return FakeResponse(payload=_object_info())
            if "/history/" in url:
                return FakeResponse(payload={"pid": {
                    "status": {"completed": True, "status_str": "success",
                               "messages": []},
                }})
            raise AssertionError(url)

        def fake_post(url, **kwargs):
            return FakeResponse(payload={"prompt_id": "pid"})

        with patch.object(trt_engine_check.requests, "get", side_effect=fake_get), \
             patch.object(trt_engine_check.requests, "post", side_effect=fake_post):
            result = trt_engine_check._run_probe(OK_STATIC, timeout=2)
        self.assertEqual(result["status"], "可用")
        self.assertTrue(result["probed"])

    def test_probe_engine_error_is_incompatible(self):
        def fake_get(url, **kwargs):
            if url.endswith("/object_info"):
                return FakeResponse(payload=_object_info())
            if "/history/" in url:
                return FakeResponse(payload={"pid": {
                    "status": {"completed": False, "status_str": "error",
                               "messages": [["execution_error",
                                             "Failed to deserialize TensorRT engine"]]},
                }})
            raise AssertionError(url)

        with patch.object(trt_engine_check.requests, "get", side_effect=fake_get), \
             patch.object(trt_engine_check.requests, "post",
                          return_value=FakeResponse(payload={"prompt_id": "pid"})):
            result = trt_engine_check._run_probe(OK_STATIC, timeout=2)
        self.assertEqual(result["status"], "不兼容")

    def test_probe_prompt_contains_encode_decode_and_preview(self):
        prompt = trt_engine_check._build_probe_prompt("dec.engine", "enc.engine")
        types = [node["class_type"] for node in prompt.values()]
        self.assertEqual(types, ["MiniMaxH3TRTVAELoader", "EmptyImage",
                                 "VAEEncode", "VAEDecode", "PreviewImage"])


if __name__ == "__main__":
    unittest.main()

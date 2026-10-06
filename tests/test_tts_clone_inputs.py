# -*- coding: utf-8 -*-
"""TTS 节点入参契约回归测试（钉死「文本入参名」这类 schema 漂移）。

背景（2026-10-06）
------------------
ComfyUI-Qwen-TTS 的三个合成节点里，**只有** ``FB_Qwen3TTSVoiceClone`` 的文本
入参叫 ``target_text``；``FB_Qwen3TTSCustomVoice`` / ``FB_Qwen3TTSVoiceDesign``
叫 ``text``。``tts_client._node_inputs`` 的 clone 分支一旦写成 ``text=text``，
ComfyUI 会在**校验阶段**返回 400 ``prompt_outputs_failed_validation``
（``Required input is missing: target_text``），一句音频都出不来；而 ComfyUI
只把原因打进自己的 stderr，应用日志里只剩一个光秃秃的
「试听失败：HTTP Error 400」—— 极易误判成「模型慢/在加载」，极难定位。

本测试用**线上真实 schema 快照**（GET /object_info/FB_Qwen3TTS*）逐模式核对
必填入参是否齐备。纯 CPU：不连 ComfyUI、不提交 prompt、不占显卡。

维护约定
--------
升级 ComfyUI-Qwen-TTS 后若 schema 变了，先重新抓一遍 required 入参名更新下面
的 ``NODE_REQUIRED_INPUTS``，再跑本测试。届时它会以「缺哪个入参」的方式直接
失败，而不是等到用户在界面上点「试听」才发现合成链路整条挂掉。

抓取方法（需要 ComfyUI 在跑）::

    Invoke-RestMethod http://127.0.0.1:8190/object_info/FB_Qwen3TTSVoiceClone |
      Select-Object -ExpandProperty input | Select-Object -ExpandProperty required
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import tts_client  # noqa: E402

# 2026-10-06 从线上 GET /object_info/<node> 抓下来的 required 入参名
NODE_REQUIRED_INPUTS = {
    "FB_Qwen3TTSCustomVoice": ["text", "speaker", "model_choice",
                               "device", "precision", "language"],
    "FB_Qwen3TTSVoiceDesign": ["text", "instruct", "model_choice",
                               "device", "precision", "language"],
    # ⭐ 唯一用 target_text 的节点
    "FB_Qwen3TTSVoiceClone": ["target_text", "model_choice",
                              "device", "precision", "language"],
}


def _client() -> "tts_client.QwenTTSClient":
    # 构造无副作用（不连网、不建目录），comfyui_url 指向假地址即可
    return tts_client.QwenTTSClient(
        comfyui_url="http://127.0.0.1:1", out_root=None, params=None)


class CloneTextInputContractTests(unittest.TestCase):
    """clone 模式必须提交 target_text（本次线上故障的根因）。"""

    def test_clone_mode_submits_target_text(self):
        cls, inputs = _client()._node_inputs(
            "我是林霄，今日便让你见识见识。",
            {"mode": "clone", "ref_audio": "x.wav", "ref_text": "原文",
             "character": "林霄", "seed": 7},
            is_last=True, ref_node="5")

        self.assertEqual("FB_Qwen3TTSVoiceClone", cls)
        self.assertEqual("我是林霄，今日便让你见识见识。", inputs.get("target_text"),
                         "clone 模式必须用 target_text 提交文本，否则 ComfyUI 校验 400")
        self.assertEqual("原文", inputs.get("ref_text"))
        self.assertEqual(["5", 0], inputs.get("ref_audio"))
        self.assertEqual(7, inputs.get("seed"))

    def test_clone_mode_works_without_ref_text(self):
        """ref_text 为空时不该塞空串（节点有 optional 语义，留着反而可能触发必填校验）。"""
        _, inputs = _client()._node_inputs(
            "台词", {"mode": "clone", "character": "林霄"},
            is_last=True, ref_node="5")
        self.assertNotIn("ref_text", inputs)
        self.assertEqual("台词", inputs.get("target_text"))

    def test_non_clone_modes_keep_text_key(self):
        """CustomVoice / VoiceDesign 的入参名是 text —— 别被 clone 的修复带跑。"""
        _, design = _client()._node_inputs(
            "台词", {"mode": "design", "instruct": "低沉男声"}, is_last=True)
        self.assertEqual("台词", design.get("text"))
        self.assertNotIn("target_text", design)

        _, preset = _client()._node_inputs(
            "台词", {"mode": "preset", "speaker": "Ryan"}, is_last=True)
        self.assertEqual("台词", preset.get("text"))
        self.assertEqual("Ryan", preset.get("speaker"))
        self.assertNotIn("target_text", preset)


class RequiredInputsCoveredTests(unittest.TestCase):
    """逐模式核对：节点 required 入参必须全部出现在我们提交的 inputs 里。"""

    CASES = (
        ("FB_Qwen3TTSVoiceClone",
         {"mode": "clone", "character": "林霄", "seed": 0}, "5"),
        ("FB_Qwen3TTSVoiceDesign",
         {"mode": "design", "instruct": "低沉男声"}, None),
        ("FB_Qwen3TTSCustomVoice",
         {"mode": "preset", "speaker": "Ryan"}, None),
    )

    def test_every_required_input_is_submitted(self):
        for expected_cls, voice, ref_node in self.CASES:
            with self.subTest(node=expected_cls):
                cls, inputs = _client()._node_inputs("台词", voice, True, ref_node)
                self.assertEqual(expected_cls, cls)
                missing = [k for k in NODE_REQUIRED_INPUTS[expected_cls]
                           if k not in inputs]
                self.assertEqual([], missing,
                                 f"{expected_cls} 缺少必填入参 {missing} —— "
                                 f"ComfyUI 会返回 400 prompt_outputs_failed_validation")

    def test_snapshot_matches_known_signature_drift(self):
        """把「三个节点里只有 clone 用 target_text」这件事本身钉住。"""
        self.assertIn("target_text", NODE_REQUIRED_INPUTS["FB_Qwen3TTSVoiceClone"])
        self.assertNotIn("text", NODE_REQUIRED_INPUTS["FB_Qwen3TTSVoiceClone"])
        self.assertIn("text", NODE_REQUIRED_INPUTS["FB_Qwen3TTSCustomVoice"])
        self.assertIn("text", NODE_REQUIRED_INPUTS["FB_Qwen3TTSVoiceDesign"])


class ComfyUiErrorBodyTests(unittest.TestCase):
    """_http_json 遇到 4xx 必须把 ComfyUI 的响应体贴进异常信息。"""

    BODY = json.dumps({
        "error": {
            "type": "prompt_outputs_failed_validation",
            "message": "Prompt outputs failed validation",
            "details": "",
            "extra_info": {
                "node_errors": {
                    "cv0": {
                        "class_type": "FB_Qwen3TTSVoiceClone",
                        "errors": [{
                            "type": "required_input_missing",
                            "message": "Required input is missing: target_text",
                            "details": "target_text",
                        }],
                    },
                },
            },
        },
        "node_id": "cv0",
        "prompt_id": "abc",
    }, ensure_ascii=False)

    def test_http_error_body_is_surfaced(self):
        def _boom(*_a, **_k):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:1/prompt", 400, "Bad Request",
                {}, io.BytesIO(self.BODY.encode("utf-8")))

        with patch.object(urllib.request, "urlopen", _boom):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                tts_client._http_json("http://127.0.0.1:1/prompt", {"prompt": {}})

        text = str(ctx.exception)
        self.assertIn("Required input is missing: target_text", text,
                      "HTTP 400 的真实原因只在响应体里，丢掉就没法排查")
        self.assertIn("prompt_outputs_failed_validation", text)
        self.assertEqual(400, ctx.exception.code)

    def test_json_response_still_works(self):
        ok = json.dumps({"prompt_id": "p-1"})
        resp = io.BytesIO(ok.encode("utf-8"))
        resp.__enter__ = lambda s: s          # noqa: E731
        resp.__exit__ = lambda s, *a: False    # noqa: E731

        with patch.object(urllib.request, "urlopen", lambda *a, **k: resp):
            self.assertEqual({"prompt_id": "p-1"},
                             tts_client._http_json("http://127.0.0.1:1/prompt"))


if __name__ == "__main__":
    unittest.main()

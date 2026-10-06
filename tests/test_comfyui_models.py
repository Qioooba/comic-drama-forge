# -*- coding: utf-8 -*-
"""P0-2 七槽位语义 + combo fail-closed 校验测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import comfyui_models  # noqa: E402


H3_UNET = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
QWEN_UNET = "qwen_image_2.1_int8_convrot.safetensors"
H3_CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
QWEN_CLIP = "qwen3vl_8b_int8_convrot.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
DECODER = "minimax_h3_vae_decoder_w4a16_awq.engine"
ENCODER = "minimax_h3_vae_encoder.engine"


def _slot_object_info(values_by_slot):
    out = {}
    for key, values in values_by_slot.items():
        meta = comfyui_models.SLOTS[key]
        blob = out.setdefault(meta["node_type"], {"input": {"required": {}}})
        blob["input"]["required"][meta["field"]] = [list(values), {}]
    return out


class SemanticValidationTests(unittest.TestCase):
    def test_invalid_values_are_rejected_before_write(self):
        cases = [
            ("unet_main", QWEN_UNET),
            ("clip", QWEN_CLIP),
            ("vae_audio", VIDEO_VAE),
            ("vae_video_decoder", ENCODER),
            ("vae_video_encoder", DECODER),
            ("lora_channel_a", "qwen_image_lora.safetensors"),
            ("lora_channel_b", "qwen_image_lora.safetensors"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "comfyui_models.json"
            with patch.object(comfyui_models, "_STORE_PATH", str(store)):
                for key, value in cases:
                    with self.subTest(key=key, value=value):
                        with self.assertRaises(ValueError):
                            comfyui_models.save_selection(
                                {key: value},
                                object_info=_slot_object_info({key: [value]}),
                            )
                        self.assertFalse(store.exists())

    def test_semantic_mismatch_is_rejected_even_when_combo_allows_it(self):
        # TRT loader 的 decoder/encoder combo 完全相同，必须靠语义规则拦截。
        info = _slot_object_info({
            "vae_video_decoder": [DECODER, ENCODER],
            "vae_video_encoder": [DECODER, ENCODER],
        })
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "comfyui_models.json"
            with patch.object(comfyui_models, "_STORE_PATH", str(store)):
                with self.assertRaisesRegex(ValueError, "encoder"):
                    comfyui_models.save_selection(
                        {"vae_video_decoder": ENCODER}, object_info=info)
                with self.assertRaisesRegex(ValueError, "decoder"):
                    comfyui_models.save_selection(
                        {"vae_video_encoder": DECODER}, object_info=info)

    def test_combo_mismatch_and_missing_object_info_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "comfyui_models.json"
            with patch.object(comfyui_models, "_STORE_PATH", str(store)):
                with self.assertRaisesRegex(ValueError, "合法候选"):
                    comfyui_models.save_selection(
                        {"unet_main": H3_UNET},
                        object_info={"UNETLoader": {"input": {"required": {
                            "unet_name": [["other.safetensors"], {}]}}}},
                    )
                with self.assertRaisesRegex(ValueError, "无法从 ComfyUI"):
                    comfyui_models.save_selection({"unet_main": H3_UNET})
                self.assertFalse(store.exists())

    def test_valid_semantic_and_combo_value_is_saved(self):
        info = _slot_object_info({
            "vae_video_decoder": [DECODER, ENCODER],
        })
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "comfyui_models.json"
            with patch.object(comfyui_models, "_STORE_PATH", str(store)):
                saved = comfyui_models.save_selection(
                    {"vae_video_decoder": DECODER}, object_info=info)
                self.assertEqual(saved["vae_video_decoder"], DECODER)
                self.assertEqual(comfyui_models.resolve_selection(),
                                 {"vae_video_decoder": DECODER})


class SelectionAndScanTests(unittest.TestCase):
    def test_resolve_selection_filters_invalid_stored_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "comfyui_models.json"
            store.write_text(json.dumps({
                "unet_main": QWEN_UNET,
                "vae_audio": VIDEO_VAE,
                "vae_video_decoder": ENCODER,
                "clip": H3_CLIP,
            }, ensure_ascii=False), encoding="utf-8")
            with patch.object(comfyui_models, "_STORE_PATH", str(store)):
                resolved = comfyui_models.resolve_selection()
        self.assertEqual(resolved, {"clip": H3_CLIP})

    def test_scan_filters_candidates_and_marks_invalid_selection(self):
        info = _slot_object_info({
            "unet_main": [QWEN_UNET, H3_UNET],
            "vae_video_decoder": [DECODER, ENCODER],
        })

        class FakeClient:
            def get_object_info(self, force=False):
                return info

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "comfyui_models.json"
            store.write_text(json.dumps({
                "unet_main": QWEN_UNET,
                "vae_video_decoder": ENCODER,
            }, ensure_ascii=False), encoding="utf-8")
            with patch.object(comfyui_models, "_STORE_PATH", str(store)):
                result = comfyui_models.scan(FakeClient())

        slots = {slot["key"]: slot for slot in result["slots"]}
        self.assertEqual(slots["unet_main"]["values"], [H3_UNET])
        self.assertFalse(slots["unet_main"]["selected_valid"])
        self.assertEqual(slots["vae_video_decoder"]["values"], [DECODER])
        self.assertFalse(slots["vae_video_decoder"]["selected_valid"])


if __name__ == "__main__":
    unittest.main()

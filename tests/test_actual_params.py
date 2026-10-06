# -*- coding: utf-8 -*-
"""P0-5 实际提交参数快照测试。"""

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

import actual_params  # noqa: E402
import comfyui_client  # noqa: E402


class ActualParamsTests(unittest.TestCase):
    def test_extract_reads_final_prompt_values(self):
        timeline = {
            "segments": [
                {"frameCount": 48, "duration": 2.0},
                {"frameCount": 72, "duration": 3.0},
            ],
            "audioMode": "source",
            "continuity": True,
            "overlapFrames": 22,
            "commonRefs": 3,
        }
        prompt = {
            "1": {"class_type": "UNETLoader",
                  "inputs": {"unet_name": "minimax_h3_ref2va.safetensors"}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": "qwen3vl_32b_minimax_h3.safetensors"}},
            "3": {"class_type": "VAELoader",
                  "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
            "4": {"class_type": "MiniMaxH3TRTVAELoader",
                  "inputs": {"decoder": "decoder.engine", "encoder": "encoder.engine"}},
            "5": {"class_type": "MiniMaxH3Director",
                  "inputs": {"width": 544, "height": 960, "fps": 24,
                             "noise_seed": 1001,
                             "timeline_data": json.dumps(timeline)}},
            "6": {"class_type": "MiniMaxH3DirectorRefine", "inputs": {}},
            "7": {"class_type": "NvidiaDLSSFrameInterpolation", "inputs": {}},
        }
        snap = actual_params.extract(
            prompt,
            workflow_meta={"path": "/wf/h3.json", "file": "h3.json", "hash": "abc"},
            prompt_id="pid",
        )
        self.assertEqual(snap["node_count"], 7)
        self.assertEqual(snap["width"], 544)
        self.assertEqual(snap["height"], 960)
        self.assertEqual(snap["fps"], 24)
        self.assertEqual(snap["seed"], 1001)
        self.assertEqual(snap["segment_count"], 2)
        self.assertEqual(snap["total_frames"], 120)
        self.assertEqual(snap["audio_mode"], "source")
        self.assertTrue(snap["refine_present"])
        self.assertTrue(snap["dlss_present"])
        self.assertEqual(snap["workflow_hash"], "abc")
        self.assertEqual(snap["models"]["unet_main"],
                         "minimax_h3_ref2va.safetensors")

    def test_save_and_latest_are_atomic_and_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(actual_params, "_DIR", tmp):
                first = {"prompt_id": "a", "node_count": 1}
                second = {"prompt_id": "b", "node_count": 2}
                actual_params.save(first)
                actual_params.save(second)
                items = actual_params.latest(limit=10)
        self.assertEqual([item["prompt_id"] for item in items], ["b", "a"])
        self.assertTrue(items[0]["snapshot_path"].endswith("b.json"))

    def test_queue_prompt_writes_snapshot_without_changing_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(actual_params, "_DIR", tmp), \
                 patch.object(comfyui_client.ComfyUIClient, "_post",
                              return_value={"prompt_id": "pid-1"}):
                client = comfyui_client.ComfyUIClient()
                client.last_workflow_meta = {"path": "/wf/x.json",
                                             "file": "x.json", "hash": "deadbeef"}
                pid = client.queue_prompt({"1": {"class_type": "Note", "inputs": {}}})
                saved = Path(tmp) / "pid-1.json"
                self.assertEqual(pid, "pid-1")
                self.assertTrue(saved.is_file())
                data = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(data["prompt_id"], "pid-1")
        self.assertEqual(data["workflow_hash"], "deadbeef")


if __name__ == "__main__":
    unittest.main()

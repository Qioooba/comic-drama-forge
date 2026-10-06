# -*- coding: utf-8 -*-
"""P1-9 质检 Vision 强制自检 / fail-closed 测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import qc_client  # noqa: E402


ENDPOINT = {"base_url": "https://qc.example/v1",
            "api_key": "test-key",
            "model": "vision-model",
            "source": "test"}


def _result(vision):
    if vision is True:
        return {"success": True, "vision": True, "verdict": "ok"}
    if vision is None:
        return {"success": True, "vision": None, "uncertain": True,
                "hint": "正文为空，无法确认"}
    return {"success": False, "vision": False, "error": "model does not support images"}


class QcVisionTests(unittest.TestCase):
    def _save(self, tmp: str, vision, patch_fields=None):
        path = str(Path(tmp) / "qc_config.json")
        data = {"enabled": True, "image_enabled": True, "video_enabled": True,
                "base_url": ENDPOINT["base_url"], "model": ENDPOINT["model"]}
        data.update(patch_fields or {})
        with patch.object(qc_client, "_sync_credentials_db"), \
             patch.object(qc_client, "resolve_endpoint",
                          return_value=dict(ENDPOINT)), \
             patch.object(qc_client, "test_vision",
                          return_value=_result(vision)):
            cfg = qc_client.save_config(path, data, keep_key_if_blank=False)
        return path, cfg

    def test_vision_ok_enables_image_and_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg = self._save(tmp, True)
        self.assertEqual(cfg["vision_status"], "ok")
        with patch.object(qc_client, "resolve_endpoint", return_value=dict(ENDPOINT)):
            self.assertTrue(qc_client.image_qc_ready(cfg))
            self.assertTrue(qc_client.video_qc_ready(cfg))
            view = qc_client.public_view(cfg)
        self.assertTrue(view["vision_ok"])
        self.assertTrue(view["image_qc_active"])
        self.assertTrue(view["video_qc_active"])

    def test_uncertain_is_treated_as_not_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg = self._save(tmp, None)
        self.assertEqual(cfg["vision_status"], "uncertain")
        with patch.object(qc_client, "resolve_endpoint", return_value=dict(ENDPOINT)):
            self.assertFalse(qc_client.image_qc_ready(cfg))
            self.assertFalse(qc_client.video_qc_ready(cfg))
            view = qc_client.public_view(cfg)
        self.assertFalse(view["vision_ok"])
        self.assertFalse(view["image_qc_active"])
        self.assertFalse(view["video_qc_active"])

    def test_vision_false_blocks_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg = self._save(tmp, False)
        self.assertEqual(cfg["vision_status"], "failed")
        self.assertIn("does not support", cfg["vision_error"])
        with patch.object(qc_client, "resolve_endpoint", return_value=dict(ENDPOINT)):
            self.assertFalse(qc_client.image_qc_ready(cfg))
            self.assertFalse(qc_client.video_qc_ready(cfg))

    def test_disabling_visual_qc_does_not_require_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "qc_config.json")
            with patch.object(qc_client, "_sync_credentials_db"), \
                 patch.object(qc_client, "resolve_endpoint",
                              return_value=dict(ENDPOINT)), \
                 patch.object(qc_client, "test_vision") as probe:
                cfg = qc_client.save_config(path, {
                    "enabled": True, "image_enabled": False, "video_enabled": False,
                })
            probe.assert_not_called()
        self.assertEqual(cfg["vision_status"], "untested")

    def test_forced_refresh_uses_test_vision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cfg = self._save(tmp, True)
            with patch.object(qc_client, "resolve_endpoint",
                              return_value=dict(ENDPOINT)), \
                 patch.object(qc_client, "test_vision",
                              return_value=_result(False)) as probe:
                refreshed = qc_client.refresh_vision_status(path, force=True)
            probe.assert_called_once()
        self.assertEqual(refreshed["vision_status"], "failed")
        self.assertEqual(refreshed["vision_endpoint_key"],
                         cfg["vision_endpoint_key"])


if __name__ == "__main__":
    unittest.main()

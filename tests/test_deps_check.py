# -*- coding: utf-8 -*-
"""P0-1 依赖检测模型扫描回归测试（不依赖 Flask / 真机模型盘）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import deps_check  # noqa: E402


H3_UNET = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
H3_UNET_CHECKLIST = {
    "path": f"diffusion_models\\minimax-h3\\{H3_UNET}",
    "kind": "video_unet",
    "required": True,
    "note": "test",
}


def _object_info_with_unet_combo(values):
    return {
        "UNETLoader": {
            "input": {
                "required": {
                    "unet_name": [list(values), {}],
                }
            }
        }
    }


class DepsCheckModelScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root_a = Path(self._tmp.name) / "tree_a"
        self.root_b = Path(self._tmp.name) / "tree_b"
        self.root_a.mkdir()
        self.root_b.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, root: Path, rel: str) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
        return path

    def test_multi_root_exact_recursive_ambiguity_and_subdirs(self):
        rel = "diffusion_models/minimax-h3/" + H3_UNET
        self._touch(self.root_a, rel)
        self._touch(self.root_b, rel)

        with patch.object(deps_check, "MODEL_CHECKLIST", [H3_UNET_CHECKLIST]), \
             patch.object(deps_check, "models_search_dirs",
                          lambda: [str(self.root_a), str(self.root_b)]):
            items = deps_check._check_models([H3_UNET])

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertTrue(item["found"])
        self.assertEqual(item["match"], "exact")
        self.assertTrue(item["ambiguous"])
        self.assertEqual(len(item["candidates"]), 2)
        self.assertIn(str(self.root_a / "diffusion_models" / "minimax-h3"), item["subdirs"])
        self.assertIn(str(self.root_b / "diffusion_models" / "minimax-h3"), item["subdirs"])
        self.assertIsNone(item["combo_ok"])

    def test_object_info_combo_visible_and_invisible(self):
        rel = "diffusion_models/minimax-h3/" + H3_UNET
        self._touch(self.root_a, rel)

        visible = _object_info_with_unet_combo(
            [f"minimax-h3\\{H3_UNET}", H3_UNET]
        )
        invisible = _object_info_with_unet_combo(["wrong\\other.safetensors"])

        with patch.object(deps_check, "MODEL_CHECKLIST", [H3_UNET_CHECKLIST]), \
             patch.object(deps_check, "models_search_dirs", lambda: [str(self.root_a)]):
            ok_item = deps_check._check_models([H3_UNET], object_info=visible)[0]
            bad_item = deps_check._check_models([H3_UNET], object_info=invisible)[0]
            missing_loader_item = deps_check._check_models(
                [H3_UNET], object_info={"UNETLoader": {}}
            )[0]

        self.assertIs(ok_item["combo_ok"], True)
        self.assertIn(ok_item["combo_value"].lower(),
                      {H3_UNET.lower(), f"minimax-h3\\{H3_UNET}".lower()})
        self.assertIs(bad_item["combo_ok"], False)
        self.assertTrue(any("object_info" in note for note in bad_item["notes"]))
        self.assertIsNone(missing_loader_item["combo_ok"])

    def test_check_deps_passes_object_info_and_blocks_invisible_required_model(self):
        rel = "diffusion_models/minimax-h3/" + H3_UNET
        self._touch(self.root_a, rel)
        invisible = _object_info_with_unet_combo(["wrong\\other.safetensors"])

        with patch.object(deps_check, "MODEL_CHECKLIST", [H3_UNET_CHECKLIST]), \
             patch.object(deps_check, "WORKFLOW_TEMPLATE", {}), \
             patch.object(deps_check, "models_search_dirs", lambda: [str(self.root_a)]), \
             patch.object(deps_check, "MODELS_DIR", str(self.root_a)), \
             patch.object(deps_check, "_comfyui_online", return_value=True), \
             patch.object(deps_check, "_fetch_object_info", return_value=invisible):
            result = deps_check.check_deps()

        self.assertTrue(result["comfyui"]["online"])
        self.assertEqual(result["models"]["missing_required"], [])
        self.assertEqual(result["models"]["combo_invisible_required"], [H3_UNET])
        self.assertFalse(result["models"]["ready"])
        self.assertFalse(result["summary"]["models_ok"])
        self.assertTrue(any("combo 不可见" in blocker
                            for blocker in result["summary"]["blockers"]))

    def test_force_offline_keeps_combo_untested(self):
        rel = "diffusion_models/minimax-h3/" + H3_UNET
        self._touch(self.root_a, rel)

        with patch.object(deps_check, "MODEL_CHECKLIST", [H3_UNET_CHECKLIST]), \
             patch.object(deps_check, "WORKFLOW_TEMPLATE", {}), \
             patch.object(deps_check, "models_search_dirs", lambda: [str(self.root_a)]), \
             patch.object(deps_check, "MODELS_DIR", str(self.root_a)):
            result = deps_check.check_deps(force_offline=True)

        self.assertFalse(result["comfyui"]["online"])
        item = result["models"]["items"][0]
        self.assertTrue(item["found"])
        self.assertIsNone(item["combo_ok"])
        self.assertEqual(result["models"]["combo_invisible_required"], [])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""P2-12 稳定参数机器锁定测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import config  # noqa: E402
import stable_profile  # noqa: E402


class StableProfileTests(unittest.TestCase):
    def test_current_environment_matches_locked_profile(self):
        report = stable_profile.report()
        self.assertTrue(report["ok"], report["violations"])
        self.assertEqual(report["violations"], [])

    def test_expected_profile_contains_redline_keys(self):
        for key in ("DEPLOY_PROFILE", "TASK_QUEUE_CONCURRENCY",
                    "H3_ENABLE_REFINE", "H3_DISABLE_DLSS",
                    "H3_WORKFLOW", "H3_UNET", "QWEN_UNET",
                    "SOLATTN", "EASY_CACHE", "TE_SPEED_MINIMAX"):
            self.assertIn(key, stable_profile.EXPECTED)
        self.assertFalse(stable_profile.EXPECTED["SOLATTN"]["morton"])

    def test_changed_env_is_reported(self):
        with patch.object(config, "DEPLOY_PROFILE", "24G"):
            found = stable_profile.violations()
        self.assertTrue(any(item.startswith("DEPLOY_PROFILE:") for item in found))


if __name__ == "__main__":
    unittest.main()

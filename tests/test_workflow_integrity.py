# -*- coding: utf-8 -*-
"""P0-4 workflows 多副本完整性核对测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import workflow_integrity  # noqa: E402


class WorkflowIntegrityTests(unittest.TestCase):
    def _roots(self, tmp: str):
        canonical = Path(tmp) / "canonical"
        mirror = Path(tmp) / "mirror"
        canonical.mkdir()
        mirror.mkdir()
        return canonical, mirror

    def test_consistent_copies_are_ok_and_actual_path_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical, mirror = self._roots(tmp)
            (canonical / "a.json").write_text("same", encoding="utf-8")
            (mirror / "a.json").write_text("same", encoding="utf-8")
            actual = canonical / "a.json"
            report = workflow_integrity.check(
                files=["a.json"], roots=[canonical, mirror],
                actual_resolver=lambda _: str(actual),
            )
        self.assertTrue(report["ok"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["files"][0]["status"], "consistent")
        self.assertEqual(report["files"][0]["actual_hash"],
                         report["files"][0]["candidates"][0]["hash"])
        self.assertEqual(report["actual_root"], str(canonical))

    def test_hash_mismatch_reports_actual_path_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical, mirror = self._roots(tmp)
            (canonical / "a.json").write_text("A", encoding="utf-8")
            (mirror / "a.json").write_text("B", encoding="utf-8")
            actual = mirror / "a.json"
            report = workflow_integrity.check(
                files=["a.json"], roots=[canonical, mirror],
                actual_resolver=lambda _: str(actual),
            )
        self.assertFalse(report["ok"])
        entry = report["files"][0]
        self.assertEqual(entry["status"], "hash_mismatch")
        self.assertEqual(entry["actual_hash"], entry["candidates"][1]["hash"])
        self.assertTrue(any("hash 不一致" in warning for warning in report["warnings"]))

    def test_missing_copy_is_reported_without_blocking_consistent_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical, mirror = self._roots(tmp)
            (canonical / "a.json").write_text("A", encoding="utf-8")
            actual = canonical / "a.json"
            report = workflow_integrity.check(
                files=["a.json"], roots=[canonical, mirror],
                actual_resolver=lambda _: str(actual),
            )
        self.assertFalse(report["ok"])
        entry = report["files"][0]
        self.assertEqual(entry["status"], "missing_copy")
        self.assertTrue(any("副本缺失" in warning for warning in report["warnings"]))

    def test_actual_missing_is_distinct_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical, _ = self._roots(tmp)
            report = workflow_integrity.check(
                files=["missing.json"], roots=[canonical],
                actual_resolver=lambda _: str(canonical / "missing.json"),
            )
        self.assertFalse(report["ok"])
        self.assertEqual(report["files"][0]["status"], "actual_missing")


if __name__ == "__main__":
    unittest.main()

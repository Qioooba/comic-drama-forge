# -*- coding: utf-8 -*-
"""P2-11 性能采样与 history 解析测试。"""

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

import performance_metrics  # noqa: E402
import analytics  # noqa: E402


class PerformanceMetricsTests(unittest.TestCase):
    def test_session_samples_writes_snapshot_and_analytics_event(self):
        events = []
        # ⭐ 2026-10-07 修计时竞态：原实现靠 `_SESSIONS[...]["started_monotonic"] -= 4.5`
        # 伪造耗时，于是 elapsed = 4.5 + 真实跑这段代码的耗时。而断言
        # `seconds_per_video_second == 0.562` 是 round(4.5/8, 3)，**只有 0.5ms 余量**：
        # 只要 begin 到 finish 之间机器卡过 0.5ms（IO、抗病毒、并发导入），实测值就
        # 变成 4.501 → 0.563 → 随机失败（实测在全量跑时偶发挂过一次，单跑必过）。
        # 改为注入假时钟，elapsed 恒等于 4.5。
        clock = {"t": 1000.0}

        def fake_monotonic():
            return clock["t"]

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(performance_metrics, "_DIR", tmp), \
             patch.object(performance_metrics, "_gpu_sample",
                          return_value={"vram_mb": 12000.5, "util": 77.0}), \
             patch.object(performance_metrics, "_windows_system_sample",
                          return_value={"cpu": 42.0, "ram_mb": 16000.0}), \
             patch.object(performance_metrics.time, "monotonic", fake_monotonic), \
             patch.object(analytics, "record_event",
                          side_effect=lambda **kw: events.append(kw) or True):
            performance_metrics.begin("perf-1", context={
                "node_count": 12, "segment_count": 4,
                "total_frames": 192, "fps": 24,
            })
            performance_metrics._sample_once()
            clock["t"] = 1004.5          # 精确推进 4.5s，不再依赖真实墙钟
            performance_metrics.attach_history("perf-1", {
                "status": {
                    "completed": True, "status_str": "success",
                    "messages": [
                        ["execution_cached", {"nodes": ["83", "84"]}],
                        ["execution_progress", {"timestamp": 100.0}],
                        ["execution_success", {"timestamp": 104.5}],
                    ],
                },
                "performance": {"sampler": {"execution_time": 3.5}},
            })
            metrics = performance_metrics.finish("perf-1")
            path = Path(tmp) / "perf-1.json"
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(metrics["vram"]["peak"], 12000.5)
        self.assertEqual(metrics["gpu_util"]["peak"], 77.0)
        self.assertEqual(metrics["system_cpu"]["peak"], 42.0)
        self.assertTrue(saved["success"])
        self.assertEqual(saved["execution_cached_count"], 2)
        self.assertEqual(saved["seconds_per_segment"], 1.125)
        self.assertEqual(saved["seconds_per_video_second"], 0.562)
        self.assertTrue(any(k == "performance" for k in [e["kind"] for e in events]))

    def test_history_timing_walk_collects_loader_time(self):
        result = performance_metrics._history_metrics({
            "status": {"messages": []},
            "outputs": {},
            "node_meta": {"83": {"load_time_ms": 250}},
        })
        self.assertTrue(any("load_time_ms" in key
                            for key in result["node_times"]))

    def test_timeout_status_marks_failure(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(performance_metrics, "_DIR", tmp), \
             patch.object(performance_metrics, "_gpu_sample", return_value=None), \
             patch.object(performance_metrics, "_windows_system_sample",
                          return_value=None), \
             patch.object(analytics, "record_event", return_value=True):
            performance_metrics.begin("timeout-1")
            performance_metrics.attach_history(
                "timeout-1", {"status": {"status_str": "timeout"}})
            metrics = performance_metrics.finish("timeout-1")
        self.assertFalse(metrics["success"])


if __name__ == "__main__":
    unittest.main()

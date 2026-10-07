# -*- coding: utf-8 -*-
"""总控状态真值回归测试（2026-10-07 实测事故）。

事故复盘（这些用例就是把它钉死）：
    run-once 正在跑「打耳光」第 1 集，托管守护线程每 45~60 秒挑中同一集 → 集级锁返回
    busy → 旧代码先 ``_set_current("开始生产")`` 再 ``_clear_current()``，把 run-once 写在
    同一个 ``_STATE["current"]`` 槽里的实时进度**反复抹掉**（20 分钟内 23 次）。
    于是 ``/api/autopilot/status`` 长期返回 ``running=true, current=null``，
    ``/api/autopilot/progress/<项目>`` 又按「已完成集」算、把在跑的集说成 ``todo 0%`` ——
    AI 总控如实转述成「当前没有正在跑的环节 / 这一集还没真正进入流水线」，
    用户看到的就是「任务明明在跑，总控却说没跑」。

本文件覆盖四件事：
1. 进度写入带执行体身份，非持有者不能清空（``_clear_current`` 不越权）；
2. 托管 busy **预检**在任何写入之前返回 —— 不再覆盖/清空别人的实时进度；
3. ``status()`` 在 current 为空或被别的项目占用时，仍能给出「谁在跑哪一集」
   （``running_episodes`` 真值源：内存登记表 ∪ 磁盘集级租约）；
4. ``project_progress()`` 把在跑的集标成 ``running`` 而不是 ``todo 0%``。
"""

from __future__ import annotations

import copy
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import autopilot  # noqa: E402
import pipeline  # noqa: E402
import task_lease  # noqa: E402


def _live_progress(**over):
    """一条「run-once 正在跑资产」的进度快照。"""
    rec = {"project": "打耳光", "episode": 1, "title": "第1章 开除通知书",
           "step": "assets", "phase": "assets:scene",
           "message": "生成scene资产 2/4：君悦酒店二楼 机位档「顶部鸟瞰」",
           "percent": 27, "steps_done": ["script", "tts_pre"], "retries": 0,
           "started_at": "2026-10-07 15:59:41"}
    rec.update(over)
    return rec


class _AutopilotStateIsolated(unittest.TestCase):
    """保存/恢复模块级内存态，避免用例之间互相污染（也不碰真实生产状态）。"""

    def setUp(self):
        self._saved_current = copy.deepcopy(autopilot._STATE.get("current"))
        self._saved_runs = copy.deepcopy(autopilot._RUNS)
        self._saved_last_runs = copy.deepcopy(autopilot._STATE.get("last_runs"))
        self._saved_last_run = copy.deepcopy(autopilot._LAST_RUN)
        autopilot._STATE["current"] = None
        autopilot._RUNS.clear()
        autopilot._STATE["last_runs"] = {}
        autopilot._LAST_RUN.update({"key": None, "repeat": 0})

    def tearDown(self):
        autopilot._STATE["current"] = self._saved_current
        autopilot._RUNS.clear()
        autopilot._RUNS.update(self._saved_runs)
        autopilot._STATE["last_runs"] = self._saved_last_runs
        autopilot._LAST_RUN.update(self._saved_last_run)


class OwnershipTests(_AutopilotStateIsolated):
    def test_set_current_stamps_exec_identity(self):
        autopilot._set_current(**_live_progress())
        cur = autopilot._STATE["current"]
        self.assertEqual(cur["exec_id"], autopilot._exec_id())
        self.assertTrue(cur["owner"])
        self.assertGreater(cur["started_ts"], 0)
        # 登记表按 (项目, 集) 记账且为活跃
        self.assertTrue(autopilot._RUNS["打耳光#1"]["active"])

    def test_repeated_pings_keep_run_start_and_do_not_duplicate(self):
        """run-once 每次上报都带 started_at —— 起始时刻不能被刷新成 0 秒前，也不能重复记账。"""
        autopilot._set_current(**_live_progress())
        first_ts = autopilot._STATE["current"]["started_ts"]
        autopilot._set_current(project="打耳光", episode=1, step="storyboard", percent=34,
                               started_at="2026-10-07 15:59:41")
        cur = autopilot._STATE["current"]
        self.assertEqual(cur["started_ts"], first_ts)
        self.assertEqual(cur["step"], "storyboard")
        self.assertEqual(cur["title"], "第1章 开除通知书")      # 同一集：字段要继承
        self.assertEqual(list(autopilot._RUNS.keys()), ["打耳光#1"])

    def test_foreign_executor_cannot_clear_live_progress(self):
        autopilot._set_current(**_live_progress())
        # 托管线程（身份不同）来清 → 必须被拒绝，进度原样保留
        self.assertFalse(autopilot._clear_current(exec_id="autopilot"))
        self.assertIsNotNone(autopilot._STATE["current"])
        self.assertEqual(autopilot._STATE["current"]["step"], "assets")
        self.assertTrue(autopilot._RUNS["打耳光#1"]["active"])
        # 持有者自己清 → 通过，且登记表转为非活跃（status 不会再用它派生 current）
        self.assertTrue(autopilot._clear_current(exec_id=autopilot._exec_id()))
        self.assertIsNone(autopilot._STATE["current"])
        self.assertFalse(autopilot._RUNS["打耳光#1"]["active"])

    def test_force_clear_is_reserved_for_delete(self):
        autopilot._set_current(**_live_progress())
        self.assertTrue(autopilot._clear_current(force=True))
        self.assertIsNone(autopilot._STATE["current"])

    def test_switching_project_does_not_inherit_previous_fields(self):
        autopilot._set_current(project="A", episode=3, title="A-3", step="video", percent=50)
        autopilot._set_current(project="B", episode=1, step="script", percent=4)
        cur = autopilot._STATE["current"]
        self.assertEqual(cur["project"], "B")
        self.assertEqual(cur["episode"], 1)
        # 关键：A 的集号/标题/百分比不得被 B 继承（否则总控会把 A 的进度说成 B 的）
        self.assertNotEqual(cur.get("title"), "A-3")
        self.assertEqual(cur["percent"], 4)


class BusyPrecheckTests(_AutopilotStateIsolated):
    """核心回归：托管轮转撞上「另一个执行体在跑」时，不得动别人的实时进度。"""

    def test_busy_precheck_does_not_touch_live_progress(self):
        autopilot._set_current(**_live_progress())
        before = copy.deepcopy(autopilot._STATE["current"])

        plan = {"project": "打耳光", "enabled": True, "max_episode_attempts": 2}
        pick = {"episode_no": 1, "chapter": {"title": "第1章 开除通知书"},
                "reason": "按章节顺序推进"}

        with patch.object(autopilot, "is_paused", lambda: False), \
                patch.object(autopilot, "enabled_projects", lambda: [plan]), \
                patch.object(autopilot, "_auto_revive", lambda *a, **k: 0), \
                patch.object(autopilot, "_pick_episode", lambda *a, **k: pick), \
                patch.object(pipeline, "is_episode_running", lambda *a, **k: True), \
                patch.object(pipeline, "run_episode",
                             side_effect=AssertionError("预检 busy 后绝不能再进 run_episode")):
            did = autopilot._one_round()

        self.assertTrue(did)                     # 本轮确实挑到了这一集
        after = autopilot._STATE["current"]
        self.assertIsNotNone(after, "托管 busy 预检把别人正在跑的进度清空了（本次事故本体）")
        self.assertEqual(after["step"], "assets")
        self.assertEqual(after["percent"], before["percent"])
        self.assertEqual(after["exec_id"], before["exec_id"])
        # 也不得写入任何运行结果（busy 不是一次生产）
        self.assertEqual(autopilot._STATE["last_runs"], {})
        # 但必须留下防自旋节流：刚刚试过这一集
        self.assertEqual(autopilot._LAST_RUN["key"], "打耳光#1")


class StatusTruthTests(_AutopilotStateIsolated):
    def _patched_status(self, project="打耳光", **kw):
        with patch.object(autopilot, "_restore_once", lambda: None), \
                patch.object(autopilot, "list_plans", lambda: []), \
                patch.object(autopilot, "read_last_run", lambda *a, **k: {}), \
                patch.object(autopilot, "production_curve", lambda *a, **k: {}):
            return autopilot.status(project, **kw)

    def test_running_episodes_from_lease_when_current_empty(self):
        """current 为空（正是事故发生时的状态）也必须有「谁在跑哪一集」的真话。"""
        now = time.time()
        lease = {"scope": "episode:打耳光#1", "stale": False, "owner": "pipeline",
                 "pid": 15384, "acquired_at": now - 1900, "heartbeat_at": now - 5}
        with patch.object(task_lease, "list_leases", lambda: [lease]):
            st = self._patched_status()
        self.assertIsNone(st["current"])
        self.assertEqual(len(st["running_episodes"]), 1)
        row = st["running_episodes"][0]
        self.assertEqual((row["project"], row["episode"]), ("打耳光", 1))
        self.assertGreaterEqual(row["elapsed_sec"], 1800)
        self.assertLessEqual(row["last_ping_sec"], 60)

    def test_running_episodes_in_brief_payload(self):
        """brief（AI 总控读的那条）必须带上 running_episodes，否则模型又只能看 current。"""
        now = time.time()
        lease = {"scope": "episode:打耳光#1", "stale": False, "owner": "pipeline",
                 "pid": 1, "acquired_at": now - 10, "heartbeat_at": now}
        with patch.object(task_lease, "list_leases", lambda: [lease]):
            st = self._patched_status(brief=True)
        self.assertIn("running_episodes", st)
        self.assertEqual(st["running_episodes"][0]["episode"], 1)

    def test_stale_lease_is_not_reported_as_running(self):
        """崩溃/删项目留下的过期租约不得被当成「正在跑」（幽灵任务）。"""
        now = time.time()
        lease = {"scope": "episode:第二记耳光#1", "stale": True, "owner": "pipeline",
                 "pid": 15924, "acquired_at": now - 6000, "heartbeat_at": now - 5000}
        with patch.object(task_lease, "list_leases", lambda: [lease]):
            st = self._patched_status(project="第二记耳光")
        self.assertEqual(st["running_episodes"], [])

    def test_status_derives_current_for_scoped_project_from_registry(self):
        """槽位被别的项目占着时，本项目仍要能看到自己的进度（而不是只有 other_project_running）。"""
        autopilot._set_current(project="打耳光", episode=2, title="第2章",
                               step="storyboard", percent=34)
        autopilot._set_current(project="别的项目", episode=1, title="第1章",
                               step="script", percent=4)
        with patch.object(task_lease, "list_leases", lambda: []):
            st = self._patched_status(project="打耳光")
        self.assertTrue(st["other_project_running"])
        self.assertIsNotNone(st["current"], "本项目的进度被别的项目占用槽位后彻底不可见了")
        self.assertEqual(st["current"]["project"], "打耳光")
        self.assertEqual(st["current"]["episode"], 2)
        self.assertEqual(st["current"]["step"], "storyboard")


class ProjectProgressTests(_AutopilotStateIsolated):
    def test_inflight_episode_marked_running(self):
        """在跑的集不能再显示成 todo 0%（总控据此得出「还没真正开始跑」的错误结论）。"""
        units = [{"episode_no": 1, "chapter": {"title": "第1章", "char_count": 1842},
                  "chapter_index": 1, "part": 1, "parts": 1},
                 {"episode_no": 2, "chapter": {"title": "第2章", "char_count": 900},
                  "chapter_index": 2, "part": 1, "parts": 1}]
        states = {1: {"state": "todo", "note": ""},
                  2: {"state": "done", "note": ""}}
        with patch.object(autopilot, "get_plan", lambda *a, **k: {}), \
                patch.object(autopilot, "_novel_meta", lambda *a, **k: {}), \
                patch.object(autopilot, "chapters_and_text", lambda *a, **k: ([], "")), \
                patch.object(autopilot, "episode_units", lambda *a, **k: units), \
                patch.object(autopilot, "_episode_state",
                             lambda project, no, plan, chapters: dict(states[no])), \
                patch.object(autopilot, "running_episodes",
                             lambda project="": [{"project": "打耳光", "episode": 1}]):
            prog = autopilot.project_progress("打耳光", plan={})
        rows = {r["episode_no"]: r for r in prog["episodes"]}
        self.assertEqual(rows[1]["state"], "running")
        self.assertIn("正在生产", rows[1]["note"])
        self.assertEqual(rows[2]["state"], "done")
        self.assertEqual(prog["done"], 1)


if __name__ == "__main__":
    unittest.main()

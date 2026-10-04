# -*- coding: utf-8 -*-
"""C6：橡皮章反证 —— 把 is_h3_family_lora 永远返回 True，再跑新增守卫。

若守卫仍全绿 => 那道白名单守卫是橡皮章；若变红 => 有效。
"""
import os
import runpy
import sys

ROOT = r"C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统"
APP = os.path.join(ROOT, "app")
GUARD = os.path.join(ROOT, ".workbuddy", "test", "verify_h3_template_and_lora_whitelist.py")
sys.path.insert(0, APP)
os.environ.setdefault("MJSCXT_AUTOPILOT", "0")

import comfyui_models as CM      # noqa: E402
import h3_segment_loras as L     # noqa: E402

# monkeypatch：白名单判据永远 True（模拟「守卫写成了橡皮章」的场景）
L.is_h3_family_lora = lambda name: True
CM.is_h3_family_lora = lambda name: True
print(f"[patched] L.is_h3_family_lora={L.is_h3_family_lora!r}")
print(f"[patched] CM.is_h3_family_lora={CM.is_h3_family_lora!r}")

rc = 0
try:
    runpy.run_path(GUARD, run_name="__main__")
except SystemExit as e:
    rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
print(f"[guard exit code under monkeypatch] {rc}")
print(f"[判据] 守卫变红(rc!=0)? {rc != 0}  -> "
      f"{'有效（非橡皮章）' if rc != 0 else '!! 橡皮章：白名单判据失效守卫仍全绿'}")

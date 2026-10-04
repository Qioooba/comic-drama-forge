# -*- coding: utf-8 -*-
"""A2：验证 h3_director_builder 的 import 期 assert 真能拦住 named/位置不一致。

做法：备份原文件 -> 用子进程外的写文件方式把 named 的 tau 改成 1.2（位置列表仍 1.3）
-> 用子进程 import h3_director_builder，看是否抛 AssertionError -> 恢复并核对 sha256。
"""
import hashlib
import os
import shutil
import subprocess
import sys

ROOT = r"C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统"
APP = os.path.join(ROOT, "app")
TARGET = os.path.join(APP, "h3_director_builder.py")
PY = r"C:/Python314/python.exe"


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


base_sha = sha(TARGET)
print(f"原始 sha256 = {base_sha}")

# 1) 先确认未变异时能正常 import
r0 = subprocess.run([PY, "-c", "import h3_director_builder as m; print('IMPORT_OK')"],
                    cwd=APP, capture_output=True, text=True)
print(f"[baseline] rc={r0.returncode} out={r0.stdout.strip()!r} err_tail={r0.stderr.strip()[-200:]!r}")

# 2) 变异：named tau 1.3 -> 1.2
orig = open(TARGET, "r", encoding="utf-8").read()
needle = '"tau": 1.3, "start_percent": 0.2'
assert needle in orig, "未找到变异锚点"
mut = orig.replace(needle, '"tau": 1.2, "start_percent": 0.2')
open(TARGET, "w", encoding="utf-8", newline="").write(mut)
print(f"[mutated] sha256 = {sha(TARGET)}")

try:
    r1 = subprocess.run([PY, "-c", "import h3_director_builder as m; print('IMPORT_OK')"],
                        cwd=APP, capture_output=True, text=True)
    print(f"[mutated-import] rc={r1.returncode}")
    print(f"[mutated-import] stderr_tail=\n{r1.stderr.strip()[-600:]}")
    caught = (r1.returncode != 0) and ("AssertionError" in r1.stderr or "不一致" in r1.stderr)
    print(f"[判据] import 被 assert 拦住: {caught}")
finally:
    # 3) 恢复
    open(TARGET, "w", encoding="utf-8", newline="").write(orig)
    print(f"[restored] sha256 = {sha(TARGET)}  match_baseline={sha(TARGET)==base_sha}")

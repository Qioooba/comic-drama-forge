"""pytest 全局夹具。

两件**必须**在这里做的事，缺一不可：

1. ``MJSCXT_AUTOPILOT=0``
   ``import app`` 会在模块末尾调度一次 autopilot 自动恢复生产
   （``app/api/_shared.py`` → ``_schedule_autopilot_boot``，默认 3 秒延时）。
   项目自己的 ``scripts/*.py`` 都设了这个变量，但测试没有 —— 于是任何
   ``import app`` 的用例会**真的把托管守护线程拉起来跑生产**，与正在挂机的
   服务抢同一块 GPU。项目注释里记录过这个坑（"实测踩过"），这里固化下来。

2. 把 ``app/`` 放进 ``sys.path``
   ``app/`` 不是包（无 ``__init__.py``），模块之间以裸名互相 import
   （``from domain import delivery``），所以必须让 ``app/`` 本身在路径上。
"""
import os
import sys

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

# 必须在任何 app 模块被 import 之前设好，否则调度已经挂上了。
os.environ.setdefault("MJSCXT_AUTOPILOT", "0")

if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
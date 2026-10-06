# -*- coding: utf-8 -*-
"""只读诊断链：以 SQLite ``mode=ro`` + ``query_only`` 读取已登记状态。

定位（评估文档 §P2-8 第 4 件）
---------------------------
「只读诊断」与「验收」是两件事。本脚本只回答**已登记状态是什么**：

  - 数据库能不能打开、schema 是第几版、登记了多少条状态
  - 哪些表缺失（登记表不完整本身就是信号）

它**不能**回答：

  - 成片质量如何（需要人看）
  - 依赖是否齐备（需要连 ComfyUI ``/object_info``，那是另一个工具的事）
  - ``workflow_hash`` 免重渲判据是否成立（需要重建工作流才能算）

**诊断不等于验收。** 真正的验收见 ``docs/release/g0-contract.json`` 的 ``quality_gates``。

只读保证（三重）
----------------
1. 连接串带 ``?mode=ro`` 且以 ``uri=True`` 打开 —— ⚠️ 少了 ``uri=True``，
   ``file:...?mode=ro`` 会被当成**普通文件名**静默建出一个空库（真实踩过的坑）；
2. 连上后再执行一次 ``PRAGMA query_only=1``（连接级第二道保险）；
3. 全程只发 SELECT，**不 import app**（避免 ``bind_app`` 的启动副作用）

退出码语义
----------
``0`` = 诊断**完成**。数据缺失/库不存在**不算失败** —— 诊断工具不该因为
「用户还没建过项目」就报错，那不是工具的问题。

但「登记表缺失」是**真信号**：已登记组件的表不见了，说明迁移没跑或 schema 不对。
这种情况本脚本给 ``exit 2``，让 CI 能拦住 —— 否则这道门禁永远是假绿。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MJSCXT_DATA_DIR") or ROOT)

# 已登记组件 → (数据库相对路径, 该组件应当存在的表)
EXPECTED: dict[str, tuple[str, tuple[str, ...]]] = {
    "tasks": ("output/tasks.db", ("tasks",)),
    "production_facts": ("output/facts.db",
                         ("generation_intents", "media_versions",
                          "selection_decisions", "approval_decisions")),
    "timeline": ("output/facts.db", ("timeline_revisions",)),
    "jobs": ("output/jobs.db", ("jobs", "attempts")),
    "delivery": ("output/delivery/delivery.db", ("delivery_packages",)),
}


def _reconfigure_stdout() -> None:
    """默认控制台是 GBK，打不出 ⚠/✅ 会直接 UnicodeEncodeError（真实踩过）。

    这是「门禁在默认环境下是假绿」的根因之一 —— 脚本崩了，
    CI 只看到 exit 1，却以为是「诊断发现��题」。这里对齐
    ``scripts/check_generated_client.py`` 的既有做法。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 某些被重定向的流不支持
            pass


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读方式打开；不存在则返回 None。"""
    if not db_path.is_file():
        return None
    uri = "file:{}?mode=ro".format(str(db_path).replace("\\", "/").replace("?", "%3f").replace("#", "%23"))
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.execute("PRAGMA query_only=1")
    return conn


def _tables(conn: sqlite3.Connection) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def main() -> int:
    _reconfigure_stdout()
    print("=" * 70)
    print("只读诊断  ·  comic-drama-forge")
    print("=" * 70)

    present = 0
    problems: list[str] = []

    for name, (rel, expect_tables) in EXPECTED.items():
        db_path = DATA_DIR / rel
        conn = open_readonly(db_path)
        if conn is None:
            print(f"  [未登记] {name:18} {rel}")
            continue
        try:
            tables = _tables(conn)
            present += 1
            missing = [t for t in expect_tables if t not in tables]
            if missing:
                problems.append(f"{name}: 缺表 {missing}（{rel}）")
                print(f"  [表缺失] {name:18} {rel}  缺 {missing}")
            else:
                counts = []
                for t in expect_tables:
                    try:
                        n = conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
                        counts.append(f"{t}={n}")
                    except sqlite3.Error as e:
                        counts.append(f"{t}=<{type(e).__name__}>")
                print(f"  [已登记] {name:18} {rel}  {' '.join(counts)}")
            if "schema_version" in tables:
                try:
                    rows = conn.execute(
                        "SELECT module, version FROM schema_version ORDER BY module"
                    ).fetchall()
                    if rows:
                        print(f"{'':22}schema_version: " +
                              ", ".join(f"{m}={v}" for m, v in rows))
                except sqlite3.Error:
                    pass
        finally:
            conn.close()

    print("\n" + "-" * 70)
    print("已登记组件:", present, "/", len(EXPECTED))
    print("\n诊断**不等于**验收。本脚本不替代以下任何一项：")
    print("  · 成片质量的人工判断")
    print("  · ComfyUI 依赖自检（需 /object_info，另一个工具）")
    print("  · workflow_hash 免重渲判据 —— 需要重建工作流才能算")
    print("真正的验收见 docs/release/g0-contract.json 的 quality_gates。")

    if problems:
        print("\n[!] 登记表异常：")
        for p in problems:
            print(f"    - {p}")
        # 数据库存在但表缺失 = 迁移没跑 / schema 不对 → 真信号，exit 2
        return 2

    print("\n结果：已登记组件的表结构完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# -*- coding: utf-8 -*-
"""导出 Flask 全量路由快照，用于「Blueprint 拆分前后 URL 逐字不变」的守门。

为什么需要它
------------
``app/app.py`` 曾是 16,844 行 / 242 条路由的单文件。本次拆分把它按域搬进
``app/api/*.py``，**搬迁的风险不是写错逻辑，而是漏搬 / 改路径**：
前端 ``frontend/src/api/client.ts`` 1,508 行全靠字符串拼 URL，改一个字符就是 404，
而这种错误在「只审核不测试」的一轮里不会被任何用例拦住。

所以把「全部 rule + methods」当**契约**冻结下来：拆分前后逐字 diff，不一致即回滚。

安全边界
--------
本脚本只做 ``import`` + 读 ``url_map``，**不启动 WSGI 服务器、不监听端口、不连 ComfyUI、不碰 GPU**。
但 ``import app`` 会在模块末尾调度 autopilot 托管恢复（会真的续跑生产），所以：

    MJSCXT_AUTOPILOT=0 必须设置，否则脚本会顺手把用户的生产队列跑起来。

用法::

    MJSCXT_AUTOPILOT=0 python scripts/snapshot_routes.py out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# app/ 目录直接进 sys.path（无 app/__init__.py，模块以裸名互相 import）
sys.path.insert(0, str(_ROOT / "app"))

# 必须在 import app 之前设：否则模块级 _schedule_autopilot_boot 会续跑生产
os.environ.setdefault("MJSCXT_AUTOPILOT", "0")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


def collect(app_obj) -> dict:
    rules = []
    for r in app_obj.url_map.iter_rules():
        methods = sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))
        rules.append({
            "rule": str(r.rule),
            "endpoint": r.endpoint,
            "methods": methods,
        })
    rules.sort(key=lambda x: (x["rule"], x["endpoint"]))
    return {"count": len(rules), "rules": rules}


def diff(old_path: Path, new_path: Path, fail_on_added: bool = False) -> int:
    """返回**不可接受**的漂移处数量；0 表示门禁通过。

    ``old_path`` 是拆分前的基线，``new_path`` 是拆分后的现状。
    打印时必须标出方向，否则「丢失/新增」容易被读反（本次就踩过一次）。

    为什么「新增」默认不判失败（2026-10-07 修正）
    ----------------------------------------------
    协调书铁律 R1 是「**既有** URL 逐字不变」——前端 1508 行全靠字符串拼 URL，
    改一个既有字符就 404。新增端点不违反 R1，而且本轮就是**设计性地**新增：
    W2/W3 加了生产事实、任务、契约、交付、授权、风格、时间线等共 56 条。

    旧实现 ``return len(lost) + len(added)`` 导致：只要新增一条，退出码就非 0。
    叠加本轮 56 条新增，这道门禁**永远不可能通过** —— 一道永远红的门禁不是门禁，
    它只会训练所有人忽略退出码。本函数此前一度被记成「EXIT=0 全绿」，那是误读
    输出文本、没看真实退出码，属于比缺陷更伤信任的错误，故一并写在此处。

    真要严判「本轮不该有任何新增」时，用 ``--fail-on-added`` 显式打开。
    """

    old = json.loads(old_path.read_text(encoding="utf-8"))
    new = json.loads(new_path.read_text(encoding="utf-8"))

    def keyset(doc):
        # endpoint 名允许随蓝图改名而变（app.py → projects.list_x），
        # 因此比对只看 **路径 + 方法**，那才是前端真正依赖的契约。
        return {(r["rule"], tuple(r["methods"])) for r in doc["rules"]}

    ko, kn = keyset(old), keyset(new)
    lost, added = sorted(ko - kn), sorted(kn - ko)
    print(f"基线(base) : {old_path.name}  {len(ko)} 条")
    print(f"现状(now)  : {new_path.name}  {len(kn)} 条")

    if lost:
        print(f"\n❌ 既有 URL 丢失/改路径 {len(lost)} 处（违反协调书 R1，不可接受）：")
        for item in lost:
            print(f"  [丢失于现状] {item[0]}  {item[1]}")
        if added:
            print(f"  （另有 {len(added)} 处新增，一并列出供人工确认）")
            for item in added:
                print(f"  [现状新增]   {item[0]}  {item[1]}")
        return len(lost)

    if added:
        print(f"\n✅ 既有 URL 契约零丢失：{len(ko)} 条 (rule, methods) 逐字保留")
        print(f"ℹ 另有 {len(added)} 处**新增**端点 —— 不违反 R1，默认不判失败。")
        for item in added:
            print(f"  [现状新增]   {item[0]}  {item[1]}")
        if fail_on_added:
            print("\n❌ --fail-on-added 已开启：本次存在新增端点，判定失败。")
            return len(added)
        return 0

    print(f"\n✅ URL 契约一致：{len(ko)} 条 (rule, methods) 无丢失、无改路径、无新增")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 / 比对 Flask 路由快照")
    ap.add_argument("out", nargs="?", help="输出 JSON 路径")
    ap.add_argument("--diff", metavar="NOW",
                    help="与基线比对。第一个位置参数是基线(base)，--diff 是现状(now)")
    ap.add_argument("--fail-on-added", action="store_true",
                    help="把「新增端点」也算作失败（默认只对丢失/改路径失败，见 diff() 注释）")
    args = ap.parse_args()

    if args.diff:
        return 1 if diff(Path(args.out), Path(args.diff), args.fail_on_added) else 0

    import app as app_mod  # noqa: PLC0415  必须在设好环境变量之后

    doc = collect(app_mod.app)
    out = Path(args.out or "_route_snapshot_after.json")
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"已写出 {out}：{doc['count']} 条规则")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# -*- coding: utf-8 -*-
"""把 ``transition-all`` 收敛为 ``transition-colors``（§5.4 动效规范）。

为什么：``transition-all`` 会让**每一次重排**都跑一遍全属性过渡。长列表
（分镜画布、日志流、61 张风格缩略图）在换筛选/缩放/轮询刷新时会明显掉帧。
绝大多数用点其实只涉及颜色（``hover:bg-*`` / ``border-*`` / ``text-*``），
因此收敛到 ``transition-colors`` 观感不变但开销大幅下降。

⚠️ 少数确实涉及 transform/opacity 的（如进度条宽度、hover 上移）需要保留
``transition-transform`` / ``transition-opacity``，本脚本会跳过它们并在
输出里列出来供人工确认。

⚠️ **本脚本会改码，不是检查脚本。** 把 ``transition-all`` 收敛成
``transition-colors`` 会*静默改变*那些实际在过渡 ``border-radius`` /
``grid-template-columns`` / ``box-shadow`` 等非颜色属性的元素 —— 脚本的
提示词表只认得 transform 系列，认不出的语义一律按颜色处理。因此：
默认先跑 ``--dry-run`` 复核，确认每处用点真的只涉及颜色再改写。
（保留本脚本的理由见 docs/release/g0-contract.json required_components：
它是 ADR-0011 里「豁免该报哪几处」的成文约定，不是跑完即弃的一次性脚本。）

用法::

    python scripts/fix_transition_all.py --dry-run   # 只报告（默认走这条）
    python scripts/fix_transition_all.py            # 改写
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"

#: 同行出现这些属性时，transition-all 覆盖的是 transform/opacity，
#: 不能简单换成 colors，必须人工判断。
_TRANSFORM_HINTS = ("translate", "scale-", "rotate", "opacity", "shadow")

_SUFFIXES = {".tsx", ".ts"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    changed = 0
    skipped = []
    for p in sorted(SRC.rglob("*")):
        if p.suffix not in _SUFFIXES or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        if "transition-all" not in text:
            continue
        out_lines = []
        touched = False
        for i, line in enumerate(text.splitlines(keepends=True), 1):
            if "transition-all" in line:
                if any(h in line for h in _TRANSFORM_HINTS):
                    skipped.append(f"{p.relative_to(ROOT).as_posix()}:{i}  {line.strip()[:90]}")
                    out_lines.append(line)
                    continue
                new = line.replace("transition-all", "transition-colors")
                out_lines.append(new)
                touched = True
                changed += 1
            else:
                out_lines.append(line)
        if touched and not args.dry_run:
            p.write_text("".join(out_lines), encoding="utf-8")

    print(f"{'DRY-RUN: ' if args.dry_run else ''}已收敛 {changed} 处 transition-all -> transition-colors")
    if skipped:
        print(f"\n以下 {len(skipped)} 处涉及 transform/opacity，**保留** transition-all 并需人工确认：")
        for s in skipped:
            print(f"  {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
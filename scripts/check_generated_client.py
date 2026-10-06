# -*- coding: utf-8 -*-
"""CI 门禁：检查前端生成客户端是否过期（P1-6 契约优先的守门人）。

为什么需要它
------------
「契约优先」如果没有守门人，就只是多了一份会过期的文档。后端改了接口、
忘了跑生成，前端就会拿着旧类型继续编译通过 —— 直到用户在生产环境点下去
才炸。这个脚本把这件事变成 CI 里的一条**红色**。

判定方式（三项，缺一不可）
--------------------------
① **新鲜度**：重新渲染一遍生成物，与 ``frontend/src/api/generated/`` 的磁盘
   内容**逐字**比对。生成是确定性的（无时间戳、无随机 id），所以「不一致」
   只有一个含义：**生成物落后于契约**。

② **合法性**：生成物必须**本身就能通过 TypeScript 类型检查**。

③ **信封提升**：响应/请求体不得退化成 ``Record<string, unknown>``。

为什么必须是多项（2026-10-07 实测教训）
--------------------------------------
只做①时，一个**稳定产出非法 TS** 的生成器照样 PASS —— 因为
「生成物 == 规格」这个等式对着一份语法错误的内容依然成立。当时
``generated/index.ts`` 有 15 处 ``TS2300 Duplicate identifier``，本脚本
报「5 个文件逐字匹配」，而 ``frontend-types`` 全量 tsc 报 15 条错误。

反方向同样成立：③ 那种**合法但没类型**的产物能通过 tsc，只有文本检查
能抓。三项一起，本脚本才不是「只查新鲜度」。

所以本脚本与 ``frontend-types`` **不冗余**，且本脚本不能只当新鲜度检查用：
把合法性一起收进来，生成器自身的 bug 才会在 ``generated-fresh`` 这一道门
就变红。

⚠ **只对 ``generated/**` 单独跑 tsc**，绝不用全量 ``npx tsc --noEmit``：
后者会连带检查 ``src/features/**`` 等由别人负责、可能处于半完成状态的
文件，把别人的进度问题算到生成器头上。命令行参数与
``frontend/tsconfig.json`` 的关键项保持一致（那里是唯一真源）。

三种结局
--------
* 三项都过       → exit 0
* 任一项不过     → exit 1，并打印「哪一项、差在哪一行」
* 目录不存在/为空 → exit 1（别让「还没生成过」看起来像「通过」）

安全边界
--------
只读 + 纯计算：**不启动服务、不连 ComfyUI、不碰 GPU**，默认也**不 import app**。
类型检查只调用 ``tsc`` 的类型分析，不 emit、不构建。

用法::

    python scripts/check_generated_client.py            # CI 用，exit code 即结论
    python scripts/check_generated_client.py --fix      # 本地顺手重新生成
    python scripts/check_generated_client.py --diff     # 打印差异明细
    python scripts/check_generated_client.py --require-ts  # 缺 typescript 时也判失败
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import os
import re
import subprocess
import sys
from typing import List

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import generate_client  # noqa: E402  与本脚本同目录

DEFAULT_OUT = generate_client.DEFAULT_OUT

_FRONTEND_DIR = os.path.join(generate_client._ROOT, "frontend")
_TSC_JS = os.path.join(_FRONTEND_DIR, "node_modules", "typescript", "bin", "tsc")

#: 单独类型检查 ``generated/**`` 用的参数，取值与 ``frontend/tsconfig.json``
#: 的关键项一致；改那里就要同步改这里（唯一真源是 tsconfig.json）。
#: 不带 include 全量 src，是刻意的：见模块文档「只对 generated/** 单独跑」。
_TS_FLAGS = [
    "--noEmit",
    "--strict",
    "--target", "ES2020",
    "--module", "ESNext",
    "--moduleResolution", "bundler",
    "--skipLibCheck",
    "--lib", "ES2020,DOM,DOM.Iterable",
]

#: tsc 输出最多回显多少行（避免 CI 日志被刷爆；计数是真实的全量条数）。
_TS_OUTPUT_LIMIT = 40


def _read(path: str):
    """读生成物，并把 CRLF 归一为 LF。

    为什么归一：``git config core.autocrlf`` 会让同一份内容在不同机器上
    落成 LF 或 CRLF。逐字比对若不做归一，换台机器 clone 就会报「生成物过期」，
    而契约其实一点没变 —— 这种假红比没有门禁更糟，它会训练人忽略门禁。
    """
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def _rel(path: str) -> str:
    """把路径显示成「相对仓库根」，**跨盘时退回绝对路径**而不是抛异常。

    为什么不直接用 ``os.path.relpath``：Windows 上它要求两个路径在**同一个盘**，
    跨盘会抛 ``ValueError: path is on mount 'E:', start on mount 'H:'``。
    而 CI 完全可能把 ``--out`` 指向独立卷（构建缓存 / 临时盘），
    此时门禁会输出一段裸 traceback —— 看不到「哪一项、差在哪一行」，
    排查成本远高于「生成物过期」本身。**任何情况下都不许抛裸 traceback。**
    """
    try:
        return os.path.relpath(path, generate_client._ROOT).replace("\\", "/")
    except ValueError:      # 跨盘（不同 mount）
        return os.path.abspath(path).replace("\\", "/")


def _undocumented_routes():
    """本该有 schema 却没有的路由（契约漂移自检）。

    ⚠️ 收口审核发现这里原先**只注册 3 个蓝图**（contracts/delivery/licensing），
    而 ``MANUAL_BLUEPRINTS`` 已声称覆盖 7 个 —— 另外四域
    （production_facts / timeline / jobs / styles）根本不在校验范围内，
    门禁报 0 属于**空转通过**。「白名单承诺 7、门禁只验 3」比没有门禁更危险：
    它让人以为这四域受保护。

    现在改为注册 ``MANUAL_BLUEPRINTS`` 承诺的**全部**蓝图。
    刻意**不**用 ``api.discover_modules()`` 全量扫描：那会把 200+ 条历史路由
    也纳入校验，而它们本就没有手工 schema（由反射覆盖），会让本项恒为红、
    从而被无视 —— 那同样是假门禁。

    ``/health``：各域的存活探针，属运维接口而非业务契约，显式豁免并写明理由；
    不豁免的话将来加探针就会误报，久了大家就会习惯性忽略这个红。
    """
    try:
        from flask import Flask
    except ImportError:
        return None  # 环境没装 flask：本项跳过，不误报
    from contracts import openapi as openapi_spec

    mods, failed = [], []
    for name in openapi_spec.MANUAL_BLUEPRINTS:
        try:
            mods.append(importlib.import_module("api." + name))
        except Exception:  # noqa: BLE001
            failed.append(name)
    if failed:
        return ["<模块导入失败，无法校验契约漂移>: {}".format(", ".join(sorted(failed)))]

    flask_app = Flask("contract-check")
    for module in mods:
        flask_app.register_blueprint(module.bp)

    found = openapi_spec.undocumented_routes(flask_app)
    health = [r for r in found if r.split(" ", 1)[-1].rstrip("/").endswith("/health")]
    real = [r for r in found if r not in health]
    if health:
        print("[契约自检] 已豁免 {} 个 /health 运维探针（不属业务契约）".format(len(health)))
    return real


def _typecheck_generated(out_dir: str, require_ts: bool = False):
    """只对 ``generated/**`` 里的 ``.ts`` 做一次类型检查。

    返回 ``(ok, lines)``：``ok`` 为 ``True`` 表示通过；``False`` 表示**确定
    编译不过**；``None`` 表示**没做成**（缺 typescript / node），此时按
    ``require_ts`` 决定是判失败还是放行 —— 默认放行是因为这台机器可能压根
    没装前端依赖，但**必须在输出里明说跳过**，不能让它看起来像「查过了」。
    """
    sources = sorted(
        os.path.join(out_dir, fn)
        for fn in os.listdir(out_dir)
        if fn.endswith(".ts") and os.path.isfile(os.path.join(out_dir, fn))
    ) if os.path.isdir(out_dir) else []
    if not sources:
        return False, ["生成物目录里没有 .ts 文件可检查：{}".format(out_dir)]

    if not os.path.isfile(_TSC_JS):
        msg = "已跳过类型检查：找不到 {}（前端依赖未安装？）".format(_rel(_TSC_JS))
        if require_ts:
            return False, [msg, "处理方式：cd frontend && npm install（或加 --require-ts 之外先装依赖）"]
        return None, [msg]

    # 源文件一律传绝对路径：tsc 的 cwd 是 frontend/，相对路径会按它解析，
    # 于是任何非 frontend 下的 --out 都会拿到 TS6053 File not found。
    cmd = ["node", _TSC_JS] + _TS_FLAGS + [os.path.abspath(p) for p in sources]
    try:
        # errors="replace"：Windows 控制台默认 GBK，tsc 输出里的中文路径若解不开
        # 会抛 UnicodeDecodeError，把「类型检查」变成「检查脚本自己崩」。
        proc = subprocess.run(cmd, cwd=_FRONTEND_DIR, capture_output=True,
                              text=True, errors="replace")
    except OSError as exc:
        msg = "类型检查未执行：调用 tsc 失败（{}）".format(exc)
        return (False, [msg]) if require_ts else (None, [msg])

    if proc.returncode == 0:
        return True, ["生成物类型检查通过：{} 个文件，0 错误".format(len(sources))]

    raw = ((proc.stdout or "") + (proc.stderr or "")).strip()
    err_lines = [line for line in raw.splitlines() if line.strip()]
    lines = ["生成物类型检查失败（tsc exit {}，{} 个文件）：".format(
        proc.returncode, len(sources))]
    lines.extend("  " + line for line in err_lines[:_TS_OUTPUT_LIMIT])
    if len(err_lines) > _TS_OUTPUT_LIMIT:
        lines.append("  …… 另有 {} 行未回显（完整输出请手动跑 tsc）".format(
            len(err_lines) - _TS_OUTPUT_LIMIT))
    lines.append("")
    lines.append("处理方式：改 scripts/generate_client.py（生成物禁止手改），"
                 "再重跑 python scripts/generate_client.py")
    return False, lines


#: 未提升的信封特征：方法签名的响应类型或请求体参数退化成自由字典。
#:
#: 规格侧靠 ``_ref()``/``_ok()`` 把响应信封与请求体提为命名 component
#: （ADR-0004 的设计前提：否则生成物会退化成 ``Record<string, unknown>``）。
#: 但那只是**当前**的做法，生成器侧没有守护：新增一个用内联 object 的
#: operation，``ts_type()`` 会静默返回 ``Record<string, unknown>``，而这种产物
#: **能通过 tsc**（它语法与类型都合法），两道门禁都会放过。所以在这里补一条
#: 窄口径的文本检查，只盯响应类型与请求体参数这两个位置。
_UNNAMED_ENVELOPE = re.compile(
    r"request<[^>\n]*Record<string, unknown>|body\??: Record<string, unknown>")


def _untyped_envelopes(out_dir: str):
    """检查响应信封/请求体有没有退化成自由字典。返回 ``(ok, lines)``。"""
    if not os.path.isdir(out_dir):
        return True, []
    hits: List[str] = []
    for fn in sorted(os.listdir(out_dir)):
        if not fn.endswith(".ts"):
            continue
        text = _read(os.path.join(out_dir, fn)) or ""
        for idx, line in enumerate(text.splitlines(), start=1):
            if _UNNAMED_ENVELOPE.search(line):
                hits.append("  {}:{}: {}".format(fn, idx, line.strip()))
    if not hits:
        return True, ["信封提升检查通过：响应与请求体均为命名 component"]
    lines = ["信封未提升（{} 处响应/请求体退化成 Record<string, unknown>）：".format(len(hits))]
    lines.extend(hits[:_TS_OUTPUT_LIMIT])
    lines.append("")
    lines.append("处理方式：在 app/contracts/openapi.py 里用 _ref()/_ok() "
                 "把该信封登记为 components/schemas 下的命名 schema")
    return False, lines


def check(out_dir: str = DEFAULT_OUT, show_diff: bool = False, require_ts: bool = False):
    """新鲜度 + 合法性 + 信封提升三项检查。

    返回 ``(ok, report_lines, stale_files)``。
    """
    # 归一成绝对路径：tsc 的 cwd 是 frontend/，相对路径会按它解析。
    out_dir = os.path.abspath(out_dir)
    expected = generate_client.render_all()
    lines: List[str] = []
    stale: List[str] = []

    # ① 契约漂移：新增路由忘了登记 schema（端点能调通、规格里没有 —— 静默失败）
    undocumented = _undocumented_routes()
    if undocumented:
        lines.append("契约漂移：以下路由在蓝图里存在，但 OpenAPI 规格里没有对应 operation：")
        lines.extend("  - {}".format(item) for item in undocumented)
        lines.append("处理方式：在 app/contracts/openapi.py 的 _build_manual_paths() 里补上")
        lines.append("")
        stale.append("<契约漂移>")

    # ② 目录本身
    if not os.path.isdir(out_dir):
        lines.append("生成物目录不存在：{}".format(out_dir))
        lines.append("请运行：python scripts/generate_client.py")
        return False, lines, sorted(expected.keys())

    # ② 逐字比对每个应有文件
    for name in sorted(expected):
        want = expected[name]
        got = _read(os.path.join(out_dir, name))
        rel = _rel(os.path.join(out_dir, name))
        if got is None:
            stale.append(rel)
            lines.append("缺失：{}".format(rel))
            continue
        if got != _normalize(want):
            stale.append(rel)
            lines.append("已过期：{}".format(rel))
            if show_diff:
                lines.extend(_diff_lines(got, want, rel))
    # ③ 目录里多出来的文件（有人手改了生成物，或换了生成器版本没清干净）
    expected_names = set(expected)
    for fn in sorted(os.listdir(out_dir)):
        full = os.path.join(out_dir, fn)
        if os.path.isfile(full) and fn not in expected_names:
            rel = _rel(full)
            stale.append(rel)
            lines.append("多余（生成物禁止手改，请勿保留手工新增文件）：{}".format(rel))

    if not stale:
        lines.append("生成客户端与契约一致：{} 个文件逐字匹配".format(len(expected)))
    else:
        lines.append("")
        lines.append("生成客户端已过期（{} 个文件）：".format(len(stale)))
        lines.extend("  - {}".format(s) for s in stale)
        lines.append("")
        lines.append("处理方式：python scripts/generate_client.py")
        lines.append("如果接口确实没变却报过期，说明有人手改了生成物 —— "
                     "生成物禁止手改，改生成器（scripts/generate_client.py）。")

    # ④ 合法性：新鲜不等于合法。一个稳定产出非法 TS 的生成器，① 是抓不到的。
    ts_ok, ts_lines = _typecheck_generated(out_dir, require_ts=require_ts)
    lines.append("")
    lines.extend(ts_lines)

    # ⑤ 信封提升：合法但「没类型」的产物（Record<string, unknown>）tsc 抓不到
    env_ok, env_lines = _untyped_envelopes(out_dir)
    lines.append("")
    lines.extend(env_lines)
    return (not stale) and ts_ok is not False and env_ok, lines, stale


def _diff_lines(got: str, want: str, rel: str) -> List[str]:
    """行级差异（前 20 行，避免把 CI 日志刷爆）。"""
    diff = list(difflib.unified_diff(
        got.replace("\r\n", "\n").splitlines(),
        want.replace("\r\n", "\n").splitlines(),
        fromfile="{}（当前）".format(rel),
        tofile="{}（应生成）".format(rel),
        lineterm="",
        n=1,
    ))
    return ["    " + line for line in diff[:20]]


def main() -> int:
    ap = argparse.ArgumentParser(description="检查前端生成客户端是否过期且能编译（CI 门禁）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="生成物目录")
    ap.add_argument("--diff", action="store_true", help="打印行级差异")
    ap.add_argument("--fix", action="store_true", help="发现过期时直接重新生成")
    ap.add_argument("--require-ts", action="store_true",
                    help="缺 typescript 依赖时判失败（CI 建议加；本地无依赖时省略）")
    args = ap.parse_args()

    ok, lines, stale = check(args.out, show_diff=args.diff, require_ts=args.require_ts)
    print("\n".join(lines))

    if ok:
        return 0
    if args.fix:
        for path in generate_client.write_all(args.out, generate_client.render_all()):
            print("已重新生成 {}".format(_rel(path)))
        # 重新生成后必须**重跑一遍检查**，否则「重新生成了」会被当成「修好了」——
        # 生成器自己有 bug 时，重跑多少次都是同一份非法产物。
        ok2, lines2, _ = check(args.out, show_diff=args.diff, require_ts=args.require_ts)
        print("\n".join(lines2))
        return 0 if ok2 else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
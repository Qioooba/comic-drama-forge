# -*- coding: utf-8 -*-
"""令牌一致性审计：把设计规范变成可执行门禁。

为什么需要它
------------
改造前的设计规范**全部埋在 ``index.css`` 的注释里** —— 写得认真，但没有任何工具
读得到。后果是规则随时间腐化：有人加了个 ``dark:`` 变体、有人写了字面量 hex、
有人把 ``state-done`` 同时当成"已选中"和"已批准"，都没人会收到提醒。

本脚本把规范里**可静态判定**的部分变成 CI 门禁。它不检查审美，只检查规范里
明确写死的约束。每条规则都在 ``design-system/comicdrama/MASTER.md`` 里有出处。

用法::

    python scripts/audit_tokens.py            # 人读的报告
    python scripts/audit_tokens.py --strict   # 有违规即退出码 1（CI 用）

白名单
------
少数位置**必须**写字面量（``index.css`` 自身定义令牌、``index.html`` 的
防闪烁底色、``ThemeContext`` 的主题判定）。这些位置硬编码是**规范要求**，
不是违规 —— 列入 ``_ALLOWED_LITERAL_FILES`` 显式声明，而不是靠正则猜。

⚠️ **白名单本身也是门禁的一部分**（``WHITELIST001``）
---------------------------------------------------
2026-10-07 修正：原扫描根是 ``frontend/src/``，而白名单里却列了
``frontend/src/tailwind.config.js``（不存在）与 ``frontend/index.html``
（在扫描根之外）。这两条「显式豁免」实际上**什么都没豁免** —— 门禁看起来
管住了这两个文件，实则一次都没读过它们。这比没有门禁更危险。
现在：① 扫描根扩到 ``frontend/``，两者真正进入检查范围；② ``WHITELIST001``
断言每条白名单都**存在**且**落在扫描根内**，死条目今后会直接 FAIL。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SRC = ROOT / "frontend" / "src"

#: 扫描根：2026-10-07 由 ``frontend/src`` 扩到 ``frontend``，
#: 这样 ``index.html``（第三份颜色真源）与 ``tailwind.config.js`` 才真正被检查。
FRONTEND = ROOT / "frontend"

#: 依赖 / 产物目录：里面有海量 .ts/.js，且不是本项目的源
_EXCLUDED_DIRS = {"node_modules", "dist", "build", "coverage", ".git", ".vite", ".next"}

#: 这些文件按定义必须出现字面量：它们就是定义令牌 / 判定主题的地方。
#: 每一项都必须**真实存在于扫描根内** —— 由 WHITELIST001 强制。
_ALLOWED_LITERAL_FILES = {
    FRONTEND_SRC / "index.css",                   # 令牌定义本体
    FRONTEND / "index.html",                      # React 挂载前的防闪烁底色（由 BG001 交叉校验）
    FRONTEND_SRC / "context" / "ThemeContext.tsx",  # 主题判定逻辑
    # 独立的 Three.js 阻塞渲染探针：它是**出图输入参数**（背景不能是大面积
    # 近黑、角色配色），不是 UI 样式，且以 file:// 独立打开、读不到 index.css
    # 的 token。改成变量只会让它无法运行。
    FRONTEND / "public" / "te_3d_render" / "render.html",
}

_SUFFIXES = {".tsx", ".ts", ".css", ".html"}

# ---- 规则定义 -------------------------------------------------------------
# 每条： (规则 id, 说明, 违规谓词, 是否仅扫 tsx/ts)

_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_RGBA_RE = re.compile(r"\brgba?\(\s*\d")
_DARK_VARIANT_RE = re.compile(r"(?<![\w-])dark:")

# transition-all 在长列表上会让每次重排都跑全属性过渡
_TRANSITION_ALL_RE = re.compile(r"\btransition-all\b")


def _iter_source_files():
    for p in sorted(FRONTEND.rglob("*")):
        if p.suffix not in _SUFFIXES or not p.is_file():
            continue
        rel_parts = p.relative_to(FRONTEND).parts[:-1]
        if any(part in _EXCLUDED_DIRS for part in rel_parts):
            continue
        yield p


def audit_whitelist_integrity():
    """白名单条目必须**真实存在**且**真的落在扫描根内**。

    出处：审核发现 P1-7a。历史上 ``_ALLOWED_LITERAL_FILES`` 列了
    ``frontend/src/tailwind.config.js``（文件不存在）与 ``frontend/index.html``
    （在扫描根 ``frontend/src`` 之外）—— 两条都是**死条目**：它们让读者以为
    审计覆盖了这两个文件，实际一次都没读过。「看起来在管、实际没管」比没有
    白名单更危险，所以把白名单本身纳入门禁。
    """
    hits = []
    scanned = {p.resolve() for p in _iter_source_files()}
    for f in sorted(_ALLOWED_LITERAL_FILES, key=str):
        if not f.exists():
            hits.append((_rel(f), 0, "白名单条目指向的文件不存在 —— 死条目"))
        elif f.suffix in _SUFFIXES and f.resolve() not in scanned:
            hits.append((_rel(f), 0, "白名单条目在扫描根之外，实际不会被扫描 —— 死条目"))
    return hits


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def audit_hex_literals():
    """组件层禁止写 hex / rgba 字面量，一律引用 token。

    出处：MASTER.md §2「令牌表（唯一真源）」+ index.css 顶部约束 1。
    排除：数据可视化 SVG 里按分类取色的 ``fill="#xxx"`` 属正常用法 ——
    但为了不过早收紧，先只对**样式字符串**（className / style）报违规。
    """
    hits = []
    for p in _iter_source_files():
        if p in _ALLOWED_LITERAL_FILES:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _HEX_RE.search(line) or _RGBA_RE.search(line):
                hits.append((_rel(p), i, line.strip()[:110]))
    return hits


def audit_dark_variants():
    """组件层禁止写 ``dark:`` 变体，主题一律走 token 覆盖。

    出处：index.css 约束 4 + MASTER.md §2（令牌唯一真源）。
    历史：曾有 619 处因 ``.dark`` 从未被注入而**不可达**的 dark: 类。

    误报排除：``dark:`` 在 JS/TS 里还可能是 ``Record<ThemeMode, ...>`` 的**对象
    键**（如 Navbar 的 ``THEME_ICONS = { dark: (<svg/>) }``），那是主题枚举值，
    不是 Tailwind 变体类。故只匹配**字符串字面量内部**的 ``dark:``。

    ⚠️ 2026-10-07 修正：原先只扫 ``.tsx/.ts``，``.css`` 完全不设防 ——
    在样式文件里写 ``.btn:hover { }`` 之外的 ``dark:`` 覆盖同样会绕过主题
    token，而门禁一个字都不报。现已把 ``.css`` 纳入扫描（``.html`` 由
    HEX001 覆盖）。
    """
    hits = []
    # 在字符串字面量里找 dark:；排除 `dark: (` 这种对象键写法
    str_dark = re.compile(r"(['\"`])[^\n]*?(?<![\w-])dark:[a-z]")
    for p in _iter_source_files():
        if p in _ALLOWED_LITERAL_FILES:
            continue
        if p.suffix not in (".tsx", ".ts", ".css"):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if str_dark.search(line):
                hits.append((_rel(p), i, line.strip()[:110]))
    return hits


def audit_transition_all():
    """长列表禁用 ``transition-all``：只动 transform/opacity。

    出处：MASTER.md §4.4 invariant 14。

    白名单：``transition-all`` 与 transform/opacity **同行**出现时不算违规 ——
    那说明作者本意就是过渡位移/透明度（hover 上移、进度条宽度、弹窗入场），
    ``transition-all`` 是合理的写法。仅在纯颜色类上下文里用才是浪费开销。

    历史：本轮改造实测 26 处，其中 21 处收敛为 ``transition-colors``，
    5 处确属 transform/opacity 语义而保留。

    ⚠️ 2026-10-07 修正：白名单曾含 ``shadow``。``hover:shadow-md`` 是 **paint**
    属性，不是 transform/opacity —— 它每次重排都要重绘，和 ``transition-all``
    在长列表上是同一类开销，只是被白名单放行了。这与 invariant 14 的字面
    要求（只动 transform/opacity）不符，已移除。
    """
    hits = []
    # 只认真正的 transform/opacity 语义；shadow 属 paint，不在其中
    transform_hints = ("translate", "scale-", "rotate", "opacity")
    for p in _iter_source_files():
        if p.suffix not in (".tsx", ".ts"):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _TRANSITION_ALL_RE.search(line) and not any(h in line for h in transform_hints):
                hits.append((_rel(p), i, line.strip()[:110]))
    return hits


def audit_selected_approved():
    """不变量：``selected`` 永不显示为 ``approved``。

    出处：MASTER.md §2.4 + §6 invariant 2/3。

    静态可判定的部分：组件里若同时出现 ``selected`` 与 ``approved``
    的**样式类**且指向同一元素，多半是把两个决策合并渲染了。这条规则无法
    完全静态判定（需要运行时），因此这里只做**提醒级**检查：列出所有同时
    出现两档的文件供 review 逐个确认，而不是直接判失败。

    ⚠️ 2026-10-07 修正：**声明与实现对齐**。MASTER.md §2.4 原先写
    「``audit_tokens.py`` 静态校验这条」，但本规则只落在 ADVISORY_CHECKS、
    **不计入退出码**，且判据只是「同文件同时出现两个类名」，无法证明两者
    指向同一元素 —— 把它说成「静态校验」是**文档比实现强**，比没有门禁
    更危险：它让人以为这条最重要的语义不变量已经被机器守住。
    现已改为如实描述为「提醒级、需人工逐个确认」，并把输出显式标注为
    不参与门禁。真正要静态卡死它，需要的是「同一元素的 className 同时
    含 selected 与 approved」这类更精确的判据，留待后续。
    """
    both = []
    for p in _iter_source_files():
        if p.suffix not in (".tsx", ".ts"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        has_sel = re.search(r"\b(bg|text|border|ring)-selected\b", text)
        has_app = re.search(r"\b(bg|text|border|ring)-approved\b", text)
        if has_sel and has_app:
            both.append((_rel(p), "同时出现 selected 与 approved 样式类，请确认未把采用渲染成批准"))
    return both


_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _strip_css_comments(text: str) -> str:
    """先剥掉 ``/* … */`` 注释再解析选择器。

    ⚠️ 这不是洁癖，是必需的：注释里**只要出现一个字面量 ``:root {``**
    （哪怕是在解释这个脚本自己的文档里），朴素的 ``css.find(":root {")``
    就会把它当成真选择器，然后从那个 ``{`` 开始数括号 —— 把后面整份 CSS
    卷进同一个「块」里，甚至一路数到文件末尾还没归零，于是后续所有真实块
    一起消失。表现是 TOKEN001 静默失效：它既不报错也不报违规，只是
    什么都不检查了。**解析 CSS 必须先去注释。**
    """
    return _CSS_COMMENT_RE.sub("", text)


def _all_blocks_after(css: str, marker: str):
    """取**所有** ``marker {`` 之后到匹配顶层 ``}`` 之间的内容。

    为什么要「所有」：``index.css`` 曾有**两个** ``:root {}``（色值块 +
    尺寸块），旧实现用 ``css.find(":root {")`` 只取第一个 —— 第二块里的
    ``--control-h`` / ``--space-*`` / ``--radius-*`` 对 TOKEN001 **完全
    不可见**。只取第一个块是「实现细节泄漏成盲区」的典型：脚本自己不报错，
    门禁却漏检了一整段，还让人以为整份 CSS 都在检查内。
    （2026-10-07 已把两个块合并为唯一一个，此处保留多块遍历做纵深防御。）
    """
    css = _strip_css_comments(css)
    out = []
    pos = 0
    while True:
        idx = css.find(marker, pos)
        if idx < 0:
            return out
        start = css.find("{", idx)
        if start < 0:
            return out
        depth = 0
        for i in range(start, len(css)):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    out.append(css[start + 1:i])
                    pos = i + 1
                    break
        else:
            return out


def _token_rgb_hex(css: str, marker: str, token_name: str):
    """从 ``marker`` 命中的所有块里取 ``--token`` 的 RGB 三元组，转成 ``#RRGGBB``。"""
    pat = re.compile(re.escape(token_name) + r"\s*:\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*;")
    for block in _all_blocks_after(css, marker):
        m = pat.search(block)
        if m:
            r, g, b = (int(float(x)) for x in m.groups())
            return "#%02X%02X%02X" % (r, g, b)
    return None


#: 刻意跨主题共用的色值令牌（浅色块里**故意不重复定义**）。
#: 每一项都要能说出理由 —— 无理由的例外等于给规范开后门。
_SHARED_ACROSS_THEMES = {
    # 分类色板用于「区分」而非「表意」：关系图的 10 种关系、多序列折线图。
    # 它们必须跨主题保持**同一组色相**，否则同一张图在深浅两套皮肤下会
    # 变成两套不同的编码，用户无法建立「这条线=这个关系」的条件反射。
    "--viz-rose", "--viz-violet", "--viz-teal", "--viz-amber",
}


def audit_token_parity():
    """深色 ``:root`` 与浅色 ``:root.light`` 的令牌集必须对齐。

    出处：MASTER.md §2（两套皮肤都必须是完整可用的一套）。
    漏一个令牌 = 该语义在浅色下静默沿用深色值，是最难发现的视觉 bug。

    注意：只比对**色值**令牌（``--xxx: 1 2 3``）。尺寸类令牌（``--control-h``、
    ``--space-*``…）只在 :root 定义一次，浅色皮肤直接继承，这是设计意图。

    ⚠️ 2026-10-07 修正：改为遍历**所有** ``:root {}`` 块（见 ``_all_blocks_after``），
    不再只取第一个 —— 第二个 :root 块（尺寸类令牌）原先对这条规则完全不可见。
    """
    css = (FRONTEND_SRC / "index.css").read_text(encoding="utf-8", errors="replace")
    css = _strip_css_comments(css)   # 注释里的 `:root {` 会让解析错位，见 _strip_css_comments
    color_re = re.compile(r"(--[a-z0-9-]+)\s*:\s*[\d.]+\s+[\d.]+\s+[\d.]+\s*;")

    root_colors = set()
    for block in _all_blocks_after(css, ":root {"):
        root_colors |= set(color_re.findall(block))
    light_colors = set()
    for block in _all_blocks_after(css, ":root.light {"):
        light_colors |= set(color_re.findall(block))

    if not root_colors:
        return [("index.css", 0, "未能从任何 `:root {}` 块解析出色值令牌 —— 解析失败视为违规")]
    if not light_colors:
        return [("index.css", 0, "未找到 `:root.light {}` 块或其中无色值令牌 —— 浅色皮肤缺失")]

    missing = sorted((root_colors - light_colors) - _SHARED_ACROSS_THEMES)
    extra = sorted(light_colors - root_colors)
    hits = []
    # ⚠️ 必须返回三元组：main() 按 (path, line, text) 解包。
    #   原先这里返回二元组，一旦 TOKEN001 真的判失败，脚本会抛
    #   ValueError 崩掉 —— 门禁在**最该报警的时候**自己炸了，
    #   CI 上只会看到一个 traceback 而不是违规内容。恰好因为它一直 PASS，
    #   这个 bug 才活到今天。
    if missing:
        hits.append(("index.css", 0, f"浅色皮肤缺少色值令牌: {', '.join(missing)}"))
    if extra:
        hits.append(("index.css", 0, f"浅色皮肤有深色没有的色值令牌: {', '.join(extra)}"))
    return hits


def audit_boot_bg_parity():
    """``index.html`` 的首屏兜底底色必须与 ``index.css`` 的 ``--bg-canvas`` 一致。

    出处：MASTER.md §2.1（``--bg-canvas`` 是唯一真源）。

    为什么需要这条：``index.html`` 的防闪烁样式必须在 **index.css 加载之前**
    就生效，那时读不到 CSS 变量，只能硬编码 hex（``#020617`` / ``#F7F8FA``）。
    于是同一语义存在**第三份颜色真源**，与 index.css 之间没有任何交叉校验：
    改了 index.css 的画布色，首屏兜底底色会**静默错位** —— 表现为 React 挂载
    瞬间底色闪一下，全站最刺眼的那个位置。

    这类「必须硬编码」的位置靠正则豁免是安全的（本就必须写死），危险的是
    **写死之后没人比对**。所以豁免照旧（HEX001 里的白名单条目），但补一条
    交叉校验让它无法漂移。
    """
    hits = []
    css_path = FRONTEND_SRC / "index.css"
    html_path = FRONTEND / "index.html"
    if not css_path.exists() or not html_path.exists():
        return [("index.html/index.css", 0, "源文件缺失，无法校验首屏底色一致性")]

    css = css_path.read_text(encoding="utf-8", errors="replace")
    html = _strip_css_comments(html_path.read_text(encoding="utf-8", errors="replace"))

    # index.html：html { background: #xxx } 与 html.light { background: #xxx }
    html_bg = {}
    for m in re.finditer(r"html(\.light)?\s*\{[^}]*?background:\s*(#[0-9a-fA-F]{3,8})\s*;", html, re.S):
        html_bg[".light" if m.group(1) else ""] = m.group(2)

    # index.css：:root 与 :root.light 的 --bg-canvas
    css_bg = {
        "": _token_rgb_hex(css, ":root {", "--bg-canvas"),
        ".light": _token_rgb_hex(css, ":root.light {", "--bg-canvas"),
    }

    for key, label in (("", "深色（默认）"), (".light", "浅色 .light")):
        want, got = css_bg[key], html_bg.get(key)
        if want is None:
            hits.append(("frontend/src/index.css", 0, f"{label}未解析到 --bg-canvas，无法校验"))
        elif got is None:
            hits.append(("frontend/index.html", 0, f"{label}未解析到 html{key} 的 background 硬编码值"))
        elif got.upper() != want.upper():
            hits.append(("frontend/index.html", 0,
                         f"{label}首屏兜底底色 {got} 与 --bg-canvas {want} 不一致 —— 首屏会闪色"))
    return hits


CHECKS = [
    ("WHITELIST001", "白名单条目必须真实存在且落在扫描根内（禁止死条目）",
     audit_whitelist_integrity),
    ("HEX001", "组件层禁止 hex/rgba 字面量（令牌唯一真源）", audit_hex_literals),
    ("BG001", "index.html 首屏兜底底色必须等于 index.css 的 --bg-canvas",
     audit_boot_bg_parity),
    ("DARK001", "组件层禁止 dark: 变体（主题走 token 覆盖）", audit_dark_variants),
    ("TRANS001", "长列表禁用 transition-all（只动 transform/opacity）", audit_transition_all),
    ("TOKEN001", "深浅两套皮肤令牌集必须对齐", audit_token_parity),
]

ADVISORY_CHECKS = [
    ("SEL001", "提醒：同一文件同时出现 selected 与 approved，请确认语义未混淆",
     audit_selected_approved),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="设计令牌一致性审计")
    ap.add_argument("--strict", action="store_true", help="有违规即退出码 1")
    args = ap.parse_args()

    total = 0
    print("=" * 74)
    print("设计令牌审计  ·  comic-drama-forge")
    print("=" * 74)

    for rule_id, desc, fn in CHECKS:
        hits = fn()
        total += len(hits)
        status = "PASS" if not hits else f"FAIL ({len(hits)})"
        print(f"\n[{rule_id}] {status}  {desc}")
        for path, line, text in hits[:25]:
            print(f"    {path}:{line}")
            print(f"      {text}")
        if len(hits) > 25:
            print(f"    ... 另有 {len(hits) - 25} 处")

    for rule_id, desc, fn in ADVISORY_CHECKS:
        hits = fn()
        print(f"\n[{rule_id}] 提醒级（**不计入退出码**，需人工逐个确认）  {desc}")
        if hits:
            for path, note in hits[:15]:
                print(f"    {path}")
                print(f"      {note}")
            if len(hits) > 15:
                print(f"    ... 另有 {len(hits) - 15} 处")
        else:
            print("    无命中（注意：这不代表该不变量已被静态守住，见 MASTER.md §2.4）")

    print("\n" + "=" * 74)
    if total == 0:
        print("结果：通过 —— 令牌层无违规")
        return 0
    print(f"结果：{total} 处违规")
    if not args.strict:
        print("（加 --strict 可作为 CI 门禁；当前为报告模式）")
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
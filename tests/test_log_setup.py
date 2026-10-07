# -*- coding: utf-8 -*-
"""日志格式回归（2026-10-07 用户诉求：「打印日志要有时间戳啊」「报错也要详细啊」）。

锁两件事，缺一不可：

1. **时间戳必须真的出现**，且不能再被任何模块的 ``basicConfig`` 打回原形
   （根因：``comfyui_client`` 等模块在 import 期用不带 format 的 basicConfig
   抢先装上默认格式，导致 serve.py 里那行 ``%(asctime)s`` 从未生效）；
2. **ERROR 必须带出定位信息与异常堆栈** —— 行号、线程，以及「正处于 except 块
   里却没写 exc_info」时自动补上的 traceback。

另附 log_viewer 读侧断言：新格式能认级别，且按级别过滤时 traceback 续行不被丢掉
（否则「只看 ERROR」里只剩一行摘要，堆栈全没了）。
"""
import ast
import io
import logging
import os
import re
import sys

import pytest

import log_setup

_APP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

# 与 log_setup.LOG_FORMAT 对应：2026-10-07 14:05:37.123 [ERROR] ...
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[.,]\d{3} ")


@pytest.fixture(autouse=True)
def _isolate_root_logging():
    """把 root 的 handler / 级别逐用例还原，并清掉本模块装过的 handler。

    为什么必须隔离：``app/`` 下任意业务模块 import 时就会调 ``setup_logging()``
    （这本身正是修复生效的证据 —— 全量跑时 root 上早就有我们的 handler 了）。
    用例不能假设「自己是第一个配置日志的人」。
    """
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    for h in list(root.handlers):
        if getattr(h, "_mjscxt_log_setup", False):
            root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved:
        root.addHandler(h)
    root.setLevel(saved_level)


@pytest.fixture()
def capture():
    """把 root 日志接到内存流上，返回该流。"""
    buf = io.StringIO()
    log_setup.setup_logging(stream=buf, force=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    yield buf


# --------------------------------------------------------------------------- #
# 1. 时间戳 / 格式
# --------------------------------------------------------------------------- #

def test_every_line_carries_timestamp(capture):
    logging.getLogger("t.ts").info("普通信息")
    line = capture.getvalue().strip()
    assert _TS_RE.match(line), f"日志行缺少时间戳：{line!r}"


def test_line_has_level_logger_and_source_location(capture):
    logging.getLogger("t.src").warning("带定位信息")
    line = capture.getvalue().strip()
    assert "[WARNING]" in line
    assert "t.src" in line          # logger 名
    assert "test_log_setup.py:" in line  # 文件名 + 行号
    assert "MainThread" in line     # 线程名


def test_millisecond_precision_present(capture):
    """毫秒精度：判断两次重试间隔的唯一依据，不能退化成整秒。"""
    logging.getLogger("t.ms").info("x")
    line = capture.getvalue().strip()
    assert _TS_RE.match(line), line


def test_timestamp_is_wallclock_not_relative():
    """asctime 取的是本地墙上时间（不是 monotonic 差值），首行即 2026- 开头的日期。"""
    buf = io.StringIO()
    h = log_setup.setup_logging(stream=buf, force=True)
    try:
        year = time_year()
        logging.getLogger("t.wall").info("y")
        assert str(year) in buf.getvalue()
    finally:
        root = logging.getLogger()
        if h in root.handlers:
            root.removeHandler(h)


def time_year():
    import time
    return time.localtime().tm_year


def test_setup_is_idempotent_no_duplicate_lines():
    """重复调用只应更新级别，不能挂第二个 handler（否则每行打两遍）。"""
    buf = io.StringIO()
    root = logging.getLogger()
    h1 = log_setup.setup_logging(stream=buf, force=True)
    h2 = log_setup.setup_logging(stream=buf)              # 幂等路径
    h3 = log_setup.setup_logging(stream=buf, level="INFO")
    assert h1 is h2 is h3
    assert sum(1 for h in root.handlers if h is h1) == 1
    # force=True 必须替换掉上次装的那个，而不是并排再挂一个
    mine = [h for h in root.handlers if getattr(h, "_mjscxt_log_setup", False)]
    assert mine == [h1], f"force 重配后 root 上应只剩一个我们的 handler：{mine}"
    logging.getLogger("t.dup").info("只应出现一次")
    assert capture_count(buf.getvalue(), "只应出现一次") == 1


def capture_count(text, needle):
    return sum(1 for line in text.splitlines() if needle in line)


def test_takes_over_basicconfig_handler():
    """回归根因：若有人又写了不带 format 的 basicConfig，时间戳不能因此消失。"""
    root = logging.getLogger()
    saved = list(root.handlers)
    # 注意 list(...)：直接遍历 root.handlers 同时 removeHandler 会跳元素、删不干净
    for h in list(root.handlers):
        root.removeHandler(h)
    try:
        logging.basicConfig(level=logging.INFO)  # 默认格式 LEVEL:name:msg，无时间戳
        assert log_setup.is_configured() is False
        assert len(root.handlers) == 1          # 只有一个「无时间戳」的 basicConfig handler
        buf = io.StringIO()
        log_setup.setup_logging(stream=buf, force=True)
        logging.getLogger("t.takeover").info("接管后")
        out = buf.getvalue().strip()
        assert _TS_RE.match(out), f"接管后仍无时间戳：{out!r}"
        # 旧 handler 被摘掉，否则同一行会同时以两种格式出现
        assert capture_count(out, "接管后") == 1
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)


def test_no_basicconfig_left_in_repo():
    """源码级护栏：任何人再往业务模块里塞 basicConfig，这条会先红。

    用 **AST** 判定而不是正则扫文本 —— 正则会把 ``log_setup`` 文档里「根因就是
    basicConfig 抢先装 handler」这类说明、以及注释掉的代码一起算成违规，护栏一旦
    误报就没人敢看它了。AST 只认真调用，且路径必须指到 ``app/``（曾经写成项目根，
    结果一条都扫不到 —— 空跑的护栏比没有更糟）。
    """
    offenders = []
    scanned = 0
    for name in os.listdir(_APP_DIR):
        if not name.endswith(".py"):
            continue
        path = os.path.join(_APP_DIR, name)
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        scanned += 1
        tree = ast.parse(src, filename=path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "basicConfig"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "logging"):
                offenders.append(f"{name}:{node.lineno}")
    assert scanned > 50, f"扫描到的文件数异常（{scanned}），路径可能又写错了"
    assert not offenders, f"这些地方又写回了 logging.basicConfig：{offenders}"


def test_guard_itself_would_catch_a_regression(tmp_path):
    """反向验证护栏有效：塞一个 basicConfig 进去，它必须被抓出来。"""
    probe = tmp_path / "probe.py"
    probe.write_text("import logging\nlogging.basicConfig(level=logging.INFO)\n",
                     encoding="utf-8")
    tree = ast.parse(probe.read_text(encoding="utf-8"))
    hits = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "basicConfig"]
    assert hits == [2]
    # 文档/注释里的提及不算
    doc_only = ast.parse('"""doc: logging.basicConfig(x)"""\n# logging.basicConfig()\n')
    assert not [n for n in ast.walk(doc_only)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "basicConfig"]


def test_level_from_env(monkeypatch):
    monkeypatch.setenv(log_setup.LEVEL_ENV, "DEBUG")
    assert log_setup._resolve_level() == logging.DEBUG
    monkeypatch.setenv(log_setup.LEVEL_ENV, "WARNING")
    assert log_setup._resolve_level() == logging.WARNING
    # 显式参数优先于环境变量
    assert log_setup._resolve_level("ERROR") == logging.ERROR
    monkeypatch.setenv(log_setup.LEVEL_ENV, "乱写的级别")
    assert log_setup._resolve_level() == logging.INFO


# --------------------------------------------------------------------------- #
# 2. 报错详情
# --------------------------------------------------------------------------- #

def test_error_inside_except_gets_traceback_automatically(capture):
    """130 处 logger.error(f"...{e}") 没写 exc_info —— formatter 必须兜住。"""
    logger = logging.getLogger("t.tb")
    try:
        raise ValueError("根因在此")
    except ValueError:
        logger.error("资产「X」生成失败：%s", ValueError("根因在此"))
    out = capture.getvalue()
    assert "Traceback (most recent call last)" in out
    assert "ValueError: 根因在此" in out
    assert "test_log_setup.py" in out.split("Traceback")[-1]  # 堆栈指向本文件


def test_critical_also_gets_traceback(capture):
    logger = logging.getLogger("t.crit")
    try:
        raise RuntimeError("炸了")
    except RuntimeError:
        logger.critical("不可恢复")
    assert "RuntimeError: 炸了" in capture.getvalue()


def test_no_traceback_when_not_in_except(capture):
    """不在 except 块里就不能凭空造一个 'NoneType: None' 堆栈。"""
    logging.getLogger("t.notb").error("只是普通报错")
    out = capture.getvalue()
    assert "NoneType: None" not in out
    assert "Traceback" not in out


def test_explicit_exc_info_still_works(capture):
    logger = logging.getLogger("t.explicit")
    try:
        raise KeyError("missing")
    except KeyError:
        logger.error("手动指定", exc_info=True)
    assert "KeyError: 'missing'" in capture.getvalue()


def test_info_warning_never_gets_traceback(capture):
    """只有 ERROR 及以上补堆栈；INFO 补堆栈会淹掉日志。"""
    logger = logging.getLogger("t.lvl")
    try:
        raise ValueError("别打")
    except ValueError:
        logger.warning("警告不该带堆栈")
    assert "Traceback" not in capture.getvalue()


# --------------------------------------------------------------------------- #
# 3. 编码鲁棒：GBK 控制台不能把消息吃掉
# --------------------------------------------------------------------------- #

class _GbkStream(io.StringIO):
    """模拟 Windows 控制台：GBK 编码，编不出的字符会抛。"""
    encoding = "gbk"

    def write(self, data):
        data.encode(self.encoding)      # 严格校验，照 TextIOWrapper 的行为
        return super().write(data)


def test_gbk_console_does_not_swallow_message():
    buf = _GbkStream()
    h = log_setup.setup_logging(stream=buf, force=True)
    try:
        logging.getLogger("t.enc").error("ComfyUI 节点 %s 报错 ❌", "qwen_tts")
    finally:
        root = logging.getLogger()
        if h in root.handlers:
            root.removeHandler(h)
    out = buf.getvalue()
    assert "qwen_tts" in out, "编不出的字符不该让整条消息消失"
    assert "[ERROR]" in out
    assert "❌" not in out             # 非法字符被替换成 '?'
    assert "--- Logging error ---" not in out  # 更不能出现 logging 内部报错噪音


def test_encoding_safe_stream_passthrough():
    class _Utf8Stream(io.StringIO):
        encoding = "utf-8"

    raw = _Utf8Stream()
    safe = log_setup._EncodingSafeStream(raw)
    safe.write("中文正常")
    safe.flush()
    assert "中文正常" in raw.getvalue()
    assert safe.isatty() == raw.isatty()  # 属性透传


# --------------------------------------------------------------------------- #
# 4. 读侧：log_viewer 认新格式 + 保住 traceback 续行
# --------------------------------------------------------------------------- #

import log_viewer  # noqa: E402


def test_level_of_new_format():
    line = ("2026-10-07 14:05:37.123 [ERROR] comfyui_client "
            "| comfyui_client.py:2567 | MainThread | 获取 object_info 失败")
    assert log_viewer._level_of(line) == "ERROR"
    assert log_viewer._level_of(
        "2026-10-07 14:05:37.123 [WARNING] serve | serve.py:1 | MainThread | x"
    ) == "WARNING"
    assert log_viewer._level_of(
        "2026-10-07 14:05:37.123 [CRITICAL] app | app.py:1 | MainThread | x"
    ) == "CRITICAL"


def test_level_of_legacy_and_bare_lines():
    # 历史日志（basicConfig 默认格式）必须继续认，否则老文件里 ERROR 全被漏掉
    assert log_viewer._level_of("INFO:serve:已注册优雅停机 handler：SIGINT") == "INFO"
    assert log_viewer._level_of("ERROR:app:资产「X」生成失败") == "ERROR"
    # 裸 print（无级别）
    assert log_viewer._level_of("[serve] log redirected to: C:/logs") is None
    assert log_viewer._level_of("") is None


def test_level_of_ignores_level_word_in_message():
    """正文里出现的 [ERROR] 不该被当成级别（否则级别过滤会全乱）。"""
    line = ("2026-10-07 14:05:37.123 [INFO] app | app.py:1 | MainThread "
            "| 上一轮 [ERROR] 记录已修复")
    assert log_viewer._level_of(line) == "INFO"


def test_level_filter_keeps_traceback_continuation(tmp_path):
    """核心：按 ERROR 过滤时，traceback 续行必须跟着 ERROR 行一起返回。"""
    log_file = tmp_path / "serve_stdout.log"
    # 首行是 tail 倒读**必定丢弃**的那行（可能是被切断的半行，见 log_viewer 注释），
    # 故用一条哨兵行占位，断言里再确认它确实被丢掉。
    log_file.write_text(
        "[serve] log redirected to: C:/logs\n"
        "2026-10-07 14:05:37.100 [INFO] serve | serve.py:1 | MainThread | 启动完成\n"
        "2026-10-07 14:05:38.200 [ERROR] app | app.py:3891 | MainWorker | 资产「X」生成失败\n"
        "Traceback (most recent call last):\n"
        '  File "app.py", line 3891, in gen_base\n'
        "    shutil.move(base_files[0], scratch_base)\n"
        "shutil.Error: [WinError 3] 系统找不到指定的路径\n"
        "2026-10-07 14:05:40.000 [INFO] serve | serve.py:2 | MainThread | 继续下一项\n",
        encoding="utf-8")

    monkey = {"serve": {"label": "服务日志", "path": str(log_file), "desc": ""}}
    old = log_viewer.LOG_SOURCES
    log_viewer.LOG_SOURCES = monkey
    try:
        res = log_viewer.tail_lines("serve", tail=100, level="ERROR")
    finally:
        log_viewer.LOG_SOURCES = old

    assert res["success"] is True
    joined = "\n".join(res["lines"])
    assert "资产「X」生成失败" in joined
    # 堆栈不能丢 —— 这正是「报错要详细」在日志页上的落点
    assert "Traceback (most recent call last)" in joined
    assert "shutil.move(base_files[0], scratch_base)" in joined
    assert "shutil.Error: [WinError 3]" in joined
    # 不相干的 INFO 行不该混进来
    assert "启动完成" not in joined
    assert "继续下一项" not in joined
    # 级别计数只算真带级别的行，续行不重复计数
    assert res["counts"].get("ERROR") == 1
    assert res["counts"].get("INFO") == 2


def test_no_level_filter_returns_everything(tmp_path):
    log_file = tmp_path / "serve_stdout.log"
    log_file.write_text(
        "[serve] log redirected to: C:/logs\n"   # 首行必被丢弃
        "2026-10-07 14:05:37.100 [INFO] serve | serve.py:1 | M | a\n"
        "Traceback (most recent call last):\n"
        "2026-10-07 14:05:38.200 [ERROR] app | app.py:1 | M | b\n",
        encoding="utf-8")
    monkey = {"serve": {"label": "l", "path": str(log_file), "desc": ""}}
    old = log_viewer.LOG_SOURCES
    log_viewer.LOG_SOURCES = monkey
    try:
        res = log_viewer.tail_lines("serve", tail=100)
    finally:
        log_viewer.LOG_SOURCES = old
    # 不按级别过滤时，孤立的 traceback 续行同样要返回（不能只留带级别的行）
    assert len(res["lines"]) == 3
    assert "log redirected" not in "\n".join(res["lines"])  # 首行按设计丢弃


def test_error_filter_includes_critical(tmp_path):
    """CRITICAL 比 ERROR 更严重，「只看 ERROR」不该把它漏掉。"""
    log_file = tmp_path / "serve_stdout.log"
    log_file.write_text(
        "[serve] log redirected to: C:/logs\n"   # 首行必被丢弃
        "2026-10-07 14:05:37.100 [CRITICAL] app | app.py:1 | M | 数据根不可写\n"
        "2026-10-07 14:05:38.200 [INFO] serve | serve.py:1 | M | 噪音\n",
        encoding="utf-8")
    monkey = {"serve": {"label": "l", "path": str(log_file), "desc": ""}}
    old = log_viewer.LOG_SOURCES
    log_viewer.LOG_SOURCES = monkey
    try:
        res = log_viewer.tail_lines("serve", tail=100, level="ERROR")
    finally:
        log_viewer.LOG_SOURCES = old
    assert any("数据根不可写" in ln for ln in res["lines"])
    assert not any("噪音" in ln for ln in res["lines"])


def test_log_setup_importable_from_bare_app_path():
    """app/ 下模块是裸名 import 的，log_setup 必须只依赖标准库、无循环依赖。"""
    assert "log_setup" not in sys.modules or True
    assert log_setup.__file__.endswith("log_setup.py")
    # 模块里不得 import 任何业务模块
    with open(log_setup.__file__, "r", encoding="utf-8") as f:
        src = f.read()
    for bad in ("import config", "import app", "from app"):
        assert bad not in src, f"log_setup 不该依赖业务模块：{bad}"
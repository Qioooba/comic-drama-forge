# -*- coding: utf-8 -*-
"""统一日志配置（2026-10-07 新增）：**时间戳 + 报错详情**的唯一事实源。

## 为什么要单独一个模块（用户原话：「打印日志要有时间戳啊」「报错也要详细啊」）

现场日志长这样，一眼就知道问题出在哪、什么时候出的：

    INFO:serve:已注册优雅停机 handler：SIGINT

**没有时间戳**，也没有出错位置。根因是一次「谁先 basicConfig 谁说了算」的竞态：

1. ``serve.py`` 顶层 ``app = _load_flask_app()`` 会 exec 整份 ``app.py``，
   import 期先撞上 ``comfyui_client.py`` / ``script_generator.py`` 等模块里的
   ``logging.basicConfig(level=logging.INFO)`` —— **不带 format**，于是装上的是
   Python 默认格式 ``LEVEL:name:message``；
2. 真正的 ``logging.basicConfig(..., format="%(asctime)s ...")` 写在
   ``serve.py`` 的 ``if __name__ == "__main__":`` 里，**跑在第 1 步之后**；
3. ``logging.basicConfig`` 的语义是「root 上已有 handler 就什么都不做」，
   所以那行带 ``%(asctime)s`` 的配置**从头到尾没生效过**，时间戳就此蒸发。

修法不是「把时间戳那行提前」（那只是把竞态换了个方向，任何人以后再加一个模块级
basicConfig 就会复发），而是：

* 本模块 ``setup_logging()`` **幂等**，且会接管 root handler —— 谁先调用谁装上，
  后到的都是 no-op，格式永远一致；
* 全仓**不再有任何** ``logging.basicConfig``（连同那 5 个模块级调用一起清掉），
  入口（``serve.py`` / ``main.py``）在 exec app.py **之前**先调本模块。

## 报错为什么「详细」

``%(filename)s:%(lineno)d`` 给出行号，``%(threadName)s`` 给出线程（本项目是多线程
流水线：托管守护、ComfyUI 轮询、资产 worker 并行跑，不标线程根本分不清是谁报的）。

更重要的是 ``_DetailFormatter``：**ERROR/CRITICAL 若正处于 ``except`` 块中且调用点
没写 ``exc_info=True``，自动把当前异常的完整 traceback 挂上去**。全仓有 130 处
``logger.error(f"...：{e}")`` 这种只留一行消息、丢掉堆栈的写法，逐个改不现实也不
容易漏 —— 在 formatter 里兜住，一次性全部生效。

## 用法

    # 入口处，且必须在任何业务模块 import 之前
    import log_setup
    log_setup.setup_logging()

    # 临时调级别（排障用，不必改代码）
    set MJSCXT_LOG_LEVEL=DEBUG

⚠️ 本模块**只依赖标准库**：`app/` 下所有模块都是裸名互相 import（``app/`` 在
``sys.path`` 上），任何业务模块都可以安全地 ``import log_setup``，不会引入循环依赖。
"""

from __future__ import annotations

import logging
import os
import sys
import threading

__all__ = [
    "LOG_FORMAT",
    "DATE_FORMAT",
    "DEFAULT_LEVEL",
    "LEVEL_ENV",
    "setup_logging",
    "is_configured",
]

# 级别：完整日期 + 毫秒。本项目一次生产任务能跑几十分钟、单集日志上千行，
# 毫秒级时间戳是判断「谁先谁后 / 两次重试隔了多久」的唯一依据。
LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s "
    "| %(filename)s:%(lineno)d | %(threadName)s | %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_LEVEL = "INFO"
LEVEL_ENV = "MJSCXT_LOG_LEVEL"

# 标记自己装的 handler，便于幂等判定与接管
_MARK = "_mjscxt_log_setup"

_lock = threading.Lock()


def _resolve_level(level=None) -> int:
    """级别取值优先级：显式参数 > 环境变量 > DEFAULT_LEVEL。"""
    raw = level if level is not None else (os.environ.get(LEVEL_ENV) or "").strip()
    if not raw:
        return logging.getLevelName(DEFAULT_LEVEL)
    if isinstance(raw, int):
        return raw
    name = str(raw).upper()
    if name.isdigit():
        return int(name)
    resolved = logging.getLevelName(name)
    # getLevelName 对未知名字返回字符串 "Level XXX"，不是 int
    return resolved if isinstance(resolved, int) else logging.getLevelName(DEFAULT_LEVEL)


def _in_pytest() -> bool:
    """pytest 的 caplog / live-log 把捕获 handler 挂在 root 上，接管时必须放行。"""
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


class _EncodingSafeStream:
    """写之前先验编码，把「编不出来的字符」就地替换掉。

    Windows 控制台默认 GBK/CP936，日志里一个 emoji、``❌`` 或生僻字就会让
    ``stream.write()`` 抛 ``UnicodeEncodeError``。logging 捕获后打的是
    ``--- Logging error ---`` 的内部堆栈 —— **真正的业务消息反被这坨噪音盖掉**，
    正好和「报错要详细」背道而驰。

    只在**写之前**预检并降级，绝不「先写一半再重试」：``serve.py`` 的 ``_Tee``
    会把同一条内容同时送给控制台与日志文件，失败后重试会造成日志文件里出现
    半条 + 整条两行。预检一次就没有这个窗口。
    """

    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        enc = getattr(self._stream, "encoding", None)
        if enc:
            try:
                data.encode(enc)
            except (UnicodeEncodeError, LookupError):
                # 用目标流自己的编码做 replace，保证写进去的是合法字符
                data = data.encode(enc, errors="replace").decode(enc, errors="replace")
        return self._stream.write(data)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001 刷盘失败不该反过来搞挂日志
            pass

    def __getattr__(self, name):
        # isatty / fileno / buffer 等一律透传
        return getattr(self._stream, name)


class _DetailFormatter(logging.Formatter):
    """给没有 ``exc_info`` 的 ERROR/CRITICAL 自动补上当前异常堆栈。"""

    def format(self, record):
        if record.levelno >= logging.ERROR and not record.exc_info:
            exc_info = sys.exc_info()
            # exc_info[0] is None 表示当前不在 except 块里（如 collect-then-log 汇总）
            if exc_info and exc_info[0] is not None:
                record.exc_info = exc_info
                # 强制重算：同一 record 可能被多个 handler 格式化，留着旧文本会漏
                record.exc_text = None
        return super().format(record)


def is_configured() -> bool:
    """root 上是否已经挂了本模块装的 handler。"""
    return any(getattr(h, _MARK, False) for h in logging.getLogger().handlers)


def setup_logging(level=None, stream=None, force=False) -> logging.Handler:
    """配置 root logger，幂等。返回本模块装的 handler（便于测试与二次改流）。

    幂等语义：重复调用只更新**级别**，不重复挂 handler —— 否则每行日志会打两遍。
    ``force=True`` 用于「明知要重配」（如测试里换 stream 抓输出）。

    接管规则：清掉 ``logging.basicConfig()`` 装出来的 handler（``type`` 精确等于
    ``logging.StreamHandler`` 且无自定义 formatter 的那种），因为它正是时间戳消失
    的元凶；**子类的 handler 一律不动**（pytest 的 LogCaptureHandler、第三方库
    自带的 FileHandler 都在其列）。
    """
    root = logging.getLogger()
    with _lock:
        existing = [h for h in root.handlers if getattr(h, _MARK, False)]
        if existing and not force:
            if level is not None:
                root.setLevel(_resolve_level(level))
            return existing[0]

        if not _in_pytest():
            for h in list(root.handlers):
                # 只清「basicConfig 原样装的那个」：类不派生、formatter 是默认的
                basic = (type(h) is logging.StreamHandler
                         and type(getattr(h, "formatter", None)) is logging.Formatter)
                if basic:
                    root.removeHandler(h)
                    try:
                        h.close()
                    except Exception:  # noqa: BLE001
                        pass

        # force=True 表示「重配」：必须把**上一次自己装的**先摘掉，否则 root 上会
        # 同时挂着两个我们的 handler，同一行日志打两遍（实测踩过：force 后再走幂等
        # 路径，is_configured() 会把旧的那个当成第一个返回）。
        if force:
            for h in list(root.handlers):
                if getattr(h, _MARK, False):
                    root.removeHandler(h)
                    try:
                        h.close()
                    except Exception:  # noqa: BLE001
                        pass

        target = stream if stream is not None else sys.stderr
        handler = logging.StreamHandler(_EncodingSafeStream(target))
        handler.setFormatter(_DetailFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        setattr(handler, _MARK, True)
        handler.setLevel(logging.NOTSET)  # 级别由 root 统一管，别在 handler 上重复设
        root.addHandler(handler)
        root.setLevel(_resolve_level(level))

        # 捕获 warnings：DeprecationWarning 之类默认被 warning 过滤器压着不显示，
        # 接进日志后才看得到（本项目 Python/依赖版本偏旧，排障时很需要）
        logging.captureWarnings(True)
        return handler
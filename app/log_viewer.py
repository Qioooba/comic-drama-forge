# -*- coding: utf-8 -*-
"""服务日志查看（只读）。

背景（2026-09-29 用户反馈）：服务由计划任务 ``MJSCXT_Flask`` 经
``.workbuddy/run_serve.bat`` 后台启动，stdout/stderr 重定向到
``.workbuddy/test/_out/serve_stdout.log``。**没有终端窗口、也没有读取入口**，
一旦出事只能手动去翻那个深层目录下 2MB+ 的文件。这里把它接到 Web 上。

设计要点：

* **只读**：本模块不写、不清理、不 rotate 任何日志文件。
* **路径白名单**：source 只能取白名单里的 key，绝不接受任意路径参数 ——
  否则一个 ``../`` 就能把整台机器的任意文件通过 Web 读出来（路径穿越）。
* **编码鲁棒**：Windows 中文环境写出的日志是 **GBK**（用 UTF-8 解是乱码），
  因此按 utf-8 → gbk → latin-1 依次尝试，并回报实际使用的编码。
* **大文件友好**：2MB+ 甚至更大的日志不做全量读入，用**从尾部倒着读块**的方式
  取最后 N 行；``since`` 偏移支持只取新增部分（前端「自动刷新」用）。
"""

import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

# ⚠️ frozen 感知（2026-09-29 桌面版看不到日志的根因）：
#   源码模式下 __file__ 落在 <root>/app/log_viewer.py，上一级就是项目根，
#   日志目录 = <root>/.workbuddy/test/_out。
#   但 PyInstaller 打包后 __file__ 落在临时 _MEIPASS 目录，按它找 .workbuddy
#   必然「日志文件不存在」。打包版的本进程日志改写到 exe 同级的 logs/ 目录
#   （见 main.py 的 _redirect_stdouts），故 frozen 时优先定位 exe 同级 logs/。
def _resolve_log_dir() -> str:
    if getattr(sys, "frozen", False):
        # 单文件 exe：同级目录放 logs/
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "logs")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".workbuddy", "test", "_out")


_LOG_DIR = _resolve_log_dir()

# ⚠️ source -> 文件路径的白名单。新增日志源在此登记，**永不接受外部传入的路径**。
LOG_SOURCES = {
    "serve": {
        "label": "服务日志",
        "path": os.path.join(_LOG_DIR, "serve_stdout.log"),
        "desc": "Flask 后台服务（流水线、托管循环、资产生成）的 stdout/stderr",
    },
    "comfyui": {
        "label": "ComfyUI 日志",
        "path": os.path.join(_LOG_DIR, "comfyui_stdout.log"),
        "desc": "ComfyUI 自身日志（注意：可能是陈旧文件，是否更新取决于其启动方式）",
    },
}

DEFAULT_SOURCE = "serve"
DEFAULT_TAIL = 300
MAX_TAIL = 2000
# 单次倒读的上限，防止超大日志把请求拖死
_MAX_SCAN_BYTES = 8 * 1024 * 1024
# since 增量读取的单次上限
_MAX_FOLLOW_BYTES = 1024 * 1024

LEVELS = ("ERROR", "WARNING", "INFO", "DEBUG")

__all__ = ["LOG_SOURCES", "tail_lines", "available_sources", "DEFAULT_SOURCE",
           "DEFAULT_TAIL", "MAX_TAIL", "LEVELS"]


# --------------------------------------------------------------------------- #

def _resolve(source):
    """白名单解析，返回 ``(归一化 key, meta)``；未知 source 的 meta 为 None。

    归一化后的 key 必须回传给调用方：调用方可能传 ""（前端未选来源），此时实际读的是
    默认源，返回体就该写 "serve" —— 否则前端拿空 key 再去请求会陷入怪圈。
    """
    key = str(source or DEFAULT_SOURCE).strip().lower()
    return key, LOG_SOURCES.get(key)


def _decode(data):
    """按 utf-8 → gbk → gbk(replace) 尝试，返回 (文本, 实际编码)。

    ⚠️ 刻意**不放 latin-1**：Windows 中文环境的 stdout 是 GBK/CP936，而 latin-1 永远
    解码成功（任何字节都能映射成字符），一旦退到这里，整段就变成了看似正常实则错误的
    mojibake，排查时会彻底跑偏。宁可用 replace 丢掉一两个字节，也要保住其余正文。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("gbk", errors="replace"), "gbk(replace)"


def _read_tail_bytes(path, tail_lines):
    """从文件尾部倒着读块，返回 (bytes, 是否被扫描上限截断)。

    不一次 read() 整个文件：日志会长到几十 MB，那样会把内存和响应时间一起吃掉。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return b"", False
    if size <= 0:
        return b"", False

    step = 8192
    pos = size
    chunks = []
    scanned = 0
    need = tail_lines + 1          # +1 为丢弃可能被切断的首行
    found = 0
    while pos > 0 and found <= need:
        read_len = min(step, pos)
        if scanned + read_len > _MAX_SCAN_BYTES:
            # 到达扫描上限：把剩下的尾部数据补齐后返回
            if chunks:
                return b"".join(chunks), True
            with open(path, "rb") as f:
                f.seek(max(0, size - _MAX_SCAN_BYTES))
                return f.read(), True
        pos -= read_len
        scanned += read_len
        with open(path, "rb") as f:
            f.seek(pos)
            buf = f.read(read_len)
        chunks.insert(0, buf)
        found += buf.count(b"\n")

    raw = b"".join(chunks)
    if pos > 0:
        # ⭐ 关键：倒读的起点几乎必然落在**某个多字节字符的中间**（中文 GBK 一个字 2 字节）。
        # 带着半个汉字去解码，strict gbk 会直接抛 UnicodeDecodeError，然后一路退到
        # latin-1 —— 结果是整段变成 mojibake，比乱码还难救。这里对齐到行起始后再返回。
        nl = raw.find(b"\n")
        if nl >= 0:
            raw = raw[nl + 1:]
    return raw, False


def _read_since_bytes(path, since):
    """从 since 字节偏移读到文件末尾（增量 follow），返回 (bytes, 新偏移)。"""
    size = os.path.getsize(path)
    if since < 0 or since > size:
        # 文件被 rotate / 截断过，偏移量失效 → 从头重读（与 size 一致即从末尾）
        since = 0 if since > size else since
    with open(path, "rb") as f:
        f.seek(since)
        data = f.read(_MAX_FOLLOW_BYTES)
        offset = f.tell()
    # 审计 P2-9（2026-09-29）：按 1MB 字节边界截断可能切在 GBK 双字节中间 —— 下次
    # 从该偏移续读会产生半个字符。回退到最后一个换行之后再定偏移，保证每次增量
    # 读取都从完整行首开始（纯日志尾部无换行的极端情况保持原行为）。
    if offset < size:
        last_nl = data.rfind(b"\n")
        if last_nl >= 0:
            data = data[: last_nl + 1]
            offset = since + last_nl + 1
    return data, offset


def _level_of(line):
    """从 logging 默认格式（如 ``INFO:app:...``）里取级别；无法识别时返回 None。"""
    head = line.split(":", 1)[0].strip().upper()
    if head in LEVELS:
        return head
    for lv in LEVELS:
        if lv in line[:64]:
            return lv
    return None


def available_sources():
    """列出可查看的日志源及其大小/修改时间（不存在时 size=0）。"""
    out = []
    for key, meta in LOG_SOURCES.items():
        path = meta["path"]
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        out.append({
            "key": key,
            "label": meta["label"],
            "desc": meta["desc"],
            "size": size,
            "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else "",
            "exists": bool(size or mtime),
        })
    return out


def tail_lines(source=DEFAULT_SOURCE, tail=DEFAULT_TAIL, since=0,
               query="", level=""):
    """读取日志尾部（或增量）内容。

    参数:
        source: 白名单 key；
        tail:   最多返回多少行（上限 MAX_TAIL）；since>0 时该值用于限制增量行数；
        since:  >0 时只返回该字节偏移之后的新内容，并返回新的 offset；
        query:  关键字过滤（大小写不敏感）；
        level:  ERROR / WARNING / INFO / DEBUG 之一，仅保留该级别。

    返回的 dict 里 ``lines`` 已是纯文本列表（已按实际编码解码）。
    """
    key, meta = _resolve(source)
    if not meta:
        return {"success": False, "error": f"未知日志源：{source}",
                "available": [k for k in LOG_SOURCES]}
    path = meta["path"]

    tail = max(1, min(int(tail or DEFAULT_TAIL), MAX_TAIL))
    since = int(since or 0)
    if not os.path.isfile(path):
        return {"success": False, "error": f"日志文件不存在：{path}",
                "source": key, "path": path}

    size = os.path.getsize(path)
    offset = size
    try:
        if since > 0:
            if size <= since:
                # 没有新内容（含日志被清空/rotate 导致偏移失效的情形）
                return {"success": True, "source": source, "path": path, "size": size,
                        "modified_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                                     time.localtime(os.path.getmtime(path))),
                        "offset": size, "encoding": "", "truncated": False,
                        "lines": [], "counts": {}}
            data, offset = _read_since_bytes(path, since)
            truncated = False
        else:
            data, truncated = _read_tail_bytes(path, tail)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取日志失败 source={source}: {e}")
        return {"success": False, "error": f"读取日志失败：{e}", "source": source}

    text, encoding = _decode(data)
    lines = text.splitlines()
    if lines and since <= 0:
        # 倒读得到的第一行多半是被切断的半行，丢掉它才是干净的尾部
        lines = lines[1:]

    # 级别与关键字过滤
    counts = {}
    wanted = level.strip().upper() or ""
    kw = (query or "").strip().lower()
    filtered = []
    for ln in lines:
        lv = _level_of(ln)
        if lv:
            counts[lv] = counts.get(lv, 0) + 1
        if wanted:
            # 指定了级别：无级别行（如 python -u 的裸 print）一律排除，
            # 否则「只看 ERROR」里会混进一堆无法归类的裸输出。
            if lv != wanted:
                continue
        if kw and kw not in ln.lower():
            continue
        filtered.append(ln)

    if len(filtered) > tail:
        filtered = filtered[-tail:]

    return {
        "success": True,
        "source": key,
        "path": path,
        "size": size,
        "modified_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(os.path.getmtime(path))),
        "offset": offset,
        "encoding": encoding,
        "truncated": bool(truncated),
        "lines": filtered,
        "counts": counts,
    }

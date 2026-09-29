# -*- coding: utf-8 -*-
"""漫剧生成系统 - 桌面应用入口

支持两种运行模式:
1. Web模式: 正常Flask应用，浏览器访问
2. 桌面模式: 通过PyInstaller打包为EXE，内置浏览器
"""
import sys
import os
import argparse
import threading
import webbrowser
from pathlib import Path

# 确保app目录在路径中
sys.path.insert(0, str(Path(__file__).parent / 'app'))

def parse_args():
    parser = argparse.ArgumentParser(description='漫剧工坊 - 全自动AI漫剧生产平台')
    parser.add_argument('--desktop', action='store_true', help='启动桌面模式（内置浏览器）')
    parser.add_argument('--port', type=int, default=5000, help='Web服务器端口')
    parser.add_argument('--host', default='127.0.0.1', help='Web服务器地址')
    return parser.parse_args()

def open_in_browser(url):
    """在默认浏览器打开URL"""
    threading.Timer(1, lambda: webbrowser.open(url)).start()


_FROZEN_LOG_FH = None  # 模块级持有，防止被 GC 连带 close() 掉 dup2 共享的 fd


def _redirect_frozen_logs():
    """打包版（frozen）把本进程的 stdout/stderr 追加写到 exe 同级 logs/serve_stdout.log。

    背景：源码/计划任务模式由 run_serve.bat 把 stdout 重定向到
    .workbuddy/test/_out/serve_stdout.log；但双击 EXE 没有那个重定向，
    Web「日志」页读不到本进程输出（表现为「启动后看不到日志」）。这里在
    进程内直接把 stdout/stderr 镜像进 exe 同级 logs/ 目录，与
    log_viewer._resolve_log_dir（frozen 时定位 exe 同级 logs/）对齐。

    两个坑（都踩过）：
      1. 桌面控制台默认 **GBK** 编码，print 里的 emoji（📝/⚠️）直接
         UnicodeEncodeError 崩掉整个启动 —— 本函数所有日志**不带 emoji**。
      2. dup2 后 fd 与 log 文件共享，若把 fh 交给 `with` 块，块结束 close()
         会连带关掉日志 fd —— 故 fh 存到模块级全局，进程存活期内不回收。

    非 frozen（直接 python main.py / 计划任务）保持原行为，不落这份日志。
    """
    import sys
    global _FROZEN_LOG_FH
    if not getattr(sys, "frozen", False):
        return
    log_dir = Path(os.path.dirname(os.path.abspath(sys.executable))) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "serve_stdout.log"
    try:
        fh = open(log_path, "a", encoding="utf-8")
        os.dup2(fh.fileno(), sys.stdout.fileno())
        os.dup2(fh.fileno(), sys.stderr.fileno())
        _FROZEN_LOG_FH = fh  # 持有引用，阻止 GC 关闭共享 fd
        # Python 层 sys.stdout/stderr 仍按原 console 编码（GBK），改成 UTF-8 容错，
        # 后续 print 中文/emoji 不再崩；line_buffered 保证日志即时落盘。
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:  # noqa: BLE001  某些流不支持 reconfigure，忽略
                pass
        print(f"[desktop] log redirected to: {log_path}")
    except Exception as e:  # noqa: BLE001  重定向失败绝不阻塞启动
        print(f"[desktop] log redirect failed (ignored): {e}")

def run_web_mode(port, host):
    """运行Web模式，返回进程退出码

    ⚠️ 审计 G20：`serve.main()` 是**无参**函数 —— host/port 由环境变量 `APP_HOST` /
    `APP_PORT` 读取（见 `app/serve.py` 的 `_safe_run`）。旧代码写 `main(port, host)`，
    传给一个不收参数的函数两个位置参数 → 打包成 EXE / 桌面入口启动即
    `TypeError: main() takes 0 positional arguments but 2 were given`。
    日常都走 `run_app.bat`（`python app/serve.py`），这条入口从未被真正跑过，所以一直没暴露。
    现在改为先把参数写进环境变量，再调用无参 `main()`。
    """
    import serve
    host = str(host or "127.0.0.1")
    os.environ["APP_HOST"] = host
    os.environ["APP_PORT"] = str(int(port or 5000))
    print(f"🚀 漫剧工坊启动: http://{host}:{os.environ['APP_PORT']}")
    print("按 Ctrl+C 停止服务")
    return serve.main()

def run_desktop_mode(port):
    """运行桌面模式 - 使用内置浏览器或Electron包装"""
    url = f"http://{port}"
    print(f"🖥️  桌面模式启动: {url}")
    open_in_browser(url)
    return run_web_mode(port, '127.0.0.1')

def _setup_frozen_datadir():
    """打包版把可写数据根指到 exe 同级 data/（避免 _MEIPASS 只读崩溃）。

    frozen 单文件 exe 解压到临时 _MEIPASS（只读），直接在那 mkdir output 会崩。
    这里把 MJSCXT_DATA_DIR 指到 <exe目录>/data（可写），config.PROJECT_DATA_DIR
    随之后所有可写数据（output/novels/tasks.db/.secret_key...）都落到这。
    非 frozen（python main.py / 计划任务）不动，就地读写源码树，行为不变。
    """
    if not getattr(sys, "frozen", False):
        return
    exe_dir = Path(os.path.dirname(os.path.abspath(sys.executable)))
    data_dir = exe_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MJSCXT_DATA_DIR"] = str(data_dir)
    (data_dir / "output").mkdir(parents=True, exist_ok=True)
    print(f"[desktop] data dir: {data_dir}")


def main():
    args = parse_args()

    # 打包版：① 可写数据根指到 exe 同级 data/（勿在只读 _MEIPASS 上 mkdir）
    _setup_frozen_datadir()
    #        ② 本进程 stdout/stderr 镜像到 exe 同级 logs/（Web 日志页可读到）
    _redirect_frozen_logs()

    # 创建输出目录（frozen 时 MJSCXT_DATA_DIR 已指向可写 data/，源码树时指向 __file__ 上级）
    data_root = os.environ.get("MJSCXT_DATA_DIR") or str(Path(__file__).parent)
    output_dir = Path(data_root) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.desktop:
        return run_desktop_mode(args.port)
    return run_web_mode(args.port, args.host)

if __name__ == '__main__':
    sys.exit(main())

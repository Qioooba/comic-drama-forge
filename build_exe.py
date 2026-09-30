#!/usr/bin/env python3
"""
漫剧工坊 - PyInstaller 单文件打包脚本

用法：
    python build_exe.py          # 构建 + 自动部署到 ASCII 目录
    python build_exe.py clean    # 清理构建缓存

⚠️ 2026-09-30 关键背景：PyInstaller 6.x 的单文件 bootloader **无法在本项目根目录
（含中文「漫剧生成系统」）下创建临时解压目录**，启动即报
"[PYI-xxxx:ERROR] Could not create temporary directory!" 并立刻退出。
所以构建产物必须部署到一个**纯 ASCII 路径**才能双击运行。本脚本在构建成功后
自动复制到 <用户主目录>/mjscxt_desktop/msjcxt.exe（可用 MJSCXT_DEPLOY_DIR 覆盖），
并写出 datadir.txt 指向源码树数据根，使桌面版与 Web 版共用同一批小说/项目。
"""
import os
import sys
import shutil
import subprocess

# 控制台默认 GBK，打印 ✓/✗ 会 UnicodeEncodeError（构建成功后被这行崩掉，误导成构建失败）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(HERE, "dist")
BUILD_DIR = os.path.join(HERE, "build")
SPEC_FILE = os.path.join(HERE, "漫剧工坊.spec")

EXE_NAME = "漫剧工坊.exe"
DEPLOY_NAME = "msjcxt.exe"
DEPLOY_DIR = os.environ.get("MJSCXT_DEPLOY_DIR") or os.path.join(
    os.path.expanduser("~"), "mjscxt_desktop"
)


def clean():
    for d in [BUILD_DIR, DIST_DIR]:
        if os.path.isdir(d):
            shutil.rmtree(d)
            print(f"已删除: {d}")
    print("清理完成")


def deploy(src_exe):
    """把产物复制到纯 ASCII 目录（含 datadir.txt），返回部署后的路径。"""
    try:
        os.makedirs(DEPLOY_DIR, exist_ok=True)
        dst = os.path.join(DEPLOY_DIR, DEPLOY_NAME)
        # 旧版本可能正在运行 → 文件被占用（WinError 32），先结束它
        if os.name == "nt":
            subprocess.run(["taskkill", "/f", "/im", DEPLOY_NAME],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import time as _t
            _t.sleep(2)
        shutil.copy2(src_exe, dst)
        # 数据根：源码树根（桌面版与 Web 版共用 novels/ 与 output/）
        with open(os.path.join(DEPLOY_DIR, "datadir.txt"), "w", encoding="utf-8") as f:
            f.write(HERE)
        size_mb = os.path.getsize(dst) / 1024 / 1024
        print(f"[deploy] {dst} ({size_mb:.1f} MB)")
        print(f"[deploy] datadir.txt -> {HERE}")
        return dst
    except Exception as e:  # noqa: BLE001
        print(f"[deploy] 部署失败（产物仍在 dist/）: {e}")
        return None


def build():
    os.makedirs(DIST_DIR, exist_ok=True)
    cmd = [sys.executable, "-m", "PyInstaller", SPEC_FILE, "--clean"]
    print(f"构建命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        print("构建失败，请检查上方错误信息")
        return result.returncode

    exe = os.path.join(DIST_DIR, EXE_NAME)
    size_mb = os.path.getsize(exe) / 1024 / 1024 if os.path.exists(exe) else 0
    print(f"构建成功: {exe} ({size_mb:.1f} MB)")
    dst = deploy(exe)
    if dst:
        print("")
        print("注意：dist\ 下的 exe 位于中文目录，双击会崩（PyInstaller bootloader 限制）。")
        print(f"请双击这个：{dst}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        clean()
    else:
        sys.exit(build())

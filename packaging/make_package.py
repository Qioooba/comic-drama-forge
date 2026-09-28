# -*- coding: utf-8 -*-
"""漫剧工坊 · 应用层绿色便携打包器
=================================================
只做「白名单复制 + 清理 + 打 zip」。不触碰 app/*.py，不重启 Flask，
不影响正在跑的 ep1 生成任务。

产物：
  packaging/_dist/漫剧工坊_便携版/     解包即用目录（含 环境\\venv 占位 + 3 个 .bat/清单）
  packaging/漫剧工坊_便携版.zip         单文件分发件（首次运行需跑 安装依赖.bat 建 venv）

白名单见下方常量；密钥/运行产物/模型权重/版权文本一律排除。
幂等：每次运行先清空 staging 再重建。

运行（离线解释器即可）：
    C:\\Python314\\python.exe packaging\\make_package.py
"""
import os
import sys
import shutil
import zipfile
import io
from pathlib import Path

HERE = Path(__file__).resolve().parent          # packaging/
ROOT = HERE.parent                               # 项目根
STAGING = HERE / "_dist" / "漫剧工坊_便携版"
ZIP_OUT = HERE / "漫剧工坊_便携版.zip"

# 需要原样整拷的顶层目录（相对 ROOT）
COPY_DIRS = [
    "app",          # Flask 后端 + static 前端产物 + providers/plugins/templates
    "locales",      # i18n
    "workflows",    # ComfyUI 工作流模板（开箱可用）
    "electron-app", # 桌面版脚手架（用户可选 npm 安装）
]

# 需要原样拷的根目录文件
COPY_FILES = [
    "main.py",
    "requirements.txt",       # 根版（模型/旧脚本用）
    ".env.example",
]

# 需要补进的运行期空目录
EMPTY_DIRS = [
    "output",
    "novels",
    "bin/ffmpeg",            # 放 ffmpeg.exe/ffprobe.exe 的位置（可留空）
]

# 打包后必须移除的目录/文件（相对 staging）
STRIP_DIRS = []

def is_secret_file(name):
    low = name.lower()
    # 明确排除：含密钥的活凭据 + 运行产物
    secret = {
        ".env", ".secret_key", "secrets.enc",
        "ai_config.json", "llm_config.json", "qc_config.json",
        "watermark_config.json", "ref_img_path.txt",
        "qc_config.json.bak", "tasks.db",
    }
    return low in secret or low.endswith(".safetensors") or low.endswith(".ckpt")

def iter_copyable(rel, dest):
    """yield (src, dst) 对所有需要拷的文件。"""
    src = ROOT / rel
    if src.is_file():
        dest_file = dest / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        yield src, dest_file
    else:
        for p in src.rglob("*"):
            if p.is_dir():
                continue
            # 跳 __pycache__ 与 pyc
            if "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            # 跳密钥/大权重（目录里若有）
            if is_secret_file(p.name):
                continue
            rel_to_root = p.relative_to(ROOT)
            dest_file = dest / rel_to_root
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            yield p, dest_file

def main():
    # 1. 清理 staging
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True, exist_ok=True)
    if ZIP_OUT.exists():
        ZIP_OUT.unlink()

    total = 0
    # 2a. 整目录（自动排除 __pycache__/pyc/密钥/权重）
    for d in COPY_DIRS:
        for src, dst in iter_copyable(d, STAGING):
            shutil.copy2(src, dst)
            total += 1

    # 2b. 根目录文件
    for f in COPY_FILES:
        src = ROOT / f
        if src.is_file():
            shutil.copy2(src, STAGING / f)
            total += 1
        else:
            print(f"[warn] 根文件缺失，跳过: {f}")

    # 2c. 补进便携专用文件（启动/安装/清单）
    for name in ["启动.bat", "安装依赖.bat", "环境准备清单.txt"]:
        p = HERE / name
        if p.is_file():
            shutil.copy2(p, STAGING / name)
            total += 1
        else:
            print(f"[warn] 缺少便携文件: {name}")

    # 2d. 运行期空目录
    for d in EMPTY_DIRS:
        (STAGING / d).mkdir(parents=True, exist_ok=True)

    # 2e. app/requirements.txt 已在 app/ 目录里整拷，确认存在
    if not (STAGING / "app" / "requirements.txt").exists():
        src = ROOT / "app" / "requirements.txt"
        if src.is_file():
            shutil.copy2(src, STAGING / "app" / "requirements.txt")
            total += 1

    # 3. 体积统计
    def dir_size(p):
        s = 0
        for f in p.rglob("*"):
            if f.is_file():
                s += f.stat().st_size
        return s
    size_mb = dir_size(STAGING) / 1024 / 1024

    # 4. 打 zip（用相对路径）
    with zipfile.ZipFile(ZIP_OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in STAGING.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(STAGING))
    zip_mb = ZIP_OUT.stat().st_size / 1024 / 1024

    print("=" * 50)
    print(f"文件数: {total}")
    print(f"staging: {STAGING}  ({size_mb:.1f} MB)")
    print(f"zip:     {ZIP_OUT}  ({zip_mb:.1f} MB)")
    print("=" * 50)
    return 0

if __name__ == "__main__":
    sys.exit(main())

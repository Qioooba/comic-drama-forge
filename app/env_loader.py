# -*- coding: utf-8 -*-
"""环境配置加载（单一入口）

为什么单独抽一个模块
--------------------
原先 `.env` 的加载写在 config.py 里，导致**只有 import config 的模块才能读到 .env**。
而 secret_store / qc_client 等模块可能在 config 之前被导入（例如只跑一个质检脚本），
此时 `MJSCXT_SECRET_KEY` 等环境变量尚未注入 → 主密钥取不到 → 已加密的密钥全部解密失败。
（这是实际踩到的坑：qc_config 的密钥迁移成功，但独立调用时解密失败。）

因此把「项目根目录 + .env 加载」下沉到本模块，config 与 secret_store 都依赖它，
保证**任何入口导入任一模块时 .env 都已生效**。

加载策略：不覆盖已存在的环境变量（系统环境变量优先级高于 .env）。
未安装 python-dotenv 时使用内置最简解析器，保证零依赖也能启动。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

def _resolve_project_root() -> str:
    """项目根目录（app/ 的上一级）—— 只读资源（locales/、workflows/）的基址。

    ⚠️ 2026-09-30 修复（frozen）：旧实现无条件算 `dirname(__file__)/..`。
    PyInstaller 单文件包把模块收进 PYZ，`__file__` = `<_MEIPASS>/env_loader.pyc`，
    于是 `..` 拿到的是 **_MEIPASS 的父目录**（如 C:\\Users\\...\\Temp），
    而只读资源实际被 spec 的 datas 打在 `<_MEIPASS>/locales`、`<_MEIPASS>/workflows`。
    实测后果：exe 里 `GET /api/i18n/zh-CN` 恒 404 → 前端 loadLocale 失败 →
    `t()` 全部回落成 key 原文（界面满是 `vault.fallbackTitle` 这种字样，
    看起来像「新功能没上」）；工作流模板同样找不到。
    现在 frozen 时按 `_MEIPASS` 解析（与 spec 布局一致）；非 frozen 行为一字不变。
    """
    import sys as _sys
    if getattr(_sys, "frozen", False):
        _mp = getattr(_sys, "_MEIPASS", "") or ""
        if _mp and os.path.isdir(_mp):
            return os.path.abspath(_mp)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# 项目根目录（app/ 的上一级）
PROJECT_ROOT_DIR = _resolve_project_root()

# 数据根目录（可写）。
#   默认 = 项目根（源码树里就地读写，开发/浏览器版行为完全不变）。
#   设置 MJSCXT_DATA_DIR 后可把全部「可写数据」重定向到独立目录：
#   output/、novels/、各 *_config.json、.secret_key、secrets.enc、tasks.db、.env。
#   只读资源（workflows/、locales/、app/static）仍留在 PROJECT_ROOT_DIR。
#   桌面独立 exe 用它把数据写到用户可写目录（避免 Program Files 只读）。
PROJECT_DATA_DIR = os.path.abspath(
    os.environ.get("MJSCXT_DATA_DIR") or PROJECT_ROOT_DIR
)
# 数据根一旦确定即固定（同 PROJECT_ROOT_DIR 语义），供 .env 加载使用
_DATA_ROOT_FIXED = PROJECT_DATA_DIR

_LOADED = False


def _parse_env_file(path: str) -> None:
    """内置最简 .env 解析器（python-dotenv 缺失时的兜底）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k.startswith("export "):
                    k = k[7:].strip()
                if k and k not in os.environ:
                    os.environ[k] = v
        logger.debug(f"已加载环境配置（内置解析器）：{path}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f".env 解析失败（忽略，按默认值启动）：{e}")


def load_project_env(force: bool = False) -> str:
    """加载 .env（幂等）。优先数据根（MJSCXT_DATA_DIR），缺失则回退源根。

    返回实际加载的 .env 路径（都不存在则返回空串）。
    """
    global _LOADED
    if _LOADED and not force:
        return _resolve_env_path()
    _LOADED = True
    env_path = _resolve_env_path()
    if not os.path.isfile(env_path):
        return ""
    # 优先用 python-dotenv（正确处理引号、转义、多行）
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        logger.debug(f"已加载环境配置：{env_path}")
        return env_path
    except ImportError:
        _parse_env_file(env_path)
        return env_path
    except Exception as e:  # noqa: BLE001
        logger.warning(f"python-dotenv 加载失败，改用内置解析器：{e}")
        _parse_env_file(env_path)
        return env_path


def _resolve_env_path() -> str:
    """.env 定位：数据根优先，源根兜底（覆盖便携/安装两种布局）。"""
    if PROJECT_DATA_DIR != PROJECT_ROOT_DIR:
        cand = os.path.join(PROJECT_DATA_DIR, ".env")
        if os.path.isfile(cand):
            return cand
    return os.path.join(PROJECT_ROOT_DIR, ".env")


def env(key: str, default: str = "") -> str:
    """取环境变量（去空白）；空串视为未设置，回退默认值"""
    val = (os.getenv(key) or "").strip()
    return val if val else default


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except (TypeError, ValueError):
        return default


# 导入即加载，保证任何模块引用本模块时环境已就绪
load_project_env()

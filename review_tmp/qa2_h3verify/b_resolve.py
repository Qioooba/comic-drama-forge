# -*- coding: utf-8 -*-
"""B：二采模板两份副本分歧 + 运行期落点。"""
import hashlib
import json
import os
import sys

ROOT = r"C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统"
sys.path.insert(0, os.path.join(ROOT, "app"))

import config as CFG  # noqa: E402

NAME = "minimax_h3_director_二采_加速.json"
P_PROJ = os.path.join(ROOT, "workflows", NAME)
P_COM = r"D:/ComfyUI_portable_TE_v260619/ComfyUI/ComfyUI/user/default/workflows/" + NAME


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def walk_vae(p):
    with open(p, "r", encoding="utf-8-sig") as f:
        wf = json.load(f)
    vae = [n for n in wf["nodes"] if n.get("type") == "VAELoader"]
    out = []
    for n in vae:
        out.append({"id": n.get("id"), "widgets_values": n.get("widgets_values")})
    return len(wf["nodes"]), out


print("=" * 70)
print("B1. 两份副本")
for tag, p in (("项目内 workflows/", P_PROJ), ("ComfyUI 目录", P_COM)):
    n, vae = walk_vae(p)
    print(f"  {tag}: exists={os.path.isfile(p)} sha256={sha(p)} size={os.path.getsize(p)}")
    print(f"    节点数={n}  VAELoader={vae}")
print(f"  两副本逐字节相同: {open(P_PROJ,'rb').read()==open(P_COM,'rb').read()}")

print("=" * 70)
print("B2. config 路径优先级（实测）")
print(f"  PROJECT_WORKFLOWS_DIR = {CFG.PROJECT_WORKFLOWS_DIR}")
print(f"  COMFYUI_WORKFLOWS_DIR = {CFG.COMFYUI_WORKFLOWS_DIR}")
print(f"  workflow_search_dirs() = {CFG.workflow_search_dirs()}")
print(f"  _WORKFLOWS_DIR_EXPLICIT = {CFG._WORKFLOWS_DIR_EXPLICIT!r}")
print(f"  _WORKFLOWS_PREFER_COMFYUI = {CFG._WORKFLOWS_PREFER_COMFYUI!r}")

print("=" * 70)
print("B3. resolve_workflow_path('minimax_h3_director_二采_加速.json') 实跑落点")
resolved = CFG.resolve_workflow_path(NAME)
print(f"  resolved = {resolved}")
print(f"  落在项目目录内: {os.path.normcase(resolved)==os.path.normcase(P_PROJ)}")
print(f"  落在 ComfyUI 目录内: {os.path.normcase(resolved)==os.path.normcase(P_COM)}")
print(f"  文件存在: {os.path.isfile(resolved)}  sha256={sha(resolved)}")

# 顺带确认新模板与其它模板落点
for extra in ("h3_director_r2v_单采.json", "H3信号10段测试001.json",
              "角色生成_Qwen21.json"):
    r = CFG.resolve_workflow_path(extra)
    print(f"  resolve({extra!r}) = {r}")

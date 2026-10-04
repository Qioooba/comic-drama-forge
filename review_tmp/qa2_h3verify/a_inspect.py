# -*- coding: utf-8 -*-
"""A 段：模板产物独立检查（只读）。"""
import hashlib
import json
import os
import sys
from collections import Counter

ROOT = r"C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统"
P_NEW = os.path.join(ROOT, "workflows", "h3_director_r2v_单采.json")
D_NEW = r"D:/ComfyUI_portable_TE_v260619/ComfyUI/ComfyUI/user/default/workflows/h3_director_r2v_单采.json"
SRC = r"D:/ComfyUI_portable_TE_v260619/ComfyUI/ComfyUI/user/default/workflows/蛊真人_第一集_H3视频工作流.json"

TARGET_SOLATTN = [1.3, 0.2, 0.9, 4096, True, "exact_kv_and_rows", False,
                  "3d", True, False, False, "0-2,-1"]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(p):
    with open(p, "r", encoding="utf-8-sig") as f:
        return json.load(f)


print("=" * 70)
print("A1. 两副本 sha256 / 字节 / 节点数")
for tag, p in (("项目内", P_NEW), ("ComfyUI", D_NEW)):
    print(f"  {tag}: sha256={sha(p)} size={os.path.getsize(p)} exists={os.path.isfile(p)}")

wn = load(P_NEW)
wd = load(D_NEW)
print(f"  项目内 nodes={len(wn.get('nodes', []))} ComfyUI nodes={len(wd.get('nodes', []))}")
print(f"  两副本逐字节相同: {open(P_NEW,'rb').read() == open(D_NEW,'rb').read()}")

print("=" * 70)
print("A1b. 源工作流 sha256")
print(f"  SRC sha256={sha(SRC)} size={os.path.getsize(SRC)}")

print("=" * 70)
print("A1c. 节点类型清单 + id")
tcons = Counter()
for n in wn["nodes"]:
    tcons[n.get("type")] += 1
for t, c in sorted(tcons.items()):
    print(f"  {t}: {c}")

print("=" * 70)
print("A2. MiniMaxH3Director widgets_values 长度 + SolAttnPatch 对照")
director = next(n for n in wn["nodes"] if n.get("type") == "MiniMaxH3Director")
print(f"  director id={director['id']} type={director['type']}")
print(f"  director widgets_values len={len(director.get('widgets_values') or [])}")
print(f"  director widgets_values_named keys={list((director.get('widgets_values_named') or {}).keys())}")

sol = [n for n in wn["nodes"] if n.get("type") == "SolAttnPatch"]
print(f"  SolAttnPatch count={len(sol)}")
if sol:
    s = sol[0]
    wv = s.get("widgets_values") or []
    named = s.get("widgets_values_named") or {}
    print(f"  SolAttnPatch id={s['id']}")
    print(f"  widgets_values   = {wv}")
    print(f"  TARGET           = {TARGET_SOLATTN}")
    print(f"  wv == target: {wv == TARGET_SOLATTN}")
    print(f"  named            = {named}")
    print(f"  named.values()==wv: {list(named.values()) == wv}")
    print(f"  named.keys()     = {list(named.keys())}")
    for k in ("tau", "int8_qk", "sink_conditioning", "verbose", "dense_blocks"):
        print(f"    {k}: named={named.get(k)!r}")

print("=" * 70)
print("A5. 禁止/应存在节点")
for bad in ("MiniMaxH3DirectorRefine", "BasicScheduler",
            "NvidiaDLSSFrameInterpolation", "DLSSNR_Video"):
    n = [x for x in wn["nodes"] if x.get("type") == bad]
    print(f"  {bad}: count={len(n)} (期望 0)")
lora = [x for x in wn["nodes"] if x.get("type") == "LoraLoaderModelOnly"]
print(f"  LoraLoaderModelOnly count={len(lora)} (期望 1)")
for x in lora:
    print(f"    id={x['id']} widgets_values={x.get('widgets_values')}")
print(f"  SolAttnPatch count={len(sol)} (期望 1)")

print("=" * 70)
print("A3. 清洗检查")
tv = director.get("widgets_values") or []
print(f"  widgets_values[1] (global_prompt)={tv[1]!r} is_empty_str={tv[1]==''}")
td_raw = tv[11] if len(tv) > 11 else None
td = json.loads(td_raw) if isinstance(td_raw, str) else td_raw
print(f"  timeline_data type={type(td).__name__}")
segs = td.get("segments")
print(f"  segments={segs}")
print(f"  shots (top)={td.get('shots')!r}")
print(f"  keyframes (top)={td.get('keyframes')!r}")
print(f"  videoClips (top)={td.get('videoClips')!r}")
print(f"  batchWorkspaces={td.get('batchWorkspaces')!r}")
g = td.get("global") or {}
print(f"  global.refs={g.get('refs')!r} global.prompt={g.get('prompt')!r}")
v = td.get("video") or {}
print(f"  video.sourceFrameCount={v.get('sourceFrameCount')!r}")
named = director.get("widgets_values_named") or {}
print(f"  wv[10]={tv[10]!r} named[total_frames]={named.get('total_frames')!r} "
      f"timeline.totalFrames={td.get('totalFrames')!r}")
print(f"  43 三处一致: {tv[10] == named.get('total_frames') == td.get('totalFrames') == 43}")

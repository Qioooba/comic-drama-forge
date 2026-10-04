# -*- coding: utf-8 -*-
"""A4：新模板 vs 源工作流逐节点 diff（Director 除外应完全相等）。"""
import hashlib
import json
import os

ROOT = r"C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统"
P_NEW = os.path.join(ROOT, "workflows", "h3_director_r2v_单采.json")
SRC = r"D:/ComfyUI_portable_TE_v260619/ComfyUI/ComfyUI/user/default/workflows/蛊真人_第一集_H3视频工作流.json"


def load(p):
    with open(p, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


new = load(P_NEW)
src = load(SRC)
print(f"源工作流 sha256 = {sha(SRC)}")
print(f"新模板  sha256 = {sha(P_NEW)}")
print(f"源 nodes={len(src['nodes'])}  新 nodes={len(new['nodes'])}")
print(f"源 links={len(src.get('links', []))}  新 links={len(new.get('links', []))}")
print(f"顶层键 源={sorted(src.keys())}")
print(f"顶层键 新={sorted(new.keys())}")

by_id_src = {n.get("id"): n for n in src["nodes"]}
by_id_new = {n.get("id"): n for n in new["nodes"]}
print(f"源 id 集合   = {sorted(map(str, by_id_src.keys()))}")
print(f"新 id 集合   = {sorted(map(str, by_id_new.keys()))}")

print("=" * 70)
print("逐节点对比（只比较 id 相同的节点）:")
changed = []
for nid in sorted(by_id_new.keys(), key=lambda x: (str(type(x)), str(x))):
    a = by_id_src.get(nid)
    b = by_id_new[nid]
    if a is None:
        print(f"  id={nid}: 仅新模板有 (type={b.get('type')})")
        changed.append(nid)
        continue
    if a == b:
        print(f"  id={nid} ({b.get('type')}): 相等")
    else:
        keys = set(a.keys()) | set(b.keys())
        diffk = [k for k in keys if a.get(k) != b.get(k)]
        print(f"  id={nid} ({b.get('type')}): 不同 -> {diffk}")
        changed.append(nid)
print(f"  有差异的节点 id: {changed}")

# 顶层非 nodes/links 字段对比
print("=" * 70)
print("顶层其它键对比:")
for k in sorted(set(src.keys()) | set(new.keys())):
    if k in ("nodes", "links"):
        continue
    same = src.get(k) == new.get(k)
    print(f"  {k}: same={same} src={src.get(k)!r} new={new.get(k)!r}"[:200])

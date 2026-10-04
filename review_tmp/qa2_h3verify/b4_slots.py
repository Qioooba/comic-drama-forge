# -*- coding: utf-8 -*-
"""B4/C 前置：核对 SLOTS 的 targets 节点 id 在项目内模板里真实存在且类型匹配。"""
import json
import os
import sys

ROOT = r"C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统"
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)  # for fs_atomic? comfyui_models imports fs_atomic (in app/)
sys.path.insert(0, os.path.join(ROOT, "app"))

import comfyui_models as CM  # noqa: E402

TEMPLATES = {
    "h3_director_r2v_单采.json": os.path.join(ROOT, "workflows", "h3_director_r2v_单采.json"),
    "minimax_h3_director_二采_加速.json": os.path.join(ROOT, "workflows", "minimax_h3_director_二采_加速.json"),
}
loaded = {}
for name, p in TEMPLATES.items():
    with open(p, "r", encoding="utf-8-sig") as f:
        loaded[name] = json.load(f)

print("=" * 78)
for key in CM.SLOT_ORDER:
    meta = CM.SLOTS[key]
    print(f"[{key}] node_type={meta['node_type']} field={meta['field']} "
          f"filter={meta.get('values_filter')}")
    for tname, ids in (meta.get("targets") or {}).items():
        wf = loaded.get(tname)
        if wf is None:
            print(f"    {tname}: (无此模板)")
            continue
        by_id = {n.get("id"): n for n in wf["nodes"]}
        for nid in ids:
            n = by_id.get(nid)
            if n is None:
                print(f"    {tname} id={nid}: !! 不存在")
            else:
                ok = n.get("type") == meta["node_type"]
                print(f"    {tname} id={nid}: type={n.get('type')} "
                      f"({'OK' if ok else '!!类型不符'}) widgets={n.get('widgets_values')}")

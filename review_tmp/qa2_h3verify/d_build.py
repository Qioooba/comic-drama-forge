# -*- coding: utf-8 -*-
"""D：端到端离线构建（不连 ComfyUI、不碰 GPU）。"""
import json
import os
import sys

ROOT = r"C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统"
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import config as CFG  # noqa: E402
import h3_director_builder as HB  # noqa: E402
import h3_segment_loras as SL  # noqa: E402

NEW_TPL = os.path.join(ROOT, "workflows", "h3_director_r2v_单采.json")
OLD_TPL = os.path.join(ROOT, "workflows", "minimax_h3_director_二采_加速.json")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if not cond else ""))


# ---- 构造 3 段 segments：第 2 段带「战斗/挥刀」触发段级 LoRA ----
# 忠实复刻真实调用链（app.py:7924）：LoRA 由 **shot dict** 的 description/action
# 等字段算出（_SHOT_TEXT_FIELDS 不含 "prompt"），再挂到段上。
shots = [
    {"name": "seg01", "description": "清晨，少年独立于山巅，俯瞰云海。", "duration": 2.0},
    {"name": "seg02", "description": "他猛然拔刀，与黑衣人激战，刀光交错。",
     "action": "挥刀斩出", "duration": 1.5},
    {"name": "seg03", "description": "雨落，他缓缓收刀入鞘。", "duration": 1.0},
]
segs = []
for sh in shots:
    rows = SL.select_loras_for_shot_smart(sh, use_llm=False)   # 纯规则表，确定性
    seg = {"name": sh["name"], "prompt": sh["description"] + " " + sh.get("action", ""),
           "duration": sh["duration"]}
    if rows:
        seg["loras"] = rows
    segs.append(seg)
print("传入的段级 LoRA:")
for s in segs:
    print(f"  {s['name']}: loras={s.get('loras', [])}")

print("=" * 78)
print("D1. 单采模板构建")
b_new = HB.H3DirectorBuilder(NEW_TPL)
wf, layout = b_new.build(segs, filename_prefix="video/test_qa2",
                         width=544, height=960, frame_rate=24)

check("D1.1 layout['template'] 指向新模板",
      os.path.normcase(layout["template"]) == os.path.normcase(NEW_TPL), str(layout["template"]))
check("D1.2 layout['director_node'] == 12", layout["director_node"] == 12, str(layout["director_node"]))
check("D1.3 layout['refine_node'] is None", layout["refine_node"] is None, str(layout["refine_node"]))

nodes = wf["nodes"]
sols = [n for n in nodes if n.get("type") == "SolAttnPatch"]
check("D1.4 SolAttnPatch 恰好 1 个", len(sols) == 1, str(len(sols)))
if sols:
    check("D1.5 SolAttnPatch.widgets_values == _SOLATTN_ALIGNED_WV（12 项逐项）",
          sols[0].get("widgets_values") == HB._SOLATTN_ALIGNED_WV,
          json.dumps(sols[0].get("widgets_values")))
    check("D1.5b == 期望字面 [1.3,0.2,0.9,4096,True,'exact_kv_and_rows',False,'3d',True,False,False,'0-2,-1']",
          sols[0].get("widgets_values") == [1.3, 0.2, 0.9, 4096, True, "exact_kv_and_rows",
                                            False, "3d", True, False, False, "0-2,-1"])
loras = [n for n in nodes if n.get("type") == "LoraLoaderModelOnly"]
check("D1.6 LoraLoaderModelOnly 恰好 1 个", len(loras) == 1, str(len(loras)))
if loras:
    lv = loras[0].get("widgets_values") or []
    check("D1.7 其值为 ref2v_turbo",
          lv and lv[0] == "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
          json.dumps(lv))
for bad in ("MiniMaxH3DirectorRefine", "BasicScheduler", "NvidiaDLSSFrameInterpolation"):
    check(f"D1.8 无 {bad}", not [n for n in nodes if n.get("type") == bad])

sv = [n for n in nodes if n.get("type") == "SaveVideo"]
check("D1.9 SaveVideo 恰好 1 个", len(sv) == 1, str(len(sv)))
if sv:
    pref = (sv[0].get("widgets_values") or [None])[0]
    check("D1.10 SaveVideo.filename_prefix == 传入值", pref == "video/test_qa2", repr(pref))

# 段级 LoRA
dirnode = next(n for n in nodes if n.get("type") == "MiniMaxH3Director")
tl = json.loads((dirnode.get("widgets_values") or [])[11])
seg_tl = tl["segments"]
check("D1.11 timeline 段数 == 3", len(seg_tl) == 3, str(len(seg_tl)))
for i, s in enumerate(seg_tl):
    has = "loras" in s and s["loras"]
    nm = [r["name"] for r in s.get("loras", [])]
    if i == 1:
        ok = has and all(SL.is_h3_family_lora(x) for x in nm) and \
            nm == ["H3/H3_Combat_V2.safetensors"]
        check(f"D1.12 段2(战斗) loras={nm} 全过白名单且==H3/H3_Combat_V2", ok, json.dumps(nm))
    else:
        check(f"D1.13 段{i+1}(未触发) 无凭空 LoRA", not has, json.dumps(nm))

# total_frames 自洽
frames_each = [b_new._segment_frames(s, 24) for s in segs]
print(f"  各段帧数={frames_each} 合计={sum(frames_each)}")
wv = dirnode.get("widgets_values") or []
named = dirnode.get("widgets_values_named") or {}
check("D1.14 Director total_frames == 各段帧数之和（覆盖模板 43）",
      wv[10] == sum(frames_each) and sum(frames_each) != 43,
      f"wv[10]={wv[10]} sum={sum(frames_each)}")
check("D1.15 timeline.totalFrames == total_frames == named",
      tl.get("totalFrames") == wv[10] == named.get("total_frames"),
      f"tl={tl.get('totalFrames')} wv={wv[10]} named={named.get('total_frames')}")

print("=" * 78)
print("D2. 回退路径（内存里把 h3_video 指回旧二采模板，构建旧模板）")
old_val = CFG.WORKFLOW_TEMPLATE["h3_video"]
CFG.WORKFLOW_TEMPLATE["h3_video"] = "minimax_h3_director_二采_加速.json"
try:
    b_old = HB.H3DirectorBuilder(OLD_TPL)
    wf2, layout2 = b_old.build(segs, filename_prefix="video/test_qa2_old",
                               width=544, height=960, frame_rate=24)
    got = True
except Exception as e:
    import traceback
    traceback.print_exc()
    wf2, layout2 = None, None
    got = False
finally:
    CFG.WORKFLOW_TEMPLATE["h3_video"] = old_val

check("D2.1 旧二采模板 build 不抛异常", got)
if got:
    check("D2.2 layout['refine_node'] 非 None", layout2["refine_node"] is not None, str(layout2["refine_node"]))
    sols2 = [n for n in wf2["nodes"] if n.get("type") == "SolAttnPatch"]
    check("D2.3 二采构建后存在 SolAttnPatch", bool(sols2), str(len(sols2)))
    if sols2:
        check("D2.4 二采 SolAttnPatch.widgets_values == _SOLATTN_ALIGNED_WV（align_accel_chain 默认 True）",
              sols2[0].get("widgets_values") == HB._SOLATTN_ALIGNED_WV,
              json.dumps(sols2[0].get("widgets_values")))
    print(f"  （二采 layout refine_node={layout2['refine_node']} director_node={layout2['director_node']}）")

print()
print("=" * 78)
print(f"D 总计: {len(PASS)} PASS / {len(FAIL)} FAIL")
for f in FAIL:
    print("  - " + f)
print(f"config.WORKFLOW_TEMPLATE['h3_video'] 复原值 = {CFG.WORKFLOW_TEMPLATE['h3_video']!r}")

# -*- coding: utf-8 -*-
"""C：LoRA 白名单负路径。"""
import hashlib
import json
import os
import sys

ROOT = r"C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统"
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import comfyui_models as CM  # noqa: E402
import h3_segment_loras as SL  # noqa: E402

STORE = os.path.join(ROOT, "output", "comfyui_models.json")


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def rd(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


STORE_BASE = rd(STORE)
STORE_SHA = sha(STORE)
print(f"[store baseline] sha256={STORE_SHA} content={STORE_BASE!r}")

H3_FILES = [
    "H3/H3_Combat_V2.safetensors",
    "H3/Motion_Repair.safetensors",
    "h3-realism-people-t2v-i2v-r2v.safetensors",
    "minimax_h3/minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors",
    "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
]
IMG_FILES = [
    "Qwen-Image-Lightning-4steps-V1.0.safetensors",
    "zi-base-gm_male_20.safetensors",
    "flux_style_lora.safetensors",
]

print("=" * 78)
print("C1. is_h3_family_lora 真值表")
print("  -- 磁盘实测 5 个 H3 文件（应全 True）--")
c1_ok = True
for n in H3_FILES:
    r = SL.is_h3_family_lora(n)
    c1_ok &= (r is True)
    print(f"    {r!s:5}  {n}")
print("  -- 图片链路名（应全 False）--")
for n in IMG_FILES:
    r = SL.is_h3_family_lora(n)
    c1_ok &= (r is False)
    print(f"    {r!s:5}  {n}")
print("  -- 边界 --")
edge = [
    ("空串", "", False),
    ("None", None, False),
    ("非字符串 int", 12345, False),
    ("非字符串 bytes", b"h3/x", False),
    ("大写 H3", "H3Foo.safetensors", True),
    ("子目录 H3/", "H3/x.safetensors", True),
    ("纯空格", "   ", False),
    ("大写含h3小写", "SOMETHING_H3.safetensors", True),
    ("无h3", "zi-base-gm_male_20.safetensors", False),
]
for label, v, want in edge:
    r = SL.is_h3_family_lora(v)
    ok = (r == want)
    c1_ok &= ok
    print(f"    {r!s:5} (期望 {want}) {label}: {v!r}  {'OK' if ok else '!!FAIL'}")
print(f"  C1 总判: {'PASS' if c1_ok else 'FAIL'}")

print("=" * 78)
print("C2. scan() 候选过滤（假 object_info，不连 ComfyUI）")


def fake_object_info():
    core = {}
    core["UNETLoader"] = {"input": {"required": {"unet_name": [["m.safetensors"]]}},
                          "python_module": "nodes"}
    core["CLIPLoader"] = {"input": {"required": {"clip_name": [["c.safetensors"]]}},
                          "python_module": "nodes"}
    core["VAELoader"] = {"input": {"required": {"vae_name": [["v.safetensors"]]}},
                         "python_module": "nodes"}
    core["MiniMaxH3TRTVAELoader"] = {"input": {"required": {
        "decoder": [["d.engine"]], "encoder": [["e.engine"]]}}, "python_module": "nodes"}
    all_loras = list(H3_FILES) + list(IMG_FILES)
    core["LoraLoaderModelOnly"] = {"input": {"required": {"lora_name": [all_loras]}},
                                   "python_module": "nodes"}
    core["MiniMaxH3Director"] = {"python_module": "custom_nodes.ComfyUI_MiniMaxH3_Director"}
    return core


class FakeClient:
    def __init__(self, oi):
        self.oi = oi

    def get_object_info(self, force=False):
        return self.oi


res = CM.scan(FakeClient(fake_object_info()))
c2_ok = True
if not res.get("success"):
    print("  scan 未成功:", res)
    c2_ok = False
for slot in res["slots"]:
    vals = slot["values"]
    if slot["key"] in ("lora_channel_a", "lora_channel_b"):
        bad = [v for v in vals if not SL.is_h3_family_lora(v)]
        has_non_h3 = bool(bad)
        c2_ok &= (not has_non_h3)
        print(f"  [{slot['key']}] values={vals}")
        print(f"      非 H3 混入={bad}  -> {'OK' if not has_non_h3 else '!!FAIL'}")
    else:
        print(f"  [{slot['key']}] (无过滤) values={vals}")
print(f"  C2 总判: {'PASS' if c2_ok else 'FAIL'}")

print("=" * 78)
print("C3. save_selection 负路径（图片 LoRA 必须被拒 + 文件字节不变）")
before = open(STORE, "rb").read()
try:
    CM.save_selection({"lora_channel_a": "Qwen-Image-Lightning-4steps-V1.0.safetensors"})
    print("  !! FAIL: 未抛异常")
    c3_ok = False
except ValueError as e:
    print(f"  已拒绝: ValueError: {e}")
    c3_ok = True
except Exception as e:
    print(f"  抛了非 ValueError: {type(e).__name__}: {e}")
    c3_ok = False
after = open(STORE, "rb").read()
print(f"  文件字节不变: {before == after}  sha256={sha(STORE)} (baseline {STORE_SHA})")
c3_ok &= (before == after)

print("=" * 78)
print("C4. 读侧闸门（手工写脏值到 store，断言不会带出）")
backup = open(STORE, "rb").read()
try:
    dirty = {"unet_main": "minimax-h3\\minimax_h3_ref2va_pruned_int8_convrot.safetensors",
             "lora_channel_a": "Qwen-Image-Lightning-4steps-V1.0.safetensors"}
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(dirty, f, ensure_ascii=False, indent=2)
    print(f"  已写入脏值: {rd(STORE)!r}")
    sel = CM.resolve_selection()
    print(f"  resolve_selection() = {sel}")
    ov = CM.overrides_for_template("h3_director_r2v_单采.json")
    print(f"  overrides_for_template(单采) = {ov}")
    ov2 = CM.overrides_for_template("minimax_h3_director_二采_加速.json")
    print(f"  overrides_for_template(二采) = {ov2}")
    leaked = ("lora_channel_a" in sel) or any(
        "Qwen-Image" in str(v) for d in list(ov.values()) + list(ov2.values()) for v in d.values())
    c4_ok = not leaked
    print(f"  脏值泄漏: {leaked} -> {'OK' if c4_ok else '!!FAIL'}")
finally:
    with open(STORE, "wb") as f:
        f.write(backup)
    print(f"  已恢复 store: sha256={sha(STORE)} match_baseline={sha(STORE)==STORE_SHA} content={rd(STORE)!r}")
    c4_ok &= (sha(STORE) == STORE_SHA)

print("=" * 78)
print("C5. discover_style_loras() 白名单 + 内置三条 + turbo 仍在")
lst = SL.discover_style_loras()
names = [it["name"] for it in lst]
print(f"  discovered = {names}")
c5_ok = all(SL.is_h3_family_lora(n) for n in names)
print(f"  每项都过 is_h3_family_lora: {c5_ok}")
for req in ("H3/H3_Combat_V2.safetensors", "H3/Motion_Repair.safetensors",
            "h3-realism-people-t2v-i2v-r2v.safetensors"):
    present = req in names
    c5_ok &= present
    print(f"  必含 {req}: {present}")
# lora_channel_a 候选仍含 turbo
res2 = CM.scan(FakeClient(fake_object_info()))
la = next(s for s in res2["slots"] if s["key"] == "lora_channel_a")
turbo = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
print(f"  lora_channel_a 候选含 turbo: {turbo in la['values']} -> {la['values']}")
c5_ok &= (turbo in la["values"])
print(f"  C5 总判: {'PASS' if c5_ok else 'FAIL'}")

print("=" * 78)
print(f"最终 store sha256={sha(STORE)} == baseline {STORE_SHA}: {sha(STORE)==STORE_SHA}")
print(f"C1={c1_ok} C2={c2_ok} C3={c3_ok} C4={c4_ok} C5={c5_ok}")

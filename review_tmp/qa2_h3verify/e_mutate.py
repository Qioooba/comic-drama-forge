# -*- coding: utf-8 -*-
"""E 变异测试：把源改坏 -> 跑守卫应变红 -> 立即恢复并核对 sha256。"""
import hashlib
import os
import subprocess

ROOT = r"C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统"
PY = r"C:/Python314/python.exe"
GUARD_DIR = os.path.join(ROOT, ".workbuddy", "test")


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


MUTATIONS = [
    {
        "name": "MUT-A  h3_director_builder: SolAttn tau 1.3→1.2（wv+named 同步）",
        "file": os.path.join(ROOT, "app", "h3_director_builder.py"),
        "guard": "verify_h3_director_builder.py",
        "edits": [
            ("_SOLATTN_ALIGNED_WV = [1.3, 0.2",
             "_SOLATTN_ALIGNED_WV = [1.2, 0.2"),
            ('"tau": 1.3, "start_percent": 0.2',
             '"tau": 1.2, "start_percent": 0.2'),
        ],
    },
    {
        "name": "MUT-B  h3_director_builder: align_frames 网格 17→16",
        "file": os.path.join(ROOT, "app", "h3_director_builder.py"),
        "guard": "verify_h3_director_builder.py",
        "edits": [
            ("return n + (5 - (n % 17)) % 17",
             "return n + (5 - (n % 16)) % 16"),
        ],
    },
    {
        "name": "MUT-C  comfyui_models: 去掉 lora_channel_a 的 values_filter",
        "file": os.path.join(ROOT, "app", "comfyui_models.py"),
        "guard": "verify_h3_template_and_lora_whitelist.py",
        "edits": [
            ('        "field": "lora_name",\n'
             '        # ⭐ 白名单：只接受 MiniMax H3 系 LoRA，禁止图片链路 LoRA 混入视频生成\n'
             '        "values_filter": "h3_lora",\n',
             '        "field": "lora_name",\n'),
        ],
    },
    {
        "name": "MUT-D  comfyui_models: 二采 vae_audio target 67→68",
        "file": os.path.join(ROOT, "app", "comfyui_models.py"),
        "guard": "verify_h3_template_and_lora_whitelist.py",
        "edits": [
            ('"minimax_h3_director_二采_加速.json": [67],',
             '"minimax_h3_director_二采_加速.json": [68],'),
        ],
    },
]

for m in MUTATIONS:
    fp = m["file"]
    base_sha = sha(fp)
    orig = open(fp, "r", encoding="utf-8", newline="").read()
    mut = orig
    ok_apply = True
    for old, new in m["edits"]:
        if old not in mut:
            print(f"!! 锚点未找到: {old[:60]!r}")
            ok_apply = False
            break
        mut = mut.replace(old, new, 1)
    if not ok_apply:
        continue
    open(fp, "w", encoding="utf-8", newline="").write(mut)
    print("=" * 78)
    print(m["name"])
    print(f"  变异后 sha256={sha(fp)} (baseline {base_sha})")
    try:
        r = subprocess.run([PY, os.path.join(GUARD_DIR, m["guard"])],
                           cwd=ROOT, capture_output=True, text=True)
        tail = [l for l in r.stdout.splitlines() if "[FAIL]" in l or "总计" in l
                or "结果:" in l]
        print(f"  守卫 {m['guard']} rc={r.returncode}  -> {'变红 OK' if r.returncode != 0 else '!! 仍全绿'}")
        for l in tail[:14]:
            print(f"    | {l.strip()}")
        if r.stderr.strip():
            print(f"    stderr尾: {r.stderr.strip()[-200:]!r}")
    finally:
        open(fp, "w", encoding="utf-8", newline="").write(orig)
        same = sha(fp) == base_sha
        print(f"  恢复后 sha256={sha(fp)} match_baseline={same}")
        assert same, "！！恢复失败"

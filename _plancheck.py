import sys, os, json, io
sys.path.insert(0, r"C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统")
sys.path.insert(0, r"C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\\app")
base_dir = r"C:\\Users\\liujianghua\\WorkBuddy\\2026-09-09-16-55-22\\漫剧生成系统"
comfy_root = r"D:\\ComfyUI_portable_TE_v260619\\ComfyUI\\ComfyUI"
sys.path.insert(0, comfy_root)
sys.path.insert(0, os.path.join(comfy_root, "custom_nodes", "ComfyUI_MiniMaxH3_Director"))
import folder_paths
folder_paths.folder_names_and_paths["input"] = [[os.path.join(comfy_root, "input")], folder_paths.folder_names_and_paths["input"][1]]
from director.gen_timeline import is_gen_timeline, is_prompt_batch_timeline
from lib.task_modes import resolve_task_key
td = open(os.path.join(base_dir, "_td_dump.json"), encoding="utf-8").read()
t = json.loads(td)
g = t.get("global") or {}
task_type = g.get("taskType") or ""
tk = resolve_task_key(task_type)
print("task_key:", tk)
print("timelineMode:", t.get("timelineMode"), "| is_gen:", is_gen_timeline(t, tk), "| is_prompt_batch:", is_prompt_batch_timeline(t, tk))
print("editMode:", t.get("editMode"))
print("commonEnabled:", g.get("commonEnabled"))
print("global refs:", [r.get("imageFile") for r in (g.get("refs") or [])])
print("global audios:", [r.get("audioFile") for r in (g.get("refAudios") or [])])
print("segments:", len(t.get("segments") or []))
for s in t.get("segments") or []:
    print("  seg", s.get("id"), "taskType=", repr(s.get("taskType")), "len=", s.get("length"), "refs=", [r.get("imageFile") for r in (s.get("refs") or [])])
# now try building the plan
try:
    from nodes.director_common import prepare_director_plan
    plan = prepare_director_plan(
        timeline_data=td, task_type=task_type, global_prompt="",
        total_frames=int(t.get("totalFrames") or 438), frame_rate=float(t.get("frameRate") or 24),
        width=int(t.get("width") or 960), height=int(t.get("height") or 544),
        ref_max_size=int(t.get("refMaxSize") or 960), unique_id="test",
        i2v_groups=None, r2v_groups=None, selflift=None, semantic_bridge=None,
        refine=None, face_refine=None,
    )
    print("PLAN OK: segs=", plan.segment_count, "edit_mode=", plan.edit_mode, "global_task_key=", plan.global_task_key)
    for seg in plan.segments:
        print("   ", seg.index, seg.task_key, "refs=", [r.image_file for r in seg.refs], "audios=", [a.audio_file for a in seg.ref_audios], "prompt_head=", (seg.prompt or "")[:60].replace("\n"," "))
except Exception as e:
    import traceback; traceback.print_exc()
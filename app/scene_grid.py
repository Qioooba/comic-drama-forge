# -*- coding: utf-8 -*-
"""场景九宫格机位预览（2026-10-01，对标 BigBanana 的候选构图机制）。

把一张场景 base 图扩展为「9 个机位各一张同场景变体 + 一张 3x3 拼接预览」：
- 生成走现有 Qwen-Image-Edit 参考图编辑链路（base 图作参考，逐机位出全分辨率图）；
- 机位句子与 comfyui_client.SCENE_VIEW_ANGLE_ZH 同款中文格式（4 档沿用 + 5 档预览新增）；
- 拼接预览仅作选格参考，每张都是全分辨率、可直接「应用」为场景新 base；
- 「应用」= 选中机位图升级为 base.png（旧 base 移入回收站），后续分镜参考图与
  按机位出图自动沿用新视角。

设计依据（2026-10-01 调研）：qwenmultiangle/ComfyUI 官方均无可靠的单 prompt 九宫格——
可靠做法是 9 次独立生成 + 拼接（单角度质量远高于让模型画九宫格）；多角度提示词必须
与参考图同语言（中文机位句 + 中文场景描述，不要中英混拼）。
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 9 个机位档：前 4 档与现有机位口径一致（front/left45/right45/top），后 5 档为预览新增。
# sentence 为该机位的中文画面描述（与 comfyui_client 场景多视角提示词同款格式）。
SCENE_GRID_ANGLES: List[Dict[str, str]] = [
    {"key": "front",   "label": "正面全景",
     "sentence": "从场景正前方平视拍摄的全景，地面纵深与背景层次完整"},
    {"key": "left45",  "label": "左前 45°",
     "sentence": "从场景左前方约 45 度平视拍摄，同时呈现场景正面与左侧面"},
    {"key": "right45", "label": "右前 45°",
     "sentence": "从场景右前方约 45 度平视拍摄，同时呈现场景正面与右侧面"},
    {"key": "top",     "label": "顶部鸟瞰",
     "sentence": "相机升到场景正上方俯拍（鸟瞰机位），画面以地面布局与陈设的顶面为主"},
    {"key": "wide",    "label": "大远景",
     "sentence": "拉远到大远景，整个场景居于画面中央，四周留出大片周围环境"},
    {"key": "low",     "label": "低角度仰拍",
     "sentence": "相机贴近地面向上仰拍，前景物件因透视被放大，天空或顶部结构入画"},
    {"key": "detail",  "label": "细节特写",
     "sentence": "近距离特写场景中最有辨识度的陈设细节，背景浅景深虚化"},
    {"key": "depth",   "label": "纵深透视",
     "sentence": "沿场景主轴纵深拍摄，两侧物件向画面深处汇聚，强调空间透视"},
    {"key": "back",    "label": "背面反打",
     "sentence": "从场景背后向入口方向反打拍摄，呈现与正面相反的空间关系"},
]


def stitch_grid(image_paths: List[str], out_path: str,
                cell_width: int = 640) -> str:
    """把 9 张（或任意 n 张）图拼成 3 列网格预览图；不足 9 张时按实际数量排布。"""
    from PIL import Image
    imgs = [Image.open(p).convert("RGB") for p in image_paths if os.path.isfile(p)]
    if not imgs:
        raise ValueError("没有可拼接的图片")
    cell_h = int(cell_width * imgs[0].height / max(1, imgs[0].width))
    cells = [im.resize((cell_width, cell_h)) for im in imgs]
    cols = 3
    rows = (len(cells) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_width, rows * cell_h), (12, 12, 16))
    for idx, im in enumerate(cells):
        sheet.paste(im, ((idx % cols) * cell_width, (idx // cols) * cell_h))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    sheet.save(out_path, "PNG")
    return out_path


def generate_scene_grid(client, project: str, scene_name: str,
                        base_image_path: str, scene_prompt: str, style: str,
                        asset_dir: str, seed: int = None, size=None,
                        progress_cb: Callable[[int, int, Dict], None] = None) -> Dict:
    """逐机位生成场景变体并拼接 3x3 预览。

    client: comfyui_client.ComfyUIClient 实例（app 模块级实例，由调用方传入避免循环导入）。
    scene_prompt: 场景内容描述（剧本 scenes[].reference_prompt_zh / appearance）。
    返回 {"grid": 预览图路径, "angles": [{key,label,path}], "failed": [...]}。
    """
    import style_kit
    from comfyui_client import SCENE_NO_CHARACTER_SUFFIX

    grid_dir = os.path.join(asset_dir, "grid")
    os.makedirs(grid_dir, exist_ok=True)
    base_name = client.upload_image(
        base_image_path,
        f"comic_drama_scenegrid_{project}_{scene_name}_base.png",
        image_type="output")
    styled = style_kit.with_style(scene_prompt, style, with_tail=False) if style else scene_prompt
    # 场景图必须去人（与 generate_scene_base 同一口径：人物属于分镜，不属于场景资产）
    styled = client.sanitize_scene_prompt(styled)

    total = len(SCENE_GRID_ANGLES)
    done_paths: List[Dict[str, str]] = []
    failed: List[Dict[str, str]] = []
    for i, angle in enumerate(SCENE_GRID_ANGLES):
        if progress_cb:
            try:
                progress_cb(i, total, {"angle": angle["key"], "label": angle["label"]})
            except Exception:  # noqa: BLE001
                pass
        view_prompt = f"{styled}。本图机位（{angle['label']}）：{angle['sentence']}。"
        if SCENE_NO_CHARACTER_SUFFIX not in view_prompt:
            view_prompt = view_prompt.rstrip("。;； ") + SCENE_NO_CHARACTER_SUFFIX
        try:
            img_path = client._run_multiview_workflow(
                base_name, view_prompt, seed=seed, size=None,
                filename_prefix=f"comic_drama_scenegrid/{project}_{scene_name}_{angle['key']}")
            dst = os.path.join(grid_dir, f"{angle['key']}.png")
            if img_path and os.path.isfile(img_path):
                os.makedirs(grid_dir, exist_ok=True)
                shutil.copy2(img_path, dst)
                done_paths.append({"key": angle["key"], "label": angle["label"], "path": dst})
            else:
                failed.append({"angle": angle["key"], "error": "生成未返回文件"})
        except Exception as e:  # noqa: BLE001  单机位失败不阻断其余机位
            logger.warning("场景九宫格[%s/%s] 机位 %s 生成失败：%s: %s",
                           project, scene_name, angle["key"], type(e).__name__, e)
            failed.append({"angle": angle["key"], "error": str(e)[:200]})

    grid_path = ""
    if len(done_paths) >= 2:
        try:
            grid_path = stitch_grid([d["path"] for d in done_paths],
                                    os.path.join(grid_dir, "grid_preview.png"))
        except Exception as e:  # noqa: BLE001
            logger.warning("场景九宫格预览拼接失败：%s", e)
    return {"grid": grid_path, "angles": done_paths, "failed": failed,
            "total": total, "ok_count": len(done_paths)}


def apply_grid_angle(asset_dir: str, angle_key: str, trash_move=None) -> Dict[str, str]:
    """把选中机位图升级为场景新 base（旧 base 交由 trash_move 移入回收站）。

    trash_move: app.py 的 _trash_move(src, category, trash_root, cleared, skipped)；
    传 None 则直接覆盖（不推荐，但保证函数可独立测试）。
    """
    src = os.path.join(asset_dir, "grid", f"{angle_key}.png")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"预览图不存在：{src}")
    base_png = os.path.join(asset_dir, "base.png")
    result = {"promoted": src, "replaced": ""}
    if os.path.isfile(base_png) and trash_move is not None:
        trash_move(base_png)
        result["replaced"] = base_png
    shutil.copy2(src, base_png)
    return result

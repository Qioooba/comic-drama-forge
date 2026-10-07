# -*- coding: utf-8 -*-
"""分镜“画布优先 + 身份锚点不裁剪”的集成级无GPU测试。"""
from __future__ import annotations

import logging
import os
import sys
import types

from PIL import Image

os.environ.setdefault("MJSCXT_AUTOPILOT", "0")
_APP_DIR = os.path.abspath("app")
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from api import _shared as sh  # noqa: E402
import identity_contract as ic  # noqa: E402


def _write_image(path, size, color):
    Image.new("RGB", size, color).save(path, format="PNG")


def test_unify_canvas_crops_scene_but_never_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "QC_DIR", str(tmp_path / "qc"))
    monkeypatch.setattr(
        sh, "app", types.SimpleNamespace(logger=logging.getLogger("identity-test"))
    )
    identity_path = tmp_path / "char.png"
    scene_path = tmp_path / "scene.png"
    _write_image(identity_path, (512, 512), (210, 160, 130))
    _write_image(scene_path, (640, 360), (30, 40, 50))

    identity = ("主角色", "参考图1（<image1>）是角色「甲」的身份锚点", str(identity_path))
    scene = ("场景", "参考图2（<image2>）是场景「会议室」的空间锚点", str(scene_path))
    ordered = sh._promote_storyboard_canvas([identity, scene])
    result = sh._unify_ref_canvas(ordered, (320, 180), "identity_pipeline_test")

    assert result[0][0] == "场景"
    assert result[1][2] == str(identity_path)
    with Image.open(result[0][2]) as im:
        assert im.size == (320, 180)
    with Image.open(result[1][2]) as im:
        assert im.size == (512, 512)


def test_identity_picker_derives_clean_anchor_without_touching_base(tmp_path, monkeypatch):
    asset_dir = tmp_path / "角色甲"
    asset_dir.mkdir()
    base = asset_dir / "base.png"
    _write_image(base, (640, 640), (255, 255, 255))

    # 在白底上绘制可被纯CPU检测器识别的脸部色块与五官。
    from PIL import ImageDraw

    im = Image.open(base).convert("RGB")
    d = ImageDraw.Draw(im)
    d.ellipse((210, 105, 430, 370), fill=(206, 151, 122))
    d.pieslice((195, 70, 445, 290), 180, 360, fill=(45, 35, 30))
    d.ellipse((265, 215, 300, 240), fill=(35, 25, 20))
    d.ellipse((340, 215, 375, 240), fill=(35, 25, 20))
    im.save(base)

    before = base.read_bytes()
    monkeypatch.setattr(sh, "app", types.SimpleNamespace(logger=logging.getLogger("identity-test")))
    payload = {"_dir": str(asset_dir), "base": str(base)}
    picked = sh._pick_char_identity_anchor(payload)

    assert picked == str(asset_dir / "identity" / "face.png")
    assert os.path.isfile(picked)
    assert base.read_bytes() == before


def test_asset_and_qc_helpers_use_same_identity_contract():
    assert sh.identity_contract is ic
    assert "_pick_char_identity_anchor" in sh.__all__
    assert "_promote_storyboard_canvas" in sh.__all__

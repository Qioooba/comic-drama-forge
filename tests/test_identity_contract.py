# -*- coding: utf-8 -*-
"""身份契约与画布优先重构的无GPU回归测试。"""
from __future__ import annotations

import json
import os
import sys

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.abspath("app"))

import identity_contract as ic  # noqa: E402
import comfyui_client  # noqa: E402


def _write_synthetic_sheet(path):
    im = Image.new("RGB", (640, 640), "white")
    d = ImageDraw.Draw(im)
    # 简化的皮肤脸 + 深色眼睛/头发，足够触发纯CPU身份候选。
    d.ellipse((210, 105, 430, 370), fill=(206, 151, 122))
    d.pieslice((195, 70, 445, 290), 180, 360, fill=(45, 35, 30))
    d.ellipse((265, 215, 300, 240), fill=(35, 25, 20))
    d.ellipse((340, 215, 375, 240), fill=(35, 25, 20))
    d.rectangle((245, 350, 395, 600), fill=(100, 105, 110))
    im.save(path, format="PNG")


def test_derive_face_anchor_is_cpu_only_and_preserves_source(tmp_path):
    source = tmp_path / "base.png"
    _write_synthetic_sheet(str(source))
    before = source.read_bytes()
    out = tmp_path / "identity" / "face.png"

    anchor = ic.derive_face_anchor(str(source), str(out))

    assert anchor is not None
    assert out.is_file()
    assert source.read_bytes() == before
    assert anchor.source_sha256
    assert anchor.bbox is not None


def test_ensure_identity_assets_is_idempotent_and_tracks_source(tmp_path):
    source = tmp_path / "base.png"
    _write_synthetic_sheet(str(source))

    first = ic.ensure_identity_assets(str(tmp_path), str(source))
    second = ic.ensure_identity_assets(str(tmp_path), str(source))

    assert first["ok"] is True
    assert first["status"] == "ready"
    assert os.path.isfile(first["face_path"])
    assert second["face_path"] == first["face_path"]
    manifest = json.loads((tmp_path / "identity" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_sha256"] == ic._file_sha256(str(source))
    assert manifest["character_id"].startswith("char_")


def test_ensure_identity_assets_fail_open_for_blank_sheet(tmp_path):
    source = tmp_path / "base.png"
    Image.new("RGB", (320, 320), "white").save(source)

    result = ic.ensure_identity_assets(str(tmp_path), str(source))

    assert result["ok"] is True
    assert result["status"] == "source_only"
    assert result["face_path"] == ""


def test_canvas_priority_promotes_scene_and_keeps_identity_uncropped():
    identity_a = ("主角色", "参考图1（<image1>）是角色「甲」的身份锚点", "/char/a.png")
    identity_b = ("次角色", "参考图2（<image2>）是角色「乙」的身份锚点", "/char/b.png")
    scene = ("场景", "参考图3（<image3>）是场景「会议室」的空间锚点", "/scene/base.png")
    item = ("道具", "参考图4（<image4>）是物品「本子」的锚点", "/item/base.png")

    ordered = ic.promote_canvas_reference([identity_a, identity_b, scene, item])

    assert ordered[0][0] == "场景"
    assert [r[0] for r in ordered[1:3]] == ["主角色", "次角色"]
    assert ic.is_identity_reference(identity_a)
    assert not ic.should_unify_reference(identity_a)
    assert ic.should_unify_reference(scene)
    assert ic.should_unify_reference(item)


def test_blocking_reference_wins_canvas_slot():
    identity = ("主角色", "身份锚点", "/char/a.png")
    scene = ("场景", "空间锚点", "/scene/base.png")
    blocking = "/qc/block.png"

    ordered = ic.promote_canvas_reference(
        [identity, scene], blocking_ref=blocking, blocking_label="3D导演台构图基准"
    )

    assert ordered[0][0] == blocking
    assert "3D导演台构图基准" in ordered[0][1]


def test_scene_canvas_is_not_called_identity_anchor_in_prompt():
    shot = {
        "shot_id": 1,
        "camera": "中景固定",
        "shot_type": "中景",
        "location": "公司会议室",
        "description": "两人在会议室对峙。",
        "characters_in_shot": ["甲", "乙"],
    }
    labels = [
        "参考图1（<image1>）是场景「公司会议室」的空间与光照锚点",
        "参考图2（<image2>）是角色「甲」的身份锚点（面部身份锚点）：保持其面部身份",
        "参考图3（<image3>）是角色「乙」的身份锚点（面部身份锚点）：独立保持其面部身份",
    ]
    prompt = comfyui_client.ComfyUIClient.build_storyboard_prompt(shot, labels, has_characters=True)

    assert "PRIMARY CANVAS:" in prompt
    assert "and identity anchor" not in prompt.split("IDENTITY:", 1)[0]
    assert "must not define any character's identity or appearance" in prompt
    assert "IDENTITY:" in prompt
    assert "<image2>" in prompt and "<image3>" in prompt


def test_identity_first_prompt_still_declares_identity_anchor():
    shot = {
        "shot_id": 2,
        "camera": "近景固定",
        "shot_type": "近景",
        "location": "公司会议室",
        "description": "角色直视镜头。",
        "characters_in_shot": ["甲"],
    }
    labels = ["参考图1（<image1>）是角色「甲」的身份锚点（面部身份锚点）"]
    prompt = comfyui_client.ComfyUIClient.build_storyboard_prompt(shot, labels, has_characters=True)

    assert "and identity anchor" in prompt

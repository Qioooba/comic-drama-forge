# -*- coding: utf-8 -*-
"""双层角色资产 / 镜头级 selector / H3 / Structured QC 离线测试（零 GPU）。"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

from PIL import Image, ImageDraw

os.environ.setdefault("MJSCXT_AUTOPILOT", "0")
_APP = os.path.abspath("app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import character_assets as ca  # noqa: E402
import h3_common_refs  # noqa: E402
import qc_client  # noqa: E402
from api import _shared as sh  # noqa: E402
from comfyui_client import ComfyUIClient  # noqa: E402


def _img(path, color=(180, 120, 90), size=(64, 80)):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def _complete_char(root: Path) -> Path:
    d = root / "char_a"
    _img(d / "base.png")
    _img(d / "identity" / "face_front.png")
    _img(d / "body" / "full_front.png")
    _img(d / "body" / "full_back.png")
    _img(d / "body" / "full_left45.png")
    _img(d / "framing" / "half_front.png")
    return d


def test_full_shot_selects_full_front_not_sheet(tmp_path):
    d = _complete_char(tmp_path)
    r = ca.select_character_reference({"_dir": str(d)}, {"shot_type": "全景"})
    assert Path(r["path"]).name == "full_front.png"
    assert r["asset_role"] == "machine_anchor"


def test_closeup_selects_face_and_does_not_crop_sheet(tmp_path, monkeypatch):
    d = _complete_char(tmp_path)
    r = ca.select_character_reference({"_dir": str(d)}, {"shot_type": "大特写"})
    assert Path(r["path"]).name == "face_front.png"

    monkeypatch.setattr(sh, "QC_DIR", str(tmp_path / "qc"))
    monkeypatch.setattr(sh, "app", types.SimpleNamespace(logger=_DummyLogger()))
    sheet = d / "base.png"
    assert sh._closeup_char_crop(str(sheet), "proj", 1) == str(d / "identity" / "face_front.png")


def test_back_shot_selects_full_back(tmp_path):
    d = _complete_char(tmp_path)
    r = ca.select_character_reference({"_dir": str(d)}, {"shot_type": "背面"})
    assert Path(r["path"]).name == "full_back.png"


def test_left45_selects_and_falls_back_with_warning(tmp_path):
    d = _complete_char(tmp_path)
    r = ca.select_character_reference({"_dir": str(d)}, {"camera": "左侧45度"})
    assert Path(r["path"]).name == "full_left45.png"
    (d / "body" / "full_left45.png").unlink()
    r2 = ca.select_character_reference({"_dir": str(d)}, {"camera": "左侧45度"})
    assert Path(r2["path"]).name == "full_front.png"
    assert r2["fallback"]


def test_two_characters_get_distinct_references(tmp_path, monkeypatch):
    a = _complete_char(tmp_path)
    b = tmp_path / "char_b"
    _img(b / "base.png")
    _img(b / "identity" / "face_front.png")
    _img(b / "body" / "full_front.png")
    _img(b / "framing" / "half_front.png")
    monkeypatch.setattr(sh, "app", types.SimpleNamespace(logger=_DummyLogger()))
    shot = {"shot_id": 1, "shot_type": "中景", "characters_in_shot": ["A", "B"]}
    idx = {
        "A": {"name": "A", "_dir": str(a), "image": str(a / "base.png")},
        "B": {"name": "B", "_dir": str(b), "image": str(b / "base.png")},
    }
    refs = sh._allocate_storyboard_refs(shot, idx, {}, {})
    paths = [r[2] for r in refs if r[0] in ("主角色", "次角色")]
    assert len(paths) == 2 and len(set(paths)) == 2
    assert all("base.png" not in p for p in paths)


def test_prompt_binds_screen_left_and_right_to_identity_images():
    shot = {
        "shot_id": 1, "shot_type": "中景", "camera": "中景固定",
        "location": "会议室", "description": "两人对峙",
        "characters_in_shot": ["A", "B"],
        "blocking": [
            {"name": "A", "x": "left"},
            {"name": "B", "x": "right"},
        ],
    }
    labels = [
        "参考图1（<image1>）是场景「会议室」的空间锚点",
        "参考图2（<image2>）是角色「A」的身份锚点（half_front视图）",
        "参考图3（<image3>）是角色「B」的身份锚点（half_front视图）",
    ]
    prompt = ComfyUIClient.build_storyboard_prompt(shot, labels, has_characters=True)
    assert "POSITION BINDINGS:" in prompt
    assert "screen-left" in prompt and "<image2>" in prompt
    assert "screen-right" in prompt and "<image3>" in prompt
    assert "do not swap these bindings" in prompt


def test_strict_outfit_missing_blocks_selection(tmp_path):
    d = _complete_char(tmp_path)
    r = ca.select_character_reference(
        {"_dir": str(d)}, {"shot_type": "中景", "outfit_key": "missing"},
        strict=True)
    assert r["error"] and "服装资产" in r["error"]
    assert r["path"] == ""


def test_existing_outfit_selects_current_framing_asset(tmp_path):
    d = _complete_char(tmp_path)
    outfit = d / "outfits" / "red"
    _img(outfit / "half_front.png", color=(220, 20, 20))
    r = ca.select_character_reference(
        {"_dir": str(d)}, {"shot_type": "中景", "outfit_key": "red"}, strict=True)
    assert Path(r["path"]).parent.name == "half_front" or Path(r["path"]).name == "half_front.png"
    assert Path(r["path"]).parent == outfit
    assert r["outfit_key"] == "red"


def test_machine_anchor_generation_prompt_is_clean(monkeypatch):
    captured = {}

    def fake_generate(self, workflow, prompt, **kwargs):
        captured["prompt"] = prompt
        return ["/fake.png"]

    monkeypatch.setattr(ComfyUIClient, "_generate_base_image", fake_generate)
    client = ComfyUIClient.__new__(ComfyUIClient)
    client.generate_character_anchor("full_front", "黑发男性角色设定")
    prompt = captured["prompt"]
    assert "单人物" in prompt and "无文字" in prompt and "无多视角拼版" in prompt
    assert "人物设定表" not in prompt and "三视图设定" not in prompt


def test_legacy_sheet_only_has_explicit_fallback(tmp_path):
    d = tmp_path / "legacy"
    _img(d / "base.png")
    comp = ca.asset_completeness(str(d))
    assert comp["status"] == "legacy_sheet_fallback"
    r = ca.select_character_reference({"_dir": str(d)}, {"shot_type": "全景"})
    assert Path(r["path"]).name == "base.png"
    assert "Character Sheet" in r["warning"]


def test_legacy_front_half_back_are_mapped(tmp_path):
    d = tmp_path / "legacy_views"
    _img(d / "base.png")
    _img(d / "front.png")
    _img(d / "half.png")
    _img(d / "back.png")
    assert Path(ca.select_character_reference({"_dir": str(d)}, {"shot_type": "全景"})["path"]).name == "front.png"
    assert Path(ca.select_character_reference({"_dir": str(d)}, {"shot_type": "中景"})["path"]).name == "half.png"
    assert Path(ca.select_character_reference({"_dir": str(d)}, {"shot_type": "背面"})["path"]).name == "back.png"


def test_h3_component_uses_clean_anchor_and_metadata(tmp_path, monkeypatch):
    d = _complete_char(tmp_path)
    monkeypatch.setattr(sh, "app", types.SimpleNamespace(logger=_DummyLogger()))
    shot = {"shot_id": 1, "shot_type": "中景", "characters_in_shot": ["A"]}
    idx = {"A": {"name": "A", "_dir": str(d), "image": str(d / "base.png")}}
    comps = sh._h3_shot_ref_components(shot, idx, {}, {})
    ch = next(c for c in comps if c["kind"] == "character")
    assert Path(ch["path"]).name == "half_front.png"
    assert ch["asset_role"] == "machine_anchor"
    assert ch["path"] != str(d / "base.png")


def test_h3_common_refs_do_not_merge_different_paths(tmp_path):
    a = {"kind": "character", "name": "A", "common_key": "char:A",
         "path": str(tmp_path / "a.png")}
    b = {"kind": "character", "name": "A", "common_key": "char:A",
         "path": str(tmp_path / "b.png")}
    assert h3_common_refs.asset_key(a) != h3_common_refs.asset_key(b)


def test_h3_picture_numbering_matches_declared_refs():
    chars = [{"name": "A", "path": "/a.png", "asset_role": "machine_anchor",
              "view": "half_front"}]
    scenes = [{"name": "S", "path": "/s.png"}]
    defs, subjects, sb = ComfyUIClient._h3_picture_defs(
        chars, scenes, storyboard_ref={"name": "storyboard"})
    labels = [x[0] for x in defs]
    assert labels[0] == "<Picture 1>"
    assert sb == "<Picture 1>"
    assert len(subjects) == 1 and subjects[0]["picture"] == "<Picture 2>"
    assert all(f"<Picture {i}>" == labels[i - 1] for i in range(1, len(labels) + 1))


def test_structured_qc_hard_gate_blocks_high_score():
    verdict = {"ok": True, "passed": True, "accepted": True, "score": 95,
               "issues": ["角色A换脸，且左右站位颠倒"],
               "critical_issues": []}
    checks = qc_client.build_structured_checks(
        "镜头描述", verdict, blocking_spec="人物数2，A在左B在右")
    assert checks["hard_pass"] is False
    merged = qc_client._merge_structured_checks(dict(verdict), blocking_spec="x")
    assert merged["passed"] is False and merged["blocked"] is True


def test_structured_qc_accepts_positive_spatial_text():
    verdict = {"ok": True, "passed": True, "accepted": True, "score": 88,
               "issues": ["左右站位正确，服装一致，人物未变形"],
               "critical_issues": []}
    checks = qc_client.build_structured_checks(
        "镜头", verdict, blocking_spec="A screen-left, B screen-right")
    assert checks["hard_pass"] is True


def test_candidate_selector_prioritizes_hard_gates_over_score():
    scores = [95, 80]
    gates = [False, True]
    assert qc_client.pick_best_candidate(scores, gates) == 1


def test_sheet_split_requires_explicit_regions(tmp_path):
    import sheet_split
    src = tmp_path / "base.png"
    _img(src, size=(200, 200))
    out = tmp_path / "out"
    result = sheet_split.split_regions(
        str(src),
        {"full_front": {"bbox": [0.1, 0.1, 0.5, 0.8]}},
        str(out), layout_version="test")
    assert Path(result["full_front"]).is_file()
    try:
        sheet_split.split_regions(str(src), {}, str(out))
    except sheet_split.SheetSplitError:
        pass
    else:
        raise AssertionError("empty regions must be rejected")


def test_strict_first_frame_gate_requires_structured_qc(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "STORYBOARDS_DIR", str(tmp_path / "sb"))
    sb = tmp_path / "sb" / "proj"
    sb.mkdir(parents=True)
    (sb / "shot_01.png").write_bytes(b"png")
    (sb / "shot_01.meta.json").write_text(json.dumps({
        "qc": {"accept": True, "blocked": False,
               "structured_checks": {"hard_pass": True}}
    }), encoding="utf-8")
    ok, reason = sh._first_frame_hard_gate("proj", 1, 1)
    assert ok and not reason
    (sb / "shot_01.meta.json").write_text(json.dumps({
        "qc": {"accept": True, "blocked": False,
               "structured_checks": {"hard_pass": False}}
    }), encoding="utf-8")
    ok2, reason2 = sh._first_frame_hard_gate("proj", 1, 1)
    assert not ok2 and "硬门槛" in reason2


def test_anchor_incremental_generation_only_fills_missing(tmp_path, monkeypatch):
    d = tmp_path / "char"
    _img(d / "base.png")
    _img(d / "identity" / "face_front.png")
    _img(d / "body" / "full_front.png")
    # half_front 缺失；伪造 GPU 输出与 ingest，验证只补这一档。
    fake_out = tmp_path / "fake_half.png"
    _img(fake_out, color=(10, 20, 30))
    calls = []

    class _DummyClient:
        def generate_character_anchor(self, key, *args, **kwargs):
            calls.append(key)
            return [str(fake_out)]

    monkeypatch.setattr(sh, "app", types.SimpleNamespace(logger=_DummyLogger()))
    monkeypatch.setattr(sh, "comfyui_client", _DummyClient())
    monkeypatch.setattr(sh, "CHARACTER_ANCHOR_GENERATION", True)
    monkeypatch.setattr(sh, "QC_DIR", str(tmp_path / "qc"))
    monkeypatch.setattr(sh, "_ingest_comfy_output", lambda files, dst, logger=None: shutil_copy(files[0], dst))
    res = sh._generate_character_machine_anchors(
        str(d), "角色提示词", project_name="p", asset_name="char")
    assert calls == ["half_front"]
    assert (d / "framing" / "half_front.png").is_file()
    assert res["generated"].get("half_front")


def shutil_copy(src, dst):
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Image.open(src).save(dst)
    return dst


class _DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

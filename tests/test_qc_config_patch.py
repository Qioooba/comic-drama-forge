# -*- coding: utf-8 -*-
"""Q1（2026-10-07）「保存质检配置点了没效果」的回归测试。

背景：`POST /api/qc/config` 直接把 ``request.json`` 丢给 ``save_config``，而前端
``qcApi.updateConfig`` 发的是 ``{"config": {...}}``。``save_config`` 按 CONFIG_KEYS
白名单**静默跳过**不认识的键，于是接口回 ``success: True`` +「质检配置已保存」，
磁盘上一个字节没变 —— 用户看到的就是「点保存没反应」，而前端紧接着 ``load(true)``
又把草稿刷回旧值。

这里锁三件事：
1. 包裹形状能被归一成有效 patch（旧代码下必须是空 → 本组测试即回归点）；
2. 归一后的 patch **真的落盘**（不止是函数返回值对）；
3. 真正一个字段都认不出来时返回空 patch，让路由据此 fail-loud（400），
   杜绝「假成功」再次发生。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import qc_client  # noqa: E402


# 前端 qcApi.updateConfig 在 2026-10-07 之前一直发的形状（已发布的 app/static 产物同款）
WRAPPED = {"config": {"pass_score": 42, "max_retries": 5}}


def _on_disk(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class NormalizeConfigPatchTests(unittest.TestCase):
    """纯归一化逻辑（不碰磁盘、不碰凭证库）"""

    def test_wrapped_shape_is_recognized(self):
        patch_ = qc_client.normalize_config_patch(dict(WRAPPED))
        self.assertEqual(patch_, {"pass_score": 42, "max_retries": 5})
        # 旧实现的失败点：包裹键 "config" 不在白名单 → 返回空 → 路由静默什么都没做
        self.assertTrue(patch_, "包裹形状被归一成了空 patch，保存会再次静默失效")

    def test_flat_shape_is_authoritative(self):
        self.assertEqual(
            qc_client.normalize_config_patch({"pass_score": 80, "enabled": True}),
            {"pass_score": 80, "enabled": True})

    def test_top_level_wins_but_nested_is_not_lost(self):
        patch_ = qc_client.normalize_config_patch(
            {"config": {"pass_score": 1, "best_of": 3}, "pass_score": 9})
        self.assertEqual(patch_, {"pass_score": 9, "best_of": 3})

    def test_unknown_only_body_yields_empty_patch(self):
        """路由靠「空 patch」判 400。没有这条，假成功就还有缝可钻。"""
        for body in ({}, {"reuse_llm": True}, {"totally_unknown": 1},
                     {"config": "not-a-dict"}, {"config": {"nope": 1}},
                     None, [], "string"):
            with self.subTest(body=body):
                self.assertEqual(qc_client.normalize_config_patch(body), {})

    def test_non_dict_config_is_ignored_not_crashed(self):
        self.assertEqual(qc_client.normalize_config_patch({"config": None,
                                                          "enabled": False}),
                         {"enabled": False})

    def test_probe_vision_pseudo_key_survives(self):
        """`_probe_vision` 不在 CONFIG_KEYS 但被 _save_config_impl 读，不能被过滤掉。"""
        self.assertIn("_probe_vision", qc_client.ACCEPTED_PATCH_KEYS)
        self.assertEqual(qc_client.normalize_config_patch({"_probe_vision": True}),
                         {"_probe_vision": True})

    def test_updated_at_passes_normalize_but_is_never_written(self):
        """`updated_at` 在 CONFIG_KEYS 里，故能过归一化；但 `_save_config_impl` 显式
        `continue` 掉它 —— 时间戳必须由 save_config 自己盖，不能被请求体注入。"""
        self.assertIn("updated_at", qc_client.ACCEPTED_PATCH_KEYS)
        self.assertEqual(
            qc_client.normalize_config_patch({"updated_at": "1999-01-01T00:00:00"}),
            {"updated_at": "1999-01-01T00:00:00"})
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "qc_config.json")
            with patch.object(qc_client, "_sync_credentials_db"), \
                 patch.object(qc_client, "resolve_endpoint",
                              return_value={"base_url": "", "api_key": "",
                                            "model": "", "source": "test"}), \
                 patch.object(qc_client, "_refresh_vision_status",
                              side_effect=lambda _p, _b, c, **kw: c):
                saved = qc_client.save_config(path, {"updated_at": "1999-01-01T00:00:00"})
        self.assertNotEqual(saved["updated_at"], "1999-01-01T00:00:00")


class WrappedShapeReachesDiskTests(unittest.TestCase):
    """端到端：包裹形状 → 归一 → save_config → 真的写进 qc_config.json"""

    def _save(self, tmp: str, body: dict) -> dict:
        path = str(Path(tmp) / "qc_config.json")
        patch_ = qc_client.normalize_config_patch(body)
        self.assertTrue(patch_, "包裹形状归一后为空，未进入保存路径")
        with patch.object(qc_client, "_sync_credentials_db"), \
             patch.object(qc_client, "resolve_endpoint",
                          return_value={"base_url": "", "api_key": "",
                                        "model": "", "source": "test"}), \
             patch.object(qc_client, "_refresh_vision_status",
                          side_effect=lambda _p, _b, c, **kw: c):
            qc_client.save_config(path, patch_)
        return _on_disk(path)

    def test_wrapped_shape_actually_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._save(tmp, dict(WRAPPED))
        self.assertEqual(cfg["pass_score"], 42)
        self.assertEqual(cfg["max_retries"], 5)

    def test_flat_shape_actually_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._save(tmp, {"pass_score": 70, "enabled": True})
        self.assertEqual(cfg["pass_score"], 70)
        self.assertTrue(cfg["enabled"])


if __name__ == "__main__":
    unittest.main()
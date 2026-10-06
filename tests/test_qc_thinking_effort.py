# -*- coding: utf-8 -*-
"""质检「思考档位」透传回归测试（2026-10-06）。

背景（真实缺陷）：AI 设置页「质检模型」的「思考档位」下拉是**死控件** ——
页面按 ``ai_config`` 回显 low/high/max，而质检运行时压根不读它
（``resolve_endpoint`` 只返回 ``disable_thinking``），用户改档位对请求体零影响。
同时 ``test_override`` 分支连 ``disable_thinking`` 都没带，导致「测试连通性」
永远按「允许思考」发请求，与真实质检行为分叉。

本测试锁住契约：
1. 档位与开关**互斥、档位优先**，归一成唯一有效状态；
2. 档位必须真的进到请求体（``chat_template_kwargs.reasoning_effort``），
   且不给 ``enable_thinking``（否则等于又关思考）；
3. 档位模式下 max_tokens 抬到档位水位（≥2048），避免思考吃光额度只剩空正文；
4. test_override 分支不再丢思考状态；
5. 非法档位必须收敛成 ""（否则部分模型静默按 max 烧 token）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import qc_client  # noqa: E402


def _cfg(**kw):
    """一份最小可用的质检配置（端点齐备，思考字段由调用方给）。"""
    base = {"enabled": True, "image_enabled": True,
            "base_url": "https://qc.example/v1",
            "api_key": "test-key", "model": "vision-model"}
    base.update(kw)
    return qc_client.load_config_dict(base)


class ThinkingResolveTests(unittest.TestCase):
    """档位 / 开关的归一规则。"""

    def test_effort_overrides_disable_switch(self):
        """设了档位就必须按档位发 —— 开关为 true 也不能把它压掉。"""
        ep = qc_client.resolve_endpoint(_cfg(reasoning_effort="high", disable_thinking=True))
        self.assertEqual(ep["reasoning_effort"], "high")
        self.assertFalse(ep["disable_thinking"])

    def test_off_maps_to_disable_switch(self):
        """off = 显式关思考（走 enable_thinking=false 分支，不注入档位）。"""
        ep = qc_client.resolve_endpoint(_cfg(reasoning_effort="off"))
        self.assertEqual(ep["reasoning_effort"], "")
        self.assertTrue(ep["disable_thinking"])

    def test_no_effort_keeps_legacy_switch(self):
        """没配档位时，老的 disable_thinking 开关必须继续生效（向后兼容）。"""
        ep = qc_client.resolve_endpoint(_cfg(disable_thinking=True))
        self.assertEqual(ep["reasoning_effort"], "")
        self.assertTrue(ep["disable_thinking"])

    def test_default_allows_thinking_without_effort(self):
        """默认既不关思考也不注入档位（由服务端取默认档）。"""
        ep = qc_client.resolve_endpoint(_cfg())
        self.assertEqual(ep["reasoning_effort"], "")
        self.assertFalse(ep["disable_thinking"])

    def test_illegal_effort_falls_back_to_empty(self):
        """非法档位收敛成 ""：绝不原样发给服务端（会被静默解析成最贵的 max 档）。"""
        for bad in ("turbo", "medium", "LOW!", "1"):
            with self.subTest(bad=bad):
                self.assertEqual(qc_client.normalize_reasoning_effort(bad), "")
        self.assertEqual(qc_client.normalize_reasoning_effort("HIGH"), "high")
        self.assertEqual(qc_client.normalize_reasoning_effort(" high "), "high")

    def test_test_override_branch_keeps_thinking_state(self):
        """回归：override 分支此前直接 return {**ov}，思考状态整个丢失，
        「测试连通性」于是永远按默认（允许思考）发，与真实质检分叉。"""
        ep = qc_client.resolve_endpoint(
            _cfg(reasoning_effort="max"),
            {"base_url": "https://other/v1", "api_key": "k2", "model": "m2"})
        self.assertEqual(ep["source"], "test_override")
        self.assertEqual(ep["reasoning_effort"], "max")
        self.assertFalse(ep["disable_thinking"])
        self.assertEqual(ep["min_tokens_when_thinking"],
                         qc_client.MIN_TOKENS_WHEN_THINKING)


class PayloadInjectionTests(unittest.TestCase):
    """档位必须真的进请求体 —— 这是「死控件」的核心断言。"""

    def _capture(self, ep, payload=None):
        """跑一次 _post_chat 并返回实际发出的 payload。"""
        seen = {}

        def _fake_once(_ep, sent, _timeout):
            seen.update(sent)
            return {"content": '{"ok": true}', "latency_ms": 1, "url": "u"}

        base = {"base_url": "https://qc.example/v1", "api_key": "k", "model": "m"}
        base.update(ep)
        with patch.object(qc_client, "_post_chat_once", _fake_once):
            qc_client._post_chat(base, payload or {"max_tokens": 512,
                                                   "messages": [{"role": "user", "content": "x"}]},
                                 timeout=5)
        return seen

    def test_effort_reaches_request_body(self):
        sent = self._capture({"reasoning_effort": "high", "disable_thinking": False})
        ctk = sent.get("chat_template_kwargs") or {}
        self.assertEqual(ctk.get("reasoning_effort"), "high")
        # 档位与开关互斥：发了档位就绝不能再带 enable_thinking（那等于把思考关了）
        self.assertNotIn("enable_thinking", ctk)

    def test_effort_raises_token_floor(self):
        """档位模式下思考本身要吃 token，额度太小必然只剩 reasoning_content。"""
        sent = self._capture({"reasoning_effort": "low", "disable_thinking": False},
                             payload={"max_tokens": 256, "messages": []})
        self.assertGreaterEqual(sent["max_tokens"],
                                qc_client.MIN_TOKENS_WHEN_REASONING_EFFORT)

    def test_effort_never_lowers_configured_floor(self):
        """用户在 qc_config 里把下限调得更高时，档位分支不能把它拉低。"""
        sent = self._capture({"reasoning_effort": "low", "disable_thinking": False,
                              "min_tokens_when_thinking": 8192},
                             payload={"max_tokens": 256, "messages": []})
        self.assertGreaterEqual(sent["max_tokens"], 8192)

    def test_disable_branch_unchanged(self):
        """无档位 + 关思考：仍是老行为（enable_thinking=false）。"""
        sent = self._capture({"disable_thinking": True})
        ctk = sent.get("chat_template_kwargs") or {}
        self.assertIs(ctk.get("enable_thinking"), False)
        self.assertNotIn("reasoning_effort", ctk)

    def test_no_config_injects_nothing(self):
        """都没配：不注入任何思考参数，只保证额度下限。"""
        sent = self._capture({})
        self.assertNotIn("chat_template_kwargs", sent)
        self.assertGreaterEqual(sent["max_tokens"], qc_client.MIN_TOKENS_WHEN_THINKING)


class SingleSourceTests(unittest.TestCase):
    """档位的单一事实源 = AI 凭证表（AI 设置页写入）。"""

    def test_load_config_picks_up_credentials_db_effort(self):
        db_ep = {"base_url": "https://db/v1", "model": "m-db", "api_key": "k-db",
                 "reasoning_effort": "max"}
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "qc_config.json")
            with patch("ai_credentials_db.get_credentials", return_value=dict(db_ep)):
                cfg = qc_client.load_config(path)
        self.assertEqual(cfg["reasoning_effort"], "max")
        self.assertEqual(qc_client.resolve_endpoint(cfg)["reasoning_effort"], "max")

    def test_save_config_normalizes_illegal_effort(self):
        """非法档位绝不能落盘（部分模型会把非法值静默按最贵档解析）。

        ⚠️ 必须把凭证 DB 也打桩空：load_config 以 DB 为单一事实源，不隔离的话
        读到的是**本机真实配置**（谁在跑这套测试谁的值），断言就成了「碰运气」。
        """
        empty_db = {"base_url": "", "model": "", "api_key": "", "reasoning_effort": ""}
        for raw, expect in (("turbo", ""), ("HIGH", "high"), ("off", "off")):
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    path = str(Path(tmp) / "qc_config.json")
                    with patch("ai_credentials_db.get_credentials",
                               return_value=dict(empty_db)), \
                         patch.object(qc_client, "_sync_credentials_db"), \
                         patch.object(qc_client, "_refresh_vision_status",
                                      side_effect=lambda _p, _b, c, **kw: c):
                        cfg = qc_client.save_config(path, {"reasoning_effort": raw})
                    self.assertEqual(cfg["reasoning_effort"], expect)
                    import json
                    with open(path, "r", encoding="utf-8") as f:
                        on_disk = json.load(f)
                    self.assertEqual(on_disk.get("reasoning_effort", ""), expect)

    def test_public_view_exposes_effective_state(self):
        """public_view 必须下发**归一后**的状态，否则界面只能各说各话。"""
        view = qc_client.public_view(_cfg(reasoning_effort="low", disable_thinking=True))
        self.assertEqual(view["reasoning_effort"], "low")
        self.assertFalse(view["thinking_disabled"])


if __name__ == "__main__":
    unittest.main()

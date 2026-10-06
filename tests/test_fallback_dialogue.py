# -*- coding: utf-8 -*-
"""兜底剧本台词恢复 + 拆镜不重复 + 台词闸门（2026-10-07）

背景：实测《测灵根》第 1 集，模型分镜只返回 reasoning_content 没有正文 →
整集 21 镜走原文兜底 → ``dialogue`` 全空 → 成片完全无人声，且流程一路「成功」。

本组用例锁住四件事：
1. 兜底镜头能从原文引号**恢复台词**（旧实现写死 ``"dialogue": []``）；
2. 切句**不会从引号中间劈开**（朴素按句末标点切会吃掉引号内的句号）；
3. 拆镜**不会把同一条台词复制成两份**（旧实现两镜都拿完整 lines）；
4. 说话人**宁缺毋滥**：原文不写全名时留空，绝不把台词安到主角头上。

全部纯函数、纯 CPU：不 import app、不触网、不占 GPU、不连 ComfyUI。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app"))

import dialogue_utils as D           # noqa: E402
import novel_to_script as N          # noqa: E402

CHARS = ["周野", "执事弟子", "木灵根少年", "土灵根少年", "无灵根少年", "测灵队伍群像"]
BIBLE = {"characters": [{"name": n} for n in CHARS],
         "scenes": [{"name": "山门石阶"}]}


# ---------------------------------------------------------------- 台词抽取

def test_extracts_quoted_dialogue():
    got = D.extract_quoted_dialogue("“木灵根，中品。”执事抬眼，“记名。”", characters=CHARS)
    assert [g["text"] for g in got] == ["木灵根，中品", "记名"], got


def test_keeps_verb_inside_line():
    """台词内容里的「说」字不能被当成叙述成分剥掉。

    真实回归：早期实现按「说/道/问…」剥引语开头，把「你说什么？」整句剥成空串。
    """
    got = D.extract_quoted_dialogue("“你说什么？”执事抬起眼。", characters=CHARS)
    assert [g["text"] for g in got] == ["你说什么？"], got


def test_speaker_only_when_adjacent_verb():
    """一级判据：角色名 + 紧邻说话动词才认，否则留空。"""
    got = D.extract_quoted_dialogue("周野低声道：“下一个。”", characters=CHARS)
    assert got == [{"speaker": "周野", "text": "下一个"}], got
    # 名字在远处出现、且没有紧邻的说话动词 → 留空（宁可退回默认音色）
    got2 = D.extract_quoted_dialogue("周野排在最后，前面还有两百来人，汗顺着脖子往下淌，“下一个。”",
                                     characters=CHARS)
    assert got2 == [{"speaker": "", "text": "下一个"}], got2


def test_unclosed_quote_is_skipped():
    assert D.extract_quoted_dialogue("引号不配对：“没闭合", characters=CHARS) == []


def test_nan_style_quotes_supported():
    got = D.extract_quoted_dialogue("老文风：「你来了。」", characters=CHARS)
    assert [g["text"] for g in got] == ["你来了"], got


def test_pure_punctuation_quote_dropped():
    assert D.extract_quoted_dialogue("“……”", characters=CHARS) == []


# ---------------------------------------------------------------- 切句

def test_split_sentences_keeps_quote_intact():
    """引号内的句号不能当断点，否则引号被劈成两半、台词凭空消失。"""
    text = "他抬头。“土灵根，下品。”执事翻了翻名册。"
    got = N._split_sentences(text)
    assert any("土灵根，下品。" in s and "执事翻了翻名册" in s for s in got), got


def test_split_sentences_loses_nothing():
    text = "“下一个。”\n\n他排队。\n“木灵根，中品。”执事道。\n没有了。"
    joined = "".join(N._split_sentences(text))
    for frag in ("下一个", "他排队", "木灵根，中品", "没有了"):
        assert frag in joined, (frag, joined)


# ---------------------------------------------------------------- 兜底镜头

def test_fallback_recovers_dialogue():
    """兜底镜头必须带出台词 —— 旧实现写死 []，这是整集无声的根因。"""
    text = "“下一个。”他排队。\n“木灵根，中品。”执事抬眼。\n“记名，入外门。”"
    shots = N._fallback_shots_for_chunk(
        {"index": 1, "text": text, "char_count": len(text)}, 3, BIBLE)
    lines = [d["text"] for s in shots for d in s["dialogue"]]
    assert lines, shots
    for want in ("下一个", "木灵根，中品", "记名，入外门"):
        assert want in lines, (want, lines)


def test_fallback_keeps_original_text_in_description():
    """「内容不丢」契约不变：原文措辞仍在 description 里。"""
    text = "“下一个。”他排在队尾，日头晒在背上。"
    shots = N._fallback_shots_for_chunk(
        {"index": 1, "text": text, "char_count": len(text)}, 2, BIBLE)
    assert shots[0]["description"].strip(), shots
    assert all(s["fallback"] for s in shots)


def test_fallback_without_quotes_still_gets_cues():
    """没有引号（纯叙述段）的兜底镜头不能是「空响镜」：无台词必须补音效铺底。"""
    text = "山脚下排着长队。日头晒在背上。汗顺着脖子往下淌。"
    shots = N._fallback_shots_for_chunk(
        {"index": 1, "text": text, "char_count": len(text)}, 2, BIBLE)
    for s in shots:
        if not s["dialogue"]:
            assert str(s["audio_cues"] or "").strip(), s


def test_fallback_empty_text_returns_nothing():
    assert N._fallback_shots_for_chunk({"index": 1, "text": "  "}, 2, BIBLE) == []


# ---------------------------------------------------------------- 拆镜不重复

def test_split_dialogue_shot_does_not_duplicate_lines():
    """拆镜后同一条台词只能存在于一镜，否则配音会念两遍。

    真实回归：旧实现两镜都拿完整 lines 列表，实测《测灵根》12 条台词被重复。
    """
    lines = [{"speaker": "执事弟子", "text": "记名，入外门。"},
             {"speaker": "执事弟子", "text": "去左边，候补堂领一百文路钱，那边去领吧，别站在这儿了。"}]
    row = {"shot_id": 1, "camera": "中景", "shot_type": "中景", "camera_motion": "固定",
           "duration": 8.0, "dialogue": lines, "dialogue_text": " ".join(
               x["text"] for x in lines),
           "description": "执事低头写名册。", "audio_cues": "人声嘈杂", "fallback": True}
    out = N._split_long_dialogue_shot(row)
    if len(out) == 1:
        return                      # 未触发拆镜（本组用例不强依赖阈值）
    a, b = out
    seen = [d["text"] for d in a["dialogue"]] + [d["text"] for d in b["dialogue"]]
    assert len(seen) == len(set(seen)), f"台词被重复：{seen}"


def test_split_action_shot_second_half_gets_cues():
    """动作拆镜的后半镜按设计不带台词，必须自带音效，否则成片到该镜黑屏静音。"""
    row = {"shot_id": 1, "camera": "中景", "shot_type": "中景", "camera_motion": "固定",
           "duration": 4.0, "dialogue": [], "dialogue_text": "",
           "description": "他抬手按上石面，又低头凑近细看，随后退开半步。",
           "audio_cues": ""}
    out = N._split_multi_action_row(row)
    if len(out) == 1:
        return
    assert str(out[1]["audio_cues"] or "").strip(), out[1]


# ---------------------------------------------------------------- 归一化不污染 speaker

def test_norm_shots_keeps_fallback_speaker_empty():
    """兜底镜头归一化时不能把留空的 speaker 填成角色表首位。

    否则刚躲掉的「主角替全场说话」会被重新灌回来（实测 30 条全被安到主角头上）。
    """
    shots = [{"camera": "中景", "location": "山门石阶",
              "description": "执事抬眼。", "emotion": "平静", "audio_cues": "",
              "dialogue": [{"speaker": "", "text": "下一个"}],
              "characters_in_shot": ["周野"], "items_in_shot": [],
              "prompt_h3": "", "fallback": True}]
    norm = N._norm_shots(shots, BIBLE, 1)
    assert norm, norm
    for s in norm:
        for d in s["dialogue"]:
            assert d["text"] == "下一个", d
            assert d["speaker"] == "", f"speaker 被污染成 {d['speaker']!r}"


# ---------------------------------------------------------------- infer_when_empty 开关

def test_infer_when_empty_false_keeps_speaker_blank():
    got = D.normalize_lines([{"speaker": "", "text": "下一个"}], characters=CHARS,
                            cast=["周野"], infer_when_empty=False)
    assert got == [{"speaker": "", "text": "下一个"}], got
    # 默认口径不变：老剧本仍按角色表补全
    got2 = D.normalize_lines([{"speaker": "", "text": "周野说下一个"}], characters=CHARS,
                             cast=["周野"])
    assert got2[0]["speaker"] == "周野", got2


# ---------------------------------------------------------------- 闸门判据

def test_audit_blocks_all_silent_episode():
    """整集 0 台词必须判不合格（出片闸门靠它 fail-closed）。"""
    shots = [{"shot_id": i, "description": "画面描述", "dialogue": [], "audio_cues": "风声"}
             for i in range(1, 4)]
    aud = D.audit_script({"shots": shots, "characters": [{"name": "周野"}]})
    assert aud["ok"] is False
    assert any("没有任何台词" in w for w in aud["warnings"]), aud["warnings"]


def test_audit_allows_episode_with_dialogue():
    shots = [{"shot_id": 1, "description": "画面", "dialogue": [{"speaker": "周野", "text": "走"}]},
             {"shot_id": 2, "description": "画面", "dialogue": [], "audio_cues": "风声"}]
    aud = D.audit_script({"shots": shots, "characters": [{"name": "周野"}]})
    assert aud["ok"] is True, aud["warnings"]


def test_audit_blocks_silent_shot_without_cues():
    shots = [{"shot_id": 1, "description": "画面", "dialogue": [{"speaker": "周野", "text": "走"}]},
             {"shot_id": 2, "description": "画面", "dialogue": [], "audio_cues": ""}]
    aud = D.audit_script({"shots": shots, "characters": [{"name": "周野"}]})
    assert aud["ok"] is False
    assert aud["stats"]["silent_shot_count"] == 1


# ---------------------------------------------------------------- 故障转移

def _fake_client(name, mode):
    """构造一个只实现故障转移所需最小接口的 LLMClient 替身。

    不继承 LLMClient：它的 ``configured`` 等是只读 property，子类赋值会抛
    AttributeError。这里用鸭子对象即可 —— FailoverLLMClient 只碰这几个属性。
    """
    import llm_client as LC

    class _Fake:
        def __init__(self):
            self.reasoning_effort = ""
            self.model = self.base_url = self.chat_url = name
            self.last_error, self.last_json_meta = "", {}
            self.disable_thinking = False
            self.calls = 0

        @property
        def configured(self):
            return True

        def chat_json_robust(self, *a, **k):
            self.calls += 1
            if mode == "reasoning_only":
                raise LC.LLMReasoningOnlyError("只有思考没有正文")
            return {"shots": [{"ok": True}]}

        def _build_payload(self, *a, **k):     # 探活路径
            raise LC.LLMError("探活失败（测试里不需要回切）")

    return _Fake()


def test_reasoning_only_triggers_failover():
    """「只吐思考」必须立刻切备用 —— 同模型提额/降档已被内部试尽，再试无意义。

    真实回归：这类失败原先要连吃 3 次预算才切换，而实测那次「连续 1 次」
    就抛出，用户配的备用模型根本没被用上，整集降级 → 台词全丢。
    """
    import llm_client as LC

    primary = _fake_client("主模型", "reasoning_only")
    backup = _fake_client("备用1", "ok")
    fo = LC.FailoverLLMClient([primary, backup], labels=["主模型", "备用1"], module="text")
    out = fo.chat_json_robust("p")
    assert out["shots"][0]["ok"] is True, out
    assert primary.calls == 1, f"主模型不该被反复重试，实际调了 {primary.calls} 次"
    assert backup.calls == 1, backup.calls
    assert fo._switch_log, "切换必须留痕"


def test_reasoning_only_raises_when_chain_exhausted():
    import llm_client as LC

    fo = LC.FailoverLLMClient([_fake_client("唯一", "reasoning_only")],
                              labels=["唯一"], module="text")
    try:
        fo.chat_json_robust("p")
        raise AssertionError("应当抛出 LLMReasoningOnlyError")
    except LC.LLMReasoningOnlyError:
        pass


def test_api_error_still_needs_threshold_before_switching():
    """普通 API 报错**保持原语义**：连吃 3 次预算才切，不被本次改动带偏。"""
    import llm_client as LC

    class _ApiFail(_fake_client("主模型", "ok").__class__):
        def chat_json_robust(self, *a, **k):
            self.calls += 1
            raise LC.LLMError("503 服务不可用")

    primary, backup = _ApiFail(), _fake_client("备用1", "ok")
    fo = LC.FailoverLLMClient([primary, backup], labels=["主模型", "备用1"], module="text")
    out = fo.chat_json_robust("p")
    assert out["shots"][0]["ok"] is True, out
    assert primary.calls == 3, f"API 报错应连吃 3 次预算，实际 {primary.calls} 次"


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)

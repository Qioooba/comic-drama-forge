# -*- coding: utf-8 -*-
"""台词归位（LLM 成功路径）+ 金句校验诚实度（2026-10-07）

背景：实测《测灵根》第 1 集，模型把 5 条原文金句原样写进了 ``description``，
``dialogue`` 写 ``[]``。而 ``dialogue`` 是本系统**唯一人声来源**（旁白 narration
已于 2026-09-19 关闭），于是成片整集无声，且流程一路报「成功」。

三道防线在同一点集体失效：
1. 提示词的「若上方给出金句强制写进 dialogue」—— quotes 从未被注入，条件永不成立；
2. ``continuity.check_quotes_in_script`` 搜的是整个 ``shots``（含 description），
   金句躺在画面描述里照样判 ``hit_rate=1.0``；
3. 剧本质检的「对白驱动率 ≥40%」没拦住 21/21 零台词。

本组锁住两件事：
1. :func:`split_quoted_dialogue` 能把描述里的引号台词**摘出并就地删除**，
   归位到 ``dialogue`` 后 description 只剩画面内容；
2. 说话人**宁缺毋滥** —— 名字与本句引号之间夹着别的引语时必须留空，
   否则「…周野问，“能，”执事说，“资质是差了些，”」会把执事的台词安到主角头上；
3. ``check_quotes_in_script`` 在「台词为 0、描述里却写着金句」时必须报未命中。

全部纯函数、纯 CPU：不 import app、不触网、不占 GPU、不连 ComfyUI。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app"))

import continuity  # noqa: E402
from dialogue_utils import split_quoted_dialogue  # noqa: E402

CHARS = ["周野", "执事弟子"]


class SplitQuotedDialogueTests(unittest.TestCase):
    """抽取 + 剥离"""

    def test_extracts_and_strips(self):
        desc = '周野说：“石头还没凉。”山风吹上来。'
        lines, cleaned = split_quoted_dialogue(desc, CHARS)
        self.assertEqual([ln["text"] for ln in lines], ["石头还没凉"])
        self.assertNotIn("“", cleaned)
        self.assertNotIn("石头还没凉", cleaned)
        self.assertIn("山风吹上来", cleaned)

    def test_speaker_from_adjacent_attribution(self):
        lines, _ = split_quoted_dialogue('周野道：“能修仙吗？”', CHARS)
        self.assertEqual(lines[0]["speaker"], "周野")

    def test_speaker_blank_when_name_not_written_in_full(self):
        """原文只写「执事」不写「执事弟子」→ 留空，绝不猜。"""
        lines, _ = split_quoted_dialogue('执事说：“记名，入外门。”', CHARS)
        self.assertEqual(lines[0]["speaker"], "")
        self.assertEqual(lines[0]["text"], "记名，入外门")

    def test_speaker_not_stolen_across_intervening_quote(self):
        """回归锁：《测灵根》ep01 shot16 的真实串。

        「资质是差了些」紧邻的是「执事说」，但回看窗口里「周野 + 问」也成立。
        缺了「名字与本句之间不得夹引号」这条判据，台词会被安到主角头上。
        """
        s = ('执事顿了顿，“四行俱有，俱不纯，”“杂灵根能修仙吗，”周野问，'
             '“能，”执事说，“资质是差了些，”山风吹上来')
        lines, _ = split_quoted_dialogue(s, CHARS)
        by_text = {ln["text"]: ln["speaker"] for ln in lines}
        self.assertIn("资质是差了些", by_text)
        self.assertEqual(by_text["资质是差了些"], "", "执事的台词不得被安到周野头上")
        # 「杂灵根能修仙吗」前面紧邻的引导不存在，同样必须留空
        self.assertEqual(by_text.get("杂灵根能修仙吗", ""), "")

    def test_no_quote_returns_text_untouched(self):
        desc = "周野抬头望向山门。"
        lines, cleaned = split_quoted_dialogue(desc, CHARS)
        self.assertEqual(lines, [])
        self.assertEqual(cleaned, desc, "无引号时不得做任何改写")

    def test_pure_interjection_kept_in_description(self):
        """「嗯」「……」这类纯语气不算台词，应留在描述里而不是被摘走。"""
        desc = "周野低声道：“嗯。”然后走开。"
        lines, cleaned = split_quoted_dialogue(desc, CHARS)
        self.assertEqual(lines, [])
        self.assertIn("嗯", cleaned)


class CheckQuotesHonestyTests(unittest.TestCase):
    """校验器不许再报假通过"""

    def _script(self, dialogue, description):
        return {"shots": [{"shot_id": 1, "dialogue": dialogue,
                           "description": description}]}

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="quotes_honest_")
        self.quote = "四行俱有，俱不纯。宗门叫它杂灵根。"

    def _write_quote(self):
        import json
        p = continuity.quotes_path(self._tmp, "K")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"quotes": [{"episode_no": 1, "text": self.quote,
                                  "speaker_hint": "执事弟子"}]}, f, ensure_ascii=False)

    def test_quote_in_description_only_is_MISSED(self):
        """本组最核心的一条：金句躺在 description、dialogue 为空 → 必须报未命中。"""
        self._write_quote()
        rep = continuity.check_quotes_in_script(
            self._tmp, "K", self._script([], f'执事说：“{self.quote}”'), 1)
        self.assertEqual(rep["hit"], [])
        self.assertEqual(len(rep["missed"]), 1)
        self.assertEqual(rep["hit_rate"], 0.0)
        self.assertEqual(rep["spoken_total_chars"], 0)

    def test_quote_in_dialogue_is_HIT(self):
        self._write_quote()
        rep = continuity.check_quotes_in_script(
            self._tmp, "K",
            self._script([{"speaker": "执事弟子", "text": self.quote}], ""), 1)
        self.assertEqual(len(rep["hit"]), 1)
        self.assertEqual(rep["missed"], [])
        self.assertEqual(rep["hit_rate"], 1.0)
        self.assertGreater(rep["spoken_total_chars"], 0)

    def test_zero_dialogue_never_reports_full_hit(self):
        """任何「整集 0 台词」的剧本都不该拿到 hit_rate=1.0。"""
        self._write_quote()
        rep = continuity.check_quotes_in_script(
            self._tmp, "K", self._script([], f'“{self.quote}”'), 1)
        self.assertNotEqual(rep["hit_rate"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

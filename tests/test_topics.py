"""Tests: tìm kiếm chủ đề + tự tạo kịch bản (automuse/topics).

Không cần mạng: đường Muse dùng ask_muse_text giả (monkeypatch
automuse.topics.ask_muse_text — topics.py import tên này vào namespace
của nó nên patch đúng chỗ đó); đường fallback test trực tiếp.

Chạy: python -m unittest tests.test_topics -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automuse import topics as tp  # noqa: E402
from automuse.script import validate  # noqa: E402

FAKE_YAML_DOC = """title: Gấu Bắc Cực
kind: documentary
aspect: "16:9"
fps: 30
default_voice: narrator
scenes:
  - id: scene-a
    title: Mở đầu băng giá
    duration: 10
    visual: Polar bear walking on sea ice at dawn, wide shot.
    narration: Bắc Cực lạnh âm bốn mươi độ, nhưng gấu vẫn sống.
  - id: scene-b
    title: Bơi giữa biển băng
    visual: Polar bear swimming between ice floes, tracking shot.
    narration: Gấu bơi một trăm cây số để tìm hải cẩu.
  - title: Gấu con chào đời
    duration: 10
    visual: Polar bear cub beside mother in snow den, close-up.
    narration: Gấu con chỉ nặng sáu trăm gam khi chào đời.
"""


class TestParseTopicReply(unittest.TestCase):
    def test_json_plain_array(self):
        reply = (
            '[{"title": "Quán nhỏ cuối phố", "summary": "Cô chủ quán phở bán đến 2 giờ sáng."}, '
            '{"title": "Chuyến xe cuối cùng", "summary": "Nữ tài xế xe buýt đêm."}]'
        )
        ideas = tp.parse_topic_reply(reply)
        self.assertEqual(len(ideas), 2)
        self.assertEqual(ideas[0].title, "Quán nhỏ cuối phố")
        self.assertIn("2 giờ sáng", ideas[0].summary)

    def test_json_fenced_array(self):
        reply = (
            "```json\n"
            '[{"title": "Vua của thảo nguyên", "summary": "Sư tử cái săn mồi."}]\n'
            "```"
        )
        ideas = tp.parse_topic_reply(reply)
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0].title, "Vua của thảo nguyên")

    def test_json_mixed_with_commentary(self):
        reply = (
            "Dưới đây là 2 chủ đề hay nhất:\n"
            '[{"title": "Tiệm spa của mèo Mimi", "summary": "Mèo chải lông."}, '
            '{"title": "Tiệm kem mèo", "summary": "Mèo múc kem."}]\n'
            "Chúc bạn làm video vui vẻ!"
        )
        ideas = tp.parse_topic_reply(reply)
        self.assertEqual(len(ideas), 2)
        self.assertEqual(ideas[1].title, "Tiệm kem mèo")

    def test_json_string_items(self):
        reply = '["Quán nhỏ cuối phố — Cô chủ quán phở.", "Chuyến xe cuối cùng"]'
        ideas = tp.parse_topic_reply(reply)
        self.assertEqual(ideas[0].title, "Quán nhỏ cuối phố")
        self.assertEqual(ideas[0].summary, "Cô chủ quán phở.")
        self.assertEqual(ideas[1].title, "Chuyến xe cuối cùng")
        self.assertEqual(ideas[1].summary, "")

    def test_numbered_lines(self):
        reply = (
            "1. Quán nhỏ cuối phố — Cô chủ quán phở bán đến 2 giờ sáng.\n"
            "2. Chuyến xe cuối cùng — Nữ tài xế xe buýt đêm.\n"
            "3. Bức thư không gửi — Ông lão viết thư mỗi tuần."
        )
        ideas = tp.parse_topic_reply(reply)
        self.assertEqual(len(ideas), 3)
        self.assertEqual(ideas[0].title, "Quán nhỏ cuối phố")
        self.assertEqual(ideas[0].summary, "Cô chủ quán phở bán đến 2 giờ sáng.")
        self.assertEqual(ideas[2].title, "Bức thư không gửi")

    def test_numbered_lines_title_only(self):
        reply = "1. Quán nhỏ cuối phố\n2. Chuyến xe cuối cùng"
        ideas = tp.parse_topic_reply(reply)
        self.assertEqual(ideas[0].title, "Quán nhỏ cuối phố")
        self.assertEqual(ideas[0].summary, "")

    def test_numbered_lines_continuation(self):
        reply = (
            "1. Quán nhỏ cuối phố\n"
            "Cô chủ quán phở bán đến 2 giờ sáng, khách quen gọi đúng một tô.\n"
            "2. Chuyến xe cuối cùng\n"
            "Nữ tài xế xe buýt đêm."
        )
        ideas = tp.parse_topic_reply(reply)
        self.assertEqual(ideas[0].title, "Quán nhỏ cuối phố")
        self.assertIn("đúng một tô", ideas[0].summary)

    def test_blank_line_blocks(self):
        reply = (
            "Quán nhỏ cuối phố\nCô chủ quán phở bán đến 2 giờ sáng.\n\n"
            "Chuyến xe cuối cùng\nNữ tài xế xe buýt đêm."
        )
        ideas = tp.parse_topic_reply(reply)
        self.assertEqual(len(ideas), 2)
        self.assertEqual(ideas[1].title, "Chuyến xe cuối cùng")

    def test_empty_and_junk_raise(self):
        for bad in ("", "   ", "...", "ok"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                tp.parse_topic_reply(bad)

    def test_junk_entries_dropped(self):
        reply = (
            '[{"title": "...", "summary": ""}, '
            '{"title": "Quán nhỏ cuối phố", "summary": "Cô chủ quán phở."}]'
        )
        ideas = tp.parse_topic_reply(reply)
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0].title, "Quán nhỏ cuối phố")


class TestSuggestFallback(unittest.TestCase):
    def test_fallback_nonempty_all_kinds(self):
        for kind in ("documentary", "movie", "animation", "asmr"):
            ideas = tp.suggest_topics(kind)
            self.assertGreaterEqual(len(ideas), 6, msg=kind)
            for idea in ideas:
                self.assertTrue(idea.title.strip(), msg=kind)
                self.assertTrue(idea.summary.strip(), msg=kind)

    def test_fallback_unknown_kind_uses_documentary(self):
        ideas = tp.suggest_topics("không-có-hướng-này")
        self.assertGreaterEqual(len(ideas), 6)
        self.assertEqual(ideas[0].title, "Vua của thảo nguyên")

    def test_fallback_count_cap(self):
        ideas = tp.suggest_topics("movie", count=3)
        self.assertEqual(len(ideas), 3)

    def test_keyword_filters_fallback(self):
        ideas = tp.suggest_topics("movie", keyword="phở")
        self.assertTrue(ideas)
        for idea in ideas:
            hay = (idea.title + " " + idea.summary).lower()
            self.assertIn("phở", hay)

    def test_keyword_no_match_returns_full_list(self):
        full = tp.suggest_topics("asmr")
        ideas = tp.suggest_topics("asmr", keyword="xyz-không-có")
        self.assertEqual([i.title for i in ideas],
                         [i.title for i in full])

    def test_keyword_case_insensitive(self):
        ideas = tp.suggest_topics("movie", keyword="PHỞ")
        self.assertTrue(ideas)
        self.assertEqual(ideas[0].title, "Quán nhỏ cuối phố")


class TestSuggestWithClient(unittest.TestCase):
    def test_client_json_parsed_and_capped(self):
        reply = (
            '[{"title": "A", "summary": "s1"}, {"title": "B", "summary": "s2"}, '
            '{"title": "C", "summary": "s3"}]'
        )
        with patch.object(tp, "ask_muse_text", return_value=reply) as fake:
            ideas = tp.suggest_topics("movie", count=2, client=object())
        fake.assert_called_once()
        self.assertEqual([i.title for i in ideas], ["A", "B"])

    def test_client_numbered_parsed(self):
        reply = "1. Chủ đề một — Hook một.\n2. Chủ đề hai — Hook hai."
        with patch.object(tp, "ask_muse_text", return_value=reply):
            ideas = tp.suggest_topics("documentary", client=object())
        self.assertEqual(len(ideas), 2)
        self.assertEqual(ideas[1].summary, "Hook hai.")

    def test_client_failure_falls_back(self):
        with patch.object(tp, "ask_muse_text",
                          side_effect=RuntimeError("gateway down")):
            ideas = tp.suggest_topics("asmr", client=object())
        self.assertGreaterEqual(len(ideas), 6)
        self.assertEqual(ideas[0].title, "Tiệm spa của mèo Mimi")

    def test_client_unparsable_falls_back(self):
        with patch.object(tp, "ask_muse_text", return_value="..."):
            ideas = tp.suggest_topics("animation", client=object())
        self.assertGreaterEqual(len(ideas), 6)

    def test_client_instruction_mentions_direction_and_count(self):
        seen = {}

        def fake_ask(client, text, timeout=180.0):
            seen["text"] = text
            return '[{"title": "X", "summary": "Y"}]'

        with patch.object(tp, "ask_muse_text", side_effect=fake_ask):
            tp.suggest_topics("asmr", keyword="mèo", count=5, client=object())
        self.assertIn("asmr", seen["text"])
        self.assertIn("mèo", seen["text"])
        self.assertIn("5", seen["text"])


class TestWriteScript(unittest.TestCase):
    def test_fake_client_yaml_parsed(self):
        with patch.object(tp, "ask_muse_text", return_value=FAKE_YAML_DOC):
            script = tp.write_script("Gấu Bắc Cực", kind="documentary",
                                     scene_count=3, scene_duration=10,
                                     client=object())
        self.assertEqual(script.title, "Gấu Bắc Cực")
        self.assertEqual(script.kind, "documentary")
        self.assertEqual(len(script.scenes), 3)
        self.assertEqual(validate(script), [])

    def test_fake_client_ids_normalized_to_s01_order(self):
        with patch.object(tp, "ask_muse_text", return_value=FAKE_YAML_DOC):
            script = tp.write_script("Gấu Bắc Cực", client=object())
        self.assertEqual([s.id for s in script.scenes],
                         ["s01", "s02", "s03"])

    def test_fake_client_missing_duration_gets_scene_duration(self):
        # Cảnh 2 trong FAKE_YAML_DOC không ghi duration -> nhận 10.
        with patch.object(tp, "ask_muse_text", return_value=FAKE_YAML_DOC):
            script = tp.write_script("Gấu Bắc Cực", scene_duration=10,
                                     client=object())
        self.assertEqual(script.scenes[1].duration, 10)

    def test_fake_client_kind_enforced(self):
        yaml_movie = FAKE_YAML_DOC.replace("kind: documentary",
                                           "kind: movie")
        with patch.object(tp, "ask_muse_text", return_value=yaml_movie):
            script = tp.write_script("Gấu Bắc Cực", kind="documentary",
                                     client=object())
        self.assertEqual(script.kind, "documentary")

    def test_fake_client_fenced_yaml(self):
        reply = "```yaml\n" + FAKE_YAML_DOC + "\n```"
        with patch.object(tp, "ask_muse_text", return_value=reply):
            script = tp.write_script("Gấu Bắc Cực", client=object())
        self.assertEqual(len(script.scenes), 3)
        self.assertEqual(validate(script), [])

    def test_fake_client_commentary_before_yaml(self):
        reply = "Đây là kịch bản bạn yêu cầu:\n\n" + FAKE_YAML_DOC
        with patch.object(tp, "ask_muse_text", return_value=reply):
            script = tp.write_script("Gấu Bắc Cực", client=object())
        self.assertEqual(len(script.scenes), 3)

    def test_skeleton_fallback_validates(self):
        for kind in ("documentary", "movie", "animation", "asmr"):
            script = tp.write_script("Chủ đề thử", kind=kind,
                                     scene_count=4, scene_duration=10)
            self.assertEqual(validate(script), [], msg=kind)
            self.assertEqual(len(script.scenes), 4)
            self.assertEqual([s.id for s in script.scenes],
                             ["s01", "s02", "s03", "s04"])
            self.assertTrue(all(s.duration == 10 for s in script.scenes))
            self.assertTrue(all(s.visual for s in script.scenes))

    def test_skeleton_narration_rules(self):
        doc = tp.write_script("Chủ đề thử", kind="documentary")
        self.assertTrue(all(s.narration for s in doc.scenes))
        self.assertIn("[Viết lời dẫn", doc.scenes[0].narration)
        movie = tp.write_script("Chủ đề thử", kind="movie")
        self.assertTrue(all(s.narration for s in movie.scenes))
        for kind in ("animation", "asmr"):
            script = tp.write_script("Chủ đề thử", kind=kind)
            self.assertTrue(all(s.narration == "" for s in script.scenes),
                            msg=kind)

    def test_skeleton_mentions_topic_in_visual(self):
        script = tp.write_script("Gấu Bắc Cực", kind="documentary")
        self.assertIn("Gấu Bắc Cực", script.scenes[0].visual)

    def test_strict_raises_on_bad_yaml(self):
        with patch.object(tp, "ask_muse_text",
                          return_value="không phải yaml: [lỗi"):
            with self.assertRaises(ValueError):
                tp.write_script("Chủ đề thử", client=object(), strict=True)

    def test_strict_raises_without_client(self):
        with self.assertRaises(ValueError):
            tp.write_script("Chủ đề thử", strict=True)

    def test_non_strict_bad_yaml_falls_back_to_skeleton(self):
        with patch.object(tp, "ask_muse_text",
                          return_value="không phải yaml: [lỗi"):
            script = tp.write_script("Chủ đề thử", client=object())
        self.assertEqual(validate(script), [])
        self.assertEqual(script.title, "Chủ đề thử")

    def test_non_strict_invalid_script_falls_back(self):
        # YAML hợp lệ nhưng cảnh thiếu visual -> validate lỗi -> skeleton.
        bad = (
            "title: X\nkind: documentary\nscenes:\n"
            "  - id: s01\n    title: Không visual\n    duration: 10\n"
            "    narration: Có lời dẫn nhưng không có visual.\n"
        )
        with patch.object(tp, "ask_muse_text", return_value=bad):
            script = tp.write_script("Chủ đề thử", client=object())
        self.assertEqual(validate(script), [])
        self.assertTrue(all(s.visual for s in script.scenes))

    def test_client_failure_falls_back(self):
        with patch.object(tp, "ask_muse_text",
                          side_effect=RuntimeError("gateway down")):
            script = tp.write_script("Chủ đề thử", client=object())
        self.assertEqual(validate(script), [])

    def test_scene_count_and_duration_guards(self):
        script = tp.write_script("Chủ đề thử", scene_count=0,
                                 scene_duration=0)
        self.assertEqual(len(script.scenes), 1)
        self.assertEqual(script.scenes[0].duration, 10)
        self.assertEqual(validate(script), [])


class TestWriteScriptAutoMode(unittest.TestCase):
    """Chế độ tự quyết (scene_count=None): Muse tự chọn số cảnh + duration."""

    FAKE_YAML_AUTO = """title: Rừng Mưa Nhiệt Đới
kind: documentary
aspect: "16:9"
fps: 30
default_voice: narrator
scenes:
  - id: a
    title: Tán rừng buổi sáng
    duration: 10
    visual: Sunlight through rainforest canopy, wide shot.
    narration: Rừng mưa nhận hai mét mưa mỗi năm mà vẫn khát nắng.
  - id: b
    title: Cơn mưa bất chợt
    duration: 15
    visual: Sudden rain over rainforest, tracking shot.
    narration: Mưa trút xuống trong mười lăm phút rồi tạnh, đủ làm sông dâng.
  - id: c
    title: Ếch độc nhỏ xíu
    duration: 8
    visual: Tiny poison frog on a leaf, macro shot.
    narration: Ếch chỉ dài hai phân mà độc đủ giết mười người.
  - id: d
    title: Đêm rừng không ngủ
    duration: 10
    visual: Rainforest at night with fireflies, wide shot.
    narration: Đêm xuống, rừng ồn ào hơn ban ngày vì côn trùng thức.
"""

    def test_auto_instruction_lets_muse_decide(self):
        seen = {}

        def fake_ask(client, text, timeout=180.0):
            seen["text"] = text
            return self.FAKE_YAML_AUTO

        with patch.object(tp, "ask_muse_text", side_effect=fake_ask):
            tp.write_script("Rừng Mưa", scene_count=None, client=object())
        self.assertIn("Decide YOURSELF how many scenes", seen["text"])
        self.assertIn("Decide YOURSELF each scene's duration", seen["text"])
        self.assertNotIn("Number of scenes:", seen["text"])

    def test_auto_preserves_variable_durations(self):
        with patch.object(tp, "ask_muse_text",
                          return_value=self.FAKE_YAML_AUTO):
            script = tp.write_script("Rừng Mưa", scene_count=None,
                                     client=object())
        self.assertEqual(len(script.scenes), 4)
        self.assertEqual([s.id for s in script.scenes],
                         ["s01", "s02", "s03", "s04"])
        self.assertEqual([s.duration for s in script.scenes],
                         [10, 15, 8, 10])
        self.assertEqual(sum(s.duration for s in script.scenes), 43)
        self.assertEqual(validate(script), [])

    def test_auto_no_client_gives_long_skeleton(self):
        script = tp.write_script("Rừng Mưa", scene_count=None)
        self.assertEqual(len(script.scenes), 20)
        self.assertEqual(validate(script), [])

    def test_auto_target_scenes_in_instruction(self):
        seen = {}

        def fake_ask(client, text, timeout=180.0):
            seen["text"] = text
            return self.FAKE_YAML_AUTO

        with patch.object(tp, "ask_muse_text", side_effect=fake_ask):
            tp.write_script("Rừng Mưa", scene_count=None, target_scenes=48,
                            client=object())
        self.assertIn("roughly 48 scenes", seen["text"])

    def test_auto_research_inserted_in_instruction(self):
        seen = {}

        def fake_ask(client, text, timeout=180.0):
            seen["text"] = text
            return self.FAKE_YAML_AUTO

        with patch.object(tp, "ask_muse_text", side_effect=fake_ask):
            tp.write_script("Rừng Mưa", scene_count=None,
                            research="Ếch độc dài hai phân, độc giết mười người.",
                            client=object())
        self.assertIn("Ếch độc dài hai phân", seen["text"])
        self.assertIn("researched source material", seen["text"])


class TestResearchTopic(unittest.TestCase):
    def test_returns_fact_sheet(self):
        with patch.object(tp, "ask_muse_text",
                          return_value="Kích thước: 30m. Nặng: 200 tấn."):
            out = tp.research_topic("Cá voi xanh", client=object())
        self.assertIn("200 tấn", out)

    def test_failure_returns_empty_not_raise(self):
        with patch.object(tp, "ask_muse_text",
                          side_effect=RuntimeError("gateway down")):
            out = tp.research_topic("Cá voi xanh", client=object())
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()

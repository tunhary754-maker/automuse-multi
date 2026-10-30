"""Test rà soát & sửa kịch bản (topics.review_script). Không gọi thật."""

import unittest
from unittest import mock

import automuse.topics as topics

ORIG = """title: "Test"
kind: documentary
aspect: "16:9"
fps: 30
default_voice: narrator
scenes:
  - id: s01
    title: "Mo dau"
    duration: 10
    visual: "A penguin walks on ice."
    narration: "Chim cánh cụt đi trên băng."
  - id: s02
    title: "San moi"
    duration: 10
    visual: "A penguin dives."
    narration: "Chim cánh cụt lặn bắt cá."
"""

FIXED = """```yaml
title: "Test"
kind: documentary
aspect: "16:9"
fps: 30
default_voice: narrator
scenes:
  - id: s01
    title: "Mo dau"
    duration: 10
    visual: "A penguin walks on cracked ice under a pale sky."
    narration: "Hai đầu Trái Đất, băng âm 60 độ, chim cánh cụt vẫn sống."
  - id: s02
    title: "San moi"
    duration: 10
    visual: "A penguin dives beneath clear ice, chasing fish."
    narration: "Chim cánh cụt lặn sâu 500 m, nín thở 20 phút để bắt cá."
```
CHANGES:
- s01: thêm hook đối lập + số liệu nhiệt độ vào mở đầu
- s02: bổ sung số liệu độ sâu và thời gian nín thở
"""


class TestReviewScript(unittest.TestCase):
    def test_review_returns_fixed_yaml_and_changes(self):
        with mock.patch.object(topics, "ask_muse_text",
                               return_value=FIXED) as fake:
            fixed, changes, script = topics.review_script(
                ORIG, client=object(),
                user_instruction="thêm số liệu thật vào cảnh 2")
        # Prompt gửi Muse phải chứa hướng dẫn của người dùng + nguyên
        # tắc giữ sát bản gốc.
        prompt = fake.call_args[0][1]
        self.assertIn("thêm số liệu thật vào cảnh 2", prompt)
        self.assertIn("MINIMAL", prompt)
        self.assertIn("Do not invent a different script", prompt)
        # YAML sửa parse được, validate qua, changes tách đúng.
        self.assertEqual(script.title, "Test")
        self.assertEqual(len(script.scenes), 2)
        self.assertNotIn("```", fixed)
        self.assertEqual(changes, [
            "s01: thêm hook đối lập + số liệu nhiệt độ vào mở đầu",
            "s02: bổ sung số liệu độ sâu và thời gian nín thở",
        ])

    def test_review_no_client_raises(self):
        with self.assertRaises(ValueError):
            topics.review_script(ORIG, client=None)

    def test_review_bad_yaml_raises_and_keeps_original(self):
        with mock.patch.object(topics, "ask_muse_text",
                               return_value="```yaml\ntitle: [\n```\nCHANGES:\n"):
            with self.assertRaises(ValueError):
                topics.review_script(ORIG, client=object())

    def test_review_ask_failure_raises(self):
        with mock.patch.object(topics, "ask_muse_text",
                               side_effect=RuntimeError("hong")):
            with self.assertRaises(ValueError):
                topics.review_script(ORIG, client=object())

    def test_parse_changes_variants(self):
        self.assertEqual(topics._parse_changes("không có gì"), [])
        self.assertEqual(
            topics._parse_changes("CHANGES:\n- none: script already good\n"),
            ["none: script already good"])


if __name__ == "__main__":
    unittest.main()

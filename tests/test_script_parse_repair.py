"""Test lỗi parse YAML kịch bản + tự sửa 1 lần trong write_script.

- Lỗi parse phải kèm lỗi YAML thật + cuối reply (để chẩn đoán).
- Parse hỏng -> write_script nhờ Muse sửa 1 lần, sửa được thì trả script.
- Sửa xong vẫn hỏng -> báo lỗi chi tiết, không thử lần 3.

Chạy: python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BROKEN = """```yaml
title: Test
kind: documentary
aspect: "16:9"
fps: 30
default_voice: narrator
scenes:
  - id: s01
    title: Canh 1
    duration: 10
    visual: A sifaka leaps between tall trees in Madagascar.
    narration: Sifaka nhay: mot cu nhay xa toi muoi met giua cac tan cay cao o Madagascar trong mua kho khan hiem.
  - id: s02
    title: Canh 2
    duration: 10
    visual: Close-up of a sifaka eating leaves.
    narration: No an la ca ngay de tich du nang luong cho nhung cu nhay dai tiep theo giua rung kho.
```"""

# Chỉ sửa lỗi cú pháp (bỏ dấu ":" trong lời dẫn), giữ nguyên nội dung.
FIXED = BROKEN.replace(
    "narration: Sifaka nhay: mot cu nhay xa",
    "narration: Sifaka nhay mot cu nhay xa")


class TestParseRepair(unittest.TestCase):
    def setUp(self):
        import automuse.topics as topics

        self.topics = topics
        self._orig = topics.ask_muse_text

    def tearDown(self):
        self.topics.ask_muse_text = self._orig

    def _fake_ask(self, replies):
        calls = []

        def fake(client, text, timeout=180.0):
            calls.append(text)
            return replies.pop(0)

        self.topics.ask_muse_text = fake
        return calls

    def test_parse_error_includes_yaml_error_and_tail(self):
        from automuse.topics import _parse_script_yaml

        with self.assertRaises(ValueError) as ctx:
            _parse_script_yaml(BROKEN)
        msg = str(ctx.exception)
        # Lỗi YAML thật (kèm dòng/cột) + cuối reply, không chỉ đầu reply.
        self.assertIn("lỗi YAML", msg)
        self.assertIn("cuối reply", msg)
        self.assertIn("No an la ca ngay", msg)

    def test_repair_succeeds_on_second_try(self):
        from automuse.topics import write_script

        calls = self._fake_ask([BROKEN, FIXED])
        script = write_script("Test", kind="documentary", scene_count=2,
                              scene_duration=10, client=object(),
                              strict=True)
        self.assertEqual(len(script.scenes), 2)
        self.assertEqual(len(calls), 2)
        self.assertIn("failed to parse", calls[1])

    def test_repair_failure_raises_detailed_only_twice(self):
        from automuse.topics import write_script

        calls = self._fake_ask([BROKEN, BROKEN])
        with self.assertRaises(ValueError) as ctx:
            write_script("Test", kind="documentary", scene_count=2,
                         scene_duration=10, client=object(), strict=True)
        msg = str(ctx.exception)
        self.assertIn("Không viết được kịch bản từ Muse", msg)
        self.assertIn("lỗi YAML", msg)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

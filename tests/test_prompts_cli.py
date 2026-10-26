"""Tests: lệnh prompts — viết prompt từ kịch bản (template, không cần acc).

Chạy: python -m unittest discover -s tests -v
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli  # noqa: E402
from automuse.script import load_script  # noqa: E402

SCRIPT_YAML = """title: Test prompts
kind: documentary
aspect: '16:9'
scenes:
  - title: Bình minh thảo nguyên
    duration: 8
    visual: "Bình minh lên trên thảo nguyên"
    narration: "Bình minh lên trên thảo nguyên châu Phi."
  - title: Sư tử cái rình mồi
    duration: 8
    visual: ""
    narration: "Sư tử cái rình mồi trong cỏ cao."
"""


class TestPromptsCli(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            args = cli.build_parser().parse_args(argv)
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_script_template_path(self):
        tmp = Path(tempfile.mkdtemp())
        script = tmp / "s.yaml"
        script.write_text(SCRIPT_YAML, encoding="utf-8")
        rc, out = self._run(["prompts", "--script", str(script)])
        self.assertEqual(rc, 0)
        self.assertIn("Bình minh thảo nguyên", out)
        self.assertIn("16:9", out)

    def test_apply_writes_prompts_yaml(self):
        tmp = Path(tempfile.mkdtemp())
        script = tmp / "s.yaml"
        script.write_text(SCRIPT_YAML, encoding="utf-8")
        rc, out = self._run(["prompts", "--script", str(script), "--apply"])
        self.assertEqual(rc, 0)
        dest = tmp / "s.prompts.yaml"
        self.assertTrue(dest.exists())
        reloaded = load_script(dest)
        self.assertEqual(len(reloaded.scenes), 2)
        self.assertIn("documentary cinematography", reloaded.scenes[0].visual)
        self.assertEqual(reloaded.scenes[0].narration,
                         "Bình minh lên trên thảo nguyên châu Phi.")

    def test_text_flag(self):
        rc, out = self._run([
            "prompts", "--text",
            "Cảnh 1 — Sư tử săn mồi\nSư tử rình trong cỏ.",
        ])
        self.assertEqual(rc, 0)
        self.assertIn("Sư tử", out)

    def test_no_input_errors(self):
        rc, out = self._run(["prompts"])
        self.assertEqual(rc, 1)
        self.assertTrue("Chưa có kịch bản" in out or "Kịch bản rỗng" in out)

    def test_unknown_account_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        script = tmp / "s.yaml"
        script.write_text(SCRIPT_YAML, encoding="utf-8")
        rc, out = self._run(["prompts", "--script", str(script),
                             "--account", "khongco"])
        self.assertEqual(rc, 1)
        self.assertIn("Không có tài khoản", out)


if __name__ == "__main__":
    unittest.main()

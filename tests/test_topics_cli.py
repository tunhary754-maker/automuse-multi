"""Tests: lệnh topics và write-script (fallback, không cần acc).

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
from automuse.script import load_script, validate  # noqa: E402


class TestTopicsCli(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            args = cli.build_parser().parse_args(argv)
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_topics_fallback_documentary(self):
        rc, out = self._run(["topics", "--kind", "documentary", "--count", "3"])
        self.assertEqual(rc, 0)
        self.assertIn("documentary", out)
        # 3 numbered topics
        self.assertIn("1.", out)
        self.assertIn("3.", out)

    def test_topics_fallback_all_kinds(self):
        for kind in ("movie", "documentary", "animation", "asmr"):
            rc, out = self._run(["topics", "--kind", kind, "--count", "2"])
            self.assertEqual(rc, 0, kind)
            self.assertIn("1.", out, kind)

    def test_topics_keyword_filters(self):
        rc, out = self._run(["topics", "--kind", "asmr", "--keyword", "mèo"])
        self.assertEqual(rc, 0)
        self.assertIn("mèo", out.lower())

    def test_topics_unknown_account_rejected(self):
        rc, out = self._run(["topics", "--kind", "documentary",
                             "--account", "khong-co-acc-nay"])
        self.assertEqual(rc, 1)
        self.assertIn("Không có tài khoản", out)

    def test_topics_invalid_kind_rejected(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["topics", "--kind", "khong-co"])


class TestWriteScriptCli(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            args = cli.build_parser().parse_args(argv)
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_write_script_skeleton_saves_and_loads(self):
        tmp = Path(tempfile.mkdtemp())
        dest = tmp / "gau.yaml"
        rc, out = self._run(["write-script", "--topic", "Gấu Bắc Cực",
                             "--kind", "documentary", "--scenes", "3",
                             "--out", str(dest)])
        self.assertEqual(rc, 0)
        self.assertTrue(dest.exists())
        self.assertIn("Đã lưu kịch bản", out)
        script = load_script(dest)
        self.assertEqual(script.title, "Gấu Bắc Cực")
        self.assertEqual(script.kind, "documentary")
        self.assertEqual(len(script.scenes), 3)
        self.assertEqual([s.id for s in script.scenes], ["s01", "s02", "s03"])
        self.assertEqual(validate(script), [])

    def test_write_script_default_out_slug(self):
        tmp = Path(tempfile.mkdtemp())
        # Run with cwd inside tmp so the default scripts/ lands there.
        import os
        old = os.getcwd()
        try:
            os.chdir(tmp)
            rc, out = self._run(["write-script", "--topic", "Gấu Bắc Cực",
                                 "--kind", "documentary", "--scenes", "2"])
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0)
        dest = tmp / "scripts" / "gau-bac-cuc.yaml"
        self.assertTrue(dest.exists())
        script = load_script(dest)
        self.assertEqual(len(script.scenes), 2)

    def test_write_script_asmr_has_no_narration(self):
        tmp = Path(tempfile.mkdtemp())
        dest = tmp / "asmr.yaml"
        rc, _ = self._run(["write-script", "--topic", "Tiệm spa mèo",
                           "--kind", "asmr", "--scenes", "2",
                           "--out", str(dest)])
        self.assertEqual(rc, 0)
        script = load_script(dest)
        self.assertEqual(script.kind, "asmr")
        for s in script.scenes:
            self.assertEqual(s.narration, "")

    def test_write_script_unknown_account_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        rc, out = self._run(["write-script", "--topic", "Test",
                             "--account", "khong-co-acc-nay",
                             "--out", str(tmp / "x.yaml")])
        self.assertEqual(rc, 1)
        self.assertIn("Không có tài khoản", out)


if __name__ == "__main__":
    unittest.main()

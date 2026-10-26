"""Smoke tests: cookie/proxy parsing, đọc kịch bản, SRT. Chạy: python -m unittest discover -s tests -v"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automuse.accounts import (  # noqa: E402
    ProxyConfig,
    parse_cookie_header,
    parse_netscape_file,
)
from automuse.audio import write_srt  # noqa: E402
from automuse.script import _from_markdown, _from_yaml  # noqa: E402


class TestCookies(unittest.TestCase):
    def test_header(self):
        cookies = parse_cookie_header("sessionid=abc; csrftoken=xyz; a=b=c")
        self.assertEqual(cookies, {"sessionid": "abc", "csrftoken": "xyz", "a": "b=c"})

    def test_header_with_prefix(self):
        cookies = parse_cookie_header("Cookie: sessionid=abc")
        self.assertEqual(cookies, {"sessionid": "abc"})

    def test_netscape(self, tmp_path=None):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write(".muse.ai\tTRUE\t/\tTRUE\t0\tsessionid\tabc123\n")
            path = f.name
        self.assertEqual(parse_netscape_file(path), {"sessionid": "abc123"})


class TestProxy(unittest.TestCase):
    def test_http_auth(self):
        p = ProxyConfig.parse("http://user:pass@proxy.example.com:8080")
        self.assertTrue(p.is_valid())
        self.assertEqual(p.scheme, "http")
        self.assertEqual(p.host, "proxy.example.com")
        self.assertEqual(p.port, 8080)
        self.assertEqual(p.to_requests()["http"], "http://user:pass@proxy.example.com:8080")
        self.assertEqual(p.masked(), "http://user:***@proxy.example.com:8080")

    def test_empty(self):
        self.assertFalse(ProxyConfig.parse("").is_valid())


class TestScript(unittest.TestCase):
    def test_yaml(self):
        script = _from_yaml("""
title: Test
kind: documentary
aspect: "16:9"
scenes:
  - title: Mở đầu
    duration: 8
    visual: "Savanna dawn"
    narration: "Bình minh lên."
""")
        self.assertEqual(len(script.scenes), 1)
        self.assertEqual(script.scenes[0].visual, "Savanna dawn")
        self.assertEqual(script.total_duration, 8)

    def test_markdown(self):
        script = _from_markdown("""
# Vua thảo nguyên
kind: documentary
aspect: 16:9

## Cảnh 1 — Bình minh (8s)
Visual: Savanna at dawn
Lời thoại: Bình minh lên trên thảo nguyên.
Giọng: narrator
""")
        self.assertEqual(script.title, "Vua thảo nguyên")
        self.assertEqual(len(script.scenes), 1)
        self.assertEqual(script.scenes[0].duration, 8)
        self.assertEqual(script.scenes[0].narration, "Bình minh lên trên thảo nguyên.")

    def test_srt(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "test.srt"
            write_srt([(0.0, 3.5, "Xin chào"), (3.5, 8.0, "Tạm biệt")], out)
            text = out.read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:03,500", text)
        self.assertIn("Xin chào", text)


if __name__ == "__main__":
    unittest.main()

"""Test created_at + thứ tự viết của bảng kịch bản.

- _save_script_yaml luôn ghi created_at, lưu đè giữ nguyên mốc cũ.
- _scripts_catalog xếp kịch bản trong scripts/ theo thời gian viết
  (viết trước đứng trước), không xếp theo tên file.
- Kịch bản cũ chưa có created_at thì lấy mtime làm mốc.
- Lưu từ khung soạn (POST /api/scripts) thiếu created_at thì chèn lại
  mốc cũ, không làm kịch bản cũ nhảy xuống cuối bảng.

Chạy: python -m unittest discover -s tests -v
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fake_script(title):
    scene = SimpleNamespace(id="s01", title="Cảnh 1", duration=10,
                            visual="...", narration="...",
                            voice="", character="", reference_image="",
                            sfx="", lipsync=False)
    return SimpleNamespace(title=title, kind="documentary", aspect="16:9",
                           characters=[], voices=[], default_voice="",
                           fps=30, music="", scenes=[scene])


class TestCreatedAt(unittest.TestCase):
    def setUp(self):
        from automuse.web import server as server_mod
        from automuse.web.server import StudioHandler

        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._mod = server_mod
        self._orig_root = server_mod.REPO_ROOT
        self._orig_scripts = server_mod.SCRIPTS_DIR
        server_mod.REPO_ROOT = self.base
        server_mod.SCRIPTS_DIR = self.base / "scripts"
        server_mod.SCRIPTS_DIR.mkdir()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StudioHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._mod.REPO_ROOT = self._orig_root
        self._mod.SCRIPTS_DIR = self._orig_scripts
        self.tmp.cleanup()

    def _post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def _get_scripts(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/scripts")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return [s for s in data if s["source"] == "scripts"]

    def test_save_writes_and_preserves_created_at(self):
        from automuse.web.server import _save_script_yaml
        import yaml

        dest = _save_script_yaml(_fake_script("Test"), "scripts/test.yaml")
        first = yaml.safe_load(dest.read_text(encoding="utf-8"))["created_at"]
        self.assertTrue(first)
        _save_script_yaml(_fake_script("Test Đổi Tiêu Đề"), "scripts/test.yaml")
        second = yaml.safe_load(dest.read_text(encoding="utf-8"))["created_at"]
        self.assertEqual(first, second)

    def test_catalog_sorts_by_created_at_not_name(self):
        from automuse.web.server import _scripts_catalog

        # Viết sau nhưng tên đứng trước theo alphabet.
        (self._mod.SCRIPTS_DIR / "alpha.yaml").write_text(
            "title: Alpha\ncreated_at: '2026-01-02T00:00:00+00:00'\n"
            "kind: documentary\nscenes: []\n", encoding="utf-8")
        # Viết trước nhưng tên đứng sau theo alphabet.
        (self._mod.SCRIPTS_DIR / "zebra.yaml").write_text(
            "title: Zebra\ncreated_at: '2026-01-01T00:00:00+00:00'\n"
            "kind: documentary\nscenes: []\n", encoding="utf-8")
        names = [s["name"] for s in _scripts_catalog()
                 if s["source"] == "scripts"]
        self.assertEqual(names, ["zebra", "alpha"])

    def test_catalog_falls_back_to_mtime(self):
        from automuse.web.server import _scripts_catalog

        old = self._mod.SCRIPTS_DIR / "cu.yaml"
        old.write_text("title: Cu\nkind: documentary\nscenes: []\n",
                       encoding="utf-8")
        os.utime(old, (1_000_000_000, 1_000_000_000))
        new = self._mod.SCRIPTS_DIR / "moi.yaml"
        new.write_text("title: Moi\ncreated_at: '2026-01-02T00:00:00+00:00'\n"
                       "kind: documentary\nscenes: []\n", encoding="utf-8")
        names = [s["name"] for s in _scripts_catalog()
                 if s["source"] == "scripts"]
        self.assertEqual(names, ["cu", "moi"])

    def test_editor_save_keeps_old_created_at(self):
        import yaml
        from automuse.web.server import _parse_created_at

        old_stamp = "2020-05-05T05:05:05+00:00"
        f = self._mod.SCRIPTS_DIR / "giu.yaml"
        f.write_text(f"title: Giu\ncreated_at: '{old_stamp}'\n"
                     "kind: documentary\nscenes: []\n", encoding="utf-8")
        status, data = self._post("/api/scripts", {
            "name": "giu",
            "content": "title: Giu\nkind: documentary\nscenes: []\n",
        })
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        saved = yaml.safe_load(f.read_text(encoding="utf-8"))
        self.assertEqual(_parse_created_at(saved["created_at"]),
                         _parse_created_at(old_stamp))
        via_api = self._get_scripts()
        self.assertEqual(len(via_api), 1)
        self.assertEqual(_parse_created_at(via_api[0]["created_at"]),
                         _parse_created_at(old_stamp))


if __name__ == "__main__":
    unittest.main()

"""Test endpoint /api/outputs phân trang + tìm kiếm, cùng _send_file
stream từng chunk với Range.

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestOutputsApi(unittest.TestCase):
    N = 25

    def setUp(self):
        from automuse.web import server as server_mod
        from automuse.web.server import StudioHandler
        import automuse.config as config_mod

        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._server_mod = server_mod
        self._orig_output_dir = server_mod.DEFAULT_OUTPUT_DIR
        server_mod.DEFAULT_OUTPUT_DIR = self.base
        self._config_mod = config_mod
        self._orig_settings = config_mod.SETTINGS_FILE
        config_mod.SETTINGS_FILE = self.base / "settings.yaml"
        self.data = bytes(range(256)) * 40  # 10240 byte, nội dung xác định
        base_t = 1_700_000_000
        for i in range(self.N):
            proj = self.base / f"proj-{i:02d}"
            proj.mkdir()
            (proj / "final.mp4").write_bytes(self.data)
            title = "Sư tử thảo nguyên" if i == 7 else f"Phim số {i:02d}"
            manifest = {
                "title": title, "kind": "movie",
                "scenes": [{"id": "s01"}], "scene_durations": {"s01": 5},
            }
            (proj / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (proj / "poster.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)
            # mtime tăng dần theo i -> mới nhất là proj-24
            os.utime(proj / "final.mp4", (base_t + i, base_t + i))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StudioHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._server_mod.DEFAULT_OUTPUT_DIR = self._orig_output_dir
        self._config_mod.SETTINGS_FILE = self._orig_settings
        self.tmp.cleanup()

    def _get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, hdrs, body

    def _post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_voice_settings_endpoint(self):
        status, data = self._post("/api/voices/settings",
                                  {"speed": 1.2, "stability": 0.7})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        import yaml
        saved = yaml.safe_load((self.base / "settings.yaml").read_text(encoding="utf-8"))
        self.assertEqual(saved["elevenlabs"]["speed"], 1.2)
        self.assertEqual(saved["elevenlabs"]["stability"], 0.7)

    def test_voice_settings_invalid_range(self):
        status, _data = self._post("/api/voices/settings", {"speed": 5})
        self.assertEqual(status, 400)

    def test_status_has_voice_settings(self):
        status, _, body = self._get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("elevenlabs_settings", data)
        self.assertEqual(data["elevenlabs_settings"]["speed"], 1.0)

    def test_pagination(self):
        status, _, body = self._get("/api/outputs?page=1&per_page=10")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total"], self.N)
        self.assertEqual(data["pages"], 3)
        self.assertEqual(len(data["items"]), 10)
        # Mới nhất trước: proj-24 đầu trang 1
        self.assertEqual(data["items"][0]["project"], "proj-24")
        self.assertTrue(data["items"][0]["poster"].endswith("/poster.jpg"))

        _, _, body2 = self._get("/api/outputs?page=2&per_page=10")
        page2 = json.loads(body2)
        self.assertEqual(len(page2["items"]), 10)
        names1 = {it["project"] for it in data["items"]}
        names2 = {it["project"] for it in page2["items"]}
        self.assertFalse(names1 & names2, "2 trang không được trùng project")

        _, _, body3 = self._get("/api/outputs?page=3&per_page=10")
        self.assertEqual(len(json.loads(body3)["items"]), 5)

    def test_page_clamped(self):
        _, _, body = self._get("/api/outputs?page=99&per_page=10")
        data = json.loads(body)
        self.assertEqual(data["page"], 3)
        self.assertEqual(len(data["items"]), 5)

    def test_search(self):
        _, _, body = self._get("/api/outputs?q=s%C6%B0%20t%E1%BB%AD")
        data = json.loads(body)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["project"], "proj-07")

        _, _, body = self._get("/api/outputs?q=phim%20s%E1%BB%91")
        self.assertEqual(json.loads(body)["total"], self.N - 1)

        _, _, body = self._get("/api/outputs?q=khongco")
        self.assertEqual(json.loads(body)["total"], 0)

    def test_range_streaming(self):
        status, hdrs, body = self._get(
            "/files/proj-00/final.mp4", {"Range": "bytes=100-199"})
        self.assertEqual(status, 206)
        self.assertEqual(hdrs["content-range"], f"bytes 100-199/{len(self.data)}")
        self.assertEqual(len(body), 100)
        self.assertEqual(body, self.data[100:200])

    def test_full_file(self):
        status, hdrs, body = self._get("/files/proj-00/final.mp4")
        self.assertEqual(status, 200)
        self.assertEqual(int(hdrs["content-length"]), len(self.data))
        self.assertEqual(body, self.data)

    def test_bad_range_falls_back_to_full(self):
        status, _, body = self._get(
            "/files/proj-00/final.mp4", {"Range": "bytes=abc"})
        self.assertEqual(status, 200)
        self.assertEqual(body, self.data)

    def test_missing_file_404(self):
        status, _, _ = self._get("/files/proj-00/khongco.mp4")
        self.assertEqual(status, 404)

    def test_status_has_lipsync_configured(self):
        status, _, body = self._get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("lipsync_configured", data)
        self.assertIsInstance(data["lipsync_configured"], bool)

    def test_lipsync_key_endpoint(self):
        from unittest import mock as _mock
        from automuse.web import server as _server_mod

        class FakeSyncClient:
            def __init__(self, key):
                self.key = key

            def check_key(self):
                return self.key == "sk-sync-good"

        saved = {}

        def fake_save(key, value):
            saved[key] = value

        with _mock.patch.object(_server_mod, "SyncLipSyncClient", FakeSyncClient), \
             _mock.patch.object(_server_mod, "save_env_key", fake_save):
            status, data = self._post("/api/lipsync/key", {"key": "sk-sync-good"})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(saved.get("SYNC_API_KEY"), "sk-sync-good")

            status, data = self._post("/api/lipsync/key", {"key": "sk-sync-bad"})
            self.assertEqual(status, 400)
            self.assertIn("error", data)

            status, _data = self._post("/api/lipsync/key", {"key": ""})
            self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

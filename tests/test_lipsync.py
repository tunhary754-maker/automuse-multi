"""Test client Sync Labs (lip-sync hậu kỳ) + mock client + factory.

Mọi gọi mạng đều qua session giả — không cần key thật, không gọi
api.sync.so.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automuse.config import DEFAULT_SETTINGS, load_settings  # noqa: E402
from automuse.lipsync import (  # noqa: E402
    MockSyncLipSyncClient,
    SyncError,
    SyncLipSyncClient,
    build_lipsync,
)


class _FakeResp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content
        if payload is not None:
            self.text = json.dumps(payload)
        elif isinstance(content, bytes):
            self.text = content.decode("utf-8", "replace")
        else:
            self.text = str(content)

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1024 * 1024):
        yield self.content


class _FakeSession:
    """Giả lập api.sync.so: presign -> PUT -> đăng ký -> generate -> poll."""

    def __init__(self, key_ok=True, poll_states=None, presign_status=201):
        self.headers = {}
        self.key_ok = key_ok
        self.poll_states = list(poll_states or ["QUEUED", "COMPLETED"])
        self.presign_status = presign_status
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        if url.endswith("/models"):
            return _FakeResp(200 if self.key_ok else 401,
                             {"models": ["lipsync-2"]} if self.key_ok else
                             {"error": "unauthorized"})
        if "/generate/" in url:
            state = self.poll_states.pop(0) if self.poll_states else "COMPLETED"
            payload = {"id": "job-1", "status": state}
            if state == "COMPLETED":
                payload["outputUrl"] = "https://cdn.example/out.mp4"
            if state == "FAILED":
                payload["error"] = "bad video"
            return _FakeResp(200, payload)
        return _FakeResp(404, {"error": "not found"})

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/assets/upload"):
            if self.presign_status != 201:
                return _FakeResp(self.presign_status, {"error": "nope"})
            return _FakeResp(201, {
                "uploadUrl": "https://upload.example/x",
                "url": "https://cdn.example/asset",
                "expiresIn": 3600,
            })
        if url.endswith("/assets"):
            return _FakeResp(201, {"id": "asset-123"})
        if url.endswith("/generate"):
            return _FakeResp(200, {"id": "job-1", "status": "QUEUED"})
        return _FakeResp(404, {"error": "not found"})


def _make_files(tmp: Path):
    video = tmp / "s02.mp4"
    audio = tmp / "s02.mp3"
    video.write_bytes(b"fake-video-bytes")
    audio.write_bytes(b"fake-audio-bytes")
    return video, audio


class TestSyncHappyPath(unittest.TestCase):
    def test_full_flow_presign_put_register_generate_poll_download(self):
        tmp = Path(tempfile.mkdtemp())
        video, audio = _make_files(tmp)
        session = _FakeSession()
        puts = []

        def fake_put(url, data=None, headers=None, timeout=None):
            puts.append((url, data, headers))
            return _FakeResp(200)

        out = tmp / "out.mp4"
        client = SyncLipSyncClient("sk-test", session=session,
                                   poll_interval=0.01, poll_timeout=10)
        with mock.patch("automuse.lipsync.requests.put", fake_put), \
             mock.patch("automuse.lipsync.requests.get",
                        return_value=_FakeResp(200, content=b"synced-video")):
            dest = client.sync(video, audio, out)
        self.assertEqual(dest, out)
        self.assertEqual(out.read_bytes(), b"synced-video")
        # PUT upload không được gửi x-api-key (presigned URL công khai),
        # và Content-Type phải đúng từng file (mp4 -> video/mp4, mp3 -> audio/mpeg).
        self.assertEqual(len(puts), 2)
        for _url, data, headers in puts:
            self.assertNotIn("x-api-key", headers)
            if data == b"fake-video-bytes":
                self.assertEqual(headers["Content-Type"], "video/mp4")
            elif data == b"fake-audio-bytes":
                self.assertEqual(headers["Content-Type"], "audio/mpeg")
            else:
                self.fail(f"PUT dữ liệu lạ: {data!r}")
        # Generate gửi đúng model + sync_mode + 2 asset input.
        gen_calls = [c for c in session.calls
                     if c[0] == "POST" and c[1].endswith("/generate")]
        self.assertEqual(len(gen_calls), 1)
        payload = gen_calls[0][2]
        self.assertEqual(payload["model"], "lipsync-2")
        self.assertEqual(payload["options"], {"sync_mode": "cut_off"})
        self.assertEqual(
            [i["type"] for i in payload["input"]], ["video", "audio"])

    def test_check_key_ok_and_bad(self):
        self.assertTrue(SyncLipSyncClient("sk-good",
                                          session=_FakeSession(key_ok=True)).check_key())
        self.assertFalse(SyncLipSyncClient("sk-bad",
                                           session=_FakeSession(key_ok=False)).check_key())

    def test_check_key_server_error_raises(self):
        session = _FakeSession()
        session.get = lambda url, timeout=None: _FakeResp(500, {"error": "boom"})
        with self.assertRaises(SyncError) as ctx:
            SyncLipSyncClient("sk-x", session=session).check_key()
        self.assertIn("500", str(ctx.exception))

    def test_failed_status_raises(self):
        tmp = Path(tempfile.mkdtemp())
        video, audio = _make_files(tmp)
        session = _FakeSession(poll_states=["FAILED"])
        client = SyncLipSyncClient("sk-test", session=session,
                                   poll_interval=0.01, poll_timeout=10)
        with mock.patch("automuse.lipsync.requests.put",
                        return_value=_FakeResp(200)), \
             mock.patch("automuse.lipsync.requests.get",
                        return_value=_FakeResp(200, content=b"x")):
            with self.assertRaises(SyncError) as ctx:
                client.sync(video, audio, tmp / "out.mp4")
        self.assertIn("FAILED", str(ctx.exception))

    def test_poll_timeout_raises(self):
        tmp = Path(tempfile.mkdtemp())
        video, audio = _make_files(tmp)
        session = _FakeSession(poll_states=["QUEUED"] * 50)
        client = SyncLipSyncClient("sk-test", session=session,
                                   poll_interval=0.01, poll_timeout=0.05)
        with mock.patch("automuse.lipsync.requests.put",
                        return_value=_FakeResp(200)):
            with self.assertRaises(SyncError) as ctx:
                client.sync(video, audio, tmp / "out.mp4")
        self.assertIn("Hết thời gian chờ", str(ctx.exception))

    def test_presign_error_raises(self):
        tmp = Path(tempfile.mkdtemp())
        video, audio = _make_files(tmp)
        session = _FakeSession(presign_status=400)
        client = SyncLipSyncClient("sk-test", session=session,
                                   poll_interval=0.01, poll_timeout=10)
        with self.assertRaises(SyncError) as ctx:
            client.sync(video, audio, tmp / "out.mp4")
        self.assertIn("400", str(ctx.exception))

    def test_invalid_model_rejected(self):
        with self.assertRaises(SyncError):
            SyncLipSyncClient("sk-test", model="khong-co",
                              session=_FakeSession())

    def test_missing_key_rejected(self):
        with self.assertRaises(SyncError):
            SyncLipSyncClient("", session=_FakeSession())


class TestMockClient(unittest.TestCase):
    def test_mock_copies_input_video(self):
        tmp = Path(tempfile.mkdtemp())
        video, audio = _make_files(tmp)
        out = tmp / "out.mp4"
        dest = MockSyncLipSyncClient().sync(video, audio, out)
        self.assertEqual(dest, out)
        self.assertEqual(out.read_bytes(), b"fake-video-bytes")
        self.assertTrue(MockSyncLipSyncClient().check_key())


class TestBuildLipSync(unittest.TestCase):
    def test_mock_flag_uses_mock_client(self):
        self.assertIsInstance(
            build_lipsync("sk-x", load_settings(), mock=True),
            MockSyncLipSyncClient)

    def test_no_key_uses_mock_client(self):
        self.assertIsInstance(
            build_lipsync("", load_settings(), mock=False),
            MockSyncLipSyncClient)

    def test_real_client_uses_settings_model(self):
        settings = load_settings()
        settings["lipsync"]["model"] = "sync-3"
        client = build_lipsync("sk-x", settings, mock=False)
        self.assertIsInstance(client, SyncLipSyncClient)
        self.assertEqual(client.model, "sync-3")


class TestLipSyncSettings(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULT_SETTINGS["lipsync"]["model"], "lipsync-2")
        self.assertEqual(DEFAULT_SETTINGS["lipsync"]["sync_mode"], "cut_off")
        self.assertEqual(DEFAULT_SETTINGS["lipsync"]["poll_timeout"], 600)
        self.assertEqual(load_settings()["lipsync"]["model"], "lipsync-2")


class TestGetSyncKey(unittest.TestCase):
    def test_env_var_first(self):
        from automuse.config import get_sync_key
        with mock.patch.dict("os.environ", {"SYNC_API_KEY": "sk-env"}):
            self.assertEqual(get_sync_key(), "sk-env")

    def test_dotenv_file(self):
        from automuse.config import get_sync_key
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".env").write_text('SYNC_API_KEY="sk-file"\n', encoding="utf-8")
        with mock.patch("automuse.config.REPO_ROOT", tmp), \
             mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("SYNC_API_KEY", None)
            self.assertEqual(get_sync_key(), "sk-file")

    def test_no_key_returns_empty(self):
        from automuse.config import get_sync_key
        tmp = Path(tempfile.mkdtemp())
        with mock.patch("automuse.config.REPO_ROOT", tmp), \
             mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("SYNC_API_KEY", None)
            self.assertEqual(get_sync_key(), "")


class TestSceneLipSyncFlag(unittest.TestCase):
    def test_yaml_flag(self):
        from automuse.script import load_script
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "s.yaml"
        p.write_text(
            "title: T\nkind: movie\nscenes:\n"
            "  - id: s01\n    title: A\n    duration: 5\n    visual: x\n"
            "    narration: hi\n    lipsync: true\n"
            "  - id: s02\n    title: B\n    duration: 5\n    visual: y\n"
            "    narration: yo\n",
            encoding="utf-8")
        script = load_script(p)
        self.assertTrue(script.scenes[0].lipsync)
        self.assertFalse(script.scenes[1].lipsync)

    def test_markdown_alias(self):
        from automuse.script import load_script
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "s.md"
        p.write_text(
            "# T\nkind: movie\n\n## Cảnh 1 — Nói (5s)\n"
            "Visual: close-up talking\nLời thoại: chào bạn\nKhớp môi: true\n\n"
            "## Cảnh 2 — Nghe (5s)\nVisual: wide shot\nLời thoại: ừ\n"
            "Khớp môi: false\n",
            encoding="utf-8")
        script = load_script(p)
        self.assertTrue(script.scenes[0].lipsync)
        self.assertFalse(script.scenes[1].lipsync)


if __name__ == "__main__":
    unittest.main()

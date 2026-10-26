"""Tests: ElevenLabs — resolve giọng theo voice_id, tốc độ đọc, cache key.

Chạy: python -m unittest discover -s tests -v
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automuse.audio import probe_duration  # noqa: E402
from automuse.elevenlabs import (  # noqa: E402
    ElevenLabsClient,
    ElevenLabsError,
    MockElevenLabs,
    build_elevenlabs,
)

CUSTOM_ID = "21m00Tcm4TlvDq8ikWAM"


class _FakeResp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload or {}
        self.content = content
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeSession:
    """Giả session requests: list thiếu ID lạ, get_voice có ID đó."""

    def __init__(self, audio_bytes=b""):
        self.headers = {}
        self.audio_bytes = audio_bytes
        self.posts = 0
        self.last_payload = None

    def get(self, url, timeout=None):
        if url.endswith("/voices"):
            return _FakeResp(200, {"voices": [
                {"voice_id": "id-rachel", "name": "Rachel"},
            ]})
        if "/voices/" in url:
            voice_id = url.rsplit("/", 1)[-1]
            if voice_id == CUSTOM_ID:
                return _FakeResp(200, {"voice_id": voice_id, "name": "Custom Voice"})
            return _FakeResp(404, {"detail": {"message": "not found"}})
        raise AssertionError(url)

    def post(self, url, json=None, timeout=None):
        self.posts += 1
        self.last_payload = json
        return _FakeResp(200, content=self.audio_bytes)


def _make_client(tmp, speed=1.0, audio_bytes=b""):
    client = ElevenLabsClient("sk-test", cache_dir=tmp, speed=speed)
    client.session = _FakeSession(audio_bytes)
    return client


class TestResolveVoice(unittest.TestCase):
    def test_by_name(self):
        with tempfile.TemporaryDirectory() as d:
            client = _make_client(d)
            self.assertEqual(client.resolve_voice("Rachel"), "id-rachel")

    def test_raw_id_falls_back_to_get_voice(self):
        """ID không có trong list acc vẫn dùng được (giọng share/clone)."""
        with tempfile.TemporaryDirectory() as d:
            client = _make_client(d)
            self.assertEqual(client.resolve_voice(CUSTOM_ID), CUSTOM_ID)

    def test_unknown_voice_raises(self):
        with tempfile.TemporaryDirectory() as d:
            client = _make_client(d)
            with self.assertRaises(ElevenLabsError) as cm:
                client.resolve_voice("KhongCo")
            self.assertIn("Không thấy giọng", str(cm.exception))

    def test_get_voice_404(self):
        with tempfile.TemporaryDirectory() as d:
            client = _make_client(d)
            with self.assertRaises(ElevenLabsError):
                client.get_voice("nope")


class TestSpeed(unittest.TestCase):
    def test_cache_key_changes_with_speed(self):
        with tempfile.TemporaryDirectory() as d:
            a = _make_client(d, speed=1.0)
            b = _make_client(d, speed=1.2)
            self.assertNotEqual(a._cache_key("hello", "v1"), b._cache_key("hello", "v1"))

    def test_invalid_speed_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ElevenLabsError):
                ElevenLabsClient("sk-test", cache_dir=d, speed=3.0)

    def test_tts_speed_applies_atempo(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.mp3"
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "sine=frequency=440:duration=2",
                "-c:a", "libmp3lame", str(src),
            ], check=True, capture_output=True)
            audio = src.read_bytes()
            client = _make_client(d, speed=1.5, audio_bytes=audio)
            out = client.tts("hello world", "id-rachel")
            self.assertTrue(out.exists())
            # 2s đọc nhanh 1.5x còn ~1.33s
            self.assertAlmostEqual(probe_duration(out), 2.0 / 1.5, delta=0.15)
            payload = client.session.last_payload
            self.assertIn("style", payload["voice_settings"])
            self.assertIn("use_speaker_boost", payload["voice_settings"])

    def test_tts_speed_1_uses_cache(self):
        with tempfile.TemporaryDirectory() as d:
            client = _make_client(d, speed=1.0, audio_bytes=b"fake-mp3-bytes")
            client.tts("hello", "id-rachel")
            client.tts("hello", "id-rachel")
            self.assertEqual(client.session.posts, 1)

    def test_mock_speed_shortens_duration(self):
        with tempfile.TemporaryDirectory() as d:
            mock = MockElevenLabs(cache_dir=d, speed=2.0)
            out = mock.tts("x" * 24, "mock-an")
            # ước lượng 24/12 = 2.0s, nhanh 2x còn ~1.0s
            self.assertAlmostEqual(probe_duration(out), 1.0, delta=0.2)


class TestBuildElevenlabs(unittest.TestCase):
    def test_reads_new_settings(self):
        with tempfile.TemporaryDirectory() as d:
            client = build_elevenlabs("sk-x", {"elevenlabs": {
                "speed": 1.3, "style": 0.4, "model": "eleven_v3",
            }}, cache_dir=d)
            self.assertEqual(client.speed, 1.3)
            self.assertEqual(client.style, 0.4)
            self.assertEqual(client.model, "eleven_v3")

    def test_mock_reads_speed(self):
        with tempfile.TemporaryDirectory() as d:
            mock = build_elevenlabs("", {"elevenlabs": {"speed": 1.5}}, cache_dir=d)
            self.assertIsInstance(mock, MockElevenLabs)
            self.assertEqual(mock.speed, 1.5)


if __name__ == "__main__":
    unittest.main()

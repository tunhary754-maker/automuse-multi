"""Test lệnh lipsync — set-key / sync / build --lipsync (fail-fast).

Key Sync Labs được kiểm tra qua API rồi mới lưu, giống voices set-key.
Không gọi mạng thật: client Sync Labs được giả lập.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli  # noqa: E402
from automuse.lipsync import SyncError  # noqa: E402


class FakeSyncClient:
    def __init__(self, api_key, **_kwargs):
        self.api_key = api_key

    def check_key(self):
        if self.api_key == "sk-sync-good":
            return True
        if self.api_key == "sk-sync-boom":
            raise SyncError("Kiểm tra key Sync Labs lỗi HTTP 500: boom")
        return False


class TestLipSyncSetKey(unittest.TestCase):
    def _run(self, key):
        with mock.patch("cli.getpass.getpass", return_value=key), \
             mock.patch("cli.SyncLipSyncClient", FakeSyncClient), \
             mock.patch("automuse.config.ENV_FILE",
                        Path(tempfile.mkdtemp()) / ".env") as env_file:
            args = cli.build_parser().parse_args(["lipsync", "set-key"])
            rc = args.func(args)
            return rc, env_file

    def test_good_key_verified_then_saved(self):
        rc, env_file = self._run("sk-sync-good")
        self.assertEqual(rc, 0)
        self.assertIn("SYNC_API_KEY=sk-sync-good",
                      env_file.read_text(encoding="utf-8"))

    def test_bad_key_not_saved(self):
        rc, env_file = self._run("sk-sync-bad")
        self.assertEqual(rc, 1)
        self.assertFalse(env_file.exists())

    def test_check_error_not_saved(self):
        rc, env_file = self._run("sk-sync-boom")
        self.assertEqual(rc, 1)
        self.assertFalse(env_file.exists())


class TestLipSyncSync(unittest.TestCase):
    def test_mock_sync_writes_output(self):
        tmp = Path(tempfile.mkdtemp())
        video = tmp / "clip.mp4"
        audio = tmp / "line.mp3"
        video.write_bytes(b"fake-video")
        audio.write_bytes(b"fake-audio")
        out = tmp / "clip-lipsync.mp4"
        args = cli.build_parser().parse_args([
            "lipsync", "sync", "--video", str(video), "--audio", str(audio),
            "--out", str(out), "--mock",
        ])
        rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes(), b"fake-video")

    def test_no_key_without_mock_fails_with_hint(self):
        tmp = Path(tempfile.mkdtemp())
        video = tmp / "clip.mp4"
        audio = tmp / "line.mp3"
        video.write_bytes(b"fake-video")
        audio.write_bytes(b"fake-audio")
        args = cli.build_parser().parse_args([
            "lipsync", "sync", "--video", str(video), "--audio", str(audio),
            "--out", str(tmp / "out.mp4"),
        ])
        buf = io.StringIO()
        with mock.patch("cli.get_sync_key", return_value=""), \
             redirect_stdout(buf):
            rc = args.func(args)
        self.assertEqual(rc, 1)
        self.assertIn("lipsync set-key", buf.getvalue())

    def test_invalid_model_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        video = tmp / "clip.mp4"
        audio = tmp / "line.mp3"
        video.write_bytes(b"x")
        audio.write_bytes(b"x")
        args = cli.build_parser().parse_args([
            "lipsync", "sync", "--video", str(video), "--audio", str(audio),
            "--model", "khong-co", "--mock",
        ])
        rc = args.func(args)
        self.assertEqual(rc, 1)

    def test_missing_files_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        args = cli.build_parser().parse_args([
            "lipsync", "sync", "--video", str(tmp / "no.mp4"),
            "--audio", str(tmp / "no.mp3"), "--mock",
        ])
        rc = args.func(args)
        self.assertEqual(rc, 1)


class TestBuildLipSyncFailFast(unittest.TestCase):
    def test_build_lipsync_without_key_fails_fast(self):
        """Bật --lipsync mà chưa có key (không mock) -> dừng ngay với
        hướng dẫn chạy `lipsync set-key`, chưa sinh clip nào."""
        tmp = Path(tempfile.mkdtemp())
        script = tmp / "phim.yaml"
        script.write_text(
            "title: T\nkind: movie\nscenes:\n"
            "  - id: s01\n    title: A\n    duration: 5\n    visual: x\n"
            "    narration: hi\n",
            encoding="utf-8")
        args = cli.build_parser().parse_args([
            "build", "--script", str(script), "--lipsync",
        ])
        fake_mgr = mock.Mock()
        fake_mgr.enabled_accounts.return_value = [mock.Mock(name="acc1")]
        buf = io.StringIO()
        with mock.patch("cli._manager", return_value=fake_mgr), \
             mock.patch("cli.get_sync_key", return_value=""), \
             redirect_stdout(buf):
            rc = args.func(args)
        self.assertEqual(rc, 1)
        self.assertIn("lipsync set-key", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

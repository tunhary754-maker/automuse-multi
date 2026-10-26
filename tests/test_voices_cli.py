"""Tests: lệnh voices — set-key / set-default / test / list.

Key ElevenLabs trước đây chỉ nhập được bằng cách sửa tay file .env;
các lệnh mới phải kiểm tra key trước khi lưu và cho nghe thử giọng.
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
from automuse.elevenlabs import ElevenLabsError  # noqa: E402

FAKE_VOICES = [
    {"voice_id": "id-rachel", "name": "Rachel"},
    {"voice_id": "id-an", "name": "An"},
]


class FakeElevenLabsClient:
    def __init__(self, api_key):
        self.api_key = api_key

    def list_voices(self):
        if self.api_key != "sk-good":
            raise ElevenLabsError("API key ElevenLabs không đúng (401)")
        return FAKE_VOICES

    def get_voice(self, voice_id):
        if voice_id == "21m00Tcm4TlvDq8ikWAM":
            return {"voice_id": voice_id, "name": "Custom Voice"}
        raise ElevenLabsError(f"Không thấy giọng có voice_id '{voice_id}'")

    def tts(self, text, voice_id, out_path=None):
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-audio")
        return dest


class TestVoicesSetKey(unittest.TestCase):
    def _run(self, key):
        with mock.patch("cli.getpass.getpass", return_value=key), \
             mock.patch("cli.ElevenLabsClient", FakeElevenLabsClient), \
             mock.patch("automuse.config.ENV_FILE",
                        Path(tempfile.mkdtemp()) / ".env") as env_file:
            args = cli.build_parser().parse_args(["voices", "set-key"])
            rc = args.func(args)
            return rc, env_file

    def test_good_key_verified_then_saved(self):
        rc, env_file = self._run("sk-good")
        self.assertEqual(rc, 0)
        self.assertIn("ELEVENLABS_API_KEY=sk-good",
                      env_file.read_text(encoding="utf-8"))

    def test_bad_key_not_saved(self):
        rc, env_file = self._run("sk-bad")
        self.assertEqual(rc, 1)
        self.assertFalse(env_file.exists())


class TestVoicesSetDefault(unittest.TestCase):
    def test_saves_voice_name_to_settings(self):
        tmp = Path(tempfile.mkdtemp())
        settings_file = tmp / "settings.yaml"
        with mock.patch("cli.elevenlabs_api_key", return_value="sk-good"), \
             mock.patch("cli.build_elevenlabs",
                        return_value=FakeElevenLabsClient("sk-good")), \
             mock.patch("automuse.config.SETTINGS_FILE", settings_file):
            args = cli.build_parser().parse_args(["voices", "set-default", "Rachel"])
            rc = args.func(args)
        self.assertEqual(rc, 0)
        import yaml
        saved = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["elevenlabs"]["default_voice"], "Rachel")

    def test_unknown_voice_rejected(self):
        with mock.patch("cli.elevenlabs_api_key", return_value="sk-good"), \
             mock.patch("cli.build_elevenlabs",
                        return_value=FakeElevenLabsClient("sk-good")), \
             mock.patch("automuse.config.SETTINGS_FILE",
                        Path(tempfile.mkdtemp()) / "settings.yaml"):
            args = cli.build_parser().parse_args(["voices", "set-default", "KhongCo"])
            rc = args.func(args)
        self.assertEqual(rc, 1)

    def test_raw_id_saved_when_in_get_voice(self):
        """voice_id không có trong list acc vẫn đặt được (giọng share/clone)."""
        tmp = Path(tempfile.mkdtemp())
        settings_file = tmp / "settings.yaml"
        with mock.patch("cli.elevenlabs_api_key", return_value="sk-good"), \
             mock.patch("cli.build_elevenlabs",
                        return_value=FakeElevenLabsClient("sk-good")), \
             mock.patch("automuse.config.SETTINGS_FILE", settings_file):
            args = cli.build_parser().parse_args(
                ["voices", "set-default", "21m00Tcm4TlvDq8ikWAM"])
            rc = args.func(args)
        self.assertEqual(rc, 0)
        import yaml
        saved = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["elevenlabs"]["default_voice"], "21m00Tcm4TlvDq8ikWAM")

    def test_no_key_saves_raw_with_warning(self):
        tmp = Path(tempfile.mkdtemp())
        settings_file = tmp / "settings.yaml"
        with mock.patch("cli.elevenlabs_api_key", return_value=""), \
             mock.patch("automuse.config.SETTINGS_FILE", settings_file):
            buf = io.StringIO()
            with redirect_stdout(buf):
                args = cli.build_parser().parse_args(
                    ["voices", "set-default", "21m00Tcm4TlvDq8ikWAM"])
                rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertIn("chưa kiểm chứng", buf.getvalue())


class TestVoicesSettings(unittest.TestCase):
    def _run(self, argv):
        tmp = Path(tempfile.mkdtemp())
        settings_file = tmp / "settings.yaml"
        with mock.patch("automuse.config.SETTINGS_FILE", settings_file):
            args = cli.build_parser().parse_args(argv)
            rc = args.func(args)
        saved = None
        if settings_file.exists():
            import yaml
            saved = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
        return rc, saved

    def test_set_speed_saves(self):
        rc, saved = self._run(["voices", "set-speed", "1.2"])
        self.assertEqual(rc, 0)
        self.assertEqual(saved["elevenlabs"]["speed"], 1.2)

    def test_set_speed_out_of_range_rejected(self):
        rc, saved = self._run(["voices", "set-speed", "3"])
        self.assertEqual(rc, 1)
        self.assertIsNone(saved)

    def test_set_stability_invalid_rejected(self):
        rc, saved = self._run(["voices", "set-stability", "abc"])
        self.assertEqual(rc, 1)
        self.assertIsNone(saved)

    def test_set_model_saves(self):
        rc, saved = self._run(["voices", "set-model", "eleven_v3"])
        self.assertEqual(rc, 0)
        self.assertEqual(saved["elevenlabs"]["model"], "eleven_v3")

    def test_show_prints_settings(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            args = cli.build_parser().parse_args(["voices", "show"])
            rc = args.func(args)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Tốc độ", out)
        self.assertIn("Độ ổn định", out)


class TestVoicesTest(unittest.TestCase):
    def test_writes_sample_mp3(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch("cli.elevenlabs_api_key", return_value="sk-good"), \
             mock.patch("cli.build_elevenlabs",
                        return_value=FakeElevenLabsClient("sk-good")), \
             mock.patch("cli.REPO_ROOT", tmp):
            args = cli.build_parser().parse_args(["voices", "test", "An"])
            rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertTrue((tmp / "output" / "voice-test" / "An.mp3").exists())


class TestVoicesListNoKey(unittest.TestCase):
    def test_points_to_set_key(self):
        with mock.patch("cli.elevenlabs_api_key", return_value=""):
            buf = io.StringIO()
            with redirect_stdout(buf):
                args = cli.build_parser().parse_args(["voices"])
                rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertIn("voices set-key", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

"""Tests: save_env_key + save_user_settings (nhập key ElevenLabs / đặt giọng mặc định)."""

import os
import tempfile
import unittest
from pathlib import Path

from automuse.config import save_env_key, save_user_settings


class TestSaveEnvKey(unittest.TestCase):
    def test_create_update_preserves_others(self):
        tmp = Path(tempfile.mkdtemp()) / ".env"
        save_env_key("ELEVENLABS_API_KEY", "sk-first", path=tmp)
        save_env_key("OTHER", "x", path=tmp)
        save_env_key("ELEVENLABS_API_KEY", "sk-second", path=tmp)
        text = tmp.read_text(encoding="utf-8")
        self.assertIn("ELEVENLABS_API_KEY=sk-second", text)
        self.assertIn("OTHER=x", text)
        self.assertEqual(text.count("ELEVENLABS_API_KEY="), 1)
        self.assertEqual(oct(os.stat(tmp).st_mode & 0o777), "0o600")

    def test_missing_file_creates_it(self):
        tmp = Path(tempfile.mkdtemp()) / ".env"
        self.assertFalse(tmp.exists())
        save_env_key("ELEVENLABS_API_KEY", "sk-x", path=tmp)
        self.assertEqual(tmp.read_text(encoding="utf-8").strip(),
                         "ELEVENLABS_API_KEY=sk-x")


class TestSaveUserSettings(unittest.TestCase):
    def test_merge_preserves_other_keys(self):
        tmp = Path(tempfile.mkdtemp()) / "settings.yaml"
        tmp.write_text("limits:\n  weekly_max_videos: 20\n", encoding="utf-8")
        out = save_user_settings({"elevenlabs": {"default_voice": "Rachel"}}, path=tmp)
        self.assertEqual(out["elevenlabs"]["default_voice"], "Rachel")
        self.assertEqual(out["limits"]["weekly_max_videos"], 20)
        out2 = save_user_settings({"elevenlabs": {"stability": 0.7}}, path=tmp)
        self.assertEqual(out2["elevenlabs"]["default_voice"], "Rachel")
        self.assertEqual(out2["elevenlabs"]["stability"], 0.7)

    def test_missing_file_creates_minimal(self):
        tmp = Path(tempfile.mkdtemp()) / "settings.yaml"
        out = save_user_settings({"elevenlabs": {"default_voice": "An"}}, path=tmp)
        self.assertEqual(out, {"elevenlabs": {"default_voice": "An"}})


if __name__ == "__main__":
    unittest.main()

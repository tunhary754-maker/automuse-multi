"""Tests: bảng characters + khóa mô tả nhân vật đồng nhất.

Chạy: .venv/bin/python -m unittest discover -s tests -v
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
from automuse.characters import (  # noqa: E402
    apply_character_lock,
    character_warnings,
    lock_fragment,
)
from automuse.prompting import prompts_from_script  # noqa: E402
from automuse.script import load_script  # noqa: E402

SCRIPT_YAML = """title: Test characters
kind: movie
aspect: '16:9'
fps: 24
default_voice: narrator
voices:
  narrator: "voice-narrator"
characters:
  An:
    description: "young woman, yellow raincoat"
    image: "assets/characters/an.png"
  Bình: "young man, blue shirt"
scenes:
  - id: s01
    title: "Gặp nhau"
    duration: 5
    visual: "Rainy street at night"
    narration: "Đêm mưa."
    voice: narrator
    character: An
    reference_image: "assets/characters/an.png"
    sfx: "rain, thunder"
    lipsync: true
  - id: s02
    title: "Người lạ"
    duration: 5
    visual: "A stranger walks by"
    narration: "Ai đó đi qua."
    voice: narrator
    character: Người lạ
"""

SCRIPT_MD = """# Phim test
kind: movie
Nhân vật An: young woman, yellow raincoat

## Cảnh 1 — Gặp nhau (5s)
Visual: Rainy street at night
Lời thoại: Đêm mưa.
Nhân vật: An
"""


def _write(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


class TestCharactersParse(unittest.TestCase):
    def test_yaml_characters_map(self):
        tmp = Path(tempfile.mkdtemp())
        script = load_script(_write(tmp, "s.yaml", SCRIPT_YAML))
        self.assertEqual(
            script.characters["An"]["description"], "young woman, yellow raincoat")
        self.assertEqual(
            script.characters["An"]["image"], "assets/characters/an.png")
        # Entry dạng chuỗi được chuẩn hóa thành {"description": ...}.
        self.assertEqual(
            script.characters["Bình"]["description"], "young man, blue shirt")
        self.assertEqual(script.scenes[0].reference_image,
                         "assets/characters/an.png")
        self.assertEqual(script.scenes[0].sfx, "rain, thunder")
        self.assertTrue(script.scenes[0].lipsync)

    def test_markdown_characters(self):
        tmp = Path(tempfile.mkdtemp())
        script = load_script(_write(tmp, "s.md", SCRIPT_MD))
        self.assertEqual(
            script.characters["An"]["description"], "young woman, yellow raincoat")
        self.assertEqual(script.scenes[0].character, "An")


class TestCharacterLock(unittest.TestCase):
    def test_lock_appends_description_once(self):
        tmp = Path(tempfile.mkdtemp())
        script = load_script(_write(tmp, "s.yaml", SCRIPT_YAML))
        apply_character_lock(script)
        self.assertIn(
            "Featuring An (young woman, yellow raincoat).",
            script.scenes[0].visual)
        first = script.scenes[0].visual
        apply_character_lock(script)  # gọi lần hai không ghi trùng
        self.assertEqual(script.scenes[0].visual, first)
        self.assertEqual(
            script.scenes[0].visual.count("young woman, yellow raincoat"), 1)

    def test_unknown_character_leaves_visual_unchanged(self):
        tmp = Path(tempfile.mkdtemp())
        script = load_script(_write(tmp, "s.yaml", SCRIPT_YAML))
        before = script.scenes[1].visual
        apply_character_lock(script)
        self.assertEqual(script.scenes[1].visual, before)

    def test_prompts_from_script_locks_description(self):
        tmp = Path(tempfile.mkdtemp())
        script = load_script(_write(tmp, "s.yaml", SCRIPT_YAML))
        prompts = prompts_from_script(script)
        self.assertIn(
            "Featuring An (young woman, yellow raincoat).", prompts[0].prompt)
        # Cảnh nhân vật lạ vẫn có Featuring tên, không có mô tả.
        self.assertIn("Featuring Người lạ.", prompts[1].prompt)

    def test_no_duplication_when_visual_already_has_description(self):
        tmp = Path(tempfile.mkdtemp())
        script = load_script(_write(tmp, "s.yaml", SCRIPT_YAML))
        script.scenes[0].visual = (
            "Rainy street, young woman, yellow raincoat waiting")
        prompts = prompts_from_script(script)
        self.assertEqual(
            prompts[0].prompt.count("young woman, yellow raincoat"), 1)
        self.assertNotIn("Featuring An", prompts[0].prompt)

    def test_lock_fragment_variants(self):
        self.assertEqual(
            lock_fragment({"An": {"description": "young woman"}},
                          "An"),
            "Featuring An (young woman).")
        self.assertEqual(lock_fragment({}, "An"), "Featuring An.")
        self.assertEqual(lock_fragment({}, ""), "")


class TestCharacterWarnings(unittest.TestCase):
    def test_missing_image_and_unknown_character_warn(self):
        tmp = Path(tempfile.mkdtemp())
        script = load_script(_write(tmp, "s.yaml", SCRIPT_YAML))
        warnings = character_warnings(script, tmp)
        self.assertTrue(any("Người lạ" in w for w in warnings))
        self.assertTrue(any("an.png" in w for w in warnings))

    def test_no_warnings_when_all_good(self):
        tmp = Path(tempfile.mkdtemp())
        img = tmp / "an.png"
        img.write_bytes(b"fake-png")
        script = load_script(_write(tmp, "s.yaml", SCRIPT_YAML.replace(
            "assets/characters/an.png", str(img))))
        warnings = character_warnings(script, tmp)
        self.assertFalse([w for w in warnings if "an.png" in w])


class TestCharactersCli(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            args = cli.build_parser().parse_args(argv)
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_apply_preserves_fields_and_characters(self):
        tmp = Path(tempfile.mkdtemp())
        yaml_text = SCRIPT_YAML.replace(
            "title: Test characters",
            "title: Test characters\nmusic: \"music.mp3\"")
        script = _write(tmp, "s.yaml", yaml_text)
        rc, _out = self._run(["prompts", "--script", str(script), "--apply"])
        self.assertEqual(rc, 0)
        reloaded = load_script(tmp / "s.prompts.yaml")
        self.assertEqual(
            reloaded.characters["An"]["description"],
            "young woman, yellow raincoat")
        self.assertEqual(reloaded.scenes[0].reference_image,
                         "assets/characters/an.png")
        self.assertEqual(reloaded.scenes[0].sfx, "rain, thunder")
        self.assertTrue(reloaded.scenes[0].lipsync)
        self.assertEqual(reloaded.voices, {"narrator": "voice-narrator"})
        self.assertEqual(reloaded.default_voice, "narrator")
        self.assertEqual(reloaded.fps, 24)
        self.assertEqual(reloaded.music, "music.mp3")

    def test_show_lists_characters_and_image_status(self):
        tmp = Path(tempfile.mkdtemp())
        img = tmp / "an.png"
        img.write_bytes(b"fake-png")
        yaml_text = SCRIPT_YAML.replace(
            '  Bình: "young man, blue shirt"',
            '  Bình:\n    description: "young man, blue shirt"\n'
            '    image: "assets/characters/binh.png"')
        yaml_text = yaml_text.replace("assets/characters/an.png", str(img))
        script = _write(tmp, "s.yaml", yaml_text)
        rc, out = self._run(["characters", "show", "--script", str(script)])
        self.assertEqual(rc, 0)
        self.assertIn("An", out)
        self.assertIn("young woman, yellow raincoat", out)
        self.assertIn("có file", out)
        self.assertIn("KHÔNG thấy file", out)  # ảnh của Bình chưa có
        self.assertIn("Người lạ", out)  # cảnh báo nhân vật chưa khai báo

    def test_show_no_characters(self):
        tmp = Path(tempfile.mkdtemp())
        script = _write(tmp, "s.yaml", "title: Test\nkind: movie\n"
                        "scenes:\n  - title: A\n    duration: 5\n"
                        "    visual: x\n    narration: y\n")
        rc, out = self._run(["characters", "show", "--script", str(script)])
        self.assertEqual(rc, 0)
        self.assertIn("Chưa có bảng characters", out)


class TestGatewayReferenceImageWarns(unittest.TestCase):
    def _client(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_gateway import FakeGateway, FakeWS, _settings
        import automuse.gateway as gw
        from automuse.accounts import Account

        acc = Account(name="test-acc", cookie="x")
        client = gw.GatewayClient(acc, _settings())
        fake = FakeGateway()
        ws = FakeWS(fake)
        client.fetch_init = lambda: {  # noqa: E731 - test stub
            "gatewayUrl": "wss://11111111-2222-3333-4444-555555555555.metaaivm.com/",
            "vmName": "11111111-2222-3333-4444-555555555555",
            "authToken": "s0:fake", "noiseNotaryToken": "endorsement.v1.fake"}
        with mock.patch.object(gw.websocket, "create_connection",
                               return_value=ws):
            client.connect()
        return client, fake

    def test_reference_image_attached_and_proceeds(self):
        # Trước đây raise GatewayError rồi chỉ cảnh báo; giờ ảnh được
        # đính kèm inline base64 trong chat.stream items rồi sinh tiếp.
        import base64 as _b64
        import automuse.gateway as gw
        import json as _json

        png_1x1 = _b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "Xw8AAwMCAO+ip1sAAAAASUVORK5CYII=")
        client, fake = self._client()
        orig = gw.GatewayClient._download_video

        def fake_dl(self, url, dest):
            dest.write_bytes(b"FAKEVIDEO")
            return dest

        try:
            with mock.patch.object(gw.GatewayClient, "_download_video", fake_dl):
                with tempfile.TemporaryDirectory() as d:
                    img = Path(d) / "binh.png"
                    img.write_bytes(png_1x1)
                    dest = client.generate_video(
                        "a young man smiling", aspect="16:9", duration=8,
                        reference_image=img,
                        out_path=Path(d) / "s02.mp4")
                    self.assertEqual(dest.read_bytes(), b"FAKEVIDEO")
            sent = _json.loads(fake.stream_bodies[0])
            self.assertEqual(sent["items"][0]["type"], "image")
            self.assertEqual(sent["items"][0]["filename"], "binh.png")
        finally:
            client.close()
            gw.GatewayClient._download_video = orig


class TestRunBuildCharacterLock(unittest.TestCase):
    def test_mock_build_logs_character_lock(self):
        """Mock build: lock được áp vào trước khi sinh clip (log ghi rõ)."""
        tmp = Path(tempfile.mkdtemp())
        script = _write(tmp, "s.yaml", SCRIPT_YAML)
        settings = {"generation": {"delay_between_jobs": 0,
                                   "max_retries_per_scene": 3}}
        from automuse.pipeline import run_build

        buf = io.StringIO()
        with redirect_stdout(buf):
            manifest = run_build(script, out_dir=tmp / "out",
                                 settings=settings, mock=True)
        self.assertIn("Khóa mô tả nhân vật cho 1 cảnh", buf.getvalue())
        self.assertTrue((tmp / "out" / "final.mp4").exists())


if __name__ == "__main__":
    unittest.main()

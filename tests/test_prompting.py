"""Tests: Muse tự viết prompt sinh video từ kịch bản (automuse/prompting).

Không cần mạng: đường template test trực tiếp; đường gateway dùng client
giả phát lại các frame subscribe (xây bằng chính hàm encode của gateway).

Chạy: python -m unittest tests.test_prompting -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automuse import gateway as gw  # noqa: E402
from automuse.prompting import (  # noqa: E402
    ScenePrompt,
    parse_prompt_reply,
    prompts_from_script,
    prompts_from_text,
    split_beats,
)
from automuse.script import Script, Scene  # noqa: E402

SAMPLE_VI = """Cảnh 1 — Bình minh thảo nguyên
Bình minh lên trên thảo nguyên châu Phi, bầy sư tử trở về sau chuyến săn đêm.

Cảnh 2 — Sư tử cái rình mồi
Sư tử cái di chuyển nhẹ nhàng trong cỏ cao, mắt nhìn chằm chằm con mồi.

Cảnh 3 — Đàn linh dương
Đàn linh dương gặm cỏ, tai cử động liên tục vì cảm nhận được nguy hiểm.
"""


# ---------------------------------------------------------------------------
# Client gateway giả: phát lại các frame đã xếp sẵn (cùng interface với
# GatewayClient._recv_service_frame -> (stream_id, kind, payload bytes)).
# ---------------------------------------------------------------------------

class FakeGatewayClient:
    SUB_ID = 11
    SEND_ID = 22

    def __init__(self, subscribe_lines: list[str]):
        self.subscribe_lines = subscribe_lines
        self.sent: list[tuple[str, dict]] = []
        self.timezone = "Asia/Bangkok"
        self._frames: list[tuple[int, str, bytes]] = []
        self._build_frames()

    def _build_frames(self):
        # Payload "response" phải là ApplicationResponse protobuf thật
        # (status varint + body + end_body) — decode_app_response đọc
        # field 1 làm status, đưa ApplicationRequest vào sẽ vỡ kiểu.
        def _app_response(status: int, body: bytes, end_body: bool) -> bytes:
            return (gw._enc_varint_field(1, status)
                    + gw._enc_bytes_field(3, body)
                    + gw._enc_bool_field(4, end_body))

        sub_ack = json.dumps({"type": "res", "status": "ok", "payload": {
            "subscribed": True, "session_id": "s1"}}).encode() + b"\n"
        self._frames.append((self.SUB_ID, "response",
                             _app_response(200, sub_ack, False)))
        send_ack = json.dumps({"ok": True, "result": {
            "message_id": "m1", "session_id": "s1"}}).encode() + b"\n"
        self._frames.append((self.SEND_ID, "response",
                             _app_response(200, send_ack, False)))
        for line in self.subscribe_lines:
            data = (line if line.endswith("\n") else line + "\n").encode()
            chunk = gw._enc_bytes_field(1, data) + gw._enc_bool_field(2, False)
            self._frames.append((self.SUB_ID, "body_chunk", chunk))
        end = gw._enc_bytes_field(1, b"") + gw._enc_bool_field(2, True)
        self._frames.append((self.SUB_ID, "body_chunk", end))
        self._frames.append((self.SEND_ID, "body_chunk", end))

    def connect(self):
        pass

    def _device_id(self):
        return "test-device"

    def _send_rpc(self, method, params, stream_mode):
        self.sent.append((method, params))
        return self.SUB_ID if method == "chat.subscribe" else self.SEND_ID

    def _validate_ack(self, obj, method):
        return obj

    def _recv_service_frame(self, timeout):
        if not self._frames:
            raise AssertionError("FakeGateway: hết frame mà client còn đọc")
        return self._frames.pop(0)


def _text_event(text: str) -> str:
    return json.dumps({"type": "event", "event": "delta.text_append",
                       "payload": {"text": text}})


def _done_event() -> str:
    return json.dumps({"type": "event", "event": "delta.message_done",
                       "payload": {"status": "completed"}})


# ---------------------------------------------------------------------------
# Tách beat + template fallback
# ---------------------------------------------------------------------------

class TestSplitBeats(unittest.TestCase):
    def test_numbered_scenes(self):
        beats = split_beats(SAMPLE_VI)
        self.assertEqual(len(beats), 3)
        self.assertEqual(beats[0].title, "Bình minh thảo nguyên")
        self.assertIn("sư tử", beats[0].text)

    def test_blank_line_paragraphs(self):
        beats = split_beats("Một con chuột chui ra khỏi cống.\n\n"
                            "Một con gián bay vù qua đầu nó.")
        self.assertEqual(len(beats), 2)

    def test_numbered_list(self):
        beats = split_beats("1. Sư tử đi săn\n2. Linh dương chạy trốn")
        self.assertEqual(len(beats), 2)
        self.assertIn("Sư tử", beats[0].text)

    def test_empty_text(self):
        self.assertEqual(split_beats(""), [])
        self.assertEqual(split_beats("   \n  \n"), [])


class TestTemplateFallback(unittest.TestCase):
    def test_three_beats_three_prompts(self):
        prompts = prompts_from_text(SAMPLE_VI, kind="documentary", aspect="16:9")
        self.assertEqual(len(prompts), 3)
        for p in prompts:
            self.assertIsInstance(p, ScenePrompt)

    def test_prompt_mentions_key_subject(self):
        prompts = prompts_from_text(SAMPLE_VI, kind="documentary", aspect="16:9")
        # Beat 1: sư tử / lion; beat 2: sư tử cái / lioness; beat 3: linh dương
        self.assertIn("lion", prompts[0].prompt.lower())
        self.assertIn("lioness", prompts[1].prompt.lower())
        self.assertIn("antelope", prompts[2].prompt.lower())
        # Chủ thể gốc (tiếng Việt) vẫn giữ trong prompt
        self.assertIn("sư tử", prompts[0].prompt)

    def test_prompt_has_aspect_and_style(self):
        prompts = prompts_from_text(SAMPLE_VI, kind="documentary", aspect="9:16")
        for p in prompts:
            self.assertIn("9:16", p.prompt)
            self.assertIn("documentary", p.prompt.lower())
            self.assertIn("no dialogue", p.prompt.lower())

    def test_movie_style_differs(self):
        # Phim truyện: drama live-action có thoại (không còn Larva câm).
        prompts = prompts_from_text("Một con chuột ăn vụng pho mát.",
                                    kind="movie", aspect="16:9")
        self.assertEqual(len(prompts), 1)
        self.assertIn("live-action drama", prompts[0].prompt.lower())
        self.assertIn("lip movement", prompts[0].prompt.lower())
        self.assertNotIn("slapstick", prompts[0].prompt.lower())
        self.assertNotIn("silent", prompts[0].prompt.lower())
        self.assertIn("rat", prompts[0].prompt.lower())

    def test_animation_style_is_larva_silent(self):
        # Hoạt hình: Larva slapstick câm (phong cách cũ của movie).
        prompts = prompts_from_text("Một con chuột ăn vụng pho mát.",
                                    kind="animation", aspect="16:9")
        self.assertEqual(len(prompts), 1)
        self.assertIn("slapstick", prompts[0].prompt.lower())
        self.assertIn("no dialogue", prompts[0].prompt.lower())

    def test_empty_text_returns_empty(self):
        self.assertEqual(prompts_from_text(""), [])
        self.assertEqual(prompts_from_text("  \n "), [])

    def test_single_unstructured_text_is_one_prompt(self):
        prompts = prompts_from_text("Sư tử đi săn lúc bình minh.")
        self.assertEqual(len(prompts), 1)
        self.assertIn("lion", prompts[0].prompt.lower())


# ---------------------------------------------------------------------------
# prompts_from_script
# ---------------------------------------------------------------------------

class TestPromptsFromScript(unittest.TestCase):
    def _script(self):
        return Script(
            title="Test", kind="documentary", aspect="16:9",
            scenes=[
                Scene(id="s01", title="Bình minh", duration=8,
                      visual="Cinematic aerial shot of savanna at dawn",
                      narration="Bình minh lên."),
                Scene(id="s02", title="Rình mồi", duration=8,
                      visual="", character="Sư tử cái",
                      narration="Sư tử cái rình mồi trong cỏ cao."),
            ],
        )

    def test_one_prompt_per_scene(self):
        prompts = prompts_from_script(self._script())
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0].title, "Bình minh")
        self.assertEqual(prompts[0].scene_id, "s01")
        self.assertEqual(prompts[0].duration, 8)

    def test_prompt_has_aspect_and_camera(self):
        prompts = prompts_from_script(self._script())
        for p in prompts:
            self.assertIn("16:9", p.prompt)
            self.assertIn("documentary", p.prompt.lower())

    def test_existing_visual_kept(self):
        prompts = prompts_from_script(self._script())
        self.assertIn("aerial shot of savanna at dawn", prompts[0].prompt)

    def test_empty_visual_derived_from_title_and_narration(self):
        prompts = prompts_from_script(self._script())
        self.assertIn("lioness", prompts[1].prompt.lower())
        self.assertIn("Sư tử cái", prompts[1].prompt)

    def test_no_dialogue_text_in_prompt(self):
        prompts = prompts_from_script(self._script())
        self.assertNotIn("Bình minh lên.", prompts[0].prompt)

    def test_vertical_aspect(self):
        script = self._script()
        script.aspect = "9:16"
        prompts = prompts_from_script(script)
        self.assertIn("9:16", prompts[0].prompt)


# ---------------------------------------------------------------------------
# Parse câu trả lời của Muse
# ---------------------------------------------------------------------------

class TestParsePromptReply(unittest.TestCase):
    def test_json_array(self):
        reply = ('[{"title": "Dawn", "prompt": "Savanna at dawn, lions walk."},'
                 ' {"title": "Stalk", "prompt": "Lioness stalks prey."}]')
        out = parse_prompt_reply(reply)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].title, "Dawn")
        self.assertIn("Savanna", out[0].prompt)

    def test_json_in_markdown_fence(self):
        reply = ('```json\n[{"title": "Dawn", "prompt": "Savanna at dawn."}]\n```')
        out = parse_prompt_reply(reply)
        self.assertEqual(len(out), 1)

    def test_json_mixed_with_commentary(self):
        reply = ('Here are your prompts:\n'
                 '[{"title": "A", "prompt": "First prompt."}]\nHope it helps!')
        out = parse_prompt_reply(reply)
        self.assertEqual(len(out), 1)

    def test_numbered_lines(self):
        reply = ("1. Savanna at dawn, a lion pride walks.\n"
                 "2. Lioness stalks through tall grass.")
        out = parse_prompt_reply(reply)
        self.assertEqual(len(out), 2)
        self.assertIn("Savanna", out[0].prompt)
        self.assertIn("Lioness", out[1].prompt)

    def test_scene_headings(self):
        reply = ("Scene 1: Dawn — Savanna at dawn, mist over grassland.\n"
                 "Scene 2: Stalk — Lioness stalks prey in tall grass.")
        out = parse_prompt_reply(reply)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].title, "Dawn")

    def test_blank_line_blocks(self):
        reply = ("Savanna at dawn, lions walk.\n\n"
                 "Lioness stalks through tall grass.")
        out = parse_prompt_reply(reply)
        self.assertEqual(len(out), 2)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_prompt_reply("")
        with self.assertRaises(ValueError):
            parse_prompt_reply("   ")


# ---------------------------------------------------------------------------
# Đường gateway (client giả) + fallback
# ---------------------------------------------------------------------------

class TestPromptsFromTextGateway(unittest.TestCase):
    def test_muse_json_reply_used(self):
        reply = ('[{"title": "Dawn", "prompt": "Golden savanna at dawn."},'
                 ' {"title": "Stalk", "prompt": "Lioness stalks prey."},'
                 ' {"title": "Herd", "prompt": "Antelopes graze."}]')
        client = FakeGatewayClient([_text_event(reply), _done_event()])
        prompts = prompts_from_text(SAMPLE_VI, client=client)
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[0].title, "Dawn")
        self.assertIn("Golden savanna", prompts[0].prompt)
        # Gửi đúng luồng: subscribe trước, stream sau
        methods = [m for m, _ in client.sent]
        self.assertEqual(methods, ["chat.subscribe", "chat.stream"])
        # Instruction + beats đều nằm trong tin gửi đi
        sent_text = client.sent[1][1]["items"][0]["text"]
        self.assertIn("Bình minh", sent_text)
        self.assertIn("JSON array", sent_text)

    def test_text_append_chunks_concatenated(self):
        part1 = '[{"title": "Dawn", "prompt": "Golden '
        part2 = 'savanna at dawn."}]'
        client = FakeGatewayClient([_text_event(part1), _text_event(part2),
                                    _done_event()])
        prompts = prompts_from_text("Bình minh thảo nguyên.", client=client)
        self.assertEqual(len(prompts), 1)
        self.assertIn("Golden savanna at dawn", prompts[0].prompt)

    def test_unparsable_reply_falls_back_to_template(self):
        client = FakeGatewayClient([_text_event("..."), _done_event()])
        prompts = prompts_from_text(SAMPLE_VI, client=client)
        self.assertEqual(len(prompts), 3)
        self.assertIn("lion", prompts[0].prompt.lower())

    def test_gateway_error_falls_back_to_template(self):
        class BrokenClient(FakeGatewayClient):
            def connect(self):
                raise RuntimeError("WebSocket down")

        client = BrokenClient([])
        prompts = prompts_from_text(SAMPLE_VI, client=client)
        self.assertEqual(len(prompts), 3)
        self.assertIn("lion", prompts[0].prompt.lower())

    def test_gateway_error_strict_raises(self):
        class BrokenClient(FakeGatewayClient):
            def connect(self):
                raise RuntimeError("WebSocket down")

        client = BrokenClient([])
        with self.assertRaises(RuntimeError):
            prompts_from_text(SAMPLE_VI, client=client, strict=True)


if __name__ == "__main__":
    unittest.main()

"""Tests: gateway transport (Noise WS) — protobuf framing, handshake payloads,
gateway-init parsing, and a full handshake + RPC + chat.stream round-trip
against an in-process fake gateway (real noiseprotocol responder).
Chạy: python -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from noise.connection import Keypair, NoiseConnection

import automuse.gateway as gw
from automuse.accounts import Account
from automuse.config import DEFAULT_SETTINGS


def _settings(**overrides):
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    s["muse"].update(overrides)
    return s


class TestProtobuf(unittest.TestCase):
    def test_noise_frame_roundtrip(self):
        blob = gw.encode_noise_frame(-1234567890123, 2, 3, b"hello")
        d = gw.decode_noise_frame(blob)
        self.assertEqual(d, {"chunk_id": -1234567890123, "chunk_index": 2,
                             "total_chunks": 3, "payload": b"hello"})

    def test_chunk_payload_small_and_large(self):
        frames = gw.chunk_payload(b"abc")
        self.assertEqual(len(frames), 1)
        big = os.urandom(gw.MAX_CHUNK_PAYLOAD * 2 + 10)
        frames = gw.chunk_payload(big)
        self.assertEqual(len(frames), 3)
        parts = [gw.decode_noise_frame(f) for f in frames]
        self.assertEqual(b"".join(p["payload"] for p in parts), big)
        self.assertEqual({p["chunk_index"] for p in parts}, {0, 1, 2})
        self.assertEqual(len({p["chunk_id"] for p in parts}), 1)

    def test_service_envelope_roundtrip(self):
        app = gw.encode_app_request(
            "POST", "/chat/stream",
            [("Content-Type", "application/json"), ("x-app-id", "hatch-web")],
            b'{"a":1}', True)
        frame = gw.encode_service_frame(7, 2, app)
        blob = gw.encode_service_request(gw.SERVICE_DAEMON, frame)
        service, payload = gw.decode_service_request(blob)
        self.assertEqual(service, gw.SERVICE_DAEMON)
        sid, kind, app_bytes = gw.decode_service_frame(payload)
        self.assertEqual((sid, kind), (7, "request"))
        resp = gw.decode_app_response(_encode_app_response_for_test())
        self.assertEqual(resp["status"], 200)
        self.assertEqual(resp["body"], b'{"ok":true}')
        self.assertTrue(resp["end_body"])
        self.assertEqual(resp["headers"], [("Content-Type", "application/json")])

    def test_reset_roundtrip(self):
        blob = gw.encode_reset(3, "bye")
        _service, payload = gw.decode_service_request(blob)
        sid, kind, data = gw.decode_service_frame(payload)
        self.assertEqual((sid, kind), (3, "reset"))
        r = gw.decode_reset(data)
        self.assertEqual(r["code"], 1)
        self.assertEqual(r["reason"], "bye")

    def test_client_nonce_payload(self):
        nonce = bytes(range(32))
        self.assertEqual(gw.encode_client_nonce_message(nonce),
                         b"\x0a\x20" + nonce)
        with self.assertRaises(gw.GatewayError):
            gw.encode_client_nonce_message(b"short")

    def test_rv_challenge_decode(self):
        # valid provisioned challenge
        att = b"A" * 10
        payload = (gw._enc_bytes_field(1, att) + gw._enc_varint_field(2, 2)
                   + gw._enc_bytes_field(3, b"N" * 32)
                   + gw._enc_bytes_field(4, b"P" * 32))
        d = gw.decode_rv_challenge(payload)
        self.assertEqual(d["state"], "provisioned")
        self.assertEqual(d["attestation"], att)
        # unprovisioned (no vm_proof)
        payload = (gw._enc_bytes_field(1, att) + gw._enc_varint_field(2, 1)
                   + gw._enc_bytes_field(3, b"N" * 32))
        self.assertEqual(gw.decode_rv_challenge(payload)["state"], "unprovisioned")
        # not a challenge -> None (standard VM: random bytes / empty)
        self.assertIsNone(gw.decode_rv_challenge(b""))
        self.assertIsNone(gw.decode_rv_challenge(b"\x08\x01"))
        # bad state / short nonce -> None
        bad = gw._enc_bytes_field(1, att) + gw._enc_varint_field(2, 9)
        self.assertIsNone(gw.decode_rv_challenge(bad))


def _encode_app_response_for_test() -> bytes:
    out = gw._enc_varint_field(1, 200)
    out += gw._enc_bytes_field(2, gw.encode_header("Content-Type", "application/json"))
    out += gw._enc_bytes_field(3, b'{"ok":true}')
    out += gw._enc_bool_field(4, True)
    return out


class TestBootstrap(unittest.TestCase):
    def test_parse_gateway_init_double_escaped(self):
        init = {
            "version": 1,
            "gatewayUrl": "wss://11111111-2222-3333-4444-555555555555.metaaivm.com/",
            "vmName": "11111111-2222-3333-4444-555555555555",
            "authToken": "s0:fake",
            "noiseNotaryToken": "endorsement.v1.fake",
        }
        # mimic the RSC payload: JSON inside a JSON string inside HTML
        inner = json.dumps({"x": "window.__HATCH_GATEWAY_INIT__=" + json.dumps(init)})
        html = "<html><script>self.__next_f.push([1," + json.dumps(inner) + "])</script></html>"
        parsed = gw.parse_gateway_init(html)
        self.assertEqual(parsed["gatewayUrl"], init["gatewayUrl"])
        self.assertEqual(parsed["authToken"], "s0:fake")

    def test_parse_gateway_init_missing(self):
        with self.assertRaises(gw.GatewayError):
            gw.parse_gateway_init("<html>no init here</html>")

    def test_fetch_init_missing_blob_hints_expired_cookie(self):
        """Trang chủ 200 nhưng không có blob -> lỗi phải gợi ý kiểm tra
        cookie (đã xác minh: cookie sống trả 200 + blob; cookie chết thì
        bị đẩy về auth.muse.ai và cũng không có blob)."""
        client = gw.GatewayClient(Account(name="acc test"), _settings())

        class FakeResp:
            status_code = 200
            text = "<html>trang shell khong co blob gateway</html>"

        class FakeSession:
            def get(self, url, timeout=None):
                return FakeResp()

        client._http = lambda: FakeSession()  # noqa: E731 - test stub
        with self.assertRaises(gw.GatewayError) as ctx:
            client.fetch_init()
        self.assertIn("accounts check", str(ctx.exception))

    def test_build_ws_url(self):
        url = gw.build_ws_url(
            "wss://11111111-2222-3333-4444-555555555555.metaaivm.com/",
            "s0:tok", "endo:xyz", request_id="req-1")
        self.assertTrue(url.startswith("wss://hatch.metaaivm.com/v1/noise?"))
        self.assertIn("vm_id=11111111-2222-3333-4444-555555555555", url)
        self.assertIn("auth_token=s0%3Atok", url)
        self.assertIn("notary_token=endo%3Axyz", url)
        self.assertIn("app_id=hatch-web", url)
        self.assertIn("request_id=req-1", url)
        with self.assertRaises(gw.GatewayError):
            gw.build_ws_url("wss://example.com/", "s0:tok", None)

    def test_extract_video_url(self):
        recs = [
            {"type": "event", "event": "thinking"},
            {"message": {"content": [{"type": "text", "text": "đang tạo..."}]}},
            {"message": {"content": [
                {"type": "video", "video_url": "https://cdn.example/v/abc.mp4"},
                {"type": "image", "url": "https://cdn.example/thumb.jpg"},
            ]}},
        ]
        self.assertEqual(gw._extract_video_url(recs), "https://cdn.example/v/abc.mp4")
        self.assertIsNone(gw._extract_video_url([{"a": 1}]))
        # Regression: id "http-subscribe-<uuid>" của subscribe ack bắt đầu
        # bằng "http" nhưng không phải URL — không được nhận nhầm làm link.
        ack = {"id": "http-subscribe-871ac925-e13c-4b0d-afa4-fb8a2d551bc8",
               "payload": {"subscribed": True}, "status": "ok", "type": "res"}
        self.assertIsNone(gw._extract_video_url([ack]))
        self.assertEqual(gw._extract_video_url([ack] + recs),
                         "https://cdn.example/v/abc.mp4")
        # Regression 2: ảnh avatar (fbcdn .png 200x200) trong record sớm
        # từng bị tải về làm final.mp4 và báo thành công giả — không nhận
        # ảnh hay link web thường làm link video.
        avatar = {"message": {"content": [
            {"type": "image",
             "url": "https://scontent-mia3-3.xx.fbcdn.net/v/t39.82052-6/abc_n.png?stp=dst-png_s200x200"},
        ]}}
        self.assertIsNone(gw._extract_video_url([avatar]))
        self.assertIsNone(gw._extract_video_url(
            [{"link": "https://muse.ai/"}]))
        self.assertEqual(
            gw._extract_video_url([avatar, {"url":
                "https://gw.example/api/feed/units/u1/media/h1"}]),
            "https://gw.example/api/feed/units/u1/media/h1")
        self.assertEqual(
            gw._extract_video_url([{"video_url": "https://cdn.example/v/xyz"}]),
            "https://cdn.example/v/xyz")

    def test_is_video_bytes(self):
        self.assertTrue(gw._is_video_bytes(b"\x00\x00\x00\x18ftypmp42"))
        self.assertTrue(gw._is_video_bytes(b"\x1a\x45\xdf\xa3\x01\x02"))
        self.assertFalse(gw._is_video_bytes(b"\x89PNG\r\n\x1a\n\x00\x00"))
        self.assertFalse(gw._is_video_bytes(b"<!doctype html>"))
        self.assertFalse(gw._is_video_bytes(b""))

    def test_media_url_to_fs_path(self):
        self.assertEqual(
            gw._media_url_to_fs_path(
                "https://vm-1.metaaivm.com/media/raw/workspace/imagine_media/a.mp4"),
            "workspace/imagine_media/a.mp4")
        self.assertEqual(
            gw._media_url_to_fs_path(
                "https://vm-1.metaaivm.com/fs/raw/workspace/imagine_media/a.mp4"),
            "workspace/imagine_media/a.mp4")
        self.assertEqual(
            gw._media_url_to_fs_path("sandbox://workspace/imagine_media/a.mp4"),
            "workspace/imagine_media/a.mp4")
        # query string bị cắt
        self.assertEqual(
            gw._media_url_to_fs_path(
                "https://vm-1.metaaivm.com/media/raw/workspace/a.mp4?x=1"),
            "workspace/a.mp4")
        self.assertIsNone(gw._media_url_to_fs_path("https://cdn.example/v/abc.mp4"))
        self.assertIsNone(gw._media_url_to_fs_path(
            "https://gw.example/api/feed/units/u1/media/h1"))
        self.assertIsNone(gw._media_url_to_fs_path(""))

    def test_presentation_video_url(self):
        hist = {"chat_events": [
            # event cũ hơn since_ms: bỏ qua
            {"event_name": "delta.presentation", "occurred_at_ms": 100,
             "payload": {"data": {"videos": [{"path": "sandbox://workspace/old.mp4"}]}}},
            # message_done không có videos: bỏ qua
            {"event_name": "delta.message_done", "occurred_at_ms": 300,
             "payload": {"status": "completed"}},
            # presentation mới nhất: lấy variants.original
            {"event_name": "delta.presentation", "occurred_at_ms": 200,
             "payload": {"data": {"videos": [{
                 "path": "sandbox://workspace/imagine_media/a.mp4",
                 "variants": {"original":
                     "https://vm-1.metaaivm.com/media/raw/workspace/imagine_media/a.mp4"}}]}}},
            # presentation mới hơn nhưng không có videos: không ghi đè
            {"event_name": "delta.presentation", "occurred_at_ms": 400,
             "payload": {"data": {"videos": []}}},
        ]}
        self.assertEqual(
            gw._presentation_video_url(hist, 150),
            "https://vm-1.metaaivm.com/media/raw/workspace/imagine_media/a.mp4")
        self.assertIsNone(gw._presentation_video_url(hist, 500))
        self.assertIsNone(gw._presentation_video_url({}, 0))
        # fallback: chỉ có .path (sandbox://) khi không có variants
        hist2 = {"chat_events": [
            {"event_name": "delta.presentation", "occurred_at_ms": 10,
             "payload": {"data": {"videos": [
                 {"path": "sandbox://workspace/imagine_media/b.mp4"}]}}},
        ]}
        self.assertEqual(gw._presentation_video_url(hist2, 0),
                         "sandbox://workspace/imagine_media/b.mp4")


# ---------------------------------------------------------------------------
# Fake in-process gateway (real Noise responder + same framing)
# ---------------------------------------------------------------------------

class FakeWS:
    """websocket-client stand-in backed by a FakeGateway (synchronous)."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.inbox: list[bytes] = []
        self.closed = False

    def settimeout(self, t):
        pass

    def send(self, data, opcode=None):
        self.gateway.on_client_bytes(bytes(data), self.inbox.append)

    def recv(self):
        if not self.inbox:
            import websocket as _ws
            raise _ws.WebSocketTimeoutException("fake ws: no data")
        return self.inbox.pop(0)

    def close(self):
        self.closed = True


class FakeGateway:
    """Noise responder speaking the same protobuf framing as the real VM."""

    def __init__(self):
        self.noise = NoiseConnection.from_name(gw.NOISE_PROTOCOL)
        self.noise.set_keypair_from_private_bytes(Keypair.STATIC, os.urandom(32))
        self.noise.set_as_responder()
        self.noise.set_prologue(b"")
        self.noise.start_handshake()
        self.handshook = False
        self._asm: dict[int, dict] = {}
        self.stream_ids_seen: list[int] = []
        self.stream_bodies: list[bytes] = []  # body JSON của /chat/stream
        # Test hooks: subscribe có kèm record video không; chat.history
        # có trả delta.presentation không; fs.raw trả bytes nào.
        self.subscribe_video = True
        self.history_video_url = None
        self.fs_raw_bytes = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 200

    def on_client_bytes(self, data: bytes, emit):
        if not self.handshook:
            if not self.noise.handshake_finished and self._stage() == 0:
                self.noise.read_message(data)
                emit(self.noise.write_message(b""))
                self._stage_set(1)
            elif self._stage() == 1:
                self.noise.read_message(data)
                self.handshook = True
            return
        # transport: decrypt -> reassemble -> respond
        plain = bytes(self.noise.decrypt(data))
        fr = gw.decode_noise_frame(plain)
        asm = self._asm.setdefault(fr["chunk_id"], {"total": fr["total_chunks"], "chunks": {}})
        asm["chunks"][fr["chunk_index"]] = fr["payload"]
        if len(asm["chunks"]) < asm["total"]:
            return
        del self._asm[fr["chunk_id"]]
        blob = b"".join(asm["chunks"][i] for i in range(asm["total"]))
        _service, payload = gw.decode_service_request(blob)
        sid, kind, app_bytes = gw.decode_service_frame(payload)
        self.stream_ids_seen.append(sid)
        req = self._decode_app_request(app_bytes)
        self._respond(sid, req, emit)

    _s = 0

    def _stage(self):
        return self._s

    def _stage_set(self, v):
        self._s = v

    @staticmethod
    def _decode_app_request(data: bytes) -> dict:
        fs = gw._dec_fields(data)
        headers = []
        for fn, _wt, v in fs:
            if fn == 3 and isinstance(v, bytes):
                hfs = gw._dec_fields(v)
                headers.append((gw._field(hfs, 1, b"").decode(),
                                gw._field(hfs, 2, b"").decode()))
        return {"verb": gw._field(fs, 1, b"").decode(),
                "path": gw._field(fs, 2, b"").decode(),
                "headers": headers,
                "body": gw._field(fs, 4, b""),
                "end_body": bool(gw._field(fs, 5, 0))}

    def _respond(self, sid: int, req: dict, emit):
        # GET mang query string (?limit=...) — so sánh theo path gốc.
        path = req["path"].split("?", 1)[0]
        if path == "/identity":
            body = json.dumps({"ok": True, "result": {"name": "test-vm"}}).encode()
            self._emit_response(sid, 200, body, True, emit)
        elif path == "/chat/subscribe":
            # Luồng thật: subscribe ack rồi các event (video + done) chảy
            # về trên stream này; chat.stream chỉ gửi + ack.
            ack = json.dumps({"type": "res", "status": "ok", "payload": {
                "subscribed": True, "session_id": "s1",
                "agent_id": "a1"}}).encode() + b"\n"
            self._emit_response(sid, 200, ack, False, emit)
            if self.subscribe_video:
                rec = json.dumps({"type": "event", "event": "message", "message": {
                    "content": [{"type": "video",
                                 "video_url": "https://cdn.example/v/abc.mp4"}]}}).encode() + b"\n"
                self._emit_body_chunk(sid, rec, False, emit)
            done = json.dumps({"type": "event", "event": "delta.message_done",
                               "payload": {"status": "completed",
                                           "message_id": "m2"}}).encode() + b"\n"
            self._emit_body_chunk(sid, done, False, emit)
            self._emit_body_chunk(sid, b"", True, emit)
        elif path == "/chat/history":
            events = []
            if self.history_video_url:
                events.append({
                    "event_name": "delta.presentation",
                    "occurred_at_ms": 9999999999999,
                    "payload": {"data": {"videos": [{
                        "path": "sandbox://workspace/imagine_media/test.mp4",
                        "variants": {"original": self.history_video_url}}]}}})
            body = json.dumps({"ok": True, "result": {
                "channel": "all", "chat_events": events,
                "has_more": False}}).encode()
            self._emit_response(sid, 200, body, True, emit)
        elif path.startswith("/fs/raw/"):
            self._emit_response(sid, 200, self.fs_raw_bytes, True, emit)
        elif path == "/chat/stream":
            self.stream_bodies.append(req["body"])
            ack = json.dumps({"ok": True, "result": {
                "message_id": "m1", "session_id": "s1"}}).encode() + b"\n"
            self._emit_response(sid, 200, ack, False, emit)
            self._emit_body_chunk(sid, b"", True, emit)
        else:
            body = json.dumps({"ok": False, "error": {"code": "nope",
                                                      "message": "unknown path"}}).encode()
            self._emit_response(sid, 404, body, True, emit)

    def _emit_response(self, sid, status, body, end_body, emit):
        out = gw._enc_varint_field(1, status)
        out += gw._enc_bytes_field(3, body)
        out += gw._enc_bool_field(4, end_body)
        frame = gw.encode_service_frame(sid, 3, out)
        self._emit_service(sid, frame, emit)

    def _emit_body_chunk(self, sid, data, end_body, emit):
        chunk = gw._enc_bytes_field(1, data) + gw._enc_bool_field(2, end_body)
        frame = gw.encode_service_frame(sid, 4, chunk)
        self._emit_service(sid, frame, emit)

    def _emit_service(self, sid, frame, emit):
        blob = gw.encode_service_response(frame)
        for chunk in gw.chunk_payload(blob):
            emit(self.noise.encrypt(chunk))


class TestGatewayClientLive(unittest.TestCase):
    def _client(self):
        acc = Account(name="test-acc", cookie="hatch_sess=x")
        client = gw.GatewayClient(acc, _settings())
        fake = FakeGateway()
        ws = FakeWS(fake)
        client.fetch_init = lambda: {  # noqa: E731 - test stub
            "gatewayUrl": "wss://11111111-2222-3333-4444-555555555555.metaaivm.com/",
            "vmName": "11111111-2222-3333-4444-555555555555",
            "authToken": "s0:fake", "noiseNotaryToken": "endorsement.v1.fake"}
        with mock.patch.object(gw.websocket, "create_connection", return_value=ws):
            client.connect()
        self.assertIsNotNone(client._noise)
        self.assertTrue(client._noise.handshake_finished)
        return client, fake

    def test_handshake_and_identity(self):
        client, fake = self._client()
        try:
            self.assertEqual(client.identity(), {"name": "test-vm"})
        finally:
            client.close()

    def test_chat_stream_ack_only(self):
        # Luồng thật: chat.stream chỉ gửi tin nhắn và ack rồi đóng;
        # nội dung trả lời chảy về qua chat.subscribe.
        client, fake = self._client()
        try:
            events = list(client.stream("chat.stream", {
                "items": [{"type": "text", "text": "hi"}],
                "node_id": "dev-1", "capabilities": {}}))
            kinds = [k for k, _ in events]
            self.assertEqual(kinds, ["ack"])
            ack = [o for k, o in events if k == "ack"][0]
            self.assertEqual(ack["message_id"], "m1")
        finally:
            client.close()

    def test_chat_subscribe_carries_records(self):
        client, fake = self._client()
        try:
            events = list(client.stream("chat.subscribe", {
                "after_stream_seq": 0, "after_chat_event_seq": 0,
                "capabilities": {}}))
            kinds = [k for k, _ in events]
            self.assertEqual(kinds, ["ack", "record", "record"])
            recs = [o for k, o in events if k == "record"]
            self.assertEqual(gw._extract_video_url(recs),
                             "https://cdn.example/v/abc.mp4")
            self.assertEqual(recs[-1]["event"], "delta.message_done")
        finally:
            client.close()

    def test_generate_video_end_to_end(self):
        client, fake = self._client()
        seen = {}
        orig = gw.GatewayClient._download_video

        def fake_dl(self, url, dest):
            seen["url"] = url
            dest.write_bytes(b"FAKEVIDEO")
            return dest

        try:
            with mock.patch.object(gw.GatewayClient, "_download_video", fake_dl):
                with tempfile.TemporaryDirectory() as d:
                    dest = client.generate_video(
                        "a lion at dawn", aspect="16:9", duration=8,
                        out_path=Path(d) / "s01.mp4")
                    self.assertEqual(seen["url"], "https://cdn.example/v/abc.mp4")
                    self.assertEqual(dest.read_bytes(), b"FAKEVIDEO")
        finally:
            client.close()
            gw.GatewayClient._download_video = orig

    def test_generate_video_via_history_retrieval(self):
        # Luồng thật (đã xác minh live): subscribe không mang link video,
        # chỉ tới delta.message_done; video về sau dưới dạng
        # delta.presentation trong chat.history, tải bằng fs.raw.
        client, fake = self._client()
        fake.subscribe_video = False
        fake.history_video_url = (
            "https://11111111-2222-3333-4444-555555555555.metaaivm.com"
            "/media/raw/workspace/imagine_media/test.mp4")
        try:
            with tempfile.TemporaryDirectory() as d:
                dest = client.generate_video(
                    "a lion at dawn", aspect="16:9", duration=8,
                    out_path=Path(d) / "s01.mp4")
                self.assertEqual(dest.read_bytes(), fake.fs_raw_bytes)
        finally:
            client.close()

    def test_generate_video_no_video_anywhere_raises(self):
        # Không link trong subscribe lẫn history -> lỗi rõ ràng, không
        # báo thành công giả. Loại lỗi là NoVideoError (không phải
        # GatewayError) để pipeline phân biệt với lỗi kết nối.
        client, fake = self._client()
        fake.subscribe_video = False
        try:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(gw.NoVideoError) as cm:
                    client.generate_video(
                        "a lion at dawn", aspect="16:9", duration=8,
                        out_path=Path(d) / "s01.mp4", history_timeout=0.2)
                self.assertIn("chat.history", str(cm.exception))
        finally:
            client.close()

    def test_generate_video_with_reference_image_sends_image_item(self):
        # Ảnh tham chiếu đi INLINE base64 trong chat.stream items: ảnh
        # trước, text sau; text có câu dặn dùng ảnh làm mẫu nhân vật.
        import base64 as _b64

        png_1x1 = _b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "Xw8AAwMCAO+ip1sAAAAASUVORK5CYII=")
        client, fake = self._client()
        try:
            def _fake_dl(_self, _url, _dest):
                _dest.write_bytes(b"FAKEVIDEO")
                return _dest

            with mock.patch.object(gw.GatewayClient, "_download_video",
                                   _fake_dl):
                with tempfile.TemporaryDirectory() as d:
                    img = Path(d) / "model-sheet.png"
                    img.write_bytes(png_1x1)
                    dest = client.generate_video(
                        "a rat sniffs cheese", aspect="16:9", duration=8,
                        reference_image=img, out_path=Path(d) / "s01.mp4")
                    self.assertEqual(dest.read_bytes(), b"FAKEVIDEO")
            self.assertEqual(len(fake.stream_bodies), 1)
            sent = json.loads(fake.stream_bodies[0])
            items = sent["items"]
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["type"], "image")
            self.assertEqual(items[0]["mime_type"], "image/png")
            self.assertEqual(items[0]["filename"], "model-sheet.png")
            self.assertEqual(_b64.b64decode(items[0]["data_base64"]), png_1x1)
            self.assertEqual(items[1]["type"], "text")
            self.assertIn("Use the attached image as the character reference",
                          items[1]["text"])
        finally:
            client.close()

    def test_generate_video_without_reference_image_sends_text_only(self):
        client, fake = self._client()
        try:
            def _fake_dl(_self, _url, _dest):
                _dest.write_bytes(b"FAKEVIDEO")
                return _dest

            with mock.patch.object(gw.GatewayClient, "_download_video",
                                   _fake_dl):
                with tempfile.TemporaryDirectory() as d:
                    client.generate_video(
                        "a lion at dawn", aspect="16:9", duration=8,
                        out_path=Path(d) / "s01.mp4")
            sent = json.loads(fake.stream_bodies[0])
            items = sent["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["type"], "text")
            self.assertNotIn("character reference", items[0]["text"])
        finally:
            client.close()

    def test_generate_video_reference_image_missing_raises(self):
        client, fake = self._client()
        try:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(gw.GatewayError) as cm:
                    client.generate_video(
                        "a lion at dawn", aspect="16:9", duration=8,
                        reference_image=Path(d) / "nope.jpg",
                        out_path=Path(d) / "s01.mp4")
                self.assertIn("Không thấy file ảnh tham chiếu", str(cm.exception))
            self.assertEqual(fake.stream_bodies, [])  # chưa gửi gì đi
        finally:
            client.close()

    def test_generate_video_reference_image_bad_extension_raises(self):
        client, fake = self._client()
        try:
            with tempfile.TemporaryDirectory() as d:
                img = Path(d) / "sheet.gif"
                img.write_bytes(b"GIF89a")
                with self.assertRaises(gw.GatewayError) as cm:
                    client.generate_video(
                        "a lion at dawn", aspect="16:9", duration=8,
                        reference_image=img, out_path=Path(d) / "s01.mp4")
                self.assertIn("JPEG hoặc PNG", str(cm.exception))
        finally:
            client.close()

    def test_rpc_unknown_path_raises(self):
        client, fake = self._client()
        try:
            with self.assertRaises(gw.GatewayError):
                client.rpc("feed.list", {})
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()

"""Gateway transport: the REAL Muse API over a Noise-encrypted WebSocket.

The muse.ai web client does not call any REST video endpoint (the guessed
``/api/media/generate/video`` does not exist in its JS and returns 403).
Instead it talks RPC-style (method name + JSON params) to a per-user gateway
over ``wss://hatch.metaaivm.com/v1/noise`` protected by a Noise XX
handshake. Full protocol spec: ``docs/gateway-protocol.md``.

Layers (bottom-up):
  * Noise XX (``noiseprotocol`` lib): ``Noise_XX_25519_AESGCM_SHA256``.
  * Protobuf framing (hand-written minimal encode/decode, no runtime dep):
    ``NoiseTransportFrame`` chunking + ``hatch.noise`` service envelope.
  * RPC: route table (method -> HTTP verb/path/service), JSON bodies,
    NDJSON record streams for ``chat.stream``.

Security note: this client does NOT verify the server's confidential-VM
attestation (that check is client-side in the web app and the server cannot
observe it). Server authentication here rests on TLS (``*.metaaivm.com``)
plus the bearer ``s0:`` token. If the gateway answers msg 2 with an RV
(confidential-VM) challenge, the handshake aborts — RV credentials live in
the browser's IndexedDB and are out of scope.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import requests
import websocket
from noise.connection import Keypair, NoiseConnection

from .accounts import Account
from .config import REPO_ROOT
from .muse_client import DEFAULT_HEADERS, MuseClient, MuseError

APP_ID = "hatch-web"
GATEWAY_LB_HOST = "hatch.metaaivm.com"
NOISE_WS_PATH = "/v1/noise"
NOISE_PROTOCOL = b"Noise_XX_25519_AESGCM_SHA256"
HANDSHAKE_TIMEOUT = 10
MAX_CHUNK_PAYLOAD = 65489
MAX_CHUNKS = 256

# service enum (hatch.noise.ServiceType)
SERVICE_DAEMON = 0
SERVICE_SENTINEL = 1
SERVICE_VAULT = 2
SERVICE_AUTHD = 3
SERVICE_SSH_ACCESS = 4

# method -> (http_method, path, service, stream_mode)
# stream_mode: None = single response, "subscription" = NDJSON records
ROUTES: dict[str, tuple[str, str, int, str | None]] = {
    "identity": ("GET", "/identity", SERVICE_DAEMON, None),
    "chat.stream": ("POST", "/chat/stream", SERVICE_DAEMON, "subscription"),
    "chat.stream_events": ("POST", "/chat/stream-events", SERVICE_DAEMON, "subscription"),
    "chat.subscribe": ("POST", "/chat/subscribe", SERVICE_DAEMON, "subscription"),
    "chat.history": ("GET", "/chat/history", SERVICE_DAEMON, None),
    "chat.history_window": ("GET", "/chat/history-window", SERVICE_DAEMON, None),
    "chat.message_get": ("GET", "/chat/message", SERVICE_DAEMON, None),
    "chat.search": ("GET", "/chat/search", SERVICE_DAEMON, None),
    "feed.list": ("GET", "/api/feed", SERVICE_DAEMON, None),
    "feed.infinite": ("GET", "/api/feed/infinite", SERVICE_DAEMON, "subscription"),
    "feed.unit": ("GET", "/api/feed/units/{unit_id}", SERVICE_DAEMON, None),
    "feed.status": ("GET", "/api/feed/status", SERVICE_DAEMON, None),
    "feed.prompt.get": ("GET", "/api/feed/prompt", SERVICE_DAEMON, None),
    "feed.prompt.update": ("PATCH", "/api/feed/prompt", SERVICE_DAEMON, None),
    "feed.regenerate": ("POST", "/api/feed/regenerate", SERVICE_DAEMON, None),
    "feed.unit.media": (
        "GET", "/api/feed/units/{unit_id}/media/{handle}", SERVICE_DAEMON, "binary",
    ),
    "fs.raw": ("GET", "/fs/raw/{*path}", SERVICE_DAEMON, "binary"),
    "fs.stat": ("POST", "/fs/stat", SERVICE_DAEMON, None),
    "fs.list": ("POST", "/fs/list", SERVICE_DAEMON, None),
    "fs.read": ("POST", "/fs/read", SERVICE_DAEMON, None),
}

_VIDEO_URL_KEYS = ("video_url", "url", "media_url", "download_url", "src",
                   "originalSourceUrl", "original_source_url", "result_url",
                   "output_url")


class GatewayError(MuseError):
    """Lỗi ở tầng gateway (handshake / RPC / stream)."""


class NoVideoError(MuseError):
    """Model trả lời xong (message_done) nhưng không đưa ra video nào.

    KHÁC với lỗi kết nối: stream + history đều đọc được, chỉ là không
    có delta.presentation. Thường do nội dung prompt bị từ chối (bộ lọc
    an toàn) — không được tính là acc hỏng, không báo "không kết nối
    được", mà phải xử lý ở tầng prompt (viết lại cho nhẹ nhàng).
    """


# --------------------------------------------------------------------------
# Minimal protobuf encode/decode (varint + length-delimited only)
# --------------------------------------------------------------------------

def _enc_varint(n: int) -> bytes:
    if n < 0:  # int64 negative -> 10-byte two's complement
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _enc_bytes_field(no: int, data: bytes) -> bytes:
    return _enc_varint((no << 3) | 2) + _enc_varint(len(data)) + data


def _enc_varint_field(no: int, n: int) -> bytes:
    return _enc_varint((no << 3) | 0) + _enc_varint(n)


def _enc_bool_field(no: int, value: bool) -> bytes:
    return _enc_varint_field(no, 1 if value else 0)


def _dec_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """-> [(field_no, wire_type, value)] ; value is int (varint) or bytes."""
    out: list[tuple[int, int, int | bytes]] = []
    p = 0
    while p < len(data):
        tag, p = _read_varint(data, p)
        no, wt = tag >> 3, tag & 7
        if wt == 0:
            v, p = _read_varint(data, p)
            out.append((no, wt, v))
        elif wt == 2:
            ln, p = _read_varint(data, p)
            out.append((no, wt, data[p:p + ln]))
            p += ln
        elif wt == 5:
            out.append((no, wt, int.from_bytes(data[p:p + 4], "little")))
            p += 4
        elif wt == 1:
            out.append((no, wt, int.from_bytes(data[p:p + 8], "little")))
            p += 8
        else:
            raise GatewayError(f"Protobuf: wire type {wt} không hỗ trợ (field {no})")
    return out


def _read_varint(data: bytes, p: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if p >= len(data):
            raise GatewayError("Protobuf: varint bị cắt giữa chừng")
        b = data[p]
        p += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, p
        shift += 7
        if shift > 70:
            raise GatewayError("Protobuf: varint quá dài")


def _field(fields: list, no: int, default=None):
    for fn, _wt, v in fields:
        if fn == no:
            return v
    return default


# -- NoiseTransportFrame ----------------------------------------------------

def encode_noise_frame(chunk_id: int, chunk_index: int, total_chunks: int,
                       payload: bytes) -> bytes:
    return (
        _enc_varint_field(1, chunk_id)
        + _enc_varint_field(2, chunk_index)
        + _enc_varint_field(3, total_chunks)
        + _enc_bytes_field(4, payload)
    )


def decode_noise_frame(data: bytes) -> dict:
    fs = _dec_fields(data)
    chunk_id = _field(fs, 1, 0)
    if chunk_id >= (1 << 63):
        chunk_id -= 1 << 64
    return {
        "chunk_id": chunk_id,
        "chunk_index": _field(fs, 2, 0),
        "total_chunks": _field(fs, 3, 1),
        "payload": _field(fs, 4, b""),
    }


def chunk_payload(payload: bytes) -> list[bytes]:
    """Split a ServiceRequest blob into NoiseTransportFrame blobs."""
    chunk_id = random.randint(1, (1 << 62) - 1)
    n = max(1, (len(payload) + MAX_CHUNK_PAYLOAD - 1) // MAX_CHUNK_PAYLOAD)
    if n > MAX_CHUNKS:
        raise GatewayError(f"Payload quá lớn cho noise framing ({len(payload)} B)")
    frames = []
    for i in range(n):
        part = payload[i * MAX_CHUNK_PAYLOAD:(i + 1) * MAX_CHUNK_PAYLOAD]
        frames.append(encode_noise_frame(chunk_id, i, n, part))
    return frames


# -- hatch.noise envelope ---------------------------------------------------

def encode_header(key: str, value: str) -> bytes:
    return _enc_bytes_field(1, key.encode()) + _enc_bytes_field(2, value.encode())


def encode_app_request(verb: str, path: str, headers: list[tuple[str, str]],
                       body: bytes, end_body: bool) -> bytes:
    out = _enc_bytes_field(1, verb.encode()) + _enc_bytes_field(2, path.encode())
    for k, v in headers:
        out += _enc_bytes_field(3, encode_header(k, v))
    out += _enc_bytes_field(4, body) + _enc_bool_field(5, end_body)
    return out


def encode_service_frame(stream_id: int, kind_no: int, kind_bytes: bytes) -> bytes:
    return _enc_varint_field(1, stream_id) + _enc_bytes_field(kind_no, kind_bytes)


def encode_service_request(service: int, payload: bytes) -> bytes:
    return _enc_varint_field(1, service) + _enc_bytes_field(2, payload)


def encode_service_response(payload: bytes) -> bytes:
    return _enc_bytes_field(1, payload)


def decode_service_request(data: bytes) -> tuple[int, bytes]:
    """-> (service, payload) for a client->server ServiceRequest blob."""
    fs = _dec_fields(data)
    return _field(fs, 1, 0), _field(fs, 2, b"")


def decode_service_response(data: bytes) -> bytes:
    return _field(_dec_fields(data), 1, b"")


def decode_service_frame(data: bytes) -> tuple[int, str, bytes]:
    """-> (stream_id, kind, kind_bytes); kind in request/response/body_chunk/reset."""
    fs = _dec_fields(data)
    stream_id = _field(fs, 1, 0)
    if stream_id >= (1 << 63):
        stream_id -= 1 << 64
    kinds = {2: "request", 3: "response", 4: "body_chunk", 5: "reset"}
    for no, name in kinds.items():
        v = _field(fs, no)
        if v is not None:
            return stream_id, name, v
    raise GatewayError("ServiceFrame không có kind nào (request/response/body_chunk/reset)")


def decode_app_response(data: bytes) -> dict:
    fs = _dec_fields(data)
    headers = []
    for _fn, _wt, v in fs:
        if _fn == 2 and isinstance(v, bytes):
            hfs = _dec_fields(v)
            headers.append((_field(hfs, 1, b"").decode("utf-8", "replace"),
                            _field(hfs, 2, b"").decode("utf-8", "replace")))
    return {
        "status": _field(fs, 1, 0),
        "headers": headers,
        "body": _field(fs, 3, b""),
        "end_body": bool(_field(fs, 4, 0)),
    }


def decode_body_chunk(data: bytes) -> dict:
    fs = _dec_fields(data)
    return {"data": _field(fs, 1, b""), "end_body": bool(_field(fs, 2, 0))}


def decode_reset(data: bytes) -> dict:
    fs = _dec_fields(data)
    return {"code": _field(fs, 1, 0), "reason": _field(fs, 2, b"").decode("utf-8", "replace")}


def encode_reset(stream_id: int, reason: str) -> bytes:
    reset = _enc_varint_field(1, 1) + _enc_bytes_field(2, reason.encode())
    frame = encode_service_frame(stream_id, 5, reset)
    return encode_service_request(SERVICE_DAEMON, frame)


# -- handshake payloads -----------------------------------------------------

def encode_client_nonce_message(nonce: bytes) -> bytes:
    """Msg 1 payload: protobuf field 1 = 32-byte client nonce."""
    if len(nonce) != 32:
        raise GatewayError("Client nonce phải đúng 32 byte")
    return _enc_bytes_field(1, nonce)


def decode_rv_challenge(payload: bytes) -> dict | None:
    """Parse msg 2 payload as an RV (confidential VM) challenge.

    Returns None when the payload is not a valid challenge (standard VM).
    Fields: 1=attestation, 2=state (1 unprovisioned / 2 provisioned),
    3=vm_nonce (32 B), 4=vm_proof (32 B), 5=chip_binding.
    """
    try:
        fs = _dec_fields(payload)
    except GatewayError:
        return None
    attestation = _field(fs, 1)
    state = _field(fs, 2)
    vm_nonce = _field(fs, 3)
    if attestation is None or state not in (1, 2):
        return None
    if vm_nonce is not None and len(vm_nonce) != 32:
        return None
    vm_proof = _field(fs, 4)
    if state == 1:
        if vm_proof is not None:
            return None
        return {"state": "unprovisioned", "attestation": attestation,
                "vm_nonce": vm_nonce}
    if vm_proof is None or len(vm_proof) != 32:
        return None
    return {"state": "provisioned", "attestation": attestation,
            "vm_nonce": vm_nonce, "vm_proof": vm_proof}


# --------------------------------------------------------------------------
# Bootstrap: __HATCH_GATEWAY_INIT__ + token refresh
# --------------------------------------------------------------------------

def parse_gateway_init(html: str) -> dict:
    """Parse window.__HATCH_GATEWAY_INIT__ from the muse.ai home page HTML.

    The object sits inside a double JSON-escaped RSC payload, so escapes are
    collapsed iteratively before the balanced-brace scan.
    """
    i = html.find("window.__HATCH_GATEWAY_INIT__=")
    if i < 0:
        raise GatewayError("Không thấy __HATCH_GATEWAY_INIT__ trong trang chủ muse.ai")
    seg = html[i:i + 6000]
    for _ in range(4):
        seg2 = seg.replace('\\"', '"').replace("\\\\", "\\")
        if seg2 == seg:
            break
        seg = seg2
    j = seg.find("{")
    if j < 0:
        raise GatewayError("Không parse được __HATCH_GATEWAY_INIT__ (không thấy '{')")
    depth = 0
    end = None
    for k in range(j, len(seg)):
        if seg[k] == "{":
            depth += 1
        elif seg[k] == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    if end is None:
        raise GatewayError("Không parse được __HATCH_GATEWAY_INIT__ (ngoặc không cân)")
    try:
        init = json.loads(seg[j:end])
    except ValueError as e:
        raise GatewayError(f"Không parse được __HATCH_GATEWAY_INIT__: {e}") from None
    for key in ("gatewayUrl", "vmName", "authToken"):
        if not init.get(key):
            raise GatewayError(f"__HATCH_GATEWAY_INIT__ thiếu khóa {key}")
    return init


def build_ws_url(gateway_url: str, token: str, notary_token: str | None,
                 request_id: str | None = None) -> str:
    """wss://hatch.metaaivm.com/v1/noise?vm_id=...&auth_token=...&..."""
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  gateway_url or "")
    if not m:
        raise GatewayError(f"Không trích được vm_id từ gatewayUrl: {gateway_url!r}")
    params = [
        ("vm_id", m.group(1)),
        ("auth_token", token),
    ]
    if notary_token:
        params.append(("notary_token", notary_token))
    params.append(("app_id", APP_ID))
    params.append(("request_id", request_id or str(uuid.uuid4())))
    return f"wss://{GATEWAY_LB_HOST}{NOISE_WS_PATH}?{urlencode(params)}"


# --------------------------------------------------------------------------
# Ảnh tham chiếu trong chat.stream
# --------------------------------------------------------------------------

# Web UI gửi ảnh đính kèm INLINE base64 trong items của chat.stream
# (không qua bước upload): {type:"image", mime_type, data_base64,
# filename} — ảnh trước, text sau. Chỉ JPEG/PNG (web UI chuẩn hóa ảnh
# về JPEG q=0.85, tối đa 4096px / 16MP); file đã đúng chuẩn của web UI
# thì gửi thẳng. Có Pillow thì ảnh quá khổ được thu nhỏ thay vì từ chối.
_IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
_MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_SIDE_PX = 4096
_MAX_IMAGE_MEGAPIXELS = 16


def _image_item(path) -> dict:
    """Build một chat.stream image item từ file ảnh JPEG/PNG local."""
    p = Path(path)
    if not p.exists():
        raise GatewayError(f"Không thấy file ảnh tham chiếu: {p}")
    mime = _IMAGE_MIME.get(p.suffix.lower())
    if not mime:
        raise GatewayError(f"Ảnh tham chiếu phải là JPEG hoặc PNG: {p}")
    data = p.read_bytes()
    if len(data) > _MAX_INLINE_IMAGE_BYTES:
        data, mime = _shrink_image(data, p)
    return {"type": "image", "mime_type": mime,
            "data_base64": base64.b64encode(data).decode(),
            "filename": p.name}


def _shrink_image(data: bytes, path: Path) -> tuple[bytes, str]:
    """Thu nhỏ ảnh quá lớn về JPEG q=0.85 (cần Pillow, không có thì lỗi rõ)."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise GatewayError(
            f"Ảnh tham chiếu quá lớn ({len(data)} byte) — thu nhỏ ảnh "
            f"dưới {_MAX_INLINE_IMAGE_BYTES // (1024 * 1024)}MB hoặc cài "
            "Pillow để tool tự thu nhỏ.")
    import io

    im = Image.open(io.BytesIO(data))
    w, h = im.size
    if max(w, h) > _MAX_IMAGE_SIDE_PX or w * h > _MAX_IMAGE_MEGAPIXELS * 1_000_000:
        scale = min(_MAX_IMAGE_SIDE_PX / max(w, h),
                    (_MAX_IMAGE_MEGAPIXELS * 1_000_000 / (w * h)) ** 0.5)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


# --------------------------------------------------------------------------
# GatewayClient
# --------------------------------------------------------------------------

class GatewayClient:
    """Client thật qua gateway Noise/WebSocket. API sync (giống MuseClient)."""

    def __init__(self, account: Account, settings: dict, on_record=None):
        self.account = account
        self.settings = settings
        self.on_record = on_record  # callable(kind, obj) for stream events
        muse_cfg = settings.get("muse", {})
        self.base_url = muse_cfg.get("base_url", "https://muse.ai").rstrip("/")
        self.timeout = int(muse_cfg.get("timeout", 120))
        self.poll_timeout = int(muse_cfg.get("poll_timeout", 900))
        self.timezone = muse_cfg.get("timezone", "Asia/Bangkok")
        self._session: requests.Session | None = None
        self._ws = None
        self._noise: NoiseConnection | None = None
        self._init: dict | None = None
        self._stream_id = 0
        self._pending_frames: dict[int, dict] = {}
        self.last_ack: dict | None = None
        self.last_records: list = []

    # -- HTTP session (cookie + proxy, giống MuseClient) -------------------

    def _http(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.headers.update(DEFAULT_HEADERS)
            # muse.ai kiểm tra Sec-Fetch-*: thiếu là /api/* trả 403 dù
            # cookie còn sống (đã xác minh: POST /api/hatch/token 403 ->
            # 200 chỉ nhờ thêm 3 header này, trình duyệt tự gửi chúng).
            s.headers.update({
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            })
            s.cookies.update(self.account.cookies())
            proxy = self.account.proxy_config()
            if proxy.is_valid():
                s.proxies.update(proxy.to_requests())
            self._session = s
        return self._session

    def _url(self, path: str) -> str:
        from urllib.parse import urljoin

        return urljoin(self.base_url + "/", path.lstrip("/"))

    # -- bootstrap ----------------------------------------------------------

    def fetch_init(self) -> dict:
        """GET trang chủ muse.ai, parse __HATCH_GATEWAY_INIT__."""
        r = self._http().get(self._url("/"), timeout=20)
        if r.status_code != 200:
            raise GatewayError(
                f"Không tải được trang chủ muse.ai (HTTP {r.status_code}) — "
                "cookie có thể đã hết hạn"
            )
        try:
            self._init = parse_gateway_init(r.text)
        except GatewayError as e:
            # Trang chủ trả 200 nhưng không có blob gateway: phiên đăng
            # nhập không còn sống (vd: cookie chết nhưng server vẫn trả
            # trang shell thay vì redirect về auth). Đã xác minh: cookie
            # sống trả 200 + blob đầy đủ; không cookie/cookie giả thì
            # bị đẩy về auth.muse.ai (400) và cũng không có blob.
            raise GatewayError(
                f"{e} — phiên đăng nhập có thể đã hết hạn (trang chủ trả về "
                "nhưng không có thông tin gateway). Chạy `accounts check` để "
                "kiểm tra cookie, lấy cookie mới nếu cần."
            ) from None
        return self._init

    def refresh_token(self, gateway_url: str, vm_name: str | None) -> tuple[str, str | None]:
        """POST /api/hatch/token -> (token, notary_token)."""
        body: dict = {"vmAddress": gateway_url}
        if vm_name:
            body["vmName"] = vm_name
        r = self._http().post(self._url("/api/hatch/token"), json=body, timeout=15)
        if r.status_code != 200:
            raise GatewayError(
                f"Refresh gateway token thất bại (HTTP {r.status_code}): {r.text[:200]}"
            )
        try:
            data = r.json()
        except ValueError:
            raise GatewayError("Refresh token trả về không phải JSON") from None
        token = data.get("token")
        if not token:
            raise GatewayError(f"Refresh token không có 'token': {str(data)[:200]}")
        return token, data.get("notary_token")

    def _device_id(self) -> str:
        """Device/node id ổn định theo acc (client tự sinh UUID, lưu ở cache)."""
        d = REPO_ROOT / "cache" / "gateway"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{self.account.name}.json"
        try:
            saved = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(saved.get("device_id"), str) and saved["device_id"]:
                return saved["device_id"]
        except (ValueError, OSError):
            pass
        device_id = str(uuid.uuid4())
        try:
            f.write_text(json.dumps({"device_id": device_id}), encoding="utf-8")
        except OSError:
            pass
        return device_id

    # -- connect + Noise handshake ------------------------------------------

    def connect(self) -> None:
        """Mở WebSocket tới gateway và hoàn tất Noise XX handshake."""
        if self._ws is not None:
            return
        init = self._init or self.fetch_init()
        token = init.get("authToken") or ""
        notary = init.get("noiseNotaryToken")
        if not token:
            token, notary = self.refresh_token(init["gatewayUrl"], init.get("vmName"))
        try:
            self._connect_with_token(init["gatewayUrl"], token, notary)
        except GatewayError as e:
            # Token trong init có thể đã dùng một lần (ot: one-time) hoặc
            # hết hạn -> refresh rồi thử lại đúng một lần.
            if "401" not in str(e):
                raise
            token, notary = self.refresh_token(init["gatewayUrl"], init.get("vmName"))
            self._connect_with_token(init["gatewayUrl"], token, notary)

    def _connect_with_token(self, gateway_url: str, token: str,
                            notary: str | None) -> None:
        url = build_ws_url(gateway_url, token, notary)
        proxy = self.account.proxy_config()
        ws_kwargs: dict = {
            "timeout": 30,  # qua egress proxy, TLS + CONNECT có thể chậm
            # websocket-client tự thêm Origin: <host của URL> nếu không tắt;
            # phải tắt để chỉ còn Origin: https://muse.ai (hai Origin
            # khiến edge của Meta trả 401).
            "suppress_origin": True,
            "header": [
                f"Origin: {self.base_url}",
                "Sec-Fetch-Site: same-origin",
                "Sec-Fetch-Mode: websocket",
                "Sec-Fetch-Dest: websocket",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36",
            ],
        }
        if proxy.is_valid():
            if proxy.scheme in ("http", "https"):
                ws_kwargs["http_proxy_host"] = proxy.host
                ws_kwargs["http_proxy_port"] = proxy.port
                if proxy.username:
                    ws_kwargs["http_proxy_auth"] = (proxy.username, proxy.password or "")
            elif proxy.scheme.startswith("socks"):
                ws_kwargs["proxy_type"] = proxy.scheme
                ws_kwargs["http_proxy_host"] = proxy.host
                ws_kwargs["http_proxy_port"] = proxy.port
                if proxy.username:
                    ws_kwargs["http_proxy_auth"] = (proxy.username, proxy.password or "")
        try:
            ws = websocket.create_connection(url, **ws_kwargs)
        except Exception as e:
            raise GatewayError(f"Không mở được WebSocket tới gateway: {e}") from None
        self._ws = ws
        try:
            self._handshake()
        except Exception:
            self._close_ws()
            raise

    def _handshake(self) -> None:
        noise = NoiseConnection.from_name(NOISE_PROTOCOL)
        noise.set_keypair_from_private_bytes(Keypair.STATIC, os.urandom(32))
        noise.set_as_initiator()
        noise.set_prologue(b"")
        noise.start_handshake()
        # Msg 1: ephemeral pubkey + client-nonce payload (32 random bytes)
        nonce = os.urandom(32)
        msg1 = noise.write_message(encode_client_nonce_message(nonce))
        self._ws_send(msg1)
        # Msg 2: server ephemeral + static + payload
        self._ws.settimeout(HANDSHAKE_TIMEOUT)
        msg2 = self._ws_recv()
        if len(msg2) < 96:
            raise GatewayError(f"Noise msg 2 quá ngắn ({len(msg2)} B, cần >= 96)")
        try:
            payload2 = noise.read_message(msg2)
        except Exception as e:
            raise GatewayError(f"Noise msg 2 giải mã thất bại: {e}") from None
        challenge = decode_rv_challenge(bytes(payload2))
        if challenge is not None:
            raise GatewayError(
                "Gateway là confidential VM (RV challenge) — cần RV credentials "
                "trong IndexedDB của trình duyệt, chưa hỗ trợ. "
                f"state={challenge['state']}"
            )
        # Msg 3: static pubkey + empty payload (standard VM)
        msg3 = noise.write_message(b"")
        self._ws_send(msg3)
        if not noise.handshake_finished:
            raise GatewayError("Noise handshake chưa xong sau msg 3")
        self._noise = noise
        self.handshake_hash = bytes(noise.get_handshake_hash())

    def _ws_send(self, data: bytes) -> None:
        assert self._ws is not None
        self._ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)

    def _ws_recv(self) -> bytes:
        assert self._ws is not None
        data = self._ws.recv()
        if isinstance(data, str):
            raise GatewayError(f"Gateway gửi frame text, chờ binary: {data[:100]!r}")
        return bytes(data)

    def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        self._noise = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def close(self) -> None:
        self._close_ws()

    # -- framing: send / receive --------------------------------------------

    def _next_stream_id(self) -> int:
        self._stream_id += 1
        return self._stream_id

    def _send_rpc(self, method: str, params: dict, stream_mode: str | None) -> int:
        """Encrypt + send one RPC request. Returns the stream id."""
        if self._ws is None or self._noise is None:
            raise GatewayError("Chưa connect gateway (gọi connect() trước)")
        http_method, path, service, _mode = ROUTES[method]
        if "{" in path:  # path params: /api/feed/units/{unit_id}/...
            for key in ("unit_id", "handle", "prompt_id", "ideaCardId", "source"):
                if key in params:
                    path = path.replace("{" + key + "}", str(params.pop(key)))
            if "{*path}" in path and "path" in params:
                # fs.raw: wildcard path, không urlencode (tên file an toàn)
                path = path.replace("{*path}", str(params.pop("path")).lstrip("/"))
        if http_method == "GET":
            if params:
                sep = "&" if "?" in path else "?"
                path = path + sep + urlencode(params)
            body = b""
        else:
            body = json.dumps(params).encode()
        headers = [
            ("Content-Type", "application/json"),
            ("x-request-id", str(uuid.uuid4())),
            ("x-app-id", APP_ID),
            ("Accept-Language", "en-US"),
            ("Accept-Encoding", "identity"),
        ]
        # end_body=True luôn: body của request đã đầy đủ trong một
        # ApplicationRequest; "subscription" chỉ nói về phía response
        # (server stream NDJSON về). Gửi end_body=False khiến server chờ
        # thêm BodyChunk mãi không tới -> treo ~120s rồi 502/timeout.
        app_req = encode_app_request(http_method, path, headers, body,
                                     end_body=True)
        stream_id = self._next_stream_id()
        frame = encode_service_frame(stream_id, 2, app_req)
        blob = encode_service_request(service, frame)
        for chunk in chunk_payload(blob):
            self._ws_send(self._noise.encrypt(chunk))
        return stream_id

    def _recv_service_frame(self, timeout: float) -> tuple[int, str, bytes]:
        """Read WS messages until one complete ServiceFrame is reassembled."""
        assert self._ws is not None and self._noise is not None
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise GatewayError("Hết thời gian chờ gateway trả lời")
            self._ws.settimeout(remaining)
            try:
                raw = self._ws_recv()
            except websocket.WebSocketTimeoutException:
                raise GatewayError("Hết thời gian chờ gateway trả lời") from None
            except websocket.WebSocketConnectionClosedException as e:
                raise GatewayError(f"Gateway đóng kết nối: {e}") from None
            try:
                plain = self._noise.decrypt(raw)
            except Exception as e:
                raise GatewayError(f"Giải mã Noise frame thất bại: {e}") from None
            fr = decode_noise_frame(bytes(plain))
            asm = self._pending_frames.setdefault(fr["chunk_id"], {
                "total": fr["total_chunks"], "chunks": {}, "bytes": 0})
            if fr["total_chunks"] != asm["total"]:
                raise GatewayError("Noise frame: total_chunks không nhất quán")
            if fr["chunk_index"] in asm["chunks"]:
                raise GatewayError("Noise frame: chunk_index trùng")
            asm["chunks"][fr["chunk_index"]] = fr["payload"]
            asm["bytes"] += len(fr["payload"])
            if asm["bytes"] > 16 * 1024 * 1024:
                raise GatewayError("Noise frame: vượt 16 MB")
            if len(asm["chunks"]) < asm["total"]:
                continue
            del self._pending_frames[fr["chunk_id"]]
            blob = b"".join(asm["chunks"][i] for i in range(asm["total"]))
            payload = decode_service_response(blob)
            return decode_service_frame(payload)

    # -- RPC ------------------------------------------------------------------

    @staticmethod
    def _resolve_body(status: int, body_text: str, method: str):
        """Response resolver của client (h): {ok,result} / plain object."""
        data = None
        if body_text:
            try:
                data = json.loads(body_text)
            except ValueError:
                data = None
        if 200 <= status < 300:
            if isinstance(data, dict) and data.get("ok") is True:
                return data.get("result", data)
            if isinstance(data, dict) and "ok" not in data and "error" not in data:
                return data
            if data is None:
                return None
        code = message = None
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                code, message = err.get("code"), err.get("message")
            elif isinstance(err, str):
                message = err
            code = code or data.get("code")
            message = message or data.get("message")
        raise GatewayError(
            f"Gateway {method}: HTTP {status} ({code or '?'}) — "
            f"{message or body_text[:200]}"
        )

    @staticmethod
    def _validate_ack(obj, method: str):
        """Ack validator của client (m): {ok,result} / {type:res,status:ok}."""
        if isinstance(obj, dict) and obj.get("ok") is True and "result" in obj:
            return obj["result"]
        if isinstance(obj, dict) and obj.get("type") == "res" and obj.get("status") == "ok":
            return obj.get("payload")
        if isinstance(obj, dict) and (
            obj.get("ok") is False or obj.get("status") == "err" or "error" in obj
        ):
            raise GatewayError(f"Gateway {method}: ack lỗi: {str(obj)[:300]}")
        return obj

    def rpc(self, method: str, params: dict | None = None,
            timeout: float = 60, binary: bool = False):
        """One-shot RPC (non-streaming). Returns parsed JSON (or raw bytes).

        Server thật gửi response theo kiểu: frame `response` (status +
        headers, body rỗng, end_body=False), rồi các frame `body_chunk`
        cho tới khi end_body=True — nên phải gom đủ body mới xử lý.
        """
        self.connect()
        stream_id = self._send_rpc(method, dict(params or {}), None)
        deadline = time.time() + timeout
        status: int | None = None
        body = b""
        while True:
            sid, kind, payload = self._recv_service_frame(max(1.0, deadline - time.time()))
            if sid != stream_id:
                continue  # stream khác (không dùng ở client đơn luồng)
            if kind == "reset":
                r = decode_reset(payload)
                raise GatewayError(f"Gateway reset stream {method}: {r}")
            if kind == "response":
                resp = decode_app_response(payload)
                status = resp["status"]
                body += resp["body"]
                if resp["end_body"]:
                    break
            elif kind == "body_chunk":
                chunk = decode_body_chunk(payload)
                body += chunk["data"]
                if chunk["end_body"]:
                    break
            else:
                continue
        if status is None:
            raise GatewayError(f"Gateway {method}: không nhận được response")
        if status >= 400:
            return self._resolve_body(status, body.decode("utf-8", "replace"), method)
        if binary:
            return body
        return self._resolve_body(status, body.decode("utf-8", "replace"), method)

    def identity(self) -> dict:
        """Probe miễn phí: GET /identity qua gateway (không tốn quota)."""
        result = self.rpc("identity", {}, timeout=30)
        if not isinstance(result, dict):
            raise GatewayError(f"identity trả về dạng lạ: {str(result)[:200]}")
        return result

    def stream(self, method: str, params: dict | None = None,
               timeout: float = 900):
        """Streaming RPC (chat.stream...). Yields ("ack", obj) / ("record", obj).

        Records are NDJSON lines; the first complete JSON object is the ack
        (unless it is itself a {"type": "event"} record).
        """
        self.connect()
        stream_id = self._send_rpc(method, dict(params or {}), "subscription")
        deadline = time.time() + timeout
        buf = ""
        acked = False
        while True:
            sid, kind, payload = self._recv_service_frame(max(1.0, deadline - time.time()))
            if sid != stream_id:
                continue
            if kind == "reset":
                r = decode_reset(payload)
                raise GatewayError(f"Gateway reset stream {method}: {r}")
            if kind == "response":
                resp = decode_app_response(payload)
                if resp["status"] >= 400:
                    text = resp["body"].decode("utf-8", "replace")
                    self._resolve_body(resp["status"], text, method)
                if resp["body"]:
                    buf += resp["body"].decode("utf-8", "replace")
                if resp["end_body"]:
                    break
            elif kind == "body_chunk":
                chunk = decode_body_chunk(payload)
                if chunk["data"]:
                    buf += chunk["data"].decode("utf-8", "replace")
                if chunk["end_body"]:
                    break
            else:
                continue
            # Parse complete NDJSON lines
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    raise GatewayError(f"Record không phải JSON: {line[:200]}")
                if not acked:
                    acked = True
                    if isinstance(obj, dict) and obj.get("type") == "event":
                        self.last_ack = None
                        yield ("record", obj)
                    else:
                        self.last_ack = self._validate_ack(obj, method)
                        yield ("ack", self.last_ack)
                else:
                    yield ("record", obj)
                if self.on_record:
                    self.on_record("record" if acked else "ack", obj)
        if buf.strip():
            try:
                obj = json.loads(buf.strip())
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                if not acked:
                    self.last_ack = self._validate_ack(obj, method)
                    yield ("ack", self.last_ack)
                else:
                    yield ("record", obj)

    # -- video generation (chat.stream) ---------------------------------------

    def generate_video(self, prompt: str, aspect: str = "16:9", duration: int = 5,
                       reference_image=None, out_path=None,
                       history_timeout: float = 120) -> Path:
        items: list[dict] = []
        if reference_image:
            # Ảnh tham chiếu gửi INLINE base64 trong cùng items với text
            # (đúng cách web UI gửi ảnh đính kèm) — ảnh trước, text sau.
            items.append(_image_item(reference_image))
        dest = Path(out_path) if out_path else Path("video.mp4")
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = f"Create a {duration}-second video ({aspect} aspect ratio): {prompt}"
        if reference_image:
            text += (" Use the attached image as the character reference: "
                     "keep the same character designs, colors and style "
                     "as in the image.")
        items.append({"type": "text", "text": text})
        params = {
            "items": items,
            "node_id": self._device_id(),
            "capabilities": {
                "client_id": "hatch-web",
                "platform": "web",
                "display_name": "Muse Web",
                "version": "0.0.0",
                "capabilities": {
                    "data_sources": {},
                    "device_commands": {},
                    "hatch_app_commands": {},
                    "rendering": {
                        "supported_presentations": [],
                        "supported_inline_presentations": [],
                        "supported_text_entities": [],
                    },
                },
            },
            "timezone": self.timezone,
        }
        records: list = []
        self.last_records = records
        self.last_ack = None
        # Luồng thật (đã xác minh live): chat.stream chỉ gửi tin nhắn và
        # ack rồi đóng; nội dung trả lời (text / media / feed_unit) chảy
        # về qua chat.subscribe — một subscription dài hạn mở TRƯỚC khi
        # gửi. Vì vậy generate_video mở cả hai stream trên cùng một kết
        # nối và đọc multiplex theo stream_id.
        caps = params["capabilities"]
        self.connect()
        sub_id = self._send_rpc(
            "chat.subscribe",
            {"after_stream_seq": 0, "after_chat_event_seq": 0,
             "capabilities": caps},
            "subscription",
        )
        send_id = self._send_rpc("chat.stream", params, "subscription")
        t_send_ms = int(time.time() * 1000)  # mốc cho retrieval qua history
        bufs: dict[int, str] = {sub_id: "", send_id: ""}
        closed: set[int] = set()
        deadline = time.time() + self.poll_timeout
        url = None
        acked = False
        turn_done = False
        while time.time() < deadline:
            sid, kind, payload = self._recv_service_frame(
                max(1.0, deadline - time.time()))
            if kind == "reset":
                r = decode_reset(payload)
                raise GatewayError(f"Gateway reset stream: {r}")
            if kind == "response":
                resp = decode_app_response(payload)
                if resp["status"] >= 400:
                    text = resp["body"].decode("utf-8", "replace")
                    self._resolve_body(resp["status"], text,
                                       "chat.subscribe" if sid == sub_id
                                       else "chat.stream")
                if resp["body"]:
                    bufs[sid] = bufs.get(sid, "") + resp["body"].decode(
                        "utf-8", "replace")
                if resp["end_body"]:
                    closed.add(sid)
            elif kind == "body_chunk":
                chunk = decode_body_chunk(payload)
                if chunk["data"]:
                    bufs[sid] = bufs.get(sid, "") + chunk["data"].decode(
                        "utf-8", "replace")
                if chunk["end_body"]:
                    closed.add(sid)
            else:
                continue
            while "\n" in bufs.get(sid, ""):
                line, bufs[sid] = bufs[sid].split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    raise GatewayError(f"Record không phải JSON: {line[:200]}")
                if sid == send_id:
                    if not acked:
                        acked = True
                        self.last_ack = self._validate_ack(obj, "chat.stream")
                        if self.on_record:
                            self.on_record("ack", self.last_ack)
                    continue
                records.append(obj)
                if self.on_record:
                    self.on_record("record", obj)
                url = _extract_video_url(records)
                if url:
                    break
                if (isinstance(obj, dict)
                        and obj.get("event") == "delta.message_done"
                        and isinstance(obj.get("payload"), dict)
                        and obj["payload"].get("status") == "completed"):
                    turn_done = True
                    break
            if url or turn_done:
                break
            if send_id in closed and sub_id in closed:
                break
        if bufs.get(send_id, "").strip() and not acked:
            try:
                self.last_ack = self._validate_ack(
                    json.loads(bufs[send_id].strip()), "chat.stream")
            except ValueError:
                pass
        if not url:
            url = _extract_video_url(records)
        if not url:
            # Subscribe stream chỉ mang text delta; video đã sinh xong
            # xuất hiện dưới dạng delta.presentation trong chat.history —
            # thử lấy qua đó (tải bằng fs.raw) trước khi báo lỗi.
            summary = _summarize_records(records)
            try:
                return self.retrieve_video_via_history(
                    t_send_ms, dest, timeout=history_timeout)
            except NoVideoError as e:
                raise NoVideoError(
                    "chat.stream xong nhưng không thấy link video trong "
                    f"records ({len(records)} record(s): {summary}); "
                    f"tìm trong chat.history cũng không thấy: {e}"
                ) from None
            except GatewayError:
                # Lỗi transport thật trong lúc quét history (rpc hỏng,
                # ...) — vẫn là lỗi kết nối, giữ nguyên loại để pipeline
                # báo đúng, không gói nhầm thành "không thấy video".
                raise
        return self._download_video(url, dest)

    def retrieve_video_via_history(self, since_ms: int, dest: Path,
                                   timeout: float = 120,
                                   poll_interval: float = 10) -> Path:
        """Lấy video đã sinh xong qua chat.history (không gửi tin mới).

        Poll GET /chat/history cho tới khi thấy event delta.presentation
        từ `since_ms` trở đi (video nằm ở
        payload.data.videos[].variants.original, hoặc .path dạng
        sandbox://...), rồi tải bằng fs.raw qua _download_video.
        Raises NoVideoError nếu hết `timeout` vẫn chưa thấy.
        """
        deadline = time.time() + timeout
        while True:
            hist = self.rpc(
                "chat.history", {"limit": 50, "transcriptMode": "messages"},
                timeout=60)
            url = _presentation_video_url(hist, since_ms)
            if url:
                return self._download_video(url, Path(dest))
            if time.time() >= deadline:
                raise NoVideoError(
                    "Không thấy video trong chat.history sau "
                    f"{timeout:.0f}s (đã qua delta.message_done mà không có "
                    "delta.presentation)"
                )
            time.sleep(min(poll_interval, max(0.0, deadline - time.time())))

    def _download_video(self, url: str, dest: Path) -> Path:
        """Download video: direct URL, feed.unit.media, hoặc fs.raw (VM media).

        Link VM media (https://<vm>/media/raw/<path> hoặc
        sandbox://<path>) được tải qua gateway fs.raw — HTTPS trực tiếp
        tới host VM không trả về gì (empty reply), còn fs.raw trả đúng
        bytes của file trên VM.
        """
        fs_path = _media_url_to_fs_path(url)
        if fs_path:
            data = self.rpc("fs.raw", {"path": fs_path},
                            timeout=self.timeout, binary=True)
            if not _is_video_bytes(data[:16]):
                raise GatewayError(
                    f"fs.raw trả về dữ liệu không phải video "
                    f"(đầu: {data[:8]!r})"
                )
            dest.write_bytes(data)
            return dest
        m = re.match(r"https?://[^/]+/api/feed/units/([^/]+)/media/([^/?#]+)", url)
        if m:
            data = self.rpc("feed.unit.media",
                            {"unit_id": m.group(1), "handle": m.group(2)},
                            timeout=self.timeout, binary=True)
            if not _is_video_bytes(data[:16]):
                raise GatewayError(
                    f"feed.unit.media trả về dữ liệu không phải video "
                    f"(đầu: {data[:8]!r})"
                )
            dest.write_bytes(data)
            return dest
        r = self._http().get(url, timeout=self.timeout, stream=True)
        if r.status_code != 200:
            raise GatewayError(
                f"Tải video thất bại (HTTP {r.status_code}): {url[:120]}"
            )
        ctype = r.headers.get("Content-Type", "")
        if ("text/html" in ctype or "application/json" in ctype
                or ctype.startswith("image/")):
            raise GatewayError(
                f"Link video trả về {ctype} thay vì video: {url[:120]} "
                f"(body: {r.text[:200]!r})"
            )
        with dest.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        head = dest.read_bytes()[:16]
        if not _is_video_bytes(head):
            dest.unlink(missing_ok=True)
            raise GatewayError(
                f"File tải về từ {url[:120]} không phải video "
                f"(đầu file: {head[:8]!r})"
            )
        return dest

    def generate_image(self, prompt: str, aspect: str = "16:9", out_path=None) -> Path:
        raise GatewayError("generate_image chưa hỗ trợ qua gateway (dùng chat.stream)")

    # -- check_login / check_quota: ủy quyền cho REST client ------------------
    # (kiểm tra session web không phụ thuộc transport)

    def check_login(self):
        return MuseClient(self.account, self.settings).check_login()

    def check_quota(self):
        return MuseClient(self.account, self.settings).check_quota()


def _is_video_bytes(data: bytes) -> bool:
    """MP4/MOV: 'ftyp' ở offset 4; WebM/Matroska: đầu EBML 1A 45 DF A3."""
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return True
    return len(data) >= 8 and data[4:8] == b"ftyp"


def _media_url_to_fs_path(url: str) -> str | None:
    """VM media URL / sandbox path -> relative fs path cho fs.raw.

    - sandbox://workspace/... -> workspace/...
    - https://<vm>.metaaivm.com/media/raw/workspace/... -> workspace/...
    - https://<vm>.metaaivm.com/fs/raw/workspace/... -> workspace/...
    Trả về None nếu không phải đường dẫn media của VM.
    """
    if url.startswith("sandbox://"):
        return url[len("sandbox://"):].lstrip("/")
    for marker in ("/media/raw/", "/fs/raw/"):
        if marker in url:
            path = url.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0]
            return path.lstrip("/") or None
    return None


def _presentation_video_url(history: dict, since_ms: int) -> str | None:
    """Link video mới nhất trong các delta.presentation của chat.history.

    Video đã sinh xong xuất hiện dưới dạng event delta.presentation với
    payload.data.videos[].variants.original (URL https trên VM) hoặc
    .path (sandbox://...). Chỉ nhận event từ since_ms trở đi, lấy cái
    mới nhất. Trả về URL/path đó, hoặc None nếu chưa có.
    """
    best: tuple[int, str] | None = None
    for ev in (history or {}).get("chat_events", []) or []:
        if not isinstance(ev, dict) or ev.get("event_name") != "delta.presentation":
            continue
        occurred = ev.get("occurred_at_ms") or 0
        if occurred < since_ms:
            continue
        videos = ((ev.get("payload") or {}).get("data") or {}).get("videos") or []
        if not videos or not isinstance(videos[0], dict):
            continue
        url = ((videos[0].get("variants") or {}).get("original")
               or videos[0].get("path") or "")
        if url and (best is None or occurred >= best[0]):
            best = (occurred, url)
    return best[1] if best else None


_VIDEO_EXTS = (".mp4", ".mov", ".webm")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico")
_FEED_MEDIA_RE = re.compile(r"https?://[^/]+/api/feed/units/[^/]+/media/[^/?#]+")


def _extract_video_url(records: list) -> str | None:
    """Tìm link video trong records: ưu tiên .mp4/.mov/.webm, rồi khóa video/*.

    Chỉ nhận link video thật — URL thường khác (link web, ảnh avatar,
    ...) không được nhận: lần chạy lại thứ hai đã tải nhầm ảnh avatar
    200x200 (fbcdn .png) về làm final.mp4 và báo thành công giả.
    Thà đợi tiếp / thất bại rõ ràng còn hơn thành công giả.
    """
    best: list[tuple[int, str]] = []

    def walk(obj, key_hint: str = ""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, k)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, key_hint)
        elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
            url = obj.split("?")[0].lower()
            if url.endswith(_IMAGE_EXTS):
                return
            if url.endswith(_VIDEO_EXTS):
                best.append((0, obj))
            elif _FEED_MEDIA_RE.match(obj):
                best.append((1, obj))
            elif any(w in key_hint.lower() for w in ("video", "media")):
                best.append((1, obj))

    for rec in records:
        if isinstance(rec, dict) and rec.get("type") == "res":
            continue  # record điều khiển (ack subscribe) không chứa media
        walk(rec)
    if not best:
        return None
    best.sort(key=lambda t: t[0])
    return best[0][1]


def _summarize_records(records: list, limit: int = 5) -> str:
    parts = []
    for rec in records[:limit]:
        if isinstance(rec, dict):
            keys = sorted(str(k) for k in rec.keys())[:8]
            parts.append(f"{{{', '.join(keys)}}}")
        else:
            parts.append(str(rec)[:120])
    if len(records) > limit:
        parts.append(f"... +{len(records) - limit} nữa")
    return "; ".join(parts) or "(không có record nào)"

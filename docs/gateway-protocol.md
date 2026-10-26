# Muse gateway protocol (reverse-engineered from muse.ai client JS)

Source: `/tmp/muse-js` — muse.ai home page (253 KB) + 93 JS chunks (5.7 MB),
fetched 2026-10-26 with a logged-in session. Key files cited as
`<chunk>.js` (Turbopack chunk names). **No secret values are in this file** —
tokens are described schematically only.

## 0. Headline: there is no REST video API

`POST /api/media/generate/video` (and every other guessed `/api/media/*`
path) appears **0 times** in the whole client. A real build against it got
`403 {"error":"Forbidden"}` with a valid session. The client never calls
muse.ai over REST for generation. Everything goes through a **per-user
gateway over a Noise-encrypted WebSocket**, as RPC calls
(`method` name + JSON params) described by a table of ~350 route specs
(`0-mkzk2l0dzfh.js`).

## 1. Bootstrap — how the client gets gateway credentials

The home page embeds (RSC flight payload, double JSON-escaped):

```js
window.__HATCH_GATEWAY_INIT__ = {
  version: 1,
  gatewayUrl: "wss://<vm-uuid>.metaaivm.com/",   // per-user VM id
  vmName: "<vm-uuid>",
  authToken: "s0:<JWT, EdDSA, ~900 chars>",       // bearer for the gateway
  noiseNotaryToken: "endorsement.v1.<...>",       // attestation trust anchor
  attestationTier: "staging",
  attestationDowngradeAllowed: false,
  credentialSession: { ownerId: "<id>", sessionBindingId: "<id>", ... },
  cvmRolloutStatus: "disabled",
  cvmReplacementEnabled: false,
}
```

Token refresh (client `defaultGatewayCredentialProvider.getToken`,
`1u9lpony9k6t-.js`): cached in memory; when missing/expiring, the client
POSTs to **`POST /api/hatch/token`** on muse.ai itself (same-origin fetch,
session cookie) with body `{"vmAddress": <gatewayUrl>, "vmName"?: ...,
"force_refresh"?: true}` and gets back `{"token": "<s0:...>",
"notary_token": "<endorsement...>"}`. A separate endpoint
`POST /api/hatch/noise-notary-token {"vmName": ...}` returns
`{"notaryToken": "..."}`.

## 2. WebSocket URL

`wsUrl` (`1u9lpony9k6t-.js`, module 240542) builds the socket URL from the
gateway URL — note the socket does **not** go to the per-VM host; it goes to
the shared load balancer:

```
wss://hatch.metaaivm.com/v1/noise
  ?vm_id=<vm-uuid>
  &auth_token=<urlencoded s0 token>
  &notary_token=<urlencoded notary token>     # if present
  &app_id=hatch-web
  &request_id=<random UUID>
```

`HATCH_APP_ID` is `NEXT_PUBLIC_ABRA_APP_ID ?? "hatch-web"`;
`NOISE_WS_PATH` is `"/v1/noise"`. The socket is binary
(`binaryType = "arraybuffer"`), 10 s handshake timeout, max inbound WS frame
4 MB.

## 3. Noise handshake — Noise_XX_25519_AESGCM_SHA256

Implementation: hand-rolled WebCrypto code in `3igqvdrze0v1x.js`
(classes `b` = CipherState, `C` = SymmetricState, `k` = HandshakeState,
`p` = handshake orchestrator `performNoiseHandshake`). It is **standard
Noise XX** — no custom crypto:

- Protocol name `Noise_XX_25519_AESGCM_SHA256` (29 bytes, zero-padded to 32).
  `h` starts as that buffer, then `MixHash("")` → `h = SHA256(h)`.
  Empty prologue.
- `MixKey(ikm)`: HKDF-SHA256, salt = current `h`, info empty:
  `PRK = HMAC(salt, ikm)`; `T1 = HMAC(PRK, 0x01)`;
  `T2 = HMAC(PRK, T1 || 0x02)`; new `h = T1`, cipher key = `T2`.
  `Split()` is the same HKDF with empty input → `(send_key, recv_key)`.
- CipherState: AES-256-GCM, 128-bit tag, 12-byte IV = 4 zero bytes +
  8-byte big-endian counter starting at 0, incremented per message.
  Additional data = current handshake hash `h`.
- X25519: WebCrypto, libsodium fallback; low-order public keys rejected;
  all-zero DH output rejected.
- Message flow (XX pattern, `-> e, <- e, ee, s, es, -> s, se`):
  - **Msg 1** (`writeMessage1(payload)`): generate ephemeral keypair `e`;
    `MixHash(e_pub)`; `EncryptAndHash(payload)`; send
    `e_pub (32 B) || ciphertext(payload + 16 B tag)`.
  - **Msg 2** (`readMessage2`, must be ≥ 96 B): `re (32 B)`, `MixHash(re)`;
    `DH(e_priv, re)` → `MixKey`; `DecryptAndHash` 48 B → `rs`;
    `DH(e_priv, rs)` → `MixKey`; `DecryptAndHash` rest → payload.
  - **Msg 3** (`writeMessage3(payload)`): generate static keypair `s`;
    `EncryptAndHash(s_pub)` (48 B); `DH(s_priv, re)` → `MixKey`;
    `EncryptAndHash(payload)`; send both ciphertexts concatenated.
  - `Split()` → `(send_cipher, recv_cipher)` for the transport.

### 3.1 Handshake payloads

- **Msg 1 payload** — a client nonce, protobuf-encoded
  (`createNoiseClientNonce` = 32 random bytes;
  `encodeNoiseClientNonceMessage1` = field 1, length-delimited, the 32 bytes
  → `0x0A 0x20 <32 bytes>`). Fallback if no nonce:
  field 2 varint = 1 → `0x10 0x01`.
- **Msg 2 payload** — decoded with `decodeNoiseRvChallenge` (protobuf):
  field 1 = attestation (bytes), field 2 = state (int32: 1 = unprovisioned,
  2 = provisioned), field 3 = vmNonce (32 B), field 4 = vmProof (32 B),
  field 5 = chipBinding. If it does **not** parse as a challenge, the VM is
  **standard** and the whole payload is the attestation.
- **Msg 3 payload** — empty (`new Uint8Array(0)`) for standard VMs, or for a
  confidential (RV) VM the output of `createNoiseRvMessage3Payload`:
  protobuf field 1 = notaryToken (string) + field 2 (`"fresh"`, the raw
  32-byte RV key, when unprovisioned) or field 3 (`"proof"`, an HMAC-based
  client proof over handshake hash + vmNonce + clientNonce, when
  provisioned; the server's vmProof is checked the same way first).
  The RV key is derived from a recovery key stored in the browser
  (IndexedDB) via `deriveCvmRvKey(recoveryKey, vmId)`.
- **Attestation verification is client-side only.** After msg 2 the client
  verifies the server attestation (`verifier.verify({attestationPayload,
  serverStaticPublicKey, handshakeHash, clientNonce, chipBinding?})`)
  against the notary endorsement, and checks VM trust. The server cannot
  observe whether the client verified. For a minimal client the handshake
  itself (plus TLS server authentication of `*.metaaivm.com`) is what the
  server sees; skipping client-side attestation verification is a
  deliberate, documented trade-off, not a protocol deviation.
  The observed VM is standard when `cvmRolloutStatus` is `"disabled"`
  (as in the captured init) — then msg 3 is empty and no RV credentials
  are needed.

## 4. Transport framing (protobuf, inside the Noise channel)

Two `.proto` descriptors are embedded as base64 in `3igqvdrze0v1x.js`
(`ingress_rev_proxy.proto`, `hatch.noise`); field numbers below were
confirmed by decoding them.

### 4.1 `NoiseTransportFrame` (package `ingress_rev_proxy`)

Every encrypted Noise message is one or more of these (chunked):

| field | # | type | notes |
|---|---|---|---|
| chunk_id | 1 | int64 | random int64 per logical message |
| chunk_index | 2 | uint32 | 0-based |
| total_chunks | 3 | uint32 | 1–256 |
| payload | 4 | bytes | ≤ 65 489 B per chunk; reassembled in index order |

Chunking: `ceil(len/65489)` chunks (1 chunk minimum, even for empty
payload); decoder keeps ≤ 16 pending assemblies, 60 s staleness eviction,
16 MB total assembly budget. After the Noise handshake, each frame is
encrypted with the **send** cipher (empty associated data) and sent as one
binary WS message; inbound frames are decrypted with the **receive** cipher
and reassembled. A complete reassembled payload is one `ServiceResponse`.

### 4.2 `hatch.noise` envelope

```
enum ServiceType { SERVICE_DAEMON=0, SERVICE_SENTINEL=1, SERVICE_VAULT=2,
                   SERVICE_AUTHD=3, SERVICE_SSH_ACCESS=4 }

message ServiceRequest  { ServiceType service = 1; bytes payload = 2; }
message ServiceResponse { bytes payload = 1; }
message ServiceFrame {
  int64 stream_id = 1;
  oneof kind {
    ApplicationRequest  request    = 2;
    ApplicationResponse response   = 3;
    BodyChunk           body_chunk = 4;
    Reset               reset      = 5;
  }
}
message Header { string key = 1; string value = 2; }
message ApplicationRequest {
  string verb = 1;            // HTTP method, e.g. "POST"
  string path = 2;            // e.g. "/chat/stream"
  repeated Header headers = 3;
  bytes body = 4;
  bool end_body = 5;
}
message ApplicationResponse {
  int32 status = 1;           // HTTP-like status
  repeated Header headers = 2;
  bytes body = 3;
  bool end_body = 4;
}
message BodyChunk { bytes data = 1; bool end_body = 2; }
message Reset {
  enum Code { CODE_UNSPECIFIED=0, CANCELLED=1, TIMEOUT=2, PROTOCOL_ERROR=3,
              REFUSED_STREAM=4, INTERNAL_ERROR=5, SERVICE_UNAVAILABLE=6 }
  Code code = 1; string reason = 2;
}
```

### 4.3 `NoiseTransport` (`class G`, `3igqvdrze0v1x.js`)

- `encryptHttpRequest({service, httpMethod, path, headers, body})`:
  builds `ApplicationRequest` (end_body=true), wraps it in
  `ServiceFrame{streamId, kind=request}`, wraps that in
  `ServiceRequest{service, payload=ServiceFrame bytes}`, chunks it into
  `NoiseTransportFrame`s, encrypts each with the send cipher, returns
  `{streamId, frames}`. Stream IDs start at 1 and increment.
- `startStreamRequest(...)`: same, but empty body and `end_body=false`
  (for streaming methods).
- `encryptBodyChunk({streamId, service, data, endBody})`,
  `encryptReset({streamId, service, reason})` (code CANCELLED).
- `decryptFrame(bytes)`: decrypt with receive cipher → reassemble chunks →
  parse `ServiceResponse` → parse `ServiceFrame` → returns
  `{case:"response", streamId, response}` /
  `{case:"bodyChunk", streamId, data, endBody}` /
  `{case:"reset", streamId, reset}`.
- **zstd**: responses may carry `Content-Encoding: zstd` (WASM decoder,
  validated as a real zstd frame, magic `0xFD2FB528`); requests may be
  zstd-compressed only after a successful `fs.stat` probe (and only for
  bodies ≥ 1024 B). A client that sends `Accept-Encoding: identity` gets
  identity responses — the simple path.

## 5. RPC layer (route table + request/response envelopes)

`NoiseChannel` (`0xc_fy1unf0ca.js`, class `S`) + `NoiseStreamTable`
(class `y`) multiplex streams over one socket. Max 100 pending requests,
128 pending streams; default request timeout 60 s; stream idle timeout
300 s.

- **Route resolution**: `resolveHttpRouteRequest(method, params)` looks the
  method up in the route table; the route gives `httpMethod`, `path`
  (with `{param}` placeholders), `service` (default `"daemon"` when absent),
  `responseType` (`"binary"` for media downloads), `streamMode`
  (`"subscription"` / `"records"`), `requestTimeoutMs`.
- **Request building** (`buildNoiseRequestArgs`, `3igqvdrze0v1x.js`):
  GET → params appended to the path as a query string; otherwise body =
  `JSON.stringify(params)` (unless `requestBody == "empty"`). Headers,
  in order: `Content-Type: application/json` (only when there is a body),
  `x-request-id: <random UUID>`, `x-app-id: hatch-web`,
  `Accept-Language: <locale or "en-US">`, plus any extra headers.
  For daemon/sentinel services the client also appends
  `Accept-Encoding: identity` when zstd is unavailable (or `zstd` when it
  is) — send `identity` to keep responses uncompressed.
- **Non-streaming response** (`function h`, `0xc_fy1unf0ca.js`): body bytes
  → UTF-8 text → `JSON.parse`. If status is 2xx and the JSON has
  `ok === true` → resolve `result` (or the object); if 2xx and the JSON has
  neither `ok` nor `error` → resolve the object. Otherwise reject with a
  `GatewayJarvisRejectionError` carrying `code`, `message`, `httpStatus`.
  Binary responses resolve as a `Blob`.
- **Streaming response** (`streamMode: "subscription"`, e.g. `chat.stream`):
  the first body chunk is the **ack** — parsed as JSON; an ack shaped
  `{ok:true, result}` resolves to `result`, `{type:"res", status:"ok"}`
  resolves to `payload`, an error shape rejects. The stream then stays
  open; subsequent body chunks are **newline-delimited JSON records**
  (max 4 MB per record), each delivered to the connection's
  `handleGatewayMessage` callback. A record with `type === "event"` and an
  `event` field also counts as an ack. Stream end = a `bodyChunk` with
  `end_body=true`, or a `reset` frame.
- **Record streams** (`streamMode: "records"`, e.g. `feed.infinite`): same
  NDJSON framing; each line is JSON-parsed and passed to `onStreamRecord`.

## 6. Method reference (route specs, `0-mkzk2l0dzfh.js`)

| method | HTTP | path | notes |
|---|---|---|---|
| `identity` | GET | `/identity` | returns `{name}` — free handshake+RPC probe, no quota |
| `chat.stream` | POST | `/chat/stream` | `noiseOnly`, `streamMode: "subscription"` |
| `chat.stream_events` | POST | `/chat/stream-events` | subscription |
| `chat.subscribe` | POST | `/chat/subscribe` | subscription, 30 s timeout |
| `chat.history` | GET | `/chat/history` | params `{limit, transcriptMode: "messages"}` → `{channel, chat_events, has_more, transcript_revision}`; see §7.2 |
| `chat.history_window` | GET | `/chat/history-window` | params `{anchor_seq, before_limit, after_limit}` |
| `chat.message_get` | GET | `/chat/message` | |
| `chat.search` | GET | `/chat/search` | params `{query}` → `{has_more, hits: [{chat_event}]}` |
| `fs.raw` | GET | `/fs/raw/{*path}` | `responseType: "binary"`, `channel: "media"`, `maxResponseBytes: ∞`; path is workspace-relative, no leading `/`; see §7.2 |
| `fs.stat` | POST | `/fs/stat` | body `{path}` (relative, no leading `/` — absolute paths → HTTP 400 `invalid_params`) |
| `fs.list` | POST | `/fs/list` | body `{path}` |
| `fs.read` | POST | `/fs/read` | body `{path, offset, len}` → base64 chunk |
| `feed.list` | GET | `/api/feed` | params `{prompt_id}?` |
| `feed.infinite` | GET | `/api/feed/infinite` | `noiseOnly`, `streamMode: "records"` |
| `feed.status` | GET | `/api/feed/status` | params `{prompt_id}` (or `{}`) |
| `feed.search` | GET | `/api/feed/search` | |
| `feed.unit` | GET | `/api/feed/units/{unit_id}` | |
| `feed.unit.media` | GET | `/api/feed/units/{unit_id}/media/{handle}` | `responseType: "binary"`, `channel: "media"` |
| `feed.prompt.get` | GET | `/api/feed/prompt` | |
| `feed.prompt.update` | PATCH | `/api/feed/prompt` | body not visible in initial bundle |
| `feed.regenerate` | POST | `/api/feed/regenerate` | body not visible in initial bundle |
| `feed.prompts.delete` | DELETE | `/api/feed/prompts/{prompt_id}` | |
| `feed.unit.delete` / `.reaction` / `.tap` / `.seen`, `feed.reorder` | — | `/api/feed/...` | |

### 6.1 `chat.stream` send shape (`1k9ii4ygx3-yx.js`)

```json
{
  "items": [{ "type": "text", "text": "<prompt>" }],
  "node_id": "<device/node id>",
  "capabilities": {
    "client_id": "hatch-web",
    "platform": "web",
    "display_name": "<brand> Web",
    "version": "0.0.0",
    "capabilities": {
      "data_sources": {},
      "device_commands": {},
      "hatch_app_commands": { "...": "..." },
      "rendering": { "supported_presentations": ["..."], "...": "..." }
    }
  },
  "session_id": "<session id, if continuing a thread>",
  "hidden_prompt": "<...>" ,
  "timezone": "<IANA tz>",
  "metadata": { "...": "..." }
}
```

(A legacy shape `{message: "<text>", node_id, capabilities, ...}` also
exists.) The call is `sendRequest("chat.stream", params,
{expectedGatewayTargetKey, onWireSent})`; the promise resolves with the
ack object (fields seen: `reply_to_message_id`, `session_id`,
`message_id`). The agent then generates media server-side and streams

#### `items` — attachments (verified from `buildAttachmentItems`, two deployments agree)

Attachments go in the same `items` array, **before** the text item:

| type | fields | when |
|---|---|---|
| `text` | `text` | always, appended **last** |
| `image` | `mime_type`, `data_base64`, `filename` | every image attachment — **always inline base64, never uploaded** |
| `file` | `mime_type`, `data_base64`, `filename` | small non-image files, inline base64 |
| `file_ref` | `mime_type`, `path`, `filename` | large non-image files; `path` = VM-relative upload path, no leading slash (uploaded first via `fs.write`, 512 KiB chunks) |

Images are never uploaded (`shouldUploadAttachment` returns false for
`category == "image"`). The web UI normalizes pasted images before
sending (JPEG/PNG only, re-encoded JPEG q=0.85, max 4096 px / 16 MP);
files already in that shape pass through as-is. **Unverified:** whether
the server-side video pipeline actually uses an attached image as a
character reference — the transport delivers it; the model may treat it
as chat context only. One live probe (model sheet + short prompt) is
the only way to settle this.
records back. `node_id` is the client's device/node id
(`hatch_native_auth_device` cookie value family); `capabilities` mostly
describes what the client can render — the server tolerates a minimal
object (exact required subset is **unverified**; start minimal and expand
from observed rejections).

## 7. Live verification results (2026-10-26, acc1)

Proven end-to-end against the real gateway:

- **Init + token refresh**: `GET muse.ai/` parses
  `window.__HATCH_GATEWAY_INIT__`; `POST /api/hatch/token` returns a fresh
  `s0:` token + notary. **Gotcha**: muse.ai checks `Sec-Fetch-*` headers —
  without `Sec-Fetch-Site: same-origin` / `Sec-Fetch-Mode: cors` /
  `Sec-Fetch-Dest: empty` the refresh returns **403** (same for other
  `/api/*` calls). Browsers send these automatically; `requests` must add
  them explicitly.
- **WebSocket open**: `wss://hatch.metaaivm.com/v1/noise?...` — **gotcha**:
  `websocket-client` auto-adds `Origin: <URL host>`; combined with a
  custom `Origin: https://muse.ai` the gateway sees **two Origin headers
  and returns 401**. Fix: `suppress_origin=True` + a single custom Origin.
- **Noise XX handshake**: completes against the real VM (standard VM,
  attestationTier `staging`, no RV challenge). `identity()` returns
  `{"name": "Muse", ...}`.
- **Response framing (real server)**: an RPC reply arrives as a `response`
  frame (status + headers, **empty body, `end_body=False`**), then one or
  more `body_chunk` frames, then a final empty `body_chunk` with
  `end_body=True`. Clients must accumulate until `end_body`.
- **`chat.stream` send shape (fixed)**: `end_body` on the
  ApplicationRequest **must be `True`** — the request body is complete in
  one frame; `streamMode: "subscription"` only describes the response.
  Sending `end_body=False` makes the server wait for more body chunks
  forever: the call hangs ~120 s, then fails (HTTP 502 / client timeout).
  With `end_body=True` the ack arrives in ~2-3 s:
  `{"channel":"main","is_thread":false,"message_id":...,
  "reply_to_message_id":...,"session_id":...}` (note: the ack line has **no
  trailing newline**; the stream then closes).
- **Replies arrive on `chat.subscribe`, not `chat.stream`**: the web app
  opens a long-lived `chat.subscribe` subscription (params
  `{after_stream_seq: 0, after_chat_event_seq: 0, capabilities: {...}}`
  for the main route; plus `session_id` for a thread route) and the agent's
  reply events stream there as NDJSON `{"type":"event","event":<name>,
  "payload":{...},"seq":N,"ts_ms":...}`. Observed event names:
  `agent.status` (`waiting_for_subagents` / `working` / `responding`),
  `task.status`, `message.user` (echo of the sent message),
  `delta.message_start`, `delta.text_append` (reply text, chunked),
  `delta.message_done` (`payload.status: "completed"`), `chat.seen`,
  `approvals.snapshot`. Subscribe ack:
  `{type:"res",status:"ok",payload:{subscribed:true,session_id,...,
  agent_id,conn_id,is_primary:true,stream_scope:"all",
  supported_reply_to_target_types:[...,"feed_unit",...]}}`.
- **Session sharing (important)**: the gateway session is the account's
  **main chat session** — the same session the web UI / main agent uses.
  Messages sent via `chat.stream` appear as user messages in that live
  conversation and the agent replies there. A video request therefore
  shows up in the user's chat history, and the video arrives as a
  `delta.presentation` event in chat history (see §7.2) — this is **not**
  a headless API. Background automation must weigh this side effect.

## 7.1 Sandbox egress notes (environment-specific, not protocol)

- This VM's egress goes through an HTTP proxy (env `https_proxy`); direct
  TCP:443 is transparently redirected to the proxy, which speaks plain
  HTTP CONNECT — a direct TLS handshake fails with `WRONG_VERSION_NUMBER`.
  `requests`/curl use the env proxy and work; `websocket-client` also uses
  it (env) and works once the Origin header is fixed.
- `POST /api/hatch/token` intermittently returns **503**
  (`AsyncSocketException ... Connection reset by peer`); retry a few times.

## 7.2 Retrieval of an already-generated video (proven live 2026-10-26)

**Settled: the subscribe stream does NOT carry the video.** Across three
live generations the `chat.subscribe` stream ended at
`delta.message_done` (completed) with only text deltas (`agent.status`,
`task.status`, `message.user`, `delta.message_start` / `delta.text_append`,
`approvals.snapshot`, `reactions.updated`) — zero `.mp4` URLs, and the only
`feed_unit` mention is the ack's `supported_reply_to_target_types` list.
A fresh subscribe kept open ~2 min after the fact also delivered no late
media event (subscribe is live-only, no backfill). **The video arrives as a
`delta.presentation` event in chat history instead.**

Retrieval chain (all read-only, no new generation/quota):

1. **`chat.history`** — GET `/chat/history`, params
   `{limit: 50, transcriptMode: "messages"}` → `{channel, chat_events,
   has_more, transcript_revision}`. Each `chat_event` has `event_name`,
   `message_id`, `occurred_at_ms`, `payload`, ... The video delivery is a
   `delta.presentation` event whose
   `payload.data.videos[]` carries, per video:
   `{byte_len, label, mime: "video/mp4", missing: false,
   path: "sandbox://workspace/imagine_media/<file>.mp4",
   variants: {original: "https://<vm>.metaaivm.com/media/raw/workspace/imagine_media/<file>.mp4"}}`.
   `byte_len` matches the file on the VM exactly. Poll history after
   `delta.message_done` (the presentation lands a few seconds after the
   turn completes) and take the newest event at/after the send timestamp.
   `chat.search` (GET `/chat/search`, params `{query}`) can find the
   request messages instead, but history is the direct path.
2. **`fs.raw`** — GET `/fs/raw/{*path}`, `responseType: "binary"`,
   `channel: "media"`. The `{*path}` is the workspace-relative path
   derived from the presentation URL: strip the `sandbox://` prefix, or
   take the part after `/media/raw/` (or `/fs/raw/`) in the
   `variants.original` URL, dropping any query string. **Do not use plain
   HTTPS for the VM URL**: a direct GET to
   `https://<vm>.metaaivm.com/media/raw/...` completes TLS but the server
   closes with an empty reply (no HTTP response). The browser fetches VM
   media over the gateway with `kind: "noise"` + that same path
   (`getHatchVmMedia` / `getVmMediaNoiseFetchPath` in the client JS) —
   `fs.raw` is the gateway RPC behind it. Verified live: `fs.raw`
   returned 6,576,161 bytes, SHA-256 identical to the file on the VM,
   ffprobe 10.0 s / 1280x720. (For completeness: `fs.stat` POST
   `{path}` returns file metadata but rejects absolute paths with
   HTTP 400 `invalid_params` — pass the relative path.)
3. **Feed is NOT where chat videos live** — `feed.list` (GET `/api/feed`)
   returns only the Vibes daily news digest (25 units, 2026-10-08–
   2026-10-26, all AI-video/animation news headlines; attachment kinds
   `none` / `hero_image` / `widget` / `carousel`). No savanna videos
   there. `feed.unit.media` (GET
   `/api/feed/units/{unit_id}/media/{handle}`, binary) does work for
   feed attachments (verified: 130,759-byte JPEG hero image), but it is
   not the chat-video path.

`GatewayClient` implements this as `retrieve_video_via_history(since_ms,
dest, timeout=120, poll_interval=10)` (polls `chat.history`, downloads via
`_download_video` → `fs.raw` branch, validates `ftyp`/EBML magic bytes)
and calls it from `generate_video()` as the fallback after
`message_done` when no URL appeared in the subscribe stream.
`_download_video` also accepts `sandbox://` paths and VM
`/media/raw/`/ `/fs/raw/` URLs directly (all routed to `fs.raw`).

## 8. What remains unknown (to be settled by live observation)

1. **Whether a live `chat.subscribe` delivers `delta.presentation` if
   kept open past `message_done`** — unobserved; the retrieval path above
   (poll `chat.history`) makes this moot for automation.
2. **`feed.regenerate` / `feed.prompt.update` request bodies** — the feed
   UI chunk is lazy-loaded and was not in the initial 93 chunks.
3. **Attestation verifier internals** — how the notary endorsement is
   checked (signature over attestation payload + handshake hash). Not
   needed for a minimal client (see section 3.1).
4. Whether the server sends any frames before msg 2.
5. **How the browser obtains a `media_grant`** for cross-origin
   `/media/raw/` fetches (`getSignedMediaCrossOrigin` requires a
   `media_grant` query param) — irrelevant over the gateway (`fs.raw`),
   but open for completeness.

## 9. Implementation notes for automuse-multi

- Use maintained libraries: `noiseprotocol` (`Noise_XX_25519_AESGCM_SHA256`,
  same HKDF/nonce layout as §3) + `websockets` (or `websocket-client`) +
  hand-written minimal protobuf encode/decode for the six messages in §4
  (no `protobuf` runtime needed; `black` not required).
- Minimal handshake path (standard VM): msg1 with client-nonce payload,
  msg2 → try `decodeNoiseRvChallenge`; if it parses → confidential VM →
  **stop and report** (RV credentials live in the browser's IndexedDB and
  are out of scope); else msg3 with empty payload, `split()`, then RPCs.
- Send `Accept-Encoding: identity`; do not implement request zstd.
- Free pre-flight: `identity` (GET `/identity`) proves handshake + RPC
  without spending quota; then one `chat.stream` generation.
- Keep the old REST `MuseClient` intact behind `muse.transport: rest`;
  new `GatewayClient` behind `muse.transport: gateway`.

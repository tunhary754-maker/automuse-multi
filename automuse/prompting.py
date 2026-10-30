"""Muse tự viết prompt sinh video từ kịch bản người dùng paste vào.

Hai đường đi:

* ``prompts_from_script(script)`` — từ một Script đã parse (xem
  ``automuse/script.py``): làm giàu từng ``scene.visual`` thành một prompt
  tiếng Anh hoàn chỉnh (hành động cụ thể, nhân vật nhất quán, góc máy,
  phong cách theo ``kind``, tỉ lệ khung hình, 1–3 câu, không thoại).
* ``prompts_from_text(raw_text, client=...)`` — từ đoạn văn bản thô
  (tiếng Việt, có thể đánh số cảnh hoặc tách bằng dòng trống): nếu có
  ``GatewayClient`` thì nhờ Muse (qua gateway chat — cùng luồng
  subscribe-trước-gửi-sau như ``GatewayClient.generate_video``) chia cảnh
  và viết prompt; nếu không có client hoặc gọi Muse thất bại thì rơi về
  đường template xác định (không bao giờ crash vì input lạ).

Lưu ý về session: tin nhắn gửi qua gateway chat xuất hiện trong đoạn chat
chính của tài khoản (giống mọi lệnh ``chat.stream`` khác) — đây không phải
API headless. Chỉ gọi Muse khi người dùng đã đồng ý.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from .characters import character_description, lock_fragment
from .script import Script

# ---------------------------------------------------------------------------
# Instruction gửi cho Muse (ngắn, tiếng Anh). Muse trả về JSON list để
# parse chắc chắn; parser vẫn chấp nhận dạng đánh số / tách đoạn nếu
# Muse không trả JSON.
# ---------------------------------------------------------------------------

PROMPT_WRITER_INSTRUCTION = (
    "You write video-generation prompts for a silent AI video generator. "
    "I will paste a script in Vietnamese, split into numbered beats. "
    "For each beat, write ONE video prompt in English: 1-3 sentences, "
    "concrete visible action, consistent character names, camera and motion, "
    "matching the style ({style}) and aspect ratio ({aspect}). "
    "No dialogue, no narration text, no on-screen text in the prompt. "
    "Reply with ONLY a JSON array, one object per beat in order, like: "
    '[{"title": "Scene title", "prompt": "The English video prompt..."}]. '
    "No markdown fences, no extra commentary."
)

# Biến thể cho ASMR (kiểu hayang): hoạt hình mèo dễ thương, không thoại,
# tiếng động nhẹ nhàng là nhân vật chính — dặn rõ từng âm thanh mềm.
PROMPT_WRITER_INSTRUCTION_ASMR = (
    "You write video-generation prompts for a cute ASMR animation generator. "
    "I will paste a script in Vietnamese, split into numbered beats. "
    "For each beat, write ONE video prompt in English: 1-3 sentences, "
    "concrete visible action, consistent character names, camera and motion, "
    "matching the style ({style}) and aspect ratio ({aspect}). "
    "Focus on one soft satisfying sound per beat (gentle brushing, pouring "
    "water, soft tapping, slow massage) and describe it clearly, because "
    "the sound is the star of the video. "
    "No dialogue, no narration text, no on-screen text in the prompt. "
    "Reply with ONLY a JSON array, one object per beat in order, like: "
    '[{"title": "Scene title", "prompt": "The English video prompt..."}]. '
    "No markdown fences, no extra commentary."
)

_STYLE_BY_KIND = {
    "documentary": "wildlife documentary cinematography, BBC Earth style",
    "movie": "cinematic live-action drama, film still, naturalistic lighting, realistic",
    "animation": "Larva-style silent slapstick animation, exaggerated cartoon physics",
    "asmr": "cute soft 3D animation, ASMR vibes, pastel colors, cozy and relaxing, gentle slow movements, satisfying repetitive actions",
}

# Hậu tố âm thanh: hoạt hình câm + tài liệu (lời dẫn thêm ở hậu kỳ) thì
# clip gốc câm; phim truyện có thoại nên dặn nhân vật nói + cử động môi
# (cần cho bước lip-sync hậu kỳ). ASMR cũng câm, nhưng tiếng động mềm
# (chải, rót nước, gõ nhẹ) là nhân vật chính — dặn rõ để model sinh
# tiếng gốc khớp từng hành động, vì tiếng gốc sẽ làm nền (không đắp nhạc).
_SUFFIX_BY_KIND = {
    "documentary": "silent, no dialogue.",
    "movie": "natural dialogue, visible lip movement when a character speaks.",
    "animation": "silent, no dialogue.",
    "asmr": "silent, no dialogue, soft satisfying ASMR sounds only.",
}

_CAMERA_BY_KIND = {
    "documentary": [
        "Wide establishing shot",
        "Close-up",
        "Slow tracking shot",
        "Aerial shot",
        "Slow motion close-up",
    ],
    "movie": [
        "Wide shot",
        "Close-up",
        "Low angle shot",
        "Tracking shot",
        "Over-the-shoulder shot",
    ],
    "animation": [
        "Wide shot",
        "Close-up, slapstick timing",
        "Low angle shot",
        "Tracking shot",
        "Overhead shot",
    ],
    # ASMR: cận cảnh là chính — hành động gây tiếng nằm ở chi tiết nhỏ.
    "asmr": [
        "Extreme close-up",
        "Macro close-up",
        "Slow close-up",
        "Overhead close-up",
        "Soft focus close-up",
    ],
}

_DEFAULT_DURATION = 8


@dataclass
class ScenePrompt:
    """Một prompt sinh video cho một cảnh: tiêu đề + prompt tiếng Anh."""

    title: str
    prompt: str
    duration: int = _DEFAULT_DURATION
    scene_id: str = ""


# ---------------------------------------------------------------------------
# Tách beat + dịch thô Việt -> Anh (đường template, không cần mạng)
# ---------------------------------------------------------------------------

# Cụm từ tiếng Việt thường gặp trong kịch bản -> tiếng Anh. Khớp cụm dài
# trước, cụm ngắn sau; phần còn lại giữ nguyên tiếng Việt (vẫn nằm trong
# prompt để không mất ý).
_VI_EN = {
    "sư tử cái": "lioness",
    "sư tử": "lion",
    "linh dương": "antelope",
    "thảo nguyên": "savanna",
    "bình minh": "dawn",
    "hoàng hôn": "sunset",
    "rình mồi": "stalking prey",
    "rượt đuổi": "chase",
    "săn mồi": "hunting",
    "gặm cỏ": "grazing",
    "bồ câu": "pigeon",
    "chuột cống": "sewer rat",
    "chuột": "rat",
    "gián": "cockroach",
    "mặt trời": "sun",
    "mặt trăng": "moon",
    "thành phố": "city",
    "con phố": "street",
    "phố": "street",
    "rừng": "forest",
    "sông": "river",
    "biển": "sea",
    "núi": "mountain",
    "cỏ": "grass",
    "mưa": "rain",
    "tuyết": "snow",
    "đêm": "night",
    "buổi sáng": "morning",
    "buổi chiều": "afternoon",
    "trẻ em": "children",
    "em bé": "baby",
    "voi": "elephant",
    "hổ": "tiger",
    "gấu": "bear",
    "khỉ": "monkey",
    "rắn": "snake",
    "mèo": "cat",
    "chó": "dog",
    "chim": "bird",
    "cá": "fish",
    "ăn": "eating",
    "ngủ": "sleeping",
    "nhảy": "jumping",
    "đánh nhau": "fighting",
    "chơi": "playing",
    "cười": "laughing",
    "chạy": "running",
    "bay": "flying",
    "bơi": "swimming",
    "vàng": "golden",
    "đỏ": "red",
    "xanh": "green",
    "trắng": "white",
    "đen": "black",
    "lớn": "big",
    "nhỏ": "small",
    "nhanh": "fast",
    "chậm": "slow",
    "đẹp": "beautiful",
    "nguy hiểm": "dangerous",
}

# Dòng mở đầu một cảnh/beat: "Cảnh 1 — ...", "Scene 2: ...", "1. ...", "1) ..."
_BEAT_HEADING_RE = re.compile(
    r"^(?:cảnh|scene|phần|part)\s*\d+\s*[-:.—)]?\s*(.*)$"
    r"|^(?:\d+)\s*[.)]\s+(.*)$",
    re.IGNORECASE,
)

_SENTENCE_END_RE = re.compile(r"[.!?…]+\s*")


@dataclass
class _Beat:
    title: str
    text: str


def split_beats(raw_text: str) -> list[_Beat]:
    """Tách văn bản thô thành các beat (mỗi beat = 1 cảnh).

    Nhận: dòng trống ngăn đoạn, dòng mở đầu "Cảnh N ..."/"1. ..." tách beat
    mới, phần còn lại gộp vào beat hiện tại. Văn bản rỗng -> [].
    """
    if not raw_text or not raw_text.strip():
        return []
    lines = [ln.strip() for ln in raw_text.strip().splitlines()]
    beats: list[_Beat] = []
    current_title = ""
    current_parts: list[str] = []

    def flush():
        nonlocal current_title, current_parts
        text = " ".join(p for p in current_parts if p).strip()
        if not text and current_title:
            # Beat một dòng kiểu "1. Sư tử đi săn": phần sau số thứ tự
            # vừa là tiêu đề vừa là nội dung của beat.
            text = current_title
        if text:
            beats.append(_Beat(title=current_title, text=text))
        current_title, current_parts = "", []

    for line in lines:
        if not line:
            flush()
            continue
        m = _BEAT_HEADING_RE.match(line)
        if m:
            flush()
            title = (m.group(1) or m.group(2) or "").strip(" —-:.").strip()
            current_title = title
            continue
        current_parts.append(line)
    flush()
    return beats


def _translate_core(text: str) -> str:
    """Dịch thô cụm từ Việt -> Anh theo bảng _VI_EN (cụm dài trước)."""
    out = text
    for vi, en in sorted(_VI_EN.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"\b{re.escape(vi)}\b", en, out, flags=re.IGNORECASE)
    return out


def _clean_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip("\"'“”")
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    if text and text[-1] not in ".!?…":
        text += "."
    return text


def _template_prompt(beat: _Beat, index: int, kind: str, aspect: str,
                     duration: int) -> ScenePrompt:
    """Một prompt theo template xác định cho 1 beat (không cần Muse)."""
    style = _STYLE_BY_KIND.get(kind, _STYLE_BY_KIND["documentary"])
    cameras = _CAMERA_BY_KIND.get(kind, _CAMERA_BY_KIND["documentary"])
    camera = cameras[index % len(cameras)]
    core = _translate_core(beat.text)
    # Câu 1: hành động (bản dịch thô, cắt gọn); câu 2: phong cách/góc máy.
    core_short = core[:200].strip()
    sentence = _clean_sentence(core_short)
    suffix = (
        f"{camera}, {style}, {aspect} aspect ratio, "
        f"{_SUFFIX_BY_KIND.get(kind, _SUFFIX_BY_KIND['documentary'])}"
    )
    prompt = f"{sentence} {suffix}"
    # Giữ lại mô tả gốc (tiếng Việt) để không mất chủ thể của beat.
    original = beat.text[:120].strip()
    if original:
        prompt += f" (Scene: {original})"
    title = beat.title or f"Scene {index + 1}"
    return ScenePrompt(title=title, prompt=prompt, duration=duration,
                       scene_id=f"s{index + 1:02d}")


# ---------------------------------------------------------------------------
# Từ Script đã parse
# ---------------------------------------------------------------------------

def _scene_prompt(scene, index: int, kind: str, aspect: str,
                  characters: dict | None = None) -> ScenePrompt:
    """Làm giàu 1 scene thành prompt hoàn chỉnh (xác định, không gọi Muse)."""
    style = _STYLE_BY_KIND.get(kind, _STYLE_BY_KIND["documentary"])
    cameras = _CAMERA_BY_KIND.get(kind, _CAMERA_BY_KIND["documentary"])
    camera = cameras[index % len(cameras)]
    title = scene.title or scene.id or f"Scene {index + 1}"

    core = (scene.visual or "").strip()
    if not core:
        # Chưa có visual: dựng hành động từ tiêu đề + lời dẫn (dịch thô).
        seed = " ".join(p for p in (scene.title, scene.narration) if p).strip()
        core = _translate_core(seed) if seed else title
    core = re.sub(r"\s+", " ", core).strip()
    if core and core[0].islower():
        core = core[0].upper() + core[1:]
    if core and core[-1] not in ".!?…":
        core += "."

    parts = [core]
    if scene.character:
        # Khóa mô tả nhân vật (nếu có trong bảng characters) — không ghi
        # trùng khi mô tả hoặc câu Featuring đã nằm trong visual.
        frag = lock_fragment(characters or {}, scene.character)
        desc = character_description(characters or {}, scene.character)
        if frag and frag not in core and not (desc and desc in core):
            parts.append(frag)
    suffix = (f"{camera}, {style}, {aspect} aspect ratio, "
              f"{_SUFFIX_BY_KIND.get(kind, _SUFFIX_BY_KIND['documentary'])}")
    if suffix.lower() not in core.lower():
        parts.append(suffix)
    prompt = " ".join(parts)
    return ScenePrompt(title=title, prompt=prompt,
                       duration=int(scene.duration or _DEFAULT_DURATION),
                       scene_id=scene.id or f"s{index + 1:02d}")


def prompts_from_script(script: Script) -> list[ScenePrompt]:
    """Một prompt tiếng Anh cho mỗi scene của Script đã parse."""
    return [
        _scene_prompt(scene, i, script.kind, script.aspect, script.characters)
        for i, scene in enumerate(script.scenes)
    ]


# ---------------------------------------------------------------------------
# Hỏi Muse qua gateway (text chat) + parse câu trả lời
# ---------------------------------------------------------------------------

def _event_text(event: dict) -> str:
    """Trích đoạn text trong 1 subscribe event (nhiều dạng payload).

    delta.text_append thật mang text trong payload; khóa cụ thể không được
    ghi chắc trong doc nên thử lần lượt các khóa thường gặp, kể cả lồng nhau.
    """
    payload = event.get("payload") if isinstance(event, dict) else None
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("text", "delta", "content", "append", "chunk", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            chunks = []
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                elif isinstance(item, str):
                    chunks.append(item)
            if chunks:
                return "".join(chunks)
        if isinstance(value, dict) and isinstance(value.get("text"), str):
            return value["text"]
    return ""


def _chat_text_params(client, user_text: str) -> tuple[dict, dict]:
    """Params chat.stream / chat.subscribe — cùng shape với generate_video."""
    caps = {
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
    }
    params = {
        "items": [{"type": "text", "text": user_text}],
        "node_id": client._device_id(),
        "capabilities": caps,
        "timezone": getattr(client, "timezone", "Asia/Bangkok"),
    }
    return params, caps


def ask_muse_text(client, user_text: str, timeout: float = 180.0) -> str:
    """Hỏi Muse 1 câu text qua gateway, trả về toàn bộ text trả lời.

    Cùng luồng với ``GatewayClient.generate_video``: mở ``chat.subscribe``
    TRƯỚC, gửi ``chat.stream`` sau, đọc multiplex theo stream_id; text
    chảy về qua các event ``delta.text_append`` trên subscribe, dừng ở
    ``delta.message_done`` (completed). Dùng các method private của
    GatewayClient (_send_rpc/_recv_service_frame/_validate_ack) — cùng
    package, cùng pattern với generate_video.
    """
    from .gateway import decode_app_response, decode_body_chunk, decode_reset

    params, caps = _chat_text_params(client, user_text)
    client.connect()
    sub_id = client._send_rpc(
        "chat.subscribe",
        {"after_stream_seq": 0, "after_chat_event_seq": 0, "capabilities": caps},
        "subscription",
    )
    send_id = client._send_rpc("chat.stream", params, "subscription")
    bufs: dict[int, str] = {sub_id: "", send_id: ""}
    closed: set[int] = set()
    deadline = time.time() + timeout
    # Delta có "seq" (số thứ tự event, xem docs/gateway-protocol.md): ráp
    # theo seq và khử trùng, vì reply dài có nhiều chunk — delta tới lệch
    # thứ tự hoặc lặp lại sẽ xáo trộn/nhân đôi text nếu chỉ append theo
    # thứ tự tới (lỗi 2026-10-30: kịch bản 18 cảnh trả về title nhân đôi,
    # xáo trộn ký tự, YAML không parse được). Event không có seq (fake
    # test) thì giữ thứ tự tới như cũ.
    text_by_seq: dict[int, str] = {}
    text_unseq: list[str] = []
    acked = False
    done = False
    while time.time() < deadline and not done:
        sid, kind, payload = client._recv_service_frame(
            max(1.0, deadline - time.time()))
        if kind == "reset":
            r = decode_reset(payload)
            raise RuntimeError(f"Gateway reset stream: {r}")
        if kind == "response":
            resp = decode_app_response(payload)
            if resp["status"] >= 400:
                raise RuntimeError(
                    f"Gateway chat: HTTP {resp['status']}: "
                    f"{resp['body'].decode('utf-8', 'replace')[:200]}"
                )
            if resp["body"]:
                bufs[sid] = bufs.get(sid, "") + resp["body"].decode("utf-8", "replace")
            if resp["end_body"]:
                closed.add(sid)
        elif kind == "body_chunk":
            chunk = decode_body_chunk(payload)
            if chunk["data"]:
                bufs[sid] = bufs.get(sid, "") + chunk["data"].decode("utf-8", "replace")
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
                continue
            if sid == send_id:
                if not acked:
                    acked = True
                    client._validate_ack(obj, "chat.stream")
                continue
            if not isinstance(obj, dict) or obj.get("type") == "res":
                continue  # ack của subscribe, không chứa text
            if obj.get("event") == "delta.message_start":
                # Reply mới bắt đầu: bỏ text của message cũ nếu subscribe
                # replay lại event cũ — chỉ giữ reply hiện tại.
                text_by_seq.clear()
                text_unseq.clear()
            elif obj.get("event") == "delta.text_append":
                text = _event_text(obj)
                seq = obj.get("seq")
                try:
                    seq = int(seq) if seq is not None else None
                except (TypeError, ValueError):
                    seq = None
                if seq is not None:
                    text_by_seq.setdefault(seq, text)
                else:
                    text_unseq.append(text)
            elif obj.get("event") == "delta.message_done":
                done = True
                break
        if send_id in closed and sub_id in closed:
            break
    if bufs.get(send_id, "").strip() and not acked:
        try:
            client._validate_ack(json.loads(bufs[send_id].strip()), "chat.stream")
        except ValueError:
            pass
    if text_by_seq:
        ordered = "".join(text_by_seq[s] for s in sorted(text_by_seq))
        return (ordered + "".join(text_unseq)).strip()
    return "".join(text_unseq).strip()


def parse_prompt_reply(text: str) -> list[ScenePrompt]:
    """Parse câu trả lời của Muse thành list ScenePrompt.

    Chấp nhận, theo thứ tự: JSON array (có thể trong ```json fence, hoặc
    lẫn trong text), dòng đánh số (1. / 1) / Scene N: / Cảnh N:), rồi đoạn
    tách bằng dòng trống. Không parse được gì -> ValueError.
    """
    if not text or not text.strip():
        raise ValueError("Câu trả lời của Muse rỗng")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    # 1) JSON array
    m = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
        except ValueError:
            data = None
        if isinstance(data, list) and data:
            out: list[ScenePrompt] = []
            for i, item in enumerate(data):
                if isinstance(item, str) and item.strip():
                    out.append(ScenePrompt(title=f"Scene {i + 1}",
                                           prompt=item.strip()))
                elif isinstance(item, dict):
                    prompt = str(item.get("prompt") or item.get("text")
                                 or item.get("video_prompt") or "").strip()
                    title = str(item.get("title") or item.get("scene")
                                or f"Scene {i + 1}").strip()
                    if prompt:
                        out.append(ScenePrompt(title=title, prompt=prompt))
            if out:
                return out

    # 2) Dòng đánh số / "Scene N:" / "Cảnh N:" — chỉ khi thực sự có
    # dòng tiêu đề đánh số; nếu không thì rơi xuống tách theo đoạn trống.
    numbered: list[tuple[str, str]] = []
    current_title = ""
    current_parts: list[str] = []
    saw_heading = False

    def flush_numbered():
        nonlocal current_title, current_parts
        body = " ".join(p for p in current_parts if p).strip()
        if body:
            numbered.append((current_title, body))
        current_title, current_parts = "", []

    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(
            r"^(?:scene|cảnh|phần)\s*\d+\s*[-:.—)]?\s*(.*)$"
            r"|^\d+\s*[.)]\s+(.*)$",
            line, flags=re.IGNORECASE,
        )
        if m:
            saw_heading = True
            flush_numbered()
            rest = (m.group(1) or m.group(2) or "").strip()
            # "Scene 1: Title — prompt..." -> tách title ở dấu — đầu tiên
            if " — " in rest:
                current_title, first = rest.split(" — ", 1)
                current_title = current_title.strip()
                if first.strip():
                    current_parts.append(first.strip())
            else:
                current_title = ""
                if rest:
                    current_parts.append(rest)
            continue
        current_parts.append(line)
    flush_numbered()
    if saw_heading and numbered:
        return [
            ScenePrompt(title=t or f"Scene {i + 1}", prompt=p)
            for i, (t, p) in enumerate(numbered)
        ]

    # 3) Đoạn tách bằng dòng trống
    blocks = [b.strip() for b in re.split(r"\n\s*\n", cleaned) if b.strip()]
    if len(blocks) > 1:
        out = []
        for i, block in enumerate(blocks):
            lines = block.splitlines()
            title = ""
            if len(lines) > 1 and len(lines[0]) <= 60:
                title = lines[0].strip(" #*-")
                block = "\n".join(lines[1:])
            out.append(ScenePrompt(title=title or f"Scene {i + 1}",
                                   prompt=re.sub(r"\s+", " ", block).strip()))
        return out
    if blocks:
        block = re.sub(r"\s+", " ", blocks[0]).strip()
        # Một khối duy nhất quá ngắn / không có từ thật thì không phải
        # prompt (vd: Muse chỉ trả "...") -> để caller fallback.
        if len(block) >= 12 and len(block.split()) >= 2:
            return [ScenePrompt(title="Scene 1", prompt=block)]
    raise ValueError("Không parse được prompt nào từ câu trả lời của Muse")


# ---------------------------------------------------------------------------
# Điểm vào chính: từ văn bản thô
# ---------------------------------------------------------------------------

def prompts_from_text(raw_text: str, client=None, kind: str = "documentary",
                      aspect: str = "16:9", timeout: float = 180.0,
                      strict: bool = False) -> list[ScenePrompt]:
    """Viết prompt sinh video từ kịch bản paste vào (tiếng Việt).

    Có ``client`` (GatewayClient) thì nhờ Muse viết; không có client hoặc
    gọi Muse thất bại / trả về không parse được thì rơi về template xác
    định (``strict=True`` thì raise thay vì fallback). Văn bản rỗng -> [].
    """
    beats = split_beats(raw_text)
    if not beats:
        return []

    if client is not None:
        style = _STYLE_BY_KIND.get(kind, _STYLE_BY_KIND["documentary"])
        numbered = "\n".join(
            f"Beat {i + 1}: {b.text}" for i, b in enumerate(beats)
        )
        # .format() không dùng được: instruction chứa ngoặc {} của JSON mẫu.
        if kind == "movie":
            base_instruction = PROMPT_WRITER_INSTRUCTION_MOVIE
        elif kind == "asmr":
            base_instruction = PROMPT_WRITER_INSTRUCTION_ASMR
        else:
            base_instruction = PROMPT_WRITER_INSTRUCTION
        instruction = base_instruction.replace(
            "{style}", style).replace("{aspect}", aspect)
        user_text = f"{instruction}\n\nScript:\n{numbered}"
        try:
            reply = ask_muse_text(client, user_text, timeout=timeout)
            parsed = parse_prompt_reply(reply)
            # Giữ tiêu đề/duration của beat khi Muse không đặt tiêu đề,
            # và cắt về đúng số beat nếu Muse trả thừa.
            out: list[ScenePrompt] = []
            for i, item in enumerate(parsed[:len(beats)]):
                title = item.title
                if title.startswith("Scene ") and beats[i].title:
                    title = beats[i].title
                out.append(ScenePrompt(
                    title=title, prompt=item.prompt,
                    duration=_DEFAULT_DURATION, scene_id=f"s{i + 1:02d}"))
            if out:
                return out
        except Exception:
            if strict:
                raise
    return [
        _template_prompt(beat, i, kind, aspect, _DEFAULT_DURATION)
        for i, beat in enumerate(beats)
    ]

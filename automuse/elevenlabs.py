"""Lồng tiếng bằng ElevenLabs: đọc lời thoại từng cảnh theo kịch bản.

- ElevenLabsClient (thật): gọi api.elevenlabs.io, model mặc định
  eleven_multilingual_v2 (đọc tốt tiếng Việt), cache audio theo hash
  (text + voice + model) để không tốn quota khi chạy lại.
- MockElevenLabs: không cần API key, tạo file wav im lặng đúng độ dài
  ước lượng để chạy thử pipeline.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import requests

from .audio import run_ffmpeg

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"


def estimate_speech_duration(text: str, chars_per_second: float = 12.0) -> float:
    """Ước lượng độ dài lời đọc (tiếng Việt ~12 ký tự/giây)."""
    text = (text or "").strip()
    if not text:
        return 0.0
    return max(1.0, len(text) / chars_per_second)


class ElevenLabsError(Exception):
    pass


class ElevenLabsClient:
    def __init__(
        self,
        api_key: str,
        model: str = "eleven_multilingual_v2",
        cache_dir: str | Path | None = None,
        stability: float = 0.5,
        similarity_boost: float = 0.75,
    ):
        if not api_key:
            raise ElevenLabsError("Thiếu ELEVENLABS_API_KEY")
        self.api_key = api_key
        self.model = model
        self.stability = stability
        self.similarity_boost = similarity_boost
        self.cache_dir = Path(cache_dir) if cache_dir else Path("cache/tts")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        })

    # -- voices -------------------------------------------------------
    def list_voices(self) -> list[dict]:
        r = self.session.get(f"{ELEVENLABS_BASE}/voices", timeout=30)
        if r.status_code == 401:
            raise ElevenLabsError("API key ElevenLabs không đúng (401)")
        r.raise_for_status()
        return r.json().get("voices", [])

    def resolve_voice(self, name_or_id: str) -> str:
        """Cho phép dùng tên giọng (vd: 'Rachel') thay vì voice_id."""
        if not name_or_id:
            raise ElevenLabsError("Chưa chỉ định giọng đọc")
        voices = self.list_voices()
        for v in voices:
            if v.get("voice_id") == name_or_id or v.get("name", "").lower() == name_or_id.lower():
                return str(v["voice_id"])
        raise ElevenLabsError(
            f"Không thấy giọng '{name_or_id}'. Chạy `voices` để xem danh sách."
        )

    # -- tts ----------------------------------------------------------
    def _cache_key(self, text: str, voice_id: str) -> str:
        raw = f"{self.model}|{voice_id}|{self.stability}|{self.similarity_boost}|{text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def tts(
        self, text: str, voice_id: str, out_path: str | Path | None = None
    ) -> Path:
        text = (text or "").strip()
        if not text:
            raise ElevenLabsError("Text rỗng, không có gì để đọc")
        key = self._cache_key(text, voice_id)
        dest = Path(out_path) if out_path else self.cache_dir / f"{key}.mp3"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            return dest  # dùng cache
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {
                "stability": self.stability,
                "similarity_boost": self.similarity_boost,
            },
        }
        r = self.session.post(
            f"{ELEVENLABS_BASE}/text-to-speech/{voice_id}",
            json=payload,
            timeout=120,
        )
        if r.status_code == 401:
            raise ElevenLabsError("API key ElevenLabs không đúng (401)")
        if r.status_code == 422:
            raise ElevenLabsError(f"ElevenLabs từ chối: {r.text[:300]}")
        r.raise_for_status()
        dest.write_bytes(r.content)
        return dest


class MockElevenLabs:
    """Tạo wav im lặng đúng độ dài ước lượng — chạy thử không cần key."""

    def __init__(self, cache_dir: str | Path | None = None, **_kwargs):
        self.cache_dir = Path(cache_dir) if cache_dir else Path("cache/tts")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def list_voices(self) -> list[dict]:
        return [
            {"voice_id": "mock-narrator", "name": "narrator"},
            {"voice_id": "mock-an", "name": "An"},
            {"voice_id": "mock-binh", "name": "Bình"},
        ]

    def resolve_voice(self, name_or_id: str) -> str:
        return f"mock-{name_or_id}"

    def tts(self, text: str, voice_id: str, out_path=None) -> Path:
        key = hashlib.sha1(f"{voice_id}|{text}".encode()).hexdigest()[:16]
        dest = Path(out_path) if out_path else self.cache_dir / f"mock-{key}.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        duration = estimate_speech_duration(text)
        # Pipeline lưu TTS mock ra .mp3 nhưng pcm_s16le không mux được
        # vào MP3 (ffmpeg lỗi 234) — dùng libmp3lame cho .mp3,
        # giữ pcm_s16le cho .wav.
        codec = "libmp3lame" if dest.suffix.lower() == ".mp3" else "pcm_s16le"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration:.2f}",
            "-c:a", codec, str(dest),
        ]
        run_ffmpeg(cmd)
        return dest


def build_elevenlabs(api_key: str, settings: dict, mock: bool = False,
                     cache_dir: str | Path | None = None):
    cfg = settings.get("elevenlabs", {})
    if mock or not api_key:
        return MockElevenLabs(cache_dir)
    return ElevenLabsClient(
        api_key,
        model=cfg.get("model", "eleven_multilingual_v2"),
        cache_dir=cache_dir,
        stability=float(cfg.get("stability", 0.5)),
        similarity_boost=float(cfg.get("similarity_boost", 0.75)),
    )

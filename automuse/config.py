"""Cấu hình chung: đường dẫn, settings.yaml, biến môi trường."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
DEFAULT_CACHE_DIR = REPO_ROOT / "cache"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output"

DEFAULT_SETTINGS = {
    "muse": {
        # Base URL của Muse web app. Endpoint API của Muse web chưa công khai,
        # nên các path dưới đây có thể chỉnh lại cho khớp với API thật mà bạn
        # quan sát được (DevTools -> Network) khi dùng muse.ai / automuse.site.
        "base_url": "https://muse.ai",
        "login_check_path": "/api/auth/session",
        "generate_video_path": "/api/media/generate/video",
        "generate_image_path": "/api/media/generate/image",
        "job_status_path": "/api/media/jobs/{job_id}",
        "timeout": 120,
        "poll_interval": 5,
        "poll_timeout": 900,
    },
    "generation": {
        "default_aspect": "16:9",
        "default_duration": 5,
        "default_fps": 30,
        "delay_between_jobs": 2,
        "max_retries_per_scene": 3,
    },
    "elevenlabs": {
        "model": "eleven_multilingual_v2",
        "default_voice": "narrator",
        "stability": 0.5,
        "similarity_boost": 0.75,
    },
    "video": {
        "width_16_9": 1280,
        "height_16_9": 720,
        "width_9_16": 720,
        "height_9_16": 1280,
        "fps": 30,
        "crf": 20,
        "preset": "medium",
        "iris_out_seconds": 1.0,
        "fade_out_seconds": 1.0,
        "narration_gap_seconds": 0.3,
    },
}


def load_settings(path: Path | str | None = None) -> dict:
    """Đọc settings.yaml, thiếu key nào thì lấy từ DEFAULT_SETTINGS."""
    settings = _deep_copy(DEFAULT_SETTINGS)
    p = Path(path) if path else SETTINGS_FILE
    if p.exists():
        with open(p, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        _deep_merge(settings, user)
    return settings


def _deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _deep_copy(obj):
    if isinstance(obj, dict):
        return {k: _deep_copy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_copy(v) for v in obj]
    return obj


def elevenlabs_api_key() -> str:
    """Lấy ElevenLabs API key từ biến môi trường hoặc file .env ở repo root."""
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key:
        return key
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ELEVENLABS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

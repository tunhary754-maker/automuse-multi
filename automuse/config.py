"""Cấu hình chung: đường dẫn, settings.yaml, biến môi trường."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
ENV_FILE = REPO_ROOT / ".env"
DEFAULT_CACHE_DIR = REPO_ROOT / "cache"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output"

DEFAULT_SETTINGS = {
    "muse": {
        # Base URL của Muse web app. Endpoint API của Muse web chưa công khai,
        # nên các path dưới đây có thể chỉnh lại cho khớp với API thật mà bạn
        # quan sát được (DevTools -> Network) khi dùng muse.ai / automuse.site.
        "base_url": "https://muse.ai",
        # Login check không dùng /api/auth/session nữa: endpoint đó trả 401
        # ngay cả với cookie còn sống. check_login() GET thẳng trang chủ
        # (phụ: /api/me) và coi 200 + ở đúng host + trang app đầy đủ là OK.
        # Quota/token: API không công khai — check_quota() thử các path
        # ứng viên dưới đây, đọc được thì báo số dư, không thì báo không rõ
        # (giới hạn tuần cục bộ vẫn chạy bình thường).
        "quota_check_paths": [
            "/api/user/usage", "/api/usage", "/api/credits",
            "/api/user/credits", "/api/quota", "/api/user/quota",
            "/api/balance", "/api/subscription",
        ],
        "generate_video_path": "/api/media/generate/video",
        "generate_image_path": "/api/media/generate/image",
        "job_status_path": "/api/media/jobs/{job_id}",
        # Transport gọi Muse: "rest" (REST đoán mò, đã biết sai: 403) hoặc
        # "gateway" (Noise WebSocket thật, xem docs/gateway-protocol.md).
        # Mặc định "gateway" từ 2026-10-26: vòng lặp đã khép kín live
        # (gửi qua chat.stream + lấy video về qua chat.history/fs.raw).
        "transport": "gateway",
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
    "limits": {
        # Giới hạn tuần (tính theo tuần lịch, thứ Hai 00:00 -> thứ Hai sau).
        # 0 = không giới hạn. Đổi số trong config/settings.yaml cho khớp
        # với gói Muse của bạn; mỗi acc có thể ghi đè riêng bằng
        # `accounts set-limit --name <tên> --videos N --minutes M`.
        "weekly_max_videos": 20,
        "weekly_max_minutes": 120,
    },
    "elevenlabs": {
        "model": "eleven_multilingual_v2",
        "default_voice": "narrator",
        "stability": 0.5,
        "similarity_boost": 0.75,
        "speed": 1.0,
        "style": 0.0,
        "use_speaker_boost": True,
    },
    "lipsync": {
        # Khớp môi hậu kỳ bằng Sync Labs (sync.so) — chỉ chạy khi build
        # có --lipsync và cảnh có lipsync: true. Model: lipsync-2
        # (mặc định, nhanh/rẻ), lipsync-2-pro, sync-3 (tốt nhất, 4K),
        # react-1 (biểu cảm, clip tối đa 15s). sync_mode "cut_off" cắt
        # audio dài hơn video ở cuối clip; poll_timeout là thời gian chờ
        # tối đa mỗi cảnh. Sync Labs tính phí theo giây ngoài gói tháng
        # (xem sync.so/pricing). Key nhập bằng `lipsync set-key`.
        "model": "lipsync-2",
        "sync_mode": "cut_off",
        "poll_timeout": 600,
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


def save_env_key(key: str, value: str, path: Path | str | None = None) -> Path:
    """Ghi/cập nhật một dòng KEY=value trong .env (giữ các dòng khác).

    Dùng cho key nhạy cảm (vd: ELEVENLABS_API_KEY) — nhập qua CLI/Web,
    không cần sửa file tay. File .env luôn ở chế độ 0600.
    """
    import os

    p = Path(path) if path else ENV_FILE
    lines: list[str] = []
    found = False
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def save_user_settings(patch: dict, path: Path | str | None = None) -> dict:
    """Ghi đè từng phần settings.yaml của user (giữ các key khác).

    Chỉ ghi những gì user chỉnh (vd: {"elevenlabs": {"default_voice": "Rachel"}}),
    không dump toàn bộ DEFAULT_SETTINGS ra file.
    """
    p = Path(path) if path else SETTINGS_FILE
    current: dict = {}
    if p.exists():
        with open(p, encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}
    _deep_merge(current, patch)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(current, f, allow_unicode=True, sort_keys=False)
    return current


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


def get_sync_key() -> str:
    """Lấy Sync Labs API key từ biến môi trường hoặc file .env ở repo root."""
    key = os.environ.get("SYNC_API_KEY", "").strip()
    if key:
        return key
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SYNC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

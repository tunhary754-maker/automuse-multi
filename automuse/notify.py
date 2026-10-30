"""Thông báo Telegram khi build hết acc dùng được.

Không có connector Telegram sẵn trong hệ thống nên gọi trực tiếp Telegram
Bot API bằng stdlib (urllib, không cần thêm dependency). Người dùng tạo
bot qua @BotFather lấy token, chat với bot rồi lấy chat ID (vd: qua
@userinfobot hoặc GET /bot<token>/getUpdates). Token + chat ID lưu trong
.env (0600, gitignored) như ElevenLabs/Sync key — KHÔNG lưu vào
settings.yaml hay accounts.json.

Dùng khi build hết acc thật giữa chừng: pipeline báo cho user biết để
thêm acc mới, rồi tự làm tiếp khi thấy acc mới (xem pipeline.py).
Gửi lỗi không bao giờ raise — thông báo hỏng thì build vẫn chạy tiếp.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .config import ENV_FILE, REPO_ROOT, save_env_key

API_BASE = "https://api.telegram.org"


def _read_env_value(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if value:
        return value
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def telegram_config() -> tuple[str, str]:
    """Trả (bot_token, chat_id), chuỗi rỗng nếu chưa cấu hình."""
    return (_read_env_value("TELEGRAM_BOT_TOKEN"),
            _read_env_value("TELEGRAM_CHAT_ID"))


def telegram_configured() -> bool:
    token, chat_id = telegram_config()
    return bool(token and chat_id)


def save_telegram_config(token: str, chat_id: str,
                         path: Path | str | None = None) -> Path:
    """Lưu token + chat ID vào .env (0600). Trả path của file .env."""
    p = Path(path) if path else ENV_FILE
    save_env_key("TELEGRAM_BOT_TOKEN", token, path=p)
    save_env_key("TELEGRAM_CHAT_ID", chat_id, path=p)
    return p


def _api_call(token: str, method: str, payload: dict,
              timeout: float = 30.0) -> dict:
    url = f"{API_BASE}/bot{token}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 - thông báo không được làm hỏng build
        return {"ok": False, "description": str(e)}


def check_bot_token(token: str) -> tuple[bool, str]:
    """Kiểm tra token qua getMe (không gửi tin nhắn). Trả (ok, tên bot/lỗi)."""
    result = _api_call(token, "getMe", {})
    if result.get("ok"):
        username = (result.get("result") or {}).get("username", "")
        return True, f"@{username}" if username else "bot"
    return False, result.get("description", "không rõ lỗi")


def send_telegram_to(token: str, chat_id: str, text: str,
                     timeout: float = 30.0) -> bool:
    """Gửi tin nhắn với token/chat truyền trực tiếp (để kiểm tra trước
    khi lưu — tránh động vào os.environ chung của server đa luồng)."""
    if not token or not chat_id:
        return False
    result = _api_call(token, "sendMessage",
                       {"chat_id": chat_id, "text": text}, timeout=timeout)
    return bool(result.get("ok"))


def send_telegram(text: str, timeout: float = 30.0) -> bool:
    """Gửi một tin nhắn Telegram theo cấu hình đã lưu. Trả True nếu ok."""
    token, chat_id = telegram_config()
    return send_telegram_to(token, chat_id, text, timeout=timeout)

"""Theo dõi mức dùng từng tài khoản theo tuần + giới hạn tuần.

Muse không có endpoint quota/token công khai nào đọc được (đã dò thử
nhiều path /api/* — tất cả đều bị auth chặn trước khi routing), nên
giới hạn tuần được thực thi cục bộ: mỗi clip sinh thành công ghi một
sự kiện vào cache/usage.json, tuần tính theo lịch (thứ Hai 00:00 ->
thứ Hai tuần sau, giờ địa phương). Hết giới hạn thì pipeline không
dùng acc đó nữa (đổi acc khác, hoặc dừng build với thông báo rõ ràng).

Giới hạn mặc định nằm ở settings["limits"]:
  weekly_max_videos: số clip tối đa mỗi tuần (0 = không giới hạn)
  weekly_max_minutes: tổng số phút clip tối đa mỗi tuần (0 = không giới hạn)
Mỗi tài khoản có thể ghi đè riêng bằng Account.weekly_max_videos /
Account.weekly_max_minutes (0 = dùng giới hạn chung).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import DEFAULT_CACHE_DIR

USAGE_FILE = DEFAULT_CACHE_DIR / "usage.json"


def week_start(now: datetime | None = None) -> datetime:
    """0h00 thứ Hai của tuần chứa `now` (giờ địa phương)."""
    now = now or datetime.now()
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def week_label(now: datetime | None = None) -> str:
    """Nhãn tuần, vd: '27/10 – 02/11/2026'."""
    start = week_start(now)
    end = start + timedelta(days=6)
    return f"{start:%d/%m} – {end:%d/%m/%Y}"


class UsageTracker:
    """Ghi/đọc mức dùng từ file JSON (cache/usage.json)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else USAGE_FILE
        self._events: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._events = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._events = data if isinstance(data, list) else []
        except (ValueError, OSError):
            self._events = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._events, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def record(
        self,
        account: str,
        kind: str = "video",
        duration_s: float = 0.0,
        project: str = "",
        ts: float | None = None,
    ) -> None:
        """Ghi một lần dùng (một clip video / một ảnh đã sinh xong)."""
        self._events.append(
            {
                "ts": ts if ts is not None else time.time(),
                "account": account,
                "kind": kind,
                "duration_s": float(duration_s),
                "project": project,
            }
        )
        self._save()

    def weekly(self, account: str, now: datetime | None = None) -> dict:
        """Tổng dùng của `account` trong tuần hiện tại."""
        start = week_start(now).timestamp()
        events = [
            e
            for e in self._events
            if e.get("account") == account and float(e.get("ts", 0)) >= start
        ]
        videos = sum(1 for e in events if e.get("kind") == "video")
        images = sum(1 for e in events if e.get("kind") == "image")
        minutes = sum(float(e.get("duration_s", 0)) for e in events) / 60.0
        return {"videos": videos, "images": images, "minutes": round(minutes, 1)}

    def limits_for(self, account, settings: dict) -> tuple[int, int]:
        """(max_videos, max_minutes) — ưu tiên giới hạn riêng của acc."""
        limits = settings.get("limits", {}) if settings else {}
        max_videos = int(limits.get("weekly_max_videos", 0) or 0)
        max_minutes = int(limits.get("weekly_max_minutes", 0) or 0)
        per_acc_v = int(getattr(account, "weekly_max_videos", 0) or 0)
        per_acc_m = int(getattr(account, "weekly_max_minutes", 0) or 0)
        if per_acc_v:
            max_videos = per_acc_v
        if per_acc_m:
            max_minutes = per_acc_m
        return max_videos, max_minutes

    def remaining(self, account, settings: dict) -> dict:
        """Mức dùng tuần này + còn lại bao nhiêu (None = không giới hạn)."""
        name = account.name if hasattr(account, "name") else str(account)
        used = self.weekly(name)
        max_videos, max_minutes = self.limits_for(account, settings)
        videos_left = None if max_videos <= 0 else max(0, max_videos - used["videos"])
        minutes_left = (
            None if max_minutes <= 0 else round(max(0.0, max_minutes - used["minutes"]), 1)
        )
        return {
            "account": name,
            "week": week_label(),
            "videos": used["videos"],
            "images": used["images"],
            "minutes": used["minutes"],
            "max_videos": max_videos,
            "max_minutes": max_minutes,
            "videos_left": videos_left,
            "minutes_left": minutes_left,
        }

    def has_budget(self, account, settings: dict) -> bool:
        """Còn quota tuần để sinh thêm một clip không?"""
        r = self.remaining(account, settings)
        if r["videos_left"] is not None and r["videos_left"] <= 0:
            return False
        if r["minutes_left"] is not None and r["minutes_left"] <= 0:
            return False
        return True

    def summary(self, accounts, settings: dict) -> list[dict]:
        """Mức dùng tuần này của nhiều acc, dùng cho CLI/Web."""
        return [self.remaining(a, settings) for a in accounts]

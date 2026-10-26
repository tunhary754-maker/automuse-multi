"""Quản lý nhiều tài khoản Muse: cookie + proxy riêng từng acc.

Cookie có thể nhập bằng 3 cách:
  1. Chuỗi header Cookie thô:  "sessionid=abc; csrftoken=xyz"
  2. File cookies.txt chuẩn Netscape (export từ extension trình duyệt)
  3. File JSON: [{"name": ..., "value": ...}] hoặc {"name": "value"}

Mỗi tài khoản có thể gắn 1 proxy riêng (http/https/socks5), dùng cho mọi
request của acc đó. File accounts.json được lưu với quyền 0600.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import ACCOUNTS_FILE


# ---------------------------------------------------------------- cookie utils

def parse_cookie_header(raw: str) -> dict[str, str]:
    """'a=1; b=2' -> {'a': '1', 'b': '2'} (bỏ phần 'Cookie:' nếu có)."""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:"):].strip()
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        out[name.strip()] = value.strip()
    return out


def parse_netscape_file(path: Path | str) -> dict[str, str]:
    """Đọc file cookies.txt chuẩn Netscape (7 cột, tab-separated)."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) >= 7:
            out[cols[5].strip()] = cols[6].strip()
        elif "=" in line:  # fallback: dòng dạng name=value
            name, value = line.split("=", 1)
            out[name.strip()] = value.strip()
    return out


def parse_cookie_json(data) -> dict[str, str]:
    """JSON export: list[{name,value}] hoặc dict {name: value}."""
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        out: dict[str, str] = {}
        for item in data:
            if isinstance(item, dict) and "name" in item and "value" in item:
                out[str(item["name"])] = str(item["value"])
        return out
    raise ValueError("Định dạng JSON cookie không hỗ trợ")


def load_cookies(source: str) -> dict[str, str]:
    """Tự nhận diện: đường dẫn file (.txt/.json) hoặc chuỗi cookie thô."""
    p = Path(source)
    if p.exists():
        if p.suffix.lower() == ".json":
            return parse_cookie_json(json.loads(p.read_text(encoding="utf-8")))
        return parse_netscape_file(p)
    if source.strip().startswith(("{", "[")):
        return parse_cookie_json(json.loads(source))
    return parse_cookie_header(source)


def cookies_to_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# ---------------------------------------------------------------- proxy

@dataclass
class ProxyConfig:
    raw: str = ""
    scheme: str = ""
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""

    @classmethod
    def parse(cls, raw: str) -> "ProxyConfig":
        raw = (raw or "").strip()
        if not raw:
            return cls()
        if "://" not in raw:
            raw = "http://" + raw
        u = urlparse(raw)
        return cls(
            raw=raw,
            scheme=(u.scheme or "http").lower(),
            host=u.hostname or "",
            port=u.port or 0,
            username=u.username or "",
            password=u.password or "",
        )

    def is_valid(self) -> bool:
        return bool(self.host and self.port)

    def to_requests(self) -> dict[str, str]:
        """Dict proxies cho requests. socks5 cần gói PySocks (pip install pysocks)."""
        if not self.is_valid():
            return {}
        return {"http": self.raw, "https": self.raw}

    def masked(self) -> str:
        if not self.is_valid():
            return "-"
        auth = f"{self.username}:***@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def test(self, timeout: int = 10) -> tuple[bool, str]:
        """Kiểm tra proxy có dùng được không (qua httpbin.org/ip)."""
        if not self.is_valid():
            return False, "proxy không hợp lệ"
        try:
            r = requests.get(
                "https://httpbin.org/ip",
                proxies=self.to_requests(),
                timeout=timeout,
            )
            if r.status_code == 200:
                return True, f"OK, IP qua proxy: {r.json().get('origin')}"
            return False, f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001 - báo lỗi gọn cho CLI
            return False, str(e)


# ---------------------------------------------------------------- account

@dataclass
class Account:
    name: str
    cookie: str = ""          # chuỗi header Cookie thô
    cookie_file: str = ""     # hoặc đường dẫn file cookies.txt / .json
    proxy: str = ""           # vd: http://user:pass@host:port
    enabled: bool = True
    note: str = ""
    jobs_done: int = 0
    jobs_failed: int = 0
    last_used: float = 0.0
    cooldown_until: float = 0.0

    def cookies(self) -> dict[str, str]:
        if self.cookie_file:
            return load_cookies(self.cookie_file)
        return parse_cookie_header(self.cookie)

    def proxy_config(self) -> ProxyConfig:
        return ProxyConfig.parse(self.proxy)

    def is_on_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    def to_public_dict(self) -> dict:
        d = asdict(self)
        d["cookie"] = "***" if self.cookie else ""
        d["proxy"] = self.proxy_config().masked()
        return d


class AccountManager:
    """Load/save accounts.json + chọn acc theo vòng tròn (round-robin)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else ACCOUNTS_FILE
        self.accounts: list[Account] = []
        self._rr_index = 0
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.accounts = []
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        items = data.get("accounts", []) if isinstance(data, dict) else data
        self.accounts = [Account(**a) for a in items]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"accounts": [asdict(a) for a in self.accounts]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> Account | None:
        return next((a for a in self.accounts if a.name == name), None)

    def add(self, account: Account) -> None:
        if self.get(account.name):
            raise ValueError(f"Đã có tài khoản tên '{account.name}'")
        if not account.cookies():
            raise ValueError("Cookie rỗng — kiểm tra lại cookie/file cookie")
        self.accounts.append(account)
        self.save()

    def remove(self, name: str) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.name != name]
        if len(self.accounts) != before:
            self.save()
            return True
        return False

    def enabled_accounts(self) -> list[Account]:
        return [a for a in self.accounts if a.enabled]

    def pick(self, names: list[str] | None = None) -> Account | None:
        """Round-robin: acc enabled, không cooldown, ít job nhất thì ưu tiên."""
        pool = self.enabled_accounts()
        if names:
            wanted = set(names)
            pool = [a for a in pool if a.name in wanted]
        pool = [a for a in pool if not a.is_on_cooldown()]
        if not pool:
            return None
        pool.sort(key=lambda a: (a.jobs_done, a.last_used))
        acc = pool[self._rr_index % len(pool)]
        self._rr_index += 1
        return acc

    def mark_success(self, account: Account) -> None:
        account.jobs_done += 1
        account.jobs_failed = 0
        account.last_used = time.time()
        self.save()

    def mark_failure(self, account: Account, cooldown_seconds: int = 300) -> None:
        account.jobs_failed += 1
        account.last_used = time.time()
        if account.jobs_failed >= 3:
            account.cooldown_until = time.time() + cooldown_seconds
        self.save()

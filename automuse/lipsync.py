"""Khớp môi (lip-sync) hậu kỳ bằng Sync Labs (sync.so).

Muse sinh clip từ prompt chữ, không nhận file audio đầu vào, nên môi
nhân vật không khớp với giọng ElevenLabs. Bước này chạy sau khi sinh
clip: đưa clip + file audio TTS của cảnh vào Sync Labs, nó vẽ lại miệng
khớp từng âm rồi trả về clip mới.

- SyncLipSyncClient (thật): gọi api.sync.so/v2, key ở biến môi trường
  SYNC_API_KEY hoặc file .env. Upload asset 3 bước (presign -> PUT
  bytes -> đăng ký asset), rồi POST /v2/generate, poll tới COMPLETED.
- MockSyncLipSyncClient: không cần key, chép nguyên clip vào đích để
  chạy thử pipeline.
"""

from __future__ import annotations

import mimetypes
import shutil
import time
from pathlib import Path

import requests

SYNC_BASE = "https://api.sync.so/v2"
KNOWN_MODELS = ("lipsync-2", "lipsync-2-pro", "sync-3", "react-1")


class SyncError(Exception):
    pass


def _snippet(text: str, limit: int = 300) -> str:
    return (text or "")[:limit]


class SyncLipSyncClient:
    def __init__(
        self,
        api_key: str,
        model: str = "lipsync-2",
        sync_mode: str = "cut_off",
        session=None,
        base_url: str = SYNC_BASE,
        poll_interval: float = 5,
        poll_timeout: float = 600,
    ):
        if not api_key:
            raise SyncError("Thiếu SYNC_API_KEY")
        if model not in KNOWN_MODELS:
            raise SyncError(
                f"Model lip-sync '{model}' không hợp lệ "
                f"({', '.join(KNOWN_MODELS)})"
            )
        self.api_key = api_key
        self.model = model
        self.sync_mode = sync_mode
        self.base_url = base_url.rstrip("/")
        self.poll_interval = float(poll_interval)
        self.poll_timeout = float(poll_timeout)
        self.session = session or requests.Session()
        self.session.headers.update({"x-api-key": api_key})

    # -- key --------------------------------------------------------
    def check_key(self) -> bool:
        """GET /v2/models — 200 là key đúng, 401 là key sai."""
        r = self.session.get(f"{self.base_url}/models", timeout=30)
        if r.status_code == 200:
            return True
        if r.status_code == 401:
            return False
        raise SyncError(
            f"Kiểm tra key Sync Labs lỗi HTTP {r.status_code}: "
            f"{_snippet(r.text)}"
        )

    # -- upload asset (3 bước) --------------------------------------
    def _upload_asset(self, path: str | Path, asset_type: str) -> str:
        """Upload 1 file local thành asset, trả về asset id."""
        p = Path(path)
        if not p.exists():
            raise SyncError(f"Không thấy file: {p}")
        content_type = mimetypes.guess_type(str(p))[0] or (
            "video/mp4" if asset_type == "VIDEO" else "audio/mpeg"
        )
        data = p.read_bytes()
        # 1) Xin URL upload (presign)
        r = self.session.post(
            f"{self.base_url}/assets/upload",
            json={"fileName": p.name, "contentType": content_type,
                  "size": len(data)},
            timeout=60,
        )
        if r.status_code != 201:
            raise SyncError(
                f"Xin URL upload lỗi HTTP {r.status_code}: {_snippet(r.text)}"
            )
        presigned = r.json()
        upload_url = presigned.get("uploadUrl")
        url = presigned.get("url")
        if not upload_url or not url:
            raise SyncError(
                f"Sync Labs không trả uploadUrl: {_snippet(r.text)}"
            )
        # 2) PUT bytes thô lên uploadUrl — KHÔNG gửi x-api-key.
        put = requests.put(
            upload_url, data=data,
            headers={"Content-Type": content_type}, timeout=300,
        )
        if put.status_code not in (200, 201, 204):
            raise SyncError(
                f"Upload file lên Sync Labs lỗi HTTP {put.status_code}: "
                f"{_snippet(put.text)}"
            )
        # 3) Đăng ký asset
        r = self.session.post(
            f"{self.base_url}/assets",
            json={"url": url, "type": asset_type, "name": p.name},
            timeout=60,
        )
        if r.status_code != 201:
            raise SyncError(
                f"Đăng ký asset lỗi HTTP {r.status_code}: {_snippet(r.text)}"
            )
        asset_id = r.json().get("id")
        if not asset_id:
            raise SyncError(f"Sync Labs không trả asset id: {_snippet(r.text)}")
        return str(asset_id)

    # -- generate + poll --------------------------------------------
    def sync(
        self, video_path: str | Path, audio_path: str | Path,
        out_path: str | Path,
    ) -> Path:
        """Khớp môi 1 cặp clip + audio, tải kết quả về out_path."""
        video_id = self._upload_asset(video_path, "VIDEO")
        audio_id = self._upload_asset(audio_path, "AUDIO")
        r = self.session.post(
            f"{self.base_url}/generate",
            json={
                "model": self.model,
                "input": [
                    {"type": "video", "assetId": video_id},
                    {"type": "audio", "assetId": audio_id},
                ],
                "options": {"sync_mode": self.sync_mode},
            },
            timeout=60,
        )
        if r.status_code not in (200, 201):
            raise SyncError(
                f"Tạo job lip-sync lỗi HTTP {r.status_code}: {_snippet(r.text)}"
            )
        job_id = r.json().get("id")
        if not job_id:
            raise SyncError(f"Sync Labs không trả job id: {_snippet(r.text)}")
        output_url = self._poll(job_id)
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dl = requests.get(output_url, timeout=300, stream=True)
        if dl.status_code != 200:
            raise SyncError(
                f"Tải kết quả lip-sync lỗi HTTP {dl.status_code}: "
                f"{_snippet(dl.text)}"
            )
        with open(dest, "wb") as f:
            for chunk in dl.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        return dest

    def _poll(self, job_id: str) -> str:
        """Poll GET /v2/generate/{id} tới COMPLETED, trả outputUrl."""
        deadline = time.time() + self.poll_timeout
        last_status = ""
        while time.time() < deadline:
            r = self.session.get(
                f"{self.base_url}/generate/{job_id}", timeout=60
            )
            if r.status_code != 200:
                raise SyncError(
                    f"Poll job lip-sync lỗi HTTP {r.status_code}: "
                    f"{_snippet(r.text)}"
                )
            data = r.json()
            status = str(data.get("status", "")).upper()
            last_status = status
            if status == "COMPLETED":
                output_url = data.get("outputUrl")
                if not output_url:
                    raise SyncError(
                        f"Job lip-sync xong nhưng không có outputUrl: "
                        f"{_snippet(r.text)}"
                    )
                return str(output_url)
            if status in ("FAILED", "REJECTED"):
                raise SyncError(
                    f"Lip-sync thất bại ({status}): {_snippet(r.text)}"
                )
            time.sleep(self.poll_interval)
        raise SyncError(
            f"Hết thời gian chờ lip-sync ({self.poll_timeout:.0f}s, "
            f"trạng thái cuối: {last_status or '?'})"
        )


class MockSyncLipSyncClient:
    """Chép nguyên clip vào đích — chạy thử không cần key Sync Labs."""

    def __init__(self, **_kwargs):
        pass

    def check_key(self) -> bool:
        return True

    def sync(
        self, video_path: str | Path, audio_path: str | Path,
        out_path: str | Path,
    ) -> Path:
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(video_path), dest)
        return dest


def build_lipsync(api_key: str, settings: dict, mock: bool = False,
                  session=None):
    """Factory: mock (hoặc chưa có key) -> mock client, còn lại -> thật."""
    cfg = settings.get("lipsync", {})
    if mock or not api_key:
        return MockSyncLipSyncClient()
    return SyncLipSyncClient(
        api_key,
        model=cfg.get("model", "lipsync-2"),
        sync_mode=cfg.get("sync_mode", "cut_off"),
        session=session,
        poll_timeout=float(cfg.get("poll_timeout", 600)),
    )

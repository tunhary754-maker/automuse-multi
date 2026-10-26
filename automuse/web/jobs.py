"""Job store trong bộ nhớ cho Web Studio: trạng thái + log từng lần build."""

from __future__ import annotations

import time

_JOBS: dict[str, dict] = {}


def create(job_id: str, params: dict) -> dict:
    job = {
        "id": job_id,
        "status": "queued",
        "script": params.get("script", ""),
        "mock": bool(params.get("mock")),
        "accounts": params.get("accounts") or [],
        "log": [],
        "manifest": None,
        "error": None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": None,
        "finished_at": None,
    }
    _JOBS[job_id] = job
    return job


def get(job_id: str) -> dict | None:
    return _JOBS.get(job_id)


def list_all() -> list[dict]:
    return sorted(_JOBS.values(), key=lambda j: j["created_at"], reverse=True)

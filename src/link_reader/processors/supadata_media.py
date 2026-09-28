from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from link_reader.types import TranscriptSegment


def supadata_key(settings) -> str | None:
    value = getattr(settings, "supadata_api_key", None)
    if value:
        return str(value).strip() or None
    for path in (Path("/data/supadata.key"), Path("data/supadata.key")):
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    return None


def fetch_media_transcript(settings, url: str, *, timeout_seconds: int = 300):
    key = supadata_key(settings)
    if not key:
        raise RuntimeError("Supadata אינו מוגדר.")

    params = urllib.parse.urlencode({"url": url, "text": "false"})
    endpoint = "https://api.supadata.ai/v1/transcript?" + params
    req = urllib.request.Request(
        endpoint,
        headers={"x-api-key": key, "User-Agent": "link-reader-bot/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            status = response.status
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read(800).decode(errors="ignore")
        raise RuntimeError(f"Supadata transcript failed ({exc.code}): {body[:300]}") from exc

    if status == 202 or data.get("jobId"):
        job_id = data.get("jobId")
        if not job_id:
            raise RuntimeError("Supadata returned no job ID.")
        data = _poll_job(key, job_id, timeout_seconds)

    content = data.get("content")
    language = data.get("lang")
    if isinstance(content, str):
        text = content.strip()
        return ([TranscriptSegment(0.0, 0.0, text)] if text else []), language
    if not isinstance(content, list):
        return [], language

    segments = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        offset_ms = float(item.get("offset") or 0)
        duration_ms = float(item.get("duration") or 0)
        segments.append(
            TranscriptSegment(
                start=offset_ms / 1000.0,
                duration=duration_ms / 1000.0,
                text=text,
            )
        )
    return segments, language


def _poll_job(key: str, job_id: str, timeout_seconds: int) -> dict:
    endpoint = "https://api.supadata.ai/v1/transcript/" + urllib.parse.quote(job_id, safe="")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        req = urllib.request.Request(
            endpoint,
            headers={"x-api-key": key, "User-Agent": "link-reader-bot/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.load(response)
        status = data.get("status")
        if status == "completed":
            return data
        if status == "failed":
            raise RuntimeError(f"Supadata transcript job failed: {data.get('error')}")
        time.sleep(1)
    raise RuntimeError("Supadata transcript job timed out.")

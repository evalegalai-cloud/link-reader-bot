from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urlsplit

from link_reader.processors.supadata_media import fetch_media_transcript, supadata_key
from link_reader.processors.webpage import WebPageProcessor
from link_reader.types import ExtractedContent


_SOCIAL_HOSTS = {
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch",
}


class SocialVideoProcessor:
    source_type = "social_video"

    def __init__(self, settings):
        self.settings = settings
        self._web_safety = WebPageProcessor(settings)

    def supports(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return parsed.scheme.lower() in {"http", "https"} and (parsed.hostname or "").lower() in _SOCIAL_HOSTS
        except ValueError:
            return False

    def external_id(self, url: str) -> str:
        normalized = self._web_safety._normalize_url(url)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    async def extract(self, url: str) -> ExtractedContent:
        return await asyncio.to_thread(self._extract_sync, url)

    def _extract_sync(self, url: str) -> ExtractedContent:
        normalized = self._web_safety._normalize_url(url)
        self._web_safety._validate_public_url(normalized)
        if not supadata_key(self.settings):
            raise ValueError("תמלול קישורי רשת חברתית דורש Supadata.")
        segments, language = fetch_media_transcript(self.settings, normalized)
        if not segments:
            raise ValueError("לא נמצא דיבור בקישור הזה.")
        duration = max(seg.start + seg.duration for seg in segments)
        parsed = urlsplit(normalized)
        host = (parsed.hostname or "social").removeprefix("www.")
        identifier = parsed.path.strip("/").split("/")[-1] or "post"
        return ExtractedContent(
            external_id=self.external_id(normalized),
            source_type=self.source_type,
            url=normalized,
            title=f"{host} · {identifier}"[:500],
            author=None,
            duration_seconds=duration,
            language=language,
            segments=segments,
            extraction_method="supadata_generated",
        )

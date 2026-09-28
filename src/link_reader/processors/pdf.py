from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx
from pypdf import PdfReader

from link_reader.processors.webpage import WebPageProcessor
from link_reader.types import ExtractedContent, TranscriptSegment


class PDFProcessor:
    source_type = "pdf"
    max_bytes = 30 * 1024 * 1024
    max_pages = 1200

    def __init__(self, settings):
        self.settings = settings
        self._web_safety = WebPageProcessor(settings)

    def supports(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme.lower() in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.path.lower().endswith(".pdf")
            )
        except ValueError:
            return False

    def external_id(self, url: str) -> str:
        normalized = self._web_safety._normalize_url(url)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    async def extract(self, url: str) -> ExtractedContent:
        return await asyncio.to_thread(self._extract_sync, url)

    def _extract_sync(self, url: str) -> ExtractedContent:
        normalized = self._web_safety._normalize_url(url)
        final_url, data = self._fetch_pdf(normalized)
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
        except Exception as exc:
            raise ValueError("לא הצלחתי לקרוא את קובץ ה-PDF.") from exc

        if len(reader.pages) > self.max_pages:
            raise ValueError("ה-PDF ארוך מדי לעיבוד כרגע.")

        segments: list[TranscriptSegment] = []
        nonempty_pages = 0
        total_chars = 0
        for page_no, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:
                text = ""
            text = self._clean_page(text)
            if not text:
                continue
            nonempty_pages += 1
            total_chars += len(text)
            for part in self._split_page(text):
                segments.append(
                    TranscriptSegment(
                        start=float(page_no - 1),
                        duration=1.0,
                        text=part,
                        reference=f"p.{page_no}",
                    )
                )

        if not segments or total_chars < 120:
            raise ValueError("ה-PDF כנראה סרוק ואין בו שכבת טקסט מספקת. OCR יתווסף בהמשך.")

        metadata = reader.metadata or {}
        title = str(getattr(metadata, "title", "") or "").strip()
        author = str(getattr(metadata, "author", "") or "").strip() or None
        if not title:
            name = PurePosixPath(unquote(urlsplit(final_url).path)).name
            title = name.rsplit(".", 1)[0] if name else "PDF"

        return ExtractedContent(
            external_id=self.external_id(normalized),
            source_type=self.source_type,
            url=final_url,
            title=title[:500],
            author=author[:300] if author else None,
            duration_seconds=None,
            language=None,
            segments=segments,
            extraction_method="pypdf",
        )

    def _fetch_pdf(self, url: str) -> tuple[str, bytes]:
        current = url
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; LinkReaderBot/1.0; +https://github.com/evalegalai-cloud/link-reader-bot)",
            "Accept": "application/pdf,*/*;q=0.5",
        }
        with httpx.Client(timeout=httpx.Timeout(35.0, connect=8.0), headers=headers) as client:
            for _ in range(self._web_safety.max_redirects + 1):
                self._web_safety._validate_public_url(current)
                with client.stream("GET", current, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            response.raise_for_status()
                        from urllib.parse import urljoin
                        current = self._web_safety._normalize_url(urljoin(current, location))
                        continue
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise ValueError("קובץ ה-PDF גדול מדי.")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise ValueError("קובץ ה-PDF גדול מדי.")
                    raw = bytes(data)
                    content_type = (response.headers.get("content-type") or "").lower()
                    if not raw.startswith(b"%PDF-") and "pdf" not in content_type:
                        raise ValueError("הקישור אינו מחזיר קובץ PDF.")
                    return str(response.url), raw
        raise ValueError("יותר מדי הפניות בדרך ל-PDF.")

    def _clean_page(self, text: str) -> str:
        lines = [" ".join(line.split()) for line in text.replace("\r", "\n").split("\n")]
        return "\n".join(line for line in lines if line)

    def _split_page(self, text: str, max_chars: int = 3500) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        parts: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= max_chars:
                parts.append(remaining.strip())
                break
            cut = remaining.rfind("\n", 0, max_chars)
            if cut < max_chars // 2:
                cut = remaining.rfind(". ", 0, max_chars)
            if cut < max_chars // 2:
                cut = remaining.rfind(" ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            else:
                cut += 1
            parts.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        return [part for part in parts if part]

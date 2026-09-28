from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from trafilatura import bare_extraction

from link_reader.types import ExtractedContent, TranscriptSegment


_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
_TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "fbclid", "mc_cid", "mc_eid",
}


class WebPageProcessor:
    source_type = "web"
    max_bytes = 8 * 1024 * 1024
    max_redirects = 5

    def __init__(self, settings):
        self.settings = settings

    def supports(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme.lower() in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.hostname.lower() not in _YOUTUBE_HOSTS
            )
        except ValueError:
            return False

    def external_id(self, url: str) -> str:
        normalized = self._normalize_url(url)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    async def extract(self, url: str) -> ExtractedContent:
        return await asyncio.to_thread(self._extract_sync, url)

    def _extract_sync(self, url: str) -> ExtractedContent:
        requested = self._normalize_url(url)
        final_url, html = self._fetch_html(requested)
        doc = bare_extraction(
            html,
            url=final_url,
            include_comments=False,
            include_tables=True,
            include_links=False,
            with_metadata=True,
            deduplicate=True,
            favor_precision=False,
            favor_recall=False,
        )
        if doc is None or not (doc.text or "").strip():
            raise ValueError("לא הצלחתי לחלץ טקסט מהעמוד.")

        text = self._clean_text(doc.text)
        if len(text) < 180:
            raise ValueError("לא מצאתי מספיק תוכן לקריאה בעמוד.")

        paragraphs = self._paragraphs(text)
        if doc.title:
            title_norm = " ".join(doc.title.split()).casefold()
            paragraphs = [p for p in paragraphs if " ".join(p.split()).casefold() != title_norm]
        segments = [
            TranscriptSegment(
                start=float(i - 1),
                duration=1.0,
                text=paragraph,
                reference=f"§{i}",
            )
            for i, paragraph in enumerate(paragraphs, start=1)
        ]
        if not segments:
            raise ValueError("לא מצאתי תוכן לקריאה בעמוד.")

        title = (doc.title or "").strip() or (urlsplit(final_url).hostname or "Web page")
        author = (doc.author or "").strip() or None
        language = (getattr(doc, "language", None) or "").strip() or None

        return ExtractedContent(
            external_id=self.external_id(requested),
            source_type=self.source_type,
            url=final_url,
            title=title[:500],
            author=author[:300] if author else None,
            duration_seconds=None,
            language=language,
            segments=segments,
            extraction_method="trafilatura",
        )

    def _fetch_html(self, url: str) -> tuple[str, str]:
        current = url
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; LinkReaderBot/1.0; +https://github.com/evalegalai-cloud/link-reader-bot)",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        }
        with httpx.Client(timeout=httpx.Timeout(25.0, connect=8.0), headers=headers) as client:
            for _ in range(self.max_redirects + 1):
                self._validate_public_url(current)
                with client.stream("GET", current, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            response.raise_for_status()
                        current = self._normalize_url(urljoin(current, location))
                        continue
                    response.raise_for_status()
                    content_type = (response.headers.get("content-type") or "").lower()
                    if "pdf" in content_type:
                        raise ValueError("זה קובץ PDF. תמיכה ב-PDF תתווסף בשלב הבא.")
                    if content_type and not any(x in content_type for x in ("html", "xhtml", "text/plain")):
                        raise ValueError("הקישור אינו עמוד טקסט נתמך.")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise ValueError("העמוד גדול מדי לעיבוד.")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise ValueError("העמוד גדול מדי לעיבוד.")
                    encoding = response.encoding or "utf-8"
                    return str(response.url), bytes(data).decode(encoding, errors="replace")
        raise ValueError("יותר מדי הפניות בדרך לעמוד.")

    def _validate_public_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("קישור לא תקין.")
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"}:
            raise ValueError("כתובת מקומית אינה נתמכת.")
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("לא הצלחתי למצוא את האתר.") from exc
        if not infos:
            raise ValueError("לא הצלחתי למצוא את האתר.")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise ValueError("כתובת מקומית או פרטית אינה נתמכת.")

    def _normalize_url(self, url: str) -> str:
        parsed = urlsplit(url.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("קישור לא תקין.")
        host = parsed.hostname.lower()
        port = parsed.port
        default_port = (parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)
        netloc = host if port is None or default_port else f"{host}:{port}"
        query = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_KEYS
        ]
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", urlencode(query), ""))

    def _clean_text(self, text: str) -> str:
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n")]
        return "\n".join(line for line in lines if line)

    def _paragraphs(self, text: str, max_chars: int = 2200) -> list[str]:
        raw = [line.strip() for line in text.split("\n") if line.strip()]
        result: list[str] = []
        for paragraph in raw:
            remaining = paragraph
            while len(remaining) > max_chars:
                cut = max(
                    remaining.rfind(". ", 0, max_chars),
                    remaining.rfind("? ", 0, max_chars),
                    remaining.rfind("! ", 0, max_chars),
                )
                if cut < max_chars // 2:
                    cut = remaining.rfind(" ", 0, max_chars)
                if cut < max_chars // 2:
                    cut = max_chars
                else:
                    cut += 1
                result.append(remaining[:cut].strip())
                remaining = remaining[cut:].strip()
            if remaining:
                result.append(remaining)
        return result

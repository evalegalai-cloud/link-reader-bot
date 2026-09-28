from __future__ import annotations

import asyncio
import hashlib
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from link_reader.processors.webpage import WebPageProcessor
from link_reader.types import ExtractedContent, TranscriptSegment


_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "ol", "p", "pre", "section", "table", "td",
    "th", "tr", "ul",
}
_SKIP_TAGS = {"script", "style", "svg", "math", "nav"}


class _EPUBHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.heading_parts: list[str] = []
        self._skip_depth = 0
        self._heading_depth = 0

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in {"h1", "h2", "h3", "title"}:
            self._heading_depth += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in {"h1", "h2", "h3", "title"} and self._heading_depth:
            self._heading_depth -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if self._skip_depth:
            return
        if data:
            self.parts.append(data)
            if self._heading_depth:
                self.heading_parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts).replace("\r", "\n")
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)

    def heading(self) -> str | None:
        value = " ".join(" ".join(self.heading_parts).split())
        return value[:300] or None


class EPUBProcessor:
    source_type = "epub"
    max_bytes = 60 * 1024 * 1024
    max_uncompressed_bytes = 250 * 1024 * 1024
    max_spine_items = 4000
    max_member_bytes = 20 * 1024 * 1024

    def __init__(self, settings):
        self.settings = settings
        self._web_safety = WebPageProcessor(settings)

    def supports(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme.lower() in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.path.lower().endswith(".epub")
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
        final_url, data = self._fetch_epub(normalized)
        title, author, segments = self._parse_epub(data)
        if not title:
            name = PurePosixPath(unquote(urlsplit(final_url).path)).name
            title = name.rsplit(".", 1)[0] if name else "EPUB"
        return ExtractedContent(
            external_id=self.external_id(normalized),
            source_type=self.source_type,
            url=final_url,
            title=title[:500],
            author=author[:300] if author else None,
            duration_seconds=None,
            language=None,
            segments=segments,
            extraction_method="epub_spine",
        )

    def _fetch_epub(self, url: str) -> tuple[str, bytes]:
        current = url
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; LinkReaderBot/1.0; +https://github.com/evalegalai-cloud/link-reader-bot)",
            "Accept": "application/epub+zip,application/zip;q=0.8,*/*;q=0.3",
        }
        with httpx.Client(timeout=httpx.Timeout(35.0, connect=8.0), headers=headers) as client:
            for _ in range(self._web_safety.max_redirects + 1):
                self._web_safety._validate_public_url(current)
                with client.stream("GET", current, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            response.raise_for_status()
                        current = self._web_safety._normalize_url(urljoin(current, location))
                        continue
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise ValueError("קובץ ה-EPUB גדול מדי.")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise ValueError("קובץ ה-EPUB גדול מדי.")
                    raw = bytes(data)
                    if not raw.startswith(b"PK"):
                        raise ValueError("הקישור אינו מחזיר קובץ EPUB תקין.")
                    return str(response.url), raw
        raise ValueError("יותר מדי הפניות בדרך ל-EPUB.")

    def _parse_epub(self, data: bytes) -> tuple[str | None, str | None, list[TranscriptSegment]]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("לא הצלחתי לקרוא את קובץ ה-EPUB.") from exc

        with archive:
            infos = archive.infolist()
            if sum(info.file_size for info in infos) > self.max_uncompressed_bytes:
                raise ValueError("ה-EPUB גדול מדי לאחר פתיחה.")
            names = set(archive.namelist())
            if "META-INF/container.xml" not in names:
                raise ValueError("מבנה ה-EPUB אינו תקין: חסר container.xml.")

            container = self._read_member(archive, "META-INF/container.xml")
            try:
                container_root = ET.fromstring(container)
                rootfile = next(
                    (node for node in container_root.iter() if self._local_name(node.tag) == "rootfile"),
                    None,
                )
                opf_path = (rootfile.attrib.get("full-path") if rootfile is not None else "") or ""
            except ET.ParseError as exc:
                raise ValueError("מבנה ה-EPUB אינו תקין.") from exc
            opf_path = self._safe_member_path(opf_path)
            if not opf_path or opf_path not in names:
                raise ValueError("מבנה ה-EPUB אינו תקין: קובץ התוכן הראשי חסר.")

            opf_bytes = self._read_member(archive, opf_path)
            try:
                opf = ET.fromstring(opf_bytes)
            except ET.ParseError as exc:
                raise ValueError("לא הצלחתי לקרוא את תוכן ה-EPUB.") from exc

            title = self._metadata_text(opf, "title")
            author = self._metadata_text(opf, "creator")
            opf_dir = posixpath.dirname(opf_path)

            manifest: dict[str, tuple[str, str]] = {}
            for node in opf.iter():
                if self._local_name(node.tag) != "item":
                    continue
                item_id = (node.attrib.get("id") or "").strip()
                href = (node.attrib.get("href") or "").strip()
                media_type = (node.attrib.get("media-type") or "").lower()
                if item_id and href:
                    manifest[item_id] = (href, media_type)

            spine_ids = [
                (node.attrib.get("idref") or "").strip()
                for node in opf.iter()
                if self._local_name(node.tag) == "itemref"
            ]
            spine_ids = [item_id for item_id in spine_ids if item_id]
            if len(spine_ids) > self.max_spine_items:
                raise ValueError("ה-EPUB מכיל יותר מדי פרקים לעיבוד.")

            segments: list[TranscriptSegment] = []
            chapter_no = 0
            for item_id in spine_ids:
                item = manifest.get(item_id)
                if not item:
                    continue
                href, media_type = item
                if media_type and media_type not in {"application/xhtml+xml", "text/html"}:
                    continue
                member = self._resolve_member(opf_dir, href)
                if member not in names:
                    continue
                raw = self._read_member(archive, member)
                text, heading = self._chapter_text(raw)
                if len(text) < 20:
                    continue
                chapter_no += 1
                if heading and not text.startswith(heading):
                    text = heading + "\n" + text
                for part in self._split_text(text):
                    segments.append(
                        TranscriptSegment(
                            start=float(chapter_no - 1),
                            duration=1.0,
                            text=part,
                            reference=f"ch.{chapter_no}",
                        )
                    )

            if not segments or sum(len(s.text) for s in segments) < 120:
                raise ValueError("לא מצאתי מספיק טקסט קריא ב-EPUB.")
            return title, author, segments

    def _read_member(self, archive: zipfile.ZipFile, name: str) -> bytes:
        safe = self._safe_member_path(name)
        if not safe:
            raise ValueError("נתיב פנימי לא תקין ב-EPUB.")
        try:
            info = archive.getinfo(safe)
        except KeyError as exc:
            raise ValueError("חסר קובץ פנימי ב-EPUB.") from exc
        if info.file_size > self.max_member_bytes:
            raise ValueError("פרק EPUB גדול מדי.")
        return archive.read(info)

    def _safe_member_path(self, name: str) -> str:
        name = (name or "").split("#", 1)[0].split("?", 1)[0].replace("\\", "/")
        normalized = posixpath.normpath(name).lstrip("/")
        if not normalized or normalized == "." or normalized.startswith("../") or "/../" in normalized:
            return ""
        return normalized

    def _resolve_member(self, base_dir: str, href: str) -> str:
        href = href.split("#", 1)[0].split("?", 1)[0]
        return self._safe_member_path(posixpath.join(base_dir, href))

    def _metadata_text(self, root: ET.Element, local_name: str) -> str | None:
        for node in root.iter():
            if self._local_name(node.tag) == local_name and (node.text or "").strip():
                return " ".join((node.text or "").split())
        return None

    def _local_name(self, tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    def _chapter_text(self, raw: bytes) -> tuple[str, str | None]:
        text = raw.decode("utf-8-sig", errors="replace")
        parser = _EPUBHTMLParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            pass
        return parser.text(), parser.heading()

    def _split_text(self, text: str, max_chars: int = 3500) -> list[str]:
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

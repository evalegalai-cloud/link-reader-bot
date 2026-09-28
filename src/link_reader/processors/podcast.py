from __future__ import annotations

import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from link_reader.processors.audio import AudioProcessor
from link_reader.processors.supadata_media import fetch_media_transcript, supadata_key
from link_reader.processors.webpage import WebPageProcessor
from link_reader.types import ExtractedContent


_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac", ".webm"}
_FEED_HINTS = ("/feed", "/rss", "podcast", "episode", "episodes", "/shows/", "/listen")
_PODCAST_HOST_HINTS = (
    "podcasts.apple.com",
    "open.spotify.com",
    "podcasters.spotify.com",
    "pca.st",
    "podbean.com",
    "buzzsprout.com",
    "libsyn.com",
    "simplecast.com",
    "captivate.fm",
    "transistor.fm",
    "megaphone.fm",
    "spreaker.com",
    "rss.com",
    "anchor.fm",
)


class _DiscoveryParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.feed_urls: list[str] = []
        self.audio_urls: list[str] = []
        self.title: str | None = None
        self.author: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        values = {str(k).lower(): str(v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        if tag == "link":
            rel = values.get("rel", "").lower()
            typ = values.get("type", "").lower()
            href = values.get("href", "")
            if href and "alternate" in rel and ("rss" in typ or "atom" in typ):
                self.feed_urls.append(href)
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "")
            if content and key in {
                "og:audio", "og:audio:url", "twitter:player:stream",
                "twitter:player:stream:url",
            }:
                self.audio_urls.append(content)
            if content and key in {"og:title", "twitter:title"} and not self.title:
                self.title = content.strip()
            if content and key in {"author", "article:author"} and not self.author:
                self.author = content.strip()
        elif tag in {"audio", "source"}:
            src = values.get("src", "")
            if src:
                self.audio_urls.append(src)
        elif tag == "a":
            href = values.get("href", "")
            if href:
                path = urlsplit(href).path.lower()
                if Path(path).suffix in _AUDIO_EXTENSIONS:
                    self.audio_urls.append(href)
                elif any(hint in path for hint in ("/feed", "/rss")) or path.endswith((".rss", ".xml")):
                    self.feed_urls.append(href)

    def handle_endtag(self, tag: str):
        if tag.lower() == "title":
            self._in_title = False
            if not self.title:
                text = " ".join("".join(self._title_parts).split())
                self.title = text or None

    def handle_data(self, data: str):
        if self._in_title:
            self._title_parts.append(data)


class PodcastProcessor:
    source_type = "podcast"
    max_bytes = 6 * 1024 * 1024

    def __init__(self, settings):
        self.settings = settings
        self._web_safety = WebPageProcessor(settings)
        self._audio = AudioProcessor(settings)

    def supports(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                return False
            host = parsed.hostname.lower()
            path = parsed.path.lower()
            query = parsed.query.lower()
            if host in _PODCAST_HOST_HINTS or any(host.endswith("." + h) for h in _PODCAST_HOST_HINTS):
                return True
            if path.endswith((".rss", ".xml")) or "feed=" in query:
                return True
            return any(hint in path for hint in _FEED_HINTS)
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

        # Hosted transcription providers can often resolve podcast platforms
        # directly. Prefer that path when available before HTML/RSS discovery.
        if supadata_key(self.settings):
            try:
                segments, language = fetch_media_transcript(self.settings, normalized)
                if segments:
                    duration = max(seg.start + seg.duration for seg in segments)
                    return ExtractedContent(
                        external_id=self.external_id(normalized),
                        source_type=self.source_type,
                        url=normalized,
                        title=self._fallback_title(normalized),
                        author=None,
                        duration_seconds=duration,
                        language=language,
                        segments=segments,
                        extraction_method="supadata_generated",
                    )
            except Exception:
                pass

        final_url, body, content_type = self._fetch(normalized)
        title = None
        author = None
        audio_url = None

        if self._looks_like_feed(body, content_type):
            episode = self._episode_from_feed(body, final_url)
            if episode:
                audio_url, title, author = episode
        else:
            parser = _DiscoveryParser()
            parser.feed(body)
            title = parser.title
            author = parser.author
            audio_url = self._first_media_url(parser.audio_urls, final_url)
            if not audio_url:
                for feed_href in parser.feed_urls[:4]:
                    feed_url = self._web_safety._normalize_url(urljoin(final_url, feed_href))
                    self._web_safety._validate_public_url(feed_url)
                    try:
                        _, feed_body, feed_type = self._fetch(feed_url)
                    except Exception:
                        continue
                    if not self._looks_like_feed(feed_body, feed_type):
                        continue
                    episode = self._episode_from_feed(feed_body, feed_url)
                    if episode:
                        audio_url, episode_title, episode_author = episode
                        title = episode_title or title
                        author = episode_author or author
                        break

        if not audio_url:
            raise ValueError("לא מצאתי פרק אודיו בעמוד או בפיד הפודקאסט.")

        item = self._audio._extract_sync(audio_url)
        return ExtractedContent(
            external_id=self.external_id(normalized),
            source_type=self.source_type,
            url=final_url,
            title=(title or item.title or self._fallback_title(final_url))[:500],
            author=(author or item.author)[:300] if (author or item.author) else None,
            duration_seconds=item.duration_seconds,
            language=item.language,
            segments=item.segments,
            extraction_method="podcast_" + item.extraction_method,
        )

    def _fetch(self, url: str) -> tuple[str, str, str]:
        current = url
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; LinkReaderBot/1.0; +https://github.com/evalegalai-cloud/link-reader-bot)",
            "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,text/html;q=0.9,*/*;q=0.3",
        }
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=8.0), headers=headers) as client:
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
                        raise ValueError("עמוד הפודקאסט גדול מדי.")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise ValueError("עמוד הפודקאסט גדול מדי.")
                    encoding = response.encoding or "utf-8"
                    return str(response.url), bytes(data).decode(encoding, errors="replace"), (
                        response.headers.get("content-type") or ""
                    ).lower()
        raise ValueError("יותר מדי הפניות בדרך לפודקאסט.")

    def _looks_like_feed(self, body: str, content_type: str) -> bool:
        head = body.lstrip()[:500].lower()
        return (
            "rss" in content_type
            or "atom" in content_type
            or "xml" in content_type
            or head.startswith("<?xml")
            or "<rss" in head
            or "<feed" in head
        )

    def _episode_from_feed(self, body: str, base_url: str) -> tuple[str, str | None, str | None] | None:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None

        channel = root.find("channel")
        if channel is not None:
            show_author = self._text_any(channel, ["{http://www.itunes.com/dtds/podcast-1.0.dtd}author", "author"])
            for item in channel.findall("item"):
                audio = self._rss_audio_url(item, base_url)
                if not audio:
                    continue
                title = self._child_text(item, "title")
                author = self._text_any(
                    item,
                    ["{http://www.itunes.com/dtds/podcast-1.0.dtd}author", "author"],
                ) or show_author
                return audio, title, author

        # Atom feeds use namespaces inconsistently; compare local tag names.
        entries = [node for node in root.iter() if self._local_name(node.tag) == "entry"]
        for entry in entries:
            title = self._first_local_text(entry, "title")
            author = self._atom_author(entry)
            for node in entry.iter():
                if self._local_name(node.tag) != "link":
                    continue
                rel = (node.attrib.get("rel") or "").lower()
                typ = (node.attrib.get("type") or "").lower()
                href = node.attrib.get("href") or ""
                if href and (rel == "enclosure" or typ.startswith("audio/")):
                    return self._web_safety._normalize_url(urljoin(base_url, href)), title, author
        return None

    def _rss_audio_url(self, item: ET.Element, base_url: str) -> str | None:
        for node in item.iter():
            local = self._local_name(node.tag)
            if local == "enclosure":
                href = node.attrib.get("url") or node.attrib.get("href") or ""
                typ = (node.attrib.get("type") or "").lower()
                if href and (not typ or typ.startswith("audio/") or Path(urlsplit(href).path).suffix.lower() in _AUDIO_EXTENSIONS):
                    return self._web_safety._normalize_url(urljoin(base_url, href))
            if local in {"content", "link"}:
                href = node.attrib.get("url") or node.attrib.get("href") or ""
                typ = (node.attrib.get("type") or "").lower()
                if href and typ.startswith("audio/"):
                    return self._web_safety._normalize_url(urljoin(base_url, href))
        return None

    def _first_media_url(self, values: list[str], base_url: str) -> str | None:
        for raw in values:
            try:
                candidate = self._web_safety._normalize_url(urljoin(base_url, raw))
                self._web_safety._validate_public_url(candidate)
            except Exception:
                continue
            path = urlsplit(candidate).path.lower()
            if Path(path).suffix in _AUDIO_EXTENSIONS or "audio" in path or "podcast" in path:
                return candidate
        return None

    def _fallback_title(self, url: str) -> str:
        parsed = urlsplit(url)
        tail = parsed.path.rstrip("/").split("/")[-1]
        return tail.replace("-", " ").replace("_", " ").strip() or (parsed.hostname or "Podcast")

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    @staticmethod
    def _child_text(node: ET.Element, tag: str) -> str | None:
        child = node.find(tag)
        text = (child.text or "").strip() if child is not None else ""
        return text or None

    @staticmethod
    def _text_any(node: ET.Element, tags: list[str]) -> str | None:
        for tag in tags:
            child = node.find(tag)
            text = (child.text or "").strip() if child is not None else ""
            if text:
                return text
        return None

    def _first_local_text(self, node: ET.Element, wanted: str) -> str | None:
        for child in node.iter():
            if self._local_name(child.tag) == wanted:
                text = (child.text or "").strip()
                if text:
                    return text
        return None

    def _atom_author(self, node: ET.Element) -> str | None:
        for author in node.iter():
            if self._local_name(author.tag) != "author":
                continue
            name = self._first_local_text(author, "name")
            if name:
                return name
            text = (author.text or "").strip()
            if text:
                return text
        return None

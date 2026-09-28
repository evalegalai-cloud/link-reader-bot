from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from link_reader.processors.webpage import WebPageProcessor
from link_reader.types import ExtractedContent, TranscriptSegment


_REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "redd.it"}
_POST_RE = re.compile(r"/comments/([a-z0-9]+)", re.I)


class RedditProcessor:
    source_type = "reddit"
    max_comments = 40
    max_bytes = 4 * 1024 * 1024

    def __init__(self, settings):
        self.settings = settings
        self._web_safety = WebPageProcessor(settings)

    def supports(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme.lower() in {"http", "https"}
                and (parsed.hostname or "").lower() in _REDDIT_HOSTS
            )
        except ValueError:
            return False

    def external_id(self, url: str) -> str:
        parsed = urlsplit(url)
        match = _POST_RE.search(parsed.path)
        if match:
            return match.group(1).lower()
        if (parsed.hostname or "").lower() == "redd.it":
            short_id = parsed.path.strip("/").split("/")[0]
            if re.fullmatch(r"[A-Za-z0-9]+", short_id or ""):
                return short_id.lower()
        return self._web_safety.external_id(url)

    async def extract(self, url: str) -> ExtractedContent:
        return await asyncio.to_thread(self._extract_sync, url)

    def _extract_sync(self, url: str) -> ExtractedContent:
        canonical, payload = self._fetch_json(url)
        post, comments = self._parse_payload(payload)
        title = str(post.get("title") or "Reddit post").strip()
        author = str(post.get("author") or "").strip() or None
        body = str(post.get("selftext") or "").strip()
        subreddit = str(post.get("subreddit_name_prefixed") or "").strip()

        segments: list[TranscriptSegment] = []
        post_parts = [title]
        if subreddit:
            post_parts.append(subreddit)
        if body:
            post_parts.append(body)
        segments.append(
            TranscriptSegment(
                start=0.0,
                duration=1.0,
                text="\n\n".join(post_parts),
                reference="§1",
            )
        )
        for idx, comment in enumerate(comments, start=2):
            text = str(comment.get("body") or "").strip()
            if not text or text in {"[deleted]", "[removed]"}:
                continue
            commenter = str(comment.get("author") or "").strip()
            if commenter:
                text = f"{commenter}: {text}"
            segments.append(
                TranscriptSegment(
                    start=float(idx - 1),
                    duration=1.0,
                    text=text,
                    reference=f"§{idx}",
                )
            )
        if not body and len(segments) == 1 and len(title) < 20:
            raise ValueError("לא מצאתי מספיק טקסט בפוסט Reddit.")

        return ExtractedContent(
            external_id=self.external_id(canonical),
            source_type=self.source_type,
            url=canonical,
            title=title[:500],
            author=author[:300] if author else None,
            duration_seconds=None,
            language=None,
            segments=segments,
            extraction_method="reddit_json",
        )

    def _fetch_json(self, url: str) -> tuple[str, object]:
        normalized = self._web_safety._normalize_url(url)
        self._web_safety._validate_public_url(normalized)
        headers = {
            "User-Agent": "LinkReaderBot/0.1 (+https://github.com/evalegalai-cloud/link-reader-bot)",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=httpx.Timeout(25.0, connect=8.0), headers=headers, follow_redirects=True) as client:
            response = client.get(normalized)
            response.raise_for_status()
            final = str(response.url)
            parsed = urlsplit(final)
            host = (parsed.hostname or "").lower()
            if host not in _REDDIT_HOSTS:
                raise ValueError("הקישור אינו מפנה לפוסט Reddit.")
            path = parsed.path
            if not _POST_RE.search(path):
                raise ValueError("כרגע נתמכים קישורים לפוסט Reddit בודד.")
            if not path.endswith(".json"):
                path = path.rstrip("/") + ".json"
            api_url = urlunsplit(("https", "www.reddit.com", path, "raw_json=1&limit=50", ""))
            self._web_safety._validate_public_url(api_url)
            with client.stream("GET", api_url, follow_redirects=True) as api:
                api.raise_for_status()
                declared = api.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise ValueError("פוסט Reddit גדול מדי.")
                data = bytearray()
                for chunk in api.iter_bytes():
                    data.extend(chunk)
                    if len(data) > self.max_bytes:
                        raise ValueError("פוסט Reddit גדול מדי.")
                try:
                    payload = __import__("json").loads(bytes(data))
                except Exception as exc:
                    raise ValueError("Reddit לא החזיר מידע קריא לפוסט.") from exc
            canonical = urlunsplit(("https", "www.reddit.com", parsed.path.rstrip("/") + "/", "", ""))
            return canonical, payload

    def _parse_payload(self, payload: object) -> tuple[dict, list[dict]]:
        if not isinstance(payload, list) or not payload:
            raise ValueError("מבנה התגובה של Reddit אינו צפוי.")
        try:
            post = payload[0]["data"]["children"][0]["data"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("לא הצלחתי לקרוא את פוסט Reddit.") from exc
        comments: list[dict] = []
        if len(payload) > 1:
            children = (payload[1].get("data") or {}).get("children") or []
            self._collect_comments(children, comments)
        return post, comments[: self.max_comments]

    def _collect_comments(self, children: list, out: list[dict]) -> None:
        for child in children:
            if len(out) >= self.max_comments:
                return
            if not isinstance(child, dict) or child.get("kind") != "t1":
                continue
            data = child.get("data") or {}
            body = str(data.get("body") or "").strip()
            if body and body not in {"[deleted]", "[removed]"}:
                out.append(data)
            replies = data.get("replies")
            if isinstance(replies, dict):
                nested = (replies.get("data") or {}).get("children") or []
                self._collect_comments(nested, out)

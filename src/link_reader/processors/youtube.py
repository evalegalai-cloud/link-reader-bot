from __future__ import annotations

import asyncio
import json
import random
import re
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApi
import httpx
from yt_dlp import YoutubeDL

from link_reader.types import ExtractedContent, TranscriptSegment


_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}


class YouTubeProcessor:
    source_type = "youtube"

    def __init__(self, settings):
        self.settings = settings
        self._last_good_proxy: str | None = None

    def supports(self, url: str) -> bool:
        try:
            return (urlparse(url).hostname or "").lower() in _YT_HOSTS
        except ValueError:
            return False

    def external_id(self, url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        elif parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
            video_id = parsed.path.strip("/").split("/")[1]
        else:
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
            raise ValueError("לא הצלחתי לזהות מזהה סרטון YouTube בקישור.")
        return video_id

    async def extract(self, url: str) -> ExtractedContent:
        return await asyncio.to_thread(self._extract_sync, url)

    def _proxy_candidates(self, limit: int = 24) -> list[str | None]:
        candidates: list[str] = []
        if self._last_good_proxy:
            candidates.append(self._last_good_proxy)
        if self.settings.youtube_proxy_url:
            candidates.append(self.settings.youtube_proxy_url.strip())

        proxy_file = getattr(self.settings, "youtube_proxy_file", None)
        if proxy_file:
            path = Path(proxy_file)
            if path.exists():
                file_candidates = []
                for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    value = raw.strip()
                    if not value or value.startswith("#"):
                        continue
                    if "://" not in value:
                        value = "http://" + value
                    file_candidates.append(value)
                file_candidates = list(dict.fromkeys(file_candidates))
                if len(file_candidates) > limit:
                    file_candidates = random.SystemRandom().sample(file_candidates, limit)
                candidates.extend(file_candidates)

        unique = list(dict.fromkeys(x for x in candidates if x))
        return [None, *unique]

    def _ydl_options(self, download: bool = False, proxy: str | None = None) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 2,
        }
        if proxy:
            opts["proxy"] = proxy
        if not download:
            opts["skip_download"] = True
        return opts

    def _oembed_metadata(self, url: str) -> dict:
        endpoint = (
            "https://www.youtube.com/oembed?url="
            + urllib.parse.quote(url, safe="")
            + "&format=json"
        )
        try:
            with urllib.request.urlopen(endpoint, timeout=15) as response:
                data = json.load(response)
            return {
                "title": data.get("title"),
                "author": data.get("author_name"),
            }
        except Exception:
            return {}

    def _extract_sync(self, url: str) -> ExtractedContent:
        video_id = self.external_id(url)
        metadata = self._oembed_metadata(url)

        if getattr(self.settings, "supadata_api_key", None):
            try:
                segments, language = self._supadata_transcript(url)
                if segments:
                    return ExtractedContent(
                        external_id=video_id,
                        source_type=self.source_type,
                        url=url,
                        title=metadata.get("title") or f"YouTube {video_id}",
                        author=metadata.get("author"),
                        duration_seconds=None,
                        language=language,
                        segments=segments,
                        extraction_method="supadata",
                    )
            except Exception:
                pass

        segments, language, caption_error, working_proxy = self._captions(video_id)
        method = "youtube_captions"

        if not segments:
            if self.settings.asr_mode != "local":
                self._raise_extraction_error(caption_error)
            try:
                segments, language = self._transcribe_audio(url, working_proxy)
                method = "local_whisper"
            except Exception as audio_error:
                self._raise_extraction_error(caption_error or audio_error, audio_error)

        return ExtractedContent(
            external_id=video_id,
            source_type=self.source_type,
            url=url,
            title=metadata.get("title") or f"YouTube {video_id}",
            author=metadata.get("author"),
            duration_seconds=None,
            language=language,
            segments=segments,
            extraction_method=method,
        )

    def _supadata_transcript(self, url: str):
        headers = {"x-api-key": self.settings.supadata_api_key}
        params = {"url": url, "mode": self.settings.supadata_mode}
        endpoint = "https://api.supadata.ai/v1/transcript"

        with httpx.Client(timeout=120.0) as client:
            response = client.get(endpoint, headers=headers, params=params)
            if response.status_code == 202:
                job_id = response.json().get("jobId")
                if not job_id:
                    raise RuntimeError("Supadata returned 202 without a job ID")
                for _ in range(180):
                    time.sleep(1)
                    job = client.get(f"{endpoint}/{job_id}", headers=headers)
                    job.raise_for_status()
                    payload = job.json()
                    status = payload.get("status")
                    if status == "completed":
                        return self._segments_from_supadata(payload)
                    if status == "failed":
                        raise RuntimeError(payload.get("error") or "Supadata transcription failed")
                raise RuntimeError("Supadata transcription timed out")

            response.raise_for_status()
            return self._segments_from_supadata(response.json())

    def _segments_from_supadata(self, payload: dict):
        content = payload.get("content")
        language = payload.get("lang")
        if not isinstance(content, list):
            raise RuntimeError("Supadata returned an unexpected transcript format")
        segments = [
            TranscriptSegment(
                float(item.get("offset", 0)) / 1000.0,
                float(item.get("duration", 0)) / 1000.0,
                str(item.get("text", "")).strip(),
            )
            for item in content
            if str(item.get("text", "")).strip()
        ]
        return segments, language

    def _captions(self, video_id: str):
        last_error = None
        for proxy in self._proxy_candidates():
            try:
                api = self._transcript_api(proxy)
                try:
                    fetched = api.fetch(video_id, languages=("en", "he"))
                    segments = [
                        TranscriptSegment(float(x.start), float(x.duration), x.text)
                        for x in fetched
                        if x.text and x.text.strip()
                    ]
                    if proxy:
                        self._last_good_proxy = proxy
                    return segments, getattr(fetched, "language_code", None), None, proxy
                except NoTranscriptFound:
                    available = list(api.list(video_id))
                    if not available:
                        return [], None, None, proxy

                    preferred = ["en", "he"]

                    def score(transcript):
                        lang_score = 0
                        if transcript.language_code in preferred:
                            lang_score = len(preferred) - preferred.index(transcript.language_code)
                        return (not transcript.is_generated, lang_score)

                    selected = sorted(available, key=score, reverse=True)[0]
                    fetched = selected.fetch()
                    segments = [
                        TranscriptSegment(float(x.start), float(x.duration), x.text)
                        for x in fetched
                        if x.text and x.text.strip()
                    ]
                    if proxy:
                        self._last_good_proxy = proxy
                    return segments, selected.language_code, None, proxy
            except Exception as exc:
                last_error = exc
                continue
        return [], None, last_error, None

    def _transcript_api(self, proxy: str | None = None):
        if not proxy:
            return YouTubeTranscriptApi()
        from youtube_transcript_api.proxies import GenericProxyConfig

        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy)
        )

    def _raise_extraction_error(self, caption_error=None, audio_error=None):
        details = " ".join(
            str(exc) for exc in (caption_error, audio_error) if exc is not None
        ).lower()
        blocked_markers = (
            "requestblocked",
            "ipblocked",
            "blocking requests from your ip",
            "blocking your requests",
            "sign in to confirm you",
            "not a bot",
        )
        if any(marker in details for marker in blocked_markers):
            raise RuntimeError(
                "YouTube חסם את כתובת ה-IP של השרת וגם את ה-proxies שנוסו. "
                "יש להגדיר proxy pool פעיל ב-YOUTUBE_PROXY_FILE או rotating residential proxy."
            )
        raise RuntimeError(
            "לא הצלחתי לקבל כתוביות או אודיו מהסרטון הזה. "
            "ייתכן שאין כתוביות זמינות או שהסרטון מוגבל."
        )

    def _transcribe_audio(self, url: str, preferred_proxy: str | None = None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper אינו מותקן. התקן את חבילת [asr].") from exc

        candidates = self._proxy_candidates(limit=6)
        if preferred_proxy:
            candidates = [preferred_proxy] + [x for x in candidates if x != preferred_proxy]

        last_error = None
        for proxy in candidates:
            try:
                with tempfile.TemporaryDirectory(prefix="link-reader-") as tmp:
                    outtmpl = str(Path(tmp) / "audio.%(ext)s")
                    opts = self._ydl_options(download=True, proxy=proxy)
                    opts.update({
                        "format": "bestaudio/best",
                        "outtmpl": outtmpl,
                        "postprocessors": [{
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "96",
                        }],
                    })
                    with YoutubeDL(opts) as ydl:
                        ydl.download([url])

                    audio_path = Path(tmp) / "audio.mp3"
                    if not audio_path.exists():
                        matches = list(Path(tmp).glob("audio.*"))
                        if not matches:
                            raise RuntimeError("הורדת האודיו נכשלה.")
                        audio_path = matches[0]

                    model = WhisperModel(
                        self.settings.whisper_model,
                        device="cpu",
                        compute_type="int8",
                    )
                    raw_segments, meta = model.transcribe(str(audio_path), vad_filter=True)
                    result = [
                        TranscriptSegment(
                            float(s.start),
                            float(s.end - s.start),
                            s.text.strip(),
                        )
                        for s in raw_segments
                        if s.text.strip()
                    ]
                    return result, getattr(meta, "language", None)
            except Exception as exc:
                last_error = exc
                continue

        raise last_error or RuntimeError("הורדת האודיו נכשלה.")

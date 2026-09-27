from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import YouTubeTranscriptApi
from yt_dlp import YoutubeDL

from link_reader.types import ExtractedContent, TranscriptSegment


_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}


class YouTubeProcessor:
    source_type = "youtube"

    def __init__(self, settings):
        self.settings = settings

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

    def _ydl_options(self, download: bool = False) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,
        }
        if self.settings.youtube_proxy_url:
            opts["proxy"] = self.settings.youtube_proxy_url
        if not download:
            opts["skip_download"] = True
        return opts

    def _metadata(self, url: str) -> dict:
        with YoutubeDL(self._ydl_options()) as ydl:
            return ydl.extract_info(url, download=False)

    def _extract_sync(self, url: str) -> ExtractedContent:
        video_id = self.external_id(url)
        info = self._metadata(url)
        duration = int(info.get("duration") or 0) or None
        if duration and duration > self.settings.max_video_minutes * 60:
            raise ValueError(f"הסרטון ארוך מהמגבלה שהוגדרה ({self.settings.max_video_minutes} דקות).")

        segments = self._captions(video_id, info.get("language"))
        method = "youtube_captions"
        language = info.get("language")

        if not segments:
            if self.settings.asr_mode != "local":
                raise RuntimeError("לא נמצאו כתוביות, ותמלול מקומי אינו מופעל.")
            segments, language = self._transcribe_audio(url)
            method = "local_whisper"

        return ExtractedContent(
            external_id=video_id,
            source_type=self.source_type,
            url=url,
            title=info.get("title") or f"YouTube {video_id}",
            author=info.get("uploader") or info.get("channel"),
            duration_seconds=duration,
            language=language,
            segments=segments,
            extraction_method=method,
        )

    def _captions(self, video_id: str, source_language: str | None):
        try:
            api = self._transcript_api()
            available = list(api.list(video_id))
            if not available:
                return []
            preferred = []
            if source_language:
                preferred.append(source_language)
                preferred.append(source_language.split("-")[0])
            preferred.extend(["en", "he"])
            def score(t):
                lang_score = 0
                if t.language_code in preferred:
                    lang_score = len(preferred) - preferred.index(t.language_code)
                return (not t.is_generated, lang_score)
            selected = sorted(available, key=score, reverse=True)[0]
            fetched = selected.fetch()
            return [
                TranscriptSegment(float(x.start), float(x.duration), x.text)
                for x in fetched
                if x.text and x.text.strip()
            ]
        except Exception:
            return []

    def _transcript_api(self):
        if not self.settings.youtube_proxy_url:
            return YouTubeTranscriptApi()
        from youtube_transcript_api.proxies import GenericProxyConfig
        proxy = self.settings.youtube_proxy_url
        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy)
        )

    def _transcribe_audio(self, url: str):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper אינו מותקן. התקן את חבילת [asr].") from exc

        with tempfile.TemporaryDirectory(prefix="link-reader-") as tmp:
            outtmpl = str(Path(tmp) / "audio.%(ext)s")
            opts = self._ydl_options(download=True)
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

            model = WhisperModel(self.settings.whisper_model, device="cpu", compute_type="int8")
            raw_segments, meta = model.transcribe(str(audio_path), vad_filter=True)
            result = [
                TranscriptSegment(float(s.start), float(s.end - s.start), s.text.strip())
                for s in raw_segments if s.text.strip()
            ]
            return result, getattr(meta, "language", None)

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

from link_reader.processors.webpage import WebPageProcessor
from link_reader.processors.supadata_media import fetch_media_transcript, supadata_key
from link_reader.types import ExtractedContent, TranscriptSegment
from link_reader.transcription import OpenAIFileTranscriber


logger = logging.getLogger(__name__)


_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".amr", ".flac", ".webm"}


class AudioProcessor:
    source_type = "audio"
    max_bytes = 250 * 1024 * 1024

    def __init__(self, settings):
        self.settings = settings
        self._web_safety = WebPageProcessor(settings)
        self._model = None
        self._openai_transcriber = OpenAIFileTranscriber()

    def supports(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            suffix = Path(parsed.path).suffix.lower()
            return (
                parsed.scheme.lower() in {"http", "https"}
                and bool(parsed.hostname)
                and suffix in _AUDIO_EXTENSIONS
            )
        except ValueError:
            return False

    def external_id(self, url: str) -> str:
        normalized = self._web_safety._normalize_url(url)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    async def extract(self, url: str) -> ExtractedContent:
        return await asyncio.to_thread(self._extract_sync, url)

    async def extract_local_file(
        self, path: str | Path, *, external_id: str, title: str,
        source_type: str = "voice", url: str | None = None,
    ) -> ExtractedContent:
        return await asyncio.to_thread(
            self._extract_local_file_sync,
            Path(path), external_id, title, source_type, url,
        )

    def _extract_local_file_sync(
        self, path: Path, external_id: str, title: str,
        source_type: str, url: str | None,
    ) -> ExtractedContent:
        local_enabled = getattr(self.settings, "asr_mode", "off") == "local"
        if not self._openai_transcriber.available and not local_enabled:
            raise ValueError("תמלול אודיו אינו פעיל כרגע.")
        method = "faster_whisper"
        try:
            if self._openai_transcriber.available:
                duration = self._duration_seconds(path)
                segments, language, duration = self._openai_transcriber.transcribe(path, duration)
                method = "openai_gpt_transcribe"
            else:
                raise RuntimeError("OpenAI transcription unavailable")
        except Exception:
            if not local_enabled:
                raise
            segments, language, duration = self._transcribe_local_path(path)
        return ExtractedContent(
            external_id=external_id,
            source_type=source_type,
            url=url or f"local://{source_type}/{external_id}",
            title=(title or "הודעה קולית")[:500],
            author=None,
            duration_seconds=duration,
            language=language,
            segments=segments,
            extraction_method=method,
        )

    def _extract_sync(self, url: str) -> ExtractedContent:
        if getattr(self.settings, "asr_mode", "off") != "local":
            raise ValueError("תמלול אודיו מקומי אינו פעיל כרגע.")
        normalized = self._web_safety._normalize_url(url)
        self._web_safety._validate_public_url(normalized)

        if supadata_key(self.settings):
            try:
                segments, language = fetch_media_transcript(self.settings, normalized)
                if segments:
                    duration = max(seg.start + seg.duration for seg in segments)
                    name = PurePosixPath(unquote(urlsplit(normalized).path)).name
                    title = name.rsplit(".", 1)[0] if "." in name else (name or "Audio")
                    return ExtractedContent(
                        external_id=self.external_id(normalized),
                        source_type=self.source_type,
                        url=normalized,
                        title=title[:500],
                        author=None,
                        duration_seconds=duration,
                        language=language,
                        segments=segments,
                        extraction_method="supadata_generated",
                    )
            except Exception:
                pass

        suffix = Path(urlsplit(normalized).path).suffix.lower() or ".audio"
        final_url, path = self._download_audio(normalized, suffix)
        try:
            segments, language, duration = self._transcribe_local_path(path)
        finally:
            try:
                path.unlink(missing_ok=True)
                path.parent.rmdir()
            except Exception:
                pass

        name = PurePosixPath(unquote(urlsplit(final_url).path)).name
        title = name.rsplit(".", 1)[0] if "." in name else (name or "Audio")
        return ExtractedContent(
            external_id=self.external_id(normalized),
            source_type=self.source_type,
            url=final_url,
            title=title[:500],
            author=None,
            duration_seconds=duration,
            language=language,
            segments=segments,
            extraction_method="faster_whisper",
        )

    def _transcribe_local_path(self, path: Path):
        duration = self._duration_seconds(path)
        max_seconds = int(getattr(self.settings, "max_video_minutes", 360)) * 60
        if duration and duration > max_seconds:
            raise ValueError("קובץ האודיו ארוך מדי לעיבוד.")

        model = self._whisper_model()
        raw_segments, meta = model.transcribe(
            str(path),
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=True,
        )
        segments = [
            TranscriptSegment(
                start=float(seg.start),
                duration=max(0.0, float(seg.end - seg.start)),
                text=seg.text.strip(),
            )
            for seg in raw_segments
            if seg.text and seg.text.strip()
        ]
        if not segments:
            raise ValueError("לא זוהה דיבור בקובץ האודיו.")
        if not duration:
            duration = max(seg.start + seg.duration for seg in segments)
        language = getattr(meta, "language", None)
        return segments, language, duration

    def _whisper_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError("faster-whisper אינו מותקן.") from exc
            self._model = WhisperModel(
                self.settings.whisper_model,
                device="cpu",
                compute_type="int8",
            )
        return self._model

    def _download_audio(self, url: str, suffix: str) -> tuple[str, Path]:
        tmpdir = Path(tempfile.mkdtemp(prefix="link-reader-audio-"))
        path = tmpdir / ("audio" + suffix)
        current = url
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; LinkReaderBot/1.0; +https://github.com/evalegalai-cloud/link-reader-bot)",
            "Accept": "audio/*,application/octet-stream;q=0.8,*/*;q=0.2",
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=8.0), headers=headers) as client:
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
                            raise ValueError("קובץ האודיו גדול מדי.")
                        total = 0
                        with path.open("wb") as out:
                            for chunk in response.iter_bytes():
                                total += len(chunk)
                                if total > self.max_bytes:
                                    raise ValueError("קובץ האודיו גדול מדי.")
                                out.write(chunk)
                        if total < 128:
                            raise ValueError("קובץ האודיו ריק או לא תקין.")
                        return str(response.url), path
            raise ValueError("יותר מדי הפניות בדרך לקובץ האודיו.")
        except Exception:
            path.unlink(missing_ok=True)
            try:
                tmpdir.rmdir()
            except Exception:
                pass
            raise

    def _duration_seconds(self, path: Path) -> float | None:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
            value = float(result.stdout.strip())
            return value if value > 0 else None
        except Exception:
            return None

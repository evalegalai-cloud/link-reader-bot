from __future__ import annotations

import concurrent.futures
import mimetypes
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from link_reader.types import TranscriptSegment


OPENAI_TRANSCRIBE_MODEL = "gpt-transcribe"
OPENAI_TRANSCRIBE_COST_PER_MINUTE = 0.0045
OPENAI_SUPPORTED_EXTENSIONS = {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm"}
TRANSCRIPTION_PROMPT = (
    "Transcribe faithfully in the original language. Preserve proper names, "
    "technical terms, numbers, punctuation, and code-switching. Do not summarize."
)


def openai_key() -> str | None:
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    if value:
        return value
    secret = Path("/run/secrets/openai_api_key")
    if secret.exists():
        value = secret.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


class OpenAIFileTranscriber:
    def __init__(self, *, chunk_seconds: int = 90, max_workers: int = 4):
        self.chunk_seconds = chunk_seconds
        self.max_workers = max_workers

    @property
    def available(self) -> bool:
        return bool(openai_key())

    def transcribe(self, path: Path, duration: float | None = None):
        key = openai_key()
        if not key:
            raise RuntimeError("OpenAI transcription key is not configured")
        duration = duration or self._duration(path)
        if path.suffix.lower() not in OPENAI_SUPPORTED_EXTENSIONS:
            with tempfile.TemporaryDirectory(prefix="link-reader-audio-convert-") as tmp:
                converted = Path(tmp) / "audio.mp3"
                self._convert_to_mp3(path, converted)
                return self.transcribe(converted, duration)
        if not duration or (duration <= self.chunk_seconds and path.stat().st_size <= 24 * 1024 * 1024):
            text, language = self._request(path, key)
            if not text:
                raise ValueError("לא זוהה דיבור בקובץ האודיו.")
            return [TranscriptSegment(0.0, float(duration or 0.0), text)], language, duration

        with tempfile.TemporaryDirectory(prefix="link-reader-transcribe-") as tmp:
            chunks = self._split(path, Path(tmp))
            if not chunks:
                raise RuntimeError("לא הצלחתי לחלק את קובץ האודיו לתמלול.")
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                results = list(pool.map(lambda p: self._request(p, key), chunks))

            segments = []
            language = None
            for i, (chunk, (text, lang)) in enumerate(zip(chunks, results)):
                if not text:
                    continue
                chunk_duration = self._duration(chunk) or float(self.chunk_seconds)
                segments.append(
                    TranscriptSegment(
                        start=float(i * self.chunk_seconds),
                        duration=float(chunk_duration),
                        text=text,
                    )
                )
                language = language or lang
            if not segments:
                raise ValueError("לא זוהה דיבור בקובץ האודיו.")
            return segments, language, duration

    def _request(self, path: Path, key: str) -> tuple[str, str | None]:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {"Authorization": f"Bearer {key}"}
        retryable = {408, 425, 429, 500, 502, 503, 504}
        last_error = None
        for attempt, delay in enumerate((0.0, 0.5, 1.5)):
            if delay:
                time.sleep(delay)
            try:
                with path.open("rb") as f, httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                    response = client.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers=headers,
                        files={"file": (path.name, f, mime)},
                        data={"model": OPENAI_TRANSCRIBE_MODEL, "prompt": TRANSCRIPTION_PROMPT},
                    )
                if response.status_code in retryable and attempt < 2:
                    last_error = RuntimeError(f"OpenAI transcription transient HTTP {response.status_code}")
                    continue
                response.raise_for_status()
                data = response.json()
                break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt >= 2:
                    raise
        else:
            raise last_error or RuntimeError("OpenAI transcription failed")
        text = str(data.get("text") or "").strip()
        languages = data.get("languages") or []
        language = None
        if languages and isinstance(languages[0], dict):
            language = languages[0].get("code")
        return text, language

    def _convert_to_mp3(self, path: Path, output: Path) -> None:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                "-b:a", "64k", str(output),
            ],
            check=True,
            timeout=180,
        )

    def _split(self, path: Path, directory: Path) -> list[Path]:
        pattern = str(directory / "chunk-%04d.mp3")
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                "-b:a", "64k", "-f", "segment", "-segment_time",
                str(self.chunk_seconds), "-reset_timestamps", "1", pattern,
            ],
            check=True,
            timeout=180,
        )
        return sorted(directory.glob("chunk-*.mp3"))

    def _duration(self, path: Path) -> float | None:
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

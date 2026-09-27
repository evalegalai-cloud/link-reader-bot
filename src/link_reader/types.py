from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TranscriptSegment:
    start: float
    duration: float
    text: str


@dataclass
class ExtractedContent:
    external_id: str
    source_type: str
    url: str
    title: str
    author: str | None
    duration_seconds: int | None
    language: str | None
    segments: list[TranscriptSegment]
    extraction_method: str

    @property
    def transcript_text(self) -> str:
        return "\n".join(
            f"[{format_timestamp(s.start)}] {s.text.strip()}"
            for s in self.segments
            if s.text.strip()
        )


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

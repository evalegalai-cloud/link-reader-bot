from types import SimpleNamespace

from link_reader.processors.youtube import YouTubeProcessor
from link_reader.service import ContentService
from link_reader.types import TranscriptSegment


def settings():
    return SimpleNamespace(
        youtube_proxy_url=None,
        max_video_minutes=360,
        asr_mode="off",
        whisper_model="small",
    )


def test_youtube_url_parsing():
    p = YouTubeProcessor(settings())
    assert p.external_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert p.external_id("https://youtu.be/dQw4w9WgXcQ?t=12") == "dQw4w9WgXcQ"
    assert p.external_id("https://youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_chunking_preserves_timestamps():
    service = ContentService(None, None, [])
    segments = [
        TranscriptSegment(0, 5, "one"),
        TranscriptSegment(5, 5, "two"),
        TranscriptSegment(10, 5, "three"),
    ]
    chunks = service._chunk_segments(segments, target_chars=18)
    assert len(chunks) >= 2
    assert chunks[0]["start_seconds"] == 0
    assert "[00:00]" in chunks[0]["text"]

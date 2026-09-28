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


def test_telegram_rendering_and_long_split():
    from link_reader.bot import TelegramBot

    bot = TelegramBot(SimpleNamespace(), SimpleNamespace())
    text = ("**כותרת:** מונח (Corpus Paulinum). *הערה*. [12:34]\n" * 100)
    text += "\n**זמן:** 1:41 דק׳ · **עלות:** כ-23.54 סנט"
    chunks = bot._split_text(text)
    assert len(chunks) > 1
    assert max(len(chunk) for chunk in chunks) <= 3500
    assert sum(chunk.count("Corpus Paulinum") for chunk in chunks) == 100
    rendered = [bot._telegram_html(chunk) for chunk in chunks]
    assert all("*" not in chunk for chunk in rendered)
    assert sum(chunk.count("<b>") for chunk in rendered) == sum(chunk.count("</b>") for chunk in rendered)
    assert any("זמן" in chunk for chunk in rendered)
    assert bot._format_duration(61) == "1:01 דק׳"
    assert bot._format_cost(0.2354) == "כ-23.54 סנט"
    assert bot._format_cost(23.45) == "כ-23.45 דולר"

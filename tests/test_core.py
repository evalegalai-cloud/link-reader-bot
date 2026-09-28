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
    mixed_rtl = bot._telegram_html("**OpenAI:** מודל חדש")
    assert mixed_rtl.startswith("\u200f")
    pure_english = bot._telegram_html("OpenAI released a new model")
    assert not pure_english.startswith("\u200f")


def test_generic_section_references_and_web_url_safety():
    from types import SimpleNamespace
    from link_reader.processors.webpage import WebPageProcessor

    service = ContentService(None, None, [])
    segments = [
        TranscriptSegment(0, 1, "first paragraph", reference="§1"),
        TranscriptSegment(1, 1, "second paragraph", reference="§2"),
    ]
    chunks = service._chunk_segments(segments, target_chars=100)
    assert "[§1]" in chunks[0]["text"]
    assert "[§2]" in chunks[0]["text"]

    p = WebPageProcessor(SimpleNamespace())
    assert p.supports("https://example.com/article")
    assert not p.supports("https://youtube.com/watch?v=dQw4w9WgXcQ")
    assert p._normalize_url("https://Example.com/a?utm_source=x&b=2#frag") == "https://example.com/a?b=2"
    for private_url in ("http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/"):
        try:
            p._validate_public_url(private_url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"private URL was allowed: {private_url}")


def test_pdf_processor_url_and_page_splitting():
    from types import SimpleNamespace
    from link_reader.processors.pdf import PDFProcessor

    p = PDFProcessor(SimpleNamespace())
    assert p.supports("https://example.com/report.pdf")
    assert p.supports("https://example.com/report.PDF?download=1")
    assert not p.supports("https://example.com/report.html")
    parts = p._split_page("word " * 2000, max_chars=1000)
    assert len(parts) > 1
    assert all(len(part) <= 1001 for part in parts)


def test_audio_and_social_processor_routing():
    from types import SimpleNamespace
    from link_reader.processors.audio import AudioProcessor
    from link_reader.processors.social import SocialVideoProcessor

    settings = SimpleNamespace(asr_mode="local", whisper_model="small", max_video_minutes=360, supadata_api_key=None)
    audio = AudioProcessor(settings)
    social = SocialVideoProcessor(settings)
    assert audio.supports("https://example.com/episode.mp3")
    assert audio.supports("https://cdn.example.com/audio.m4a?x=1")
    assert not audio.supports("https://example.com/article")
    assert social.supports("https://www.instagram.com/reel/ABC123/")
    assert social.supports("https://vm.tiktok.com/ABC123/")
    assert social.supports("https://x.com/user/status/123")
    assert social.supports("https://www.facebook.com/reel/123")
    assert not social.supports("https://example.com/video")

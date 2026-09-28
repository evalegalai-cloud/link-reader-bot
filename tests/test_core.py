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


def test_telegram_navigation_is_complete():
    from types import SimpleNamespace
    from link_reader.bot import TelegramBot

    class DB:
        def get_current_content(self, user_id):
            return {"title": "Current source"}

    bot = TelegramBot(SimpleNamespace(), SimpleNamespace(db=DB()))

    def labels(markup):
        return [button.text for row in markup.inline_keyboard for button in row]

    home = labels(bot._home_keyboard())
    assert {"מקור נוכחי", "אחרונים", "מה נתמך"}.issubset(home)

    content = labels(bot._content_keyboard())
    assert {"שאל שאלה", "תרגום מלא", "טקסט מלא", "אחרונים", "תפריט ראשי"}.issubset(content)

    nav = labels(bot._nav_keyboard())
    assert nav == ["מקור נוכחי", "תפריט ראשי"]

    recent = labels(bot._videos_keyboard([
        {"id": 1, "title": "Article", "source_type": "web"},
        {"id": 2, "title": "Video", "source_type": "youtube"},
    ]))
    assert "כתבה · Article" in recent
    assert "YouTube · Video" in recent
    assert "מקור נוכחי" in recent and "תפריט ראשי" in recent


def test_whatsapp_signature_allowlist_and_stable_user_id():
    import hashlib
    import hmac
    from link_reader.whatsapp import WhatsAppConfig, WhatsAppGateway

    config = WhatsAppConfig(
        access_token="token",
        phone_number_id="123",
        app_secret="secret",
        verify_token="verify",
        graph_version="v26.0",
        allowed_numbers=frozenset({"15551234567"}),
        admin_token="admin",
    )
    gateway = object.__new__(WhatsAppGateway)
    gateway.config = config
    body = b'{"object":"whatsapp_business_account"}'
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert gateway.verify_signature(body, sig)
    assert not gateway.verify_signature(body, "sha256=deadbeef")
    assert config.sender_allowed("+1 555-123-4567")
    assert not config.sender_allowed("15559876543")
    assert gateway.user_id("15551234567") == gateway.user_id("15551234567")
    assert gateway.user_id("15551234567") < 0


def test_webhook_event_dedup(tmp_path):
    from link_reader.db import Database
    db = Database(str(tmp_path / "test.db"))
    assert db.claim_webhook_event("whatsapp", "wamid.1")
    assert not db.claim_webhook_event("whatsapp", "wamid.1")
    assert db.claim_webhook_event("whatsapp", "wamid.2")


def test_local_audio_prefers_openai_transcriber(tmp_path):
    from types import SimpleNamespace
    from link_reader.processors.audio import AudioProcessor
    from link_reader.types import TranscriptSegment

    settings = SimpleNamespace(asr_mode="local", whisper_model="small", max_video_minutes=360, supadata_api_key=None)
    processor = AudioProcessor(settings)

    class FakeTranscriber:
        available = True
        def transcribe(self, path, duration=None):
            return [TranscriptSegment(0.0, 12.0, "hello world")], "en", 12.0

    processor._openai_transcriber = FakeTranscriber()
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"not-real-audio")
    item = processor._extract_local_file_sync(path, "voice-1", "Voice", "voice", "telegram://voice/1")
    assert item.extraction_method == "openai_gpt_transcribe"
    assert item.source_type == "voice"
    assert item.transcript_text == "[00:00] hello world"

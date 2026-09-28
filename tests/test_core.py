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

    # Global navigation is only in the persistent menu, not duplicated inline.
    assert bot._home_keyboard() is None
    assert bot._nav_keyboard() is None

    content = labels(bot._content_keyboard())
    assert content == ["תרגום מלא", "טקסט מלא"]

    recent = labels(bot._videos_keyboard([
        {"id": 1, "title": "Article", "source_type": "web"},
        {"id": 2, "title": "Video", "source_type": "youtube"},
    ]))
    assert recent == ["כתבה · Article", "YouTube · Video"]


def test_whatsapp_signature_allowlist_and_stable_user_id():
    import hashlib
    import hmac
    from link_reader.whatsapp import WhatsAppConfig, WhatsAppGateway

    config = WhatsAppConfig(
        access_token="token",
        phone_number_id="123",
        waba_id="456",
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


def test_podcast_rss_and_page_discovery():
    from types import SimpleNamespace
    from link_reader.processors.podcast import PodcastProcessor, _DiscoveryParser

    p = PodcastProcessor(SimpleNamespace(
        asr_mode="local", whisper_model="small", max_video_minutes=360, supadata_api_key=None
    ))
    assert p.supports("https://example.com/podcast/my-show")
    assert p.supports("https://example.com/feed.xml")
    assert p.supports("https://podcasts.apple.com/us/podcast/example/id123")
    assert not p.supports("https://example.com/news/article")

    rss = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <title>Example Show</title>
        <item>
          <title>Newest Episode</title>
          <enclosure url="https://cdn.example.com/ep42.mp3" type="audio/mpeg" />
        </item>
        <item>
          <title>Older Episode</title>
          <enclosure url="https://cdn.example.com/ep41.mp3" type="audio/mpeg" />
        </item>
      </channel>
    </rss>"""
    episode = p._episode_from_feed(rss, "https://example.com/feed.xml")
    assert episode == ("https://cdn.example.com/ep42.mp3", "Newest Episode", None)

    parser = _DiscoveryParser()
    parser.feed("""
      <html><head>
        <title>Episode page</title>
        <link rel="alternate" type="application/rss+xml" href="/feed.xml">
        <meta property="og:audio" content="https://cdn.example.com/direct.mp3">
      </head></html>
    """)
    assert parser.title == "Episode page"
    assert parser.feed_urls == ["/feed.xml"]
    assert parser.audio_urls == ["https://cdn.example.com/direct.mp3"]


def test_pdf_ocr_runtime_and_language_selection():
    from types import SimpleNamespace
    from link_reader.processors.pdf import PDFProcessor

    p = PDFProcessor(SimpleNamespace(max_ocr_pages=80))
    assert p._ocr_available()
    langs = p._ocr_languages().split("+")
    assert "eng" in langs
    # The production image has Hebrew OCR installed; keeping this assertion
    # protects the product's Hebrew-first requirement.
    assert "heb" in langs
    assert p.min_text_chars_per_page >= 40
    assert p.default_max_ocr_pages <= 100


def test_reddit_processor_parses_post_and_nested_comments():
    from types import SimpleNamespace
    from link_reader.processors.reddit import RedditProcessor

    p = RedditProcessor(SimpleNamespace())
    assert p.supports("https://www.reddit.com/r/test/comments/abc123/example/")
    assert p.supports("https://redd.it/abc123")
    assert not p.supports("https://example.com/r/test/comments/abc123")
    assert p.external_id("https://www.reddit.com/r/test/comments/abc123/example/") == "abc123"

    payload = [
        {"data": {"children": [{"data": {
            "title": "Example title",
            "selftext": "Post body",
            "author": "poster",
            "subreddit_name_prefixed": "r/test",
        }}]}},
        {"data": {"children": [
            {"kind": "t1", "data": {
                "author": "alice", "body": "First comment",
                "replies": {"data": {"children": [
                    {"kind": "t1", "data": {"author": "bob", "body": "Nested reply", "replies": ""}}
                ]}},
            }},
            {"kind": "more", "data": {}},
        ]}},
    ]
    post, comments = p._parse_payload(payload)
    assert post["title"] == "Example title"
    assert [c["body"] for c in comments] == ["First comment", "Nested reply"]


def test_epub_spine_order_metadata_and_registry_parity():
    import io
    import zipfile
    from types import SimpleNamespace

    from link_reader.processors import EPUBProcessor, PodcastProcessor, RedditProcessor, build_processors

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
            <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
              <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
            </container>""",
        )
        zf.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0" encoding="UTF-8"?>
            <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
              <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
                <dc:title>ספר בדיקה</dc:title><dc:creator>מחבר בדיקה</dc:creator>
              </metadata>
              <manifest>
                <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
                <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
              </manifest>
              <spine><itemref idref="c2"/><itemref idref="c1"/></spine>
            </package>""",
        )
        zf.writestr("OEBPS/c1.xhtml", "<html><body><h1>פרק ראשון</h1><p>" + "א " * 90 + "</p></body></html>")
        zf.writestr("OEBPS/c2.xhtml", "<html><body><h1>פרק שני</h1><p>" + "ב " * 90 + "</p></body></html>")

    settings = SimpleNamespace()
    processor = EPUBProcessor(settings)
    title, author, segments = processor._parse_epub(buffer.getvalue())
    assert title == "ספר בדיקה"
    assert author == "מחבר בדיקה"
    assert segments[0].reference == "ch.1"
    assert "פרק שני" in segments[0].text
    assert any(seg.reference == "ch.2" and "פרק ראשון" in seg.text for seg in segments)
    assert processor.supports("https://example.com/books/test.epub")

    registry = build_processors(settings)
    assert sum(isinstance(p, EPUBProcessor) for p in registry) == 1
    assert sum(isinstance(p, PodcastProcessor) for p in registry) == 1
    assert sum(isinstance(p, RedditProcessor) for p in registry) == 1


def test_library_fts_search_indexes_saved_chunks(tmp_path):
    from types import SimpleNamespace
    from link_reader.db import Database

    db = Database(str(tmp_path / "library.db"))
    item = SimpleNamespace(
        source_type="pdf",
        external_id="energy-doc",
        url="https://example.com/energy.pdf",
        title="דו״ח תשתיות אנרגיה",
        author="Test",
        duration_seconds=None,
        language="he",
        extraction_method="pypdf",
    )
    content_id = db.save_content(item, "[p.1] אגירת אנרגיה וסוללות לרשת החשמל")
    db.replace_chunks(content_id, [{
        "ordinal": 0,
        "start_seconds": 0.0,
        "end_seconds": 1.0,
        "text": "[p.1] אגירת אנרגיה וסוללות לרשת החשמל",
        "map_summary": None,
    }])
    rows = db.search_library("מה כתוב על אגירת אנרגיה?")
    assert rows
    assert rows[0]["content_id"] == content_id
    assert "אגירת אנרגיה" in rows[0]["text"]
    assert db.search_library("מונחשאיננוקייםבמאגר") == []


def test_freeform_router_keeps_source_default_and_menu_persistent():
    from types import SimpleNamespace
    from link_reader.bot import TelegramBot
    from link_reader.service import ContentService

    class StubDB:
        def __init__(self, current):
            self.current = current

        def get_current_content(self, user_id):
            return self.current

    service = ContentService(StubDB({"id": 1, "title": "Source"}), None, [])
    assert service._freeform_mode(1, "מה הוא אומר על זה היום?") == "source"
    assert service._freeform_mode(1, "בדוק באינטרנט מה המצב היום") == "web"
    assert service._freeform_mode(1, "מה השתנה מאז הסרטון?") == "web"
    assert service._freeform_mode(1, "מה אומרים כל המקורות על הנושא?") == "library"

    no_source = ContentService(StubDB(None), None, [])
    assert no_source._freeform_mode(1, "מה קורה היום?") == "web"
    assert no_source._freeform_mode(1, "תסביר לי מה זה") == "general"

    menu = TelegramBot(SimpleNamespace(), SimpleNamespace())._persistent_menu()
    labels = [button.text for row in menu.keyboard for button in row]
    assert menu.is_persistent is True
    assert "🌐 אינטרנט" in labels
    assert "🔎 כל המקורות" in labels


def test_pasted_article_becomes_new_text_source():
    from link_reader.service import ContentService

    class StubDB:
        def get_current_content(self, user_id):
            return {"id": 99, "title": "old source"}

    service = ContentService(StubDB(), None, [])
    article = (
        "כותרת הכתבה\n\n"
        + ("זוהי פסקה ארוכה של כתבה מתורגמת עם מידע ותוכן ענייני. " * 12)
        + "\n\n"
        + ("פסקה נוספת שממשיכה את הכתבה ומוסיפה פרטים והסברים. " * 10)
        + "\n\nhttps://example.com/original"
    )
    assert service.looks_like_pasted_source(article) is True
    item = service._pasted_text_item(article)
    assert item.source_type == "text"
    assert item.title == "כותרת הכתבה"
    assert item.url == "https://example.com/original"
    assert item.segments
    assert item.segments[0].reference == "§1"

    long_question = "שאלה: " + ("תסביר לי לעומק את הטענה הזאת ואת ההקשר שלה. " * 40)
    assert service.looks_like_pasted_source(long_question) is False

from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx
from pypdf import PdfReader

from link_reader.processors.webpage import WebPageProcessor
from link_reader.types import ExtractedContent, TranscriptSegment


class PDFProcessor:
    source_type = "pdf"
    max_bytes = 30 * 1024 * 1024
    max_pages = 1200
    min_text_chars_per_page = 80
    default_max_ocr_pages = 80
    ocr_dpi = 180

    def __init__(self, settings):
        self.settings = settings
        self._web_safety = WebPageProcessor(settings)
        self._ocr_languages_cache: str | None = None

    def supports(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme.lower() in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.path.lower().endswith(".pdf")
            )
        except ValueError:
            return False

    def external_id(self, url: str) -> str:
        normalized = self._web_safety._normalize_url(url)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    async def extract(self, url: str) -> ExtractedContent:
        return await asyncio.to_thread(self._extract_sync, url)

    def _extract_sync(self, url: str) -> ExtractedContent:
        normalized = self._web_safety._normalize_url(url)
        final_url, data = self._fetch_pdf(normalized)
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
        except Exception as exc:
            raise ValueError("לא הצלחתי לקרוא את קובץ ה-PDF.") from exc

        if len(reader.pages) > self.max_pages:
            raise ValueError("ה-PDF ארוך מדי לעיבוד כרגע.")

        page_texts: list[str] = []
        ocr_candidates: list[int] = []
        for page_no, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:
                text = ""
            text = self._clean_page(text)
            page_texts.append(text)
            if len(text) < self.min_text_chars_per_page:
                ocr_candidates.append(page_no)

        ocr_used = False
        if ocr_candidates:
            max_ocr_pages = int(getattr(self.settings, "max_ocr_pages", self.default_max_ocr_pages))
            # OCR is deliberately bounded so a large scanned book cannot pin the
            # bot for hours. Text-layer pages remain available regardless.
            if len(ocr_candidates) > max_ocr_pages and sum(map(len, page_texts)) < 120:
                raise ValueError(
                    f"ה-PDF סרוק ודורש OCR ל-{len(ocr_candidates)} עמודים; "
                    f"המגבלה כרגע היא {max_ocr_pages} עמודים."
                )
            if self._ocr_available():
                for page_no in ocr_candidates[:max_ocr_pages]:
                    try:
                        ocr_text = self._ocr_page(data, page_no)
                    except Exception:
                        continue
                    ocr_text = self._clean_page(ocr_text)
                    idx = page_no - 1
                    if len(ocr_text) > len(page_texts[idx]):
                        page_texts[idx] = ocr_text
                        ocr_used = True

        segments: list[TranscriptSegment] = []
        total_chars = 0
        for page_no, text in enumerate(page_texts, start=1):
            if not text:
                continue
            total_chars += len(text)
            for part in self._split_page(text):
                segments.append(
                    TranscriptSegment(
                        start=float(page_no - 1),
                        duration=1.0,
                        text=part,
                        reference=f"p.{page_no}",
                    )
                )

        if not segments or total_chars < 120:
            if not self._ocr_available():
                raise ValueError(
                    "ה-PDF כנראה סרוק ואין בו שכבת טקסט מספקת, ו-OCR אינו זמין בשרת."
                )
            raise ValueError("לא הצלחתי לחלץ מספיק טקסט מה-PDF גם לאחר OCR.")

        metadata = reader.metadata or {}
        title = str(getattr(metadata, "title", "") or "").strip()
        author = str(getattr(metadata, "author", "") or "").strip() or None
        if not title:
            name = PurePosixPath(unquote(urlsplit(final_url).path)).name
            title = name.rsplit(".", 1)[0] if name else "PDF"

        return ExtractedContent(
            external_id=self.external_id(normalized),
            source_type=self.source_type,
            url=final_url,
            title=title[:500],
            author=author[:300] if author else None,
            duration_seconds=None,
            language=None,
            segments=segments,
            extraction_method="pypdf+ocr" if ocr_used else "pypdf",
        )

    def _ocr_available(self) -> bool:
        return bool(shutil.which("pdftoppm") and shutil.which("tesseract"))

    def _ocr_languages(self) -> str:
        if self._ocr_languages_cache:
            return self._ocr_languages_cache
        preferred = ["heb", "eng"]
        available: set[str] = set()
        try:
            result = subprocess.run(
                ["tesseract", "--list-langs"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            available = {
                line.strip()
                for line in (result.stdout + "\n" + result.stderr).splitlines()
                if line.strip() and "List of available" not in line
            }
        except Exception:
            available = {"eng"}
        selected = [lang for lang in preferred if lang in available]
        self._ocr_languages_cache = "+".join(selected) if selected else "eng"
        return self._ocr_languages_cache

    def _ocr_page(self, pdf_data: bytes, page_no: int) -> str:
        with tempfile.TemporaryDirectory(prefix="link-reader-ocr-") as tmp:
            tmpdir = Path(tmp)
            pdf_path = tmpdir / "source.pdf"
            out_prefix = tmpdir / "page"
            image_path = tmpdir / "page.png"
            pdf_path.write_bytes(pdf_data)
            subprocess.run(
                [
                    "pdftoppm",
                    "-f", str(page_no),
                    "-l", str(page_no),
                    "-singlefile",
                    "-r", str(self.ocr_dpi),
                    "-png",
                    str(pdf_path),
                    str(out_prefix),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            result = subprocess.run(
                [
                    "tesseract",
                    str(image_path),
                    "stdout",
                    "-l", self._ocr_languages(),
                    "--psm", "3",
                    "-c", "preserve_interword_spaces=1",
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=True,
            )
            return result.stdout

    def _fetch_pdf(self, url: str) -> tuple[str, bytes]:
        current = url
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; LinkReaderBot/1.0; +https://github.com/evalegalai-cloud/link-reader-bot)",
            "Accept": "application/pdf,*/*;q=0.5",
        }
        with httpx.Client(timeout=httpx.Timeout(35.0, connect=8.0), headers=headers) as client:
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
                        raise ValueError("קובץ ה-PDF גדול מדי.")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise ValueError("קובץ ה-PDF גדול מדי.")
                    raw = bytes(data)
                    content_type = (response.headers.get("content-type") or "").lower()
                    if not raw.startswith(b"%PDF-") and "pdf" not in content_type:
                        raise ValueError("הקישור אינו מחזיר קובץ PDF.")
                    return str(response.url), raw
        raise ValueError("יותר מדי הפניות בדרך ל-PDF.")

    def _clean_page(self, text: str) -> str:
        lines = [" ".join(line.split()) for line in text.replace("\r", "\n").split("\n")]
        return "\n".join(line for line in lines if line)

    def _split_page(self, text: str, max_chars: int = 3500) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        parts: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= max_chars:
                parts.append(remaining.strip())
                break
            cut = remaining.rfind("\n", 0, max_chars)
            if cut < max_chars // 2:
                cut = remaining.rfind(". ", 0, max_chars)
            if cut < max_chars // 2:
                cut = remaining.rfind(" ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            else:
                cut += 1
            parts.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        return [part for part in parts if part]

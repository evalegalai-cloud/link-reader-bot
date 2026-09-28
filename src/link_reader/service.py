from __future__ import annotations

import asyncio
import hashlib
import re

from link_reader.types import ExtractedContent, TranscriptSegment, format_timestamp
from link_reader.transcription import OPENAI_TRANSCRIBE_COST_PER_MINUTE


MAP_SYSTEM = """Create a faithful content map from the transcript excerpt.
Use only the transcript. Do not add external knowledge or guess.
Write 3–8 concise points and preserve useful source references (timestamps, section markers, or page markers) from the source.
When the target output is Hebrew, preserve important foreign proper names, technical terms, titles, Latin/Greek expressions, and other terms whose original spelling matters by including the original-language form in parentheses on first meaningful occurrence.
Finish with the exact marker [[END_OF_MAP]] on a line by itself."""
FINAL_SYSTEM = """Summarize the supplied source faithfully.
Do not add facts that are not present in the source.
Clearly distinguish the speaker's claims, opinions, and predictions from established facts.
Structure the answer as: one-line takeaway, 4–6 key points, and 5–7 short source-reference bullets. For time-based media use timestamps; for articles/documents use section or page markers instead.
Cover the source from beginning to end. Avoid repetition and keep the whole answer concise, roughly 300–450 Hebrew words unless the source genuinely requires more.
When writing in Hebrew, on the first meaningful occurrence of an important foreign proper name, technical term, title, Latin/Greek expression, or term whose original spelling matters, include the original-language form in parentheses after the Hebrew form. Do this selectively, not for ordinary words.
When the output is Hebrew, begin each paragraph and bullet with Hebrew wording whenever possible. Do not begin a Hebrew paragraph or bullet with an English/Latin term; introduce it in Hebrew and put the original form in parentheses.
Use **double asterisks** only for genuine emphasis; the Telegram client will render them as bold.
Finish the response with the exact marker [[END_OF_SUMMARY]] on a line by itself."""
QA_SYSTEM = """Answer only from the supplied excerpts from the current source.
Use recent conversation only to understand what the user is referring to; factual claims must still be supported by the supplied source excerpts.
Do not use outside knowledge unless the user explicitly asks for it.
When possible, cite the exact source reference: a timestamp such as [MM:SS], an article section such as [§12], or a page marker such as [p.4].
If the excerpts do not support an answer, say so clearly instead of guessing.
When answering in Hebrew, on the first meaningful occurrence of an important foreign proper name, technical term, title, Latin/Greek expression, or term whose original spelling matters, include the original-language form in parentheses after the Hebrew form. Do this selectively, not for ordinary words.
Use **double asterisks** only for genuine emphasis; the Telegram client will render them as bold."""
FREEFORM_GENERAL_SYSTEM = """Answer the user's request naturally and directly.
Use general knowledge when no saved source is active.
Do not pretend that a saved source supports a fact when it does not.
Answer in Hebrew by default unless the user asks for another language."""

SOURCE_WEB_SYSTEM = """Answer the user's question using the supplied saved-source excerpts as the primary evidence.
You may use web search only for the external or current context the user is asking for.
Clearly distinguish what comes from the saved source from what comes from external web information.
Do not silently replace the saved source with web material.
Preserve useful source references already present in the excerpts.
Answer in Hebrew by default unless the user asks for another language."""

LIBRARY_QA_SYSTEM = """Answer only from the supplied excerpts from the user's saved source library.
Do not use outside knowledge or guess.
For every substantive claim, cite the source as [#ID] using the supplied source ID.
When an excerpt already contains a page, section, chapter, or timestamp reference, preserve that reference too when useful.
If the retrieved excerpts are insufficient, say so clearly.
Keep sources distinct when they disagree or address different things.
Answer in Hebrew by default unless the user asks for another language."""

TRANSLATE_SYSTEM = """Translate every supplied segment completely and faithfully into the target language.
Do not summarize, shorten, merge, skip, or reorder segments.
Each input segment starts with an internal ID like [S000001]. Preserve every ID exactly once and in the same order.
Do not add timestamps.
When translating into Hebrew, preserve important foreign proper names, technical terms, titles, and Latin/Greek expressions in their original-language form in parentheses where useful.
When translating into Hebrew, begin each paragraph or line with Hebrew wording whenever possible rather than an English/Latin term."""


class ContentService:
    def __init__(self, db, llm, processors, target_language: str = "Hebrew"):
        self.db = db
        self.llm = llm
        self.processors = processors
        self.target_language = target_language

    def processor_for(self, url: str):
        for processor in self.processors:
            if processor.supports(url):
                return processor
        return None

    async def _progress(self, callback, text: str) -> None:
        if callback is not None:
            await callback(text)

    def looks_like_pasted_source(self, text: str) -> bool:
        value = (text or "").strip()
        if len(value) < 350:
            return False
        lowered = value.casefold()
        explicit_question_prefixes = (
            "שאלה:", "שאלה -", "אני רוצה לשאול", "תסביר לי", "ענה לי",
            "בדוק באינטרנט", "חפש באינטרנט", "חפש ברשת", "שאל הכל",
            "question:", "please explain", "search the web", "ask all",
        )
        if lowered.startswith(explicit_question_prefixes):
            return False
        if len(value) >= 1200:
            return True

        conversational_prefixes = (
            "מה ", "למה ", "איך ", "האם ", "מתי ", "איפה ", "מי ",
            "תוכל ", "אפשר ", "אני רוצה לדעת", "בדוק ", "השווה ",
            "explain ", "what ", "why ", "how ", "can you ", "could you ",
        )
        if lowered.startswith(conversational_prefixes):
            return False

        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", value) if p.strip()]
        sentences = len(re.findall(r"[.!?。！？]\s|[.!?。！？]$", value))
        urls = len(re.findall(r"https?://\S+", value))
        return len(paragraphs) >= 3 or sentences >= 5 or (urls >= 1 and len(value) >= 500)

    def _pasted_text_item(self, text: str) -> ExtractedContent:
        value = (text or "").strip()
        if not value:
            raise ValueError("לא התקבל טקסט.")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
        urls = re.findall(r"https?://\S+", value)
        source_url = urls[0].rstrip(".,)>]") if urls else f"text://{digest}"

        lines = [line.strip() for line in value.splitlines() if line.strip()]
        title = "טקסט שהודבק"
        if lines:
            first = re.sub(r"^#+\s*", "", lines[0]).strip()
            if 4 <= len(first) <= 180 and not first.startswith(("http://", "https://")):
                title = first

        paragraphs = [
            p.strip()
            for p in re.split(r"\n\s*\n", value)
            if p.strip()
        ]
        if len(paragraphs) <= 1:
            paragraphs = [
                p.strip()
                for p in re.split(r"(?<=[.!?。！？])\s+", value)
                if p.strip()
            ]
        if not paragraphs:
            paragraphs = [value]

        segments = []
        buffer = []
        chars = 0
        section = 1
        for paragraph in paragraphs:
            if buffer and chars + len(paragraph) > 3000:
                segments.append(
                    TranscriptSegment(
                        start=float(section - 1),
                        duration=1.0,
                        text="\n\n".join(buffer),
                        reference=f"§{section}",
                    )
                )
                section += 1
                buffer = []
                chars = 0
            buffer.append(paragraph)
            chars += len(paragraph) + 2
        if buffer:
            segments.append(
                TranscriptSegment(
                    start=float(section - 1),
                    duration=1.0,
                    text="\n\n".join(buffer),
                    reference=f"§{section}",
                )
            )

        return ExtractedContent(
            external_id=digest,
            source_type="text",
            url=source_url,
            title=title[:500],
            author=None,
            duration_seconds=None,
            language=None,
            segments=segments,
            extraction_method="pasted_text",
        )

    async def ingest_text(self, text: str, user_id: int, progress=None):
        await self._progress(progress, "קורא את הטקסט…")
        item = self._pasted_text_item(text)
        return await self.ingest_item(item, user_id, progress=progress)

    async def ingest(self, url: str, user_id: int, progress=None):
        processor = self.processor_for(url)
        if processor is None:
            raise ValueError("הקישור הזה עדיין לא נתמך.")

        external_id = processor.external_id(url)
        cached = self.db.get_content_by_external_id(processor.source_type, external_id)
        if cached and cached["summary"]:
            self.db.save_user_content(user_id, cached["id"])
            self.db.set_current_content(user_id, cached["id"])
            return cached, True

        await self._progress(progress, "פותח וקורא את המקור…")
        item = await processor.extract(url)
        return await self.ingest_item(item, user_id, cached=cached, progress=progress)

    async def ingest_item(self, item, user_id: int, cached=None, progress=None):
        if cached is None:
            cached = self.db.get_content_by_external_id(item.source_type, item.external_id)
        if cached and cached["summary"]:
            self.db.save_user_content(user_id, cached["id"])
            self.db.set_current_content(user_id, cached["id"])
            return cached, True

        transcript = item.transcript_text
        if not transcript.strip():
            raise RuntimeError("לא התקבל טקסט מהמקור.")

        if cached:
            content_id = cached["id"]
        else:
            content_id = self.db.save_content(item, transcript)

        self.db.save_user_content(user_id, content_id)
        await self._progress(progress, "מכין את המקור…")
        chunks = self._chunk_segments(item.segments)
        self.db.replace_chunks(content_id, chunks)

        usage_token, usage_tracker = self.llm.start_usage_tracking()
        try:
            await self._progress(progress, "מסכם…")
            if len(transcript) <= 80_000:
                time_based = item.source_type in {"youtube", "audio", "social_video", "voice"}
                source_kind = "time-based transcript" if time_based else "section/page-marked document"
                final_prompt = (
                    f"Title: {item.title}\nAuthor/creator: {item.author or 'Unknown'}"
                    f"\nSource type: {item.source_type}\n\nFull source text:\n{transcript}"
                )
                final_system = (
                    FINAL_SYSTEM
                    + f"\nTarget output language: {self.target_language}."
                    + f"\nThe supplied source is the full {source_kind}. Preserve its reference style."
                )
            else:
                mapped = await self._ensure_maps(content_id, self.db.get_chunks(content_id))
                maps = self._maps_text(mapped)
                final_prompt = (
                    f"Title: {item.title}\nAuthor/creator: {item.author or 'Unknown'}"
                    f"\n\nComplete content map:\n{maps}"
                )
                final_system = (
                    FINAL_SYSTEM
                    + f"\nTarget output language: {self.target_language}."
                    + "\nThe supplied source is a complete content map covering the full source. Preserve its reference style."
                )

            summary = await self._complete_checked(
                final_system, final_prompt, "[[END_OF_SUMMARY]]",
                first_budget=2400, retry_budget=3600, tier="smart",
                label="סיכום",
            )
            self.db.set_summary(content_id, summary)
        finally:
            self.llm.stop_usage_tracking(usage_token)

        by_model = usage_tracker.get("by_model", {})
        input_tokens = sum(v.get("input_tokens", 0) for v in by_model.values())
        output_tokens = sum(v.get("output_tokens", 0) for v in by_model.values())
        cached_input_tokens = sum(v.get("cached_input_tokens", 0) for v in by_model.values())
        llm_cost = self.llm.estimate_usage_cost_usd(usage_tracker)
        if item.extraction_method == "supadata_transcript":
            transcript_credits = 1.0
        elif item.extraction_method == "supadata_generated":
            minutes = max(0.0, float(item.duration_seconds or 0.0)) / 60.0
            transcript_credits = max(1.0, minutes * 2.0)
        else:
            transcript_credits = 0.0
        if item.extraction_method == "openai_gpt_transcribe":
            minutes = max(0.0, float(item.duration_seconds or 0.0)) / 60.0
            transcript_cost = minutes * OPENAI_TRANSCRIBE_COST_PER_MINUTE
        else:
            transcript_cost = transcript_credits * 0.01
        self.db.set_processing_stats(
            content_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            llm_cost_usd=llm_cost,
            transcript_credits=transcript_credits,
            transcript_cost_usd=transcript_cost,
        )
        self.db.set_current_content(user_id, content_id)
        return self.db.get_content(content_id), False

    async def _complete_checked(
        self, system: str, prompt: str, marker: str, *,
        first_budget: int, retry_budget: int, tier: str, label: str,
    ) -> str:
        result = await self.llm.complete(
            system, prompt, max_tokens=first_budget, tier=tier, reasoning_effort="none"
        )
        if marker not in result:
            result = await self.llm.complete(
                system
                + f"\nIMPORTANT: the previous {label} was incomplete. Produce it again in full and finish with the required end marker.",
                prompt,
                max_tokens=retry_budget,
                tier=tier,
                reasoning_effort="none",
            )
        if marker not in result:
            raise RuntimeError(f"המודל החזיר {label} לא שלם גם לאחר ניסיון חוזר.")
        return result.split(marker, 1)[0].rstrip()

    def _maps_text(self, chunks) -> str:
        blocks = []
        for c in chunks:
            first_line = (c["text"] or "").splitlines()[0] if c["text"] else ""
            time_based = bool(re.match(r"^\[(?:\d{1,2}:)?\d{2}:\d{2}\]", first_line))
            if time_based:
                label = (
                    f"Chunk {c['ordinal']} ({format_timestamp(c['start_seconds'])}–"
                    f"{format_timestamp(c['end_seconds'])})"
                )
            else:
                label = f"Chunk {c['ordinal']}"
            blocks.append(f"{label}:\n{c['map_summary'] or ''}")
        return "\n\n".join(blocks)

    async def _ensure_maps(self, content_id: int, chunks):
        items = [dict(c) for c in chunks]
        missing = [c for c in items if not (c.get("map_summary") or "").strip()]
        if not missing:
            return chunks

        semaphore = asyncio.Semaphore(6)
        marker = "[[END_OF_MAP]]"

        async def map_one(chunk):
            async with semaphore:
                system = MAP_SYSTEM + f"\nTarget output language: {self.target_language}."
                prompt = f"Excerpt {chunk['ordinal']}:\n\n{chunk['text']}"
                mapped = await self._complete_checked(
                    system, prompt, marker, first_budget=1800, retry_budget=2600,
                    tier="fast", label="מפת תוכן",
                )
                chunk["map_summary"] = mapped

        await asyncio.gather(*(map_one(c) for c in missing))
        self.db.replace_chunks(content_id, items)
        return self.db.get_chunks(content_id)

    def _chunk_segments(self, segments, target_chars: int = 12000):
        chunks = []
        current = []
        chars = 0
        start = 0.0
        end = 0.0
        ordinal = 0

        def flush():
            nonlocal current, chars, start, end, ordinal
            if not current:
                return
            chunks.append({
                "ordinal": ordinal,
                "start_seconds": start,
                "end_seconds": end,
                "text": "\n".join(current),
                "map_summary": None,
            })
            ordinal += 1
            current = []
            chars = 0

        for seg in segments:
            reference = seg.reference or format_timestamp(seg.start)
            line = f"[{reference}] {seg.text.strip()}"
            if current and chars + len(line) > target_chars:
                flush()
            if not current:
                start = seg.start
            current.append(line)
            chars += len(line) + 1
            end = seg.start + seg.duration
        flush()
        return chunks

    def search_library(self, user_id: int, query: str, limit: int = 8):
        return self.db.search_library(user_id, query, limit=limit)

    def library_search_text(self, user_id: int, query: str, limit: int = 6) -> str:
        rows = self.search_library(user_id, query, limit=limit)
        if not rows:
            return "לא מצאתי התאמות בספריית המקורות."
        lines = ["נמצאו התאמות:"]
        seen = set()
        number = 1
        for row in rows:
            key = (row["content_id"], row["ordinal"])
            if key in seen:
                continue
            seen.add(key)
            snippet = re.sub(r"\s+", " ", row["text"] or "").strip()
            if len(snippet) > 260:
                snippet = snippet[:257].rstrip() + "…"
            lines.append(
                f"{number}. [#{row['content_id']}] {row['title']} "
                f"({row['source_type']}, קטע {row['ordinal']})\n{snippet}"
            )
            number += 1
            if number > limit:
                break
        return "\n\n".join(lines)

    async def answer_library(self, user_id: int, question: str) -> str:
        rows = self.search_library(user_id, question, limit=10)
        if not rows:
            return "לא מצאתי בספריית המקורות חומר שמספיק כדי לענות על השאלה."
        evidence = []
        for row in rows:
            text = (row["text"] or "").strip()
            if len(text) > 5000:
                text = text[:5000].rstrip() + "…"
            evidence.append(
                f"--- Source #{row['content_id']} | {row['title']} | "
                f"{row['source_type']} | chunk {row['ordinal']} ---\n{text}"
            )
        prompt = (
            f"שאלה: {question}\n\n"
            "קטעים שנשלפו מספריית המקורות:\n" + "\n\n".join(evidence)
        )
        return await self.llm.complete(
            LIBRARY_QA_SYSTEM + f"\nDefault answer language: {self.target_language}.",
            prompt,
            max_tokens=1400,
            tier="smart",
            reasoning_effort="none",
        )

    def _freeform_mode(self, user_id: int, text: str, force_mode: str | None = None) -> str:
        if force_mode in {"source", "library", "web", "general"}:
            return force_mode
        lowered = (text or "").casefold()
        library_signals = (
            "בכל המקורות", "מכל המקורות", "בספרייה", "בספרית", "כל המקורות",
            "השווה בין המקורות", "חוצה מקורות", "שאל הכל", "ask all", "library",
        )
        web_signals = (
            "באינטרנט", "מהאינטרנט", "ברשת", "חפש ברשת", "חפש באינטרנט",
            "בדוק באינטרנט", "ידע כללי", "מידע חיצוני", "מקור חיצוני",
            "מעבר לסרטון", "מעבר למסמך", "מעבר לספר", "מה ידוע היום",
            "נכון להיום", "נכון לעכשיו", "עדכני", "הכי חדש", "האחרון ביותר",
            "latest", "current", "on the web", "internet", "web search",
        )
        freshness_signals = (
            "היום", "כרגע", "עכשיו", "השבוע", "החודש", "לאחרונה",
            "מחיר נוכחי", "שער נוכחי", "latest", "today", "currently", "recent",
        )
        if any(signal in lowered for signal in library_signals):
            return "library"
        if any(signal in lowered for signal in web_signals):
            return "web"
        current = self.db.get_current_content(user_id)
        if current:
            changed_since_signals = (
                "מה קרה מאז", "מה השתנה מאז", "השתנה מאז", "מאז הסרטון",
                "מאז המסמך", "מאז הספר", "לעומת היום", "לעומת המצב היום",
            )
            if any(signal in lowered for signal in changed_since_signals):
                return "web"
            return "source"
        if any(signal in lowered for signal in freshness_signals):
            return "web"
        return "general"

    async def answer_freeform(
        self, user_id: int, question: str, force_mode: str | None = None
    ) -> tuple[str, str]:
        mode = self._freeform_mode(user_id, question, force_mode=force_mode)
        if mode == "library":
            return await self.answer_library(user_id, question), mode
        if mode == "web":
            return await self.answer_with_web(user_id, question), mode
        if mode == "source":
            return await self.answer(user_id, question), mode

        answer = await self.llm.complete(
            FREEFORM_GENERAL_SYSTEM
            + f"\nDefault answer language: {self.target_language}.",
            question,
            max_tokens=1200,
            tier="smart",
            reasoning_effort="none",
        )
        return answer, "general"

    async def answer_with_web(self, user_id: int, question: str) -> str:
        content = self.db.get_current_content(user_id)
        if not content:
            return await self.llm.complete_with_web(
                FREEFORM_GENERAL_SYSTEM
                + "\nUse web search for current/external factual information and cite it.",
                question,
                max_tokens=1600,
                tier="smart",
                max_tool_calls=3,
            )

        chunks = self.db.get_chunks(content["id"])
        if not chunks:
            raise RuntimeError("לא נמצאו מקטעים שמורים למקור.")
        history = self.db.get_recent_qa(user_id, content["id"], limit=4)
        history_text = self._conversation_context(history)
        total_chars = sum(len(c["text"]) for c in chunks)
        selected = list(chunks) if total_chars <= 45_000 else await self._select_chunks(
            question, chunks, history_text
        )
        evidence = "\n\n".join(
            f"--- Saved source chunk {c['ordinal']} ---\n{c['text']}" for c in selected
        )
        prompt = (
            f"Saved source title: {content['title']}\n"
            f"User question: {question}\n\n"
            f"Saved-source excerpts:\n{evidence}\n\n"
            "Use the saved source as the primary frame. Search the web only for the "
            "external/current context requested by the user."
        )
        return await self.llm.complete_with_web(
            SOURCE_WEB_SYSTEM
            + f"\nDefault answer language: {self.target_language}.",
            prompt,
            max_tokens=1800,
            tier="smart",
            max_tool_calls=3,
        )

    async def answer(self, user_id: int, question: str) -> str:
        content = self.db.get_current_content(user_id)
        if not content:
            raise ValueError("שלח קודם קישור.")
        chunks = self.db.get_chunks(content["id"])
        if not chunks:
            raise RuntimeError("לא נמצאו מקטעים שמורים למקור.")

        history = self.db.get_recent_qa(user_id, content["id"], limit=6)
        history_text = self._conversation_context(history)

        total_chars = sum(len(c["text"]) for c in chunks)
        if total_chars <= 55_000:
            selected = list(chunks)
        else:
            selected = await self._select_chunks(question, chunks, history_text)
        evidence = "\n\n".join(
            f"--- Chunk {c['ordinal']} ---\n{c['text']}" for c in selected
        )
        prompt = (
            f"Source title: {content['title']}\n"
            f"Question: {question}\n\n"
            f"Recent conversation about this source (use it to resolve follow-ups and references):\n{history_text or 'None'}\n\n"
            f"Source excerpts:\n{evidence}"
        )
        lower = question.lower()
        detailed = any(x in lower for x in ("מפורט", "בהרחבה", "לעומק", "detailed", "comprehensive"))
        budget = 1600 if detailed else 900
        answer = await self.llm.complete(
            QA_SYSTEM
            + f"\nDefault answer language: {self.target_language}, unless the user explicitly asks for another language."
            + "\nAnswer directly and avoid repetition. Unless the user asks for detail, lead with the answer and use 3–5 supporting points.",
            prompt,
            max_tokens=budget,
            tier="smart",
            reasoning_effort="none",
        )
        self.db.save_qa(user_id, content["id"], question, answer)
        return answer

    def _conversation_context(self, history, max_chars: int = 5000) -> str:
        if not history:
            return ""
        blocks = []
        for row in history:
            question = (row["question"] or "").strip()
            answer = (row["answer"] or "").strip()[:1000]
            blocks.append(f"User: {question}\nAssistant: {answer}")
        text = "\n\n".join(blocks)
        return text[-max_chars:]

    async def _select_chunks(self, question: str, chunks, history_text: str = ""):
        if any(not (c["map_summary"] or "").strip() for c in chunks):
            chunks = await self._ensure_maps(chunks[0]["content_id"], chunks)
        index = "\n\n".join(
            f"Chunk {c['ordinal']}: {c['map_summary'] or ''}" for c in chunks
        )
        routing = await self.llm.complete(
            "Select up to 6 transcript chunks relevant to the current question. "
            "Use the recent conversation only to resolve pronouns, ellipsis, and follow-up references. "
            "Return only chunk numbers separated by commas.",
            f"Recent conversation:\n{history_text or 'None'}\n\n"
            f"Current question: {question}\n\nContent map:\n{index}",
            max_tokens=64,
            tier="fast",
            reasoning_effort="none",
        )
        wanted = []
        for num in re.findall(r"\d+", routing):
            i = int(num)
            if 0 <= i < len(chunks) and i not in wanted:
                wanted.append(i)
            if len(wanted) == 6:
                break
        if not wanted:
            wanted = list(range(min(4, len(chunks))))
        return [chunks[i] for i in wanted]

    async def translate_current(self, user_id: int) -> tuple[str, str]:
        content = self.db.get_current_content(user_id)
        if not content:
            raise ValueError("שלח קודם קישור.")
        chunks = self.db.get_chunks(content["id"])
        if not chunks:
            raise RuntimeError("לא נמצאו מקטעים שמורים למקור.")

        parts = self._translation_parts(chunks, target_chars=1800)
        semaphore = asyncio.Semaphore(8)
        system = TRANSLATE_SYSTEM + f"\nTranslate into: {self.target_language}."

        async def translate_units(units, depth: int = 0) -> str:
            source = "\n".join(f"[{sid}] {text}" for sid, text in units)
            expected = [sid for sid, _ in units]
            async with semaphore:
                result = await self.llm.complete(
                    system, source, max_tokens=2800, tier="fast", reasoning_effort="none"
                )
            found = ["S" + x for x in re.findall(r"\[S(\d{6})\]", result)]
            if found == expected:
                return re.sub(r"\[S\d{6}\]\s*", "", result).strip()

            if len(units) <= 1 or depth >= 4:
                # One strict retry for a single stubborn unit.
                async with semaphore:
                    retry = await self.llm.complete(
                        system
                        + "\nCRITICAL: preserve the single segment ID exactly and translate every word.",
                        source,
                        max_tokens=1400,
                        tier="fast",
                        reasoning_effort="none",
                    )
                retry_found = ["S" + x for x in re.findall(r"\[S(\d{6})\]", retry)]
                if retry_found != expected:
                    raise RuntimeError("חלק מהתרגום לא עבר בדיקת שלמות גם לאחר ניסיון חוזר.")
                return re.sub(r"\[S\d{6}\]\s*", "", retry).strip()

            mid = len(units) // 2
            left, right = await asyncio.gather(
                translate_units(units[:mid], depth + 1),
                translate_units(units[mid:], depth + 1),
            )
            return left + "\n" + right

        translated = await asyncio.gather(*(translate_units(part) for part in parts))
        text = "\n\n".join(translated)
        if self.target_language.casefold() in {"hebrew", "עברית", "he"}:
            rlm = "\u200f"
            hebrew = re.compile(r"[\u0590-\u05FF]")
            text = "\n".join(
                (rlm + line) if line.strip() and hebrew.search(line) else line
                for line in text.split("\n")
            )
        safe_title = re.sub(r"[^\w\- ]+", "", content["title"], flags=re.UNICODE).strip()[:70]
        return (safe_title or "content") + "-translated.txt", text

    def _translation_parts(self, chunks, target_chars: int = 1800):
        units = []
        ordinal = 1
        for chunk in chunks:
            for raw in chunk["text"].splitlines():
                clean = re.sub(r"^\[[^\]]+\]\s*", "", raw).strip()
                if clean:
                    units.append((f"S{ordinal:06d}", clean))
                    ordinal += 1

        parts = []
        current = []
        chars = 0
        for sid, text in units:
            line_len = len(sid) + len(text) + 4
            if current and chars + line_len > target_chars:
                parts.append(current)
                current = []
                chars = 0
            current.append((sid, text))
            chars += line_len
        if current:
            parts.append(current)
        return parts

    def transcript_current(self, user_id: int) -> tuple[str, str]:
        content = self.db.get_current_content(user_id)
        if not content:
            raise ValueError("שלח קודם קישור.")
        safe_title = re.sub(r"[^\w\- ]+", "", content["title"], flags=re.UNICODE).strip()[:70]
        suffix = "transcript" if content["source_type"] in {"youtube", "audio", "social_video", "voice"} else "source"
        return (safe_title or "content") + f"-{suffix}.txt", content["transcript"]

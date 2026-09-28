from __future__ import annotations

import asyncio
import re

from link_reader.types import format_timestamp


MAP_SYSTEM = """Create a faithful content map from the transcript excerpt.
Use only the transcript. Do not add external knowledge or guess.
Write 3–8 concise points and preserve useful timestamps from the source.
When the target output is Hebrew, preserve important foreign proper names, technical terms, titles, Latin/Greek expressions, and other terms whose original spelling matters by including the original-language form in parentheses on first meaningful occurrence.
The map will later be used to route follow-up questions to the right excerpt."""
FINAL_SYSTEM = """Summarize the video faithfully from the supplied content map.
Do not add facts that are not present in the source.
Clearly distinguish the speaker's claims, opinions, and predictions from established facts.
Structure the answer as: one-line takeaway, key points, and a timestamped timeline.
When writing in Hebrew, on the first meaningful occurrence of an important foreign proper name, technical term, title, Latin/Greek expression, or term whose original spelling matters, include the original-language form in parentheses after the Hebrew form. Do this selectively, not for ordinary words.
Use **double asterisks** only for genuine emphasis; the Telegram client will render them as bold.
Finish the response with the exact marker [[END_OF_SUMMARY]] on a line by itself."""
QA_SYSTEM = """Answer only from the supplied transcript excerpts.
Do not use outside knowledge unless the user explicitly asks for it.
When possible, cite the exact timestamp as [MM:SS] or [HH:MM:SS].
If the excerpts do not support an answer, say so clearly instead of guessing.
When answering in Hebrew, on the first meaningful occurrence of an important foreign proper name, technical term, title, Latin/Greek expression, or term whose original spelling matters, include the original-language form in parentheses after the Hebrew form. Do this selectively, not for ordinary words.
Use **double asterisks** only for genuine emphasis; the Telegram client will render them as bold."""
TRANSLATE_SYSTEM = """Translate the supplied transcript passage completely and faithfully.
Translate every sentence. Do not summarize, shorten, skip, or reorder material.
The input has no timestamps; do not add timestamps or time references of your own.
When translating into Hebrew, preserve important foreign proper names, technical terms, titles, and Latin/Greek expressions in their original-language form in parentheses on first meaningful occurrence.
Finish with the exact marker [[END_OF_TRANSLATION_PART]] on a line by itself."""


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

    async def ingest(self, url: str, user_id: int):
        processor = self.processor_for(url)
        if processor is None:
            raise ValueError("כרגע הבוט תומך בקישורי YouTube בלבד.")

        external_id = processor.external_id(url)
        cached = self.db.get_content_by_external_id(processor.source_type, external_id)
        if cached and cached["summary"]:
            self.db.set_current_content(user_id, cached["id"])
            return cached, True

        item = await processor.extract(url)
        transcript = item.transcript_text
        if not transcript.strip():
            raise RuntimeError("לא התקבל תמלול מהסרטון.")

        if cached:
            content_id = cached["id"]
        else:
            content_id = self.db.save_content(item, transcript)

        usage_token, usage_tracker = self.llm.start_usage_tracking()
        try:
            chunks = self._chunk_segments(item.segments)
            semaphore = asyncio.Semaphore(3)

            async def summarize_chunk(chunk):
                async with semaphore:
                    chunk["map_summary"] = await self.llm.complete(
                        MAP_SYSTEM + f"\nTarget output language: {self.target_language}.",
                        f"Excerpt {chunk['ordinal']}:\n\n{chunk['text']}",
                        max_tokens=4000,
                        tier="fast",
                    )
                    return chunk

            chunks = await asyncio.gather(*(summarize_chunk(c) for c in chunks))
            self.db.replace_chunks(content_id, chunks)

            maps = "\n\n".join(
                f"Chunk {c['ordinal']} ({format_timestamp(c['start_seconds'])}–"
                f"{format_timestamp(c['end_seconds'])}):\n{c['map_summary']}"
                for c in chunks
            )
            final_prompt = (
                f"Title: {item.title}\nCreator/channel: {item.author or 'Unknown'}\n\n{maps}"
            )
            summary = await self.llm.complete(
                FINAL_SYSTEM + f"\nTarget output language: {self.target_language}.",
                final_prompt,
                max_tokens=7000,
                tier="smart",
            )
            marker = "[[END_OF_SUMMARY]]"
            if marker not in summary:
                summary = await self.llm.complete(
                    FINAL_SYSTEM
                    + f"\nTarget output language: {self.target_language}."
                    + "\nIMPORTANT: the previous generation was truncated. Produce the complete summary from scratch and do not stop before the end marker.",
                    final_prompt,
                    max_tokens=12000,
                    tier="smart",
                )
            if marker not in summary:
                raise RuntimeError("המודל החזיר סיכום לא שלם גם לאחר ניסיון חוזר.")
            summary = summary.split(marker, 1)[0].rstrip()
            self.db.set_summary(content_id, summary)
        finally:
            self.llm.stop_usage_tracking(usage_token)

        by_model = usage_tracker.get("by_model", {})
        input_tokens = sum(v.get("input_tokens", 0) for v in by_model.values())
        output_tokens = sum(v.get("output_tokens", 0) for v in by_model.values())
        cached_input_tokens = sum(v.get("cached_input_tokens", 0) for v in by_model.values())
        llm_cost = self.llm.estimate_usage_cost_usd(usage_tracker)

        # Supadata native transcript = 1 credit. We use $0.01/credit as a
        # conservative API-equivalent reference (paid auto-recharge rate);
        # actual billed cost can be $0 when covered by included credits.
        transcript_credits = 1.0 if item.extraction_method == "supadata_transcript" else 0.0
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
            line = f"[{format_timestamp(seg.start)}] {seg.text.strip()}"
            if current and chars + len(line) > target_chars:
                flush()
            if not current:
                start = seg.start
            current.append(line)
            chars += len(line) + 1
            end = seg.start + seg.duration
        flush()
        return chunks

    async def answer(self, user_id: int, question: str) -> str:
        content = self.db.get_current_content(user_id)
        if not content:
            raise ValueError("שלח קודם קישור לסרטון YouTube.")
        chunks = self.db.get_chunks(content["id"])
        if not chunks:
            raise RuntimeError("לא נמצאו מקטעים שמורים לסרטון.")

        total_chars = sum(len(c["text"]) for c in chunks)
        if total_chars <= 55000:
            selected = list(chunks)
        else:
            selected = await self._select_chunks(question, chunks)

        history = self.db.get_recent_qa(user_id, content["id"], limit=4)
        history_text = "\n".join(
            f"Previous question: {row['question']}\nPrevious answer: {row['answer'][:1200]}"
            for row in history
        )
        evidence = "\n\n".join(
            f"--- Chunk {c['ordinal']} ---\n{c['text']}" for c in selected
        )
        prompt = (
            f"Video title: {content['title']}\n"
            f"Question: {question}\n\n"
            f"Previous Q&A context (only for resolving references):\n{history_text or 'None'}\n\n"
            f"Transcript excerpts:\n{evidence}"
        )
        answer = await self.llm.complete(QA_SYSTEM + f"\nDefault answer language: {self.target_language}, unless the user explicitly asks for another language.", prompt, max_tokens=5000, tier="smart")
        self.db.save_qa(user_id, content["id"], question, answer)
        return answer

    async def _select_chunks(self, question: str, chunks):
        index = "\n\n".join(
            f"Chunk {c['ordinal']}: {c['map_summary'] or ''}" for c in chunks
        )
        routing = await self.llm.complete(
            "Select up to 6 transcript chunks most relevant to the question. Return only chunk numbers separated by commas.",
            f"Question: {question}\n\nContent map:\n{index}",
            max_tokens=3500,
            tier="fast",
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
            raise ValueError("שלח קודם קישור לסרטון YouTube.")
        chunks = self.db.get_chunks(content["id"])
        if not chunks:
            raise RuntimeError("לא נמצאו מקטעים שמורים לסרטון.")

        parts = self._translation_parts(chunks)
        marker = "[[END_OF_TRANSLATION_PART]]"
        semaphore = asyncio.Semaphore(6)

        async def translate_part(index: int, part: str) -> str:
            system = TRANSLATE_SYSTEM + f"\nTranslate into: {self.target_language}."
            prompt = part
            async with semaphore:
                result = await self.llm.complete(
                    system, prompt, max_tokens=8000, tier="fast"
                )
                if marker not in result:
                    result = await self.llm.complete(
                        system
                        + "\nIMPORTANT: the previous translation was truncated. Translate this entire part again and do not stop before the end marker.",
                        prompt,
                        max_tokens=12000,
                        tier="fast",
                    )
                if marker not in result:
                    raise RuntimeError(
                        f"התרגום נקטע בחלק {index + 1} גם לאחר ניסיון חוזר."
                    )
                return result.split(marker, 1)[0].rstrip()

        translated = await asyncio.gather(
            *(translate_part(i, part) for i, part in enumerate(parts))
        )
        safe_title = re.sub(r"[^\w\- ]+", "", content["title"], flags=re.UNICODE).strip()[:70]
        return (safe_title or "youtube") + "-translated.txt", "\n\n".join(translated)

    def _translation_parts(self, chunks, target_chars: int = 2000) -> list[str]:
        lines = []
        for chunk in chunks:
            for raw in chunk["text"].splitlines():
                clean = re.sub(r"^\[[0-9:]+\]\s*", "", raw).strip()
                if clean:
                    lines.append(clean)

        parts = []
        current = []
        chars = 0
        for line in lines:
            if current and chars + len(line) + 1 > target_chars:
                parts.append("\n".join(current))
                current = []
                chars = 0
            current.append(line)
            chars += len(line) + 1
        if current:
            parts.append("\n".join(current))
        return parts

    def transcript_current(self, user_id: int) -> tuple[str, str]:
        content = self.db.get_current_content(user_id)
        if not content:
            raise ValueError("שלח קודם קישור לסרטון YouTube.")
        safe_title = re.sub(r"[^\w\- ]+", "", content["title"], flags=re.UNICODE).strip()[:70]
        return (safe_title or "youtube") + "-transcript.txt", content["transcript"]

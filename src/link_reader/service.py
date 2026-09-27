from __future__ import annotations

import asyncio
import re

from link_reader.types import format_timestamp


MAP_SYSTEM = """Create a faithful content map from the transcript excerpt.
Use only the transcript. Do not add external knowledge or guess.
Write 3–8 concise points and preserve useful timestamps from the source.
The map will later be used to route follow-up questions to the right excerpt."""
FINAL_SYSTEM = """Summarize the video faithfully from the supplied content map.
Do not add facts that are not present in the source.
Clearly distinguish the speaker's claims, opinions, and predictions from established facts.
Structure the answer as: one-line takeaway, key points, and a timestamped timeline."""
QA_SYSTEM = """Answer only from the supplied transcript excerpts.
Do not use outside knowledge unless the user explicitly asks for it.
When possible, cite the exact timestamp as [MM:SS] or [HH:MM:SS].
If the excerpts do not support an answer, say so clearly instead of guessing."""
TRANSLATE_SYSTEM = """Translate the transcript faithfully and naturally.
Preserve every timestamp exactly as written. Do not summarize or omit material information."""


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
        summary = await self.llm.complete(
            FINAL_SYSTEM + f"\nTarget output language: {self.target_language}.",
            f"Title: {item.title}\nCreator/channel: {item.author or 'Unknown'}\n\n{maps}",
            max_tokens=5000,
            tier="smart",
        )
        self.db.set_summary(content_id, summary)
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
        translated = []
        for chunk in chunks:
            translated.append(
                await self.llm.complete(TRANSLATE_SYSTEM + f"\nTranslate into: {self.target_language}.", chunk["text"], max_tokens=9000, tier="fast")
            )
        safe_title = re.sub(r"[^\w\- ]+", "", content["title"], flags=re.UNICODE).strip()[:70]
        return (safe_title or "youtube") + "-translated.txt", "\n\n".join(translated)

    def transcript_current(self, user_id: int) -> tuple[str, str]:
        content = self.db.get_current_content(user_id)
        if not content:
            raise ValueError("שלח קודם קישור לסרטון YouTube.")
        safe_title = re.sub(r"[^\w\- ]+", "", content["title"], flags=re.UNICODE).strip()[:70]
        return (safe_title or "youtube") + "-transcript.txt", content["transcript"]

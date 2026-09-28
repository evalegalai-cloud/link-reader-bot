from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import time
import tempfile
from pathlib import Path
from collections import defaultdict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://\S+")


class TelegramBot:
    def __init__(self, settings, service):
        self.settings = settings
        self.service = service
        self._locks = defaultdict(asyncio.Lock)

    def build_application(self):
        app = (
            ApplicationBuilder()
            .token(self.settings.telegram_bot_token)
            .concurrent_updates(4)
            .build()
        )
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(CommandHandler("translate", self.translate))
        app.add_handler(CommandHandler("transcript", self.transcript))
        app.add_handler(CommandHandler("videos", self.videos))
        app.add_handler(CommandHandler("use", self.use_content))
        app.add_handler(CallbackQueryHandler(self.callback))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, self.audio_message))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.message))
        app.add_error_handler(self.error_handler)
        return app

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        if not user:
            return False
        if self.settings.allowed_user_ids:
            return user.id in self.settings.allowed_user_ids
        owner_id = self.service.db.get_bot_owner()
        return owner_id is not None and user.id == owner_id

    async def _deny(self, update: Update):
        if update.effective_message:
            await update.effective_message.reply_text("אין הרשאה להשתמש בבוט הזה.")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return
        if not self.settings.allowed_user_ids and self.service.db.get_bot_owner() is None:
            if chat.type != "private":
                return await self._deny(update)
            if not self.service.db.claim_bot_owner(user.id):
                return await self._deny(update)
            logger.info("Telegram bot owner claimed on first private /start")
        if not self._authorized(update):
            return await self._deny(update)
        await update.effective_message.reply_text(
            self._home_text(user.id),
            reply_markup=self._home_keyboard(),
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        await update.effective_message.reply_text(
            self._supported_text(),
            reply_markup=self._nav_keyboard(),
        )

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        text = (update.effective_message.text or "").strip()
        match = URL_RE.search(text)
        user_id = update.effective_user.id
        if match:
            return await self._handle_url(update, match.group(0), user_id)
        return await self._handle_question(update, text, user_id)

    async def audio_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        message = update.effective_message
        user_id = update.effective_user.id
        media = message.voice or message.audio or message.document
        if media is None:
            return

        is_voice = message.voice is not None
        duration = int(getattr(media, "duration", 0) or 0)
        current = self.service.db.get_current_content(user_id)
        as_question = bool(is_voice and current is not None and duration <= 120)
        status = await message.reply_text("מקשיב…" if as_question else "מתמלל…")

        suffix = ".ogg" if is_voice else Path(getattr(media, "file_name", "") or "audio.bin").suffix or ".bin"
        tmp_path = None
        try:
            async with self._locks[user_id]:
                with tempfile.NamedTemporaryFile(prefix="link-reader-voice-", suffix=suffix, delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                telegram_file = await context.bot.get_file(media.file_id)
                await telegram_file.download_to_drive(custom_path=str(tmp_path))

                processor = next(
                    (p for p in self.service.processors if p.__class__.__name__ == "AudioProcessor"),
                    None,
                )
                if processor is None:
                    raise RuntimeError("תמלול אודיו אינו זמין כרגע.")

                source_type = "voice" if is_voice else "audio"
                title = "הודעה קולית" if is_voice else (getattr(media, "file_name", None) or "קובץ אודיו")
                item = await processor.extract_local_file(
                    tmp_path,
                    external_id=f"telegram-{media.file_unique_id}",
                    title=title,
                    source_type=source_type,
                    url=f"telegram://{source_type}/{media.file_unique_id}",
                )

                if as_question:
                    question = " ".join(seg.text.strip() for seg in item.segments if seg.text.strip())
                    if not question:
                        raise ValueError("לא הצלחתי להבין את ההודעה הקולית.")
                    answer = await self.service.answer(user_id, question)
                    await status.delete()
                    return await self._send_long(
                        message, answer, reply_markup=self._content_keyboard()
                    )

                started = time.monotonic()
                content, cached = await self.service.ingest_item(item, user_id)
                elapsed = time.monotonic() - started
                prefix = "שמור\n\n" if cached else ""
                footer = f"\n\n**זמן:** {self._format_duration(elapsed)}"
                keys = set(content.keys())
                cost = content["processing_cost_usd"] if "processing_cost_usd" in keys else None
                if cost is not None:
                    footer += f" · **עלות:** {self._format_cost(cost)}"
                body = f"{prefix}{content['title']}\n\n{content['summary']}{footer}"
                chunks = self._split_text(body)
                await status.edit_text(
                    self._telegram_html(chunks[0]),
                    parse_mode="HTML",
                    reply_markup=self._content_keyboard() if len(chunks) == 1 else None,
                )
                for i, chunk in enumerate(chunks[1:], start=1):
                    await message.reply_text(
                        self._telegram_html(chunk),
                        parse_mode="HTML",
                        reply_markup=self._content_keyboard() if i == len(chunks) - 1 else None,
                    )
        except Exception as exc:
            logger.exception("Failed processing Telegram audio")
            await status.edit_text(
                self._friendly_error(exc),
                reply_markup=self._content_keyboard() if current else self._home_keyboard(),
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    async def _handle_url(self, update: Update, url: str, user_id: int):
        async with self._locks[user_id]:
            started = time.monotonic()
            status = await update.effective_message.reply_text("מעבד…")
            try:
                content, cached = await self.service.ingest(url, user_id)
                elapsed = time.monotonic() - started
                prefix = "שמור\n\n" if cached else ""
                footer = f"\n\n**זמן:** {self._format_duration(elapsed)}"
                keys = set(content.keys())
                cost = content["processing_cost_usd"] if "processing_cost_usd" in keys else None
                if cost is not None:
                    footer += f" · **עלות:** {self._format_cost(cost)}"
                body = f"{prefix}{content['title']}\n\n{content['summary']}{footer}"
                chunks = self._split_text(body)
                await status.edit_text(
                    self._telegram_html(chunks[0]),
                    parse_mode="HTML",
                    reply_markup=self._content_keyboard() if len(chunks) == 1 else None,
                )
                for i, chunk in enumerate(chunks[1:], start=1):
                    await update.effective_message.reply_text(
                        self._telegram_html(chunk),
                        parse_mode="HTML",
                        reply_markup=self._content_keyboard() if i == len(chunks) - 1 else None,
                    )
            except Exception as exc:
                logger.exception("Failed processing URL")
                await status.edit_text(
                    f"לא הצלחתי לעבד: {self._friendly_error(exc)}",
                    reply_markup=self._home_keyboard(),
                )

    async def _handle_question(self, update: Update, question: str, user_id: int):
        if not question:
            return
        async with self._locks[user_id]:
            status = await update.effective_message.reply_text("בודק…")
            try:
                answer = await self.service.answer(user_id, question)
                await status.delete()
                await self._send_long(
                    update.effective_message, answer, reply_markup=self._content_keyboard()
                )
            except Exception as exc:
                await status.edit_text(
                    self._friendly_error(exc), reply_markup=self._content_keyboard()
                )

    async def translate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        user_id = update.effective_user.id
        async with self._locks[user_id]:
            status = await update.effective_message.reply_text("מתרגם…")
            try:
                filename, text = await self.service.translate_current(user_id)
                data = io.BytesIO(text.encode("utf-8"))
                data.name = filename
                await update.effective_message.reply_document(
                    document=data,
                    filename=filename,
                    caption="תרגום מלא",
                    reply_markup=self._content_keyboard(),
                )
                await status.delete()
            except Exception as exc:
                await status.edit_text(
                    self._friendly_error(exc), reply_markup=self._content_keyboard()
                )

    async def transcript(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        try:
            filename, text = self.service.transcript_current(update.effective_user.id)
            data = io.BytesIO(text.encode("utf-8"))
            data.name = filename
            await update.effective_message.reply_document(
                document=data,
                filename=filename,
                caption="טקסט מלא",
                reply_markup=self._content_keyboard(),
            )
        except Exception as exc:
            await update.effective_message.reply_text(
                self._friendly_error(exc), reply_markup=self._content_keyboard()
            )

    async def videos(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        rows = self.service.db.list_recent_content(10)
        if not rows:
            return await update.effective_message.reply_text("אין עדיין מקורות שמורים.")
        await update.effective_message.reply_text(
            "בחר מקור:",
            reply_markup=self._videos_keyboard(rows),
        )

    async def use_content(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        if not context.args or not context.args[0].isdigit():
            return await update.effective_message.reply_text("/use ID")
        content = self.service.db.get_content(int(context.args[0]))
        if not content:
            return await update.effective_message.reply_text("לא מצאתי את המקור.")
        self.service.db.set_current_content(update.effective_user.id, content["id"])
        await update.effective_message.reply_text(
            f"נבחר: {content['title']}",
            reply_markup=self._content_keyboard(),
        )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not self._authorized(update):
            return await query.message.reply_text("אין הרשאה להשתמש בבוט הזה.")
        user_id = update.effective_user.id

        if query.data == "home":
            return await query.edit_message_text(
                self._home_text(user_id), reply_markup=self._home_keyboard()
            )
        if query.data == "help":
            return await query.edit_message_text(
                self._supported_text(), reply_markup=self._nav_keyboard()
            )
        if query.data == "current":
            content = self.service.db.get_current_content(user_id)
            if not content:
                return await query.edit_message_text(
                    "אין מקור נוכחי. שלח קישור.", reply_markup=self._home_keyboard()
                )
            return await query.edit_message_text(
                f"מקור נוכחי:\n{content['title']}",
                reply_markup=self._content_keyboard(),
            )
        if query.data == "ask":
            return await query.edit_message_text(
                "כתוב את השאלה שלך על המקור הנוכחי.",
                reply_markup=self._nav_keyboard(),
            )
        if query.data == "translate":
            return await self.translate(update, context)
        if query.data == "transcript":
            return await self.transcript(update, context)
        if query.data == "videos":
            rows = self.service.db.list_recent_content(10)
            if not rows:
                return await query.edit_message_text(
                    "אין עדיין מקורות שמורים.", reply_markup=self._nav_keyboard()
                )
            return await query.edit_message_text(
                "בחר מקור:", reply_markup=self._videos_keyboard(rows)
            )
        if query.data and query.data.startswith("use:"):
            content_id = query.data.split(":", 1)[1]
            if not content_id.isdigit():
                return
            content = self.service.db.get_content(int(content_id))
            if not content:
                return await query.edit_message_text(
                    "לא מצאתי את המקור.", reply_markup=self._nav_keyboard()
                )
            self.service.db.set_current_content(user_id, content["id"])
            return await query.edit_message_text(
                f"נבחר:\n{content['title']}",
                reply_markup=self._content_keyboard(),
            )

    def _home_text(self, user_id: int) -> str:
        current = self.service.db.get_current_content(user_id)
        if current:
            return f"שלח קישור חדש, או המשך עם:\n{current['title']}"
        return "שלח קישור או הודעה קולית כדי להתחיל."

    def _supported_text(self) -> str:
        return (
            "נתמך עכשיו:\n"
            "• YouTube\n"
            "• כתבות ואתרים\n"
            "• PDF עם שכבת טקסט\n"
            "• הודעות קוליות וקבצי אודיו\n"
            "• MP3 / M4A / WAV ועוד\n"
            "• TikTok / Instagram / Facebook ציבוריים\n"
            "• X עם וידאו — ניסיוני\n\n"
            "PDF סרוק ללא טקסט עדיין דורש OCR."
        )

    def _home_keyboard(self):
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("מקור נוכחי", callback_data="current"),
                InlineKeyboardButton("אחרונים", callback_data="videos"),
            ],
            [InlineKeyboardButton("מה נתמך", callback_data="help")],
        ])

    def _nav_keyboard(self):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("מקור נוכחי", callback_data="current"),
            InlineKeyboardButton("תפריט ראשי", callback_data="home"),
        ]])

    def _content_keyboard(self, include_ask: bool = True):
        rows = []
        if include_ask:
            rows.append([InlineKeyboardButton("שאל שאלה", callback_data="ask")])
        rows.extend([
            [
                InlineKeyboardButton("תרגום מלא", callback_data="translate"),
                InlineKeyboardButton("טקסט מלא", callback_data="transcript"),
            ],
            [
                InlineKeyboardButton("אחרונים", callback_data="videos"),
                InlineKeyboardButton("תפריט ראשי", callback_data="home"),
            ],
        ])
        return InlineKeyboardMarkup(rows)

    def _source_label(self, source_type: str) -> str:
        return {
            "youtube": "YouTube",
            "web": "כתבה",
            "pdf": "PDF",
            "audio": "אודיו",
            "voice": "קול",
            "social_video": "וידאו",
        }.get(source_type or "", "מקור")

    def _videos_keyboard(self, rows):
        buttons = []
        for row in rows:
            title = row["title"] or "ללא כותרת"
            prefix = self._source_label(row["source_type"])
            available = max(8, 48 - len(prefix) - 3)
            short = title if len(title) <= available else title[: available - 1].rstrip() + "…"
            buttons.append([
                InlineKeyboardButton(
                    f"{prefix} · {short}", callback_data=f"use:{row['id']}"
                )
            ])
        buttons.append([
            InlineKeyboardButton("מקור נוכחי", callback_data="current"),
            InlineKeyboardButton("תפריט ראשי", callback_data="home"),
        ])
        return InlineKeyboardMarkup(buttons)

    def _force_rtl_lines(self, text: str) -> str:
        rlm = "\u200f"
        hebrew = re.compile(r"[\u0590-\u05FF]")
        return "\n".join(
            (rlm + line) if line.strip() and hebrew.search(line) else line
            for line in text.split("\n")
        )

    def _telegram_html(self, text: str) -> str:
        text = self._force_rtl_lines(text)
        escaped = html.escape(text, quote=False)
        escaped = re.sub(r"(?m)^\u200f\s*[-*]\s+", "\u200f• ", escaped)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", escaped)
        escaped = re.sub(r"(?m)^\u200f#{1,6}\s+", "\u200f", escaped)
        escaped = escaped.replace("*", "")
        return escaped

    def _split_text(self, text: str, limit: int = 3500) -> list[str]:
        remaining = text.strip()
        raw_chunks = []
        while remaining:
            if len(remaining) <= limit:
                raw_chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut < 1000:
                cut = remaining.rfind(" ", 0, limit)
            if cut < 1000:
                cut = limit
            raw_chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()

        # Preserve **bold** across Telegram message boundaries by closing and
        # reopening the span around each chunk when a split occurs inside it.
        chunks = []
        bold_open = False
        for chunk in raw_chunks:
            starts_inside_bold = bold_open
            if chunk.count("**") % 2:
                bold_open = not bold_open
            rendered = chunk
            if starts_inside_bold:
                rendered = "**" + rendered
            if bold_open:
                rendered = rendered + "**"
            chunks.append(rendered)
        return chunks

    def _format_cost(self, usd: float) -> str:
        usd = max(0.0, float(usd))
        if usd < 1.0:
            return f"כ-{usd * 100:.2f} סנט"
        return f"כ-{usd:.2f} דולר"

    def _format_duration(self, seconds: float) -> str:
        total = max(0, int(round(seconds)))
        if total < 60:
            return f"{total} שנ׳"
        minutes, secs = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}:{secs:02d} דק׳"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"

    async def _send_long(self, message, text: str, reply_markup=None):
        chunks = self._split_text(text)
        for i, chunk in enumerate(chunks):
            await message.reply_text(
                self._telegram_html(chunk),
                parse_mode="HTML",
                reply_markup=reply_markup if i == len(chunks) - 1 else None,
            )

    def _friendly_error(self, exc: Exception) -> str:
        text = str(exc).strip()
        lower = text.lower()
        if "429" in lower or "rate limit" in lower or "too many requests" in lower:
            return "יש עומס זמני. נסה שוב בעוד רגע."
        if "credit" in lower or "billing" in lower or "entitlement" in lower or "quota" in lower:
            return "נגמרה מכסת ה-API."
        if text and all(ord(ch) < 128 for ch in text[:120]):
            return "אירעה שגיאה. נסה שוב."
        return text[:220] if text else "אירעה שגיאה. נסה שוב."

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.exception("Unhandled Telegram error", exc_info=context.error)

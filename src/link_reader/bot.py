from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import time
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
        app.add_handler(CommandHandler("help", self.start))
        app.add_handler(CommandHandler("translate", self.translate))
        app.add_handler(CommandHandler("transcript", self.transcript))
        app.add_handler(CommandHandler("videos", self.videos))
        app.add_handler(CommandHandler("use", self.use_content))
        app.add_handler(CallbackQueryHandler(self.callback))
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
            "שלח קישור. אקצר אותו בעברית ואפשר יהיה לשאול עליו.",
            reply_markup=self._home_keyboard(),
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

    async def _handle_url(self, update: Update, url: str, user_id: int):
        async with self._locks[user_id]:
            started = time.monotonic()
            status = await update.effective_message.reply_text(
                "מעבד את הסרטון… לרוב 10–30 שניות."
            )
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
                await status.edit_text(f"לא הצלחתי לעבד: {self._friendly_error(exc)}")

    async def _handle_question(self, update: Update, question: str, user_id: int):
        if not question:
            return
        async with self._locks[user_id]:
            status = await update.effective_message.reply_text("בודק…")
            try:
                answer = await self.service.answer(user_id, question)
                await status.delete()
                await self._send_long(update.effective_message, answer)
            except Exception as exc:
                await status.edit_text(self._friendly_error(exc))

    async def translate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        user_id = update.effective_user.id
        async with self._locks[user_id]:
            status = await update.effective_message.reply_text("מתרגם את הסרטון…")
            try:
                filename, text = await self.service.translate_current(user_id)
                data = io.BytesIO(text.encode("utf-8"))
                data.name = filename
                await update.effective_message.reply_document(
                    document=data,
                    filename=filename,
                    caption="תרגום מלא",
                )
                await status.delete()
            except Exception as exc:
                await status.edit_text(self._friendly_error(exc))

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
            )
        except Exception as exc:
            await update.effective_message.reply_text(self._friendly_error(exc))

    async def videos(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        rows = self.service.db.list_recent_content(10)
        if not rows:
            return await update.effective_message.reply_text("אין עדיין סרטונים שמורים.")
        await update.effective_message.reply_text(
            "בחר סרטון:",
            reply_markup=self._videos_keyboard(rows),
        )

    async def use_content(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        if not context.args or not context.args[0].isdigit():
            return await update.effective_message.reply_text("/use ID")
        content = self.service.db.get_content(int(context.args[0]))
        if not content:
            return await update.effective_message.reply_text("לא מצאתי את הסרטון.")
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
        if query.data == "translate":
            return await self.translate(update, context)
        if query.data == "transcript":
            return await self.transcript(update, context)
        if query.data == "videos":
            return await self.videos(update, context)
        if query.data and query.data.startswith("use:"):
            content_id = query.data.split(":", 1)[1]
            if not content_id.isdigit():
                return
            content = self.service.db.get_content(int(content_id))
            if not content:
                return await query.message.reply_text("לא מצאתי את הסרטון.")
            self.service.db.set_current_content(update.effective_user.id, content["id"])
            return await query.message.reply_text(
                f"נבחר: {content['title']}",
                reply_markup=self._content_keyboard(),
            )

    def _home_keyboard(self):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("סרטונים", callback_data="videos"),
        ]])

    def _content_keyboard(self):
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("תרגום מלא", callback_data="translate"),
                InlineKeyboardButton("טקסט מלא", callback_data="transcript"),
            ],
            [InlineKeyboardButton("סרטונים", callback_data="videos")],
        ])

    def _videos_keyboard(self, rows):
        buttons = []
        for row in rows:
            title = row["title"] or "ללא כותרת"
            label = title if len(title) <= 48 else title[:45].rstrip() + "…"
            buttons.append([
                InlineKeyboardButton(label, callback_data=f"use:{row['id']}")
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

    async def _send_long(self, message, text: str):
        for chunk in self._split_text(text):
            await message.reply_text(self._telegram_html(chunk), parse_mode="HTML")

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

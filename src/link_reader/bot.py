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
            "שלח לי קישור לסרטון YouTube.\n"
            "אחזיר סיכום בעברית עם חותמות זמן, ואז אפשר לשאול שאלות חופשיות.\n\n"
            "/translate — תרגום מלא לעברית\n"
            "/transcript — התמלול המקורי\n"
            "/videos — הסרטונים האחרונים\n"
            "/use ID — חזרה לסרטון קודם"
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
                "קיבלתי. אני מחלץ את התמלול ומכין מפת תוכן וסיכום בעברית. "
                "בסיום אציג גם את זמן העיבוד."
            )
            try:
                content, cached = await self.service.ingest(url, user_id)
                elapsed = time.monotonic() - started
                prefix = "כבר עיבדתי את הסרטון הזה בעבר.\n\n" if cached else ""
                footer = f"\n\n**זמן עיבוד:** {self._format_duration(elapsed)}"
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
                await status.edit_text(f"לא הצלחתי לעבד את הקישור: {self._friendly_error(exc)}")

    async def _handle_question(self, update: Update, question: str, user_id: int):
        if not question:
            return
        async with self._locks[user_id]:
            status = await update.effective_message.reply_text("בודק בתמלול…")
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
            status = await update.effective_message.reply_text("מכין תרגום מלא לעברית…")
            try:
                filename, text = await self.service.translate_current(user_id)
                data = io.BytesIO(text.encode("utf-8"))
                data.name = filename
                await update.effective_message.reply_document(
                    document=data,
                    filename=filename,
                    caption="התרגום המלא לעברית.",
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
                caption="התמלול המקורי עם חותמות זמן.",
            )
        except Exception as exc:
            await update.effective_message.reply_text(self._friendly_error(exc))

    async def videos(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        rows = self.service.db.list_recent_content(10)
        if not rows:
            return await update.effective_message.reply_text("עדיין אין סרטונים שמורים.")
        text = "הסרטונים האחרונים:\n\n" + "\n".join(
            f"{row['id']} — {row['title']}" for row in rows
        )
        text += "\n\nכדי לחזור לסרטון: /use ID"
        await self._send_long(update.effective_message, text)

    async def use_content(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return await self._deny(update)
        if not context.args or not context.args[0].isdigit():
            return await update.effective_message.reply_text("שימוש: /use ID")
        content = self.service.db.get_content(int(context.args[0]))
        if not content:
            return await update.effective_message.reply_text("לא מצאתי פריט עם ה-ID הזה.")
        self.service.db.set_current_content(update.effective_user.id, content["id"])
        await update.effective_message.reply_text(
            f"חזרתי אל: {content['title']}",
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

    def _content_keyboard(self):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("תרגום מלא", callback_data="translate"),
            InlineKeyboardButton("תמלול מקורי", callback_data="transcript"),
        ]])

    def _telegram_html(self, text: str) -> str:
        escaped = html.escape(text, quote=False)
        escaped = re.sub(r"(?m)^\s*\*\s+", "• ", escaped)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", escaped)
        escaped = re.sub(r"(?m)^#{1,6}\s+", "", escaped)
        escaped = escaped.replace("*", "")
        return escaped

    def _split_text(self, text: str, limit: int = 3500) -> list[str]:
        remaining = text.strip()
        chunks = []
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut < 1000:
                cut = remaining.rfind(" ", 0, limit)
            if cut < 1000:
                cut = limit

            # Do not split inside a **bold** span. If the tentative chunk has
            # an unmatched opening marker, move the boundary before it.
            prefix = remaining[:cut]
            if prefix.count("**") % 2:
                opening = prefix.rfind("**")
                if opening >= 1000:
                    cut = opening

            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        return chunks

    def _format_duration(self, seconds: float) -> str:
        total = max(0, int(round(seconds)))
        if total < 1:
            return "פחות משנייה"
        if total < 60:
            return "שנייה אחת" if total == 1 else f"{total} שניות"
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        parts = []
        if hours:
            parts.append("שעה אחת" if hours == 1 else f"{hours} שעות")
        if minutes:
            parts.append("דקה אחת" if minutes == 1 else f"{minutes} דקות")
        if secs:
            parts.append("שנייה אחת" if secs == 1 else f"{secs} שניות")
        return " ו־".join(parts)

    async def _send_long(self, message, text: str):
        for chunk in self._split_text(text):
            await message.reply_text(self._telegram_html(chunk), parse_mode="HTML")

    def _friendly_error(self, exc: Exception) -> str:
        text = str(exc).strip()
        return text[:900] if text else "אירעה שגיאה בעיבוד."

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.exception("Unhandled Telegram error", exc_info=context.error)

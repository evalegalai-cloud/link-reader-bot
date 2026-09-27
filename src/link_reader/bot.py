from __future__ import annotations

import asyncio
import io
import logging
import re
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
        return bool(user and user.id in self.settings.allowed_user_ids)

    async def _deny(self, update: Update):
        if update.effective_message:
            await update.effective_message.reply_text("אין הרשאה להשתמש בבוט הזה.")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            status = await update.effective_message.reply_text(
                "קיבלתי. אני מחלץ את התמלול ומכין מפת תוכן וסיכום בעברית."
            )
            try:
                content, cached = await self.service.ingest(url, user_id)
                prefix = "כבר עיבדתי את הסרטון הזה בעבר.\n\n" if cached else ""
                await status.edit_text(
                    f"{prefix}{content['title']}\n\n{content['summary'][:3600]}",
                    reply_markup=self._content_keyboard(),
                )
                if len(content["summary"]) > 3600:
                    await self._send_long(update.effective_message, content["summary"][3600:])
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

    async def _send_long(self, message, text: str):
        remaining = text.strip()
        while remaining:
            if len(remaining) <= 3900:
                chunk, remaining = remaining, ""
            else:
                cut = remaining.rfind("\n", 0, 3900)
                if cut < 1000:
                    cut = 3900
                chunk, remaining = remaining[:cut], remaining[cut:].lstrip()
            await message.reply_text(chunk)

    def _friendly_error(self, exc: Exception) -> str:
        text = str(exc).strip()
        return text[:900] if text else "אירעה שגיאה בעיבוד."

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.exception("Unhandled Telegram error", exc_info=context.error)

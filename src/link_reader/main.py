from __future__ import annotations

import logging

from link_reader.bot import TelegramBot
from link_reader.config import Settings
from link_reader.db import Database
from link_reader.llm import LLMClient
from link_reader.processors import build_processors
from link_reader.service import ContentService


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Avoid leaking Telegram bot tokens embedded in HTTP request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = Settings.from_env()
    settings.validate()

    db = Database(settings.database_path)
    llm = LLMClient(settings)
    processors = build_processors(settings)
    service = ContentService(db, llm, processors, target_language=settings.target_language)
    app = TelegramBot(settings, service).build_application()

    logging.getLogger(__name__).info("Starting Link Reader Bot")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()

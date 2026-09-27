from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    database_path: str
    llm_provider: str
    llm_model_fast: str
    llm_model_smart: str
    llm_api_key: str
    llm_base_url: str | None
    asr_mode: str
    whisper_model: str
    youtube_proxy_url: str | None
    max_video_minutes: int
    target_language: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
        ids = frozenset(int(x.strip()) for x in raw_ids.split(",") if x.strip())
        legacy_model = os.getenv("LLM_MODEL", "").strip()
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=ids,
            database_path=os.getenv("DATABASE_PATH", "./data/link_reader.db"),
            llm_provider=os.getenv("LLM_PROVIDER", "openai_compatible").strip().lower(),
            llm_model_fast=os.getenv("LLM_MODEL_FAST", legacy_model).strip(),
            llm_model_smart=os.getenv("LLM_MODEL_SMART", legacy_model).strip(),
            llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
            llm_base_url=os.getenv("LLM_BASE_URL") or None,
            asr_mode=os.getenv("ASR_MODE", "local").strip().lower(),
            whisper_model=os.getenv("WHISPER_MODEL", "small").strip(),
            youtube_proxy_url=os.getenv("YOUTUBE_PROXY_URL") or None,
            max_video_minutes=int(os.getenv("MAX_VIDEO_MINUTES", "360")),
            target_language=os.getenv("TARGET_LANGUAGE", "Hebrew").strip() or "Hebrew",
        )

    def validate(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.allowed_user_ids:
            missing.append("TELEGRAM_ALLOWED_USER_IDS")
        if not self.llm_model_fast:
            missing.append("LLM_MODEL_FAST")
        if not self.llm_model_smart:
            missing.append("LLM_MODEL_SMART")
        if not self.llm_api_key:
            missing.append("LLM_API_KEY")
        if missing:
            raise RuntimeError("Missing required settings: " + ", ".join(missing))
        if self.llm_provider not in {"openai_compatible", "anthropic"}:
            raise RuntimeError("LLM_PROVIDER must be openai_compatible or anthropic")

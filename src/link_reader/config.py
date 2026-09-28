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
    youtube_proxy_file: str | None
    supadata_api_key: str | None
    supadata_mode: str
    max_video_minutes: int
    target_language: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
        ids = frozenset(int(x.strip()) for x in raw_ids.split(",") if x.strip())
        legacy_model = os.getenv("LLM_MODEL", "").strip()
        llm_base_url = os.getenv("LLM_BASE_URL") or None
        llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
        if llm_base_url and "openrouter.ai" in llm_base_url:
            openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
            if openrouter_key:
                llm_api_key = openrouter_key
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=ids,
            database_path=os.getenv("DATABASE_PATH", "./data/link_reader.db"),
            llm_provider=os.getenv("LLM_PROVIDER", "openai_compatible").strip().lower(),
            llm_model_fast=os.getenv("LLM_MODEL_FAST", legacy_model).strip(),
            llm_model_smart=os.getenv("LLM_MODEL_SMART", legacy_model).strip(),
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            asr_mode=os.getenv("ASR_MODE", "local").strip().lower(),
            whisper_model=os.getenv("WHISPER_MODEL", "small").strip(),
            youtube_proxy_url=os.getenv("YOUTUBE_PROXY_URL") or None,
            youtube_proxy_file=os.getenv("YOUTUBE_PROXY_FILE") or None,
            supadata_api_key=os.getenv("SUPADATA_API_KEY") or None,
            supadata_mode=os.getenv("SUPADATA_MODE", "native").strip().lower(),
            max_video_minutes=int(os.getenv("MAX_VIDEO_MINUTES", "360")),
            target_language=os.getenv("TARGET_LANGUAGE", "Hebrew").strip() or "Hebrew",
        )

    def validate(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
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
        if self.supadata_mode not in {"native", "auto", "generate"}:
            raise RuntimeError("SUPADATA_MODE must be native, auto, or generate")

# Link Reader Bot

A private-by-default Telegram bot that turns a YouTube link into a structured summary, a full translation, a timestamped transcript, and a grounded Q&A session.

The project is currently focused on YouTube, but the core is deliberately source-agnostic so additional processors can later handle web articles, social posts, PDFs, books, podcasts, and other links.

## What it does

Send a YouTube URL to the bot. It will:

1. Detect the video and fetch metadata.
2. Try to use existing YouTube captions or auto-generated captions.
3. If no captions are available, optionally download audio and transcribe it locally with faster-whisper.
4. Split long transcripts into timestamped chunks.
5. Build compact content maps for efficient long-video retrieval.
6. Generate a structured summary.
7. Keep the video active so you can ask follow-up questions in natural language.
8. Return answers grounded in the transcript, with timestamps when possible.
9. Export the original transcript or a full translation on demand.
10. Cache processed videos locally so the same video does not need to be processed twice.

## Language support

The architecture is language-agnostic.

- The source video can be in any language supported by YouTube captions or the configured Whisper model.
- Summaries, Q&A answers, and full translations can be generated in any target language supported by the configured LLM.
- Set the output language with:

```env
TARGET_LANGUAGE=Hebrew
```

For example:

```env
TARGET_LANGUAGE=English
TARGET_LANGUAGE=Spanish
TARGET_LANGUAGE=Arabic
TARGET_LANGUAGE=French
```

The example configuration defaults to Hebrew. The practical language coverage depends on the ASR and LLM providers you choose.

## Current features

- Telegram long polling: no public webhook, domain, TLS certificate, or inbound port required.
- YouTube watch, youtu.be, Shorts, Live, and embed URL support.
- Existing captions and auto-caption extraction.
- Optional local Whisper fallback.
- Timestamp-preserving transcript storage.
- Chunked summarization for long videos.
- Follow-up Q&A grounded in transcript excerpts.
- Full translation export.
- Original transcript export.
- SQLite persistence and caching.
- Recent-video history and switching between saved videos.
- Telegram user allow-list.
- Separate fast and smart LLM routes.
- Anthropic or OpenAI-compatible LLM endpoints.
- Docker deployment.

## Architecture

```text
Telegram
   |
   v
TelegramBot
   |
   v
ContentService
   |---------------------> SQLite
   |---------------------> LLM provider
   |
   v
Processor registry
   |
   v
YouTubeProcessor
   |---- captions first
   |---- local Whisper fallback
```

The processor registry is intentionally generic. Future source types can reuse the same storage, summarization, translation, and Q&A layers.

## Model routing

Two model tiers can be configured:

- `LLM_MODEL_FAST`: chunk maps, routing, and translation.
- `LLM_MODEL_SMART`: final summaries and follow-up Q&A.

This keeps routine processing inexpensive while reserving the stronger model for higher-value reasoning.

## Setup

### 1. Create a Telegram bot

Open `@BotFather` in Telegram, run `/newbot`, choose a name and username, and copy the bot token.

You will also need your numeric Telegram user ID for the allow-list.

### 2. Configure the environment

```bash
cp .env.example .env
```

Fill at least:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=

LLM_PROVIDER=openai_compatible
LLM_MODEL_FAST=
LLM_MODEL_SMART=
LLM_API_KEY=
LLM_BASE_URL=

TARGET_LANGUAGE=Hebrew
```

For multiple allowed users, separate Telegram IDs with commas.

### 3. Run with Docker

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f --tail=200
```

## Telegram commands

- `/start` — help.
- `/translate` — export a full translation of the active video.
- `/transcript` — export the original timestamped transcript.
- `/videos` — list recently processed videos.
- `/use ID` — switch back to a previous video.

Any ordinary text message sent after processing a video is treated as a follow-up question about the active video.

## YouTube reliability

YouTube may occasionally restrict requests from cloud-provider IP addresses. The normal setup does not require a proxy, but `YOUTUBE_PROXY_URL` is available if needed.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

To install the local ASR fallback:

```bash
.venv/bin/pip install -e '.[asr]'
```

## Security

The bot rejects Telegram users whose numeric IDs are not listed in `TELEGRAM_ALLOWED_USER_IDS`.

Do not commit:

- `.env`
- Telegram bot tokens
- LLM API keys
- proxy credentials

The supplied `.gitignore` excludes the local environment file and runtime data.

## Planned extensions

The next logical processors are:

- ordinary web pages and articles
- PDFs and uploaded books/documents
- podcast and audio links
- X / Twitter
- Reddit
- LinkedIn
- TikTok
- Instagram
- Facebook

These should remain thin source adapters over the same transcript/content pipeline rather than separate applications.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the planned expansion.

## Contributors

The initial architecture, implementation, testing, and documentation were produced with **ChatGPT (OpenAI)**. See [CONTRIBUTORS.md](CONTRIBUTORS.md).

## License

Released under the [MIT License](LICENSE).

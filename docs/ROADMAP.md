# Roadmap

The project is designed around a reusable content-processing pipeline rather than a YouTube-specific monolith.

## Phase 1 — YouTube

Implemented:

- caption and auto-caption extraction
- local Whisper fallback
- timestamped transcript storage
- long-video chunking
- content-map generation
- structured summaries
- grounded follow-up Q&A
- full translation export
- transcript export
- caching and recent-content history
- configurable target language

## Phase 2 — Generic web content

Add processors for:

- normal web pages
- long-form articles
- public blog posts
- PDFs
- uploaded books and documents
- podcast and audio URLs

Each processor should return the same normalized content contract so the existing summary, translation, storage, and Q&A layers can be reused.

## Phase 3 — Social links

Add dedicated adapters where technically and legally practical for:

- X / Twitter
- Reddit
- LinkedIn
- TikTok
- Instagram
- Facebook

Prefer official APIs or reliably accessible public content. Avoid brittle scraping when a supported access path exists.

## Phase 4 — Messaging surfaces

Keep Telegram as the reference client and add other thin front ends over the same backend, especially:

- WhatsApp
- a lightweight web UI
- browser share-sheet / extension workflows

## Phase 5 — Personal knowledge library

Optional additions:

- tags and folders
- saved collections
- cross-content search
- ask questions across multiple saved sources
- Drive export
- hybrid/vector retrieval when the corpus becomes large enough to justify it

## Language strategy

The core should remain language-agnostic:

- source language is determined by captions or ASR
- output language is configured independently
- prompts should remain in a neutral internal language
- user-facing output should follow `TARGET_LANGUAGE`
- language-specific logic should stay outside the retrieval and storage layers

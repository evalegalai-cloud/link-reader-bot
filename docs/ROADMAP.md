# Link Reader Roadmap

## Stage 1 — YouTube V1 ✅
- Supadata transcript ingestion
- Fast OpenRouter / DeepSeek summarization
- Full translation without timestamps
- Grounded Q&A with short per-video conversational memory
- Hebrew RTL handling
- Simple Telegram UX and cost/time display
- Production validation on uncached videos

Validation (2026-09-28):
- TED 2026 (~16m): ingest 14.67s; full translation 17.12s; initial API-equivalent cost ~$0.01277.
- TED 2024 (~22m): ingest 10.19s; Q&A 4.25s / follow-up 3.82s; initial API-equivalent cost ~$0.01332.

## Stage 2 — Reliability & routing ✅
- OpenRouter throughput/latency routing benchmark
- Retries for 429/5xx/network errors
- Provider failover
- Bounded timeouts and concise user-facing errors
- Provider-reported cost

## Stage 3 — Web / Articles ✅
- Generic URL classifier + processor registry
- Safe HTTP fetch with SSRF/redirect/size protections
- Trafilatura main-article extraction
- Section provenance `[§N]`
- Summary, conversational Q&A, source export, full translation
- Browser fallback deferred until a real extraction failure justifies it

## Stage 4 — PDFs / books
- PDF text + page provenance
- OCR only for scanned pages
- EPUB later

## Stage 5 — Podcasts / audio
- RSS/direct audio
- ASR + timestamps
- Q&A / translation
- Diarization later

## Stage 6 — Social sources
- Reddit / X first
- LinkedIn / TikTok / Instagram / Facebook as access permits

## Stage 7 — WhatsApp
- WhatsApp Business / Cloud API adapter

## Stage 8 — Knowledge library
- Cross-content search and Q&A
- SQLite FTS first; vector DB only if justified

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import secrets
import logging
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from twilio.request_validator import RequestValidator

from link_reader.config import Settings
from link_reader.db import Database
from link_reader.llm import LLMClient
from link_reader.processors import AudioProcessor, build_processors
from link_reader.service import ContentService

logger = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


WHATSAPP_ENV_PATH = Path("/data/whatsapp.env")


def _stored_whatsapp_env() -> dict[str, str]:
    if not WHATSAPP_ENV_PATH.exists():
        return {}
    values: dict[str, str] = {}
    for raw in WHATSAPP_ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _env_quote(value: str) -> str:
    value = (value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")
    return f'"{value}"'


def _write_whatsapp_env(values: dict[str, str]) -> None:
    WHATSAPP_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    order = [
        "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_ADMIN_TOKEN", "WHATSAPP_GRAPH_VERSION",
        "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_WABA_ID",
        "WHATSAPP_APP_SECRET", "WHATSAPP_ALLOWED_NUMBERS",
    ]
    text = "\n".join(f"{key}={_env_quote(values.get(key, ''))}" for key in order) + "\n"
    tmp = WHATSAPP_ENV_PATH.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(WHATSAPP_ENV_PATH)
    WHATSAPP_ENV_PATH.chmod(0o600)


def _secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = Path("/run/secrets") / name.lower()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


@dataclass(frozen=True)
class WhatsAppConfig:
    access_token: str
    phone_number_id: str
    waba_id: str
    app_secret: str
    verify_token: str
    graph_version: str
    allowed_numbers: frozenset[str]
    admin_token: str

    @classmethod
    def from_env(cls):
        stored = _stored_whatsapp_env()
        def value(name: str, default: str = "") -> str:
            return (stored.get(name) or os.getenv(name, default) or "").strip()

        verify = value("WHATSAPP_VERIFY_TOKEN")
        changed = False
        if not verify:
            verify = secrets.token_urlsafe(32)
            stored["WHATSAPP_VERIFY_TOKEN"] = verify
            changed = True
        admin_token = value("WHATSAPP_ADMIN_TOKEN")
        if not admin_token:
            admin_token = secrets.token_urlsafe(32)
            stored["WHATSAPP_ADMIN_TOKEN"] = admin_token
            changed = True
        stored.setdefault("WHATSAPP_GRAPH_VERSION", "v26.0")
        if changed:
            _write_whatsapp_env(stored)

        version = value("WHATSAPP_GRAPH_VERSION", "v26.0") or "v26.0"
        if not version.startswith("v"):
            version = "v" + version
        allowed_raw = value("WHATSAPP_ALLOWED_NUMBERS")
        allowed = frozenset(
            normalized
            for raw in allowed_raw.split(",")
            if (normalized := re.sub(r"\D", "", raw))
        )
        return cls(
            access_token=value("WHATSAPP_ACCESS_TOKEN") or _secret("WHATSAPP_ACCESS_TOKEN"),
            phone_number_id=value("WHATSAPP_PHONE_NUMBER_ID"),
            waba_id=value("WHATSAPP_WABA_ID"),
            app_secret=value("WHATSAPP_APP_SECRET") or _secret("WHATSAPP_APP_SECRET"),
            verify_token=verify,
            graph_version=version,
            allowed_numbers=allowed,
            admin_token=admin_token,
        )

    @property
    def configured(self) -> bool:
        return all((self.access_token, self.phone_number_id, self.app_secret, self.verify_token))

    def sender_allowed(self, wa_id: str) -> bool:
        sender = re.sub(r"\D", "", wa_id or "")
        return bool(sender and self.allowed_numbers and sender in self.allowed_numbers)


@dataclass
class TwilioWhatsAppConfig:
    account_sid: str
    auth_token: str
    from_number: str
    allowed_numbers: frozenset[str]

    @classmethod
    def from_env(cls):
        allowed_raw = os.getenv("TWILIO_ALLOWED_NUMBERS", "").strip()
        allowed = frozenset(
            normalized
            for raw in allowed_raw.split(",")
            if (normalized := re.sub(r"\D", "", raw))
        )
        from_number = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
        if from_number and not from_number.startswith("whatsapp:"):
            digits = re.sub(r"\D", "", from_number)
            from_number = f"whatsapp:+{digits}" if digits else ""
        return cls(
            account_sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            from_number=from_number,
            allowed_numbers=allowed,
        )

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token)

    def sender_allowed(self, wa_id: str) -> bool:
        sender = re.sub(r"\D", "", wa_id or "")
        return bool(sender and (not self.allowed_numbers or sender in self.allowed_numbers))




class WhatsAppGateway:
    def __init__(self):
        self.config = WhatsAppConfig.from_env()
        settings = Settings.from_env()
        db = Database(settings.database_path)
        llm = LLMClient(settings)
        self.service = ContentService(
            db,
            llm,
            build_processors(settings),
            target_language=settings.target_language,
        )
        self.settings = settings
        self._locks: dict[int, asyncio.Lock] = {}

    def user_id(self, wa_id: str) -> int:
        digest = hashlib.blake2b(
            f"whatsapp:{wa_id}".encode("utf-8"), digest_size=8
        ).digest()
        value = int.from_bytes(digest, "big") & ((1 << 63) - 1)
        return -(value or 1)

    def verify_signature(self, body: bytes, signature: str | None) -> bool:
        if not self.config.app_secret or not signature or not signature.startswith("sha256="):
            return False
        expected = hmac.new(
            self.config.app_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature[7:], expected)

    async def handle_payload(self, payload: dict) -> None:
        messages = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                messages.extend(value.get("messages") or [])
        for message in messages:
            event_id = str(message.get("id") or "")
            if event_id and not self.service.db.claim_webhook_event("whatsapp", event_id):
                continue
            try:
                await self.handle_message(message)
            except Exception:
                logger.exception("WhatsApp message processing failed")
                sender = str(message.get("from") or "")
                if sender and self.config.configured:
                    try:
                        await self.send_text(sender, "לא הצלחתי לעבד את ההודעה. אפשר לנסות שוב.")
                    except Exception:
                        logger.exception("Failed sending WhatsApp error reply")

    async def handle_message(self, message: dict) -> None:
        sender = str(message.get("from") or "").strip()
        if not sender:
            return
        if not self.config.sender_allowed(sender):
            logger.warning("Ignoring WhatsApp message from non-allowlisted sender")
            return
        user_id = self.user_id(sender)
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            msg_type = message.get("type")
            if msg_type == "text":
                await self._handle_text(sender, user_id, (message.get("text") or {}).get("body") or "")
                return
            if msg_type == "audio":
                await self._handle_audio(sender, user_id, message)
                return
            await self.send_text(sender, "אפשר לשלוח קישור, טקסט או הודעה קולית.")

    async def _handle_text(self, sender: str, user_id: int, text: str) -> None:
        text = text.strip()
        if not text:
            return
        lowered = text.casefold()
        if self.service.looks_like_pasted_source(text):
            started = time.monotonic()
            content, cached = await self.service.ingest_text(text, user_id)
            elapsed = time.monotonic() - started
            prefix = "שמור\n\n" if cached else ""
            footer = f"\n\nזמן: {self._duration(elapsed)}"
            cost = content["processing_cost_usd"]
            if cost is not None:
                footer += f" · עלות: {self._cost(cost)}"
            return await self.send_text(
                sender,
                f"{prefix}{content['title']}\n\n{content['summary']}{footer}",
            )

        library_question = re.match(r"^(?:שאל\s+הכל|askall)\s+(.+)$", text, re.IGNORECASE)
        if library_question:
            answer = await self.service.answer_library(library_question.group(1).strip())
            return await self.send_text(sender, answer)
        library_search = re.match(r"^(?:חפש|search)\s+(.+)$", text, re.IGNORECASE)
        if library_search:
            return await self.send_text(
                sender, self.service.library_search_text(library_search.group(1).strip())
            )
        if lowered in {"עזרה", "תפריט", "help", "menu"}:
            return await self.send_text(sender, self.help_text(user_id))
        if lowered in {"מקור נוכחי", "current"}:
            current = self.service.db.get_current_content(user_id)
            body = f"מקור נוכחי: {current['title']}" if current else "אין מקור נוכחי. שלח קישור או הודעה קולית."
            return await self.send_text(sender, body)
        if lowered in {"אחרונים", "recent"}:
            rows = self.service.db.list_recent_content(8)
            if not rows:
                return await self.send_text(sender, "אין עדיין מקורות שמורים.")
            lines = [f"{row['id']} · {row['title']}" for row in rows]
            return await self.send_text(sender, "אחרונים:\n" + "\n".join(lines) + "\n\nלבחירה: בחר 12")
        select = re.match(r"^(?:בחר|use)\s+(\d+)\s*$", text, re.IGNORECASE)
        if select:
            content = self.service.db.get_content(int(select.group(1)))
            if not content:
                return await self.send_text(sender, "לא מצאתי את המקור.")
            self.service.db.set_current_content(user_id, content["id"])
            return await self.send_text(sender, f"נבחר: {content['title']}")
        match = URL_RE.search(text)
        if match:
            started = time.monotonic()
            content, cached = await self.service.ingest(match.group(0).rstrip(".,)>]"), user_id)
            elapsed = time.monotonic() - started
            prefix = "שמור\n\n" if cached else ""
            footer = f"\n\nזמן: {self._duration(elapsed)}"
            cost = content["processing_cost_usd"]
            if cost is not None:
                footer += f" · עלות: {self._cost(cost)}"
            return await self.send_text(
                sender,
                f"{prefix}{content['title']}\n\n{content['summary']}{footer}",
            )
        answer, _mode = await self.service.answer_freeform(user_id, text)
        await self.send_text(sender, answer)

    async def _handle_audio(self, sender: str, user_id: int, message: dict) -> None:
        audio = message.get("audio") or {}
        media_id = str(audio.get("id") or "")
        if not media_id:
            raise ValueError("Missing WhatsApp media id")
        current = self.service.db.get_current_content(user_id)
        path, mime = await self.download_media(media_id)
        try:
            processor = next(
                p for p in self.service.processors if isinstance(p, AudioProcessor)
            )
            duration = processor._duration_seconds(path) or 0.0
            mime_lower = (mime or str(audio.get("mime_type") or "")).lower()
            is_voice = bool(audio.get("voice")) or ("audio/ogg" in mime_lower and "opus" in mime_lower)
            as_question = bool(is_voice and duration <= 120)
            item = await processor.extract_local_file(
                path,
                external_id=f"whatsapp-{media_id}",
                title="הודעה קולית" if is_voice else "קובץ אודיו",
                source_type="voice" if is_voice else "audio",
                url=f"whatsapp://media/{media_id}",
            )
            if as_question:
                question = " ".join(seg.text.strip() for seg in item.segments if seg.text.strip())
                answer, _mode = await self.service.answer_freeform(user_id, question)
                return await self.send_text(sender, answer)
            started = time.monotonic()
            content, cached = await self.service.ingest_item(item, user_id)
            elapsed = time.monotonic() - started
            prefix = "שמור\n\n" if cached else ""
            footer = f"\n\nזמן: {self._duration(elapsed)}"
            cost = content["processing_cost_usd"]
            if cost is not None:
                footer += f" · עלות: {self._cost(cost)}"
            await self.send_text(
                sender,
                f"{prefix}{content['title']}\n\n{content['summary']}{footer}",
            )
        finally:
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except Exception:
                pass

    async def download_media(self, media_id: str) -> tuple[Path, str]:
        base = f"https://graph.facebook.com/{self.config.graph_version}"
        headers = {"Authorization": f"Bearer {self.config.access_token}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            meta = await client.get(
                f"{base}/{media_id}",
                headers=headers,
                params={"phone_number_id": self.config.phone_number_id},
            )
            meta.raise_for_status()
            data = meta.json()
            media_url = data.get("url")
            if not media_url:
                raise RuntimeError("WhatsApp media URL missing")
            mime = str(data.get("mime_type") or "application/octet-stream")
            base_mime = mime.split(";", 1)[0].strip().lower()
            suffix = {
                "audio/ogg": ".ogg",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
                "audio/aac": ".aac",
                "audio/amr": ".amr",
                "audio/wav": ".wav",
            }.get(base_mime) or mimetypes.guess_extension(base_mime) or ".bin"
            tmpdir = Path(tempfile.mkdtemp(prefix="link-reader-wa-"))
            path = tmpdir / ("media" + suffix)
            response = await client.get(media_url, headers=headers)
            response.raise_for_status()
            if len(response.content) > 50 * 1024 * 1024:
                raise ValueError("קובץ האודיו גדול מדי.")
            path.write_bytes(response.content)
            return path, mime

    async def send_text(self, to: str, text: str) -> None:
        if not self.config.configured:
            raise RuntimeError("WhatsApp is not configured")
        url = (
            f"https://graph.facebook.com/{self.config.graph_version}/"
            f"{self.config.phone_number_id}/messages"
        )
        headers = {
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
        }
        chunks = self._split(text)
        async with httpx.AsyncClient(timeout=30) as client:
            for chunk in chunks:
                response = await client.post(
                    url,
                    headers=headers,
                    json={
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": to,
                        "type": "text",
                        "text": {"preview_url": False, "body": chunk},
                    },
                )
                response.raise_for_status()

    def help_text(self, user_id: int) -> str:
        current = self.service.db.get_current_content(user_id)
        lines = [
            "שלח קישור, טקסט או הודעה קולית.",
            "קישור חדש יוצר מקור; טקסט חופשי נשאל כברירת מחדל על המקור הנוכחי.",
            "הודעה קולית קצרה היא שאלה חופשית; הודעה/קובץ ארוכים נשמרים כמקור.",
            "אפשר לכתוב: מקור נוכחי · אחרונים · תפריט",
            "ספרייה: חפש <מילים> · שאל הכל <שאלה>",
            "אינטרנט: כתוב במפורש 'בדוק באינטרנט' / 'ידע כללי' / שאלה עדכנית.",
        ]
        if current:
            lines.append(f"\nמקור נוכחי: {current['title']}")
        return "\n".join(lines)

    def _split(self, text: str, limit: int = 3500) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = remaining.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        return chunks

    def _cost(self, usd: float) -> str:
        return f"כ-{usd * 100:.2f} סנט" if usd < 1 else f"כ-{usd:.2f} דולר"

    def _duration(self, seconds: float) -> str:
        total = max(0, int(round(seconds)))
        if total < 60:
            return f"{total} שנ׳"
        minutes, secs = divmod(total, 60)
        return f"{minutes}:{secs:02d} דק׳"


class TwilioWhatsAppGateway(WhatsAppGateway):
    """Twilio transport adapter reusing the same Link Reader WhatsApp logic."""

    def __init__(self, service: ContentService, settings: Settings):
        self.config = TwilioWhatsAppConfig.from_env()
        self.service = service
        self.settings = settings
        self._locks: dict[int, asyncio.Lock] = {}
        self._runtime_from = self.config.from_number

    def set_from_number(self, value: str) -> None:
        value = (value or "").strip()
        if value and not value.startswith("whatsapp:"):
            digits = re.sub(r"\D", "", value)
            value = f"whatsapp:+{digits}" if digits else ""
        if value:
            self._runtime_from = value

    def verify_request(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        if not self.config.auth_token or not signature:
            return False
        try:
            return bool(RequestValidator(self.config.auth_token).validate(url, params, signature))
        except Exception:
            return False

    async def send_text(self, to: str, text: str) -> None:
        if not self.config.configured:
            raise RuntimeError("Twilio WhatsApp is not configured")
        from_number = self._runtime_from or self.config.from_number
        if not from_number:
            raise RuntimeError("Twilio WhatsApp sender is unknown")
        digits = re.sub(r"\D", "", to or "")
        if not digits:
            raise ValueError("Invalid WhatsApp recipient")
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{self.config.account_sid}/Messages.json"
        )
        async with httpx.AsyncClient(
            auth=(self.config.account_sid, self.config.auth_token),
            timeout=30,
        ) as client:
            for chunk in self._split(text, limit=1500):
                response = await client.post(
                    url,
                    data={
                        "From": from_number,
                        "To": f"whatsapp:+{digits}",
                        "Body": chunk,
                    },
                )
                response.raise_for_status()

    async def download_media(self, media_id: str) -> tuple[Path, str]:
        if not media_id.startswith(("https://", "http://")):
            raise ValueError("Invalid Twilio media URL")
        async with httpx.AsyncClient(
            auth=(self.config.account_sid, self.config.auth_token),
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(media_id)
            response.raise_for_status()
        if len(response.content) > 50 * 1024 * 1024:
            raise ValueError("קובץ האודיו גדול מדי.")
        mime = (response.headers.get("content-type") or "application/octet-stream").split(";", 1)[0].strip()
        suffix = {
            "audio/ogg": ".ogg",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/aac": ".aac",
            "audio/amr": ".amr",
            "audio/wav": ".wav",
        }.get(mime.lower()) or mimetypes.guess_extension(mime) or ".bin"
        tmpdir = Path(tempfile.mkdtemp(prefix="link-reader-twilio-wa-"))
        path = tmpdir / ("media" + suffix)
        path.write_bytes(response.content)
        return path, mime




gateway = WhatsAppGateway()
app = FastAPI(title="Link Reader WhatsApp", docs_url=None, redoc_url=None)


def _admin_html(request: Request, action_path: str, message: str = "") -> str:
    cfg = gateway.config
    allowed = ", ".join(sorted(cfg.allowed_numbers))
    status = "מוכן" if cfg.configured and cfg.allowed_numbers else "חסרים פרטים"
    notice = f'<p class="ok">{html.escape(message)}</p>' if message else ""
    host = request.headers.get("host") or request.url.netloc or "localhost"
    webhook = f"https://{host}/whatsapp/webhook"
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link Reader · WhatsApp</title><style>body{{font-family:system-ui,-apple-system,sans-serif;max-width:720px;margin:40px auto;padding:0 18px;line-height:1.5}}input{{width:100%;box-sizing:border-box;padding:10px;margin:5px 0 14px;font-size:16px}}button{{padding:11px 18px;font-size:16px}}code{{direction:ltr;display:inline-block;background:#eee;padding:3px 6px;border-radius:5px;overflow-wrap:anywhere}}.muted{{color:#666}}.ok{{background:#e8f5e9;padding:10px;border-radius:6px}}</style></head>
<body><h2>WhatsApp · Link Reader</h2>{notice}<p><b>מצב:</b> {status}</p>
<p><b>Callback URL:</b> <code>{html.escape(webhook)}</code><br><b>Verify token:</b> <code>{html.escape(cfg.verify_token)}</code></p>
<form method="post" action="{html.escape(action_path)}">
<label>Access token</label><input type="password" name="access_token" {'required' if not cfg.access_token else ''} placeholder="{'מוגדר — השאר ריק כדי לשמור' if cfg.access_token else 'הדבק token'}">
<label>Phone Number ID</label><input name="phone_number_id" required value="{html.escape(cfg.phone_number_id)}">
<label>WABA ID</label><input name="waba_id" required value="{html.escape(cfg.waba_id)}">
<label>App Secret</label><input type="password" name="app_secret" {'required' if not cfg.app_secret else ''} placeholder="{'מוגדר — השאר ריק כדי לשמור' if cfg.app_secret else 'הדבק App Secret'}">
<label>המספרים שמותר להם להשתמש בבוט</label><input name="allowed_numbers" required value="{html.escape(allowed)}" placeholder="9725XXXXXXXX, ...">
<label>Graph API version</label><input name="graph_version" value="{html.escape(cfg.graph_version)}">
<button type="submit">שמור</button></form>
<p class="muted">הסודות נשמרים רק בקובץ מקומי על השרת. השאר שדה סוד ריק כדי לשמור את הערך הקיים.</p></body></html>"""


def _check_admin_token(token: str) -> None:
    if not gateway.config.admin_token or not hmac.compare_digest(token or "", gateway.config.admin_token):
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/whatsapp-admin/{token}", response_class=HTMLResponse)
async def admin_whatsapp(request: Request, token: str):
    _check_admin_token(token)
    return HTMLResponse(_admin_html(request, request.url.path))


@app.post("/whatsapp-admin/{token}", response_class=HTMLResponse)
async def save_admin_whatsapp(request: Request, token: str):
    _check_admin_token(token)
    body = (await request.body()).decode("utf-8", errors="ignore")
    form = {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}
    current = gateway.config
    access = form.get("access_token", "").strip() or current.access_token
    phone = re.sub(r"\D", "", form.get("phone_number_id", "")) or current.phone_number_id
    waba = re.sub(r"\D", "", form.get("waba_id", "")) or current.waba_id
    app_secret = form.get("app_secret", "").strip() or current.app_secret
    allowed_text = form.get("allowed_numbers", "").strip()
    allowed = frozenset(
        n for raw in allowed_text.split(",") if (n := re.sub(r"\D", "", raw))
    ) if allowed_text else current.allowed_numbers
    version = form.get("graph_version", "").strip() or current.graph_version
    if not version.startswith("v"):
        version = "v" + version
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise HTTPException(status_code=400, detail="Invalid Graph API version")
    if not all((access, phone, waba, app_secret, allowed)):
        return HTMLResponse(
            _admin_html(request, request.url.path, "חסרים פרטים. מלא את כל השדות."),
            status_code=400,
        )
    next_admin_token = secrets.token_urlsafe(32)
    values = {
        "WHATSAPP_VERIFY_TOKEN": current.verify_token,
        "WHATSAPP_ADMIN_TOKEN": next_admin_token,
        "WHATSAPP_GRAPH_VERSION": version,
        "WHATSAPP_ACCESS_TOKEN": access,
        "WHATSAPP_PHONE_NUMBER_ID": phone,
        "WHATSAPP_WABA_ID": waba,
        "WHATSAPP_APP_SECRET": app_secret,
        "WHATSAPP_ALLOWED_NUMBERS": ",".join(sorted(allowed)),
    }
    _write_whatsapp_env(values)
    gateway.config = WhatsAppConfig(
        access_token=access, phone_number_id=phone, waba_id=waba, app_secret=app_secret,
        verify_token=current.verify_token, graph_version=version, allowed_numbers=allowed,
        admin_token=next_admin_token,
    )
    host = request.headers.get("host") or request.url.netloc or "localhost"
    webhook = f"https://{host}/whatsapp/webhook"
    subscribed, subscription_message = await _subscribe_waba(gateway.config, webhook)
    subscription_html = (
        '<p class="ok">ה-Webhook נרשם אוטומטית ב-Meta.</p>'
        if subscribed else
        '<p>הפרטים נשמרו, אבל ההרשמה האוטומטית ל-Meta לא הושלמה. ' + html.escape(subscription_message) + '</p>'
    )
    success = f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WhatsApp נשמר</title><style>body{{font-family:system-ui,-apple-system,sans-serif;max-width:720px;margin:40px auto;padding:0 18px;line-height:1.6}}code{{direction:ltr;display:inline-block;background:#eee;padding:3px 6px;border-radius:5px;overflow-wrap:anywhere}}.ok{{background:#e8f5e9;padding:10px;border-radius:6px}}</style></head><body><h2>נשמר בהצלחה</h2><p>לינק ההגדרה הזה בוטל אוטומטית.</p>{subscription_html}<p><b>Callback URL:</b> <code>{html.escape(webhook)}</code><br><b>Verify token:</b> <code>{html.escape(current.verify_token)}</code></p><p>אם ההרשמה האוטומטית הצליחה, אפשר לשלוח הודעה למספר הבדיקה של Meta מיד.</p></body></html>"""
    return HTMLResponse(success)


async def _subscribe_waba(config: WhatsAppConfig, callback_url: str) -> tuple[bool, str]:
    if not config.waba_id:
        return False, "WABA ID missing"
    url = f"https://graph.facebook.com/{config.graph_version}/{config.waba_id}/subscribed_apps"
    headers = {"Authorization": f"Bearer {config.access_token}", "Content-Type": "application/json"}
    payload = {"override_callback_uri": callback_url, "verify_token": config.verify_token}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.is_success:
            return True, "Webhook subscription configured automatically."
        detail = response.text[:500]
        return False, f"Meta subscription failed ({response.status_code}): {detail}"
    except Exception as exc:
        return False, f"Meta subscription request failed: {type(exc).__name__}"


@app.get("/whatsapp/health")
async def health():
    return {
        "ok": True,
        "configured": gateway.config.configured,
        "allowlist_configured": bool(gateway.config.allowed_numbers),
    }


@app.get("/whatsapp/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if not gateway.config.verify_token:
        raise HTTPException(status_code=503, detail="WhatsApp not configured")
    if mode == "subscribe" and hmac.compare_digest(token or "", gateway.config.verify_token):
        return Response(content=challenge or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/whatsapp/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    if not gateway.config.configured:
        raise HTTPException(status_code=503, detail="WhatsApp not configured")
    if not gateway.verify_signature(body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    background_tasks.add_task(gateway.handle_payload, payload)
    return {"ok": True}


twilio_gateway = TwilioWhatsAppGateway(gateway.service, gateway.settings)


def _twiml_empty() -> Response:
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


@app.post("/twilio/whatsapp")
async def receive_twilio_whatsapp(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    params_multi = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
    params = {key: values[-1] for key, values in params_multi.items() if values}

    forwarded_proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = request.headers.get("host") or request.url.netloc
    signed_url = f"{forwarded_proto}://{host}{request.url.path}"
    if request.url.query:
        signed_url += "?" + request.url.query

    if not twilio_gateway.verify_request(
        signed_url,
        params,
        request.headers.get("x-twilio-signature"),
    ):
        raise HTTPException(status_code=401, detail="Invalid Twilio signature")

    if params.get("AccountSid") and params.get("AccountSid") != twilio_gateway.config.account_sid:
        raise HTTPException(status_code=403, detail="Wrong Twilio account")

    event_id = params.get("MessageSid") or params.get("SmsMessageSid") or ""
    if event_id and not gateway.service.db.claim_webhook_event("twilio_whatsapp", event_id):
        return _twiml_empty()

    twilio_gateway.set_from_number(params.get("To", ""))
    sender = re.sub(r"\D", "", params.get("From", ""))
    if not sender or not twilio_gateway.config.sender_allowed(sender):
        return _twiml_empty()

    num_media = int(params.get("NumMedia") or "0")
    media_url = params.get("MediaUrl0", "") if num_media else ""
    media_type = params.get("MediaContentType0", "") if num_media else ""
    if media_url and media_type.lower().startswith("audio/"):
        message = {
            "from": sender,
            "type": "audio",
            "audio": {
                "id": media_url,
                "mime_type": media_type,
                "voice": True,
            },
        }
    else:
        message = {
            "from": sender,
            "type": "text",
            "text": {"body": params.get("Body", "")},
        }

    background_tasks.add_task(twilio_gateway.handle_message, message)
    return _twiml_empty()


@app.get("/twilio/whatsapp/health")
async def twilio_whatsapp_health():
    return {
        "ok": True,
        "configured": twilio_gateway.config.configured,
        "sender_known": bool(twilio_gateway._runtime_from),
    }

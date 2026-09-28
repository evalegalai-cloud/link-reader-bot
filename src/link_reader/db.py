from __future__ import annotations

import re
import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS content (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            author TEXT,
            duration_seconds INTEGER,
            language TEXT,
            extraction_method TEXT NOT NULL,
            transcript TEXT NOT NULL,
            summary TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_type, external_id)
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL,
            text TEXT NOT NULL,
            map_summary TEXT,
            UNIQUE(content_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_content (
            user_id INTEGER NOT NULL,
            content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
            saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, content_id)
        );
        CREATE INDEX IF NOT EXISTS idx_user_content_recent
            ON user_content(user_id, saved_at DESC, content_id DESC);
        CREATE TABLE IF NOT EXISTS qa_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bot_owner (
            slot INTEGER PRIMARY KEY CHECK(slot = 1),
            user_id INTEGER UNIQUE NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS webhook_events (
            channel TEXT NOT NULL,
            event_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(channel, event_id)
        );
        CREATE TABLE IF NOT EXISTS app_migrations (
            migration_key TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
        with self.connect() as conn:
            conn.executescript(schema)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(content)")}
            additions = {
                "processing_input_tokens": "INTEGER",
                "processing_output_tokens": "INTEGER",
                "processing_cached_input_tokens": "INTEGER",
                "llm_cost_usd": "REAL",
                "transcript_credits": "REAL",
                "transcript_cost_usd": "REAL",
                "processing_cost_usd": "REAL",
            }
            for name, sql_type in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE content ADD COLUMN {name} {sql_type}")
            try:
                conn.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                       content_id UNINDEXED, chunk_id UNINDEXED, title, text,
                       tokenize='unicode61'
                    )"""
                )
                conn.execute(
                    """INSERT OR REPLACE INTO chunk_fts(rowid, content_id, chunk_id, title, text)
                       SELECT ch.id, ch.content_id, ch.id, c.title, ch.text
                       FROM chunks ch JOIN content c ON c.id=ch.content_id"""
                )
            except sqlite3.OperationalError:
                pass

            # Versioned, idempotent migration for installations that predate
            # per-user libraries. Existing current-source and Q&A relationships
            # are preserved; the old personal corpus belongs to the claimed owner.
            migrated = conn.execute(
                "SELECT 1 FROM app_migrations WHERE migration_key=?",
                ("user_content_v1",),
            ).fetchone()
            if not migrated:
                conn.execute(
                    """INSERT OR IGNORE INTO user_content(user_id, content_id)
                       SELECT user_id, content_id FROM user_state"""
                )
                conn.execute(
                    """INSERT OR IGNORE INTO user_content(user_id, content_id)
                       SELECT DISTINCT user_id, content_id FROM qa_history"""
                )
                owner = conn.execute(
                    "SELECT user_id FROM bot_owner WHERE slot=1"
                ).fetchone()
                if owner:
                    conn.execute(
                        """INSERT OR IGNORE INTO user_content(user_id, content_id)
                           SELECT ?, id FROM content""",
                        (int(owner["user_id"]),),
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO app_migrations(migration_key) VALUES (?)",
                    ("user_content_v1",),
                )

    def get_content_by_external_id(self, source_type: str, external_id: str):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM content WHERE source_type=? AND external_id=?",
                (source_type, external_id),
            ).fetchone()

    def save_content(self, item, transcript: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO content
                   (source_type, external_id, url, title, author, duration_seconds,
                    language, extraction_method, transcript)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.source_type, item.external_id, item.url, item.title,
                    item.author, item.duration_seconds, item.language,
                    item.extraction_method, transcript,
                ),
            )
            return int(cur.lastrowid)

    def replace_chunks(self, content_id: int, chunks: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM chunks WHERE content_id=?", (content_id,))
            conn.executemany(
                """INSERT INTO chunks
                   (content_id, ordinal, start_seconds, end_seconds, text, map_summary)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        content_id, c["ordinal"], c["start_seconds"],
                        c["end_seconds"], c["text"], c.get("map_summary"),
                    )
                    for c in chunks
                ],
            )
            try:
                conn.execute("DELETE FROM chunk_fts WHERE content_id=?", (content_id,))
                conn.execute(
                    """INSERT INTO chunk_fts(rowid, content_id, chunk_id, title, text)
                       SELECT ch.id, ch.content_id, ch.id, c.title, ch.text
                       FROM chunks ch JOIN content c ON c.id=ch.content_id
                       WHERE ch.content_id=?""",
                    (content_id,),
                )
            except sqlite3.OperationalError:
                pass

    def set_summary(self, content_id: int, summary: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE content SET summary=? WHERE id=?", (summary, content_id))

    def set_processing_stats(
        self, content_id: int, *, input_tokens: int, output_tokens: int,
        cached_input_tokens: int, llm_cost_usd: float | None,
        transcript_credits: float, transcript_cost_usd: float,
    ) -> None:
        total = None if llm_cost_usd is None else llm_cost_usd + transcript_cost_usd
        with self.connect() as conn:
            conn.execute(
                """UPDATE content SET
                   processing_input_tokens=?, processing_output_tokens=?,
                   processing_cached_input_tokens=?, llm_cost_usd=?,
                   transcript_credits=?, transcript_cost_usd=?, processing_cost_usd=?
                   WHERE id=?""",
                (
                    input_tokens, output_tokens, cached_input_tokens, llm_cost_usd,
                    transcript_credits, transcript_cost_usd, total, content_id,
                ),
            )

    def save_user_content(self, user_id: int, content_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO user_content(user_id, content_id)
                   VALUES (?, ?)""",
                (user_id, content_id),
            )

    def get_user_content(self, user_id: int, content_id: int):
        with self.connect() as conn:
            return conn.execute(
                """SELECT c.* FROM content c
                   JOIN user_content uc ON uc.content_id=c.id
                   WHERE uc.user_id=? AND c.id=?""",
                (user_id, content_id),
            ).fetchone()

    def set_current_content(self, user_id: int, content_id: int) -> None:
        with self.connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM user_content WHERE user_id=? AND content_id=?",
                (user_id, content_id),
            ).fetchone()
            if not owned:
                raise ValueError("המקור הזה אינו נמצא בספרייה שלך.")
            conn.execute(
                """INSERT INTO user_state(user_id, content_id) VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   content_id=excluded.content_id, updated_at=CURRENT_TIMESTAMP""",
                (user_id, content_id),
            )

    def get_current_content(self, user_id: int):
        with self.connect() as conn:
            return conn.execute(
                """SELECT c.* FROM content c
                   JOIN user_state u ON u.content_id=c.id
                   JOIN user_content uc
                     ON uc.user_id=u.user_id AND uc.content_id=c.id
                   WHERE u.user_id=?""",
                (user_id,),
            ).fetchone()

    def get_content(self, content_id: int):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM content WHERE id=?", (content_id,)).fetchone()

    def get_chunks(self, content_id: int):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM chunks WHERE content_id=? ORDER BY ordinal",
                (content_id,),
            ).fetchall()

    def save_qa(self, user_id: int, content_id: int, question: str, answer: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO qa_history(user_id, content_id, question, answer) VALUES (?, ?, ?, ?)",
                (user_id, content_id, question, answer),
            )

    def get_recent_qa(self, user_id: int, content_id: int, limit: int = 6):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT question, answer FROM qa_history
                   WHERE user_id=? AND content_id=?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, content_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def list_recent_content(self, user_id: int, limit: int = 10):
        with self.connect() as conn:
            return conn.execute(
                """SELECT c.id, c.title, c.source_type, c.created_at
                   FROM user_content uc
                   JOIN content c ON c.id=uc.content_id
                   WHERE uc.user_id=?
                   ORDER BY uc.saved_at DESC, c.id DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()

    def search_library(self, user_id: int, query: str, limit: int = 8):
        stopwords = {
            "מה", "מי", "איך", "האם", "על", "של", "את", "זה", "זו", "עם", "כל",
            "בכל", "לי", "לגבי", "אמר", "אמרו", "יש", "אין", "מצא", "חפש", "שאל",
            "הכל", "המקורות", "מקורות", "the", "a", "an", "of", "to", "in", "on",
            "and", "or", "what", "how", "about", "find", "search", "all",
        }
        terms = []
        for term in re.findall(r"[\w\u0590-\u05FF]+", query or "", flags=re.UNICODE):
            clean = term.strip("_").casefold()
            if len(clean) < 2 or clean in stopwords or clean in terms:
                continue
            terms.append(clean)
            if len(terms) >= 10:
                break
        if not terms:
            return []

        with self.connect() as conn:
            try:
                fts_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
                return conn.execute(
                    """SELECT c.id AS content_id, c.title, c.source_type, c.url,
                              ch.ordinal, ch.text,
                              bm25(chunk_fts, 0.0, 0.0, 4.0, 1.0) AS rank
                       FROM chunk_fts
                       JOIN chunks ch ON ch.id=chunk_fts.rowid
                       JOIN content c ON c.id=ch.content_id
                       JOIN user_content uc ON uc.content_id=c.id
                       WHERE uc.user_id=? AND chunk_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (user_id, fts_query, max(1, min(int(limit), 30))),
                ).fetchall()
            except sqlite3.OperationalError:
                clauses = []
                params = []
                for term in terms:
                    clauses.append("(lower(c.title) LIKE ? OR lower(ch.text) LIKE ?)")
                    like = f"%{term}%"
                    params.extend((like, like))
                params.append(max(1, min(int(limit), 30)))
                return conn.execute(
                    f"""SELECT c.id AS content_id, c.title, c.source_type, c.url,
                               ch.ordinal, ch.text, 0.0 AS rank
                        FROM chunks ch
                        JOIN content c ON c.id=ch.content_id
                        JOIN user_content uc ON uc.content_id=c.id
                        WHERE uc.user_id=? AND ({' OR '.join(clauses)})
                        ORDER BY c.id DESC, ch.ordinal
                        LIMIT ?""",
                    [user_id, *params],
                ).fetchall()

    def get_bot_owner(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute("SELECT user_id FROM bot_owner WHERE slot=1").fetchone()
        return int(row["user_id"]) if row else None

    def claim_bot_owner(self, user_id: int) -> bool:
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO bot_owner(slot, user_id) VALUES (1, ?)", (user_id,))
            row = conn.execute("SELECT user_id FROM bot_owner WHERE slot=1").fetchone()
        return bool(row and int(row["user_id"]) == int(user_id))

    def claim_webhook_event(self, channel: str, event_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO webhook_events(channel, event_id) VALUES (?, ?)",
                (channel, event_id),
            )
            return cur.rowcount == 1

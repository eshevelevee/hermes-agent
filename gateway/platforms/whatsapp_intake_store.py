"""
WhatsApp READ_ONLY_INTAKE store.

Owns a Hermes-local SQLite database at ``~/.hermes/whatsapp/intake.db`` and
the six staging tables defined in the security spec.

Invariants enforced here (mirror of plan §Staging Schema):
- No DNA writes. This module imports nothing from any portal_bot / DNA
  module; it only opens its own SQLite handle.
- No negotiation_touchpoints writes. Promotion to that table is a v1 concern
  gated behind explicit SD approval / deterministic mapping.
- No outbound side effects. Pure storage; no HTTP, no Telegram, no Hermes
  core message bus.
- Idempotent on (message_id, chat_id). Replays do not duplicate.
- Backpressure: SQLite write failures are logged + dropped; never blocks the
  polling loop, never signals back to the bridge.

The single ``persist`` coroutine is the only entry point the adapter calls
in READ_ONLY_INTAKE mode (instead of ``handle_message``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    """ISO-8601 UTC with 'Z' suffix, second resolution."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS whatsapp_ingest_messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id      TEXT NOT NULL,
  chat_id         TEXT NOT NULL,
  sender_id       TEXT NOT NULL,
  sender_name     TEXT,
  chat_name       TEXT,
  is_group        INTEGER NOT NULL DEFAULT 0,
  from_me         INTEGER NOT NULL DEFAULT 0,
  upsert_type     TEXT NOT NULL,
  body            TEXT NOT NULL DEFAULT '',
  body_truncated  INTEGER NOT NULL DEFAULT 0,
  has_media       INTEGER NOT NULL DEFAULT 0,
  media_type      TEXT,
  media_local_path TEXT,
  mentioned_ids   TEXT,
  quoted_message_id TEXT,
  quoted_participant TEXT,
  quoted_remote_jid TEXT,
  has_quoted_message INTEGER NOT NULL DEFAULT 0,
  ts_unix         INTEGER NOT NULL,
  ingested_at     TEXT NOT NULL,
  raw_event_sha256 TEXT NOT NULL,
  UNIQUE(message_id, chat_id)
);
CREATE INDEX IF NOT EXISTS ix_ingest_chat_ts ON whatsapp_ingest_messages(chat_id, ts_unix DESC);
CREATE INDEX IF NOT EXISTS ix_ingest_ingested_at ON whatsapp_ingest_messages(ingested_at);

CREATE TABLE IF NOT EXISTS whatsapp_chat_map (
  chat_id         TEXT PRIMARY KEY,
  canonical_phone TEXT,
  lid             TEXT,
  label           TEXT,
  is_group        INTEGER NOT NULL DEFAULT 0,
  allowlisted     INTEGER NOT NULL DEFAULT 0,
  source          TEXT NOT NULL,
  first_seen      TEXT NOT NULL,
  last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whatsapp_media_metadata (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ingest_id       INTEGER NOT NULL REFERENCES whatsapp_ingest_messages(id) ON DELETE CASCADE,
  file_local_path TEXT NOT NULL,
  mime            TEXT,
  size_bytes      INTEGER,
  sha256          TEXT,
  file_name       TEXT,
  media_type      TEXT NOT NULL,
  classification_status TEXT NOT NULL DEFAULT 'pending',
  scanned_at      TEXT,
  scanner_result_json TEXT,
  UNIQUE(ingest_id, file_local_path)
);

CREATE TABLE IF NOT EXISTS extracted_promises (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  source_ingest_id INTEGER NOT NULL REFERENCES whatsapp_ingest_messages(id) ON DELETE CASCADE,
  promise_text    TEXT NOT NULL,
  promise_class   TEXT,
  confidence      REAL,
  status          TEXT NOT NULL DEFAULT 'pending_sd_review',
  extracted_by    TEXT NOT NULL,
  extracted_at    TEXT NOT NULL,
  sd_reviewed_at  TEXT,
  sd_reviewed_by  TEXT,
  sd_review_notes TEXT
);

CREATE TABLE IF NOT EXISTS extracted_questions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  source_ingest_id INTEGER NOT NULL REFERENCES whatsapp_ingest_messages(id) ON DELETE CASCADE,
  question_text   TEXT NOT NULL,
  question_class  TEXT,
  confidence      REAL,
  status          TEXT NOT NULL DEFAULT 'pending_sd_review',
  extracted_by    TEXT NOT NULL,
  extracted_at    TEXT NOT NULL,
  sd_reviewed_at  TEXT,
  sd_reviewed_by  TEXT,
  sd_review_notes TEXT
);

CREATE TABLE IF NOT EXISTS digest_runs (
  run_id          TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  ended_at        TEXT,
  status          TEXT NOT NULL,
  messages_in     INTEGER DEFAULT 0,
  messages_out    INTEGER DEFAULT 0,
  promises_found  INTEGER DEFAULT 0,
  questions_found INTEGER DEFAULT 0,
  cost_usd        REAL DEFAULT 0,
  notes           TEXT,
  error           TEXT
);
"""


class WhatsAppIntakeStore:
    """SQLite writer for the READ_ONLY_INTAKE staging tables.

    All public coroutines wrap their blocking SQLite work in ``asyncio.to_thread``
    so they never block the adapter's polling loop. The connection itself is
    created lazily on first ``persist``, isolated from any other DB in the
    Hermes process (no shared cursor / pool / ORM).
    """

    # Defaults match plan §Bounded History Policy.
    DEFAULT_PER_CHAT_LIMIT = 500
    DEFAULT_RETENTION_DAYS = 30
    DEFAULT_BODY_MAX = 4096
    DEFAULT_MEDIA_RETENTION_DAYS = 7

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        per_chat_limit: Optional[int] = None,
        retention_days: Optional[int] = None,
        body_max: Optional[int] = None,
        media_retention_days: Optional[int] = None,
        media_cache_dirs: Optional[Iterable[Path]] = None,
    ) -> None:
        env = os.environ
        if db_path is None:
            home = Path(env.get("HOME", "~")).expanduser()
            db_path = home / ".hermes" / "whatsapp" / "intake.db"
        self.path = Path(db_path)
        self.per_chat_limit = int(per_chat_limit if per_chat_limit is not None
                                  else env.get("WHATSAPP_INTAKE_PER_CHAT_LIMIT",
                                               self.DEFAULT_PER_CHAT_LIMIT))
        self.retention_days = int(retention_days if retention_days is not None
                                  else env.get("WHATSAPP_INTAKE_RETENTION_DAYS",
                                               self.DEFAULT_RETENTION_DAYS))
        self.body_max = int(body_max if body_max is not None
                            else env.get("WHATSAPP_INTAKE_BODY_MAX",
                                         self.DEFAULT_BODY_MAX))
        self.media_retention_days = int(media_retention_days if media_retention_days is not None
                                        else env.get("WHATSAPP_INTAKE_MEDIA_RETENTION_DAYS",
                                                     self.DEFAULT_MEDIA_RETENTION_DAYS))
        if media_cache_dirs is None:
            home = Path(env.get("HOME", "~")).expanduser()
            media_cache_dirs = [
                home / ".hermes" / "image_cache",
                home / ".hermes" / "document_cache",
                home / ".hermes" / "audio_cache",
            ]
        self.media_cache_dirs = [Path(p) for p in media_cache_dirs]
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ schema

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Restrict parent dir + DB file to owner only; matches session-key hardening.
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        first_create = not self.path.exists()
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_SQL)
        if first_create:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    # ------------------------------------------------------------------ persist

    @staticmethod
    def _truncate_body(body: str, body_max: int) -> Tuple[str, int]:
        if not isinstance(body, str):
            body = "" if body is None else str(body)
        if len(body) <= body_max:
            return body, 0
        # Keep room for the marker; the spec wants a visible "[truncated]" tail.
        marker = "…[truncated]"
        keep = max(1, body_max - len(marker))
        return body[:keep] + marker, 1

    @staticmethod
    def _normalize_mentioned(raw: Any) -> str:
        if not raw:
            return "[]"
        if isinstance(raw, (list, tuple)):
            return json.dumps([str(x) for x in raw if x is not None])
        return json.dumps([str(raw)])

    @staticmethod
    def _coerce_ts_unix(value: Any) -> int:
        if value is None:
            return int(time.time())
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return int(time.time())

    def _build_row_for_insert(
        self,
        event: Any,
        raw: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Project an adapter event + the raw bridge dict into one SQL row.

        The event may be a MessageEvent dataclass (production path) or a plain
        dict (tests). We pull from raw for fields the event doesn't expose
        (sender id, mention ids, quoted-message context).
        """
        # Body — prefer event.text, fall back to raw['body'].
        body = getattr(event, "text", None)
        if body is None:
            body = raw.get("body", "")
        body, truncated = self._truncate_body(body or "", self.body_max)

        chat_id = raw.get("chatId", "") or ""
        message_id = raw.get("messageId", "") or ""
        sender_id = raw.get("senderId") or chat_id
        ts_unix = self._coerce_ts_unix(raw.get("timestamp"))

        media_local_path = None
        media_urls = raw.get("mediaUrls") or []
        if media_urls and isinstance(media_urls, list):
            media_local_path = str(media_urls[0])

        raw_normalized = json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
        raw_event_sha256 = _sha256_hex(raw_normalized)

        return {
            "message_id": message_id,
            "chat_id": chat_id,
            "sender_id": str(sender_id),
            "sender_name": raw.get("senderName"),
            "chat_name": raw.get("chatName"),
            "is_group": 1 if raw.get("isGroup") else 0,
            "from_me": 1 if raw.get("fromMe") else 0,
            "upsert_type": str(raw.get("upsertType", "notify")),
            "body": body,
            "body_truncated": truncated,
            "has_media": 1 if raw.get("hasMedia") else 0,
            "media_type": raw.get("mediaType"),
            "media_local_path": media_local_path,
            "mentioned_ids": self._normalize_mentioned(raw.get("mentionedIds")),
            "quoted_message_id": raw.get("quotedMessageId"),
            "quoted_participant": raw.get("quotedParticipant"),
            "quoted_remote_jid": raw.get("quotedRemoteJid"),
            "has_quoted_message": 1 if raw.get("hasQuotedMessage") else 0,
            "ts_unix": ts_unix,
            "ingested_at": _utcnow_iso(),
            "raw_event_sha256": raw_event_sha256,
        }

    def _insert_sync(self, row: Dict[str, Any], raw: Dict[str, Any]) -> Optional[int]:
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO whatsapp_ingest_messages (
              message_id, chat_id, sender_id, sender_name, chat_name,
              is_group, from_me, upsert_type, body, body_truncated,
              has_media, media_type, media_local_path,
              mentioned_ids, quoted_message_id, quoted_participant,
              quoted_remote_jid, has_quoted_message,
              ts_unix, ingested_at, raw_event_sha256
            ) VALUES (
              :message_id, :chat_id, :sender_id, :sender_name, :chat_name,
              :is_group, :from_me, :upsert_type, :body, :body_truncated,
              :has_media, :media_type, :media_local_path,
              :mentioned_ids, :quoted_message_id, :quoted_participant,
              :quoted_remote_jid, :has_quoted_message,
              :ts_unix, :ingested_at, :raw_event_sha256
            )
            """,
            row,
        )
        if cur.rowcount == 0:
            # Duplicate (message_id, chat_id) — idempotent, no-op.
            return None
        ingest_id = cur.lastrowid

        # Media metadata row (one per ingest row in v0; bridge currently
        # emits at most one mediaUrls entry per message).
        if row["has_media"] and row["media_local_path"]:
            self._insert_media_metadata_sync(ingest_id, row, raw)

        # Chat map upsert.
        self._upsert_chat_map_sync(row)

        return ingest_id

    def _insert_media_metadata_sync(
        self,
        ingest_id: int,
        row: Dict[str, Any],
        raw: Dict[str, Any],
    ) -> None:
        conn = self._conn
        if conn is None:
            return
        file_path = Path(row["media_local_path"])
        size_bytes: Optional[int] = None
        sha256: Optional[str] = None
        mime: Optional[str] = None
        file_name: Optional[str] = None
        try:
            if file_path.exists() and file_path.is_file():
                size_bytes = file_path.stat().st_size
                # Hash up to 32 MB; larger files get a marker hash so we
                # don't stall the polling loop on a giant attachment.
                if size_bytes <= 32 * 1024 * 1024:
                    h = hashlib.sha256()
                    with file_path.open("rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                    sha256 = h.hexdigest()
                file_name = file_path.name
        except OSError as exc:
            logger.debug("intake_store: failed to stat/hash %s: %s", file_path, exc)
        media_type = row.get("media_type") or "unknown"
        conn.execute(
            """
            INSERT OR IGNORE INTO whatsapp_media_metadata (
              ingest_id, file_local_path, mime, size_bytes, sha256,
              file_name, media_type, classification_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                ingest_id, str(file_path), mime, size_bytes, sha256,
                file_name, media_type,
            ),
        )

    def _upsert_chat_map_sync(self, row: Dict[str, Any]) -> None:
        conn = self._conn
        if conn is None:
            return
        now = _utcnow_iso()
        chat_id = row["chat_id"]
        if not chat_id:
            return
        canonical = chat_id.split("@", 1)[0] if "@" in chat_id else chat_id
        lid_form = canonical if chat_id.endswith("@lid") else None
        # Insert-or-bump last_seen. Allowlisted defaults to 1 because the
        # bridge already gated it through allowlist.js#matchesAllowedUser.
        conn.execute(
            """
            INSERT INTO whatsapp_chat_map (
              chat_id, canonical_phone, lid, label, is_group,
              allowlisted, source, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, 1, 'bridge_allowlist', ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
              last_seen = excluded.last_seen,
              label = COALESCE(excluded.label, whatsapp_chat_map.label),
              canonical_phone = COALESCE(excluded.canonical_phone, whatsapp_chat_map.canonical_phone),
              lid = COALESCE(excluded.lid, whatsapp_chat_map.lid)
            """,
            (
                chat_id, canonical if not lid_form else None, lid_form,
                row.get("chat_name"), row.get("is_group", 0),
                now, now,
            ),
        )

    async def persist(self, event: Any, raw: Dict[str, Any]) -> Optional[int]:
        """Async-safe entry point. Returns the new ingest row id or None on dup."""
        row = self._build_row_for_insert(event, raw or {})
        try:
            async with self._write_lock:
                return await asyncio.to_thread(self._insert_sync, row, raw or {})
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "intake_store.persist failed for message_id=%s chat_id=%s — dropped",
                row.get("message_id"), row.get("chat_id"),
            )
            return None

    # ----------------------------------------------------------------- retention

    def _sweep_sync(self) -> Dict[str, int]:
        conn = self._ensure_conn()
        run_id = str(uuid.uuid4())
        started_at = _utcnow_iso()
        conn.execute(
            "INSERT INTO digest_runs (run_id, kind, started_at, status) "
            "VALUES (?, 'retention_sweep', ?, 'running')",
            (run_id, started_at),
        )

        # 1. Age sweep on the message table.
        cutoff_ts = int(time.time()) - self.retention_days * 86400
        cur = conn.execute(
            "DELETE FROM whatsapp_ingest_messages WHERE ts_unix < ?",
            (cutoff_ts,),
        )
        age_deleted = cur.rowcount or 0

        # 2. Per-chat overflow sweep.
        overflow_total = 0
        chats = conn.execute(
            "SELECT chat_id, COUNT(*) AS n FROM whatsapp_ingest_messages "
            "GROUP BY chat_id HAVING n > ?",
            (self.per_chat_limit,),
        ).fetchall()
        for chat_id, n in chats:
            to_drop = n - self.per_chat_limit
            if to_drop <= 0:
                continue
            cur = conn.execute(
                """
                DELETE FROM whatsapp_ingest_messages
                WHERE id IN (
                  SELECT id FROM whatsapp_ingest_messages
                  WHERE chat_id = ?
                  ORDER BY ts_unix ASC
                  LIMIT ?
                )
                """,
                (chat_id, to_drop),
            )
            overflow_total += cur.rowcount or 0

        # 3. Cache file sweep (filesystem). Files older than media_retention_days
        #    are removed; metadata rows referencing them are marked 'expired'.
        media_deleted = 0
        media_cutoff = time.time() - self.media_retention_days * 86400
        for cache_dir in self.media_cache_dirs:
            if not cache_dir.exists():
                continue
            try:
                for entry in cache_dir.iterdir():
                    try:
                        if entry.is_file() and entry.stat().st_mtime < media_cutoff:
                            entry.unlink()
                            media_deleted += 1
                    except OSError:
                        continue
            except OSError:
                continue

        if media_deleted:
            conn.execute(
                "UPDATE whatsapp_media_metadata SET classification_status = 'expired' "
                "WHERE classification_status = 'pending' AND ingest_id NOT IN "
                "(SELECT id FROM whatsapp_ingest_messages)"
            )

        ended_at = _utcnow_iso()
        conn.execute(
            "UPDATE digest_runs SET ended_at = ?, status = 'ok', "
            "messages_in = ?, messages_out = ?, "
            "notes = ? WHERE run_id = ?",
            (
                ended_at,
                age_deleted + overflow_total,
                age_deleted + overflow_total,
                json.dumps({
                    "age_deleted": age_deleted,
                    "overflow_deleted": overflow_total,
                    "media_files_deleted": media_deleted,
                }),
                run_id,
            ),
        )
        return {
            "age_deleted": age_deleted,
            "overflow_deleted": overflow_total,
            "media_files_deleted": media_deleted,
            "run_id": run_id,
        }

    async def sweep(self) -> Dict[str, int]:
        async with self._write_lock:
            return await asyncio.to_thread(self._sweep_sync)

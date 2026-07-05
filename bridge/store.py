from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def encode_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    return json.loads(value)


SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "signature",
    "signed",
    "token",
    "url",
)
SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)"
    r"(?P<key_quote>['\"]?)"
    r"(?P<key>\b[A-Za-z0-9_-]*(?:api[-_]?key|apikey|authorization|cookie|"
    r"password|secret|signature|signed|token|url)[A-Za-z0-9_-]*\b)"
    r"(?P=key_quote)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?:(?P<quote>['\"])(?P<quoted_value>.*?)(?P=quote)|"
    r"(?P<value>(?:(?:Bearer|Basic)\s+)?[^\s,'\";\n]+))"
)
MAX_BRIDGE_EVENTS = 5_000
SQLITE_PARAMETER_CHUNK_SIZE = 900
QUEUE_STATUSES = (
    "pending_approval",
    "approval_claimed",
    "needs_release",
    "denied",
    "download_claimed",
    "download_submitted",
    "download_unverified",
    "download_failed",
    "imported",
    "available",
)
QUEUE_STATUS_CHECK_PATTERN = re.compile(
    r"\bstatus\s+TEXT\s+NOT\s+NULL\s+CHECK\s*"
    r"\(\s*status\s+IN\s*\((?P<values>.*?)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
SQL_STRING_LITERAL_PATTERN = re.compile(r"'((?:''|[^'])*)'")
DOWNLOADABLE_QUEUE_STATUSES = {"needs_release", "download_failed"}
TERMINAL_QUEUE_STATUSES = {"denied", "imported", "available"}
ERROR_EVENT_MARKERS = ("failed", "error", "unverified")


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text):
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = sanitize_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_event_message(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(value)


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def sanitize_event_message(message: str) -> str:
    return SENSITIVE_FIELD_PATTERN.sub(redact_sensitive_field, message)


def redact_sensitive_field(match: re.Match[str]) -> str:
    quote = match.group("quote") or ""
    key_quote = match.group("key_quote")
    return (
        f"{key_quote}{match.group('key')}{key_quote}"
        f"{match.group('sep')}{quote}[REDACTED]{quote}"
    )


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def queue_status_check_matches(table_sql: str) -> bool:
    match = QUEUE_STATUS_CHECK_PATTERN.search(table_sql)
    if match is None:
        return False
    values = {
        value.replace("''", "'")
        for value in SQL_STRING_LITERAL_PATTERN.findall(match.group("values"))
    }
    return values == set(QUEUE_STATUSES)


def merge_seasons(existing: Any, incoming: list[int]) -> list[int]:
    merged = {int(season) for season in incoming}
    if isinstance(existing, list):
        merged.update(int(season) for season in existing)
    return sorted(merged)


class BridgeStore:
    def __init__(self, path: Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("Screenarr requires a file-backed SQLite path")
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queue_items (
                    id TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'show')),
                    external_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    profile_id INTEGER NOT NULL,
                    profile_name TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('auto', 'manual', 'approval')),
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'pending_approval', 'approval_claimed', 'needs_release',
                            'denied', 'download_claimed', 'download_submitted',
                            'download_unverified', 'download_failed', 'imported',
                            'available'
                        )
                    ),
                    mediamanager_id TEXT,
                    season_number INTEGER NOT NULL DEFAULT 0,
                    seasons_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT,
                    UNIQUE(media_type, external_id, profile_id, season_number)
                );

                CREATE TABLE IF NOT EXISTS release_candidates (
                    id TEXT PRIMARY KEY,
                    queue_id TEXT NOT NULL REFERENCES queue_items(id) ON DELETE CASCADE,
                    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'show')),
                    season_number INTEGER NOT NULL DEFAULT 0,
                    result_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    seeders INTEGER NOT NULL DEFAULT 0,
                    size INTEGER NOT NULL DEFAULT 0,
                    usenet INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bridge_events (
                    id TEXT PRIMARY KEY,
                    queue_id TEXT REFERENCES queue_items(id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_release_candidates_queue_id
                    ON release_candidates(queue_id);

                DELETE FROM release_candidates
                WHERE rowid NOT IN (
                    SELECT MIN(rowid)
                    FROM release_candidates
                    GROUP BY queue_id, season_number, result_id
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_release_candidates_identity
                    ON release_candidates(queue_id, season_number, result_id);

                CREATE INDEX IF NOT EXISTS idx_bridge_events_queue_id
                    ON bridge_events(queue_id);

                CREATE INDEX IF NOT EXISTS idx_bridge_events_created_at
                    ON bridge_events(created_at);

                INSERT INTO schema_meta(key, value)
                VALUES ('schema_version', '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """
            )
            self._migrate_queue_status_check(conn)

    def _migrate_queue_status_check(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'queue_items'"
        ).fetchone()
        table_sql = str(row["sql"] if row is not None else "")
        if queue_status_check_matches(table_sql):
            return

        status_values = ", ".join(f"'{status}'" for status in QUEUE_STATUSES)
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            create_queue_items_sql = """
                CREATE TABLE queue_items_new (
                    id TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'show')),
                    external_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    profile_id INTEGER NOT NULL,
                    profile_name TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('auto', 'manual', 'approval')),
                    status TEXT NOT NULL CHECK (status IN (__STATUS_VALUES__)),
                    mediamanager_id TEXT,
                    season_number INTEGER NOT NULL DEFAULT 0,
                    seasons_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT,
                    UNIQUE(media_type, external_id, profile_id, season_number)
                )
                """.replace("__STATUS_VALUES__", status_values)
            conn.execute(
                create_queue_items_sql
            )
            conn.execute(
                """
                INSERT INTO queue_items_new (
                    id, media_type, external_id, title, profile_id, profile_name, mode,
                    status, mediamanager_id, season_number, seasons_json, payload_json,
                    created_at, updated_at, resolved_at
                )
                SELECT
                    id, media_type, external_id, title, profile_id, profile_name, mode,
                    status, mediamanager_id, season_number, seasons_json, payload_json,
                    created_at, updated_at, resolved_at
                FROM queue_items
                """
            )
            conn.execute("DROP TABLE queue_items")
            conn.execute("ALTER TABLE queue_items_new RENAME TO queue_items")
            failed_fk = conn.execute("PRAGMA foreign_key_check").fetchone()
            if failed_fk is not None:
                raise sqlite3.IntegrityError("queue item migration failed foreign key check")
            conn.execute("COMMIT")
        except Exception:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_queue_item(
        self,
        *,
        media_type: str,
        external_id: int,
        title: str,
        profile_id: int,
        profile_name: str,
        mode: str,
        status: str,
        mediamanager_id: str | None = None,
        season_number: int = 0,
        seasons: list[int] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        payload_json = encode_json(sanitize_payload(payload)) if payload is not None else None
        with self._connect() as conn:
            if seasons is not None:
                conn.execute("BEGIN IMMEDIATE")
            seasons_json = self._merged_seasons_json(
                conn,
                media_type=media_type,
                external_id=external_id,
                profile_id=profile_id,
                season_number=season_number,
                seasons=seasons,
            )
            conn.execute(
                """
                INSERT INTO queue_items (
                    id, media_type, external_id, title, profile_id, profile_name, mode,
                    status, mediamanager_id, season_number, seasons_json, payload_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_type, external_id, profile_id, season_number)
                DO UPDATE SET
                    title = excluded.title,
                    profile_name = excluded.profile_name,
                    mode = queue_items.mode,
                    status = queue_items.status,
                    mediamanager_id = COALESCE(
                        excluded.mediamanager_id,
                        queue_items.mediamanager_id
                    ),
                    seasons_json = COALESCE(?, queue_items.seasons_json),
                    payload_json = COALESCE(?, queue_items.payload_json),
                    resolved_at = queue_items.resolved_at,
                    updated_at = queue_items.updated_at
                """,
                (
                    str(uuid.uuid4()),
                    media_type,
                    external_id,
                    title,
                    profile_id,
                    profile_name,
                    mode,
                    status,
                    mediamanager_id,
                    season_number,
                    seasons_json or "[]",
                    payload_json or "{}",
                    now,
                    now,
                    seasons_json,
                    payload_json,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM queue_items
                WHERE media_type = ? AND external_id = ? AND profile_id = ? AND season_number = ?
                """,
                (media_type, external_id, profile_id, season_number),
            ).fetchone()
            if row is None:
                raise KeyError(f"{media_type}:{external_id}:{profile_id}:{season_number}")
            return self._queue_item_from_conn(conn, str(row["id"]))

    def list_queue_items(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM queue_items
                ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC
                """
            ).fetchall()
            return self._queue_rows_with_details(conn, rows)

    def recover_stale_claims(self, *, older_than_seconds: int = 900) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE queue_items
                SET status = CASE
                        WHEN status = 'approval_claimed' THEN 'pending_approval'
                        WHEN status = 'download_claimed' THEN 'needs_release'
                        ELSE status
                    END,
                    updated_at = ?
                WHERE status IN ('approval_claimed', 'download_claimed')
                    AND datetime(updated_at) <= datetime(?)
                """,
                (utc_now(), cutoff.replace(microsecond=0).isoformat()),
            )
            return cursor.rowcount

    def get_queue_item(self, queue_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._queue_item_from_conn(conn, queue_id)

    def find_queue_items(
        self,
        *,
        media_type: str,
        external_id: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM queue_items
                WHERE media_type = ? AND external_id = ?
                ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC
                """,
                (media_type, external_id),
            ).fetchall()
            return self._queue_rows_with_details(conn, rows)

    def list_events(self, queue_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(0, min(limit, MAX_BRIDGE_EVENTS))
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM queue_items WHERE id = ?",
                (queue_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(queue_id)
            rows = conn.execute(
                """
                SELECT * FROM bridge_events
                WHERE queue_id = ?
                ORDER BY datetime(created_at) DESC, rowid DESC
                LIMIT ?
                """,
                (queue_id, safe_limit),
            ).fetchall()
            return [self._event_row(row) for row in rows]

    def get_candidate(self, queue_id: str, candidate_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM release_candidates
                WHERE queue_id = ? AND id = ?
                """,
                (queue_id, candidate_id),
            ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return self._candidate_row(row)

    def delete_candidates_for_season(self, queue_id: str, season_number: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM release_candidates WHERE queue_id = ? AND season_number = ?",
                (queue_id, season_number),
            )

    def replace_candidates(
        self,
        queue_id: str,
        candidates: list[dict[str, Any]],
        *,
        media_type: str,
        season_number: int = 0,
        clear_empty_batches: bool = True,
    ) -> None:
        self.replace_candidate_batches(
            queue_id,
            [(candidates, media_type, season_number)],
            clear_empty_batches=clear_empty_batches,
        )

    def replace_candidate_batches(
        self,
        queue_id: str,
        batches: list[tuple[list[dict[str, Any]], str, int]],
        *,
        clear_empty_batches: bool = True,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            parent = conn.execute(
                "SELECT media_type FROM queue_items WHERE id = ?",
                (queue_id,),
            ).fetchone()
            if parent is None:
                raise KeyError(queue_id)
            parent_media_type = str(parent["media_type"])
            prepared_batches = []
            seen_batch_keys: set[tuple[str, int]] = set()
            for candidates, media_type, season_number in batches:
                if media_type != parent_media_type:
                    raise ValueError(
                        "candidate media_type does not match queue item media_type"
                    )
                batch_key = (media_type, season_number)
                if batch_key in seen_batch_keys:
                    raise ValueError(
                        "duplicate candidate batch for queue item and season_number"
                    )
                seen_batch_keys.add(batch_key)
                prepared_batches.append(
                    (
                        media_type,
                        season_number,
                        self._prepare_candidate_rows(queue_id, candidates, season_number),
                    )
                )
            for media_type, season_number, rows in prepared_batches:
                if not rows and not clear_empty_batches:
                    continue
                conn.execute(
                    "DELETE FROM release_candidates WHERE queue_id = ? AND season_number = ?",
                    (queue_id, season_number),
                )
                if not rows:
                    continue
                for candidate, result_id, candidate_id in rows:
                    conn.execute(
                        """
                        INSERT INTO release_candidates (
                            id, queue_id, media_type, season_number, result_id, title, score,
                            seeders, size, usenet, raw_json, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(queue_id, season_number, result_id) DO UPDATE SET
                            media_type = excluded.media_type,
                            title = excluded.title,
                            score = excluded.score,
                            seeders = excluded.seeders,
                            size = excluded.size,
                            usenet = excluded.usenet,
                            raw_json = excluded.raw_json,
                            created_at = excluded.created_at
                        """,
                        (
                            candidate_id,
                            queue_id,
                            media_type,
                            season_number,
                            result_id,
                            str(
                                candidate.get("title")
                                or candidate.get("torrent_title")
                                or result_id
                            ),
                            as_int(candidate.get("score")),
                            as_int(candidate.get("seeders")),
                            as_int(candidate.get("size")),
                            1 if as_bool(candidate.get("usenet")) else 0,
                            encode_json(sanitize_payload(candidate)),
                            now,
                        ),
                    )

    def transition_queue_item(
        self,
        queue_id: str,
        *,
        from_status: str,
        to_status: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE queue_items
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (to_status, utc_now(), queue_id, from_status),
            )
            if cursor.rowcount == 0:
                return None
            return self._queue_item_from_conn(conn, queue_id)

    def update_queue_item(
        self,
        queue_id: str,
        *,
        status: str | None = None,
        mediamanager_id: str | None = None,
        resolved: bool = False,
        from_status: str | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM queue_items WHERE id = ?", (queue_id,)).fetchone()
            if row is None:
                raise KeyError(queue_id)
            resolved_at = utc_now() if resolved else row["resolved_at"]
            cursor = conn.execute(
                """
                UPDATE queue_items
                SET status = COALESCE(?, status),
                    mediamanager_id = COALESCE(?, mediamanager_id),
                    updated_at = ?, resolved_at = ?
                WHERE id = ? AND (? IS NULL OR status = ?)
                """,
                (
                    status,
                    mediamanager_id,
                    utc_now(),
                    resolved_at,
                    queue_id,
                    from_status,
                    from_status,
                ),
            )
            if cursor.rowcount == 0:
                return None
            return self._queue_item_from_conn(conn, queue_id)

    def add_event(
        self,
        *,
        event_type: str,
        message: str,
        queue_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bridge_events(
                    id, queue_id, event_type, message, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    queue_id,
                    event_type,
                    sanitize_event_message(message),
                    encode_json(sanitize_payload(payload or {})),
                    utc_now(),
                ),
            )
            self._prune_events(conn)

    def get_or_create_meta(self, key: str, value_factory: Callable[[], str]) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                return str(row["value"])
            generated = value_factory()
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
                (key, generated),
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row is not None else generated

    def _prune_events(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            DELETE FROM bridge_events
            WHERE rowid NOT IN (
                SELECT rowid
                FROM bridge_events
                ORDER BY datetime(created_at) DESC, rowid DESC
                LIMIT ?
            )
            """,
            (MAX_BRIDGE_EVENTS,),
        )

    def _prepare_candidate_rows(
        self,
        queue_id: str,
        candidates: list[dict[str, Any]],
        season_number: int,
    ) -> list[tuple[dict[str, Any], str, str]]:
        rows: list[tuple[dict[str, Any], str, str]] = []
        seen_result_ids: set[str] = set()
        for candidate in candidates:
            result_id = candidate.get("public_indexer_result_id") or candidate.get("id")
            if result_id is None:
                raise ValueError("release candidate is missing an id")
            result_id = str(result_id).strip()
            if not result_id:
                raise ValueError("release candidate is missing an id")
            if result_id in seen_result_ids:
                continue
            seen_result_ids.add(result_id)
            candidate_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"screenarr:{queue_id}:{season_number}:{result_id}",
                )
            )
            rows.append((candidate, result_id, candidate_id))
        return rows

    def _merged_seasons_json(
        self,
        conn: sqlite3.Connection,
        *,
        media_type: str,
        external_id: int,
        profile_id: int,
        season_number: int,
        seasons: list[int] | None,
    ) -> str | None:
        if seasons is None:
            return None
        existing = conn.execute(
            """
            SELECT seasons_json FROM queue_items
            WHERE media_type = ?
                AND external_id = ?
                AND profile_id = ?
                AND season_number = ?
            """,
            (media_type, external_id, profile_id, season_number),
        ).fetchone()
        if existing is None:
            return encode_json(merge_seasons([], seasons))
        return encode_json(merge_seasons(decode_json(existing["seasons_json"], []), seasons))

    def _queue_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "media_type": row["media_type"],
            "external_id": row["external_id"],
            "title": row["title"],
            "profile_id": row["profile_id"],
            "profile_name": row["profile_name"],
            "mode": row["mode"],
            "status": row["status"],
            "mediamanager_id": row["mediamanager_id"],
            "season_number": row["season_number"],
            "seasons": decode_json(row["seasons_json"], []),
            "payload": decode_json(row["payload_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
        }

    def _queue_item_from_conn(self, conn: sqlite3.Connection, queue_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM queue_items WHERE id = ?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(queue_id)
        candidates = conn.execute(
            """
            SELECT * FROM release_candidates
            WHERE queue_id = ?
            ORDER BY season_number ASC, score DESC, seeders DESC, size ASC
            """,
            (queue_id,),
        ).fetchall()
        item = self._queue_row(row)
        item["candidates"] = [self._candidate_row(candidate) for candidate in candidates]
        self._attach_event_summary(conn, item)
        return item

    def _queue_rows_with_details(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        items = [self._queue_row(row) for row in rows]
        queue_ids = [item["id"] for item in items]
        candidates_by_queue: dict[str, list[dict[str, Any]]] = {}
        event_summaries: dict[str, dict[str, Any]] = {}
        for start in range(0, len(queue_ids), SQLITE_PARAMETER_CHUNK_SIZE):
            chunk = queue_ids[start : start + SQLITE_PARAMETER_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            candidate_rows = conn.execute(
                f"""
                SELECT * FROM release_candidates
                WHERE queue_id IN ({placeholders})
                ORDER BY queue_id, season_number ASC, score DESC, seeders DESC, size ASC
                """,
                chunk,
            ).fetchall()
            for candidate in candidate_rows:
                candidates_by_queue.setdefault(candidate["queue_id"], []).append(
                    self._candidate_row(candidate)
                )
            event_summaries.update(self._event_summaries(conn, chunk))
        for item in items:
            item["candidates"] = candidates_by_queue.get(item["id"], [])
            item.update(
                event_summaries.get(
                    item["id"],
                    {"last_event": None, "last_error": None},
                )
            )
        return items

    def _attach_event_summary(self, conn: sqlite3.Connection, item: dict[str, Any]) -> None:
        summary = self._event_summaries(conn, [item["id"]]).get(
            item["id"],
            {"last_event": None, "last_error": None},
        )
        item.update(summary)

    def _event_summaries(
        self,
        conn: sqlite3.Connection,
        queue_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not queue_ids:
            return {}
        placeholders = ",".join("?" for _ in queue_ids)
        rows = conn.execute(
            f"""
            SELECT * FROM bridge_events
            WHERE queue_id IN ({placeholders})
            ORDER BY queue_id, datetime(created_at) DESC, rowid DESC
            """,
            queue_ids,
        ).fetchall()
        summaries = {
            queue_id: {"last_event": None, "last_error": None}
            for queue_id in queue_ids
        }
        for row in rows:
            queue_id = str(row["queue_id"])
            summary = summaries.setdefault(
                queue_id,
                {"last_event": None, "last_error": None},
            )
            event = self._event_row(row)
            if summary["last_event"] is None:
                summary["last_event"] = event
            if summary["last_error"] is None:
                event_type = str(row["event_type"]).lower()
                if any(marker in event_type for marker in ERROR_EVENT_MARKERS):
                    summary["last_error"] = event
        return summaries

    def _event_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "queue_id": row["queue_id"],
            "event_type": row["event_type"],
            "message": row["message"],
            "payload": decode_json(row["payload_json"], {}),
            "created_at": row["created_at"],
        }

    def _candidate_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "queue_id": row["queue_id"],
            "media_type": row["media_type"],
            "season_number": row["season_number"],
            "result_id": row["result_id"],
            "title": row["title"],
            "score": row["score"],
            "seeders": row["seeders"],
            "size": row["size"],
            "usenet": bool(row["usenet"]),
            "raw": decode_json(row["raw_json"], {}),
            "created_at": row["created_at"],
        }

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import bridge.main as main_module
import bridge.store as store_module
from bridge.config import BridgeConfig, BridgeProfile, ReleaseMode, Settings
from bridge.dashboard import (
    candidate_table_html,
    dashboard_html,
    events_html,
    format_size,
    queue_controls_html,
    queue_profile_label,
)
from bridge.main import (
    MAX_BULK_RECONCILE_ITEMS,
    MAX_DASHBOARD_LOGIN_BODY_BYTES,
    MAX_WEBHOOK_BODY_BYTES,
    AutoDownloadAmbiguity,
    create_app,
    mark_download_failed,
    mediamanager_reconcile_loop,
    mediamanager_reconcile_summary,
    mediamanager_reports_grab_activity,
    parse_webhook_int,
    record_auto_download_unverified,
    refresh_candidates_for_item,
    resolve_dashboard_session_secret,
    validate_live_libraries,
)
from bridge.mediamanager import MediaManagerError, ReleaseChoice
from bridge.security import LoginThrottle, verify_dashboard_session, verify_onscreen_signature
from bridge.store import BridgeStore
from bridge.validation import numeric_score, validate_static_config

TEST_WEBHOOK_SECRET = "webhook-secret-value-for-tests-0001"  # noqa: S105
DOWNLOAD_FAILURE_MESSAGE = "temporary download failure"
DOWNLOAD_TIMEOUT_MESSAGE = "timed out"
DOWNLOAD_CONNECT_MESSAGE = "connect failed"
UNEXPECTED_DOWNLOAD_MESSAGE = "transport exploded"


def webhook_secret_setting() -> dict[str, str]:
    return {"ONSCREEN_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}  # noqa: S105


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeMediaManager:
    def __init__(self) -> None:
        self.added_movies: list[int] = []
        self.added_shows: list[int] = []
        self.downloaded_movies: list[str] = []
        self.downloaded_shows: list[str] = []
        self.movie_library_sets: list[tuple[str, str]] = []
        self.show_library_sets: list[tuple[str, str]] = []
        self.fail_next_download = False
        self.fail_next_download_timeout = False
        self.fail_next_download_transport = False
        self.fail_next_download_unexpectedly = False
        self.movie_detail: dict[str, Any] = {"id": "movie-uuid", "downloaded": False}
        self.show_detail: dict[str, Any] = {"id": "show-uuid", "downloaded": False}
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def add_movie(self, tmdb_id: int, language: str | None = None) -> dict[str, Any]:
        self.added_movies.append(tmdb_id)
        return {"id": "movie-uuid", "external_id": tmdb_id, "library": "Default"}

    async def set_movie_library(self, movie_uuid: str, library: str) -> None:
        self.movie_library_sets.append((movie_uuid, library))

    async def search_movie_torrents(self, movie_uuid: str) -> list[dict[str, Any]]:
        return [
            {
                "public_indexer_result_id": "release-one",
                "title": "Release One",
                "score": 100,
                "seeders": 20,
            }
        ]

    def raise_next_download_failure(self) -> None:
        if self.fail_next_download:
            self.fail_next_download = False
            raise MediaManagerError(DOWNLOAD_FAILURE_MESSAGE)
        if self.fail_next_download_timeout:
            self.fail_next_download_timeout = False
            raise httpx.ReadTimeout(DOWNLOAD_TIMEOUT_MESSAGE)
        if self.fail_next_download_transport:
            self.fail_next_download_transport = False
            raise httpx.ConnectError(DOWNLOAD_CONNECT_MESSAGE)
        if self.fail_next_download_unexpectedly:
            self.fail_next_download_unexpectedly = False
            raise RuntimeError(UNEXPECTED_DOWNLOAD_MESSAGE)

    async def download_movie_torrent(
        self,
        _movie_uuid: str,
        result_id: str,
    ) -> dict[str, Any]:
        self.raise_next_download_failure()
        self.downloaded_movies.append(result_id)
        # A successful grab shows up as active download activity on the detail.
        self.movie_detail = {**self.movie_detail, "downloading": True}
        return {"id": "torrent-uuid"}

    async def get_movie(self, movie_uuid: str) -> dict[str, Any]:
        assert movie_uuid == self.movie_detail["id"]
        return self.movie_detail

    async def add_show(self, tmdb_id: int, language: str | None = None) -> dict[str, Any]:
        self.added_shows.append(tmdb_id)
        return {"id": "show-uuid", "external_id": tmdb_id, "library": "Default"}

    async def set_show_library(self, show_uuid: str, library: str) -> None:
        self.show_library_sets.append((show_uuid, library))

    async def search_show_torrents(
        self,
        show_uuid: str,
        season_number: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "public_indexer_result_id": f"show-release-s{season_number}",
                "title": f"Show Release S{season_number}",
                "score": 100,
                "seeders": 20,
            }
        ]

    async def download_show_torrent(self, show_uuid: str, result_id: str) -> dict[str, Any]:
        self.raise_next_download_failure()
        self.downloaded_shows.append(result_id)
        # A successful grab shows up as active download activity on the detail.
        self.show_detail = {**self.show_detail, "downloading": True}
        return {"id": "torrent-uuid"}

    async def get_show(self, show_uuid: str) -> dict[str, Any]:
        assert show_uuid == self.show_detail["id"]
        return self.show_detail

    async def movie_libraries(self) -> list[dict[str, Any]]:
        return [{"name": "Default"}]

    async def show_libraries(self) -> list[dict[str, Any]]:
        return [{"name": "Default"}]


class MissingLibraryMediaManager(FakeMediaManager):
    async def movie_libraries(self) -> list[dict[str, Any]]:
        return [{"name": "Other"}]

    async def show_libraries(self) -> list[dict[str, Any]]:
        return [{"name": "Other"}]


class FailingLibraryMediaManager(FakeMediaManager):
    async def movie_libraries(self) -> list[dict[str, Any]]:
        raise MediaManagerError("down")


class InvalidSecondSeasonMediaManager(FakeMediaManager):
    async def search_show_torrents(
        self,
        show_uuid: str,
        season_number: int,
    ) -> list[dict[str, Any]]:
        if season_number == 1:
            return [{"public_indexer_result_id": "new-season-one", "title": "New Season One"}]
        return [{"title": "Missing Result ID"}]


class SelectiveDetailMediaManager(FakeMediaManager):
    async def get_movie(self, movie_uuid: str) -> dict[str, Any]:
        if movie_uuid == "movie-down":
            raise MediaManagerError("MediaManager detail unavailable")
        if movie_uuid == "movie-transport-down":
            raise httpx.ConnectError("MediaManager transport unavailable")
        return {"id": movie_uuid, "downloaded": True}


class SlowDetailMediaManager(SelectiveDetailMediaManager):
    async def get_movie(self, movie_uuid: str) -> dict[str, Any]:
        if movie_uuid == "movie-slow":
            await anyio.sleep(60)
        return await super().get_movie(movie_uuid)


def settings_for(path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "BRIDGE_API_KEY": "test",
        "MEDIAMANAGER_BASE_URL": "http://mediamanager:8000",
        "CONFIG_PATH": "missing.yaml",
        "SCREENARR_DATA_PATH": path,
    }
    values.update(overrides)
    return Settings(**values)


def test_settings_rejects_excessive_mediamanager_timeout(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match=r"(?s)MEDIAMANAGER_TIMEOUT_SECONDS.*less than or equal to 600",
    ):
        settings_for(tmp_path / "screenarr.db", MEDIAMANAGER_TIMEOUT_SECONDS=601)


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()


@pytest.fixture
def movie_store(tmp_path: Path) -> BridgeStore:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    return store


def seed_movie_item(store: BridgeStore, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "media_type": "movie",
        "external_id": 550,
        "title": "Fight Club",
        "profile_id": 101,
        "profile_name": "Manual",
        "mode": "manual",
        "status": "needs_release",
    }
    values.update(overrides)
    return store.upsert_queue_item(**values)


def movie_payload(profile_id: int = 101) -> dict[str, Any]:
    return {
        "title": "Fight Club",
        "tmdbId": 550,
        "qualityProfileId": profile_id,
        "addOptions": {"searchForMovie": True},
    }


def show_payload(profile_id: int = 101, seasons: list[int] | None = None) -> dict[str, Any]:
    season_numbers = seasons or [1]
    return {
        "title": "A Show",
        "tvdbId": 12345,
        "qualityProfileId": profile_id,
        "seasons": [
            {"seasonNumber": season_number, "monitored": True}
            for season_number in season_numbers
        ],
        "addOptions": {"searchForMissingEpisodes": True},
    }


def test_settings_require_webhook_secret_when_webhook_is_enabled(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ONSCREEN_WEBHOOK_SECRET"):
        settings_for(tmp_path / "screenarr.db", ENABLE_ONSCREEN_WEBHOOK=True)
    with pytest.raises(ValueError, match="at least 32 characters"):
        settings_for(
            tmp_path / "screenarr.db",
            ENABLE_ONSCREEN_WEBHOOK=True,
            ONSCREEN_WEBHOOK_SECRET="too-short",
        )
    settings = settings_for(
        tmp_path / "screenarr.db",
        ENABLE_ONSCREEN_WEBHOOK=True,
        ONSCREEN_WEBHOOK_SECRET=f"  {TEST_WEBHOOK_SECRET}\n",
    )
    assert settings.onscreen_webhook_secret.get_secret_value() == TEST_WEBHOOK_SECRET
    body = b'{"event":"ok"}'
    timestamp = str(int(time.time()))
    signature = sign_webhook(TEST_WEBHOOK_SECRET, timestamp, body)
    assert verify_onscreen_signature(
        settings.onscreen_webhook_secret.get_secret_value(),
        timestamp,
        signature,
        body,
    )
    exact_secret = "x" * 32
    exact_settings = settings_for(
        tmp_path / "screenarr.db",
        ENABLE_ONSCREEN_WEBHOOK=True,
        ONSCREEN_WEBHOOK_SECRET=f"\n{exact_secret} ",
    )
    assert exact_settings.onscreen_webhook_secret.get_secret_value() == exact_secret
    dashboard_secret = "d" * 32
    dashboard_settings = settings_for(
        tmp_path / "screenarr.db",
        DASHBOARD_SESSION_SECRET=f" {dashboard_secret}\n",
    )
    assert dashboard_settings.dashboard_session_secret.get_secret_value() == dashboard_secret
    with pytest.raises(ValueError, match="DASHBOARD_SESSION_SECRET"):
        settings_for(tmp_path / "screenarr.db", DASHBOARD_SESSION_SECRET="too-short")


def test_settings_strip_mediamanager_config_path(tmp_path: Path) -> None:
    config_path = tmp_path / "mediamanager.toml"
    settings = settings_for(
        tmp_path / "screenarr.db",
        MEDIAMANAGER_CONFIG_PATH=f"  {config_path}\n",
    )
    disabled = settings_for(tmp_path / "screenarr.db", MEDIAMANAGER_CONFIG_PATH=" \t ")

    assert settings.mediamanager_config_path == config_path
    assert disabled.mediamanager_config_path is None


def test_settings_include_mediamanager_timeout_and_reconcile_defaults(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "screenarr.db")
    custom = settings_for(
        tmp_path / "custom-screenarr.db",
        MEDIAMANAGER_TIMEOUT_SECONDS=45,
        ENABLE_MEDIAMANAGER_RECONCILE=True,
        MEDIAMANAGER_RECONCILE_INTERVAL_SECONDS=60,
    )

    assert settings.mediamanager_timeout_seconds == 120
    assert not settings.enable_mediamanager_reconcile
    assert settings.mediamanager_reconcile_interval_seconds == 300
    assert custom.mediamanager_timeout_seconds == 45
    assert custom.enable_mediamanager_reconcile
    assert custom.mediamanager_reconcile_interval_seconds == 60


def test_numeric_score_rejects_invalid_numeric_edges() -> None:
    assert numeric_score(7) == 7.0
    assert numeric_score(float("inf")) is None
    assert numeric_score(float("nan")) is None
    assert numeric_score(10**10_000) is None
    assert numeric_score(True) is None
    assert numeric_score(False) is None


def test_dashboard_session_secret_fallback_is_persisted(tmp_path: Path) -> None:
    db_path = tmp_path / "screenarr.db"
    settings = settings_for(db_path, ENABLE_DASHBOARD=True)
    store = BridgeStore(db_path)
    store.init()

    first = resolve_dashboard_session_secret(settings, store)
    second = resolve_dashboard_session_secret(settings, store)

    assert first == second
    assert len(first) == 64


def make_flow_client(
    path: Path,
    profile: BridgeProfile,
    *,
    mediamanager: FakeMediaManager | None = None,
    **settings_overrides: Any,
) -> tuple[TestClient, FakeMediaManager]:
    fake = mediamanager or FakeMediaManager()
    app = create_app(
        settings=settings_for(path, ENABLE_DASHBOARD=True, **settings_overrides),
        config=BridgeConfig(profiles=[profile]),
        mediamanager_factory=lambda _settings: fake,
    )
    client = TestClient(app)
    return client, fake


def post_media_and_first_queue_item(
    client: TestClient,
    *,
    endpoint: str = "/api/v3/movie",
    payload: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    response = client.post(
        endpoint,
        json=payload or movie_payload(),
        headers={"X-Api-Key": "test"},
    )
    item = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()[
        "items"
    ][0]
    return response, item


class StopReconcileLoop(Exception):
    pass


def stop_background_reconcile_after_iterations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    iterations: int = 1,
) -> None:
    sleep_calls = 0

    async def stop_after_iterations(_interval_seconds: int) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > iterations:
            raise StopReconcileLoop

    monkeypatch.setattr(main_module.asyncio, "sleep", stop_after_iterations)


def cookie_flags(set_cookie: str) -> set[str]:
    return {part.strip().lower() for part in set_cookie.split(";")}


def test_flow_client_injects_fake_mediamanager_during_lifespan(tmp_path: Path) -> None:
    client, fake = make_flow_client(
        tmp_path / "screenarr.db",
        BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL),
    )

    with client:
        assert client.app.state.mm is fake
        assert not fake.closed

    assert fake.closed


def test_store_migrates_and_keeps_duplicate_requests_idempotent(
    movie_store: BridgeStore,
) -> None:
    first = seed_movie_item(movie_store)
    second = seed_movie_item(movie_store)

    assert first["id"] == second["id"]
    assert movie_store.get_queue_item(first["id"])["status"] == "needs_release"

    approval_duplicate = seed_movie_item(
        movie_store,
        profile_name="Approval",
        mode="approval",
        status="pending_approval",
    )
    assert approval_duplicate["status"] == "needs_release"


def test_store_duplicate_show_enqueue_merges_seasons(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    first = store.upsert_queue_item(
        media_type="show",
        external_id=12345,
        title="A Show",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="needs_release",
        seasons=[1],
    )
    second = store.upsert_queue_item(
        media_type="show",
        external_id=12345,
        title="A Show",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="needs_release",
        seasons=[2, 1],
    )

    assert first["id"] == second["id"]
    assert second["seasons"] == [1, 2]


def test_store_redacts_persisted_payloads(movie_store: BridgeStore) -> None:
    item = seed_movie_item(
        movie_store,
        payload={
            "api_key": "secret",
            "safe": "ok",
            "upstream_error": "failed with Authorization=Bearer abc123",
            "quoted_error": (
                '"api_key":"secret" and \'token\': \'secret\' '
                'password="abc def" cookie="a=b; foo=bar"'
            ),
        },
    )
    movie_store.replace_candidates(
        item["id"],
        [
            {
                "public_indexer_result_id": "release-one",
                "download_url": "https://signed",
                "usenet": "false",
                "score": "bad",
                "seeders": "",
                "size": None,
            }
        ],
        media_type="movie",
    )

    stored = movie_store.get_queue_item(item["id"])
    assert stored["payload"] == {
        "api_key": "[REDACTED]",
        "safe": "ok",
        "upstream_error": "failed with Authorization=[REDACTED]",
        "quoted_error": (
            '"api_key":"[REDACTED]" and \'token\': \'[REDACTED]\' '
            'password="[REDACTED]" cookie="[REDACTED]"'
        ),
    }
    assert stored["candidates"][0]["raw"]["download_url"] == "[REDACTED]"
    assert stored["candidates"][0]["usenet"] is False
    assert stored["candidates"][0]["score"] == 0
    assert stored["candidates"][0]["seeders"] == 0
    assert stored["candidates"][0]["size"] == 0


def test_store_redacts_sensitive_event_messages(tmp_path: Path) -> None:
    db_path = tmp_path / "screenarr.db"
    store = BridgeStore(db_path)
    store.init()

    store.add_event(event_type="test", message="token leaked in upstream error")
    store.add_event(
        event_type="test",
        message="download failed token=super-secret url=https://signed",
    )

    conn = sqlite3.connect(db_path)
    try:
        messages = [
            row[0]
            for row in conn.execute(
                "SELECT message FROM bridge_events ORDER BY rowid"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert messages == [
        "token leaked in upstream error",
        "download failed token=[REDACTED] url=[REDACTED]",
    ]


def test_store_prunes_old_bridge_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "screenarr.db"
    store = BridgeStore(db_path)
    store.init()
    monkeypatch.setattr(store_module, "MAX_BRIDGE_EVENTS", 2)

    store.add_event(event_type="test", message="first")
    store.add_event(event_type="test", message="second")
    store.add_event(event_type="test", message="third")

    conn = sqlite3.connect(db_path)
    try:
        messages = [
            row[0]
            for row in conn.execute(
                "SELECT message FROM bridge_events ORDER BY rowid"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert messages == ["second", "third"]


def test_store_list_events_clamps_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "screenarr.db"
    store = BridgeStore(db_path)
    store.init()
    monkeypatch.setattr(store_module, "MAX_BRIDGE_EVENTS", 2)
    item = seed_movie_item(store)

    store.add_event(event_type="test", message="first", queue_id=item["id"])
    store.add_event(event_type="test", message="second", queue_id=item["id"])
    store.add_event(event_type="test", message="third", queue_id=item["id"])

    assert store.list_events(item["id"], limit=-1) == []
    assert [
        event["message"] for event in store.list_events(item["id"], limit=10_000)
    ] == ["third", "second"]


def test_store_replaces_candidates_with_stable_ids(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="needs_release",
    )
    candidate = {"public_indexer_result_id": "release-one", "title": "Release One"}

    store.replace_candidates(item["id"], [candidate], media_type="movie")
    first = store.get_queue_item(item["id"])["candidates"][0]
    store.replace_candidates(item["id"], [candidate], media_type="movie")
    second = store.get_queue_item(item["id"])["candidates"][0]
    store.replace_candidates(item["id"], [], media_type="movie")

    assert first["id"] == second["id"]
    assert store.get_queue_item(item["id"])["candidates"] == []


def test_store_accepts_new_queue_statuses_and_event_summaries(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_unverified",
    )
    store.add_event(
        event_type="queue.download_unverified",
        message="MediaManager timed out",
        queue_id=item["id"],
    )
    imported = store.update_queue_item(
        item["id"],
        status="imported",
        resolved=True,
        from_status="download_unverified",
    )
    store.add_event(event_type="queue.imported", message="Imported", queue_id=item["id"])
    available = store.update_queue_item(
        item["id"],
        status="available",
        resolved=True,
        from_status="imported",
    )

    assert imported is not None
    assert available is not None
    stored = store.get_queue_item(item["id"])
    assert stored["status"] == "available"
    assert stored["last_event"]["event_type"] == "queue.imported"
    assert stored["last_error"]["event_type"] == "queue.download_unverified"
    assert store.list_events(item["id"])[0]["event_type"] == "queue.imported"


def test_store_chunks_event_summaries_for_large_queue_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    original_event_summaries = store._event_summaries
    chunk_sizes: list[int] = []
    queue_ids: list[str] = []
    event_count = 1_105
    monkeypatch.setattr(store_module, "MAX_BRIDGE_EVENTS", event_count)

    def capped_event_summaries(
        conn: sqlite3.Connection,
        ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        chunk_sizes.append(len(ids))
        if len(ids) > 999:
            raise AssertionError("event summary query exceeded SQLite parameter cap")
        return original_event_summaries(conn, ids)

    for external_id in range(event_count):
        item = store.upsert_queue_item(
            media_type="movie",
            external_id=10_000 + external_id,
            title=f"Movie {external_id}",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_failed",
        )
        queue_ids.append(item["id"])
        store.add_event(
            event_type="queue.download_failed",
            message="download failed",
            queue_id=item["id"],
        )
    monkeypatch.setattr(store, "_event_summaries", capped_event_summaries)

    items = store.list_queue_items()

    assert len(items) == event_count
    assert len(chunk_sizes) > 1
    assert max(chunk_sizes) <= 999
    summaries_by_id = {
        item["id"]: (
            item["last_event"]["event_type"],
            item["last_error"]["event_type"],
        )
        for item in items
    }
    assert summaries_by_id[queue_ids[0]] == (
        "queue.download_failed",
        "queue.download_failed",
    )
    assert summaries_by_id[queue_ids[-1]] == (
        "queue.download_failed",
        "queue.download_failed",
    )


def test_queue_status_check_parser_requires_exact_status_set() -> None:
    status_values = ", ".join(f"'{status}'" for status in store_module.QUEUE_STATUSES)
    matching = (
        "CREATE TABLE queue_items ("
        "status TEXT NOT NULL CHECK (status IN ("
        f"{status_values}"
        ")))"
    )
    stale_with_unrelated_status_text = (
        "CREATE TABLE queue_items ("
        "status TEXT NOT NULL CHECK (status IN ('download_submitted')),"
        "note TEXT DEFAULT 'download_unverified imported available'"
        ")"
    )
    extra = (
        "CREATE TABLE queue_items ("
        "status TEXT NOT NULL CHECK (status IN ("
        f"{status_values}, 'obsolete'"
        ")))"
    )

    assert store_module.queue_status_check_matches(matching)
    assert not store_module.queue_status_check_matches(stale_with_unrelated_status_text)
    assert not store_module.queue_status_check_matches(extra)


def test_store_migrates_old_queue_status_check_without_losing_children(tmp_path: Path) -> None:
    db_path = tmp_path / "screenarr.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE queue_items (
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
                        'download_failed'
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
            CREATE TABLE release_candidates (
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
            CREATE TABLE bridge_events (
                id TEXT PRIMARY KEY,
                queue_id TEXT REFERENCES queue_items(id) ON DELETE SET NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            INSERT INTO queue_items (
                id, media_type, external_id, title, profile_id, profile_name, mode,
                status, mediamanager_id, created_at, updated_at
            )
            VALUES (
                'queue-old', 'movie', 550, 'Fight Club', 101, 'Manual', 'manual',
                'download_submitted', 'movie-uuid', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO release_candidates (
                id, queue_id, media_type, result_id, title, raw_json, created_at
            )
            VALUES (
                'candidate-old', 'queue-old', 'movie', 'release-one', 'Release One',
                '{}', '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO bridge_events (
                id, queue_id, event_type, message, payload_json, created_at
            )
            VALUES (
                'event-old', 'queue-old', 'queue.download_submitted', 'submitted',
                '{}', '2026-01-01T00:00:00+00:00'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    store = BridgeStore(db_path)
    store.init()
    updated = store.update_queue_item(
        "queue-old",
        status="download_unverified",
        from_status="download_submitted",
    )

    assert updated is not None
    assert updated["status"] == "download_unverified"
    assert updated["candidates"][0]["result_id"] == "release-one"
    assert store.list_events("queue-old")[0]["event_type"] == "queue.download_submitted"


def test_store_can_preserve_candidates_for_empty_refresh_batches(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="needs_release",
    )
    store.replace_candidates(
        item["id"],
        [{"public_indexer_result_id": "release-one", "title": "Release One"}],
        media_type="movie",
    )

    store.replace_candidate_batches(
        item["id"],
        [([], "movie", 0)],
        clear_empty_batches=False,
    )
    preserved = store.get_queue_item(item["id"])["candidates"]

    assert [candidate["result_id"] for candidate in preserved] == ["release-one"]


def test_store_metadata_update_preserves_current_status(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="needs_release",
    )
    claimed = store.transition_queue_item(
        item["id"],
        from_status="needs_release",
        to_status="download_claimed",
    )

    updated = store.update_queue_item(item["id"], mediamanager_id="movie-uuid")

    assert claimed is not None
    assert updated is not None
    assert updated["status"] == "download_claimed"
    assert updated["mediamanager_id"] == "movie-uuid"


def test_store_duplicate_enqueue_preserves_pending_approval(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Approval",
        mode="approval",
        status="pending_approval",
    )
    original_updated_at = "2026-01-01T00:00:00+00:00"
    conn = sqlite3.connect(tmp_path / "screenarr.db")
    try:
        conn.execute(
            "UPDATE queue_items SET updated_at = ? WHERE id = ?",
            (original_updated_at, item["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    duplicate = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="needs_release",
        mediamanager_id="movie-uuid",
    )

    assert duplicate["status"] == "pending_approval"
    assert duplicate["mode"] == "approval"
    assert duplicate["updated_at"] == original_updated_at


def store_with_recovered_claims(
    path: Path,
) -> tuple[BridgeStore, dict[str, Any], dict[str, Any], int]:
    store = BridgeStore(path / "screenarr.db")
    store.init()
    approval_item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Approval",
        mode="approval",
        status="pending_approval",
    )
    manual_item = store.upsert_queue_item(
        media_type="movie",
        external_id=551,
        title="Another Movie",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="needs_release",
    )

    store.transition_queue_item(
        approval_item["id"],
        from_status="pending_approval",
        to_status="approval_claimed",
    )
    store.transition_queue_item(
        manual_item["id"],
        from_status="needs_release",
        to_status="download_claimed",
    )
    recovered = store.recover_stale_claims(older_than_seconds=-1)
    return store, approval_item, manual_item, recovered


def test_store_recovers_stale_claims(tmp_path: Path) -> None:
    store, approval_item, manual_item, recovered = store_with_recovered_claims(tmp_path)
    assert recovered == 2
    assert store.get_queue_item(approval_item["id"])["status"] == "pending_approval"
    assert store.get_queue_item(manual_item["id"])["status"] == "needs_release"


def test_store_guarded_updates_do_not_overwrite_recovered_claims(tmp_path: Path) -> None:
    store, approval_item, manual_item, _recovered = store_with_recovered_claims(tmp_path)

    stale_approval = store.update_queue_item(
        approval_item["id"],
        status="needs_release",
        from_status="approval_claimed",
    )
    stale_download = store.update_queue_item(
        manual_item["id"],
        status="download_submitted",
        resolved=True,
        from_status="download_claimed",
    )

    assert stale_approval is None
    assert stale_download is None
    assert store.get_queue_item(approval_item["id"])["status"] == "pending_approval"
    assert store.get_queue_item(manual_item["id"])["status"] == "needs_release"


def test_recovered_claims_can_be_completed_after_external_success(tmp_path: Path) -> None:
    store, approval_item, manual_item, _recovered = store_with_recovered_claims(tmp_path)

    recovered_approval = store.update_queue_item(
        approval_item["id"],
        status="needs_release",
        mediamanager_id="movie-uuid",
        from_status="pending_approval",
    )
    recovered_download = store.update_queue_item(
        manual_item["id"],
        status="download_submitted",
        resolved=True,
        from_status="needs_release",
    )

    assert recovered_approval is not None
    assert recovered_approval["status"] == "needs_release"
    assert recovered_approval["mediamanager_id"] == "movie-uuid"
    assert recovered_download is not None
    assert recovered_download["status"] == "download_submitted"


def test_candidate_replacement_validates_parent_and_media_type(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="needs_release",
    )
    old_candidate = {"public_indexer_result_id": "old-release", "title": "Old Release"}
    store.replace_candidates(item["id"], [old_candidate], media_type="movie")

    with pytest.raises(KeyError):
        store.replace_candidates("missing-queue", [], media_type="movie")
    with pytest.raises(ValueError, match="media_type"):
        store.replace_candidates(
            item["id"],
            [{"public_indexer_result_id": "wrong-type", "title": "Wrong Type"}],
            media_type="show",
        )
    with pytest.raises(ValueError, match="missing an id"):
        store.replace_candidates(
            item["id"],
            [{"public_indexer_result_id": "   ", "title": "Blank ID"}],
            media_type="movie",
        )
    with pytest.raises(ValueError, match="duplicate candidate batch"):
        store.replace_candidate_batches(
            item["id"],
            [
                ([{"public_indexer_result_id": "new-one", "title": "New One"}], "movie", 0),
                ([{"public_indexer_result_id": "new-two", "title": "New Two"}], "movie", 0),
            ],
        )

    candidates = store.get_queue_item(item["id"])["candidates"]
    assert [candidate["result_id"] for candidate in candidates] == ["old-release"]


@pytest.mark.anyio
async def test_show_candidate_refresh_is_all_or_nothing(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="show",
        external_id=550,
        title="A Show",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="needs_release",
        mediamanager_id="show-uuid",
        seasons=[1, 2],
    )
    store.replace_candidates(
        item["id"],
        [{"public_indexer_result_id": "old-season-one", "title": "Old Season One"}],
        media_type="show",
        season_number=1,
    )

    with pytest.raises(MediaManagerError, match="missing an id"):
        await refresh_candidates_for_item(
            store,
            InvalidSecondSeasonMediaManager(),
            store.get_queue_item(item["id"]),
            BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL),
        )

    candidates = store.get_queue_item(item["id"])["candidates"]
    assert [candidate["result_id"] for candidate in candidates] == ["old-season-one"]


def test_manual_mode_stores_release_candidates(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        query_auth = client.get("/api/bridge/v1/queue", params={"apikey": "test"})
        assert query_auth.status_code == 401
        response, item = post_media_and_first_queue_item(client)
        assert response.status_code == 200

    assert item["status"] == "needs_release"
    assert item["mediamanager_id"] == "movie-uuid"
    assert item["candidates"][0]["result_id"] == "release-one"


def test_non_default_libraries_are_sent_to_mediamanager(tmp_path: Path) -> None:
    movie_profile = BridgeProfile(
        id=101,
        name="Manual 4K",
        mode=ReleaseMode.MANUAL,
        mediamanager_library="4K",
    )
    movie_client, movie_fake = make_flow_client(tmp_path / "movie.db", movie_profile)

    with movie_client:
        movie_response, _movie_item = post_media_and_first_queue_item(movie_client)

    show_profile = BridgeProfile(
        id=101,
        name="Manual TV 4K",
        mode=ReleaseMode.MANUAL,
        mediamanager_library="4K",
    )
    show_client, show_fake = make_flow_client(tmp_path / "show.db", show_profile)

    with show_client:
        show_response, _show_item = post_media_and_first_queue_item(
            show_client,
            endpoint="/api/v3/series",
            payload=show_payload(),
        )

    assert movie_response.status_code == 200
    assert show_response.status_code == 200
    assert movie_fake.movie_library_sets == [("movie-uuid", "4K")]
    assert show_fake.show_library_sets == [("show-uuid", "4K")]


def test_approval_mode_waits_for_approval_then_downloads_selected_release(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Approval", mode=ReleaseMode.APPROVAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        add_response, item = post_media_and_first_queue_item(client)
        assert add_response.status_code == 200
        assert fake.added_movies == []
        assert item["status"] == "pending_approval"

        approved = client.post(
            f"/api/bridge/v1/queue/{item['id']}/approve",
            headers={"X-Api-Key": "test"},
        ).json()
        refreshed = client.post(
            f"/api/bridge/v1/queue/{item['id']}/refresh-candidates",
            headers={"X-Api-Key": "test"},
        ).json()
        assert [candidate["result_id"] for candidate in refreshed["candidates"]] == [
            "release-one"
        ]
        assert approved["candidates"][0]["result_id"] == "release-one"
        assert approved["candidates"][0]["id"] == refreshed["candidates"][0]["id"]
        candidate_id = refreshed["candidates"][0]["id"]

        downloaded = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        duplicate_download = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        refresh_after_download = client.post(
            f"/api/bridge/v1/queue/{item['id']}/refresh-candidates",
            headers={"X-Api-Key": "test"},
        )
        deny_after_approval = client.post(
            f"/api/bridge/v1/queue/{item['id']}/deny",
            headers={"X-Api-Key": "test"},
        )

    assert fake.added_movies == [550]
    assert fake.downloaded_movies == ["release-one"]
    assert downloaded["status"] == "download_submitted"
    assert duplicate_download.status_code == 409
    assert refresh_after_download.status_code == 409
    assert deny_after_approval.status_code == 409


def test_approval_duplicate_after_approval_is_idempotent(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Approval", mode=ReleaseMode.APPROVAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        add_response, item = post_media_and_first_queue_item(client)
        approved = client.post(
            f"/api/bridge/v1/queue/{item['id']}/approve",
            headers={"X-Api-Key": "test"},
        ).json()
        duplicate = client.post(
            "/api/v3/movie",
            json=movie_payload(),
            headers={"X-Api-Key": "test"},
        )
        queue = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()

    assert add_response.status_code == 200
    assert duplicate.status_code == 200
    assert approved["status"] == "needs_release"
    assert queue["items"][0]["status"] == "needs_release"
    assert fake.added_movies == [550]


def test_manual_duplicate_after_download_is_idempotent(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]
        downloaded = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        duplicate = client.post(
            "/api/v3/movie",
            json=movie_payload(),
            headers={"X-Api-Key": "test"},
        )
        queue = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()

    assert duplicate.status_code == 200
    assert downloaded["status"] == "download_submitted"
    assert queue["items"][0]["status"] == "download_submitted"
    assert fake.added_movies == [550]
    assert fake.downloaded_movies == ["release-one"]


def test_show_approval_duplicate_after_approval_is_idempotent(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Approval", mode=ReleaseMode.APPROVAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(
            client,
            endpoint="/api/v3/series",
            payload=show_payload(),
        )
        approved = client.post(
            f"/api/bridge/v1/queue/{item['id']}/approve",
            headers={"X-Api-Key": "test"},
        ).json()
        duplicate = client.post(
            "/api/v3/series",
            json=show_payload(),
            headers={"X-Api-Key": "test"},
        )
        queue = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()

    assert duplicate.status_code == 200
    assert approved["status"] == "needs_release"
    assert queue["items"][0]["status"] == "needs_release"
    assert fake.added_shows == [12345]


def test_multi_season_show_download_resolves_after_last_season(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(
            client,
            endpoint="/api/v3/series",
            payload=show_payload(seasons=[1, 2]),
        )
        first_candidate = next(
            candidate for candidate in item["candidates"] if candidate["season_number"] == 1
        )
        first_download = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{first_candidate['id']}",
            headers={"X-Api-Key": "test"},
        ).json()
        second_candidate = first_download["candidates"][0]
        second_download = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{second_candidate['id']}",
            headers={"X-Api-Key": "test"},
        ).json()

    assert first_download["status"] == "needs_release"
    assert [candidate["season_number"] for candidate in first_download["candidates"]] == [2]
    assert second_download["status"] == "download_submitted"
    assert second_download["candidates"] == []
    assert fake.downloaded_shows == ["show-release-s1", "show-release-s2"]


def test_download_failure_keeps_queue_item_retryable(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]

        fake.fail_next_download = True
        failed = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        retryable = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()[
            "items"
        ][0]
        retried = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()

    assert failed.status_code == 502
    assert retryable["status"] == "download_failed"
    assert retried["status"] == "download_submitted"
    assert fake.downloaded_movies == ["release-one"]


def test_download_timeout_marks_queue_item_unverified(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]

        fake.fail_next_download_timeout = True
        timed_out = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        unverified = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()[
            "items"
        ][0]
        duplicate_submit = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )

    assert timed_out.status_code == 200
    assert timed_out.json()["status"] == "download_unverified"
    assert timed_out.json()["resolved_at"] is None
    assert unverified["last_error"]["event_type"] == "queue.download_unverified"
    assert duplicate_submit.status_code == 409
    assert fake.downloaded_movies == []


def test_download_transport_failure_marks_queue_item_unverified(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]
        fake.fail_next_download_transport = True
        failed = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        retryable = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()[
            "items"
        ][0]

    assert failed.status_code == 200
    assert failed.json()["status"] == "download_unverified"
    assert retryable["status"] == "download_unverified"
    assert retryable["last_error"]["event_type"] == "queue.download_unverified"
    assert (
        retryable["last_error"]["message"]
        == "MediaManager download submit outcome unknown: ConnectError"
    )
    assert fake.downloaded_movies == []


def test_auto_movie_download_transport_failure_records_unverified_queue(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Auto", mode=ReleaseMode.AUTO)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        fake.fail_next_download_transport = True
        accepted = client.post(
            "/api/v3/movie",
            json=movie_payload(),
            headers={"X-Api-Key": "test"},
        )
        queue_item = client.get(
            "/api/bridge/v1/queue",
            headers={"X-Api-Key": "test"},
        ).json()["items"][0]
        fake.movie_detail = {"id": "movie-uuid", "downloaded": True}
        reconciled = client.post(
            f"/api/bridge/v1/queue/{queue_item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()

    assert accepted.status_code == 200
    assert fake.downloaded_movies == []
    assert queue_item["status"] == "download_unverified"
    assert queue_item["mediamanager_id"] == "movie-uuid"
    assert queue_item["candidates"][0]["result_id"] == "release-one"
    assert queue_item["last_error"]["event_type"] == "queue.download_unverified"
    assert (
        queue_item["last_error"]["message"]
        == "MediaManager download submit outcome unknown: ConnectError"
    )
    assert reconciled["status"] == "imported"


def test_auto_show_download_transport_failure_records_unverified_queue(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Auto", mode=ReleaseMode.AUTO)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        fake.fail_next_download_transport = True
        accepted = client.post(
            "/api/v3/series",
            json=show_payload(),
            headers={"X-Api-Key": "test"},
        )
        queue_item = client.get(
            "/api/bridge/v1/queue",
            headers={"X-Api-Key": "test"},
        ).json()["items"][0]
        fake.show_detail = {
            "id": "show-uuid",
            "seasons": [{"season_number": 1, "downloaded": True}],
        }
        reconciled = client.post(
            f"/api/bridge/v1/queue/{queue_item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()

    assert accepted.status_code == 200
    assert fake.downloaded_shows == []
    assert queue_item["status"] == "download_unverified"
    assert queue_item["mediamanager_id"] == "show-uuid"
    assert queue_item["candidates"][0]["result_id"] == "show-release-s1"
    assert queue_item["last_error"]["event_type"] == "queue.download_unverified"
    assert (
        queue_item["last_error"]["message"]
        == "MediaManager download submit outcome unknown: ConnectError"
    )
    assert reconciled["status"] == "imported"


@pytest.mark.anyio
async def test_auto_download_unverified_does_not_overwrite_raced_queue_status(
    tmp_path: Path,
) -> None:
    class RacingStore(BridgeStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.raced = False

        def update_queue_item(self, queue_id: str, **kwargs: Any) -> dict[str, Any] | None:
            if not self.raced and kwargs.get("status") == "download_unverified":
                self.raced = True
                super().update_queue_item(queue_id, status="available", resolved=True)
            return super().update_queue_item(queue_id, **kwargs)

    store = RacingStore(tmp_path / "screenarr.db")
    store.init()
    profile = BridgeProfile(id=101, name="Auto", mode=ReleaseMode.AUTO)
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Auto",
        mode="auto",
        status="download_failed",
        mediamanager_id="movie-uuid",
    )

    await record_auto_download_unverified(
        store,
        AutoDownloadAmbiguity(
            media_type="movie",
            external_id=550,
            title="Fight Club",
            profile=profile,
            mediamanager_id="movie-uuid",
            payload={},
            choice=ReleaseChoice(id="release-one", title="Release One"),
            message="timed out",
        ),
    )
    stored = store.get_queue_item(item["id"])

    assert stored["status"] == "available"
    assert stored["candidates"] == []
    assert store.list_events(item["id"]) == []


def test_refresh_candidates_transport_failure_records_queue_event(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)

        async def fail_search(_movie_uuid: str) -> list[dict[str, Any]]:
            raise httpx.ConnectError("connect failed")

        fake.search_movie_torrents = fail_search
        failed = client.post(
            f"/api/bridge/v1/queue/{item['id']}/refresh-candidates",
            headers={"X-Api-Key": "test"},
        )
        stored = client.app.state.store.get_queue_item(item["id"])

    assert failed.status_code == 502
    assert failed.json()["detail"] == "MediaManager candidate refresh request failed"
    assert stored["last_error"]["event_type"] == "queue.candidate_refresh_failed"
    assert (
        stored["last_error"]["message"]
        == "MediaManager candidate refresh request failed: ConnectError"
    )


def test_download_submit_returns_terminal_state_after_reconcile_race(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]

        async def mark_imported_during_submit(
            _movie_uuid: str,
            _result_id: str,
        ) -> dict[str, str]:
            updated = client.app.state.store.update_queue_item(
                item["id"],
                status="imported",
                resolved=True,
                from_status="download_submitted",
            )
            assert updated is not None
            return {"id": "torrent-uuid"}

        fake.download_movie_torrent = mark_imported_during_submit
        submitted = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        stored = client.app.state.store.get_queue_item(item["id"])

    assert submitted.status_code == 200
    assert submitted.json()["status"] == "imported"
    assert stored["status"] == "imported"
    assert stored["resolved_at"] is not None


@pytest.mark.anyio
async def test_late_download_failure_does_not_record_stale_error(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=1,
        title="Imported Movie",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_submitted",
    )
    imported = store.update_queue_item(
        item["id"],
        status="imported",
        resolved=True,
        from_status="download_submitted",
    )
    assert imported is not None

    await mark_download_failed(
        store,
        item["id"],
        "candidate-id",
        {"result_id": "release-one"},
        message="late MediaManager failure",
    )

    stored = store.get_queue_item(item["id"])
    assert stored["status"] == "imported"
    assert stored["last_error"] is None


def test_reconcile_transitions_downloads_to_imported(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]
        submitted = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        not_imported = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        fake.movie_detail = {"id": "movie-uuid", "downloaded": True}
        imported = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        stored = client.app.state.store.get_queue_item(item["id"])
        events = client.get(
            f"/api/bridge/v1/queue/{item['id']}/events",
            headers={"X-Api-Key": "test"},
        ).json()["events"]

    assert submitted["status"] == "download_submitted"
    assert submitted["resolved_at"] is None
    assert not_imported["status"] == "download_submitted"
    assert imported["status"] == "imported"
    assert imported["resolved_at"] is not None
    assert stored["status"] == "imported"
    assert events[0]["event_type"] == "queue.imported"


def test_bulk_reconcile_transitions_unverified_show_to_imported(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(
            client,
            endpoint="/api/v3/series",
            payload=show_payload(),
        )
        candidate_id = item["candidates"][0]["id"]
        fake.fail_next_download_timeout = True
        timed_out = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        fake.show_detail = {
            "id": "show-uuid",
            "seasons": [{"season_number": 1, "downloaded": True}],
        }
        reconciled = client.post(
            "/api/bridge/v1/queue/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()["items"]
        stored = client.app.state.store.get_queue_item(item["id"])

    assert timed_out["status"] == "download_unverified"
    assert reconciled[0]["status"] == "imported"
    assert stored["status"] == "imported"
    assert stored["resolved_at"] is not None


def test_show_reconcile_requires_requested_season_completion(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(
            client,
            endpoint="/api/v3/series",
            payload=show_payload(),
        )
        candidate_id = item["candidates"][0]["id"]
        submitted = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        fake.show_detail = {
            "id": "show-uuid",
            "downloaded": True,
            "seasons": [
                {"season_number": 1, "downloading": True},
                {"season_number": 2, "downloaded": True},
            ],
        }
        reconciled = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        stored = client.app.state.store.get_queue_item(item["id"])

    assert submitted["status"] == "download_submitted"
    assert reconciled["status"] == "download_submitted"
    assert reconciled["resolved_at"] is None
    assert stored["status"] == "download_submitted"
    assert stored["resolved_at"] is None


def test_mediamanager_reconcile_summary_ignores_malformed_detail_payload() -> None:
    summary = mediamanager_reconcile_summary(
        {"id": "movie-uuid", "downloaded": True, "nested": {"ignored": True}}
    )

    assert mediamanager_reconcile_summary(["not", "a", "mapping"]) == {}
    assert summary == {"id": "movie-uuid", "downloaded": True}


def test_bulk_reconcile_continues_after_item_failure(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, _fake = make_flow_client(
        tmp_path / "screenarr.db",
        profile,
        mediamanager=SelectiveDetailMediaManager(),
    )

    with client:
        failed_item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=550,
            title="Failure",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-down",
        )
        transport_failed_item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=552,
            title="Transport Failure",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-transport-down",
        )
        imported_item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=551,
            title="Success",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-ok",
        )
        response = client.post(
            "/api/bridge/v1/queue/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        failed = client.app.state.store.get_queue_item(failed_item["id"])
        transport_failed = client.app.state.store.get_queue_item(transport_failed_item["id"])
        imported = client.app.state.store.get_queue_item(imported_item["id"])

    assert [item["id"] for item in response["items"]] == [imported_item["id"]]
    failures = {failure["queue_id"]: failure for failure in response["failures"]}
    assert failures == {
        failed_item["id"]: {
            "queue_id": failed_item["id"],
            "status_code": 502,
            "detail": "MediaManager detail request failed",
        },
        transport_failed_item["id"]: {
            "queue_id": transport_failed_item["id"],
            "status_code": 502,
            "detail": "MediaManager detail request failed",
        },
    }
    assert failed["status"] == "download_submitted"
    assert failed["last_error"]["event_type"] == "queue.reconcile_failed"
    assert "MediaManager detail unavailable" not in failed["last_error"]["message"]
    assert transport_failed["status"] == "download_submitted"
    assert transport_failed["last_error"]["event_type"] == "queue.reconcile_failed"
    assert imported["status"] == "imported"


def test_bulk_reconcile_times_out_slow_items_without_blocking_successes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "MAX_BULK_RECONCILE_ITEM_SECONDS", 0.2)
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, _fake = make_flow_client(
        tmp_path / "screenarr.db",
        profile,
        mediamanager=SlowDetailMediaManager(),
    )

    with client:
        slow_item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=553,
            title="Slow",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-slow",
        )
        imported_item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=554,
            title="Success",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-ok",
        )
        response = client.post(
            "/api/bridge/v1/queue/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        slow = client.app.state.store.get_queue_item(slow_item["id"])
        imported = client.app.state.store.get_queue_item(imported_item["id"])

    assert {item["id"] for item in response["items"]} == {imported_item["id"]}
    failures = {failure["queue_id"]: failure for failure in response["failures"]}
    assert failures == {
        slow_item["id"]: {
            "queue_id": slow_item["id"],
            "status_code": 504,
            "detail": "MediaManager detail request timed out",
        }
    }
    assert slow["status"] == "download_submitted"
    assert slow["last_error"]["event_type"] == "queue.reconcile_failed"
    assert slow["last_error"]["message"] == "MediaManager detail request timed out"
    assert imported["status"] == "imported"


def test_bulk_reconcile_preserves_successes_when_task_escapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, _fake = make_flow_client(
        tmp_path / "screenarr.db",
        profile,
        mediamanager=SelectiveDetailMediaManager(),
    )

    with client:
        failed_item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=556,
            title="Escaped Failure",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-fail",
        )
        imported_item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=557,
            title="Success",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-ok",
        )
        original_reconcile = main_module.reconcile_bulk_queue_item

        async def fail_one(
            request: Any,
            item: dict[str, Any],
            limiter: Any,
        ) -> tuple[bool, dict[str, Any]]:
            if item["id"] == failed_item["id"]:
                raise RuntimeError("boom")
            return await original_reconcile(request, item, limiter)

        monkeypatch.setattr(main_module, "reconcile_bulk_queue_item", fail_one)
        response = client.post(
            "/api/bridge/v1/queue/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        imported = client.app.state.store.get_queue_item(imported_item["id"])

    assert {item["id"] for item in response["items"]} == {imported_item["id"]}
    failures = {failure["queue_id"]: failure for failure in response["failures"]}
    assert failures == {
        failed_item["id"]: {
            "queue_id": failed_item["id"],
            "status_code": 500,
            "detail": "unexpected reconcile failure",
        }
    }
    assert imported["status"] == "imported"


def test_single_reconcile_times_out_slow_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "MAX_BULK_RECONCILE_ITEM_SECONDS", 0.2)
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, _fake = make_flow_client(
        tmp_path / "screenarr.db",
        profile,
        mediamanager=SlowDetailMediaManager(),
    )

    with client:
        slow_item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=555,
            title="Slow Single",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-slow",
        )
        response = client.post(
            f"/api/bridge/v1/queue/{slow_item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        )
        slow = client.app.state.store.get_queue_item(slow_item["id"])

    assert response.status_code == 504
    assert response.json()["detail"] == "MediaManager detail request timed out"
    assert slow["status"] == "download_submitted"
    assert slow["last_error"]["event_type"] == "queue.reconcile_failed"
    assert slow["last_error"]["message"] == "MediaManager detail request timed out"


def test_bulk_reconcile_processes_bounded_number_of_items(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    db_path = tmp_path / "screenarr.db"
    client, _fake = make_flow_client(
        db_path,
        profile,
        mediamanager=SelectiveDetailMediaManager(),
    )

    with client:
        items = [
            client.app.state.store.upsert_queue_item(
                media_type="movie",
                external_id=600 + index,
                title=f"Movie {index}",
                profile_id=101,
                profile_name="Manual",
                mode="manual",
                status="download_submitted",
                mediamanager_id=f"movie-ok-{index}",
            )
            for index in range(MAX_BULK_RECONCILE_ITEMS + 1)
        ]
        response = client.post(
            "/api/bridge/v1/queue/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        processed = [
            client.app.state.store.get_queue_item(item["id"])["status"] for item in items
        ]

    assert len(response["items"]) == MAX_BULK_RECONCILE_ITEMS
    assert response["failures"] == []
    assert processed.count("imported") == MAX_BULK_RECONCILE_ITEMS
    assert processed.count("download_submitted") == 1


def test_bulk_reconcile_prioritizes_items_without_recent_failure(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    db_path = tmp_path / "screenarr.db"
    client, _fake = make_flow_client(
        db_path,
        profile,
        mediamanager=SelectiveDetailMediaManager(),
    )

    with client:
        success = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=650,
            title="Success",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-ok",
        )
        failures = [
            client.app.state.store.upsert_queue_item(
                media_type="movie",
                external_id=651 + index,
                title=f"Failure {index}",
                profile_id=101,
                profile_name="Manual",
                mode="manual",
                status="download_submitted",
                mediamanager_id="movie-down",
            )
            for index in range(MAX_BULK_RECONCILE_ITEMS)
        ]
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE queue_items SET updated_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00+00:00", success["id"]),
            )
            for index, item in enumerate(failures):
                conn.execute(
                    "UPDATE queue_items SET updated_at = ? WHERE id = ?",
                    (f"2026-01-02T00:{index:02d}:00+00:00", item["id"]),
                )
            conn.commit()
        finally:
            conn.close()

        first = client.post(
            "/api/bridge/v1/queue/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        second = client.post(
            "/api/bridge/v1/queue/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        stored = client.app.state.store.get_queue_item(success["id"])

    assert first["items"] == []
    assert len(first["failures"]) == MAX_BULK_RECONCILE_ITEMS
    assert [item["id"] for item in second["items"]] == [success["id"]]
    assert stored["status"] == "imported"


@pytest.mark.anyio
async def test_background_reconcile_processes_bounded_number_of_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    items = [
        store.upsert_queue_item(
            media_type="movie",
            external_id=700 + index,
            title=f"Movie {index}",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id=f"movie-ok-{index}",
        )
        for index in range(MAX_BULK_RECONCILE_ITEMS + 1)
    ]
    fake = SelectiveDetailMediaManager()

    stop_background_reconcile_after_iterations(monkeypatch)

    with pytest.raises(StopReconcileLoop):
        await mediamanager_reconcile_loop(store, fake, interval_seconds=1)

    processed = [store.get_queue_item(item["id"])["status"] for item in items]
    assert processed.count("imported") == MAX_BULK_RECONCILE_ITEMS
    assert processed.count("download_submitted") == 1


@pytest.mark.anyio
async def test_background_reconcile_retries_previous_reconcile_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=750,
        title="Retry Me",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_unverified",
        mediamanager_id="movie-ok",
    )
    store.add_event(
        event_type="queue.reconcile_failed",
        message="MediaManager detail request failed: ConnectError",
        queue_id=item["id"],
    )
    fake = SelectiveDetailMediaManager()

    stop_background_reconcile_after_iterations(monkeypatch)

    with pytest.raises(StopReconcileLoop):
        await mediamanager_reconcile_loop(store, fake, interval_seconds=1)

    stored = store.get_queue_item(item["id"])
    assert stored["status"] == "imported"
    assert stored["last_event"]["event_type"] == "queue.imported"


def test_reconcile_missing_mediamanager_id_records_one_failure_event(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=settings_for(tmp_path / "screenarr.db"),
        config=BridgeConfig(),
        mediamanager_factory=lambda _settings: SelectiveDetailMediaManager(),
    )

    with TestClient(app) as client:
        item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=553,
            title="Missing ID",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
        )
        response = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        )
        events = client.app.state.store.list_events(item["id"])

    assert response.status_code == 502
    assert response.json()["detail"] == "MediaManager detail request failed"
    assert [(event["event_type"], event["message"]) for event in events] == [
        ("queue.reconcile_failed", "queue item has no MediaManager id")
    ]


@pytest.mark.anyio
async def test_background_reconcile_persists_failed_item_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Failure",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_submitted",
        mediamanager_id="movie-down",
    )
    transport_item = store.upsert_queue_item(
        media_type="movie",
        external_id=552,
        title="Transport Failure",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_submitted",
        mediamanager_id="movie-transport-down",
    )
    missing_id_item = store.upsert_queue_item(
        media_type="movie",
        external_id=553,
        title="Missing ID",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_submitted",
    )
    fake = SelectiveDetailMediaManager()

    stop_background_reconcile_after_iterations(monkeypatch)

    with pytest.raises(StopReconcileLoop):
        await mediamanager_reconcile_loop(store, fake, interval_seconds=1)

    stored = store.get_queue_item(item["id"])
    transport_stored = store.get_queue_item(transport_item["id"])
    item_events = store.list_events(item["id"])
    transport_events = store.list_events(transport_item["id"])
    missing_id_events = store.list_events(missing_id_item["id"])
    assert stored["status"] == "download_submitted"
    assert stored["last_error"]["event_type"] == "queue.reconcile_failed"
    assert stored["last_error"]["message"] == (
        "MediaManager detail request failed: MediaManagerError"
    )
    assert transport_stored["status"] == "download_submitted"
    assert transport_stored["last_error"]["event_type"] == "queue.reconcile_failed"
    assert transport_stored["last_error"]["message"] == (
        "MediaManager detail request failed: ConnectError"
    )
    assert len(item_events) == 1
    assert len(transport_events) == 1
    assert [(event["event_type"], event["message"]) for event in missing_id_events] == [
        ("queue.reconcile_failed", "queue item has no MediaManager id")
    ]


@pytest.mark.anyio
async def test_background_reconcile_continues_when_failure_event_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Failure",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_submitted",
        mediamanager_id="movie-down",
    )
    next_item = store.upsert_queue_item(
        media_type="movie",
        external_id=551,
        title="Next",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_submitted",
        mediamanager_id="movie-ok",
    )
    original_add_event = store.add_event
    failed_writes = 0

    def fail_first_reconcile_failure_event(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal failed_writes
        if kwargs.get("event_type") == "queue.reconcile_failed" and failed_writes == 0:
            failed_writes += 1
            raise sqlite3.OperationalError("database is locked")
        return original_add_event(*args, **kwargs)

    monkeypatch.setattr(store, "add_event", fail_first_reconcile_failure_event)
    fake = SelectiveDetailMediaManager()

    stop_background_reconcile_after_iterations(monkeypatch)

    with pytest.raises(StopReconcileLoop):
        await mediamanager_reconcile_loop(store, fake, interval_seconds=1)

    stored_next_item = store.get_queue_item(next_item["id"])
    assert failed_writes == 1
    assert stored_next_item["status"] == "imported"
    assert stored_next_item["last_event"]["event_type"] == "queue.imported"


@pytest.mark.anyio
async def test_background_reconcile_does_not_churn_noop_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Waiting",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_submitted",
        mediamanager_id="movie-uuid",
    )
    fake = FakeMediaManager()
    # An in-progress grab keeps the noop reconcile from churning events.
    fake.movie_detail = {"id": "movie-uuid", "downloaded": False, "downloading": True}

    stop_background_reconcile_after_iterations(monkeypatch)

    with pytest.raises(StopReconcileLoop):
        await mediamanager_reconcile_loop(store, fake, interval_seconds=1)

    stored = store.get_queue_item(item["id"])
    assert stored["status"] == "download_submitted"
    assert stored["last_event"] is None
    assert stored["last_error"] is None


def test_unexpected_download_failure_keeps_queue_item_retryable(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]

        fake.fail_next_download_unexpectedly = True
        failed = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        retryable = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()[
            "items"
        ][0]

    assert failed.status_code == 502
    assert retryable["status"] == "download_failed"
    assert fake.downloaded_movies == []


def test_approval_queue_can_deny_without_calling_mediamanager(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Approval", mode=ReleaseMode.APPROVAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        denied = client.post(
            f"/api/bridge/v1/queue/{item['id']}/deny",
            headers={"X-Api-Key": "test"},
        ).json()

    assert fake.added_movies == []
    assert denied["status"] == "denied"


def test_dashboard_session_rejects_malformed_tokens_without_raising() -> None:
    future = str(int(time.time()) + 600)
    malformed_tokens = [
        "no-dot",
        "not-an-int." + ("0" * 64),
        f"{future}.",
        f"{future}.abc",
        f"{future}." + ("g" * 64),
        f"{future}.not-ascii-\u2603",
    ]

    for token in malformed_tokens:
        assert not verify_dashboard_session("secret", token)

    expires = int(time.time()) - 600
    signature = hmac.new(
        b"secret",
        str(expires).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert not verify_dashboard_session("secret", f"{expires}.{signature}")


def test_queue_profile_label_fallbacks() -> None:
    config = BridgeConfig(
        profiles=[BridgeProfile(id=101, name="Movie Only", media_types=["movie"])]
    )

    assert queue_profile_label(config, {"media_type": "movie"}) == ""
    assert queue_profile_label(config, {"profile_id": 101}) == "101"
    assert queue_profile_label(config, {"profile_id": 0, "media_type": "movie"}) == "0"
    assert queue_profile_label(config, {"profile_id": 999, "media_type": "movie"}) == "999"
    assert queue_profile_label(config, {"profile_id": 101, "media_type": "show"}) == "101"


def test_candidate_table_does_not_select_empty_candidate_id() -> None:
    rendered = candidate_table_html(
        {
            "status": "needs_release",
            "last_event": {"payload": {}},
            "candidates": [{"id": "", "title": "Nameless", "result_id": ""}],
        }
    )

    assert 'class="candidate selected"' not in rendered
    assert "/dashboard/queue//download/" not in rendered


def test_queue_controls_hide_mutating_actions_for_terminal_items() -> None:
    rendered = queue_controls_html({"id": "queue-id", "status": "available"})
    escaped = queue_controls_html({"id": 'queue"id', "status": "available"})

    assert "/dashboard/queue/queue-id/events" in rendered
    assert "/dashboard/queue/queue-id/reconcile" not in rendered
    assert "/dashboard/queue/queue-id/refresh-candidates" not in rendered
    assert 'href="/dashboard/queue/queue%22id/events"' in escaped
    assert 'queue"id' not in escaped


def test_format_size_normalizes_non_finite_values() -> None:
    assert format_size(float("nan")) == "0 B"
    assert format_size(float("inf")) == "0 B"
    assert format_size(10**10_000) == "0 B"
    assert format_size("-1") == "0 B"


def test_dashboard_renders_queue_validation_and_empty_states(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "screenarr.db", ENABLE_DASHBOARD=True)
    config = BridgeConfig(
        profiles=[BridgeProfile(id=101, name="Manual & Safe", mode=ReleaseMode.MANUAL)]
    )
    empty = dashboard_html(settings, config, [], [])
    rendered = dashboard_html(
        settings,
        config,
        [
            {
                "id": "queue-id",
                "title": "<Movie>",
                "media_type": "movie",
                "status": "needs_release",
                "profile_id": 101,
                "mediamanager_id": "movie-uuid",
                "candidates": [
                    {
                        "id": "candidate",
                        "title": "Candidate <One>",
                        "score": 250,
                        "seeders": 42,
                        "size": 1024**3,
                    }
                ],
                "last_event": {
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "message": "selected release submitted",
                    "payload": {"candidate_id": "candidate"},
                },
                "last_error": {
                    "created_at": "2026-01-01T00:01:00+00:00",
                    "message": "MediaManager timed out",
                },
            }
        ],
        [
            {
                "severity": "warning",
                "code": "ruleset.library_drift",
                "message": "Check <library>",
            }
        ],
    )

    assert "No queued requests" in empty
    assert "No local validation warnings" in empty
    assert "&lt;Movie&gt;" in rendered
    assert "Candidate &lt;One&gt;" in rendered
    assert "score 250" in rendered
    assert "seeders 42" in rendered
    assert "1.0 GiB" in rendered
    assert "/dashboard/queue/queue-id/download/candidate" in rendered
    assert "/dashboard/queue/queue-id/reconcile" in rendered
    assert "/dashboard/queue/queue-id/events" in rendered
    assert "MediaManager timed out" in rendered
    assert "Manual &amp; Safe" in rendered
    assert '<span class="muted">None</span>' in rendered
    assert "ruleset.library_drift" in rendered
    assert "Check &lt;library&gt;" in rendered


def test_events_html_escapes_rows_and_handles_empty_state() -> None:
    rendered = events_html(
        "queue<id>",
        [
            {
                "created_at": "2026-07-04T20:00:00Z",
                "event_type": "queue.<event>",
                "message": "selected <release> & done",
            }
        ],
    )
    empty = events_html("queue-id", [])

    assert "queue&lt;id&gt;" in rendered
    assert "2026-07-04T20:00:00Z" in rendered
    assert "queue.&lt;event&gt;" in rendered
    assert "selected &lt;release&gt; &amp; done" in rendered
    assert "No events recorded" in empty


def test_dashboard_login_rejects_invalid_access_and_disables_caching(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    app = create_app(
        settings=settings_for(
            tmp_path / "screenarr.db",
            ENABLE_DASHBOARD=True,
        ),
        config=BridgeConfig(profiles=[profile]),
    )

    with TestClient(app) as client:
        unauthenticated_dashboard = client.get("/dashboard")
        login_page = client.get("/dashboard/login")
        wrong_login = client.post("/dashboard/login", json={"api_key": "wrong"})
        malformed_login = client.post(
            "/dashboard/login",
            content="{",
            headers={"Content-Type": "application/json"},
        )
        malformed_cookie = client.get(
            "/dashboard",
            headers={"cookie": "screenarr_session=123.not-a-hex-signature"},
        )
        oversized_login = client.post(
            "/dashboard/login",
            content=b"x" * (MAX_DASHBOARD_LOGIN_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )

        assert unauthenticated_dashboard.status_code == 401
        assert unauthenticated_dashboard.headers["cache-control"] == "no-store"
        assert login_page.status_code == 200
        assert login_page.headers["cache-control"] == "no-store"
        assert wrong_login.status_code == 401
        assert wrong_login.headers["cache-control"] == "no-store"
        assert malformed_login.status_code == 400
        assert malformed_login.headers["cache-control"] == "no-store"
        assert malformed_cookie.status_code == 401
        assert malformed_cookie.headers["cache-control"] == "no-store"
        assert oversized_login.status_code == 413
        assert oversized_login.headers["cache-control"] == "no-store"


def test_dashboard_login_sets_cookie_and_allows_dashboard(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    dashboard_secret = "dashboard-session-secret-0000001"  # noqa: S105
    app = create_app(
        settings=settings_for(
            tmp_path / "screenarr.db",
            ENABLE_DASHBOARD=True,
            DASHBOARD_SESSION_SECRET=dashboard_secret,
        ),
        config=BridgeConfig(profiles=[profile]),
    )

    with TestClient(app) as client:
        login = client.post("/dashboard/login", json={"api_key": "test"})
        assert login.status_code == 200
        assert login.headers["cache-control"] == "no-store"
        set_cookie = login.headers["set-cookie"]
        assert "screenarr_session" in set_cookie
        assert "httponly" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower()
        assert "secure" not in cookie_flags(set_cookie)
        session_cookie = client.cookies.get("screenarr_session")
        assert session_cookie is not None
        assert verify_dashboard_session(dashboard_secret, session_cookie)
        assert not verify_dashboard_session("test", session_cookie)

        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert dashboard.headers["cache-control"] == "no-store"
        assert "Screenarr" in dashboard.text

        form_login = client.post(
            "/dashboard/login",
            data={"api_key": "test"},
            follow_redirects=False,
        )
        assert form_login.status_code == 303
        assert form_login.headers["cache-control"] == "no-store"
        form_cookie = form_login.headers["set-cookie"]
        assert "screenarr_session" in form_cookie
        assert "httponly" in form_cookie.lower()
        assert "samesite=lax" in form_cookie.lower()
        assert "secure" not in cookie_flags(form_cookie)
        client.app.state.settings.bridge_api_key = SecretStr("rotated-api-key")
        dashboard_after_api_key_rotation = client.get("/dashboard")
        assert dashboard_after_api_key_rotation.status_code == 200

    secure_app = create_app(
        settings=settings_for(tmp_path / "secure-screenarr.db", ENABLE_DASHBOARD=True),
        config=BridgeConfig(profiles=[profile]),
    )
    with TestClient(secure_app, base_url="https://testserver") as secure_client:
        secure_login = secure_client.post("/dashboard/login", json={"api_key": "test"})
        assert secure_login.status_code == 200
        assert "secure" in cookie_flags(secure_login.headers["set-cookie"])


def test_dashboard_events_page_uses_dashboard_session(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    app = create_app(
        settings=settings_for(tmp_path / "screenarr.db", ENABLE_DASHBOARD=True),
        config=BridgeConfig(profiles=[profile]),
    )

    with TestClient(app) as client:
        item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=550,
            title="Fight Club",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-uuid",
        )
        client.app.state.store.add_event(
            event_type="queue.download_submitted",
            message="selected release submitted to MediaManager",
            queue_id=item["id"],
        )
        unauthenticated = client.get(f"/dashboard/queue/{item['id']}/events")
        query_auth = client.get(
            f"/dashboard/queue/{item['id']}/events",
            params={"apikey": "test"},
        )
        header_auth = client.get(
            f"/dashboard/queue/{item['id']}/events",
            headers={"X-Api-Key": "test"},
        )
        login = client.post("/dashboard/login", json={"api_key": "test"})
        events = client.get(f"/dashboard/queue/{item['id']}/events")

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"] == "invalid dashboard session"
    assert query_auth.status_code == 401
    assert query_auth.json()["detail"] == "dashboard query API key auth is not allowed"
    assert header_auth.status_code == 200
    assert login.status_code == 200
    assert events.status_code == 200
    assert "queue.download_submitted" in events.text
    assert "selected release submitted" in events.text


def test_dashboard_post_actions_reject_query_api_key_auth(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    app = create_app(
        settings=settings_for(tmp_path / "screenarr.db", ENABLE_DASHBOARD=True),
        config=BridgeConfig(profiles=[profile]),
    )

    with TestClient(app) as client:
        item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=550,
            title="Fight Club",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="needs_release",
            mediamanager_id="movie-uuid",
        )
        client.app.state.store.replace_candidates(
            item["id"],
            [{"public_indexer_result_id": "release-one", "title": "Release One"}],
            media_type="movie",
        )
        candidate_id = client.app.state.store.get_queue_item(item["id"])["candidates"][0]["id"]

        refresh = client.post(
            f"/dashboard/queue/{item['id']}/refresh-candidates",
            params={"apikey": "test"},
        )
        reconcile = client.post(
            f"/dashboard/queue/{item['id']}/reconcile",
            params={"apikey": "test"},
        )
        download = client.post(
            f"/dashboard/queue/{item['id']}/download/{candidate_id}",
            params={"apikey": "test"},
        )

    responses = (refresh, reconcile, download)
    assert [response.status_code for response in responses] == [401, 401, 401]
    assert [response.json()["detail"] for response in responses] == [
        "dashboard query API key auth is not allowed",
        "dashboard query API key auth is not allowed",
        "dashboard query API key auth is not allowed",
    ]


def test_dashboard_post_actions_require_same_origin(tmp_path: Path) -> None:
    app = create_app(
        settings=settings_for(tmp_path / "screenarr.db", ENABLE_DASHBOARD=True),
        config=BridgeConfig(),
        mediamanager_factory=lambda _settings: SelectiveDetailMediaManager(),
    )

    with TestClient(app) as client:
        item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=550,
            title="Fight Club",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="download_submitted",
            mediamanager_id="movie-ok",
        )
        url = f"/dashboard/queue/{item['id']}/reconcile"
        missing_origin = client.post(url, headers={"X-Api-Key": "test"})
        bad_origin = client.post(
            url,
            headers={"X-Api-Key": "test", "Origin": "http://evil.example"},
        )
        good_origin = client.post(
            url,
            headers={"X-Api-Key": "test", "Origin": "http://testserver"},
            follow_redirects=False,
        )
        forwarded_proto_origin = client.post(
            url,
            headers={
                "X-Api-Key": "test",
                "Origin": "https://testserver",
                "X-Forwarded-Proto": "https",
            },
            follow_redirects=False,
        )
        host_only_proxy_origin = client.post(
            url,
            headers={"X-Api-Key": "test", "Origin": "https://testserver"},
            follow_redirects=False,
        )

    assert missing_origin.status_code == 403
    assert bad_origin.status_code == 403
    assert good_origin.status_code == 303
    # Forwarded headers are not trusted by default, so the https origin no
    # longer matches the plain-http request URL.
    assert forwarded_proto_origin.status_code == 403
    assert host_only_proxy_origin.status_code == 403


def test_dashboard_post_actions_return_not_found_when_dashboard_disabled(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=settings_for(tmp_path / "screenarr.db", ENABLE_DASHBOARD=False),
        config=BridgeConfig(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/dashboard/queue/missing/reconcile",
            headers={"X-Api-Key": "test"},
        )

    assert response.status_code == 404


def test_onscreen_webhook_signature_validation_and_endpoint(tmp_path: Path) -> None:
    body = b'{"event":"library.scan.complete"}'
    timestamp = str(int(time.time()))
    signature = sign_webhook(TEST_WEBHOOK_SECRET, timestamp, body)
    assert verify_onscreen_signature(TEST_WEBHOOK_SECRET, timestamp, signature, body)
    assert not verify_onscreen_signature(TEST_WEBHOOK_SECRET, timestamp, "sha256=bad", body)
    assert not verify_onscreen_signature(
        TEST_WEBHOOK_SECRET, timestamp, "sha256=not-ascii-\u2603", body
    )
    stale_timestamp = str(int(time.time()) - 1_000)
    stale_signature = sign_webhook(TEST_WEBHOOK_SECRET, stale_timestamp, body)
    assert not verify_onscreen_signature(
        TEST_WEBHOOK_SECRET, stale_timestamp, stale_signature, body
    )

    app = create_app(
        settings=settings_for(
            tmp_path / "screenarr.db",
            ENABLE_ONSCREEN_WEBHOOK=True,
            **webhook_secret_setting(),
        ),
        config=BridgeConfig(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/integrations/onscreen/webhook",
            content=body,
            headers={
                "X-OnScreen-Timestamp": timestamp,
                "X-OnScreen-Signature": signature,
                "Content-Type": "application/json",
            },
        )
        rejected = client.post(
            "/integrations/onscreen/webhook",
            content=body,
            headers={
                "X-OnScreen-Timestamp": timestamp,
                "X-OnScreen-Signature": "sha256=bad",
                "Content-Type": "application/json",
            },
        )
        stale = client.post(
            "/integrations/onscreen/webhook",
            content=body,
            headers={
                "X-OnScreen-Timestamp": stale_timestamp,
                "X-OnScreen-Signature": stale_signature,
                "Content-Type": "application/json",
            },
        )
        oversized = client.post(
            "/integrations/onscreen/webhook",
            content=b"x" * (MAX_WEBHOOK_BODY_BYTES + 1),
            headers={
                "X-OnScreen-Timestamp": timestamp,
                "X-OnScreen-Signature": "sha256=bad",
                "Content-Type": "application/json",
            },
        )
        queue_items = client.app.state.store.list_queue_items()
    assert response.status_code == 200
    assert rejected.status_code == 401
    assert stale.status_code == 401
    assert oversized.status_code == 413
    assert queue_items == []


def test_onscreen_webhook_can_mark_matching_queue_item_available(tmp_path: Path) -> None:
    app = create_app(
        settings=settings_for(
            tmp_path / "screenarr.db",
            ENABLE_ONSCREEN_WEBHOOK=True,
            **webhook_secret_setting(),
        ),
        config=BridgeConfig(),
    )
    body = b'{"event":"request.available","media_type":"movie","tmdb_id":550}'
    timestamp = str(int(time.time()))
    signature = sign_webhook(TEST_WEBHOOK_SECRET, timestamp, body)

    with TestClient(app) as client:
        item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=550,
            title="Fight Club",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="imported",
            mediamanager_id="movie-uuid",
        )
        response = client.post(
            "/integrations/onscreen/webhook",
            content=body,
            headers={
                "X-OnScreen-Timestamp": timestamp,
                "X-OnScreen-Signature": signature,
                "Content-Type": "application/json",
            },
        )
        stored = client.app.state.store.get_queue_item(item["id"])

    assert response.status_code == 200
    assert stored["status"] == "available"
    assert stored["resolved_at"] is not None
    assert stored["last_event"]["event_type"] == "queue.available"


def test_parse_webhook_int_accepts_only_plain_numeric_values() -> None:
    assert parse_webhook_int(550) == 550
    assert parse_webhook_int(" 550 ") == 550
    assert parse_webhook_int(True) is None
    assert parse_webhook_int(None) is None
    assert parse_webhook_int("tt0137523") is None
    assert parse_webhook_int("550abc") is None


def test_onscreen_webhook_does_not_extract_imdb_digits_for_external_id(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=settings_for(
            tmp_path / "screenarr.db",
            ENABLE_ONSCREEN_WEBHOOK=True,
            **webhook_secret_setting(),
        ),
        config=BridgeConfig(),
    )
    body = b'{"event":"request.available","media_type":"movie","tmdb_id":"tt0137523"}'
    timestamp = str(int(time.time()))

    with TestClient(app) as client:
        item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=137_523,
            title="Fight Club",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status="imported",
            mediamanager_id="movie-uuid",
        )
        response = client.post(
            "/integrations/onscreen/webhook",
            content=body,
            headers={
                "X-OnScreen-Timestamp": timestamp,
                "X-OnScreen-Signature": sign_webhook(TEST_WEBHOOK_SECRET, timestamp, body),
                "Content-Type": "application/json",
            },
        )
        stored = client.app.state.store.get_queue_item(item["id"])

    assert response.status_code == 200
    assert stored["status"] == "imported"
    assert stored["last_event"] is None


@pytest.mark.parametrize("queue_status", ["download_submitted", "download_unverified"])
def test_onscreen_webhook_does_not_close_unreconciled_download(
    tmp_path: Path,
    queue_status: str,
) -> None:
    app = create_app(
        settings=settings_for(
            tmp_path / "screenarr.db",
            ENABLE_ONSCREEN_WEBHOOK=True,
            **webhook_secret_setting(),
        ),
        config=BridgeConfig(),
    )
    body = b'{"event":"request.available","media_type":"movie","tmdb_id":550}'
    timestamp = str(int(time.time()))

    with TestClient(app) as client:
        item = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=550,
            title="Fight Club",
            profile_id=101,
            profile_name="Manual",
            mode="manual",
            status=queue_status,
            mediamanager_id="movie-uuid",
        )
        response = client.post(
            "/integrations/onscreen/webhook",
            content=body,
            headers={
                "X-OnScreen-Timestamp": timestamp,
                "X-OnScreen-Signature": sign_webhook(TEST_WEBHOOK_SECRET, timestamp, body),
                "Content-Type": "application/json",
            },
        )
        stored = client.app.state.store.get_queue_item(item["id"])

    assert response.status_code == 200
    assert stored["status"] == queue_status
    assert stored["resolved_at"] is None
    assert stored["last_event"] is None


def test_onscreen_webhook_rejects_negated_availability_and_targets_profile(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=settings_for(
            tmp_path / "screenarr.db",
            ENABLE_ONSCREEN_WEBHOOK=True,
            **webhook_secret_setting(),
        ),
        config=BridgeConfig(
            profiles=[
                BridgeProfile(id=101, name="Manual 1080p", mode=ReleaseMode.MANUAL),
                BridgeProfile(id=202, name="Manual 4K", mode=ReleaseMode.MANUAL),
            ]
        ),
    )

    with TestClient(app) as client:
        first = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=550,
            title="Fight Club",
            profile_id=101,
            profile_name="Manual 1080p",
            mode="manual",
            status="imported",
            mediamanager_id="movie-1080p",
        )
        client.app.state.store.add_event(
            event_type="queue.imported",
            message="MediaManager reports item downloaded/imported",
            queue_id=first["id"],
        )
        second = client.app.state.store.upsert_queue_item(
            media_type="movie",
            external_id=550,
            title="Fight Club",
            profile_id=202,
            profile_name="Manual 4K",
            mode="manual",
            status="imported",
            mediamanager_id="movie-4k",
        )
        negated_body = (
            b'{"event":"request.not_downloaded","media_type":"movie",'
            b'"status":"available","tmdb_id":550,"qualityProfileId":101}'
        )
        timestamp = str(int(time.time()))
        negated = client.post(
            "/integrations/onscreen/webhook",
            content=negated_body,
            headers={
                "X-OnScreen-Timestamp": timestamp,
                "X-OnScreen-Signature": sign_webhook(
                    TEST_WEBHOOK_SECRET, timestamp, negated_body
                ),
                "Content-Type": "application/json",
            },
        )
        first_after_negated = client.app.state.store.get_queue_item(first["id"])
        second_after_negated = client.app.state.store.get_queue_item(second["id"])
        available_body = (
            b'{"event":"request.available","media_type":"movie",'
            b'"tmdb_id":550,"qualityProfileId":101}'
        )
        timestamp = str(int(time.time()))
        available = client.post(
            "/integrations/onscreen/webhook",
            content=available_body,
            headers={
                "X-OnScreen-Timestamp": timestamp,
                "X-OnScreen-Signature": sign_webhook(
                    TEST_WEBHOOK_SECRET, timestamp, available_body
                ),
                "Content-Type": "application/json",
            },
        )
        first_stored = client.app.state.store.get_queue_item(first["id"])
        second_stored = client.app.state.store.get_queue_item(second["id"])

    assert negated.status_code == 200
    assert first_after_negated["status"] == "imported"
    assert first_after_negated["last_event"]["event_type"] == "queue.imported"
    assert second_after_negated["status"] == "imported"
    assert second_after_negated["last_event"] is None
    assert available.status_code == 200
    assert first_stored["status"] == "available"
    assert first_stored["last_event"]["event_type"] == "queue.available"
    assert second_stored["status"] == "imported"
    assert second_stored["last_event"] is None


def test_duplicate_profile_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="profile ids must be unique"):
        BridgeConfig(
            profiles=[
                BridgeProfile(id=101, name="One"),
                BridgeProfile(id=101, name="Two"),
            ]
        )


@pytest.mark.anyio
async def test_live_library_validation_reports_missing_and_unavailable_libraries() -> None:
    config = BridgeConfig(
        profiles=[BridgeProfile(id=101, name="Default", mediamanager_library="Default")]
    )
    missing = await validate_live_libraries(config, MissingLibraryMediaManager())
    unavailable = await validate_live_libraries(config, FailingLibraryMediaManager())

    missing_codes = {item["code"] for item in missing}
    assert "library.movie_missing" in missing_codes
    assert "library.show_missing" in missing_codes
    assert unavailable[0]["code"] == "mediamanager.validation_unavailable"


def test_static_mediamanager_ruleset_validation(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        [indexers]
        [indexers.prowlarr]
        enabled = false

        [indexers.jackett]
        enabled = true

        [[indexers.title_scoring_rules]]
        name = "prefer_h265"
        keywords = ["h265"]
        score_modifier = 100

        [[indexers.title_scoring_rules]]
        name = "prefer_h265"
        keywords = ["x265"]
        score_modifier = 50

        [[indexers.title_scoring_rules]]
        name = "avoid_ts"
        keywords = ["ts", "cam"]
        score_modifier = -100

        [[indexers.title_scoring_rules]]
        name = "bad_score"
        keywords = ["bad"]
        score_modifier = "bad"

        [[indexers.scoring_rule_sets]]
        libraries = ["ALL_MOVIES"]
        rule_names = ["prefer_h265", "avoid_ts"]

        [[indexers.scoring_rule_sets]]
        name = "duplicate"
        libraries = ["ALL_MOVIES"]
        rule_names = ["prefer_h265", "avoid_ts"]

        [[indexers.scoring_rule_sets]]
        name = "duplicate"
        libraries = ["ALL_MOVIES"]
        rule_names = ["prefer_h265", "avoid_ts"]

        [[indexers.scoring_rule_sets]]
        name = "default"
        libraries = ["ALL_MOVIES"]
        rule_names = ["prefer_h265", "avoid_ts", "missing_rule"]
        """,
        encoding="utf-8",
    )
    config = BridgeConfig(
        profiles=[BridgeProfile(id=101, name="Default", mediamanager_ruleset="default")]
    )
    issues = validate_static_config(
        config,
        settings_for(tmp_path / "screenarr.db", MEDIAMANAGER_CONFIG_PATH=config_path),
    )

    codes = {item["code"] for item in issues}
    assert "ruleset.invalid_entry" in codes
    assert "ruleset.duplicate_name" in codes
    assert "ruleset.missing_rules" in codes
    assert "ruleset.library_drift" in codes
    assert "indexer.prowlarr_disabled" in codes
    assert "indexer.jackett_enabled" in codes
    assert "indexer.short_negative_keyword" in codes
    assert "indexer.invalid_score_modifier" in codes
    assert "indexer.duplicate_title_scoring_rule_name" in codes
    missing_rules = [
        item for item in issues if item["code"] == "ruleset.missing_rules"
    ]
    assert missing_rules
    assert "missing_rule" in missing_rules[0]["message"]
    assert "prefer_h265" not in missing_rules[0]["message"]
    assert "indexer.duplicate_title_scoring_rule_name" in codes
    assert any(
        item["code"] == "indexer.duplicate_title_scoring_rule_name"
        and item["severity"] == "error"
        for item in issues
    )
    assert "indexer.invalid_title_scoring_rules" not in codes

    non_finite_score_path = tmp_path / "non-finite-score.toml"
    non_finite_score_path.write_text(
        """
        [indexers]

        [[indexers.title_scoring_rules]]
        name = "non_finite_score"
        keywords = ["bad"]
        score_modifier = nan
        """,
        encoding="utf-8",
    )
    non_finite_score_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=non_finite_score_path,
        ),
    )
    non_finite_score_codes = {item["code"] for item in non_finite_score_issues}
    assert "indexer.invalid_score_modifier" in non_finite_score_codes

    missing_title_rules_path = tmp_path / "missing-title-rules.toml"
    missing_title_rules_path.write_text(
        """
        [indexers]

        [[indexers.scoring_rule_sets]]
        name = "default"
        libraries = ["ALL_MOVIES"]
        rule_names = []
        """,
        encoding="utf-8",
    )
    missing_title_rule_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=missing_title_rules_path,
        ),
    )
    missing_title_rule_codes = {item["code"] for item in missing_title_rule_issues}
    assert "indexer.invalid_title_scoring_rules" not in missing_title_rule_codes

    bad_config_path = tmp_path / "bad-config.toml"
    bad_config_path.write_bytes(b"\xff")
    bad_issues = validate_static_config(
        config,
        settings_for(tmp_path / "screenarr.db", MEDIAMANAGER_CONFIG_PATH=bad_config_path),
    )
    assert bad_issues[0]["code"] == "mediamanager_config.invalid_toml"

    table_config_path = tmp_path / "table-config.toml"
    table_config_path.write_text(
        """
        [indexers]
        [indexers.scoring_rule_sets]
        name = "default"
        """,
        encoding="utf-8",
    )
    table_issues = validate_static_config(
        config,
        settings_for(tmp_path / "screenarr.db", MEDIAMANAGER_CONFIG_PATH=table_config_path),
    )
    assert table_issues[0]["code"] == "mediamanager_config.invalid_shape"

    invalid_title_rules_path = tmp_path / "invalid-title-rules.toml"
    invalid_title_rules_path.write_text(
        """
        [indexers]
        title_scoring_rules = "bad"
        """,
        encoding="utf-8",
    )
    invalid_title_rule_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=invalid_title_rules_path,
        ),
    )
    invalid_title_rule_codes = {item["code"] for item in invalid_title_rule_issues}
    assert "indexer.invalid_title_scoring_rules" in invalid_title_rule_codes

    malformed_title_rule_path = tmp_path / "malformed-title-rule.toml"
    malformed_title_rule_path.write_text(
        """
        [indexers]
        title_scoring_rules = ["bad"]
        """,
        encoding="utf-8",
    )
    malformed_title_rule_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=malformed_title_rule_path,
        ),
    )
    malformed_title_rule_codes = {item["code"] for item in malformed_title_rule_issues}
    assert "indexer.invalid_title_scoring_rule" in malformed_title_rule_codes

    missing_title_rule_name_path = tmp_path / "missing-title-rule-name.toml"
    missing_title_rule_name_path.write_text(
        """
        [indexers]

        [[indexers.title_scoring_rules]]
        keywords = ["ts"]
        score_modifier = -100
        """,
        encoding="utf-8",
    )
    missing_title_rule_name_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=missing_title_rule_name_path,
        ),
    )
    missing_title_rule_name_codes = {
        item["code"] for item in missing_title_rule_name_issues
    }
    assert "indexer.invalid_title_scoring_rule_name" in missing_title_rule_name_codes

    invalid_keywords_path = tmp_path / "invalid-keywords.toml"
    invalid_keywords_path.write_text(
        """
        [indexers]

        [[indexers.title_scoring_rules]]
        name = "bad_keywords"
        keywords = "ts"
        score_modifier = -100
        """,
        encoding="utf-8",
    )
    invalid_keyword_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=invalid_keywords_path,
        ),
    )
    invalid_keyword_codes = {item["code"] for item in invalid_keyword_issues}
    assert "indexer.invalid_keywords" in invalid_keyword_codes

    non_string_keywords_path = tmp_path / "non-string-keywords.toml"
    non_string_keywords_path.write_text(
        """
        [indexers]

        [[indexers.title_scoring_rules]]
        name = "non_string_keywords"
        keywords = ["ts", 123]
        score_modifier = -100
        """,
        encoding="utf-8",
    )
    non_string_keyword_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=non_string_keywords_path,
        ),
    )
    non_string_keyword_codes = {item["code"] for item in non_string_keyword_issues}
    assert "indexer.invalid_keyword" in non_string_keyword_codes

    blank_keyword_path = tmp_path / "blank-keyword.toml"
    blank_keyword_path.write_text(
        """
        [indexers]

        [[indexers.title_scoring_rules]]
        name = "blank_keyword"
        keywords = [" "]
        score_modifier = -100
        """,
        encoding="utf-8",
    )
    blank_keyword_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=blank_keyword_path,
        ),
    )
    blank_keyword_codes = {item["code"] for item in blank_keyword_issues}
    assert "indexer.invalid_keyword" in blank_keyword_codes

    invalid_rules_path = tmp_path / "invalid-rules-config.toml"
    invalid_rules_path.write_text(
        """
        [indexers]
        title_scoring_rules = "bad"
        indexer_flag_scoring_rules = { name = "also_bad" }

        [[indexers.scoring_rule_sets]]
        name = "default"
        libraries = ["ALL_MOVIES", "ALL_TV"]
        rule_names = []
        """,
        encoding="utf-8",
    )
    invalid_rule_issues = validate_static_config(
        config,
        settings_for(tmp_path / "screenarr.db", MEDIAMANAGER_CONFIG_PATH=invalid_rules_path),
    )
    invalid_rule_messages = {
        item["message"]
        for item in invalid_rule_issues
        if item["code"] == "indexer.invalid_flag_scoring_rules"
    }
    assert "MediaManager config 'indexers.indexer_flag_scoring_rules' must be a list" in (
        invalid_rule_messages
    )
    invalid_rule_codes = {item["code"] for item in invalid_rule_issues}
    assert "indexer.invalid_title_scoring_rules" in invalid_rule_codes

    malformed_flag_rule_path = tmp_path / "malformed-flag-rule.toml"
    malformed_flag_rule_path.write_text(
        """
        [indexers]
        indexer_flag_scoring_rules = ["bad"]
        """,
        encoding="utf-8",
    )
    malformed_flag_rule_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=malformed_flag_rule_path,
        ),
    )
    malformed_flag_rule_messages = {
        item["message"]
        for item in malformed_flag_rule_issues
        if item["code"] == "indexer.invalid_flag_scoring_rule"
    }
    assert "indexer flag scoring rule entry must be a table" in (
        malformed_flag_rule_messages
    )

    malformed_flag_rule_name_path = tmp_path / "malformed-flag-rule-name.toml"
    malformed_flag_rule_name_path.write_text(
        """
        [indexers]
        indexer_flag_scoring_rules = [{ name = "" }]
        """,
        encoding="utf-8",
    )
    malformed_flag_rule_name_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=malformed_flag_rule_name_path,
        ),
    )
    malformed_flag_rule_name_messages = {
        item["message"]
        for item in malformed_flag_rule_name_issues
        if item["code"] == "indexer.invalid_flag_scoring_rule_name"
    }
    assert "indexer flag scoring rule name must be a non-empty string" in (
        malformed_flag_rule_name_messages
    )

    malformed_backends_path = tmp_path / "malformed-backends.toml"
    malformed_backends_path.write_text(
        """
        [indexers]
        prowlarr = "bad"
        jackett = "bad"
        """,
        encoding="utf-8",
    )
    malformed_backend_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=malformed_backends_path,
        ),
    )
    malformed_backend_messages = {
        item["message"]
        for item in malformed_backend_issues
        if item["code"] == "indexer.invalid_shape"
    }
    assert "MediaManager config 'indexers.prowlarr' must be a table" in (
        malformed_backend_messages
    )
    assert "MediaManager config 'indexers.jackett' must be a table" in (
        malformed_backend_messages
    )

    duplicate_flag_rule_path = tmp_path / "duplicate-flag-rule-name.toml"
    duplicate_flag_rule_path.write_text(
        """
        [indexers]
        indexer_flag_scoring_rules = [{ name = "flag_dupe" }, { name = "flag_dupe" }]

        [[indexers.scoring_rule_sets]]
        name = "default"
        libraries = ["ALL_MOVIES"]
        rule_names = ["flag_dupe"]
        """,
        encoding="utf-8",
    )
    duplicate_flag_rule_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=duplicate_flag_rule_path,
        ),
    )
    duplicate_flag_rule_codes = {
        item["code"] for item in duplicate_flag_rule_issues
    }
    assert "ruleset.duplicate_indexer_flag_scoring_rule_name" in (
        duplicate_flag_rule_codes
    )
    duplicate_flag_missing_rules = [
        item
        for item in duplicate_flag_rule_issues
        if item["code"] == "ruleset.missing_rules"
    ]
    assert duplicate_flag_missing_rules
    assert "flag_dupe" in duplicate_flag_missing_rules[0]["message"]

    cross_duplicate_rule_path = tmp_path / "cross-duplicate-rule-name.toml"
    cross_duplicate_rule_path.write_text(
        """
        [indexers]
        indexer_flag_scoring_rules = [{ name = "shared_rule" }]

        [[indexers.title_scoring_rules]]
        name = "shared_rule"
        keywords = ["shared"]
        score_modifier = 100
        """,
        encoding="utf-8",
    )
    cross_duplicate_rule_issues = validate_static_config(
        config,
        settings_for(
            tmp_path / "screenarr.db",
            MEDIAMANAGER_CONFIG_PATH=cross_duplicate_rule_path,
        ),
    )
    cross_duplicate_rule_codes = {
        item["code"] for item in cross_duplicate_rule_issues
    }
    assert "ruleset.duplicate_scoring_rule_name" in cross_duplicate_rule_codes


def test_trust_forwarded_headers_setting_defaults_off(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "screenarr.db")
    trusted = settings_for(tmp_path / "trusted.db", TRUST_FORWARDED_HEADERS=True)

    assert not settings.trust_forwarded_headers
    assert trusted.trust_forwarded_headers


def seed_submitted_movie_item(client: TestClient, mediamanager_id: str) -> dict[str, Any]:
    return client.app.state.store.upsert_queue_item(
        media_type="movie",
        external_id=550,
        title="Fight Club",
        profile_id=101,
        profile_name="Manual",
        mode="manual",
        status="download_submitted",
        mediamanager_id=mediamanager_id,
    )


def test_dashboard_csrf_rejects_spoofed_forwarded_headers_by_default(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=settings_for(tmp_path / "screenarr.db", ENABLE_DASHBOARD=True),
        config=BridgeConfig(),
        mediamanager_factory=lambda _settings: SelectiveDetailMediaManager(),
    )

    with TestClient(app) as client:
        item = seed_submitted_movie_item(client, "movie-ok")
        url = f"/dashboard/queue/{item['id']}/reconcile"
        spoofed = client.post(
            url,
            headers={
                "X-Api-Key": "test",
                "Origin": "https://media.example.com",
                "X-Forwarded-Host": "media.example.com",
                "X-Forwarded-Proto": "https",
            },
            follow_redirects=False,
        )

    assert spoofed.status_code == 403


def test_dashboard_csrf_accepts_forwarded_origin_when_trusted(tmp_path: Path) -> None:
    app = create_app(
        settings=settings_for(
            tmp_path / "screenarr.db",
            ENABLE_DASHBOARD=True,
            TRUST_FORWARDED_HEADERS=True,
        ),
        config=BridgeConfig(),
        mediamanager_factory=lambda _settings: SelectiveDetailMediaManager(),
    )

    with TestClient(app) as client:
        item = seed_submitted_movie_item(client, "movie-ok")
        url = f"/dashboard/queue/{item['id']}/reconcile"
        proxy_origin = client.post(
            url,
            headers={
                "X-Api-Key": "test",
                "Origin": "https://media.example.com",
                "X-Forwarded-Host": "media.example.com",
                "X-Forwarded-Proto": "https",
            },
            follow_redirects=False,
        )
        mismatched_origin = client.post(
            url,
            headers={
                "X-Api-Key": "test",
                "Origin": "https://evil.example.com",
                "X-Forwarded-Host": "media.example.com",
                "X-Forwarded-Proto": "https",
            },
            follow_redirects=False,
        )

    assert proxy_origin.status_code == 303
    assert mismatched_origin.status_code == 403


def test_dashboard_cookie_secure_follows_forwarded_proto_only_when_trusted(
    tmp_path: Path,
) -> None:
    default_app = create_app(
        settings=settings_for(tmp_path / "plain.db", ENABLE_DASHBOARD=True),
        config=BridgeConfig(),
    )
    with TestClient(default_app) as client:
        login = client.post(
            "/dashboard/login",
            json={"api_key": "test"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert login.status_code == 200
        assert "secure" not in cookie_flags(login.headers["set-cookie"])

    trusted_app = create_app(
        settings=settings_for(
            tmp_path / "trusted.db",
            ENABLE_DASHBOARD=True,
            TRUST_FORWARDED_HEADERS=True,
        ),
        config=BridgeConfig(),
    )
    with TestClient(trusted_app) as client:
        login = client.post(
            "/dashboard/login",
            json={"api_key": "test"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert login.status_code == 200
        assert "secure" in cookie_flags(login.headers["set-cookie"])


EXPECTED_DASHBOARD_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def assert_dashboard_security_headers(response: httpx.Response) -> None:
    assert response.headers["content-security-policy"] == EXPECTED_DASHBOARD_CSP
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_dashboard_and_login_responses_include_security_headers(tmp_path: Path) -> None:
    app = create_app(
        settings=settings_for(tmp_path / "screenarr.db", ENABLE_DASHBOARD=True),
        config=BridgeConfig(),
    )

    with TestClient(app) as client:
        login_page = client.get("/dashboard/login")
        dashboard_page = client.get("/dashboard", headers={"X-Api-Key": "test"})
        unauthenticated = client.get("/dashboard")
        api_response = client.get("/api/v3/system/status", headers={"X-Api-Key": "test"})

    assert login_page.status_code == 200
    assert dashboard_page.status_code == 200
    assert unauthenticated.status_code == 401
    for page in (login_page, dashboard_page, unauthenticated):
        assert_dashboard_security_headers(page)
    assert "content-security-policy" not in api_response.headers
    assert "x-frame-options" not in api_response.headers


def test_dashboard_login_throttles_repeated_failures(tmp_path: Path) -> None:
    app = create_app(
        settings=settings_for(tmp_path / "screenarr.db", ENABLE_DASHBOARD=True),
        config=BridgeConfig(),
    )
    now = [1000.0]

    with TestClient(app) as client:
        client.app.state.login_throttle = LoginThrottle(
            max_attempts=3,
            lockout_seconds=60.0,
            clock=lambda: now[0],
        )
        failures = [
            client.post("/dashboard/login", json={"api_key": "wrong"}) for _ in range(3)
        ]
        locked = client.post("/dashboard/login", json={"api_key": "wrong"})
        correct_while_locked = client.post("/dashboard/login", json={"api_key": "test"})
        api_not_throttled = client.get(
            "/api/v3/system/status",
            headers={"X-Api-Key": "test"},
        )
        now[0] += 61
        after_expiry = client.post("/dashboard/login", json={"api_key": "test"})

    assert [response.status_code for response in failures] == [401, 401, 401]
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    assert correct_while_locked.status_code == 429
    assert api_not_throttled.status_code == 200
    assert after_expiry.status_code == 200


def test_dashboard_login_success_resets_throttle(tmp_path: Path) -> None:
    app = create_app(
        settings=settings_for(tmp_path / "screenarr.db", ENABLE_DASHBOARD=True),
        config=BridgeConfig(),
    )

    with TestClient(app) as client:
        client.app.state.login_throttle = LoginThrottle(
            max_attempts=3,
            lockout_seconds=60.0,
            clock=lambda: 1000.0,
        )
        attempts = [
            client.post("/dashboard/login", json={"api_key": "wrong"}),
            client.post("/dashboard/login", json={"api_key": "wrong"}),
            client.post("/dashboard/login", json={"api_key": "test"}),
            client.post("/dashboard/login", json={"api_key": "wrong"}),
            client.post("/dashboard/login", json={"api_key": "wrong"}),
        ]

    assert [response.status_code for response in attempts] == [401, 401, 200, 401, 401]


def test_login_throttle_validates_configuration() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        LoginThrottle(max_attempts=0)
    with pytest.raises(ValueError, match="lockout_seconds"):
        LoginThrottle(lockout_seconds=0)
    with pytest.raises(ValueError, match="failure_window_seconds"):
        LoginThrottle(failure_window_seconds=0)


def test_login_throttle_prunes_stale_failures() -> None:
    now = [1000.0]
    throttle = LoginThrottle(
        max_attempts=3,
        lockout_seconds=60.0,
        failure_window_seconds=300.0,
        clock=lambda: now[0],
    )
    # Two sub-threshold failures, then the client goes quiet beyond the window.
    throttle.record_failure("10.0.0.1")
    throttle.record_failure("10.0.0.1")
    assert "10.0.0.1" in throttle._failures
    now[0] += 301
    throttle.retry_after_seconds("10.0.0.2")
    assert "10.0.0.1" not in throttle._failures
    # The slate was wiped: two more failures do not reach the lockout threshold.
    throttle.record_failure("10.0.0.1")
    throttle.record_failure("10.0.0.1")
    assert throttle.retry_after_seconds("10.0.0.1") == 0
    # Expired lockouts are pruned from the lockout map as well.
    throttle.record_failure("10.0.0.1")
    assert throttle.retry_after_seconds("10.0.0.1") > 0
    now[0] += 61
    throttle._prune_stale()
    assert "10.0.0.1" not in throttle._locked_until


def test_mediamanager_reports_grab_activity_variants() -> None:
    movie_item: dict[str, Any] = {"media_type": "movie"}
    assert mediamanager_reports_grab_activity({"downloading": True}, movie_item)
    assert mediamanager_reports_grab_activity({"grabbed": 1}, movie_item)
    assert mediamanager_reports_grab_activity({"status": "snatched"}, movie_item)
    assert mediamanager_reports_grab_activity({"state": "Queued"}, movie_item)
    assert not mediamanager_reports_grab_activity({"status": "pending"}, movie_item)
    assert not mediamanager_reports_grab_activity({"downloaded": False}, movie_item)
    assert not mediamanager_reports_grab_activity({}, movie_item)
    assert not mediamanager_reports_grab_activity(["not", "a", "mapping"], movie_item)

    show_item: dict[str, Any] = {"media_type": "show", "seasons": [1, 2]}
    assert mediamanager_reports_grab_activity(
        {"seasons": [{"season_number": 2, "downloaded": True}]},
        show_item,
    )
    assert mediamanager_reports_grab_activity(
        {"seasons": [{"season_number": 1, "status": "downloading"}]},
        show_item,
    )
    assert not mediamanager_reports_grab_activity(
        {"seasons": [{"season_number": 3, "downloaded": True}]},
        show_item,
    )
    # Show-level signals on the payload itself count even when season rows exist.
    assert mediamanager_reports_grab_activity(
        {"downloading": True, "seasons": [{"season_number": 3, "downloaded": False}]},
        show_item,
    )
    assert mediamanager_reports_grab_activity(
        {"status": "grabbed", "seasons": [{"season_number": 1}]},
        show_item,
    )
    # No seasons list falls back to top-level activity, mirroring the import check.
    assert mediamanager_reports_grab_activity({"grabbed": True}, show_item)
    assert mediamanager_reports_grab_activity(
        {"downloading": True},
        {"media_type": "show", "seasons": []},
    )


def test_reconcile_returns_never_grabbed_submission_to_release_selection(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(
        tmp_path / "screenarr.db",
        profile,
        RECONCILE_GRACE_SECONDS=0,
    )

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]
        submitted = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        # Crash window: the bridge marked the item submitted but the grab never
        # reached MediaManager, which shows neither import nor download activity.
        fake.movie_detail = {"id": "movie-uuid", "downloaded": False}
        reconciled = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        stored = client.app.state.store.get_queue_item(item["id"])
        events = client.app.state.store.list_events(item["id"])
        retry = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        retried = client.app.state.store.get_queue_item(item["id"])

    assert submitted["status"] == "download_submitted"
    assert reconciled["status"] == "needs_release"
    assert stored["status"] == "needs_release"
    assert [candidate["result_id"] for candidate in stored["candidates"]] == ["release-one"]
    assert events[0]["event_type"] == "queue.download_not_grabbed"
    assert retry.status_code == 200
    assert retried["status"] == "download_submitted"
    assert fake.downloaded_movies == ["release-one", "release-one"]


def test_reconcile_keeps_submitted_item_with_active_grab(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]
        client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        # The fake marks the detail with active download activity on a grab.
        reconciled = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        # Status-string based activity also counts as an in-progress grab.
        fake.movie_detail = {"id": "movie-uuid", "status": "queued"}
        reconciled_again = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        events = client.app.state.store.list_events(item["id"])

    assert reconciled["status"] == "download_submitted"
    assert reconciled_again["status"] == "download_submitted"
    assert all(
        event["event_type"] != "queue.download_not_grabbed" for event in events
    )


def test_reconcile_imported_item_still_transitions_then_webhook_marks_available(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    fake = FakeMediaManager()
    app = create_app(
        settings=settings_for(
            tmp_path / "screenarr.db",
            ENABLE_DASHBOARD=True,
            ENABLE_ONSCREEN_WEBHOOK=True,
            **webhook_secret_setting(),
        ),
        config=BridgeConfig(profiles=[profile]),
        mediamanager_factory=lambda _settings: fake,
    )

    with TestClient(app) as client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]
        client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        fake.movie_detail = {"id": "movie-uuid", "downloaded": True}
        reconciled = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        body = (
            b'{"event":"library.scan.complete","status":"available",'
            b'"media_type":"movie","tmdb_id":550}'
        )
        timestamp = str(int(time.time()))
        webhook = client.post(
            "/integrations/onscreen/webhook",
            content=body,
            headers={
                "X-OnScreen-Timestamp": timestamp,
                "X-OnScreen-Signature": sign_webhook(TEST_WEBHOOK_SECRET, timestamp, body),
                "Content-Type": "application/json",
            },
        )
        stored = client.app.state.store.get_queue_item(item["id"])

    assert reconciled["status"] == "imported"
    assert webhook.status_code == 200
    assert stored["status"] == "available"


def test_show_reconcile_bounces_only_without_any_season_activity(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(
        tmp_path / "screenarr.db",
        profile,
        RECONCILE_GRACE_SECONDS=0,
    )

    with client:
        _response, item = post_media_and_first_queue_item(
            client,
            endpoint="/api/v3/series",
            payload=show_payload(),
        )
        candidate_id = item["candidates"][0]["id"]
        submitted = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        # Season 1 grab in progress: the item must stay submitted.
        fake.show_detail = {
            "id": "show-uuid",
            "seasons": [{"season_number": 1, "status": "downloading"}],
        }
        stays = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        # No grab activity for any requested season: bounce back for re-pick.
        fake.show_detail = {
            "id": "show-uuid",
            "seasons": [{"season_number": 2, "downloaded": True}],
        }
        bounced = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        events = client.app.state.store.list_events(item["id"])

    assert submitted["status"] == "download_submitted"
    assert stays["status"] == "download_submitted"
    assert bounced["status"] == "needs_release"
    assert events[0]["event_type"] == "queue.download_not_grabbed"


def test_recover_stale_claims_leaves_download_submitted_untouched(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "screenarr.db")
    store.init()
    item = seed_movie_item(
        store,
        status="download_submitted",
        mediamanager_id="movie-uuid",
    )

    recovered = store.recover_stale_claims(older_than_seconds=-1)

    assert recovered == 0
    assert store.get_queue_item(item["id"])["status"] == "download_submitted"


def test_settings_include_reconcile_grace_default(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "screenarr.db")
    custom = settings_for(tmp_path / "custom.db", RECONCILE_GRACE_SECONDS=0)

    assert settings.reconcile_grace_seconds == 900
    assert custom.reconcile_grace_seconds == 0


def test_reconcile_holds_fresh_submission_during_grace_period(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]
        submitted = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        # Negative snapshot right after the grab: MediaManager detail lags.
        fake.movie_detail = {"id": "movie-uuid", "downloaded": False}
        reconciled = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        events = client.app.state.store.list_events(item["id"])

    assert submitted["status"] == "download_submitted"
    assert reconciled["status"] == "download_submitted"
    # The grace hold records nothing: no bounce and no noop churn.
    assert [event["event_type"] for event in events] == [
        "queue.download_submitted",
        "queue.created",
    ]


def test_reconcile_bounces_never_grabbed_submission_after_grace_elapses(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    db_path = tmp_path / "screenarr.db"
    client, fake = make_flow_client(db_path, profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]
        client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        )
        fake.movie_detail = {"id": "movie-uuid", "downloaded": False}
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE queue_items SET updated_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00+00:00", item["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        reconciled = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        events = client.app.state.store.list_events(item["id"])

    assert reconciled["status"] == "needs_release"
    assert events[0]["event_type"] == "queue.download_not_grabbed"


def test_reconcile_never_bounces_download_unverified(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(
        tmp_path / "screenarr.db",
        profile,
        RECONCILE_GRACE_SECONDS=0,
    )

    with client:
        _response, item = post_media_and_first_queue_item(client)
        candidate_id = item["candidates"][0]["id"]
        fake.fail_next_download_timeout = True
        timed_out = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        # Unverified stays ambiguous even with a negative snapshot past grace.
        fake.movie_detail = {"id": "movie-uuid", "downloaded": False}
        reconciled = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        events = client.app.state.store.list_events(item["id"])

    assert timed_out["status"] == "download_unverified"
    assert reconciled["status"] == "download_unverified"
    assert all(
        event["event_type"] != "queue.download_not_grabbed" for event in events
    )


def test_show_reconcile_holds_fresh_submission_during_grace_period(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(
            client,
            endpoint="/api/v3/series",
            payload=show_payload(),
        )
        candidate_id = item["candidates"][0]["id"]
        submitted = client.post(
            f"/api/bridge/v1/queue/{item['id']}/download/{candidate_id}",
            headers={"X-Api-Key": "test"},
        ).json()
        # No activity for any requested season, but still inside the grace window.
        fake.show_detail = {
            "id": "show-uuid",
            "seasons": [{"season_number": 2, "downloaded": True}],
        }
        reconciled = client.post(
            f"/api/bridge/v1/queue/{item['id']}/reconcile",
            headers={"X-Api-Key": "test"},
        ).json()
        events = client.app.state.store.list_events(item["id"])

    assert submitted["status"] == "download_submitted"
    assert reconciled["status"] == "download_submitted"
    assert all(
        event["event_type"] != "queue.download_not_grabbed" for event in events
    )


def test_login_throttle_is_scoped_per_client_ip() -> None:
    now = [1000.0]
    throttle = LoginThrottle(max_attempts=2, lockout_seconds=60.0, clock=lambda: now[0])

    throttle.record_failure("10.0.0.1")
    throttle.record_failure("10.0.0.1")

    assert throttle.retry_after_seconds("10.0.0.1") > 0
    # A second client is unaffected by the first client's lockout.
    assert throttle.retry_after_seconds("10.0.0.2") == 0
    throttle.record_failure("10.0.0.2")
    assert throttle.retry_after_seconds("10.0.0.2") == 0
    throttle.record_success("10.0.0.2")
    assert throttle.retry_after_seconds("10.0.0.2") == 0
    assert throttle.retry_after_seconds("10.0.0.1") > 0
    now[0] += 61
    assert throttle.retry_after_seconds("10.0.0.1") == 0


def test_validate_static_config_warns_when_forwarded_headers_trusted(
    tmp_path: Path,
) -> None:
    trusted_issues = validate_static_config(
        BridgeConfig(),
        settings_for(tmp_path / "trusted.db", TRUST_FORWARDED_HEADERS=True),
    )
    default_issues = validate_static_config(
        BridgeConfig(),
        settings_for(tmp_path / "default.db"),
    )

    trusted_codes = {item["code"] for item in trusted_issues}
    default_codes = {item["code"] for item in default_issues}
    assert "settings.trust_forwarded_headers" in trusted_codes
    assert "settings.trust_forwarded_headers" not in default_codes


def test_manual_add_transport_failure_rolls_back_claim_and_stays_retryable(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        async def fail_add(_tmdb_id: int, language: str | None = None) -> dict[str, Any]:
            raise httpx.ConnectError("add connect failed")

        fake.add_movie = fail_add
        failed = client.post(
            "/api/v3/movie",
            json=movie_payload(),
            headers={"X-Api-Key": "test"},
        )
        item = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()[
            "items"
        ][0]
        # Transport failure rolled the claim back; a retry adds cleanly.
        del fake.add_movie
        retry = client.post(
            "/api/v3/movie",
            json=movie_payload(),
            headers={"X-Api-Key": "test"},
        )
        retried = client.get(
            "/api/bridge/v1/queue",
            headers={"X-Api-Key": "test"},
        ).json()["items"][0]

    assert failed.status_code == 502
    assert failed.json()["detail"] == "MediaManager request failed"
    assert item["status"] == "needs_release"
    assert item["mediamanager_id"] is None
    assert item["last_error"]["event_type"] == "queue.add_failed"
    assert retry.status_code == 200
    assert retried["status"] == "needs_release"
    assert retried["mediamanager_id"] == "movie-uuid"
    assert retried["candidates"][0]["result_id"] == "release-one"
    assert fake.added_movies == [550]


def test_manual_add_transport_failure_after_landed_add_reuses_existing(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        original_add = fake.add_movie
        landed = False

        async def flaky_add(tmdb_id: int, language: str | None = None) -> dict[str, Any]:
            nonlocal landed
            if not landed:
                landed = True
                await original_add(tmdb_id)
                raise httpx.ConnectError("response lost")
            # The real client gets a 409 here and looks up the existing entry
            # instead of adding a duplicate.
            return {"id": "movie-uuid", "external_id": tmdb_id, "library": "Default"}

        fake.add_movie = flaky_add
        first = client.post(
            "/api/v3/movie",
            json=movie_payload(),
            headers={"X-Api-Key": "test"},
        )
        second = client.post(
            "/api/v3/movie",
            json=movie_payload(),
            headers={"X-Api-Key": "test"},
        )
        item = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()[
            "items"
        ][0]

    assert first.status_code == 502
    assert second.status_code == 200
    assert item["status"] == "needs_release"
    assert item["mediamanager_id"] == "movie-uuid"
    assert fake.added_movies == [550]


def test_approval_add_transport_failure_rolls_back_to_pending_approval(
    tmp_path: Path,
) -> None:
    profile = BridgeProfile(id=101, name="Approval", mode=ReleaseMode.APPROVAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        _response, item = post_media_and_first_queue_item(client)

        async def fail_add(_tmdb_id: int, language: str | None = None) -> dict[str, Any]:
            raise httpx.ConnectError("add connect failed")

        fake.add_movie = fail_add
        failed = client.post(
            f"/api/bridge/v1/queue/{item['id']}/approve",
            headers={"X-Api-Key": "test"},
        )
        stored = client.app.state.store.get_queue_item(item["id"])

    assert failed.status_code == 502
    assert failed.json()["detail"] == "MediaManager request failed"
    assert stored["status"] == "pending_approval"
    assert stored["last_error"]["event_type"] == "queue.approval_failed"


def test_manual_add_race_keeps_winning_mediamanager_association(tmp_path: Path) -> None:
    profile = BridgeProfile(id=101, name="Manual", mode=ReleaseMode.MANUAL)
    client, fake = make_flow_client(tmp_path / "screenarr.db", profile)

    with client:
        store = client.app.state.store
        original_update = store.update_queue_item
        raced = False

        def racing_update(queue_id: str, **kwargs: Any) -> dict[str, Any] | None:
            nonlocal raced
            if (
                not raced
                and kwargs.get("from_status") == "download_claimed"
                and kwargs.get("mediamanager_id")
            ):
                raced = True
                # A competing worker completes the claim first.
                original_update(
                    queue_id,
                    status="needs_release",
                    mediamanager_id="winner-uuid",
                    from_status="download_claimed",
                )
            return original_update(queue_id, **kwargs)

        store.update_queue_item = racing_update
        response = client.post(
            "/api/v3/movie",
            json=movie_payload(),
            headers={"X-Api-Key": "test"},
        )
        stored = client.get("/api/bridge/v1/queue", headers={"X-Api-Key": "test"}).json()[
            "items"
        ][0]

    assert raced
    assert response.status_code == 200
    assert stored["status"] == "needs_release"
    assert stored["mediamanager_id"] == "winner-uuid"

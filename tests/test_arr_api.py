from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.config import BridgeConfig, BridgeProfile, RootFolder, Settings
from bridge.main import (
    create_app,
    parse_id_term,
    should_auto_download_series,
    should_set_library,
)
from bridge.mediamanager import (
    FOREGROUND_TIMEOUT_SECONDS,
    MediaManagerClient,
    MediaManagerError,
    choose_best_release,
)

READ_TIMEOUT_MESSAGE = "search timed out"
CONNECT_ERROR_MESSAGE = "connect failed"


def screenarr_db_path(tmp_path: Path) -> Path:
    return tmp_path / "screenarr.db"


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(
        settings=Settings(
            BRIDGE_API_KEY="test",
            MEDIAMANAGER_BASE_URL="http://mediamanager:8000",
            CONFIG_PATH="missing.yaml",
            SCREENARR_DATA_PATH=screenarr_db_path(tmp_path),
        ),
        config=BridgeConfig(
            profiles=[BridgeProfile(id=101, name="TRaSH: HD Bluray + WEB 1080p")],
            root_folders=[RootFolder(id=1, path="Default")],
        ),
    )
    return TestClient(app)


def make_dashboard_client(enabled: bool, tmp_path: Path) -> TestClient:
    app = create_app(
        settings=Settings(
            BRIDGE_API_KEY="test",
            MEDIAMANAGER_BASE_URL="http://mediamanager:8000",
            CONFIG_PATH="missing.yaml",
            ENABLE_DASHBOARD=enabled,
            SCREENARR_DATA_PATH=screenarr_db_path(tmp_path),
        ),
        config=BridgeConfig(
            profiles=[BridgeProfile(id=101, name="TRaSH: HD Bluray + WEB 1080p")],
            root_folders=[RootFolder(id=1, path="Default")],
        ),
    )
    return TestClient(app)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_mm_client(handler: httpx.MockTransport) -> MediaManagerClient:
    client = MediaManagerClient(
        Settings(
            BRIDGE_API_KEY="test",
            MEDIAMANAGER_BASE_URL="http://mediamanager:8000",
            MEDIAMANAGER_TOKEN="token",
            CONFIG_PATH="missing.yaml",
        )
    )
    client._client = httpx.AsyncClient(
        base_url="http://mediamanager:8000",
        transport=handler,
    )
    return client


@pytest.mark.anyio
async def test_mediamanager_foreground_timeout_caps_configured_value() -> None:
    capped = MediaManagerClient(
        Settings(
            BRIDGE_API_KEY="test",
            MEDIAMANAGER_BASE_URL="http://mediamanager:8000",
            MEDIAMANAGER_TOKEN="token",  # noqa: S106
            MEDIAMANAGER_TIMEOUT_SECONDS=120,
            CONFIG_PATH="missing.yaml",
        )
    )
    passthrough = MediaManagerClient(
        Settings(
            BRIDGE_API_KEY="test",
            MEDIAMANAGER_BASE_URL="http://mediamanager:8000",
            MEDIAMANAGER_TOKEN="token",  # noqa: S106
            MEDIAMANAGER_TIMEOUT_SECONDS=12,
            CONFIG_PATH="missing.yaml",
        )
    )
    try:
        assert capped._foreground_timeout.read == FOREGROUND_TIMEOUT_SECONDS
        assert capped._mediamanager_timeout.read == 120.0
        assert passthrough._foreground_timeout.read == 12.0
        assert passthrough._mediamanager_timeout.read == 12.0
    finally:
        await capped.close()
        await passthrough.close()


@pytest.mark.anyio
async def test_mediamanager_transport_errors_are_wrapped_for_add_and_search() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/movies/movie-uuid/torrents":
            raise httpx.ReadTimeout(READ_TIMEOUT_MESSAGE)
        raise httpx.ConnectError(CONNECT_ERROR_MESSAGE)

    client = make_mm_client(httpx.MockTransport(handler))
    try:
        with pytest.raises(MediaManagerError, match="POST /api/v1/movies request failed"):
            await client.add_movie(550)
        with pytest.raises(
            MediaManagerError,
            match="GET /api/v1/movies/movie-uuid/torrents request failed",
        ):
            await client.search_movie_torrents("movie-uuid")
    finally:
        await client.close()


@pytest.mark.anyio
async def test_mediamanager_http_error_does_not_include_response_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text="upstream token=super-secret signed_url=https://example.invalid",
        )

    client = make_mm_client(httpx.MockTransport(handler))
    try:
        with pytest.raises(MediaManagerError) as exc_info:
            await client.add_movie(550)
    finally:
        await client.close()

    message = str(exc_info.value)
    assert message == "MediaManager POST /api/v1/movies failed: 500"
    assert "super-secret" not in message
    assert "signed_url" not in message


@pytest.mark.anyio
async def test_mediamanager_detail_and_download_transport_errors_are_preserved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1/movies/"):
            raise httpx.ReadTimeout("download timed out")
        if request.url.path == "/api/v1/tv/shows/show-uuid":
            raise httpx.ConnectError("detail connect failed")
        if request.url.path == "/api/v1/tv/torrents":
            raise httpx.ConnectError("download connect failed")
        return httpx.Response(404)

    client = make_mm_client(httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.TimeoutException):
            await client.get_movie("movie-uuid")
        with pytest.raises(httpx.TransportError):
            await client.get_show("show-uuid")
        with pytest.raises(httpx.TimeoutException):
            await client.download_movie_torrent("movie-uuid", "release-one")
        with pytest.raises(httpx.TransportError):
            await client.download_show_torrent("show-uuid", "release-one")
    finally:
        await client.close()


@pytest.mark.anyio
async def test_mediamanager_download_wraps_login_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/jwt/login":
            raise httpx.ConnectError("login connect failed")
        raise AssertionError("download endpoint should not be called")

    client = MediaManagerClient(
        Settings(
            BRIDGE_API_KEY="test",
            MEDIAMANAGER_BASE_URL="http://mediamanager:8000",
            MEDIAMANAGER_USERNAME="user",
            MEDIAMANAGER_PASSWORD="password",  # noqa: S106
            CONFIG_PATH="missing.yaml",
        )
    )
    client._client = httpx.AsyncClient(
        base_url="http://mediamanager:8000",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(MediaManagerError, match="MediaManager login request failed"):
            await client.download_movie_torrent("movie-uuid", "release-one")
    finally:
        await client.close()


def test_auth_required(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/api/v3/system/status")
    assert response.status_code == 401


def test_auth_rejects_wrong_key(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/api/v3/system/status", headers={"X-Api-Key": "wrong"})
    assert response.status_code == 401


def test_system_status(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/api/v3/system/status", headers={"X-Api-Key": "test"})
    assert response.status_code == 200
    assert response.json()["appName"] == "Screenarr"


def test_quality_profiles_use_trash_names(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/api/v3/qualityprofile", headers={"X-Api-Key": "test"})
    assert response.status_code == 200
    assert response.json() == [{"id": 101, "name": "TRaSH: HD Bluray + WEB 1080p"}]


def test_movie_lookup_accepts_tmdb_term(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get(
        "/api/v3/movie/lookup",
        params={"term": "tmdb:550"},
        headers={"X-Api-Key": "test"},
    )
    assert response.status_code == 200
    assert response.json()[0]["tmdbId"] == 550


def test_movie_lookup_accepts_tmdb_endpoint(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get(
        "/api/v3/movie/lookup/tmdb",
        params={"tmdbId": 550},
        headers={"X-Api-Key": "test"},
    )
    assert response.status_code == 200
    assert response.json()[0]["tmdbId"] == 550


def test_dashboard_disabled_by_default(tmp_path: Path) -> None:
    response = make_client(tmp_path).get("/dashboard")
    assert response.status_code == 404


def test_dashboard_enabled(tmp_path: Path) -> None:
    response = make_dashboard_client(True, tmp_path).get(
        "/dashboard",
        headers={"X-Api-Key": "test"},
    )
    assert response.status_code == 200
    assert "Screenarr" in response.text
    assert "TRaSH: HD Bluray + WEB 1080p" in response.text


def test_dashboard_enabled_requires_auth(tmp_path: Path) -> None:
    client = make_dashboard_client(True, tmp_path)
    response = client.get("/dashboard")
    query_auth = client.get("/dashboard", params={"apikey": "test"})

    assert response.status_code == 401
    assert query_auth.status_code == 401


def test_tvdb_series_lookup_intentionally_misses(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get(
        "/api/v3/series/lookup",
        params={"term": "tvdb:123"},
        headers={"X-Api-Key": "test"},
    )
    assert response.status_code == 200
    assert response.json() == []


def test_tmdb_series_lookup_does_not_fabricate_monitored_seasons(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get(
        "/api/v3/series/lookup",
        params={"term": "tmdb:123"},
        headers={"X-Api-Key": "test"},
    )
    assert response.status_code == 200
    assert response.json()[0]["seasons"] == []


def test_parse_id_term() -> None:
    assert parse_id_term("tmdb:123", "tmdb") == 123
    assert parse_id_term("tvdb:123", "tmdb") is None
    assert parse_id_term("nope", "tmdb") is None


def test_full_series_requires_opt_in() -> None:
    settings = Settings(
        BRIDGE_API_KEY="test",
        MEDIAMANAGER_BASE_URL="http://mediamanager:8000",
        CONFIG_PATH="missing.yaml",
        MAX_AUTO_TV_SEASONS=3,
        AUTO_DOWNLOAD_FULL_SERIES=False,
    )
    assert should_auto_download_series(settings, [1, 2, 3])
    assert not should_auto_download_series(settings, [1, 2, 3, 4])


def test_full_series_allows_explicit_opt_in() -> None:
    settings = Settings(
        BRIDGE_API_KEY="test",
        MEDIAMANAGER_BASE_URL="http://mediamanager:8000",
        CONFIG_PATH="missing.yaml",
        MAX_AUTO_TV_SEASONS=3,
        AUTO_DOWNLOAD_FULL_SERIES=True,
    )
    assert should_auto_download_series(settings, [1, 2, 3, 4])


def test_should_set_library_only_when_different() -> None:
    assert should_set_library({"library": "Default"}, "4K")
    assert not should_set_library({"library": "Default"}, "Default")
    assert not should_set_library({"library": "Default"}, "")


def test_choose_best_release_uses_public_indexer_result_id() -> None:
    choice = choose_best_release(
        [
            {
                "id": "internal-id",
                "public_indexer_result_id": "public-id",
                "title": "Release",
                "score": 10,
            }
        ],
        10,
    )
    assert choice is not None
    assert choice.id == "public-id"


def test_choose_best_release_handles_empty_trim() -> None:
    assert choose_best_release([{"id": "one"}], 0) is None


def assert_foreground_timeout(request: httpx.Request) -> None:
    assert request.extensions["timeout"]["read"] == FOREGROUND_TIMEOUT_SECONDS


def assert_mediamanager_timeout(request: httpx.Request, seconds: float = 120.0) -> None:
    assert request.extensions["timeout"]["read"] == seconds


@pytest.mark.anyio
async def test_mediamanager_movie_flow_requests_expected_endpoints() -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        assert request.headers["Authorization"] == "Bearer token"
        if request.method == "POST" and request.url.path == "/api/v1/movies":
            assert request.url.params["movie_id"] == "550"
            assert_foreground_timeout(request)
            return httpx.Response(
                201,
                json={"id": "movie-uuid", "external_id": 550, "library": "Default"},
            )
        if request.method == "GET" and request.url.path == "/api/v1/movies/movie-uuid/torrents":
            assert_foreground_timeout(request)
            return httpx.Response(
                200,
                json=[{"public_indexer_result_id": "public-id", "title": "Release", "score": 10}],
            )
        if request.method == "GET" and request.url.path == "/api/v1/movies/movie-uuid":
            assert_mediamanager_timeout(request)
            return httpx.Response(200, json={"id": "movie-uuid", "downloaded": True})
        if request.method == "POST" and request.url.path == "/api/v1/movies/movie-uuid/torrents":
            assert request.url.params["public_indexer_result_id"] == "public-id"
            assert_foreground_timeout(request)
            return httpx.Response(201, json={"id": "torrent-id"})
        return httpx.Response(404)

    client = make_mm_client(httpx.MockTransport(handler))
    try:
        movie = await client.add_movie(550)
        releases = await client.search_movie_torrents(str(movie["id"]))
        choice = choose_best_release(releases, 10)
        assert choice is not None
        await client.download_movie_torrent(str(movie["id"]), choice.id)
        assert await client.get_movie(str(movie["id"])) == {
            "id": "movie-uuid",
            "downloaded": True,
        }
    finally:
        await client.close()

    assert seen == [
        ("POST", "/api/v1/movies", "movie_id=550"),
        ("GET", "/api/v1/movies/movie-uuid/torrents", ""),
        ("POST", "/api/v1/movies/movie-uuid/torrents", "public_indexer_result_id=public-id"),
        ("GET", "/api/v1/movies/movie-uuid", ""),
    ]


@pytest.mark.anyio
async def test_mediamanager_show_flow_requests_expected_endpoints() -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        assert request.headers["Authorization"] == "Bearer token"
        if request.method == "POST" and request.url.path == "/api/v1/tv/shows":
            assert request.url.params["show_id"] == "123"
            assert_foreground_timeout(request)
            return httpx.Response(
                201,
                json={"id": "show-uuid", "external_id": 123, "library": "Default"},
            )
        if request.method == "GET" and request.url.path == "/api/v1/tv/torrents":
            assert request.url.params["show_id"] == "show-uuid"
            assert request.url.params["season_number"] == "1"
            assert_foreground_timeout(request)
            return httpx.Response(
                200,
                json=[{"public_indexer_result_id": "public-id", "title": "Season", "score": 10}],
            )
        if request.method == "GET" and request.url.path == "/api/v1/tv/shows/show-uuid":
            assert_mediamanager_timeout(request)
            return httpx.Response(200, json={"id": "show-uuid", "downloaded": True})
        if request.method == "POST" and request.url.path == "/api/v1/tv/torrents":
            assert request.url.params["show_id"] == "show-uuid"
            assert request.url.params["public_indexer_result_id"] == "public-id"
            assert_foreground_timeout(request)
            return httpx.Response(200, json={"id": "torrent-id"})
        return httpx.Response(404)

    client = make_mm_client(httpx.MockTransport(handler))
    try:
        show = await client.add_show(123)
        releases = await client.search_show_torrents(str(show["id"]), 1)
        choice = choose_best_release(releases, 10)
        assert choice is not None
        await client.download_show_torrent(str(show["id"]), choice.id)
        assert await client.get_show(str(show["id"])) == {
            "id": "show-uuid",
            "downloaded": True,
        }
    finally:
        await client.close()

    assert seen == [
        ("POST", "/api/v1/tv/shows", "show_id=123"),
        ("GET", "/api/v1/tv/torrents", "show_id=show-uuid&season_number=1"),
        (
            "POST",
            "/api/v1/tv/torrents",
            "public_indexer_result_id=public-id&show_id=show-uuid",
        ),
        ("GET", "/api/v1/tv/shows/show-uuid", ""),
    ]


@pytest.mark.anyio
async def test_mediamanager_library_endpoints_request_expected_paths() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer token"
        if request.method == "GET" and request.url.path == "/api/v1/movies/libraries":
            assert_foreground_timeout(request)
            return httpx.Response(200, json=[{"name": "Movies"}])
        if request.method == "GET" and request.url.path == "/api/v1/tv/shows/libraries":
            assert_foreground_timeout(request)
            return httpx.Response(200, json=[{"name": "TV"}])
        if request.method == "POST" and request.url.path == "/api/v1/movies/movie-uuid/library":
            assert_foreground_timeout(request)
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/api/v1/tv/shows/show-uuid/library":
            assert_foreground_timeout(request)
            return httpx.Response(204)
        return httpx.Response(404)

    client = make_mm_client(httpx.MockTransport(handler))
    try:
        assert await client.movie_libraries() == [{"name": "Movies"}]
        assert await client.show_libraries() == [{"name": "TV"}]
        await client.set_movie_library("movie-uuid", "Movies")
        await client.set_show_library("show-uuid", "TV")
    finally:
        await client.close()

    assert seen == [
        ("GET", "/api/v1/movies/libraries"),
        ("GET", "/api/v1/tv/shows/libraries"),
        ("POST", "/api/v1/movies/movie-uuid/library"),
        ("POST", "/api/v1/tv/shows/show-uuid/library"),
    ]


@pytest.mark.anyio
async def test_mediamanager_conflict_fallback_lookup_uses_foreground_timeout() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer token"
        assert_foreground_timeout(request)
        if request.method == "POST" and request.url.path == "/api/v1/movies":
            return httpx.Response(409)
        if request.method == "GET" and request.url.path == "/api/v1/movies":
            return httpx.Response(200, json=[{"id": "movie-uuid", "external_id": 550}])
        if request.method == "POST" and request.url.path == "/api/v1/tv/shows":
            return httpx.Response(409)
        if request.method == "GET" and request.url.path == "/api/v1/tv/shows":
            return httpx.Response(200, json=[{"id": "show-uuid", "external_id": 123}])
        return httpx.Response(404)

    client = make_mm_client(httpx.MockTransport(handler))
    try:
        assert await client.add_movie(550) == {"id": "movie-uuid", "external_id": 550}
        assert await client.add_show(123) == {"id": "show-uuid", "external_id": 123}
    finally:
        await client.close()

    assert seen == [
        ("POST", "/api/v1/movies"),
        ("GET", "/api/v1/movies"),
        ("POST", "/api/v1/tv/shows"),
        ("GET", "/api/v1/tv/shows"),
    ]


@pytest.mark.anyio
async def test_mediamanager_library_endpoint_errors_are_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token"
        return httpx.Response(500, text="boom")

    client = make_mm_client(httpx.MockTransport(handler))
    try:
        with pytest.raises(
            MediaManagerError,
            match=r"GET /api/v1/movies/libraries failed: 500",
        ) as exc_info:
            await client.movie_libraries()
        assert "boom" not in str(exc_info.value)
    finally:
        await client.close()


@pytest.mark.anyio
async def test_mediamanager_refreshes_login_token_once_on_401() -> None:
    login_count = 0
    search_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count, search_count
        if request.method == "POST" and request.url.path == "/api/v1/auth/jwt/login":
            assert_foreground_timeout(request)
            login_count += 1
            return httpx.Response(200, json={"access_token": f"token-{login_count}"})
        if request.method == "GET" and request.url.path == "/api/v1/movies/movie-uuid/torrents":
            search_count += 1
            if request.headers["Authorization"] == "Bearer token-1":
                return httpx.Response(401, json={"detail": "expired"})
            assert request.headers["Authorization"] == "Bearer token-2"
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    client = MediaManagerClient(
        Settings(
            BRIDGE_API_KEY="test",
            MEDIAMANAGER_BASE_URL="http://mediamanager:8000",
            MEDIAMANAGER_USERNAME="user",
            MEDIAMANAGER_PASSWORD="pass",
            CONFIG_PATH="missing.yaml",
        )
    )
    client._client = httpx.AsyncClient(
        base_url="http://mediamanager:8000",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.search_movie_torrents("movie-uuid") == []
    finally:
        await client.close()

    assert login_count == 2
    assert search_count == 2

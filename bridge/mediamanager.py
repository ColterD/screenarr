from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel

from bridge.config import Settings

log = logging.getLogger(__name__)
FOREGROUND_TIMEOUT_SECONDS = 30.0


class MediaManagerError(RuntimeError):
    pass


class MediaManagerAuthError(MediaManagerError):
    pass


class MediaManagerClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = str(settings.mediamanager_base_url).rstrip("/")
        self._static_token = bool(settings.mediamanager_token.get_secret_value())
        self._token = settings.mediamanager_token.get_secret_value()
        foreground_timeout = min(settings.mediamanager_timeout_seconds, FOREGROUND_TIMEOUT_SECONDS)
        self._foreground_timeout = httpx.Timeout(foreground_timeout)
        self._mediamanager_timeout = httpx.Timeout(settings.mediamanager_timeout_seconds)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._mediamanager_timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _headers(self) -> dict[str, str]:
        token = self._token or await self._login()
        return {"Authorization": f"Bearer {token}"}

    async def _login(self) -> str:
        username = self._settings.mediamanager_username.get_secret_value()
        password = self._settings.mediamanager_password.get_secret_value()
        if not username or not password:
            raise MediaManagerAuthError(
                "set MEDIAMANAGER_TOKEN or MEDIAMANAGER_USERNAME/MEDIAMANAGER_PASSWORD"
            )
        try:
            response = await self._client.post(
                "/api/v1/auth/jwt/login",
                data={
                    "username": username,
                    "password": password,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._foreground_timeout,
            )
        except httpx.TransportError as exc:
            raise MediaManagerAuthError(  # noqa: TRY003 - include auth boundary context.
                f"MediaManager login request failed: {exc.__class__.__name__}"
            ) from exc
        if response.status_code >= 400:
            raise MediaManagerAuthError(f"MediaManager login failed: {response.status_code}")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise MediaManagerAuthError("MediaManager login did not return access_token")
        self._token = token
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        preserve_transport_error: bool = False,
        **kwargs: Any,  # noqa: ANN401 - httpx request kwargs are heterogeneous.
    ) -> Any:  # noqa: ANN401 - MediaManager endpoints return heterogeneous JSON.
        headers = kwargs.pop("headers", {})
        response = await self._authenticated_request(
            method,
            path,
            headers,
            kwargs,
            preserve_transport_error=preserve_transport_error,
        )
        if response.status_code == 401 and self._can_refresh_token():
            self._token = ""
            response = await self._authenticated_request(
                method,
                path,
                headers,
                kwargs,
                preserve_transport_error=preserve_transport_error,
            )
        if response.status_code == 409:
            return await self._handle_existing(method, path)
        if response.status_code >= 400:
            raise MediaManagerError(  # noqa: TRY003 - include sanitized upstream context.
                f"MediaManager {method} {path} failed: {response.status_code}"
            )
        if response.status_code == 204:
            return None
        return response.json()

    async def _authenticated_request(
        self,
        method: str,
        path: str,
        base_headers: dict[str, str],
        kwargs: dict[str, Any],
        *,
        preserve_transport_error: bool,
    ) -> httpx.Response:
        headers = dict(base_headers)
        headers.update(await self._headers())
        try:
            return await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            if preserve_transport_error:
                raise
            raise MediaManagerError(  # noqa: TRY003 - include endpoint context at boundary.
                f"MediaManager {method} {path} request failed: {exc.__class__.__name__}"
            ) from exc

    def _can_refresh_token(self) -> bool:
        return (
            not self._static_token
            and bool(self._settings.mediamanager_username.get_secret_value())
            and bool(self._settings.mediamanager_password.get_secret_value())
        )

    async def _handle_existing(self, method: str, path: str) -> Any:
        # MediaManager returns 409 when a title is already managed. The bridge
        # treats exact add-title conflicts as success by looking up the entry.
        if method == "POST" and path == "/api/v1/movies":
            return None
        if method == "POST" and path == "/api/v1/tv/shows":
            return None
        raise MediaManagerError(f"MediaManager {method} {path} failed: 409 conflict")

    async def add_movie(self, tmdb_id: int, language: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"movie_id": tmdb_id}
        if language:
            params["language"] = language
        payload = await self._request(
            "POST",
            "/api/v1/movies",
            params=params,
            timeout=self._foreground_timeout,
        )
        if payload is None:
            existing = await self.find_movie_by_external_id(tmdb_id)
            if existing:
                return existing
            raise MediaManagerError(f"movie already exists but could not be found: {tmdb_id}")
        return payload

    async def set_movie_library(self, movie_uuid: str, library: str) -> None:
        await self._request(
            "POST",
            f"/api/v1/movies/{movie_uuid}/library",
            params={"library": library},
            timeout=self._foreground_timeout,
        )

    async def find_movie_by_external_id(self, tmdb_id: int) -> dict[str, Any] | None:
        movies = await self._request(
            "GET",
            "/api/v1/movies",
            timeout=self._foreground_timeout,
        )
        for movie in movies:
            if movie.get("external_id") == tmdb_id:
                return movie
        return None

    async def get_movie(self, movie_uuid: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/movies/{movie_uuid}",
            timeout=self._mediamanager_timeout,
            preserve_transport_error=True,
        )

    async def movie_libraries(self) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            "/api/v1/movies/libraries",
            timeout=self._foreground_timeout,
        )

    async def search_movie_torrents(self, movie_uuid: str) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            f"/api/v1/movies/{movie_uuid}/torrents",
            timeout=self._foreground_timeout,
        )

    async def download_movie_torrent(self, movie_uuid: str, result_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/movies/{movie_uuid}/torrents",
            params={"public_indexer_result_id": result_id},
            timeout=self._foreground_timeout,
            preserve_transport_error=True,
        )

    async def add_show(self, tmdb_id: int, language: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"show_id": tmdb_id}
        if language:
            params["language"] = language
        payload = await self._request(
            "POST",
            "/api/v1/tv/shows",
            params=params,
            timeout=self._foreground_timeout,
        )
        if payload is None:
            existing = await self.find_show_by_external_id(tmdb_id)
            if existing:
                return existing
            raise MediaManagerError(f"show already exists but could not be found: {tmdb_id}")
        return payload

    async def set_show_library(self, show_uuid: str, library: str) -> None:
        await self._request(
            "POST",
            f"/api/v1/tv/shows/{show_uuid}/library",
            params={"library": library},
            timeout=self._foreground_timeout,
        )

    async def find_show_by_external_id(self, tmdb_id: int) -> dict[str, Any] | None:
        shows = await self._request(
            "GET",
            "/api/v1/tv/shows",
            timeout=self._foreground_timeout,
        )
        for show in shows:
            if show.get("external_id") == tmdb_id:
                return show
        return None

    async def get_show(self, show_uuid: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/tv/shows/{show_uuid}",
            timeout=self._mediamanager_timeout,
            preserve_transport_error=True,
        )

    async def show_libraries(self) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            "/api/v1/tv/shows/libraries",
            timeout=self._foreground_timeout,
        )

    async def search_show_torrents(
        self, show_uuid: str, season_number: int
    ) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            "/api/v1/tv/torrents",
            params={"show_id": show_uuid, "season_number": season_number},
            timeout=self._foreground_timeout,
        )

    async def download_show_torrent(self, show_uuid: str, result_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/tv/torrents",
            params={"public_indexer_result_id": result_id, "show_id": show_uuid},
            timeout=self._foreground_timeout,
            preserve_transport_error=True,
        )


class ReleaseChoice(BaseModel):
    id: str
    title: str
    score: int = 0
    seeders: int = 0
    size: int = 0
    usenet: bool = False

def choose_best_release(results: list[dict[str, Any]], max_results: int) -> ReleaseChoice | None:
    if not results or max_results <= 0:
        return None
    trimmed = results[:max_results]

    def sort_key(item: dict[str, Any]) -> tuple[int, int, int, int]:
        return (
            int(item.get("score") or 0),
            int(item.get("seeders") or 0),
            1 if item.get("usenet") else 0,
            -int(item.get("size") or 0),
        )

    best = max(trimmed, key=sort_key)
    result_id = best.get("public_indexer_result_id") or best.get("id")
    if result_id is None:
        raise MediaManagerError("torrent result did not include an id")
    return ReleaseChoice(
        id=str(result_id),
        title=str(best.get("title") or best.get("torrent_title") or result_id),
        score=int(best.get("score") or 0),
        seeders=int(best.get("seeders") or 0),
        size=int(best.get("size") or 0),
        usenet=bool(best.get("usenet") or False),
    )

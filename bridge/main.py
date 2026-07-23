from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from bridge import __version__
from bridge.arr_models import (
    AddMovieRequest,
    AddMovieResponse,
    AddSeriesRequest,
    AddSeriesResponse,
    MovieLookup,
    QualityProfile,
    RootFolderResponse,
    SeriesLookup,
    SeriesSeason,
    SystemStatus,
    TagResponse,
)
from bridge.config import (
    BridgeConfig,
    BridgeProfile,
    MediaType,
    ReleaseMode,
    Settings,
    load_bridge_config,
)
from bridge.dashboard import dashboard_html, dashboard_login_html, events_html
from bridge.mediamanager import (
    MediaManagerClient,
    MediaManagerError,
    ReleaseChoice,
    choose_best_release,
)
from bridge.security import (
    LoginThrottle,
    sign_dashboard_session,
    verify_dashboard_session,
    verify_onscreen_signature,
)
from bridge.store import DOWNLOADABLE_QUEUE_STATUSES, TERMINAL_QUEUE_STATUSES, BridgeStore
from bridge.validation import issue, validate_static_config

log = logging.getLogger(__name__)
MAX_WEBHOOK_BODY_BYTES = 64 * 1024
MAX_DASHBOARD_LOGIN_BODY_BYTES = 8 * 1024
# Dashboard pages render server-side HTML with an inline <style> block and no
# JavaScript, so the CSP allows inline styles only.
DASHBOARD_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
MEDIAMANAGER_DETAIL_FAILURE = "MediaManager detail request failed"
MEDIAMANAGER_REQUEST_FAILURE = "MediaManager request failed"
MEDIAMANAGER_CANDIDATE_REFRESH_FAILURE = "MediaManager candidate refresh failed"
MEDIAMANAGER_DOWNLOAD_FAILURE = "MediaManager download failed"
MAX_BULK_RECONCILE_ITEMS = 25
MAX_BULK_RECONCILE_CONCURRENCY = 5
MAX_BULK_RECONCILE_ITEM_SECONDS = 30.0
DATABASE_LOCK_MARKERS = (
    "database is locked",
    "database table is locked",
    "database is busy",
    "database busy",
    "busy timeout",
    "sqlite_busy",
)


class ReconcileFailureRecorded(MediaManagerError):
    pass


@dataclass(frozen=True, slots=True)
class AutoDownloadAmbiguity:
    media_type: MediaType
    external_id: int
    title: str
    profile: BridgeProfile
    mediamanager_id: str
    payload: dict[str, Any]
    choice: ReleaseChoice
    message: str
    seasons: list[int] | None = None
    season_number: int = 0


APPROVAL_ACCEPTED_STATUSES = {
    "pending_approval",
    "approval_claimed",
    "needs_release",
    "download_claimed",
    "download_submitted",
    "download_unverified",
    "download_failed",
    "imported",
    "available",
}
MANUAL_ACCEPTED_STATUSES = {
    "needs_release",
    "download_claimed",
    "download_submitted",
    "download_unverified",
    "download_failed",
    "imported",
    "available",
}
RECONCILE_QUEUE_STATUSES = {"download_submitted", "download_unverified"}
AVAILABLE_WEBHOOK_STATUSES = {"imported"}


def create_app(
    settings: Settings | None = None,
    config: BridgeConfig | None = None,
    mediamanager_factory: Callable[[Settings], Any] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    config = config or load_bridge_config(settings.config_path)
    store = BridgeStore(settings.screenarr_data_path)
    store.init()
    media_manager = (
        mediamanager_factory(settings)
        if mediamanager_factory is not None
        else MediaManagerClient(settings)
    )
    dashboard_session_secret = resolve_dashboard_session_secret(settings, store)
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.init()
        await store_call(store.recover_stale_claims)
        app.state.store = store
        app.state.mm = media_manager
        recovery_task = asyncio.create_task(recover_stale_claims_loop(store))
        reconcile_task: asyncio.Task[None] | None = None
        if settings.enable_mediamanager_reconcile:
            reconcile_task = asyncio.create_task(
                mediamanager_reconcile_loop(
                    store,
                    media_manager,
                    interval_seconds=settings.mediamanager_reconcile_interval_seconds,
                    grace_seconds=settings.reconcile_grace_seconds,
                )
            )
        try:
            yield
        finally:
            recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await recovery_task
            if reconcile_task is not None:
                reconcile_task.cancel()
                with suppress(asyncio.CancelledError):
                    await reconcile_task
            close = getattr(app.state.mm, "close", None)
            if close is not None:
                await close()

    app = FastAPI(
        title="Screenarr",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.bridge_config = config
    app.state.store = store
    app.state.mm = media_manager
    app.state.dashboard_session_secret = dashboard_session_secret
    app.state.login_throttle = LoginThrottle()

    @app.middleware("http")
    async def dashboard_security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if is_dashboard_path(request.url.path):
            response.headers["Cache-Control"] = "no-store"
            for header, value in DASHBOARD_SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
        return response

    async def require_api_key(
        request: Request,
        x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
        apikey: Annotated[str | None, Query(alias="apikey")] = None,
    ) -> None:
        expected = request.app.state.settings.bridge_api_key.get_secret_value()
        if not expected:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "bridge API key is not configured",
        )
        supplied = x_api_key or apikey
        if supplied is None or not constant_time_equal(supplied, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")

    Auth = Depends(require_api_key)

    async def require_header_api_key(
        request: Request,
        x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    ) -> None:
        await require_api_key(request, x_api_key, None)

    BridgeAuth = Depends(require_header_api_key)

    async def require_dashboard_auth(
        request: Request,
        x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
        apikey: Annotated[str | None, Query(alias="apikey")] = None,
        screenarr_session: Annotated[str | None, Cookie(alias="screenarr_session")] = None,
    ) -> None:
        if apikey:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "dashboard query API key auth is not allowed",
            )
        if x_api_key:
            await require_api_key(request, x_api_key, None)
            return
        if verify_dashboard_session(request.app.state.dashboard_session_secret, screenarr_session):
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid dashboard session")

    async def require_enabled_dashboard(
        request: Request,
        x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
        apikey: Annotated[str | None, Query(alias="apikey")] = None,
        screenarr_session: Annotated[str | None, Cookie(alias="screenarr_session")] = None,
    ) -> None:
        settings: Settings = request.app.state.settings
        if not settings.enable_dashboard:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "dashboard disabled")
        await require_dashboard_auth(request, x_api_key, apikey, screenarr_session)

    async def require_dashboard_csrf(request: Request) -> None:
        if not same_origin_dashboard_post(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid dashboard CSRF origin")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        _dashboard_access: None = Depends(require_enabled_dashboard),
    ) -> HTMLResponse:
        settings: Settings = request.app.state.settings
        cfg: BridgeConfig = request.app.state.bridge_config
        store: BridgeStore = request.app.state.store
        validation_issues = await store_call(validate_static_config, cfg, settings)
        queue_items = await store_call(store.list_queue_items)
        return HTMLResponse(
            dashboard_html(settings, cfg, queue_items, validation_issues)
        )

    @app.get("/dashboard/queue/{queue_id}/events", response_class=HTMLResponse)
    async def dashboard_queue_events(
        request: Request,
        queue_id: str,
        _dashboard_access: None = Depends(require_enabled_dashboard),
    ) -> HTMLResponse:
        store: BridgeStore = request.app.state.store
        try:
            events = await store_call(store.list_events, queue_id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "queue item not found") from exc
        return HTMLResponse(events_html(queue_id, events))

    @app.post("/dashboard/queue/{queue_id}/refresh-candidates", response_model=None)
    async def dashboard_refresh_candidates(
        request: Request,
        queue_id: str,
        _dashboard_access: None = Depends(require_enabled_dashboard),
    ) -> RedirectResponse:
        await require_dashboard_csrf(request)
        await refresh_queue_candidates_action(request, queue_id)
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/dashboard/queue/{queue_id}/reconcile", response_model=None)
    async def dashboard_reconcile_queue_item(
        request: Request,
        queue_id: str,
        _dashboard_access: None = Depends(require_enabled_dashboard),
    ) -> RedirectResponse:
        await require_dashboard_csrf(request)
        await reconcile_queue_item_action(request, queue_id)
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/dashboard/queue/{queue_id}/download/{candidate_id}", response_model=None)
    async def dashboard_download_candidate(
        request: Request,
        queue_id: str,
        candidate_id: str,
        _dashboard_access: None = Depends(require_enabled_dashboard),
    ) -> RedirectResponse:
        await require_dashboard_csrf(request)
        await download_queue_candidate_action(request, queue_id, candidate_id)
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/dashboard/login", response_class=HTMLResponse)
    async def dashboard_login_page(request: Request) -> HTMLResponse:
        settings: Settings = request.app.state.settings
        if not settings.enable_dashboard:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "dashboard disabled")
        return HTMLResponse(dashboard_login_html())

    @app.post("/dashboard/login", response_model=None)
    async def dashboard_login(
        request: Request,
        response: Response,
    ) -> dict[str, str] | RedirectResponse:
        settings: Settings = request.app.state.settings
        if not settings.enable_dashboard:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "dashboard disabled")
        throttle: LoginThrottle = request.app.state.login_throttle
        client_ip = request.client.host if request.client else "unknown"
        retry_after = throttle.retry_after_seconds(client_ip)
        if retry_after:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too many failed login attempts; try again later",
                headers={"Retry-After": str(retry_after)},
            )
        expected = settings.bridge_api_key.get_secret_value()
        supplied, browser_form = await parse_dashboard_login(request)
        if not expected or not constant_time_equal(supplied, expected):
            throttle.record_failure(client_ip)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
        throttle.record_success(client_ip)
        target_response: Response
        if browser_form:
            target_response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        else:
            target_response = response
        target_response.set_cookie(
            "screenarr_session",
            sign_dashboard_session(
                request.app.state.dashboard_session_secret,
                settings.dashboard_session_ttl_minutes,
            ),
            max_age=settings.dashboard_session_ttl_minutes * 60,
            path="/dashboard",
            httponly=True,
            secure=dashboard_cookie_secure(request),
            samesite="lax",
        )
        if browser_form:
            return target_response
        return {"status": "ok"}

    @app.get("/api/v3/system/status", dependencies=[Auth])
    async def system_status() -> SystemStatus:
        return SystemStatus(version=__version__)

    @app.get("/api/v3/qualityprofile", dependencies=[Auth])
    async def quality_profiles(request: Request) -> list[QualityProfile]:
        cfg: BridgeConfig = request.app.state.bridge_config
        return [QualityProfile(id=p.id, name=p.name) for p in cfg.profiles]

    @app.get("/api/v3/rootfolder", dependencies=[Auth])
    async def root_folders(request: Request) -> list[RootFolderResponse]:
        cfg: BridgeConfig = request.app.state.bridge_config
        return [
            RootFolderResponse(id=f.id, path=f.path, freeSpace=f.free_space)
            for f in cfg.root_folders
        ]

    @app.get("/api/v3/tag", dependencies=[Auth])
    async def tags(request: Request) -> list[TagResponse]:
        cfg: BridgeConfig = request.app.state.bridge_config
        return [TagResponse(id=t.id, label=t.label) for t in cfg.tags]

    @app.get("/api/bridge/v1/queue", dependencies=[BridgeAuth])
    async def bridge_queue(request: Request) -> dict[str, list[dict[str, Any]]]:
        store: BridgeStore = request.app.state.store
        return {"items": await store_call(store.list_queue_items)}

    @app.post("/api/bridge/v1/queue/reconcile", dependencies=[BridgeAuth])
    async def reconcile_bridge_queue(request: Request) -> dict[str, list[dict[str, Any]]]:
        store: BridgeStore = request.app.state.store
        started_at = time.monotonic()
        items = await store_call(store.list_queue_items)
        reconciled: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        lock_contention_failures = 0
        eligible_items = bounded_reconcile_candidates(items)
        limiter = asyncio.Semaphore(MAX_BULK_RECONCILE_CONCURRENCY)
        results = await asyncio.gather(
            *(
                reconcile_bulk_queue_item(request, item, limiter)
                for item in eligible_items
            ),
            return_exceptions=True,
        )
        for item, result in zip(eligible_items, results, strict=True):
            if isinstance(result, BaseException):
                lock_contention = database_lock_contention(result)
                if lock_contention:
                    lock_contention_failures += 1
                log.error(
                    "bulk reconcile task escaped with an exception",
                    extra={
                        "queue_id": item["id"],
                        "lock_contention": lock_contention,
                    },
                    exc_info=(type(result), result, result.__traceback__),
                )
                failures.append(
                    {
                        "queue_id": item["id"],
                        "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                        "detail": "unexpected reconcile failure",
                    }
                )
                continue
            succeeded, payload = result
            if succeeded:
                reconciled.append(payload)
            else:
                if database_lock_contention(payload.get("detail")):
                    lock_contention_failures += 1
                failures.append(payload)
        log.info(
            "bulk reconcile completed",
            extra={
                "duration_ms": elapsed_milliseconds(started_at),
                "eligible_count": len(eligible_items),
                "reconciled_count": len(reconciled),
                "failure_count": len(failures),
                "lock_contention_failures": lock_contention_failures,
            },
        )
        return {"items": reconciled, "failures": failures}

    @app.post("/api/bridge/v1/queue/{queue_id}/reconcile", dependencies=[BridgeAuth])
    async def reconcile_bridge_queue_item(request: Request, queue_id: str) -> dict[str, Any]:
        return await reconcile_queue_item_action(request, queue_id)

    @app.get("/api/bridge/v1/queue/{queue_id}/events", dependencies=[BridgeAuth])
    async def bridge_queue_events(
        request: Request,
        queue_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        store: BridgeStore = request.app.state.store
        try:
            events = await store_call(store.list_events, queue_id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "queue item not found") from exc
        return {"events": events}

    @app.get("/api/bridge/v1/validation", dependencies=[BridgeAuth])
    async def bridge_validation(request: Request) -> dict[str, list[dict[str, str]]]:
        cfg: BridgeConfig = request.app.state.bridge_config
        settings: Settings = request.app.state.settings
        mm: MediaManagerClient = request.app.state.mm
        issues = await store_call(validate_static_config, cfg, settings)
        issues.extend(await validate_live_libraries(cfg, mm))
        return {"issues": issues}

    @app.post("/api/bridge/v1/queue/{queue_id}/approve", dependencies=[BridgeAuth])
    async def approve_queue_item(request: Request, queue_id: str) -> dict[str, Any]:
        store: BridgeStore = request.app.state.store
        existing = await get_queue_or_404(store, queue_id)
        cfg: BridgeConfig = request.app.state.bridge_config
        profile = queue_profile_or_409(cfg, existing)
        item = await store_call(
            store.transition_queue_item,
            queue_id,
            from_status="pending_approval",
            to_status="approval_claimed",
        )
        if item is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "queue item is not pending approval")
        mm: MediaManagerClient = request.app.state.mm
        try:
            media_id = str(item["mediamanager_id"]) if item.get("mediamanager_id") else None
            if media_id is None:
                add_to_mediamanager = (
                    add_movie_to_mediamanager
                    if item["media_type"] == "movie"
                    else add_show_to_mediamanager
                )
                media_id = await add_to_mediamanager(mm, profile, int(item["external_id"]))
            item = await store_call(
                store.update_queue_item,
                queue_id,
                status="needs_release",
                mediamanager_id=media_id,
                from_status="approval_claimed",
            )
            if item is None:
                item = await store_call(
                    store.update_queue_item,
                    queue_id,
                    status="needs_release",
                    mediamanager_id=media_id,
                    from_status="pending_approval",
                )
                if item is None:
                    await store_call(store.update_queue_item, queue_id, mediamanager_id=media_id)
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "queue item approval claim expired; retry approval",
                    )
        except (MediaManagerError, httpx.TransportError) as exc:
            await store_call(
                store.update_queue_item,
                queue_id,
                status="pending_approval",
                from_status="approval_claimed",
            )
            await store_call(
                store.add_event,
                event_type="queue.approval_failed",
                message=str(exc) or exc.__class__.__name__,
                queue_id=queue_id,
            )
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, MEDIAMANAGER_REQUEST_FAILURE) from exc

        try:
            await refresh_candidates_for_item(store, mm, item, profile)
        except MediaManagerError as exc:
            await store_call(
                store.add_event,
                event_type="queue.candidate_refresh_failed",
                message=str(exc),
                queue_id=queue_id,
            )
        await store_call(
            store.add_event,
            event_type="queue.approved",
            message="queue item approved",
            queue_id=queue_id,
        )
        return await store_call(store.get_queue_item, queue_id)

    @app.post("/api/bridge/v1/queue/{queue_id}/deny", dependencies=[BridgeAuth])
    async def deny_queue_item(request: Request, queue_id: str) -> dict[str, Any]:
        store: BridgeStore = request.app.state.store
        await get_queue_or_404(store, queue_id)
        item = await store_call(
            store.transition_queue_item,
            queue_id,
            from_status="pending_approval",
            to_status="denied",
        )
        if item is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "queue item is not pending approval")
        item = await store_call(store.update_queue_item, queue_id, status="denied", resolved=True)
        if item is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "queue item is not denied")
        await store_call(
            store.add_event,
            event_type="queue.denied",
            message="queue item denied",
            queue_id=queue_id,
        )
        return item

    @app.post("/api/bridge/v1/queue/{queue_id}/refresh-candidates", dependencies=[BridgeAuth])
    async def refresh_queue_candidates(request: Request, queue_id: str) -> dict[str, Any]:
        return await refresh_queue_candidates_action(request, queue_id)

    @app.post(
        "/api/bridge/v1/queue/{queue_id}/download/{candidate_id}",
        dependencies=[BridgeAuth],
    )
    async def download_queue_candidate(
        request: Request,
        queue_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        return await download_queue_candidate_action(request, queue_id, candidate_id)

    @app.get("/api/v3/movie/lookup", dependencies=[Auth])
    @app.get("/api/v3/movie/lookup/tmdb", dependencies=[Auth])
    async def movie_lookup(
        term: str | None = None,
        tmdbId: int | None = Query(default=None),
    ) -> list[MovieLookup]:
        tmdb_id = tmdbId if tmdbId is not None else parse_id_term(term or "", "tmdb")
        if tmdb_id is None:
            return []
        return [
            MovieLookup(
                title=f"TMDB Movie {tmdb_id}",
                tmdbId=tmdb_id,
                titleSlug=f"tmdb-movie-{tmdb_id}",
            )
        ]

    @app.post("/api/v3/movie", dependencies=[Auth])
    async def add_movie(request: Request, body: AddMovieRequest) -> AddMovieResponse:
        cfg: BridgeConfig = request.app.state.bridge_config
        try:
            profile = cfg.profile_for(body.qualityProfileId, "movie")
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        mm: MediaManagerClient = request.app.state.mm
        store: BridgeStore = request.app.state.store

        if profile.mode in (ReleaseMode.APPROVAL, ReleaseMode.MANUAL):
            await enqueue_approval_or_manual_request(
                store,
                mm,
                profile,
                media_type="movie",
                external_id=body.tmdbId,
                title=body.title,
                seasons=None,
                search_requested=body.addOptions.searchForMovie,
                payload=body.model_dump(mode="json"),
                add_to_mediamanager=add_movie_to_mediamanager,
            )
            return AddMovieResponse(id=stable_int_id(body.tmdbId), title=body.title)

        try:
            movie_uuid = await add_movie_to_mediamanager(mm, profile, body.tmdbId)
            if body.addOptions.searchForMovie and profile.mode == ReleaseMode.AUTO:
                releases = await mm.search_movie_torrents(movie_uuid)
                choice = choose_best_release(releases, profile.max_results)
                if choice:
                    try:
                        await mm.download_movie_torrent(movie_uuid, choice.id)
                    except httpx.TransportError as exc:
                        await record_auto_download_unverified(
                            store,
                            AutoDownloadAmbiguity(
                                media_type="movie",
                                external_id=body.tmdbId,
                                title=body.title,
                                profile=profile,
                                mediamanager_id=movie_uuid,
                                payload=body.model_dump(mode="json"),
                                choice=choice,
                                message=download_submit_unknown_message(exc),
                            ),
                        )
                    else:
                        log.info(
                            "queued movie release",
                            extra={"title": body.title, "release": choice.title},
                        )
                else:
                    log.info("no movie releases found", extra={"title": body.title})
            else:
                log.info(
                    "movie added without automatic release download",
                    extra={"title": body.title, "mode": profile.mode},
                )
        except httpx.TransportError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, MEDIAMANAGER_REQUEST_FAILURE) from exc
        except MediaManagerError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, MEDIAMANAGER_REQUEST_FAILURE) from exc

        return AddMovieResponse(id=stable_int_id(body.tmdbId), title=body.title)

    @app.get("/api/v3/languageprofile", dependencies=[Auth])
    async def language_profiles() -> list[QualityProfile]:
        return [QualityProfile(id=1, name="Any")]

    @app.get("/api/v3/series/lookup", dependencies=[Auth])
    async def series_lookup(term: str) -> list[SeriesLookup]:
        if parse_id_term(term, "tvdb") is not None:
            # OnScreen falls through to tmdb:<id> after TVDB misses. MediaManager
            # is TMDB-native, so that path keeps the bridge lossless.
            return []
        tmdb_id = parse_id_term(term, "tmdb")
        if tmdb_id is None:
            return []
        return [
            SeriesLookup(
                title=f"TMDB Show {tmdb_id}",
                tvdbId=tmdb_id,
                tmdbId=tmdb_id,
                titleSlug=f"tmdb-show-{tmdb_id}",
                seasons=[],
            )
        ]

    @app.post("/api/v3/series", dependencies=[Auth])
    async def add_series(request: Request, body: AddSeriesRequest) -> AddSeriesResponse:
        cfg: BridgeConfig = request.app.state.bridge_config
        settings: Settings = request.app.state.settings
        try:
            profile = cfg.profile_for(body.qualityProfileId, "show")
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        mm: MediaManagerClient = request.app.state.mm
        store: BridgeStore = request.app.state.store
        # OnScreen reaches this path with TMDB ids after the lookup fallback above.
        tmdb_id = body.tvdbId
        seasons = monitored_seasons(body.seasons)

        if profile.mode in (ReleaseMode.APPROVAL, ReleaseMode.MANUAL):
            await enqueue_approval_or_manual_request(
                store,
                mm,
                profile,
                media_type="show",
                external_id=tmdb_id,
                title=body.title,
                seasons=seasons,
                search_requested=body.addOptions.searchForMissingEpisodes,
                payload=body.model_dump(mode="json"),
                add_to_mediamanager=add_show_to_mediamanager,
            )
            return AddSeriesResponse(id=stable_int_id(tmdb_id), title=body.title)

        try:
            show_uuid = await add_show_to_mediamanager(mm, profile, tmdb_id)
            if (
                body.addOptions.searchForMissingEpisodes
                and profile.mode == ReleaseMode.AUTO
                and should_auto_download_series(settings, seasons)
            ):
                for season_number in seasons[: settings.max_auto_tv_seasons]:
                    releases = await mm.search_show_torrents(show_uuid, season_number)
                    choice = choose_best_release(releases, profile.max_results)
                    if choice:
                        try:
                            await mm.download_show_torrent(show_uuid, choice.id)
                        except httpx.TransportError as exc:
                            await record_auto_download_unverified(
                                store,
                                AutoDownloadAmbiguity(
                                    media_type="show",
                                    external_id=tmdb_id,
                                    title=body.title,
                                    profile=profile,
                                    mediamanager_id=show_uuid,
                                    payload=body.model_dump(mode="json"),
                                    choice=choice,
                                    message=download_submit_unknown_message(exc),
                                    seasons=seasons,
                                    season_number=season_number,
                                ),
                            )
                        else:
                            log.info(
                                "queued show release",
                                extra={
                                    "title": body.title,
                                    "season": season_number,
                                    "release": choice.title,
                                },
                            )
            else:
                log.info(
                    "show added without automatic season release download",
                    extra={"title": body.title, "mode": profile.mode, "seasons": seasons},
                )
        except httpx.TransportError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, MEDIAMANAGER_REQUEST_FAILURE) from exc
        except MediaManagerError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, MEDIAMANAGER_REQUEST_FAILURE) from exc

        return AddSeriesResponse(id=stable_int_id(tmdb_id), title=body.title)

    @app.post("/integrations/onscreen/webhook")
    async def onscreen_webhook(request: Request) -> dict[str, str]:
        settings: Settings = request.app.state.settings
        if not settings.enable_onscreen_webhook:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "OnScreen webhook disabled")
        secret = settings.onscreen_webhook_secret.get_secret_value()
        if not secret:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OnScreen webhook secret not set",
            )

        body = await read_limited_body(request)
        timestamp = request.headers.get("X-OnScreen-Timestamp")
        signature = request.headers.get("X-OnScreen-Signature")
        if not verify_onscreen_signature(secret, timestamp, signature, body):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid OnScreen signature")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "invalid OnScreen webhook JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "OnScreen webhook payload must be a JSON object",
            )
        store: BridgeStore = request.app.state.store
        event_name = str(payload.get("event") or payload.get("type") or "onscreen.webhook")
        await store_call(
            store.add_event,
            event_type="onscreen.webhook",
            message=f"OnScreen event received: {event_name}",
            payload=webhook_event_summary(payload),
        )
        await apply_onscreen_availability_hint(store, payload)
        return {"status": "accepted"}

    return app


async def store_call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await run_in_threadpool(partial(func, *args, **kwargs))


async def reconcile_bulk_queue_item(
    request: Request,
    item: dict[str, Any],
    limiter: asyncio.Semaphore,
) -> tuple[bool, dict[str, Any]]:
    queue_id = str(item["id"])
    async with limiter:
        started_at = time.monotonic()
        lock_contention = False
        try:
            return (True, await reconcile_queue_item_action(request, queue_id))
        except HTTPException as exc:
            lock_contention = database_lock_contention(exc.detail)
            log.warning(
                "bulk reconcile failed for queue item",
                extra={
                    "queue_id": queue_id,
                    "status_code": exc.status_code,
                    "lock_contention": lock_contention,
                },
            )
            return (
                False,
                {
                    "queue_id": queue_id,
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
            )
        except Exception as exc:
            lock_contention = database_lock_contention(exc)
            log.exception(
                "bulk reconcile failed unexpectedly for queue item",
                extra={"queue_id": queue_id, "lock_contention": lock_contention},
            )
            store: BridgeStore = request.app.state.store
            await try_record_reconcile_failure_event(
                store,
                queue_id,
                f"unexpected reconcile failure: {exc.__class__.__name__}",
            )
            return (
                False,
                {
                    "queue_id": queue_id,
                    "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "detail": "unexpected reconcile failure",
                },
            )
        finally:
            log.info(
                "bulk reconcile item completed",
                extra={
                    "queue_id": queue_id,
                    "duration_ms": elapsed_milliseconds(started_at),
                    "lock_contention": lock_contention,
                },
            )


def same_origin_dashboard_post(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    host = request.headers.get("host") or request.url.netloc
    scheme = request.url.scheme
    if settings.trust_forwarded_headers:
        host = first_forwarded_value(request.headers.get("x-forwarded-host")) or host
        scheme = first_forwarded_value(request.headers.get("x-forwarded-proto")) or scheme
    expected_origin = f"{scheme}://{host}"
    origin = request.headers.get("origin")
    if origin:
        return origin_matches_dashboard(origin, expected_origin, host)
    referer = request.headers.get("referer")
    if not referer:
        return False
    return origin_matches_dashboard(referer, expected_origin, host)


def first_forwarded_value(value: str | None) -> str | None:
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def origin_matches_dashboard(value: str, expected_origin: str, _expected_host: str) -> bool:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin == expected_origin


def bounded_reconcile_candidates(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    eligible = [item for item in items if item["status"] in RECONCILE_QUEUE_STATUSES]
    return sorted(eligible, key=reconcile_candidate_sort_key)[:MAX_BULK_RECONCILE_ITEMS]


def reconcile_candidate_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    last_error = item.get("last_error")
    if not isinstance(last_error, Mapping):
        return 0, ""
    if last_error.get("event_type") != "queue.reconcile_failed":
        return 0, ""
    created_at = last_error.get("created_at")
    return 1, created_at if isinstance(created_at, str) else ""


def elapsed_milliseconds(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def database_lock_contention(value: object) -> bool:
    message = str(value).casefold()
    return any(marker in message for marker in DATABASE_LOCK_MARKERS)


async def recover_stale_claims_loop(
    store: BridgeStore,
    *,
    interval_seconds: int = 60,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await store_call(store.recover_stale_claims)
        except Exception:
            log.exception("stale queue claim recovery failed")


async def mediamanager_reconcile_loop(
    store: BridgeStore,
    mm: MediaManagerClient,
    *,
    interval_seconds: int,
    grace_seconds: float = 900.0,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            items = await store_call(store.list_queue_items)
        except Exception:
            log.exception("MediaManager queue reconciliation failed")
            continue
        eligible_items = bounded_reconcile_candidates(items)
        for item in eligible_items:
            queue_id = str(item["id"])
            try:
                await reconcile_queue_item(
                    store,
                    mm,
                    item,
                    record_noop=False,
                    grace_seconds=grace_seconds,
                )
            except httpx.TransportError as exc:
                log.warning(
                    "MediaManager queue item reconciliation transport failed",
                    extra={"queue_id": item.get("id")},
                    exc_info=True,
                )
                await try_record_reconcile_failure_event(
                    store,
                    queue_id,
                    f"MediaManager detail request failed: {exc.__class__.__name__}",
                )
            except ReconcileFailureRecorded:
                continue
            except MediaManagerError as exc:
                log.warning(
                    "MediaManager queue item reconciliation failed",
                    extra={"queue_id": item.get("id")},
                    exc_info=True,
                )
                await try_record_mediamanager_reconcile_failure(store, queue_id, exc)
            except Exception as exc:
                log.exception(
                    "unexpected MediaManager queue item reconciliation failure",
                    extra={"queue_id": item.get("id")},
                )
                await try_record_reconcile_failure_event(
                    store,
                    queue_id,
                    f"unexpected reconcile failure: {exc.__class__.__name__}",
                )


def resolve_dashboard_session_secret(settings: Settings, store: BridgeStore) -> str:
    configured = settings.dashboard_session_secret.get_secret_value()
    if configured:
        return configured
    if settings.enable_dashboard:
        log.warning(
            "DASHBOARD_SESSION_SECRET is not set; using generated secret persisted in SQLite"
        )
        return store.get_or_create_meta("dashboard_session_secret", lambda: secrets.token_hex(32))
    return secrets.token_hex(32)


def constant_time_equal(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


async def get_queue_or_404(store: BridgeStore, queue_id: str) -> dict[str, Any]:
    try:
        return await store_call(store.get_queue_item, queue_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "queue item not found") from exc


async def refresh_queue_candidates_action(request: Request, queue_id: str) -> dict[str, Any]:
    store: BridgeStore = request.app.state.store
    item = await get_queue_or_404(store, queue_id)
    if item["status"] not in DOWNLOADABLE_QUEUE_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "queue item is not waiting for release selection",
        )
    if not item.get("mediamanager_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, "queue item is not in MediaManager yet")
    cfg: BridgeConfig = request.app.state.bridge_config
    profile = queue_profile_or_409(cfg, item)
    mm: MediaManagerClient = request.app.state.mm
    try:
        await refresh_candidates_for_item(store, mm, item, profile)
    except httpx.TransportError as exc:
        await store_call(
            store.add_event,
            event_type="queue.candidate_refresh_failed",
            message=f"MediaManager candidate refresh request failed: {exc.__class__.__name__}",
            queue_id=queue_id,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "MediaManager candidate refresh request failed",
        ) from exc
    except MediaManagerError as exc:
        await store_call(
            store.add_event,
            event_type="queue.candidate_refresh_failed",
            message=str(exc),
            queue_id=queue_id,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            MEDIAMANAGER_CANDIDATE_REFRESH_FAILURE,
        ) from exc
    await store_call(
        store.add_event,
        event_type="queue.candidates_refreshed",
        message="release candidates refreshed",
        queue_id=queue_id,
    )
    return await store_call(store.get_queue_item, queue_id)


async def download_queue_candidate_action(
    request: Request,
    queue_id: str,
    candidate_id: str,
) -> dict[str, Any]:
    store: BridgeStore = request.app.state.store
    item = await get_queue_or_404(store, queue_id)
    if item["status"] not in DOWNLOADABLE_QUEUE_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "queue item is not waiting for release selection",
        )
    if not item.get("mediamanager_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, "queue item is not in MediaManager yet")
    try:
        candidate = await store_call(store.get_candidate, queue_id, candidate_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "candidate not found") from exc
    mm: MediaManagerClient = request.app.state.mm
    claimed = await store_call(
        store.transition_queue_item,
        queue_id,
        from_status=item["status"],
        to_status="download_claimed",
    )
    if claimed is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "queue item is not waiting for release selection",
        )
    marked_submitted = await store_call(
        store.update_queue_item,
        queue_id,
        status="download_submitted",
        from_status="download_claimed",
    )
    if marked_submitted is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "queue item download claim expired; check queue status",
        )
    try:
        if item["media_type"] == "movie":
            await mm.download_movie_torrent(item["mediamanager_id"], candidate["result_id"])
        else:
            await mm.download_show_torrent(item["mediamanager_id"], candidate["result_id"])
    except httpx.TimeoutException as exc:
        await mark_download_unverified(store, queue_id, candidate_id, candidate, message=str(exc))
        return await store_call(store.get_queue_item, queue_id)
    except httpx.TransportError as exc:
        await mark_download_unverified(
            store,
            queue_id,
            candidate_id,
            candidate,
            message=f"MediaManager download submit outcome unknown: {exc.__class__.__name__}",
        )
        return await store_call(store.get_queue_item, queue_id)
    except MediaManagerError as exc:
        await mark_download_failed(store, queue_id, candidate_id, candidate, message=str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, MEDIAMANAGER_DOWNLOAD_FAILURE) from exc
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        await mark_download_failed(
            store,
            queue_id,
            candidate_id,
            candidate,
            message=f"unexpected download failure: {message}",
        )
        log.exception("unexpected MediaManager download failure")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "MediaManager download failed unexpectedly",
        ) from exc
    if item["media_type"] == "show":
        item = await complete_show_candidate_download(store, queue_id, candidate_id, candidate)
        if item is not None:
            return item
    item = await store_call(
        store.update_queue_item,
        queue_id,
        status="download_submitted",
        from_status="download_submitted",
    )
    if item is None:
        return await terminal_item_or_expired_claim_conflict(store, queue_id)
    await store_call(
        store.add_event,
        event_type="queue.download_submitted",
        message="selected release submitted to MediaManager",
        queue_id=queue_id,
        payload={"candidate_id": candidate_id, "result_id": candidate["result_id"]},
    )
    return item


async def complete_show_candidate_download(
    store: BridgeStore,
    queue_id: str,
    candidate_id: str,
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    await store_call(
        store.delete_candidates_for_season,
        queue_id,
        int(candidate["season_number"]),
    )
    remaining = await store_call(store.get_queue_item, queue_id)
    if not remaining["candidates"]:
        return None
    item = await store_call(
        store.update_queue_item,
        queue_id,
        status="needs_release",
        from_status="download_submitted",
    )
    if item is None:
        return await terminal_item_or_expired_claim_conflict(store, queue_id)
    await store_call(
        store.add_event,
        event_type="queue.download_submitted",
        message="selected season release submitted to MediaManager",
        queue_id=queue_id,
        payload={
            "candidate_id": candidate_id,
            "result_id": candidate["result_id"],
            "season_number": candidate["season_number"],
        },
    )
    return item


async def terminal_item_or_expired_claim_conflict(
    store: BridgeStore,
    queue_id: str,
) -> dict[str, Any]:
    item = await store_call(store.get_queue_item, queue_id)
    if item["status"] in TERMINAL_QUEUE_STATUSES:
        return item
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        "queue item download claim expired; check queue status",
    )


async def reconcile_queue_item_action(request: Request, queue_id: str) -> dict[str, Any]:
    store: BridgeStore = request.app.state.store
    mm: MediaManagerClient = request.app.state.mm
    settings: Settings = request.app.state.settings
    item = await get_queue_or_404(store, queue_id)
    try:
        return await asyncio.wait_for(
            reconcile_queue_item(
                store,
                mm,
                item,
                grace_seconds=settings.reconcile_grace_seconds,
            ),
            timeout=MAX_BULK_RECONCILE_ITEM_SECONDS,
        )
    except TimeoutError as exc:
        log.warning(
            "MediaManager detail request timed out during reconciliation",
            extra={"queue_id": queue_id},
        )
        await store_call(
            store.add_event,
            event_type="queue.reconcile_failed",
            message="MediaManager detail request timed out",
            queue_id=queue_id,
        )
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "MediaManager detail request timed out",
        ) from exc
    except ReconcileFailureRecorded as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, MEDIAMANAGER_DETAIL_FAILURE) from exc
    except httpx.TransportError as exc:
        log.warning(
            "MediaManager detail request failed during reconciliation",
            extra={"queue_id": queue_id},
            exc_info=True,
        )
        await store_call(
            store.add_event,
            event_type="queue.reconcile_failed",
            message=f"MediaManager detail request failed: {exc.__class__.__name__}",
            queue_id=queue_id,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "MediaManager detail request failed",
        ) from exc
    except MediaManagerError as exc:
        await record_mediamanager_reconcile_failure(store, queue_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, MEDIAMANAGER_DETAIL_FAILURE) from exc


async def reconcile_queue_item(
    store: BridgeStore,
    mm: MediaManagerClient,
    item: dict[str, Any],
    *,
    record_noop: bool = True,
    grace_seconds: float = 900.0,
) -> dict[str, Any]:
    queue_id = str(item["id"])
    if item["status"] not in RECONCILE_QUEUE_STATUSES:
        await store_call(
            store.add_event,
            event_type="queue.reconcile_skipped",
            message=f"queue item status {item['status']} does not need reconciliation",
            queue_id=queue_id,
        )
        return await store_call(store.get_queue_item, queue_id)
    media_id = item.get("mediamanager_id")
    if not media_id:
        await store_call(
            store.add_event,
            event_type="queue.reconcile_failed",
            message="queue item has no MediaManager id",
            queue_id=queue_id,
        )
        raise ReconcileFailureRecorded("queue item is not in MediaManager yet")
    details = await mediamanager_details(mm, item["media_type"], str(media_id))
    if mediamanager_reports_imported(details, item):
        updated = await store_call(
            store.update_queue_item,
            queue_id,
            status="imported",
            resolved=True,
            from_status=item["status"],
        )
        if updated is None:
            return await store_call(store.get_queue_item, queue_id)
        await store_call(
            store.add_event,
            event_type="queue.imported",
            message="MediaManager reports item downloaded/imported",
            queue_id=queue_id,
            payload=mediamanager_reconcile_summary(details),
        )
        return updated
    if item["status"] == "download_submitted" and not mediamanager_reports_grab_activity(
        details,
        item,
    ):
        if not download_submitted_past_grace(item, grace_seconds):
            # MediaManager's detail endpoint can lag the grab. Hold the item in
            # download_submitted for the grace window so a stale negative
            # snapshot cannot bounce it into a duplicate grab. No event churn.
            return await store_call(store.get_queue_item, queue_id)
        # Crash-window recovery: the bridge marked the item download_submitted,
        # the grace window elapsed, and MediaManager still shows neither an
        # import nor any active grab, so the download call never landed.
        # Return the item to release selection so the user can re-pick a
        # release instead of stranding it forever.
        updated = await store_call(
            store.update_queue_item,
            queue_id,
            status="needs_release",
            from_status="download_submitted",
        )
        if updated is None:
            return await store_call(store.get_queue_item, queue_id)
        await store_call(
            store.add_event,
            event_type="queue.download_not_grabbed",
            message=(
                "MediaManager shows no active download for the submitted release; "
                "returned to release selection"
            ),
            queue_id=queue_id,
            payload=mediamanager_reconcile_summary(details),
        )
        return updated
    if record_noop:
        await store_call(
            store.add_event,
            event_type="queue.reconciled",
            message="MediaManager does not report this item imported yet",
            queue_id=queue_id,
            payload=mediamanager_reconcile_summary(details),
        )
    return await store_call(store.get_queue_item, queue_id)


async def record_mediamanager_reconcile_failure(
    store: BridgeStore,
    queue_id: str,
    exc: MediaManagerError,
) -> None:
    await store_call(
        store.add_event,
        event_type="queue.reconcile_failed",
        message=f"{MEDIAMANAGER_DETAIL_FAILURE}: {exc.__class__.__name__}",
        queue_id=queue_id,
    )


async def try_record_mediamanager_reconcile_failure(
    store: BridgeStore,
    queue_id: str,
    exc: MediaManagerError,
) -> None:
    try:
        await record_mediamanager_reconcile_failure(store, queue_id, exc)
    except Exception:
        log.exception(
            "failed to record MediaManager queue item reconciliation failure",
            extra={"queue_id": queue_id},
        )


async def try_record_reconcile_failure_event(
    store: BridgeStore,
    queue_id: str,
    message: str,
) -> None:
    try:
        await store_call(
            store.add_event,
            event_type="queue.reconcile_failed",
            message=message,
            queue_id=queue_id,
        )
    except Exception:
        log.exception(
            "failed to record MediaManager queue item reconciliation failure",
            extra={"queue_id": queue_id},
        )


async def record_auto_download_unverified(
    store: BridgeStore,
    ambiguity: AutoDownloadAmbiguity,
) -> None:
    item = await store_call(
        store.upsert_queue_item,
        media_type=ambiguity.media_type,
        external_id=ambiguity.external_id,
        title=ambiguity.title,
        profile_id=ambiguity.profile.id,
        profile_name=ambiguity.profile.name,
        mode=ambiguity.profile.mode,
        status="download_unverified",
        mediamanager_id=ambiguity.mediamanager_id,
        season_number=ambiguity.season_number,
        seasons=ambiguity.seasons,
        payload=ambiguity.payload,
    )
    if item["status"] in TERMINAL_QUEUE_STATUSES:
        return
    if item["status"] not in {"download_submitted", "download_unverified", "download_failed"}:
        return
    if (
        item["status"] != "download_unverified"
        or item.get("mediamanager_id") != ambiguity.mediamanager_id
    ):
        updated = await store_call(
            store.update_queue_item,
            item["id"],
            status="download_unverified",
            mediamanager_id=ambiguity.mediamanager_id,
            from_status=item["status"],
        )
        if updated is None:
            return
        item = updated
    await store_call(
        store.replace_candidates,
        item["id"],
        [release_choice_candidate(ambiguity.choice)],
        media_type=ambiguity.media_type,
        season_number=ambiguity.season_number,
    )
    refreshed = await store_call(store.get_queue_item, item["id"])
    selected_candidate = next(
        (
            candidate
            for candidate in refreshed["candidates"]
            if candidate["result_id"] == ambiguity.choice.id
            and candidate["season_number"] == ambiguity.season_number
        ),
        None,
    )
    await store_call(
        store.add_event,
        event_type="queue.download_unverified",
        message=ambiguity.message,
        queue_id=item["id"],
        payload={
            "candidate_id": selected_candidate["id"] if selected_candidate else "",
            "result_id": ambiguity.choice.id,
            "season_number": ambiguity.season_number,
        },
    )


def release_choice_candidate(choice: ReleaseChoice) -> dict[str, Any]:
    return {
        "public_indexer_result_id": choice.id,
        "title": choice.title,
        "score": choice.score,
        "seeders": choice.seeders,
        "size": choice.size,
        "usenet": choice.usenet,
    }


def download_submit_unknown_message(exc: httpx.TransportError) -> str:
    if isinstance(exc, httpx.TimeoutException) and str(exc):
        return str(exc)
    return f"MediaManager download submit outcome unknown: {exc.__class__.__name__}"


async def mark_download_failed(
    store: BridgeStore,
    queue_id: str,
    candidate_id: str,
    candidate: dict[str, Any],
    *,
    message: str,
) -> None:
    updated = await store_call(
        store.update_queue_item,
        queue_id,
        status="download_failed",
        from_status="download_submitted",
    )
    if updated is None:
        return
    await store_call(
        store.add_event,
        event_type="queue.download_failed",
        message=message,
        queue_id=queue_id,
        payload={"candidate_id": candidate_id, "result_id": candidate["result_id"]},
    )


async def mark_download_unverified(
    store: BridgeStore,
    queue_id: str,
    candidate_id: str,
    candidate: dict[str, Any],
    *,
    message: str,
) -> None:
    updated = await store_call(
        store.update_queue_item,
        queue_id,
        status="download_unverified",
        from_status="download_submitted",
    )
    if updated is None:
        return
    await store_call(
        store.add_event,
        event_type="queue.download_unverified",
        message=(
            message
            if message
            else
            "MediaManager download submit outcome unknown; leaving request unverified "
            "to avoid duplicate grabs"
        ),
        queue_id=queue_id,
        payload={"candidate_id": candidate_id, "result_id": candidate["result_id"]},
    )


async def mediamanager_details(
    mm: MediaManagerClient,
    media_type: str,
    media_id: str,
) -> dict[str, Any]:
    if media_type == "movie":
        return await mm.get_movie(media_id)
    return await mm.get_show(media_id)


def mediamanager_reports_imported(
    details: object,
    item: dict[str, Any],
) -> bool:
    if not isinstance(details, Mapping):
        return False
    if item.get("media_type") != "show":
        return top_level_reports_imported(details)
    requested_seasons = {int(season) for season in item.get("seasons") or []}
    if not requested_seasons:
        return top_level_reports_imported(details)
    season_details = details.get("seasons")
    if not isinstance(season_details, list):
        return False
    complete_seasons: set[int] = set()
    for season in season_details:
        if not isinstance(season, Mapping) or not top_level_reports_imported(season):
            continue
        season_number = season_detail_number(season)
        if season_number is not None:
            complete_seasons.add(season_number)
    return requested_seasons.issubset(complete_seasons)


def top_level_reports_imported(details: Mapping[str, Any]) -> bool:
    for key in (
        "available",
        "downloaded",
        "imported",
        "is_available",
        "is_downloaded",
        "is_imported",
        "isAvailable",
        "isDownloaded",
        "isImported",
    ):
        if truthy(details.get(key)):
            return True
    status_value = str(details.get("status") or details.get("state") or "").lower()
    return status_value in {"available", "downloaded", "imported", "complete", "completed"}


def mediamanager_reports_grab_activity(
    details: object,
    item: dict[str, Any],
) -> bool:
    if not isinstance(details, Mapping):
        return False
    # A show-level activity signal on the payload itself always counts, even
    # when per-season detail rows are present.
    if top_level_reports_grab_activity(details):
        return True
    if item.get("media_type") != "show":
        return False
    requested_seasons = {int(season) for season in item.get("seasons") or []}
    if not requested_seasons:
        return False
    season_details = details.get("seasons")
    if not isinstance(season_details, list):
        return False
    for season in season_details:
        if not isinstance(season, Mapping):
            continue
        season_number = season_detail_number(season)
        if season_number is None or season_number not in requested_seasons:
            continue
        # A partially imported or actively grabbing season still proves the
        # submitted download reached MediaManager.
        if top_level_reports_imported(season) or top_level_reports_grab_activity(season):
            return True
    return False


def download_submitted_past_grace(item: Mapping[str, Any], grace_seconds: float) -> bool:
    """Whether a download_submitted item is old enough to bounce back.

    updated_at tracks the last queue write, which for a submitted item is the
    submission transition; an unparseable timestamp fails open to recovery.
    """
    if grace_seconds <= 0:
        return True
    updated_at = item.get("updated_at")
    if not isinstance(updated_at, str):
        return True
    try:
        submitted_at = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    if submitted_at.tzinfo is None:
        # Naive timestamps are stored in UTC; normalize before comparing
        # against the aware current time.
        submitted_at = submitted_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - submitted_at).total_seconds() >= grace_seconds


def top_level_reports_grab_activity(details: Mapping[str, Any]) -> bool:
    for key in (
        "downloading",
        "grabbed",
        "in_progress",
        "is_downloading",
        "is_grabbed",
        "isDownloading",
        "isGrabbed",
        "active_download",
        "download_in_progress",
        "downloadInProgress",
    ):
        if truthy(details.get(key)):
            return True
    status_value = str(details.get("status") or details.get("state") or "").lower()
    return status_value in {"downloading", "grabbed", "snatched", "queued"}


def season_detail_number(season: Mapping[str, Any]) -> int | None:
    for key in ("season_number", "seasonNumber", "number", "season"):
        value = parse_webhook_int(season.get(key))
        if value is not None:
            return value
    return None


def truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "available",
            "downloaded",
            "imported",
        }
    return False


def mediamanager_reconcile_summary(details: object) -> dict[str, Any]:
    if not isinstance(details, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key in ("id", "external_id", "title", "status", "state", "downloaded", "imported"):
        if key in details and isinstance(details[key], str | int | float | bool | type(None)):
            summary[key] = details[key]
    return summary


def ensure_queue_status(item: dict[str, Any], allowed: set[str]) -> None:
    if str(item["status"]) not in allowed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"queue item is already {item['status']}",
        )


def queue_profile_or_409(config: BridgeConfig, item: dict[str, Any]) -> BridgeProfile:
    try:
        return config.profile_for(int(item["profile_id"]), item["media_type"])
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


async def parse_dashboard_login(request: Request) -> tuple[str, bool]:
    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = json.loads(
                await read_limited_body(
                    request,
                    max_bytes=MAX_DASHBOARD_LOGIN_BODY_BYTES,
                    too_large_message="login payload too large",
                )
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid login payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid login payload")
        return str(payload.get("api_key") or ""), False

    raw_body = await read_limited_body(
        request,
        max_bytes=MAX_DASHBOARD_LOGIN_BODY_BYTES,
        too_large_message="login payload too large",
    )
    form = parse_qs(raw_body.decode("utf-8", errors="replace"))
    return form.get("api_key", [""])[0], True


def webhook_event_summary(payload: dict[str, Any]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for key in ("event", "type", "request_id", "media_id", "tmdb_id", "external_id"):
        value = payload.get(key)
        if isinstance(value, str | int | float | bool):
            summary[key] = str(value)
    return summary


async def apply_onscreen_availability_hint(store: BridgeStore, payload: dict[str, Any]) -> None:
    if not webhook_indicates_available(payload):
        return
    identity = webhook_media_identity(payload)
    if identity is None:
        return
    media_type, external_id, profile_id = identity
    items = await store_call(store.find_queue_items, media_type=media_type, external_id=external_id)
    if profile_id is not None:
        items = [item for item in items if int(item["profile_id"]) == profile_id]
    elif len(items) > 1:
        return
    for item in items:
        if item["status"] not in AVAILABLE_WEBHOOK_STATUSES:
            continue
        updated = await store_call(
            store.update_queue_item,
            item["id"],
            status="available",
            resolved=True,
            from_status=item["status"],
        )
        if updated is not None:
            await store_call(
                store.add_event,
                event_type="queue.available",
                message="OnScreen reports request available",
                queue_id=item["id"],
                payload=webhook_event_summary(payload),
            )


def webhook_indicates_available(payload: dict[str, Any]) -> bool:
    values = [
        payload.get("event"),
        payload.get("type"),
        payload.get("status"),
        payload.get("state"),
        payload.get("request_status"),
        payload.get("requestStatus"),
    ]
    token_sets = [
        webhook_status_tokens(value)
        for value in values
        if isinstance(value, str)
    ]
    if any(status_tokens_are_negative(tokens) for tokens in token_sets):
        return False
    return any(status_tokens_are_available(tokens) for tokens in token_sets)


def webhook_status_tokens(value: str) -> set[str]:
    normalized = value.strip().lower()
    if "unavailable" in normalized:
        return {"unavailable"}
    return {token for token in re.split(r"[^a-z0-9]+", normalized) if token}


def status_tokens_are_negative(tokens: set[str]) -> bool:
    return bool(
        tokens.intersection(
            {"not", "no", "missing", "failed", "unavailable", "unfulfilled"}
        )
    )


def status_tokens_are_available(tokens: set[str]) -> bool:
    return bool(tokens.intersection({"available", "fulfilled", "imported", "downloaded"}))


def webhook_media_identity(payload: dict[str, Any]) -> tuple[str, int, int | None] | None:
    candidates = [payload]
    for key in ("media", "request", "item"):
        child = payload.get(key)
        if isinstance(child, dict):
            candidates.append(child)
    media_type: str | None = None
    external_id: int | None = None
    profile_id: int | None = None
    for candidate in candidates:
        if media_type is None:
            media_type = normalize_webhook_media_type(candidate)
        if external_id is None:
            external_id = webhook_external_id(candidate)
        if profile_id is None:
            profile_id = webhook_profile_id(candidate)
    if media_type is None or external_id is None:
        return None
    return media_type, external_id, profile_id


def normalize_webhook_media_type(payload: Mapping[str, Any]) -> str | None:
    for key in ("media_type", "mediaType", "type", "kind"):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower()
        if normalized == "movie":
            return "movie"
        if normalized in {"show", "series", "tv", "tv_show"}:
            return "show"
    return None


def webhook_external_id(payload: Mapping[str, Any]) -> int | None:
    for key in ("tmdb_id", "tmdbId", "external_id", "externalId"):
        value = payload.get(key)
        parsed = parse_webhook_int(value)
        if parsed is not None:
            return parsed
    return None


def webhook_profile_id(payload: Mapping[str, Any]) -> int | None:
    for key in ("profile_id", "profileId", "quality_profile_id", "qualityProfileId"):
        value = payload.get(key)
        parsed = parse_webhook_int(value)
        if parsed is not None:
            return parsed
    return None


def parse_webhook_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


async def read_limited_body(
    request: Request,
    *,
    max_bytes: int = MAX_WEBHOOK_BODY_BYTES,
    too_large_message: str = "webhook payload too large",
) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            too_large = int(content_length) > max_bytes
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid Content-Length") from exc
        if too_large:
            raise HTTPException(
                413,
                too_large_message,
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                413,
                too_large_message,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def dashboard_cookie_secure(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    if settings.trust_forwarded_headers:
        forwarded_proto = (
            request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        )
        if forwarded_proto == "https":
            return True
    return request.url.scheme == "https"


def is_dashboard_path(path: str) -> bool:
    return path == "/dashboard" or path.startswith("/dashboard/")


async def enqueue_approval_or_manual_request(
    store: BridgeStore,
    mm: MediaManagerClient,
    profile: BridgeProfile,
    *,
    media_type: MediaType,
    external_id: int,
    title: str,
    seasons: list[int] | None,
    search_requested: bool,
    payload: dict[str, Any],
    add_to_mediamanager: Callable[[MediaManagerClient, BridgeProfile, int], Awaitable[str]],
) -> None:
    """Shared enqueue flow for APPROVAL and MANUAL profile modes.

    Movies pass seasons=None and no per-season candidates; shows pass their
    monitored season list. Error responses, event types/messages, and the
    MediaManager call order match the previous per-route branches exactly.
    """
    if profile.mode == ReleaseMode.APPROVAL:
        item = await store_call(
            store.upsert_queue_item,
            media_type=media_type,
            external_id=external_id,
            title=title,
            profile_id=profile.id,
            profile_name=profile.name,
            mode=profile.mode,
            status="pending_approval",
            seasons=seasons,
            payload=payload,
        )
        ensure_queue_status(item, APPROVAL_ACCEPTED_STATUSES)
        if item["status"] == "pending_approval":
            await store_call(
                store.add_event,
                event_type="queue.created",
                message=f"{media_type} request is pending bridge approval",
                queue_id=item["id"],
            )
        return

    item = await store_call(
        store.upsert_queue_item,
        media_type=media_type,
        external_id=external_id,
        title=title,
        profile_id=profile.id,
        profile_name=profile.name,
        mode=profile.mode,
        status="needs_release",
        seasons=seasons,
        payload=payload,
    )
    ensure_queue_status(item, MANUAL_ACCEPTED_STATUSES)
    if item["status"] != "needs_release" or item.get("mediamanager_id"):
        return
    claimed = await store_call(
        store.transition_queue_item,
        item["id"],
        from_status="needs_release",
        to_status="download_claimed",
    )
    if claimed is None:
        return
    try:
        media_uuid = await add_to_mediamanager(mm, profile, external_id)
    except (MediaManagerError, httpx.TransportError) as exc:
        # Transport failures here are ambiguous: the add may have landed
        # upstream. Rolling the claim back is safe because a retry re-adds
        # through the client's existing-title lookup instead of duplicating.
        await store_call(
            store.update_queue_item,
            item["id"],
            status="needs_release",
            from_status="download_claimed",
        )
        await store_call(
            store.add_event,
            event_type="queue.add_failed",
            message=str(exc) or exc.__class__.__name__,
            queue_id=item["id"],
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            MEDIAMANAGER_REQUEST_FAILURE,
        ) from exc
    updated = await store_call(
        store.update_queue_item,
        item["id"],
        status="needs_release",
        mediamanager_id=media_uuid,
        from_status="download_claimed",
    )
    if updated is None:
        # The claim moved on (competing worker or stale-claim recovery). Keep a
        # winner's MediaManager association; only attach ours when the item is
        # back in needs_release without one.
        current = await store_call(store.get_queue_item, item["id"])
        if current.get("mediamanager_id"):
            return
        await store_call(
            store.update_queue_item,
            item["id"],
            mediamanager_id=media_uuid,
            from_status="needs_release",
        )
        return
    item = updated
    if search_requested:
        try:
            await refresh_candidates_for_item(store, mm, item, profile)
        except MediaManagerError as exc:
            await store_call(
                store.add_event,
                event_type="queue.candidate_refresh_failed",
                message=str(exc),
                queue_id=item["id"],
            )
    await store_call(
        store.add_event,
        event_type="queue.created",
        message=f"{media_type} request is waiting for manual release selection",
        queue_id=item["id"],
    )


async def add_movie_to_mediamanager(
    mm: MediaManagerClient,
    profile: BridgeProfile,
    tmdb_id: int,
) -> str:
    movie = await mm.add_movie(tmdb_id)
    movie_uuid = media_id_from_response(movie, "movie")
    if should_set_library(movie, profile.mediamanager_library):
        try:
            await mm.set_movie_library(movie_uuid, profile.mediamanager_library)
        except MediaManagerError:
            log.warning(
                "MediaManager movie library assignment failed",
                extra={"movie_uuid": movie_uuid, "library": profile.mediamanager_library},
            )
    return movie_uuid


async def add_show_to_mediamanager(
    mm: MediaManagerClient,
    profile: BridgeProfile,
    tmdb_id: int,
) -> str:
    show = await mm.add_show(tmdb_id)
    show_uuid = media_id_from_response(show, "show")
    if should_set_library(show, profile.mediamanager_library):
        try:
            await mm.set_show_library(show_uuid, profile.mediamanager_library)
        except MediaManagerError:
            log.warning(
                "MediaManager show library assignment failed",
                extra={"show_uuid": show_uuid, "library": profile.mediamanager_library},
            )
    return show_uuid


def media_id_from_response(media: Any, media_type: str) -> str:
    if not isinstance(media, dict) or not media.get("id"):
        raise MediaManagerError(f"MediaManager {media_type} response is missing an id")
    return str(media["id"])


async def refresh_candidates_for_item(
    store: BridgeStore,
    mm: MediaManagerClient,
    item: dict[str, Any],
    profile: BridgeProfile,
) -> None:
    media_id = item.get("mediamanager_id")
    if not media_id:
        return
    if item["media_type"] == "movie":
        releases = await mm.search_movie_torrents(str(media_id))
        selected_releases = releases[: profile.max_results]
        validate_release_candidates(selected_releases)
        try:
            await store_call(
                store.replace_candidates,
                str(item["id"]),
                selected_releases,
                media_type="movie",
                clear_empty_batches=False,
            )
        except ValueError as exc:
            raise MediaManagerError(str(exc)) from exc
        return

    seasons = item.get("seasons") or []
    if not seasons:
        return
    season_results: list[tuple[int, list[dict[str, Any]]]] = []
    for season_number in seasons:
        releases = await mm.search_show_torrents(str(media_id), int(season_number))
        selected_releases = releases[: profile.max_results]
        validate_release_candidates(selected_releases)
        season_results.append((int(season_number), selected_releases))
    try:
        await store_call(
            store.replace_candidate_batches,
            str(item["id"]),
            [(releases, "show", season_number) for season_number, releases in season_results],
            clear_empty_batches=False,
        )
    except ValueError as exc:
        raise MediaManagerError(str(exc)) from exc


def validate_release_candidates(releases: list[dict[str, Any]]) -> None:
    for release in releases:
        if release.get("public_indexer_result_id") is None and release.get("id") is None:
            raise MediaManagerError("release candidate is missing an id")


async def validate_live_libraries(
    config: BridgeConfig,
    mm: MediaManagerClient,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    needs_movie_libraries = any("movie" in profile.media_types for profile in config.profiles)
    needs_show_libraries = any("show" in profile.media_types for profile in config.profiles)
    try:
        movie_libraries = await mm.movie_libraries() if needs_movie_libraries else []
        show_libraries = await mm.show_libraries() if needs_show_libraries else []
    except MediaManagerError as exc:
        return [
            issue(
                "mediamanager.validation_unavailable",
                f"could not validate live MediaManager libraries: {exc}",
                "warning",
            )
        ]

    movie_names = library_names(movie_libraries)
    show_names = library_names(show_libraries)
    for profile in config.profiles:
        if "movie" in profile.media_types and profile.mediamanager_library not in movie_names:
            issues.append(
                issue(
                    "library.movie_missing",
                    (
                        f"profile {profile.id} references missing MediaManager movie "
                        f"library {profile.mediamanager_library!r}"
                    ),
                    "warning",
                )
            )
        if "show" in profile.media_types and profile.mediamanager_library not in show_names:
            issues.append(
                issue(
                    "library.show_missing",
                    (
                        f"profile {profile.id} references missing MediaManager TV "
                        f"library {profile.mediamanager_library!r}"
                    ),
                    "warning",
                )
            )
    return issues


def library_names(libraries: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("name") or item.get("label") or item.get("path")) for item in libraries}


def parse_id_term(term: str, prefix: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(prefix)}:(\d+)", term.strip(), re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def stable_int_id(source_id: int) -> int:
    return abs(int(source_id)) % 2_147_483_647


def monitored_seasons(seasons: list[SeriesSeason]) -> list[int]:
    return [s.seasonNumber for s in seasons if s.monitored and s.seasonNumber > 0]


def should_auto_download_series(settings: Settings, seasons: list[int]) -> bool:
    if not seasons:
        return False
    return not (
        len(seasons) > settings.max_auto_tv_seasons and not settings.auto_download_full_series
    )


def should_set_library(media: dict, configured_library: str) -> bool:
    if not configured_library:
        return False
    return str(media.get("library") or "") != configured_library

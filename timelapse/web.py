"""FastAPI web interface for centrally hosted TimeLapse exports."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Annotated
from urllib.parse import quote, urlsplit

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi import Path as ApiPath
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from timelapse import OperationTimeoutError, TimelapseError
from timelapse.config import SPEED_TO_FPS
from timelapse.web_state import DailySchedule, ExportJob, WebCapacityError, WebSettings, WebState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from types import FrameType

    from starlette.datastructures import FormData

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
KIBIBYTE = 1024
SSE_KEEPALIVE_TICKS = 30
GRACEFUL_SHUTDOWN_SECONDS = 1
SECONDS_PER_HOUR = 60 * 60
SESSION_COOKIE = "timelapse_session"
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 60.0
JOB_ID = Annotated[str, ApiPath(min_length=8, max_length=32)]
SCHEDULE_ID = Annotated[str, ApiPath(min_length=8, max_length=32)]
TIMESTAMP_QUERY = Annotated[str, Query(min_length=10, max_length=40)]
NEXT_QUERY = Annotated[str | None, Query(alias="next")]
_LOGGER = logging.getLogger(__name__)

load_dotenv(override=False)


class _ShutdownAwareServer(uvicorn.Server):
    """Notify application streams before Uvicorn drains connections."""

    def __init__(self, config: uvicorn.Config, shutdown_requested: asyncio.Event) -> None:
        super().__init__(config)
        self._shutdown_requested = shutdown_requested

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._shutdown_requested.set()
        super().handle_exit(sig, frame)


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "Unknown"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if size < KIBIBYTE or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= KIBIBYTE
    return f"{value} B"


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    local = value.astimezone()
    return f"{local:%b} {local.day}, {local:%Y} · {local.strftime('%I').lstrip('0')}:{local:%M %p}"


def _format_date(value: date | None) -> str:
    return "Not run yet" if value is None else f"{value:%b} {value.day}, {value:%Y}"


def _job_status(job: ExportJob) -> str:
    labels = {
        "queued": "Queued",
        "running": "Exporting",
        "completed": "Ready",
        "failed": "Failed",
        "cancelled": "Cancelled",
        "skipped": "Already exists",
    }
    return labels[job.status]


def _schedule_next_run(schedule: DailySchedule) -> str:
    if schedule.paused:
        return "paused"
    if schedule.next_retry_at is not None:
        return _format_datetime(schedule.next_retry_at)
    now = datetime.now().astimezone()
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()).astimezone()
    hour = tomorrow.strftime("%I").lstrip("0")
    return f"{tomorrow:%b} {tomorrow.day} at {hour}:{tomorrow:%M %p %Z}"


class _SessionStore:
    """Keep opaque browser sessions in server memory."""

    def __init__(self, lifetime_seconds: int) -> None:
        self._lifetime_seconds = lifetime_seconds
        self._sessions: dict[str, float] = {}

    def create(self) -> str:
        """Create and retain a new opaque session token."""
        self._prune()
        token = secrets.token_urlsafe(32)
        self._sessions[self._digest(token)] = monotonic() + self._lifetime_seconds
        return token

    def valid(self, token: str | None) -> bool:
        """Return whether a token exists and has not expired."""
        if not token:
            return False
        expires_at = self._sessions.get(self._digest(token))
        if expires_at is None:
            return False
        if expires_at <= monotonic():
            self.revoke(token)
            return False
        return True

    def revoke(self, token: str | None) -> None:
        """Remove a browser session if it exists."""
        if token:
            self._sessions.pop(self._digest(token), None)

    def _prune(self) -> None:
        now = monotonic()
        self._sessions = {digest: expires_at for digest, expires_at in self._sessions.items() if expires_at > now}

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()


class _LoginThrottle:
    """Apply a small per-client delay after repeated login failures."""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}

    def retry_after(self, client: str) -> int:
        """Return seconds before another attempt, or zero when allowed."""
        failures = self._recent_failures(client)
        if len(failures) < LOGIN_FAILURE_LIMIT:
            return 0
        return max(round(LOGIN_FAILURE_WINDOW_SECONDS - (monotonic() - failures[0])), 1)

    def failed(self, client: str) -> None:
        """Record one rejected login."""
        failures = self._recent_failures(client)
        failures.append(monotonic())
        self._failures[client] = failures

    def clear(self, client: str) -> None:
        """Clear failures after a successful login."""
        self._failures.pop(client, None)

    def _recent_failures(self, client: str) -> list[float]:
        cutoff = monotonic() - LOGIN_FAILURE_WINDOW_SECONDS
        failures = [attempt for attempt in self._failures.get(client, []) if attempt > cutoff]
        self._failures[client] = failures
        return failures


def _safe_return_path(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    parsed = urlsplit(value)
    return "/" if parsed.scheme or parsed.netloc else value


def _client_key(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _is_same_origin(request: Request, trusted_hosts: frozenset[str]) -> bool:
    fetch_site = request.headers.get("Sec-Fetch-Site", "").casefold()
    if fetch_site == "same-origin":
        return True
    if fetch_site in {"cross-site", "same-site"}:
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True
    actual = _normalized_origin(origin)
    if actual is None or actual[1] not in trusted_hosts:
        return False
    expected = _origin_from_parts(request.url.scheme, request.headers.get("Host"))
    return actual == expected


def _normalized_origin(value: str) -> tuple[str, str, int | None] | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = 80 if parsed.scheme == "http" else 443
    return parsed.scheme, parsed.hostname.casefold(), None if port in {None, default_port} else port


def _origin_from_parts(scheme: str, host: str | None) -> tuple[str, str, int | None] | None:
    if not scheme or not host:
        return None
    return _normalized_origin(f"{scheme}://{host}")


def _hostname(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(f"//{value}")
        _ = parsed.port
    except ValueError:
        return None
    return parsed.hostname.casefold() if parsed.hostname else None


def _trusted_hosts(settings: WebSettings) -> frozenset[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}
    hosts.update(filter(None, (_hostname(host) for host in settings.web_trusted_hosts)))
    configured_host = _hostname(settings.web_host)
    if configured_host is not None and configured_host not in {
        "0.0.0.0",  # noqa: S104 - wildcard bind value is intentionally excluded from trusted hosts
        "::",
    }:
        hosts.add(configured_host)
    return frozenset(hosts)


def _is_loopback_client(request: Request) -> bool:
    if request.client is None:
        return False
    return _is_loopback_host(request.client.host)


def _add_security_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _form_values(form: FormData, name: str) -> list[str]:
    return [str(value) for value in form.getlist(name) if str(value)]


def _required_form_value(form: FormData, name: str, label: str) -> str:
    value = str(form.get(name, "")).strip()
    if not value:
        message = f"{label} is required."
        raise ValueError(message)
    return value


def _parse_local_datetime(raw: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        message = f"{label} must be a valid date and time."
        raise ValueError(message) from exc
    return parsed.astimezone()


def _parse_export_range(form: FormData) -> tuple[datetime, datetime, bool]:
    mode = str(form.get("range_mode", "full-day"))
    if mode == "full-day":
        raw = _required_form_value(form, "day", "Export date")
        try:
            selected_day = date.fromisoformat(raw)
        except ValueError as exc:
            message = "Export date must be a valid calendar date."
            raise ValueError(message) from exc
        start = datetime.combine(selected_day, datetime.min.time()).astimezone()
        end = datetime.combine(selected_day + timedelta(days=1), datetime.min.time()).astimezone()
        return start, end, True
    if mode != "exact":
        message = "Choose a full day or an exact time range."
        raise ValueError(message)
    start = _parse_local_datetime(_required_form_value(form, "start", "Start time"), "Start time")
    end = _parse_local_datetime(_required_form_value(form, "end", "End time"), "End time")
    if end <= start:
        message = "End time must be after start time."
        raise ValueError(message)
    return start, end, False


def _parse_speed(form: FormData) -> str:
    speed = str(form.get("speed", "600x"))
    if speed not in SPEED_TO_FPS:
        message = "Choose a supported timelapse speed."
        raise ValueError(message)
    return speed


def create_app(  # noqa: C901, PLR0915 - route construction stays together for dependency closure
    settings: WebSettings | None = None,
    *,
    state: WebState | None = None,
) -> FastAPI:
    """Build an application, allowing isolated state injection in tests."""
    configured_settings = settings or WebSettings.from_environment()
    web_state = state or WebState(configured_settings)
    shutdown_requested = asyncio.Event()
    session_seconds = configured_settings.web_session_hours * SECONDS_PER_HOUR
    sessions = _SessionStore(session_seconds)
    login_throttle = _LoginThrottle()
    trusted_hosts = _trusted_hosts(configured_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if configured_settings.web_password is None and not _is_loopback_host(configured_settings.web_host):
            message = "TIMELAPSE_WEB_PASSWORD is required when the web server is accessible over the network."
            raise RuntimeError(message)
        await web_state.start()
        try:
            yield
        finally:
            await web_state.close()

    application = FastAPI(
        title="TimeLapse Web",
        summary="Create UniFi Protect timelapses from a local web interface.",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.web = web_state
    application.state.sessions = sessions
    application.state.shutdown_requested = shutdown_requested
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.filters["bytes"] = _format_bytes
    templates.env.filters["datetime"] = _format_datetime
    templates.env.filters["date"] = _format_date
    templates.env.filters["job_status"] = _job_status
    templates.env.filters["next_run"] = _schedule_next_run

    @application.middleware("http")
    async def identify_and_log_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = secrets.token_hex(8)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            _LOGGER.exception(
                "Unhandled web request failure: request_id=%s method=%s path=%s",
                request_id,
                request.method,
                request.url.path,
            )
            return PlainTextResponse(
                f"Internal server error. Request ID: {request_id}",
                status_code=500,
                headers={"X-Request-ID": request_id},
            )
        response.headers["X-Request-ID"] = request_id
        return response

    @application.middleware("http")
    async def require_web_authentication(  # noqa: PLR0911 - ordered security exits keep the boundary explicit
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if _hostname(request.headers.get("Host")) not in trusted_hosts:
            return _add_security_headers(PlainTextResponse("Untrusted host", status_code=400))
        if configured_settings.web_password is None and not _is_loopback_client(request):
            return _add_security_headers(PlainTextResponse("Passwordless access is local-only", status_code=403))
        public = request.url.path in {"/healthz", "/login"} or request.url.path.startswith("/static/")
        unsafe_request = request.method not in {"GET", "HEAD", "OPTIONS"}
        if unsafe_request and request.url.path != "/login" and not _is_same_origin(request, trusted_hosts):
            return _add_security_headers(PlainTextResponse("Cross-origin request rejected", status_code=403))
        if public or configured_settings.web_password is None:
            return _add_security_headers(await call_next(request))
        if not sessions.valid(request.cookies.get(SESSION_COOKIE)):
            next_path = request.url.path
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            login_url = f"/login?next={quote(next_path, safe='')}"
            if request.headers.get("HX-Request") == "true" or request.url.path.startswith("/api/"):
                return _add_security_headers(
                    PlainTextResponse("Login required", status_code=401, headers={"HX-Redirect": login_url})
                )
            return _add_security_headers(RedirectResponse(login_url, status_code=303))
        return _add_security_headers(await call_next(request))

    def message_response(
        request: Request,
        message: str,
        *,
        kind: str = "success",
        status_code: int = 200,
    ) -> HTMLResponse:
        response = templates.TemplateResponse(
            request=request,
            name="partials/message.html",
            context={"message": message, "kind": kind},
            status_code=status_code,
        )
        response.headers["HX-Trigger"] = "stateChanged"
        return response

    def login_response(
        request: Request,
        *,
        next_path: str,
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        response = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": error,
                "next_path": next_path,
                "web_username": configured_settings.web_username,
            },
            status_code=status_code,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next_path: NEXT_QUERY = None) -> Response:
        destination = _safe_return_path(next_path)
        if configured_settings.web_password is None or sessions.valid(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse(destination, status_code=303)
        return login_response(request, next_path=destination)

    @application.post("/login", response_class=HTMLResponse)
    async def log_in(request: Request) -> Response:
        if configured_settings.web_password is None:
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        destination = _safe_return_path(str(form.get("next", "/")))
        client = _client_key(request)
        retry_after = login_throttle.retry_after(client)
        if retry_after:
            message = f"Too many failed attempts. Try again in {retry_after} seconds."
            response = login_response(request, next_path=destination, error=message, status_code=429)
            response.headers["Retry-After"] = str(retry_after)
            return response
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        username_valid = secrets.compare_digest(username, configured_settings.web_username)
        password_valid = secrets.compare_digest(password, configured_settings.web_password)
        valid = username_valid and password_valid
        if not valid:
            login_throttle.failed(client)
            return login_response(
                request,
                next_path=destination,
                error="The username or password is incorrect.",
                status_code=401,
            )
        login_throttle.clear(client)
        token = sessions.create()
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=session_seconds,
            httponly=True,
            secure=configured_settings.web_cookie_secure or request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
        return response

    @application.post("/logout")
    async def log_out(request: Request) -> Response:
        sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        now = datetime.now().astimezone()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "speeds": SPEED_TO_FPS,
                "timezone_name": now.tzname() or "local time",
                "yesterday": (now.date() - timedelta(days=1)).isoformat(),
                "today": now.date().isoformat(),
                "now_local": now.strftime("%Y-%m-%dT%H:%M"),
                "day_ago_local": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "authentication_enabled": configured_settings.web_password is not None,
                "web_username": configured_settings.web_username,
            },
        )

    @application.get("/healthz")
    async def health() -> dict[str, object]:
        return {
            "status": "healthy",
            "connection_configured": configured_settings.connection_ready,
            "active_jobs": sum(not job.terminal for job in web_state.jobs.values()),
        }

    @application.get("/partials/status", response_class=HTMLResponse)
    async def connection_status(request: Request) -> Response:
        active_jobs = sum(not job.terminal for job in web_state.jobs.values())
        return templates.TemplateResponse(
            request=request,
            name="partials/status.html",
            context={"settings": configured_settings, "active_jobs": active_jobs},
        )

    @application.get("/partials/cameras", response_class=HTMLResponse)
    async def cameras(
        request: Request,
        refresh: Annotated[bool, Query()] = False,  # noqa: FBT002 - boolean query parameter
    ) -> Response:
        camera_error = None
        status_code = 200
        loaded_cameras = []
        try:
            loaded_cameras = await web_state.cameras(refresh=refresh)
        except OperationTimeoutError as exc:
            camera_error = str(exc)
            status_code = 504
        except TimelapseError as exc:
            camera_error = str(exc) or type(exc).__name__
            status_code = 502
        except ValueError as exc:
            camera_error = str(exc)
            status_code = 400
        return templates.TemplateResponse(
            request=request,
            name="partials/cameras.html",
            context={"cameras": loaded_cameras, "camera_error": camera_error},
            status_code=status_code,
        )

    @application.get("/partials/jobs", response_class=HTMLResponse)
    async def jobs(request: Request) -> Response:
        ordered_jobs = sorted(web_state.jobs.values(), key=lambda job: job.created_at, reverse=True)
        return templates.TemplateResponse(
            request=request,
            name="partials/jobs.html",
            context={"jobs": ordered_jobs},
        )

    @application.get("/partials/schedules", response_class=HTMLResponse)
    async def schedules(request: Request) -> Response:
        ordered_schedules = sorted(web_state.schedules.values(), key=lambda item: item.created_at, reverse=True)
        return templates.TemplateResponse(
            request=request,
            name="partials/schedules.html",
            context={"schedules": ordered_schedules},
        )

    @application.post("/actions/export", response_class=HTMLResponse)
    async def create_export(request: Request) -> Response:
        try:
            form = await request.form()
            camera_ids = _form_values(form, "camera_ids")
            start, end, full_day = _parse_export_range(form)
            speed = _parse_speed(form)
            created = await web_state.create_jobs(camera_ids, start, end, speed, full_day=full_day)
        except WebCapacityError as exc:
            return message_response(request, str(exc), kind="error", status_code=429)
        except OperationTimeoutError as exc:
            return message_response(request, str(exc), kind="error", status_code=504)
        except TimelapseError as exc:
            return message_response(request, str(exc), kind="error", status_code=502)
        except ValueError as exc:
            return message_response(request, str(exc), kind="error", status_code=400)
        noun = "export" if len(created) == 1 else "exports"
        return message_response(request, f"Started {len(created)} {noun}.")

    @application.post("/actions/schedules", response_class=HTMLResponse)
    async def create_daily_schedule(request: Request) -> Response:
        try:
            form = await request.form()
            camera_ids = _form_values(form, "camera_ids")
            speed = _parse_speed(form)
            schedule = await web_state.create_schedule(camera_ids, speed)
        except OperationTimeoutError as exc:
            return message_response(request, str(exc), kind="error", status_code=504)
        except TimelapseError as exc:
            return message_response(request, str(exc), kind="error", status_code=502)
        except ValueError as exc:
            return message_response(request, str(exc), kind="error", status_code=400)
        count = len(schedule.cameras)
        noun = "camera" if count == 1 else "cameras"
        return message_response(request, f"Daily exports enabled for {count} {noun}.")

    @application.delete("/actions/jobs/{job_id}", response_class=HTMLResponse)
    async def cancel_or_remove_job(request: Request, job_id: JOB_ID) -> Response:
        try:
            action = await web_state.cancel_or_remove_job(job_id)
        except ValueError as exc:
            return message_response(request, str(exc), kind="error", status_code=404)
        message = "Cancellation requested." if action == "cancelled" else "Export removed from the list."
        return message_response(request, message)

    @application.post("/actions/jobs/{job_id}/retry", response_class=HTMLResponse)
    async def retry_job(request: Request, job_id: JOB_ID) -> Response:
        try:
            await web_state.retry_job(job_id)
        except WebCapacityError as exc:
            return message_response(request, str(exc), kind="error", status_code=429)
        except ValueError as exc:
            return message_response(request, str(exc), kind="error", status_code=400)
        return message_response(request, "Export queued again.")

    @application.post("/actions/schedules/{schedule_id}/retry", response_class=HTMLResponse)
    async def retry_schedule(request: Request, schedule_id: SCHEDULE_ID) -> Response:
        try:
            await web_state.retry_schedule(schedule_id)
        except ValueError as exc:
            return message_response(request, str(exc), kind="error", status_code=400)
        return message_response(request, "Daily schedule resumed.")

    @application.delete("/actions/schedules/{schedule_id}", response_class=HTMLResponse)
    async def remove_schedule(request: Request, schedule_id: SCHEDULE_ID) -> Response:
        try:
            await web_state.remove_schedule(schedule_id)
        except ValueError as exc:
            return message_response(request, str(exc), kind="error", status_code=404)
        return message_response(request, "Daily schedule stopped.")

    @application.get("/api/thumbnails/{camera_id}")
    async def thumbnail(camera_id: str, timestamp: TIMESTAMP_QUERY) -> Response:
        try:
            requested_time = _parse_local_datetime(timestamp, "Preview time")
            result = await web_state.thumbnail(camera_id, requested_time)
        except OperationTimeoutError as exc:
            return PlainTextResponse(str(exc), status_code=504)
        except TimelapseError as exc:
            return PlainTextResponse(str(exc), status_code=502)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return Response(
            content=result.image,
            media_type="image/jpeg",
            headers={"X-TimeLapse-Thumbnail-Source": result.source, "Cache-Control": "no-store"},
        )

    @application.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last_version = -1
            idle_ticks = 0
            while not shutdown_requested.is_set() and not await request.is_disconnected():
                if web_state.version != last_version:
                    last_version = web_state.version
                    idle_ticks = 0
                    yield f"event: state\ndata: {last_version}\n\n"
                elif idle_ticks >= SSE_KEEPALIVE_TICKS:
                    idle_ticks = 0
                    yield ": keep-alive\n\n"
                with suppress(TimeoutError):
                    await asyncio.wait_for(shutdown_requested.wait(), timeout=0.5)
                idle_ticks += 1

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @application.get("/exports/{job_id}")
    async def download_export(job_id: JOB_ID) -> Response:
        job = web_state.jobs.get(job_id)
        if job is None or job.status != "completed" or not job.output.is_file():
            return PlainTextResponse("Export is not available.", status_code=404)
        return FileResponse(job.output, media_type="video/mp4", filename=job.output.name)

    return application


app = create_app()


def main() -> None:
    """Run the web server using environment-backed host and port settings."""
    settings = WebSettings.from_environment()
    host = settings.web_host
    if not _is_loopback_host(host) and settings.web_password is None:
        message = "TIMELAPSE_WEB_PASSWORD is required when the web server is accessible over the network."
        raise SystemExit(message)
    config = uvicorn.Config(
        "timelapse.web:app",
        host=host,
        port=_environment_port(),
        proxy_headers=True,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    with suppress(KeyboardInterrupt):
        _ShutdownAwareServer(config, app.state.shutdown_requested).run()


def _environment_port() -> int:
    try:
        return int(os.environ.get("TIMELAPSE_WEB_PORT", "8000"))
    except ValueError:
        return 8000


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    main()

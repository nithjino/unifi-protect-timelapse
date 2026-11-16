"""The web dashboard: app factory, security, routes and ``main``."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import logging
import math
import secrets
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from timelapse import (
    CapacityError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    OperationTimeoutError,
    TimelapseError,
    __version__,
)
from timelapse.daily import DailyRunner, export_run
from timelapse.jobs import JobQueue
from timelapse.model import SPEEDS, Camera, ExportRequest, day_bounds, format_bytes
from timelapse.protect import Protect
from timelapse.registry import Automation, Registry
from timelapse.settings import Settings
from timelapse.storage import MIB, ExportFolder, ensure_writable

LOGGER = logging.getLogger(__name__)
PACKAGE = Path(__file__).parent
COOKIE = "timelapse_session"
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {"/healthz", "/login"}
MAX_FAILURES, FAILURE_WINDOW = 5, 60.0
CAMERA_CACHE_SECONDS = 60.0
NETWORK_ACCESS_ERROR = "TIMELAPSE_WEB_PASSWORD is required when the web server is accessible over the network."
WEB_CONNECTION = ("web-environment", "default")
STATUS_LABELS = {"running": "Exporting", "completed": "Ready", "failed": "Failed", "cancelled": "Cancelled"}
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
}


def http_status(error: Exception) -> int:
    """Map the error hierarchy to an HTTP status."""
    for kind, status in (
        (NotFoundError, 404),
        (ConflictError, 409),
        (CapacityError, 429),
        (InvalidRequestError, 400),
        (OperationTimeoutError, 504),
    ):
        if isinstance(error, kind):
            return status
    return 502


def is_loopback(host: str) -> bool:
    """Return whether ``host`` names this machine only."""
    host = host.strip("[]").lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def split_host(value: str, scheme: str) -> tuple[str, int] | None:
    """Split a Host header into hostname and port, with the scheme's default port."""
    try:
        parts = urlsplit(f"//{value}")
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname.lower(), port or (443 if scheme == "https" else 80)


def safe_next(value: str | None) -> str:
    """Accept only a local absolute path as a redirect target."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    parts = urlsplit(value)
    return "/" if parts.scheme or parts.netloc else value


def _format_time(value: datetime) -> str:
    return f"{value:%b} {value.day}, {value.year} · {value.hour % 12 or 12}:{value:%M %p}"


class Dashboard:
    """Everything one web server process owns."""

    def __init__(self, settings: Settings, protect: Any) -> None:
        self.settings = settings
        self.protect = protect
        self.queue = JobQueue(
            max_active=settings.max_active_exports, history=settings.data_dir / "web-jobs.json", limit=100
        )
        self.registry = Registry(settings.data_dir / "web-automations.json")
        self.folder = ExportFolder(
            settings.output_dir, quota_mib=settings.storage_quota_mib, retention_days=settings.retention_days
        )
        self.runner = DailyRunner(self.registry, self.queue, connect=self.connect, storage_problem=self.daily_problem)
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, list[float]] = {}
        self.camera_cache: tuple[float, list[Camera]] | None = None
        self.trusted = LOOPBACK_NAMES | settings.trusted_hosts
        if settings.host not in {"0.0.0.0", "::"}:  # noqa: S104
            self.trusted = self.trusted | {settings.host.strip("[]").lower()}

    async def start(self) -> None:
        """Prepare directories, reload jobs, apply retention and start automations."""
        ensure_writable(self.settings.data_dir)
        ensure_writable(self.settings.output_dir)
        if self.protect is None and self.settings.connection is not None:
            self.protect = Protect(self.settings.connection)
        self.queue.load(self.settings.output_dir)
        self.prune()
        try:
            self.registry.load()
        except TimelapseError:
            LOGGER.exception("Could not load Daily Automations")
        for automation in self.registry.automations:
            if automation.status == "active":
                self.runner.start(automation.id)

    async def stop(self) -> None:
        """Stop the automation loops, then the queue, then the console client."""
        await self.runner.close()
        await self.queue.close()
        if self.protect is not None:
            await self.protect.close()

    def incomplete(self) -> InvalidRequestError:
        """Explain which Protect settings are missing."""
        names = ", ".join(self.settings.missing_connection_values)
        return InvalidRequestError(f"Server configuration is incomplete. Set {names} and restart the web server.")

    def connect(self, automation: Automation) -> Any:
        """Resolve an automation's connection; the web server knows only its own environment."""
        if automation.connection != WEB_CONNECTION:
            raise TimelapseError("the web server can only use web-environment connections")
        return self.connect_web()

    def connect_web(self) -> Any:
        """Return the console client built from the server environment."""
        if self.protect is None:
            raise self.incomplete()
        return self.protect

    def prune(self) -> None:
        """Apply retention, keeping the outputs of unfinished jobs."""
        keep = {job.output.name for job in self.queue.active()}
        self.queue.forget(self.folder.prune(datetime.now(UTC), keep))

    def daily_problem(self) -> str | None:
        """Retention runs first, so an expired file can free the quota."""
        self.prune()
        return self.folder.full_problem()

    async def cameras(self, *, refresh: bool = False) -> list[Camera]:
        """Return the camera list, cached for a minute."""
        protect = self.connect_web()
        cached = self.camera_cache
        if refresh or cached is None or time.monotonic() - cached[0] > CAMERA_CACHE_SECONDS:
            self.camera_cache = cached = (time.monotonic(), await protect.cameras())
        return cached[1]

    async def selected(self, camera_ids: list[str]) -> list[Camera]:
        """Return the known cameras among ``camera_ids``, in request order, without duplicates."""
        if not camera_ids:
            raise InvalidRequestError("Select at least one available camera.")
        known = {camera.id: camera for camera in await self.cameras()}
        chosen = [known[camera_id] for camera_id in dict.fromkeys(camera_ids) if camera_id in known]
        if not chosen:
            raise InvalidRequestError("Select at least one available camera.")
        return chosen

    def submit_manual(self, requests: list[ExportRequest], *, daily: bool = False) -> list[Any]:
        """Queue manual exports under the queue bound and the storage reservation."""
        self.prune()
        settings, active = self.settings, len(self.queue.active())
        if active + len(requests) > settings.max_active_exports + settings.max_queued_exports:
            raise CapacityError("The export queue is full. Wait for an active export to finish and try again.")
        per_job = (settings.connection.max_download_mib if settings.connection else 0) * MIB or self.folder.quota_bytes
        if self.folder.usage_bytes() + (active + len(requests)) * per_job > self.folder.quota_bytes:
            raise CapacityError("The configured export storage quota cannot reserve space for this request.")
        protect = self.connect_web()
        items = [
            (request, settings.output_dir / request.filename, export_run(protect, request)) for request in requests
        ]
        return self.queue.submit(items, daily=daily)

    def parse_time(self, value: str, label: str) -> datetime:
        """Read a form or query time; naive values are in the server timezone."""
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise InvalidRequestError(f"{label} must be a valid date and time.") from error
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=self.settings.timezone)

    def export_range(self, form: Any) -> tuple[datetime, datetime, bool]:
        """Validate the requested range."""
        mode = form.get("range_mode") or "full-day"
        if mode == "full-day":
            text = (form.get("day") or "").strip()
            if not text:
                raise InvalidRequestError("Export date is required.")
            try:
                day = date.fromisoformat(text)
            except ValueError as error:
                raise InvalidRequestError("Export date must be a valid calendar date.") from error
            start, end = day_bounds(day, self.settings.timezone)
            full_day = True
        elif mode == "exact":
            values = {}
            for field, label in (("start", "Start time"), ("end", "End time")):
                text = (form.get(field) or "").strip()
                if not text:
                    raise InvalidRequestError(f"{label} is required.")
                values[field] = self.parse_time(text, label)
            start, end, full_day = values["start"], values["end"], False
            if end <= start:
                raise InvalidRequestError("End time must be after start time.")
        else:
            raise InvalidRequestError("Choose a full day or an exact time range.")
        if end.timestamp() - start.timestamp() > self.settings.max_export_hours * 3600:
            raise InvalidRequestError(f"Exports are limited to {self.settings.max_export_hours} hours.")
        return start, end, full_day

    def check_login_rate(self, client: str) -> int | None:
        """Return the seconds to wait when this client failed too often, else None."""
        now = time.monotonic()
        recent = [moment for moment in self.failures.get(client, []) if now - moment < FAILURE_WINDOW]
        self.failures[client] = recent
        if len(recent) < MAX_FAILURES:
            return None
        return max(1, math.ceil(FAILURE_WINDOW - (now - recent[0])))

    def session_valid(self, token: str | None) -> bool:
        """Return whether the cookie names a live session."""
        if not token:
            return False
        key = hashlib.sha256(token.encode()).hexdigest()
        expiry = self.sessions.get(key)
        if expiry is None or expiry < time.time():
            self.sessions.pop(key, None)
            return False
        return True

    def guard(self, request: Request) -> Response | None:  # noqa: PLR0911
        """Apply the host, network, origin and session rules; return a refusal or None."""
        settings, headers = self.settings, request.headers
        scheme = request.url.scheme
        host = split_host(headers.get("host", ""), scheme)
        if host is None or host[0].strip("[]") not in self.trusted:
            return PlainTextResponse("Untrusted host", 400)
        client = request.client.host if request.client else ""
        if not settings.password and not is_loopback(client):
            return PlainTextResponse(NETWORK_ACCESS_ERROR, 403)
        path = request.url.path
        if request.method not in SAFE_METHODS and not (request.method == "POST" and path == "/login"):
            fetch_site = headers.get("sec-fetch-site")
            origin = headers.get("origin")
            if fetch_site in {"cross-site", "same-site"}:
                return PlainTextResponse("Cross-origin request refused", 403)
            if fetch_site != "same-origin" and origin is not None:
                parts = urlsplit(origin)
                own = split_host(parts.netloc, parts.scheme) if parts.scheme in {"http", "https"} else None
                if own is None or own[0] not in self.trusted or parts.scheme != scheme or own != host:
                    return PlainTextResponse("Cross-origin request refused", 403)
        if not settings.password or path in PUBLIC_PATHS or path.startswith("/static/"):
            return None
        if self.session_valid(request.cookies.get(COOKIE)):
            return None
        target = path + (f"?{request.url.query}" if request.url.query else "")
        location = "/login?next=" + quote(target if request.method == "GET" else "/", safe="")
        if headers.get("hx-request") == "true" or path.startswith("/api/"):
            return PlainTextResponse("Sign in to continue.", 401, headers={"HX-Redirect": location})
        return RedirectResponse(location, 303)


def create_app(settings: Settings, *, protect: Any = None) -> FastAPI:  # noqa: C901, PLR0915
    """Build the web application; ``protect`` replaces the console client in tests."""
    if not settings.password and not is_loopback(settings.host):
        raise RuntimeError(NETWORK_ACCESS_ERROR)
    board = Dashboard(settings, protect)
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    css_version = hashlib.sha256((PACKAGE / "static" / "app.css").read_bytes()).hexdigest()[:12]
    tz = settings.timezone

    def local(value: datetime | None) -> str:
        return "—" if value is None else _format_time(value.astimezone(tz))

    def next_run(automation: Automation) -> str:
        zone = ZoneInfo(automation.timezone)
        when = automation.next_retry_at
        if when is None:
            today = datetime.now(zone).date()
            when = day_bounds(today, zone)[1]
        when = when.astimezone(zone)
        return f"{when:%b} {when.day} at {when.hour % 12 or 12}:{when:%M %p %Z}"

    def job_status(job: Any) -> str:
        if job.status == "queued":
            return "Queued — Rate Limited" if job.error else "Queued"
        return STATUS_LABELS[job.status]

    filters = templates.env.filters
    filters["bytes"] = format_bytes
    filters["datetime"] = local
    filters["date"] = lambda day: "Not Run Yet" if day is None else f"{day:%b} {day.day}, {day.year}"
    filters["job_status"] = job_status
    filters["next_run"] = next_run

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await board.start()
        try:
            yield
        finally:
            await board.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.board = board
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")

    def render(request: Request, name: str, status: int = 200, **context: Any) -> Response:
        context.setdefault("app_css_version", css_version)
        return templates.TemplateResponse(request=request, name=name, context=context, status_code=status)

    def message(request: Request, text: str, kind: str = "success", status: int = 200) -> Response:
        response = render(request, "partials/message.html", status, message=text, kind=kind)
        response.headers["HX-Trigger"] = "stateChanged"
        return response

    @app.middleware("http")
    async def security(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = secrets.token_hex(8)
        try:
            response = board.guard(request) or await call_next(request)
        except Exception:
            LOGGER.exception("Request %s failed", request_id)
            response = PlainTextResponse(f"Internal server error. Request ID: {request_id}", 500)
        response.headers["X-Request-ID"] = request_id
        response.headers.update(SECURITY_HEADERS)
        return response

    async def handle_error(request: Request, error: Exception) -> Response:
        status = http_status(error)
        path = request.url.path
        if path.startswith("/actions/"):
            return message(request, str(error), "error", status)
        if path == "/partials/cameras":
            return render(request, "partials/cameras.html", status, cameras=[], camera_error=str(error))
        return PlainTextResponse(str(error), status)

    app.add_exception_handler(InvalidRequestError, handle_error)
    app.add_exception_handler(TimelapseError, handle_error)

    @app.get("/")
    async def index(request: Request) -> Response:
        now = datetime.now(tz).replace(second=0, microsecond=0)
        return render(
            request,
            "index.html",
            speeds=list(SPEEDS),
            timezone_name=str(tz),
            yesterday=(now.date() - timedelta(days=1)).isoformat(),
            today=now.date().isoformat(),
            now_local=f"{now:%Y-%m-%dT%H:%M}",
            day_ago_local=f"{now - timedelta(days=1):%Y-%m-%dT%H:%M}",
            authentication_enabled=bool(settings.password),
            web_username=settings.username,
        )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "status": "healthy",
                "connection_configured": settings.connection_ready,
                "active_jobs": len(board.queue.active()),
            }
        )

    @app.get("/partials/status")
    async def status_partial(request: Request) -> Response:
        return render(
            request,
            "partials/status.html",
            active_jobs=len(board.queue.active()),
            timelapse_version=__version__,
            settings=settings,
            storage=board.folder.host_storage(),
        )

    @app.get("/partials/cameras")
    async def cameras_partial(request: Request, refresh: bool = False) -> Response:  # noqa: FBT001, FBT002
        cameras = await board.cameras(refresh=refresh)
        return render(request, "partials/cameras.html", cameras=cameras, camera_error=None)

    @app.get("/partials/jobs")
    async def jobs_partial(request: Request) -> Response:
        jobs = list(reversed(board.queue.jobs.values()))
        listed = {job.output.name for job in jobs if job.status in {"queued", "running", "completed"}}
        older = [video for video in board.folder.videos() if video.name not in listed]
        return render(request, "partials/jobs.html", jobs=jobs, unlisted_exports=older)

    @app.get("/partials/automations")
    async def automations_partial(request: Request) -> Response:
        automations = sorted(board.registry.automations, key=lambda item: item.created_at, reverse=True)
        return render(request, "partials/automations.html", automations=automations)

    @app.post("/actions/export")
    async def export_action(request: Request) -> Response:
        form = await request.form()
        speed = str(form.get("speed") or "600x")
        start, end, full_day = board.export_range(form)
        if speed not in SPEEDS:
            raise InvalidRequestError("Choose a supported timelapse speed.")
        cameras = await board.selected([str(value) for value in form.getlist("camera_ids")])
        jobs = board.submit_manual([ExportRequest(camera, start, end, speed, full_day) for camera in cameras])
        return message(request, f"Started {len(jobs)} export(s).")

    @app.post("/actions/automations")
    async def create_automation(request: Request) -> Response:
        form = await request.form()
        name = str(form.get("automation_name") or "").strip()
        if not name:
            raise InvalidRequestError("Automation name is required.")
        speed = str(form.get("speed") or "600x")
        if speed not in SPEEDS:
            raise InvalidRequestError("Choose a supported timelapse speed.")
        cameras = await board.selected([str(value) for value in form.getlist("camera_ids")])
        automation = board.registry.add(
            name=name,
            cameras=cameras,
            connection=WEB_CONNECTION,
            speed=speed,
            output_directory=settings.output_dir,
            timezone=str(tz),
        )
        board.runner.start(automation.id)
        return message(request, f"Created Daily Automation for {len(automation.cameras)} camera(s).")

    @app.delete("/actions/jobs/{job_id}")
    async def delete_job(request: Request, job_id: str) -> Response:
        job = board.queue.jobs.get(job_id)
        if job is None:
            raise NotFoundError("That export is no longer in the job list.")
        if not job.terminal:
            await board.queue.cancel(job_id)
            return message(request, "Cancellation requested.")
        board.queue.remove(job_id)
        return message(request, "Export deleted.")

    @app.post("/actions/jobs/{job_id}/retry")
    async def retry_job(request: Request, job_id: str) -> Response:
        job = board.queue.jobs.get(job_id)
        if job is None:
            raise NotFoundError("That export is no longer in the job list.")
        if job.status not in {"failed", "cancelled"}:
            raise InvalidRequestError("Only failed or cancelled exports can be retried.")
        board.submit_manual([job.request], daily=job.daily)
        return message(request, "Export queued again.")

    def older_export(name: str) -> Path:
        if name not in {video.name for video in board.folder.videos()}:
            raise NotFoundError("That export is no longer on the server.")
        return settings.output_dir / name

    @app.delete("/actions/files/{name}")
    async def delete_file(request: Request, name: str) -> Response:
        older_export(name).unlink(missing_ok=True)
        return message(request, "Export deleted.")

    def automation_or_404(automation_id: str) -> Automation:
        automation = board.registry.get(automation_id)
        if automation is None:
            raise NotFoundError("That Daily Automation no longer exists.")
        return automation

    @app.post("/actions/automations/{automation_id}/stop")
    async def stop_automation(request: Request, automation_id: str) -> Response:
        board.registry.put(automation_or_404(automation_id).stopped())
        return message(request, "Daily Automation stopped.")

    @app.post("/actions/automations/{automation_id}/resume")
    async def resume_automation(request: Request, automation_id: str) -> Response:
        board.registry.put(automation_or_404(automation_id).resumed())
        board.runner.start(automation_id)
        return message(request, "Daily Automation started.")

    @app.delete("/actions/automations/{automation_id}")
    async def delete_automation(request: Request, automation_id: str) -> Response:
        board.registry.delete(automation_or_404(automation_id).id)
        return message(request, "Daily Automation removed. Existing artifacts were kept.")

    @app.get("/api/thumbnails/{camera_id}")
    async def thumbnail(camera_id: str, timestamp: str = "") -> Response:
        if not 10 <= len(timestamp) <= 40:
            raise InvalidRequestError("Preview time must be a valid date and time.")
        at = board.parse_time(timestamp, "Preview time")
        camera = next((camera for camera in await board.cameras() if camera.id == camera_id), None)
        if camera is None:
            raise InvalidRequestError(
                "The selected camera is no longer available. Refresh the camera list and try again."
            )
        data, source = await board.connect_web().thumbnail(camera, at)
        headers = {"X-TimeLapse-Thumbnail-Source": source, "Cache-Control": "no-store"}
        return Response(data, media_type="image/jpeg", headers=headers)

    def video(path: Path | None, *, inline: bool) -> Response:
        if path is None or not path.is_file():
            return PlainTextResponse("Export is not available.", 404)
        disposition = "inline" if inline else "attachment"
        return FileResponse(path, media_type="video/mp4", filename=path.name, content_disposition_type=disposition)

    def job_video(job_id: str) -> Path | None:
        job = board.queue.jobs.get(job_id)
        return job.output if job is not None and job.status == "completed" else None

    def file_video(name: str) -> Path | None:
        return settings.output_dir / name if name in {video.name for video in board.folder.videos()} else None

    @app.get("/exports/{job_id}/play")
    async def play_export(job_id: str) -> Response:
        return video(job_video(job_id), inline=True)

    @app.get("/exports/{job_id}")
    async def download_export(job_id: str) -> Response:
        return video(job_video(job_id), inline=False)

    @app.get("/files/{name}/play")
    async def play_file(name: str) -> Response:
        return video(file_video(name), inline=True)

    @app.get("/files/{name}")
    async def download_file(name: str) -> Response:
        return video(file_video(name), inline=False)

    def login_page(request: Request, next_path: str, error: str | None = None, status: int = 200) -> Response:
        response = render(
            request, "login.html", status, next_path=next_path, error=error, web_username=settings.username
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/login")
    async def login_form(request: Request, next: str = "/") -> Response:  # noqa: A002
        target = safe_next(next)
        if not settings.password or board.session_valid(request.cookies.get(COOKIE)):
            return RedirectResponse(target, 303)
        return login_page(request, target)

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        target = safe_next(str(form.get("next") or "/"))
        client = request.client.host if request.client else ""
        if wait := board.check_login_rate(client):
            response = login_page(request, target, f"Too many failed attempts. Try again in {wait} seconds.", 429)
            response.headers["Retry-After"] = str(wait)
            return response
        username, password = str(form.get("username") or ""), str(form.get("password") or "")
        user_ok = hmac.compare_digest(username.encode(), settings.username.encode())
        password_ok = hmac.compare_digest(password.encode(), (settings.password or "").encode())
        if not (user_ok and password_ok and settings.password):
            board.failures.setdefault(client, []).append(time.monotonic())
            return login_page(request, target, "The username or password is incorrect.", 401)
        board.failures.pop(client, None)
        token = secrets.token_urlsafe(32)
        board.sessions[hashlib.sha256(token.encode()).hexdigest()] = time.time() + settings.session_hours * 3600
        response = RedirectResponse(target, 303)
        response.set_cookie(
            COOKIE,
            token,
            max_age=settings.session_hours * 3600,
            path="/",
            httponly=True,
            samesite="strict",
            secure=settings.cookie_secure or request.url.scheme == "https",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        token = request.cookies.get(COOKIE)
        if token:
            board.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
        response = RedirectResponse("/login", 303)
        response.delete_cookie(COOKIE, path="/")
        return response

    return app


def main() -> None:
    """Run the web server with settings from the environment and ``./.env``."""
    import uvicorn  # noqa: PLC0415
    from dotenv import load_dotenv  # noqa: PLC0415

    load_dotenv(Path.cwd() / ".env", override=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
        app = create_app(settings)
    except (ValueError, RuntimeError) as error:
        sys.exit(f"Error: {error}")
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=True, timeout_graceful_shutdown=1)


if __name__ == "__main__":
    main()

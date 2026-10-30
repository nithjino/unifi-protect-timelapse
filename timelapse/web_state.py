"""In-process state and background work for the web interface."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timelapse.automation_registry import (
    AutomationCamera,
    AutomationRegistry,
    ConnectionReference,
    DailyAutomation,
)
from timelapse.config import (
    DEFAULT_MAX_DOWNLOAD_MIB,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    SPEED_TO_FPS,
    Config,
    ConnectionSettings,
    is_truthy,
)
from timelapse.download import MEBIBYTE, DownloadProgress, default_output_path, format_bytes
from timelapse.jobs import DEFAULT_MAX_ACTIVE_EXPORTS, CoordinatorJob, ExportJobCoordinator, ExportJobSpec
from timelapse.protect import CameraInfo, ProtectClients
from timelapse.schedule import DailyAutomationEngine, ResolvedAutomation, seconds_until_next_local_day
from timelapse.service import CameraThumbnail, export_timelapse, fetch_camera_thumbnail, list_available_cameras

JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset({"completed", "failed", "cancelled"})
ALL_JOB_STATUSES: frozenset[JobStatus] = frozenset({"queued", "running", *TERMINAL_JOB_STATUSES})
CAMERA_CACHE_SECONDS = 60.0
MAX_VISIBLE_JOBS = 100
DEFAULT_MAX_QUEUED_EXPORTS = 20
DEFAULT_MAX_EXPORT_HOURS = 24 * 7
DEFAULT_STORAGE_QUOTA_MIB = 100 * 1024
_LOGGER = logging.getLogger(__name__)

CameraLoader = Callable[[ConnectionSettings], Awaitable[list[CameraInfo]]]
ThumbnailLoader = Callable[[Config, CameraInfo, datetime], Awaitable[CameraThumbnail]]
Exporter = Callable[[Config, CameraInfo, Path, Callable[[DownloadProgress], None] | None], Awaitable[None]]


class WebCapacityError(ValueError):
    """The bounded web export queue or storage budget is full."""


def _environment_integer(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except ValueError:
        return default


def _environment_timezone() -> ZoneInfo:
    name = os.environ.get("TZ", "").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        message = f"TZ must be a valid IANA timezone name, such as America/New_York; got {name!r}."
        raise ValueError(message) from exc


def _stored_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(cast("str", value))
    if parsed.utcoffset() is None:
        message = "stored datetime must include a timezone"
        raise ValueError(message)
    return parsed


@dataclass(frozen=True)
class WebSettings:
    """Server configuration loaded from the environment without exposing secrets."""

    instance_url: str
    token: str = field(repr=False)
    username: str
    password: str = field(repr=False)
    verify_ssl: bool
    request_timeout_seconds: int
    max_download_mib: int
    data_dir: Path
    output_dir: Path
    web_username: str = "timelapse"
    web_password: str | None = field(default=None, repr=False)
    web_session_hours: int = 168
    web_cookie_secure: bool = False
    web_host: str = "127.0.0.1"
    web_trusted_hosts: tuple[str, ...] = ()
    web_max_active_exports: int = DEFAULT_MAX_ACTIVE_EXPORTS
    web_max_queued_exports: int = DEFAULT_MAX_QUEUED_EXPORTS
    web_max_export_hours: int = DEFAULT_MAX_EXPORT_HOURS
    web_storage_quota_mib: int = DEFAULT_STORAGE_QUOTA_MIB
    web_retention_days: int = 0
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    @classmethod
    def from_environment(cls) -> WebSettings:
        """Load connection, storage, and web access settings."""
        data_dir = Path(os.environ.get("TIMELAPSE_WEB_DATA_DIR", "data")).expanduser().resolve()
        output_dir = Path(os.environ.get("TIMELAPSE_WEB_OUTPUT_DIR", str(data_dir / "exports"))).expanduser().resolve()
        trusted_hosts = os.environ.get("TIMELAPSE_WEB_TRUSTED_HOSTS", "").split(",")
        return cls(
            instance_url=os.environ.get("UNIFI_PROTECT_URL", "").strip().rstrip("/"),
            token=os.environ.get("UNIFI_PROTECT_TOKEN", ""),
            username=os.environ.get("UNIFI_PROTECT_USERNAME", ""),
            password=os.environ.get("UNIFI_PROTECT_PASSWORD", ""),
            verify_ssl=is_truthy(os.environ.get("UNIFI_PROTECT_VERIFY_SSL", "true")),
            request_timeout_seconds=_environment_integer(
                "TIMELAPSE_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
            max_download_mib=_environment_integer("TIMELAPSE_MAX_DOWNLOAD_MIB", DEFAULT_MAX_DOWNLOAD_MIB),
            data_dir=data_dir,
            output_dir=output_dir,
            web_username=os.environ.get("TIMELAPSE_WEB_USERNAME") or "timelapse",
            web_password=os.environ.get("TIMELAPSE_WEB_PASSWORD") or None,
            web_session_hours=_environment_integer("TIMELAPSE_WEB_SESSION_HOURS", 168, minimum=1),
            web_cookie_secure=is_truthy(os.environ.get("TIMELAPSE_WEB_COOKIE_SECURE", "false")),
            web_host=os.environ.get("TIMELAPSE_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1",
            web_trusted_hosts=tuple(host.strip().casefold() for host in trusted_hosts if host.strip()),
            web_max_active_exports=_environment_integer(
                "TIMELAPSE_WEB_MAX_ACTIVE_EXPORTS", DEFAULT_MAX_ACTIVE_EXPORTS, minimum=1
            ),
            web_max_queued_exports=_environment_integer("TIMELAPSE_WEB_MAX_QUEUED_EXPORTS", DEFAULT_MAX_QUEUED_EXPORTS),
            web_max_export_hours=_environment_integer(
                "TIMELAPSE_WEB_MAX_EXPORT_HOURS", DEFAULT_MAX_EXPORT_HOURS, minimum=1
            ),
            web_storage_quota_mib=_environment_integer(
                "TIMELAPSE_WEB_STORAGE_QUOTA_MIB", DEFAULT_STORAGE_QUOTA_MIB, minimum=1
            ),
            web_retention_days=_environment_integer("TIMELAPSE_WEB_RETENTION_DAYS", 0),
            timezone=_environment_timezone(),
        )

    @property
    def missing_connection_values(self) -> tuple[str, ...]:
        """List required Protect environment variables that are empty."""
        values = {
            "UNIFI_PROTECT_URL": self.instance_url,
            "UNIFI_PROTECT_TOKEN": self.token,
            "UNIFI_PROTECT_USERNAME": self.username,
            "UNIFI_PROTECT_PASSWORD": self.password,
        }
        return tuple(name for name, value in values.items() if not value)

    @property
    def connection_ready(self) -> bool:
        """Return whether all Protect credentials are configured."""
        return not self.missing_connection_values

    @property
    def timezone_name(self) -> str:
        """Return the configured IANA timezone name."""
        return self.timezone.key

    def now(self) -> datetime:
        """Return the current time in the server's configured local timezone."""
        return datetime.now(self.timezone)

    def localize(self, value: datetime) -> datetime:
        """Interpret a wall time in, or convert an instant to, the server timezone."""
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        """Return configured-local midnights surrounding one calendar day."""
        start = self.localize(datetime.combine(day, datetime.min.time()))
        end = self.localize(datetime.combine(day + timedelta(days=1), datetime.min.time()))
        return start, end

    def connection_settings(self) -> ConnectionSettings:
        """Resolve server connection facts without choosing export policy."""
        if self.missing_connection_values:
            missing = ", ".join(self.missing_connection_values)
            message = f"Server configuration is incomplete. Set {missing} and restart the web server."
            raise ValueError(message)
        return ConnectionSettings(
            instance_url=self.instance_url,
            token=self.token,
            username=self.username,
            password=self.password,
            verify_ssl=self.verify_ssl,
            request_timeout_seconds=self.request_timeout_seconds,
            max_download_mib=self.max_download_mib,
        )

    def config(self, start: datetime, end: datetime, speed: str, *, full_day: bool = False) -> Config:
        """Build an export configuration without leaking connection values."""
        return Config.from_connection(self.connection_settings(), speed=speed, start=start, end=end, full_day=full_day)


@dataclass
class ExportJob:
    """One web-managed camera export."""

    id: str
    camera: CameraInfo
    start: datetime
    end: datetime
    speed: str
    output: Path
    daily: bool = False
    full_day: bool = False
    status: JobStatus = "queued"
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    bytes_per_second: float = 0.0
    elapsed_seconds: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def terminal(self) -> bool:
        """Return whether the job has stopped changing state."""
        return self.status in TERMINAL_JOB_STATUSES

    @property
    def progress_percent(self) -> float | None:
        """Return determinate progress when the server reported a size."""
        if not self.total_bytes:
            return None
        return min(self.downloaded_bytes / self.total_bytes * 100, 100.0)

    @property
    def display_file_size(self) -> int | None:
        """Return the size shown below active and ready status labels."""
        if self.status == "running":
            return self.total_bytes
        if self.status != "completed":
            return None
        try:
            return self.output.stat().st_size
        except OSError:
            return None


@dataclass(frozen=True)
class ExportFile:
    """A finished video on disk that no retained job describes."""

    name: str
    size: int
    modified: datetime


class WebState:
    """Own camera cache, export jobs, and Daily Automation tasks."""

    def __init__(
        self,
        settings: WebSettings,
        *,
        camera_loader: CameraLoader | None = None,
        thumbnail_loader: ThumbnailLoader | None = None,
        exporter: Exporter | None = None,
    ) -> None:
        """Initialize state with overridable service operations for tests."""
        self.settings = settings
        self.jobs: dict[str, ExportJob] = {}
        self.version = 0
        self._protect = ProtectClients()
        self._camera_loader = camera_loader or partial(list_available_cameras, self._protect)
        self._thumbnail_loader = thumbnail_loader or partial(fetch_camera_thumbnail, self._protect)
        self._exporter = exporter or partial(export_timelapse, self._protect)
        self._cameras: list[CameraInfo] = []
        self._cameras_loaded_at = 0.0
        self._camera_lock = asyncio.Lock()
        self._coordinator = ExportJobCoordinator(
            max_active=settings.web_max_active_exports,
            on_transition=self._coordinator_transition,
        )
        self._job_mutation_lock = asyncio.Lock()
        self._job_persist_lock = asyncio.Lock()
        self._job_state_file = settings.data_dir / "web-jobs.json"
        self._automation_registry = AutomationRegistry(settings.data_dir / "web-automations.json")
        self._automation_tasks: dict[str, asyncio.Task[None]] = {}
        self._automation_engine = DailyAutomationEngine(
            self._automation_registry,
            self._coordinator,
            resolve_connection=self._resolve_web_automation,
            exporter=self._export_automation_job,
            check_storage=self._daily_storage_problem,
        )

    @property
    def automations(self) -> dict[str, DailyAutomation]:
        """Return retained automations by ID."""
        return {item.id: item for item in self._automation_registry.list()}

    async def start(self) -> None:
        """Prepare storage and resume persisted automations."""
        for directory in {self.settings.data_dir, self.settings.output_dir}:
            try:
                await asyncio.to_thread(self._prepare_storage_directory, directory)
            except OSError as exc:
                message = (
                    f"Web storage directory {directory} is not writable by the server process. "
                    "For Docker bind mounts, set TIMELAPSE_UID and TIMELAPSE_GID to the host directory owner's "
                    "numeric IDs, rebuild the image, and recreate the container."
                )
                raise RuntimeError(message) from exc
        await self._load_jobs()
        await self.prune_exports()
        self._automation_registry.load()
        for automation in self._automation_registry.list():
            if automation.status == "active":
                self._start_automation_task(automation.id)

    async def close(self) -> None:
        """Cancel background work during server shutdown."""
        tasks = [task for task in self._automation_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._coordinator.close()
        await self._persist_jobs()
        await self._protect.close()

    async def cameras(self, *, refresh: bool = False) -> list[CameraInfo]:
        """Return cached cameras, refreshing them when stale or requested."""
        loop = asyncio.get_running_loop()
        async with self._camera_lock:
            if refresh or not self._cameras or loop.time() - self._cameras_loaded_at >= CAMERA_CACHE_SECONDS:
                self._cameras = await self._camera_loader(self.settings.connection_settings())
                self._cameras_loaded_at = loop.time()
            return list(self._cameras)

    async def camera(self, camera_id: str) -> CameraInfo:
        """Resolve an allowed camera by its exact identifier."""
        cameras = await self.cameras()
        camera = next((item for item in cameras if secrets.compare_digest(item.id, camera_id)), None)
        if camera is None:
            message = "The selected camera is no longer available. Refresh the camera list and try again."
            raise ValueError(message)
        return camera

    async def thumbnail(self, camera_id: str, timestamp: datetime) -> CameraThumbnail:
        """Fetch a timestamp preview using server-held credentials."""
        camera = await self.camera(camera_id)
        config = self.settings.config(timestamp, timestamp + timedelta(seconds=1), "600x")
        return await self._thumbnail_loader(config, camera, timestamp)

    async def _selected_cameras(self, camera_ids: list[str]) -> list[CameraInfo]:
        by_id = {camera.id: camera for camera in await self.cameras()}
        selected = [by_id[camera_id] for camera_id in dict.fromkeys(camera_ids) if camera_id in by_id]
        if not selected:
            message = "Select at least one available camera."
            raise ValueError(message)
        return selected

    async def create_jobs(
        self,
        camera_ids: list[str],
        start: datetime,
        end: datetime,
        speed: str,
        *,
        daily: bool = False,
        full_day: bool = False,
    ) -> list[ExportJob]:
        """Durably queue one bounded export job per selected camera."""
        if end <= start:
            message = "End time must be after start time."
            raise ValueError(message)
        if end - start > timedelta(hours=self.settings.web_max_export_hours):
            message = f"Exports are limited to {self.settings.web_max_export_hours} hours."
            raise WebCapacityError(message)
        selected = await self._selected_cameras(camera_ids)
        await self.prune_exports()
        async with self._job_mutation_lock:
            await self._ensure_export_capacity(len(selected))
            config = self.settings.config(start, end, speed, full_day=full_day or daily)
            jobs = [
                ExportJob(
                    id=secrets.token_urlsafe(9),
                    camera=camera,
                    start=start,
                    end=end,
                    speed=speed,
                    output=self.settings.output_dir / default_output_path(config, camera).name,
                    daily=daily,
                    full_day=config.full_day,
                    created_at=self.settings.now(),
                )
                for camera in selected
            ]
            previous_jobs = dict(self.jobs)

            async def persist_intent(claimed: tuple[CoordinatorJob, ...]) -> None:
                for job, runtime in zip(jobs, claimed, strict=True):
                    job.output = runtime.output
                    self.jobs[job.id] = job
                self._trim_jobs()
                await self._persist_jobs()

            try:
                await self._coordinator.submit(
                    (self._job_spec(job, config) for job in jobs),
                    before_admission=persist_intent,
                )
            except BaseException:
                self.jobs = previous_jobs
                raise
            self._changed()
            return jobs

    async def cancel_or_remove_job(self, job_id: str) -> str:
        """Cancel an active job or delete a terminal job and its output file."""
        async with self._job_mutation_lock:
            job = self.jobs.get(job_id)
            if job is None:
                message = "That export is no longer in the job list."
                raise ValueError(message)
            if job.terminal:
                await asyncio.to_thread(job.output.unlink, missing_ok=True)
                del self.jobs[job_id]
                try:
                    await self._persist_jobs()
                except Exception:
                    self.jobs[job_id] = job
                    raise
                self._changed()
                return "removed"
        await self._coordinator.cancel(job.id)
        return "cancelled"

    async def unlisted_exports(self) -> list[ExportFile]:
        """Return finished videos with no job in the list, newest first."""
        listed = {job.output.name for job in self.jobs.values() if job.status in {"queued", "running", "completed"}}
        return await asyncio.to_thread(self._read_export_files, listed)

    async def unlisted_export_path(self, name: str) -> Path | None:
        """Resolve a filename only if it is a listed unlisted export."""
        files = await self.unlisted_exports()
        return self.settings.output_dir / name if any(item.name == name for item in files) else None

    async def remove_unlisted_export(self, name: str) -> None:
        """Delete a finished video that has no job."""
        path = await self.unlisted_export_path(name)
        if path is None:
            message = "That export is no longer on the server."
            raise ValueError(message)
        await asyncio.to_thread(path.unlink, missing_ok=True)
        self._changed()

    async def prune_exports(self) -> None:
        """Delete exports past the configured retention age and forget their jobs."""
        if not self.settings.web_retention_days:
            return
        cutoff = self.settings.now() - timedelta(days=self.settings.web_retention_days)
        async with self._job_mutation_lock:
            active = {job.output.name for job in self.jobs.values() if not job.terminal}
            removed = await asyncio.to_thread(self._delete_exports_before, cutoff, active)
            if not removed:
                return
            _LOGGER.info("Retention removed %d export(s) older than %s", len(removed), cutoff.date())
            for job in [item for item in self.jobs.values() if item.terminal and item.output.name in removed]:
                del self.jobs[job.id]
            await self._persist_jobs()
            self._changed()

    async def retry_job(self, job_id: str) -> ExportJob:
        """Create a replacement for a failed or cancelled export."""
        job = self.jobs.get(job_id)
        if job is None or job.status not in {"failed", "cancelled"}:
            message = "Only failed or cancelled exports can be retried."
            raise ValueError(message)
        created = await self.create_jobs(
            [job.camera.id], job.start, job.end, job.speed, daily=job.daily, full_day=job.full_day
        )
        return created[0]

    async def create_automation(self, name: str, camera_ids: list[str], speed: str) -> DailyAutomation:
        """Persist and start a named Daily Automation."""
        selected = await self._selected_cameras(camera_ids)
        if speed not in SPEED_TO_FPS:
            message = "Choose a supported timelapse speed."
            raise ValueError(message)
        automation = self._automation_registry.add(
            name=name,
            cameras=tuple(AutomationCamera(camera.id, camera.name) for camera in selected),
            connection=ConnectionReference("web-environment", "default"),
            speed=speed,
            output_directory=self.settings.output_dir,
            timezone=self.settings.timezone_name,
            now=self.settings.now(),
        )
        self._start_automation_task(automation.id)
        self._changed()
        return automation

    async def stop_automation(self, automation_id: str) -> DailyAutomation:
        """Stop future batches without removing the automation."""
        automation = self._automation_registry.stop(automation_id)
        self._changed()
        return automation

    async def remove_automation(self, automation_id: str) -> None:
        """Remove an automation after its submitted batch drains."""
        await self._automation_engine.remove(automation_id)
        task = self._automation_tasks.pop(automation_id, None)
        if task is not None:
            task.cancel()
        self._changed()

    async def resume_automation(self, automation_id: str) -> DailyAutomation:
        """Resume a paused or stopped automation after operator intervention."""
        automation = self._automation_registry.resume(automation_id)
        self._start_automation_task(automation.id)
        self._changed()
        return automation

    def _job_spec(self, job: ExportJob, config: Config) -> ExportJobSpec:
        """Describe a manual export using the coordinator-provided output."""

        async def operation(output: Path) -> None:
            await self._exporter(config, job.camera, output, lambda progress: self._report_progress(job, progress))

        return ExportJobSpec.create(output=job.output, operation=operation, job_id=job.id)

    def _report_progress(self, job: ExportJob, progress: DownloadProgress) -> None:
        job.downloaded_bytes = progress.downloaded_bytes
        job.total_bytes = progress.total_bytes
        job.bytes_per_second = progress.bytes_per_second
        job.elapsed_seconds = progress.elapsed_seconds
        self._changed()

    async def _coordinator_transition(self, runtime: CoordinatorJob) -> None:
        """Project coordinator state into the retained Web job history."""
        job = self.jobs.get(runtime.id)
        spec = runtime.spec
        if job is None and spec.config is not None and spec.camera is not None:
            job = ExportJob(
                id=runtime.id,
                camera=spec.camera,
                start=spec.config.start,
                end=spec.config.end,
                speed=spec.config.speed,
                output=runtime.output,
                daily=True,
                full_day=True,
                created_at=runtime.created_at.astimezone(self.settings.timezone),
            )
            self.jobs[job.id] = job
        if job is None:
            return
        job.output = runtime.output
        job.status = "queued" if runtime.status == "pending" else runtime.status
        job.started_at = runtime.started_at.astimezone(self.settings.timezone) if runtime.started_at else None
        job.finished_at = runtime.finished_at.astimezone(self.settings.timezone) if runtime.finished_at else None
        job.error = runtime.error
        if runtime.status != "running":
            job.bytes_per_second = 0
        self._changed()
        if runtime.terminal:
            self._trim_jobs()
        await self._persist_jobs()

    async def _run_automation(self, automation_id: str) -> None:
        """Run one automation, reporting each pass to connected browsers."""
        registry = self._automation_registry
        with suppress(asyncio.CancelledError):
            while registry.contains(automation_id):
                progressed = await self._automation_engine.run_due_once(automation_id)
                self._changed()
                if progressed or not registry.contains(automation_id):
                    continue
                automation = registry.resolve(automation_id)
                active = automation.status == "active"
                await asyncio.sleep(
                    seconds_until_next_local_day(self.settings.now(), automation.timezone) if active else 60.0
                )

    async def _resolve_web_automation(self, _automation: DailyAutomation) -> ResolvedAutomation:
        return ResolvedAutomation(self.settings.connection_settings(), tuple(await self.cameras(refresh=True)))

    async def _export_automation_job(self, config: Config, camera: CameraInfo, output: Path) -> None:
        job = next((item for item in self.jobs.values() if item.output == output and not item.terminal), None)
        callback = None if job is None else lambda progress: self._report_progress(job, progress)
        await self._exporter(config, camera, output, callback)

    def _start_automation_task(self, automation_id: str) -> None:
        task = self._automation_tasks.get(automation_id)
        if task is None or task.done():
            self._automation_tasks[automation_id] = asyncio.create_task(
                self._run_automation(automation_id),
                name=f"daily-automation-{automation_id}",
            )

    async def _daily_storage_problem(self) -> str | None:
        """Explain why a daily export must wait; its unknown size only counts once on disk."""
        quota_bytes = self.settings.web_storage_quota_mib * MEBIBYTE
        try:
            await self.prune_exports()
            usage_bytes = await asyncio.to_thread(self._output_storage_bytes)
        except (OSError, WebCapacityError) as exc:
            return f"Could not check export storage: {exc}"
        if usage_bytes < quota_bytes:
            return None
        return (
            f"Export storage is full ({format_bytes(usage_bytes)} of {format_bytes(quota_bytes)}). "
            "Delete old exports, or raise TIMELAPSE_WEB_STORAGE_QUOTA_MIB or set TIMELAPSE_WEB_RETENTION_DAYS, "
            "then Resume this automation."
        )

    async def _ensure_export_capacity(self, requested_jobs: int) -> None:
        active_jobs = sum(not job.terminal for job in self.jobs.values())
        maximum_jobs = self.settings.web_max_active_exports + self.settings.web_max_queued_exports
        if active_jobs + requested_jobs > maximum_jobs:
            message = "The export queue is full. Wait for an active export to finish and try again."
            raise WebCapacityError(message)

        quota_bytes = self.settings.web_storage_quota_mib * MEBIBYTE
        usage_bytes = await asyncio.to_thread(self._output_storage_bytes)
        per_job_bytes = self.settings.max_download_mib * MEBIBYTE or quota_bytes
        reserved_bytes = sum(not job.terminal for job in self._coordinator.jobs) * per_job_bytes
        if usage_bytes + reserved_bytes + requested_jobs * per_job_bytes > quota_bytes:
            message = "The configured export storage quota cannot reserve space for this request."
            raise WebCapacityError(message)

    def _output_storage_bytes(self) -> int:
        try:
            return sum(path.stat().st_size for path in self.settings.output_dir.rglob("*") if path.is_file())
        except OSError as exc:
            message = f"Could not measure export storage usage: {exc}"
            raise WebCapacityError(message) from exc

    def _export_paths(self) -> list[Path]:
        """List finished videos, skipping dotfiles such as macOS ._ metadata."""
        return [path for path in self.settings.output_dir.glob("*.mp4") if not path.name.startswith(".")]

    def _read_export_files(self, listed: set[str]) -> list[ExportFile]:
        files: list[ExportFile] = []
        for path in self._export_paths():
            if path.name not in listed:
                with suppress(OSError):
                    stat = path.stat()
                    modified = datetime.fromtimestamp(stat.st_mtime, self.settings.timezone)
                    files.append(ExportFile(path.name, stat.st_size, modified))
        return sorted(files, key=lambda item: item.modified, reverse=True)

    def _delete_exports_before(self, cutoff: datetime, active: set[str]) -> set[str]:
        removed: set[str] = set()
        for path in self._export_paths():
            if path.name in active:
                continue
            try:
                if path.stat().st_mtime < cutoff.timestamp():
                    path.unlink()
                    removed.add(path.name)
            except OSError:
                _LOGGER.warning("Could not apply retention to %s", path, exc_info=True)
        return removed

    def _trim_jobs(self) -> None:
        excess = len(self.jobs) - MAX_VISIBLE_JOBS
        if excess > 0:
            terminal = sorted((job for job in self.jobs.values() if job.terminal), key=lambda job: job.created_at)
            for job in terminal[:excess]:
                del self.jobs[job.id]

    def _changed(self) -> None:
        self.version += 1

    def _stored_job(self, item: dict[str, object]) -> ExportJob:
        camera = cast("dict[str, str | None]", item["camera"])
        status = item["status"]
        speed = item["speed"]
        output_name = cast("str", item["output_name"])
        if status not in ALL_JOB_STATUSES or speed not in SPEED_TO_FPS:
            message = "stored job status or speed is invalid"
            raise ValueError(message)
        if not output_name or Path(output_name).name != output_name:
            message = "stored output filename is invalid"
            raise ValueError(message)
        start = _stored_datetime(item["start"])
        end = _stored_datetime(item["end"])
        if end <= start:
            message = "stored export range is invalid"
            raise ValueError(message)
        started_at, finished_at = item.get("started_at"), item.get("finished_at")
        return ExportJob(
            id=cast("str", item["id"]),
            camera=CameraInfo(
                id=cast("str", camera["id"]),
                name=cast("str", camera["name"]),
                state=camera.get("state"),
                model=camera.get("model"),
            ),
            start=start,
            end=end,
            speed=cast("str", speed),
            output=self.settings.output_dir / output_name,
            daily=item.get("daily") is True,
            full_day=item.get("full_day", item.get("daily")) is True,
            status=cast("JobStatus", status),
            downloaded_bytes=cast("int", item.get("downloaded_bytes") or 0),
            total_bytes=cast("int | None", item.get("total_bytes")),
            bytes_per_second=cast("float", item.get("bytes_per_second") or 0.0),
            elapsed_seconds=cast("float", item.get("elapsed_seconds") or 0.0),
            created_at=_stored_datetime(item["created_at"]),
            started_at=_stored_datetime(started_at) if started_at else None,
            finished_at=_stored_datetime(finished_at) if finished_at else None,
            error=cast("str | None", item.get("error")),
        )

    async def _load_jobs(self) -> None:
        try:
            raw = await asyncio.to_thread(self._job_state_file.read_text, encoding="utf-8")
            items = json.loads(raw)["jobs"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return

        restored_jobs: dict[str, ExportJob] = {}
        changed = False
        for item in items:
            try:
                job = self._stored_job(item)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if job.status in {"queued", "running"}:
                job.status = "cancelled"
                job.error = "The server stopped before this export completed."
                job.finished_at = self.settings.now()
                changed = True
            elif job.status == "completed" and not job.output.is_file():
                job.status = "failed"
                job.error = "The exported video is no longer available on the server."
                changed = True
            restored_jobs[job.id] = job

        self.jobs = restored_jobs
        self._trim_jobs()
        if changed:
            await self._persist_jobs()

    async def _persist_jobs(self) -> None:
        async with self._job_persist_lock:
            payload = {
                "jobs": [
                    {
                        "id": job.id,
                        "camera": {
                            "id": job.camera.id,
                            "name": job.camera.name,
                            "state": job.camera.state,
                            "model": job.camera.model,
                        },
                        "start": job.start.isoformat(),
                        "end": job.end.isoformat(),
                        "speed": job.speed,
                        "output_name": job.output.name,
                        "daily": job.daily,
                        "full_day": job.full_day,
                        "status": job.status,
                        "downloaded_bytes": job.downloaded_bytes,
                        "total_bytes": job.total_bytes,
                        "bytes_per_second": job.bytes_per_second,
                        "elapsed_seconds": job.elapsed_seconds,
                        "created_at": job.created_at.isoformat(),
                        "started_at": job.started_at.isoformat() if job.started_at else None,
                        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                        "error": job.error,
                    }
                    for job in self.jobs.values()
                ]
            }
            try:
                await asyncio.to_thread(self._write_job_state, json.dumps(payload, indent=2, sort_keys=True))
            except Exception:
                _LOGGER.exception("Failed to persist web export state to %s", self._job_state_file)
                raise

    def _write_job_state(self, serialized: str) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._job_state_file.with_name(f".{self._job_state_file.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(self._job_state_file)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    @staticmethod
    def _prepare_storage_directory(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".timelapse-write-test-{secrets.token_hex(8)}.tmp"
        try:
            probe.touch(exist_ok=False)
        finally:
            with suppress(FileNotFoundError):
                probe.unlink()

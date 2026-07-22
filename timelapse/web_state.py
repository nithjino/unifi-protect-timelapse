"""In-process state and background work for the web interface."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from timelapse.config import DEFAULT_MAX_DOWNLOAD_MIB, DEFAULT_REQUEST_TIMEOUT_SECONDS, SPEED_TO_FPS, Config
from timelapse.download import DownloadProgress, default_output_path
from timelapse.protect import CameraInfo
from timelapse.schedule import daily_output_path, latest_complete_local_day
from timelapse.service import CameraThumbnail, export_timelapse, fetch_camera_thumbnail, list_available_cameras

JobStatus = Literal["queued", "running", "completed", "failed", "cancelled", "skipped"]
TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset({"completed", "failed", "cancelled", "skipped"})
ALL_JOB_STATUSES: frozenset[JobStatus] = frozenset({"queued", "running", *TERMINAL_JOB_STATUSES})
CAMERA_CACHE_SECONDS = 60.0
MAX_VISIBLE_JOBS = 100

CameraLoader = Callable[[Config], Awaitable[list[CameraInfo]]]
ThumbnailLoader = Callable[[Config, CameraInfo, datetime], Awaitable[CameraThumbnail]]
Exporter = Callable[[Config, CameraInfo, Path, Callable[[DownloadProgress], None] | None], Awaitable[None]]


def _environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, 0)


def _environment_boolean(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _stored_datetime(value: object, *, required: bool = False) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        message = "stored datetime must be an ISO-8601 string"
        raise TypeError(message)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        message = "stored datetime must include a timezone"
        raise ValueError(message)
    return parsed


def _stored_nonnegative_integer(value: object, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        message = "stored value must be a nonnegative integer"
        raise ValueError(message)
    return value


def _stored_nonnegative_number(value: object, *, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        message = "stored value must be a nonnegative number"
        raise ValueError(message)
    return float(value)


@dataclass(frozen=True)
class WebSettings:
    """Server configuration loaded from the environment without exposing secrets."""

    instance_url: str
    token: str
    username: str
    password: str
    verify_ssl: bool
    request_timeout_seconds: int
    max_download_mib: int
    data_dir: Path
    output_dir: Path
    web_username: str = "timelapse"
    web_password: str | None = None
    web_session_hours: int = 168
    web_cookie_secure: bool = False
    web_host: str = "127.0.0.1"
    web_trusted_hosts: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> WebSettings:
        """Load connection, storage, and web access settings."""
        data_dir = Path(os.environ.get("TIMELAPSE_WEB_DATA_DIR", "data")).expanduser().resolve()
        output_dir = Path(os.environ.get("TIMELAPSE_WEB_OUTPUT_DIR", str(data_dir / "exports"))).expanduser().resolve()
        web_password = os.environ.get("TIMELAPSE_WEB_PASSWORD") or None
        web_host = os.environ.get("TIMELAPSE_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
        web_trusted_hosts = tuple(
            host.strip().casefold()
            for host in os.environ.get("TIMELAPSE_WEB_TRUSTED_HOSTS", "").split(",")
            if host.strip()
        )
        return cls(
            instance_url=os.environ.get("UNIFI_PROTECT_URL", "").strip().rstrip("/"),
            token=os.environ.get("UNIFI_PROTECT_TOKEN", ""),
            username=os.environ.get("UNIFI_PROTECT_USERNAME", ""),
            password=os.environ.get("UNIFI_PROTECT_PASSWORD", ""),
            verify_ssl=_environment_boolean("UNIFI_PROTECT_VERIFY_SSL", default=True),
            request_timeout_seconds=_environment_integer(
                "TIMELAPSE_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
            max_download_mib=_environment_integer("TIMELAPSE_MAX_DOWNLOAD_MIB", DEFAULT_MAX_DOWNLOAD_MIB),
            data_dir=data_dir,
            output_dir=output_dir,
            web_username=os.environ.get("TIMELAPSE_WEB_USERNAME", "timelapse") or "timelapse",
            web_password=web_password,
            web_session_hours=max(_environment_integer("TIMELAPSE_WEB_SESSION_HOURS", 168), 1),
            web_cookie_secure=_environment_boolean("TIMELAPSE_WEB_COOKIE_SECURE", default=False),
            web_host=web_host,
            web_trusted_hosts=web_trusted_hosts,
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

    def config(self, start: datetime, end: datetime, speed: str, *, daily: bool = False) -> Config:
        """Build an export configuration without leaking connection values."""
        if self.missing_connection_values:
            missing = ", ".join(self.missing_connection_values)
            message = f"Server configuration is incomplete. Set {missing} and restart the web server."
            raise ValueError(message)
        return Config(
            instance_url=self.instance_url,
            token=self.token,
            username=self.username,
            password=self.password,
            verify_ssl=self.verify_ssl,
            speed=speed,
            start=start,
            end=end,
            output=None,
            request_timeout_seconds=self.request_timeout_seconds,
            max_download_mib=self.max_download_mib,
            daily=daily,
        )


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
    status: JobStatus = "queued"
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    bytes_per_second: float = 0.0
    elapsed_seconds: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

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


@dataclass
class DailySchedule:
    """A persistent daily export schedule."""

    id: str
    cameras: list[CameraInfo]
    speed: str
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    last_run_day: date | None = None
    last_error: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class WebState:
    """Own camera cache, export tasks, and persistent daily schedules."""

    def __init__(
        self,
        settings: WebSettings,
        *,
        camera_loader: CameraLoader = list_available_cameras,
        thumbnail_loader: ThumbnailLoader = fetch_camera_thumbnail,
        exporter: Exporter = export_timelapse,
    ) -> None:
        """Initialize state with overridable service operations for tests."""
        self.settings = settings
        self.jobs: dict[str, ExportJob] = {}
        self.schedules: dict[str, DailySchedule] = {}
        self.version = 0
        self._camera_loader = camera_loader
        self._thumbnail_loader = thumbnail_loader
        self._exporter = exporter
        self._cameras: list[CameraInfo] = []
        self._cameras_loaded_at = 0.0
        self._camera_lock = asyncio.Lock()
        self._schedule_persist_lock = asyncio.Lock()
        self._job_persist_lock = asyncio.Lock()
        self._schedule_state_file = settings.data_dir / "web-schedules.json"
        self._job_state_file = settings.data_dir / "web-jobs.json"

    async def start(self) -> None:
        """Prepare storage and resume persisted schedules."""
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        await self._load_jobs()
        await self._load_schedules()
        for schedule in self.schedules.values():
            schedule.task = asyncio.create_task(self._run_schedule(schedule), name=f"daily-{schedule.id}")

    async def close(self) -> None:
        """Cancel background work during server shutdown."""
        tasks = [job.task for job in self.jobs.values() if job.task is not None and not job.task.done()]
        tasks.extend(
            schedule.task
            for schedule in self.schedules.values()
            if schedule.task is not None and not schedule.task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._persist_jobs()

    async def cameras(self, *, refresh: bool = False) -> list[CameraInfo]:
        """Return cached cameras, refreshing them when stale or requested."""
        loop = asyncio.get_running_loop()
        if self._cameras and not refresh and loop.time() - self._cameras_loaded_at < CAMERA_CACHE_SECONDS:
            return list(self._cameras)
        async with self._camera_lock:
            if self._cameras and not refresh and loop.time() - self._cameras_loaded_at < CAMERA_CACHE_SECONDS:
                return list(self._cameras)
            now = datetime.now().astimezone()
            config = self.settings.config(now, now + timedelta(seconds=1), "600x")
            self._cameras = await self._camera_loader(config)
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

    async def create_jobs(
        self,
        camera_ids: list[str],
        start: datetime,
        end: datetime,
        speed: str,
        *,
        daily: bool = False,
    ) -> list[ExportJob]:
        """Create one concurrent export job per selected camera."""
        if end <= start:
            message = "End time must be after start time."
            raise ValueError(message)
        cameras = await self.cameras()
        by_id = {camera.id: camera for camera in cameras}
        selected = [by_id[camera_id] for camera_id in dict.fromkeys(camera_ids) if camera_id in by_id]
        if not selected:
            message = "Select at least one available camera."
            raise ValueError(message)
        base_config = self.settings.config(start, end, speed, daily=daily)
        jobs: list[ExportJob] = []
        for camera in selected:
            output = self.settings.output_dir / default_output_path(base_config, camera).name
            if daily:
                output = daily_output_path(base_config, camera, self.settings.output_dir)
            job = ExportJob(
                id=secrets.token_urlsafe(9),
                camera=camera,
                start=start,
                end=end,
                speed=speed,
                output=output,
                daily=daily,
            )
            self.jobs[job.id] = job
            job.task = asyncio.create_task(self._run_job(job), name=f"export-{job.id}")
            jobs.append(job)
        self._trim_jobs()
        self._changed()
        await self._persist_jobs()
        return jobs

    async def cancel_or_remove_job(self, job_id: str) -> str:
        """Cancel an active job or remove a terminal job from the list."""
        job = self.jobs.get(job_id)
        if job is None:
            message = "That export is no longer in the job list."
            raise ValueError(message)
        if job.terminal:
            del self.jobs[job_id]
            self._changed()
            await self._persist_jobs()
            return "removed"
        if job.task is not None:
            job.task.cancel()
        return "cancelled"

    async def retry_job(self, job_id: str) -> ExportJob:
        """Create a replacement for a failed or cancelled export."""
        job = self.jobs.get(job_id)
        if job is None or job.status not in {"failed", "cancelled"}:
            message = "Only failed or cancelled exports can be retried."
            raise ValueError(message)
        return (await self.create_jobs([job.camera.id], job.start, job.end, job.speed, daily=job.daily))[0]

    async def create_schedule(self, camera_ids: list[str], speed: str) -> DailySchedule:
        """Persist and start a daily schedule for selected cameras."""
        cameras = await self.cameras()
        by_id = {camera.id: camera for camera in cameras}
        selected = [by_id[camera_id] for camera_id in dict.fromkeys(camera_ids) if camera_id in by_id]
        if not selected:
            message = "Select at least one available camera."
            raise ValueError(message)
        schedule = DailySchedule(id=secrets.token_urlsafe(9), cameras=selected, speed=speed)
        self.schedules[schedule.id] = schedule
        await self._persist_schedules()
        schedule.task = asyncio.create_task(self._run_schedule(schedule), name=f"daily-{schedule.id}")
        self._changed()
        return schedule

    async def remove_schedule(self, schedule_id: str) -> None:
        """Stop and remove a persistent daily schedule."""
        schedule = self.schedules.pop(schedule_id, None)
        if schedule is None:
            message = "That daily schedule no longer exists."
            raise ValueError(message)
        if schedule.task is not None:
            schedule.task.cancel()
        await self._persist_schedules()
        self._changed()

    async def _run_job(self, job: ExportJob) -> None:
        job.status = "running"
        job.started_at = datetime.now().astimezone()
        self._changed()
        if job.output.exists():
            job.status = "skipped" if job.daily else "failed"
            job.error = "A file already exists for this camera and time range."
            job.finished_at = datetime.now().astimezone()
            self._changed()
            await self._persist_jobs()
            return

        config = self.settings.config(job.start, job.end, job.speed, daily=job.daily)

        def report_progress(progress: DownloadProgress) -> None:
            job.downloaded_bytes = progress.downloaded_bytes
            job.total_bytes = progress.total_bytes
            job.bytes_per_second = progress.bytes_per_second
            job.elapsed_seconds = progress.elapsed_seconds
            self._changed()

        try:
            await self._exporter(config, job.camera, job.output, report_progress)
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc) or type(exc).__name__
        else:
            job.status = "completed"
        finally:
            job.finished_at = datetime.now().astimezone()
            self._changed()
            await self._persist_jobs()

    async def _run_schedule(self, schedule: DailySchedule) -> None:
        try:
            while schedule.id in self.schedules:
                latest_day = latest_complete_local_day()
                day = schedule.last_run_day + timedelta(days=1) if schedule.last_run_day else latest_day
                while day <= latest_day and schedule.id in self.schedules:
                    start = datetime.combine(day, datetime.min.time()).astimezone()
                    end_day = day + timedelta(days=1)
                    end = datetime.combine(end_day, datetime.min.time()).astimezone()
                    try:
                        jobs = await self.create_jobs(
                            [camera.id for camera in schedule.cameras],
                            start,
                            end,
                            schedule.speed,
                            daily=True,
                        )
                        tasks = [job.task for job in jobs if job.task is not None]
                        await asyncio.gather(*tasks)
                    except Exception as exc:
                        schedule.last_error = str(exc) or type(exc).__name__
                        self._changed()
                        await asyncio.sleep(60)
                        continue
                    failed_jobs = [job for job in jobs if job.status in {"failed", "cancelled"}]
                    if failed_jobs:
                        schedule.last_error = f"{len(failed_jobs)} daily export(s) failed; retrying this day."
                        self._changed()
                        await asyncio.sleep(60)
                        continue
                    schedule.last_error = None
                    schedule.last_run_day = day
                    await self._persist_schedules()
                    self._changed()
                    day += timedelta(days=1)
                now = datetime.now().astimezone()
                next_midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()).astimezone()
                await asyncio.sleep(max((next_midnight - now).total_seconds(), 1.0))
        except asyncio.CancelledError:
            return

    def _trim_jobs(self) -> None:
        if len(self.jobs) <= MAX_VISIBLE_JOBS:
            return
        terminal = sorted(
            (job for job in self.jobs.values() if job.terminal),
            key=lambda job: job.created_at,
        )
        for job in terminal[: max(len(self.jobs) - MAX_VISIBLE_JOBS, 0)]:
            self.jobs.pop(job.id, None)

    def _changed(self) -> None:
        self.version += 1

    def _stored_job(self, item: dict[object, object]) -> ExportJob:
        job_id = str(item["id"])
        camera_payload = item["camera"]
        if not job_id or not isinstance(camera_payload, dict):
            message = "stored job requires a camera"
            raise ValueError(message)
        camera_id = str(camera_payload["id"])
        camera_name = str(camera_payload["name"])
        if not camera_id or not camera_name:
            message = "stored camera requires an ID and name"
            raise ValueError(message)
        raw_status = item["status"]
        if not isinstance(raw_status, str) or raw_status not in ALL_JOB_STATUSES:
            message = "stored job status is invalid"
            raise ValueError(message)
        status = cast("JobStatus", raw_status)
        speed = str(item["speed"])
        if speed not in SPEED_TO_FPS:
            message = "stored export speed is invalid"
            raise ValueError(message)
        output_name = str(item["output_name"])
        if not output_name or Path(output_name).name != output_name:
            message = "stored output filename is invalid"
            raise ValueError(message)
        output = self.settings.output_dir / output_name
        if output.resolve().parent != self.settings.output_dir.resolve():
            message = "stored output is outside the export directory"
            raise ValueError(message)
        start = _stored_datetime(item["start"], required=True)
        end = _stored_datetime(item["end"], required=True)
        created_at = _stored_datetime(item["created_at"], required=True)
        if start is None or end is None or created_at is None or end <= start:
            message = "stored export range is invalid"
            raise ValueError(message)
        daily = item.get("daily", False)
        if not isinstance(daily, bool):
            message = "stored daily flag must be a boolean"
            raise TypeError(message)
        total_value = item.get("total_bytes")
        total_bytes = None if total_value is None else _stored_nonnegative_integer(total_value)
        error_value = item.get("error")
        if error_value is not None and not isinstance(error_value, str):
            message = "stored error must be text"
            raise TypeError(message)
        return ExportJob(
            id=job_id,
            camera=CameraInfo(
                id=camera_id,
                name=camera_name,
                state=None if camera_payload.get("state") is None else str(camera_payload["state"]),
                model=None if camera_payload.get("model") is None else str(camera_payload["model"]),
            ),
            start=start,
            end=end,
            speed=speed,
            output=output,
            daily=daily,
            status=status,
            downloaded_bytes=_stored_nonnegative_integer(item.get("downloaded_bytes")),
            total_bytes=total_bytes,
            bytes_per_second=_stored_nonnegative_number(item.get("bytes_per_second")),
            elapsed_seconds=_stored_nonnegative_number(item.get("elapsed_seconds")),
            created_at=created_at,
            started_at=_stored_datetime(item.get("started_at")),
            finished_at=_stored_datetime(item.get("finished_at")),
            error=error_value,
        )

    async def _load_jobs(self) -> None:
        if not self._job_state_file.exists():
            return
        try:
            raw = await asyncio.to_thread(self._job_state_file.read_text, encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            return

        restored_jobs: dict[str, ExportJob] = {}
        changed = False
        for item in payload["jobs"]:
            if not isinstance(item, dict):
                continue
            try:
                job = self._stored_job(item)
            except (KeyError, TypeError, ValueError):
                continue

            if job.status in {"queued", "running"}:
                job.status = "cancelled"
                job.error = "The server stopped before this export completed."
                job.finished_at = datetime.now().astimezone()
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
            serialized = json.dumps(payload, indent=2, sort_keys=True)
            await self._write_state_safely(self._job_state_file, serialized)

    async def _load_schedules(self) -> None:
        if not self._schedule_state_file.exists():
            return
        try:
            raw = await asyncio.to_thread(self._schedule_state_file.read_text, encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return
        for item in payload.get("schedules", []):
            try:
                cameras = [
                    CameraInfo(
                        id=str(camera["id"]),
                        name=str(camera["name"]),
                        state=camera.get("state"),
                        model=camera.get("model"),
                    )
                    for camera in item["cameras"]
                ]
                last_run = date.fromisoformat(item["last_run_day"]) if item.get("last_run_day") else None
                schedule = DailySchedule(
                    id=str(item["id"]),
                    cameras=cameras,
                    speed=str(item["speed"]),
                    created_at=datetime.fromisoformat(item["created_at"]),
                    last_run_day=last_run,
                )
            except (KeyError, TypeError, ValueError):
                continue
            self.schedules[schedule.id] = schedule

    async def _persist_schedules(self) -> None:
        payload = {
            "schedules": [
                {
                    "id": schedule.id,
                    "cameras": [
                        {
                            "id": camera.id,
                            "name": camera.name,
                            "state": camera.state,
                            "model": camera.model,
                        }
                        for camera in schedule.cameras
                    ],
                    "speed": schedule.speed,
                    "created_at": schedule.created_at.isoformat(),
                    "last_run_day": schedule.last_run_day.isoformat() if schedule.last_run_day else None,
                }
                for schedule in self.schedules.values()
            ]
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        async with self._schedule_persist_lock:
            await self._write_state_safely(self._schedule_state_file, serialized)

    async def _write_state_safely(self, state_file: Path, serialized: str) -> None:
        write_task = asyncio.create_task(
            asyncio.to_thread(self._write_state, state_file, serialized),
            name=f"persist-{state_file.stem}",
        )
        try:
            await asyncio.shield(write_task)
        except asyncio.CancelledError:
            await write_task
            raise

    def _write_state(self, state_file: Path, serialized: str) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = state_file.with_name(f".{state_file.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(state_file)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

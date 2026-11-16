"""Export Jobs: the queue, its concurrency limit and the history file."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from timelapse import CapacityError, ConflictError, NotFoundError, RateLimitError, TimelapseError
from timelapse.download import Progress
from timelapse.model import SPEEDS, Camera, ExportRequest, write_json

LOGGER = logging.getLogger(__name__)
STATUSES = ("queued", "running", "completed", "failed", "cancelled")
TERMINAL = frozenset({"completed", "failed", "cancelled"})
STOPPED = "The server stopped before this export completed."
Run = Callable[[Path, Callable[[Progress], None]], Awaitable[None]]


def output_key(path: Path) -> str:
    """Compare outputs by resolved, case-folded path."""
    return str(path.resolve()).casefold()


def _when(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive datetime")
    return parsed


@dataclass(eq=False)
class Job:
    """One camera over one range into one Export Artifact, from queue to history."""

    id: str
    request: ExportRequest
    output: Path
    created_at: datetime
    daily: bool = False
    status: str = "queued"
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    bytes_per_second: float = 0.0
    elapsed_seconds: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    cancel_reason: str = "Export was cancelled."

    @property
    def terminal(self) -> bool:
        """Return whether the job has finished for good."""
        return self.status in TERMINAL

    @property
    def file_size(self) -> int | None:
        """Return the reported size while running, the real size when completed."""
        if self.status == "running":
            return self.total_bytes
        if self.status == "completed":
            try:
                return self.output.stat().st_size
            except OSError:
                return None
        return None

    @property
    def progress_percent(self) -> float | None:
        """Return the download percentage when the total is known."""
        if not self.total_bytes:
            return None
        return min(100.0, self.downloaded_bytes * 100 / self.total_bytes)

    def to_dict(self) -> dict[str, Any]:
        """Return the history file entry."""
        camera = self.request.camera
        iso = lambda value: None if value is None else value.isoformat()  # noqa: E731
        return {
            "id": self.id,
            "camera": {"id": camera.id, "name": camera.name, "state": camera.state, "model": camera.model},
            "start": self.request.start.isoformat(),
            "end": self.request.end.isoformat(),
            "speed": self.request.speed,
            "output_name": self.output.name,
            "daily": self.daily,
            "full_day": self.request.full_day,
            "status": self.status,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "bytes_per_second": self.bytes_per_second,
            "elapsed_seconds": self.elapsed_seconds,
            "created_at": self.created_at.isoformat(),
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, entry: dict[str, Any], output_dir: Path) -> Job:
        """Read a history entry, raising ``ValueError``, ``KeyError`` or ``TypeError`` when it is malformed."""
        name = entry["output_name"]
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("invalid output name")
        if entry["status"] not in STATUSES or entry["speed"] not in SPEEDS:
            raise ValueError("invalid status or speed")
        camera = entry["camera"]
        text = lambda value: None if value is None else str(value)  # noqa: E731
        request = ExportRequest(
            Camera(str(camera["id"]), str(camera["name"]), text(camera.get("state")), text(camera.get("model"))),
            _when(entry["start"]) or datetime.now(UTC),
            _when(entry["end"]) or datetime.now(UTC),
            entry["speed"],
            bool(entry.get("full_day", entry["daily"])),
        )
        job = cls(
            id=str(entry["id"]),
            request=request,
            output=output_dir / name,
            created_at=_when(entry["created_at"]) or datetime.now(UTC),
            daily=bool(entry["daily"]),
            status=entry["status"],
            downloaded_bytes=int(entry.get("downloaded_bytes") or 0),
            total_bytes=None if entry.get("total_bytes") is None else int(entry["total_bytes"]),
            bytes_per_second=float(entry.get("bytes_per_second") or 0),
            elapsed_seconds=float(entry.get("elapsed_seconds") or 0),
            started_at=_when(entry.get("started_at")),
            finished_at=_when(entry.get("finished_at")),
            error=text(entry.get("error")),
        )
        if job.terminal:
            job.done.set()
        return job


class JobQueue:
    """Runs Export Jobs with bounded concurrency and keeps their history."""

    def __init__(
        self,
        *,
        max_active: int,
        history: Path | None = None,
        limit: int = 100,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.jobs: dict[str, Job] = {}
        self.history = history
        self.limit = limit
        self.now = now
        self.sleep = sleep
        self._slots = asyncio.Semaphore(max_active)
        self._closed = False

    def active(self) -> list[Job]:
        """Return the jobs that are queued or running."""
        return [job for job in self.jobs.values() if not job.terminal]

    def load(self, output_dir: Path) -> None:
        """Read the history file; interrupted and vanished exports are marked as such."""
        if self.history is None:
            return
        try:
            entries = json.loads(self.history.read_text(encoding="utf-8"))["jobs"]
            if not isinstance(entries, list):
                raise TypeError("jobs is not a list")  # noqa: TRY301
        except (OSError, ValueError, KeyError, TypeError) as error:
            LOGGER.warning("Starting with an empty job history: %s", error)
            entries = []
        for entry in entries:
            try:
                job = Job.from_dict(entry, output_dir)
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                LOGGER.warning("Skipping a malformed job history entry: %s", error)
                continue
            if not job.terminal:
                self._finish(job, "cancelled", STOPPED, save=False)
            elif job.status == "completed" and not job.output.is_file():
                self._finish(job, "failed", "The exported video is no longer available on the server.", save=False)
            self.jobs[job.id] = job
        self._save_quietly()

    def _save(self) -> None:
        if self.history is not None:
            write_json(self.history, {"jobs": [job.to_dict() for job in self.jobs.values()]})

    def _save_quietly(self) -> None:
        try:
            self._save()
        except Exception:
            LOGGER.exception("Could not save the export history")

    def submit(self, items: Sequence[tuple[ExportRequest, Path, Run]], *, daily: bool = False) -> list[Job]:
        """Queue every item or none of them; the history is saved before any job can start."""
        if self._closed:
            raise CapacityError("The server is shutting down.")
        busy = {output_key(job.output) for job in self.active()}
        keys: set[str] = set()
        for _, output, _ in items:
            key = output_key(output)
            if output.exists() or key in busy or key in keys:
                raise ConflictError(f"An export for {output.name} already exists.")
            keys.add(key)
        previous = dict(self.jobs)
        self.jobs = {job_id: job for job_id, job in self.jobs.items() if output_key(job.output) not in keys}
        new_jobs = [
            Job(secrets.token_urlsafe(9), request, output, self.now(), daily=daily) for request, output, _ in items
        ]
        self.jobs.update((job.id, job) for job in new_jobs)
        self._trim()
        try:
            self._save()
        except Exception as error:
            self.jobs = previous
            raise TimelapseError(f"Could not save the export history: {error}") from error
        for job, (_, _, run) in zip(new_jobs, items, strict=True):
            job.task = asyncio.create_task(self._run(job, run))
        return new_jobs

    def _trim(self) -> None:
        excess = len(self.jobs) - self.limit
        for job in [job for job in self.jobs.values() if job.terminal][: max(0, excess)]:
            del self.jobs[job.id]

    def _finish(self, job: Job, status: str, error: str | None, *, save: bool = True) -> None:
        job.status, job.error, job.bytes_per_second = status, error, 0.0
        job.finished_at = self.now()
        job.done.set()
        if save:
            self._save_quietly()

    async def _run(self, job: Job, run: Run) -> None:
        def progress(update: Progress) -> None:
            job.downloaded_bytes, job.total_bytes = update.downloaded_bytes, update.total_bytes
            job.bytes_per_second, job.elapsed_seconds = update.bytes_per_second, update.elapsed_seconds

        try:
            while True:
                async with self._slots:
                    job.status, job.started_at = "running", self.now()
                    self._save_quietly()
                    try:
                        await run(job.output, progress)
                    except RateLimitError as error:
                        job.status, job.error, job.bytes_per_second = "queued", str(error), 0.0
                        self._save_quietly()
                        until = error.retry_not_before
                        delay = 60.0 if until is None else max(0.0, (until - self.now()).total_seconds())
                    else:
                        self._finish(job, "completed", None)
                        return
                await self.sleep(delay)
        except asyncio.CancelledError:
            self._finish(job, "cancelled", job.cancel_reason)
        except Exception as error:
            self._finish(job, "failed", str(error) or type(error).__name__)

    async def cancel(self, job_id: str) -> bool:
        """Cancel one queued, running or rate-limited job and wait until it has stopped."""
        job = self.jobs.get(job_id)
        if job is None or job.terminal or job.task is None:
            return False
        await self._stop([job])
        return True

    def remove(self, job_id: str) -> None:
        """Delete a finished job's file and record."""
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFoundError("That export is no longer in the job list.")
        if not job.terminal:
            raise ConflictError("Cancel the export before deleting it.")
        previous = dict(self.jobs)
        del self.jobs[job_id]
        try:
            self._save()
        except Exception as error:
            self.jobs = previous
            raise TimelapseError(f"Could not save the export history: {error}") from error
        job.output.unlink(missing_ok=True)

    def forget(self, output_names: set[str]) -> None:
        """Drop finished records whose files were removed."""
        if not output_names:
            return
        self.jobs = {
            job_id: job for job_id, job in self.jobs.items() if not (job.terminal and job.output.name in output_names)
        }
        self._save_quietly()

    async def wait(self, jobs: Iterable[Job]) -> None:
        """Return once every given job has finished."""
        for job in jobs:
            await job.done.wait()

    async def close(self) -> None:
        """Stop every unfinished job and refuse new ones."""
        self._closed = True
        jobs = self.active()
        for job in jobs:
            job.cancel_reason = STOPPED
        await self._stop(jobs)

    async def _stop(self, jobs: list[Job]) -> None:
        tasks = [job.task for job in jobs if job.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        for job in jobs:
            if not job.terminal:  # cancelled before its first step
                self._finish(job, "cancelled", job.cancel_reason)

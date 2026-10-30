"""Runtime-wide coordination for manual and Daily Automation exports."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from timelapse import ProtectRateLimitError, TimelapseError

if TYPE_CHECKING:
    from timelapse.config import Config
    from timelapse.protect import CameraInfo

DEFAULT_MAX_ACTIVE_EXPORTS = 4
RATE_LIMIT_FALLBACK_SECONDS = 60.0

CoordinatorJobStatus = Literal["pending", "queued", "running", "completed", "failed", "cancelled"]
ExportOperation = Callable[[Path], Awaitable[None]]
ClaimCallback = Callable[[tuple["CoordinatorJob", ...]], Awaitable[None]]
TransitionCallback = Callable[["CoordinatorJob"], Awaitable[None]]


class OutputCollisionError(TimelapseError, ValueError):
    """An existing artifact or in-flight Export Job owns the requested path."""


@dataclass(frozen=True)
class ExportJobSpec:
    """One export operation submitted to a process-wide coordinator."""

    id: str
    output: Path
    operation: ExportOperation
    automation_id: str | None = None
    config: Config | None = None
    camera: CameraInfo | None = None

    @classmethod
    def create(
        cls,
        *,
        output: Path,
        operation: ExportOperation,
        automation_id: str | None = None,
        job_id: str | None = None,
        config: Config | None = None,
        camera: CameraInfo | None = None,
    ) -> ExportJobSpec:
        """Build a job spec with a stable caller-supplied or generated ID."""
        return cls(job_id or f"job_{secrets.token_urlsafe(12)}", output, operation, automation_id, config, camera)

    @property
    def camera_name(self) -> str:
        """Return a display name for messages about this job."""
        return self.camera.name if self.camera is not None else self.output.name


@dataclass
class CoordinatorJob:
    """Observable state for one coordinator-owned export."""

    spec: ExportJobSpec
    output: Path
    completion: asyncio.Future[CoordinatorJob] = field(repr=False)
    status: CoordinatorJobStatus = "pending"
    attempt: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def id(self) -> str:
        """Return the stable job ID."""
        return self.spec.id

    @property
    def terminal(self) -> bool:
        """Return whether the job will make no further transitions."""
        return self.status in {"completed", "failed", "cancelled"}


class ExportJobCoordinator:
    """Share concurrency, cancellation, and rate-limit policy in one runtime."""

    def __init__(
        self,
        *,
        max_active: int = DEFAULT_MAX_ACTIVE_EXPORTS,
        on_transition: TransitionCallback | None = None,
        now: Callable[[], datetime] | None = None,
        rate_limit_fallback_seconds: float = RATE_LIMIT_FALLBACK_SECONDS,
    ) -> None:
        """Configure bounded runtime capacity and transition observation."""
        if max_active <= 0:
            message = "export capacity requires at least one active slot"
            raise ValueError(message)
        self.max_active = max_active
        self._on_transition = on_transition
        self._now = now or (lambda: datetime.now(UTC))
        self._rate_limit_fallback_seconds = rate_limit_fallback_seconds
        self._slots = asyncio.Semaphore(max_active)
        self._jobs: dict[str, CoordinatorJob] = {}
        self._claims: dict[str, str] = {}
        self._job_finished = asyncio.Event()
        self._closing = False

    @classmethod
    def from_environment(cls) -> ExportJobCoordinator:
        """Build a coordinator from the validated runtime-wide capacity setting."""
        raw = os.environ.get("TIMELAPSE_MAX_ACTIVE_EXPORTS", str(DEFAULT_MAX_ACTIVE_EXPORTS))
        try:
            value = int(raw)
        except ValueError as exc:
            message = "TIMELAPSE_MAX_ACTIVE_EXPORTS must be a whole number"
            raise ValueError(message) from exc
        if value < 1:
            message = "TIMELAPSE_MAX_ACTIVE_EXPORTS must be at least 1"
            raise ValueError(message)
        return cls(max_active=value)

    @property
    def jobs(self) -> tuple[CoordinatorJob, ...]:
        """Return all jobs in submission order."""
        return tuple(self._jobs.values())

    @property
    def active_count(self) -> int:
        """Return jobs whose operation has started."""
        return sum(job.status == "running" for job in self._jobs.values())

    async def submit(
        self,
        specs: Iterable[ExportJobSpec],
        *,
        before_admission: ClaimCallback | None = None,
    ) -> tuple[CoordinatorJob, ...]:
        """Claim all paths atomically and persist final intent before any job is runnable.

        Callbacks may persist and project the claimed paths, but must not re-enter
        this coordinator. A callback failure rolls back the entire submission.
        """
        if self._closing:
            message = "export coordinator is shutting down"
            raise TimelapseError(message)
        loop = asyncio.get_running_loop()
        jobs: list[CoordinatorJob] = []
        try:
            for spec in specs:
                output = self._claim_output(spec)
                jobs.append(CoordinatorJob(spec, output, loop.create_future(), created_at=self._now()))
            claimed = tuple(jobs)
            if before_admission is not None:
                await before_admission(claimed)
            for job in jobs:
                await self._notify(job)
        except BaseException:
            for job in jobs:
                self._claims.pop(self._output_key(job.output), None)
            raise
        for job in jobs:
            self._jobs[job.id] = job
            job.task = asyncio.create_task(self._run(job), name=f"export-{job.id}")
        return claimed

    @staticmethod
    def _output_key(path: Path) -> str:
        return str(path.expanduser().resolve(strict=False)).casefold()

    def _claim_output(self, spec: ExportJobSpec) -> Path:
        previous = self._jobs.get(spec.id)
        if previous is not None and not previous.terminal:
            message = f"duplicate export job ID: {spec.id}"
            raise ValueError(message)
        output = spec.output.expanduser().resolve(strict=False)
        key = self._output_key(output)
        exists = output.exists() or output.is_symlink()
        if not exists and output.parent.exists():
            exists = any(item.name.casefold() == output.name.casefold() for item in output.parent.iterdir())
        if exists or key in self._claims:
            message = f"An export artifact already exists or is claimed at {output}"
            raise OutputCollisionError(message)
        self._claims[key] = spec.id
        return output

    async def wait(self, jobs: Iterable[CoordinatorJob]) -> tuple[CoordinatorJob, ...]:
        """Wait for the selected jobs without cancelling siblings."""
        selected = tuple(jobs)
        if selected:
            await asyncio.gather(*(asyncio.shield(job.completion) for job in selected))
        return selected

    async def cancel(self, job_id: str) -> bool:
        """Cancel one job without affecting other jobs in its batch."""
        job = self._jobs.get(job_id)
        if job is None or job.terminal or job.task is None:
            return False
        job.task.cancel()
        await asyncio.wait([job.task])
        # A task cancelled before its first step never reaches its own handler.
        await self._finish(job, "cancelled", error="Export was cancelled.")
        return True

    async def close(self) -> None:
        """Cancel all work and leave durable intent to the caller."""
        self._closing = True
        for job in self.jobs:
            await self.cancel(job.id)

    async def _run(self, job: CoordinatorJob) -> None:
        try:
            job.status = "queued"
            await self._notify(job)
            while True:
                rate_limit = None
                async with self._slots:
                    job.status = "running"
                    job.attempt += 1
                    job.started_at = self._now()
                    job.error = None
                    await self._notify(job)
                    try:
                        await job.spec.operation(job.output)
                    except ProtectRateLimitError as exc:
                        rate_limit = exc
                if rate_limit is None:
                    break
                await self._wait_after_rate_limit(job, rate_limit)
            await self._finish(job, "completed")
        except asyncio.CancelledError:
            await self._finish(job, "cancelled", error="Export was cancelled.")
        except Exception as exc:
            await self._finish(job, "failed", error=str(exc) or type(exc).__name__)

    async def _wait_after_rate_limit(self, job: CoordinatorJob, error: ProtectRateLimitError) -> None:
        job.status = "queued"
        job.error = str(error)
        await self._notify(job)
        if error.retry_not_before is not None:
            await asyncio.sleep(max((error.retry_not_before - self._now()).total_seconds(), 0.0))
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self._job_finished.wait(), self._rate_limit_fallback_seconds)

    async def _finish(
        self,
        job: CoordinatorJob,
        status: Literal["completed", "failed", "cancelled"],
        *,
        error: str | None = None,
    ) -> None:
        if job.terminal:
            return
        job.status = status
        job.error = error
        job.finished_at = self._now()
        self._claims.pop(self._output_key(job.output), None)
        self._job_finished.set()
        self._job_finished = asyncio.Event()
        try:
            await self._notify(job)
        except BaseException as exc:
            job.completion.set_exception(exc)
        else:
            job.completion.set_result(job)

    async def _notify(self, job: CoordinatorJob) -> None:
        if self._on_transition is not None:
            await self._on_transition(job)

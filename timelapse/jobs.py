"""Runtime-wide coordination for manual and Daily Automation exports."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from timelapse import ProtectRateLimitError, TimelapseError

DEFAULT_MAX_ACTIVE_EXPORTS = 4
DEFAULT_MAX_QUEUED_EXPORTS = 20
RATE_LIMIT_FALLBACK_SECONDS = 60.0

CollisionPolicy = Literal["reject", "suffix", "daily"]
CoordinatorJobStatus = Literal["pending", "queued", "running", "completed", "failed", "cancelled"]
ExportOperation = Callable[[Path], Awaitable[None]]
ClaimCallback = Callable[[tuple["CoordinatorJob", ...]], Awaitable[None]]
TransitionCallback = Callable[["CoordinatorJob"], Awaitable[None] | None]


class OutputCollisionError(TimelapseError, ValueError):
    """An existing artifact or in-flight Export Job owns the requested path."""


@dataclass(frozen=True)
class ExportJobSpec:
    """One export operation submitted to a process-wide coordinator."""

    id: str
    output: Path
    operation: ExportOperation
    automation_id: str | None = None
    batch_id: str | None = None
    collision_policy: CollisionPolicy = "reject"

    @classmethod
    def create(
        cls,
        *,
        output: Path,
        operation: ExportOperation,
        automation_id: str | None = None,
        batch_id: str | None = None,
        job_id: str | None = None,
        collision_policy: CollisionPolicy = "reject",
    ) -> ExportJobSpec:
        """Build a job spec with a stable caller-supplied or generated ID."""
        return cls(
            job_id or f"job_{secrets.token_urlsafe(12)}", output, operation, automation_id, batch_id, collision_policy
        )


@dataclass
class CoordinatorJob:
    """Observable state for one coordinator-owned export."""

    spec: ExportJobSpec
    output: Path
    status: CoordinatorJobStatus = "pending"
    attempt: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    retry_not_before: datetime | None = None
    completion: asyncio.Future[CoordinatorJob] | None = field(default=None, repr=False)
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
    """Share concurrency, queue, cancellation, and rate-limit policy in one runtime."""

    def __init__(
        self,
        *,
        max_active: int = DEFAULT_MAX_ACTIVE_EXPORTS,
        max_queued: int = DEFAULT_MAX_QUEUED_EXPORTS,
        on_transition: TransitionCallback | None = None,
        now: Callable[[], datetime] | None = None,
        rate_limit_fallback_seconds: float = RATE_LIMIT_FALLBACK_SECONDS,
    ) -> None:
        """Configure bounded runtime capacity and transition observation."""
        if max_active <= 0 or max_queued < 0:
            message = "export capacity requires at least one active slot and a nonnegative queue"
            raise ValueError(message)
        self.max_active = max_active
        self.max_queued = max_queued
        self._on_transition = on_transition
        self._now = now or (lambda: datetime.now(UTC))
        self._rate_limit_fallback_seconds = rate_limit_fallback_seconds
        self._pending: deque[CoordinatorJob] = deque()
        self._jobs: dict[str, CoordinatorJob] = {}
        self._claims: dict[str, str] = {}
        self._active_jobs: set[str] = set()
        self._admitted_jobs: set[str] = set()
        self._active = 0
        self._admitted = 0
        self._terminal_generation = 0
        self._condition = asyncio.Condition()
        self._dispatcher: asyncio.Task[None] | None = None
        self._closing = False

    @classmethod
    def from_environment(
        cls,
        *,
        prefix: str = "TIMELAPSE",
        on_transition: TransitionCallback | None = None,
    ) -> ExportJobCoordinator:
        """Build a coordinator from validated runtime-wide capacity settings."""

        def capacity(name: str, default: int, *, minimum: int) -> int:
            raw = os.environ.get(f"{prefix}_{name}")
            if raw is None:
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                message = f"{prefix}_{name} must be a whole number"
                raise ValueError(message) from exc
            if value < minimum:
                message = f"{prefix}_{name} must be at least {minimum}"
                raise ValueError(message)
            return value

        return cls(
            max_active=capacity("MAX_ACTIVE_EXPORTS", DEFAULT_MAX_ACTIVE_EXPORTS, minimum=1),
            max_queued=capacity("MAX_QUEUED_EXPORTS", DEFAULT_MAX_QUEUED_EXPORTS, minimum=0),
            on_transition=on_transition,
        )

    @property
    def jobs(self) -> tuple[CoordinatorJob, ...]:
        """Return all jobs in submission order."""
        return tuple(self._jobs.values())

    @property
    def active_count(self) -> int:
        """Return jobs whose operation coroutine has started."""
        return self._active

    @property
    def queued_count(self) -> int:
        """Return admitted jobs waiting for an active slot."""
        return self._admitted - self._active

    @property
    def pending_count(self) -> int:
        """Return durable oversized-batch jobs not yet admitted."""
        return len(self._pending)

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
        loop = asyncio.get_running_loop()
        submitted: list[CoordinatorJob] = []
        previous_jobs: dict[str, CoordinatorJob | None] = {}
        async with self._condition:
            if self._closing:
                message = "export coordinator is shutting down"
                raise TimelapseError(message)
            try:
                for spec in specs:
                    previous = self._jobs.get(spec.id)
                    output = self._claim_output(spec)
                    job = CoordinatorJob(
                        spec=spec, output=output, created_at=self._now(), completion=loop.create_future()
                    )
                    previous_jobs[job.id] = previous
                    self._jobs[job.id] = job
                    submitted.append(job)
                result = tuple(submitted)
                if before_admission is not None:
                    await before_admission(result)
                for job in submitted:
                    await self._transition(job)
            except BaseException:
                for job in submitted:
                    self._claims.pop(self._output_key(job.output), None)
                    previous = previous_jobs[job.id]
                    if previous is None:
                        self._jobs.pop(job.id, None)
                    else:
                        self._jobs[job.id] = previous
                raise
            self._pending.extend(submitted)
            self._condition.notify_all()
        self._ensure_dispatcher()
        return result

    @staticmethod
    def _output_key(path: Path) -> str:
        return str(path.expanduser().resolve(strict=False)).casefold()

    def _claim_output(self, spec: ExportJobSpec) -> Path:
        previous = self._jobs.get(spec.id)
        if previous is not None and not previous.terminal:
            message = f"duplicate export job ID: {spec.id}"
            raise ValueError(message)
        if spec.collision_policy not in {"reject", "suffix", "daily"}:
            message = f"unknown output collision policy: {spec.collision_policy}"
            raise ValueError(message)
        preferred = spec.output.expanduser().resolve(strict=False)
        candidate = preferred
        for suffix in range(2, 10_001):
            key = self._output_key(candidate)
            exists = candidate.exists() or candidate.is_symlink()
            if not exists and candidate.parent.exists():
                exists = any(item.name.casefold() == candidate.name.casefold() for item in candidate.parent.iterdir())
            if key not in self._claims and not exists:
                self._claims[key] = spec.id
                return candidate
            if spec.collision_policy != "suffix":
                message = f"An export artifact already exists or is claimed at {candidate}"
                raise OutputCollisionError(message)
            candidate = preferred.with_name(f"{preferred.stem}_{suffix}{preferred.suffix}")
        message = f"could not claim a unique output path for {preferred.name}"
        raise OutputCollisionError(message)

    async def wait(self, jobs: Iterable[CoordinatorJob]) -> tuple[CoordinatorJob, ...]:
        """Wait for the selected jobs without cancelling siblings."""
        selected = tuple(jobs)
        completions = [job.completion for job in selected if job.completion is not None]
        if completions:
            await asyncio.gather(*(asyncio.shield(completion) for completion in completions))
        return selected

    async def cancel(self, job_id: str) -> bool:
        """Cancel one job without affecting other jobs in its batch."""
        job = self._jobs.get(job_id)
        if job is None or job.terminal:
            return False
        if job.status == "pending":
            with suppress(ValueError):
                self._pending.remove(job)
            await self._finish(job, "cancelled", error="Cancelled before admission.")
            return True
        if job.task is not None:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            if not job.terminal:
                await self._finish(
                    job,
                    "cancelled",
                    error="Export was cancelled.",
                )
            return True
        await self._finish(job, "cancelled", error="Cancelled before execution.")
        return True

    async def close(self) -> None:
        """Cancel active and queued work and leave durable intent to the caller."""
        self._closing = True
        dispatcher = self._dispatcher
        if dispatcher is not None:
            dispatcher.cancel()
        jobs = tuple(job for job in self._jobs.values() if not job.terminal)
        for job in jobs:
            await self.cancel(job.id)
        tasks = [job.task for job in jobs if job.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if dispatcher is not None:
            await asyncio.gather(dispatcher, return_exceptions=True)

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="export-coordinator")

    async def _dispatch_loop(self) -> None:
        try:
            while not self._closing:
                async with self._condition:
                    await self._condition.wait_for(
                        lambda: (
                            self._closing
                            or (bool(self._pending) and self._admitted < self.max_active + self.max_queued)
                        )
                    )
                    if self._closing:
                        return
                    job = self._pending.popleft()
                    self._admitted += 1
                    self._admitted_jobs.add(job.id)
                    job.status = "queued"
                try:
                    await self._transition(job)
                except Exception as exc:
                    await self._finish(job, "failed", error=str(exc))
                    continue
                if not job.terminal:
                    job.task = asyncio.create_task(self._execute(job), name=f"export-{job.id}")
        except asyncio.CancelledError:
            return

    async def _execute(self, job: CoordinatorJob) -> None:
        try:
            async with self._condition:
                await self._condition.wait_for(lambda: self._closing or self._active < self.max_active)
                if not self._closing:
                    self._active += 1
                    self._active_jobs.add(job.id)
            if self._closing:
                await self._finish(job, "cancelled", error="Coordinator is shutting down.")
                return
            job.status = "running"
            job.attempt += 1
            job.started_at = self._now()
            job.error = None
            await self._transition(job)
            try:
                await job.spec.operation(job.output)
            except ProtectRateLimitError as exc:
                await self._release_active(job)
                await self._wait_after_rate_limit(job, exc)
                if self._closing:
                    await self._finish(job, "cancelled", error="Coordinator is shutting down.")
                    return
                job.task = None
                job.status = "pending"
                self._pending.appendleft(job)
                self._admitted -= 1
                self._admitted_jobs.discard(job.id)
                await self._transition(job)
                async with self._condition:
                    self._condition.notify_all()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._finish(job, "failed", error=str(exc) or type(exc).__name__)
                return
            await self._finish(job, "completed")
        except asyncio.CancelledError:
            await self._finish(job, "cancelled", error="Export was cancelled.")
        except Exception as exc:
            await self._finish(job, "failed", error=str(exc) or type(exc).__name__)

    async def _wait_after_rate_limit(self, job: CoordinatorJob, error: ProtectRateLimitError) -> None:
        baseline = self._terminal_generation
        retry_not_before = error.retry_not_before
        job.status = "queued"
        job.retry_not_before = retry_not_before
        job.error = str(error)
        await self._transition(job)
        if retry_not_before is not None:
            now = self._now()
            if retry_not_before.tzinfo is None:
                retry_not_before = retry_not_before.replace(tzinfo=UTC)
            delay = max((retry_not_before.astimezone(UTC) - now.astimezone(UTC)).total_seconds(), 0.0)
            if delay:
                await asyncio.sleep(delay)
            return
        try:
            async with asyncio.timeout(self._rate_limit_fallback_seconds):
                async with self._condition:
                    await self._condition.wait_for(lambda: self._terminal_generation > baseline or self._closing)
        except TimeoutError:
            return

    async def _release_active(self, job: CoordinatorJob) -> None:
        async with self._condition:
            if job.id in self._active_jobs:
                self._active -= 1
                self._active_jobs.discard(job.id)
            self._condition.notify_all()

    async def _finish(
        self,
        job: CoordinatorJob,
        status: Literal["completed", "failed", "cancelled"],
        *,
        error: str | None = None,
    ) -> None:
        async with self._condition:
            if job.terminal:
                return
            with suppress(ValueError):
                self._pending.remove(job)
            if job.id in self._active_jobs:
                self._active -= 1
                self._active_jobs.discard(job.id)
            if job.id in self._admitted_jobs:
                self._admitted -= 1
                self._admitted_jobs.discard(job.id)
            job.status = status
            job.error = error
            job.finished_at = self._now()
            job.retry_not_before = None
            self._claims.pop(self._output_key(job.output), None)
            self._terminal_generation += 1
            self._condition.notify_all()
        try:
            await self._transition(job)
        except BaseException as exc:
            if job.completion is not None and not job.completion.done():
                job.completion.set_exception(exc)
        else:
            if job.completion is not None and not job.completion.done():
                job.completion.set_result(job)
        if not self._closing:
            self._ensure_dispatcher()

    async def _transition(self, job: CoordinatorJob) -> None:
        if self._on_transition is None:
            return
        result = self._on_transition(job)
        if result is not None:
            await result


def retry_not_before_from_delay(delay_seconds: float | None, *, now: datetime | None = None) -> datetime | None:
    """Convert a Retry-After delay to its durable UTC instant."""
    if delay_seconds is None:
        return None
    return (now or datetime.now(UTC)).astimezone(UTC) + timedelta(seconds=max(delay_seconds, 0.0))

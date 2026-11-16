"""Run Daily Automations: due days, attempts, backoff and re-export."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from timelapse import ConflictError, InvalidRequestError, NotFoundError, TimelapseError
from timelapse.download import is_valid_mp4
from timelapse.jobs import JobQueue, Run
from timelapse.model import Camera, ExportRequest, day_bounds, latest_complete_day
from timelapse.registry import Automation, Registry

LOGGER = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def export_run(protect: Any, request: ExportRequest) -> Run:
    """Return the job body that exports ``request`` through ``protect``."""
    return lambda output, progress: protect.export(request, output, progress)


class _Paused(Exception):  # noqa: N818
    """The automation was paused; the day stops here."""


class DailyRunner:
    """Exports each completed day for every active automation."""

    def __init__(
        self,
        registry: Registry,
        queue: JobQueue,
        *,
        connect: Callable[[Automation], Any],
        storage_problem: Callable[[], str | None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.registry = registry
        self.queue = queue
        self.connect = connect
        self.storage_problem = storage_problem
        self.now = now
        self.sleep = sleep
        self.jitter = jitter
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._idle: set[str] = set()

    def _active(self, automation_id: str) -> Automation | None:
        automation = self.registry.get(automation_id)
        return automation if automation is not None and automation.status == "active" else None

    async def run_due_once(self, automation_id: str) -> bool:
        """Run ``next_day`` if it is complete; return whether it became a Processed Day."""
        automation = self._active(automation_id)
        if automation is None:
            return False
        if automation.next_day > latest_complete_day(self.now(), ZoneInfo(automation.timezone)):
            return False
        return await self._run_day(automation_id, automation.next_day, advance=True)

    async def reexport(self, automation_id: str, day: date) -> bool:
        """Export ``day`` again for cameras whose artifact is missing or invalid."""
        automation = self.registry.get(automation_id)
        if automation is None:
            raise NotFoundError(f"automation does not exist: {automation_id}")
        if automation.status != "active":
            raise InvalidRequestError("resume the automation before re-exporting a day")
        stamp = self.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        valid = True
        for _, path in self._artifacts(automation, day):
            if path.exists() and not is_valid_mp4(path):
                path.rename(path.with_name(f"{path.name}.invalid-{stamp}"))
            valid = valid and is_valid_mp4(path)
        if valid:
            return False
        return await self._run_day(automation_id, day, advance=False)

    def _artifacts(self, automation: Automation, day: date) -> list[tuple[ExportRequest, Path]]:
        start, end = day_bounds(day, ZoneInfo(automation.timezone))
        requests = [ExportRequest(camera, start, end, automation.speed, full_day=True) for camera in automation.cameras]
        return [(request, automation.output_directory / request.filename) for request in requests]

    def _pause(self, automation_id: str, message: str) -> _Paused:
        if (current := self.registry.get(automation_id)) is not None:
            self.registry.put(current.paused(message))
        return _Paused(message)

    async def _run_day(self, automation_id: str, day: date, *, advance: bool) -> bool:
        while True:
            automation = self._active(automation_id)
            if automation is None:
                return False
            if automation.next_retry_at is not None:
                wait = (automation.next_retry_at - self.now()).total_seconds()
                if wait > 0:
                    await self.sleep(wait)
                    continue
            try:
                error = await self._attempt(automation, day)
            except _Paused:
                return False
            current = self.registry.get(automation_id)
            if current is None:
                return False
            if error is None:
                self.registry.put(current.succeeded(day, advance=advance))
                return True
            attempt = current.consecutive_failures + 1
            if attempt >= MAX_ATTEMPTS:
                self.registry.put(current.failed(attempt, error, None).paused(f"{error} Paused after 5 attempts."))
                return False
            if current.status != "active":
                self.registry.put(current.failed(attempt, error, None))
                return False
            backoff = 60.0 * 2 ** (attempt - 1)
            retry_at = self.now() + timedelta(seconds=backoff + self.jitter(0, backoff * 0.2))
            self.registry.put(current.failed(attempt, error, retry_at))

    async def _attempt(self, automation: Automation, day: date) -> str | None:  # noqa: PLR0912
        """Make one attempt at ``day``; return the failure text, or None when every artifact is valid."""
        kind, value = automation.connection
        try:
            protect = self.connect(automation)
        except Exception as error:
            message = f"Connection reference {kind}:{value} could not be resolved: {error}"
            raise self._pause(automation.id, message) from error
        directory = automation.output_directory
        try:
            next(directory.iterdir(), None)
        except OSError as error:
            raise self._pause(automation.id, f"The output directory is missing or unreadable: {error}") from error
        try:
            cameras: list[Camera] = await protect.cameras()
        except Exception as error:
            return str(error) or type(error).__name__
        current = {camera.id: camera for camera in cameras}
        for camera in automation.cameras:
            if camera.id not in current:
                message = f"Camera '{camera.name}' ({camera.id}) is unavailable. Edit the automation, then Resume it."
                raise self._pause(automation.id, message)
        artifacts = self._artifacts(automation, day)
        missing = []
        for request, path in artifacts:
            if path.exists() and not is_valid_mp4(path):
                raise self._pause(automation.id, f"Invalid artifact collision requires review: {path}")
            if not path.exists():
                missing.append((request, path, export_run(protect, request)))
        if not missing:
            return None
        if self.storage_problem is not None and (problem := self.storage_problem()):
            raise self._pause(automation.id, problem)
        try:
            jobs = self.queue.submit(missing, daily=True)
        except ConflictError as error:
            raise self._pause(automation.id, f"Export Artifact collision requires review: {error}") from error
        except TimelapseError as error:
            return str(error)
        await self.queue.wait(jobs)
        failed = [job for job in jobs if job.status != "completed" or not is_valid_mp4(job.output)]
        if not failed:
            return None
        details = "; ".join(f"{job.request.camera.name}: {job.error or 'the artifact is not valid'}" for job in failed)
        return f"{len(failed)} Export Job(s) failed: {details}"

    def start(self, automation_id: str) -> None:
        """Start the background loop for an automation, or wake one that is waiting for midnight."""
        task = self._loops.get(automation_id)
        if task is not None and not task.done():
            if automation_id not in self._idle:
                return
            task.cancel()
        self._loops[automation_id] = asyncio.create_task(self._loop(automation_id))

    async def _loop(self, automation_id: str) -> None:
        while (automation := self._active(automation_id)) is not None:
            try:
                if await self.run_due_once(automation_id):
                    continue
            except Exception:
                LOGGER.exception("Daily Automation %s failed unexpectedly", automation.name)
                await self.sleep(60)
                continue
            if (automation := self._active(automation_id)) is None:
                return
            tz = ZoneInfo(automation.timezone)
            now = self.now().astimezone(tz)
            midnight = datetime.combine(now.date() + timedelta(days=1), time(), tz)
            self._idle.add(automation_id)
            try:
                await self.sleep(max(1.0, midnight.timestamp() - now.timestamp()))
            finally:
                self._idle.discard(automation_id)

    async def wait(self) -> None:
        """Return once every loop has ended."""
        while pending := [task for task in self._loops.values() if not task.done()]:
            await asyncio.wait(pending)

    async def close(self) -> None:
        """Stop every loop."""
        for task in self._loops.values():
            task.cancel()
        await asyncio.gather(*self._loops.values(), return_exceptions=True)

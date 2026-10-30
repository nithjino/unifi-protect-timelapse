"""Daily Automation policy and calendar-day helpers."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from timelapse.automation_registry import (
    AutomationCamera,
    AutomationRegistry,
    DailyAutomation,
    RegistryError,
    canonical_output_directory,
)
from timelapse.config import Config, ConnectionSettings
from timelapse.download import default_output_path, validate_mp4
from timelapse.jobs import CoordinatorJob, ExportJobCoordinator, ExportJobSpec, OutputCollisionError
from timelapse.protect import CameraInfo

BATCH_MAX_ATTEMPTS = 5
BATCH_BACKOFF_SECONDS = (60.0, 120.0, 240.0, 480.0)
BATCH_JITTER_RATIO = 0.2


@dataclass(frozen=True)
class ResolvedAutomation:
    """Credentials and current cameras resolved immediately before a batch."""

    connection: ConnectionSettings
    cameras: tuple[CameraInfo, ...]


ConnectionResolver = Callable[[DailyAutomation], Awaitable[ResolvedAutomation]]
Exporter = Callable[[Config, CameraInfo, Path], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


def local_day_bounds(day: date, timezone: ZoneInfo | str) -> tuple[datetime, datetime]:
    """Return timezone-aware local midnights surrounding one calendar day."""
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start, end


def latest_complete_local_day(now: datetime, timezone: str) -> date:
    """Return the most recent calendar day that fully elapsed in one timezone."""
    return now.astimezone(ZoneInfo(timezone)).date() - timedelta(days=1)


def daily_output_path(config: Config, camera: CameraInfo, directory: Path) -> Path:
    """Build the canonical output path for a completed calendar day."""
    return directory / default_output_path(replace(config, full_day=True), camera).name


def seconds_until_next_local_day(now: datetime, timezone: str) -> float:
    """Return the delay until the next midnight in one timezone."""
    local_now = now.astimezone(ZoneInfo(timezone))
    next_midnight = local_day_bounds(local_now.date(), timezone)[1]
    return max((next_midnight - local_now).total_seconds(), 1.0)


class DailyAutomationEngine:
    """Own catch-up, day retries, and Daily Automation lifecycle transitions."""

    def __init__(
        self,
        registry: AutomationRegistry,
        coordinator: ExportJobCoordinator,
        *,
        resolve_connection: ConnectionResolver,
        exporter: Exporter,
        now: Callable[[], datetime] | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        """Configure the registry, coordinator, and entry-point operations."""
        self.registry = registry
        self.coordinator = coordinator
        self._resolve_connection = resolve_connection
        self._exporter = exporter
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._jitter = jitter

    async def run_due_once(self, selector: str) -> bool:
        """Process the oldest due day and return whether every artifact was produced."""
        automation = self.registry.resolve(selector)
        if automation.status != "active":
            return False
        if automation.next_day > latest_complete_local_day(self._now(), automation.timezone):
            return False
        return await self._run_day(automation.id, automation.next_day, advance_day=True)

    async def run_forever(self, selector: str | None = None) -> None:
        """Run one selected automation or all active registry automations."""
        while True:
            automations = [self.registry.resolve(selector)] if selector is not None else self.registry.list()
            progressed = [await self.run_due_once(automation.id) for automation in automations]
            if any(progressed):
                continue
            delays = [
                seconds_until_next_local_day(self._now(), item.timezone)
                for item in automations
                if item.status == "active"
            ]
            await self._sleep(min(delays, default=60.0))

    async def reexport(self, selector: str, day: date) -> bool:
        """Replace missing or damaged artifacts without rewinding Processed Days."""
        automation = self.registry.resolve(selector)
        if automation.status != "active":
            message = "resume the automation before re-exporting a day"
            raise RegistryError(message)
        resolved = await self._resolve_or_pause(automation)
        if resolved is None:
            return False
        by_id = {camera.id: camera for camera in resolved.cameras}
        config = self._batch_config(resolved.connection, automation, day)
        needs_work = False
        for selected in automation.cameras:
            camera = by_id.get(selected.id)
            if camera is None:
                self._pause_missing_camera(automation, selected)
                return False
            output = daily_output_path(config, camera, automation.output_path)
            if validate_mp4(output):
                continue
            needs_work = True
            if output.exists():
                stamp = self._now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
                try:
                    output.replace(output.with_name(f"{output.name}.invalid-{stamp}"))
                except OSError as exc:
                    self.registry.pause(automation.id, f"Could not quarantine invalid artifact {output}: {exc}")
                    return False
        return needs_work and await self._run_day(automation.id, day, advance_day=False)

    async def remove(self, selector: str) -> None:
        """Forget an automation after its submitted work drains."""
        automation = self.registry.begin_remove(selector)
        active = [job for job in self.coordinator.jobs if job.spec.automation_id == automation.id and not job.terminal]
        await self.coordinator.wait(active)
        self.registry.delete(automation.id)

    async def _run_day(self, automation_id: str, day: date, *, advance_day: bool) -> bool:  # noqa: PLR0911
        first_attempt = min(self.registry.resolve(automation_id).consecutive_failures + 1, BATCH_MAX_ATTEMPTS)
        for attempt in range(first_attempt, BATCH_MAX_ATTEMPTS + 1):
            retry_at = self.registry.resolve(automation_id).next_retry_at
            if retry_at is not None:
                await self._sleep(max((retry_at - self._now()).total_seconds(), 0.0))
            if not self.registry.contains(automation_id):
                return False
            automation = self.registry.resolve(automation_id)
            if automation.status != "active":
                return False
            jobs = await self._submit_day(automation, day)
            if jobs is None:
                return False
            await self.coordinator.wait(jobs)

            if not self.registry.contains(automation_id):
                return False
            failures = [job for job in jobs if job.status != "completed" or not validate_mp4(job.output)]
            if not failures:
                self.registry.record_success(automation_id, day, advance_day=advance_day)
                return True
            error = "; ".join(f"{job.spec.camera_name}: {job.error or job.status}" for job in failures)
            error = f"{len(failures)} Export Job(s) failed: {error}"
            if attempt >= BATCH_MAX_ATTEMPTS:
                self.registry.pause(automation_id, f"{error} Paused after {BATCH_MAX_ATTEMPTS} attempts.")
                return False
            active = self.registry.resolve(automation_id).status == "active"
            delay = BATCH_BACKOFF_SECONDS[attempt - 1]
            delay += self._jitter(0, delay * BATCH_JITTER_RATIO)
            self.registry.record_failure(
                automation_id,
                attempt=attempt,
                error=error,
                retry_at=self._now().astimezone(UTC) + timedelta(seconds=delay) if active else None,
            )
        return False

    async def _submit_day(self, automation: DailyAutomation, day: date) -> tuple[CoordinatorJob, ...] | None:
        """Submit every missing artifact, or pause and return None when review is needed."""
        resolved = await self._resolve_or_pause(automation)
        if resolved is None:
            return None
        try:
            canonical_output_directory(automation.output_path)
        except RegistryError as exc:
            self.registry.pause(automation.id, str(exc))
            return None
        by_id = {camera.id: camera for camera in resolved.cameras}
        config = self._batch_config(resolved.connection, automation, day)
        specs: list[ExportJobSpec] = []
        for selected in automation.cameras:
            camera = by_id.get(selected.id)
            if camera is None:
                self._pause_missing_camera(automation, selected)
                return None
            output = daily_output_path(config, camera, automation.output_path)
            if output.exists():
                if validate_mp4(output):
                    continue
                self.registry.pause(automation.id, f"Invalid artifact collision requires review: {output}")
                return None
            specs.append(
                ExportJobSpec.create(
                    output=output,
                    operation=partial(self._exporter, config, camera),
                    automation_id=automation.id,
                    job_id=f"batch:{automation.id}:{day.isoformat()}:{selected.id}",
                    config=config,
                    camera=camera,
                )
            )
        try:
            return await self.coordinator.submit(specs)
        except OutputCollisionError as exc:
            self.registry.pause(automation.id, f"Export Artifact collision requires review: {exc}")
            return None

    @staticmethod
    def _batch_config(connection: ConnectionSettings, automation: DailyAutomation, day: date) -> Config:
        start, end = local_day_bounds(day, automation.timezone)
        return Config.from_connection(connection, speed=automation.speed, start=start, end=end, full_day=True)

    async def _resolve_or_pause(self, automation: DailyAutomation) -> ResolvedAutomation | None:
        try:
            return await self._resolve_connection(automation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.registry.pause(
                automation.id,
                f"Connection reference {automation.connection.kind}:{automation.connection.value} "
                f"could not be resolved: {exc}",
            )
            return None

    def _pause_missing_camera(self, automation: DailyAutomation, camera: AutomationCamera) -> None:
        self.registry.pause(
            automation.id,
            f"Camera {camera.name!r} ({camera.id}) is unavailable. Edit the automation, then Resume it.",
        )

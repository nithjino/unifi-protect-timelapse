"""Daily Automation policy and calendar-day helpers."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from timelapse import TimelapseError
from timelapse.automation_registry import (
    AutomationCamera,
    AutomationRegistry,
    DailyAutomation,
    ExportBatchRecord,
    ExportJobRecord,
    RegistryError,
    canonical_output_directory,
    validate_mp4,
)
from timelapse.config import Config
from timelapse.download import default_output_path
from timelapse.jobs import CoordinatorJob, ExportJobCoordinator, ExportJobSpec
from timelapse.protect import CameraInfo

BATCH_MAX_ATTEMPTS = 5
BATCH_BACKOFF_SECONDS = (60.0, 120.0, 240.0, 480.0)
BATCH_JITTER_RATIO = 0.2


@dataclass(frozen=True)
class ResolvedAutomation:
    """Credentials and current cameras resolved immediately before a batch."""

    config: Config
    cameras: tuple[CameraInfo, ...]


@dataclass(frozen=True)
class BatchResult:
    """Result of processing one automation-day batch."""

    automation_id: str
    day: date
    generation: int
    completed: bool
    jobs: tuple[ExportJobRecord, ...]


ConnectionResolver = Callable[[DailyAutomation], Awaitable[ResolvedAutomation]]
Exporter = Callable[[Config, CameraInfo, Path], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


def local_day_bounds(day: date, timezone: ZoneInfo | str | None = None) -> tuple[datetime, datetime]:
    """Return timezone-aware local midnights surrounding one calendar day."""
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    if zone is None:
        start = datetime.combine(day, time.min).astimezone()
        end = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    else:
        start = datetime.combine(day, time.min, tzinfo=zone)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start, end


def latest_complete_local_day(now: datetime | None = None, timezone: ZoneInfo | str | None = None) -> date:
    """Return the most recent calendar day that fully elapsed in one timezone."""
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    local_now = now or datetime.now().astimezone()
    local_now = local_now.astimezone(zone) if zone is not None else local_now.astimezone()
    return local_now.date() - timedelta(days=1)


def daily_output_path(config: Config, camera: CameraInfo, directory: Path) -> Path:
    """Build the canonical output path for a completed calendar day."""
    return directory / default_output_path(replace(config, full_day=True), camera).name


def config_for_local_day(config: Config, day: date, timezone: ZoneInfo | str | None = None) -> Config:
    """Copy runtime settings with one completed calendar day as the range."""
    start, end = local_day_bounds(day, timezone)
    return replace(config, start=start, end=end, output=None, full_day=True)


def seconds_until_next_local_day(
    now: datetime | None = None,
    timezone: ZoneInfo | str | None = None,
) -> float:
    """Return the delay until the next midnight in one timezone."""
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    local_now = now or datetime.now().astimezone()
    local_now = local_now.astimezone(zone) if zone is not None else local_now.astimezone()
    next_midnight = local_day_bounds(local_now.date(), zone)[1]
    return max((next_midnight - local_now).total_seconds(), 1.0)


class DailyAutomationEngine:
    """Own catch-up, batch retries, and Daily Automation lifecycle transitions."""

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
        self._transition_lock = asyncio.Lock()

    async def run_due_once(self, selector: str) -> BatchResult | None:
        """Process the oldest due day for one active automation."""
        automation = self.registry.resolve(selector)
        batch_id = f"batch:{automation.id}:{automation.next_day.isoformat()}:0"
        recovering_submitted_batch = self._batch_needs_recovery(batch_id)
        if automation.status != "active" and not (
            automation.status in {"stopped", "removing"} and recovering_submitted_batch
        ):
            return None
        latest = latest_complete_local_day(self._now(), automation.timezone)
        if automation.next_day > latest and not recovering_submitted_batch:
            return None
        return await self._run_batch(automation, automation.next_day, generation=0)

    async def run_all_due_once(self) -> tuple[BatchResult, ...]:
        """Give every active automation one oldest-first catch-up turn."""
        results: list[BatchResult] = []
        for automation in self.registry.list():
            result = await self.run_due_once(automation.id)
            if result is not None:
                results.append(result)
        return tuple(results)

    async def run_forever(self, selector: str | None = None) -> None:
        """Run one selected automation or all active registry automations."""
        while True:
            progressed = False
            automations = [self.registry.resolve(selector)] if selector is not None else self.registry.list()
            for automation in automations:
                result = await self.run_due_once(automation.id)
                progressed = progressed or result is not None
            if progressed:
                continue
            delays = [
                seconds_until_next_local_day(self._now(), item.timezone)
                for item in automations
                if item.status == "active"
            ]
            await self._sleep(min(delays, default=60.0))

    async def reexport(self, selector: str, day: date) -> BatchResult | None:
        """Schedule missing or damaged artifacts without rewinding Processed Days."""
        automation = self.registry.resolve(selector)
        if automation.status == "paused":
            message = "resume the paused automation before re-exporting a day"
            raise RegistryError(message)
        if automation.status == "removing":
            message = "an automation being removed cannot re-export artifacts"
            raise RegistryError(message)
        resolved = await self._resolve_or_pause(automation)
        if resolved is None:
            return None
        by_id = {camera.id: camera for camera in resolved.cameras}
        config = config_for_local_day(resolved.config, day, automation.timezone)
        needs_work = False
        for selected in automation.cameras:
            camera = by_id.get(selected.id)
            if camera is None:
                self._pause_missing_camera(automation, selected)
                return None
            output = daily_output_path(config, camera, automation.output_path)
            if not output.exists():
                needs_work = True
                continue
            if validate_mp4(output):
                continue
            tracked = next(
                (
                    job
                    for job in self.registry.state.jobs.values()
                    if job.automation_id == automation.id and Path(job.output) == output
                ),
                None,
            )
            if tracked is None:
                self.registry.pause(automation.id, f"Untracked artifact collision requires review: {output}")
                return None
            quarantine = output.with_name(
                f"{output.name}.invalid-{self._now().astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            )
            try:
                output.replace(quarantine)
            except OSError as exc:
                self.registry.pause(automation.id, f"Could not quarantine invalid artifact {output}: {exc}")
                return None
            needs_work = True
        if not needs_work:
            return None
        generation = self.registry.next_reexport_generation(automation.id, day)
        return await self._run_batch(automation, day, generation=generation, resolved=resolved, advance_day=False)

    async def stop(self, selector: str) -> DailyAutomation:
        """Serialize Stop against batch completion."""
        async with self._transition_lock:
            return self.registry.stop(selector)

    async def resume(self, selector: str) -> DailyAutomation:
        """Resume a paused or stopped automation."""
        async with self._transition_lock:
            return self.registry.resume(selector)

    async def remove(self, selector: str) -> DailyAutomation | None:
        """Start removal and finish it after submitted work drains."""
        async with self._transition_lock:
            automation = self.registry.begin_remove(selector)
        if automation is None:
            return None
        active = [job for job in self.coordinator.jobs if job.spec.automation_id == automation.id and not job.terminal]
        await self.coordinator.wait(active)
        return self.registry.finish_remove(automation.id)

    async def _run_batch(  # noqa: PLR0911, PLR0912, PLR0915 - one serialized batch lifecycle
        self,
        automation: DailyAutomation,
        day: date,
        *,
        generation: int,
        resolved: ResolvedAutomation | None = None,
        advance_day: bool = True,
    ) -> BatchResult | None:
        batch_id = f"batch:{automation.id}:{day.isoformat()}:{generation}"
        recovering_submitted_batch = self._batch_needs_recovery(batch_id)
        current_at_start = self.registry.resolve(automation.id)
        if current_at_start.status == "paused" or (
            current_at_start.status in {"stopped", "removing"} and not recovering_submitted_batch
        ):
            return None
        automation = current_at_start
        resolved = resolved or await self._resolve_or_pause(automation)
        if resolved is None:
            return None
        try:
            canonical_output_directory(automation.output_path)
        except RegistryError as exc:
            self.registry.pause(automation.id, str(exc))
            return None
        by_id = {camera.id: camera for camera in resolved.cameras}
        for selected in automation.cameras:
            if selected.id not in by_id:
                self._pause_missing_camera(automation, selected)
                return None

        last_jobs: tuple[ExportJobRecord, ...] = ()
        for attempt in range(1, BATCH_MAX_ATTEMPTS + 1):
            current = self.registry.resolve(automation.id)
            if current.status == "paused" or (current.status == "removing" and not recovering_submitted_batch):
                return None
            if current.status == "stopped" and (attempt > 1 or not recovering_submitted_batch):
                return BatchResult(current.id, day, generation, completed=False, jobs=last_jobs)
            resolved = await self._resolve_or_pause(current)
            if resolved is None:
                return None
            by_id = {camera.id: camera for camera in resolved.cameras}
            config = config_for_local_day(resolved.config, day, current.timezone)
            now = self._now()
            records: list[ExportJobRecord] = []
            specs: list[ExportJobSpec] = []
            for selected in current.cameras:
                camera = by_id.get(selected.id)
                if camera is None:
                    self._pause_missing_camera(current, selected)
                    return None
                output = daily_output_path(config, camera, current.output_path)
                job_id = f"{batch_id}:{selected.id}"
                existing = self.registry.state.jobs.get(job_id)
                if output.exists():
                    if validate_mp4(output):
                        records.append(
                            replace(existing, status="completed", error=None, updated_at=now)
                            if existing is not None
                            else ExportJobRecord(
                                id=job_id,
                                batch_id=batch_id,
                                automation_id=current.id,
                                camera=selected,
                                output=str(output),
                                status="completed",
                                attempt=attempt,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                        continue
                    self.registry.pause(current.id, f"Invalid artifact collision requires review: {output}")
                    return None
                record = ExportJobRecord(
                    id=job_id,
                    batch_id=batch_id,
                    automation_id=current.id,
                    camera=selected,
                    output=str(output),
                    status="pending",
                    attempt=attempt,
                    created_at=existing.created_at if existing else now,
                    updated_at=now,
                )
                records.append(record)

                async def operation(
                    *,
                    batch_config: Config = config,
                    batch_camera: CameraInfo = camera,
                    batch_output: Path = output,
                ) -> None:
                    await self._exporter(batch_config, batch_camera, batch_output)
                    if not validate_mp4(batch_output):
                        message = f"export did not produce a valid MP4: {batch_output}"
                        raise TimelapseError(message)

                specs.append(
                    ExportJobSpec.create(
                        output=output,
                        operation=operation,
                        automation_id=current.id,
                        batch_id=batch_id,
                        job_id=job_id,
                    )
                )

            previous_batch = self.registry.state.batches.get(batch_id)
            batch = ExportBatchRecord(
                id=batch_id,
                automation_id=current.id,
                day=day,
                generation=generation,
                attempt=attempt,
                created_at=previous_batch.created_at if previous_batch is not None else now,
            )
            self.registry.put_batch(batch, tuple(records))
            coordinator_jobs = await self.coordinator.submit(specs)
            await self.coordinator.wait(coordinator_jobs)
            runtime_by_id = {job.id: job for job in coordinator_jobs}
            terminal_records: list[ExportJobRecord] = []
            retry_floor: datetime | None = None
            for record in records:
                runtime = runtime_by_id.get(record.id)
                terminal = record if runtime is None else self._terminal_record(record, runtime)
                if terminal.retry_not_before is not None and (
                    retry_floor is None or terminal.retry_not_before > retry_floor
                ):
                    retry_floor = terminal.retry_not_before
                self.registry.update_job(terminal)
                terminal_records.append(terminal)
            last_jobs = tuple(terminal_records)
            success = all(record.status == "completed" and validate_mp4(Path(record.output)) for record in last_jobs)
            if success:
                await self._complete_batch(current.id, day, advance_day=advance_day)
                return BatchResult(current.id, day, generation, completed=True, jobs=last_jobs)

            current = self.registry.resolve(current.id)
            if current.status in {"stopped", "removing"}:
                updated = replace(
                    current,
                    consecutive_failures=attempt,
                    last_error=self._batch_error(last_jobs),
                    next_retry_at=None,
                )
                self.registry.replace_automation(updated)
                if current.status == "removing":
                    self.registry.finish_remove(current.id)
                return BatchResult(current.id, day, generation, completed=False, jobs=last_jobs)
            if attempt >= BATCH_MAX_ATTEMPTS:
                self.registry.pause(
                    current.id,
                    f"{self._batch_error(last_jobs)} Paused after {BATCH_MAX_ATTEMPTS} batch attempts.",
                )
                return BatchResult(current.id, day, generation, completed=False, jobs=last_jobs)
            delay = BATCH_BACKOFF_SECONDS[attempt - 1]
            delay += self._jitter(0, delay * BATCH_JITTER_RATIO)
            retry_at = self._now().astimezone(UTC) + timedelta(seconds=delay)
            if retry_floor is not None and retry_floor.astimezone(UTC) > retry_at:
                retry_at = retry_floor.astimezone(UTC)
            updated = replace(
                current,
                consecutive_failures=attempt,
                next_retry_at=retry_at,
                last_error=self._batch_error(last_jobs),
            )
            self.registry.replace_automation(updated)
            await self._sleep(max((retry_at - self._now().astimezone(UTC)).total_seconds(), 0.0))
        message = "unreachable batch retry state"
        raise AssertionError(message)

    async def _resolve_or_pause(self, automation: DailyAutomation) -> ResolvedAutomation | None:
        try:
            return await self._resolve_connection(automation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.registry.pause(
                automation.id,
                f"Connection reference {automation.connection.kind}:{automation.connection.value} "
                "could not be resolved: "
                f"{exc}",
            )
            return None

    def _pause_missing_camera(self, automation: DailyAutomation, camera: AutomationCamera) -> None:
        self.registry.pause(
            automation.id,
            f"Camera {camera.name!r} ({camera.id}) is unavailable. Edit the automation, then Resume it.",
        )

    async def _complete_batch(self, automation_id: str, day: date, *, advance_day: bool) -> None:
        async with self._transition_lock:
            automation = self.registry.resolve(automation_id)
            updated = replace(
                automation,
                next_day=day + timedelta(days=1) if advance_day else automation.next_day,
                consecutive_failures=0,
                next_retry_at=None,
                last_error=None,
            )
            self.registry.replace_automation(updated)
            if automation.status == "removing":
                self.registry.finish_remove(automation_id)

    @staticmethod
    def _terminal_record(record: ExportJobRecord, runtime: CoordinatorJob) -> ExportJobRecord:
        status = runtime.status
        if status == "completed" and not validate_mp4(Path(record.output)):
            status = "failed"
        return replace(
            record,
            status=status,
            attempt=runtime.attempt,
            updated_at=runtime.finished_at or datetime.now(UTC),
            error=runtime.error,
            retry_not_before=runtime.retry_not_before,
        )

    @staticmethod
    def _batch_error(jobs: tuple[ExportJobRecord, ...]) -> str:
        failures = [job for job in jobs if job.status != "completed"]
        if not failures:
            return "Export Batch did not produce every expected artifact."
        details = "; ".join(f"{job.camera.name}: {job.error or job.status}" for job in failures)
        return f"{len(failures)} Export Job(s) failed: {details}"

    def _batch_needs_recovery(self, batch_id: str) -> bool:
        if batch_id not in self.registry.state.batches:
            return False
        jobs = [job for job in self.registry.state.jobs.values() if job.batch_id == batch_id]
        return bool(jobs) and (
            all(job.status == "completed" for job in jobs)
            or any(job.status in {"pending", "queued", "running"} for job in jobs)
        )

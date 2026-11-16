import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.fakes import MP4, Clock, FakeProtect
from timelapse import InvalidRequestError, TimelapseError
from timelapse.daily import DailyRunner
from timelapse.jobs import JobQueue
from timelapse.model import Camera, ExportRequest, day_bounds
from timelapse.registry import Automation, Registry

NOW = datetime(2026, 7, 13, 15, tzinfo=UTC)
YESTERDAY = date(2026, 7, 12)
FRONT, YARD = Camera("camera-1", "Front Door"), Camera("camera-2", "Yard")


class World:
    """A registry, queue, fake console, clock and recording sleep in one place."""

    def __init__(self, tmp_path: Path, cameras=(FRONT,), **changes) -> None:
        self.clock = Clock(NOW)
        self.sleeps: list[float] = []
        self.out = tmp_path / "out"
        self.out.mkdir(parents=True)
        self.registry = Registry(tmp_path / "automations.json")
        self.protect = FakeProtect([FRONT, YARD])
        self.queue = JobQueue(max_active=2, now=self.clock)
        self.problem: str | None = None
        self.connect_error: Exception | None = None
        self.block_sleep = False
        self.automation = self.registry.add(
            name="Daily", cameras=list(cameras), connection=("web-environment", "default"), speed="600x",
            output_directory=self.out, timezone=changes.pop("timezone", "UTC"), now=self.clock(),
        )  # fmt: skip
        if changes:
            self.automation = self.registry.put(replace(self.automation, **changes))
        self.runner = self.make_runner()

    def make_runner(self) -> DailyRunner:
        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.clock.advance(seconds)
            if self.block_sleep:
                await asyncio.Event().wait()

        def connect(automation: Automation) -> FakeProtect:
            if self.connect_error:
                raise self.connect_error
            return self.protect

        return DailyRunner(self.registry, self.queue, connect=connect, storage_problem=lambda: self.problem,
                           now=self.clock, sleep=sleep, jitter=lambda _a, _b: 0.0)  # fmt: skip

    @property
    def current(self) -> Automation:
        automation = self.registry.get(self.automation.id)
        assert automation is not None
        return automation

    def run(self) -> bool:
        return asyncio.run(self.runner.run_due_once(self.automation.id))

    def artifact(self, camera: Camera = FRONT, day: date = YESTERDAY) -> Path:
        start, end = day_bounds(day, ZoneInfo("UTC"))
        return self.out / ExportRequest(camera, start, end, "600x", full_day=True).filename


def test_dr1_only_due_active_days_run(tmp_path):
    world = World(tmp_path, next_day=date(2026, 7, 13))
    assert world.run() is False
    world = World(tmp_path / "b", status="stopped")
    assert world.run() is False
    assert world.protect.exports == []
    world = World(tmp_path / "c")
    assert world.run() is True
    assert world.current.next_day == date(2026, 7, 13)
    assert world.current.last_completed_day == YESTERDAY
    assert world.artifact().exists()


def test_dr2_full_day_request(tmp_path):
    world = World(tmp_path, timezone="America/New_York", next_day=date(2026, 3, 8))
    assert world.run()
    [request] = world.protect.exports
    assert (request.speed, request.full_day) == ("600x", True)
    assert request.start.isoformat() == "2026-03-08T00:00:00-05:00"
    assert request.end.isoformat() == "2026-03-09T00:00:00-04:00"
    assert (world.out / request.filename).exists()


@pytest.mark.parametrize("case", ["connect", "directory", "camera", "invalid", "storage", "conflict"])
def test_dr3_pauses_without_consuming_attempts(tmp_path, case):
    world = World(tmp_path, cameras=(FRONT, YARD))
    expected = {
        "connect": "Connection reference web-environment:default could not be resolved: no profile",
        "directory": "missing or unreadable",
        "camera": "Camera 'Yard' (camera-2) is unavailable. Edit the automation, then Resume it.",
        "invalid": "Invalid artifact collision requires review: ",
        "storage": "Export storage is full",
        "conflict": "Export Artifact collision requires review: ",
    }[case]
    gate = asyncio.Event()
    if case == "connect":
        world.connect_error = TimelapseError("no profile")
    elif case == "directory":
        world.out.rmdir()
    elif case == "camera":
        world.protect.camera_list = [FRONT]
    elif case == "invalid":
        world.artifact(YARD).write_bytes(b"junk")
    elif case == "storage":
        world.problem = "Export storage is full (1 of 1)."

    async def scenario():
        other = None
        if case == "conflict":

            async def hold(output: Path, progress) -> None:
                await gate.wait()
                output.write_bytes(MP4)

            other = world.queue.submit([(ExportRequest(YARD, NOW, NOW, "600x"), world.artifact(YARD), hold)])[0]
        result = await world.runner.run_due_once(world.automation.id)
        if other is not None:
            assert not other.terminal
            gate.set()
            await world.queue.wait([other])
            assert other.status == "completed"
        return result

    assert asyncio.run(scenario()) is False
    assert world.current.status == "paused"
    assert expected in (world.current.last_error or "")
    assert world.current.consecutive_failures == 0
    assert world.current.next_day == YESTERDAY


def test_dr4_existing_artifacts_are_skipped(tmp_path):
    world = World(tmp_path, cameras=(FRONT, YARD))
    world.artifact(FRONT).write_bytes(MP4)
    assert world.run()
    assert [request.camera.id for request in world.protect.exports] == ["camera-2"]
    world = World(tmp_path / "b")
    world.artifact().write_bytes(MP4)
    world.problem = "would pause if checked"
    assert world.run()
    assert world.protect.exports == []


def test_dr5_camera_list_failure_is_an_attempt(tmp_path):
    world = World(tmp_path)
    world.protect.camera_error = TimelapseError("Could not load cameras from UniFi Protect: offline")
    world.block_sleep = True

    async def scenario():
        task = asyncio.create_task(world.runner.run_due_once(world.automation.id))
        while not world.sleeps:
            await asyncio.sleep(0)
        task.cancel()

    asyncio.run(scenario())
    current = world.current
    assert (current.status, current.consecutive_failures) == ("active", 1)
    assert current.last_error == "Could not load cameras from UniFi Protect: offline"
    assert current.next_retry_at == NOW + timedelta(seconds=60)


def test_dr6_backoff_then_pause(tmp_path):
    world = World(tmp_path)
    world.protect.export_error = TimelapseError("boom")
    assert world.run() is False
    assert world.sleeps == [60, 120, 240, 480]
    current = world.current
    assert (current.status, current.consecutive_failures, current.next_retry_at) == ("paused", 5, None)
    assert current.last_error == "1 Export Job(s) failed: Front Door: boom Paused after 5 attempts."


def test_dr6_retry_exports_only_missing_and_jitter(tmp_path):
    world = World(tmp_path, cameras=(FRONT, YARD))
    world.protect.failing = {"camera-2": TimelapseError("yard down")}
    seen: list[float] = []
    world.runner.jitter = lambda low, high: seen.append(high) or high
    assert world.run()
    assert [request.camera.id for request in world.protect.exports] == ["camera-1", "camera-2", "camera-2"]
    assert world.sleeps == [72.0]
    assert seen == [12.0]
    assert (world.current.consecutive_failures, world.current.last_error) == (0, None)


def test_dr7_attempts_survive_restart(tmp_path):
    world = World(tmp_path, consecutive_failures=4, next_retry_at=NOW + timedelta(seconds=90), last_error="earlier")
    world.protect.export_error = TimelapseError("still down")
    assert world.run() is False
    assert world.sleeps == [90]
    assert len(world.protect.exports) == 1
    assert world.current.status == "paused"


def test_dr8_stopped_during_run_still_records_the_day(tmp_path):
    world = World(tmp_path)
    world.protect.gate = asyncio.Event()

    async def scenario():
        task = asyncio.create_task(world.runner.run_due_once(world.automation.id))
        while not world.protect.exports:
            await asyncio.sleep(0)
        world.registry.put(world.current.stopped())
        world.protect.gate.set()  # type: ignore[union-attr]
        return await task

    assert asyncio.run(scenario()) is True
    assert (world.current.status, world.current.next_day) == ("stopped", date(2026, 7, 13))


def test_dr8_failure_while_stopped_records_no_retry(tmp_path):
    world = World(tmp_path)
    world.protect.gate = asyncio.Event()
    world.protect.export_error = TimelapseError("boom")

    async def scenario():
        task = asyncio.create_task(world.runner.run_due_once(world.automation.id))
        while not world.protect.exports:
            await asyncio.sleep(0)
        world.registry.put(world.current.stopped())
        world.protect.gate.set()  # type: ignore[union-attr]
        result = await task
        assert await world.runner.run_due_once(world.automation.id) is False
        return result

    assert asyncio.run(scenario()) is False
    current = world.current
    assert (current.status, current.consecutive_failures, current.next_retry_at) == ("stopped", 1, None)
    assert world.sleeps == []


def test_dr9_deleted_during_run_records_nothing(tmp_path):
    world = World(tmp_path)
    world.protect.gate = asyncio.Event()

    async def scenario():
        task = asyncio.create_task(world.runner.run_due_once(world.automation.id))
        while not world.protect.exports:
            await asyncio.sleep(0)
        world.registry.delete(world.automation.id)
        world.protect.gate.set()  # type: ignore[union-attr]
        return await task

    assert asyncio.run(scenario()) is False
    assert world.registry.automations == []
    assert world.artifact().exists()
    assert [job.status for job in world.queue.jobs.values()] == ["completed"]


def test_dr10_loop_catches_up_then_waits_for_midnight(tmp_path):
    world = World(tmp_path, next_day=date(2026, 7, 10), timezone="America/New_York")
    world.block_sleep = True

    async def scenario():
        world.runner.start(world.automation.id)
        world.runner.start(world.automation.id)
        while not world.sleeps:
            await asyncio.sleep(0)
        first = world.runner._loops[world.automation.id]
        world.registry.put(world.current.stopped().resumed())
        world.runner.start(world.automation.id)
        assert first is not world.runner._loops[world.automation.id]
        while len(world.sleeps) < 2:
            await asyncio.sleep(0)
        await world.runner.close()
        assert first.cancelled()

    asyncio.run(scenario())
    # The first wait reaches midnight, so the restarted loop catches up on the 13th at once.
    assert [request.start.day for request in world.protect.exports] == [10, 11, 12, 13]
    assert world.current.next_day == date(2026, 7, 14)
    assert world.sleeps[0] == 13 * 3600


def test_dr10_loop_ends_when_not_active(tmp_path):
    world = World(tmp_path)
    world.protect.camera_list = []

    async def scenario():
        world.runner.start(world.automation.id)
        await asyncio.wait_for(world.runner.wait(), 1)

    asyncio.run(scenario())
    assert world.current.status == "paused"


def test_dr11_reexport(tmp_path):
    world = World(tmp_path, cameras=(FRONT, YARD), next_day=date(2026, 7, 13))
    day = date(2026, 7, 1)
    world.artifact(FRONT, day).write_bytes(MP4)
    world.artifact(YARD, day).write_bytes(MP4)
    assert asyncio.run(world.runner.reexport(world.automation.id, day)) is False
    world.artifact(YARD, day).write_bytes(b"broken")
    assert asyncio.run(world.runner.reexport(world.automation.id, day)) is True
    assert [request.camera.id for request in world.protect.exports] == ["camera-2"]
    assert (world.out / (world.artifact(YARD, day).name + ".invalid-20260713T150000Z")).read_bytes() == b"broken"
    assert world.current.next_day == date(2026, 7, 13)
    world.registry.put(world.current.stopped())
    with pytest.raises(InvalidRequestError, match="resume the automation before re-exporting a day"):
        asyncio.run(world.runner.reexport(world.automation.id, day))

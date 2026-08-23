from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from timelapse import ProtectRateLimitError
from timelapse.jobs import ExportJobCoordinator, ExportJobSpec


def test_runtime_capacity_environment_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMELAPSE_MAX_ACTIVE_EXPORTS", "3")
    monkeypatch.setenv("TIMELAPSE_MAX_QUEUED_EXPORTS", "7")
    coordinator = ExportJobCoordinator.from_environment()

    assert (coordinator.max_active, coordinator.max_queued) == (3, 7)

    monkeypatch.setenv("TIMELAPSE_MAX_ACTIVE_EXPORTS", "0")
    with pytest.raises(ValueError, match="at least 1"):
        ExportJobCoordinator.from_environment()


def test_oversized_batch_is_lazily_admitted(tmp_path) -> None:
    async def exercise() -> None:
        release = asyncio.Event()
        started = 0
        maximum_started = 0

        async def operation() -> None:
            nonlocal maximum_started, started
            started += 1
            maximum_started = max(maximum_started, started)
            await release.wait()
            started -= 1

        coordinator = ExportJobCoordinator(max_active=2, max_queued=1)
        jobs = await coordinator.submit(
            ExportJobSpec.create(output=tmp_path / f"{index}.mp4", operation=operation) for index in range(8)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert coordinator.active_count == 2
        assert coordinator.queued_count <= 1
        assert coordinator.pending_count >= 5
        release.set()
        await coordinator.wait(jobs)
        assert maximum_started == 2
        assert all(job.status == "completed" for job in jobs)

    asyncio.run(exercise())


def test_rate_limit_hard_floor_delays_only_the_affected_job(tmp_path) -> None:
    async def exercise() -> None:
        calls = 0
        now = datetime.now(UTC)

        async def operation() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProtectRateLimitError("wait", retry_not_before=now + timedelta(milliseconds=20))

        coordinator = ExportJobCoordinator(max_active=1, max_queued=1, now=lambda: now)
        jobs = await coordinator.submit([ExportJobSpec.create(output=tmp_path / "one.mp4", operation=operation)])
        await coordinator.wait(jobs)
        assert calls == 2
        assert jobs[0].status == "completed"

    asyncio.run(exercise())


def test_cancelling_one_job_does_not_cancel_its_sibling(tmp_path) -> None:
    async def exercise() -> None:
        blocker = asyncio.Event()

        async def wait() -> None:
            await blocker.wait()

        async def finish() -> None:
            return None

        coordinator = ExportJobCoordinator(max_active=1, max_queued=1)
        first, second = await coordinator.submit(
            [
                ExportJobSpec.create(output=tmp_path / "first.mp4", operation=wait),
                ExportJobSpec.create(output=tmp_path / "second.mp4", operation=finish),
            ]
        )
        await asyncio.sleep(0)
        assert await coordinator.cancel(first.id)
        await coordinator.wait((first, second))
        assert first.status == "cancelled"
        assert second.status == "completed"

    asyncio.run(exercise())

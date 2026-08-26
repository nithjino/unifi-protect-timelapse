from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from timelapse import ProtectRateLimitError
from timelapse.jobs import ExportJobCoordinator, ExportJobSpec, OutputCollisionError


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

        async def operation(_output) -> None:
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

        async def operation(_output) -> None:
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

        async def wait(_output) -> None:
            await blocker.wait()

        async def finish(_output) -> None:
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


@pytest.mark.parametrize("policy", ["reject", "daily"])
def test_exact_path_policy_rejects_existing_files_and_claims(tmp_path, policy) -> None:
    async def exercise() -> None:
        path = tmp_path / "artifact.mp4"
        started = asyncio.Event()

        async def operation(_output) -> None:
            started.set()
            await asyncio.Event().wait()

        coordinator = ExportJobCoordinator()
        spec = ExportJobSpec.create(output=path, operation=operation, collision_policy=policy)
        path.write_bytes(b"existing")
        with pytest.raises(OutputCollisionError):
            await coordinator.submit([spec])
        path.unlink()
        jobs = await coordinator.submit([spec])
        with pytest.raises(OutputCollisionError):
            await coordinator.submit([ExportJobSpec.create(output=path, operation=operation, collision_policy=policy)])
        await started.wait()
        with pytest.raises(OutputCollisionError):
            await coordinator.submit([ExportJobSpec.create(output=path, operation=operation, collision_policy=policy)])
        await coordinator.cancel(jobs[0].id)
        await coordinator.wait(jobs)
        await coordinator.close()

    asyncio.run(exercise())


def test_suffix_chooses_desktop_format_and_supplies_final_paths(tmp_path) -> None:
    async def exercise() -> None:
        path = tmp_path / "artifact.mp4"
        path.write_bytes(b"existing")
        (tmp_path / "artifact_2.mp4").write_bytes(b"existing")
        outputs = []

        async def operation(output) -> None:
            outputs.append(output)

        coordinator = ExportJobCoordinator()
        jobs = await coordinator.submit(
            [ExportJobSpec.create(output=path, operation=operation, collision_policy="suffix") for _ in range(3)]
        )
        expected = [tmp_path / f"artifact_{index}.mp4" for index in (3, 4, 5)]
        assert [job.output for job in jobs] == expected
        await coordinator.wait(jobs)
        assert outputs == expected
        await coordinator.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_terminal_state_releases_claim(tmp_path, terminal) -> None:
    async def exercise() -> None:
        path = tmp_path / "artifact.mp4"
        started = asyncio.Event()

        async def operation(_output) -> None:
            started.set()
            if terminal == "failed":
                message = "export failed"
                raise OSError(message)
            if terminal == "cancelled":
                await asyncio.Event().wait()

        coordinator = ExportJobCoordinator()
        jobs = await coordinator.submit([ExportJobSpec.create(output=path, operation=operation)])
        await started.wait()
        if terminal == "cancelled":
            await coordinator.cancel(jobs[0].id)
        await coordinator.wait(jobs)
        assert jobs[0].status == terminal
        replacement = await coordinator.submit([ExportJobSpec.create(output=path, operation=operation)])
        assert replacement[0].output == path
        await coordinator.close()

    asyncio.run(exercise())


def test_claims_detect_casefolded_and_resolved_aliases(tmp_path) -> None:
    async def exercise() -> None:
        directory = tmp_path / "exports"
        directory.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(directory, target_is_directory=True)

        async def operation(_output) -> None:
            await asyncio.Event().wait()

        coordinator = ExportJobCoordinator()
        await coordinator.submit([ExportJobSpec.create(output=directory / "Front.mp4", operation=operation)])
        for path in (directory / "front.MP4", alias / "Front.mp4", directory / ".." / "exports" / "Front.mp4"):
            with pytest.raises(OutputCollisionError):
                await coordinator.submit([ExportJobSpec.create(output=path, operation=operation)])
        await coordinator.close()

    asyncio.run(exercise())


def test_failed_batch_claim_rolls_back_all_paths_before_launch(tmp_path) -> None:
    async def exercise() -> None:
        ran = []

        async def operation(output) -> None:
            ran.append(output)

        coordinator = ExportJobCoordinator()
        path = tmp_path / "artifact.mp4"
        with pytest.raises(OutputCollisionError):
            await coordinator.submit([ExportJobSpec.create(output=path, operation=operation) for _ in range(2)])
        await asyncio.sleep(0)
        assert not ran
        jobs = await coordinator.submit([ExportJobSpec.create(output=path, operation=operation)])
        await coordinator.wait(jobs)
        assert ran == [path]
        await coordinator.close()

    asyncio.run(exercise())


def test_claim_persistence_blocks_admission_and_failure_releases_paths(tmp_path) -> None:
    async def exercise() -> None:
        path = tmp_path / "artifact.mp4"
        persisting = asyncio.Event()
        release = asyncio.Event()
        ran = []

        async def operation(output) -> None:
            ran.append(output)

        async def fail_persistence(jobs) -> None:
            assert jobs[0].output == path
            persisting.set()
            await release.wait()
            message = "disk unavailable"
            raise OSError(message)

        coordinator = ExportJobCoordinator()
        # An already running dispatcher must not see the unpersisted submission.
        previous = await coordinator.submit([ExportJobSpec.create(output=tmp_path / "other.mp4", operation=operation)])
        await coordinator.wait(previous)
        ran.clear()
        spec = ExportJobSpec.create(output=path, operation=operation)
        submission = asyncio.create_task(coordinator.submit([spec], before_admission=fail_persistence))
        await persisting.wait()
        await asyncio.sleep(0)
        assert not ran
        release.set()
        with pytest.raises(OSError, match="disk unavailable"):
            await submission
        jobs = await coordinator.submit([spec])
        await coordinator.wait(jobs)
        assert ran == [path]
        await coordinator.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["pending", "queued", "running", "completed"])
def test_transition_failure_releases_claim_and_does_not_strand_waiters(tmp_path, stage) -> None:
    async def exercise() -> None:
        ran = []
        fail = True

        async def transition(job) -> None:
            if fail and job.status == stage:
                message = "transition persistence failed"
                raise OSError(message)

        async def operation(output) -> None:
            ran.append(output)

        path = tmp_path / "artifact.mp4"
        coordinator = ExportJobCoordinator(on_transition=transition)
        spec = ExportJobSpec.create(output=path, operation=operation)
        if stage == "pending":
            with pytest.raises(OSError, match="persistence failed"):
                await coordinator.submit([spec])
        else:
            jobs = await coordinator.submit([spec])
            if stage == "completed":
                with pytest.raises(OSError, match="persistence failed"):
                    await coordinator.wait(jobs)
            else:
                await coordinator.wait(jobs)
                assert jobs[0].status == "failed"
        assert len(ran) == (1 if stage == "completed" else 0)
        fail = False
        jobs = await coordinator.submit([spec])
        await coordinator.wait(jobs)
        assert jobs[0].status == "completed"
        await coordinator.close()

    asyncio.run(exercise())


def test_cancelled_waiter_does_not_cancel_shared_completion(tmp_path) -> None:
    async def exercise() -> None:
        release = asyncio.Event()

        async def operation(_output) -> None:
            await release.wait()

        coordinator = ExportJobCoordinator()
        jobs = await coordinator.submit([ExportJobSpec.create(output=tmp_path / "artifact.mp4", operation=operation)])
        waiter = asyncio.create_task(coordinator.wait(jobs))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await coordinator.wait(jobs)
        assert jobs[0].status == "completed"
        await coordinator.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("index", [0, 1, 2])
def test_cancellation_releases_running_queued_and_pending_claims(tmp_path, index) -> None:
    async def exercise() -> None:
        started = asyncio.Event()

        async def operation(_output) -> None:
            started.set()
            await asyncio.Event().wait()

        coordinator = ExportJobCoordinator(max_active=1, max_queued=1)
        jobs = await coordinator.submit(
            [ExportJobSpec.create(output=tmp_path / f"{number}.mp4", operation=operation) for number in range(3)]
        )
        await started.wait()
        assert [job.status for job in jobs] == ["running", "queued", "pending"]
        target = jobs[index]
        assert await coordinator.cancel(target.id)
        assert not await coordinator.cancel(target.id)
        await coordinator.wait((target,))
        replacement = await coordinator.submit([ExportJobSpec.create(output=target.output, operation=operation)])
        assert replacement[0].output == target.output
        await coordinator.close()
        assert coordinator.active_count == 0
        assert coordinator.queued_count == 0
        assert coordinator.pending_count == 0

    asyncio.run(exercise())


def test_rate_limited_job_retains_path_claim(tmp_path) -> None:
    async def exercise() -> None:
        limited = asyncio.Event()

        async def transition(job) -> None:
            if job.error == "wait":
                limited.set()

        async def operation(_output) -> None:
            raise ProtectRateLimitError("wait", retry_not_before=datetime.now(UTC) + timedelta(hours=1))

        coordinator = ExportJobCoordinator(on_transition=transition)
        path = tmp_path / "artifact.mp4"
        jobs = await coordinator.submit([ExportJobSpec.create(output=path, operation=operation)])
        await limited.wait()
        with pytest.raises(OutputCollisionError):
            await coordinator.submit([ExportJobSpec.create(output=path, operation=operation)])
        await coordinator.cancel(jobs[0].id)
        replacement = await coordinator.submit([ExportJobSpec.create(output=path, operation=operation)])
        await coordinator.close()
        assert replacement[0].status == "cancelled"

    asyncio.run(exercise())

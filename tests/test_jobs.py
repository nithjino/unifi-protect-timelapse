import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import CAMERA, MP4, Clock
from timelapse import CapacityError, ConflictError, NotFoundError, RateLimitError, TimelapseError
from timelapse.download import Progress
from timelapse.jobs import STOPPED, JobQueue
from timelapse.model import ExportRequest

FIXTURES = Path(__file__).parent / "fixtures"
AT = datetime(2026, 7, 20, 8, tzinfo=UTC)


def request(hour: int = 9) -> ExportRequest:
    return ExportRequest(CAMERA, AT, AT.replace(hour=hour), "120x")


def writer(gate: asyncio.Event | None = None, error: Exception | None = None, log: list | None = None):
    async def run(output: Path, progress) -> None:
        if log is not None:
            log.append(output.name)
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise error
        output.write_bytes(MP4)

    return run


def test_jq1_conflicts(tmp_path):
    async def scenario():
        queue = JobQueue(max_active=1)
        (tmp_path / "exists.mp4").write_bytes(MP4)
        gate = asyncio.Event()
        with pytest.raises(ConflictError, match="already exists"):
            queue.submit([(request(), tmp_path / "exists.mp4", writer())])
        queue.submit([(request(), tmp_path / "busy.mp4", writer(gate))])
        (tmp_path / "Link").symlink_to(tmp_path)
        for outputs in (
            [tmp_path / "BUSY.MP4"],
            [tmp_path / "Link" / "busy.mp4"],
            [tmp_path / "x.mp4", tmp_path / "X.mp4"],
            [tmp_path / "fresh.mp4", tmp_path / "busy.mp4"],
        ):
            with pytest.raises(ConflictError, match="already exists"):
                queue.submit([(request(), output, writer()) for output in outputs])
        assert len(queue.jobs) == 1
        gate.set()
        await queue.close()

    asyncio.run(scenario())


def test_jq2_bounded_concurrency(tmp_path):
    async def scenario():
        queue = JobQueue(max_active=2)
        running = peak = 0

        async def run(output: Path, progress) -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            output.write_bytes(MP4)
            running -= 1

        jobs = queue.submit([(request(), tmp_path / f"{n}.mp4", run) for n in range(8)])
        await asyncio.sleep(0)
        assert [job.status for job in jobs].count("running") <= 2
        await queue.wait(jobs)
        return peak, [job.status for job in jobs]

    peak, statuses = asyncio.run(scenario())
    assert peak == 2
    assert statuses == ["completed"] * 8


def test_jq3_status_flow(tmp_path):
    clock = Clock()

    async def scenario():
        queue = JobQueue(max_active=2, now=clock)
        gate = asyncio.Event()
        good, bad, quiet = queue.submit([
            (request(), tmp_path / "a.mp4", writer(gate)),
            (request(), tmp_path / "b.mp4", writer(error=TimelapseError("console said no"))),
            (request(), tmp_path / "c.mp4", writer(error=RuntimeError())),
        ])  # fmt: skip
        assert good.status == "queued"
        await asyncio.sleep(0)
        assert good.status == "running"
        assert good.started_at == clock.value
        good.bytes_per_second = 99.0
        gate.set()
        await queue.wait([good, bad, quiet])
        return good, bad, quiet

    good, bad, quiet = asyncio.run(scenario())
    assert (good.status, good.error, good.bytes_per_second, good.finished_at) == ("completed", None, 0.0, clock.value)
    assert (bad.status, bad.error) == ("failed", "console said no")
    assert (quiet.status, quiet.error) == ("failed", "RuntimeError")


def test_jq4_rate_limited_job_waits_without_blocking_others(tmp_path):
    clock = Clock()
    sleeps: list[float] = []
    release = asyncio.Event()

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await release.wait()

    async def scenario():
        queue = JobQueue(max_active=1, now=clock, sleep=sleep)
        attempts = []

        async def limited(output: Path, progress) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise RateLimitError(30, now=clock())
            output.write_bytes(MP4)

        limited_job = queue.submit([(request(), tmp_path / "limited.mp4", limited)])[0]
        other = queue.submit([(request(), tmp_path / "other.mp4", writer())])[0]
        await queue.wait([other])
        assert limited_job.status == "queued"
        assert "bounded retries" in (limited_job.error or "")
        with pytest.raises(ConflictError):
            queue.submit([(request(), tmp_path / "limited.mp4", writer())])
        release.set()
        await queue.wait([limited_job])
        assert limited_job.status == "completed"

        release.clear()
        waiting = queue.submit([(request(), tmp_path / "w.mp4", writer(error=RateLimitError()))])[0]
        while not sleeps[1:]:
            await asyncio.sleep(0)
        assert await queue.cancel(waiting.id)
        return waiting

    waiting = asyncio.run(scenario())
    assert sleeps == [30.0, 60.0]
    assert waiting.status == "cancelled"


def test_jq5_cancel(tmp_path):
    async def scenario():
        queue = JobQueue(max_active=1)
        gate = asyncio.Event()
        log: list[str] = []
        running, queued = queue.submit([
            (request(), tmp_path / "a.mp4", writer(gate, log=log)),
            (request(), tmp_path / "b.mp4", writer(log=log)),
        ])  # fmt: skip
        await asyncio.sleep(0)
        assert await queue.cancel(queued.id)
        assert queued.status == "cancelled"
        assert running.status == "running"
        assert await queue.cancel(running.id)
        assert (running.status, running.error) == ("cancelled", "Export was cancelled.")
        assert not await queue.cancel(running.id)
        assert not await queue.cancel("unknown")
        again = queue.submit([(request(), tmp_path / "a.mp4", writer())])
        await queue.wait(again)
        return log, again[0].status

    log, status = asyncio.run(scenario())
    assert log == ["a.mp4"]
    assert status == "completed"
    assert not list(tmp_path.glob("*.part"))


def test_jq6_cancelled_waiter_leaves_jobs_running(tmp_path):
    async def scenario():
        queue = JobQueue(max_active=1)
        gate = asyncio.Event()
        jobs = queue.submit([(request(), tmp_path / "a.mp4", writer(gate))])
        waiter = asyncio.create_task(queue.wait(jobs))
        await asyncio.sleep(0)
        waiter.cancel()
        gate.set()
        await queue.wait(jobs)
        return jobs[0].status

    assert asyncio.run(scenario()) == "completed"


def test_jq7_history_saved_before_start(tmp_path):
    history = tmp_path / "web-jobs.json"
    seen: list[str] = []

    async def scenario():
        queue = JobQueue(max_active=1, history=history)

        async def run(output: Path, progress) -> None:
            seen.extend(entry["output_name"] for entry in json.loads(history.read_text())["jobs"])
            output.write_bytes(MP4)

        await queue.wait(queue.submit([(request(), tmp_path / "a.mp4", run)]))
        broken = JobQueue(max_active=1, history=tmp_path / "missing-dir" / "file" / "x.json")
        (tmp_path / "missing-dir").write_text("not a directory")
        log: list[str] = []
        with pytest.raises(TimelapseError, match="Could not save"):
            broken.submit([(request(), tmp_path / "b.mp4", writer(log=log))])
        await asyncio.sleep(0)
        return broken.jobs, log

    jobs, log = asyncio.run(scenario())
    assert seen == ["a.mp4"]
    assert jobs == {}
    assert log == []


def test_jq8_saves_status_changes_and_ignores_save_failures(tmp_path, caplog):
    history = tmp_path / "web-jobs.json"

    async def scenario():
        queue = JobQueue(max_active=1, history=history)
        gate = asyncio.Event()
        job = queue.submit([(request(), tmp_path / "a.mp4", writer(gate))])[0]
        await asyncio.sleep(0)
        assert json.loads(history.read_text())["jobs"][0]["status"] == "running"
        history.unlink()
        history.mkdir()
        gate.set()
        await queue.wait([job])
        return job.status

    assert asyncio.run(scenario()) == "completed"
    assert "Could not save the export history" in caplog.text


def test_jq9_one_row_per_output_and_history_limit(tmp_path):
    async def scenario():
        queue = JobQueue(max_active=4, limit=3)
        failed = queue.submit([(request(), tmp_path / "a.mp4", writer(error=RuntimeError("x")))])[0]
        await queue.wait([failed])
        retried = queue.submit([(request(), tmp_path / "a.mp4", writer())])[0]
        assert list(queue.jobs) == [retried.id]
        for name in "bcd":
            await queue.wait(queue.submit([(request(), tmp_path / f"{name}.mp4", writer())]))
        return [job.output.name for job in queue.jobs.values()]

    assert asyncio.run(scenario()) == ["b.mp4", "c.mp4", "d.mp4"]


def test_jq10_load_3_0_0_history(tmp_path):
    history = tmp_path / "web-jobs.json"
    shutil.copy(FIXTURES / "web-jobs-3.0.0.json", history)
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "timelapse_Front_Door_2026_07_19_2026_07_20_600x_6bf6f341d9a3.mp4").write_bytes(MP4)
    clock = Clock()
    queue = JobQueue(max_active=1, history=history, now=clock)
    queue.load(exports)
    jobs = list(queue.jobs.values())
    assert [job.id for job in jobs] == [
        "Zk3v0Q1xYb7P", "batch:auto_81d31296ae0844fa8156c962c2111efd:2026-07-19:camera-1", "Running00001"
    ]  # fmt: skip
    assert (jobs[0].status, jobs[0].error) == ("failed", "The exported video is no longer available on the server.")
    assert (jobs[1].status, jobs[1].daily, jobs[1].request.full_day) == ("completed", True, True)
    assert (jobs[2].status, jobs[2].error, jobs[2].finished_at) == ("cancelled", STOPPED, clock.value)
    saved = json.loads(history.read_text())["jobs"]
    assert [entry["status"] for entry in saved] == ["failed", "completed", "cancelled"]
    assert set(saved[0]) == set(json.loads((FIXTURES / "web-jobs-3.0.0.json").read_text())["jobs"][0])
    for content in ("not json", '{"jobs": 3}', "[]"):
        history.write_text(content)
        empty = JobQueue(max_active=1, history=history)
        empty.load(exports)
        assert empty.jobs == {}


def test_jq11_remove(tmp_path):
    history = tmp_path / "web-jobs.json"

    async def scenario():
        queue = JobQueue(max_active=1, history=history)
        job = queue.submit([(request(), tmp_path / "a.mp4", writer())])[0]
        await queue.wait([job])
        history.unlink()
        history.mkdir()
        with pytest.raises(TimelapseError):
            queue.remove(job.id)
        assert job.id in queue.jobs
        assert job.output.exists()
        history.rmdir()
        queue.remove(job.id)
        assert job.id not in queue.jobs
        assert not job.output.exists()
        with pytest.raises(NotFoundError, match="That export is no longer in the job list"):
            queue.remove(job.id)

    asyncio.run(scenario())


def test_jq12_close_stops_everything(tmp_path):
    async def scenario():
        queue = JobQueue(max_active=1)
        gate = asyncio.Event()
        jobs = queue.submit([(request(), tmp_path / f"{n}.mp4", writer(gate)) for n in range(2)])
        await asyncio.sleep(0)
        await queue.close()
        with pytest.raises(CapacityError):
            queue.submit([(request(), tmp_path / "late.mp4", writer())])
        return jobs

    jobs = asyncio.run(scenario())
    assert [(job.status, job.error) for job in jobs] == [("cancelled", STOPPED)] * 2
    assert all(job.finished_at is not None for job in jobs)


def test_progress_is_tracked_but_not_saved(tmp_path):
    history = tmp_path / "web-jobs.json"

    async def scenario():
        queue = JobQueue(max_active=1, history=history)
        gate = asyncio.Event()

        async def run(output: Path, progress) -> None:
            progress(Progress(50, 200, 25.0, 2.0))
            await gate.wait()
            output.write_bytes(MP4)

        job = queue.submit([(request(), tmp_path / "a.mp4", run)])[0]
        await asyncio.sleep(0)
        snapshot = (job.downloaded_bytes, job.total_bytes, job.progress_percent, job.file_size)
        on_disk = json.loads(history.read_text())["jobs"][0]["downloaded_bytes"]
        gate.set()
        await queue.wait([job])
        return snapshot, on_disk, job.file_size

    snapshot, on_disk, size = asyncio.run(scenario())
    assert snapshot == (50, 200, 25.0, 200)
    assert on_disk == 0
    assert size == len(MP4)


def test_created_at_uses_the_clock(tmp_path):
    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))

    async def scenario():
        queue = JobQueue(max_active=1, now=clock)
        job = queue.submit([(request(), tmp_path / "a.mp4", writer())])[0]
        clock.advance(60)
        await queue.wait([job])
        return job

    job = asyncio.run(scenario())
    assert job.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert job.finished_at == datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=60)

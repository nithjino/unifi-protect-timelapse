from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from timelapse.automation_registry import (
    AutomationCamera,
    AutomationRegistry,
    ConnectionReference,
    ExportBatchRecord,
    ExportJobRecord,
)
from timelapse.config import Config, ConnectionSettings
from timelapse.jobs import ExportJobCoordinator, ExportJobSpec
from timelapse.protect import CameraInfo
from timelapse.schedule import DailyAutomationEngine, ResolvedAutomation, config_for_local_day, daily_output_path


def _valid_mp4() -> bytes:
    return b"\0\0\0\x18ftypisom"


def _config(now: datetime) -> Config:
    return Config(
        instance_url="https://protect.local",
        token="token",
        username="user",
        password="password",
        verify_ssl=True,
        speed="600x",
        start=now,
        end=now + timedelta(seconds=1),
        output=None,
        request_timeout_seconds=0,
        max_download_mib=1024,
    )


def _connection() -> ConnectionSettings:
    return ConnectionSettings(
        instance_url="https://protect.local",
        token="token",
        username="user",
        password="password",
        verify_ssl=False,
        request_timeout_seconds=37,
        max_download_mib=321,
        connection_kind="python-profile",
        connection_value="home",
    )


def test_engine_retries_only_missing_camera_and_advances_after_all_artifacts_validate(tmp_path) -> None:
    async def exercise() -> None:
        output = tmp_path / "exports"
        output.mkdir()
        now = datetime(2026, 8, 23, 12, tzinfo=UTC)
        cameras = (
            CameraInfo("camera-1", "Front", None, None),
            CameraInfo("camera-2", "Back", None, None),
        )
        registry = AutomationRegistry(tmp_path / "automations.json")
        registry.load()
        automation = registry.add(
            name="Home",
            cameras=tuple(AutomationCamera(item.id, item.name) for item in cameras),
            connection=ConnectionReference("python-profile", "home"),
            speed="600x",
            output_directory=output,
            timezone="UTC",
            now=now,
            next_day=date(2026, 8, 22),
        )
        attempts: dict[str, int] = {camera.id: 0 for camera in cameras}
        sleeps: list[float] = []

        async def resolve(_automation):
            return ResolvedAutomation(_connection(), cameras)

        async def export(_config, camera, path) -> None:
            attempts[camera.id] += 1
            if camera.id == "camera-2" and attempts[camera.id] == 1:
                raise OSError("temporary failure")
            path.write_bytes(_valid_mp4())

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        coordinator = ExportJobCoordinator()
        engine = DailyAutomationEngine(
            registry,
            coordinator,
            resolve_connection=resolve,
            exporter=export,
            now=lambda: now,
            sleep=sleep,
            jitter=lambda _start, _end: 0,
        )
        result = await engine.run_due_once(automation.id)

        assert result is not None and result.completed
        assert attempts == {"camera-1": 1, "camera-2": 2}
        assert sleeps == [60]
        assert registry.resolve(automation.id).next_day == date(2026, 8, 23)

    asyncio.run(exercise())


def test_missing_camera_pauses_without_consuming_an_attempt(tmp_path) -> None:
    async def exercise() -> None:
        output = tmp_path / "exports"
        output.mkdir()
        now = datetime(2026, 8, 23, 12, tzinfo=UTC)
        registry = AutomationRegistry(tmp_path / "automations.json")
        registry.load()
        automation = registry.add(
            name="Home",
            cameras=(AutomationCamera("missing", "Missing Camera"),),
            connection=ConnectionReference("python-profile", "home"),
            speed="600x",
            output_directory=output,
            timezone="UTC",
            now=now,
        )

        async def resolve(_automation):
            return ResolvedAutomation(_connection(), ())

        async def export(_config, _camera, _path) -> None:
            raise AssertionError("missing cameras must pause before export")

        coordinator = ExportJobCoordinator()
        engine = DailyAutomationEngine(
            registry,
            coordinator,
            resolve_connection=resolve,
            exporter=export,
            now=lambda: now,
        )
        assert await engine.run_due_once(automation.id) is None
        paused = registry.resolve(automation.id)
        assert paused.status == "paused"
        assert paused.consecutive_failures == 0

    asyncio.run(exercise())


def test_stop_before_batch_submission_starts_no_work(tmp_path) -> None:
    async def exercise() -> None:
        output = tmp_path / "exports"
        output.mkdir()
        now = datetime(2026, 8, 23, 12, tzinfo=UTC)
        camera = CameraInfo("camera-1", "Front", None, None)
        registry = AutomationRegistry(tmp_path / "automations.json")
        registry.load()
        automation = registry.add(
            name="Home",
            cameras=(AutomationCamera(camera.id, camera.name),),
            connection=ConnectionReference("python-profile", "home"),
            speed="600x",
            output_directory=output,
            timezone="UTC",
            now=now,
        )
        registry.stop(automation.id)
        exports = 0

        async def resolve(_automation):
            return ResolvedAutomation(_connection(), (camera,))

        async def export(_config, _camera, _path) -> None:
            nonlocal exports
            exports += 1

        engine = DailyAutomationEngine(
            registry,
            ExportJobCoordinator(),
            resolve_connection=resolve,
            exporter=export,
            now=lambda: now,
        )

        assert await engine.run_due_once(automation.id) is None
        assert exports == 0

    asyncio.run(exercise())


def test_stopped_automation_recovers_a_persisted_batch_then_stays_stopped(tmp_path) -> None:
    async def exercise() -> None:
        output = tmp_path / "exports"
        output.mkdir()
        now = datetime(2026, 8, 23, 12, tzinfo=UTC)
        day = date(2026, 8, 22)
        camera = CameraInfo("camera-1", "Front", None, None)
        selected = AutomationCamera(camera.id, camera.name)
        registry = AutomationRegistry(tmp_path / "automations.json")
        registry.load()
        automation = registry.add(
            name="Home",
            cameras=(selected,),
            connection=ConnectionReference("python-profile", "home"),
            speed="600x",
            output_directory=output,
            timezone="UTC",
            now=now,
            next_day=day,
        )
        batch_id = f"batch:{automation.id}:{day.isoformat()}:0"
        config = _config(now)
        expected = daily_output_path(config_for_local_day(config, day, "UTC"), camera, output)
        registry.put_batch(
            ExportBatchRecord(batch_id, automation.id, day, 0, 1, now),
            (
                ExportJobRecord(
                    f"{batch_id}:{camera.id}",
                    batch_id,
                    automation.id,
                    selected,
                    str(expected),
                    "pending",
                    1,
                    now,
                    now,
                ),
            ),
        )
        registry.stop(automation.id)

        async def resolve(_automation):
            return ResolvedAutomation(_connection(), (camera,))

        async def export(_config, _camera, path) -> None:
            path.write_bytes(_valid_mp4())

        engine = DailyAutomationEngine(
            registry,
            ExportJobCoordinator(),
            resolve_connection=resolve,
            exporter=export,
            now=lambda: now,
        )
        result = await engine.run_due_once(automation.id)

        assert result is not None and result.completed
        restored = registry.resolve(automation.id)
        assert restored.status == "stopped"
        assert restored.next_day == date(2026, 8, 23)

    asyncio.run(exercise())


def test_stored_policy_controls_speed_dst_and_reexport(tmp_path) -> None:
    async def exercise() -> None:
        now = datetime(2026, 3, 9, 12, tzinfo=UTC)
        camera = CameraInfo("camera-1", "Front", None, None)
        registry = AutomationRegistry(tmp_path / "automations.json")
        registry.load()
        automation = registry.add(
            name="DST",
            cameras=(AutomationCamera(camera.id, camera.name),),
            connection=ConnectionReference("python-profile", "home"),
            speed="120x",
            output_directory=tmp_path,
            timezone="America/New_York",
            now=now,
            next_day=date(2026, 3, 8),
        )
        exports = []

        async def resolve(_automation):
            return ResolvedAutomation(_connection(), (camera,))

        async def export(config, _camera, path) -> None:
            exports.append((config, path))
            assert config.speed == "120x"
            assert "120x" in path.name
            assert config.start.isoformat() == "2026-03-08T00:00:00-05:00"
            assert config.end.isoformat() == "2026-03-09T00:00:00-04:00"
            assert config.end.astimezone(UTC) - config.start.astimezone(UTC) == timedelta(hours=23)
            assert config.daily and config.full_day and config.output is None
            for field in (
                "instance_url",
                "token",
                "username",
                "password",
                "verify_ssl",
                "request_timeout_seconds",
                "max_download_mib",
                "connection_kind",
                "connection_value",
            ):
                assert getattr(config, field) == getattr(_connection(), field)
            path.write_bytes(_valid_mp4())

        engine = DailyAutomationEngine(
            registry,
            ExportJobCoordinator(),
            resolve_connection=resolve,
            exporter=export,
            now=lambda: now,
        )
        result = await engine.run_due_once(automation.id)
        assert result is not None and result.completed
        exports[0][1].unlink()
        result = await engine.reexport(automation.id, date(2026, 3, 8))
        assert result is not None and result.completed
        assert len(exports) == 2
        assert registry.resolve(automation.id).next_day == date(2026, 3, 9)

    asyncio.run(exercise())


def test_manual_claim_safety_pauses_daily_batch_without_running_it(tmp_path) -> None:
    async def exercise() -> None:
        now = datetime(2026, 8, 23, 12, tzinfo=UTC)
        camera = CameraInfo("camera-1", "Front", None, None)
        registry = AutomationRegistry(tmp_path / "automations.json")
        registry.load()
        automation = registry.add(
            name="Home",
            cameras=(AutomationCamera(camera.id, camera.name),),
            connection=ConnectionReference("python-profile", "home"),
            speed="600x",
            output_directory=tmp_path,
            timezone="UTC",
            now=now,
        )
        path = daily_output_path(config_for_local_day(_config(now), automation.next_day, "UTC"), camera, tmp_path)
        coordinator = ExportJobCoordinator()

        async def manual(_output) -> None:
            await asyncio.Event().wait()

        async def resolve(_automation):
            return ResolvedAutomation(_connection(), (camera,))

        async def export(_config, _camera, _output) -> None:
            raise AssertionError("a collided Daily Automation must not launch")

        manual_jobs = await coordinator.submit([ExportJobSpec.create(output=path, operation=manual)])
        engine = DailyAutomationEngine(
            registry, coordinator, resolve_connection=resolve, exporter=export, now=lambda: now
        )
        assert await engine.run_due_once(automation.id) is None
        paused = registry.resolve(automation.id)
        assert paused.status == "paused"
        assert paused.next_day == automation.next_day
        records = registry.jobs_for_automation(automation.id)
        assert len(records) == 1
        assert records[0].output == str(path)
        assert records[0].status == "failed"
        assert len(coordinator.jobs) == 1
        assert not manual_jobs[0].terminal
        await coordinator.close()

    asyncio.run(exercise())

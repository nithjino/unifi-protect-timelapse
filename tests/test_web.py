from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from timelapse import OperationTimeoutError, TimelapseError
from timelapse.download import DownloadProgress
from timelapse.protect import CameraInfo
from timelapse.service import CameraThumbnail
from timelapse.web import create_app, main
from timelapse.web_state import DailySchedule, WebCapacityError, WebSettings, WebState

if TYPE_CHECKING:
    from pathlib import Path

    import uvicorn
    from fastapi import FastAPI

    from timelapse.config import Config


def _settings(tmp_path: Path, *, web_password: str | None = None, configured: bool = True) -> WebSettings:
    return WebSettings(
        instance_url="https://protect.local/proxy/protect/integration/v1" if configured else "",
        token="integration-token" if configured else "",
        username="local-user" if configured else "",
        password="protect-password" if configured else "",
        verify_ssl=True,
        request_timeout_seconds=0,
        max_download_mib=10240,
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "data" / "exports",
        web_username="viewer",
        web_password=web_password,
    )


async def _cameras(_config: Config) -> list[CameraInfo]:
    return [
        CameraInfo(id="camera-1", name="Front Door", state="CONNECTED", model="G5 Pro"),
        CameraInfo(id="camera-2", name="Back Yard", state="CONNECTED", model="G4 Bullet"),
    ]


async def _thumbnail(_config: Config, _camera: CameraInfo, _timestamp: datetime) -> CameraThumbnail:
    return CameraThumbnail(b"jpeg-data", "exact")


async def _export(
    _config: Config,
    _camera: CameraInfo,
    output: Path,
    progress_callback: object,
) -> None:
    assert callable(progress_callback)
    progress_callback(DownloadProgress(5, 10, 5.0, 1.0))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"video")  # noqa: ASYNC240 - synchronous test double


def _app(tmp_path: Path, *, web_password: str | None = None, configured: bool = True) -> tuple[FastAPI, WebState]:
    settings = _settings(tmp_path, web_password=web_password, configured=configured)
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    return create_app(settings, state=state), state


def _client(app: FastAPI, *, host: str = "127.0.0.1") -> TestClient:
    return TestClient(app, base_url="http://localhost", client=(host, 50000))


def _wait_for_completed_jobs(client: TestClient, state: WebState) -> str:
    for _attempt in range(10):
        response = client.get("/partials/jobs")
        if state.jobs and all(job.status == "completed" for job in state.jobs.values()):
            return response.text
    pytest.fail("exports did not complete after polling the jobs endpoint")


def test_dashboard_and_local_assets_render(tmp_path: Path) -> None:
    app, _state = _app(tmp_path)

    with _client(app) as client:
        response = client.get("/")
        javascript = client.get("/static/htmx.min.js")

    assert response.status_code == 200
    assert response.headers["x-request-id"]
    assert "htmx.min.js" in response.text
    assert "cdn.jsdelivr.net" not in response.text
    assert 'id="server-info-button"' in response.text
    assert 'id="server-info-dialog"' in response.text
    assert ">Full Day<" in response.text
    assert ">Exact Range<" in response.text
    assert 'id="full-day-start-date"' in response.text
    assert 'id="full-day-end-date"' in response.text
    assert 'id="full-day-end-date" type="date"' in response.text
    assert response.text.count('type="time" value="00:00" disabled') == 2
    assert "Times are interpreted in the server\u2019s UTC timezone." in response.text
    assert javascript.status_code == 200
    assert "htmx" in javascript.text


def test_dashboard_reports_configured_server_timezone(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), timezone=ZoneInfo("America/New_York"))
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    app = create_app(settings, state=state)

    with _client(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Times are interpreted in the server\u2019s America/New_York timezone." in response.text


def test_web_settings_load_timezone_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")

    settings = WebSettings.from_environment()

    assert settings.timezone == ZoneInfo("America/New_York")
    assert settings.timezone_name == "America/New_York"


def test_web_settings_default_timezone_is_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)

    settings = WebSettings.from_environment()

    assert settings.timezone == ZoneInfo("UTC")
    assert settings.timezone_name == "UTC"


def test_web_settings_reject_invalid_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "not/a-timezone")

    with pytest.raises(ValueError, match="valid IANA timezone"):
        WebSettings.from_environment()


def test_login_session_protects_ui_but_not_health(tmp_path: Path) -> None:
    app, _state = _app(tmp_path, web_password="web-secret")  # noqa: S106 - test credential

    with _client(app) as client:
        health = client.get("/healthz")
        denied = client.get("/", follow_redirects=False)
        login_page = client.get(denied.headers["location"])
        rejected = client.post(
            "/login",
            data={"username": "viewer", "password": "wrong", "next": "/"},
            follow_redirects=False,
        )
        signed_in = client.post(
            "/login",
            headers={"Origin": "http://localhost:8000"},
            data={"username": "viewer", "password": "web-secret", "next": "/"},
            follow_redirects=False,
        )
        allowed = client.get("/")
        signed_out = client.post("/logout", follow_redirects=False)
        denied_again = client.get("/", follow_redirects=False)

    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
    assert denied.status_code == 303
    assert denied.headers["location"] == "/login?next=%2F"
    assert login_page.status_code == 200
    assert "Enter dashboard" in login_page.text
    assert "Your recordings stay" in login_page.text
    assert rejected.status_code == 401
    assert "username or password is incorrect" in rejected.text
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/"
    assert "HttpOnly" in signed_in.headers["set-cookie"]
    assert "SameSite=strict" in signed_in.headers["set-cookie"]
    assert "web-secret" not in signed_in.headers["set-cookie"]
    assert allowed.status_code == 200
    assert "Log out" in allowed.text
    assert allowed.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in allowed.headers["content-security-policy"]
    assert signed_out.status_code == 303
    assert denied_again.status_code == 303


def test_reauthentication_from_partial_returns_to_dashboard(tmp_path: Path) -> None:
    app, _state = _app(tmp_path, web_password="web-secret")  # noqa: S106 - test credential

    with _client(app) as client:
        prompt = client.get(
            "/partials/jobs",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        login_page = client.get(prompt.headers["hx-redirect"])
        signed_in = client.post(
            "/login",
            data={"username": "viewer", "password": "web-secret", "next": "/"},
            follow_redirects=False,
        )

    assert prompt.status_code == 401
    assert prompt.headers["hx-redirect"] == "/login?next=%2F"
    assert 'name="next" value="/"' in login_page.text
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/"


def test_login_rejects_external_return_path_and_throttles_failures(tmp_path: Path) -> None:
    app, _state = _app(tmp_path, web_password="web-secret")  # noqa: S106 - test credential

    with _client(app) as client:
        for _attempt in range(5):
            response = client.post(
                "/login",
                data={"username": "viewer", "password": "wrong", "next": "https://malicious.example"},
                follow_redirects=False,
            )
            assert response.status_code == 401
        throttled = client.post(
            "/login",
            data={"username": "viewer", "password": "web-secret", "next": "https://malicious.example"},
            follow_redirects=False,
        )

    assert throttled.status_code == 429
    assert throttled.headers["retry-after"]
    assert 'name="next" value="/"' in throttled.text


def test_authenticated_mutation_rejects_untrusted_forwarded_public_origin(tmp_path: Path) -> None:
    app, _state = _app(tmp_path, web_password="web-secret")  # noqa: S106 - test credential

    with _client(app) as client:
        client.post(
            "/login",
            data={"username": "viewer", "password": "web-secret", "next": "/"},
            follow_redirects=False,
        )
        response = client.post(
            "/logout",
            headers={
                "Origin": "https://timelapse.local",
                "X-Forwarded-Host": "timelapse.local",
                "X-Forwarded-Proto": "https",
            },
            follow_redirects=False,
        )

    assert response.status_code == 403


def test_cross_origin_mutation_is_rejected(tmp_path: Path) -> None:
    app, state = _app(tmp_path)

    with _client(app) as client:
        response = client.post(
            "/actions/export",
            headers={"Origin": "https://malicious.example"},
            data={"range_mode": "full-day", "day": "2026-07-20", "speed": "600x"},
        )

    assert response.status_code == 403
    assert not state.jobs


def test_camera_export_thumbnail_and_download_flow(tmp_path: Path) -> None:
    app, state = _app(tmp_path)
    start = datetime(2026, 7, 20, 8, tzinfo=UTC)
    end = start + timedelta(hours=2)

    with _client(app) as client:
        camera_response = client.get("/partials/cameras")
        created = client.post(
            "/actions/export",
            data={
                "camera_ids": ["camera-1", "camera-2"],
                "range_mode": "exact",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "speed": "120x",
            },
        )
        jobs_html = _wait_for_completed_jobs(client, state)
        job = next(iter(state.jobs.values()))
        preview = client.get(f"/api/thumbnails/{job.camera.id}", params={"timestamp": start.isoformat()})
        download = client.get(f"/exports/{job.id}")

    assert camera_response.status_code == 200
    assert "Front Door" in camera_response.text
    assert created.status_code == 200
    assert "Started 2 exports" in created.text
    assert created.headers["hx-trigger"] == "stateChanged"
    assert len(state.jobs) == 2
    assert "Ready" in jobs_html
    assert preview.content == b"jpeg-data"
    assert preview.headers["x-timelapse-thumbnail-source"] == "exact"
    assert download.status_code == 200
    assert download.content == b"video"
    assert job.full_day is False
    assert job.output.name.endswith("_120x__6bf6f341d9a3.mp4")


def test_full_day_web_export_uses_date_only_filename(tmp_path: Path) -> None:
    timezone = ZoneInfo("America/New_York")
    settings = replace(_settings(tmp_path), timezone=timezone)
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    app = create_app(settings, state=state)

    with _client(app) as client:
        created = client.post(
            "/actions/export",
            data={
                "camera_ids": ["camera-1"],
                "range_mode": "full-day",
                "day": "2026-07-20",
                "speed": "600x",
            },
        )
        _wait_for_completed_jobs(client, state)

    job = next(iter(state.jobs.values()))
    assert created.status_code == 200
    assert job.full_day is True
    assert job.start == datetime(2026, 7, 20, tzinfo=timezone)
    assert job.end == datetime(2026, 7, 21, tzinfo=timezone)
    assert job.start.astimezone(UTC) == datetime(2026, 7, 20, 4, tzinfo=UTC)
    assert job.end.astimezone(UTC) == datetime(2026, 7, 21, 4, tzinfo=UTC)
    assert job.output.name == "timelapse_Front_Door_2026_07_20_2026_07_21_600x_6bf6f341d9a3.mp4"


def test_exact_web_range_and_preview_use_configured_timezone(tmp_path: Path) -> None:
    timezone = ZoneInfo("America/New_York")
    settings = replace(_settings(tmp_path), timezone=timezone)
    preview_times: list[datetime] = []

    async def capture_thumbnail(
        _config: Config,
        _camera: CameraInfo,
        timestamp: datetime,
    ) -> CameraThumbnail:
        preview_times.append(timestamp)
        return CameraThumbnail(b"jpeg-data", "exact")

    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=capture_thumbnail, exporter=_export)
    app = create_app(settings, state=state)

    with _client(app) as client:
        created = client.post(
            "/actions/export",
            data={
                "camera_ids": ["camera-1"],
                "range_mode": "exact",
                "start": "2026-07-20T00:00",
                "end": "2026-07-20T01:00",
                "speed": "600x",
            },
        )
        preview = client.get("/api/thumbnails/camera-1", params={"timestamp": "2026-07-20T00:00:00"})

    job = next(iter(state.jobs.values()))
    assert created.status_code == 200
    assert preview.status_code == 200
    assert job.start == datetime(2026, 7, 20, tzinfo=timezone)
    assert job.end == datetime(2026, 7, 20, 1, tzinfo=timezone)
    assert preview_times == [datetime(2026, 7, 20, tzinfo=timezone)]


def test_configured_timezone_preserves_dst_calendar_day_boundaries(tmp_path: Path) -> None:
    timezone = ZoneInfo("America/New_York")
    settings = replace(_settings(tmp_path), timezone=timezone)

    start, end = settings.day_bounds(date(2026, 11, 1))

    assert start.utcoffset() == timedelta(hours=-4)
    assert end.utcoffset() == timedelta(hours=-5)
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=25)


def test_invalid_export_returns_actionable_message(tmp_path: Path) -> None:
    app, state = _app(tmp_path)

    with _client(app) as client:
        response = client.post(
            "/actions/export",
            data={"range_mode": "full-day", "day": "2026-07-20", "speed": "600x"},
        )

    assert response.status_code == 400
    assert "Select at least one available camera" in response.text
    assert not state.jobs


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (TimelapseError("Protect unavailable"), 502),
        (OperationTimeoutError("Protect timed out"), 504),
    ],
)
def test_camera_service_failures_return_non_success_status(
    tmp_path: Path,
    error: TimelapseError,
    status_code: int,
) -> None:
    async def failed_cameras(_config: Config) -> list[CameraInfo]:
        raise error

    settings = _settings(tmp_path)
    state = WebState(settings, camera_loader=failed_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    app = create_app(settings, state=state)

    with _client(app) as client:
        response = client.get("/partials/cameras")

    assert response.status_code == status_code
    assert str(error) in response.text


def test_unexpected_camera_failure_returns_correlated_server_error(tmp_path: Path) -> None:
    async def failed_cameras(_config: Config) -> list[CameraInfo]:
        message = "database failure"
        raise OSError(message)

    settings = _settings(tmp_path)
    state = WebState(settings, camera_loader=failed_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    app = create_app(settings, state=state)

    with _client(app) as client:
        response = client.get("/partials/cameras")

    assert response.status_code == 500
    assert response.headers["x-request-id"]
    assert response.headers["x-request-id"] in response.text
    assert "database failure" not in response.text


def test_missing_job_action_returns_not_found(tmp_path: Path) -> None:
    app, _state = _app(tmp_path)

    with _client(app) as client:
        response = client.delete("/actions/jobs/missing01")

    assert response.status_code == 404


def test_incomplete_connection_does_not_render_secrets(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path, configured=False),
        token="sensitive-integration-token",  # noqa: S106 - verifies that configured secrets are not rendered
    )
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    app = create_app(settings, state=state)

    with _client(app) as client:
        status = client.get("/partials/status")
        cameras = client.get("/partials/cameras")

    assert status.status_code == 200
    assert "Server healthy" in status.text
    assert "Configuration needed" in status.text
    assert "UNIFI_PROTECT_URL" in status.text
    assert "Server configuration is incomplete" in cameras.text
    assert "sensitive-integration-token" not in status.text + cameras.text


def test_daily_schedule_is_persisted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)

    async def exercise() -> None:
        await state.start()
        schedule = await state.create_schedule(["camera-1"], "600x")
        assert schedule.id in state.schedules
        await state.close()

    asyncio.run(exercise())

    payload = json.loads((settings.data_dir / "web-schedules.json").read_text(encoding="utf-8"))
    assert payload["schedules"][0]["cameras"][0]["id"] == "camera-1"
    assert payload["schedules"][0]["speed"] == "600x"


def test_export_list_is_restored_after_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first_state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)

    async def create_export() -> str:
        await first_state.start()
        start = datetime(2026, 7, 20, 8, tzinfo=UTC)
        jobs = await first_state.create_jobs(["camera-1"], start, start + timedelta(hours=2), "120x")
        job = jobs[0]
        assert job.task is not None
        await job.task
        assert job.status == "completed"
        await first_state.close()
        return job.id

    job_id = asyncio.run(create_export())
    second_state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)

    async def restore_export() -> None:
        await second_state.start()
        restored = second_state.jobs[job_id]
        assert restored.status == "completed"
        assert restored.camera.name == "Front Door"
        assert restored.output.read_bytes() == b"video"
        assert restored.task is None
        await second_state.close()

    asyncio.run(restore_export())

    payload = json.loads((settings.data_dir / "web-jobs.json").read_text(encoding="utf-8"))
    assert payload["jobs"][0]["id"] == job_id
    assert payload["jobs"][0]["status"] == "completed"


def test_interrupted_export_is_restored_as_cancelled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    started_at = datetime(2026, 7, 20, 8, tzinfo=UTC)
    payload = {
        "jobs": [
            {
                "id": "interrupted-job",
                "camera": {"id": "camera-1", "name": "Front Door", "state": "CONNECTED", "model": "G5 Pro"},
                "start": started_at.isoformat(),
                "end": (started_at + timedelta(hours=2)).isoformat(),
                "speed": "120x",
                "output_name": "interrupted.mp4",
                "daily": False,
                "status": "running",
                "downloaded_bytes": 1024,
                "total_bytes": 2048,
                "bytes_per_second": 512.0,
                "elapsed_seconds": 2.0,
                "created_at": started_at.isoformat(),
                "started_at": started_at.isoformat(),
                "finished_at": None,
                "error": None,
            }
        ]
    }
    (settings.data_dir / "web-jobs.json").write_text(json.dumps(payload), encoding="utf-8")
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)

    async def restore_export() -> None:
        await state.start()
        restored = state.jobs["interrupted-job"]
        assert restored.status == "cancelled"
        assert restored.finished_at is not None
        assert restored.error == "The server stopped before this export completed."
        await state.close()

    asyncio.run(restore_export())

    stored = json.loads((settings.data_dir / "web-jobs.json").read_text(encoding="utf-8"))
    assert stored["jobs"][0]["status"] == "cancelled"


def test_network_binding_requires_web_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMELAPSE_WEB_HOST", "0.0.0.0")  # noqa: S104 - verifies public-binding guard
    monkeypatch.delenv("TIMELAPSE_WEB_PASSWORD", raising=False)

    with pytest.raises(SystemExit, match="TIMELAPSE_WEB_PASSWORD is required"):
        main()


def test_web_server_bounds_graceful_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[uvicorn.Server] = []

    def run_server(server: uvicorn.Server) -> None:
        launched.append(server)

    monkeypatch.setenv("TIMELAPSE_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("TIMELAPSE_WEB_PORT", "8765")
    monkeypatch.delenv("TIMELAPSE_WEB_PASSWORD", raising=False)
    monkeypatch.setattr("timelapse.web._ShutdownAwareServer.run", run_server)

    main()

    assert len(launched) == 1
    config = launched[0].config
    assert config.app == "timelapse.web:app"
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.timeout_graceful_shutdown == 1


def test_passwordless_app_rejects_remote_clients_and_host_rebinding(tmp_path: Path) -> None:
    app, _state = _app(tmp_path)

    with _client(app, host="192.0.2.10") as remote_client:
        remote = remote_client.get("/")
    with _client(app) as local_client:
        rebound = local_client.get("/", headers={"Host": "attacker.example"})

    assert remote.status_code == 403
    assert rebound.status_code == 400


def test_web_export_queue_bounds_concurrency_duration_and_capacity(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        max_download_mib=1,
        web_max_active_exports=1,
        web_max_queued_exports=1,
        web_max_export_hours=2,
        web_storage_quota_mib=10,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_export(
        _config: Config,
        _camera: CameraInfo,
        output: Path,
        _progress_callback: object,
    ) -> None:
        started.set()
        await release.wait()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")  # noqa: ASYNC240 - synchronous test double

    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=blocking_export)

    async def exercise() -> None:
        await state.start()
        start = datetime(2026, 7, 20, 8, tzinfo=UTC)
        with pytest.raises(WebCapacityError, match="limited to 2 hours"):
            await state.create_jobs(["camera-1"], start, start + timedelta(hours=3), "120x")
        jobs = await state.create_jobs(["camera-1", "camera-2"], start, start + timedelta(hours=1), "120x")
        await started.wait()
        await asyncio.sleep(0)
        assert sorted(job.status for job in jobs) == ["queued", "running"]
        with pytest.raises(WebCapacityError, match="queue is full"):
            await state.create_jobs(["camera-1"], start + timedelta(days=1), start + timedelta(days=1, hours=1), "120x")
        release.set()
        await asyncio.gather(*(job.task for job in jobs if job.task is not None))
        await state.close()

    asyncio.run(exercise())


def test_web_export_storage_quota_is_reserved_before_launch(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), max_download_mib=1, web_storage_quota_mib=1)
    settings.output_dir.mkdir(parents=True)
    (settings.output_dir / "existing.mp4").write_bytes(b"x")
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)

    async def exercise() -> None:
        await state.start()
        start = datetime(2026, 7, 20, 8, tzinfo=UTC)
        with pytest.raises(WebCapacityError, match="storage quota"):
            await state.create_jobs(["camera-1"], start, start + timedelta(hours=1), "120x")
        assert not state.jobs
        await state.close()

    asyncio.run(exercise())


def test_web_capacity_failure_returns_http_429(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), max_download_mib=1, web_storage_quota_mib=1)
    settings.output_dir.mkdir(parents=True)
    (settings.output_dir / "existing.mp4").write_bytes(b"x")
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    app = create_app(settings, state=state)

    with _client(app) as client:
        response = client.post(
            "/actions/export",
            data={
                "camera_ids": ["camera-1"],
                "range_mode": "full-day",
                "day": "2026-07-20",
                "speed": "600x",
            },
        )

    assert response.status_code == 429
    assert "storage quota" in response.text


def test_job_persistence_failure_rolls_back_without_starting_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = WebState(_settings(tmp_path), camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    persist = AsyncMock(side_effect=OSError("disk full"))
    monkeypatch.setattr(state, "_persist_jobs", persist)

    async def exercise() -> None:
        await state.start()
        start = datetime(2026, 7, 20, 8, tzinfo=UTC)
        with pytest.raises(OSError, match="disk full"):
            await state.create_jobs(["camera-1"], start, start + timedelta(hours=1), "120x")

    asyncio.run(exercise())

    assert not state.jobs
    assert not state._reserved_output_paths
    assert not list(state.settings.output_dir.glob("*.mp4"))


def test_web_state_fails_fast_when_storage_is_not_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = WebState(_settings(tmp_path), camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)

    def reject_storage(_directory: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(state, "_prepare_storage_directory", reject_storage)

    with pytest.raises(RuntimeError, match="TIMELAPSE_UID and TIMELAPSE_GID"):
        asyncio.run(state.start())


def test_schedule_persistence_failure_rolls_back_without_starting_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = WebState(_settings(tmp_path), camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    persist = AsyncMock(side_effect=[OSError("disk full"), None])
    monkeypatch.setattr(state, "_persist_schedules", persist)

    async def exercise() -> None:
        await state.start()
        with pytest.raises(OSError, match="disk full"):
            await state.create_schedule(["camera-1"], "600x")

    asyncio.run(exercise())

    assert not state.schedules
    assert persist.await_count == 2


@pytest.mark.parametrize("payload", [[], {"version": 1, "schedules": ["invalid"]}])
def test_invalid_schedule_state_is_quarantined_without_blocking_startup(tmp_path: Path, payload: object) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    state_file = settings.data_dir / "web-schedules.json"
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)

    async def exercise() -> None:
        await state.start()
        assert not state.schedules
        await state.close()

    asyncio.run(exercise())

    assert not state_file.exists()
    assert len(list(settings.data_dir.glob("web-schedules.invalid-*.json"))) == 1


def test_legacy_schedule_state_is_migrated_to_versioned_schema(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    state_file = settings.data_dir / "web-schedules.json"
    state_file.write_text(
        json.dumps(
            {
                "schedules": [
                    {
                        "id": "legacy-schedule",
                        "cameras": [{"id": "camera-1", "name": "Front Door", "state": None, "model": None}],
                        "speed": "600x",
                        "created_at": datetime(2026, 7, 20, tzinfo=UTC).isoformat(),
                        "last_run_day": None,
                        "paused": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)

    async def exercise() -> None:
        await state.start()
        assert state.schedules["legacy-schedule"].paused is True
        await state.close()

    asyncio.run(exercise())

    assert json.loads(state_file.read_text(encoding="utf-8"))["version"] == 1


def test_daily_schedule_pauses_after_bounded_backoff_and_can_be_resumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    schedule = DailySchedule(id="schedule-1", cameras=[camera], speed="600x")
    monkeypatch.setattr("timelapse.web_state.random.uniform", lambda _start, _end: 0.0)

    async def exercise() -> None:
        await state.start()
        state.schedules[schedule.id] = schedule
        delays = [await state._record_schedule_failure(schedule, "Protect unavailable") for _attempt in range(5)]
        assert delays == [60.0, 120.0, 240.0, 480.0, None]
        assert schedule.paused is True
        assert schedule.failure_count == 5
        assert schedule.next_retry_at is None
        resumed = await state.retry_schedule(schedule.id)
        assert resumed.paused is False
        assert resumed.failure_count == 0
        assert resumed.last_error is None
        assert resumed.task is not None
        resumed.task.cancel()
        await asyncio.gather(resumed.task, return_exceptions=True)
        await state.close()

    asyncio.run(exercise())

    stored = json.loads((settings.data_dir / "web-schedules.json").read_text(encoding="utf-8"))
    assert stored["schedules"][0]["paused"] is False


def test_paused_schedule_is_visible_as_needing_attention(tmp_path: Path) -> None:
    app, state = _app(tmp_path)
    state.schedules["schedule-1"] = DailySchedule(
        id="schedule-1",
        cameras=[CameraInfo(id="camera-1", name="Front Door", state=None, model=None)],
        speed="600x",
        last_error="Protect unavailable. Paused after 5 failed attempts.",
        failure_count=5,
        paused=True,
    )

    with _client(app) as client:
        response = client.get("/partials/schedules")

    assert response.status_code == 200
    assert "Needs attention" in response.text
    assert ">Retry</button>" in response.text
    assert "Paused after 5 failed attempts" in response.text

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from timelapse.download import DownloadProgress
from timelapse.protect import CameraInfo
from timelapse.service import CameraThumbnail
from timelapse.web import create_app, main
from timelapse.web_state import WebSettings, WebState

if TYPE_CHECKING:
    from pathlib import Path

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


def test_dashboard_and_local_assets_render(tmp_path: Path) -> None:
    app, _state = _app(tmp_path)

    with TestClient(app) as client:
        response = client.get("/")
        javascript = client.get("/static/htmx.min.js")

    assert response.status_code == 200
    assert "Turn recorded days into" not in response.text
    assert "Local control center" not in response.text
    assert "htmx.min.js" in response.text
    assert "cdn.jsdelivr.net" not in response.text
    assert 'id="server-info-button"' in response.text
    assert 'id="server-info-dialog"' in response.text
    assert 'section-number">01' not in response.text
    assert 'section-number">02' not in response.text
    assert 'section-number">03' not in response.text
    assert 'href="/healthz"' not in response.text
    assert javascript.status_code == 200
    assert "htmx" in javascript.text


def test_login_session_protects_ui_but_not_health(tmp_path: Path) -> None:
    app, _state = _app(tmp_path, web_password="web-secret")  # noqa: S106 - test credential

    with TestClient(app) as client:
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


def test_login_rejects_external_return_path_and_throttles_failures(tmp_path: Path) -> None:
    app, _state = _app(tmp_path, web_password="web-secret")  # noqa: S106 - test credential

    with TestClient(app) as client:
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


def test_authenticated_mutation_accepts_forwarded_public_origin(tmp_path: Path) -> None:
    app, _state = _app(tmp_path, web_password="web-secret")  # noqa: S106 - test credential

    with TestClient(app) as client:
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

    assert response.status_code == 303


def test_cross_origin_mutation_is_rejected(tmp_path: Path) -> None:
    app, state = _app(tmp_path)

    with TestClient(app) as client:
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

    with TestClient(app) as client:
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
        jobs_response = client.get("/partials/jobs")
        for _attempt in range(10):
            jobs_response = client.get("/partials/jobs")
            if all(job.status == "completed" for job in state.jobs.values()):
                break
        job = next(iter(state.jobs.values()))
        preview = client.get(f"/api/thumbnails/{job.camera.id}", params={"timestamp": start.isoformat()})
        download = client.get(f"/exports/{job.id}")

    assert camera_response.status_code == 200
    assert "Front Door" in camera_response.text
    assert created.status_code == 200
    assert "Started 2 exports" in created.text
    assert created.headers["hx-trigger"] == "stateChanged"
    assert len(state.jobs) == 2
    assert "Ready" in jobs_response.text
    assert preview.content == b"jpeg-data"
    assert preview.headers["x-timelapse-thumbnail-source"] == "exact"
    assert download.status_code == 200
    assert download.content == b"video"


def test_invalid_export_returns_actionable_message(tmp_path: Path) -> None:
    app, state = _app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/actions/export",
            data={"range_mode": "full-day", "day": "2026-07-20", "speed": "600x"},
        )

    assert response.status_code == 200
    assert "Select at least one available camera" in response.text
    assert not state.jobs


def test_incomplete_connection_does_not_render_secrets(tmp_path: Path) -> None:
    app, _state = _app(tmp_path, configured=False)

    with TestClient(app) as client:
        status = client.get("/partials/status")
        cameras = client.get("/partials/cameras")

    assert status.status_code == 200
    assert "Server healthy" in status.text
    assert "Configuration needed" in status.text
    assert "UNIFI_PROTECT_TOKEN" in status.text
    assert "integration-token" not in status.text
    assert "Server configuration is incomplete" in cameras.text


def test_daily_schedule_is_persisted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = WebState(settings, camera_loader=_cameras, thumbnail_loader=_thumbnail, exporter=_export)

    async def exercise() -> None:
        await state.start()
        schedule = await state.create_schedule(["camera-1"], "600x")
        await asyncio.sleep(0)
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

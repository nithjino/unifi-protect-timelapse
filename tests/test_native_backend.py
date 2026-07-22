from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

import timelapse.native_backend as backend
from timelapse.download import DownloadProgress
from timelapse.protect import CameraInfo
from timelapse.service import CameraThumbnail

if TYPE_CHECKING:
    from pathlib import Path

    from timelapse.config import Config


def _settings() -> dict[str, object]:
    return {
        "instance_url": "https://protect.local/proxy/protect/integration/v1",
        "token": "test-token",
        "username": "timelapse-user",
        "password": "test-password",
        "verify_ssl": True,
        "request_timeout_seconds": 0,
        "max_download_mib": 10240,
    }


def test_health_command_emits_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, object]] = []
    monkeypatch.setattr(backend, "_write_event", events.append)

    asyncio.run(backend._dispatch({"id": "health-1", "command": "health"}))

    assert events == [{"id": "health-1", "event": "complete", "status": "ok"}]


def test_cancellation_sentinel_cancels_running_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    started = asyncio.Event()

    async def blocking_dispatch(_request: object) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(backend, "_dispatch", blocking_dispatch)
    monkeypatch.setattr(backend, "_write_event", events.append)
    cancel_path = tmp_path / "download.cancel"

    async def exercise() -> int:
        task = asyncio.create_task(
            backend._run({"id": "download-1", "command": "download", "cancel_path": str(cancel_path)})
        )
        await started.wait()
        cancel_path.touch()
        return await task

    assert asyncio.run(exercise()) == 0
    assert events[-1] == {"id": "download-1", "event": "cancelled"}


def test_list_cameras_emits_detached_camera_data(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, object]] = []

    async def fake_list(_config: object) -> list[CameraInfo]:
        return [CameraInfo(id="camera-1", name="Front Door", state="CONNECTED", model="G5")]

    monkeypatch.setattr(backend, "list_available_cameras", fake_list)
    monkeypatch.setattr(backend, "_write_event", events.append)

    asyncio.run(backend._dispatch({"id": "list-1", "command": "list_cameras", "settings": _settings()}))

    assert events == [
        {
            "id": "list-1",
            "event": "cameras",
            "cameras": [{"id": "camera-1", "name": "Front Door", "state": "CONNECTED", "model": "G5"}],
        }
    ]


def test_download_emits_progress_and_complete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    output = tmp_path / "output.mp4"
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model="G5")
    start = datetime(2026, 7, 11, 8, tzinfo=UTC)
    end = datetime(2026, 7, 11, 9, tzinfo=UTC)

    async def fake_export(
        config: Config,
        requested_camera: CameraInfo,
        requested_output: Path,
        progress_callback: object,
    ) -> None:
        assert (config.start, config.end, config.speed) == (start, end, "120x")
        assert requested_camera == camera
        callback = progress_callback
        assert callable(callback)
        callback(DownloadProgress(1024, 4096, 512.0, 2.0))
        requested_output.write_bytes(b"video")  # noqa: ASYNC240 - synchronous export test double

    request = {
        "id": "download-1",
        "command": "download",
        "settings": _settings(),
        "camera": {"id": camera.id, "name": camera.name, "state": camera.state, "model": camera.model},
        "start": start.isoformat(),
        "end": end.isoformat(),
        "speed": "120x",
        "output": str(output),
    }
    monkeypatch.setattr(backend, "export_timelapse", fake_export)
    monkeypatch.setattr(backend, "_write_event", events.append)

    asyncio.run(backend._dispatch(request))

    assert events[0] == {
        "id": "download-1",
        "event": "progress",
        "downloaded_bytes": 1024,
        "total_bytes": 4096,
        "bytes_per_second": 512.0,
        "elapsed_seconds": 2.0,
    }
    assert events[1] == {"id": "download-1", "event": "complete", "output": str(output)}


def test_thumbnail_emits_base64_image(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, object]] = []
    timestamp = datetime(2026, 7, 11, 8, tzinfo=UTC)

    async def fake_thumbnail(_config: object, camera: CameraInfo, requested_time: datetime) -> CameraThumbnail:
        assert camera.id == "camera-1"
        assert requested_time == timestamp
        return CameraThumbnail(b"jpeg-image", "live")

    request = {
        "id": "thumbnail-1",
        "command": "thumbnail",
        "settings": _settings(),
        "camera": {"id": "camera-1", "name": "Front Door", "state": None, "model": "G5"},
        "timestamp": timestamp.isoformat(),
    }
    monkeypatch.setattr(backend, "fetch_camera_thumbnail", fake_thumbnail)
    monkeypatch.setattr(backend, "_write_event", events.append)

    asyncio.run(backend._dispatch(request))

    assert events == [
        {
            "id": "thumbnail-1",
            "event": "thumbnail",
            "thumbnail_base64": "anBlZy1pbWFnZQ==",
            "thumbnail_source": "live",
        }
    ]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"id": "bad", "command": "unknown"}, "unsupported command"),
        ({"id": "bad", "command": "list_cameras", "settings": {}}, "instance_url"),
        (
            {
                "id": "bad",
                "command": "download",
                "settings": _settings(),
                "camera": {"id": "1", "name": "Camera"},
                "start": "2026-07-11T08:00:00",
                "end": "2026-07-11T09:00:00+00:00",
                "speed": "120x",
                "output": "output.mp4",
            },
            "timezone",
        ),
    ],
)
def test_invalid_requests_raise_protocol_errors(payload: dict[str, object], message: str) -> None:
    with pytest.raises(backend._ProtocolError, match=message):
        asyncio.run(backend._dispatch(payload))

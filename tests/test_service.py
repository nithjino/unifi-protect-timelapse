from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from uiprotect.exceptions import NotAuthorized

from timelapse import OperationTimeoutError, TimelapseError, service
from timelapse.config import Config
from timelapse.protect import CameraInfo

if TYPE_CHECKING:
    from pathlib import Path


class _FakeClient:
    def __init__(self, result: bytes | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.request_url = ""
        self.request_params: dict[str, object] = {}
        self.closed = False

    async def api_request_raw(self, url: str, **kwargs: object) -> bytes | None:
        self.request_url = url
        params = kwargs.get("params")
        assert isinstance(params, dict)
        self.request_params = params
        assert kwargs.get("raise_exception") is True
        if self.error is not None:
            raise self.error
        return self.result

    async def close_session(self) -> None:
        self.closed = True


class _LiveFallbackClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def api_request_raw(self, url: str, **kwargs: object) -> bytes | None:
        self.calls += 1
        if self.calls == 1:
            raise NotAuthorized
        assert kwargs.get("public_api") is True
        assert url == "/v1/cameras/camera-1/snapshot"
        return b"live-jpeg"


def _config(tmp_path: Path) -> Config:
    start = datetime(2026, 7, 18, 15, 35, tzinfo=UTC)
    return Config(
        instance_url="https://protect.local/proxy/protect/integration/v1",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=True,
        speed="600x",
        start=start,
        end=start + timedelta(seconds=1),
        output=tmp_path / "output.mp4",
        request_timeout_seconds=0,
        max_download_mib=10240,
    )


def test_thumbnail_requests_historical_snapshot_with_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeClient(result=b"jpeg")
    config = _config(tmp_path)
    camera = CameraInfo(id="camera-1", name="Garage Door", state=None, model=None)
    timestamp = config.start
    monkeypatch.setattr(service, "create_client", lambda _config, _connection: client)

    result = asyncio.run(service.fetch_camera_thumbnail(config, camera, timestamp))

    assert result.image == b"jpeg"
    assert result.source == "exact"
    assert client.request_url == "cameras/camera-1/recording-snapshot"
    assert client.request_params == {"ts": int(timestamp.timestamp() * 1000), "w": 384, "h": 216}
    assert client.closed is True


def test_thumbnail_permission_error_is_actionable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeClient(error=NotAuthorized("Unauthorized request"))
    config = _config(tmp_path)
    camera = CameraInfo(id="camera-1", name="Garage Door", state=None, model=None)
    monkeypatch.setattr(service, "create_client", lambda _config, _connection: client)

    with pytest.raises(TimelapseError, match="readmedia/livestream"):
        asyncio.run(service.fetch_camera_thumbnail(config, camera, config.start))

    assert client.closed is True


def test_thumbnail_falls_back_to_api_token_live_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _LiveFallbackClient()
    config = _config(tmp_path)
    camera = CameraInfo(id="camera-1", name="Garage Door", state=None, model=None)
    monkeypatch.setattr(service, "create_client", lambda _config, _connection: client)

    result = asyncio.run(service.fetch_camera_thumbnail(config, camera, config.start))

    assert result.image == b"live-jpeg"
    assert result.source == "live"
    assert client.calls == 2
    assert client.closed is True


def test_camera_discovery_timeout_covers_the_complete_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    config = replace(_config(tmp_path), request_timeout_seconds=1)

    async def blocked_load(_client: object) -> list[CameraInfo]:
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(service, "create_client", lambda _config, _connection: client)
    monkeypatch.setattr(service, "load_cameras", blocked_load)
    monkeypatch.setattr(
        service,
        "_operation_deadline",
        lambda _seconds: asyncio.get_running_loop().time() + 0.01,
    )

    with pytest.raises(OperationTimeoutError, match="configured 1-second operation timeout"):
        asyncio.run(service.list_available_cameras(config))

    assert client.closed is True

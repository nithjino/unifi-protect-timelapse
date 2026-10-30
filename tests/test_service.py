from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from uiprotect.exceptions import NotAuthorized, NvrError

from timelapse import OperationTimeoutError, TimelapseError, service
from timelapse.config import Config
from timelapse.protect import EXPORT_SLOTS, CameraInfo, ConsoleSlots, ProtectClients

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from timelapse.protect import ProtectConnection


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


class _CleanupFailureClient(_FakeClient):
    async def close_session(self) -> None:
        message = "cleanup failed"
        raise OSError(message)


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


class _FakeClients:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client
        self.console = ConsoleSlots()

    def get(self, _config: Config, _connection: object) -> _FakeClient:
        return self.client

    def slots(self, _connection: object) -> ConsoleSlots:
        return self.console


def _clients(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> ProtectClients:
    @asynccontextmanager
    async def fake_private_operation(
        slots: asyncio.Semaphore,
        _client: object,
        *,
        operation: str,
    ) -> AsyncIterator[None]:
        del operation
        async with slots:
            yield

    monkeypatch.setattr(service, "private_operation", fake_private_operation)
    return cast("ProtectClients", _FakeClients(client))


def test_thumbnail_requests_historical_snapshot_with_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeClient(result=b"jpeg")
    config = _config(tmp_path)
    camera = CameraInfo(id="camera-1", name="Garage Door", state=None, model=None)
    timestamp = config.start
    clients = _clients(monkeypatch, client)

    result = asyncio.run(service.fetch_camera_thumbnail(clients, config, camera, timestamp))

    assert result.image == b"jpeg"
    assert result.source == "exact"
    assert client.request_url == "cameras/camera-1/recording-snapshot"
    assert client.request_params == {"ts": int(timestamp.timestamp() * 1000), "w": 384, "h": 216}


def test_thumbnail_permission_error_is_actionable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeClient(error=NotAuthorized("Unauthorized request"))
    config = _config(tmp_path)
    camera = CameraInfo(id="camera-1", name="Garage Door", state=None, model=None)
    clients = _clients(monkeypatch, client)

    with pytest.raises(TimelapseError, match="readmedia/livestream"):
        asyncio.run(service.fetch_camera_thumbnail(clients, config, camera, config.start))


def test_thumbnail_falls_back_to_api_token_live_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _LiveFallbackClient()
    config = _config(tmp_path)
    camera = CameraInfo(id="camera-1", name="Garage Door", state=None, model=None)
    clients = _clients(monkeypatch, client)

    result = asyncio.run(service.fetch_camera_thumbnail(clients, config, camera, config.start))

    assert result.image == b"live-jpeg"
    assert result.source == "live"
    assert client.calls == 2


def test_camera_discovery_timeout_covers_the_complete_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    config = replace(_config(tmp_path), request_timeout_seconds=1)

    async def blocked_load(_client: object) -> list[CameraInfo]:
        await asyncio.Event().wait()
        return []

    clients = _clients(monkeypatch, client)
    monkeypatch.setattr(service, "load_cameras", blocked_load)

    with pytest.raises(OperationTimeoutError, match="configured 1-second operation timeout"):
        asyncio.run(service.list_available_cameras(clients, config))


def test_console_failure_during_discovery_is_a_timelapse_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def unreachable(_client: object) -> list[CameraInfo]:
        raise NvrError("Cannot connect to host protect.local:443")  # noqa: EM101, TRY003

    clients = _clients(monkeypatch, _FakeClient())
    monkeypatch.setattr(service, "load_cameras", unreachable)

    with pytest.raises(TimelapseError, match=r"Could not load cameras.*Cannot connect to host"):
        asyncio.run(service.list_available_cameras(clients, _config(tmp_path)))


def test_thumbnail_does_not_wait_behind_exports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _FakeClient(result=b"jpeg")
    config = _config(tmp_path)
    camera = CameraInfo(id="camera-1", name="Garage Door", state=None, model=None)
    clients = _clients(monkeypatch, client)

    async def exercise() -> bytes:
        exports = clients.slots(cast("ProtectConnection", None)).exports
        for _ in range(EXPORT_SLOTS):
            await exports.acquire()
        result = await asyncio.wait_for(service.fetch_camera_thumbnail(clients, config, camera, config.start), 1)
        return result.image

    assert asyncio.run(exercise()) == b"jpeg"

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from uiprotect import ProtectApiClient

import timelapse.protect as protect_module
from timelapse import TimelapseError
from timelapse.config import Config
from timelapse.protect import (
    CameraInfo,
    create_client,
    load_cameras,
    parse_connection,
    protect_client,
    protect_session_scope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _CameraClient:
    def __init__(self, cameras: list[object]) -> None:
        self.cameras = cameras

    async def get_cameras_public(self) -> list[object]:
        return self.cameras


class _Response:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.released = False

    def release(self) -> None:
        self.released = True


@pytest.mark.parametrize(
    "url",
    [
        "http://protect.local",
        "https://user:password@protect.local",  # trufflehog:ignore
        "https://protect.local?token=secret",
        "https://protect.local#fragment",
        "https://protect.local:invalid",
    ],
)
def test_parse_connection_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(TimelapseError):
        parse_connection(url)


def test_parse_connection_builds_export_path() -> None:
    connection = parse_connection("https://protect.local:7443/proxy/protect/integration/v1")

    assert connection.host == "protect.local"
    assert connection.port == 7443
    assert connection.export_path == "/proxy/protect/api/video/export"


def test_create_client_persists_sessions_and_bounds_library_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = Config(
        instance_url="https://protect.local",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=True,
        speed="120x",
        start=datetime(2026, 7, 11, tzinfo=UTC),
        end=datetime(2026, 7, 12, tzinfo=UTC),
        output=None,
        request_timeout_seconds=3600,
        max_download_mib=10240,
    )

    sentinel = object()
    received: list[tuple[str, int, dict[str, object]]] = []

    def fake_client(host: str, port: int, **kwargs: object) -> object:
        received.append((host, port, kwargs))
        return sentinel

    monkeypatch.setattr(protect_module, "_RetryingProtectApiClient", fake_client)
    monkeypatch.setattr(protect_module, "_SESSION_DIRECTORY", tmp_path)

    client = create_client(config, parse_connection(config.instance_url))

    assert client is sentinel
    assert received == [
        (
            "protect.local",
            443,
            {
                "username": "timelapse-user",
                "password": "test-password",
                "api_key": "test-token",
                "verify_ssl": True,
                "config_dir": tmp_path,
                "store_sessions": True,
                "max_retries": 0,
            },
        )
    ]


def test_session_scope_reuses_and_closes_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(
        instance_url="https://protect.local",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=True,
        speed="120x",
        start=datetime(2026, 7, 11, tzinfo=UTC),
        end=datetime(2026, 7, 12, tzinfo=UTC),
        output=None,
        request_timeout_seconds=3600,
        max_download_mib=10240,
    )
    created: list[object] = []

    class FakeClient:
        closed = False

        async def close_session(self) -> None:
            self.closed = True

    def fake_create(_config: Config, _connection: object) -> FakeClient:
        client = FakeClient()
        created.append(client)
        return client

    monkeypatch.setattr(protect_module, "create_client", fake_create)

    async def exercise() -> tuple[object, object]:
        connection = parse_connection(config.instance_url)
        async with protect_session_scope():
            async with protect_client(config, connection) as first:
                pass
            async with protect_client(config, connection) as second:
                pass
            assert cast("FakeClient", first).closed is False
        return first, second

    first, second = asyncio.run(exercise())

    assert first is second
    assert created == [first]
    assert cast("FakeClient", first).closed is True


def test_retry_layer_honors_retry_after_and_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    responses = [_Response(429, {"Retry-After": "2"}), _Response(200)]
    sleeps: list[float] = []

    async def fake_request(_client: object, *_args: object, **_kwargs: object) -> _Response:
        return responses.pop(0)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(ProtectApiClient, "request", fake_request)
    monkeypatch.setattr(protect_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(protect_module.random, "uniform", lambda _low, _high: 0.0)
    client = protect_module._RetryingProtectApiClient(
        "protect.local",
        443,
        "user",
        "password",
        config_dir=tmp_path,
        store_sessions=True,
        max_retries=0,
    )

    response = asyncio.run(client.request("get", "/test", auto_close=False))

    assert response.status == 200
    assert sleeps == [2.0]


def test_retry_layer_does_not_retry_before_a_long_retry_after(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    response = _Response(429, {"Retry-After": "900"})
    calls = 0

    async def fake_request(_client: object, *_args: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        return response

    async def unexpected_sleep(_delay: float) -> None:
        pytest.fail("a long Retry-After must be returned to the caller instead of retried early")

    monkeypatch.setattr(ProtectApiClient, "request", fake_request)
    monkeypatch.setattr(protect_module.asyncio, "sleep", unexpected_sleep)
    client = protect_module._RetryingProtectApiClient(
        "protect.local",
        443,
        "user",
        "password",
        config_dir=tmp_path,
        store_sessions=True,
        max_retries=0,
    )

    result = asyncio.run(client.request("get", "/test", auto_close=False))

    assert result is response
    assert calls == 1
    assert client.rate_limit_retry_after == 900


def test_retry_layer_stops_after_four_total_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_request(_client: object, *_args: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        return _Response(429)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(ProtectApiClient, "request", fake_request)
    monkeypatch.setattr(protect_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(protect_module.random, "uniform", lambda _low, _high: 0.0)
    client = protect_module._RetryingProtectApiClient(
        "protect.local",
        443,
        "user",
        "password",
        config_dir=tmp_path,
        store_sessions=True,
        max_retries=0,
    )

    response = asyncio.run(client.request("get", "/test", auto_close=False))

    assert response.status == 429
    assert calls == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_concurrent_clients_collapse_login_to_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stored_session = False
    login_count = 0
    authentication_lock = asyncio.Lock()

    @asynccontextmanager
    async def fake_file_lock(_path: Path) -> AsyncIterator[None]:
        async with authentication_lock:
            yield

    def fake_is_authenticated(client: object) -> bool:
        return bool(getattr(client, "_test_authenticated", False))

    async def fake_load_session(client: object) -> None:
        client.__dict__["_test_authenticated"] = stored_session

    async def fake_authenticate(client: object) -> None:
        nonlocal login_count, stored_session
        login_count += 1
        stored_session = True
        client.__dict__["_test_authenticated"] = True

    monkeypatch.setattr(protect_module, "_exclusive_file_lock", fake_file_lock)
    monkeypatch.setattr(protect_module._RetryingProtectApiClient, "is_authenticated", fake_is_authenticated)
    monkeypatch.setattr(protect_module._RetryingProtectApiClient, "_load_session", fake_load_session)
    monkeypatch.setattr(ProtectApiClient, "authenticate", fake_authenticate)
    monkeypatch.setattr(protect_module, "_secure_session_storage", lambda: None)

    async def exercise() -> None:
        clients = [
            protect_module._RetryingProtectApiClient(
                "protect.local",
                443,
                "user",
                "password",
                config_dir=tmp_path,
                store_sessions=True,
                max_retries=0,
            )
            for _ in range(2)
        ]
        await asyncio.gather(*(client.authenticate() for client in clients))

    asyncio.run(exercise())

    assert login_count == 1


def test_private_operations_use_two_console_wide_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_paths: tuple[Path, ...] = ()

    @asynccontextmanager
    async def fake_slots(paths: tuple[Path, ...]) -> AsyncIterator[None]:
        nonlocal captured_paths
        captured_paths = paths
        yield

    class FakeClient:
        authenticated = 0

        async def ensure_authenticated(self) -> None:
            self.authenticated += 1

    client = FakeClient()
    monkeypatch.setattr(protect_module, "_SESSION_DIRECTORY", tmp_path)
    monkeypatch.setattr(protect_module, "_first_available_lock", fake_slots)

    async def exercise() -> None:
        async with protect_module.private_operation(
            parse_connection("https://protect.local"),
            cast("ProtectApiClient", client),
            operation="test",
        ):
            pass

    asyncio.run(exercise())

    assert len(captured_paths) == 2
    assert {path.parent for path in captured_paths} == {tmp_path}
    assert client.authenticated == 1


def test_load_cameras_returns_sorted_detached_camera_info() -> None:
    client = cast(
        "ProtectApiClient",
        _CameraClient(
            [
                SimpleNamespace(id="camera-2", name="Zebra", state="CONNECTED", model="G5"),
                SimpleNamespace(id="camera-1", name="alpha", state=None, model="G4"),
            ]
        ),
    )

    cameras = asyncio.run(load_cameras(client))

    assert cameras == [
        CameraInfo(id="camera-1", name="alpha", state=None, model="G4"),
        CameraInfo(id="camera-2", name="Zebra", state="CONNECTED", model="G5"),
    ]

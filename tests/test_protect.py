from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from uiprotect import ProtectApiClient

import timelapse.protect as protect_module
from timelapse import TimelapseError
from timelapse.config import Config, ConnectionSettings
from timelapse.protect import (
    CameraInfo,
    ProtectClients,
    create_client,
    load_cameras,
    parse_connection,
)

if TYPE_CHECKING:
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
        async with ProtectClients() as clients:
            first = clients.get(config, connection)
            second = clients.get(config, connection)
            assert cast("FakeClient", first).closed is False
        return first, second

    first, second = asyncio.run(exercise())

    assert first is second
    assert created == [first]
    assert cast("FakeClient", first).closed is True


def test_failed_client_cleanup_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def close_session(self) -> None:
            raise OSError

    config = ConnectionSettings(
        instance_url="https://protect.local",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=True,
    )
    monkeypatch.setattr(protect_module, "create_client", lambda _config, _connection: FakeClient())

    async def exercise() -> None:
        async with ProtectClients() as clients:
            clients.get(config, parse_connection(config.instance_url))

    asyncio.run(exercise())


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


def _session_token() -> str:
    def encode(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    expires = int((datetime.now(UTC) + timedelta(days=1)).timestamp())
    return f"{encode({'alg': 'HS256', 'typ': 'JWT'})}.{encode({'exp': expires})}.{encode({})}"


def _login_client(tmp_path: Path) -> protect_module._RetryingProtectApiClient:
    return protect_module._RetryingProtectApiClient(
        "protect.local",
        443,
        "user",
        "password",
        config_dir=tmp_path,
        store_sessions=True,
        max_retries=0,
    )


def test_login_is_shared_and_reused_after_a_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    logins = 0

    async def fake_request(_client: object, _method: str, url: str, **_kwargs: object) -> _Response:
        nonlocal logins
        assert url == "/api/auth/login"
        logins += 1
        await asyncio.sleep(0)
        return _Response(200, {"set-cookie": f"TOKEN={_session_token()}; Path=/", "x-csrf-token": "csrf"})

    monkeypatch.setattr(ProtectApiClient, "request", fake_request)
    monkeypatch.setattr(protect_module, "_SESSION_DIRECTORY", tmp_path)

    async def exercise() -> None:
        client = _login_client(tmp_path)
        await asyncio.gather(*(client.ensure_authenticated() for _ in range(4)))
        await client.close_session()
        assert logins == 1

        restarted = _login_client(tmp_path)
        await restarted.ensure_authenticated()
        await restarted.close_session()

    asyncio.run(exercise())

    assert logins == 1


def test_export_and_thumbnail_slots_are_separate_per_console() -> None:
    clients = ProtectClients()
    connection = parse_connection("https://protect.local")

    slots = clients.slots(connection)

    assert slots is clients.slots(connection)
    assert slots is not clients.slots(parse_connection("https://other.local"))
    assert slots.exports is not slots.thumbnails
    assert slots.exports._value == protect_module.EXPORT_SLOTS
    assert slots.thumbnails._value == protect_module.THUMBNAIL_SLOTS


def test_private_operation_authenticates_inside_its_slot() -> None:
    slots = asyncio.Semaphore(1)

    class FakeClient:
        locked_during_login = False

        async def ensure_authenticated(self) -> None:
            self.locked_during_login = slots.locked()

    client = FakeClient()

    async def exercise() -> None:
        async with protect_module.private_operation(slots, cast("ProtectApiClient", client), operation="test"):
            assert slots.locked()

    asyncio.run(exercise())

    assert client.locked_during_login is True
    assert slots.locked() is False


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

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

import timelapse.protect as protect_module
from timelapse import TimelapseError
from timelapse.config import Config
from timelapse.protect import CameraInfo, create_client, load_cameras, parse_connection

if TYPE_CHECKING:
    from uiprotect import ProtectApiClient


class _CameraClient:
    def __init__(self, cameras: list[object]) -> None:
        self.cameras = cameras

    async def get_cameras_public(self) -> list[object]:
        return self.cameras


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


def test_create_client_passes_public_and_private_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(protect_module, "ProtectApiClient", fake_client)

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
                "store_sessions": False,
            },
        )
    ]


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

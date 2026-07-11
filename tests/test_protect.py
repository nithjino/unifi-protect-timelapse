from __future__ import annotations

from datetime import UTC, datetime

import pytest

from timelapse import TimelapseError
from timelapse.config import Config
from timelapse.protect import create_client, parse_connection


@pytest.mark.parametrize(
    "url",
    [
        "http://protect.local",
        "https://user:password@protect.local",
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


def test_create_client_supports_public_and_private_authentication() -> None:
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

    client = create_client(config, parse_connection(config.instance_url))

    assert client.is_public_only is False

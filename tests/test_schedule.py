from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from timelapse.config import Config
from timelapse.protect import CameraInfo
from timelapse.schedule import config_for_local_day, daily_output_path, latest_complete_local_day, local_day_bounds

if TYPE_CHECKING:
    from pathlib import Path


def _config() -> Config:
    return Config(
        instance_url="https://protect.local",
        token="token",  # noqa: S106
        username="user",
        password="password",  # noqa: S106
        verify_ssl=True,
        speed="600x",
        start=datetime(2026, 7, 10, tzinfo=UTC),
        end=datetime(2026, 7, 11, tzinfo=UTC),
        output=None,
        request_timeout_seconds=0,
        max_download_mib=10240,
        daily=True,
    )


def test_local_day_helpers_create_calendar_day_and_daily_prefix(tmp_path: Path) -> None:
    start, end = local_day_bounds(date(2026, 7, 11))
    config = config_for_local_day(_config(), date(2026, 7, 11))
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)

    assert config.start == start
    assert config.end == end
    assert daily_output_path(config, camera, tmp_path).name.startswith("daily_timelapse_Front_Door_")


def test_latest_complete_local_day_uses_previous_calendar_date() -> None:
    now = datetime(2026, 7, 13, 15, tzinfo=UTC)

    assert latest_complete_local_day(now) == date(2026, 7, 12)

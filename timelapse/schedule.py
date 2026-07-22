"""Calendar-day helpers shared by automatic timelapse schedulers."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from timelapse.download import default_output_path

if TYPE_CHECKING:
    from pathlib import Path

    from timelapse.config import Config
    from timelapse.protect import CameraInfo


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return timezone-aware local midnights surrounding one calendar day."""
    start = datetime.combine(day, time.min).astimezone()
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    return start, end


def latest_complete_local_day(now: datetime | None = None) -> date:
    """Return the most recent local calendar day that has fully elapsed."""
    local_now = (now or datetime.now().astimezone()).astimezone()
    return local_now.date() - timedelta(days=1)


def daily_output_path(config: Config, camera: CameraInfo, directory: Path) -> Path:
    """Build an output path for a completed local calendar day."""
    return directory / default_output_path(replace(config, full_day=True), camera).name


def config_for_local_day(config: Config, day: date) -> Config:
    """Copy runtime settings with one completed local calendar day as the range."""
    start, end = local_day_bounds(day)
    return replace(config, start=start, end=end, output=None, full_day=True)


def seconds_until_next_local_day(now: datetime | None = None) -> float:
    """Return the delay until the next local midnight."""
    local_now = (now or datetime.now().astimezone()).astimezone()
    next_midnight = local_day_bounds(local_now.date())[1]
    return max((next_midnight - local_now).total_seconds(), 1.0)

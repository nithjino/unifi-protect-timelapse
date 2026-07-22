from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from timelapse import cli
from timelapse.config import Config
from timelapse.protect import CameraInfo

if TYPE_CHECKING:
    from pathlib import Path


def _daily_config(output: Path) -> Config:
    now = datetime(2026, 7, 22, tzinfo=UTC)
    return Config(
        instance_url="https://protect.local",
        token="token",  # noqa: S106 - test credential
        username="user",
        password="password",  # noqa: S106 - test credential
        verify_ssl=True,
        speed="600x",
        start=now,
        end=now + timedelta(seconds=1),
        output=output,
        request_timeout_seconds=0,
        max_download_mib=1024,
        daily=True,
    )


def test_daily_cli_retries_same_day_and_persists_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_day = date(2026, 7, 21)
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    attempts: list[date] = []
    sleeps: list[float] = []

    async def flaky_export(config: Config, _camera: CameraInfo, output: Path) -> None:
        attempts.append(config.start.date())
        if len(attempts) < 3:
            message = "Protect unavailable"
            raise OSError(message)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")  # noqa: ASYNC240 - synchronous test double

    async def controlled_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(cli, "latest_complete_local_day", lambda: completed_day)
    monkeypatch.setattr(cli, "seconds_until_next_local_day", lambda: 3600.0)
    monkeypatch.setattr(cli, "_export", flaky_export)
    monkeypatch.setattr(cli.asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(cli.random, "uniform", lambda _start, _end: 0.0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli._run_daily(_daily_config(tmp_path), camera))

    assert attempts == [completed_day, completed_day, completed_day]
    assert sleeps == [30.0, 60.0, 3600.0]
    checkpoint = cli._daily_checkpoint_path(tmp_path, camera, "600x")
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload == {"next_day": "2026-07-22", "version": 1}

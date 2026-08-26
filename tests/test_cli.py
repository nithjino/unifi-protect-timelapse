from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from timelapse import cli
from timelapse.automation_registry import AutomationCamera, AutomationRegistry, ConnectionReference
from timelapse.config import Config, ConnectionSettings
from timelapse.jobs import ExportJobCoordinator
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
        connection_kind="dotenv",
        connection_value=str(output / ".env"),
    )


def test_daily_cli_imports_checkpoint_before_running_matching_automation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    checkpoint = cli._daily_checkpoint_path(tmp_path, camera, "600x")
    cli._save_daily_checkpoint(checkpoint, date(2026, 7, 21))
    captured = {}

    class FakeEngine:
        async def run_forever(self, automation_id: str) -> None:
            captured["automation"] = captured["registry"].resolve(automation_id)
            raise asyncio.CancelledError

    def engine(registry, _coordinator):
        captured["registry"] = registry
        return FakeEngine()

    monkeypatch.setattr(cli, "_automation_engine", engine)
    monkeypatch.setattr(cli, "_local_timezone_name", lambda: "UTC")
    monkeypatch.setenv(cli.CLI_REGISTRY_ENV, str(tmp_path / "cli-automations.json"))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli._run_daily(_daily_config(tmp_path), camera))

    automation = captured["automation"]
    assert automation.next_day == date(2026, 7, 21)
    assert automation.connection.kind == "dotenv"
    assert not checkpoint.exists()
    assert checkpoint.with_suffix(f"{checkpoint.suffix}.v1-backup").exists()


def test_cli_dotenv_automation_uses_stored_speed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "UNIFI_PROTECT_URL=https://protect.local\nUNIFI_PROTECT_TOKEN=test-token\n"
        "UNIFI_PROTECT_USERNAME=test-user\nUNIFI_PROTECT_PASSWORD=test-password\n"
        "UNIFI_PROTECT_VERIFY_SSL=false\nTIMELAPSE_REQUEST_TIMEOUT_SECONDS=37\nTIMELAPSE_MAX_DOWNLOAD_MIB=321\n",
        encoding="utf-8",
    )
    camera = CameraInfo("camera-1", "Front", None, None)
    registry = AutomationRegistry(tmp_path / "automations.json")
    registry.load()
    automation = registry.add(
        name="CLI",
        cameras=(AutomationCamera(camera.id, camera.name),),
        connection=ConnectionReference("dotenv", str(dotenv)),
        speed="120x",
        output_directory=tmp_path,
        timezone="UTC",
    )

    async def cameras(connection):
        assert isinstance(connection, ConnectionSettings)
        assert connection.token == "test-token"  # noqa: S105 - test credential
        assert connection.request_timeout_seconds == 37
        assert connection.max_download_mib == 321
        assert connection.verify_ssl is False
        return [camera]

    async def export(config, _camera, output):
        assert config.speed == "120x"
        assert "120x" in output.name
        output.write_bytes(b"\0\0\0\x18ftypisom")

    monkeypatch.setattr(cli, "list_available_cameras", cameras)
    monkeypatch.setattr(cli, "export_timelapse", export)
    result = asyncio.run(cli._automation_engine(registry, ExportJobCoordinator()).run_due_once(automation.id))
    assert result is not None
    assert result.completed

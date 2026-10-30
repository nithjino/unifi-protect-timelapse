from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from timelapse import cli
from timelapse.automation_registry import AutomationCamera, AutomationRegistry, ConnectionReference
from timelapse.config import ConnectionSettings
from timelapse.jobs import ExportJobCoordinator
from timelapse.protect import CameraInfo, ProtectClients

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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

    async def cameras(_clients, connection):
        assert isinstance(connection, ConnectionSettings)
        assert connection.token == "test-token"  # noqa: S105 - test credential
        assert connection.request_timeout_seconds == 37
        assert connection.max_download_mib == 321
        assert connection.verify_ssl is False
        return [camera]

    async def export(_clients, config, _camera, output):
        assert config.speed == "120x"
        assert "120x" in output.name
        output.write_bytes(b"\0\0\0\x18ftypisom")

    monkeypatch.setattr(cli, "list_available_cameras", cameras)
    monkeypatch.setattr(cli, "export_timelapse", export)
    assert asyncio.run(
        cli._automation_engine(ProtectClients(), registry, ExportJobCoordinator()).run_due_once(automation.id)
    )


def test_management_commands_are_refused_while_a_runner_owns_the_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "automations.json"
    monkeypatch.setenv(cli.CLI_REGISTRY_ENV, str(path))
    with AutomationRegistry(path).owner():
        assert asyncio.run(cli._run_automation_command(ProtectClients(), ["list"])) == cli.CLI_OWNER_EXIT_CODE
    assert "Stop the running automation process" in capsys.readouterr().err
    assert asyncio.run(cli._run_automation_command(ProtectClients(), ["list"])) == 0

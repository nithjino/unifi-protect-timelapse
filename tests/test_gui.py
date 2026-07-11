from __future__ import annotations

import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from dotenv import dotenv_values
from PySide6.QtCore import Qt

import timelapse.gui as gui_module
from timelapse.download import DownloadProgress, default_output_path
from timelapse.protect import CameraInfo

if TYPE_CHECKING:
    from pathlib import Path

    from pytestqt.qtbot import QtBot

_REQUIRED_ENVIRONMENT_VARIABLES = (
    "UNIFI_PROTECT_URL",
    "UNIFI_PROTECT_TOKEN",
    "UNIFI_PROTECT_USERNAME",
    "UNIFI_PROTECT_PASSWORD",
    "UNIFI_PROTECT_VERIFY_SSL",
    "TIMELAPSE_REQUEST_TIMEOUT_SECONDS",
    "TIMELAPSE_MAX_DOWNLOAD_MIB",
)


class _MemorySettings:
    def __init__(self, *_args: object) -> None:
        self._values: dict[str, object] = {}

    def value(self, key: str) -> object | None:
        return self._values.get(key)

    def setValue(self, key: str, value: object) -> None:  # noqa: N802
        self._values[key] = value


def _connection_settings() -> gui_module._ConnectionSettings:
    return gui_module._ConnectionSettings(
        instance_url="https://protect.local/proxy/protect/integration/v1",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=True,
        request_timeout_seconds=0,
        max_download_mib=10240,
    )


@pytest.fixture
def main_window(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> gui_module._MainWindow:
    monkeypatch.setattr(gui_module, "QSettings", _MemorySettings)
    window = gui_module._MainWindow(_connection_settings(), tmp_path / ".env")
    qtbot.add_widget(window)
    return window


def _table_text(window: gui_module._MainWindow, row: int, column: int) -> str:
    item = window._downloads.item(row, column)
    assert item is not None
    return item.text()


def test_date_editors_offer_calendar_popups(main_window: gui_module._MainWindow) -> None:
    assert main_window._start_edit.calendarPopup() is True
    assert main_window._end_edit.calendarPopup() is True


def test_missing_dotenv_values_require_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in _REQUIRED_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "UNIFI_PROTECT_URL='https://protect.local/proxy/protect/integration/v1'\n",
        encoding="utf-8",
    )

    settings = gui_module._environment_settings(dotenv_path)

    assert settings.missing_fields() == ["API token", "username", "password"]
    assert gui_module._settings_need_prompt(settings) is True


def test_invalid_verify_ssl_value_does_not_disable_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFI_PROTECT_VERIFY_SSL", "flase")

    assert gui_module._environment_bool("UNIFI_PROTECT_VERIFY_SSL", default=True) is True


def test_write_dotenv_is_private_and_round_trips_quoted_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "token with spaces, #hash, 'single', \"double\", and \\slash\nnext line"  # noqa: S105
    password = "pa'ss \\word # value\r\nsecond line"  # noqa: S105
    settings = gui_module._ConnectionSettings(
        instance_url="https://protect.local/proxy/protect/integration/v1",
        token=token,
        username="local user #1",
        password=password,
        verify_ssl=False,
        request_timeout_seconds=0,
        max_download_mib=10240,
    )
    dotenv_path = tmp_path / ".env"

    gui_module._write_dotenv(dotenv_path, settings)

    values = dotenv_values(dotenv_path)
    assert stat.S_IMODE(dotenv_path.stat().st_mode) == 0o600
    assert values["UNIFI_PROTECT_URL"] == settings.instance_url
    assert values["UNIFI_PROTECT_TOKEN"] == token
    assert values["UNIFI_PROTECT_USERNAME"] == settings.username
    assert values["UNIFI_PROTECT_PASSWORD"] == password
    assert values["UNIFI_PROTECT_VERIFY_SSL"] == "false"
    assert token not in repr(settings)
    assert password not in repr(settings)
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert password not in captured.out
    assert password not in captured.err


def test_camera_dialog_returns_multiple_checked_cameras(qtbot: QtBot) -> None:
    cameras = [
        CameraInfo(id="camera-1", name="Front Door", state="CONNECTED", model="G5"),
        CameraInfo(id="camera-2", name="Driveway", state="CONNECTED", model="G4"),
        CameraInfo(id="camera-3", name="Garden", state=None, model=None),
    ]
    dialog = gui_module._CameraSelectionDialog(cameras, set(), None)
    qtbot.add_widget(dialog)
    first_item = dialog._camera_list.item(0)
    third_item = dialog._camera_list.item(2)
    assert first_item is not None
    assert third_item is not None
    first_item.setCheckState(Qt.CheckState.Checked)
    third_item.setCheckState(Qt.CheckState.Checked)

    assert dialog.selected_cameras() == [cameras[0], cameras[2]]


def test_output_reservation_uses_camera_name_and_unique_suffixes(
    main_window: gui_module._MainWindow,
    tmp_path: Path,
) -> None:
    config = _connection_settings().make_config(
        datetime(2026, 7, 11, 8, tzinfo=UTC),
        datetime(2026, 7, 11, 9, tzinfo=UTC),
        "120x",
    )
    camera = CameraInfo(id="camera-1", name="Front Door", state="CONNECTED", model="G5")
    preferred = tmp_path / default_output_path(config, camera).name
    preferred.write_bytes(b"existing")

    first = main_window._reserve_output_path(preferred)
    second = main_window._reserve_output_path(preferred)

    assert "Front_Door" in preferred.name
    assert first == preferred.with_name(f"{preferred.stem}_2{preferred.suffix}").resolve()
    assert second == preferred.with_name(f"{preferred.stem}_3{preferred.suffix}").resolve()


def test_progress_row_shows_known_and_unknown_totals(
    main_window: gui_module._MainWindow,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state="CONNECTED", model="G5")
    config = _connection_settings().make_config(
        datetime(2026, 7, 11, 8, tzinfo=UTC),
        datetime(2026, 7, 11, 9, tzinfo=UTC),
        "120x",
    )
    output = tmp_path / "output.mp4"
    worker = gui_module._DownloadWorker(config, camera, output, main_window)
    entry = main_window._add_download_row(1, camera, output, worker)

    main_window._download_progress(
        entry,
        DownloadProgress(
            downloaded_bytes=1536,
            total_bytes=4096,
            bytes_per_second=2048,
            elapsed_seconds=1,
        ),
    )

    assert _table_text(main_window, entry.row, gui_module._COLUMN_STATUS) == "Downloading"
    assert _table_text(main_window, entry.row, gui_module._COLUMN_DOWNLOADED) == "1.5 KiB"
    assert _table_text(main_window, entry.row, gui_module._COLUMN_EXPECTED) == "4.0 KiB"
    assert _table_text(main_window, entry.row, gui_module._COLUMN_SPEED) == "2.0 KiB/s"
    assert entry.progress_bar.minimum() == 0
    assert entry.progress_bar.maximum() == gui_module._PROGRESS_SCALE
    assert entry.progress_bar.value() == 375
    assert entry.progress_bar.format() == "37.5%"

    main_window._download_progress(
        entry,
        DownloadProgress(
            downloaded_bytes=2048,
            total_bytes=None,
            bytes_per_second=1024,
            elapsed_seconds=2,
        ),
    )

    assert _table_text(main_window, entry.row, gui_module._COLUMN_DOWNLOADED) == "2.0 KiB"
    assert _table_text(main_window, entry.row, gui_module._COLUMN_EXPECTED) == "Unknown"
    assert _table_text(main_window, entry.row, gui_module._COLUMN_SPEED) == "1.0 KiB/s"
    assert entry.progress_bar.minimum() == 0
    assert entry.progress_bar.maximum() == 0


def test_stalled_download_speed_falls_to_zero(
    main_window: gui_module._MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    config = _connection_settings().make_config(
        datetime(2026, 7, 11, 8, tzinfo=UTC),
        datetime(2026, 7, 11, 9, tzinfo=UTC),
        "120x",
    )
    worker = gui_module._DownloadWorker(config, camera, tmp_path / "output.mp4", main_window)
    entry = main_window._add_download_row(1, camera, tmp_path / "output.mp4", worker)
    main_window._workers[worker] = entry
    times = iter((10.0, 13.0))
    monkeypatch.setattr(gui_module, "monotonic", lambda: next(times))

    main_window._download_progress(entry, DownloadProgress(1024, 4096, 512, 2))
    main_window._clear_stalled_speeds()
    main_window._workers.clear()

    assert _table_text(main_window, entry.row, gui_module._COLUMN_SPEED) == "0 bytes/s"

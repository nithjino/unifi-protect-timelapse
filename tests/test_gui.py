from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
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


class _MemoryProfileStore(gui_module._ProfileStore):
    def __init__(self, state: gui_module._ProfileState | None = None) -> None:
        self.state = state or gui_module._ProfileState((), None)

    def load(self) -> gui_module._ProfileState:
        return self.state

    def save(self, state: gui_module._ProfileState) -> None:
        self.state = state


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
) -> gui_module._MainWindow:
    monkeypatch.setattr(gui_module, "QSettings", _MemorySettings)
    window = gui_module._MainWindow(_connection_settings())
    qtbot.add_widget(window)
    return window


def _entry_text(window: gui_module._MainWindow, entry: gui_module._DownloadEntry, column: int) -> str:
    table = window._daily_automations if entry.daily_schedule else window._downloads
    item = table.item(entry.row, column)
    assert item is not None
    return item.text()


def test_date_editors_offer_calendar_popups(main_window: gui_module._MainWindow) -> None:
    assert main_window._start_edit.calendarPopup() is True
    assert main_window._end_edit.calendarPopup() is True


def test_24_hour_toggle_uses_date_only_one_day_range(main_window: gui_module._MainWindow) -> None:
    main_window._full_day_checkbox.setChecked(True)

    start = main_window._start_edit.dateTime()
    end = main_window._end_edit.dateTime()

    assert "h:mm" not in main_window._start_edit.displayFormat()
    assert start.time().hour() == 0
    assert end == start.addDays(1)


def test_daily_schedule_adds_list_row_and_daily_downloads(
    main_window: gui_module._MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cameras = (CameraInfo(id="camera-1", name="Front Door", state=None, model=None),)
    schedule_entry = main_window._add_daily_schedule_row(cameras, tmp_path)
    main_window._daily_schedule = gui_module._DailySchedule(cameras, tmp_path, "600x", schedule_entry)
    started: list[gui_module._DownloadEntry] = []
    monkeypatch.setattr(gui_module, "latest_complete_local_day", lambda: date(2026, 7, 12))
    monkeypatch.setattr(main_window, "_start_download_worker", lambda entry, _worker: started.append(entry))

    main_window._run_daily_schedule_if_due()

    assert main_window._job_tabs.tabText(0) == "Downloads"
    assert main_window._job_tabs.tabText(1) == "Daily Automations"
    assert main_window._daily_automations.rowCount() == 1
    assert _entry_text(main_window, schedule_entry, gui_module._COLUMN_STATUS) == "Scheduled daily"
    assert len(started) == 1
    assert started[0].output.name.startswith("daily_timelapse_Front_Door_")
    assert main_window._downloads.rowCount() == 1

    main_window._stop_daily_schedule()
    assert _entry_text(main_window, schedule_entry, gui_module._COLUMN_STATUS) == "Stopped"


def test_logs_button_opens_separate_window_and_displays_logs(
    main_window: gui_module._MainWindow,
    qtbot: QtBot,
) -> None:
    main_window.show()
    gui_module._LOGGER.info("visible test log")

    qtbot.mouseClick(main_window._logs_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(main_window._logs_window.isVisible)

    assert "visible test log" in main_window._logs_window.output.toPlainText()

    main_window._logs_window.close()
    qtbot.waitUntil(main_window._logs_window.isHidden)


def test_activity_indicator_tracks_background_work(
    main_window: gui_module._MainWindow,
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

    expected_end = config.end.astimezone().strftime("%I:%M %p")
    assert _entry_text(main_window, entry, gui_module._COLUMN_TIME_RANGE) == (
        f"{gui_module._format_job_datetime(config.start)} → {expected_end}"
    )

    assert main_window._activity_widget.isHidden() is True

    main_window._workers[worker] = entry
    main_window._update_activity_indicator()
    assert main_window._activity_widget.isHidden() is False
    assert main_window._activity_bar.minimum() == 0
    assert main_window._activity_bar.maximum() == 0

    main_window._workers.clear()
    main_window._update_activity_indicator()
    assert main_window._activity_widget.isHidden() is True


def test_source_and_bundled_apps_use_appropriate_writable_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(gui_module.sys, "frozen", raising=False)
    assert gui_module._application_dotenv_path() == tmp_path / ".env"
    assert gui_module._default_output_directory() == tmp_path

    monkeypatch.setattr(gui_module.sys, "frozen", True, raising=False)
    assert gui_module._application_dotenv_path() == gui_module._application_data_directory() / ".env"
    assert gui_module._default_output_directory().name == gui_module._APPLICATION_DIRECTORY_NAME


def test_application_icon_path_supports_source_and_bundled_apps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delattr(gui_module.sys, "_MEIPASS", raising=False)
    source_icon = gui_module._application_icon_path()
    assert source_icon.name == "timelapse.png"
    assert source_icon.is_file()

    monkeypatch.setattr(gui_module.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert gui_module._application_icon_path() == tmp_path / "timelapse_assets" / "timelapse.png"


def test_qt_gui_supports_windows_macos_and_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui_module.os, "name", "posix")
    monkeypatch.setattr(gui_module.sys, "platform", "darwin")
    assert gui_module._is_supported_gui_platform() is True

    monkeypatch.setattr(gui_module.sys, "platform", "linux")
    assert gui_module._is_supported_gui_platform() is True

    monkeypatch.setattr(gui_module.sys, "platform", "freebsd")
    assert gui_module._is_supported_gui_platform() is False

    monkeypatch.setattr(gui_module.os, "name", "nt")
    assert gui_module._is_supported_gui_platform() is True


def test_macos_uses_application_support_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui_module.os, "name", "posix")
    monkeypatch.setattr(gui_module.sys, "platform", "darwin")

    assert gui_module._application_data_directory() == (
        gui_module.Path.home() / "Library" / "Application Support" / gui_module._APPLICATION_DIRECTORY_NAME
    )


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


def test_legacy_dotenv_migrates_to_secure_profile_and_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in _REQUIRED_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gui_module.sys, "frozen", True, raising=False)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        """UNIFI_PROTECT_URL="https://protect.local/proxy/protect/integration/v1"
UNIFI_PROTECT_TOKEN="token"
UNIFI_PROTECT_USERNAME="user"
UNIFI_PROTECT_PASSWORD="password"
UNIFI_PROTECT_VERIFY_SSL=false
""",
        encoding="utf-8",
    )
    store = _MemoryProfileStore()

    state, exit_code = gui_module._initial_profiles(dotenv_path, store)

    assert exit_code == 0
    assert state is not None
    assert state.selected_profile is not None
    assert state.selected_profile.settings.verify_ssl is False
    assert store.state == state
    assert not dotenv_path.exists()


def test_invalid_verify_ssl_value_does_not_disable_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFI_PROTECT_VERIFY_SSL", "flase")

    assert gui_module._environment_bool("UNIFI_PROTECT_VERIFY_SSL", default=True) is True


def test_profile_store_keeps_secrets_out_of_qsettings(monkeypatch: pytest.MonkeyPatch) -> None:
    preferences = _MemorySettings()
    secrets: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        gui_module.keyring,
        "set_password",
        lambda service, account, value: secrets.__setitem__((service, account), value),
    )
    monkeypatch.setattr(gui_module.keyring, "get_password", lambda service, account: secrets.get((service, account)))
    monkeypatch.setattr(gui_module.keyring, "delete_password", lambda service, account: secrets.pop((service, account)))
    first = gui_module._ConnectionProfile("one", "Home", _connection_settings())
    second_settings = gui_module._ConnectionSettings(
        instance_url="https://office.local/proxy/protect/integration/v1",
        token="office-token",  # noqa: S106
        username="office-user",
        password="office-password",  # noqa: S106
        verify_ssl=False,
        request_timeout_seconds=0,
        max_download_mib=10240,
    )
    second = gui_module._ConnectionProfile("two", "", second_settings)
    store = gui_module._ProfileStore(preferences)

    store.save(gui_module._ProfileState((first, second), second.profile_id))
    loaded = store.load()

    assert loaded.profiles == (first, second.normalized())
    assert loaded.selected_profile_id == second.profile_id
    assert first.settings.token not in repr(preferences._values)
    assert second.settings.password not in repr(preferences._values)


def test_profile_name_defaults_to_protect_url() -> None:
    profile = gui_module._ConnectionProfile("profile", "   ", _connection_settings()).normalized()

    assert profile.display_name == profile.settings.instance_url


def test_profile_dropdown_switches_active_connection(qtbot: QtBot) -> None:
    first = gui_module._ConnectionProfile("one", "Home", _connection_settings())
    second_settings = gui_module._ConnectionSettings(
        instance_url="https://office.local/proxy/protect/integration/v1",
        token="office-token",  # noqa: S106
        username="office-user",
        password="office-password",  # noqa: S106
        verify_ssl=True,
        request_timeout_seconds=0,
        max_download_mib=10240,
    )
    second = gui_module._ConnectionProfile("two", "Office", second_settings)
    store = _MemoryProfileStore(gui_module._ProfileState((first, second), first.profile_id))
    window = gui_module._MainWindow(store.state, profile_store=store)
    qtbot.add_widget(window)

    window._profile_combo.setCurrentIndex(1)

    assert window._settings == second_settings
    assert store.state.selected_profile_id == second.profile_id
    assert window._connection_label.text() == second_settings.instance_url


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

    assert _entry_text(main_window, entry, gui_module._COLUMN_STATUS) == "Downloading"
    assert _entry_text(main_window, entry, gui_module._COLUMN_DOWNLOADED) == "1.5 KiB"
    assert _entry_text(main_window, entry, gui_module._COLUMN_EXPECTED) == "4.0 KiB"
    assert _entry_text(main_window, entry, gui_module._COLUMN_SPEED) == "2.0 KiB/s"
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

    assert _entry_text(main_window, entry, gui_module._COLUMN_DOWNLOADED) == "2.0 KiB"
    assert _entry_text(main_window, entry, gui_module._COLUMN_EXPECTED) == "Unknown"
    assert _entry_text(main_window, entry, gui_module._COLUMN_SPEED) == "1.0 KiB/s"
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

    assert _entry_text(main_window, entry, gui_module._COLUMN_SPEED) == "0 bytes/s"


def test_bulk_controls_only_affect_the_active_job_tab(
    main_window: gui_module._MainWindow,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    config = _connection_settings().make_config(
        datetime(2026, 7, 11, 8, tzinfo=UTC),
        datetime(2026, 7, 11, 9, tzinfo=UTC),
        "120x",
    )
    worker = gui_module._DownloadWorker(config, camera, tmp_path / "active.mp4", main_window)
    download_entry = main_window._add_download_row(1, camera, tmp_path / "active.mp4", worker)
    main_window._workers[worker] = download_entry
    schedule_entry = main_window._add_daily_schedule_row((camera,), tmp_path)
    main_window._daily_schedule = gui_module._DailySchedule((camera,), tmp_path, "600x", schedule_entry)

    main_window._job_tabs.setCurrentIndex(0)
    main_window._update_bulk_buttons()
    assert main_window._cancel_all_button.text() == "Cancel All"
    main_window._cancel_all_jobs()
    assert download_entry.cancelling is True
    assert main_window._daily_schedule is not None

    main_window._job_tabs.setCurrentIndex(1)
    assert main_window._cancel_all_button.text() == "Stop All"
    main_window._cancel_all_jobs()
    assert main_window._daily_schedule is None
    assert schedule_entry.terminal is True
    main_window._workers.clear()


def test_bulk_download_controls_preserve_active_rows(
    main_window: gui_module._MainWindow,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    config = _connection_settings().make_config(
        datetime(2026, 7, 11, 8, tzinfo=UTC),
        datetime(2026, 7, 11, 9, tzinfo=UTC),
        "120x",
    )
    finished_worker = gui_module._DownloadWorker(config, camera, tmp_path / "finished.mp4", main_window)
    active_worker = gui_module._DownloadWorker(config, camera, tmp_path / "active.mp4", main_window)
    finished = main_window._add_download_row(1, camera, tmp_path / "finished.mp4", finished_worker)
    active = main_window._add_download_row(2, camera, tmp_path / "active.mp4", active_worker)
    finished.terminal = True
    finished.completed = True
    main_window._workers[active_worker] = active
    main_window._update_bulk_buttons()

    assert main_window._clear_all_button.isEnabled() is True
    assert main_window._cancel_all_button.isEnabled() is True

    main_window._clear_finished_jobs()
    assert main_window._downloads.rowCount() == 1
    assert main_window._entries == [active]

    main_window._cancel_all_jobs()
    assert active.cancelling is True
    assert main_window._cancel_all_button.isEnabled() is False
    main_window._workers.clear()


def test_cancelled_job_can_restart_and_be_removed(
    main_window: gui_module._MainWindow,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    config = _connection_settings().make_config(
        datetime(2026, 7, 11, 8, tzinfo=UTC),
        datetime(2026, 7, 11, 9, tzinfo=UTC),
        "120x",
    )
    worker = gui_module._DownloadWorker(config, camera, tmp_path / "cancelled.mp4", main_window)
    entry = main_window._add_download_row(1, camera, tmp_path / "cancelled.mp4", worker)
    main_window._workers[worker] = entry

    main_window._download_cancelled(entry)
    main_window._download_worker_finished(worker)

    assert entry.action_button.text() == "Restart"
    assert entry.action_button.isEnabled() is True
    main_window._remove_entry(entry)
    assert main_window._downloads.rowCount() == 0


def test_double_click_completed_job_opens_video(
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
    output = tmp_path / "completed.mp4"
    output.write_bytes(b"video")
    worker = gui_module._DownloadWorker(config, camera, output, main_window)
    entry = main_window._add_download_row(1, camera, output, worker)
    entry.terminal = True
    entry.completed = True
    opened: list[str] = []
    monkeypatch.setattr(gui_module.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))

    main_window._open_completed_video(main_window._downloads, entry.row, gui_module._COLUMN_CAMERA)

    assert opened == [str(output)]

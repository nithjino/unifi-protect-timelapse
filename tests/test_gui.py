from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import Qt

import timelapse.gui as gui_module
from timelapse import __version__
from timelapse.automation_registry import AutomationCamera, ConnectionReference, DailyAutomation
from timelapse.download import DownloadProgress
from timelapse.protect import CameraInfo
from timelapse.service import CameraThumbnail

if TYPE_CHECKING:
    from pathlib import Path

    from pytestqt.qtbot import QtBot

    from timelapse.config import Config

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


class _FakeAutomationRuntime:
    def __init__(self, *_args: object) -> None:
        self.automations: list[DailyAutomation] = []

    def list(self) -> list[DailyAutomation]:
        return list(self.automations)

    def update_profiles(self, _profiles: object) -> None:
        return

    def add(self, **values: object) -> DailyAutomation:
        cameras = values["cameras"]
        assert isinstance(cameras, tuple)
        automation = DailyAutomation(
            id=f"auto_{len(self.automations) + 1:032x}",
            name=str(values["name"]),
            cameras=tuple(AutomationCamera(camera.id, camera.name) for camera in cameras),
            connection=ConnectionReference("python-profile", str(values["profile_id"])),
            speed=str(values["speed"]),
            output_directory=str(values["output_directory"]),
            timezone=str(values["timezone"]),
            created_at=datetime(2026, 7, 13, tzinfo=UTC),
            status="active",
            next_day=date(2026, 7, 12),
        )
        self.automations.append(automation)
        return automation

    def edit(self, automation_id: str, **_values: object) -> None:
        assert any(item.id == automation_id for item in self.automations)

    def stop(self, automation_id: str) -> None:
        self._status(automation_id, "stopped")

    def resume(self, automation_id: str) -> None:
        self._status(automation_id, "active")

    def remove(self, automation_id: str) -> None:
        self.automations = [item for item in self.automations if item.id != automation_id]

    def close(self) -> None:
        return

    async def export_manual(self, config, camera, output, progress_callback, path_claimed) -> None:
        path_claimed(output)
        await gui_module.export_timelapse(config, camera, output, progress_callback)

    def _status(self, automation_id: str, status: str) -> None:
        self.automations = [
            replace(item, status=status) if item.id == automation_id else item for item in self.automations
        ]


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


def _export_config() -> Config:
    return _connection_settings().make_config(
        datetime(2026, 7, 11, 8, tzinfo=UTC),
        datetime(2026, 7, 11, 9, tzinfo=UTC),
        "120x",
    )


@pytest.fixture
def main_window(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> gui_module._MainWindow:
    monkeypatch.setattr(gui_module, "QSettings", _MemorySettings)
    monkeypatch.setattr(gui_module, "_QtAutomationRuntime", _FakeAutomationRuntime)
    window = gui_module._MainWindow(_connection_settings())
    qtbot.add_widget(window)
    return window


def _entry_text(window: gui_module._MainWindow, entry: gui_module._DownloadEntry, column: int) -> str:
    table = window._daily_automations if entry.daily_schedule else window._downloads
    item = table.item(entry.row, column)
    assert item is not None
    return item.text()


def test_window_title_includes_application_version(main_window: gui_module._MainWindow) -> None:
    assert __version__ != "0+unknown"
    assert main_window.windowTitle() == f"UniFi Protect Timelapse - {__version__}"


def test_24_hour_toggle_uses_date_only_one_day_range(main_window: gui_module._MainWindow) -> None:
    main_window._full_day_checkbox.setChecked(True)

    start = main_window._start_edit.dateTime()
    end = main_window._end_edit.dateTime()

    assert "h:mm" not in main_window._start_edit.displayFormat()
    assert start.time().hour() == 0
    assert end == start.addDays(1)


def test_speed_selector_lists_normal_speed_first(main_window: gui_module._MainWindow) -> None:
    speeds = [main_window._speed_combo.itemText(index) for index in range(main_window._speed_combo.count())]

    assert speeds == ["1x", "60x", "120x", "300x", "600x"]
    assert main_window._speed_combo.currentText() == "600x"


def test_24_hour_hover_preview_uses_midnight_and_prompts_for_camera(main_window: gui_module._MainWindow) -> None:
    main_window._full_day_checkbox.setChecked(True)

    main_window._show_thumbnail_preview("start")

    assert main_window._thumbnail_timestamp("start").hour == 0
    assert main_window._thumbnail_popup.image_label.text() == "Select a camera to preview this time."


def test_thumbnail_loader_fetches_historical_image(
    main_window: gui_module._MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    timestamp = datetime(2026, 7, 11, 8, tzinfo=UTC).astimezone()
    config = main_window._settings.make_config(timestamp, timestamp.replace(hour=9), "600x")

    async def fake_thumbnail(
        _config: object,
        requested_camera: CameraInfo,
        requested_time: datetime,
    ) -> CameraThumbnail:
        assert requested_camera == camera
        assert requested_time == timestamp
        return CameraThumbnail(b"image-data", "exact")

    monkeypatch.setattr(gui_module, "fetch_camera_thumbnail", fake_thumbnail)
    loader = gui_module._ThumbnailLoader(config, camera, timestamp)

    with qtbot.waitSignal(loader.thumbnail_loaded, timeout=2_000) as signal:
        loader.start()

    assert signal.args == [CameraThumbnail(b"image-data", "exact")]
    loader.wait()


def test_failed_thumbnail_is_not_retried_on_hover(main_window: gui_module._MainWindow) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    main_window._selected_cameras = [camera]
    timestamp = main_window._thumbnail_timestamp("start")
    cache_key = (camera.id, round(timestamp.timestamp()))
    main_window._thumbnail_failures[cache_key] = "Rate limited"

    main_window._show_thumbnail_preview("start")
    main_window._show_thumbnail_preview("start")

    assert main_window._thumbnail_popup.image_label.text() == "Rate limited"
    assert not main_window._thumbnail_loaders


def test_preview_camera_dropdown_reuses_cached_thumbnails(main_window: gui_module._MainWindow) -> None:
    front = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    back = CameraInfo(id="camera-2", name="Back Yard", state=None, model=None)
    main_window._selected_cameras = [front, back]
    main_window._update_camera_summary()
    timestamps = [round(main_window._thumbnail_timestamp(boundary).timestamp()) for boundary in ("start", "end")]
    for camera in (front, back):
        for timestamp in timestamps:
            main_window._thumbnail_cache[(camera.id, timestamp)] = CameraThumbnail(b"cached-image", "exact")

    main_window._preview_camera_combo.setCurrentIndex(main_window._preview_camera_combo.findData(back.id))
    main_window._preview_camera_combo.setCurrentIndex(main_window._preview_camera_combo.findData(front.id))

    assert main_window._selected_preview_camera() == front
    assert len(main_window._thumbnail_cache) == 4
    assert not main_window._thumbnail_loaders


def test_datetime_change_prefetches_without_hover(
    main_window: gui_module._MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        main_window,
        "_show_thumbnail_preview",
        lambda boundary, *, display=True: requests.append((boundary, display)),
    )

    main_window._thumbnail_datetime_changed("start", main_window._start_edit.dateTime())

    assert requests == [("start", False)]


def test_daily_automation_list_projects_multiple_durable_automations(
    main_window: gui_module._MainWindow,
    tmp_path: Path,
) -> None:
    cameras = (
        CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
        CameraInfo(id="camera-2", name="Back Door", state=None, model=None),
    )
    runtime = main_window._automation_runtime
    assert isinstance(runtime, _FakeAutomationRuntime)
    first = runtime.add(
        name="Doors",
        cameras=cameras,
        profile_id="test-profile",
        speed="600x",
        output_directory=tmp_path / "doors",
        timezone="UTC",
    )
    second = runtime.add(
        name="Back only",
        cameras=(cameras[1],),
        profile_id="test-profile",
        speed="120x",
        output_directory=tmp_path / "back",
        timezone="UTC",
    )

    main_window._refresh_daily_automations()

    assert main_window._job_tabs.tabText(0) == "Downloads"
    assert main_window._job_tabs.tabText(1) == "Daily Automations"
    assert main_window._daily_automations.rowCount() == 2
    assert {entry.automation_id for entry in main_window._entries if entry.daily_schedule} == {first.id, second.id}

    main_window._stop_daily_automation(first.id)
    stopped = next(item for item in main_window._entries if item.automation_id == first.id)
    assert _entry_text(main_window, stopped, gui_module._COLUMN_STATUS) == "Stopped"
    assert stopped.action_button.text() == "Resume"

    main_window._resume_daily_automation(first.id)
    resumed = next(item for item in main_window._entries if item.automation_id == first.id)
    assert _entry_text(main_window, resumed, gui_module._COLUMN_STATUS) == "Active"


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
    config = _export_config()
    worker = gui_module._DownloadWorker(config, camera, tmp_path / "output.mp4", main_window)
    entry = main_window._add_download_row(1, camera, tmp_path / "output.mp4", worker)

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
    assert loaded.profiles[1].display_name == second_settings.instance_url
    assert loaded.selected_profile_id == second.profile_id
    assert first.settings.token not in repr(preferences._values)
    assert second.settings.password not in repr(preferences._values)


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


def test_progress_row_shows_known_and_unknown_totals(
    main_window: gui_module._MainWindow,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state="CONNECTED", model="G5")
    config = _export_config()
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
    config = _export_config()
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
    config = _export_config()
    worker = gui_module._DownloadWorker(config, camera, tmp_path / "active.mp4", main_window)
    download_entry = main_window._add_download_row(1, camera, tmp_path / "active.mp4", worker)
    main_window._workers[worker] = download_entry
    runtime = main_window._automation_runtime
    assert isinstance(runtime, _FakeAutomationRuntime)
    automation = runtime.add(
        name="Front Door",
        cameras=(camera,),
        profile_id="test-profile",
        speed="600x",
        output_directory=tmp_path,
        timezone="UTC",
    )
    main_window._refresh_daily_automations()

    main_window._job_tabs.setCurrentIndex(0)
    main_window._update_bulk_buttons()
    assert main_window._cancel_all_button.text() == "Cancel All"
    main_window._cancel_all_jobs()
    assert download_entry.cancelling is True
    assert runtime.list()[0].status == "active"

    main_window._job_tabs.setCurrentIndex(1)
    assert main_window._cancel_all_button.text() == "Stop All"
    main_window._cancel_all_jobs()
    assert runtime.list()[0].status == "stopped"
    schedule_entry = next(entry for entry in main_window._entries if entry.automation_id == automation.id)
    assert schedule_entry.terminal is True
    assert schedule_entry.action_button.text() == "Resume"
    main_window._clear_finished_jobs()
    assert tmp_path.exists()
    assert schedule_entry not in main_window._entries
    main_window._workers.clear()


def test_bulk_download_controls_preserve_active_rows(
    main_window: gui_module._MainWindow,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    config = _export_config()
    finished_worker = gui_module._DownloadWorker(config, camera, tmp_path / "finished.mp4", main_window)
    active_worker = gui_module._DownloadWorker(config, camera, tmp_path / "active.mp4", main_window)
    finished = main_window._add_download_row(1, camera, tmp_path / "finished.mp4", finished_worker)
    active = main_window._add_download_row(2, camera, tmp_path / "active.mp4", active_worker)
    finished.output.write_bytes(b"video")
    finished.terminal = True
    finished.completed = True
    main_window._workers[active_worker] = active
    main_window._update_bulk_buttons()

    assert main_window._clear_all_button.isEnabled() is True
    assert main_window._clear_all_button.text() == "Delete All"
    assert main_window._clear_all_button.property("danger") == "true"
    assert main_window._cancel_all_button.isEnabled() is True

    main_window._clear_finished_jobs()
    assert not finished.output.exists()
    assert main_window._downloads.rowCount() == 1
    assert main_window._entries == [active]

    main_window._cancel_all_jobs()
    assert active.cancelling is True
    assert main_window._cancel_all_button.isEnabled() is False
    main_window._workers.clear()


def test_cancelled_job_can_restart_after_its_worker_finishes(
    main_window: gui_module._MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    config = _export_config()
    worker = gui_module._DownloadWorker(config, camera, tmp_path / "cancelled.mp4", main_window)
    entry = main_window._add_download_row(1, camera, tmp_path / "cancelled.mp4", worker)
    main_window._workers[worker] = entry
    restarted: list[gui_module._DownloadWorker] = []

    def capture_restart(
        restarted_entry: gui_module._DownloadEntry, restarted_worker: gui_module._DownloadWorker
    ) -> None:
        restarted_entry.worker = restarted_worker
        restarted.append(restarted_worker)

    monkeypatch.setattr(main_window, "_start_download_worker", capture_restart)

    main_window._download_cancelled(entry)
    assert entry.action_button.isEnabled() is False
    main_window._download_worker_finished(worker)

    assert entry.action_button.text() == "Restart"
    assert entry.action_button.isEnabled() is True
    entry.action_button.click()
    assert len(restarted) == 1
    assert restarted[0] is entry.worker
    assert restarted[0] is not worker
    assert entry.terminal is False
    assert _entry_text(main_window, entry, gui_module._COLUMN_STATUS) == "Preparing export…"


def test_download_terminal_states_send_desktop_notifications(
    main_window: gui_module._MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    config = _export_config()
    notifications: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        main_window,
        "_show_notification",
        lambda title, message, icon: notifications.append((title, message, icon)),
    )

    completed = main_window._add_download_row(
        1,
        camera,
        tmp_path / "completed.mp4",
        gui_module._DownloadWorker(config, camera, tmp_path / "completed.mp4", main_window),
    )
    failed = main_window._add_download_row(
        2,
        camera,
        tmp_path / "failed.mp4",
        gui_module._DownloadWorker(config, camera, tmp_path / "failed.mp4", main_window),
    )
    cancelled = main_window._add_download_row(
        3,
        camera,
        tmp_path / "cancelled.mp4",
        gui_module._DownloadWorker(config, camera, tmp_path / "cancelled.mp4", main_window),
    )

    main_window._download_succeeded(completed, str(completed.output))
    main_window._download_failed(failed, "Connection lost")
    main_window._download_cancelled(cancelled)

    assert [title for title, _message, _icon in notifications] == [
        "Download complete",
        "Download failed",
        "Download interrupted",
    ]
    assert "Connection lost" in notifications[1][1]


def test_linux_notifications_fall_back_to_freedesktop_service(
    main_window: gui_module._MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(gui_module.sys, "platform", "linux")
    monkeypatch.setattr(gui_module.QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    monkeypatch.setattr(
        main_window,
        "_show_linux_notification",
        lambda title, message: delivered.append((title, message)) or True,
    )

    main_window._show_notification(
        "Download complete",
        "Front Door: completed.mp4",
        gui_module.QSystemTrayIcon.MessageIcon.Information,
    )

    assert delivered == [("Download complete", "Front Door: completed.mp4")]


def test_double_click_completed_job_opens_video(
    main_window: gui_module._MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    config = _export_config()
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


def test_qt_automation_resolves_connection_without_export_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    settings = replace(_connection_settings(), request_timeout_seconds=37, max_download_mib=321, verify_ssl=False)
    profile = gui_module._ConnectionProfile("test-profile", "Home", settings)
    camera = CameraInfo("camera-1", "Front", None, None)
    exports = []

    async def cameras(connection) -> list[CameraInfo]:
        assert not hasattr(connection, "speed")
        assert connection.request_timeout_seconds == 37
        assert connection.max_download_mib == 321
        assert connection.verify_ssl is False
        return [camera]

    async def export(config, _camera, output) -> None:
        assert config.speed == "120x"
        assert "120x" in output.name
        output.write_bytes(b"\0\0\0\x18ftypisom")
        exports.append(output)

    monkeypatch.setattr(gui_module, "list_available_cameras", cameras)
    monkeypatch.setattr(gui_module, "export_timelapse", export)
    runtime = gui_module._QtAutomationRuntime(tmp_path / "automations.json", (profile,))
    try:
        runtime.add(
            name="Home",
            cameras=(camera,),
            profile_id=profile.profile_id,
            speed="120x",
            output_directory=tmp_path,
            timezone="UTC",
        )
        qtbot.waitUntil(lambda: bool(exports))
    finally:
        runtime.close()


def test_qt_manual_export_reports_coordinator_suffix_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preferred = tmp_path / "artifact.mp4"
    preferred.write_bytes(b"existing")
    claimed = []
    camera = CameraInfo("camera-1", "Front", None, None)

    async def export(_config, _camera, output, _progress) -> None:
        assert claimed == [tmp_path / "artifact_2.mp4"]
        assert output == claimed[0]
        output.write_bytes(b"video")

    monkeypatch.setattr(gui_module, "export_timelapse", export)
    runtime = gui_module._QtAutomationRuntime(tmp_path / "automations.json", ())
    try:
        asyncio.run(runtime.export_manual(_export_config(), camera, preferred, lambda _progress: None, claimed.append))
    finally:
        runtime.close()
    assert claimed[0].read_bytes() == b"video"


def test_qt_claim_confirmation_updates_visible_output(main_window: gui_module._MainWindow, tmp_path: Path) -> None:
    camera = CameraInfo("camera-1", "Front", None, None)
    preferred = tmp_path / "artifact.mp4"
    worker = gui_module._DownloadWorker(_export_config(), camera, preferred, main_window)
    entry = main_window._add_download_row(1, camera, preferred, worker)
    claimed = tmp_path / "artifact_2.mp4"
    main_window._download_output_claimed(entry, str(claimed))
    assert entry.output == claimed
    assert _entry_text(main_window, entry, gui_module._COLUMN_OUTPUT) == claimed.name

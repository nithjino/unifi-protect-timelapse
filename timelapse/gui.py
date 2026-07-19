"""Cross-platform Qt interface for the UniFi Protect timelapse exporter."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

import keyring
from dotenv import load_dotenv
from keyring.errors import KeyringError
from PySide6.QtCore import (
    QDateTime,
    QObject,
    QPoint,
    QSettings,
    QStandardPaths,
    Qt,
    QThread,
    QTime,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from timelapse import TimelapseError
from timelapse.config import DEFAULT_MAX_DOWNLOAD_MIB, DEFAULT_REQUEST_TIMEOUT_SECONDS, SPEED_TO_FPS, Config
from timelapse.download import DownloadProgress, default_output_path
from timelapse.protect import CameraInfo, parse_connection
from timelapse.schedule import config_for_local_day, daily_output_path, latest_complete_local_day
from timelapse.service import export_timelapse, list_available_cameras

if TYPE_CHECKING:
    from collections.abc import Callable

_URL_TOOLTIP = (
    "The UniFi Protect Integration API address used to connect to your Protect console, for example "
    "https://protect.local/proxy/protect/integration/v1."
)
_TOKEN_TOOLTIP = "Used to query the Protect Integration API and retrieve the list of cameras."  # noqa: S105
_USERNAME_TOOLTIP = (
    "A dedicated local Protect user used to authenticate video exports. Grant it only permission to view and export "
    "recordings."
)
_PASSWORD_TOOLTIP = (
    "The password for the dedicated local Protect user. The API token can list cameras but cannot export recordings."  # noqa: S105
)
_VERIFY_SSL_TOOLTIP = (
    "Verifies the Protect server's TLS certificate. Disable only for a trusted local console using a self-signed "
    "certificate."
)
_BYTE_UNIT = 1024.0
_PROGRESS_SCALE = 1000
_STALE_SPEED_SECONDS = 2.0
_MAX_LOG_LINES = 2000
_APPLICATION_DIRECTORY_NAME = "TimeLapse"
_PROFILE_SERVICE = "io.timelapse.desktop.connection-profile"
_PROFILE_IDS_KEY = "connection_profile_ids"
_SELECTED_PROFILE_KEY = "selected_connection_profile_id"
_ICON_BUNDLE_DIRECTORY = "timelapse_assets"
_ICON_FILENAME = "timelapse.png"
_MINIMUM_DATE = QDateTime.fromString("2000-01-01T00:00:00", Qt.DateFormat.ISODate)
_TABLE_HEADERS = (
    "Job",
    "Camera",
    "Time Range",
    "Status",
    "Progress",
    "Downloaded",
    "Expected",
    "Speed",
    "Output",
    "Action",
)
_COLUMN_JOB = 0
_COLUMN_CAMERA = 1
_COLUMN_TIME_RANGE = 2
_COLUMN_STATUS = 3
_COLUMN_PROGRESS = 4
_COLUMN_DOWNLOADED = 5
_COLUMN_EXPECTED = 6
_COLUMN_SPEED = 7
_COLUMN_OUTPUT = 8
_COLUMN_ACTION = 9
_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)


@dataclass(frozen=True)
class _ConnectionSettings:
    instance_url: str
    token: str = field(repr=False)
    username: str
    password: str = field(repr=False)
    verify_ssl: bool
    request_timeout_seconds: int
    max_download_mib: int

    def missing_fields(self) -> list[str]:
        values = {
            "Protect URL": self.instance_url,
            "API token": self.token,
            "username": self.username,
            "password": self.password,
        }
        return [label for label, value in values.items() if not value.strip()]

    def make_config(self, start: datetime, end: datetime, speed: str) -> Config:
        return Config(
            instance_url=self.instance_url.strip().rstrip("/"),
            token=self.token,
            username=self.username,
            password=self.password,
            verify_ssl=self.verify_ssl,
            speed=speed,
            start=start,
            end=end,
            output=None,
            request_timeout_seconds=self.request_timeout_seconds,
            max_download_mib=self.max_download_mib,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "instance_url": self.instance_url,
            "token": self.token,
            "username": self.username,
            "password": self.password,
            "verify_ssl": self.verify_ssl,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_download_mib": self.max_download_mib,
        }

    @classmethod
    def from_dict(cls, value: object) -> _ConnectionSettings:
        if not isinstance(value, dict):
            message = "profile connection settings are not an object"
            raise TypeError(message)
        return cls(
            instance_url=str(value.get("instance_url", "")),
            token=str(value.get("token", "")),
            username=str(value.get("username", "")),
            password=str(value.get("password", "")),
            verify_ssl=bool(value.get("verify_ssl", True)),
            request_timeout_seconds=_nonnegative_value(
                value.get("request_timeout_seconds"), DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
            max_download_mib=_nonnegative_value(value.get("max_download_mib"), DEFAULT_MAX_DOWNLOAD_MIB),
        )


@dataclass(frozen=True)
class _ConnectionProfile:
    profile_id: str
    name: str
    settings: _ConnectionSettings

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.settings.instance_url.strip().rstrip("/")

    def normalized(self) -> _ConnectionProfile:
        settings = _ConnectionSettings(
            instance_url=self.settings.instance_url.strip().rstrip("/"),
            token=self.settings.token.strip(),
            username=self.settings.username.strip(),
            password=self.settings.password,
            verify_ssl=self.settings.verify_ssl,
            request_timeout_seconds=self.settings.request_timeout_seconds,
            max_download_mib=self.settings.max_download_mib,
        )
        return _ConnectionProfile(self.profile_id, self.name.strip() or settings.instance_url, settings)

    def to_json(self) -> str:
        return json.dumps(
            {"id": self.profile_id, "name": self.display_name, "settings": self.settings.to_dict()},
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> _ConnectionProfile:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            message = "stored profile is not an object"
            raise TypeError(message)
        profile_id = payload.get("id")
        name = payload.get("name")
        if not isinstance(profile_id, str) or not isinstance(name, str):
            message = "stored profile is missing its identifier or name"
            raise TypeError(message)
        return cls(profile_id, name, _ConnectionSettings.from_dict(payload.get("settings"))).normalized()


@dataclass(frozen=True)
class _ProfileState:
    profiles: tuple[_ConnectionProfile, ...]
    selected_profile_id: str | None

    @property
    def selected_profile(self) -> _ConnectionProfile | None:
        return next((profile for profile in self.profiles if profile.profile_id == self.selected_profile_id), None)


class _ProfileStoreError(RuntimeError):
    pass


def _profile_from_keyring(payload: str, expected_id: str) -> _ConnectionProfile:
    profile = _ConnectionProfile.from_json(payload)
    if profile.profile_id != expected_id:
        message = "stored profile identifier does not match its credential-store account"
        raise ValueError(message)
    return profile


class _ProfileStore:
    def __init__(self, preferences: QSettings | None = None) -> None:
        self._preferences = preferences or QSettings("TimeLapse", "UniFi Protect Timelapse")

    def load(self) -> _ProfileState:
        profile_ids = self._stored_profile_ids()
        profiles: list[_ConnectionProfile] = []
        try:
            for profile_id in profile_ids:
                payload = keyring.get_password(_PROFILE_SERVICE, profile_id)
                if payload is not None:
                    profiles.append(_profile_from_keyring(payload, profile_id))
        except (KeyringError, TypeError, ValueError, json.JSONDecodeError) as exc:
            message = (
                f"Could not read connection profiles from the operating system credential store: {_exception_text(exc)}"
            )
            raise _ProfileStoreError(message) from exc
        selected = self._preferences.value(_SELECTED_PROFILE_KEY)
        selected_id = (
            selected if isinstance(selected, str) and any(p.profile_id == selected for p in profiles) else None
        )
        if selected_id is None and profiles:
            selected_id = profiles[0].profile_id
        return _ProfileState(tuple(profiles), selected_id)

    def save(self, state: _ProfileState) -> None:
        normalized_profiles = tuple(profile.normalized() for profile in state.profiles)
        new_ids = [profile.profile_id for profile in normalized_profiles]
        old_ids = self._stored_profile_ids()
        try:
            for profile in normalized_profiles:
                keyring.set_password(_PROFILE_SERVICE, profile.profile_id, profile.to_json())
            for removed_id in set(old_ids) - set(new_ids):
                with suppress(KeyringError):
                    keyring.delete_password(_PROFILE_SERVICE, removed_id)
        except KeyringError as exc:
            message = (
                f"Could not save connection profiles in the operating system credential store: {_exception_text(exc)}"
            )
            raise _ProfileStoreError(message) from exc
        selected_id = (
            state.selected_profile_id if state.selected_profile_id in new_ids else (new_ids[0] if new_ids else "")
        )
        self._preferences.setValue(_PROFILE_IDS_KEY, new_ids)
        self._preferences.setValue(_SELECTED_PROFILE_KEY, selected_id)

    def _stored_profile_ids(self) -> list[str]:
        value = self._preferences.value(_PROFILE_IDS_KEY)
        if isinstance(value, str):
            return [value] if value else []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item]
        return []


@dataclass
class _DownloadEntry:
    row: int
    job_number: int
    output: Path
    camera: CameraInfo
    config: Config
    worker: _DownloadWorker | None
    progress_bar: QProgressBar
    action_button: QPushButton
    downloaded_bytes: int = 0
    last_progress_at: float | None = None
    cancelling: bool = False
    terminal: bool = False
    completed: bool = False
    daily_schedule: bool = False

    @property
    def camera_name(self) -> str:
        return self.camera.name


def _format_job_datetime(value: datetime) -> str:
    local_value = value.astimezone()
    return f"{local_value:%b} {local_value.day}, {local_value:%Y} {local_value:%I:%M %p}"


def _format_time_range(config: Config) -> str:
    start = config.start.astimezone()
    end = config.end.astimezone()
    if start.date() == end.date():
        return f"{_format_job_datetime(start)} → {end:%I:%M %p}"
    return f"{_format_job_datetime(start)} → {_format_job_datetime(end)}"


@dataclass
class _DailySchedule:
    cameras: tuple[CameraInfo, ...]
    output_directory: Path
    speed: str
    entry: _DownloadEntry
    last_run_day: date | None = None


class _LogEmitter(QObject):
    message_ready: ClassVar[Signal] = Signal(str)


class _QtLogHandler(logging.Handler):
    def __init__(self, emitter: _LogEmitter) -> None:
        super().__init__(logging.INFO)
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        """Forward a formatted log record to the GUI thread."""
        try:
            self._emitter.message_ready.emit(self.format(record))
        except Exception:
            self.handleError(record)


class _LogsWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Application Logs")
        self.resize(760, 420)
        self.setMinimumSize(560, 280)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Application Logs"))
        controls.addStretch(1)
        self.clear_button = QPushButton("Clear")
        controls.addWidget(self.clear_button)
        layout.addLayout(controls)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(_MAX_LOG_LINES)
        self.output.setPlaceholderText("Application activity and errors will appear here.")
        layout.addWidget(self.output)

    def show_and_activate(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()


def _is_bundled() -> bool:
    return bool(getattr(sys, "frozen", False))


def _application_icon_path() -> Path:
    bundle_directory = getattr(sys, "_MEIPASS", None)
    if isinstance(bundle_directory, str):
        return Path(bundle_directory) / _ICON_BUNDLE_DIRECTORY / _ICON_FILENAME
    return Path(__file__).resolve().parent.parent / "assets" / "icons" / _ICON_FILENAME


def _application_data_directory() -> Path:
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        base_directory = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
        return base_directory / _APPLICATION_DIRECTORY_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APPLICATION_DIRECTORY_NAME
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base_directory = Path(config_home) if config_home else Path.home() / ".config"
    return base_directory / _APPLICATION_DIRECTORY_NAME


def _application_dotenv_path() -> Path:
    if _is_bundled():
        return _application_data_directory() / ".env"
    return Path.cwd() / ".env"


def _default_output_directory() -> Path:
    if not _is_bundled():
        return Path.cwd()
    movies_directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    base_directory = Path(movies_directory) if movies_directory else Path.home()
    return base_directory / _APPLICATION_DIRECTORY_NAME


def _environment_settings(dotenv_path: Path) -> _ConnectionSettings:
    load_dotenv(dotenv_path=dotenv_path, override=False)
    return _ConnectionSettings(
        instance_url=os.environ.get("UNIFI_PROTECT_URL", "").strip(),
        token=os.environ.get("UNIFI_PROTECT_TOKEN", "").strip(),
        username=os.environ.get("UNIFI_PROTECT_USERNAME", "").strip(),
        password=os.environ.get("UNIFI_PROTECT_PASSWORD", ""),
        verify_ssl=_environment_bool("UNIFI_PROTECT_VERIFY_SSL", default=True),
        request_timeout_seconds=_environment_nonnegative_int(
            "TIMELAPSE_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        ),
        max_download_mib=_environment_nonnegative_int(
            "TIMELAPSE_MAX_DOWNLOAD_MIB",
            DEFAULT_MAX_DOWNLOAD_MIB,
        ),
    )


def _environment_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    return default


def _environment_nonnegative_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _nonnegative_value(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _settings_need_prompt(settings: _ConnectionSettings) -> bool:
    if settings.missing_fields():
        return True
    try:
        parse_connection(settings.instance_url)
    except TimelapseError:
        return True
    return False


def _format_bytes(byte_count: int) -> str:
    value = float(max(byte_count, 0))
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < _BYTE_UNIT or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= _BYTE_UNIT
    return "0 bytes"


def _format_speed(bytes_per_second: float) -> str:
    return "—" if bytes_per_second <= 0 else f"{_format_bytes(round(bytes_per_second))}/s"


def _exception_text(exc: BaseException) -> str:
    detail = str(exc).strip()
    return detail or type(exc).__name__


class _CredentialsDialog(QDialog):
    def __init__(
        self,
        profile: _ConnectionProfile,
        *,
        first_run: bool,
        new_profile: bool,
        parent: QWidget | None,
    ) -> None:
        super().__init__(parent)
        self._existing_profile = profile
        self._result: _ConnectionProfile | None = None
        if first_run:
            title = "Set Up UniFi Protect"
        elif new_profile:
            title = "New Connection Profile"
        else:
            title = "Edit Connection Profile"
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self._build_interface(first_run=first_run)

    def _build_interface(self, *, first_run: bool) -> None:
        layout = QVBoxLayout(self)
        introduction = QLabel(
            "Enter the connection details needed to list cameras and export recordings."
            if first_run
            else "Update the connection details used for future camera lists and downloads."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)

        form = QFormLayout()
        settings = self._existing_profile.settings
        self._name_edit = self._line_edit(self._existing_profile.name, "Name shown in the profile menu.")
        self._name_edit.setPlaceholderText("Defaults to Protect URL")
        self._url_edit = self._line_edit(settings.instance_url, _URL_TOOLTIP)
        self._token_edit = self._line_edit(settings.token, _TOKEN_TOOLTIP, secret=True)
        self._username_edit = self._line_edit(settings.username, _USERNAME_TOOLTIP)
        self._password_edit = self._line_edit(settings.password, _PASSWORD_TOOLTIP, secret=True)
        self._add_field(form, "Profile name:", self._name_edit, "Name shown in the profile menu.")
        self._add_field(form, "Protect URL:", self._url_edit, _URL_TOOLTIP)
        self._add_field(form, "API token:", self._token_edit, _TOKEN_TOOLTIP)
        self._add_field(form, "Local username:", self._username_edit, _USERNAME_TOOLTIP)
        self._add_field(form, "Local password:", self._password_edit, _PASSWORD_TOOLTIP)
        self._verify_ssl = QCheckBox("Verify the server's TLS certificate")
        self._verify_ssl.setChecked(settings.verify_ssl)
        self._verify_ssl.setToolTip(_VERIFY_SSL_TOOLTIP)
        self._add_field(form, "Security:", self._verify_ssl, _VERIFY_SSL_TOOLTIP)
        layout.addLayout(form)

        storage_note = QLabel("Stored securely in the operating system credential store.")
        storage_note.setWordWrap(True)
        storage_note.setToolTip("Uses Windows Credential Manager or the Linux desktop Secret Service.")
        layout.addWidget(storage_note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_settings)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _line_edit(value: str, tooltip: str, *, secret: bool = False) -> QLineEdit:
        editor = QLineEdit(value)
        editor.setToolTip(tooltip)
        if secret:
            editor.setEchoMode(QLineEdit.EchoMode.Password)
        return editor

    @staticmethod
    def _add_field(form: QFormLayout, text: str, widget: QWidget, tooltip: str) -> None:
        label = QLabel(text)
        label.setToolTip(tooltip)
        form.addRow(label, widget)

    @Slot()
    def _accept_settings(self) -> None:
        settings = _ConnectionSettings(
            instance_url=self._url_edit.text().strip().rstrip("/"),
            token=self._token_edit.text().strip(),
            username=self._username_edit.text().strip(),
            password=self._password_edit.text(),
            verify_ssl=self._verify_ssl.isChecked(),
            request_timeout_seconds=self._existing_profile.settings.request_timeout_seconds,
            max_download_mib=self._existing_profile.settings.max_download_mib,
        )
        missing = settings.missing_fields()
        if missing:
            QMessageBox.warning(self, "Missing Connection Details", f"Please provide: {', '.join(missing)}.")
            return
        try:
            parse_connection(settings.instance_url)
        except TimelapseError as exc:
            QMessageBox.warning(self, "Invalid Protect URL", str(exc))
            return
        self._result = _ConnectionProfile(
            self._existing_profile.profile_id,
            self._name_edit.text(),
            settings,
        ).normalized()
        super().accept()

    def selected_profile(self) -> _ConnectionProfile:
        if self._result is None:
            message = "connection profile was requested before the dialog was accepted"
            raise RuntimeError(message)
        return self._result


class _CameraSelectionDialog(QDialog):
    def __init__(self, cameras: list[CameraInfo], selected_ids: set[str], parent: QWidget | None) -> None:
        super().__init__(parent)
        self._cameras = cameras
        self.setWindowTitle("Select Cameras")
        self.resize(520, 420)

        layout = QVBoxLayout(self)
        explanation = QLabel("Choose one or more cameras. Each camera will download in its own background thread.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self._camera_list = QListWidget()
        self._camera_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for camera in cameras:
            details = ", ".join(value for value in (camera.state, camera.model) if value)
            text = f"{camera.name} — {details}" if details else camera.name
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, camera.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            state = Qt.CheckState.Checked if camera.id in selected_ids else Qt.CheckState.Unchecked
            item.setCheckState(state)
            item.setToolTip(f"Camera ID: {camera.id}")
            self._camera_list.addItem(item)
        layout.addWidget(self._camera_list)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        select_all = buttons.addButton("Select All", QDialogButtonBox.ButtonRole.ActionRole)
        clear_all = buttons.addButton("Clear", QDialogButtonBox.ButtonRole.ActionRole)
        select_all.clicked.connect(partial(self._set_all_checked, checked=True))
        clear_all.clicked.connect(partial(self._set_all_checked, checked=False))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all_checked(self, *, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for index in range(self._camera_list.count()):
            item = self._camera_list.item(index)
            if item is not None:
                item.setCheckState(state)

    def selected_cameras(self) -> list[CameraInfo]:
        selected_ids: set[str] = set()
        for index in range(self._camera_list.count()):
            item = self._camera_list.item(index)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            camera_id = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(camera_id, str):
                selected_ids.add(camera_id)
        return [camera for camera in self._cameras if camera.id in selected_ids]


class _DailyScheduleDialog(QDialog):
    def __init__(self, cameras: list[CameraInfo], initial_directory: Path, parent: QWidget | None) -> None:
        super().__init__(parent)
        self._cameras = cameras
        self.setWindowTitle("Daily Automatic Timelapses")
        self.resize(560, 480)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Select the cameras and destination for automatic 24-hour timelapses. "
            "The latest completed day is exported now, then each new day is exported while this app stays open."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self._camera_list = QListWidget()
        self._camera_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for camera in cameras:
            item = QListWidgetItem(camera.name)
            item.setData(Qt.ItemDataRole.UserRole, camera.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self._camera_list.addItem(item)
        layout.addWidget(self._camera_list)
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Save to:"))
        self._output_edit = QLineEdit(str(initial_directory))
        self._output_edit.setReadOnly(True)
        output_row.addWidget(self._output_edit, stretch=1)
        choose_button = QPushButton("Choose…")
        choose_button.clicked.connect(self._choose_directory)
        output_row.addWidget(choose_button)
        layout.addLayout(output_row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        select_all = buttons.addButton("Select All", QDialogButtonBox.ButtonRole.ActionRole)
        select_all.clicked.connect(self._select_all)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @Slot()
    def _choose_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose Daily Timelapse Folder", self._output_edit.text())
        if selected:
            self._output_edit.setText(selected)

    @Slot()
    def _select_all(self) -> None:
        for index in range(self._camera_list.count()):
            item = self._camera_list.item(index)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked)

    def selected_cameras(self) -> list[CameraInfo]:
        selected_ids = {
            str(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self._camera_list.count())
            if (item := self._camera_list.item(index)) is not None and item.checkState() == Qt.CheckState.Checked
        }
        return [camera for camera in self._cameras if camera.id in selected_ids]

    def output_directory(self) -> Path:
        return Path(self._output_edit.text()).expanduser()

    def accept(self) -> None:
        """Validate the daily schedule before closing the dialog."""
        if not self.selected_cameras():
            QMessageBox.warning(self, "No Cameras Selected", "Select at least one camera for the daily job.")
            return
        output = self.output_directory()
        if output.exists() and not output.is_dir():
            QMessageBox.warning(self, "Invalid Output Folder", "The selected output location is not a folder.")
            return
        super().accept()


class _CameraLoader(QThread):
    cameras_loaded: ClassVar[Signal] = Signal(object)
    load_failed: ClassVar[Signal] = Signal(str)

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[list[CameraInfo]] | None = None
        self._cancel_requested = False

    def run(self) -> None:
        """Load cameras inside this thread's private asyncio event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(list_available_cameras(self._config))
        with self._lock:
            self._loop = loop
            self._task = task
            cancel_requested = self._cancel_requested
        if cancel_requested:
            task.cancel()
        try:
            cameras = loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.load_failed.emit(_exception_text(exc))
        else:
            self.cameras_loaded.emit(cameras)
        finally:
            with self._lock:
                self._loop = None
                self._task = None
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            asyncio.set_event_loop(None)

    def cancel(self) -> None:
        """Request cancellation from the GUI thread."""
        with self._lock:
            if self._cancel_requested:
                return
            self._cancel_requested = True
            self.requestInterruption()
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)


class _DownloadWorker(QThread):
    progress_changed: ClassVar[Signal] = Signal(object)
    download_succeeded: ClassVar[Signal] = Signal(str)
    download_failed: ClassVar[Signal] = Signal(str)
    download_cancelled: ClassVar[Signal] = Signal()

    def __init__(self, config: Config, camera: CameraInfo, output: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._camera = camera
        self._output = output
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._cancel_requested = False

    def run(self) -> None:
        """Download one camera inside this thread's private asyncio event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(export_timelapse(self._config, self._camera, self._output, self._report_progress))
        with self._lock:
            self._loop = loop
            self._task = task
            cancel_requested = self._cancel_requested
        if cancel_requested:
            task.cancel()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            self.download_cancelled.emit()
        except Exception as exc:
            self.download_failed.emit(_exception_text(exc))
        else:
            self.download_succeeded.emit(str(self._output))
        finally:
            with self._lock:
                self._loop = None
                self._task = None
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            asyncio.set_event_loop(None)

    @property
    def config(self) -> Config:
        return self._config

    def _report_progress(self, progress: DownloadProgress) -> None:
        self.progress_changed.emit(progress)

    def cancel(self) -> None:
        """Request cancellation from the GUI thread."""
        with self._lock:
            if self._cancel_requested:
                return
            self._cancel_requested = True
            self.requestInterruption()
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)


class _MainWindow(QMainWindow):
    def __init__(
        self,
        profile_state: _ProfileState | _ConnectionSettings,
        profile_store: _ProfileStore | None = None,
    ) -> None:
        super().__init__()
        self._preferences = QSettings("TimeLapse", "UniFi Protect Timelapse")
        if isinstance(profile_state, _ConnectionSettings):
            profile = _ConnectionProfile("test-profile", profile_state.instance_url, profile_state).normalized()
            profile_state = _ProfileState((profile,), profile.profile_id)
        self._profiles = list(profile_state.profiles)
        self._selected_profile_id = profile_state.selected_profile_id
        selected_profile = profile_state.selected_profile or (self._profiles[0] if self._profiles else None)
        if selected_profile is None:
            message = "the main window requires at least one connection profile"
            raise ValueError(message)
        self._selected_profile_id = selected_profile.profile_id
        self._settings = selected_profile.settings
        self._profile_store = profile_store
        self._cameras: list[CameraInfo] = []
        self._selected_cameras: list[CameraInfo] = []
        self._camera_loader: _CameraLoader | None = None
        self._open_camera_dialog_after_load = False
        self._open_daily_dialog_after_load = False
        self._workers: dict[_DownloadWorker, _DownloadEntry] = {}
        self._entries: list[_DownloadEntry] = []
        self._reserved_paths: set[str] = set()
        self._next_job_number = 1
        self._daily_schedule: _DailySchedule | None = None
        self._adjusting_full_day = False
        self._closing = False
        self._log_handler_attached = False
        self.setWindowTitle("UniFi Protect Timelapse")
        self.resize(1400, 680)
        self.setMinimumSize(1250, 560)
        self._install_styles()
        self._build_menu()
        self._build_interface()
        self._speed_timer = QTimer(self)
        self._speed_timer.setInterval(1000)
        self._speed_timer.timeout.connect(self._clear_stalled_speeds)
        self._speed_timer.start()
        self._daily_timer = QTimer(self)
        self._daily_timer.setInterval(60_000)
        self._daily_timer.timeout.connect(self._run_daily_schedule_if_due)
        self._daily_timer.start()
        self._install_log_handler()
        self._update_profile_controls()
        self._update_camera_summary()
        self._update_activity_indicator()
        _LOGGER.info("Application ready")

    def _build_menu(self) -> None:
        application_menu = self.menuBar().addMenu("&File")
        new_profile_action = QAction("New Connection Profile…", self)
        new_profile_action.triggered.connect(self._new_profile)
        application_menu.addAction(new_profile_action)
        edit_profile_action = QAction("Edit Connection Profile…", self)
        edit_profile_action.triggered.connect(self._edit_connection)
        application_menu.addAction(edit_profile_action)
        application_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        application_menu.addAction(quit_action)

    def _build_interface(self) -> None:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._connection_group())
        layout.addWidget(self._options_group())
        layout.addWidget(self._downloads_group())
        self.setCentralWidget(container)
        self.statusBar().showMessage("Ready")
        self._activity_widget = QWidget()
        activity_layout = QHBoxLayout(self._activity_widget)
        activity_layout.setContentsMargins(4, 0, 4, 0)
        activity_layout.setSpacing(6)
        activity_label = QLabel("Working")
        activity_layout.addWidget(activity_label)
        self._activity_bar = QProgressBar()
        self._activity_bar.setRange(0, 0)
        self._activity_bar.setTextVisible(False)
        self._activity_bar.setFixedSize(80, 14)
        self._activity_bar.setToolTip("Background work is active. This animation stops if the interface freezes.")
        activity_layout.addWidget(self._activity_bar)
        self.statusBar().addPermanentWidget(self._activity_widget)
        self._logs_button = QPushButton("Logs")
        self._logs_button.setToolTip("Open application logs in a separate window.")
        self._logs_button.clicked.connect(self._show_logs)
        self.statusBar().addPermanentWidget(self._logs_button)

        self._logs_window = _LogsWindow()
        self._logs_window.clear_button.clicked.connect(self._clear_logs)

    def _install_styles(self) -> None:
        self.setStyleSheet(
            "QPushButton[primary='true'] { background: palette(highlight); color: palette(highlighted-text); "
            "border: 1px solid palette(highlight); border-radius: 5px; padding: 5px 12px; }"
            "QPushButton[primary='true']:disabled { background: palette(midlight); color: palette(disabled, text); "
            "border-color: palette(mid); }"
            "QGroupBox { border: 1px solid palette(mid); border-radius: 10px; margin-top: 9px; "
            "padding: 12px 10px 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; font-weight: 600; }"
        )

    def _install_log_handler(self) -> None:
        self._log_emitter = _LogEmitter(self)
        self._log_emitter.message_ready.connect(self._append_log)
        self._log_handler = _QtLogHandler(self._log_emitter)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
        )
        logging.getLogger().addHandler(self._log_handler)
        logging.getLogger("timelapse").setLevel(logging.INFO)
        self._log_handler_attached = True

    def _remove_log_handler(self) -> None:
        if self._log_handler_attached:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler_attached = False

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self._logs_window.output.appendPlainText(message)

    @Slot()
    def _clear_logs(self) -> None:
        self._logs_window.output.clear()

    @Slot()
    def _show_logs(self) -> None:
        self._logs_window.show_and_activate()

    def _update_activity_indicator(self) -> None:
        self._activity_widget.setVisible(self._camera_loader is not None or bool(self._workers))

    def _connection_group(self) -> QGroupBox:
        group = QGroupBox("Connection")
        group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(group)
        controls = QHBoxLayout()
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(220)
        self._profile_combo.currentIndexChanged.connect(self._profile_selected)
        controls.addWidget(self._profile_combo)
        controls.addStretch(1)
        new_button = QPushButton("New")
        new_button.clicked.connect(self._new_profile)
        controls.addWidget(new_button)
        self._edit_profile_button = QPushButton("Edit")
        self._edit_profile_button.clicked.connect(self._edit_connection)
        controls.addWidget(self._edit_profile_button)
        layout.addLayout(controls)
        self._connection_label = QLabel()
        self._connection_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._connection_label)
        return group

    def _options_group(self) -> QGroupBox:  # noqa: PLR0915 - constructs one cohesive form
        group = QGroupBox("New Timelapse")
        group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        outer = QHBoxLayout(group)
        date_form = QFormLayout()

        now = QDateTime.currentDateTime()
        self._start_edit = self._date_time_editor(now.addDays(-1))
        self._end_edit = self._date_time_editor(now)
        self._start_edit.dateTimeChanged.connect(self._full_day_start_changed)
        self._end_edit.dateTimeChanged.connect(self._full_day_end_changed)
        date_form.addRow("Start:", self._start_edit)
        date_form.addRow("End:", self._end_edit)

        self._full_day_checkbox = QCheckBox("24-hour timelapse")
        self._full_day_checkbox.setToolTip("Use date-only controls and export exactly one complete local calendar day.")
        self._full_day_checkbox.toggled.connect(self._full_day_toggled)
        date_form.addRow("", self._full_day_checkbox)

        self._speed_combo = QComboBox()
        self._speed_combo.addItems(list(SPEED_TO_FPS))
        self._speed_combo.setCurrentText("600x")
        self._speed_combo.setToolTip("Higher values create a faster timelapse.")
        date_form.addRow("Speed:", self._speed_combo)
        outer.addLayout(date_form)

        details_form = QFormLayout()

        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        self._output_edit = QLineEdit(self._saved_output_directory())
        self._output_edit.setReadOnly(True)
        output_layout.addWidget(self._output_edit, stretch=1)
        browse_button = QPushButton("Choose…")
        browse_button.clicked.connect(self._choose_output_directory)
        output_layout.addWidget(browse_button)
        details_form.addRow("Save to:", output_row)

        camera_row = QWidget()
        camera_layout = QHBoxLayout(camera_row)
        camera_layout.setContentsMargins(0, 0, 0, 0)
        self._camera_summary = QLabel()
        self._camera_summary.setMinimumWidth(150)
        camera_layout.addWidget(self._camera_summary)
        self._select_cameras_button = QPushButton("Select…")
        self._select_cameras_button.clicked.connect(self._request_camera_selection)
        camera_layout.addWidget(self._select_cameras_button)
        self._refresh_cameras_button = QPushButton("↻")
        self._refresh_cameras_button.setToolTip("Refresh cameras")
        self._refresh_cameras_button.clicked.connect(partial(self._load_cameras, open_dialog=True))
        camera_layout.addWidget(self._refresh_cameras_button)
        camera_layout.addStretch(1)
        details_form.addRow("Cameras:", camera_row)

        self._daily_checkbox = QCheckBox("Daily automatic timelapses")
        self._daily_checkbox.setToolTip("Export each completed local day while this program remains open.")
        self._daily_checkbox.toggled.connect(self._daily_toggled)
        details_form.addRow("", self._daily_checkbox)

        self._start_button = QPushButton("Start Downloads")
        self._start_button.setProperty("primary", "true")
        self._start_button.setMaximumWidth(180)
        self._start_button.setEnabled(False)
        self._start_button.clicked.connect(self._queue_downloads)
        details_form.addRow("", self._start_button)
        outer.addLayout(details_form, stretch=1)
        return group

    @staticmethod
    def _date_time_editor(value: QDateTime) -> QDateTimeEdit:
        editor = QDateTimeEdit(value)
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("MMM d, yyyy h:mm AP")
        editor.setMinimumDateTime(_MINIMUM_DATE)
        editor.setToolTip("Type a date and time or use the calendar button to choose a date.")
        return editor

    @Slot(bool)
    def _full_day_toggled(self, enabled: bool) -> None:  # noqa: FBT001 - Qt signal signature
        display_format = "MMM d, yyyy" if enabled else "MMM d, yyyy h:mm AP"
        tooltip = "Choose a date." if enabled else "Type a date and time or use the calendar button to choose a date."
        self._start_edit.setDisplayFormat(display_format)
        self._end_edit.setDisplayFormat(display_format)
        self._start_edit.setToolTip(tooltip)
        self._end_edit.setToolTip(tooltip)
        if enabled:
            self._set_full_day_from_start(self._start_edit.dateTime())

    @Slot(QDateTime)
    def _full_day_start_changed(self, value: QDateTime) -> None:
        if self._full_day_checkbox.isChecked() and not self._adjusting_full_day:
            self._set_full_day_from_start(value)

    @Slot(QDateTime)
    def _full_day_end_changed(self, value: QDateTime) -> None:
        if not self._full_day_checkbox.isChecked() or self._adjusting_full_day:
            return
        self._adjusting_full_day = True
        try:
            end = QDateTime(value.date(), QTime(0, 0))
            self._end_edit.setDateTime(end)
            self._start_edit.setDateTime(end.addDays(-1))
        finally:
            self._adjusting_full_day = False

    def _set_full_day_from_start(self, value: QDateTime) -> None:
        self._adjusting_full_day = True
        try:
            start = QDateTime(value.date(), QTime(0, 0))
            self._start_edit.setDateTime(start)
            self._end_edit.setDateTime(start.addDays(1))
        finally:
            self._adjusting_full_day = False

    def _downloads_group(self) -> QGroupBox:
        group = QGroupBox("Jobs")
        layout = QVBoxLayout(group)
        controls = QHBoxLayout()
        self._clear_all_button = QPushButton("Clear All")
        self._clear_all_button.setProperty("primary", "true")
        self._clear_all_button.clicked.connect(self._clear_finished_jobs)
        controls.addWidget(self._clear_all_button)
        self._cancel_all_button = QPushButton("Cancel All")
        self._cancel_all_button.setProperty("primary", "true")
        self._cancel_all_button.clicked.connect(self._cancel_all_jobs)
        controls.addWidget(self._cancel_all_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self._job_tabs = QTabWidget()
        downloads_page, self._empty_downloads, self._downloads = self._job_list_page(
            "No downloads yet\nSelect cameras and start a timelapse to track it here."
        )
        automations_page, self._empty_daily_automations, self._daily_automations = self._job_list_page(
            "No daily automations\nEnable daily automatic timelapses to track the schedule here."
        )
        self._job_tabs.addTab(downloads_page, "Downloads")
        self._job_tabs.addTab(automations_page, "Daily Automations")
        self._job_tabs.currentChanged.connect(self._job_tab_changed)
        layout.addWidget(self._job_tabs)

        self._downloads_group_widget = group
        self._sync_download_view()
        self._update_bulk_buttons()
        return group

    def _job_list_page(self, empty_message: str) -> tuple[QWidget, QLabel, QTableWidget]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        empty_label = QLabel(empty_message)
        empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_label.setMinimumHeight(58)
        layout.addWidget(empty_label)

        table = QTableWidget(0, len(_TABLE_HEADERS))
        table.setMinimumHeight(220)
        table.setHorizontalHeaderLabels(_TABLE_HEADERS)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(False)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(partial(self._show_job_context_menu, table))
        table.cellDoubleClicked.connect(partial(self._open_completed_video, table))
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COLUMN_PROGRESS, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COLUMN_OUTPUT, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table)
        return page, empty_label, table

    @Slot(int)
    def _job_tab_changed(self, _index: int) -> None:
        self._sync_download_view()
        self._update_bulk_buttons()

    def _sync_download_view(self) -> None:
        has_downloads = any(not entry.daily_schedule for entry in self._entries)
        has_daily_automations = any(entry.daily_schedule for entry in self._entries)
        self._empty_downloads.setVisible(not has_downloads)
        self._downloads.setVisible(has_downloads)
        self._empty_daily_automations.setVisible(not has_daily_automations)
        self._daily_automations.setVisible(has_daily_automations)
        active_tab_has_jobs = has_daily_automations if self._shows_daily_automations else has_downloads
        self._downloads_group_widget.setMaximumHeight(16_777_215 if active_tab_has_jobs else 180)

    @property
    def _shows_daily_automations(self) -> bool:
        return self._job_tabs.currentIndex() == 1

    def _visible_entries(self) -> list[_DownloadEntry]:
        return [entry for entry in self._entries if entry.daily_schedule == self._shows_daily_automations]

    def _saved_output_directory(self) -> str:
        saved = self._preferences.value("output_directory")
        return saved if isinstance(saved, str) and saved else str(_default_output_directory())

    @Slot()
    def _choose_output_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose Output Folder", self._output_edit.text())
        if selected:
            self._output_edit.setText(selected)
            self._preferences.setValue("output_directory", selected)

    @Slot()
    def _edit_connection(self) -> None:
        if self._camera_loader is not None:
            QMessageBox.information(self, "Loading Cameras", "Wait for the current camera refresh to finish.")
            return
        profile = self._current_profile()
        dialog = _CredentialsDialog(profile, first_run=False, new_profile=False, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.selected_profile()
        previous_profiles = self._profiles
        self._profiles = [updated if item.profile_id == updated.profile_id else item for item in self._profiles]
        self._selected_profile_id = updated.profile_id
        if not self._save_profiles():
            self._profiles = previous_profiles
            return
        self._settings = updated.settings
        self._cameras.clear()
        self._selected_cameras.clear()
        self._update_profile_controls()
        self._update_camera_summary()
        self.statusBar().showMessage(f"Saved {updated.display_name}", 5000)
        _LOGGER.info("Saved connection profile: %s", updated.display_name)

    @Slot()
    def _new_profile(self) -> None:
        if self._camera_loader is not None:
            QMessageBox.information(self, "Loading Cameras", "Wait for the current camera refresh to finish.")
            return
        blank = _ConnectionSettings(
            instance_url="",
            token="",
            username="",
            password="",
            verify_ssl=True,
            request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            max_download_mib=DEFAULT_MAX_DOWNLOAD_MIB,
        )
        profile = _ConnectionProfile(str(uuid.uuid4()), "", blank)
        dialog = _CredentialsDialog(profile, first_run=False, new_profile=True, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        created = dialog.selected_profile()
        self._profiles.append(created)
        self._selected_profile_id = created.profile_id
        if not self._save_profiles():
            self._profiles.remove(created)
            return
        self._settings = created.settings
        self._cameras.clear()
        self._selected_cameras.clear()
        self._update_profile_controls()
        self._update_camera_summary()
        self.statusBar().showMessage(f"Saved {created.display_name}", 5000)
        _LOGGER.info("Saved connection profile: %s", created.display_name)

    @Slot(int)
    def _profile_selected(self, index: int) -> None:
        profile_id = self._profile_combo.itemData(index)
        if not isinstance(profile_id, str) or profile_id == self._selected_profile_id:
            return
        if self._camera_loader is not None:
            QMessageBox.information(self, "Loading Cameras", "Wait for the current camera refresh to finish.")
            self._update_profile_controls()
            return
        profile = next((item for item in self._profiles if item.profile_id == profile_id), None)
        if profile is None:
            return
        previous_id = self._selected_profile_id
        self._selected_profile_id = profile_id
        if not self._save_profiles():
            self._selected_profile_id = previous_id
            self._update_profile_controls()
            return
        self._settings = profile.settings
        self._cameras.clear()
        self._selected_cameras.clear()
        self._update_profile_controls()
        self._update_camera_summary()
        self.statusBar().showMessage(f"Selected {profile.display_name}", 5000)
        _LOGGER.info("Selected connection profile: %s", profile.display_name)

    def _current_profile(self) -> _ConnectionProfile:
        profile = next((item for item in self._profiles if item.profile_id == self._selected_profile_id), None)
        if profile is None:
            message = "the selected connection profile no longer exists"
            raise RuntimeError(message)
        return profile

    def _save_profiles(self) -> bool:
        if self._profile_store is None:
            return True
        try:
            self._profile_store.save(_ProfileState(tuple(self._profiles), self._selected_profile_id))
        except _ProfileStoreError as exc:
            QMessageBox.critical(self, "Could Not Save Profiles", str(exc))
            return False
        return True

    def _update_profile_controls(self) -> None:
        previous_state = self._profile_combo.blockSignals(True)  # noqa: FBT003
        self._profile_combo.clear()
        selected_index = 0
        for index, profile in enumerate(self._profiles):
            self._profile_combo.addItem(profile.display_name, profile.profile_id)
            if profile.profile_id == self._selected_profile_id:
                selected_index = index
        self._profile_combo.setCurrentIndex(selected_index)
        self._profile_combo.blockSignals(previous_state)
        self._edit_profile_button.setEnabled(bool(self._profiles))
        self._connection_label.setText(self._settings.instance_url)
        self._connection_label.setToolTip(_URL_TOOLTIP)

    @Slot()
    def _request_camera_selection(self) -> None:
        if self._cameras:
            self._show_camera_selection()
        else:
            self._load_cameras(open_dialog=True)

    def _load_cameras(self, *, open_dialog: bool) -> None:
        if self._camera_loader is not None:
            return
        now = datetime.now().astimezone()
        config = self._settings.make_config(now, now + timedelta(seconds=1), "600x")
        loader = _CameraLoader(config, self)
        self._camera_loader = loader
        self._open_camera_dialog_after_load = open_dialog
        loader.cameras_loaded.connect(self._cameras_loaded)
        loader.load_failed.connect(self._camera_load_failed)
        loader.finished.connect(partial(self._camera_loader_finished, loader))
        self._select_cameras_button.setEnabled(False)
        self._refresh_cameras_button.setEnabled(False)
        self.statusBar().showMessage("Loading cameras…")
        self._update_activity_indicator()
        _LOGGER.info("Loading cameras")
        loader.start()

    @Slot(object)
    def _cameras_loaded(self, payload: object) -> None:
        if self._closing:
            return
        if not isinstance(payload, list) or not all(isinstance(camera, CameraInfo) for camera in payload):
            self._camera_load_failed("Protect returned an unexpected camera list.")
            return
        self._cameras = payload
        selected_ids = {camera.id for camera in self._selected_cameras}
        self._selected_cameras = [camera for camera in self._cameras if camera.id in selected_ids]
        if not self._cameras:
            self._open_daily_dialog_after_load = False
            self._set_daily_checkbox(checked=False)
            QMessageBox.information(self, "No Cameras", "No cameras were returned by UniFi Protect.")
            return
        self.statusBar().showMessage(f"Loaded {len(self._cameras)} cameras", 5000)
        _LOGGER.info("Loaded %d cameras", len(self._cameras))
        if self._open_camera_dialog_after_load:
            self._show_camera_selection()
        elif self._open_daily_dialog_after_load:
            self._open_daily_dialog_after_load = False
            self._show_daily_schedule_dialog()

    @Slot(str)
    def _camera_load_failed(self, message: str) -> None:
        if not self._closing:
            self._open_daily_dialog_after_load = False
            self._set_daily_checkbox(checked=False)
            QMessageBox.critical(self, "Could Not Load Cameras", message)
            self.statusBar().showMessage("Camera loading failed", 5000)
            _LOGGER.error("Camera loading failed: %s", message)

    def _camera_loader_finished(self, loader: _CameraLoader) -> None:
        if self._camera_loader is loader:
            self._camera_loader = None
        loader.deleteLater()
        self._select_cameras_button.setEnabled(True)
        self._refresh_cameras_button.setEnabled(True)
        self._update_activity_indicator()
        self._finish_close_if_ready()

    def _show_camera_selection(self) -> None:
        selected_ids = {camera.id for camera in self._selected_cameras}
        dialog = _CameraSelectionDialog(self._cameras, selected_ids, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._selected_cameras = dialog.selected_cameras()
            self._update_camera_summary()
            _LOGGER.info("Selected %d cameras", len(self._selected_cameras))

    def _update_camera_summary(self) -> None:
        count = len(self._selected_cameras)
        if count == 0:
            summary = "No cameras selected"
        elif count == 1:
            summary = self._selected_cameras[0].name
        else:
            summary = f"{count} cameras selected"
        self._camera_summary.setText(summary)
        self._camera_summary.setToolTip(", ".join(camera.name for camera in self._selected_cameras))
        self._start_button.setEnabled(count > 0 and not self._closing)

    @Slot(bool)
    def _daily_toggled(self, enabled: bool) -> None:  # noqa: FBT001 - Qt signal signature
        if not enabled:
            if self._daily_schedule is not None:
                self._stop_daily_schedule()
            return
        if self._daily_schedule is not None:
            return
        if not self._cameras:
            self._open_camera_dialog_after_load = False
            self._open_daily_dialog_after_load = True
            self._load_cameras(open_dialog=False)
            return
        self._show_daily_schedule_dialog()

    def _show_daily_schedule_dialog(self) -> None:
        dialog = _DailyScheduleDialog(self._cameras, Path(self._output_edit.text()).expanduser(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._set_daily_checkbox(checked=False)
            return
        output_directory = dialog.output_directory()
        cameras = tuple(dialog.selected_cameras())
        try:
            output_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Could Not Create Output Folder", _exception_text(exc))
            self._set_daily_checkbox(checked=False)
            return
        entry = self._add_daily_schedule_row(cameras, output_directory)
        self._daily_schedule = _DailySchedule(
            cameras=cameras,
            output_directory=output_directory,
            speed=self._speed_combo.currentText(),
            entry=entry,
        )
        self.statusBar().showMessage(f"Scheduled daily timelapses for {len(cameras)} cameras", 5000)
        _LOGGER.info("Scheduled daily timelapses for %d cameras in %s", len(cameras), output_directory)
        self._run_daily_schedule_if_due()

    def _add_daily_schedule_row(
        self,
        cameras: tuple[CameraInfo, ...],
        output_directory: Path,
    ) -> _DownloadEntry:
        now = datetime.now().astimezone()
        config = self._settings.make_config(now, now + timedelta(seconds=1), self._speed_combo.currentText())
        camera = CameraInfo(
            id="daily-schedule",
            name=f"{len(cameras)} cameras" if len(cameras) != 1 else cameras[0].name,
            state=None,
            model=None,
        )
        row = self._daily_automations.rowCount()
        self._daily_automations.insertRow(row)
        values = {
            _COLUMN_JOB: str(self._next_job_number),
            _COLUMN_CAMERA: camera.name,
            _COLUMN_TIME_RANGE: "Next completed day",
            _COLUMN_STATUS: "Scheduled daily",
            _COLUMN_DOWNLOADED: "—",
            _COLUMN_EXPECTED: "—",
            _COLUMN_SPEED: "—",
            _COLUMN_OUTPUT: output_directory.name or str(output_directory),
        }
        self._next_job_number += 1
        for column, value in values.items():
            item = QTableWidgetItem(value)
            if column == _COLUMN_OUTPUT:
                item.setToolTip(str(output_directory))
            self._daily_automations.setItem(row, column, item)
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 1)
        progress_bar.setValue(0)
        progress_bar.setFormat("Daily")
        self._daily_automations.setCellWidget(row, _COLUMN_PROGRESS, progress_bar)
        stop_button = QPushButton("Stop")
        stop_button.clicked.connect(self._stop_daily_schedule)
        self._daily_automations.setCellWidget(row, _COLUMN_ACTION, stop_button)
        entry = _DownloadEntry(
            row=row,
            job_number=self._next_job_number - 1,
            output=output_directory,
            camera=camera,
            config=config,
            worker=None,
            progress_bar=progress_bar,
            action_button=stop_button,
            daily_schedule=True,
        )
        self._entries.append(entry)
        self._sync_download_view()
        self._update_bulk_buttons()
        return entry

    @Slot()
    def _stop_daily_schedule(self) -> None:
        schedule = self._daily_schedule
        if schedule is None:
            return
        self._daily_schedule = None
        schedule.entry.terminal = True
        self._set_entry_text(schedule.entry, _COLUMN_STATUS, "Stopped")
        self._set_action_button(schedule.entry, "Remove", partial(self._remove_entry, schedule.entry))
        self._set_daily_checkbox(checked=False)
        self._update_bulk_buttons()
        self.statusBar().showMessage("Stopped daily automatic timelapses", 5000)
        _LOGGER.info("Stopped daily automatic timelapses")

    def _set_daily_checkbox(self, *, checked: bool) -> None:
        self._daily_checkbox.blockSignals(True)  # noqa: FBT003 - Qt API
        self._daily_checkbox.setChecked(checked)
        self._daily_checkbox.blockSignals(False)  # noqa: FBT003 - Qt API

    @Slot()
    def _run_daily_schedule_if_due(self) -> None:
        schedule = self._daily_schedule
        if schedule is None:
            return
        latest_day = latest_complete_local_day()
        day = schedule.last_run_day + timedelta(days=1) if schedule.last_run_day is not None else latest_day
        if day > latest_day:
            return
        while day <= latest_day:
            schedule.last_run_day = day
            config = config_for_local_day(schedule.entry.config, day)
            job_number = self._next_job_number
            self._next_job_number += 1
            for camera in schedule.cameras:
                preferred = daily_output_path(config, camera, schedule.output_directory)
                output = self._reserve_output_path(preferred)
                worker = _DownloadWorker(config, camera, output, self)
                entry = self._add_download_row(job_number, camera, output, worker, config=config)
                self._start_download_worker(entry, worker)
                _LOGGER.info("Started daily camera download: %s -> %s", camera.name, output)
            self.statusBar().showMessage(f"Started daily job {job_number} for {day.isoformat()}")
            day += timedelta(days=1)
        self._update_activity_indicator()

    @Slot()
    def _queue_downloads(self) -> None:
        if not self._selected_cameras:
            return
        start = datetime.fromtimestamp(self._start_edit.dateTime().toSecsSinceEpoch()).astimezone()
        end = datetime.fromtimestamp(self._end_edit.dateTime().toSecsSinceEpoch()).astimezone()
        if end <= start:
            QMessageBox.warning(self, "Invalid Date Range", "The end date and time must be after the start.")
            return
        output_directory = Path(self._output_edit.text()).expanduser()
        if output_directory.exists() and not output_directory.is_dir():
            QMessageBox.warning(self, "Invalid Output Folder", "The selected output location is not a folder.")
            return
        speed = self._speed_combo.currentText()
        config = self._settings.make_config(start, end, speed)
        job_number = self._next_job_number
        self._next_job_number += 1
        for camera in self._selected_cameras:
            preferred = output_directory / default_output_path(config, camera).name
            output = self._reserve_output_path(preferred)
            worker = _DownloadWorker(config, camera, output, self)
            entry = self._add_download_row(job_number, camera, output, worker, config=config)
            self._start_download_worker(entry, worker)
            _LOGGER.info("Started camera download: %s -> %s", camera.name, output)
        self._update_activity_indicator()
        self.statusBar().showMessage(f"Started job {job_number} with {len(self._selected_cameras)} downloads")
        _LOGGER.info("Started job %d with %d downloads", job_number, len(self._selected_cameras))

    def _reserve_output_path(self, preferred: Path) -> Path:
        candidate = preferred.resolve()
        counter = 2
        while candidate.exists() or self._reservation_key(candidate) in self._reserved_paths:
            candidate = preferred.with_name(f"{preferred.stem}_{counter}{preferred.suffix}").resolve()
            counter += 1
        self._reserved_paths.add(self._reservation_key(candidate))
        return candidate

    @staticmethod
    def _reservation_key(path: Path) -> str:
        return str(path).casefold()

    def _add_download_row(
        self,
        job_number: int,
        camera: CameraInfo,
        output: Path,
        worker: _DownloadWorker,
        *,
        config: Config | None = None,
    ) -> _DownloadEntry:
        row = self._downloads.rowCount()
        self._downloads.insertRow(row)
        values = {
            _COLUMN_JOB: str(job_number),
            _COLUMN_CAMERA: camera.name,
            _COLUMN_TIME_RANGE: _format_time_range(config or worker.config),
            _COLUMN_STATUS: "Preparing export…",
            _COLUMN_DOWNLOADED: "0 bytes",
            _COLUMN_EXPECTED: "Unknown",
            _COLUMN_SPEED: "—",
            _COLUMN_OUTPUT: output.name,
        }
        for column, value in values.items():
            item = QTableWidgetItem(value)
            if column == _COLUMN_OUTPUT:
                item.setToolTip(str(output))
            self._downloads.setItem(row, column, item)

        progress_bar = QProgressBar()
        progress_bar.setRange(0, 0)
        progress_bar.setMinimumWidth(150)
        self._downloads.setCellWidget(row, _COLUMN_PROGRESS, progress_bar)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(partial(self._cancel_download, worker))
        self._downloads.setCellWidget(row, _COLUMN_ACTION, cancel_button)
        entry = _DownloadEntry(
            row=row,
            job_number=job_number,
            output=output,
            camera=camera,
            config=config or worker.config,
            worker=worker,
            progress_bar=progress_bar,
            action_button=cancel_button,
        )
        self._entries.append(entry)
        self._sync_download_view()
        self._update_bulk_buttons()
        return entry

    def _start_download_worker(self, entry: _DownloadEntry, worker: _DownloadWorker) -> None:
        entry.worker = worker
        self._workers[worker] = entry
        worker.progress_changed.connect(partial(self._download_progress, entry))
        worker.download_succeeded.connect(partial(self._download_succeeded, entry))
        worker.download_failed.connect(partial(self._download_failed, entry))
        worker.download_cancelled.connect(partial(self._download_cancelled, entry))
        worker.finished.connect(partial(self._download_worker_finished, worker))
        worker.start()
        self._update_activity_indicator()
        self._update_bulk_buttons()

    def _cancel_download(self, worker: _DownloadWorker) -> None:
        entry = self._workers.get(worker)
        if entry is None or entry.terminal or entry.cancelling:
            return
        entry.cancelling = True
        entry.action_button.setEnabled(False)
        self._set_entry_text(entry, _COLUMN_STATUS, "Cancelling…")
        _LOGGER.info("Cancelling camera download: %s", entry.camera_name)
        worker.cancel()
        self._update_bulk_buttons()

    @Slot()
    def _cancel_all_jobs(self) -> None:
        if self._shows_daily_automations:
            if self._daily_schedule is not None:
                self._stop_daily_schedule()
            return
        cancellable = [worker for worker, entry in self._workers.items() if not entry.terminal and not entry.cancelling]
        for worker in cancellable:
            self._cancel_download(worker)
        if cancellable:
            self.statusBar().showMessage(f"Cancelling {len(cancellable)} downloads…")

    @Slot()
    def _clear_finished_jobs(self) -> None:
        removable = [entry for entry in self._visible_entries() if self._is_removable(entry)]
        for entry in sorted(removable, key=lambda item: item.row, reverse=True):
            self._remove_entry(entry)
        if removable:
            description = "stopped daily automations" if self._shows_daily_automations else "finished downloads"
            self.statusBar().showMessage(f"Cleared {len(removable)} {description}", 5000)
            _LOGGER.info("Cleared %d %s", len(removable), description)

    def _update_bulk_buttons(self) -> None:
        self._clear_all_button.setEnabled(any(self._is_removable(entry) for entry in self._visible_entries()))
        self._cancel_all_button.setText("Stop All" if self._shows_daily_automations else "Cancel All")
        has_cancellable_jobs = (
            self._daily_schedule is not None
            if self._shows_daily_automations
            else any(not entry.terminal and not entry.cancelling for entry in self._workers.values())
        )
        self._cancel_all_button.setEnabled(has_cancellable_jobs)

    def _is_removable(self, entry: _DownloadEntry) -> bool:
        return entry.terminal and entry.worker not in self._workers

    def _show_job_context_menu(self, table: QTableWidget, position: QPoint) -> None:
        index = table.indexAt(position)
        entry = self._entry_for_row(table, index.row())
        if entry is None:
            return
        menu = QMenu(self)
        if entry.daily_schedule and not entry.terminal:
            stop_action = menu.addAction("Stop Daily Job")
            stop_action.triggered.connect(self._stop_daily_schedule)
        elif entry.daily_schedule:
            pass
        elif entry.completed:
            open_action = menu.addAction("Open Video")
            open_action.triggered.connect(partial(self._open_entry_video, entry))
            show_action = menu.addAction("Show Folder")
            show_action.triggered.connect(partial(self._show_output_folder, entry.output))
        elif entry.terminal:
            restart_action = menu.addAction("Restart")
            restart_action.setEnabled(entry.worker not in self._workers)
            restart_action.triggered.connect(partial(self._restart_download, entry))
        else:
            cancel_action = menu.addAction("Cancel")
            worker = entry.worker
            cancel_action.setEnabled(not entry.cancelling and worker is not None)
            if worker is not None:
                cancel_action.triggered.connect(partial(self._cancel_download, worker))
        menu.addSeparator()
        remove_action = menu.addAction("Remove from List")
        remove_action.setEnabled(self._is_removable(entry))
        remove_action.triggered.connect(partial(self._remove_entry, entry))
        menu.exec(table.viewport().mapToGlobal(position))

    def _open_completed_video(self, table: QTableWidget, row: int, _column: int) -> None:
        entry = self._entry_for_row(table, row)
        if entry is not None and entry.completed:
            self._open_entry_video(entry)

    def _open_entry_video(self, entry: _DownloadEntry) -> None:
        if not entry.output.is_file():
            QMessageBox.warning(self, "Video Not Found", f"The completed video could not be found at {entry.output}.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(entry.output)))

    def _entry_for_row(self, table: QTableWidget, row: int) -> _DownloadEntry | None:
        return next(
            (entry for entry in self._entries if self._table_for_entry(entry) is table and entry.row == row),
            None,
        )

    def _table_for_entry(self, entry: _DownloadEntry) -> QTableWidget:
        return self._daily_automations if entry.daily_schedule else self._downloads

    def _remove_entry(self, entry: _DownloadEntry) -> None:
        if not self._is_removable(entry):
            return
        table = self._table_for_entry(entry)
        removed_row = entry.row
        table.removeRow(removed_row)
        self._entries.remove(entry)
        for remaining in self._entries:
            if remaining.daily_schedule == entry.daily_schedule and remaining.row > removed_row:
                remaining.row -= 1
        self._sync_download_view()
        self._update_bulk_buttons()
        self.statusBar().showMessage(f"Removed {entry.camera_name} from the job list", 5000)

    def _download_progress(self, entry: _DownloadEntry, payload: object) -> None:
        if entry.terminal or not isinstance(payload, DownloadProgress):
            return
        entry.downloaded_bytes = payload.downloaded_bytes
        entry.last_progress_at = monotonic()
        if not entry.cancelling:
            status = "Downloading" if payload.downloaded_bytes else "Preparing export…"
            self._set_entry_text(entry, _COLUMN_STATUS, status)
        self._set_entry_text(entry, _COLUMN_DOWNLOADED, _format_bytes(payload.downloaded_bytes))
        expected = "Unknown" if payload.total_bytes is None else _format_bytes(payload.total_bytes)
        self._set_entry_text(entry, _COLUMN_EXPECTED, expected)
        self._set_entry_text(entry, _COLUMN_SPEED, _format_speed(payload.bytes_per_second))
        if payload.total_bytes:
            fraction = min(payload.downloaded_bytes / payload.total_bytes, 1.0)
            entry.progress_bar.setRange(0, _PROGRESS_SCALE)
            entry.progress_bar.setValue(round(fraction * _PROGRESS_SCALE))
            entry.progress_bar.setFormat(f"{fraction * 100:.1f}%")
        else:
            entry.progress_bar.setRange(0, 0)

    @Slot()
    def _clear_stalled_speeds(self) -> None:
        now = monotonic()
        for entry in self._workers.values():
            if (
                entry.terminal
                or entry.downloaded_bytes == 0
                or entry.last_progress_at is None
                or now - entry.last_progress_at < _STALE_SPEED_SECONDS
            ):
                continue
            self._set_entry_text(entry, _COLUMN_SPEED, "0 bytes/s")

    def _download_succeeded(self, entry: _DownloadEntry, _output_text: str) -> None:
        entry.terminal = True
        entry.completed = True
        self._set_entry_text(entry, _COLUMN_STATUS, "Completed")
        with suppress(OSError):
            entry.downloaded_bytes = entry.output.stat().st_size
        self._set_entry_text(entry, _COLUMN_DOWNLOADED, _format_bytes(entry.downloaded_bytes))
        entry.progress_bar.setRange(0, _PROGRESS_SCALE)
        entry.progress_bar.setValue(_PROGRESS_SCALE)
        entry.progress_bar.setFormat("100%")
        self._set_action_button(entry, "Show", partial(self._show_output_folder, entry.output))
        self._update_bulk_buttons()
        _LOGGER.info("Completed camera download: %s -> %s", entry.camera_name, entry.output)

    def _download_failed(self, entry: _DownloadEntry, message: str) -> None:
        entry.terminal = True
        entry.completed = False
        self._reserved_paths.discard(self._reservation_key(entry.output))
        self._set_entry_text(entry, _COLUMN_STATUS, "Failed", tooltip=message)
        self._set_entry_text(entry, _COLUMN_SPEED, "—")
        self._set_action_button(entry, "Restart", partial(self._restart_download, entry), enabled=False)
        self._update_bulk_buttons()
        _LOGGER.error("Camera download failed for %s: %s", entry.camera_name, message)

    def _download_cancelled(self, entry: _DownloadEntry) -> None:
        entry.terminal = True
        entry.completed = False
        self._reserved_paths.discard(self._reservation_key(entry.output))
        self._set_entry_text(entry, _COLUMN_STATUS, "Cancelled")
        self._set_entry_text(entry, _COLUMN_SPEED, "—")
        self._set_action_button(entry, "Restart", partial(self._restart_download, entry), enabled=False)
        self._update_bulk_buttons()
        _LOGGER.info("Camera download cancelled: %s", entry.camera_name)

    def _set_action_button(
        self,
        entry: _DownloadEntry,
        text: str,
        callback: Callable[[], None],
        *,
        enabled: bool = True,
    ) -> None:
        table = self._table_for_entry(entry)
        old_widget = table.cellWidget(entry.row, _COLUMN_ACTION)
        if old_widget is not None:
            old_widget.deleteLater()
        button = QPushButton(text)
        button.clicked.connect(callback)
        button.setEnabled(enabled)
        table.setCellWidget(entry.row, _COLUMN_ACTION, button)
        entry.action_button = button

    def _restart_download(self, entry: _DownloadEntry) -> None:
        if entry.daily_schedule or not entry.terminal or entry.completed or entry.worker in self._workers:
            return
        if entry.output.exists():
            QMessageBox.warning(
                self,
                "Output Already Exists",
                f"Move or remove {entry.output.name} before restarting this job.",
            )
            return
        self._reserved_paths.add(self._reservation_key(entry.output))
        entry.terminal = False
        entry.cancelling = False
        entry.completed = False
        entry.downloaded_bytes = 0
        entry.last_progress_at = None
        self._set_entry_text(entry, _COLUMN_STATUS, "Preparing export…", tooltip="")
        self._set_entry_text(entry, _COLUMN_DOWNLOADED, "0 bytes")
        self._set_entry_text(entry, _COLUMN_EXPECTED, "Unknown")
        self._set_entry_text(entry, _COLUMN_SPEED, "—")
        entry.progress_bar.setRange(0, 0)
        entry.progress_bar.setFormat("")
        worker = _DownloadWorker(entry.config, entry.camera, entry.output, self)
        self._set_action_button(entry, "Cancel", partial(self._cancel_download, worker))
        self._start_download_worker(entry, worker)
        self.statusBar().showMessage(f"Restarted download for {entry.camera_name}", 5000)
        _LOGGER.info("Restarted camera download: %s -> %s", entry.camera_name, entry.output)

    @staticmethod
    def _show_output_folder(output: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output.parent)))

    def _download_worker_finished(self, worker: _DownloadWorker) -> None:
        entry = self._workers.pop(worker, None)
        worker.deleteLater()
        if entry is not None and entry.terminal and not entry.completed:
            entry.action_button.setEnabled(True)
        active_count = len(self._workers)
        message = "Ready" if active_count == 0 else f"{active_count} downloads active"
        self.statusBar().showMessage(message)
        self._update_activity_indicator()
        self._update_bulk_buttons()
        self._finish_close_if_ready()

    def _set_entry_text(
        self,
        entry: _DownloadEntry,
        column: int,
        text: str,
        *,
        tooltip: str | None = None,
    ) -> None:
        table = self._table_for_entry(entry)
        item = table.item(entry.row, column)
        if item is None:
            item = QTableWidgetItem()
            table.setItem(entry.row, column, item)
        item.setText(text)
        if tooltip is not None:
            item.setToolTip(tooltip)

    def _finish_close_if_ready(self) -> None:
        if self._closing and self._camera_loader is None and not self._workers:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Cancel background work before allowing the native window to close."""
        has_background_work = self._camera_loader is not None or bool(self._workers)
        if not has_background_work:
            self._daily_schedule = None
            self._preferences.setValue("output_directory", self._output_edit.text())
            self._logs_window.close()
            _LOGGER.info("Application closed")
            self._remove_log_handler()
            event.accept()
            return
        if self._closing:
            event.ignore()
            return
        response = QMessageBox.question(
            self,
            "Downloads Are Still Running",
            "Cancel the active work and quit? Partial download files will be removed.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if response != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self._closing = True
        self._daily_schedule = None
        self._daily_timer.stop()
        self._start_button.setEnabled(False)
        self.statusBar().showMessage("Cancelling active work…")
        _LOGGER.info("Application close requested; cancelling active work")
        if self._camera_loader is not None:
            self._camera_loader.cancel()
        for worker in tuple(self._workers):
            worker.cancel()
        event.ignore()


def _initial_profiles(dotenv_path: Path, store: _ProfileStore) -> tuple[_ProfileState | None, int]:  # noqa: PLR0911
    try:
        state = store.load()
    except _ProfileStoreError as exc:
        QMessageBox.critical(None, "Could Not Read Profiles", str(exc))
        return None, 1
    if state.profiles:
        _remove_legacy_dotenv(dotenv_path)
        return state, 0

    settings = _environment_settings(dotenv_path)
    if not _settings_need_prompt(settings):
        profile = _ConnectionProfile(str(uuid.uuid4()), settings.instance_url, settings).normalized()
        state = _ProfileState((profile,), profile.profile_id)
        try:
            store.save(state)
        except _ProfileStoreError as exc:
            QMessageBox.critical(None, "Could Not Save Profile", str(exc))
            return None, 1
        _remove_legacy_dotenv(dotenv_path)
        return state, 0

    blank_profile = _ConnectionProfile(str(uuid.uuid4()), "", settings)
    dialog = _CredentialsDialog(
        blank_profile,
        first_run=True,
        new_profile=True,
        parent=None,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None, 0
    profile = dialog.selected_profile()
    state = _ProfileState((profile,), profile.profile_id)
    try:
        store.save(state)
    except _ProfileStoreError as exc:
        QMessageBox.critical(None, "Could Not Save Profile", str(exc))
        return None, 1
    _remove_legacy_dotenv(dotenv_path)
    return state, 0


def _remove_legacy_dotenv(dotenv_path: Path) -> None:
    # A source checkout shares its cwd .env with the CLI, so only remove the
    # GUI-owned application-data file created by an older bundled release.
    if not _is_bundled() or not dotenv_path.is_file():
        return
    try:
        dotenv_path.unlink()
    except OSError as exc:
        QMessageBox.warning(
            None,
            "Legacy Credential File",
            f"Profiles are stored securely, but the old plaintext file could not be removed: {_exception_text(exc)}",
        )


def _is_supported_gui_platform() -> bool:
    return os.name == "nt" or sys.platform.startswith(("darwin", "linux"))


def _configure_platform_keyring() -> None:
    if os.name == "nt":
        from keyring.backends.Windows import WinVaultKeyring  # noqa: PLC0415

        keyring.set_keyring(WinVaultKeyring())
    elif sys.platform == "darwin":
        from keyring.backends.macOS import Keyring as MacOSKeyring  # noqa: PLC0415

        keyring.set_keyring(MacOSKeyring())
    elif sys.platform.startswith("linux"):
        from keyring.backends.SecretService import Keyring as SecretServiceKeyring  # noqa: PLC0415

        keyring.set_keyring(SecretServiceKeyring())


def main() -> int:
    """Run the desktop timelapse application."""
    app = QApplication(sys.argv)
    app.setApplicationName("UniFi Protect Timelapse")
    app.setOrganizationName("TimeLapse")
    icon_path = _application_icon_path()
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    if not _is_supported_gui_platform():
        QMessageBox.critical(
            None,
            "Unsupported Platform",
            "The Qt interface supports Windows, macOS, and Linux.",
        )
        return 1
    _configure_platform_keyring()
    dotenv_path = _application_dotenv_path()
    profile_store = _ProfileStore()
    profile_state, exit_code = _initial_profiles(dotenv_path, profile_store)
    if profile_state is None:
        return exit_code
    window = _MainWindow(profile_state, profile_store=profile_store)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

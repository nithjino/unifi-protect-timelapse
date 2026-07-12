"""Native desktop interface for the UniFi Protect timelapse exporter."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import ClassVar

from dotenv import load_dotenv
from PySide6.QtCore import (
    QDateTime,
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QSettings,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
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
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from timelapse import TimelapseError
from timelapse.config import DEFAULT_MAX_DOWNLOAD_MIB, DEFAULT_REQUEST_TIMEOUT_SECONDS, SPEED_TO_FPS, Config
from timelapse.download import DownloadProgress, default_output_path
from timelapse.protect import CameraInfo, parse_connection
from timelapse.service import export_timelapse, list_available_cameras

_ENVIRONMENT_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_DOTENV_TEMPLATE = """# Local runtime configuration for the UniFi Protect timelapse exporter.
UNIFI_PROTECT_URL=

# Token is needed to get a list of cameras.
UNIFI_PROTECT_TOKEN=

# Use a dedicated local Protect user with permission to view/export recordings.
# These credentials are needed to generate and download the timelapse.
UNIFI_PROTECT_USERNAME=
UNIFI_PROTECT_PASSWORD=
UNIFI_PROTECT_VERIFY_SSL=true

# Optional runtime limits. Set either value to 0 to disable that limit.
TIMELAPSE_REQUEST_TIMEOUT_SECONDS=0
TIMELAPSE_MAX_DOWNLOAD_MIB=10240
"""
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
_LOG_ANIMATION_DURATION_MS = 180
_LOG_DRAWER_HEIGHT = 220
_MAX_LOG_LINES = 2000
_APPLICATION_DIRECTORY_NAME = "TimeLapse"
_MINIMUM_DATE = QDateTime.fromString("2000-01-01T00:00:00", Qt.DateFormat.ISODate)
_TABLE_HEADERS = ("Job", "Camera", "Status", "Progress", "Downloaded", "Expected", "Speed", "Output", "Action")
_COLUMN_JOB = 0
_COLUMN_CAMERA = 1
_COLUMN_STATUS = 2
_COLUMN_PROGRESS = 3
_COLUMN_DOWNLOADED = 4
_COLUMN_EXPECTED = 5
_COLUMN_SPEED = 6
_COLUMN_OUTPUT = 7
_COLUMN_ACTION = 8
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


@dataclass
class _DownloadEntry:
    row: int
    output: Path
    camera_name: str
    progress_bar: QProgressBar
    action_button: QPushButton
    downloaded_bytes: int = 0
    last_progress_at: float | None = None
    cancelling: bool = False
    terminal: bool = False


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


def _is_bundled() -> bool:
    return bool(getattr(sys, "frozen", False))


def _application_data_directory() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APPLICATION_DIRECTORY_NAME
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        base_directory = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
        return base_directory / _APPLICATION_DIRECTORY_NAME
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


def _settings_need_prompt(settings: _ConnectionSettings) -> bool:
    if settings.missing_fields():
        return True
    try:
        parse_connection(settings.instance_url)
    except TimelapseError:
        return True
    return False


def _dotenv_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")
    return f'"{escaped}"'


def _updated_dotenv(existing: str, settings: _ConnectionSettings) -> str:
    replacements = {
        "UNIFI_PROTECT_URL": settings.instance_url,
        "UNIFI_PROTECT_TOKEN": settings.token,
        "UNIFI_PROTECT_USERNAME": settings.username,
        "UNIFI_PROTECT_PASSWORD": settings.password,
        "UNIFI_PROTECT_VERIFY_SSL": "true" if settings.verify_ssl else "false",
    }
    lines = existing.splitlines()
    replaced: set[str] = set()
    for index, line in enumerate(lines):
        match = _ENVIRONMENT_ASSIGNMENT.match(line)
        if match is None or match.group(1) not in replacements:
            continue
        name = match.group(1)
        lines[index] = f"{name}={_dotenv_quote(replacements[name])}"
        replaced.add(name)

    if lines and lines[-1]:
        lines.append("")
    for name, value in replacements.items():
        if name not in replaced:
            lines.append(f"{name}={_dotenv_quote(value)}")
    return "\n".join(lines).rstrip() + "\n"


def _write_dotenv(dotenv_path: Path, settings: _ConnectionSettings) -> None:
    existing = dotenv_path.read_text(encoding="utf-8") if dotenv_path.is_file() else _DOTENV_TEMPLATE
    contents = _updated_dotenv(existing, settings)
    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=dotenv_path.parent,
            prefix=f".{dotenv_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(contents)
            temporary_path = Path(temporary_file.name)
        temporary_path.chmod(0o600)
        temporary_path.replace(dotenv_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
        settings: _ConnectionSettings,
        dotenv_path: Path,
        *,
        first_run: bool,
        parent: QWidget | None,
    ) -> None:
        super().__init__(parent)
        self._existing_settings = settings
        self._result: _ConnectionSettings | None = None
        self.setWindowTitle("Set Up UniFi Protect" if first_run else "Protect Connection")
        self.setMinimumWidth(560)
        self._build_interface(dotenv_path, first_run=first_run)

    def _build_interface(self, dotenv_path: Path, *, first_run: bool) -> None:
        layout = QVBoxLayout(self)
        introduction = QLabel(
            "Enter the connection details needed to list cameras and export recordings."
            if first_run
            else "Update the connection details used for future camera lists and downloads."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)

        form = QFormLayout()
        self._url_edit = self._line_edit(self._existing_settings.instance_url, _URL_TOOLTIP)
        self._token_edit = self._line_edit(self._existing_settings.token, _TOKEN_TOOLTIP, secret=True)
        self._username_edit = self._line_edit(self._existing_settings.username, _USERNAME_TOOLTIP)
        self._password_edit = self._line_edit(self._existing_settings.password, _PASSWORD_TOOLTIP, secret=True)
        self._add_field(form, "Protect URL:", self._url_edit, _URL_TOOLTIP)
        self._add_field(form, "API token:", self._token_edit, _TOKEN_TOOLTIP)
        self._add_field(form, "Local username:", self._username_edit, _USERNAME_TOOLTIP)
        self._add_field(form, "Local password:", self._password_edit, _PASSWORD_TOOLTIP)
        self._verify_ssl = QCheckBox("Verify the server's TLS certificate")
        self._verify_ssl.setChecked(self._existing_settings.verify_ssl)
        self._verify_ssl.setToolTip(_VERIFY_SSL_TOOLTIP)
        self._add_field(form, "Security:", self._verify_ssl, _VERIFY_SSL_TOOLTIP)
        layout.addLayout(form)

        storage_note = QLabel(
            f"These values will be stored in {dotenv_path} as plaintext with owner-only file permissions."
        )
        storage_note.setWordWrap(True)
        storage_note.setToolTip("The .env file is local and excluded from Git, but it still contains sensitive values.")
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
        candidate = _ConnectionSettings(
            instance_url=self._url_edit.text().strip().rstrip("/"),
            token=self._token_edit.text().strip(),
            username=self._username_edit.text().strip(),
            password=self._password_edit.text(),
            verify_ssl=self._verify_ssl.isChecked(),
            request_timeout_seconds=self._existing_settings.request_timeout_seconds,
            max_download_mib=self._existing_settings.max_download_mib,
        )
        missing = candidate.missing_fields()
        if missing:
            QMessageBox.warning(self, "Missing Connection Details", f"Please provide: {', '.join(missing)}.")
            return
        try:
            parse_connection(candidate.instance_url)
        except TimelapseError as exc:
            QMessageBox.warning(self, "Invalid Protect URL", str(exc))
            return
        self._result = candidate
        super().accept()

    def selected_settings(self) -> _ConnectionSettings:
        if self._result is None:
            message = "connection settings were requested before the dialog was accepted"
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
    def __init__(self, settings: _ConnectionSettings, dotenv_path: Path) -> None:
        super().__init__()
        self._settings = settings
        self._dotenv_path = dotenv_path
        self._preferences = QSettings("TimeLapse", "UniFi Protect Timelapse")
        self._cameras: list[CameraInfo] = []
        self._selected_cameras: list[CameraInfo] = []
        self._camera_loader: _CameraLoader | None = None
        self._open_camera_dialog_after_load = False
        self._workers: dict[_DownloadWorker, _DownloadEntry] = {}
        self._reserved_paths: set[str] = set()
        self._next_job_number = 1
        self._closing = False
        self._log_handler_attached = False
        self.setWindowTitle("UniFi Protect Timelapse")
        self.resize(1180, 720)
        self.setMinimumSize(940, 600)
        self._build_menu()
        self._build_interface()
        self._speed_timer = QTimer(self)
        self._speed_timer.setInterval(1000)
        self._speed_timer.timeout.connect(self._clear_stalled_speeds)
        self._speed_timer.start()
        self._install_log_handler()
        self._update_connection_label()
        self._update_camera_summary()
        self._update_activity_indicator()
        _LOGGER.info("Application ready")

    def _build_menu(self) -> None:
        application_menu = self.menuBar().addMenu("&Application")
        connection_action = QAction("Protect Connection…", self)
        connection_action.triggered.connect(self._edit_connection)
        application_menu.addAction(connection_action)
        application_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        application_menu.addAction(quit_action)

    def _build_interface(self) -> None:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self._connection_group())
        layout.addWidget(self._options_group())
        layout.addWidget(self._downloads_group(), stretch=1)
        layout.addWidget(self._logs_drawer())
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
        self._logs_button.setCheckable(True)
        self._logs_button.setToolTip("Show or hide application logs.")
        self._logs_button.toggled.connect(self._toggle_logs)
        self.statusBar().addPermanentWidget(self._logs_button)

    def _logs_drawer(self) -> QGroupBox:
        drawer = QGroupBox("Application Logs")
        layout = QVBoxLayout(drawer)
        controls = QHBoxLayout()
        controls.addStretch(1)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._clear_logs)
        controls.addWidget(clear_button)
        layout.addLayout(controls)
        self._log_output = QPlainTextEdit()
        self._log_output.setReadOnly(True)
        self._log_output.setMaximumBlockCount(_MAX_LOG_LINES)
        self._log_output.setPlaceholderText("Application activity and errors will appear here.")
        layout.addWidget(self._log_output)
        drawer.setMaximumHeight(0)
        drawer.setVisible(False)
        self._log_drawer = drawer
        self._log_animation = QPropertyAnimation(drawer, b"maximumHeight", self)
        self._log_animation.setDuration(_LOG_ANIMATION_DURATION_MS)
        self._log_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._log_animation.finished.connect(self._finish_log_animation)
        return drawer

    def _install_log_handler(self) -> None:
        self._log_emitter = _LogEmitter(self)
        self._log_emitter.message_ready.connect(self._append_log)
        self._log_handler = _QtLogHandler(self._log_emitter)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
        )
        logging.getLogger().addHandler(self._log_handler)
        self._log_handler_attached = True

    def _remove_log_handler(self) -> None:
        if self._log_handler_attached:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler_attached = False

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self._log_output.appendPlainText(message)

    @Slot()
    def _clear_logs(self) -> None:
        self._log_output.clear()

    @Slot(bool)
    def _toggle_logs(self, checked: object) -> None:
        visible = bool(checked)
        self._log_animation.stop()
        if visible:
            self._log_drawer.setVisible(True)
        self._log_animation.setStartValue(self._log_drawer.maximumHeight())
        self._log_animation.setEndValue(_LOG_DRAWER_HEIGHT if visible else 0)
        self._log_animation.start()

    @Slot()
    def _finish_log_animation(self) -> None:
        if not self._logs_button.isChecked():
            self._log_drawer.setVisible(False)

    def _update_activity_indicator(self) -> None:
        self._activity_widget.setVisible(self._camera_loader is not None or bool(self._workers))

    def _connection_group(self) -> QGroupBox:
        group = QGroupBox("Protect Connection")
        layout = QHBoxLayout(group)
        self._connection_label = QLabel()
        self._connection_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._connection_label, stretch=1)
        edit_button = QPushButton("Edit Connection…")
        edit_button.clicked.connect(self._edit_connection)
        layout.addWidget(edit_button)
        return group

    def _options_group(self) -> QGroupBox:
        group = QGroupBox("New Timelapse")
        form = QFormLayout(group)

        now = QDateTime.currentDateTime()
        self._start_edit = self._date_time_editor(now.addDays(-1))
        self._end_edit = self._date_time_editor(now)
        form.addRow("Start:", self._start_edit)
        form.addRow("End:", self._end_edit)

        self._speed_combo = QComboBox()
        self._speed_combo.addItems(list(SPEED_TO_FPS))
        self._speed_combo.setCurrentText("600x")
        self._speed_combo.setToolTip("Higher values create a faster timelapse.")
        form.addRow("Speed:", self._speed_combo)

        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        self._output_edit = QLineEdit(self._saved_output_directory())
        self._output_edit.setReadOnly(True)
        output_layout.addWidget(self._output_edit, stretch=1)
        browse_button = QPushButton("Choose…")
        browse_button.clicked.connect(self._choose_output_directory)
        output_layout.addWidget(browse_button)
        form.addRow("Save to:", output_row)

        camera_row = QWidget()
        camera_layout = QHBoxLayout(camera_row)
        camera_layout.setContentsMargins(0, 0, 0, 0)
        self._camera_summary = QLabel()
        camera_layout.addWidget(self._camera_summary, stretch=1)
        self._select_cameras_button = QPushButton("Select Cameras…")
        self._select_cameras_button.clicked.connect(self._request_camera_selection)
        camera_layout.addWidget(self._select_cameras_button)
        self._refresh_cameras_button = QPushButton("Refresh")
        self._refresh_cameras_button.clicked.connect(partial(self._load_cameras, open_dialog=True))
        camera_layout.addWidget(self._refresh_cameras_button)
        form.addRow("Cameras:", camera_row)

        self._start_button = QPushButton("Start Downloads")
        self._start_button.setEnabled(False)
        self._start_button.clicked.connect(self._queue_downloads)
        form.addRow("", self._start_button)
        return group

    @staticmethod
    def _date_time_editor(value: QDateTime) -> QDateTimeEdit:
        editor = QDateTimeEdit(value)
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("MMM d, yyyy h:mm AP")
        editor.setMinimumDateTime(_MINIMUM_DATE)
        editor.setToolTip("Type a date and time or use the calendar button to choose a date.")
        return editor

    def _downloads_group(self) -> QGroupBox:
        group = QGroupBox("Downloads")
        layout = QVBoxLayout(group)
        self._downloads = QTableWidget(0, len(_TABLE_HEADERS))
        self._downloads.setHorizontalHeaderLabels(_TABLE_HEADERS)
        self._downloads.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._downloads.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._downloads.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._downloads.setAlternatingRowColors(True)
        self._downloads.setSortingEnabled(False)
        self._downloads.verticalHeader().setVisible(False)
        header = self._downloads.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COLUMN_PROGRESS, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COLUMN_OUTPUT, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._downloads)
        return group

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
        dialog = _CredentialsDialog(self._settings, self._dotenv_path, first_run=False, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.selected_settings()
        try:
            _write_dotenv(self._dotenv_path, updated)
        except OSError as exc:
            QMessageBox.critical(self, "Could Not Save .env", _exception_text(exc))
            return
        self._settings = updated
        self._cameras.clear()
        self._selected_cameras.clear()
        self._update_connection_label()
        self._update_camera_summary()
        self.statusBar().showMessage("Connection settings saved", 5000)
        _LOGGER.info("Protect connection settings saved")

    def _update_connection_label(self) -> None:
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
            QMessageBox.information(self, "No Cameras", "No cameras were returned by UniFi Protect.")
            return
        self.statusBar().showMessage(f"Loaded {len(self._cameras)} cameras", 5000)
        _LOGGER.info("Loaded %d cameras", len(self._cameras))
        if self._open_camera_dialog_after_load:
            self._show_camera_selection()

    @Slot(str)
    def _camera_load_failed(self, message: str) -> None:
        if not self._closing:
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
            entry = self._add_download_row(job_number, camera, output, worker)
            self._workers[worker] = entry
            worker.progress_changed.connect(partial(self._download_progress, entry))
            worker.download_succeeded.connect(partial(self._download_succeeded, entry))
            worker.download_failed.connect(partial(self._download_failed, entry))
            worker.download_cancelled.connect(partial(self._download_cancelled, entry))
            worker.finished.connect(partial(self._download_worker_finished, worker))
            worker.start()
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
    ) -> _DownloadEntry:
        row = self._downloads.rowCount()
        self._downloads.insertRow(row)
        values = {
            _COLUMN_JOB: str(job_number),
            _COLUMN_CAMERA: camera.name,
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
        return _DownloadEntry(row, output, camera.name, progress_bar, cancel_button)

    def _cancel_download(self, worker: _DownloadWorker) -> None:
        entry = self._workers.get(worker)
        if entry is None or entry.terminal or entry.cancelling:
            return
        entry.cancelling = True
        entry.action_button.setEnabled(False)
        self._set_table_text(entry.row, _COLUMN_STATUS, "Cancelling…")
        _LOGGER.info("Cancelling camera download: %s", entry.camera_name)
        worker.cancel()

    def _download_progress(self, entry: _DownloadEntry, payload: object) -> None:
        if entry.terminal or not isinstance(payload, DownloadProgress):
            return
        entry.downloaded_bytes = payload.downloaded_bytes
        entry.last_progress_at = monotonic()
        if not entry.cancelling:
            status = "Downloading" if payload.downloaded_bytes else "Preparing export…"
            self._set_table_text(entry.row, _COLUMN_STATUS, status)
        self._set_table_text(entry.row, _COLUMN_DOWNLOADED, _format_bytes(payload.downloaded_bytes))
        expected = "Unknown" if payload.total_bytes is None else _format_bytes(payload.total_bytes)
        self._set_table_text(entry.row, _COLUMN_EXPECTED, expected)
        self._set_table_text(entry.row, _COLUMN_SPEED, _format_speed(payload.bytes_per_second))
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
            self._set_table_text(entry.row, _COLUMN_SPEED, "0 bytes/s")

    def _download_succeeded(self, entry: _DownloadEntry, _output_text: str) -> None:
        entry.terminal = True
        self._set_table_text(entry.row, _COLUMN_STATUS, "Completed")
        with suppress(OSError):
            entry.downloaded_bytes = entry.output.stat().st_size
        self._set_table_text(entry.row, _COLUMN_DOWNLOADED, _format_bytes(entry.downloaded_bytes))
        entry.progress_bar.setRange(0, _PROGRESS_SCALE)
        entry.progress_bar.setValue(_PROGRESS_SCALE)
        entry.progress_bar.setFormat("100%")
        self._replace_action_with_show_folder(entry)
        _LOGGER.info("Completed camera download: %s -> %s", entry.camera_name, entry.output)

    def _download_failed(self, entry: _DownloadEntry, message: str) -> None:
        entry.terminal = True
        self._reserved_paths.discard(self._reservation_key(entry.output))
        self._set_table_text(entry.row, _COLUMN_STATUS, "Failed", tooltip=message)
        self._set_table_text(entry.row, _COLUMN_SPEED, "—")
        entry.action_button.setText("Failed")
        entry.action_button.setEnabled(False)
        _LOGGER.error("Camera download failed for %s: %s", entry.camera_name, message)

    def _download_cancelled(self, entry: _DownloadEntry) -> None:
        entry.terminal = True
        self._reserved_paths.discard(self._reservation_key(entry.output))
        self._set_table_text(entry.row, _COLUMN_STATUS, "Cancelled")
        self._set_table_text(entry.row, _COLUMN_SPEED, "—")
        entry.action_button.setText("Cancelled")
        entry.action_button.setEnabled(False)
        _LOGGER.info("Camera download cancelled: %s", entry.camera_name)

    def _replace_action_with_show_folder(self, entry: _DownloadEntry) -> None:
        old_widget = self._downloads.cellWidget(entry.row, _COLUMN_ACTION)
        if old_widget is not None:
            old_widget.deleteLater()
        show_button = QPushButton("Show Folder")
        show_button.clicked.connect(partial(self._show_output_folder, entry.output))
        self._downloads.setCellWidget(entry.row, _COLUMN_ACTION, show_button)
        entry.action_button = show_button

    @staticmethod
    def _show_output_folder(output: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output.parent)))

    def _download_worker_finished(self, worker: _DownloadWorker) -> None:
        self._workers.pop(worker, None)
        worker.deleteLater()
        active_count = len(self._workers)
        message = "Ready" if active_count == 0 else f"{active_count} downloads active"
        self.statusBar().showMessage(message)
        self._update_activity_indicator()
        self._finish_close_if_ready()

    def _set_table_text(self, row: int, column: int, text: str, *, tooltip: str | None = None) -> None:
        item = self._downloads.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self._downloads.setItem(row, column, item)
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
            self._preferences.setValue("output_directory", self._output_edit.text())
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
        self._start_button.setEnabled(False)
        self.statusBar().showMessage("Cancelling active work…")
        _LOGGER.info("Application close requested; cancelling active work")
        if self._camera_loader is not None:
            self._camera_loader.cancel()
        for worker in tuple(self._workers):
            worker.cancel()
        event.ignore()


def _initial_settings(dotenv_path: Path) -> tuple[_ConnectionSettings | None, int]:
    settings = _environment_settings(dotenv_path)
    if dotenv_path.is_file() and not _settings_need_prompt(settings):
        return settings, 0
    dialog = _CredentialsDialog(settings, dotenv_path, first_run=not dotenv_path.is_file(), parent=None)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None, 0
    settings = dialog.selected_settings()
    try:
        _write_dotenv(dotenv_path, settings)
    except OSError as exc:
        QMessageBox.critical(None, "Could Not Save .env", _exception_text(exc))
        return None, 1
    return settings, 0


def main() -> int:
    """Run the desktop timelapse application."""
    app = QApplication(sys.argv)
    app.setApplicationName("UniFi Protect Timelapse")
    app.setOrganizationName("TimeLapse")
    dotenv_path = _application_dotenv_path()
    settings, exit_code = _initial_settings(dotenv_path)
    if settings is None:
        return exit_code
    window = _MainWindow(settings, dotenv_path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

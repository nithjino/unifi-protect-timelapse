"""JSON-lines bridge between native desktop UIs and Python export services."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import signal
import sys
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from platformdirs import user_config_path

from timelapse import ProtectRateLimitError, TimelapseError
from timelapse.automation_registry import (
    AutomationCamera,
    AutomationRegistry,
    ConnectionReference,
    DailyAutomation,
    RegistryError,
    new_automation_id,
)
from timelapse.config import (
    DEFAULT_MAX_DOWNLOAD_MIB,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    SPEED_TO_FPS,
    Config,
    ConnectionSettings,
)
from timelapse.jobs import ExportJobCoordinator, ExportJobSpec
from timelapse.protect import CameraInfo, protect_session_scope
from timelapse.schedule import DailyAutomationEngine, ResolvedAutomation
from timelapse.service import export_timelapse, fetch_camera_thumbnail, list_available_cameras

if TYPE_CHECKING:
    from collections.abc import Mapping

    from timelapse.download import DownloadProgress

MAX_REQUEST_BYTES = 1024 * 1024
PROTOCOL_VERSION = 2
# This module is also executed with ``python -m`` and bundled as a standalone
# executable, where ``__name__`` is ``__main__`` rather than a package child.
_LOGGER = logging.getLogger("timelapse.native_backend")


class _ProtocolError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


def _write_event(payload: Mapping[str, object]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(f"{serialized}\n")
    sys.stdout.flush()


class _NativeLogHandler(logging.Handler):
    """Forward backend logs through the JSON-lines protocol used by native UIs."""

    def __init__(self, request_id: str | None) -> None:
        super().__init__(logging.INFO)
        self._request_id = request_id
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            _write_event(
                {
                    "id": self._request_id,
                    "event": "log",
                    "level": record.levelname,
                    "message": message,
                }
            )
        except Exception:
            self.handleError(record)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        message = f"{field} must be a JSON object"
        raise _ProtocolError(message)
    return {str(key): item for key, item in value.items()}


def _required_string(mapping: Mapping[str, object], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        message = f"{field} must be a non-empty string"
        raise _ProtocolError(message)
    return value


def _optional_string(mapping: Mapping[str, object], field: str) -> str | None:
    value = mapping.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        message = f"{field} must be a string or null"
        raise _ProtocolError(message)
    return value


def _boolean(mapping: Mapping[str, object], field: str, *, default: bool) -> bool:
    value = mapping.get(field, default)
    if not isinstance(value, bool):
        message = f"{field} must be a boolean"
        raise _ProtocolError(message)
    return value


def _nonnegative_integer(mapping: Mapping[str, object], field: str, default: int) -> int:
    value = mapping.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        message = f"{field} must be zero or a positive whole number"
        raise _ProtocolError(message)
    return value


def _aware_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        message = f"{field} must be an ISO-8601 date and time"
        raise _ProtocolError(message) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        message = f"{field} must include a timezone offset"
        raise _ProtocolError(message)
    return parsed


def _config(
    request: Mapping[str, object],
    *,
    start: datetime,
    end: datetime,
    speed: str,
    output: Path | None,
) -> Config:
    settings = _mapping(request.get("settings"), "settings")
    if end <= start:
        message = "end must be after start"
        raise _ProtocolError(message)
    if speed not in SPEED_TO_FPS:
        message = f"speed must be one of: {', '.join(SPEED_TO_FPS)}"
        raise _ProtocolError(message)
    return Config(
        **asdict(_connection_settings(settings)),
        speed=speed,
        start=start,
        end=end,
        output=output,
    )


def _connection_settings(settings: Mapping[str, object]) -> ConnectionSettings:
    return ConnectionSettings(
        instance_url=_required_string(settings, "instance_url").strip().rstrip("/"),
        token=_required_string(settings, "token"),
        username=_required_string(settings, "username"),
        password=_required_string(settings, "password"),
        verify_ssl=_boolean(settings, "verify_ssl", default=True),
        request_timeout_seconds=_nonnegative_integer(
            settings, "request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS
        ),
        max_download_mib=_nonnegative_integer(settings, "max_download_mib", DEFAULT_MAX_DOWNLOAD_MIB),
    )


def _camera(value: object) -> CameraInfo:
    camera = _mapping(value, "camera")
    return CameraInfo(
        id=_required_string(camera, "id"),
        name=_required_string(camera, "name"),
        state=_optional_string(camera, "state"),
        model=_optional_string(camera, "model"),
    )


def _output_path(value: str) -> Path:
    return Path(value).expanduser()


async def _list_cameras(request_id: str, request: Mapping[str, object]) -> None:
    now = datetime.now().astimezone()
    config = _config(request, start=now, end=now + timedelta(seconds=1), speed="600x", output=None)
    cameras = await list_available_cameras(config)
    serialized_cameras: list[object] = [
        {
            "id": camera.id,
            "name": camera.name,
            "state": camera.state,
            "model": camera.model,
        }
        for camera in cameras
    ]
    _write_event({"id": request_id, "event": "cameras", "cameras": serialized_cameras})


async def _download(request_id: str, request: Mapping[str, object]) -> None:
    start = _aware_datetime(_required_string(request, "start"), "start")
    end = _aware_datetime(_required_string(request, "end"), "end")
    speed = _required_string(request, "speed")
    output = _output_path(_required_string(request, "output"))
    camera = _camera(request.get("camera"))
    config = _config(request, start=start, end=end, speed=speed, output=output)

    def report_progress(progress: DownloadProgress) -> None:
        _write_event(
            {
                "id": request_id,
                "event": "progress",
                "downloaded_bytes": progress.downloaded_bytes,
                "total_bytes": progress.total_bytes,
                "bytes_per_second": progress.bytes_per_second,
                "elapsed_seconds": progress.elapsed_seconds,
            }
        )

    await export_timelapse(config, camera, output, report_progress)
    _write_event({"id": request_id, "event": "complete", "output": str(output)})


async def _thumbnail(request_id: str, request: Mapping[str, object]) -> None:
    timestamp = _aware_datetime(_required_string(request, "timestamp"), "timestamp")
    camera = _camera(request.get("camera"))
    config = _config(
        request,
        start=timestamp,
        end=timestamp + timedelta(seconds=1),
        speed="600x",
        output=None,
    )
    thumbnail = await fetch_camera_thumbnail(config, camera, timestamp)
    _write_event(
        {
            "id": request_id,
            "event": "thumbnail",
            "thumbnail_base64": base64.b64encode(thumbnail.image).decode("ascii"),
            "thumbnail_source": thumbnail.source,
        }
    )


async def _dispatch(request: Mapping[str, object]) -> None:
    request_id = _required_string(request, "id")
    command = _required_string(request, "command")
    if command == "health":
        _write_event({"id": request_id, "event": "complete", "status": "ok"})
        return
    if command == "list_cameras":
        await _list_cameras(request_id, request)
        return
    if command == "download":
        await _download(request_id, request)
        return
    if command == "thumbnail":
        await _thumbnail(request_id, request)
        return
    message = f"unsupported command: {command}"
    raise _ProtocolError(message, code="unsupported_command")


async def _cancel_when_requested(cancel_path: Path, task: asyncio.Task[object]) -> None:
    while not cancel_path.exists():  # noqa: ASYNC110, ASYNC240 - local sentinel poll
        await asyncio.sleep(0.1)
    task.cancel()


def _read_request() -> dict[str, object]:
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if not raw:
        message = "expected one JSON request on stdin"
        raise _ProtocolError(message)
    if len(raw) > MAX_REQUEST_BYTES:
        message = "request exceeds the maximum allowed size"
        raise _ProtocolError(message)
    try:
        decoded: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = "stdin did not contain valid JSON"
        raise _ProtocolError(message) from exc
    return _mapping(decoded, "request")


def _read_optional_request() -> dict[str, object] | None:
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if not raw:
        return None
    if len(raw) > MAX_REQUEST_BYTES:
        message = "request exceeds the maximum allowed size"
        raise _ProtocolError(message)
    try:
        decoded: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = "stdin did not contain valid JSON"
        raise _ProtocolError(message) from exc
    return _mapping(decoded, "request")


class NativeSessionSupervisor:
    """Own a version-2 native app session and multiplex commands by request ID."""

    def __init__(self, registry_path: Path) -> None:
        """Configure one native session with its private durable registry."""
        self.registry = AutomationRegistry(registry_path)
        self.coordinator = ExportJobCoordinator.from_environment()
        self._credentials: dict[str, dict[str, object]] = {}
        self._seen_ids: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self.engine = DailyAutomationEngine(
            self.registry,
            self.coordinator,
            resolve_connection=self._resolve_automation,
            exporter=export_timelapse,
        )

    @property
    def stopping(self) -> bool:
        """Return whether graceful shutdown has been accepted."""
        return self._stopping

    def start_automation(self, automation_id: str) -> None:
        """Start one active automation runner if it is not already running."""
        self._start_automation(automation_id)

    async def handshake(self, request: Mapping[str, object]) -> None:
        """Reject version mismatches before credentials or work are accepted."""
        request_id = _required_string(request, "id")
        version = request.get("protocol_version")
        if version != PROTOCOL_VERSION:
            message = f"protocol version mismatch: backend requires {PROTOCOL_VERSION}, client sent {version!r}"
            _write_event(
                {
                    "id": request_id,
                    "event": "error",
                    "code": "protocol_version_mismatch",
                    "message": message,
                }
            )
            raise _ProtocolError(message, code="protocol_version_mismatch")
        self._seen_ids.add(request_id)
        _write_event(
            {
                "id": request_id,
                "event": "complete",
                "status": "ready",
                "protocol_version": PROTOCOL_VERSION,
            }
        )

    async def accept(self, request: Mapping[str, object]) -> bool:
        """Accept one command and return whether the input loop should continue."""
        request_id = _required_string(request, "id")
        if request_id in self._seen_ids:
            _write_event(
                {
                    "id": request_id,
                    "event": "error",
                    "code": "duplicate_request_id",
                    "message": "request IDs must be unique within a backend session",
                }
            )
            return True
        self._seen_ids.add(request_id)
        command = _required_string(request, "command")
        if command == "cancel":
            await self._cancel_command(request_id, request)
            return True
        if command == "shutdown":
            self._stopping = True
            await self._graceful_shutdown(request_id)
            return False
        task = asyncio.create_task(self._run_command(request_id, command, request), name=f"native-{request_id}")
        self._tasks[request_id] = task
        task.add_done_callback(lambda _task, value=request_id: self._tasks.pop(value, None))
        return True

    async def close(self) -> None:
        """Cancel session work after an interrupted supervisor connection."""
        for task in tuple(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.coordinator.close()

    async def _run_command(self, request_id: str, command: str, request: Mapping[str, object]) -> None:
        try:
            terminal_emitted = await self._dispatch_session_command(request_id, command, request)
            if not terminal_emitted:
                _write_event({"id": request_id, "event": "complete"})
        except asyncio.CancelledError:
            _write_event({"id": request_id, "event": "cancelled"})
        except _ProtocolError as exc:
            _write_event({"id": request_id, "event": "error", "code": exc.code, "message": str(exc)})
        except ProtectRateLimitError as exc:
            _write_event(
                {
                    "id": request_id,
                    "event": "error",
                    "code": "protect_rate_limited",
                    "message": str(exc),
                    "retry_not_before": exc.retry_not_before.isoformat() if exc.retry_not_before else None,
                }
            )
        except TimelapseError as exc:
            _write_event({"id": request_id, "event": "error", "code": "timelapse_error", "message": str(exc)})
        except Exception as exc:
            _LOGGER.exception("Native session command failed: %s", command)
            _write_event({"id": request_id, "event": "error", "code": "internal_error", "message": str(exc)})

    async def _dispatch_session_command(  # noqa: PLR0911, PLR0912 - protocol command router
        self,
        request_id: str,
        command: str,
        request: Mapping[str, object],
    ) -> bool:
        if command == "hydrate_credentials":
            profile_id = _required_string(request, "profile_id")
            self._credentials[profile_id] = _mapping(request.get("settings"), "settings")
            for automation in self.registry.list():
                if (
                    automation.status == "active"
                    and automation.connection.kind == "native-profile"
                    and automation.connection.value == profile_id
                ):
                    self._start_automation(automation.id)
            return False
        if command in {"health", "list_cameras", "download", "thumbnail"}:
            command_request = dict(request)
            if "settings" not in command_request:
                profile_id = _required_string(request, "profile_id")
                settings = self._credentials.get(profile_id)
                if settings is None:
                    message = f"native profile {profile_id!r} has not been hydrated"
                    raise _ProtocolError(message, code="credentials_not_hydrated")
                command_request["settings"] = settings
            if command == "download":
                await self._coordinated_download(request_id, command_request)
                return True
            await _dispatch(command_request)
            return command == "health"
        if command == "state_snapshot":
            _write_event({"id": request_id, "event": "state", **self._snapshot()})
            return False
        if command == "automation_add":
            await self._automation_add(request_id, request)
            return False
        if command == "automation_edit":
            await self._automation_edit(request_id, request)
            return False
        if command == "automation_stop":
            await self.engine.stop(_required_string(request, "automation_id"))
            return False
        if command == "automation_resume":
            automation_id = _required_string(request, "automation_id")
            await self.engine.resume(automation_id)
            self._start_automation(automation_id)
            return False
        if command == "automation_remove":
            await self.engine.remove(_required_string(request, "automation_id"))
            return False
        if command == "automation_reexport":
            await self.engine.reexport(
                _required_string(request, "automation_id"),
                date.fromisoformat(_required_string(request, "day")),
            )
            return False
        message = f"unsupported command: {command}"
        raise _ProtocolError(message, code="unsupported_command")

    async def _coordinated_download(self, request_id: str, request: Mapping[str, object]) -> None:
        output = _output_path(_required_string(request, "output"))
        jobs = await self.coordinator.submit(
            [
                ExportJobSpec.create(
                    output=output,
                    operation=lambda: _download(request_id, request),
                    job_id=f"manual:{request_id}",
                )
            ]
        )
        try:
            await self.coordinator.wait(jobs)
        except asyncio.CancelledError:
            await self.coordinator.cancel(jobs[0].id)
            raise
        job = jobs[0]
        if job.status == "cancelled":
            raise asyncio.CancelledError
        if job.status == "failed":
            raise TimelapseError(job.error or "manual export failed")

    async def _automation_add(self, request_id: str, request: Mapping[str, object]) -> None:
        cameras_value = request.get("cameras")
        if not isinstance(cameras_value, list):
            message = "cameras must be a JSON list"
            raise _ProtocolError(message)
        cameras = tuple(
            AutomationCamera(camera.id, camera.name) for camera in (_camera(value) for value in cameras_value)
        )
        entity_id = _optional_string(request, "automation_id")
        automation_id = self._native_automation_id(entity_id) if entity_id else new_automation_id()
        automation = self.registry.add(
            name=_required_string(request, "name"),
            cameras=cameras,
            connection=ConnectionReference("native-profile", _required_string(request, "profile_id")),
            speed=_required_string(request, "speed"),
            output_directory=_output_path(_required_string(request, "output_directory")),
            timezone=_required_string(request, "timezone"),
            automation_id=automation_id,
        )
        _write_event({"id": request_id, "event": "automation", "automation": self._automation_payload(automation)})
        self._start_automation(automation.id)

    async def _automation_edit(self, request_id: str, request: Mapping[str, object]) -> None:
        cameras_value = request.get("cameras")
        cameras = None
        if cameras_value is not None:
            if not isinstance(cameras_value, list):
                message = "cameras must be a JSON list"
                raise _ProtocolError(message)
            cameras = tuple(
                AutomationCamera(camera.id, camera.name) for camera in (_camera(value) for value in cameras_value)
            )
        profile_id = _optional_string(request, "profile_id")
        automation = self.registry.edit(
            _required_string(request, "automation_id"),
            name=_optional_string(request, "name"),
            cameras=cameras,
            connection=ConnectionReference("native-profile", profile_id) if profile_id else None,
        )
        _write_event({"id": request_id, "event": "automation", "automation": self._automation_payload(automation)})

    async def _resolve_automation(self, automation: DailyAutomation) -> ResolvedAutomation:
        settings = self._credentials.get(automation.connection.value)
        if settings is None:
            message = f"native profile {automation.connection.value!r} has not been hydrated"
            raise RegistryError(message)
        connection = replace(
            _connection_settings(settings),
            connection_kind=automation.connection.kind,
            connection_value=automation.connection.value,
        )
        return ResolvedAutomation(connection, tuple(await list_available_cameras(connection)))

    async def _cancel_command(self, request_id: str, request: Mapping[str, object]) -> None:
        target_id = _required_string(request, "target_id")
        target = self._tasks.get(target_id)
        if target is not None:
            target.cancel()
        _write_event({"id": request_id, "event": "complete", "cancelled_id": target_id})

    async def _graceful_shutdown(self, request_id: str) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._tasks.values() if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.coordinator.close()
        _write_event({"id": request_id, "event": "complete", "status": "shutdown"})

    def _start_automation(self, automation_id: str) -> None:
        request_id = f"automation-run:{automation_id}"
        task = self._tasks.get(request_id)
        if task is None or task.done():
            self._tasks[request_id] = asyncio.create_task(
                self.engine.run_forever(automation_id),
                name=request_id,
            )

    def _snapshot(self) -> dict[str, object]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "automations": [self._automation_payload(item) for item in self.registry.list()],
            "jobs": [
                {
                    "id": item.id,
                    "status": item.status,
                    "output": str(item.spec.output),
                    "automation_id": item.spec.automation_id,
                    "error": item.error,
                }
                for item in self.coordinator.jobs
            ],
        }

    @staticmethod
    def _automation_payload(automation: DailyAutomation) -> dict[str, object]:
        return {
            "id": automation.id,
            "name": automation.name,
            "status": automation.status,
            "cameras": [{"id": camera.id, "name": camera.name} for camera in automation.cameras],
            "profile_id": automation.connection.value,
            "speed": automation.speed,
            "output_directory": automation.output_directory,
            "timezone": automation.timezone,
            "next_day": automation.next_day.isoformat(),
            "last_error": automation.last_error,
        }

    @staticmethod
    def _native_automation_id(entity_id: str) -> str:
        import hashlib  # noqa: PLC0415

        return f"auto_{hashlib.sha256(entity_id.encode()).hexdigest()[:32]}"


async def _run_session_stdio(handshake: Mapping[str, object]) -> int:
    registry_value = handshake.get("registry_path")
    registry_path = (
        Path(registry_value).expanduser()  # noqa: ASYNC240 - startup path normalization before session reads
        if isinstance(registry_value, str) and registry_value
        else user_config_path("TimeLapse") / "native-automations.json"
    )
    supervisor = NativeSessionSupervisor(registry_path)
    try:
        with supervisor.registry.owner(endpoint="native-session"):
            supervisor.registry.load()
            try:
                await supervisor.handshake(handshake)
            except _ProtocolError:
                return 1
            if handshake.get("recovery_mode") == "quiescent":
                for automation in supervisor.registry.list():
                    if automation.status == "active":
                        supervisor.registry.pause(
                            automation.id,
                            "Native supervisor entered quiescent recovery after repeated crashes. Resume explicitly.",
                        )
            while not supervisor.stopping:
                try:
                    request = await asyncio.to_thread(_read_optional_request)
                    if request is None:
                        break
                    if not await supervisor.accept(request):
                        break
                except _ProtocolError as exc:
                    _write_event({"id": None, "event": "error", "code": exc.code, "message": str(exc)})
    finally:
        await supervisor.close()
    return 0


def _request_id(request: Mapping[str, object] | None) -> str | None:
    if request is None:
        return None
    value = request.get("id")
    return value if isinstance(value, str) else None


async def _run(request: Mapping[str, object]) -> int:
    started_at = perf_counter()
    request_id = _request_id(request)
    command = request.get("command")
    _LOGGER.info("Backend command started: command=%s, request_id=%s", command, request_id)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    cancel_watcher: asyncio.Task[None] | None = None
    if task is not None:
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
        cancel_path = _optional_string(request, "cancel_path")
        if cancel_path:
            cancel_watcher = asyncio.create_task(
                _cancel_when_requested(Path(cancel_path), task),
                name=f"cancel-{request_id}",
            )
    try:
        await _dispatch(request)
    except asyncio.CancelledError:
        _LOGGER.info(
            "Backend command cancelled: command=%s, request_id=%s, elapsed=%.2fs",
            command,
            request_id,
            perf_counter() - started_at,
        )
        _write_event({"id": _request_id(request), "event": "cancelled"})
        return 0
    except TimelapseError as exc:
        _LOGGER.log(
            logging.ERROR,
            "Backend command failed: command=%s, request_id=%s, elapsed=%.2fs, error=%s",
            command,
            request_id,
            perf_counter() - started_at,
            exc,
        )
        raise
    except Exception:
        _LOGGER.exception(
            "Backend command failed: command=%s, request_id=%s, elapsed=%.2fs",
            command,
            request_id,
            perf_counter() - started_at,
        )
        raise
    finally:
        if cancel_watcher is not None:
            cancel_watcher.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_watcher
        with suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGTERM)
    _LOGGER.info(
        "Backend command completed: command=%s, request_id=%s, elapsed=%.2fs",
        command,
        request_id,
        perf_counter() - started_at,
    )
    return 0


def main() -> int:
    """Run one-shot compatibility mode or a version-2 native session."""
    request: dict[str, object] | None = None
    log_handler: _NativeLogHandler | None = None
    package_logger = logging.getLogger("timelapse")
    previous_log_level = package_logger.level
    try:
        request = _read_request()
        log_handler = _NativeLogHandler(_request_id(request))
        package_logger.addHandler(log_handler)
        package_logger.setLevel(logging.INFO)
        if request.get("command") == "handshake":
            return asyncio.run(_run_session_stdio(request))
        return asyncio.run(_run_in_session(request))
    except _ProtocolError as exc:
        _write_event({"id": _request_id(request), "event": "error", "code": exc.code, "message": str(exc)})
    except ProtectRateLimitError as exc:
        _write_event(
            {
                "id": _request_id(request),
                "event": "error",
                "code": "protect_rate_limited",
                "message": str(exc),
            }
        )
    except TimelapseError as exc:
        _write_event({"id": _request_id(request), "event": "error", "code": "timelapse_error", "message": str(exc)})
    except KeyboardInterrupt:
        _write_event({"id": _request_id(request), "event": "cancelled"})
        return 0
    except Exception as exc:
        _write_event({"id": _request_id(request), "event": "error", "code": "internal_error", "message": str(exc)})
    finally:
        if log_handler is not None:
            package_logger.removeHandler(log_handler)
        package_logger.setLevel(previous_log_level)
    return 1


async def _run_in_session(request: Mapping[str, object]) -> int:
    """Run one native command with session persistence and scoped cleanup."""
    async with protect_session_scope():
        return await _run(request)


if __name__ == "__main__":
    raise SystemExit(main())

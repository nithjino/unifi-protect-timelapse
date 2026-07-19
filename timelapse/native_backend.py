"""JSON-lines bridge between native desktop UIs and Python export services."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from timelapse import TimelapseError
from timelapse.config import DEFAULT_MAX_DOWNLOAD_MIB, DEFAULT_REQUEST_TIMEOUT_SECONDS, SPEED_TO_FPS, Config
from timelapse.protect import CameraInfo
from timelapse.service import export_timelapse, list_available_cameras

if TYPE_CHECKING:
    from collections.abc import Mapping

    from timelapse.download import DownloadProgress

MAX_REQUEST_BYTES = 1024 * 1024
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
        instance_url=_required_string(settings, "instance_url").strip().rstrip("/"),
        token=_required_string(settings, "token"),
        username=_required_string(settings, "username"),
        password=_required_string(settings, "password"),
        verify_ssl=_boolean(settings, "verify_ssl", default=True),
        speed=speed,
        start=start,
        end=end,
        output=output,
        request_timeout_seconds=_nonnegative_integer(
            settings,
            "request_timeout_seconds",
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
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
    message = f"unsupported command: {command}"
    raise _ProtocolError(message, code="unsupported_command")


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
    if task is not None:
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
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
    """Read one native-UI request, run it, and emit JSON-line events."""
    request: dict[str, object] | None = None
    log_handler: _NativeLogHandler | None = None
    package_logger = logging.getLogger("timelapse")
    previous_log_level = package_logger.level
    try:
        request = _read_request()
        log_handler = _NativeLogHandler(_request_id(request))
        package_logger.addHandler(log_handler)
        package_logger.setLevel(logging.INFO)
        return asyncio.run(_run(request))
    except _ProtocolError as exc:
        _write_event({"id": _request_id(request), "event": "error", "code": exc.code, "message": str(exc)})
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


if __name__ == "__main__":
    raise SystemExit(main())

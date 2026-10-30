"""Secure, streaming timelapse downloads."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from timelapse import TimelapseError
from timelapse.config import SPEED_TO_FPS, Config
from timelapse.protect import CameraInfo, ProtectConnection, camera_id, camera_name, rate_limit_error

if TYPE_CHECKING:
    from datetime import datetime

    from aiohttp import ClientResponse
    from uiprotect import ProtectApiClient

MEBIBYTE = 1024 * 1024
KIBIBYTE = 1024
BYTE_UNITS = ("bytes", "KiB", "MiB", "GiB", "TiB")
CHUNK_SIZE = MEBIBYTE
MAX_ERROR_BODY_BYTES = 8 * 1024
MP4_PROBE_BYTES = 4 * 1024
MIN_MP4_FTYP_OFFSET = 4
PROGRESS_UPDATE_INTERVAL_SECONDS = 0.1
HTTP_OK = 200
HTTP_MULTIPLE_CHOICES = 300
HTTP_TOO_MANY_REQUESTS = 429
MAX_CAMERA_FILENAME_CHARACTERS = 48
CAMERA_ID_DIGEST_CHARACTERS = 12
STORAGE_SAFETY_RESERVE_BYTES = 1024 * MEBIBYTE
STORAGE_SAFETY_RESERVE_PERCENT = 5
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadProgress:
    """A point-in-time snapshot of a streaming download."""

    downloaded_bytes: int
    total_bytes: int | None
    bytes_per_second: float
    elapsed_seconds: float


ProgressCallback = Callable[[DownloadProgress], None]


def default_output_path(config: Config, camera: CameraInfo) -> Path:
    """Build a safe, descriptive output filename."""
    start_date = config.start.strftime("%Y_%m_%d")
    end_date = config.end.strftime("%Y_%m_%d")
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "_", camera_name(camera)).strip("._-") or "camera"
    safe_name = safe_name[:MAX_CAMERA_FILENAME_CHARACTERS].rstrip("._-") or "camera"
    camera_digest = hashlib.sha256(camera_id(camera).encode()).hexdigest()[:CAMERA_ID_DIGEST_CHARACTERS]
    if config.full_day:
        return Path(f"timelapse_{safe_name}_{start_date}_{end_date}_{config.speed}_{camera_digest}.mp4")
    start_time = config.start.strftime("%H_%M_%S")
    end_time = config.end.strftime("%H_%M_%S")
    return Path(
        f"timelapse_{safe_name}_{start_date}_{start_time}_{end_date}_{end_time}_{config.speed}__{camera_digest}.mp4"
    )


def _ensure_storage(directory: Path, expected_bytes: int | None) -> None:
    """Refuse an export that cannot fit while leaving a safety buffer free."""
    try:
        usage = shutil.disk_usage(directory)
    except OSError as exc:
        message = f"could not measure free export storage at {directory}: {exc}"
        raise TimelapseError(message) from exc
    safety_bytes = min(STORAGE_SAFETY_RESERVE_BYTES, usage.total * STORAGE_SAFETY_RESERVE_PERCENT // 100)
    available_bytes = max(usage.free - safety_bytes, 0)
    if available_bytes == 0 or (expected_bytes or 0) > available_bytes:
        description = (
            "an export of unknown size" if expected_bytes is None else f"the {format_bytes(expected_bytes)} export"
        )
        message = (
            f"not enough free storage for {description}: {format_bytes(available_bytes)} is available after "
            f"the {format_bytes(safety_bytes)} safety buffer"
        )
        raise TimelapseError(message)


async def download_timelapse(  # noqa: PLR0912, PLR0915 - one atomic streamed-download lifecycle
    config: Config,
    connection: ProtectConnection,
    client: ProtectApiClient,
    camera: CameraInfo,
    output: Path,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Stream a Protect timelapse export to an atomic temporary file."""
    downloaded = 0
    params = {
        "camera": camera_id(camera),
        "start": str(_js_time(config.start)),
        "end": str(_js_time(config.end)),
        "channel": "0",
    }
    timelapse_fps = SPEED_TO_FPS[config.speed]
    if timelapse_fps is not None:
        params.update(type="timelapse", fps=str(timelapse_fps))
    output.parent.mkdir(parents=True, exist_ok=True)

    # The caller owns the whole-operation deadline, so this request has none.
    response = await client.request(
        "get",
        connection.export_path,
        require_auth=True,
        auto_close=False,
        params=params,
        timeout=0,
    )
    total_header = response.headers.get("Content-Length")
    total_bytes = int(total_header) if total_header and total_header.isdigit() else None
    temp_output: Path | None = None
    try:
        if not HTTP_OK <= response.status < HTTP_MULTIPLE_CHOICES:
            if response.status == HTTP_TOO_MANY_REQUESTS:
                raise rate_limit_error(getattr(client, "rate_limit_retry_after", None))
            detail = await _read_error_detail(response)
            reason = _sanitize_terminal_text(response.reason or "unknown error")
            message = f"timelapse export failed with HTTP {response.status}: {detail or reason}"
            raise TimelapseError(message)

        content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().casefold()
        if content_type and content_type not in {"video/mp4", "application/mp4", "application/octet-stream"}:
            message = f"timelapse export returned unexpected content type: {content_type}"
            raise TimelapseError(message)

        max_bytes = config.max_download_mib * MEBIBYTE
        if max_bytes and total_bytes and total_bytes > max_bytes:
            message = (
                f"server reported a {total_bytes / MEBIBYTE:.1f} MiB export, "
                f"exceeding the {config.max_download_mib} MiB limit"
            )
            raise TimelapseError(message)

        _ensure_storage(output.parent, total_bytes)
        download_started_at = monotonic()
        _emit_progress(progress_callback, 0, total_bytes, download_started_at, download_started_at)

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".part",
            delete=False,
        ) as file:
            temp_output = Path(file.name)
            last_progress_update = download_started_at
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                downloaded += len(chunk)
                if max_bytes and downloaded > max_bytes:
                    message = f"download exceeded the {config.max_download_mib} MiB limit"
                    raise TimelapseError(message)
                file.write(chunk)
                now = monotonic()
                if now - last_progress_update >= PROGRESS_UPDATE_INTERVAL_SECONDS:
                    _emit_progress(progress_callback, downloaded, total_bytes, download_started_at, now)
                    last_progress_update = now

        if downloaded == 0:
            message = "timelapse export returned an empty response"
            raise TimelapseError(message)
        if not validate_mp4(temp_output):
            message = "timelapse export did not contain a valid MP4 file signature"
            raise TimelapseError(message)
        if output.exists():  # noqa: ASYNC240 - local metadata check immediately before the atomic replace
            message = f"refusing to overwrite existing output file: {output}"
            raise TimelapseError(message)
        temp_output.replace(output)
        _LOGGER.info("Export finalized: output=%s, size=%s", output, format_bytes(downloaded))
        _emit_progress(progress_callback, downloaded, total_bytes, download_started_at, monotonic())
    except OSError as exc:
        message = f"could not write {output}: {exc}"
        raise TimelapseError(message) from exc
    finally:
        _release_response(response)
        if temp_output is not None:
            _remove_temporary_output(temp_output)


def _release_response(response: ClientResponse) -> None:
    """Release the response without replacing the primary operation error."""
    try:
        response.release()
    except Exception:
        _LOGGER.warning("Could not release the Protect export response", exc_info=True)


def _remove_temporary_output(path: Path) -> None:
    """Remove a partial export without replacing the primary operation error."""
    try:
        if path.exists():
            _LOGGER.info("Removing temporary export file: %s", path)
        path.unlink(missing_ok=True)
    except OSError:
        _LOGGER.warning("Could not remove temporary export file: %s", path, exc_info=True)


def validate_mp4(path: Path) -> bool:
    """Return whether a path is a regular, nonempty MP4 with an ftyp marker."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            return False
        with path.open("rb") as file:
            header = file.read(MP4_PROBE_BYTES)
    except OSError:
        return False
    return header.find(b"ftyp", MIN_MP4_FTYP_OFFSET) >= MIN_MP4_FTYP_OFFSET


def _js_time(value: datetime) -> int:
    return int(value.timestamp() * 1000)


async def _read_error_detail(response: ClientResponse) -> str:
    raw = await response.content.read(MAX_ERROR_BODY_BYTES + 1)
    truncated = len(raw) > MAX_ERROR_BODY_BYTES
    detail = raw[:MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace").strip()
    safe_detail = _sanitize_terminal_text(detail)
    return f"{safe_detail}... (truncated)" if truncated else safe_detail


def _sanitize_terminal_text(value: str) -> str:
    return value.encode("unicode_escape", errors="backslashreplace").decode("ascii")


def format_bytes(value: int | None) -> str:
    """Return a human-readable size."""
    if value is None:
        return "unknown"
    size = float(value)
    for unit in BYTE_UNITS:
        if size < KIBIBYTE or unit == BYTE_UNITS[-1]:
            return f"{value} {unit}" if unit == BYTE_UNITS[0] else f"{size:.1f} {unit}"
        size /= KIBIBYTE
    return f"{value} bytes"


def _emit_progress(
    callback: ProgressCallback | None,
    downloaded_bytes: int,
    total_bytes: int | None,
    started_at: float,
    now: float,
) -> None:
    if callback is None:
        return
    elapsed_seconds = max(now - started_at, 0.0)
    bytes_per_second = downloaded_bytes / elapsed_seconds if elapsed_seconds else 0.0
    callback(
        DownloadProgress(
            downloaded_bytes=downloaded_bytes,
            total_bytes=total_bytes,
            bytes_per_second=bytes_per_second,
            elapsed_seconds=elapsed_seconds,
        )
    )

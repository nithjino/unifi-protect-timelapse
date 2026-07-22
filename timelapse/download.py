"""Secure, streaming timelapse downloads."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, perf_counter
from typing import TYPE_CHECKING

from timelapse import TimelapseError
from timelapse.config import SPEED_TO_FPS, Config
from timelapse.protect import CameraInfo, ProtectConnection, camera_id, camera_name

if TYPE_CHECKING:
    from datetime import datetime

    from aiohttp import ClientResponse
    from uiprotect import ProtectApiClient

MEBIBYTE = 1024 * 1024
KIBIBYTE = 1024
CHUNK_SIZE = MEBIBYTE
MAX_ERROR_BODY_BYTES = 8 * 1024
MP4_PROBE_BYTES = 4 * 1024
MIN_MP4_FTYP_OFFSET = 4
PROGRESS_UPDATE_INTERVAL_SECONDS = 0.1
HTTP_OK = 200
HTTP_MULTIPLE_CHOICES = 300
MAX_CAMERA_FILENAME_CHARACTERS = 48
CAMERA_ID_DIGEST_CHARACTERS = 12
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
    start = config.start.strftime("%Y%m%d_%H%M%S")
    end = config.end.strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "_", camera_name(camera)).strip("._-") or "camera"
    safe_name = safe_name[:MAX_CAMERA_FILENAME_CHARACTERS].rstrip("._-") or "camera"
    camera_digest = hashlib.sha256(camera_id(camera).encode()).hexdigest()[:CAMERA_ID_DIGEST_CHARACTERS]
    return Path(f"timelapse_{safe_name}_{camera_digest}_{start}_{end}_{config.speed}.mp4")


async def download_timelapse(  # noqa: PLR0912, PLR0915 - one atomic streamed-download lifecycle
    config: Config,
    connection: ProtectConnection,
    client: ProtectApiClient,
    camera: CameraInfo,
    output: Path,
    progress_callback: ProgressCallback | None = None,
    *,
    request_timeout_seconds: float | None = None,
) -> None:
    """Stream a Protect timelapse export to an atomic temporary file."""
    operation_started_at = perf_counter()
    downloaded = 0
    params = {
        "camera": camera_id(camera),
        "start": str(_js_time(config.start)),
        "end": str(_js_time(config.end)),
        "channel": "0",
        "type": "timelapse",
        "fps": str(SPEED_TO_FPS[config.speed]),
    }
    client.set_header("Accept", "video/mp4,application/octet-stream,*/*")
    output.parent.mkdir(parents=True, exist_ok=True)

    _LOGGER.info(
        "Requesting Protect video export: target=%s:%d%s, camera_id=%s, fps=%s, request_timeout=%s, download_limit=%s",
        connection.host,
        connection.port,
        connection.export_path,
        params["camera"],
        params["fps"],
        _format_timeout(config.request_timeout_seconds),
        _format_limit(config.max_download_mib),
    )
    request_started_at = perf_counter()
    effective_request_timeout = (
        config.request_timeout_seconds if request_timeout_seconds is None else request_timeout_seconds
    )
    try:
        response = await client.request(
            "get",
            connection.export_path,
            require_auth=True,
            auto_close=False,
            params=params,
            timeout=effective_request_timeout or 0,
        )
    except asyncio.CancelledError:
        _LOGGER.info("Protect video export request cancelled after %.2fs", perf_counter() - request_started_at)
        raise
    except Exception:
        _LOGGER.exception("Protect video export request failed after %.2fs", perf_counter() - request_started_at)
        raise

    response_received_at = perf_counter()
    total_header = response.headers.get("Content-Length")
    total_bytes = int(total_header) if total_header and total_header.isdigit() else None
    _LOGGER.info(
        "Protect video export response received: status=%d, server_wait=%.2fs, expected_size=%s",
        response.status,
        response_received_at - request_started_at,
        _format_bytes(total_bytes),
    )
    temp_output: Path | None = None
    try:
        if not HTTP_OK <= response.status < HTTP_MULTIPLE_CHOICES:
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

        download_started_at = monotonic()
        stream_started_at = perf_counter()
        first_chunk_received = False
        _emit_progress(progress_callback, 0, total_bytes, download_started_at, download_started_at)

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".part",
            delete=False,
        ) as file:
            temp_output = Path(file.name)
            _LOGGER.info("Streaming export to temporary file: %s", temp_output)
            last_progress_update = download_started_at
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                if not first_chunk_received:
                    first_chunk_received = True
                    _LOGGER.info(
                        "First export bytes received: response_body_wait=%.2fs, first_chunk=%s",
                        perf_counter() - response_received_at,
                        _format_bytes(len(chunk)),
                    )
                next_downloaded = downloaded + len(chunk)
                if max_bytes and next_downloaded > max_bytes:
                    message = f"download exceeded the {config.max_download_mib} MiB limit"
                    raise TimelapseError(message)
                file.write(chunk)
                downloaded = next_downloaded
                now = monotonic()
                if now - last_progress_update >= PROGRESS_UPDATE_INTERVAL_SECONDS:
                    _emit_progress(progress_callback, downloaded, total_bytes, download_started_at, now)
                    last_progress_update = now

        stream_elapsed = perf_counter() - stream_started_at
        _LOGGER.info(
            "Export stream completed: downloaded=%s, stream_elapsed=%.2fs, average_speed=%s/s",
            _format_bytes(downloaded),
            stream_elapsed,
            _format_bytes(round(downloaded / stream_elapsed) if stream_elapsed else 0),
        )

        _validate_mp4(temp_output, downloaded)

        if output.exists():  # noqa: ASYNC240 - local metadata check immediately before the atomic replace
            message = f"refusing to overwrite existing output file: {output}"
            raise TimelapseError(message)
        temp_output.replace(output)
        _LOGGER.info(
            "Export finalized atomically: output=%s, total_elapsed=%.2fs",
            output,
            perf_counter() - operation_started_at,
        )
        _emit_progress(progress_callback, downloaded, total_bytes, download_started_at, monotonic())
    except asyncio.CancelledError:
        _LOGGER.info(
            "Export download cancelled: downloaded=%s, elapsed=%.2fs",
            _format_bytes(downloaded),
            perf_counter() - operation_started_at,
        )
        raise
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


def _validate_mp4(path: Path, downloaded_bytes: int) -> None:
    if downloaded_bytes == 0:
        message = "timelapse export returned an empty response"
        raise TimelapseError(message)
    with path.open("rb") as file:
        header = file.read(MP4_PROBE_BYTES)
    marker = header.find(b"ftyp")
    if marker < MIN_MP4_FTYP_OFFSET:
        message = "timelapse export did not contain a valid MP4 file signature"
        raise TimelapseError(message)


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


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value < KIBIBYTE:
        return f"{value} bytes"
    if value < MEBIBYTE:
        return f"{value / KIBIBYTE:.1f} KiB"
    return f"{value / MEBIBYTE:.1f} MiB"


def _format_timeout(seconds: int) -> str:
    return "disabled" if seconds == 0 else f"{seconds}s"


def _format_limit(mebibytes: int) -> str:
    return "disabled" if mebibytes == 0 else f"{mebibytes} MiB"


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

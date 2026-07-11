"""Secure, streaming timelapse downloads."""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from timelapse import TimelapseError
from timelapse.config import SPEED_TO_FPS, Config
from timelapse.protect import ProtectConnection, camera_id, camera_name

if TYPE_CHECKING:
    from datetime import datetime

    from aiohttp import ClientResponse
    from uiprotect import ProtectApiClient
    from uiprotect.data import PublicCamera

MEBIBYTE = 1024 * 1024
CHUNK_SIZE = MEBIBYTE
MAX_ERROR_BODY_BYTES = 8 * 1024
PROGRESS_UPDATE_INTERVAL_SECONDS = 0.1
HTTP_OK = 200
HTTP_MULTIPLE_CHOICES = 300


def default_output_path(config: Config, camera: PublicCamera) -> Path:
    """Build a safe, descriptive output filename."""
    start = config.start.strftime("%Y%m%d_%H%M%S")
    end = config.end.strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", camera_name(camera)).strip("._-") or "camera"
    return Path(f"timelapse_{safe_name}_{start}_{end}_{config.speed}.mp4")


async def download_timelapse(
    config: Config,
    connection: ProtectConnection,
    client: ProtectApiClient,
    camera: PublicCamera,
    output: Path,
) -> None:
    """Stream a Protect timelapse export to an atomic temporary file."""
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

    _write_stdout(f"Requesting {config.speed} timelapse export for {camera_name(camera)}...\n")
    response = await client.request(
        "get",
        connection.export_path,
        require_auth=True,
        auto_close=False,
        params=params,
        timeout=config.request_timeout_seconds or 0,
    )
    temp_output: Path | None = None

    try:
        if not HTTP_OK <= response.status < HTTP_MULTIPLE_CHOICES:
            detail = await _read_error_detail(response)
            reason = _sanitize_terminal_text(response.reason or "unknown error")
            message = f"timelapse export failed with HTTP {response.status}: {detail or reason}"
            raise TimelapseError(message)

        total_header = response.headers.get("Content-Length")
        total_bytes = int(total_header) if total_header and total_header.isdigit() else None
        max_bytes = config.max_download_mib * MEBIBYTE
        if max_bytes and total_bytes and total_bytes > max_bytes:
            message = (
                f"server reported a {total_bytes / MEBIBYTE:.1f} MiB export, "
                f"exceeding the {config.max_download_mib} MiB limit"
            )
            raise TimelapseError(message)

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".part",
            delete=False,
        ) as file:
            temp_output = Path(file.name)
            downloaded = 0
            last_progress_update = 0.0
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                next_downloaded = downloaded + len(chunk)
                if max_bytes and next_downloaded > max_bytes:
                    message = f"download exceeded the {config.max_download_mib} MiB limit"
                    raise TimelapseError(message)
                file.write(chunk)
                downloaded = next_downloaded
                now = monotonic()
                if now - last_progress_update >= PROGRESS_UPDATE_INTERVAL_SECONDS:
                    _print_progress(downloaded, total_bytes)
                    last_progress_update = now

        _print_progress(downloaded, total_bytes)
        temp_output.replace(output)
    except OSError as exc:
        message = f"could not write {output}: {exc}"
        raise TimelapseError(message) from exc
    finally:
        response.release()
        if temp_output is not None:
            temp_output.unlink(missing_ok=True)
        _write_stdout("\n")


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


def _print_progress(downloaded: int, total: int | None) -> None:
    if total:
        percent = min(downloaded / total * 100, 100.0)
        _write_stdout(f"\rDownloaded {downloaded / MEBIBYTE:.1f} MiB ({percent:.1f}%)")
    else:
        _write_stdout(f"\rDownloaded {downloaded / MEBIBYTE:.1f} MiB")


def _write_stdout(message: str) -> None:
    sys.stdout.write(message)
    sys.stdout.flush()

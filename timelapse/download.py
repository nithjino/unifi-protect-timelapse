"""Stream one HTTP export response to an atomic file."""

from __future__ import annotations

import logging
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from timelapse import RateLimitError, TimelapseError
from timelapse.model import format_bytes

LOGGER = logging.getLogger(__name__)
CHUNK_BYTES = 1024 * 1024
GIB = 1024**3
MP4_TYPES = {"video/mp4", "application/mp4", "application/octet-stream"}
PROGRESS_INTERVAL = 0.1


@dataclass(frozen=True)
class Progress:
    """Download progress for one export."""

    downloaded_bytes: int
    total_bytes: int | None
    bytes_per_second: float
    elapsed_seconds: float


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header given in seconds or as an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def is_valid_mp4(path: Path) -> bool:
    """Return whether ``path`` is a regular, non-empty file with an MP4 signature."""
    try:
        if not path.is_file():
            return False
        with path.open("rb") as file:
            return file.read(4096).find(b"ftyp", 4) != -1
    except OSError:
        return False


def _check_free_space(directory: Path, total: int | None) -> None:
    usage = shutil.disk_usage(directory)
    reserve = min(GIB, usage.total // 20)
    available = max(0, usage.free - reserve)
    if available == 0 or (total is not None and total > available):
        size = f"the {format_bytes(total)} export" if total is not None else "an export of unknown size"
        raise TimelapseError(
            f"not enough free storage for {size}: {format_bytes(available)} is available "
            f"after the {format_bytes(reserve)} safety buffer"
        )


async def _check_response(response: Any, max_bytes: int) -> int | None:
    status = response.status
    if status == 429:
        raise RateLimitError(retry_after_seconds(response.headers.get("Retry-After")))
    if not 200 <= status < 300:
        body = await response.content.read(8192)
        text = body.decode(errors="replace").strip()
        detail = text.encode("unicode_escape").decode() if text else response.reason
        raise TimelapseError(f"timelapse export failed with HTTP {status}: {detail}")
    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type and content_type not in MP4_TYPES:
        raise TimelapseError(f"unexpected content type from UniFi Protect: {content_type}")
    length = (response.headers.get("Content-Length") or "").strip()
    total = int(length) if length.isdigit() else None
    if max_bytes and total is not None and total > max_bytes:
        raise TimelapseError(f"the export is {format_bytes(total)}, above the {format_bytes(max_bytes)} limit")
    return total


async def save_export(
    response: Any,
    output: Path,
    *,
    max_bytes: int,
    on_progress: Callable[[Progress], None] | None = None,
) -> None:
    """Stream ``response`` into ``output`` through a temporary ``.part`` file."""
    part = output.with_name(f".{output.name}.{secrets.token_hex(4)}.part")
    started = time.monotonic()
    downloaded = 0
    total: int | None = None

    def report() -> None:
        elapsed = time.monotonic() - started
        if on_progress is not None:
            on_progress(Progress(downloaded, total, downloaded / elapsed if elapsed > 0 else 0.0, elapsed))

    try:
        if output.exists():
            raise TimelapseError(f"refusing to overwrite existing output file: {output}")
        total = await _check_response(response, max_bytes)
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            _check_free_space(output.parent, total)
            report()
            head = b""
            last_report = time.monotonic()
            with part.open("xb") as file:
                async for chunk in response.content.iter_chunked(CHUNK_BYTES):
                    downloaded += len(chunk)
                    if max_bytes and downloaded > max_bytes:
                        raise TimelapseError(f"the export grew past the {format_bytes(max_bytes)} limit")
                    if len(head) < 4096:
                        head += chunk[:4096]
                    file.write(chunk)
                    if time.monotonic() - last_report >= PROGRESS_INTERVAL:
                        last_report = time.monotonic()
                        report()
            if downloaded == 0:
                raise TimelapseError("UniFi Protect returned an empty response")
            if head[:4096].find(b"ftyp", 4) == -1:
                raise TimelapseError("UniFi Protect did not return a valid MP4 file signature")
            if output.exists():
                raise TimelapseError(f"refusing to overwrite existing output file: {output}")
            part.rename(output)
        except OSError as error:
            raise TimelapseError(f"could not write {output}: {error}") from error
        report()
    finally:
        try:
            response.release()
        except Exception:
            LOGGER.exception("Could not release the export response")
        try:
            part.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Could not remove %s", part)

"""The web export folder: usage, quota, retention and Older Exports."""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from timelapse.model import format_bytes

LOGGER = logging.getLogger(__name__)
MIB = 1024 * 1024


@dataclass(frozen=True)
class VideoFile:
    """A top-level MP4 in the export folder."""

    name: str
    size: int
    modified: datetime


@dataclass(frozen=True)
class HostStorage:
    """Free space on the export folder's filesystem."""

    free_bytes: int | None = None
    total_bytes: int | None = None

    @property
    def free_percent(self) -> float | None:
        """Return the free share in percent, when measured."""
        if not self.total_bytes or self.free_bytes is None:
            return None
        return self.free_bytes * 100 / self.total_bytes

    @property
    def low(self) -> bool:
        """Return whether less than 20 % is free."""
        return self.free_percent is not None and self.free_percent < 20


def ensure_writable(directory: Path) -> None:
    """Create ``directory`` and prove it can be written."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write-test-{secrets.token_hex(4)}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        raise RuntimeError(
            f"{directory} is not writable ({error}). Make it writable by the server user, or set "
            "TIMELAPSE_UID and TIMELAPSE_GID to the owner of the data directory and rebuild the image."
        ) from error


class ExportFolder:
    """The output directory is the truth about artifacts."""

    def __init__(self, path: Path, *, quota_mib: int, retention_days: int) -> None:
        self.path = path
        self.quota_bytes = quota_mib * MIB
        self.retention_days = retention_days

    def usage_bytes(self) -> int:
        """Return the size of every file under the folder; unreadable directories raise ``OSError``."""

        def fail(error: OSError) -> None:
            raise error

        total = 0
        for root, _, files in os.walk(self.path, onerror=fail):
            for name in files:
                with contextlib.suppress(FileNotFoundError):
                    total += Path(root, name).lstat().st_size
        return total

    def videos(self) -> list[VideoFile]:
        """Return top-level MP4s that are not hidden, newest first."""
        videos = []
        for path in self.path.glob("*.mp4"):
            if path.name.startswith(".") or not path.is_file():
                continue
            stat = path.stat()
            videos.append(VideoFile(path.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime, UTC)))
        return sorted(videos, key=lambda video: video.modified, reverse=True)

    def prune(self, now: datetime, keep: set[str]) -> set[str]:
        """Delete videos older than the retention limit, except ``keep``; return the removed names."""
        if self.retention_days <= 0:
            return set()
        cutoff = now - timedelta(days=self.retention_days)
        removed = set()
        for video in self.videos():
            if video.name not in keep and video.modified < cutoff:
                try:
                    (self.path / video.name).unlink()
                except OSError:
                    LOGGER.exception("Could not delete expired export %s", video.name)
                    continue
                removed.add(video.name)
        return removed

    def full_problem(self) -> str | None:
        """Explain why a daily export cannot start, or return None."""
        try:
            usage = self.usage_bytes()
        except OSError as error:
            return f"Could not check export storage: {error}"
        if usage < self.quota_bytes:
            return None
        return (
            f"Export storage is full ({format_bytes(usage)} of {format_bytes(self.quota_bytes)}). Delete old exports, "
            "or raise TIMELAPSE_WEB_STORAGE_QUOTA_MIB or set TIMELAPSE_WEB_RETENTION_DAYS, then Resume this automation."
        )

    def host_storage(self) -> HostStorage:
        """Measure the filesystem that holds the folder."""
        try:
            usage = shutil.disk_usage(self.path)
        except OSError:
            return HostStorage()
        return HostStorage(usage.free, usage.total)

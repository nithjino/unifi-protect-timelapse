"""Values shared by every part of TimeLapse."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SPEEDS: dict[str, int | None] = {"1x": None, "60x": 4, "120x": 8, "300x": 20, "600x": 40}
DEFAULT_SPEED = "600x"
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\s]+')


@dataclass(frozen=True)
class Connection:
    """How to reach one UniFi Protect console."""

    url: str
    token: str = field(repr=False)
    username: str
    password: str = field(repr=False)
    verify_ssl: bool = True
    timeout_seconds: float = 0
    max_download_mib: int = 10240


@dataclass(frozen=True)
class Camera:
    """A camera as listed by Protect."""

    id: str
    name: str
    state: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class ExportRequest:
    """One camera over one time range at one speed."""

    camera: Camera
    start: datetime
    end: datetime
    speed: str
    full_day: bool = False

    @property
    def filename(self) -> str:
        """Return the Export Artifact filename, which identifies existing artifacts."""
        safe = _UNSAFE.sub("_", self.camera.name).strip("._-") or "camera"
        safe = safe[:48].rstrip("._-") or "camera"
        digest = hashlib.sha256(self.camera.id.encode()).hexdigest()[:12]
        start, end, speed = self.start, self.end, self.speed
        if self.full_day:
            return f"timelapse_{safe}_{start:%Y_%m_%d}_{end:%Y_%m_%d}_{speed}_{digest}.mp4"
        return f"timelapse_{safe}_{start:%Y_%m_%d_%H_%M_%S}_{end:%Y_%m_%d_%H_%M_%S}_{speed}__{digest}.mp4"


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Return the local midnights that start and end ``day``."""
    return datetime.combine(day, time(), tz), datetime.combine(day + timedelta(days=1), time(), tz)


def latest_complete_day(now: datetime, tz: ZoneInfo) -> date:
    """Return yesterday in ``tz``."""
    return now.astimezone(tz).date() - timedelta(days=1)


def format_bytes(n: float | None) -> str:
    """Format a byte count for people."""
    if n is None:
        return "unknown"
    if n < 1024:
        return f"{int(n)} bytes"
    value = float(n)
    for unit in ("KiB", "MiB", "GiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value / 1024:.1f} TiB"


def write_json(path: Path, data: Any, *, ensure_ascii: bool = True) -> None:
    """Write JSON atomically: a temporary file in the same directory, fsync, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, sort_keys=True, ensure_ascii=ensure_ascii)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

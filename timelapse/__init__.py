"""UniFi Protect timelapse exporter."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

try:
    __version__ = version("timelapse")
except PackageNotFoundError:
    __version__ = "0+unknown"


class TimelapseError(RuntimeError):
    """Raised when a timelapse export cannot be completed."""


class OperationTimeoutError(TimelapseError):
    """Raised when a complete Protect operation exceeds its deadline."""


class ProtectRateLimitError(TimelapseError):
    """Raised after bounded retries cannot clear a Protect HTTP 429 response."""

    def __init__(self, message: str, *, retry_not_before: datetime | None = None) -> None:
        """Keep the structured UTC retry floor with the display message."""
        super().__init__(message)
        self.retry_not_before = retry_not_before

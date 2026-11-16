"""Export UniFi Protect recordings as MP4 timelapses."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

__version__ = "4.0.0"


class TimelapseError(RuntimeError):
    """Protect or export failure."""


class OperationTimeoutError(TimelapseError):
    """The whole-operation deadline passed."""


class RateLimitError(TimelapseError):
    """UniFi Protect kept answering HTTP 429."""

    def __init__(self, wait_seconds: float | None = None, *, now: datetime | None = None) -> None:
        if wait_seconds is None:
            middle = "Let the console's authentication rate-limit window clear before trying again."
            self.retry_not_before: datetime | None = None
        else:
            middle = f"Wait at least {math.ceil(wait_seconds)} seconds before trying again."
            self.retry_not_before = (now or datetime.now(UTC)) + timedelta(seconds=wait_seconds)
        super().__init__(
            f"UniFi Protect returned HTTP 429 after bounded retries. {middle} "
            "TimeLapse will reuse the saved session; do not rotate accounts or start repeated copies."
        )


class InvalidRequestError(ValueError):
    """Bad user input or definition."""


class NotFoundError(InvalidRequestError):
    """Unknown job, automation or file."""


class ConflictError(InvalidRequestError):
    """The artifact exists or is in flight."""


class CapacityError(InvalidRequestError):
    """The queue is full or the quota is exhausted."""

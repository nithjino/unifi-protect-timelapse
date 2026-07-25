"""UniFi Protect timelapse exporter."""


class TimelapseError(RuntimeError):
    """Raised when a timelapse export cannot be completed."""


class OperationTimeoutError(TimelapseError):
    """Raised when a complete Protect operation exceeds its deadline."""


class ProtectRateLimitError(TimelapseError):
    """Raised after bounded retries cannot clear a Protect HTTP 429 response."""

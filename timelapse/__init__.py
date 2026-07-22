"""UniFi Protect timelapse exporter."""


class TimelapseError(RuntimeError):
    """Raised when a timelapse export cannot be completed."""


class OperationTimeoutError(TimelapseError):
    """Raised when a complete Protect operation exceeds its deadline."""

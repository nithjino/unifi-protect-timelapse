"""Command-line and environment configuration."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import strptime

from dotenv import load_dotenv

DATE_FORMAT = "%m-%d-%Y-%H-%M-%S"
DATE_ONLY_FORMAT = "%m-%d-%Y"
SPEED_TO_FPS = {"60x": 4, "120x": 8, "300x": 20, "600x": 40}
DEFAULT_REQUEST_TIMEOUT_SECONDS = 0
DEFAULT_MAX_DOWNLOAD_MIB = 10 * 1024


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration."""

    instance_url: str
    token: str
    username: str
    password: str
    verify_ssl: bool
    speed: str
    start: datetime
    end: datetime
    output: Path | None
    request_timeout_seconds: int
    max_download_mib: int
    daily: bool = False


@dataclass(frozen=True)
class _ParsedDate:
    value: datetime
    date_only: bool


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    message = "expected true or false"
    raise argparse.ArgumentTypeError(message)


def _parse_nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        message = "expected a whole number"
        raise argparse.ArgumentTypeError(message) from exc
    if parsed < 0:
        message = "expected zero or a positive whole number"
        raise argparse.ArgumentTypeError(message)
    return parsed


def _parse_date(value: str) -> _ParsedDate:
    parsed = None
    date_only = False
    for date_format, is_date_only in ((DATE_FORMAT, False), (DATE_ONLY_FORMAT, True)):
        try:
            parsed = strptime(value, date_format)
            date_only = is_date_only
            break
        except ValueError:
            continue
    if parsed is None:
        message = f"expected MM-DD-YYYY or MM-DD-YYYY-HH-MM-SS, got {value!r}"
        raise argparse.ArgumentTypeError(message)

    # Calling astimezone on the requested local wall time applies the correct
    # local UTC offset for that date, including daylight-saving transitions.
    return _ParsedDate(
        value=datetime(
            parsed.tm_year,
            parsed.tm_mon,
            parsed.tm_mday,
            parsed.tm_hour,
            parsed.tm_min,
            parsed.tm_sec,
        ).astimezone(),
        date_only=date_only,
    )


def _full_local_day(value: datetime) -> tuple[datetime, datetime]:
    start = datetime(value.year, value.month, value.day).astimezone()
    next_date = value.date() + timedelta(days=1)
    end = datetime(next_date.year, next_date.month, next_date.day).astimezone()
    return start, end


def _date_range(
    parser: argparse.ArgumentParser,
    start: _ParsedDate | None,
    end: _ParsedDate | None,
    *,
    daily: bool,
) -> tuple[datetime, datetime]:
    if daily:
        if start is not None or end is not None:
            parser.error("--daily cannot be combined with --start-date or --end-date")
        now = datetime.now().astimezone()
        return now, now + timedelta(seconds=1)
    if start is None and end is None:
        parser.error("provide --start-date, --end-date, or --daily")
    if start is not None and end is None:
        return _full_local_day(start.value) if start.date_only else (start.value, start.value + timedelta(days=1))
    if start is None and end is not None:
        return _full_local_day(end.value) if end.date_only else (end.value - timedelta(days=1), end.value)
    if start is None or end is None:
        message = "unreachable missing date boundary"
        raise AssertionError(message)
    if start.date_only and end.date_only and start.value.date() == end.value.date():
        return _full_local_day(start.value)
    return start.value, end.value


def parse_args() -> Config:
    """Load .env defaults and parse the command line."""
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

    parser = argparse.ArgumentParser(description="Create a timelapse MP4 from UniFi Protect recordings.")
    parser.add_argument(
        "--speed",
        choices=tuple(SPEED_TO_FPS),
        default="600x",
        help="timelapse speed; defaults to 600x",
    )
    parser.add_argument(
        "--instance",
        default=os.environ.get("UNIFI_PROTECT_URL"),
        help="Protect Integration API URL; defaults to UNIFI_PROTECT_URL",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("UNIFI_PROTECT_TOKEN"),
        help="Protect API token; defaults to UNIFI_PROTECT_TOKEN",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("UNIFI_PROTECT_USERNAME"),
        help="local Protect username; defaults to UNIFI_PROTECT_USERNAME",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("UNIFI_PROTECT_PASSWORD"),
        help="local Protect password; defaults to UNIFI_PROTECT_PASSWORD",
    )
    parser.add_argument(
        "--verify-ssl",
        type=_parse_bool,
        default=os.environ.get("UNIFI_PROTECT_VERIFY_SSL", "true"),
        help="verify TLS certificates; defaults to UNIFI_PROTECT_VERIFY_SSL or true",
    )
    parser.add_argument(
        "--start-date",
        type=_parse_date,
        help="start in MM-DD-YYYY or MM-DD-YYYY-HH-MM-SS format; one boundary means a 24-hour export",
    )
    parser.add_argument(
        "--end-date",
        type=_parse_date,
        help="end in MM-DD-YYYY or MM-DD-YYYY-HH-MM-SS format; one boundary means a 24-hour export",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="export the latest completed local day, then keep running and export each completed day",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=os.environ.get("TIMELAPSE_OUTPUT"),
        help="output MP4 path, or destination directory with --daily; defaults to TIMELAPSE_OUTPUT",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=_parse_nonnegative_int,
        default=os.environ.get("TIMELAPSE_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_REQUEST_TIMEOUT_SECONDS)),
        help="request timeout; defaults to TIMELAPSE_REQUEST_TIMEOUT_SECONDS or 0 (disabled)",
    )
    parser.add_argument(
        "--max-download-mib",
        type=_parse_nonnegative_int,
        default=os.environ.get("TIMELAPSE_MAX_DOWNLOAD_MIB", str(DEFAULT_MAX_DOWNLOAD_MIB)),
        help="maximum download size; defaults to TIMELAPSE_MAX_DOWNLOAD_MIB or 10240 (0 disables)",
    )
    args = parser.parse_args()

    if not args.instance:
        parser.error("--instance is required when UNIFI_PROTECT_URL is not set")
    if not args.token:
        parser.error("--token is required when UNIFI_PROTECT_TOKEN is not set")
    if not args.username:
        parser.error("--username is required when UNIFI_PROTECT_USERNAME is not set")
    if not args.password:
        parser.error("--password is required when UNIFI_PROTECT_PASSWORD is not set")
    start, end = _date_range(parser, args.start_date, args.end_date, daily=args.daily)
    if end <= start:
        parser.error("--end-date must be after --start-date")

    instance_url = args.instance.strip().rstrip("/")
    if not instance_url:
        parser.error("--instance cannot be empty")

    return Config(
        instance_url=instance_url,
        token=args.token,
        username=args.username,
        password=args.password,
        verify_ssl=args.verify_ssl,
        speed=args.speed,
        start=start,
        end=end,
        output=args.output,
        request_timeout_seconds=args.request_timeout_seconds,
        max_download_mib=args.max_download_mib,
        daily=args.daily,
    )

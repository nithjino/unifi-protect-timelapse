"""Command-line and environment configuration."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import strptime

from dotenv import load_dotenv

DATE_FORMAT = "%m-%d-%Y-%H-%M-%S"
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


def _parse_date(value: str) -> datetime:
    try:
        parsed = strptime(value, DATE_FORMAT)
    except ValueError as exc:
        message = f"expected date format MM-DD-YYYY-HH-MM-SS, got {value!r}"
        raise argparse.ArgumentTypeError(message) from exc

    # Calling astimezone on the requested local wall time applies the correct
    # local UTC offset for that date, including daylight-saving transitions.
    return datetime(
        parsed.tm_year,
        parsed.tm_mon,
        parsed.tm_mday,
        parsed.tm_hour,
        parsed.tm_min,
        parsed.tm_sec,
    ).astimezone()


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
        required=True,
        help="start date in MM-DD-YYYY-HH-MM-SS format",
    )
    parser.add_argument(
        "--end-date",
        type=_parse_date,
        required=True,
        help="end date in MM-DD-YYYY-HH-MM-SS format",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=os.environ.get("TIMELAPSE_OUTPUT"),
        help="output MP4 path; defaults to TIMELAPSE_OUTPUT or a generated filename",
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
    if args.end_date <= args.start_date:
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
        start=args.start_date,
        end=args.end_date,
        output=args.output,
        request_timeout_seconds=args.request_timeout_seconds,
        max_download_mib=args.max_download_mib,
    )

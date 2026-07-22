"""Command-line and environment configuration."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import strptime
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from timelapse.profiles import ConnectionProfile

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
class CreateProfile:
    """Request to start the interactive profile-creation flow."""

    instance_url: str | None
    token: str | None
    username: str | None
    password: str | None
    verify_ssl: bool | None


@dataclass(frozen=True)
class _ConnectionValues:
    instance_url: str
    token: str
    username: str
    password: str
    verify_ssl: bool


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


def _prompt_required(label: str, *, secret: bool = False) -> str:
    while True:
        value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
        if value.strip():
            return value
        sys.stdout.write(f"{label} is required.\n")
        sys.stdout.flush()


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


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a timelapse MP4 from UniFi Protect recordings.")
    profile_group = parser.add_mutually_exclusive_group()
    profile_group.add_argument(
        "--create-profile",
        action="store_true",
        help="interactively create a named connection profile and exit",
    )
    profile_group.add_argument(
        "--profile",
        metavar="NAME",
        help="load connection details from a named profile",
    )
    parser.add_argument(
        "--speed",
        choices=tuple(SPEED_TO_FPS),
        default="600x",
        help="timelapse speed; defaults to 600x",
    )
    parser.add_argument(
        "--instance",
        help="Protect Integration API URL; overrides a profile or UNIFI_PROTECT_URL",
    )
    parser.add_argument(
        "--token",
        help="Protect API token; overrides a profile or UNIFI_PROTECT_TOKEN",
    )
    parser.add_argument(
        "--username",
        help="local Protect username; overrides a profile or UNIFI_PROTECT_USERNAME",
    )
    parser.add_argument(
        "--password",
        help="local Protect password; overrides a profile or UNIFI_PROTECT_PASSWORD",
    )
    parser.add_argument(
        "--verify-ssl",
        type=_parse_bool,
        help="verify TLS certificates; overrides a profile or UNIFI_PROTECT_VERIFY_SSL",
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
        help="whole-operation deadline; defaults to TIMELAPSE_REQUEST_TIMEOUT_SECONDS or 0 (disabled)",
    )
    parser.add_argument(
        "--max-download-mib",
        type=_parse_nonnegative_int,
        default=os.environ.get("TIMELAPSE_MAX_DOWNLOAD_MIB", str(DEFAULT_MAX_DOWNLOAD_MIB)),
        help="maximum download size; defaults to TIMELAPSE_MAX_DOWNLOAD_MIB or 10240 (0 disables)",
    )
    return parser


def _environment_verify_ssl(parser: argparse.ArgumentParser, *, default: bool | None) -> bool | None:
    value = os.environ.get("UNIFI_PROTECT_VERIFY_SSL")
    if value is None:
        return default
    try:
        return _parse_bool(value)
    except argparse.ArgumentTypeError as exc:
        parser.error(f"UNIFI_PROTECT_VERIFY_SSL: {exc}")


def _create_profile_request(parser: argparse.ArgumentParser) -> CreateProfile:
    return CreateProfile(
        instance_url=os.environ.get("UNIFI_PROTECT_URL"),
        token=os.environ.get("UNIFI_PROTECT_TOKEN"),
        username=os.environ.get("UNIFI_PROTECT_USERNAME"),
        password=os.environ.get("UNIFI_PROTECT_PASSWORD"),
        verify_ssl=_environment_verify_ssl(parser, default=None),
    )


def _load_requested_profile(parser: argparse.ArgumentParser, name: str | None) -> ConnectionProfile | None:
    if name is None:
        return None
    from timelapse.profiles import ProfileError, load_profile  # noqa: PLC0415

    try:
        return load_profile(name)
    except ProfileError as exc:
        parser.error(str(exc))


def _resolve_connection(
    parser: argparse.ArgumentParser,
    *,
    profile_name: str | None,
    instance: str | None,
    token: str | None,
    username: str | None,
    password: str | None,
    verify_ssl: bool | None,
    prompt_for_missing: bool,
) -> _ConnectionValues:
    profile = _load_requested_profile(parser, profile_name)
    resolved_instance = instance or (
        profile.instance_url if profile is not None else os.environ.get("UNIFI_PROTECT_URL")
    )
    resolved_token = token or (profile.token if profile is not None else os.environ.get("UNIFI_PROTECT_TOKEN"))
    resolved_username = username or (
        profile.username if profile is not None else os.environ.get("UNIFI_PROTECT_USERNAME")
    )
    resolved_password = password or (
        profile.password if profile is not None else os.environ.get("UNIFI_PROTECT_PASSWORD")
    )
    resolved_verify_ssl = verify_ssl
    if resolved_verify_ssl is None:
        resolved_verify_ssl = (
            profile.verify_ssl if profile is not None else _environment_verify_ssl(parser, default=True)
        )

    if prompt_for_missing:
        resolved_instance = resolved_instance or _prompt_required("Protect Integration API URL")
        resolved_token = resolved_token or _prompt_required("Protect API token", secret=True)
        resolved_username = resolved_username or _prompt_required("Local Protect username")
        resolved_password = resolved_password or _prompt_required("Local Protect password", secret=True)

    if not resolved_instance:
        parser.error("--instance is required when UNIFI_PROTECT_URL is not set")
    if not resolved_token:
        parser.error("--token is required when UNIFI_PROTECT_TOKEN is not set")
    if not resolved_username:
        parser.error("--username is required when UNIFI_PROTECT_USERNAME is not set")
    if not resolved_password:
        parser.error("--password is required when UNIFI_PROTECT_PASSWORD is not set")
    if resolved_verify_ssl is None:
        message = "unreachable missing TLS verification setting"
        raise AssertionError(message)

    normalized_instance = resolved_instance.strip().rstrip("/")
    if not normalized_instance:
        parser.error("--instance cannot be empty")
    return _ConnectionValues(
        normalized_instance,
        resolved_token,
        resolved_username,
        resolved_password,
        resolved_verify_ssl,
    )


def parse_args() -> Config | CreateProfile:
    """Load .env defaults and parse the command line."""
    dotenv_path = Path.cwd() / ".env"
    load_dotenv(dotenv_path=dotenv_path, override=False)
    parser = _argument_parser()
    args = parser.parse_args()

    if args.create_profile:
        return _create_profile_request(parser)
    if dotenv_path.is_file() and args.profile is not None:
        parser.error("--profile cannot be used while .env exists; remove or rename .env to use a named profile")

    connection = _resolve_connection(
        parser,
        profile_name=args.profile,
        instance=args.instance,
        token=args.token,
        username=args.username,
        password=args.password,
        verify_ssl=args.verify_ssl,
        prompt_for_missing=dotenv_path.is_file(),
    )
    start, end = _date_range(parser, args.start_date, args.end_date, daily=args.daily)
    if end <= start:
        parser.error("--end-date must be after --start-date")

    return Config(
        instance_url=connection.instance_url,
        token=connection.token,
        username=connection.username,
        password=connection.password,
        verify_ssl=connection.verify_ssl,
        speed=args.speed,
        start=start,
        end=end,
        output=args.output,
        request_timeout_seconds=args.request_timeout_seconds,
        max_download_mib=args.max_download_mib,
        daily=args.daily,
    )

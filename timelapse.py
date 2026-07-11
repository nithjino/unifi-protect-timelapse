#! /usr/bin/env python3
"""Create UniFi Protect timelapse exports from the command line."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, strptime
from typing import TYPE_CHECKING
from urllib.parse import ParseResult, urlparse

from dotenv import load_dotenv
from uiprotect import ProtectApiClient

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from uiprotect.data import PublicCamera

DATE_FORMAT = "%m-%d-%Y-%H-%M-%S"
LOCAL_TZ = datetime.now(tz=UTC).astimezone().tzinfo
SPEED_TO_FPS = {
    "60x": 4,
    "120x": 8,
    "300x": 20,
    "600x": 40,
}
MEBIBYTE = 1024 * 1024
CHUNK_SIZE = MEBIBYTE
HTTP_OK = 200
HTTP_MULTIPLE_CHOICES = 300
MAX_ERROR_BODY_BYTES = 8 * 1024
PROGRESS_UPDATE_INTERVAL_SECONDS = 0.1
DEFAULT_REQUEST_TIMEOUT_SECONDS = 3600
DEFAULT_MAX_DOWNLOAD_MIB = 10 * 1024


@dataclass(frozen=True)
class _Config:
    instance_url: str
    token: str
    verify_ssl: bool
    speed: str
    start: datetime
    end: datetime
    output: Path | None
    request_timeout_seconds: int
    max_download_mib: int


@dataclass(frozen=True)
class _ProtectConnection:
    host: str
    port: int
    export_path: str


class TimelapseError(RuntimeError):
    """Raised when the timelapse export cannot be completed."""


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
    return datetime(
        parsed.tm_year,
        parsed.tm_mon,
        parsed.tm_mday,
        parsed.tm_hour,
        parsed.tm_min,
        parsed.tm_sec,
        tzinfo=LOCAL_TZ,
    )


def _parse_args() -> _Config:
    # Existing process variables intentionally win over values from .env.
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

    parser = argparse.ArgumentParser(description="Create a timelapse MP4 from UniFi Protect camera recordings.")
    parser.add_argument(
        "--speed",
        choices=tuple(SPEED_TO_FPS),
        required=True,
        help="timelapse speed",
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
        help="request timeout; defaults to TIMELAPSE_REQUEST_TIMEOUT_SECONDS or 3600 (0 disables)",
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
    if args.end_date <= args.start_date:
        parser.error("--end-date must be after --start-date")

    instance_url = _normalize_instance_url(args.instance)
    if not instance_url:
        parser.error("--instance cannot be empty")

    return _Config(
        instance_url=instance_url,
        token=args.token,
        verify_ssl=args.verify_ssl,
        speed=args.speed,
        start=args.start_date,
        end=args.end_date,
        output=args.output,
        request_timeout_seconds=args.request_timeout_seconds,
        max_download_mib=args.max_download_mib,
    )


def _normalize_instance_url(url: str) -> str:
    return url.strip().rstrip("/")


def _parse_connection(instance_url: str) -> _ProtectConnection:
    parsed = urlparse(instance_url)
    if parsed.scheme != "https" or not parsed.hostname:
        message = "--instance must be a URL like https://192.168.1.108/proxy/protect/integration/v1"
        raise TimelapseError(message)

    if parsed.username or parsed.password:
        message = "--instance must not contain embedded credentials; use --token instead"
        raise TimelapseError(message)
    if parsed.query or parsed.fragment:
        message = "--instance must not contain a query string or fragment"
        raise TimelapseError(message)

    port = parsed.port or 443
    export_path = _build_export_path(parsed)
    return _ProtectConnection(host=parsed.hostname, port=port, export_path=export_path)


def _build_export_path(parsed: ParseResult) -> str:
    path = parsed.path.rstrip("/")
    marker = "/integration/v1"
    if marker in path:
        return f"{path.rsplit(marker, 1)[0]}/api/video/export"
    if path.endswith("/v1"):
        return f"{path[:-3]}/api/video/export"
    if path:
        return f"{path}/api/video/export"
    return "/proxy/protect/api/video/export"


async def _create_client(config: _Config) -> ProtectApiClient:
    connection = _parse_connection(config.instance_url)
    return ProtectApiClient.public_only(
        connection.host,
        connection.port,
        api_key=config.token,
        verify_ssl=config.verify_ssl,
    )


async def _load_cameras(client: ProtectApiClient) -> list[PublicCamera]:
    cameras = await client.get_cameras_public()
    return sorted(cameras, key=lambda camera: _camera_name(camera).lower())


def _choose_camera(cameras: list[PublicCamera]) -> PublicCamera:
    if not cameras:
        message = "no cameras were returned by UniFi Protect"
        raise TimelapseError(message)

    _write_stdout("Available cameras:\n")
    for index, camera in enumerate(cameras, start=1):
        details = ", ".join(
            value
            for value in (
                _string_attr(camera.state),
                _string_attr(camera.model),
                _string_attr(camera.id),
            )
            if value
        )
        _write_stdout(f"{index:>2}. {_camera_name(camera)} ({details})\n")

    while True:
        selection = input("Select a camera by number: ").strip()
        try:
            index = int(selection)
        except ValueError:
            _write_stdout("Please enter a camera number.\n")
            continue

        if 1 <= index <= len(cameras):
            return cameras[index - 1]

        _write_stdout(f"Please enter a number from 1 to {len(cameras)}.\n")


def _string_attr(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _camera_name(camera: PublicCamera) -> str:
    return _string_attr(camera.name) or _string_attr(camera.id) or "camera"


def _camera_id(camera: PublicCamera) -> str:
    value = _string_attr(camera.id)
    if not value:
        message = "selected camera is missing an id"
        raise TimelapseError(message)
    return value


def _js_time(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "camera"


def _default_output_path(config: _Config, camera: PublicCamera) -> Path:
    start = config.start.strftime("%Y%m%d_%H%M%S")
    end = config.end.strftime("%Y%m%d_%H%M%S")
    name = _safe_filename(_camera_name(camera))
    return Path(f"timelapse_{name}_{start}_{end}_{config.speed}.mp4")


async def _download_timelapse(
    config: _Config,
    client: ProtectApiClient,
    camera: PublicCamera,
    output: Path,
) -> None:
    connection = _parse_connection(config.instance_url)
    params = {
        "camera": _camera_id(camera),
        "start": str(_js_time(config.start)),
        "end": str(_js_time(config.end)),
        "channel": "0",
        "type": "timelapse",
        "fps": str(SPEED_TO_FPS[config.speed]),
    }
    client.set_header("Accept", "video/mp4,application/octet-stream,*/*")
    client.set_header("X-API-KEY", config.token)

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output: Path | None = None

    _write_stdout(f"Requesting {config.speed} timelapse export for {_camera_name(camera)}...\n")
    response = await client.request(
        "get",
        connection.export_path,
        auto_close=False,
        params=params,
        timeout=config.request_timeout_seconds or 0,
    )

    try:
        if not HTTP_OK <= response.status < HTTP_MULTIPLE_CHOICES:
            detail = await _read_error_detail(response)
            reason = _sanitize_terminal_text(response.reason or "unknown error")
            message = f"timelapse export failed with HTTP {response.status}: {detail or reason}"
            raise TimelapseError(message)

        total_header = response.headers.get("Content-Length")
        total_bytes = int(total_header) if total_header and total_header.isdigit() else None
        max_download_bytes = config.max_download_mib * MEBIBYTE
        if max_download_bytes and total_bytes and total_bytes > max_download_bytes:
            message = (
                f"server reported a {total_bytes / MEBIBYTE:.1f} MiB export, "
                f"exceeding the {config.max_download_mib} MiB limit"
            )
            raise TimelapseError(message)
        downloaded = 0
        last_progress_update = 0.0

        # A uniquely named, mode-0600 file avoids predictable .part-file symlink races.
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".part",
            delete=False,
        ) as file:
            temp_output = Path(file.name)
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                file.write(chunk)
                downloaded += len(chunk)
                if max_download_bytes and downloaded > max_download_bytes:
                    message = f"download exceeded the {config.max_download_mib} MiB limit"
                    raise TimelapseError(message)
                now = monotonic()
                if now - last_progress_update >= PROGRESS_UPDATE_INTERVAL_SECONDS:
                    _print_progress(downloaded, total_bytes)
                    last_progress_update = now

        _print_progress(downloaded, total_bytes)
        temp_output.replace(output)
    except OSError as exc:
        message = f"could not write {output}: {exc}"
        raise TimelapseError(message) from exc
    finally:
        response.release()
        if temp_output is not None:
            temp_output.unlink(missing_ok=True)
        _write_stdout("\n")


async def _read_error_detail(response: ClientResponse) -> str:
    raw = await response.content.read(MAX_ERROR_BODY_BYTES + 1)
    truncated = len(raw) > MAX_ERROR_BODY_BYTES
    raw = raw[:MAX_ERROR_BODY_BYTES]
    detail = raw.decode("utf-8", errors="replace").strip()
    safe_detail = _sanitize_terminal_text(detail)
    return f"{safe_detail}... (truncated)" if truncated else safe_detail


def _sanitize_terminal_text(value: str) -> str:
    """Prevent untrusted server text from injecting terminal control codes."""
    return value.encode("unicode_escape", errors="backslashreplace").decode("ascii")


def _print_progress(downloaded: int, total: int | None) -> None:
    if total:
        percent = min(downloaded / total * 100, 100.0)
        _write_stdout(f"\rDownloaded {downloaded / MEBIBYTE:.1f} MiB ({percent:.1f}%)")
    else:
        _write_stdout(f"\rDownloaded {downloaded / MEBIBYTE:.1f} MiB")


def _write_stdout(message: str) -> None:
    sys.stdout.write(message)
    sys.stdout.flush()


def _write_stderr(message: str) -> None:
    sys.stderr.write(message)
    sys.stderr.flush()


async def _run() -> int:
    client: ProtectApiClient | None = None
    try:
        config = _parse_args()
        client = await _create_client(config)
        cameras = await _load_cameras(client)
        camera = _choose_camera(cameras)
        output = config.output or _default_output_path(config, camera)
        await _download_timelapse(config, client, camera, output)
    except KeyboardInterrupt:
        _write_stderr("\nCancelled.\n")
        return 130
    except TimelapseError as exc:
        _write_stderr(f"Error: {exc}\n")
        return 1
    except Exception as exc:
        _write_stderr(f"Error: {exc}\n")
        return 1
    finally:
        if client is not None:
            await client.close_session()

    _write_stdout(f"Saved timelapse to {output}\n")
    return 0


def main() -> int:
    """Run the timelapse CLI."""
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())

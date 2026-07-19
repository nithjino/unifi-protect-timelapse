"""CLI orchestration and interactive camera selection."""

from __future__ import annotations

import asyncio
import getpass
import logging
import sys
from datetime import timedelta
from pathlib import Path

from timelapse import TimelapseError
from timelapse.config import Config, CreateProfile, parse_args
from timelapse.download import MEBIBYTE, DownloadProgress, default_output_path
from timelapse.profiles import ConnectionProfile, ProfileError, save_profile
from timelapse.protect import CameraInfo, camera_name
from timelapse.schedule import (
    config_for_local_day,
    daily_output_path,
    latest_complete_local_day,
    seconds_until_next_local_day,
)
from timelapse.service import export_timelapse, list_available_cameras


def _choose_camera(cameras: list[CameraInfo]) -> CameraInfo:
    if not cameras:
        message = "no cameras were returned by UniFi Protect"
        raise TimelapseError(message)

    _write_stdout("Available cameras:\n")
    for index, camera in enumerate(cameras, start=1):
        details = ", ".join(value for value in (camera.state, camera.model, camera.id) if value)
        _write_stdout(f"{index:>2}. {camera_name(camera)} ({details})\n")

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


async def _run() -> int:
    try:
        command = parse_args()
        if isinstance(command, CreateProfile):
            _create_profile(command)
            return 0
        config = command
        camera = _choose_camera(await list_available_cameras(config))
        if config.daily:
            await _run_daily(config, camera)
            return 0
        output = config.output or default_output_path(config, camera)
        await _export(config, camera, output)
    except KeyboardInterrupt:
        _write_stderr("\nCancelled.\n")
        return 130
    except TimelapseError as exc:
        _write_stderr(f"Error: {exc}\n")
        return 1
    except Exception as exc:
        _write_stderr(f"Error: {exc}\n")
        return 1

    _write_stdout(f"Saved timelapse to {output}\n")
    return 0


def _create_profile(command: CreateProfile) -> None:
    _write_stdout("Create a connection profile. Every field is required.\n")
    profile = ConnectionProfile(
        name=_prompt_required("Profile name"),
        instance_url=command.instance_url or _prompt_required("Protect Integration API URL"),
        token=command.token or _prompt_required("Protect API token", secret=True),
        username=command.username or _prompt_required("Local Protect username"),
        password=command.password or _prompt_required("Local Protect password", secret=True),
        verify_ssl=command.verify_ssl if command.verify_ssl is not None else _prompt_verify_ssl(),
    )
    try:
        save_profile(profile)
    except ProfileError as exc:
        raise TimelapseError(str(exc)) from exc
    _write_stdout(f"Created profile {profile.name.strip()!r}.\n")


def _prompt_required(label: str, *, secret: bool = False) -> str:
    while True:
        value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
        if value.strip():
            return value
        _write_stdout(f"{label} is required.\n")


def _prompt_verify_ssl() -> bool:
    while True:
        value = input("Verify TLS certificates? [Y/n]: ").strip().lower()
        if value in {"", "y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        _write_stdout("Please enter yes or no.\n")


async def _export(config: Config, camera: CameraInfo, output: Path) -> None:
    _write_stdout(f"Requesting {config.speed} timelapse export for {camera_name(camera)}...\n")
    try:
        await export_timelapse(config, camera, output, _print_progress)
    finally:
        _write_stdout("\n")


async def _run_daily(config: Config, camera: CameraInfo) -> None:
    output_directory = config.output or Path.cwd()
    if output_directory.exists() and not output_directory.is_dir():
        message = f"daily output must be a directory: {output_directory}"
        raise TimelapseError(message)

    day = latest_complete_local_day()
    while True:
        today = latest_complete_local_day() + timedelta(days=1)
        if day < today:
            daily_config = config_for_local_day(config, day)
            output = daily_output_path(daily_config, camera, output_directory)
            if output.exists():
                _write_stdout(f"Skipping {day.isoformat()}; output already exists: {output}\n")
            else:
                _write_stdout(f"Creating daily timelapse for {day.isoformat()}.\n")
                await _export(daily_config, camera, output)
                _write_stdout(f"Saved daily timelapse to {output}\n")
            day += timedelta(days=1)
            continue

        delay = seconds_until_next_local_day()
        _write_stdout(f"Waiting for the current local day to finish ({delay / 3600:.1f} hours).\n")
        await asyncio.sleep(delay)


def _print_progress(progress: DownloadProgress) -> None:
    downloaded_mib = progress.downloaded_bytes / MEBIBYTE
    if progress.total_bytes:
        percent = min(progress.downloaded_bytes / progress.total_bytes * 100, 100.0)
        _write_stdout(f"\rDownloaded {downloaded_mib:.1f} MiB ({percent:.1f}%)")
    else:
        _write_stdout(f"\rDownloaded {downloaded_mib:.1f} MiB")


def _write_stdout(message: str) -> None:
    sys.stdout.write(message)
    sys.stdout.flush()


def _write_stderr(message: str) -> None:
    sys.stderr.write(message)
    sys.stderr.flush()


def main() -> int:
    """Run the timelapse CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        _write_stderr("\nCancelled.\n")
        return 130

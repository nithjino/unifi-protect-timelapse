"""CLI orchestration and interactive camera selection."""

from __future__ import annotations

import asyncio
import sys

from timelapse import TimelapseError
from timelapse.config import parse_args
from timelapse.download import MEBIBYTE, DownloadProgress, default_output_path
from timelapse.protect import CameraInfo, camera_name
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
        config = parse_args()
        camera = _choose_camera(await list_available_cameras(config))
        output = config.output or default_output_path(config, camera)
        _write_stdout(f"Requesting {config.speed} timelapse export for {camera_name(camera)}...\n")
        try:
            await export_timelapse(config, camera, output, _print_progress)
        finally:
            _write_stdout("\n")
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
    return asyncio.run(_run())

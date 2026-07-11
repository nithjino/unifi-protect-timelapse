"""CLI orchestration and interactive camera selection."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from timelapse import TimelapseError
from timelapse.config import parse_args
from timelapse.download import default_output_path, download_timelapse
from timelapse.protect import camera_name, create_client, load_cameras, parse_connection, string_attr

if TYPE_CHECKING:
    from uiprotect import ProtectApiClient
    from uiprotect.data import PublicCamera


def _choose_camera(cameras: list[PublicCamera]) -> PublicCamera:
    if not cameras:
        message = "no cameras were returned by UniFi Protect"
        raise TimelapseError(message)

    _write_stdout("Available cameras:\n")
    for index, camera in enumerate(cameras, start=1):
        details = ", ".join(
            value for value in (string_attr(camera.state), string_attr(camera.model), string_attr(camera.id)) if value
        )
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
    client: ProtectApiClient | None = None
    try:
        config = parse_args()
        connection = parse_connection(config.instance_url)
        client = create_client(config, connection)
        camera = _choose_camera(await load_cameras(client))
        output = config.output or default_output_path(config, camera)
        await download_timelapse(config, connection, client, camera, output)
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


def _write_stdout(message: str) -> None:
    sys.stdout.write(message)
    sys.stdout.flush()


def _write_stderr(message: str) -> None:
    sys.stderr.write(message)
    sys.stderr.flush()


def main() -> int:
    """Run the timelapse CLI."""
    return asyncio.run(_run())

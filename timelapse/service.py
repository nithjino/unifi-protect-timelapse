"""UI-neutral orchestration for camera discovery and timelapse exports."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from timelapse.download import download_timelapse
from timelapse.protect import create_client, load_cameras, parse_connection

if TYPE_CHECKING:
    from pathlib import Path

    from uiprotect import ProtectApiClient

    from timelapse.config import Config
    from timelapse.download import ProgressCallback
    from timelapse.protect import CameraInfo

CLIENT_CLOSE_TIMEOUT_SECONDS = 5.0


async def list_available_cameras(config: Config) -> list[CameraInfo]:
    """Load cameras with a client owned by the current event loop."""
    connection = parse_connection(config.instance_url)
    client = create_client(config, connection)
    try:
        return await load_cameras(client)
    finally:
        await _close_client(client)


async def export_timelapse(
    config: Config,
    camera: CameraInfo,
    output: Path,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Export one camera with a client owned by the current event loop."""
    connection = parse_connection(config.instance_url)
    client = create_client(config, connection)
    try:
        await download_timelapse(config, connection, client, camera, output, progress_callback)
    finally:
        await _close_client(client)


async def _close_client(client: ProtectApiClient) -> None:
    try:
        await asyncio.wait_for(client.close_session(), timeout=CLIENT_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        return

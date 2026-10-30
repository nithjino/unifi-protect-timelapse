"""UI-neutral orchestration for camera discovery and timelapse exports."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from timelapse import OperationTimeoutError, TimelapseError
from timelapse.download import download_timelapse
from timelapse.protect import load_cameras, parse_connection, private_operation, protect_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime
    from pathlib import Path

    from timelapse.config import Config, ConnectionSettings
    from timelapse.download import ProgressCallback
    from timelapse.protect import CameraInfo

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraThumbnail:
    """Thumbnail image plus whether it represents the selected or current time."""

    image: bytes
    source: str


@asynccontextmanager
async def _operation_timeout(seconds: int, operation: str) -> AsyncIterator[None]:
    """Apply the configured whole-operation deadline; zero disables it."""
    try:
        async with asyncio.timeout(seconds or None):
            yield
    except TimeoutError as exc:
        message = f"{operation} exceeded the configured {seconds}-second operation timeout."
        raise OperationTimeoutError(message) from exc


async def list_available_cameras(config: ConnectionSettings) -> list[CameraInfo]:
    """Load cameras with a client owned by the current event loop."""
    connection = parse_connection(config.instance_url)
    async with (
        protect_client(config, connection) as client,
        _operation_timeout(config.request_timeout_seconds, "Camera discovery"),
    ):
        return await load_cameras(client)


async def fetch_camera_thumbnail(
    config: Config,
    camera: CameraInfo,
    timestamp: datetime,
    *,
    width: int = 384,
    height: int = 216,
) -> CameraThumbnail:
    """Fetch an exact historical snapshot, falling back to the API-token live image."""
    connection = parse_connection(config.instance_url)
    async with (
        protect_client(config, connection) as client,
        _operation_timeout(config.request_timeout_seconds, f"Thumbnail request for {camera.name}"),
    ):
        try:
            async with private_operation(connection, client, operation=f"thumbnail request for {camera.name}"):
                image = await client.api_request_raw(
                    f"cameras/{camera.id}/recording-snapshot",
                    params={"ts": int(timestamp.timestamp() * 1000), "w": width, "h": height},
                    raise_exception=True,
                    timeout=0,
                )
            if image:
                return CameraThumbnail(image, "exact")
        except Exception as exc:
            _LOGGER.warning("Exact thumbnail failed for %s: %s: %s", camera.name, type(exc).__name__, exc)

        try:
            image = await client.api_request_raw(
                public_api=True,
                raise_exception=True,
                url=f"/v1/cameras/{camera.id}/snapshot",
                params={"highQuality": "false"},
                timeout=0,
            )
        except Exception as exc:
            _LOGGER.warning("Live thumbnail failed for %s: %s: %s", camera.name, type(exc).__name__, exc)
            image = None
        if image:
            return CameraThumbnail(image, "live")
        message = (
            f"Could not load a thumbnail for {camera.name}. Exact historical previews require the local "
            "Protect account's Livestream permission (readmedia/livestream); the live fallback requires "
            "the Integration API token to have access to this camera. Update the permissions or token, "
            "then change the date or time to retry."
        )
        raise TimelapseError(message)


async def export_timelapse(
    config: Config,
    camera: CameraInfo,
    output: Path,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Export one camera, holding a private-operation slot for the whole stream."""
    connection = parse_connection(config.instance_url)
    _LOGGER.info(
        "Timelapse export started: camera=%s, range=%s to %s, speed=%s, output=%s",
        camera.name,
        config.start.isoformat(),
        config.end.isoformat(),
        config.speed,
        output,
    )
    async with protect_client(config, connection) as client:
        try:
            async with (
                _operation_timeout(config.request_timeout_seconds, f"Timelapse export for {camera.name}"),
                private_operation(connection, client, operation=f"timelapse export for {camera.name}"),
            ):
                await download_timelapse(config, connection, client, camera, output, progress_callback)
        except Exception:
            _LOGGER.exception("Timelapse export failed for %s", camera.name)
            raise

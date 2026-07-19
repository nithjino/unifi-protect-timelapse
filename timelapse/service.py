"""UI-neutral orchestration for camera discovery and timelapse exports."""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
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
_LOGGER = logging.getLogger(__name__)


async def list_available_cameras(config: Config) -> list[CameraInfo]:
    """Load cameras with a client owned by the current event loop."""
    started_at = perf_counter()
    connection = parse_connection(config.instance_url)
    _LOGGER.info(
        "Camera discovery started: target=%s:%d, verify_ssl=%s, request_timeout=%s",
        connection.host,
        connection.port,
        config.verify_ssl,
        _format_timeout(config.request_timeout_seconds),
    )
    client = create_client(config, connection)
    try:
        cameras = await load_cameras(client)
    except asyncio.CancelledError:
        _LOGGER.info("Camera discovery cancelled after %.2fs", perf_counter() - started_at)
        raise
    except Exception:
        _LOGGER.exception("Camera discovery failed after %.2fs", perf_counter() - started_at)
        raise
    else:
        _LOGGER.info(
            "Camera discovery completed: cameras=%d, elapsed=%.2fs",
            len(cameras),
            perf_counter() - started_at,
        )
        return cameras
    finally:
        await _close_client(client, operation="camera discovery")


async def export_timelapse(
    config: Config,
    camera: CameraInfo,
    output: Path,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Export one camera with a client owned by the current event loop."""
    started_at = perf_counter()
    connection = parse_connection(config.instance_url)
    _LOGGER.info(
        "Timelapse export started: camera=%s (id=%s), range=%s to %s, speed=%s, output=%s",
        camera.name,
        camera.id,
        config.start.isoformat(),
        config.end.isoformat(),
        config.speed,
        output,
    )
    client = create_client(config, connection)
    try:
        await download_timelapse(config, connection, client, camera, output, progress_callback)
        _LOGGER.info(
            "Timelapse export completed: camera=%s, output=%s, elapsed=%.2fs",
            camera.name,
            output,
            perf_counter() - started_at,
        )
    except asyncio.CancelledError:
        _LOGGER.info("Timelapse export cancelled for %s after %.2fs", camera.name, perf_counter() - started_at)
        raise
    except Exception:
        _LOGGER.exception("Timelapse export failed for %s after %.2fs", camera.name, perf_counter() - started_at)
        raise
    finally:
        await _close_client(client, operation=f"timelapse export for {camera.name}")


async def _close_client(client: ProtectApiClient, *, operation: str) -> None:
    started_at = perf_counter()
    _LOGGER.info(
        "Protect client cleanup started after %s (timeout=%.1fs)",
        operation,
        CLIENT_CLOSE_TIMEOUT_SECONDS,
    )
    try:
        await asyncio.wait_for(client.close_session(), timeout=CLIENT_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOGGER.warning(
            "Protect client cleanup timed out after %s (elapsed=%.2fs)",
            operation,
            perf_counter() - started_at,
        )
        return
    _LOGGER.info(
        "Protect client cleanup completed after %s (elapsed=%.2fs)",
        operation,
        perf_counter() - started_at,
    )


def _format_timeout(seconds: int) -> str:
    return "disabled" if seconds == 0 else f"{seconds}s"

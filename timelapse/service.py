"""UI-neutral orchestration for camera discovery and timelapse exports."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, TypeVar

from timelapse import OperationTimeoutError, TimelapseError
from timelapse.download import download_timelapse
from timelapse.protect import load_cameras, parse_connection, private_operation, protect_client

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from datetime import datetime
    from pathlib import Path

    from uiprotect import ProtectApiClient

    from timelapse.config import Config
    from timelapse.download import ProgressCallback
    from timelapse.protect import CameraInfo, ProtectConnection

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass(frozen=True)
class CameraThumbnail:
    """Thumbnail image plus whether it represents the selected or current time."""

    image: bytes
    source: str


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
    deadline = _operation_deadline(config.request_timeout_seconds)
    async with protect_client(config, connection) as client:
        try:
            cameras = await _await_with_deadline(
                load_cameras(client),
                deadline=deadline,
                timeout_seconds=config.request_timeout_seconds,
                operation="Camera discovery",
            )
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


async def fetch_camera_thumbnail(
    config: Config,
    camera: CameraInfo,
    timestamp: datetime,
    *,
    width: int = 384,
    height: int = 216,
) -> CameraThumbnail:
    """Fetch an exact historical snapshot, falling back to the API-token live image."""
    started_at = perf_counter()
    connection = parse_connection(config.instance_url)
    _LOGGER.info(
        "Thumbnail request started: camera=%s (id=%s), timestamp=%s, size=%dx%d, target=%s:%d",
        camera.name,
        camera.id,
        timestamp.isoformat(),
        width,
        height,
        connection.host,
        connection.port,
    )
    deadline = _operation_deadline(config.request_timeout_seconds)
    async with protect_client(config, connection) as client:
        try:
            try:
                image = _require_thumbnail(
                    await _await_with_deadline(
                        _fetch_exact_thumbnail(
                            client,
                            connection,
                            camera,
                            timestamp,
                            width=width,
                            height=height,
                            request_timeout=_remaining_timeout(deadline),
                        ),
                        deadline=deadline,
                        timeout_seconds=config.request_timeout_seconds,
                        operation=f"Thumbnail request for {camera.name}",
                    ),
                    camera,
                )
            except asyncio.CancelledError:
                raise
            except OperationTimeoutError:
                raise
            except Exception as exact_error:
                _LOGGER.warning(
                    "Exact thumbnail request failed for %s (%s); trying API-token live snapshot",
                    camera.name,
                    _exception_summary(exact_error),
                )
                try:
                    live_image = _require_thumbnail(
                        await _await_with_deadline(
                            client.api_request_raw(
                                public_api=True,
                                raise_exception=True,
                                url=f"/v1/cameras/{camera.id}/snapshot",
                                params={"highQuality": "false"},
                                timeout=_remaining_timeout(deadline) or 0,
                            ),
                            deadline=deadline,
                            timeout_seconds=config.request_timeout_seconds,
                            operation=f"Thumbnail request for {camera.name}",
                        ),
                        camera,
                    )
                except asyncio.CancelledError:
                    raise
                except OperationTimeoutError:
                    raise
                except Exception as live_error:
                    message = (
                        f"Could not load a thumbnail for {camera.name}. Exact historical previews require the local "
                        "Protect account's Livestream permission (readmedia/livestream); the live fallback requires "
                        "the Integration API token to have access to this camera. Update the permissions or token, "
                        "then change the date or time to retry."
                    )
                    _LOGGER.log(
                        logging.ERROR,
                        "Exact and live thumbnail requests failed for %s: exact=%s, live=%s",
                        camera.name,
                        _exception_summary(exact_error),
                        _exception_summary(live_error),
                    )
                    raise TimelapseError(message) from live_error
                else:
                    thumbnail = CameraThumbnail(live_image, "live")
            else:
                thumbnail = CameraThumbnail(image, "exact")
        except asyncio.CancelledError:
            _LOGGER.info("Thumbnail request cancelled for %s after %.2fs", camera.name, perf_counter() - started_at)
            raise
        except TimelapseError:
            raise
        except Exception:
            _LOGGER.exception("Thumbnail request failed for %s after %.2fs", camera.name, perf_counter() - started_at)
            raise
        else:
            _LOGGER.info(
                "Thumbnail request completed: camera=%s, source=%s, bytes=%d, elapsed=%.2fs",
                camera.name,
                thumbnail.source,
                len(thumbnail.image),
                perf_counter() - started_at,
            )
            return thumbnail


async def _fetch_exact_thumbnail(
    client: ProtectApiClient,
    connection: ProtectConnection,
    camera: CameraInfo,
    timestamp: datetime,
    *,
    width: int,
    height: int,
    request_timeout: float | None,
) -> bytes | None:
    """Fetch a private historical frame inside a bounded console slot."""
    async with private_operation(connection, client, operation=f"thumbnail request for {camera.name}"):
        return await client.api_request_raw(
            f"cameras/{camera.id}/recording-snapshot",
            params={
                "ts": int(timestamp.timestamp() * 1000),
                "w": width,
                "h": height,
            },
            raise_exception=True,
            timeout=request_timeout or 0,
        )


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
    deadline = _operation_deadline(config.request_timeout_seconds)
    async with protect_client(config, connection) as client:
        try:
            await _await_with_deadline(
                _download_private_timelapse(
                    config,
                    connection,
                    client,
                    camera,
                    output,
                    progress_callback,
                    request_timeout_seconds=_remaining_timeout(deadline),
                ),
                deadline=deadline,
                timeout_seconds=config.request_timeout_seconds,
                operation=f"Timelapse export for {camera.name}",
            )
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


async def _download_private_timelapse(
    config: Config,
    connection: ProtectConnection,
    client: ProtectApiClient,
    camera: CameraInfo,
    output: Path,
    progress_callback: ProgressCallback | None,
    *,
    request_timeout_seconds: float | None,
) -> None:
    """Hold one private-operation slot for the complete streamed export."""
    async with private_operation(connection, client, operation=f"timelapse export for {camera.name}"):
        await download_timelapse(
            config,
            connection,
            client,
            camera,
            output,
            progress_callback,
            request_timeout_seconds=request_timeout_seconds,
        )


def _format_timeout(seconds: int) -> str:
    return "disabled" if seconds == 0 else f"{seconds}s"


def _operation_deadline(timeout_seconds: int) -> float | None:
    if timeout_seconds == 0:
        return None
    return asyncio.get_running_loop().time() + timeout_seconds


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(deadline - asyncio.get_running_loop().time(), 0.0)


async def _await_with_deadline(
    awaitable: Awaitable[_T],
    *,
    deadline: float | None,
    timeout_seconds: int,
    operation: str,
) -> _T:
    if deadline is None:
        return await awaitable
    try:
        async with asyncio.timeout_at(deadline):
            return await awaitable
    except TimeoutError as exc:
        message = f"{operation} exceeded the configured {timeout_seconds}-second operation timeout."
        raise OperationTimeoutError(message) from exc


def _require_thumbnail(image: bytes | None, camera: CameraInfo) -> bytes:
    if image:
        return image
    message = f"No recording thumbnail is available for {camera.name} at the selected time."
    raise TimelapseError(message)


def _exception_summary(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"

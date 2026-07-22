"""UI-neutral orchestration for camera discovery and timelapse exports."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, TypeVar

from timelapse import OperationTimeoutError, TimelapseError
from timelapse.download import download_timelapse
from timelapse.protect import create_client, load_cameras, parse_connection

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from datetime import datetime
    from pathlib import Path

    from uiprotect import ProtectApiClient

    from timelapse.config import Config
    from timelapse.download import ProgressCallback
    from timelapse.protect import CameraInfo

CLIENT_CLOSE_TIMEOUT_SECONDS = 5.0
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
    client = create_client(config, connection)
    deadline = _operation_deadline(config.request_timeout_seconds)
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
    finally:
        await _close_client(client, operation="camera discovery")


async def fetch_camera_thumbnail(  # noqa: PLR0912 - exact/fallback requests share one deadline and cleanup path
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
    client = create_client(config, connection)
    deadline = _operation_deadline(config.request_timeout_seconds)
    try:
        try:
            image = _require_thumbnail(
                await _await_with_deadline(
                    client.api_request_raw(
                        f"cameras/{camera.id}/recording-snapshot",
                        params={
                            "ts": int(timestamp.timestamp() * 1000),
                            "w": width,
                            "h": height,
                        },
                        raise_exception=True,
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
    finally:
        await _close_client(client, operation=f"thumbnail request for {camera.name}")


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
    deadline = _operation_deadline(config.request_timeout_seconds)
    try:
        await _await_with_deadline(
            download_timelapse(
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
    except Exception:
        _LOGGER.warning(
            "Protect client cleanup failed after %s (elapsed=%.2fs)",
            operation,
            perf_counter() - started_at,
            exc_info=True,
        )
        return
    _LOGGER.info(
        "Protect client cleanup completed after %s (elapsed=%.2fs)",
        operation,
        perf_counter() - started_at,
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

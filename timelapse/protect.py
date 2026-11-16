"""One object per UniFi Protect console: cameras, thumbnails and exports."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from platformdirs import user_config_path
from uiprotect import ProtectApiClient
from uiprotect.exceptions import UnifiProtectError

from timelapse import OperationTimeoutError, RateLimitError, TimelapseError
from timelapse.download import Progress, retry_after_seconds, save_export
from timelapse.model import SPEEDS, Camera, Connection, ExportRequest

LOGGER = logging.getLogger(__name__)
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
RETRIES = 3
MAX_DELAY = 300.0
SLOTS = 2
T = TypeVar("T")


def parse_url(url: str) -> tuple[str, int, str]:
    """Return the host, port and export path for a Protect URL."""
    parts = urlsplit(url.strip().rstrip("/"))
    if parts.scheme != "https" or not parts.hostname:
        raise TimelapseError("UNIFI_PROTECT_URL must be an https:// URL with a hostname")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise TimelapseError("UNIFI_PROTECT_URL must not contain credentials, a query or a fragment")
    try:
        port = parts.port or 443
    except ValueError as error:
        raise TimelapseError(f"UNIFI_PROTECT_URL has an invalid port: {error}") from error
    path = parts.path.rstrip("/")
    if "/integration/v1" in path:
        path = path[: path.rindex("/integration/v1")]
    elif path.endswith("/v1"):
        path = path[:-3]
    elif not path:
        path = "/proxy/protect"
    return parts.hostname, port, f"{path}/api/video/export"


def is_rate_limited(error: BaseException) -> bool:
    """Return whether a uiprotect error reports HTTP 429; the library has no typed signal for it."""
    text = str(error).casefold()
    return any(marker in text for marker in ("status: 429", "http 429", "too many requests"))


class RetryingClient(ProtectApiClient):
    """A uiprotect client whose every request follows this project's retry policy."""

    def __init__(
        self,
        *args: Any,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.sleep = sleep
        self.jitter = jitter
        self.last_retry_after: float | None = None

    async def _paced_request(self, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(RETRIES + 1):
            response = await super()._paced_request(*args, **kwargs)
            if response.status not in RETRY_STATUSES:
                return response
            wait = retry_after_seconds(response.headers.get("Retry-After"))
            if attempt == RETRIES or (wait is not None and wait > MAX_DELAY):
                if response.status == 429:
                    self.last_retry_after = wait
                return response
            if wait is None:
                base = min(30.0, 2.0**attempt)
                delay = base + self.jitter(-base / 4, base / 4)
            else:
                delay = wait + self.jitter(0, wait / 4)
            response.release()
            await self.sleep(min(MAX_DELAY, max(0.1, delay)))
        raise AssertionError  # pragma: no cover


class Protect:
    """Owns one console's client, its concurrency limits and the three operations."""

    def __init__(self, connection: Connection, *, client: Any = None, session_dir: Path | None = None) -> None:
        self.connection = connection
        host, port, self.export_path = parse_url(connection.url)
        self.session_dir = session_dir or user_config_path("TimeLapse") / "protect"
        if client is None:
            self.session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                self.session_dir.chmod(0o700)
            client = RetryingClient(
                host,
                port,
                username=connection.username,
                password=connection.password,
                api_key=connection.token,
                verify_ssl=connection.verify_ssl,
                config_dir=self.session_dir,
                store_sessions=True,
                max_retries=0,
            )
        self.client = client
        self._export_slots = asyncio.Semaphore(SLOTS)
        self._thumbnail_slots = asyncio.Semaphore(SLOTS)
        self.close_timeout = 5.0

    async def _deadline(self, operation: str, work: Awaitable[T]) -> T:
        seconds = self.connection.timeout_seconds
        if seconds <= 0:
            return await work
        try:
            async with asyncio.timeout(seconds):
                return await work
        except TimeoutError as error:
            message = f"{operation} exceeded the configured {seconds:g}-second operation timeout."
            raise OperationTimeoutError(message) from error

    def _error(self, error: Exception, prefix: str = "") -> TimelapseError:
        if is_rate_limited(error):
            return RateLimitError(getattr(self.client, "last_retry_after", None))
        return TimelapseError(f"{prefix}{error}")

    async def _login(self) -> None:
        await self.client.ensure_authenticated()
        session_file = self.session_dir / "unifi_protect.json"
        if os.name != "nt" and session_file.exists():
            session_file.chmod(0o600)

    async def cameras(self) -> list[Camera]:
        """List the cameras visible to the API token."""

        def text(value: object) -> str | None:
            return None if value is None else str(getattr(value, "value", value))

        try:
            items = await self._deadline("Camera discovery", self.client.get_cameras_public())
        except UnifiProtectError as error:
            raise self._error(error, "Could not load cameras from UniFi Protect: ") from error
        cameras = []
        for item in items:
            camera_id = text(item.id) or ""
            name = text(getattr(item, "name", None)) or camera_id or "camera"
            cameras.append(
                Camera(camera_id, name, text(getattr(item, "state", None)), text(getattr(item, "model", None)))
            )
        return sorted(cameras, key=lambda camera: (camera.name.casefold(), camera.id))

    async def thumbnail(self, camera: Camera, at: datetime) -> tuple[bytes, str]:
        """Return a JPEG of ``camera`` at ``at``, or a live frame when that is unavailable."""

        async def work() -> tuple[bytes, str]:
            async with self._thumbnail_slots:
                try:
                    await self._login()
                    params = {"ts": int(at.timestamp() * 1000), "w": 384, "h": 216}
                    path = f"cameras/{camera.id}/recording-snapshot"
                    data = await self.client.api_request_raw(path, params=params, raise_exception=True, timeout=0)
                    if data:
                        return data, "exact"
                except Exception as error:
                    LOGGER.info("Exact thumbnail for %s failed, using a live frame: %s", camera.name, error)
                try:
                    data = await self.client.api_request_raw(
                        url=f"/v1/cameras/{camera.id}/snapshot",
                        public_api=True,
                        raise_exception=True,
                        params={"highQuality": "false"},
                        timeout=0,
                    )
                except UnifiProtectError as error:
                    if is_rate_limited(error):
                        raise self._error(error) from error
                    data = None
                if not data:
                    raise TimelapseError(
                        f"Could not load a thumbnail for {camera.name}. Give the local Protect user access to this "
                        "camera with the readmedia/livestream permission."
                    )
                return data, "live"

        return await self._deadline(f"Thumbnail request for {camera.name}", work())

    async def export(
        self, request: ExportRequest, output: Path, on_progress: Callable[[Progress], None] | None = None
    ) -> None:
        """Download one timelapse into ``output``."""
        if output.exists():
            raise TimelapseError(f"refusing to overwrite existing output file: {output}")
        params = {
            "camera": request.camera.id,
            "start": str(int(request.start.timestamp() * 1000)),
            "end": str(int(request.end.timestamp() * 1000)),
            "channel": "0",
        }
        fps = SPEEDS[request.speed]
        if fps is not None:
            params |= {"type": "timelapse", "fps": str(fps)}

        async def work() -> None:
            async with self._export_slots:
                await self._login()
                response = await self.client.request(
                    "get", self.export_path, require_auth=True, auto_close=False, params=params, timeout=0
                )
                max_bytes = self.connection.max_download_mib * 1024 * 1024
                await save_export(response, output, max_bytes=max_bytes, on_progress=on_progress)

        try:
            await self._deadline(f"Timelapse export for {request.camera.name}", work())
        except UnifiProtectError as error:
            raise self._error(error) from error

    async def close(self) -> None:
        """Close the client, waiting at most five seconds."""
        try:
            async with asyncio.timeout(self.close_timeout):
                await self.client.close_session()
        except Exception:
            LOGGER.exception("Could not close the UniFi Protect client")

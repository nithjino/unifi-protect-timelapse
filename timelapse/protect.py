"""UniFi Protect connection and camera operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import ParseResult, urlparse

from uiprotect import ProtectApiClient

from timelapse import TimelapseError

if TYPE_CHECKING:
    from uiprotect.data import PublicCamera

    from timelapse.config import Config


@dataclass(frozen=True)
class ProtectConnection:
    """Host and endpoint information derived from the Protect URL."""

    host: str
    port: int
    export_path: str


@dataclass(frozen=True)
class CameraInfo:
    """Camera data detached from the client that loaded it."""

    id: str
    name: str
    state: str | None
    model: str | None


def parse_connection(instance_url: str) -> ProtectConnection:
    """Validate a Protect URL and derive its export endpoint."""
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

    try:
        port = parsed.port or 443
    except ValueError as exc:
        message = f"invalid port in --instance: {exc}"
        raise TimelapseError(message) from exc
    return ProtectConnection(parsed.hostname, port, _build_export_path(parsed))


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


def create_client(config: Config, connection: ProtectConnection) -> ProtectApiClient:
    """Create a client supporting public API keys and private video export."""
    return ProtectApiClient(
        connection.host,
        connection.port,
        username=config.username,
        password=config.password,
        api_key=config.token,
        verify_ssl=config.verify_ssl,
        store_sessions=False,
    )


async def load_cameras(client: ProtectApiClient) -> list[CameraInfo]:
    """Load detached camera details in display-name order."""
    cameras = [
        CameraInfo(
            id=camera_id(camera),
            name=camera_name(camera),
            state=string_attr(camera.state),
            model=string_attr(camera.model),
        )
        for camera in await client.get_cameras_public()
    ]
    return sorted(cameras, key=lambda camera: (camera.name.casefold(), camera.id))


def string_attr(value: object | None) -> str | None:
    """Convert an optional model attribute to text."""
    return None if value is None else str(value)


def camera_name(camera: CameraInfo | PublicCamera) -> str:
    """Return a human-readable camera name."""
    return string_attr(camera.name) or string_attr(camera.id) or "camera"


def camera_id(camera: CameraInfo | PublicCamera) -> str:
    """Return the required camera identifier."""
    value = string_attr(camera.id)
    if not value:
        message = "selected camera is missing an id"
        raise TimelapseError(message)
    return value

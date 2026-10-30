"""UniFi Protect connection and camera operations."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Self
from urllib.parse import ParseResult, urlparse

from platformdirs import user_config_path
from uiprotect import ProtectApiClient
from uiprotect.exceptions import NvrError

from timelapse import ProtectRateLimitError, TimelapseError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiohttp import ClientResponse
    from uiprotect.data import PublicCamera

    from timelapse.config import ConnectionSettings

CLIENT_CLOSE_TIMEOUT_SECONDS = 5.0
EXPORT_SLOTS = 2
THUMBNAIL_SLOTS = 2
PROTECT_RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 30.0
RETRY_AFTER_MAX_WAIT_SECONDS = 5 * 60.0
RETRY_JITTER_RATIO = 0.25
RETRY_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
HTTP_TOO_MANY_REQUESTS = 429
_LOGGER = logging.getLogger(__name__)
_SESSION_DIRECTORY = user_config_path("TimeLapse") / "protect"


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


@dataclass(frozen=True)
class _ClientKey:
    instance_url: str
    username: str
    password_digest: str
    token_digest: str
    verify_ssl: bool


@dataclass
class ConsoleSlots:
    """Private-operation limits for one console; thumbnails never wait behind exports."""

    exports: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(EXPORT_SLOTS))
    thumbnails: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(THUMBNAIL_SLOTS))


class _RetryingProtectApiClient(ProtectApiClient):
    """Protect client with actionable login errors and explicit bounded retries."""

    rate_limit_retry_after: float | None = None

    async def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        require_auth: bool = False,  # noqa: FBT001, FBT002 - upstream override
        auto_close: bool = True,  # noqa: FBT001, FBT002 - upstream override
        public_api: bool = False,  # noqa: FBT001, FBT002 - upstream override
        **kwargs: object,
    ) -> ClientResponse:
        """Retry transient responses with jitter while respecting Retry-After."""
        self.rate_limit_retry_after = None
        for attempt in range(PROTECT_RETRY_ATTEMPTS + 1):
            response = await super().request(
                method,
                url,
                require_auth=require_auth,
                auto_close=False,
                public_api=public_api,
                **kwargs,
            )
            if response.status not in RETRY_STATUS_CODES or attempt >= PROTECT_RETRY_ATTEMPTS:
                if response.status == HTTP_TOO_MANY_REQUESTS:
                    self.rate_limit_retry_after = _parse_retry_after(response)
                if auto_close:
                    response.release()
                return response

            retry_after = _parse_retry_after(response)
            if retry_after is not None and retry_after > RETRY_AFTER_MAX_WAIT_SECONDS:
                self.rate_limit_retry_after = retry_after
                if auto_close:
                    response.release()
                return response

            delay = _retry_delay(attempt, retry_after)
            status = response.status
            response.release()
            _LOGGER.warning(
                "Protect request returned HTTP %d; retrying in %.2fs (attempt %d/%d)",
                status,
                delay,
                attempt + 1,
                PROTECT_RETRY_ATTEMPTS,
            )
            await asyncio.sleep(delay)

        message = "unreachable Protect retry state"
        raise AssertionError(message)

    async def authenticate(self) -> None:
        """Log in unless a session is live; uiprotect collapses concurrent callers."""
        if self.is_authenticated():
            return
        try:
            await super().authenticate()
        except NvrError as exc:
            if _is_http_429(exc):
                raise rate_limit_error(self.rate_limit_retry_after) from exc
            raise
        _secure_session_storage()

    async def _raise_for_status(
        self,
        response: ClientResponse,
        raise_exception: bool = True,  # noqa: FBT001, FBT002 - upstream override
    ) -> None:
        """Turn exhausted 429 responses into an actionable application error."""
        if raise_exception and response.status == HTTP_TOO_MANY_REQUESTS:
            retry_after = _parse_retry_after(response)
            self.rate_limit_retry_after = retry_after
            raise rate_limit_error(retry_after)
        await super()._raise_for_status(response, raise_exception)  # pyright: ignore[reportPrivateUsage]


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


def create_client(config: ConnectionSettings, connection: ProtectConnection) -> ProtectApiClient:
    """Create a client supporting public API keys and private video export."""
    _prepare_session_directory()
    return _RetryingProtectApiClient(
        connection.host,
        connection.port,
        username=config.username,
        password=config.password,
        api_key=config.token,
        verify_ssl=config.verify_ssl,
        config_dir=_SESSION_DIRECTORY,
        store_sessions=True,
        # Retry behavior is implemented above so Retry-After HTTP dates and
        # long server-requested waits are handled deliberately.
        max_retries=0,
    )


class ProtectClients:
    """Own one client per credential set, and per-console slots, until closed."""

    def __init__(self) -> None:
        """Start empty; clients are created by the first operation that needs them."""
        self._clients: dict[_ClientKey, ProtectApiClient] = {}
        self._slots: dict[tuple[str, int], ConsoleSlots] = {}

    async def __aenter__(self) -> Self:
        """Enter a scope that closes every client on exit."""
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Close every client created in this scope."""
        await self.close()

    def get(self, config: ConnectionSettings, connection: ProtectConnection) -> ProtectApiClient:
        """Return the shared client for these credentials."""
        key = _client_key(config)
        client = self._clients.get(key)
        if client is None:
            client = self._clients[key] = create_client(config, connection)
        return client

    def slots(self, connection: ProtectConnection) -> ConsoleSlots:
        """Return the private-operation limits for one console."""
        return self._slots.setdefault((connection.host, connection.port), ConsoleSlots())

    async def close(self) -> None:
        """Close all clients; a later operation starts fresh ones."""
        clients = tuple(self._clients.values())
        self._clients.clear()
        self._slots.clear()
        await asyncio.gather(*(_close_client(client) for client in clients), return_exceptions=True)


@asynccontextmanager
async def private_operation(
    slots: asyncio.Semaphore,
    client: ProtectApiClient,
    *,
    operation: str,
) -> AsyncIterator[None]:
    """Hold one slot and authenticate before private media work starts."""
    async with slots:
        try:
            await client.ensure_authenticated()
        except NvrError as exc:
            if _is_http_429(exc):
                retry_after = getattr(client, "rate_limit_retry_after", None)
                raise rate_limit_error(retry_after) from exc
            raise
        _LOGGER.debug("Protect private-operation slot acquired for %s", operation)
        yield


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


def _client_key(config: ConnectionSettings) -> _ClientKey:
    return _ClientKey(
        instance_url=config.instance_url.rstrip("/"),
        username=config.username,
        password_digest=hashlib.sha256(config.password.encode()).hexdigest(),
        token_digest=hashlib.sha256(config.token.encode()).hexdigest(),
        verify_ssl=config.verify_ssl,
    )


def _parse_retry_after(response: ClientResponse) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        base = retry_after
        jitter = random.uniform(0, base * RETRY_JITTER_RATIO)  # noqa: S311 - retry jitter is not cryptographic
    else:
        base = min(RETRY_BASE_DELAY_SECONDS * (2**attempt), RETRY_MAX_DELAY_SECONDS)
        jitter = random.uniform(-base * RETRY_JITTER_RATIO, base * RETRY_JITTER_RATIO)  # noqa: S311
    return max(min(base + jitter, RETRY_AFTER_MAX_WAIT_SECONDS), 0.1)


def _is_http_429(error: BaseException) -> bool:
    text = str(error).casefold()
    return "status: 429" in text or "http 429" in text or "too many requests" in text


def rate_limit_error(retry_after: float | None) -> ProtectRateLimitError:
    """Build a consistent, actionable exhausted-rate-limit error."""
    wait = (
        f" Wait at least {retry_after:.0f} seconds before trying again."
        if retry_after is not None and retry_after > 0
        else " Let the console's authentication rate-limit window clear before trying again."
    )
    retry_not_before = datetime.now(UTC) + timedelta(seconds=retry_after) if retry_after is not None else None
    return ProtectRateLimitError(
        "UniFi Protect returned HTTP 429 after bounded retries."
        f"{wait} TimeLapse will reuse the saved session; do not rotate accounts or start repeated copies.",
        retry_not_before=retry_not_before,
    )


def _prepare_session_directory() -> None:
    _SESSION_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        _SESSION_DIRECTORY.chmod(0o700)


def _secure_session_storage() -> None:
    if os.name == "nt":
        return
    config_file = _SESSION_DIRECTORY / "unifi_protect.json"
    try:
        config_file.chmod(0o600)
    except FileNotFoundError:
        return


async def _close_client(client: ProtectApiClient) -> None:
    try:
        await asyncio.wait_for(client.close_session(), timeout=CLIENT_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOGGER.warning("Protect client cleanup timed out")
    except Exception:
        _LOGGER.warning("Protect client cleanup failed", exc_info=True)

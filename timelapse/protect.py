"""UniFi Protect connection and camera operations."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path  # noqa: TC003 - constructs the module-level session directory
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import ParseResult, urlparse

from platformdirs import user_config_path
from uiprotect import ProtectApiClient
from uiprotect.exceptions import NvrError

from timelapse import ProtectRateLimitError, TimelapseError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiohttp import ClientResponse
    from uiprotect.data import PublicCamera

    from timelapse.config import Config

CLIENT_CLOSE_TIMEOUT_SECONDS = 5.0
PRIVATE_OPERATION_SLOTS = 2
PROTECT_RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 30.0
RETRY_AFTER_MAX_WAIT_SECONDS = 5 * 60.0
RETRY_JITTER_RATIO = 0.25
RETRY_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
HTTP_TOO_MANY_REQUESTS = 429
LOCK_POLL_SECONDS = 0.1
_LOGGER = logging.getLogger(__name__)
_SESSION_DIRECTORY = user_config_path("TimeLapse") / "protect"
_CLIENT_POOL: ContextVar[dict[_ClientKey, ProtectApiClient] | None] = ContextVar(
    "timelapse_protect_client_pool",
    default=None,
)


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


class _RetryingProtectApiClient(ProtectApiClient):
    """Protect client with coordinated login and explicit bounded retries."""

    rate_limit_retry_after: float | None = None
    _force_fresh_login = False

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
        """Collapse login attempts across clients and backend processes."""
        if self.is_authenticated():
            return
        previous_cookie = self._last_token_cookie  # pyright: ignore[reportPrivateUsage]
        async with _exclusive_file_lock(_SESSION_DIRECTORY / "authentication.lock"):
            if self.is_authenticated():
                return

            # Another process may have refreshed the persisted cookie while this
            # client waited for the authentication lock.
            self._loaded_session = False  # pyright: ignore[reportPrivateUsage]
            await self._load_session()  # pyright: ignore[reportPrivateUsage]
            loaded_cookie = self._last_token_cookie  # pyright: ignore[reportPrivateUsage]
            if self.is_authenticated() and (not self._force_fresh_login or loaded_cookie != previous_cookie):
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

    async def _reauthenticate(self) -> None:
        """Force one coordinated login when UniFi invalidates a live cookie early."""
        self._force_fresh_login = True
        try:
            await super()._reauthenticate()  # pyright: ignore[reportPrivateUsage]
        finally:
            self._force_fresh_login = False


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


@asynccontextmanager
async def protect_session_scope() -> AsyncIterator[None]:
    """Reuse Protect clients for all operations in the current async scope."""
    existing_pool = _CLIENT_POOL.get()
    if existing_pool is not None:
        yield
        return

    pool: dict[_ClientKey, ProtectApiClient] = {}
    token = _CLIENT_POOL.set(pool)
    try:
        yield
    finally:
        await asyncio.gather(
            *(_close_client(client, operation="session scope") for client in pool.values()),
            return_exceptions=True,
        )
        _CLIENT_POOL.reset(token)


@asynccontextmanager
async def protect_client(config: Config, connection: ProtectConnection) -> AsyncIterator[ProtectApiClient]:
    """Return a pooled client, or an operation-owned client outside a scope."""
    pool = _CLIENT_POOL.get()
    if pool is None:
        client = create_client(config, connection)
        try:
            yield client
        finally:
            await _close_client(client, operation="standalone operation")
        return

    key = _client_key(config)
    client = pool.get(key)
    if client is None:
        client = create_client(config, connection)
        pool[key] = client
    yield client


@asynccontextmanager
async def private_operation(
    connection: ProtectConnection,
    client: ProtectApiClient,
    *,
    operation: str,
) -> AsyncIterator[None]:
    """Limit private media work per console and authenticate before it starts."""
    lock_stem = hashlib.sha256(f"{connection.host}:{connection.port}".encode()).hexdigest()[:20]
    async with _first_available_lock(
        tuple(_SESSION_DIRECTORY / f"private-{lock_stem}-{slot}.lock" for slot in range(PRIVATE_OPERATION_SLOTS))
    ):
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


def _client_key(config: Config) -> _ClientKey:
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
    return ProtectRateLimitError(
        "UniFi Protect returned HTTP 429 after bounded retries."
        f"{wait} TimeLapse will reuse the saved session; do not rotate accounts or start repeated copies."
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


async def _close_client(client: ProtectApiClient, *, operation: str) -> None:
    try:
        await asyncio.wait_for(client.close_session(), timeout=CLIENT_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOGGER.warning("Protect client cleanup timed out after %s", operation)
    except Exception:
        _LOGGER.warning("Protect client cleanup failed after %s", operation, exc_info=True)


class _LockedFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: BinaryIO | None = None

    def try_acquire(self) -> bool:
        _prepare_session_directory()
        file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                file.seek(0, os.SEEK_END)
                if file.tell() == 0:
                    file.write(b"\0")
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()
            return False
        except OSError as exc:
            file.close()
            if os.name == "nt" and getattr(exc, "winerror", None) in {33, 36}:
                return False
            raise
        self.file = file
        return True

    def release(self) -> None:
        file = self.file
        self.file = None
        if file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()


@asynccontextmanager
async def _exclusive_file_lock(path: Path) -> AsyncIterator[None]:
    lock = _LockedFile(path)
    while not await _try_acquire(lock):  # noqa: ASYNC110 - cancellable lock polling
        await asyncio.sleep(LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        await asyncio.shield(asyncio.to_thread(lock.release))


@asynccontextmanager
async def _first_available_lock(paths: tuple[Path, ...]) -> AsyncIterator[None]:
    if not paths:
        message = "at least one private-operation slot is required"
        raise ValueError(message)
    selected: _LockedFile | None = None
    while selected is None:
        for path in paths:
            candidate = _LockedFile(path)
            if await _try_acquire(candidate):
                selected = candidate
                break
        if selected is None:
            await asyncio.sleep(LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        await asyncio.shield(asyncio.to_thread(selected.release))


async def _try_acquire(lock: _LockedFile) -> bool:
    """Avoid leaking a just-acquired OS lock when its waiter is cancelled."""
    task = asyncio.create_task(asyncio.to_thread(lock.try_acquire))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if result is True:
            await asyncio.shield(asyncio.to_thread(lock.release))
        raise

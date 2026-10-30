"""Durable definitions of Daily Automations.

An automation's only durable progress is ``next_day``. Which artifacts exist is
answered by the output directory, so an interrupted day is simply run again.
"""

from __future__ import annotations

import json
import os
import secrets
import unicodedata
import uuid
from contextlib import AbstractContextManager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timelapse import TimelapseError
from timelapse.config import SPEED_TO_FPS

REGISTRY_VERSION = 3
READABLE_REGISTRY_VERSIONS = frozenset({2, REGISTRY_VERSION})
AUTOMATION_ID_PREFIX = "auto_"
AUTOMATION_ID_HEX_LENGTH = 32
AUTOMATION_STATUSES = frozenset({"active", "paused", "stopped", "removing"})
CONNECTION_KINDS = frozenset({"python-profile", "dotenv", "web-environment"})

AutomationStatus = Literal["active", "paused", "stopped", "removing"]
ConnectionKind = Literal["python-profile", "dotenv", "web-environment"]


class RegistryError(TimelapseError, ValueError):
    """Raised when durable automation state is invalid or unavailable."""


class RegistryOwnedError(RegistryError):
    """Raised when another process owns a registry."""


@dataclass(frozen=True)
class ConnectionReference:
    """A non-secret pointer to credentials owned by an entry point."""

    kind: ConnectionKind
    value: str

    def __post_init__(self) -> None:
        if self.kind not in CONNECTION_KINDS:
            message = f"unsupported connection reference kind: {self.kind}"
            raise RegistryError(message)
        normalized = self.value.strip()
        if self.kind == "dotenv":
            path = Path(normalized).expanduser()
            if not path.is_absolute():
                message = "dotenv connection references must use an absolute path"
                raise RegistryError(message)
            normalized = str(path.resolve(strict=False))
        elif not normalized:
            message = "connection reference cannot be empty"
            raise RegistryError(message)
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True)
class AutomationCamera:
    """Stable camera identity captured by a Daily Automation."""

    id: str
    name: str

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip():
            message = "automation cameras require an ID and display name"
            raise RegistryError(message)


@dataclass(frozen=True)
class DailyAutomation:
    """Durable intent to export every completed calendar day."""

    id: str
    name: str
    cameras: tuple[AutomationCamera, ...]
    connection: ConnectionReference
    speed: str
    output_directory: str
    timezone: str
    created_at: datetime
    status: AutomationStatus
    next_day: date
    last_completed_day: date | None = None
    consecutive_failures: int = 0
    next_retry_at: datetime | None = None
    last_error: str | None = None

    @property
    def output_path(self) -> Path:
        """Return the stored canonical output directory."""
        return Path(self.output_directory)


def normalize_display_name(value: str) -> str:
    """Trim and normalize a user-visible automation name."""
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        message = "automation name cannot be empty"
        raise RegistryError(message)
    if parse_automation_id(normalized) is not None:
        message = "automation names cannot use the automation-ID format"
        raise RegistryError(message)
    return normalized


def parse_automation_id(value: str) -> str | None:
    """Return a canonical automation ID when the selector has that syntax."""
    if not value.startswith(AUTOMATION_ID_PREFIX):
        return None
    suffix = value.removeprefix(AUTOMATION_ID_PREFIX)
    if len(suffix) != AUTOMATION_ID_HEX_LENGTH:
        return None
    try:
        uuid.UUID(hex=suffix)
    except ValueError:
        return None
    return f"{AUTOMATION_ID_PREFIX}{suffix.lower()}"


def validate_timezone(name: str) -> str:
    """Validate and return one IANA timezone name."""
    try:
        zone = ZoneInfo(name.strip())
    except (ValueError, ZoneInfoNotFoundError) as exc:
        message = f"timezone must be a valid IANA name; got {name!r}"
        raise RegistryError(message) from exc
    if zone.key is None:
        message = f"timezone must be a named IANA zone; got {name!r}"
        raise RegistryError(message)
    return zone.key


def canonical_output_directory(path: Path, *, must_exist: bool = True) -> Path:
    """Resolve and validate an automation output directory."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        message = "automation output directory must be absolute"
        raise RegistryError(message)
    try:
        canonical = expanded.resolve(strict=must_exist)
        if must_exist:
            if not canonical.is_dir():
                message = f"automation output directory is not a directory: {canonical}"
                raise RegistryError(message)
            with os.scandir(canonical):
                pass
    except OSError as exc:
        message = f"automation output directory is unavailable: {expanded}: {exc}"
        raise RegistryError(message) from exc
    return canonical


class RegistryOwner(AbstractContextManager["RegistryOwner"]):
    """Exclusive lifetime lock for a registry, released by the OS on exit."""

    def __init__(self, registry_path: Path) -> None:
        self.path = registry_path.with_suffix(f"{registry_path.suffix}.lock")
        self._file: IO[bytes] | None = None

    def acquire(self) -> Self:
        """Acquire the lock without waiting."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        try:
            _lock_file(file)
        except OSError as exc:
            file.close()
            message = f"another process owns automation registry {self.path.with_suffix('')}"
            raise RegistryOwnedError(message) from exc
        self._file = file
        return self

    def release(self) -> None:
        """Release the owner lock."""
        file, self._file = self._file, None
        if file is None:
            return
        try:
            _unlock_file(file)
        finally:
            file.close()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *args: object) -> None:
        self.release()


class AutomationRegistry:
    """Validate and atomically persist automation definitions."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self._automations: dict[str, DailyAutomation] = {}

    def owner(self) -> RegistryOwner:
        """Create a lifetime owner lock for this registry."""
        return RegistryOwner(self.path)

    def load(self) -> None:
        """Load and validate the registry, quarantining an unreadable file."""
        if self.path.parent.exists():
            for leftover in self.path.parent.glob(f".{self.path.name}.*.tmp"):
                with suppress(OSError):
                    leftover.unlink()
        if not self.path.exists():
            self._automations = {}
            return
        try:
            self._automations = _decode_state(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, KeyError, TypeError, ValueError, AttributeError, RegistryError) as exc:
            quarantined = self._quarantine_invalid()
            message = f"automation registry was invalid and moved to {quarantined}: {exc}"
            raise RegistryError(message) from exc

    def persist(self) -> None:
        """Replace the registry file atomically."""
        payload = {
            "version": REGISTRY_VERSION,
            "automations": [_encode_automation(item) for item in self._automations.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.path)
        except OSError as exc:
            message = f"could not persist automation registry {self.path}: {exc}"
            raise RegistryError(message) from exc
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def list(self) -> tuple[DailyAutomation, ...]:
        """Return automations ordered by display name and ID."""
        return tuple(sorted(self._automations.values(), key=lambda item: (item.name.casefold(), item.id)))

    def contains(self, automation_id: str) -> bool:
        """Return whether a Daily Automation definition is still retained."""
        return automation_id in self._automations

    def resolve(self, selector: str) -> DailyAutomation:
        """Resolve an immutable ID before a normalized display name."""
        parsed_id = parse_automation_id(selector.strip())
        if parsed_id is not None:
            match = self._automations.get(parsed_id)
        else:
            key = normalize_display_name(selector).casefold()
            match = next((item for item in self._automations.values() if item.name.casefold() == key), None)
        if match is None:
            message = f"automation does not exist: {selector}"
            raise RegistryError(message)
        return match

    def add(
        self,
        *,
        name: str,
        cameras: tuple[AutomationCamera, ...],
        connection: ConnectionReference,
        speed: str,
        output_directory: Path,
        timezone: str,
        now: datetime | None = None,
        next_day: date | None = None,
    ) -> DailyAutomation:
        """Validate and persist a new Daily Automation."""
        normalized_name = self._available_name(name)
        unique_cameras = _unique_cameras(cameras)
        if speed not in SPEED_TO_FPS:
            message = f"unsupported automation speed: {speed}"
            raise RegistryError(message)
        canonical = canonical_output_directory(output_directory)
        timezone_name = validate_timezone(timezone)
        self._reject_overlap(canonical, unique_cameras, speed)
        created_at = now or datetime.now(UTC)
        if created_at.tzinfo is None:
            message = "automation creation time must include a timezone"
            raise RegistryError(message)
        automation = DailyAutomation(
            id=f"{AUTOMATION_ID_PREFIX}{uuid.uuid4().hex}",
            name=normalized_name,
            cameras=unique_cameras,
            connection=connection,
            speed=speed,
            output_directory=str(canonical),
            timezone=timezone_name,
            created_at=created_at,
            status="active",
            next_day=next_day or created_at.astimezone(ZoneInfo(timezone_name)).date() - timedelta(days=1),
        )
        self._commit({**self._automations, automation.id: automation})
        return automation

    def edit(
        self,
        selector: str,
        *,
        name: str | None = None,
        cameras: tuple[AutomationCamera, ...] | None = None,
        connection: ConnectionReference | None = None,
    ) -> DailyAutomation:
        """Edit the mutable identity and connection fields."""
        automation = self.resolve(selector)
        updated_name = automation.name if name is None else self._available_name(name, excluding=automation.id)
        updated_cameras = automation.cameras if cameras is None else _unique_cameras(cameras)
        self._reject_overlap(automation.output_path, updated_cameras, automation.speed, excluding=automation.id)
        return self._update(
            automation,
            name=updated_name,
            cameras=updated_cameras,
            connection=connection or automation.connection,
        )

    def stop(self, selector: str) -> DailyAutomation:
        """Prevent future batches while allowing submitted work to drain."""
        automation = self.resolve(selector)
        if automation.status == "removing":
            message = "an automation being removed cannot be stopped"
            raise RegistryError(message)
        return self._update(automation, status="stopped", next_retry_at=None)

    def resume(self, selector: str) -> DailyAutomation:
        """Resume a paused or stopped automation and reset batch failures."""
        automation = self.resolve(selector)
        if automation.status not in {"paused", "stopped"}:
            message = "only paused or stopped automations can be resumed"
            raise RegistryError(message)
        return self._update(automation, status="active", consecutive_failures=0, next_retry_at=None, last_error=None)

    def pause(self, selector: str, error: str) -> DailyAutomation:
        """Record a safety failure that requires explicit Resume."""
        automation = self.resolve(selector)
        if automation.status in {"stopped", "removing"}:
            return automation
        return self._update(automation, status="paused", next_retry_at=None, last_error=error)

    def record_failure(self, selector: str, *, attempt: int, error: str, retry_at: datetime | None) -> DailyAutomation:
        """Record a failed day and the time its next attempt may start."""
        return self._update(
            self.resolve(selector),
            consecutive_failures=attempt,
            last_error=error,
            next_retry_at=retry_at,
        )

    def record_success(self, selector: str, day: date, *, advance_day: bool) -> DailyAutomation:
        """Clear failures and, for the oldest pending day, record a Processed Day."""
        automation = self.resolve(selector)
        changes: dict[str, object] = {"consecutive_failures": 0, "next_retry_at": None, "last_error": None}
        if advance_day:
            if automation.next_day != day:
                message = "Processed Day must advance the oldest pending day exactly once"
                raise RegistryError(message)
            changes |= {"next_day": day + timedelta(days=1), "last_completed_day": day}
        return self._update(automation, **changes)

    def begin_remove(self, selector: str) -> DailyAutomation:
        """Mark an automation for removal so no new work starts."""
        return self._update(self.resolve(selector), status="removing", next_retry_at=None)

    def delete(self, automation_id: str) -> None:
        """Forget a definition; existing artifacts are kept."""
        remaining = {key: value for key, value in self._automations.items() if key != automation_id}
        self._commit(remaining)

    def _update(self, automation: DailyAutomation, **changes: object) -> DailyAutomation:
        updated = replace(automation, **changes)  # pyright: ignore[reportArgumentType]
        self._commit({**self._automations, automation.id: updated})
        return updated

    def _commit(self, automations: dict[str, DailyAutomation]) -> None:
        previous = self._automations
        self._automations = automations
        try:
            self.persist()
        except Exception:
            self._automations = previous
            raise

    def _available_name(self, name: str, *, excluding: str | None = None) -> str:
        normalized = normalize_display_name(name)
        if any(
            item.id != excluding and item.name.casefold() == normalized.casefold()
            for item in self._automations.values()
        ):
            message = f"automation name is already in use: {normalized!r}"
            raise RegistryError(message)
        return normalized

    def _reject_overlap(
        self,
        output: Path,
        cameras: tuple[AutomationCamera, ...],
        speed: str,
        *,
        excluding: str | None = None,
    ) -> None:
        directory = _directory_key(output)
        camera_ids = {camera.id for camera in cameras}
        for existing in self._automations.values():
            if existing.id == excluding or existing.speed != speed or _directory_key(existing.output_path) != directory:
                continue
            overlap = camera_ids.intersection(camera.id for camera in existing.cameras)
            if overlap:
                message = (
                    f"automation overlaps retained automation {existing.name!r} for camera "
                    f"{sorted(overlap)[0]} in {output} at {speed}"
                )
                raise RegistryError(message)

    def _quarantine_invalid(self) -> Path:
        quarantined = self.path.with_name(f"{self.path.stem}.invalid-{secrets.token_hex(4)}{self.path.suffix}")
        try:
            self.path.replace(quarantined)
        except OSError as exc:
            message = f"could not quarantine invalid automation registry {self.path}: {exc}"
            raise RegistryError(message) from exc
        return quarantined


def _unique_cameras(cameras: tuple[AutomationCamera, ...]) -> tuple[AutomationCamera, ...]:
    unique = tuple({camera.id: camera for camera in cameras}.values())
    if not unique:
        message = "automation requires at least one camera"
        raise RegistryError(message)
    return unique


def _directory_key(path: Path) -> str:
    return str(path.resolve(strict=False)).casefold()


def _encode_automation(automation: DailyAutomation) -> dict[str, object]:
    return {
        "id": automation.id,
        "name": automation.name,
        "cameras": [asdict(camera) for camera in automation.cameras],
        "connection": asdict(automation.connection),
        "speed": automation.speed,
        "output_directory": automation.output_directory,
        "timezone": automation.timezone,
        "created_at": automation.created_at.isoformat(),
        "status": automation.status,
        "next_day": automation.next_day.isoformat(),
        "last_completed_day": automation.last_completed_day.isoformat() if automation.last_completed_day else None,
        "consecutive_failures": automation.consecutive_failures,
        "next_retry_at": automation.next_retry_at.isoformat() if automation.next_retry_at else None,
        "last_error": automation.last_error,
    }


def _decode_state(payload: dict[str, object]) -> dict[str, DailyAutomation]:
    version = payload.get("version")
    if version not in READABLE_REGISTRY_VERSIONS:
        message = f"unsupported automation registry version: {version}"
        raise RegistryError(message)
    automations: dict[str, DailyAutomation] = {}
    batch_owners = {batch["automation_id"] for batch in payload.get("batches", ())}  # type: ignore[union-attr]
    for item in payload["automations"]:  # type: ignore[union-attr]
        automation = _decode_automation(item)
        if automation.id in automations or any(
            existing.name.casefold() == automation.name.casefold() for existing in automations.values()
        ):
            message = f"duplicate automation: {automation.name}"
            raise RegistryError(message)
        if version == 2 and automation.id in batch_owners:  # noqa: PLR2004 - older files only imply this field
            automation = replace(automation, last_completed_day=automation.next_day - timedelta(days=1))
        # A crash during removal leaves nothing to finish: the intent was to forget it.
        if automation.status != "removing":
            automations[automation.id] = automation
    return automations


def _decode_automation(item: dict[str, object]) -> DailyAutomation:
    identifier = str(item["id"])
    if parse_automation_id(identifier) != identifier:
        message = f"invalid automation ID: {identifier}"
        raise RegistryError(message)
    status = item["status"]
    if status not in AUTOMATION_STATUSES:
        message = f"invalid automation status: {status}"
        raise RegistryError(message)
    speed = str(item["speed"])
    if speed not in SPEED_TO_FPS:
        message = f"unsupported automation speed: {speed}"
        raise RegistryError(message)
    connection = item["connection"]
    retry_at = item.get("next_retry_at")
    completed_day = item.get("last_completed_day")
    return DailyAutomation(
        id=identifier,
        name=normalize_display_name(str(item["name"])),
        cameras=_unique_cameras(tuple(AutomationCamera(str(c["id"]), str(c["name"])) for c in item["cameras"])),  # type: ignore[union-attr]
        connection=ConnectionReference(connection["kind"], connection["value"]),  # type: ignore[index]
        speed=speed,
        output_directory=str(canonical_output_directory(Path(str(item["output_directory"])), must_exist=False)),
        timezone=validate_timezone(str(item["timezone"])),
        created_at=_aware_datetime(item["created_at"]),
        status=status,  # type: ignore[arg-type]
        next_day=date.fromisoformat(str(item["next_day"])),
        last_completed_day=date.fromisoformat(str(completed_day)) if completed_day else None,
        consecutive_failures=int(item.get("consecutive_failures", 0)),  # type: ignore[arg-type]
        next_retry_at=_aware_datetime(retry_at) if retry_at else None,
        last_error=None if item.get("last_error") is None else str(item["last_error"]),
    )


def _aware_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.utcoffset() is None:
        message = f"stored time must include a timezone: {value}"
        raise RegistryError(message)
    return parsed


def _lock_file(file: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415

        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl  # noqa: PLC0415

    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(file: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl  # noqa: PLC0415

    fcntl.flock(file.fileno(), fcntl.LOCK_UN)

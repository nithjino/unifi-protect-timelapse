"""Daily Automations: the stored definitions, their transitions and the registry file."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import sys
import unicodedata
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timelapse import InvalidRequestError, NotFoundError, TimelapseError
from timelapse.model import SPEEDS, Camera, latest_complete_day, write_json

LOGGER = logging.getLogger(__name__)
ID_PATTERN = re.compile(r"auto_[0-9a-f]{32}")
CONNECTION_KINDS = ("python-profile", "dotenv", "web-environment")
STATUSES = ("active", "paused", "stopped")


class RegistryOwnedError(TimelapseError):
    """Another process holds the registry lock."""


def clean_name(name: str) -> str:
    """Trim and NFC-normalise an automation name."""
    return unicodedata.normalize("NFC", name.strip())


def valid_timezone(name: str) -> ZoneInfo:
    """Return the named IANA zone, or raise ``InvalidRequestError``."""
    try:
        if not name or name != name.strip():
            raise ValueError(name)  # noqa: TRY301
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise InvalidRequestError(f"timezone must be a valid IANA timezone name: {name!r}") from error


@dataclass(frozen=True)
class Automation:
    """A named, durable intent to export every completed day for a set of cameras."""

    id: str
    name: str
    cameras: tuple[Camera, ...]
    connection: tuple[str, str]
    speed: str
    output_directory: Path
    timezone: str
    created_at: datetime
    status: str
    next_day: date
    last_completed_day: date | None = None
    consecutive_failures: int = 0
    next_retry_at: datetime | None = None
    last_error: str | None = None

    def stopped(self) -> Automation:
        """Stop; a running day may still finish."""
        return replace(self, status="stopped", next_retry_at=None)

    def resumed(self) -> Automation:
        """Make a paused or stopped automation active again."""
        if self.status not in {"paused", "stopped"}:
            raise InvalidRequestError("only paused or stopped automations can be resumed")
        return replace(self, status="active", consecutive_failures=0, next_retry_at=None, last_error=None)

    def paused(self, error: str) -> Automation:
        """Pause with an explanation; a stopped automation stays stopped."""
        if self.status == "stopped":
            return self
        return replace(self, status="paused", last_error=error, next_retry_at=None)

    def failed(self, attempt: int, error: str, retry_at: datetime | None) -> Automation:
        """Record a failed attempt."""
        return replace(self, consecutive_failures=attempt, last_error=error, next_retry_at=retry_at)

    def succeeded(self, day: date, *, advance: bool) -> Automation:
        """Record a processed day, advancing ``next_day`` when asked."""
        done = replace(self, consecutive_failures=0, next_retry_at=None, last_error=None)
        if not advance:
            return done
        if day != self.next_day:
            raise InvalidRequestError(f"{day} cannot be recorded: each day is processed exactly once, in order")
        return replace(done, next_day=day + timedelta(days=1), last_completed_day=day)

    def to_dict(self) -> dict[str, Any]:
        """Return the registry entry."""
        return {
            "id": self.id,
            "name": self.name,
            "cameras": [{"id": camera.id, "name": camera.name} for camera in self.cameras],
            "connection": {"kind": self.connection[0], "value": self.connection[1]},
            "speed": self.speed,
            "output_directory": str(self.output_directory),
            "timezone": self.timezone,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "next_day": self.next_day.isoformat(),
            "last_completed_day": None if self.last_completed_day is None else self.last_completed_day.isoformat(),
            "consecutive_failures": self.consecutive_failures,
            "next_retry_at": None if self.next_retry_at is None else self.next_retry_at.isoformat(),
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, entry: dict[str, Any]) -> Automation:
        """Read a registry entry, raising ``ValueError``, ``KeyError`` or ``TypeError`` when it is malformed."""

        def moment(value: Any) -> datetime | None:
            if value is None:
                return None
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError(f"datetime without an offset: {value}")
            return parsed

        def text(value: Any) -> str:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"expected text, got {value!r}")
            return value

        cameras = tuple(Camera(text(item["id"]), text(item["name"])) for item in entry["cameras"])
        connection = (text(entry["connection"]["kind"]), text(entry["connection"]["value"]))
        failures = entry.get("consecutive_failures", 0)
        if type(failures) is not int or failures < 0:
            raise ValueError(f"invalid consecutive_failures {failures!r}")
        last_day = entry.get("last_completed_day")
        automation = cls(
            id=text(entry["id"]),
            name=text(entry["name"]),
            cameras=cameras,
            connection=connection,
            speed=text(entry["speed"]),
            output_directory=Path(text(entry["output_directory"])),
            timezone=text(entry["timezone"]),
            created_at=moment(entry["created_at"]) or datetime.now(UTC),
            status=text(entry["status"]),
            next_day=date.fromisoformat(entry["next_day"]),
            last_completed_day=None if last_day is None else date.fromisoformat(last_day),
            consecutive_failures=failures,
            next_retry_at=moment(entry.get("next_retry_at")),
            last_error=entry.get("last_error") if isinstance(entry.get("last_error"), str) else None,
        )
        valid_timezone(automation.timezone)
        if (
            not ID_PATTERN.fullmatch(automation.id)
            or not cameras
            or connection[0] not in CONNECTION_KINDS
            or automation.speed not in SPEEDS
            or not automation.output_directory.is_absolute()
            or automation.status not in STATUSES
        ):
            raise ValueError(f"invalid automation {automation.id}")
        return automation


def _directory_key(path: Path) -> str:
    return str(path.resolve()).casefold()


class Registry:
    """The registry file of one runtime; every change is saved before it takes effect."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, Automation] = {}

    @property
    def automations(self) -> list[Automation]:
        """Return automations sorted by case-folded name, then id."""
        return sorted(self._items.values(), key=lambda item: (item.name.casefold(), item.id))

    def load(self) -> None:
        """Read the registry file, moving an invalid one aside."""
        for leftover in self.path.parent.glob(f".{self.path.name}.*.tmp"):
            leftover.unlink(missing_ok=True)
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._items = {}
            return
        try:
            self._items = self._parse(json.loads(raw))
        except (ValueError, KeyError, TypeError, AttributeError, InvalidRequestError) as error:
            moved = self.path.with_name(f"{self.path.stem}.invalid-{secrets.token_hex(4)}{self.path.suffix}")
            self.path.rename(moved)
            raise TimelapseError(f"{self.path} is invalid ({error}); it was moved to {moved}") from error

    @staticmethod
    def _parse(data: dict[str, Any]) -> dict[str, Automation]:
        version = data["version"]
        if version not in (2, 3) or type(version) is not int:
            raise ValueError(f"unsupported registry version {version!r}")
        batch_owners = {batch.get("automation_id") for batch in data.get("batches") or []} if version == 2 else set()
        items: dict[str, Automation] = {}
        names: set[str] = set()
        for entry in data["automations"]:
            if entry.get("status") == "removing":
                continue
            automation = Automation.from_dict(entry)
            if automation.last_completed_day is None and automation.id in batch_owners:
                automation = replace(automation, last_completed_day=automation.next_day - timedelta(days=1))
            if automation.id in items or automation.name.casefold() in names:
                raise ValueError(f"duplicate automation {automation.name!r}")
            items[automation.id] = automation
            names.add(automation.name.casefold())
        return items

    def _commit(self, items: dict[str, Automation]) -> None:
        data = {"version": 3, "automations": [item.to_dict() for item in items.values()]}
        try:
            write_json(self.path, data, ensure_ascii=False)
        except OSError as error:
            raise TimelapseError(f"Could not save {self.path}: {error}") from error
        self._items = items

    def get(self, automation_id: str) -> Automation | None:
        """Return the automation with this id, if any."""
        return self._items.get(automation_id)

    def resolve(self, selector: str) -> Automation:
        """Find an automation by id, then by case-insensitive name."""
        if found := self._items.get(selector.strip()):
            return found
        name = clean_name(selector).casefold()
        for automation in self._items.values():
            if automation.name.casefold() == name:
                return automation
        raise NotFoundError(f"automation does not exist: {selector}")

    def _validated(self, automation: Automation) -> Automation:
        name = clean_name(automation.name)
        if not name:
            raise InvalidRequestError("Automation name is required.")
        if ID_PATTERN.fullmatch(name):
            raise InvalidRequestError("an automation name cannot look like an automation ID")
        cameras: dict[str, Camera] = {}
        for camera in automation.cameras:
            if not camera.id.strip() or not camera.name.strip():
                raise InvalidRequestError("every camera needs an ID and a name")
            cameras.setdefault(camera.id, Camera(camera.id, camera.name))
        if not cameras:
            raise InvalidRequestError("Select at least one available camera.")
        if automation.speed not in SPEEDS:
            raise InvalidRequestError("Choose a supported timelapse speed.")
        valid_timezone(automation.timezone)
        automation = replace(automation, name=name, cameras=tuple(cameras.values()))
        for other in self._items.values():
            if other.id == automation.id:
                continue
            if other.name.casefold() == name.casefold():
                raise InvalidRequestError(f"an automation named {other.name!r} already exists")
            if (
                _directory_key(other.output_directory) == _directory_key(automation.output_directory)
                and other.speed == automation.speed
                and {camera.id for camera in other.cameras} & set(cameras)
            ):
                raise InvalidRequestError(f"this automation overlaps {other.name!r}: same folder, speed and camera")
        return automation

    def add(
        self,
        *,
        name: str,
        cameras: Sequence[Camera],
        connection: tuple[str, str],
        speed: str,
        output_directory: Path,
        timezone: str,
        now: datetime | None = None,
        next_day: date | None = None,
    ) -> Automation:
        """Create an active automation that starts with yesterday."""
        if not output_directory.is_absolute():
            raise InvalidRequestError(f"the output directory must be absolute: {output_directory}")
        try:
            next(output_directory.iterdir(), None)
        except OSError as error:
            raise InvalidRequestError(f"the output directory must exist and be readable: {error}") from error
        now = now or datetime.now(UTC)
        automation = self._validated(
            Automation(
                id=f"auto_{uuid.uuid4().hex}",
                name=name,
                cameras=tuple(cameras),
                connection=connection,
                speed=speed,
                output_directory=output_directory.resolve(),
                timezone=timezone,
                created_at=now,
                status="active",
                next_day=next_day or latest_complete_day(now, valid_timezone(timezone)),
            )
        )
        self._commit({**self._items, automation.id: automation})
        return automation

    def edit(
        self,
        selector: str,
        *,
        name: str | None = None,
        cameras: Sequence[Camera] | None = None,
        connection: tuple[str, str] | None = None,
    ) -> Automation:
        """Change the name, cameras or connection."""
        current = self.resolve(selector)
        changed = replace(
            current,
            name=current.name if name is None else name,
            cameras=current.cameras if cameras is None else tuple(cameras),
            connection=connection or current.connection,
        )
        return self.put(self._validated(changed))

    def put(self, automation: Automation) -> Automation:
        """Save a transition of an existing automation."""
        if automation.id not in self._items:
            raise NotFoundError(f"automation does not exist: {automation.id}")
        self._commit({**self._items, automation.id: automation})
        return automation

    def delete(self, automation_id: str) -> None:
        """Remove an automation; its artifacts stay on disk."""
        if automation_id not in self._items:
            raise NotFoundError(f"automation does not exist: {automation_id}")
        self._commit({key: value for key, value in self._items.items() if key != automation_id})

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the registry exclusively for this process, failing at once if another holds it."""
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as file:
            try:
                if sys.platform == "win32":
                    import msvcrt  # noqa: PLC0415

                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl  # noqa: PLC0415

                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RegistryOwnedError(
                    f"Another TimeLapse process is using {self.path}. Stop the running automation process first."
                ) from error
            yield

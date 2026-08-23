"""Durable state for Daily Automations and their export work."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import unicodedata
import uuid
from contextlib import AbstractContextManager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Literal, Self, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timelapse import TimelapseError
from timelapse.config import SPEED_TO_FPS

REGISTRY_VERSION = 2
AUTOMATION_ID_PREFIX = "auto_"
AUTOMATION_ID_HEX_LENGTH = 32
MP4_MARKER_MINIMUM_OFFSET = 4
AUTOMATION_STATUSES = frozenset({"active", "paused", "stopped", "removing"})
JOB_STATUSES = frozenset({"pending", "queued", "running", "completed", "failed", "cancelled", "deleting"})

AutomationStatus = Literal["active", "paused", "stopped", "removing"]
JobStatus = Literal["pending", "queued", "running", "completed", "failed", "cancelled", "deleting"]
ConnectionKind = Literal["python-profile", "dotenv", "web-environment", "native-profile"]


class RegistryError(TimelapseError):
    """Raised when durable automation state is invalid or unavailable."""


class RegistryOwnedError(RegistryError):
    """Raised when another process owns a registry."""


@dataclass(frozen=True)
class ConnectionReference:
    """A non-secret pointer to credentials owned by an entry point."""

    kind: ConnectionKind
    value: str

    def __post_init__(self) -> None:
        if self.kind not in {"python-profile", "dotenv", "web-environment", "native-profile"}:
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
    consecutive_failures: int = 0
    next_retry_at: datetime | None = None
    last_error: str | None = None
    source_fingerprint: str | None = None
    imported_at: datetime | None = None

    @property
    def output_path(self) -> Path:
        """Return the stored canonical output directory."""
        return Path(self.output_directory)

    @property
    def paused(self) -> bool:
        """Return whether a safety pause blocks new work."""
        return self.status == "paused"

    @property
    def last_run_day(self) -> date | None:
        """Return the latest Processed Day for display adapters."""
        return self.next_day - timedelta(days=1)


@dataclass(frozen=True)
class ExportBatchRecord:
    """Durable work for one automation and local calendar day."""

    id: str
    automation_id: str
    day: date
    generation: int
    attempt: int
    created_at: datetime
    retry_not_before: datetime | None = None


@dataclass(frozen=True)
class ExportJobRecord:
    """Durable state for one camera export within a batch."""

    id: str
    batch_id: str
    automation_id: str | None
    camera: AutomationCamera
    output: str
    status: JobStatus
    attempt: int
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    retry_not_before: datetime | None = None
    deletion_requested: bool = False


@dataclass
class RegistryState:
    """Complete state written by one atomic registry replacement."""

    automations: dict[str, DailyAutomation] = field(default_factory=dict)
    batches: dict[str, ExportBatchRecord] = field(default_factory=dict)
    jobs: dict[str, ExportJobRecord] = field(default_factory=dict)
    reexport_generations: dict[str, int] = field(default_factory=dict)


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


def automation_name_key(value: str) -> str:
    """Return the Unicode-aware uniqueness key for an automation name."""
    return normalize_display_name(value).casefold()


def new_automation_id() -> str:
    """Create an opaque immutable automation identifier."""
    return f"{AUTOMATION_ID_PREFIX}{uuid.uuid4().hex}"


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
    normalized = name.strip()
    try:
        zone = ZoneInfo(normalized)
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
        if must_exist and not canonical.is_dir():
            message = f"automation output directory is not a directory: {canonical}"
            raise RegistryError(message)
        if must_exist:
            with os.scandir(canonical):
                pass
    except OSError as exc:
        message = f"automation output directory is unavailable: {expanded}: {exc}"
        raise RegistryError(message) from exc
    return canonical


def validate_mp4(path: Path) -> bool:
    """Return whether a path is a regular, nonempty MP4 with an ftyp marker."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            return False
        with path.open("rb") as file:
            header = file.read(4096)
    except OSError:
        return False
    return header.find(b"ftyp", MP4_MARKER_MINIMUM_OFFSET) >= MP4_MARKER_MINIMUM_OFFSET


class RegistryOwner(AbstractContextManager["RegistryOwner"]):
    """Exclusive lifetime lock and control-channel metadata for a registry."""

    def __init__(self, registry_path: Path, *, endpoint: str | None = None) -> None:
        self.path = registry_path.with_suffix(f"{registry_path.suffix}.lock")
        self.endpoint = endpoint
        self.nonce = secrets.token_urlsafe(32)
        self._file: IO[bytes] | None = None

    def acquire(self) -> Self:
        """Acquire the OS-released lock without waiting."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        try:
            _lock_file(file)
        except (BlockingIOError, OSError) as exc:
            file.close()
            message = f"another process owns automation registry {self.path.with_suffix('')}"
            raise RegistryOwnedError(message) from exc
        metadata = {
            "pid": os.getpid(),
            "endpoint": self.endpoint,
            "nonce": self.nonce,
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        file.seek(0)
        file.truncate()
        file.write(json.dumps(metadata, sort_keys=True).encode())
        file.flush()
        os.fsync(file.fileno())
        self._file = file
        return self

    def release(self) -> None:
        """Release the owner lock."""
        file = self._file
        self._file = None
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
    """Validate, migrate, recover, and atomically persist automation state."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self._state = RegistryState()

    @property
    def state(self) -> RegistryState:
        """Return the live registry state."""
        return self._state

    def owner(self, *, endpoint: str | None = None) -> RegistryOwner:
        """Create a lifetime owner lock for this registry."""
        return RegistryOwner(self.path, endpoint=endpoint)

    def load(self) -> RegistryState:
        """Load and validate the registry, recovering a complete temporary write."""
        self._recover_atomic_write()
        if not self.path.exists():
            self._state = RegistryState()
            return self._state
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._state = self._decode_state(payload)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, RegistryError) as exc:
            quarantined = self._quarantine_invalid()
            message = f"automation registry was invalid and moved to {quarantined}: {exc}"
            raise RegistryError(message) from exc
        self.reconcile()
        return self._state

    def persist(self) -> None:
        """Replace the registry atomically after validating all records."""
        payload = self._encode_state(self._state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with temporary.open("x", encoding="utf-8") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.path)
            _fsync_directory(self.path.parent)
        except OSError as exc:
            message = f"could not persist automation registry {self.path}: {exc}"
            raise RegistryError(message) from exc
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def list(self) -> list[DailyAutomation]:
        """Return automations ordered by display name and ID."""
        return sorted(self._state.automations.values(), key=lambda item: (item.name.casefold(), item.id))

    def resolve(self, selector: str) -> DailyAutomation:
        """Resolve an immutable ID before a normalized display name."""
        parsed_id = parse_automation_id(selector.strip())
        if parsed_id is not None:
            automation = self._state.automations.get(parsed_id)
            if automation is None:
                message = f"automation does not exist: {selector}"
                raise RegistryError(message)
            return automation
        key = automation_name_key(selector)
        match = next((item for item in self._state.automations.values() if item.name.casefold() == key), None)
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
        automation_id: str | None = None,
        source_fingerprint: str | None = None,
        imported_at: datetime | None = None,
    ) -> DailyAutomation:
        """Validate and persist a new Daily Automation."""
        normalized_name = normalize_display_name(name)
        if any(item.name.casefold() == normalized_name.casefold() for item in self._state.automations.values()):
            message = f"automation name is already in use: {normalized_name!r}"
            raise RegistryError(message)
        unique_cameras = tuple({camera.id: camera for camera in cameras}.values())
        if not unique_cameras:
            message = "automation requires at least one camera"
            raise RegistryError(message)
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
        initial_day = next_day or created_at.astimezone(ZoneInfo(timezone_name)).date().fromordinal(
            created_at.astimezone(ZoneInfo(timezone_name)).date().toordinal() - 1
        )
        identifier = automation_id or new_automation_id()
        if parse_automation_id(identifier) != identifier:
            message = f"invalid automation ID: {identifier}"
            raise RegistryError(message)
        if identifier in self._state.automations:
            message = f"automation ID already exists: {identifier}"
            raise RegistryError(message)
        automation = DailyAutomation(
            id=identifier,
            name=normalized_name,
            cameras=unique_cameras,
            connection=connection,
            speed=speed,
            output_directory=str(canonical),
            timezone=timezone_name,
            created_at=created_at,
            status="active",
            next_day=initial_day,
            source_fingerprint=source_fingerprint,
            imported_at=imported_at,
        )
        self._state.automations[identifier] = automation
        try:
            self.persist()
        except Exception:
            self._state.automations.pop(identifier, None)
            raise
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
        updated_name = normalize_display_name(name) if name is not None else automation.name
        if any(
            item.id != automation.id and item.name.casefold() == updated_name.casefold()
            for item in self._state.automations.values()
        ):
            message = f"automation name is already in use: {updated_name!r}"
            raise RegistryError(message)
        updated_cameras = automation.cameras if cameras is None else tuple({item.id: item for item in cameras}.values())
        if not updated_cameras:
            message = "automation requires at least one camera"
            raise RegistryError(message)
        self._reject_overlap(automation.output_path, updated_cameras, automation.speed, excluding=automation.id)
        updated = replace(
            automation,
            name=updated_name,
            cameras=updated_cameras,
            connection=connection or automation.connection,
        )
        self._replace_automation(updated)
        return updated

    def stop(self, selector: str) -> DailyAutomation:
        """Prevent future batches while allowing submitted work to drain."""
        automation = self.resolve(selector)
        if automation.status == "removing":
            message = "an automation being removed cannot be stopped"
            raise RegistryError(message)
        updated = replace(automation, status="stopped", next_retry_at=None)
        self._replace_automation(updated)
        return updated

    def resume(self, selector: str) -> DailyAutomation:
        """Resume a paused or stopped automation and reset batch failures."""
        automation = self.resolve(selector)
        if automation.status not in {"paused", "stopped"}:
            message = "only paused or stopped automations can be resumed"
            raise RegistryError(message)
        updated = replace(
            automation,
            status="active",
            consecutive_failures=0,
            next_retry_at=None,
            last_error=None,
        )
        self._replace_automation(updated)
        return updated

    def pause(self, selector: str, error: str) -> DailyAutomation:
        """Record a safety failure that requires explicit Resume."""
        automation = self.resolve(selector)
        if automation.status in {"stopped", "removing"}:
            return automation
        updated = replace(automation, status="paused", next_retry_at=None, last_error=error)
        self._replace_automation(updated)
        return updated

    def replace_automation(self, automation: DailyAutomation) -> None:
        """Persist a complete engine-owned automation transition."""
        if automation.id not in self._state.automations:
            message = f"automation does not exist: {automation.id}"
            raise RegistryError(message)
        self._replace_automation(automation)

    def begin_remove(self, selector: str) -> DailyAutomation | None:
        """Mark an automation for removal, deleting it when no work remains."""
        automation = self.resolve(selector)
        updated = replace(automation, status="removing", next_retry_at=None)
        self._replace_automation(updated)
        return self.finish_remove(updated.id)

    def finish_remove(self, automation_id: str) -> DailyAutomation | None:
        """Delete a removing definition after its nonterminal jobs finish."""
        automation = self._state.automations.get(automation_id)
        if automation is None or automation.status != "removing":
            return automation
        if any(
            job.automation_id == automation_id and job.status not in {"completed", "failed", "cancelled"}
            for job in self._state.jobs.values()
        ):
            return automation
        self._state.automations.pop(automation_id)
        self.persist()
        return None

    def put_batch(self, batch: ExportBatchRecord, jobs: tuple[ExportJobRecord, ...]) -> None:
        """Persist a batch and every job before any work is admitted."""
        old_batch = self._state.batches.get(batch.id)
        old_jobs = {job.id: self._state.jobs.get(job.id) for job in jobs}
        self._state.batches[batch.id] = batch
        self._state.jobs.update((job.id, job) for job in jobs)
        try:
            self.persist()
        except Exception:
            if old_batch is None:
                self._state.batches.pop(batch.id, None)
            else:
                self._state.batches[batch.id] = old_batch
            for job_id, old in old_jobs.items():
                if old is None:
                    self._state.jobs.pop(job_id, None)
                else:
                    self._state.jobs[job_id] = old
            raise

    def update_job(self, job: ExportJobRecord) -> None:
        """Persist one job transition."""
        previous = self._state.jobs.get(job.id)
        self._state.jobs[job.id] = job
        try:
            self.persist()
        except Exception:
            if previous is None:
                self._state.jobs.pop(job.id, None)
            else:
                self._state.jobs[job.id] = previous
            raise

    def delete_artifact(self, job_id: str) -> None:
        """Delete one tracked exact canonical regular file without rewinding progress."""
        job = self._state.jobs.get(job_id)
        if job is None:
            message = "export job does not exist"
            raise RegistryError(message)
        if job.automation_id is None:
            message = "artifact is not owned by a Daily Automation"
            raise RegistryError(message)
        automation = self._state.automations.get(job.automation_id)
        if automation is None:
            message = "artifact owner no longer exists"
            raise RegistryError(message)
        output = Path(job.output)
        canonical_parent = output.parent.resolve(strict=False)
        if canonical_parent != automation.output_path or output.is_symlink():
            message = "refusing to delete an artifact outside its automation output directory"
            raise RegistryError(message)
        deleting = replace(job, status="deleting", deletion_requested=True, updated_at=datetime.now(UTC))
        self.update_job(deleting)
        try:
            info = output.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            message = f"could not inspect tracked artifact {output}: {exc}"
            raise RegistryError(message) from exc
        else:
            if not stat.S_ISREG(info.st_mode):
                message = f"refusing to delete non-regular artifact: {output}"
                raise RegistryError(message)
            try:
                output.unlink()
            except OSError as exc:
                message = f"could not delete tracked artifact {output}: {exc}"
                raise RegistryError(message) from exc
        self._state.jobs.pop(job_id, None)
        self.persist()

    def next_reexport_generation(self, automation_id: str, day: date) -> int:
        """Increment and persist the generation for explicit re-export work."""
        key = f"{automation_id}:{day.isoformat()}"
        generation = self._state.reexport_generations.get(key, 0) + 1
        self._state.reexport_generations[key] = generation
        self.persist()
        return generation

    def reconcile(self) -> None:
        """Converge interrupted job transitions against on-disk artifacts."""
        changed = False
        now = datetime.now(UTC)
        for job_id, job in tuple(self._state.jobs.items()):
            path = Path(job.output)
            if job.deletion_requested:
                if not path.exists():
                    self._state.jobs.pop(job_id)
                    changed = True
                continue
            if job.status in {"queued", "running"}:
                status: JobStatus = "completed" if validate_mp4(path) else "pending"
                self._state.jobs[job_id] = replace(job, status=status, updated_at=now)
                changed = True
            elif job.status == "completed" and not validate_mp4(path):
                self._state.jobs[job_id] = replace(
                    job,
                    status="failed",
                    error="The tracked export artifact is missing or invalid.",
                    updated_at=now,
                )
                changed = True
        for automation in tuple(self._state.automations.values()):
            if automation.status == "removing":
                before = automation.id in self._state.automations
                self.finish_remove(automation.id)
                changed = changed or (before and automation.id not in self._state.automations)
        if changed:
            self.persist()

    def migrate_web_v1(self, legacy_path: Path, *, output_directory: Path, timezone: str) -> list[DailyAutomation]:
        """Import Web schedule schema version 1 into this registry."""
        if not legacy_path.exists():
            return []
        timezone_name = validate_timezone(timezone)
        output = canonical_output_directory(output_directory)
        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version", 1) not in {0, 1}:
                message = "legacy web schedule root or version is invalid"
                raise TypeError(message)
            items = payload["schedules"]
            if not isinstance(items, list):
                message = "schedules must be a list"
                raise TypeError(message)
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("cameras"), list):
                    message = "legacy web schedule must be an object with cameras"
                    raise TypeError(message)
                camera_items = item["cameras"]
                if not camera_items or not all(
                    isinstance(camera, dict)
                    and isinstance(camera.get("id"), str)
                    and isinstance(camera.get("name"), str)
                    for camera in camera_items
                ):
                    message = "legacy web schedule cameras are invalid"
                    raise TypeError(message)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            message = f"could not migrate legacy web schedules: {exc}"
            raise RegistryError(message) from exc
        backup = self._backup(legacy_path)
        imported: list[DailyAutomation] = []
        now = datetime.now(UTC)
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                message = "unreachable invalid legacy web schedule"
                raise AssertionError(message)
            cameras_value = item.get("cameras")
            if not isinstance(cameras_value, list):
                message = "unreachable missing legacy cameras"
                raise AssertionError(message)
            cameras = tuple(
                AutomationCamera(id=str(camera["id"]), name=str(camera["name"]))
                for camera in cameras_value
                if isinstance(camera, dict)
            )
            last_run = item.get("last_run_day")
            next_day = (
                date.fromisoformat(cast("str", last_run)).fromordinal(
                    date.fromisoformat(cast("str", last_run)).toordinal() + 1
                )
                if last_run
                else now.astimezone(ZoneInfo(timezone_name))
                .date()
                .fromordinal(now.astimezone(ZoneInfo(timezone_name)).date().toordinal() - 1)
            )
            camera_names = ", ".join(camera.name for camera in cameras[:2])
            base_name = f"{camera_names} daily" if camera_names else f"Web automation {index}"
            name = base_name
            suffix = 2
            while any(existing.name.casefold() == name.casefold() for existing in self._state.automations.values()):
                name = f"{base_name} {suffix}"
                suffix += 1
            fingerprint = f"web-v1:{item.get('id', index)}:{backup.name}"
            existing_import = next(
                (
                    automation
                    for automation in self._state.automations.values()
                    if automation.source_fingerprint == fingerprint
                ),
                None,
            )
            if existing_import is not None:
                imported.append(existing_import)
                continue
            created_value = item.get("created_at")
            created_at = _aware_datetime(created_value, "created_at") if created_value else now
            automation = self.add(
                name=name,
                cameras=cameras,
                connection=ConnectionReference("web-environment", "default"),
                speed=str(item.get("speed", "600x")),
                output_directory=output,
                timezone=timezone_name,
                now=created_at,
                next_day=next_day,
                source_fingerprint=fingerprint,
                imported_at=now,
            )
            if item.get("paused") is True:
                retry_value = item.get("next_retry_at")
                automation = replace(
                    automation,
                    status="paused",
                    consecutive_failures=_nonnegative_integer(item.get("failure_count", 0)),
                    next_retry_at=None if retry_value is None else _aware_datetime(retry_value, "next_retry_at"),
                    last_error=_optional_text(item.get("last_error"), "last_error"),
                )
                self.replace_automation(automation)
            imported.append(automation)
        legacy_path.unlink()
        return imported

    def _replace_automation(self, automation: DailyAutomation) -> None:
        previous = self._state.automations[automation.id]
        self._state.automations[automation.id] = automation
        try:
            self.persist()
        except Exception:
            self._state.automations[automation.id] = previous
            raise

    def _reject_overlap(
        self,
        output: Path,
        cameras: tuple[AutomationCamera, ...],
        speed: str,
        *,
        excluding: str | None = None,
    ) -> None:
        identity = _directory_identity(output)
        camera_ids = {camera.id for camera in cameras}
        for existing in self._state.automations.values():
            if existing.id == excluding:
                continue
            if _directory_identity(existing.output_path) != identity or existing.speed != speed:
                continue
            overlap = camera_ids.intersection(camera.id for camera in existing.cameras)
            if overlap:
                message = (
                    f"automation overlaps retained automation {existing.name!r} for camera "
                    f"{sorted(overlap)[0]} in {output} at {speed}"
                )
                raise RegistryError(message)

    def _recover_atomic_write(self) -> None:
        candidates = sorted(self.path.parent.glob(f".{self.path.name}.*.tmp")) if self.path.parent.exists() else []
        if self.path.exists():
            for candidate in candidates:
                with suppress(OSError):
                    candidate.unlink()
            return
        for candidate in reversed(candidates):
            try:
                self._decode_state(json.loads(candidate.read_text(encoding="utf-8")))
                candidate.replace(self.path)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, RegistryError):
                continue
            break

    def _quarantine_invalid(self) -> Path:
        quarantined = self.path.with_name(f"{self.path.stem}.invalid-{secrets.token_hex(4)}{self.path.suffix}")
        try:
            self.path.replace(quarantined)
        except OSError as exc:
            message = f"could not quarantine invalid automation registry {self.path}: {exc}"
            raise RegistryError(message) from exc
        return quarantined

    @staticmethod
    def _backup(path: Path) -> Path:
        backup = path.with_name(f"{path.name}.v1-backup")
        if not backup.exists():
            try:
                shutil.copy2(path, backup)
            except OSError as exc:
                message = f"could not back up legacy state {path}: {exc}"
                raise RegistryError(message) from exc
        return backup

    @classmethod
    def _decode_state(cls, payload: object) -> RegistryState:
        if not isinstance(payload, dict) or payload.get("version") != REGISTRY_VERSION:
            raise RegistryError("unsupported automation registry version")
        automation_items = _record_list(payload, "automations")
        batch_items = _record_list(payload, "batches")
        job_items = _record_list(payload, "jobs")
        state = RegistryState()
        for item in automation_items:
            automation = cls._decode_automation(item)
            if automation.id in state.automations:
                raise RegistryError(f"duplicate automation ID: {automation.id}")
            if any(existing.name.casefold() == automation.name.casefold() for existing in state.automations.values()):
                raise RegistryError(f"duplicate automation name: {automation.name}")
            state.automations[automation.id] = automation
        for item in batch_items:
            batch = cls._decode_batch(item)
            if batch.id in state.batches or batch.automation_id not in state.automations:
                raise RegistryError(f"invalid or duplicate batch: {batch.id}")
            state.batches[batch.id] = batch
        for item in job_items:
            job = cls._decode_job(item)
            if job.id in state.jobs or job.batch_id not in state.batches:
                raise RegistryError(f"invalid or duplicate job: {job.id}")
            state.jobs[job.id] = job
        generations = payload.get("reexport_generations", {})
        if not isinstance(generations, dict) or not all(
            isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for key, value in generations.items()
        ):
            raise RegistryError("invalid re-export generations")
        state.reexport_generations = cast("dict[str, int]", generations)
        return state

    @staticmethod
    def _decode_automation(item: dict[str, object]) -> DailyAutomation:
        identifier = _required_text(item, "id")
        if parse_automation_id(identifier) != identifier:
            raise RegistryError(f"invalid automation ID: {identifier}")
        status = _required_text(item, "status")
        if status not in AUTOMATION_STATUSES:
            raise RegistryError(f"invalid automation status: {status}")
        cameras_value = item.get("cameras")
        if not isinstance(cameras_value, list) or not cameras_value:
            raise RegistryError("automation requires cameras")
        cameras = tuple(
            AutomationCamera(_required_text(_mapping(camera), "id"), _required_text(_mapping(camera), "name"))
            for camera in cameras_value
        )
        connection_value = _mapping(item.get("connection"))
        created_at = _aware_datetime(item.get("created_at"), "created_at")
        next_retry = item.get("next_retry_at")
        imported_at = item.get("imported_at")
        output = canonical_output_directory(Path(_required_text(item, "output_directory")), must_exist=False)
        timezone = validate_timezone(_required_text(item, "timezone"))
        speed = _required_text(item, "speed")
        if speed not in SPEED_TO_FPS:
            raise RegistryError(f"unsupported automation speed: {speed}")
        return DailyAutomation(
            id=identifier,
            name=normalize_display_name(_required_text(item, "name")),
            cameras=cameras,
            connection=ConnectionReference(
                cast("ConnectionKind", _required_text(connection_value, "kind")),
                _required_text(connection_value, "value"),
            ),
            speed=speed,
            output_directory=str(output),
            timezone=timezone,
            created_at=created_at,
            status=cast("AutomationStatus", status),
            next_day=date.fromisoformat(_required_text(item, "next_day")),
            consecutive_failures=_nonnegative_integer(item.get("consecutive_failures")),
            next_retry_at=None if next_retry is None else _aware_datetime(next_retry, "next_retry_at"),
            last_error=_optional_text(item.get("last_error"), "last_error"),
            source_fingerprint=_optional_text(item.get("source_fingerprint"), "source_fingerprint"),
            imported_at=None if imported_at is None else _aware_datetime(imported_at, "imported_at"),
        )

    @staticmethod
    def _decode_batch(item: dict[str, object]) -> ExportBatchRecord:
        retry = item.get("retry_not_before")
        return ExportBatchRecord(
            id=_required_text(item, "id"),
            automation_id=_required_text(item, "automation_id"),
            day=date.fromisoformat(_required_text(item, "day")),
            generation=_nonnegative_integer(item.get("generation")),
            attempt=_nonnegative_integer(item.get("attempt")),
            created_at=_aware_datetime(item.get("created_at"), "created_at"),
            retry_not_before=None if retry is None else _aware_datetime(retry, "retry_not_before"),
        )

    @staticmethod
    def _decode_job(item: dict[str, object]) -> ExportJobRecord:
        status = _required_text(item, "status")
        if status not in JOB_STATUSES:
            raise RegistryError(f"invalid job status: {status}")
        camera_value = _mapping(item.get("camera"))
        retry = item.get("retry_not_before")
        automation_id = item.get("automation_id")
        if automation_id is not None and not isinstance(automation_id, str):
            raise RegistryError("job automation_id must be text or null")
        deletion_requested = item.get("deletion_requested", False)
        if not isinstance(deletion_requested, bool):
            raise RegistryError("job deletion_requested must be boolean")
        return ExportJobRecord(
            id=_required_text(item, "id"),
            batch_id=_required_text(item, "batch_id"),
            automation_id=automation_id,
            camera=AutomationCamera(_required_text(camera_value, "id"), _required_text(camera_value, "name")),
            output=_required_text(item, "output"),
            status=cast("JobStatus", status),
            attempt=_nonnegative_integer(item.get("attempt")),
            created_at=_aware_datetime(item.get("created_at"), "created_at"),
            updated_at=_aware_datetime(item.get("updated_at"), "updated_at"),
            error=_optional_text(item.get("error"), "error"),
            retry_not_before=None if retry is None else _aware_datetime(retry, "retry_not_before"),
            deletion_requested=deletion_requested,
        )

    @staticmethod
    def _encode_state(state: RegistryState) -> dict[str, object]:
        for automation in state.automations.values():
            AutomationRegistry._decode_automation(_automation_payload(automation))
        for batch in state.batches.values():
            AutomationRegistry._decode_batch(_batch_payload(batch))
        for job in state.jobs.values():
            AutomationRegistry._decode_job(_job_payload(job))
        return {
            "version": REGISTRY_VERSION,
            "automations": [_automation_payload(item) for item in state.automations.values()],
            "batches": [_batch_payload(item) for item in state.batches.values()],
            "jobs": [_job_payload(item) for item in state.jobs.values()],
            "reexport_generations": state.reexport_generations,
        }


def _automation_payload(automation: DailyAutomation) -> dict[str, object]:
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
        "consecutive_failures": automation.consecutive_failures,
        "next_retry_at": automation.next_retry_at.isoformat() if automation.next_retry_at else None,
        "last_error": automation.last_error,
        "source_fingerprint": automation.source_fingerprint,
        "imported_at": automation.imported_at.isoformat() if automation.imported_at else None,
    }


def _batch_payload(batch: ExportBatchRecord) -> dict[str, object]:
    return {
        "id": batch.id,
        "automation_id": batch.automation_id,
        "day": batch.day.isoformat(),
        "generation": batch.generation,
        "attempt": batch.attempt,
        "created_at": batch.created_at.isoformat(),
        "retry_not_before": batch.retry_not_before.isoformat() if batch.retry_not_before else None,
    }


def _job_payload(job: ExportJobRecord) -> dict[str, object]:
    return {
        "id": job.id,
        "batch_id": job.batch_id,
        "automation_id": job.automation_id,
        "camera": asdict(job.camera),
        "output": job.output,
        "status": job.status,
        "attempt": job.attempt,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "error": job.error,
        "retry_not_before": job.retry_not_before.isoformat() if job.retry_not_before else None,
        "deletion_requested": job.deletion_requested,
    }


def _directory_identity(path: Path) -> tuple[int, int, str]:
    try:
        info = path.stat()
    except OSError:
        return 0, 0, str(path.resolve(strict=False)).casefold()
    return info.st_dev, info.st_ino, str(path.resolve(strict=False)).casefold()


def _record_list(payload: dict[object, object], key: str) -> list[dict[str, object]]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise RegistryError(f"registry {key} must be a list")
    return [_mapping(item) for item in value]


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RegistryError("stored record must be an object with text keys")
    return cast("dict[str, object]", value)


def _required_text(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{key} must be nonempty text")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryError(f"{field_name} must be text or null")
    return value


def _aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise RegistryError(f"{field_name} must be an ISO datetime")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RegistryError(f"{field_name} must include a timezone")
    return parsed


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RegistryError("stored integer must be nonnegative")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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

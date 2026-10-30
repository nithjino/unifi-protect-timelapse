from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from timelapse.automation_registry import (
    AutomationCamera,
    AutomationRegistry,
    ConnectionReference,
    RegistryError,
    RegistryOwnedError,
)
from timelapse.download import validate_mp4


def test_registry_round_trip_normalizes_names_and_resolves_id_first(tmp_path) -> None:
    output = tmp_path / "exports"
    output.mkdir()
    registry = AutomationRegistry(tmp_path / "automations.json")
    registry.load()

    automation = registry.add(
        name="  Cafe\u0301  ",
        cameras=(AutomationCamera("camera-1", "Front Door"),),
        connection=ConnectionReference("python-profile", "home"),
        speed="600x",
        output_directory=output,
        timezone="America/New_York",
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )

    restored = AutomationRegistry(registry.path)
    restored.load()
    assert restored.resolve(automation.id).name == "Café"
    assert restored.resolve("CAFÉ").id == automation.id
    assert restored.resolve(automation.id).next_day == date(2026, 8, 22)


def test_registry_rejects_artifact_identity_overlap_even_when_timezone_differs(tmp_path) -> None:
    output = tmp_path / "exports"
    output.mkdir()
    registry = AutomationRegistry(tmp_path / "automations.json")
    registry.load()
    values = {
        "cameras": (AutomationCamera("camera-1", "Front Door"),),
        "connection": ConnectionReference("python-profile", "home"),
        "speed": "600x",
        "output_directory": output,
        "now": datetime(2026, 8, 23, 12, tzinfo=UTC),
    }
    registry.add(name="New York", timezone="America/New_York", **values)

    with pytest.raises(RegistryError, match="overlaps"):
        registry.add(name="Chicago", timezone="America/Chicago", **values)


def test_owner_lock_refuses_a_second_process_owner(tmp_path) -> None:
    registry = AutomationRegistry(tmp_path / "automations.json")
    with registry.owner(), pytest.raises(RegistryOwnedError):
        registry.owner().acquire()


@pytest.mark.parametrize(
    ("content", "valid"),
    [
        (b"\0\0\0\x18ftypisom", True),
        (b"", False),
        (b"not an mp4", False),
    ],
)
def test_validate_mp4_checks_regular_nonempty_ftyp(tmp_path, content: bytes, valid: bool) -> None:
    artifact = tmp_path / "artifact.mp4"
    artifact.write_bytes(content)
    assert validate_mp4(artifact) is valid


def test_invalid_registry_is_quarantined(tmp_path) -> None:
    path = tmp_path / "automations.json"
    path.write_text(json.dumps({"version": 999}), encoding="utf-8")

    with pytest.raises(RegistryError, match="moved"):
        AutomationRegistry(path).load()

    assert not path.exists()
    assert len(list(tmp_path.glob("automations.invalid-*.json"))) == 1


@pytest.fixture
def registry_with_automation(tmp_path):
    registry = AutomationRegistry(tmp_path / "automations.json")
    registry.load()
    automation = registry.add(
        name="Home",
        cameras=(AutomationCamera("camera-1", "Front"),),
        connection=ConnectionReference("python-profile", "home"),
        speed="120x",
        output_directory=tmp_path,
        timezone="UTC",
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    return registry, automation


def test_success_advances_the_day_once_and_keeps_stopped_status(registry_with_automation):
    registry, automation = registry_with_automation
    failed = registry.record_failure(automation.id, attempt=1, error="unavailable", retry_at=automation.created_at)
    assert failed.next_retry_at == automation.created_at
    assert failed.consecutive_failures == 1
    registry.stop(automation.id)
    complete = registry.record_success(automation.id, automation.next_day, advance_day=True)
    assert complete.status == "stopped"
    assert complete.next_day == date(2026, 8, 23)
    assert complete.last_completed_day == automation.next_day
    assert complete.consecutive_failures == 0
    assert complete.last_error is None
    with pytest.raises(RegistryError, match="exactly once"):
        registry.record_success(automation.id, automation.next_day, advance_day=True)


def test_new_automation_starts_without_completed_day(registry_with_automation):
    _, automation = registry_with_automation
    assert automation.last_completed_day is None


@pytest.mark.parametrize("transition", ["add", "edit", "stop", "pause", "resume", "failure", "success", "delete"])
def test_transition_rolls_back_failed_persistence(registry_with_automation, monkeypatch, transition):
    registry, automation = registry_with_automation
    if transition == "resume":
        registry.stop(automation.id)
    before = registry.list()
    stored = registry.path.read_bytes()

    def fail() -> None:
        message = "disk unavailable"
        raise RegistryError(message)

    operations = {
        "add": lambda: registry.add(
            name="Other",
            cameras=(AutomationCamera("camera-2", "Back"),),
            connection=automation.connection,
            speed="120x",
            output_directory=automation.output_path,
            timezone="UTC",
        ),
        "edit": lambda: registry.edit(automation.id, name="Renamed"),
        "stop": lambda: registry.stop(automation.id),
        "pause": lambda: registry.pause(automation.id, "unavailable"),
        "resume": lambda: registry.resume(automation.id),
        "failure": lambda: registry.record_failure(automation.id, attempt=1, error="failed", retry_at=None),
        "success": lambda: registry.record_success(automation.id, automation.next_day, advance_day=True),
        "delete": lambda: registry.delete(automation.id),
    }
    with monkeypatch.context() as patch:
        patch.setattr(registry, "persist", fail)
        with pytest.raises(RegistryError, match="disk unavailable"):
            operations[transition]()
    assert registry.list() == before
    assert registry.path.read_bytes() == stored


def test_automation_left_removing_by_a_crash_is_forgotten(registry_with_automation):
    registry, automation = registry_with_automation
    registry.begin_remove(automation.id)
    restored = AutomationRegistry(registry.path)
    restored.load()
    assert not restored.contains(automation.id)


def test_version_2_registry_loads_and_keeps_progress(tmp_path):
    automation = {
        "id": "auto_81d31296ae0844fa8156c962c2111efd",
        "name": "Plant",
        "cameras": [{"id": "camera-1", "name": "Plant Cam"}],
        "connection": {"kind": "web-environment", "value": "default"},
        "speed": "600x",
        "output_directory": str(tmp_path),
        "timezone": "UTC",
        "created_at": "2026-08-23T12:00:00+00:00",
        "status": "active",
        "next_day": "2026-10-30",
        "consecutive_failures": 0,
        "next_retry_at": None,
        "last_error": None,
        "source_fingerprint": "web-v1:old",
        "imported_at": "2026-08-23T12:00:00+00:00",
    }
    path = tmp_path / "automations.json"
    payload = {
        "version": 2,
        "automations": [automation],
        "batches": [{"id": "b", "automation_id": automation["id"]}],
        "jobs": [{"id": "j", "batch_id": "b", "status": "running"}],
        "reexport_generations": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    registry = AutomationRegistry(path)
    registry.load()
    loaded = registry.resolve("Plant")
    assert loaded.next_day == date(2026, 10, 30)
    assert loaded.last_completed_day == date(2026, 10, 29)

    registry.stop(loaded.id)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["version"] == 3
    assert "jobs" not in stored

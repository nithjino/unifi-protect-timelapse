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
    validate_mp4,
)


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
    with registry.owner(endpoint="test"), pytest.raises(RegistryOwnedError):
        registry.owner(endpoint="other").acquire()


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

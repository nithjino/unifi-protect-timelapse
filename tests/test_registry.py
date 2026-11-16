import json
import shutil
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from timelapse import InvalidRequestError, NotFoundError, TimelapseError
from timelapse.model import Camera
from timelapse.registry import Automation, Registry, RegistryOwnedError

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 13, 15, tzinfo=UTC)
WEB = ("web-environment", "default")
CAM = Camera("camera-1", "Plant Cam")


@pytest.fixture
def registry(tmp_path):
    (tmp_path / "out").mkdir()
    registry = Registry(tmp_path / "web-automations.json")
    registry.load()
    return registry


def add(registry: Registry, **changes) -> Automation:
    values = {
        "name": "Plant",
        "cameras": [CAM],
        "connection": WEB,
        "speed": "600x",
        "output_directory": registry.path.parent / "out",
        "timezone": "UTC",
        "now": NOW,
    }
    return registry.add(**(values | changes))


def test_reg1_add_normalises(registry):
    automation = add(
        registry,
        name="  Café  ",
        cameras=[CAM, Camera("camera-1", "dupe"), Camera("camera-2", "Two")],
        output_directory=registry.path.parent / "out" / ".." / "out",
        timezone="Asia/Tokyo",
    )
    assert automation.name == "Café"
    assert [camera.id for camera in automation.cameras] == ["camera-1", "camera-2"]
    assert automation.output_directory == registry.path.parent.resolve() / "out"
    assert (automation.status, automation.next_day) == ("active", date(2026, 7, 13))
    assert len(automation.id) == 37
    assert automation.id.startswith("auto_")
    assert add(registry, name="Later", speed="1x", next_day=date(2026, 1, 1)).next_day == date(2026, 1, 1)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": "   "}, "name is required"),
        ({"name": "auto_81d31296ae0844fa8156c962c2111efd"}, "look like an automation ID"),
        ({"name": "PLANT", "speed": "1x"}, "already exists"),
        ({"cameras": []}, "at least one"),
        ({"cameras": [Camera(" ", "x")]}, "ID and a name"),
        ({"cameras": [Camera("x", "")]}, "ID and a name"),
        ({"speed": "2x"}, "speed"),
        ({"output_directory": Path("relative")}, "absolute"),
        ({"output_directory": Path("/nonexistent/dir")}, "exist"),
        ({"timezone": "Mars/Base"}, "valid IANA timezone"),
        ({"timezone": ""}, "valid IANA timezone"),
    ],
)
def test_reg1_add_rejects(registry, changes, message):
    add(registry)
    with pytest.raises(InvalidRequestError, match=message):
        add(registry, **({"name": "Other"} | changes))
    assert len(registry.automations) == 1


def test_reg2_overlap(registry, tmp_path):
    add(registry, cameras=[CAM, Camera("camera-2", "B")])
    with pytest.raises(InvalidRequestError, match="overlaps"):
        add(registry, name="Copy", cameras=[Camera("camera-2", "B")], timezone="Asia/Tokyo")
    add(registry, name="Faster", speed="120x")
    (tmp_path / "other").mkdir()
    add(registry, name="Elsewhere", output_directory=tmp_path / "other")
    add(registry, name="Different camera", cameras=[Camera("camera-3", "C")])


def test_reg3_resolve(registry):
    automation = add(registry, name="  Café  ")
    assert registry.resolve("CAFÉ") == automation
    assert registry.resolve(automation.id) == automation
    with pytest.raises(NotFoundError, match="automation does not exist: nope"):
        registry.resolve("nope")


def make(status: str, **changes) -> Automation:
    automation = Automation(
        "auto_" + "0" * 32, "A", (CAM,), WEB, "600x", Path("/out"), "UTC", NOW, status, date(2026, 7, 12),
        consecutive_failures=2, next_retry_at=NOW, last_error="old",
    )  # fmt: skip
    return replace(automation, **changes)


@pytest.mark.parametrize("status", ["active", "paused", "stopped"])
def test_reg4_stop(status):
    assert make(status).stopped() == make("stopped", next_retry_at=None)


def test_reg4_resume():
    for status in ("paused", "stopped"):
        assert make(status).resumed() == make("active", consecutive_failures=0, next_retry_at=None, last_error=None)
    with pytest.raises(InvalidRequestError, match="only paused or stopped automations can be resumed"):
        make("active").resumed()


def test_reg4_pause_fail_succeed():
    assert make("active").paused("why") == make("paused", last_error="why", next_retry_at=None)
    assert make("paused").paused("again").last_error == "again"
    assert make("stopped").paused("why") == make("stopped")
    later = datetime(2026, 7, 14, tzinfo=UTC)
    assert make("stopped").failed(3, "e", later) == make("stopped", consecutive_failures=3, last_error="e",
                                                         next_retry_at=later)  # fmt: skip
    done = make("stopped").succeeded(date(2026, 7, 12), advance=True)
    assert done == make("stopped", consecutive_failures=0, next_retry_at=None, last_error=None,
                        next_day=date(2026, 7, 13), last_completed_day=date(2026, 7, 12))  # fmt: skip
    assert make("active").succeeded(date(2026, 7, 1), advance=False).next_day == date(2026, 7, 12)
    with pytest.raises(InvalidRequestError, match="exactly once"):
        make("active").succeeded(date(2026, 7, 11), advance=True)


def test_reg5_edit(registry):
    automation = add(registry)
    other = add(registry, name="Other", cameras=[Camera("camera-9", "Nine")])
    edited = registry.edit("plant", name=" Renamed ", cameras=[Camera("camera-5", "Five")], connection=("dotenv", "/e"))
    assert (edited.id, edited.name, edited.cameras, edited.connection) == (
        automation.id, "Renamed", (Camera("camera-5", "Five"),), ("dotenv", "/e")
    )  # fmt: skip
    with pytest.raises(InvalidRequestError, match="already exists"):
        registry.edit(other.id, name="RENAMED")
    with pytest.raises(InvalidRequestError, match="overlaps"):
        registry.edit(other.id, cameras=[Camera("camera-5", "Five")])
    with pytest.raises(InvalidRequestError, match="at least one"):
        registry.edit(other.id, cameras=[])


def test_reg6_failed_writes_change_nothing(registry):
    automation = add(registry)
    before = registry.path.read_bytes()
    registry.path.parent.chmod(0o500)
    try:
        attempts = [
            lambda: add(registry, name="New", speed="1x"),
            lambda: registry.edit(automation.id, name="Changed"),
            lambda: registry.put(automation.stopped()),
            lambda: registry.delete(automation.id),
        ]
        for attempt in attempts:
            with pytest.raises(TimelapseError, match="Could not save"):
                attempt()
            assert registry.automations == [automation]
    finally:
        registry.path.parent.chmod(0o700)
    assert registry.path.read_bytes() == before
    registry.delete(automation.id)
    assert registry.automations == []
    with pytest.raises(NotFoundError):
        registry.delete(automation.id)


def test_reg7_file_format_round_trip(tmp_path):
    path = tmp_path / "web-automations.json"
    shutil.copy(FIXTURES / "web-automations-v3.json", path)
    registry = Registry(path)
    registry.load()
    automation = registry.automations[0]
    registry.put(automation.stopped().resumed())
    text = path.read_text()
    assert json.loads(text) == json.loads((FIXTURES / "web-automations-v3.json").read_text())
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert "password" not in text
    assert "token" not in text


def test_reg8_version_2_files(tmp_path):
    path = tmp_path / "web-automations.json"
    shutil.copy(FIXTURES / "web-automations-v2.json", path)
    registry = Registry(path)
    registry.load()
    plant, garage = registry.resolve("Plant"), registry.resolve("Garage")
    assert [a.name for a in registry.automations] == ["Garage", "Plant"]
    assert plant.last_completed_day == date(2026, 10, 29)
    assert garage.last_completed_day is None
    assert garage.consecutive_failures == 5
    registry.put(garage)
    saved = json.loads(path.read_text())
    assert saved["version"] == 3
    assert set(saved) == {"version", "automations"}
    assert "source_fingerprint" not in saved["automations"][1]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(version=4),
        lambda data: data["automations"].append(dict(data["automations"][0])),
        lambda data: data["automations"].append(dict(data["automations"][0], id="auto_" + "1" * 32, name="PLANT")),
        lambda data: data["automations"][0].update(created_at="2026-08-23T12:00:00"),
        lambda data: data["automations"][0].update(status="removed"),
        lambda data: data["automations"][0].update(cameras=[]),
        lambda data: data["automations"][0].pop("speed"),
        lambda data: data.update(automations="nope"),
    ],
)
def test_reg9_invalid_files_are_moved(tmp_path, mutate):
    path = tmp_path / "web-automations.json"
    data = json.loads((FIXTURES / "web-automations-v3.json").read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    leftover = tmp_path / ".web-automations.json.abc.tmp"
    leftover.write_text("partial")
    with pytest.raises(TimelapseError, match="moved"):
        Registry(path).load()
    assert not path.exists()
    assert not leftover.exists()
    [moved] = tmp_path.glob("web-automations.invalid-*.json")
    assert len(moved.name) == len("web-automations.invalid-12345678.json")


def test_reg10_lock_is_exclusive(tmp_path):
    first, second = Registry(tmp_path / "cli-automations.json"), Registry(tmp_path / "cli-automations.json")
    with first.lock():
        assert (tmp_path / "cli-automations.json.lock").exists()
        with pytest.raises(RegistryOwnedError, match="Stop the running automation process first"), second.lock():
            pass
    with second.lock():
        pass

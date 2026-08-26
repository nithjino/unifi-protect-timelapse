from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from timelapse.automation_registry import (
    AutomationCamera,
    AutomationRegistry,
    ConnectionReference,
    ExportBatchRecord,
    ExportJobRecord,
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


@pytest.fixture
def batch_registry(tmp_path):
    registry = AutomationRegistry(tmp_path / "automations.json")
    registry.load()
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    automation = registry.add(
        name="Home",
        cameras=(AutomationCamera("camera-1", "Front"),),
        connection=ConnectionReference("python-profile", "home"),
        speed="120x",
        output_directory=tmp_path,
        timezone="UTC",
        now=now,
    )
    batch = ExportBatchRecord("batch-1", automation.id, automation.next_day, 0, 1, now)
    job = ExportJobRecord(
        "job-1", batch.id, automation.id, automation.cameras[0], str(tmp_path / "artifact.mp4"), "pending", 1, now, now
    )
    registry.put_batch(batch, (job,))
    return registry, automation, batch, job


def test_registry_queries_are_immutable_domain_snapshots(batch_registry):
    registry, automation, batch, job = batch_registry
    snapshot = registry.list()
    jobs = registry.jobs_for_batch(batch.id)
    assert registry.contains(automation.id)
    assert registry.job(job.id) == job
    assert registry.job("missing") is None
    assert registry.job_context("missing") is None
    assert registry.jobs_for_automation(automation.id) == (job,)
    context = registry.job_context(job.id)
    assert context is not None
    assert (context.job, context.batch, context.automation) == (job, batch, automation)
    with pytest.raises(FrozenInstanceError):
        setattr(context, "job", None)  # noqa: B010 - exercise immutable boundary
    registry.stop(automation.id)
    assert snapshot == (automation,)
    assert isinstance(snapshot, tuple)
    assert jobs == (job,)


@pytest.mark.parametrize(
    ("status", "recoverable"),
    [
        ("pending", True),
        ("queued", True),
        ("running", True),
        ("completed", True),
        ("failed", False),
        ("cancelled", False),
        ("deleting", False),
    ],
)
def test_submitted_batch_recovery_query(batch_registry, status, recoverable):
    registry, _, batch, job = batch_registry
    registry.update_job(replace(job, status=status))
    assert registry.batch_needs_recovery(batch.id) is recoverable
    assert not registry.batch_needs_recovery("missing")


def test_retry_and_success_transitions_preserve_stopped_status(batch_registry):
    registry, automation, batch, job = batch_registry
    registry.update_job(replace(job, status="failed"))
    retry_at = job.created_at + timedelta(minutes=1)
    failed = registry.record_batch_failure(batch.id, error="unavailable", retry_at=retry_at)
    assert failed.next_retry_at == retry_at
    assert failed.consecutive_failures == 1
    assert registry.batch(batch.id).retry_not_before == retry_at
    registry.stop(automation.id)
    with pytest.raises(RegistryError, match="status"):
        registry.record_batch_failure(batch.id, error="unavailable", retry_at=retry_at)
    registry.update_job(replace(job, status="completed"))
    complete = registry.record_batch_success(batch.id, advance_day=True)
    assert complete.status == "stopped"
    assert complete.next_day == automation.next_day + timedelta(days=1)
    assert complete.consecutive_failures == 0
    assert complete.last_error is None
    assert registry.batch(batch.id).retry_not_before is None
    with pytest.raises(RegistryError, match="exactly once"):
        registry.record_batch_success(batch.id, advance_day=True)


@pytest.mark.parametrize(
    "transition",
    ["batch", "job", "failure", "success", "checkpoint", "generation", "stop", "pause", "resume", "remove"],
)
def test_registry_transition_rolls_back_failed_persistence(batch_registry, monkeypatch, transition):
    registry, automation, batch, job = batch_registry
    registry.update_job(replace(job, status="failed"))
    if transition == "success":
        registry.update_job(replace(job, status="completed"))
    if transition == "resume":
        registry.stop(automation.id)
    before = (registry.list(), registry.batch(batch.id), registry.jobs_for_batch(batch.id))
    stored = registry.path.read_bytes()

    def fail() -> None:
        message = "disk unavailable"
        raise RegistryError(message)

    operations = {
        "batch": lambda: registry.put_batch(replace(batch, attempt=2), (replace(job, attempt=2),)),
        "job": lambda: registry.update_job(replace(job, status="completed")),
        "failure": lambda: registry.record_batch_failure(batch.id, error="failed", retry_at=job.created_at),
        "success": lambda: registry.record_batch_success(batch.id, advance_day=True),
        "checkpoint": lambda: registry.import_checkpoint(
            automation.id, next_day=date(2026, 1, 1), source_fingerprint="legacy", imported_at=job.created_at
        ),
        "generation": lambda: registry.next_reexport_generation(automation.id, batch.day),
        "stop": lambda: registry.stop(automation.id),
        "pause": lambda: registry.pause(automation.id, "unavailable"),
        "resume": lambda: registry.resume(automation.id),
        "remove": lambda: registry.begin_remove(automation.id),
    }
    with monkeypatch.context() as patch:
        patch.setattr(registry, "persist", fail)
        with pytest.raises(RegistryError, match="disk unavailable"):
            operations[transition]()
    assert (registry.list(), registry.batch(batch.id), registry.jobs_for_batch(batch.id)) == before
    assert registry.path.read_bytes() == stored
    if transition == "generation":
        assert registry.next_reexport_generation(automation.id, batch.day) == 1


def test_removed_automation_history_survives_restart(batch_registry):
    registry, automation, batch, job = batch_registry
    registry.update_job(replace(job, status="failed"))
    assert registry.begin_remove(automation.id) is None
    assert not registry.contains(automation.id)
    restored = AutomationRegistry(registry.path)
    restored.load()
    context = restored.job_context(job.id)
    assert context is not None
    assert context.automation is None
    assert context.batch == batch


def test_deletion_intent_survives_a_failed_final_write(batch_registry, monkeypatch):
    registry, automation, _, job = batch_registry
    output = Path(job.output)
    output.write_bytes(b"\0\0\0\x18ftypisom")
    registry.update_job(replace(job, status="completed"))
    persist = registry.persist
    calls = 0

    def fail_final_write() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            message = "disk unavailable"
            raise RegistryError(message)
        persist()

    with monkeypatch.context() as patch:
        patch.setattr(registry, "persist", fail_final_write)
        with pytest.raises(RegistryError, match="disk unavailable"):
            registry.delete_artifact(job.id)
    assert not output.exists()
    assert registry.job(job.id).deletion_requested
    restored = AutomationRegistry(registry.path)
    restored.load()
    assert restored.job(job.id) is None
    assert restored.resolve(automation.id).next_day == automation.next_day


def test_checkpoint_import_is_idempotent_and_rejects_submitted_work(batch_registry):
    registry, automation, _, job = batch_registry
    values = {"next_day": date(2026, 1, 1), "source_fingerprint": "legacy", "imported_at": job.created_at}
    with pytest.raises(RegistryError, match="submitted work"):
        registry.import_checkpoint(automation.id, **values)
    registry.update_job(replace(job, status="failed"))
    imported = registry.import_checkpoint(automation.id, **values)
    assert imported.next_day == date(2026, 1, 1)
    assert registry.import_checkpoint(automation.id, **(values | {"next_day": date(2025, 1, 1)})) == imported


def test_recovery_rolls_back_in_memory_and_retries_after_failed_write(batch_registry, monkeypatch):
    registry, automation, _, job = batch_registry
    registry.update_job(replace(job, status="running"))
    registry.begin_remove(automation.id)
    Path(job.output).write_bytes(b"\0\0\0\x18ftypisom")
    before = registry.job_context(job.id)

    def fail() -> None:
        message = "disk unavailable"
        raise RegistryError(message)

    with monkeypatch.context() as patch:
        patch.setattr(registry, "persist", fail)
        with pytest.raises(RegistryError, match="disk unavailable"):
            registry.reconcile()
    assert registry.job_context(job.id) == before
    registry.reconcile()
    assert registry.job(job.id).status == "completed"
    assert not registry.contains(automation.id)


def test_migration_retries_paused_state_after_interrupted_import(tmp_path, monkeypatch):
    legacy = tmp_path / "web-schedules.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "schedules": [
                    {
                        "id": "old",
                        "cameras": [{"id": "camera-1", "name": "Front"}],
                        "speed": "120x",
                        "paused": True,
                        "failure_count": 3,
                        "last_error": "unavailable",
                        "last_run_day": "2026-08-20",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = AutomationRegistry(tmp_path / "automations.json")
    persist = registry.persist
    calls = 0

    def fail_pause() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            message = "disk unavailable"
            raise RegistryError(message)
        persist()

    with monkeypatch.context() as patch:
        patch.setattr(registry, "persist", fail_pause)
        with pytest.raises(RegistryError, match="disk unavailable"):
            registry.migrate_web_v1(legacy, output_directory=tmp_path, timezone="UTC")
    restored = AutomationRegistry(registry.path)
    restored.load()
    imported = restored.migrate_web_v1(legacy, output_directory=tmp_path, timezone="UTC")
    assert len(restored.list()) == 1
    assert imported[0].status == "paused"
    assert imported[0].consecutive_failures == 3
    assert imported[0].next_day == date(2026, 8, 21)
    assert not legacy.exists()


def test_invalid_batch_relationships_and_incomplete_success_are_rejected(batch_registry):
    registry, automation, batch, job = batch_registry
    with pytest.raises(RegistryError, match="belong"):
        registry.put_batch(batch, (replace(job, batch_id="other"),))
    with pytest.raises(RegistryError, match="identity"):
        registry.update_job(replace(job, output="other.mp4"))
    with pytest.raises(RegistryError, match="every Export Job"):
        registry.record_batch_success(batch.id, advance_day=True)
    with pytest.raises(RegistryError, match="terminal"):
        registry.delete_artifact(job.id)
    assert registry.resolve(automation.id) == automation
    assert registry.job(job.id) == job

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tests.fakes import CAMERA, MP4, FakeProtect
from timelapse import OperationTimeoutError, TimelapseError
from timelapse.model import Camera, Connection
from timelapse.registry import Registry
from timelapse.settings import Settings
from timelapse.storage import ExportFolder
from timelapse.web import create_app

FIXTURES = Path(__file__).parent / "fixtures"
YARD = Camera("camera-2", "Yard", "CONNECTED", "G4")
CONNECTION = Connection("https://protect.local", "test-token", "test-user", "test-password")
PASSWORD = "test-password"
FULL_DAY = {"camera_ids": ["camera-1"], "range_mode": "full-day", "day": "2026-07-20", "speed": "600x"}


def make_settings(tmp_path: Path, **changes) -> Settings:
    data = tmp_path / "data"
    base = Settings(timezone=ZoneInfo("UTC"), data_dir=data, output_dir=data / "exports", connection=CONNECTION)
    return replace(base, **changes)


@contextmanager
def serve(tmp_path: Path, protect: FakeProtect | None = None, client=("127.0.0.1", 50000), **changes) -> Iterator:
    protect = protect or FakeProtect([CAMERA, YARD])
    app = create_app(make_settings(tmp_path, **changes), protect=protect)
    with TestClient(app, base_url="http://localhost", client=client) as test_client:
        test_client.board = app.state.board  # type: ignore[attr-defined]
        test_client.protect = protect  # type: ignore[attr-defined]
        yield test_client


def settle(client) -> None:
    """Wait until every job has finished."""
    queue = client.board.queue
    client.portal.call(queue.wait, list(queue.jobs.values()))


def login(client) -> None:
    data = {"username": "timelapse", "password": PASSWORD, "next": "/"}
    assert client.post("/login", data=data, follow_redirects=False).status_code == 303


# --- Export folder -------------------------------------------------------------------------------


def test_st1_unwritable_directories_stop_startup(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data").chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match=r"data.*TIMELAPSE_UID and TIMELAPSE_GID"), serve(tmp_path):
            pass
    finally:
        (tmp_path / "data").chmod(0o700)


def test_st2_st5_usage_and_full_problem(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.bin").write_bytes(b"x" * 1024 * 1024)
    (tmp_path / ".b.part").write_bytes(b"x" * 10)
    folder = ExportFolder(tmp_path, quota_mib=1, retention_days=0)
    assert folder.usage_bytes() == 1024 * 1024 + 10
    assert folder.full_problem() == (
        "Export storage is full (1.0 MiB of 1.0 MiB). Delete old exports, or raise TIMELAPSE_WEB_STORAGE_QUOTA_MIB "
        "or set TIMELAPSE_WEB_RETENTION_DAYS, then Resume this automation."
    )
    assert ExportFolder(tmp_path, quota_mib=2, retention_days=0).full_problem() is None
    assert "Could not check" in (ExportFolder(tmp_path / "missing", quota_mib=1, retention_days=0).full_problem() or "")
    blocked = tmp_path / "blocked"
    (blocked / "inner").mkdir(parents=True)
    blocked.chmod(0o300)
    try:
        assert (ExportFolder(blocked, quota_mib=1, retention_days=0).full_problem() or "").startswith(
            "Could not check export storage: "
        )
    finally:
        blocked.chmod(0o700)


def test_st3_manual_exports_reserve_space(tmp_path):
    two = {**FULL_DAY, "camera_ids": ["camera-1", "camera-2"]}
    with serve(tmp_path, storage_quota_mib=10, connection=replace(CONNECTION, max_download_mib=4)) as client:
        client.protect.gate = asyncio.Event()
        assert client.post("/actions/export", data=two).status_code == 200
        response = client.post("/actions/export", data={**FULL_DAY, "day": "2026-07-19"})
        assert response.status_code == 429
        assert "The configured export storage quota cannot reserve space for this request." in response.text
        client.portal.call(client.protect.gate.set)
    with serve(tmp_path / "b", storage_quota_mib=10, connection=replace(CONNECTION, max_download_mib=0)) as client:
        assert client.post("/actions/export", data=two).status_code == 429


def test_st4_queue_bound_spares_daily_jobs(tmp_path):
    with serve(tmp_path, max_active_exports=1, max_queued_exports=0) as client:
        client.protect.gate = asyncio.Event()
        assert client.post("/actions/export", data=FULL_DAY).status_code == 200
        response = client.post("/actions/export", data={**FULL_DAY, "day": "2026-07-19"})
        assert response.status_code == 429
        assert "The export queue is full. Wait for an active export to finish and try again." in response.text
        board = client.board
        automation = board.registry.add(
            name="Daily", cameras=[YARD], connection=("web-environment", "default"), speed="600x",
            output_directory=board.settings.output_dir, timezone="UTC",
        )  # fmt: skip
        task = client.portal.start_task_soon(board.runner.run_due_once, automation.id)
        while len(board.queue.jobs) < 2:
            time.sleep(0.01)
        client.portal.call(client.protect.gate.set)
        assert task.result(timeout=5) is True


def test_st6_retention(tmp_path):
    exports = tmp_path / "data" / "exports"
    exports.mkdir(parents=True)
    old = time.time() - 3 * 86400
    for name in ("old.mp4", ".hidden.mp4", ".old.mp4.abc.part", "new.mp4"):
        (exports / name).write_bytes(MP4)
        if name != "new.mp4":
            os.utime(exports / name, (old, old))
    history = {"jobs": [json.loads((FIXTURES / "web-jobs-3.0.0.json").read_text())["jobs"][0] | {
        "output_name": "old.mp4", "status": "completed"}]}  # fmt: skip
    (tmp_path / "data" / "web-jobs.json").write_text(json.dumps(history))
    with serve(tmp_path, retention_days=2) as client:
        assert sorted(path.name for path in exports.iterdir()) == [".hidden.mp4", ".old.mp4.abc.part", "new.mp4"]
        assert client.board.queue.jobs == {}
    folder = ExportFolder(exports, quota_mib=1, retention_days=2)
    (exports / "busy.mp4").write_bytes(MP4)
    os.utime(exports / "busy.mp4", (old, old))
    assert folder.prune(datetime.now(UTC), {"busy.mp4"}) == set()
    assert ExportFolder(exports, quota_mib=1, retention_days=0).prune(datetime.now(UTC), set()) == set()


def test_st7_older_exports(tmp_path):
    with serve(tmp_path) as client:
        exports = client.board.settings.output_dir
        assert "No Exports Yet" in client.get("/partials/jobs").text
        client.post("/actions/export", data=FULL_DAY)
        settle(client)
        (exports / "older.mp4").write_bytes(MP4)
        (exports / ".hidden.mp4").write_bytes(MP4)
        page = client.get("/partials/jobs").text
        assert page.count("Older Exports") == 1
        assert 'href="/files/older.mp4"' in page
        assert ".hidden.mp4" not in page
        assert page.count("job-card") == 2
        assert client.get("/files/older.mp4").content == MP4
        assert client.get("/files/..%2Fweb-jobs.json").status_code == 404
        assert client.get("/files/.hidden.mp4").status_code == 404
        response = client.delete("/actions/files/older.mp4")
        assert "Export deleted." in response.text
        assert not (exports / "older.mp4").exists()
        response = client.delete("/actions/files/older.mp4")
        assert response.status_code == 404
        assert "That export is no longer on the server." in response.text


# --- Security ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "changes", "status"),
    [
        ("evil.example", {}, 400),
        ("localhost:8000", {}, 200),
        ("[::1]:8000", {}, 200),
        ("NAS.local", {"trusted_hosts": frozenset({"nas.local"})}, 200),
        ("192.168.1.5", {"host": "192.168.1.5", "password": PASSWORD}, 200),
        ("0.0.0.0", {"host": "0.0.0.0", "password": PASSWORD}, 400),
        ("bad:port", {}, 400),
    ],
)
def test_ws1_trusted_hosts(tmp_path, host, changes, status):
    with serve(tmp_path, **changes) as client:
        response = client.get("/healthz", headers={"Host": host})
        assert response.status_code == status
        if status == 400:
            assert response.text == "Untrusted host"


def test_ws2_password_required_off_loopback(tmp_path):
    with pytest.raises(RuntimeError, match="TIMELAPSE_WEB_PASSWORD is required when the web server is accessible"):
        create_app(make_settings(tmp_path, host="0.0.0.0"))
    with serve(tmp_path, client=("192.168.1.9", 50000)) as client:
        assert client.get("/healthz").status_code == 403
    with serve(tmp_path, client=("192.168.1.9", 50000), password=PASSWORD) as client:
        assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Sec-Fetch-Site": "same-origin", "Origin": "https://evil.example"}, 200),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Sec-Fetch-Site": "same-site"}, 403),
        ({}, 200),
        ({"Origin": "http://localhost"}, 200),
        ({"Origin": "http://localhost:80"}, 200),
        ({"Origin": "http://localhost:8000"}, 403),
        ({"Origin": "https://localhost"}, 403),
        ({"Origin": "http://evil.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://evil.example", "X-Forwarded-Host": "evil.example"}, 403),
    ],
)
def test_ws3_same_origin(tmp_path, headers, status):
    with serve(tmp_path) as client:
        response = client.post("/actions/automations/missing/stop", headers=headers)
        assert response.status_code == (404 if status == 200 else 403)


def test_ws4_login_required(tmp_path):
    with serve(tmp_path, password=PASSWORD) as client:
        response = client.get("/", follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/login?next=%2F")
        response = client.get("/partials/jobs", headers={"HX-Request": "true"})
        assert response.status_code == 401
        assert response.headers["HX-Redirect"].startswith("/login?next=")
        assert client.get("/api/thumbnails/camera-1?timestamp=2026-07-20T08:00").status_code == 401
        assert client.get("/healthz").status_code == 200
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/login").status_code == 200


def test_ws5_login(tmp_path):
    with serve(tmp_path, password=PASSWORD, session_hours=2) as client:
        page = client.get("/login?next=/partials/jobs")
        assert '<input type="hidden" name="next" value="/partials/jobs">' in page.text
        assert page.headers["Cache-Control"] == "no-store"
        wrong = client.post("/login", data={"username": "timelapse", "password": "nope", "next": "/"})
        assert wrong.status_code == 401
        assert "The username or password is incorrect." in wrong.text
        for target, expected in (("/partials/jobs", "/partials/jobs"), ("//evil.example", "/"),
                                 ("https://evil.example/", "/"), ("/\\evil", "/")):  # fmt: skip
            response = client.post(
                "/login", data={"username": "timelapse", "password": PASSWORD, "next": target}, follow_redirects=False
            )
            assert (response.status_code, response.headers["location"]) == (303, expected)
        cookie = response.headers["set-cookie"]
        for part in ("timelapse_session=", "HttpOnly", "SameSite=strict", "Path=/", "Max-Age=7200"):
            assert part in cookie
        assert "Secure" not in cookie
        assert client.get("/").status_code == 200
    with serve(tmp_path / "b", password=PASSWORD, cookie_secure=True) as client:
        response = client.post("/login", data={"username": "timelapse", "password": PASSWORD}, follow_redirects=False)
        assert "Secure" in response.headers["set-cookie"]


def test_ws6_failed_login_limit(tmp_path):
    with serve(tmp_path, password=PASSWORD) as client:
        for _ in range(5):
            client.post("/login", data={"username": "timelapse", "password": "nope"})
        response = client.post("/login", data={"username": "timelapse", "password": PASSWORD})
        assert response.status_code == 429
        assert re.search(r"Too many failed attempts\. Try again in \d+ seconds\.", response.text)
        assert 0 < int(response.headers["Retry-After"]) <= 60
        client.board.failures.clear()
        for _ in range(4):
            client.post("/login", data={"username": "timelapse", "password": "nope"})
        login(client)
        assert client.board.failures == {}


def test_ws7_sessions_and_logout(tmp_path):
    with serve(tmp_path, password=PASSWORD) as client:
        login(client)
        assert len(client.board.sessions) == 1
        token = client.cookies.get("timelapse_session")
        assert token not in client.board.sessions
        assert client.get("/login?next=/partials/jobs", follow_redirects=False).headers["location"] == "/partials/jobs"
        response = client.post("/logout", follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/login")
        assert client.board.sessions == {}
        client.cookies.set("timelapse_session", token or "")
        assert client.get("/", follow_redirects=False).status_code == 303
    with serve(tmp_path / "b") as client:
        assert client.get("/login?next=/x", follow_redirects=False).headers["location"] == "/x"


def test_ws8_security_headers(tmp_path):
    with serve(tmp_path) as client:
        for response in (client.get("/"), client.get("/missing"), client.get("/healthz", headers={"Host": "evil"})):
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert re.fullmatch(r"[0-9a-f]{16}", response.headers["X-Request-ID"])


def test_ws9_unexpected_errors_hide_details(tmp_path, caplog):
    with serve(tmp_path) as client:
        client.protect.camera_error = RuntimeError("secret detail")
        response = client.get("/partials/cameras")
        request_id = response.headers["X-Request-ID"]
        assert response.status_code == 500
        assert response.text == f"Internal server error. Request ID: {request_id}"
        assert request_id in caplog.text


# --- Routes --------------------------------------------------------------------------------------


def test_dashboard(tmp_path):
    with serve(tmp_path, timezone=ZoneInfo("America/New_York"), password=PASSWORD) as client:
        login(client)
        page = client.get("/").text
        assert "Times are interpreted in the server’s America/New_York timezone." in page
        assert "Log Out" in page
        css = client.get("/static/app.css").content
        assert f"/static/app.css?v={hashlib.sha256(css).hexdigest()[:12]}" in page
        assert 'hx-trigger="load, stateChanged from:body, every 2s"' in page
        assert 'hx-trigger="load, stateChanged from:body, every 5s"' in page
        assert re.search(r'name="start" value="\d{4}-\d\d-\d\dT\d\d:\d\d"', page)
        for speed in ("1x", "60x", "120x", "300x", "600x"):
            assert f'value="{speed}"' in page
    assert "EventSource" not in (Path(__file__).parents[1] / "timelapse" / "static" / "app.js").read_text()


def test_healthz_and_status(tmp_path):
    with serve(tmp_path) as client:
        assert client.get("/healthz").json() == {"status": "healthy", "connection_configured": True, "active_jobs": 0}
        page = client.get("/partials/status").text
        assert "Server Healthy" in page
        assert "<code>4.0.0</code>" in page
        assert "Protect Connection Configured" in page
        assert str(client.board.settings.output_dir) in page
        assert re.search(r"free of [\d.]+ \w+\s+\(\d+\.\d% available\)", page)
    with serve(tmp_path, connection=None, missing_connection_values=("UNIFI_PROTECT_TOKEN",)) as client:
        client.board.protect = None
        page = client.get("/partials/status").text
        assert "Configuration Needed" in page
        assert "UNIFI_PROTECT_TOKEN" in page
        assert "test-token" not in page
        assert client.get("/healthz").json()["connection_configured"] is False
        response = client.get("/partials/cameras")
        assert response.status_code == 400
        assert (
            "Server configuration is incomplete. Set UNIFI_PROTECT_TOKEN and restart the web server." in response.text
        )


def test_status_low_storage(tmp_path, monkeypatch):
    with serve(tmp_path) as client:
        monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(total=100 * 1024**3, used=0, free=1024**3))
        assert 'data-storage-low="true"' in client.get("/partials/status").text

        def broken(_):
            raise OSError("gone")

        monkeypatch.setattr(shutil, "disk_usage", broken)
        assert "Available storage could not be measured." in client.get("/partials/status").text


def test_cameras_partial_cache_and_errors(tmp_path):
    with serve(tmp_path) as client:
        page = client.get("/partials/cameras").text
        assert '<input type="checkbox" name="camera_ids" value="camera-1" data-camera-name="Front Door">' in page
        client.protect.camera_list = [YARD]
        assert "Front Door" in client.get("/partials/cameras").text
        assert "Front Door" not in client.get("/partials/cameras?refresh=true").text
        for error, status in ((OperationTimeoutError("Camera discovery exceeded"), 504),
                              (TimelapseError("Could not load cameras from UniFi Protect: offline"), 502)):  # fmt: skip
            client.protect.camera_error = error
            response = client.get("/partials/cameras?refresh=true")
            assert response.status_code == status
            assert "Could Not Load Cameras" in response.text
            assert str(error) in response.text


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"camera_ids": []}, "Select at least one available camera."),
        ({"camera_ids": ["unknown"]}, "Select at least one available camera."),
        ({"day": ""}, "Export date is required."),
        ({"day": "2026-02-30"}, "Export date must be a valid calendar date."),
        ({"range_mode": "exact", "start": "", "end": "2026-07-20T09:00"}, "Start time is required."),
        ({"range_mode": "exact", "start": "2026-07-20T08:00", "end": ""}, "End time is required."),
        ({"range_mode": "exact", "start": "soon", "end": "2026-07-20T09:00"},
         "Start time must be a valid date and time."),
        ({"range_mode": "exact", "start": "2026-07-20T09:00", "end": "2026-07-20T08:00"},
         "End time must be after start time."),
        ({"range_mode": "weekly"}, "Choose a full day or an exact time range."),
        ({"speed": "2x"}, "Choose a supported timelapse speed."),
        ({"range_mode": "exact", "start": "2026-07-01T00:00", "end": "2026-07-20T00:00"},
         "Exports are limited to 168 hours."),
    ],
)  # fmt: skip
def test_wr1_export_validation(tmp_path, form, message):
    with serve(tmp_path) as client:
        response = client.post("/actions/export", data={**FULL_DAY, **form})
        assert response.status_code == 400
        assert message in response.text
        assert response.headers["HX-Trigger"] == "stateChanged"
        assert 'class="toast toast-error"' in response.text


def test_wr1_automation_name_required(tmp_path):
    with serve(tmp_path) as client:
        response = client.post("/actions/automations", data={"automation_name": " ", "camera_ids": ["camera-1"]})
        assert (response.status_code, "Automation name is required." in response.text) == (400, True)


def test_wr2_wr3_export_requests_and_conflicts(tmp_path):
    with serve(tmp_path, timezone=ZoneInfo("America/New_York")) as client:
        response = client.post("/actions/export", data={**FULL_DAY, "camera_ids": ["camera-1", "camera-1"]})
        assert "Started 1 export(s)." in response.text
        assert response.headers["HX-Trigger"] == "stateChanged"
        settle(client)
        [request] = client.protect.exports
        assert request.start.isoformat() == "2026-07-20T00:00:00-04:00"
        assert request.end.isoformat() == "2026-07-21T00:00:00-04:00"
        assert request.full_day
        assert request.filename == "timelapse_Front_Door_2026_07_20_2026_07_21_600x_6bf6f341d9a3.mp4"
        response = client.post("/actions/export", data={**FULL_DAY, "camera_ids": ["camera-2", "camera-1"]})
        assert response.status_code == 409
        assert "already exists" in response.text
        assert len(client.board.queue.jobs) == 1
        exact = {**FULL_DAY, "range_mode": "exact", "start": "2026-07-20T08:00", "end": "2026-07-20T16:00:00+00:00"}
        assert client.post("/actions/export", data=exact).status_code == 200
        settle(client)
        assert client.protect.exports[-1].filename.endswith("_600x__6bf6f341d9a3.mp4")
        assert client.protect.exports[-1].start.isoformat() == "2026-07-20T08:00:00-04:00"


def test_wr4_thumbnails(tmp_path):
    with serve(tmp_path, timezone=ZoneInfo("America/New_York")) as client:
        response = client.get("/api/thumbnails/camera-1?timestamp=2026-07-20T08:00")
        assert response.content == b"jpeg"
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["X-TimeLapse-Thumbnail-Source"] == "exact"
        assert response.headers["Cache-Control"] == "no-store"
        assert client.protect.thumbnails == [("camera-1", datetime(2026, 7, 20, 12, tzinfo=UTC))]
        response = client.get("/api/thumbnails/camera-9?timestamp=2026-07-20T08:00")
        assert response.status_code == 400
        assert response.text == "The selected camera is no longer available. Refresh the camera list and try again."
        assert client.get("/api/thumbnails/camera-1?timestamp=short").status_code == 400
        assert client.get("/api/thumbnails/camera-1?timestamp=2026-07-20T99:99").status_code == 400


def test_wr5_unreachable_console(tmp_path):
    protect = FakeProtect([CAMERA])
    protect.camera_error = TimelapseError("Could not load cameras from UniFi Protect: connection refused")
    with serve(tmp_path, protect=protect) as client:
        responses = [
            client.get("/api/thumbnails/camera-1?timestamp=2026-07-20T08:00"),
            client.get("/partials/cameras"),
            client.post("/actions/export", data=FULL_DAY),
            client.post("/actions/automations", data={"automation_name": "A", "camera_ids": ["camera-1"]}),
        ]
        for response in responses:
            assert response.status_code == 502
            assert "Could not load cameras" in response.text
            assert "connection refused" in response.text


def test_wr6_job_cards_and_actions(tmp_path):
    with serve(tmp_path) as client:
        client.protect.gate = asyncio.Event()
        client.post("/actions/export", data={**FULL_DAY, "range_mode": "exact", "start": "2026-07-20T08:00",
                                              "end": "2026-07-20T09:00"})  # fmt: skip
        client.post("/actions/export", data=FULL_DAY)
        while not client.protect.exports:
            time.sleep(0.01)
        jobs = list(client.board.queue.jobs.values())
        jobs[0].total_bytes, jobs[0].downloaded_bytes, jobs[0].bytes_per_second = 2048, 1024, 512.0
        page = client.get("/partials/jobs").text
        assert "Exporting" in page
        assert "Jul 20, 2026 · 8:00 AM — Jul 20, 2026 · 9:00 AM" in page
        assert '<progress max="100" value="50.0">' in page
        assert "1.0 KiB / 2.0 KiB" in page
        assert "512 bytes/s" in page
        assert '<small class="job-file-size">2.0 KiB</small>' in page
        assert page.count("Cancel</button>") == 2
        response = client.delete(f"/actions/jobs/{jobs[1].id}")
        assert "Cancellation requested." in response.text
        client.portal.call(client.protect.gate.set)
        settle(client)
        page = client.get("/partials/jobs").text
        assert "Ready" in page
        assert "Cancelled" in page
        assert f'href="/exports/{jobs[0].id}"' in page
        assert f'data-video-url="/exports/{jobs[0].id}/play"' in page
        assert f'<small class="job-file-size">{len(MP4)} bytes</small>' in page
        assert page.count("job-file-size") == 1
        assert "Retry</button>" in page
        response = client.post(f"/actions/jobs/{jobs[0].id}/retry")
        assert (response.status_code, "Only failed or cancelled exports can be retried." in response.text) == (
            400,
            True,
        )
        response = client.post(f"/actions/jobs/{jobs[1].id}/retry")
        assert "Export queued again." in response.text
        settle(client)
        assert len(client.board.queue.jobs) == 2
        assert [job.status for job in client.board.queue.jobs.values()] == ["completed", "completed"]
        assert client.delete("/actions/jobs/unknown").status_code == 404
        response = client.delete(f"/actions/jobs/{jobs[0].id}")
        assert "Export deleted." in response.text
        assert not jobs[0].output.exists()


def test_wr6_status_labels(tmp_path):
    with serve(tmp_path) as client:
        client.protect.export_error = TimelapseError("console said no")
        client.post("/actions/export", data=FULL_DAY)
        settle(client)
        job = next(iter(client.board.queue.jobs.values()))
        job.daily = True
        page = client.get("/partials/jobs").text
        assert "Failed" in page
        assert "console said no" in page
        assert '<span class="mini-chip">Daily</span>' in page
        assert "job-file-size" not in page
        job.status, job.error = "queued", "rate limited"
        assert "Queued — Rate Limited" in client.get("/partials/jobs").text
        job.error = None
        assert ">Queued<" in client.get("/partials/jobs").text


def test_exports_download_play_and_ranges(tmp_path):
    with serve(tmp_path) as client:
        client.post("/actions/export", data=FULL_DAY)
        settle(client)
        job_id = next(iter(client.board.queue.jobs))
        download = client.get(f"/exports/{job_id}")
        assert download.content == MP4
        assert download.headers["content-type"] == "video/mp4"
        assert download.headers["content-disposition"].startswith("attachment")
        play = client.get(f"/exports/{job_id}/play", headers={"Range": "bytes=0-3"})
        assert play.status_code == 206
        assert play.content == MP4[:4]
        assert play.headers["accept-ranges"] == "bytes"
        assert play.headers["content-range"] == f"bytes 0-3/{len(MP4)}"
        assert play.headers["content-disposition"].startswith("inline")
        missing = client.get("/exports/unknown")
        assert (missing.status_code, missing.text) == (404, "Export is not available.")


def test_wr7_automation_cards_and_actions(tmp_path):
    with serve(tmp_path, timezone=ZoneInfo("America/New_York")) as client:
        client.protect.camera_list = []
        response = client.post(
            "/actions/automations", data={"automation_name": "Front", "camera_ids": ["camera-1"], "speed": "120x"}
        )
        assert response.status_code == 400
        client.protect.camera_list = [CAMERA, YARD]
        client.board.camera_cache = None
        client.protect.gate = asyncio.Event()
        response = client.post(
            "/actions/automations",
            data={"automation_name": "Front", "camera_ids": ["camera-1", "camera-2"], "speed": "120x"},
        )
        assert "Created Daily Automation for 2 camera(s)." in response.text
        [automation] = client.board.registry.automations
        assert (automation.connection, automation.timezone) == (("web-environment", "default"), "America/New_York")
        assert automation.output_directory == client.board.settings.output_dir
        duplicate = client.post("/actions/automations", data={"automation_name": "FRONT", "camera_ids": ["camera-1"]})
        assert duplicate.status_code == 400
        page = client.get("/partials/automations").text
        assert "Front Door, Yard" in page
        assert re.search(r"Next Run \w{3} \d+ at 12:00 AM E[DS]T · 120x", page)
        assert "Last Completed Day: Not Run Yet" in page
        assert 'hx-target="#notifications"' in page
        assert ">Stop</button>" in page
        assert "Daily Automation stopped." in client.post(f"/actions/automations/{automation.id}/stop").text
        page = client.get("/partials/automations").text
        assert "Stopped · 120x" in page
        assert ">Start</button>" in page
        client.portal.call(client.protect.gate.set)
        board = client.board
        client.portal.call(board.runner.wait)
        board.registry.put(replace(board.registry.automations[0], status="paused", last_error="Needs a look",
                                   last_completed_day=date(2026, 7, 19)))  # fmt: skip
        page = client.get("/partials/automations").text
        assert "Needs Attention · 120x" in page
        assert "Needs a look" in page
        assert "Last Completed Day: Jul 19, 2026" in page
        assert ">Resume</button>" in page
        assert "Daily Automation started." in client.post(f"/actions/automations/{automation.id}/resume").text
        assert client.post(f"/actions/automations/{automation.id}/resume").status_code == 400
        response = client.delete(f"/actions/automations/{automation.id}")
        assert "Daily Automation removed. Existing artifacts were kept." in response.text
        assert client.post(f"/actions/automations/{automation.id}/stop").status_code == 404
        assert client.delete(f"/actions/automations/{automation.id}").status_code == 404


def test_wr7_next_run_prefers_retry_time(tmp_path):
    with serve(tmp_path) as client:
        board = client.board
        automation = board.registry.add(
            name="Later", cameras=[CAMERA], connection=("web-environment", "default"), speed="600x",
            output_directory=board.settings.output_dir, timezone="UTC",
            next_day=datetime.now(UTC).date() + timedelta(days=5),
        )  # fmt: skip
        retry = datetime(2026, 11, 15, 17, 5, tzinfo=UTC)
        board.registry.put(replace(automation, next_retry_at=retry))
        assert "Next Run Nov 15 at 5:05 PM UTC" in client.get("/partials/automations").text


def test_wr8_restart_restores_jobs_and_automations(tmp_path):
    with serve(tmp_path) as client:
        board = client.board
        automation = board.registry.add(
            name="Daily", cameras=[CAMERA], connection=("web-environment", "default"), speed="600x",
            output_directory=board.settings.output_dir, timezone="UTC",
        )  # fmt: skip
        assert client.portal.call(board.runner.run_due_once, automation.id)
        settle(client)
    with serve(tmp_path) as client:
        board = client.board
        [job] = board.queue.jobs.values()
        assert (job.status, job.daily) == ("completed", True)
        assert client.get(f"/exports/{job.id}").content == MP4
        assert client.get(f"/exports/{job.id}/play").status_code == 200
        assert board.runner._loops
        before = board.registry.automations[0].last_completed_day
        assert "Export deleted." in client.delete(f"/actions/jobs/{job.id}").text
        assert board.registry.automations[0].last_completed_day == before is not None


def test_wr8_legacy_files_load(tmp_path):
    data = tmp_path / "data"
    (data / "exports").mkdir(parents=True)
    shutil.copy(FIXTURES / "web-jobs-3.0.0.json", data / "web-jobs.json")
    registry_data = json.loads((FIXTURES / "web-automations-v3.json").read_text())
    registry_data["automations"][0]["output_directory"] = str(data / "exports")
    registry_data["automations"][0]["status"] = "stopped"
    (data / "web-automations.json").write_text(json.dumps(registry_data))
    name = "timelapse_Front_Door_2026_07_19_2026_07_20_600x_6bf6f341d9a3.mp4"
    (data / "exports" / name).write_bytes(MP4)
    with serve(tmp_path) as client:
        job_id = "batch:auto_81d31296ae0844fa8156c962c2111efd:2026-07-19:camera-1"
        assert client.get(f"/exports/{job_id}").content == MP4
        assert "Plant" in client.get("/partials/automations").text
        assert "Export deleted." in client.delete(f"/actions/jobs/{job_id}").text
    assert Registry(data / "web-automations.json").path.exists()


def test_wr9_shutdown_order(tmp_path):
    with serve(tmp_path) as client:
        client.protect.gate = asyncio.Event()
        client.post("/actions/export", data=FULL_DAY)
        job = next(iter(client.board.queue.jobs.values()))
    assert (job.status, job.error) == ("cancelled", "The server stopped before this export completed.")
    assert client.protect.closed


def test_settings_from_env(tmp_path):
    env = {
        "TZ": "America/New_York", "TIMELAPSE_WEB_DATA_DIR": str(tmp_path), "TIMELAPSE_WEB_PORT": "x",
        "TIMELAPSE_WEB_TRUSTED_HOSTS": " NAS.local , 10.0.0.2 ,", "TIMELAPSE_WEB_PASSWORD": "",
        "TIMELAPSE_WEB_SESSION_HOURS": "0", "TIMELAPSE_WEB_MAX_QUEUED_EXPORTS": "-3",
        "TIMELAPSE_WEB_COOKIE_SECURE": "YES", "UNIFI_PROTECT_URL": " https://p.local/ ", "UNIFI_PROTECT_TOKEN": "t",
        "UNIFI_PROTECT_USERNAME": "u", "UNIFI_PROTECT_PASSWORD": "p", "UNIFI_PROTECT_VERIFY_SSL": "off",
        "TIMELAPSE_MAX_DOWNLOAD_MIB": "0",
    }  # fmt: skip
    settings = Settings.from_env(env)
    assert (settings.port, settings.password, settings.session_hours, settings.max_queued_exports) == (8000, None, 1, 0)
    assert settings.trusted_hosts == {"nas.local", "10.0.0.2"}
    assert settings.output_dir == tmp_path / "exports"
    assert settings.cookie_secure
    assert settings.connection == Connection("https://p.local", "t", "u", "p", verify_ssl=False, max_download_mib=0)
    defaults = Settings.from_env({})
    assert (defaults.host, defaults.username, defaults.retention_days, str(defaults.timezone)) == (
        "127.0.0.1", "timelapse", 0, "UTC"
    )  # fmt: skip
    assert defaults.missing_connection_values == (
        "UNIFI_PROTECT_URL", "UNIFI_PROTECT_TOKEN", "UNIFI_PROTECT_USERNAME", "UNIFI_PROTECT_PASSWORD"
    )  # fmt: skip
    with pytest.raises(ValueError, match="valid IANA timezone"):
        Settings.from_env({"TZ": "Nowhere/City"})


def test_assets_use_root_relative_links(tmp_path):
    # Absolute http:// links break behind an HTTPS reverse proxy (mixed content).
    with serve(tmp_path, password=PASSWORD) as client:
        pages = [client.get("/login").text]
        login(client)
        pages.append(client.get("/", headers={"X-Forwarded-Proto": "https"}).text)
        for page in pages:
            links = re.findall(r'(?:href|src)="([^"]*/static/[^"]*)"', page)
            assert links
            assert all(link.startswith("/static/") for link in links), links
            for link in links:
                assert client.get(link).status_code == 200

import asyncio
import enum
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from uiprotect.exceptions import NotAuthorized, NvrError

from tests.fakes import CAMERA, MP4, FakeResponse
from timelapse import OperationTimeoutError, RateLimitError, TimelapseError
from timelapse.model import Connection, ExportRequest
from timelapse.protect import Protect, RetryingClient, parse_url

URL = "https://protect.local/proxy/protect/integration/v1"
CONNECTION = Connection(URL, "test-token", "test-user", "test-password")
AT = datetime(2026, 7, 20, 8, tzinfo=UTC)
REQUEST = ExportRequest(CAMERA, AT, AT.replace(hour=10), "600x")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://p:7443/proxy/protect/integration/v1", ("p", 7443, "/proxy/protect/api/video/export")),
        ("https://protect.local/", ("protect.local", 443, "/proxy/protect/api/video/export")),
        ("https://protect.local/custom/v1", ("protect.local", 443, "/custom/api/video/export")),
        ("https://protect.local/custom", ("protect.local", 443, "/custom/api/video/export")),
        ("https://h/a/integration/v1/b/integration/v1", ("h", 443, "/a/integration/v1/b/api/video/export")),
    ],
)  # fmt: skip
def test_pr1_parse_url(url, expected):
    assert parse_url(url) == expected


@pytest.mark.parametrize(
    "url",
    ["http://protect.local", "https://user:pw@protect.local", "https://h/?a=1", "https://h/#x", "https://h:99999"],
)
def test_pr1_rejects_unsafe_urls(url):
    with pytest.raises(TimelapseError):
        parse_url(url)


def test_pr2_client_arguments_and_session_directory(tmp_path):
    session_dir = tmp_path / "protect"
    protect = Protect(Connection("https://h:7443", "test-token", "u", "p", verify_ssl=False), session_dir=session_dir)
    client = protect.client
    assert (client._host, client._port, client._username, client._password, client._api_key) == (
        "h", 7443, "u", "p", "test-token"
    )  # fmt: skip
    assert (client._verify_ssl, client.config_dir, client.store_sessions, client._max_retries) == (
        False, session_dir, True, 0
    )  # fmt: skip
    assert session_dir.stat().st_mode & 0o777 == 0o700


class State(enum.Enum):
    CONNECTED = "CONNECTED"


class FakeClient:
    def __init__(self) -> None:
        self.logins = 0
        self.events: list[str] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.camera_items: list = []
        self.camera_error: Exception | None = None
        self.exact: object = b"exact-jpeg"
        self.live: object = b"live-jpeg"
        self.requests: list[dict] = []
        self.login_error: Exception | None = None
        self.last_retry_after: float | None = None
        self.closed = False

    async def ensure_authenticated(self) -> None:
        self.logins += 1
        self.events.append("login")
        if self.login_error:
            raise self.login_error

    async def get_cameras_public(self):
        if self.camera_error:
            raise self.camera_error
        return self.camera_items

    async def api_request_raw(self, url, **kwargs):
        result = self.live if kwargs.get("public_api") else self.exact
        self.events.append("live" if kwargs.get("public_api") else "exact")
        if isinstance(result, Exception):
            raise result
        return result

    async def request(self, method, path, **kwargs):
        self.requests.append({"method": method, "path": path, **kwargs})
        self.events.append("export")
        await self.gate.wait()
        return FakeResponse()

    async def close_session(self) -> None:
        self.closed = True


def fake_protect(**changes) -> tuple[Protect, FakeClient]:
    client = FakeClient()
    connection = Connection(URL, "test-token", "u", "p", **changes)
    return Protect(connection, client=client, session_dir=Path("/nonexistent")), client


def test_pr3_cameras_sorted_and_converted():
    protect, client = fake_protect()
    client.camera_items = [
        SimpleNamespace(id="b", name="porch", state=State.CONNECTED, model="G4"),
        SimpleNamespace(id="a", name="Porch", state=None, model=None),
        SimpleNamespace(id="c", name=None, state=None, model=None),
    ]
    cameras = asyncio.run(protect.cameras())
    assert [(c.id, c.name, c.state, c.model) for c in cameras] == [
        ("c", "c", None, None), ("a", "Porch", None, None), ("b", "porch", "CONNECTED", "G4")
    ]  # fmt: skip
    client.camera_error = NvrError("network down")
    with pytest.raises(TimelapseError, match="Could not load cameras from UniFi Protect: network down"):
        asyncio.run(protect.cameras())


def stub_transport(client: RetryingClient, statuses: list[tuple[int, dict]]) -> list[str]:
    calls: list[str] = []
    token = jwt.encode({"exp": int(time.time()) + 3600}, "k" * 32, algorithm="HS256")

    async def do_request(session, method, url, headers, **kwargs):
        calls.append(method)
        await asyncio.sleep(0)
        if method == "post":
            return FakeResponse(headers={"set-cookie": f"TOKEN={token}; Path=/", "x-csrf-token": "csrf"})
        status, response_headers = statuses.pop(0) if statuses else (200, {"Content-Type": "video/mp4"})
        return FakeResponse(MP4, status=status, headers=response_headers)

    client._do_request = do_request  # type: ignore[method-assign]
    return calls


def real_client(tmp_path: Path, sleeps: list[float]) -> RetryingClient:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return RetryingClient(
        "h", 443, username="u", password="p", api_key="test-token", config_dir=tmp_path, max_retries=0,
        sleep=sleep, jitter=lambda _a, _b: 0.0,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("statuses", "expected_sleeps", "final_status", "recorded"),
    [
        ([(429, {"Retry-After": "2"}), (200, {})], [2.0], 200, None),
        ([(429, {})] * 4, [1.0, 2.0, 4.0], 429, None),
        ([(429, {"Retry-After": "900"})], [], 429, 900.0),
        ([(503, {}), (408, {}), (200, {})], [1.0, 2.0], 200, None),
        ([(429, {"Retry-After": "0"}), (404, {})], [0.1], 404, None),
    ],
)
def test_pr4_retry_policy(tmp_path, statuses, expected_sleeps, final_status, recorded):
    sleeps: list[float] = []

    async def scenario():
        client = real_client(tmp_path, sleeps)
        stub_transport(client, list(statuses))
        response = await client.request("get", "/x", auto_close=False)
        await client.close_session()
        return response.status, client.last_retry_after

    assert asyncio.run(scenario()) == (final_status, recorded)
    assert sleeps == expected_sleeps


def test_pr4_jitter_bounds(tmp_path):
    seen: list[tuple[float, float]] = []

    async def scenario():
        client = real_client(tmp_path, [])
        client.jitter = lambda a, b: seen.append((a, b)) or b
        stub_transport(client, [(500, {}), (429, {"Retry-After": "8"})])
        await client.request("get", "/x")
        await client.close_session()

    asyncio.run(scenario())
    assert seen == [(-0.25, 0.25), (0, 2.0)]


def test_pr6_one_login_shared_and_reused(tmp_path):
    async def scenario():
        protect = Protect(CONNECTION, client=real_client(tmp_path, []), session_dir=tmp_path)
        calls = stub_transport(protect.client, [])
        await asyncio.gather(
            protect.export(REQUEST, tmp_path / "a.mp4"),
            protect.export(ExportRequest(CAMERA, AT, AT.replace(hour=11), "1x"), tmp_path / "b.mp4"),
            protect.thumbnail(CAMERA, AT),
            protect.thumbnail(CAMERA, AT),
        )
        await protect.thumbnail(CAMERA, AT)
        await protect.close()
        again = Protect(CONNECTION, client=real_client(tmp_path, []), session_dir=tmp_path)
        again_calls = stub_transport(again.client, [])
        await again.thumbnail(CAMERA, AT)
        await again.close()
        return calls.count("post"), again_calls.count("post")

    assert asyncio.run(scenario()) == (1, 0)
    assert (tmp_path / "unifi_protect.json").stat().st_mode & 0o777 == 0o600


def test_pr5_rate_limit_messages():
    protect, client = fake_protect()
    client.login_error = NvrError("Request failed: /api/auth/login - Status: 429 - Reason: Too Many Requests")
    client.last_retry_after = 120
    with pytest.raises(RateLimitError) as caught:
        asyncio.run(protect.export(REQUEST, Path("/nonexistent/out.mp4")))
    assert "UniFi Protect returned HTTP 429 after bounded retries. Wait at least 120 seconds" in str(caught.value)
    assert "do not rotate accounts or start repeated copies" in str(caught.value)
    assert caught.value.retry_not_before is not None
    client.last_retry_after = None
    client.camera_error = NvrError("HTTP 429 from console")
    with pytest.raises(RateLimitError, match="authentication rate-limit window clear") as caught:
        asyncio.run(protect.cameras())
    assert caught.value.retry_not_before is None
    client.live = NvrError("too many requests")
    with pytest.raises(RateLimitError):
        asyncio.run(protect.thumbnail(CAMERA, AT))


def test_pr7_separate_slots_and_login_after_slot(tmp_path):
    protect, client = fake_protect()
    client.gate.clear()

    async def scenario():
        exports = [asyncio.create_task(protect.export(REQUEST, tmp_path / f"{n}.mp4")) for n in range(3)]
        for _ in range(10):
            await asyncio.sleep(0)
        assert client.events.count("export") == 2
        assert client.logins == 2
        assert await protect.thumbnail(CAMERA, AT) == (b"exact-jpeg", "exact")
        client.gate.set()
        await asyncio.gather(*exports)

    asyncio.run(scenario())
    assert client.logins == 4
    params = client.requests[0]["params"]
    assert params == {
        "camera": "camera-1", "start": str(int(AT.timestamp() * 1000)),
        "end": str(int(AT.replace(hour=10).timestamp() * 1000)), "channel": "0", "type": "timelapse", "fps": "40",
    }  # fmt: skip
    assert client.requests[0]["path"] == "/proxy/protect/api/video/export"
    assert client.requests[0]["require_auth"] is True
    assert client.requests[0]["timeout"] == 0


def test_export_params_for_normal_speed(tmp_path):
    protect, client = fake_protect()
    asyncio.run(protect.export(ExportRequest(CAMERA, AT, AT.replace(hour=9), "1x"), tmp_path / "a.mp4"))
    assert set(client.requests[0]["params"]) == {"camera", "start", "end", "channel"}


@pytest.mark.parametrize(
    ("exact", "live", "result"),
    [
        (NotAuthorized("no permission"), b"live-jpeg", (b"live-jpeg", "live")),
        (b"", b"live-jpeg", (b"live-jpeg", "live")),
        (b"exact-jpeg", NvrError("unused"), (b"exact-jpeg", "exact")),
    ],
)
def test_pr8_thumbnail_fallback(exact, live, result):
    protect, client = fake_protect()
    client.exact, client.live = exact, live
    assert asyncio.run(protect.thumbnail(CAMERA, AT)) == result


def test_pr8_thumbnail_failure_names_camera():
    protect, client = fake_protect()
    client.exact, client.live = NvrError("a"), NvrError("b")
    with pytest.raises(TimelapseError, match=r"Front Door.*readmedia/livestream"):
        asyncio.run(protect.thumbnail(CAMERA, AT))


def test_pr9_operation_deadline(tmp_path):
    protect, client = fake_protect(timeout_seconds=0.01)
    client.gate.clear()

    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    client.get_cameras_public = hang  # type: ignore[method-assign]
    client.api_request_raw = hang  # type: ignore[method-assign]

    async def scenario():
        return await asyncio.gather(
            protect.cameras(), protect.thumbnail(CAMERA, AT), protect.export(REQUEST, tmp_path / "a.mp4"),
            return_exceptions=True,
        )  # fmt: skip

    errors = asyncio.run(scenario())
    assert [str(error) for error in errors] == [
        "Camera discovery exceeded the configured 0.01-second operation timeout.",
        "Thumbnail request for Front Door exceeded the configured 0.01-second operation timeout.",
        "Timelapse export for Front Door exceeded the configured 0.01-second operation timeout.",
    ]
    assert all(isinstance(error, OperationTimeoutError) for error in errors)


def test_pr10_close_is_bounded_and_quiet(caplog):
    protect, client = fake_protect()
    asyncio.run(protect.close())
    assert client.closed

    async def hang() -> None:
        await asyncio.Event().wait()

    client.close_session = hang  # type: ignore[method-assign]
    protect.close_timeout = 0.01
    asyncio.run(protect.close())

    async def fail() -> None:
        raise RuntimeError("boom")

    client.close_session = fail  # type: ignore[method-assign]
    asyncio.run(protect.close())
    assert "Could not close" in caplog.text


def test_d9_existing_output_refused_before_request(tmp_path):
    protect, client = fake_protect()
    (tmp_path / "a.mp4").write_bytes(MP4)
    with pytest.raises(TimelapseError, match="refusing to overwrite"):
        asyncio.run(protect.export(REQUEST, tmp_path / "a.mp4"))
    assert client.requests == []

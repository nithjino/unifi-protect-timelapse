from __future__ import annotations

import asyncio
import errno
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

import timelapse.download as download_module
from timelapse import ProtectRateLimitError, TimelapseError
from timelapse.config import Config
from timelapse.download import DownloadProgress, default_output_path, download_timelapse
from timelapse.protect import CameraInfo, parse_connection

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from uiprotect import ProtectApiClient


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterated = False

    async def iter_chunked(self, chunk_size: int) -> AsyncIterator[bytes]:
        del chunk_size
        self.iterated = True
        for chunk in self.chunks:
            yield chunk

    async def read(self, size: int = -1) -> bytes:
        del size
        return b""


class _BlockingContent:
    def __init__(self) -> None:
        self.blocked = asyncio.Event()

    async def iter_chunked(self, chunk_size: int) -> AsyncIterator[bytes]:
        del chunk_size
        yield b"partial"
        self.blocked.set()
        await asyncio.Event().wait()

    async def read(self, size: int = -1) -> bytes:
        del size
        return b""


class _FakeResponse:
    def __init__(
        self,
        content: _FakeContent | _BlockingContent,
        headers: dict[str, str],
        *,
        status: int = 200,
    ) -> None:
        self.status = status
        self.reason = "OK"
        self.content = content
        self.headers = headers
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.headers: dict[str, str] = {}
        self.request_kwargs: dict[str, object] = {}

    def set_header(self, key: str, value: str) -> None:
        self.headers[key] = value

    async def request(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        del method, url
        self.request_kwargs = kwargs
        return self.response


def _config() -> Config:
    return Config(
        instance_url="https://protect.local/proxy/protect/integration/v1",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=True,
        speed="120x",
        start=datetime(2026, 7, 11, 8, tzinfo=UTC),
        end=datetime(2026, 7, 11, 9, tzinfo=UTC),
        output=None,
        request_timeout_seconds=0,
        max_download_mib=10240,
    )


@pytest.mark.parametrize(
    ("camera_id", "camera_name", "full_day", "expected"),
    [
        pytest.param(
            "camera-1",
            "Front Door",
            False,
            "timelapse_Front_Door_2026_07_11_08_00_00_2026_07_11_09_00_00_120x__6bf6f341d9a3.mp4",
            id="exact-range",
        ),
        pytest.param(
            "camera-1",
            "Front Door",
            True,
            "timelapse_Front_Door_2026_07_11_2026_07_12_120x_6bf6f341d9a3.mp4",
            id="full-day",
        ),
        pytest.param(
            "camera-1",
            "玄関 📷",
            True,
            "timelapse_玄関_📷_2026_07_11_2026_07_12_120x_6bf6f341d9a3.mp4",
            id="unicode-name",
        ),
        pytest.param(
            "camera-2",
            "Front Door",
            True,
            "timelapse_Front_Door_2026_07_11_2026_07_12_120x_381fff0067f8.mp4",
            id="same-name-on-another-camera",
        ),
    ],
)
def test_default_output_path_names_the_camera_range_and_speed(
    camera_id: str,
    camera_name: str,
    *,
    full_day: bool,
    expected: str,
) -> None:
    camera = CameraInfo(id=camera_id, name=camera_name, state=None, model=None)
    config = _config()
    if full_day:
        config = replace(
            config,
            start=datetime(2026, 7, 11, tzinfo=UTC),
            end=datetime(2026, 7, 12, tzinfo=UTC),
            full_day=True,
        )

    assert default_output_path(config, camera).name == expected


@pytest.mark.parametrize(
    ("speed", "expected_export_options"),
    [
        ("1x", {}),
        ("60x", {"type": "timelapse", "fps": "4"}),
    ],
)
def test_download_uses_the_protect_export_mode_for_the_selected_speed(
    tmp_path: Path,
    speed: str,
    expected_export_options: dict[str, str],
) -> None:
    response = _FakeResponse(_FakeContent([b"\x00\x00\x00\x18ftypisomdata"]), {})
    fake_client = _FakeClient(response)
    config = replace(_config(), speed=speed)

    asyncio.run(
        download_timelapse(
            config,
            parse_connection(config.instance_url),
            cast("ProtectApiClient", fake_client),
            CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
            tmp_path / f"{speed}.mp4",
        )
    )

    params = cast("dict[str, str]", fake_client.request_kwargs["params"])
    assert {key: params[key] for key in ("camera", "start", "end", "channel")} == {
        "camera": "camera-1",
        "start": "1783756800000",
        "end": "1783760400000",
        "channel": "0",
    }
    assert {key: params[key] for key in ("type", "fps") if key in params} == expected_export_options


@pytest.mark.parametrize("content_length", [16, None])
def test_download_emits_initial_throttled_and_final_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content_length: int | None,
) -> None:
    video = b"\x00\x00\x00\x18ftypisomdata"
    headers = {} if content_length is None else {"Content-Length": str(content_length)}
    response = _FakeResponse(_FakeContent([video[:8], video[8:]]), headers)
    client = cast("ProtectApiClient", _FakeClient(response))
    camera = CameraInfo(id="camera-1", name="Front Door", state="CONNECTED", model="G5")
    output = tmp_path / "output.mp4"
    progress: list[DownloadProgress] = []
    times = iter((10.0, 10.05, 10.2, 10.3))
    monkeypatch.setattr(download_module, "monotonic", lambda: next(times))

    asyncio.run(
        download_timelapse(
            _config(),
            parse_connection(_config().instance_url),
            client,
            camera,
            output,
            progress.append,
        )
    )

    assert output.read_bytes() == video
    assert response.released is True
    assert [event.downloaded_bytes for event in progress] == [0, len(video), len(video)]
    assert [event.total_bytes for event in progress] == [content_length, content_length, content_length]
    assert progress[0].elapsed_seconds == 0
    assert progress[0].bytes_per_second == 0
    assert progress[1].bytes_per_second == pytest.approx(80)
    assert progress[-1].bytes_per_second == pytest.approx(160 / 3)
    assert all(path.suffix != ".part" for path in tmp_path.iterdir())


def test_download_cancellation_releases_response_and_removes_partial_file(tmp_path: Path) -> None:
    output = tmp_path / "cancelled.mp4"

    async def cancel_download() -> _FakeResponse:
        content = _BlockingContent()
        response = _FakeResponse(content, {})
        client = cast("ProtectApiClient", _FakeClient(response))
        task = asyncio.create_task(
            download_timelapse(
                _config(),
                parse_connection(_config().instance_url),
                client,
                CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
                output,
            )
        )
        await asyncio.wait_for(content.blocked.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return response

    response = asyncio.run(cancel_download())

    assert response.released is True
    assert not output.exists()
    assert all(path.suffix != ".part" for path in tmp_path.iterdir())


def test_download_rejects_known_size_before_streaming_when_storage_is_insufficient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = _FakeContent([b"\x00\x00\x00\x18ftypisomdata"])
    response = _FakeResponse(content, {"Content-Length": "60"})
    client = cast("ProtectApiClient", _FakeClient(response))
    monkeypatch.setattr(
        download_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=900, free=100),
    )

    with pytest.raises(TimelapseError, match=r"not enough free storage.*50 bytes is available.*50 bytes safety"):
        asyncio.run(
            download_timelapse(
                _config(),
                parse_connection(_config().instance_url),
                client,
                CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
                tmp_path / "insufficient.mp4",
            )
        )

    assert content.iterated is False
    assert response.released is True
    assert list(tmp_path.iterdir()) == []


def test_download_cleans_up_when_the_disk_fills_mid_stream(tmp_path: Path) -> None:
    class _FullDiskContent(_FakeContent):
        async def iter_chunked(self, chunk_size: int) -> AsyncIterator[bytes]:
            del chunk_size
            yield b"\x00\x00\x00\x18ftypisom"
            raise OSError(errno.ENOSPC, "No space left on device")

    response = _FakeResponse(_FullDiskContent([]), {})
    client = cast("ProtectApiClient", _FakeClient(response))

    with pytest.raises(TimelapseError, match="No space left"):
        asyncio.run(
            download_timelapse(
                _config(),
                parse_connection(_config().instance_url),
                client,
                CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
                tmp_path / "full.mp4",
            )
        )

    assert response.released is True
    assert list(tmp_path.iterdir()) == []


def test_download_preserves_an_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep me")
    response = _FakeResponse(_FakeContent([b"\x00\x00\x00\x18ftypisomnew data"]), {})
    client = cast("ProtectApiClient", _FakeClient(response))

    with pytest.raises(TimelapseError, match="refusing to overwrite"):
        asyncio.run(
            download_timelapse(
                _config(),
                parse_connection(_config().instance_url),
                client,
                CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
                output,
            )
        )

    assert output.read_bytes() == b"keep me"
    assert response.released is True
    assert all(path.suffix != ".part" for path in tmp_path.iterdir())


@pytest.mark.parametrize(
    ("chunks", "headers", "status", "error", "message"),
    [
        ([], {}, 200, TimelapseError, "empty response"),
        ([b"not an mp4"], {}, 200, TimelapseError, "valid MP4 file signature"),
        ([b"\x00\x00\x00\x18ftypisom"], {"Content-Type": "text/html"}, 200, TimelapseError, "unexpected content type"),
        ([], {}, 429, ProtectRateLimitError, "bounded retries"),
    ],
)
def test_download_rejects_unusable_export_responses(
    tmp_path: Path,
    chunks: list[bytes],
    headers: dict[str, str],
    status: int,
    error: type[TimelapseError],
    message: str,
) -> None:
    output = tmp_path / "invalid.mp4"
    response = _FakeResponse(_FakeContent(chunks), headers, status=status)
    client = cast("ProtectApiClient", _FakeClient(response))

    with pytest.raises(error, match=message):
        asyncio.run(
            download_timelapse(
                _config(),
                parse_connection(_config().instance_url),
                client,
                CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
                output,
            )
        )

    assert not output.exists()
    assert response.released is True
    assert all(path.suffix != ".part" for path in tmp_path.iterdir())


def test_partial_file_cleanup_failure_does_not_replace_export_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "invalid.mp4"
    response = _FakeResponse(_FakeContent([b"not an mp4"]), {})
    client = cast("ProtectApiClient", _FakeClient(response))
    original_unlink = download_module.Path.unlink

    def fail_part_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.suffix == ".part":
            message = "cleanup failed"
            raise OSError(message)
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(download_module.Path, "unlink", fail_part_unlink)

    with pytest.raises(TimelapseError, match="valid MP4 file signature"):
        asyncio.run(
            download_timelapse(
                _config(),
                parse_connection(_config().instance_url),
                client,
                CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
                output,
            )
        )

    assert response.released is True

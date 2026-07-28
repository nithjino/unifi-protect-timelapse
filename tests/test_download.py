from __future__ import annotations

import asyncio
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


def test_default_output_path_preserves_safe_unicode_camera_name() -> None:
    camera = CameraInfo(id="camera-1", name="玄関 📷", state=None, model=None)

    output = default_output_path(_config(), camera)

    assert "玄関_📷" in output.name


def test_default_output_path_formats_exact_range_with_hash_last() -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)

    output = default_output_path(_config(), camera)

    assert output.name == ("timelapse_Front_Door_2026_07_11_08_00_00_2026_07_11_09_00_00_120x__6bf6f341d9a3.mp4")


def test_default_output_path_formats_full_day_without_times() -> None:
    camera = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    config = replace(
        _config(),
        start=datetime(2026, 7, 11, tzinfo=UTC),
        end=datetime(2026, 7, 12, tzinfo=UTC),
        full_day=True,
    )

    output = default_output_path(config, camera)

    assert output.name == "timelapse_Front_Door_2026_07_11_2026_07_12_120x_6bf6f341d9a3.mp4"


def test_default_output_path_distinguishes_cameras_with_colliding_names() -> None:
    first = CameraInfo(id="camera-1", name="Front Door", state=None, model=None)
    second = CameraInfo(id="camera-2", name="Front Door", state=None, model=None)

    first_output = default_output_path(_config(), first)
    second_output = default_output_path(_config(), second)

    assert first_output != second_output
    assert first_output.name.startswith("timelapse_Front_Door_")
    assert second_output.name.startswith("timelapse_Front_Door_")


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
        await content.blocked.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return response

    response = asyncio.run(cancel_download())

    assert response.released is True
    assert not output.exists()
    assert all(path.suffix != ".part" for path in tmp_path.iterdir())
    assert not download_module._STORAGE_RESERVATIONS


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
    assert not download_module._STORAGE_RESERVATIONS


def test_unknown_size_reserves_configured_maximum_across_concurrent_downloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mebibyte = download_module.MEBIBYTE
    monkeypatch.setattr(
        download_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10 * mebibyte, used=0, free=mebibyte + mebibyte // 2),
    )
    config = replace(_config(), max_download_mib=1)

    async def exercise() -> tuple[_FakeResponse, _FakeResponse, _FakeContent]:
        blocking_content = _BlockingContent()
        first_response = _FakeResponse(blocking_content, {})
        first_task = asyncio.create_task(
            download_timelapse(
                config,
                parse_connection(config.instance_url),
                cast("ProtectApiClient", _FakeClient(first_response)),
                CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
                tmp_path / "first.mp4",
            )
        )
        await blocking_content.blocked.wait()

        second_content = _FakeContent([b"\x00\x00\x00\x18ftypisom"])
        second_response = _FakeResponse(second_content, {"Content-Length": "16"})
        with pytest.raises(TimelapseError, match="not enough free storage"):
            await download_timelapse(
                config,
                parse_connection(config.instance_url),
                cast("ProtectApiClient", _FakeClient(second_response)),
                CameraInfo(id="camera-2", name="Back Yard", state=None, model=None),
                tmp_path / "second.mp4",
            )

        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        return first_response, second_response, second_content

    first_response, second_response, second_content = asyncio.run(exercise())

    assert first_response.released is True
    assert second_response.released is True
    assert second_content.iterated is False
    assert not download_module._STORAGE_RESERVATIONS


def test_download_stops_and_cleans_up_when_free_space_drops_mid_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = b"\x00\x00\x00\x18ftypisomdata"
    content = _FakeContent([video[:8], video[8:]])
    response = _FakeResponse(content, {"Content-Length": str(len(video))})
    client = cast("ProtectApiClient", _FakeClient(response))
    free_values = iter((100, 100, 55))
    monkeypatch.setattr(
        download_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=0, free=next(free_values)),
    )

    with pytest.raises(TimelapseError, match="free storage fell below the download safety buffer"):
        asyncio.run(
            download_timelapse(
                _config(),
                parse_connection(_config().instance_url),
                client,
                CameraInfo(id="camera-1", name="Front Door", state=None, model=None),
                tmp_path / "space-dropped.mp4",
            )
        )

    assert content.iterated is True
    assert response.released is True
    assert list(tmp_path.iterdir()) == []
    assert not download_module._STORAGE_RESERVATIONS


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


def test_download_surfaces_exhausted_http_429_with_actionable_message(tmp_path: Path) -> None:
    output = tmp_path / "rate-limited.mp4"
    response = _FakeResponse(_FakeContent([]), {}, status=429)
    client = cast("ProtectApiClient", _FakeClient(response))

    with pytest.raises(ProtectRateLimitError, match="bounded retries"):
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
    assert not output.exists()


@pytest.mark.parametrize(
    ("chunks", "headers", "message"),
    [
        ([], {}, "empty response"),
        ([b"not an mp4"], {}, "valid MP4 file signature"),
        ([b"\x00\x00\x00\x18ftypisom"], {"Content-Type": "text/html"}, "unexpected content type"),
    ],
)
def test_download_rejects_invalid_video_responses(
    tmp_path: Path,
    chunks: list[bytes],
    headers: dict[str, str],
    message: str,
) -> None:
    output = tmp_path / "invalid.mp4"
    response = _FakeResponse(_FakeContent(chunks), headers)
    client = cast("ProtectApiClient", _FakeClient(response))

    with pytest.raises(TimelapseError, match=message):
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

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fakes import MP4, FakeResponse
from timelapse import RateLimitError, TimelapseError
from timelapse.download import Progress, is_valid_mp4, retry_after_seconds, save_export


def save(response: FakeResponse, output: Path, *, max_bytes: int = 0, events: list | None = None) -> None:
    asyncio.run(
        save_export(response, output, max_bytes=max_bytes, on_progress=None if events is None else events.append)
    )


def leftovers(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_dl1_streams_chunks_into_place(tmp_path):
    output = tmp_path / "nested" / "out.mp4"
    response = FakeResponse([MP4, b"more", b"data"])
    save(response, output)
    assert output.read_bytes() == MP4 + b"moredata"
    assert leftovers(output.parent) == ["out.mp4"]
    assert response.released == 1


def test_dl2_progress_events(tmp_path):
    events: list[Progress] = []
    body = [MP4, *[b"x" * 10] * 50]
    save(FakeResponse(body, headers={"Content-Type": "video/mp4", "Content-Length": str(len(b"".join(body)))}),
         tmp_path / "a.mp4", events=events)  # fmt: skip
    assert events[0].downloaded_bytes == 0
    assert events[-1].downloaded_bytes == len(b"".join(body))
    assert events[-1].total_bytes == len(b"".join(body))
    assert len(events) < 10
    assert all(event.bytes_per_second >= 0 for event in events)
    events.clear()
    save(FakeResponse(headers={"Content-Length": "12abc"}), tmp_path / "b.mp4", events=events)
    assert events[-1].total_bytes is None


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FakeResponse(b""), "empty response"),
        (FakeResponse(b"not an mp4 at all"), "valid MP4 file signature"),
        (FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}), "unexpected content type"),
        (FakeResponse("bad camera é\n".encode(), status=500, reason="Server Error"),
         "timelapse export failed with HTTP 500: bad camera \\xe9"),
        (FakeResponse(b"", status=403, reason="Forbidden"), "timelapse export failed with HTTP 403: Forbidden"),
    ],
)  # fmt: skip
def test_dl3_rejects_bad_responses(tmp_path, response, message):
    with pytest.raises(TimelapseError, match=None) as caught:
        save(response, tmp_path / "out.mp4")
    assert message in str(caught.value)
    assert leftovers(tmp_path) == []
    assert response.released == 1


@pytest.mark.parametrize("content_type", ["application/mp4", "application/octet-stream", None])
def test_dl3_accepts_mp4_content_types(tmp_path, content_type):
    save(FakeResponse(headers={} if content_type is None else {"Content-Type": content_type}), tmp_path / "o.mp4")
    assert is_valid_mp4(tmp_path / "o.mp4")


def test_dl4_rate_limit_carries_the_wait(tmp_path):
    with pytest.raises(RateLimitError, match="bounded retries") as caught:
        save(FakeResponse(status=429, headers={"Retry-After": "30"}), tmp_path / "out.mp4")
    assert "Wait at least 30 seconds" in str(caught.value)
    assert caught.value.retry_not_before is not None
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0
    assert retry_after_seconds("soon") is None


def test_dl5_size_limit(tmp_path):
    with pytest.raises(TimelapseError, match="limit"):
        save(FakeResponse(headers={"Content-Length": "5000"}), tmp_path / "a.mp4", max_bytes=100)
    with pytest.raises(TimelapseError, match="limit"):
        save(FakeResponse([MP4, b"x" * 200]), tmp_path / "b.mp4", max_bytes=100)
    save(FakeResponse([MP4, b"x" * 200]), tmp_path / "c.mp4", max_bytes=0)
    assert leftovers(tmp_path) == ["c.mp4"]


@pytest.mark.parametrize(
    ("free", "length", "message"),
    [
        (500 * 1024**2, None, "an export of unknown size: 0 bytes is available after the 500.0 MiB safety buffer"),
        (3 * 1024**3, str(4 * 1024**3), "the 4.0 GiB export: 2.0 GiB is available after the 1.0 GiB safety buffer"),
    ],
)
def test_dl6_free_space(tmp_path, monkeypatch, free, length, message):
    total = 100 * 1024**3 if length else 10000 * 1024**2
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(total=total, used=0, free=free))
    response = FakeResponse(headers={"Content-Length": length} if length else {})
    with pytest.raises(TimelapseError) as caught:
        save(response, tmp_path / "out.mp4")
    assert f"not enough free storage for {message}" in str(caught.value)
    assert leftovers(tmp_path) == []


def test_dl7_never_overwrites(tmp_path):
    output = tmp_path / "out.mp4"
    output.write_bytes(b"old")
    response = FakeResponse()
    with pytest.raises(TimelapseError, match="refusing to overwrite existing output file"):
        save(response, output)
    assert output.read_bytes() == b"old"
    assert response.released == 1


def test_dl7_rechecks_before_rename(tmp_path):
    output = tmp_path / "out.mp4"

    class Racing(FakeResponse):
        async def _chunks(self):
            yield MP4
            output.write_bytes(b"winner")

    response = Racing()
    response.content.iter_chunked = lambda _: response._chunks()  # type: ignore[method-assign]
    with pytest.raises(TimelapseError, match="refusing to overwrite"):
        save(response, output)
    assert output.read_bytes() == b"winner"
    assert leftovers(tmp_path) == ["out.mp4"]


def test_dl8_write_errors_are_reported(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(TimelapseError, match="could not write"):
        save(FakeResponse(), blocker / "out.mp4")


def test_dl9_cancellation_cleans_up(tmp_path):
    gate = asyncio.Event()
    response = FakeResponse([MP4, b"never"], gate=gate)

    async def scenario():
        task = asyncio.create_task(save_export(response, tmp_path / "out.mp4", max_bytes=0))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert leftovers(tmp_path) == []
    assert response.released == 1


def test_dl9_cleanup_failure_keeps_original_error(tmp_path, caplog):
    response = FakeResponse(b"")

    def broken() -> None:
        raise RuntimeError("release failed")

    response.release = broken  # type: ignore[method-assign]
    with pytest.raises(TimelapseError, match="empty response"):
        save(response, tmp_path / "out.mp4")
    assert "Could not release" in caplog.text


@pytest.mark.parametrize(
    ("content", "valid"),
    [(b"\0\0\0\x18ftypisom", True), (b"", False), (b"not an mp4", False), (b"ftyp at start", False),
     (b"x" * 5000 + b"ftyp", False)],
)  # fmt: skip
def test_dl10_is_valid_mp4(tmp_path, content, valid):
    path = tmp_path / "v.mp4"
    path.write_bytes(content)
    assert is_valid_mp4(path) is valid
    assert not is_valid_mp4(tmp_path / "missing.mp4")
    assert not is_valid_mp4(tmp_path)

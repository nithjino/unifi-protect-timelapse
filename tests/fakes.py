"""Fakes shared by the tests. Nothing here touches the network."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from timelapse.download import Progress, save_export
from timelapse.model import Camera, ExportRequest

MP4 = b"\0\0\0\x18ftypisom" + b"\0" * 64
CAMERA = Camera("camera-1", "Front Door", "CONNECTED", "G5 Pro")


class FakeContent:
    def __init__(self, chunks: list[bytes], gate: asyncio.Event | None = None) -> None:
        self.chunks = chunks
        self.gate = gate

    async def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
        assert n == 1024 * 1024
        for chunk in self.chunks:
            if self.gate is not None:
                await self.gate.wait()
            yield chunk

    async def read(self, n: int) -> bytes:
        return b"".join(self.chunks)[:n]


class FakeResponse:
    def __init__(
        self,
        body: bytes | list[bytes] = MP4,
        *,
        status: int = 200,
        reason: str = "OK",
        headers: dict[str, str] | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.headers = {"Content-Type": "video/mp4"} if headers is None else headers
        self.content = FakeContent(body if isinstance(body, list) else [body], gate)
        self.released = 0

    def release(self) -> None:
        self.released += 1


class Clock:
    """A settable clock for code that takes ``now``."""

    def __init__(self, start: datetime | None = None) -> None:
        self.value = start or datetime(2026, 7, 13, 15, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeProtect:
    """Stands in for ``timelapse.protect.Protect``."""

    def __init__(self, cameras: list[Camera] | None = None) -> None:
        self.camera_list = [CAMERA] if cameras is None else cameras
        self.camera_error: Exception | None = None
        self.export_error: Exception | None = None
        self.exports: list[ExportRequest] = []
        self.gate: asyncio.Event | None = None
        self.thumbnails: list[tuple[str, datetime]] = []
        self.closed = False

    async def cameras(self) -> list[Camera]:
        if self.camera_error is not None:
            raise self.camera_error
        return list(self.camera_list)

    async def thumbnail(self, camera: Camera, at: datetime) -> tuple[bytes, str]:
        self.thumbnails.append((camera.id, at))
        return b"jpeg", "exact"

    async def export(self, request: ExportRequest, output: Path, on_progress=None) -> None:
        self.exports.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.export_error is not None:
            raise self.export_error
        await save_export(FakeResponse(), output, max_bytes=0, on_progress=on_progress)

    async def close(self) -> None:
        self.closed = True


def no_progress(_: Progress) -> None:
    """Ignore progress."""

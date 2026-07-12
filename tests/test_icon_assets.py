from __future__ import annotations

import plistlib
import struct
from pathlib import Path

ICON_DIRECTORY = Path(__file__).parent.parent / "assets" / "icons"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_dimensions(path: Path) -> tuple[int, int, int]:
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    width, height, _bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    return width, height, color_type


def test_master_icon_is_1024_rgba_png() -> None:
    assert _png_dimensions(ICON_DIRECTORY / "timelapse.png") == (1024, 1024, 6)


def test_platform_icon_containers_are_present() -> None:
    ico_header = (ICON_DIRECTORY / "timelapse.ico").read_bytes()[:6]
    reserved, image_type, image_count = struct.unpack("<HHH", ico_header)
    assert (reserved, image_type, image_count) == (0, 1, 7)

    icns = (ICON_DIRECTORY / "timelapse.icns").read_bytes()
    assert icns.startswith(b"icns")
    assert struct.unpack(">I", icns[4:8])[0] == len(icns)


def test_common_png_icon_sizes_are_available() -> None:
    for size in (16, 32, 48, 64, 128, 256, 512):
        assert _png_dimensions(ICON_DIRECTORY / "png" / f"timelapse-{size}.png")[:2] == (size, size)


def test_native_macos_bundle_declares_the_icon() -> None:
    with (ICON_DIRECTORY.parent.parent / "native-macos" / "Info.plist").open("rb") as file:
        info = plistlib.load(file)

    assert info["CFBundleIconFile"] == "TimeLapse.icns"

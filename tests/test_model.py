from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from timelapse.model import Camera, ExportRequest, day_bounds, format_bytes, latest_complete_day

UTC_DAY = datetime(2026, 7, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    ("camera", "start", "end", "full_day", "expected"),
    [
        (
            Camera("camera-1", "Front Door"),
            UTC_DAY.replace(hour=8),
            UTC_DAY.replace(hour=9),
            False,
            "timelapse_Front_Door_2026_07_11_08_00_00_2026_07_11_09_00_00_120x__6bf6f341d9a3.mp4",
        ),
        (
            Camera("camera-1", "Front Door"),
            UTC_DAY,
            UTC_DAY + timedelta(days=1),
            True,
            "timelapse_Front_Door_2026_07_11_2026_07_12_120x_6bf6f341d9a3.mp4",
        ),
        (
            Camera("camera-1", "玄関 📷"),
            UTC_DAY,
            UTC_DAY + timedelta(days=1),
            True,
            "timelapse_玄関_📷_2026_07_11_2026_07_12_120x_6bf6f341d9a3.mp4",
        ),
        (
            Camera("camera-2", "Front Door"),
            UTC_DAY,
            UTC_DAY + timedelta(days=1),
            True,
            "timelapse_Front_Door_2026_07_11_2026_07_12_120x_381fff0067f8.mp4",
        ),
    ],
)
def test_md1_filename_vectors(camera, start, end, full_day, expected):
    assert ExportRequest(camera, start, end, "120x", full_day=full_day).filename == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("  ..<a>:b?  ", "a_b"), ("...", "camera"), ("x" * 60, "x" * 48), ("a" * 47 + "-" * 5 + "b", "a" * 47)],
)
def test_md1_unsafe_names_are_cleaned(name, expected):
    request = ExportRequest(Camera("c", name), UTC_DAY, UTC_DAY + timedelta(days=1), "1x", full_day=True)
    assert request.filename.startswith(f"timelapse_{expected}_2026_07_11")


def test_md2_day_bounds_follow_daylight_saving():
    tz = ZoneInfo("America/New_York")
    start, end = day_bounds(date(2026, 3, 8), tz)
    assert start.isoformat() == "2026-03-08T00:00:00-05:00"
    assert end.isoformat() == "2026-03-09T00:00:00-04:00"
    assert end.timestamp() - start.timestamp() == 23 * 3600
    start, end = day_bounds(date(2026, 11, 1), tz)
    assert end.timestamp() - start.timestamp() == 25 * 3600


def test_md3_latest_complete_day_is_yesterday():
    assert latest_complete_day(datetime(2026, 7, 13, 15, tzinfo=UTC), ZoneInfo("UTC")) == date(2026, 7, 12)
    tokyo = ZoneInfo("Asia/Tokyo")
    assert latest_complete_day(datetime(2026, 7, 13, 15, tzinfo=UTC), tokyo) == date(2026, 7, 13)


@pytest.mark.parametrize(
    ("n", "text"),
    [(None, "unknown"), (0, "0 bytes"), (1023, "1023 bytes"), (1536, "1.5 KiB"), (5 * 1024**2, "5.0 MiB"),
     (3 * 1024**3, "3.0 GiB"), (2 * 1024**4, "2.0 TiB")],
)  # fmt: skip
def test_md4_format_bytes(n, text):
    assert format_bytes(n) == text

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

from timelapse.config import parse_args

ENVIRONMENT_VARIABLES = (
    "UNIFI_PROTECT_URL",
    "UNIFI_PROTECT_TOKEN",
    "UNIFI_PROTECT_USERNAME",
    "UNIFI_PROTECT_PASSWORD",
    "UNIFI_PROTECT_VERIFY_SSL",
    "TIMELAPSE_OUTPUT",
    "TIMELAPSE_REQUEST_TIMEOUT_SECONDS",
    "TIMELAPSE_MAX_DOWNLOAD_MIB",
)
REQUIRED_ARGUMENTS = [
    "--speed",
    "120x",
    "--start-date",
    "07-11-2026-08-00-00",
    "--end-date",
    "07-11-2026-09-00-00",
]


def test_parse_args_loads_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        """UNIFI_PROTECT_URL=https://protect.local/proxy/protect/integration/v1
UNIFI_PROTECT_TOKEN=test-token
UNIFI_PROTECT_USERNAME=timelapse-user
UNIFI_PROTECT_PASSWORD=test-password
UNIFI_PROTECT_VERIFY_SSL=false
TIMELAPSE_OUTPUT=from-env.mp4
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["timelapse", *REQUIRED_ARGUMENTS])

    config = parse_args()

    assert config.instance_url == "https://protect.local/proxy/protect/integration/v1"
    assert config.token == "test-token"  # noqa: S105
    assert config.username == "timelapse-user"
    assert config.password == "test-password"  # noqa: S105
    assert config.verify_ssl is False
    assert config.speed == "120x"
    assert config.output == Path("from-env.mp4")
    assert config.request_timeout_seconds == 0
    assert config.max_download_mib == 10240


def test_process_environment_wins_over_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TIMELAPSE_OUTPUT=from-dotenv.mp4\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TIMELAPSE_OUTPUT", "from-process-env.mp4")
    monkeypatch.setenv("UNIFI_PROTECT_USERNAME", "timelapse-user")
    monkeypatch.setenv("UNIFI_PROTECT_PASSWORD", "test-password")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "timelapse",
            "--instance",
            "https://protect.local",
            "--token",
            "test-token",
            *REQUIRED_ARGUMENTS,
        ],
    )

    assert parse_args().output == Path("from-process-env.mp4")


@pytest.mark.parametrize(
    ("flag", "environment_name", "environment_value"),
    [
        ("--start-date", "TIMELAPSE_START_DATE", "07-11-2026-08-00-00"),
        ("--end-date", "TIMELAPSE_END_DATE", "07-11-2026-09-00-00"),
    ],
)
def test_dates_are_cli_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
    environment_name: str,
    environment_value: str,
) -> None:
    arguments = ["timelapse", "--instance", "https://protect.local", "--token", "test-token", *REQUIRED_ARGUMENTS]
    flag_index = arguments.index(flag)
    del arguments[flag_index : flag_index + 2]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(environment_name, environment_value)
    monkeypatch.setenv("UNIFI_PROTECT_USERNAME", "timelapse-user")
    monkeypatch.setenv("UNIFI_PROTECT_PASSWORD", "test-password")
    monkeypatch.setattr(sys, "argv", arguments)

    config = parse_args()

    assert config.end - config.start == timedelta(days=1)


@pytest.mark.parametrize("flag", ["--start-date", "--end-date"])
def test_one_date_only_boundary_creates_full_local_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNIFI_PROTECT_USERNAME", "timelapse-user")
    monkeypatch.setenv("UNIFI_PROTECT_PASSWORD", "test-password")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "timelapse",
            "--instance",
            "https://protect.local",
            "--token",
            "test-token",
            flag,
            "07-11-2026",
        ],
    )

    config = parse_args()

    assert config.start.hour == 0
    assert config.start.minute == 0
    assert config.end.date() == config.start.date() + timedelta(days=1)


def test_daily_mode_does_not_require_dates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNIFI_PROTECT_USERNAME", "timelapse-user")
    monkeypatch.setenv("UNIFI_PROTECT_PASSWORD", "test-password")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "timelapse",
            "--instance",
            "https://protect.local",
            "--token",
            "test-token",
            "--daily",
            "--output",
            str(tmp_path),
        ],
    )

    config = parse_args()

    assert config.daily is True
    assert config.output == tmp_path


def test_speed_defaults_to_600x(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNIFI_PROTECT_USERNAME", "timelapse-user")
    monkeypatch.setenv("UNIFI_PROTECT_PASSWORD", "test-password")
    arguments = [
        "timelapse",
        "--instance",
        "https://protect.local",
        "--token",
        "test-token",
        "--start-date",
        "07-11-2026-08-00-00",
        "--end-date",
        "07-11-2026-09-00-00",
    ]
    monkeypatch.setattr(sys, "argv", arguments)

    assert parse_args().speed == "600x"


def test_private_credentials_can_be_passed_on_command_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    arguments = [
        "timelapse",
        "--instance",
        "https://protect.local",
        "--token",
        "test-token",
        "--username",
        "command-line-user",
        "--password",
        "command-line-password",
        *REQUIRED_ARGUMENTS,
    ]
    monkeypatch.setattr(sys, "argv", arguments)

    config = parse_args()

    assert config.username == "command-line-user"
    assert config.password == "command-line-password"  # noqa: S105

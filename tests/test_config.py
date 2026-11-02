from __future__ import annotations

import sys
from pathlib import Path

import pytest

from timelapse.config import Config, CreateProfile, parse_args
from timelapse.profiles import ConnectionProfile

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


def _parse_config() -> Config:
    command = parse_args()
    assert isinstance(command, Config)
    return command


@pytest.fixture(autouse=True)
def _clear_configuration_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_parse_args_loads_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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

    config = _parse_config()

    assert config.instance_url == "https://protect.local/proxy/protect/integration/v1"
    assert config.token == "test-token"  # noqa: S105
    assert config.username == "timelapse-user"
    assert config.password == "test-password"  # noqa: S105
    assert config.verify_ssl is False
    assert config.speed == "120x"
    assert config.output == Path("from-env.mp4")
    assert config.request_timeout_seconds == 0
    assert config.max_download_mib == 10240


def test_command_line_wins_over_environment_and_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "TIMELAPSE_OUTPUT=from-dotenv.mp4\nUNIFI_PROTECT_USERNAME=dotenv-user\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TIMELAPSE_OUTPUT", "from-process-env.mp4")
    monkeypatch.setenv("UNIFI_PROTECT_USERNAME", "environment-user")
    monkeypatch.setenv("UNIFI_PROTECT_PASSWORD", "environment-password")
    monkeypatch.setattr(
        sys,
        "argv",
        [
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
        ],
    )

    config = _parse_config()

    assert config.output == Path("from-process-env.mp4")
    assert config.username == "command-line-user"
    assert config.password == "command-line-password"  # noqa: S105


@pytest.mark.parametrize(
    ("flag", "value", "expected_start", "expected_end"),
    [
        ("--start-date", "07-11-2026", "2026-07-11 00:00", "2026-07-12 00:00"),
        ("--end-date", "07-11-2026", "2026-07-11 00:00", "2026-07-12 00:00"),
        ("--start-date", "07-11-2026-09-00-00", "2026-07-11 09:00", "2026-07-12 09:00"),
        ("--end-date", "07-11-2026-09-00-00", "2026-07-10 09:00", "2026-07-11 09:00"),
    ],
)
def test_one_cli_date_spans_a_day_and_environment_dates_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
    value: str,
    expected_start: str,
    expected_end: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TIMELAPSE_START_DATE", "01-01-2026-08-00-00")
    monkeypatch.setenv("TIMELAPSE_END_DATE", "12-31-2026-09-00-00")
    monkeypatch.setenv("UNIFI_PROTECT_USERNAME", "timelapse-user")
    monkeypatch.setenv("UNIFI_PROTECT_PASSWORD", "test-password")
    monkeypatch.setattr(
        sys,
        "argv",
        ["timelapse", "--instance", "https://protect.local", "--token", "test-token", flag, value],
    )

    config = _parse_config()

    assert config.start.strftime("%Y-%m-%d %H:%M") == expected_start
    assert config.end.strftime("%Y-%m-%d %H:%M") == expected_end
    assert config.full_day is (value == "07-11-2026")


def test_create_profile_uses_dotenv_connection_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        """UNIFI_PROTECT_URL=https://protect.local
UNIFI_PROTECT_TOKEN=test-token
UNIFI_PROTECT_USERNAME=timelapse-user
UNIFI_PROTECT_PASSWORD=test-password
UNIFI_PROTECT_VERIFY_SSL=false
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["timelapse", "--create-profile"])

    command = parse_args()

    assert command == CreateProfile(
        instance_url="https://protect.local",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=False,
    )


def test_named_profile_supplies_connection_details(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "timelapse.profiles.load_profile",
        lambda name: ConnectionProfile(
            name=name,
            instance_url="https://profile.local",
            token="profile-token",  # noqa: S106
            username="profile-user",
            password="profile-password",  # noqa: S106
            verify_ssl=False,
        ),
    )
    monkeypatch.setattr(sys, "argv", ["timelapse", "--profile", "home", *REQUIRED_ARGUMENTS])

    config = _parse_config()

    assert config.instance_url == "https://profile.local"
    assert config.token == "profile-token"  # noqa: S105
    assert config.username == "profile-user"
    assert config.password == "profile-password"  # noqa: S105
    assert config.verify_ssl is False


def test_profile_is_rejected_when_dotenv_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".env").write_text(
        """UNIFI_PROTECT_URL=https://protect.local
UNIFI_PROTECT_TOKEN=test-token
UNIFI_PROTECT_USERNAME=timelapse-user
UNIFI_PROTECT_PASSWORD=test-password
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["timelapse", "--profile", "home", *REQUIRED_ARGUMENTS])

    with pytest.raises(SystemExit) as error:
        parse_args()

    assert error.value.code == 2
    assert "--profile cannot be used while .env exists" in capsys.readouterr().err


def test_incomplete_dotenv_prompts_for_missing_connection_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        """UNIFI_PROTECT_URL=https://protect.local
UNIFI_PROTECT_VERIFY_SSL=true
""",
        encoding="utf-8",
    )
    secret_answers = iter(["prompted-token", "prompted-password"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "prompted-user")
    monkeypatch.setattr("timelapse.config.getpass.getpass", lambda _prompt: next(secret_answers))
    monkeypatch.setattr(sys, "argv", ["timelapse", *REQUIRED_ARGUMENTS])

    config = _parse_config()

    assert config.instance_url == "https://protect.local"
    assert config.token == "prompted-token"  # noqa: S105
    assert config.username == "prompted-user"
    assert config.password == "prompted-password"  # noqa: S105

from __future__ import annotations

import sys
from datetime import timedelta
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

    assert _parse_config().output == Path("from-process-env.mp4")


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

    config = _parse_config()

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

    config = _parse_config()

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

    config = _parse_config()

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

    assert _parse_config().speed == "600x"


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

    config = _parse_config()

    assert config.username == "command-line-user"
    assert config.password == "command-line-password"  # noqa: S105


def test_create_profile_uses_dotenv_connection_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
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
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
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

    assert isinstance(config, Config)
    assert config.instance_url == "https://profile.local"
    assert config.token == "profile-token"  # noqa: S105
    assert config.username == "profile-user"
    assert config.password == "profile-password"  # noqa: S105
    assert config.verify_ssl is False


def test_dotenv_is_used_instead_of_named_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
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

    with pytest.raises(SystemExit):
        parse_args()


def test_incomplete_dotenv_prompts_for_missing_connection_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
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

    assert isinstance(config, Config)
    assert config.instance_url == "https://protect.local"
    assert config.token == "prompted-token"  # noqa: S105
    assert config.username == "prompted-user"
    assert config.password == "prompted-password"  # noqa: S105

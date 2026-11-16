import builtins
import getpass
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import keyring
import pytest
import tzlocal
from keyring.errors import KeyringError

from tests.fakes import CAMERA, MP4, FakeProtect
from timelapse.cli import main
from timelapse.model import Camera, Connection
from timelapse.profiles import SERVICE, ProfileError, load_profile, save_profile
from timelapse.registry import Registry

NY = ZoneInfo("America/New_York")
YARD = Camera("camera-2", "Yard", "DISCONNECTED", "G4")
DOTENV = (
    "UNIFI_PROTECT_URL=https://dotenv.local/\nUNIFI_PROTECT_TOKEN=dotenv-token\n"
    "UNIFI_PROTECT_USERNAME=dotenv-user\nUNIFI_PROTECT_PASSWORD=dotenv-password\n"
)
FLAGS = ["--instance", "https://flag.local", "--token", "flag-token", "--username", "u", "--password", "p"]


class Console:
    """Scripted input, a fake keyring and a recording console factory."""

    def __init__(self, monkeypatch, tmp_path: Path) -> None:
        self.answers: list[str] = []
        self.prompts: list[str] = []
        self.store: dict[tuple[str, str], str] = {}
        self.connections: list[Connection] = []
        self.protect = FakeProtect([CAMERA, YARD])
        self.env = {"TIMELAPSE_CLI_REGISTRY": str(tmp_path / "cli-automations.json")}
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(builtins, "input", self.answer)
        monkeypatch.setattr(getpass, "getpass", self.answer)
        monkeypatch.setattr(keyring, "get_password", lambda service, name: self.store.get((service, name)))
        monkeypatch.setattr(keyring, "set_password", lambda s, n, v: self.store.__setitem__((s, n), v))
        monkeypatch.setattr(tzlocal, "get_localzone", lambda: NY)
        monkeypatch.setattr(tzlocal, "get_localzone_name", lambda: "America/New_York")

    def answer(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def make_protect(self, connection: Connection) -> FakeProtect:
        self.connections.append(connection)
        return self.protect

    def run(self, *argv: str, **env: str) -> int:
        return main(list(argv), env=self.env | env, make_protect=self.make_protect)


@pytest.fixture
def console(monkeypatch, tmp_path):
    return Console(monkeypatch, tmp_path)


def test_cl1_interactive_export(console, capsys, tmp_path):
    console.answers = ["9", "abc", "2"]
    assert console.run(*FLAGS, "--start-date", "07-30-2026", "--speed", "120x") == 0
    out = capsys.readouterr().out
    assert (
        "Available cameras:\n1. Front Door (CONNECTED, G5 Pro, camera-1)\n2. Yard (DISCONNECTED, G4, camera-2)" in out
    )
    assert console.prompts == ["Select a camera by number: "] * 3
    assert "Requesting 120x timelapse export for Yard..." in out
    assert "\rDownloaded 0.0 MiB" in out
    name = "timelapse_Yard_2026_07_30_2026_07_31_120x_381fff0067f8.mp4"
    assert f"Saved timelapse to {tmp_path / name}" in out
    assert (tmp_path / name).read_bytes() == MP4
    assert console.protect.closed


def test_cl1_output_flag_and_env(console, tmp_path):
    console.answers = ["1"]
    assert console.run(*FLAGS, "--start-date", "07-30-2026", TIMELAPSE_OUTPUT=str(tmp_path / "env.mp4")) == 0
    assert (tmp_path / "env.mp4").exists()
    console.answers = ["1"]
    assert console.run(*FLAGS, "--start-date", "07-30-2026", "--output", str(tmp_path / "flag.mp4")) == 0
    assert (tmp_path / "flag.mp4").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--speed", "2x"],
        ["--dotenv", "relative.env"],
        ["--dotenv", "/nonexistent.env"],
        ["--profile", "a", "--create-profile"],
        ["--request-timeout-seconds", "-1"],
        ["--max-download-mib", "lots"],
        ["--verify-ssl", "maybe"],
        ["--start-date", "2026-07-30"],
    ],
)
def test_cl2_usage_errors(console, argv):
    with pytest.raises(SystemExit) as caught:
        console.run(*FLAGS, "--start-date", "07-30-2026", *argv)
    assert caught.value.code == 2


def test_cl2_numeric_options_reach_the_connection(console, tmp_path):
    console.answers = ["1"]
    argv = [*FLAGS, "--start-date", "07-30-2026", "--request-timeout-seconds", "30", "--max-download-mib", "0"]
    assert console.run(*argv, "--verify-ssl", "no") == 0
    connection = console.connections[0]
    assert (connection.timeout_seconds, connection.max_download_mib, connection.verify_ssl) == (30, 0, False)
    console.answers = ["1"]
    (tmp_path / "out.mp4").unlink(missing_ok=True)
    env = {"TIMELAPSE_REQUEST_TIMEOUT_SECONDS": "12", "TIMELAPSE_MAX_DOWNLOAD_MIB": "7"}
    assert console.run(*FLAGS, "--end-date", "07-29-2026", **env) == 0
    assert (console.connections[1].timeout_seconds, console.connections[1].max_download_mib) == (12, 7)


def test_cl3_precedence(console, tmp_path):
    (tmp_path / ".env").write_text(DOTENV + "UNIFI_PROTECT_VERIFY_SSL=false\n")
    save_profile("home", Connection("https://profile.local", "profile-token", "profile-user", "pw", verify_ssl=True))
    console.answers = ["1"]
    env = {"UNIFI_PROTECT_TOKEN": "env-token", "UNIFI_PROTECT_USERNAME": "env-user"}
    argv = ["--start-date", "07-30-2026", "--instance", "https://flag.local", "--profile", "home"]
    assert console.run(*argv, **env) == 0
    assert console.connections[0] == Connection("https://flag.local", "profile-token", "profile-user", "pw", True)
    console.answers = ["1"]
    assert console.run("--start-date", "07-29-2026", **env) == 0
    assert console.connections[1] == Connection(
        "https://dotenv.local", "env-token", "env-user", "dotenv-password", verify_ssl=False
    )


def test_cl3_prompts_for_missing_values(console, capsys):
    console.answers = ["https://typed.local/", "typed-token", "typed-user", "typed-password", "1"]
    assert console.run("--start-date", "07-30-2026") == 0
    assert console.connections[0] == Connection("https://typed.local", "typed-token", "typed-user", "typed-password")
    assert console.prompts[:4] == [
        "UniFi Protect URL: ", "Integration API token: ", "Local Protect username: ", "Local Protect password: "
    ]  # fmt: skip
    console.answers = ["https://typed.local"]
    assert console.run("--start-date", "07-30-2026") == 1
    assert "Integration API token is required; pass --token" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "start", "end", "full_day"),
    [
        (["--start-date", "07-30-2026"], "2026-07-30T00:00:00-04:00", "2026-07-31T00:00:00-04:00", True),
        (["--end-date", "03-08-2026"], "2026-03-08T00:00:00-05:00", "2026-03-09T00:00:00-04:00", True),
        (["--start-date", "07-30-2026-08-00-00"], "2026-07-30T08:00:00-04:00", "2026-07-31T08:00:00-04:00", False),
        (["--end-date", "07-30-2026-08-00-00"], "2026-07-29T08:00:00-04:00", "2026-07-30T08:00:00-04:00", False),
        (["--start-date", "07-30-2026", "--end-date", "07-30-2026"],
         "2026-07-30T00:00:00-04:00", "2026-07-31T00:00:00-04:00", True),
        (["--start-date", "07-30-2026", "--end-date", "07-30-2026-18-30-00"],
         "2026-07-30T00:00:00-04:00", "2026-07-30T18:30:00-04:00", False),
        (["--start-date", "07-30-2026", "--end-date", "07-31-2026"],
         "2026-07-30T00:00:00-04:00", "2026-07-31T00:00:00-04:00", False),
    ],
)  # fmt: skip
def test_cl4_date_rules(console, argv, start, end, full_day):
    console.answers = ["1"]
    assert console.run(*FLAGS, *argv, TIMELAPSE_START_DATE="01-01-2020") == 0
    [request] = console.protect.exports
    assert (request.start.isoformat(), request.end.isoformat(), request.full_day) == (start, end, full_day)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "provide --start-date or --end-date"),
        (["--start-date", "07-30-2026", "--end-date", "07-29-2026"], "--end-date must be after --start-date"),
    ],
)
def test_cl4_date_errors(console, capsys, argv, message):
    with pytest.raises(SystemExit) as caught:
        console.run(*FLAGS, *argv)
    assert caught.value.code == 2
    assert message in capsys.readouterr().err


def test_cl5_create_profile(console, capsys, tmp_path):
    (tmp_path / ".env").write_text(DOTENV)
    console.answers = ["", "  home  ", "n"]
    assert console.run("--create-profile") == 0
    out = capsys.readouterr().out
    assert "Profile name is required." in out
    assert "Created profile 'home'." in out
    assert console.prompts == ["Profile name: ", "Profile name: ", "Verify TLS certificates? [Y/n] "]
    stored = console.store[(SERVICE, "home")]
    assert stored == (
        '{"version":1,"instance_url":"https://dotenv.local","token":"dotenv-token","username":"dotenv-user",'
        '"password":"dotenv-password","verify_ssl":false}'
    )
    console.answers = ["home", ""]
    assert console.run("--create-profile") == 1
    assert "already exists" in capsys.readouterr().err


def test_cl6_profiles(console, monkeypatch):
    save_profile(" spaced ", Connection(" https://p.local/ ", " t ", " u ", " pw ", verify_ssl=False))
    assert load_profile("spaced") == Connection("https://p.local", "t", "u", " pw ", verify_ssl=False)
    with pytest.raises(ProfileError, match="does not exist; create it with --create-profile"):
        load_profile("Spaced")
    for value, message in (('{"version":2}', "unsupported version"), ("not json", "malformed"),
                           ('{"version":1,"instance_url":"","token":"t","username":"u","password":"p",'
                            '"verify_ssl":true}', "missing or empty")):  # fmt: skip
        console.store[(SERVICE, "bad")] = value
        with pytest.raises(ProfileError, match=message):
            load_profile("bad")

    def broken(*_args):
        raise KeyringError("locked")

    monkeypatch.setattr(keyring, "get_password", broken)
    with pytest.raises(ProfileError, match="credential store: locked"):
        load_profile("spaced")
    with pytest.raises(ProfileError, match="credential store: locked"):
        save_profile("other", Connection("https://p", "t", "u", "p"))


def automation_env(tmp_path: Path) -> Path:
    env_file = tmp_path / "home.env"
    env_file.write_text(DOTENV + "TIMELAPSE_REQUEST_TIMEOUT_SECONDS=5\n")
    (tmp_path / "out").mkdir()
    return env_file


def test_cl7_add_list_edit(console, capsys, tmp_path):
    env_file = automation_env(tmp_path)
    base = ["automation", "add", "--name", "Doors", "--output", str(tmp_path / "out"), "--dotenv", str(env_file)]
    assert console.run(*base, "--camera", "front door", "--camera", "camera-2") == 0
    out = capsys.readouterr().out
    assert re.search(r"Created 'Doors' \(auto_[0-9a-f]{32}\)\.", out)
    registry = Registry(tmp_path / "cli-automations.json")
    registry.load()
    [automation] = registry.automations
    assert automation.timezone == "America/New_York"
    assert automation.connection == ("dotenv", str(env_file))
    assert [camera.id for camera in automation.cameras] == ["camera-1", "camera-2"]
    assert console.connections[0].url == "https://dotenv.local"
    assert console.connections[0].timeout_seconds == 5
    assert console.run("automation", "list") == 0
    assert f"{automation.id}  active    Doors  next {automation.next_day}" in capsys.readouterr().out
    assert console.run("automation", "list", "--json") == 0
    [row] = json.loads(capsys.readouterr().out)
    assert set(row) == {"id", "name", "status", "camera_ids", "speed", "output_directory", "timezone", "next_day",
                        "consecutive_failures", "next_retry_at", "last_error", "connection"}  # fmt: skip
    assert row["camera_ids"] == ["camera-1", "camera-2"]
    assert console.run("automation", "edit", "doors", "--name", "Gates", "--camera", "Yard") == 0
    assert f"Updated 'Gates' ({automation.id})." in capsys.readouterr().out
    for camera, message in (("nobody", "camera does not exist: nobody"),):
        assert console.run(*base, "--name", "Other", "--camera", camera) == 1
        assert message in capsys.readouterr().err
    console.protect.camera_list = [CAMERA, Camera("camera-3", "FRONT DOOR")]
    assert console.run(*base, "--name", "Other", "--camera", "Front Door") == 1
    assert "camera name is ambiguous; use its ID: Front Door" in capsys.readouterr().err


def test_cl7_add_requires_timezone(console, capsys, tmp_path, monkeypatch):
    env_file = automation_env(tmp_path)
    monkeypatch.setattr(tzlocal, "get_localzone_name", lambda: None)
    argv = ["automation", "add", "--name", "A", "--camera", "camera-1", "--output", str(tmp_path / "out")]
    assert console.run(*argv, "--dotenv", str(env_file)) == 1
    assert "pass --timezone" in capsys.readouterr().err
    assert console.run(*argv, "--dotenv", str(env_file), "--timezone", "UTC") == 0
    with pytest.raises(SystemExit):
        console.run(*argv)


def test_cl7_state_commands_and_reexport(console, capsys, tmp_path):
    env_file = automation_env(tmp_path)
    argv = ["automation", "add", "--name", "Doors", "--camera", "camera-1", "--output", str(tmp_path / "out")]
    assert console.run(*argv, "--dotenv", str(env_file), "--speed", "120x", "--timezone", "UTC") == 0
    assert console.run("automation", "stop", "doors") == 0
    assert "Stopped 'Doors'." in capsys.readouterr().out
    assert console.run("automation", "re-export", "Doors", "2026-07-01") == 1
    assert "resume the automation before re-exporting a day" in capsys.readouterr().err
    assert console.run("automation", "resume", "DOORS") == 0
    assert "Resumed 'Doors'." in capsys.readouterr().out
    assert console.run("automation", "re-export", "Doors", "2026-07-02") == 0
    assert "Re-export finished." in capsys.readouterr().out
    assert [request.speed for request in console.protect.exports][-1] == "120x"
    assert console.run("automation", "reexport", "Doors", "2026-07-02") == 0
    assert "No export was needed." in capsys.readouterr().out
    assert console.run("automation", "remove", "Doors") == 0
    assert "Removed Daily Automation." in capsys.readouterr().out
    assert console.run("automation", "list") == 0
    assert "No Daily Automations." in capsys.readouterr().out
    assert console.run("automation", "stop", "Doors") == 1
    assert "automation does not exist: Doors" in capsys.readouterr().err


def test_cl7_d8_run_ends_when_nothing_is_active(console, capsys, tmp_path):
    env_file = automation_env(tmp_path)
    argv = ["automation", "add", "--name", "Doors", "--camera", "camera-2", "--output", str(tmp_path / "out")]
    assert console.run(*argv, "--dotenv", str(env_file)) == 0
    console.protect.camera_list = [CAMERA]
    assert console.run("automation", "run", TIMELAPSE_MAX_ACTIVE_EXPORTS="2") == 1
    captured = capsys.readouterr()
    assert "Doors: paused (Camera 'Yard' (camera-2) is unavailable." in captured.out
    assert "Error: no selected Daily Automation is active" in captured.err
    assert console.run("automation", "run", "Doors", TIMELAPSE_MAX_ACTIVE_EXPORTS="0") == 1
    assert "at least 1" in capsys.readouterr().err
    assert console.run("automation", "run", TIMELAPSE_MAX_ACTIVE_EXPORTS="many") == 1
    assert "must be an integer" in capsys.readouterr().err


def test_cl8_registry_owned(console, capsys, tmp_path):
    with Registry(tmp_path / "cli-automations.json").lock():
        assert console.run("automation", "list") == 3
    assert "Stop the running automation process first." in capsys.readouterr().err


def test_cl9_references_resolve_at_run_time(console, capsys, tmp_path):
    env_file = automation_env(tmp_path)
    argv = ["automation", "add", "--name", "Doors", "--camera", "camera-1", "--output", str(tmp_path / "out")]
    assert console.run(*argv, "--dotenv", str(env_file)) == 0
    env_file.write_text("UNIFI_PROTECT_URL=https://x\n")
    assert console.run("automation", "re-export", "Doors", "2026-07-02") == 1
    assert "The re-export did not finish" in capsys.readouterr().err
    registry = Registry(tmp_path / "cli-automations.json")
    registry.load()
    assert registry.automations[0].status == "paused"
    assert f"{env_file} does not define UNIFI_PROTECT_TOKEN" in (registry.automations[0].last_error or "")
    assert console.run("automation", "add", "--name", "P", "--camera", "camera-1", "--output", str(tmp_path / "out"),
                       "--profile", "missing", "--speed", "1x") == 1  # fmt: skip
    assert "profile 'missing' does not exist" in capsys.readouterr().err
    registry.put(registry.automations[0].resumed())
    registry.edit("Doors", connection=("web-environment", "default"))
    assert console.run("automation", "re-export", "Doors", "2026-07-02") == 1
    registry.load()
    assert "only be used by the web server" in (registry.automations[0].last_error or "")


def test_cl10_exit_codes(console, capsys, monkeypatch):
    def interrupt(connection):
        raise KeyboardInterrupt

    assert main([*FLAGS, "--start-date", "07-30-2026"], env={}, make_protect=interrupt) == 130
    assert "Cancelled." in capsys.readouterr().err
    console.protect.camera_list = []
    assert console.run(*FLAGS, "--start-date", "07-30-2026") == 1
    assert capsys.readouterr().err.startswith("Error: ")

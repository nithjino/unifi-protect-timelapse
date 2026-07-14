from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from timelapse import cli
from timelapse.config import CreateProfile
from timelapse.profiles import ConnectionProfile, ProfileError, load_profile, save_profile

if TYPE_CHECKING:
    from collections.abc import Iterator


def _profile(name: str = "home") -> ConnectionProfile:
    return ConnectionProfile(
        name=name,
        instance_url="https://protect.local/proxy/protect/integration/v1/",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=False,
    )


def test_profile_round_trip_uses_credential_store(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        "timelapse.profiles.keyring.get_password",
        lambda service, name: stored.get((service, name)),
    )
    monkeypatch.setattr(
        "timelapse.profiles.keyring.set_password",
        lambda service, name, value: stored.__setitem__((service, name), value),
    )

    save_profile(_profile())
    loaded = load_profile("home")

    assert loaded == ConnectionProfile(
        name="home",
        instance_url="https://protect.local/proxy/protect/integration/v1",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=False,
    )
    payload = json.loads(next(iter(stored.values())))
    assert payload["password"] == "test-password"  # noqa: S105


def test_save_profile_does_not_overwrite_existing_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("timelapse.profiles.keyring.get_password", lambda _service, _name: "existing")

    with pytest.raises(ProfileError, match="already exists"):
        save_profile(_profile())


def test_load_profile_reports_unknown_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("timelapse.profiles.keyring.get_password", lambda _service, _name: None)

    with pytest.raises(ProfileError, match="does not exist"):
        load_profile("missing")


def test_create_profile_requires_explicit_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers: Iterator[str] = iter(["", "home"])
    saved: list[ConnectionProfile] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli, "save_profile", saved.append)
    command = CreateProfile(
        instance_url="https://protect.local",
        token="test-token",  # noqa: S106
        username="timelapse-user",
        password="test-password",  # noqa: S106
        verify_ssl=True,
    )

    cli._create_profile(command)

    assert saved[0].name == "home"
    assert "Profile name is required." in capsys.readouterr().out

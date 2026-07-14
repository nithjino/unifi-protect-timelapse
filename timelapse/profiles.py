"""Secure storage for reusable CLI connection profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import keyring
from keyring.errors import KeyringError

_PROFILE_SERVICE = "io.timelapse.cli.connection-profile"
_PROFILE_VERSION = 1


class ProfileError(RuntimeError):
    """Raised when a CLI connection profile cannot be loaded or saved."""


@dataclass(frozen=True)
class ConnectionProfile:
    """Connection details stored under an explicit profile name."""

    name: str
    instance_url: str
    token: str = field(repr=False)
    username: str
    password: str = field(repr=False)
    verify_ssl: bool

    def normalized(self) -> ConnectionProfile:
        """Return a copy with user-facing identifiers normalized."""
        return ConnectionProfile(
            name=self.name.strip(),
            instance_url=self.instance_url.strip().rstrip("/"),
            token=self.token.strip(),
            username=self.username.strip(),
            password=self.password,
            verify_ssl=self.verify_ssl,
        )

    def to_json(self) -> str:
        """Serialize this profile for the credential store."""
        profile = self.normalized()
        return json.dumps(
            {
                "version": _PROFILE_VERSION,
                "instance_url": profile.instance_url,
                "token": profile.token,
                "username": profile.username,
                "password": profile.password,
                "verify_ssl": profile.verify_ssl,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, name: str, value: str) -> ConnectionProfile:
        """Deserialize and validate a credential-store payload."""
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            message = f"profile {name!r} contains invalid data"
            raise ProfileError(message) from exc
        if not isinstance(payload, dict) or payload.get("version") != _PROFILE_VERSION:
            message = f"profile {name!r} uses an unsupported data format"
            raise ProfileError(message)

        instance_url = payload.get("instance_url")
        token = payload.get("token")
        username = payload.get("username")
        password = payload.get("password")
        verify_ssl = payload.get("verify_ssl")
        if (
            not isinstance(instance_url, str)
            or not isinstance(token, str)
            or not isinstance(username, str)
            or not isinstance(password, str)
            or not isinstance(verify_ssl, bool)
        ):
            message = f"profile {name!r} is missing required connection details"
            raise ProfileError(message)

        profile = cls(name, instance_url, token, username, password, verify_ssl).normalized()
        if not all((profile.name, profile.instance_url, profile.token, profile.username, profile.password)):
            message = f"profile {name!r} contains empty connection details"
            raise ProfileError(message)
        return profile


def load_profile(name: str) -> ConnectionProfile:
    """Load a named profile from the operating system credential store."""
    normalized_name = name.strip()
    if not normalized_name:
        message = "profile name cannot be empty"
        raise ProfileError(message)
    try:
        payload = keyring.get_password(_PROFILE_SERVICE, normalized_name)
    except KeyringError as exc:
        message = f"could not read profile {normalized_name!r} from the operating system credential store: {exc}"
        raise ProfileError(message) from exc
    if payload is None:
        message = f"profile {normalized_name!r} does not exist; create it with --create-profile"
        raise ProfileError(message)
    return ConnectionProfile.from_json(normalized_name, payload)


def save_profile(profile: ConnectionProfile) -> None:
    """Save a new profile without silently replacing an existing profile."""
    normalized = profile.normalized()
    if not normalized.name:
        message = "profile name cannot be empty"
        raise ProfileError(message)
    if not all((normalized.instance_url, normalized.token, normalized.username, normalized.password)):
        message = "all profile connection details are required"
        raise ProfileError(message)

    try:
        if keyring.get_password(_PROFILE_SERVICE, normalized.name) is not None:
            message = f"profile {normalized.name!r} already exists"
            raise ProfileError(message)
        keyring.set_password(_PROFILE_SERVICE, normalized.name, normalized.to_json())
    except KeyringError as exc:
        message = f"could not save profile {normalized.name!r} in the operating system credential store: {exc}"
        raise ProfileError(message) from exc

"""CLI connection profiles kept in the OS credential store."""

from __future__ import annotations

import json

import keyring
from keyring.errors import KeyringError

from timelapse import InvalidRequestError
from timelapse.model import Connection

SERVICE = "io.timelapse.cli.connection-profile"


class ProfileError(InvalidRequestError):
    """A profile is missing, malformed or cannot be stored."""


def save_profile(name: str, connection: Connection) -> str:
    """Store a new profile and return its trimmed name."""
    name = name.strip()
    if not name:
        raise ProfileError("Profile name is required.")
    value = {
        "version": 1,
        "instance_url": connection.url.strip().rstrip("/"),
        "token": connection.token.strip(),
        "username": connection.username.strip(),
        "password": connection.password,
        "verify_ssl": connection.verify_ssl,
    }
    if not all(value[key] for key in ("instance_url", "token", "username", "password")):
        raise ProfileError("every profile value is required")
    try:
        if keyring.get_password(SERVICE, name) is not None:
            raise ProfileError(f"profile '{name}' already exists")
        keyring.set_password(SERVICE, name, json.dumps(value, separators=(",", ":")))
    except KeyringError as error:
        raise ProfileError(f"could not save profile '{name}' in the credential store: {error}") from error
    return name


def load_profile(name: str) -> Connection:
    """Read a profile's connection values."""
    name = name.strip()
    try:
        raw = keyring.get_password(SERVICE, name)
    except KeyringError as error:
        raise ProfileError(f"could not read profile '{name}' from the credential store: {error}") from error
    if raw is None:
        raise ProfileError(f"profile '{name}' does not exist; create it with --create-profile")
    try:
        data = json.loads(raw)
        if data["version"] != 1:
            raise ProfileError(f"profile '{name}' has unsupported version {data['version']!r}")
        fields = [data[key] for key in ("instance_url", "token", "username", "password")]
        if not all(isinstance(field, str) and field.strip() for field in fields):
            raise ProfileError(f"profile '{name}' has a missing or empty value")
        if not isinstance(data["verify_ssl"], bool):
            raise ProfileError(f"profile '{name}' has an invalid verify_ssl value")
    except (ValueError, KeyError, TypeError) as error:
        raise ProfileError(f"profile '{name}' is malformed: {error}") from error
    return Connection(*fields, verify_ssl=data["verify_ssl"])

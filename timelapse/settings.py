"""Read configuration from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timelapse.model import Connection

TRUE = {"true", "1", "yes", "y", "on"}
FALSE = {"false", "0", "no", "n", "off"}
CONNECTION_NAMES = ("UNIFI_PROTECT_URL", "UNIFI_PROTECT_TOKEN", "UNIFI_PROTECT_USERNAME", "UNIFI_PROTECT_PASSWORD")


def env_int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    """Read an integer; unparsable values give the default, small ones the minimum."""
    try:
        value = int(env.get(name, "").strip())
    except ValueError:
        return default
    return max(minimum, value)


def protect_values(env: Mapping[str, str]) -> dict[str, str]:
    """Return the four Protect connection values that are set."""
    values = {name: env.get(name, "").strip() for name in CONNECTION_NAMES}
    values["UNIFI_PROTECT_URL"] = values["UNIFI_PROTECT_URL"].rstrip("/")
    return {name: value for name, value in values.items() if value}


@dataclass(frozen=True)
class Settings:
    """Web server configuration."""

    timezone: ZoneInfo
    data_dir: Path
    output_dir: Path
    host: str = "127.0.0.1"
    port: int = 8000
    trusted_hosts: frozenset[str] = frozenset()
    username: str = "timelapse"
    password: str | None = None
    session_hours: int = 168
    cookie_secure: bool = False
    max_active_exports: int = 4
    max_queued_exports: int = 20
    max_export_hours: int = 168
    storage_quota_mib: int = 102400
    retention_days: int = 0
    connection: Connection | None = None
    missing_connection_values: tuple[str, ...] = ()

    @property
    def connection_ready(self) -> bool:
        """Return whether every Protect value is set."""
        return self.connection is not None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read the web settings from ``env`` (the process environment by default)."""
        env = os.environ if env is None else env
        tz_name = env.get("TZ", "").strip() or "UTC"
        try:
            timezone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"TZ must be a valid IANA timezone name, not {tz_name!r}") from error
        data_dir = Path(env.get("TIMELAPSE_WEB_DATA_DIR", "").strip() or "./data").resolve()
        output = env.get("TIMELAPSE_WEB_OUTPUT_DIR", "").strip()
        values = protect_values(env)
        missing = tuple(name for name in CONNECTION_NAMES if name not in values)
        connection = None
        if not missing:
            connection = Connection(
                values["UNIFI_PROTECT_URL"],
                values["UNIFI_PROTECT_TOKEN"],
                values["UNIFI_PROTECT_USERNAME"],
                values["UNIFI_PROTECT_PASSWORD"],
                verify_ssl=env.get("UNIFI_PROTECT_VERIFY_SSL", "true").strip().lower() in TRUE,
                timeout_seconds=env_int(env, "TIMELAPSE_REQUEST_TIMEOUT_SECONDS", 0),
                max_download_mib=env_int(env, "TIMELAPSE_MAX_DOWNLOAD_MIB", 10240),
            )
        hosts = env.get("TIMELAPSE_WEB_TRUSTED_HOSTS", "")
        return cls(
            timezone=timezone,
            data_dir=data_dir,
            output_dir=Path(output).resolve() if output else data_dir / "exports",
            host=env.get("TIMELAPSE_WEB_HOST", "").strip() or "127.0.0.1",
            port=env_int(env, "TIMELAPSE_WEB_PORT", 8000, 1),
            trusted_hosts=frozenset(host.strip().lower() for host in hosts.split(",") if host.strip()),
            username=env.get("TIMELAPSE_WEB_USERNAME", "").strip() or "timelapse",
            password=env.get("TIMELAPSE_WEB_PASSWORD") or None,
            session_hours=env_int(env, "TIMELAPSE_WEB_SESSION_HOURS", 168, 1),
            cookie_secure=env.get("TIMELAPSE_WEB_COOKIE_SECURE", "").strip().lower() in TRUE,
            max_active_exports=env_int(env, "TIMELAPSE_WEB_MAX_ACTIVE_EXPORTS", 4, 1),
            max_queued_exports=env_int(env, "TIMELAPSE_WEB_MAX_QUEUED_EXPORTS", 20, 0),
            max_export_hours=env_int(env, "TIMELAPSE_WEB_MAX_EXPORT_HOURS", 168, 1),
            storage_quota_mib=env_int(env, "TIMELAPSE_WEB_STORAGE_QUOTA_MIB", 102400, 1),
            retention_days=env_int(env, "TIMELAPSE_WEB_RETENTION_DAYS", 0, 0),
            connection=connection,
            missing_connection_values=missing,
        )

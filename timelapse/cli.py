"""CLI orchestration and interactive camera selection."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from functools import partial
from pathlib import Path

from dotenv import dotenv_values
from platformdirs import user_config_path

from timelapse import TimelapseError
from timelapse.automation_registry import (
    AutomationCamera,
    AutomationRegistry,
    ConnectionReference,
    DailyAutomation,
    RegistryError,
    RegistryOwnedError,
)
from timelapse.config import (
    DEFAULT_MAX_DOWNLOAD_MIB,
    SPEED_TO_FPS,
    Config,
    ConnectionSettings,
    CreateProfile,
    is_truthy,
    parse_args,
    prompt_required,
)
from timelapse.download import MEBIBYTE, DownloadProgress, default_output_path
from timelapse.jobs import ExportJobCoordinator
from timelapse.profiles import ConnectionProfile, ProfileError, load_profile, save_profile
from timelapse.protect import CameraInfo, ProtectClients, camera_name
from timelapse.schedule import DailyAutomationEngine, ResolvedAutomation
from timelapse.service import export_timelapse, list_available_cameras

CLI_REGISTRY_ENV = "TIMELAPSE_CLI_REGISTRY"
CLI_OWNER_EXIT_CODE = 3


def _choose_camera(cameras: list[CameraInfo]) -> CameraInfo:
    if not cameras:
        message = "no cameras were returned by UniFi Protect"
        raise TimelapseError(message)

    _write_stdout("Available cameras:\n")
    for index, camera in enumerate(cameras, start=1):
        details = ", ".join(value for value in (camera.state, camera.model, camera.id) if value)
        _write_stdout(f"{index:>2}. {camera_name(camera)} ({details})\n")

    while True:
        selection = input("Select a camera by number: ").strip()
        try:
            index = int(selection)
        except ValueError:
            _write_stdout("Please enter a camera number.\n")
            continue
        if 1 <= index <= len(cameras):
            return cameras[index - 1]
        _write_stdout(f"Please enter a number from 1 to {len(cameras)}.\n")


async def _run() -> int:
    async with ProtectClients() as clients:
        return await _run_in_session(clients)


async def _run_in_session(clients: ProtectClients) -> int:
    """Run one CLI lifecycle with reusable Protect connections."""
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "automation":
            return await _run_automation_command(clients, sys.argv[2:])
        command = parse_args()
        if isinstance(command, CreateProfile):
            _create_profile(command)
            return 0
        config = command
        camera = _choose_camera(await list_available_cameras(clients, config))
        output = config.output or default_output_path(config, camera)
        await _export(clients, config, camera, output)
    except KeyboardInterrupt:
        _write_stderr("\nCancelled.\n")
        return 130
    except Exception as exc:
        _write_stderr(f"Error: {exc}\n")
        return 1

    _write_stdout(f"Saved timelapse to {output}\n")
    return 0


def _create_profile(command: CreateProfile) -> None:
    _write_stdout("Create a connection profile. Every field is required.\n")
    profile = ConnectionProfile(
        name=prompt_required("Profile name"),
        instance_url=command.instance_url or prompt_required("Protect Integration API URL"),
        token=command.token or prompt_required("Protect API token", secret=True),
        username=command.username or prompt_required("Local Protect username"),
        password=command.password or prompt_required("Local Protect password", secret=True),
        verify_ssl=command.verify_ssl if command.verify_ssl is not None else _prompt_verify_ssl(),
    )
    try:
        save_profile(profile)
    except ProfileError as exc:
        raise TimelapseError(str(exc)) from exc
    _write_stdout(f"Created profile {profile.name.strip()!r}.\n")


def _prompt_verify_ssl() -> bool:
    while True:
        value = input("Verify TLS certificates? [Y/n]: ").strip().lower()
        if value in {"", "y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        _write_stdout("Please enter yes or no.\n")


async def _export(clients: ProtectClients, config: Config, camera: CameraInfo, output: Path) -> None:
    _write_stdout(f"Requesting {config.speed} timelapse export for {camera_name(camera)}...\n")
    try:
        await export_timelapse(clients, config, camera, output, _print_progress)
    finally:
        _write_stdout("\n")


def _automation_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="timelapse automation", description="Manage durable Daily Automations.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    add = subparsers.add_parser("add", help="create a Daily Automation")
    add.add_argument("--name", required=True)
    add.add_argument("--camera", action="append", required=True, help="camera ID or exact display name; repeatable")
    add.add_argument("--speed", choices=tuple(SPEED_TO_FPS), default="600x")
    add.add_argument("--output", type=Path, required=True)
    add.add_argument("--timezone", default=None, help="IANA timezone; defaults to this system's timezone")
    add_connection = add.add_mutually_exclusive_group(required=True)
    add_connection.add_argument("--profile")
    add_connection.add_argument("--dotenv", type=Path)

    edit = subparsers.add_parser("edit", help="edit a Daily Automation")
    edit.add_argument("selector")
    edit.add_argument("--name")
    edit.add_argument("--camera", action="append", help="replace cameras; repeatable")
    edit_connection = edit.add_mutually_exclusive_group()
    edit_connection.add_argument("--profile")
    edit_connection.add_argument("--dotenv", type=Path)

    listing = subparsers.add_parser("list", help="list Daily Automations")
    listing.add_argument("--json", action="store_true")
    run = subparsers.add_parser("run", help="run active Daily Automations")
    run.add_argument("selector", nargs="?")
    for action in ("resume", "stop", "remove"):
        command = subparsers.add_parser(action)
        command.add_argument("selector")
    reexport = subparsers.add_parser("re-export", aliases=["reexport"])
    reexport.add_argument("selector")
    reexport.add_argument("day", type=date.fromisoformat, help="local calendar day in YYYY-MM-DD form")
    return parser


async def _run_automation_command(clients: ProtectClients, arguments: list[str]) -> int:
    """Run one management command; the registry has one owner at a time."""
    parser = _automation_parser()
    args = parser.parse_args(arguments)
    registry = AutomationRegistry(_cli_registry_path())
    try:
        with registry.owner():
            registry.load()
            await _automation_action(clients, parser, args, registry)
    except RegistryOwnedError as exc:
        _write_stderr(f"Error: {exc}. Stop the running automation process first.\n")
        return CLI_OWNER_EXIT_CODE
    except (RegistryError, ProfileError, ValueError) as exc:
        _write_stderr(f"Error: {exc}\n")
        return 1
    return 0


async def _automation_action(
    clients: ProtectClients,
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    registry: AutomationRegistry,
) -> None:
    if args.action == "list":
        _print_automations(registry, as_json=args.json)
    elif args.action == "add":
        reference = _reference_from_args(args.profile, args.dotenv)
        resolved = await _resolve_reference(clients, reference)
        automation = registry.add(
            name=args.name,
            cameras=_automation_cameras(args.camera, resolved.cameras),
            connection=reference,
            speed=args.speed,
            output_directory=args.output,
            timezone=args.timezone or _discover_timezone(parser),
        )
        _write_stdout(f"Created {automation.name!r} ({automation.id}).\n")
    elif args.action == "edit":
        automation = registry.resolve(args.selector)
        reference = automation.connection
        if args.profile is not None or args.dotenv is not None:
            reference = _reference_from_args(args.profile, args.dotenv)
        cameras = None
        if args.camera is not None:
            cameras = _automation_cameras(args.camera, (await _resolve_reference(clients, reference)).cameras)
        updated = registry.edit(args.selector, name=args.name, cameras=cameras, connection=reference)
        _write_stdout(f"Updated {updated.name!r} ({updated.id}).\n")
    elif args.action == "stop":
        _write_stdout(f"Stopped {registry.stop(args.selector).name!r}.\n")
    elif args.action == "resume":
        _write_stdout(f"Resumed {registry.resume(args.selector).name!r}.\n")
    elif args.action == "remove":
        registry.delete(registry.resolve(args.selector).id)
        _write_stdout("Removed Daily Automation.\n")
    else:
        coordinator = ExportJobCoordinator.from_environment()
        engine = _automation_engine(clients, registry, coordinator)
        try:
            if args.action == "run":
                await engine.run_forever(args.selector)
            else:
                exported = await engine.reexport(args.selector, args.day)
                _write_stdout("Re-export finished.\n" if exported else "No export was needed.\n")
        finally:
            await coordinator.close()


def _automation_engine(
    clients: ProtectClients,
    registry: AutomationRegistry,
    coordinator: ExportJobCoordinator,
) -> DailyAutomationEngine:
    return DailyAutomationEngine(
        registry,
        coordinator,
        resolve_connection=partial(_resolve_automation, clients),
        exporter=partial(export_timelapse, clients),
    )


async def _resolve_automation(clients: ProtectClients, automation: DailyAutomation) -> ResolvedAutomation:
    return await _resolve_reference(clients, automation.connection)


async def _resolve_reference(clients: ProtectClients, reference: ConnectionReference) -> ResolvedAutomation:
    if reference.kind == "python-profile":
        profile = load_profile(reference.value)
        config = ConnectionSettings(
            profile.instance_url, profile.token, profile.username, profile.password, profile.verify_ssl
        )
    elif reference.kind == "dotenv":
        config = _connection_from_dotenv(Path(reference.value))
    else:
        message = f"CLI cannot resolve {reference.kind} connection references"
        raise RegistryError(message)
    cameras = tuple(await list_available_cameras(clients, config))
    return ResolvedAutomation(config, cameras)


def _connection_from_dotenv(path: Path) -> ConnectionSettings:
    absolute = path.expanduser()
    if not absolute.is_absolute():
        message = "dotenv connection references must use an absolute path"
        raise RegistryError(message)
    values = dotenv_values(absolute)

    def required(name: str) -> str:
        value = values.get(name)
        if not value:
            message = f"{absolute} does not define {name}"
            raise RegistryError(message)
        return value

    return ConnectionSettings(
        instance_url=required("UNIFI_PROTECT_URL").rstrip("/"),
        token=required("UNIFI_PROTECT_TOKEN"),
        username=required("UNIFI_PROTECT_USERNAME"),
        password=required("UNIFI_PROTECT_PASSWORD"),
        verify_ssl=is_truthy(values.get("UNIFI_PROTECT_VERIFY_SSL") or "true"),
        request_timeout_seconds=int(values.get("TIMELAPSE_REQUEST_TIMEOUT_SECONDS") or 0),
        max_download_mib=int(values.get("TIMELAPSE_MAX_DOWNLOAD_MIB", str(DEFAULT_MAX_DOWNLOAD_MIB)) or 0),
    )


def _reference_from_args(profile: str | None, dotenv: Path | None) -> ConnectionReference:
    if profile is not None:
        return ConnectionReference("python-profile", profile)
    if dotenv is None:
        message = "choose a named profile or an absolute dotenv path"
        raise RegistryError(message)
    return ConnectionReference("dotenv", str(dotenv))


def _automation_cameras(selectors: list[str], cameras: tuple[CameraInfo, ...]) -> tuple[AutomationCamera, ...]:
    selected: list[AutomationCamera] = []
    for selector in selectors:
        match = next((camera for camera in cameras if camera.id == selector), None)
        if match is None:
            name_matches = [camera for camera in cameras if camera.name.casefold() == selector.strip().casefold()]
            if len(name_matches) > 1:
                message = f"camera name is ambiguous; use its ID: {selector}"
                raise ValueError(message)
            match = name_matches[0] if name_matches else None
        if match is None:
            message = f"camera does not exist: {selector}"
            raise ValueError(message)
        if all(existing.id != match.id for existing in selected):
            selected.append(AutomationCamera(match.id, match.name))
    return tuple(selected)


def _print_automations(registry: AutomationRegistry, *, as_json: bool) -> None:
    automations = registry.list()
    if as_json:
        payload = [
            {
                "id": item.id,
                "name": item.name,
                "status": item.status,
                "camera_ids": [camera.id for camera in item.cameras],
                "speed": item.speed,
                "output_directory": item.output_directory,
                "timezone": item.timezone,
                "next_day": item.next_day.isoformat(),
                "consecutive_failures": item.consecutive_failures,
                "next_retry_at": item.next_retry_at.isoformat() if item.next_retry_at else None,
                "last_error": item.last_error,
                "connection": {"kind": item.connection.kind, "value": item.connection.value},
            }
            for item in automations
        ]
        _write_stdout(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return
    if not automations:
        _write_stdout("No Daily Automations.\n")
        return
    for item in automations:
        _write_stdout(f"{item.id}  {item.status:<8}  {item.name}  next {item.next_day.isoformat()}\n")


def _discover_timezone(parser: argparse.ArgumentParser) -> str:
    try:
        return _local_timezone_name()
    except Exception as exc:
        parser.error(f"could not discover an IANA timezone; pass --timezone explicitly: {exc}")


def _cli_registry_path() -> Path:
    configured = os.environ.get(CLI_REGISTRY_ENV)
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return user_config_path("TimeLapse") / "cli-automations.json"


def _local_timezone_name() -> str:
    from tzlocal import get_localzone_name  # noqa: PLC0415

    return get_localzone_name()


def _print_progress(progress: DownloadProgress) -> None:
    downloaded_mib = progress.downloaded_bytes / MEBIBYTE
    if progress.total_bytes:
        percent = min(progress.downloaded_bytes / progress.total_bytes * 100, 100.0)
        _write_stdout(f"\rDownloaded {downloaded_mib:.1f} MiB ({percent:.1f}%)")
    else:
        _write_stdout(f"\rDownloaded {downloaded_mib:.1f} MiB")


def _write_stdout(message: str) -> None:
    sys.stdout.write(message)
    sys.stdout.flush()


def _write_stderr(message: str) -> None:
    sys.stderr.write(message)
    sys.stderr.flush()


def main() -> int:
    """Run the timelapse CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        _write_stderr("\nCancelled.\n")
        return 130

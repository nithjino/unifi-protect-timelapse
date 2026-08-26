"""CLI orchestration and interactive camera selection."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import secrets
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
    RegistryOwner,
    canonical_output_directory,
)
from timelapse.config import Config, ConnectionSettings, CreateProfile, parse_args
from timelapse.download import MEBIBYTE, DownloadProgress, default_output_path
from timelapse.jobs import ExportJobCoordinator
from timelapse.profiles import ConnectionProfile, ProfileError, load_profile, save_profile
from timelapse.protect import CameraInfo, camera_name, protect_session_scope
from timelapse.schedule import (
    DailyAutomationEngine,
    ResolvedAutomation,
)
from timelapse.service import export_timelapse, list_available_cameras

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DAILY_CHECKPOINT_VERSION = 1
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
    async with protect_session_scope():
        return await _run_in_session()


async def _run_in_session() -> int:  # noqa: PLR0911 - command dispatcher returns CLI exit codes
    """Run one CLI lifecycle with reusable Protect connections."""
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "automation":
            return await _run_automation_command(sys.argv[2:])
        command = parse_args()
        if isinstance(command, CreateProfile):
            _create_profile(command)
            return 0
        config = command
        camera = _choose_camera(await list_available_cameras(config))
        if config.daily:
            await _run_daily(config, camera)
            return 0
        output = config.output or default_output_path(config, camera)
        await _export(config, camera, output)
    except KeyboardInterrupt:
        _write_stderr("\nCancelled.\n")
        return 130
    except TimelapseError as exc:
        _write_stderr(f"Error: {exc}\n")
        return 1
    except Exception as exc:
        _write_stderr(f"Error: {exc}\n")
        return 1

    _write_stdout(f"Saved timelapse to {output}\n")
    return 0


def _create_profile(command: CreateProfile) -> None:
    _write_stdout("Create a connection profile. Every field is required.\n")
    profile = ConnectionProfile(
        name=_prompt_required("Profile name"),
        instance_url=command.instance_url or _prompt_required("Protect Integration API URL"),
        token=command.token or _prompt_required("Protect API token", secret=True),
        username=command.username or _prompt_required("Local Protect username"),
        password=command.password or _prompt_required("Local Protect password", secret=True),
        verify_ssl=command.verify_ssl if command.verify_ssl is not None else _prompt_verify_ssl(),
    )
    try:
        save_profile(profile)
    except ProfileError as exc:
        raise TimelapseError(str(exc)) from exc
    _write_stdout(f"Created profile {profile.name.strip()!r}.\n")


def _prompt_required(label: str, *, secret: bool = False) -> str:
    while True:
        value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
        if value.strip():
            return value
        _write_stdout(f"{label} is required.\n")


def _prompt_verify_ssl() -> bool:
    while True:
        value = input("Verify TLS certificates? [Y/n]: ").strip().lower()
        if value in {"", "y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        _write_stdout("Please enter yes or no.\n")


async def _export(config: Config, camera: CameraInfo, output: Path) -> None:
    _write_stdout(f"Requesting {config.speed} timelapse export for {camera_name(camera)}...\n")
    try:
        await export_timelapse(config, camera, output, _print_progress)
    finally:
        _write_stdout("\n")


async def _run_daily(config: Config, camera: CameraInfo) -> None:
    """Import a legacy checkpoint, then run only its matching automation."""
    output_directory = config.output or Path.cwd()
    output_directory = canonical_output_directory(output_directory.resolve())
    checkpoint = _daily_checkpoint_path(output_directory, camera, config.speed)
    registry = AutomationRegistry(_cli_registry_path())
    endpoint = _cli_control_endpoint(registry.path)
    with registry.owner(endpoint=endpoint) as owner:
        registry.load()
        matches = [
            item
            for item in registry.list()
            if item.output_path == output_directory
            and item.speed == config.speed
            and any(selected.id == camera.id for selected in item.cameras)
        ]
        if matches:
            automation = matches[0]
            if checkpoint.exists():
                next_day = _load_daily_checkpoint(checkpoint)
                fingerprint = _checkpoint_fingerprint(checkpoint)
                backup = checkpoint.with_suffix(f"{checkpoint.suffix}.v1-backup")
                if not backup.exists():
                    shutil.copy2(checkpoint, backup)
                if next_day is not None and automation.source_fingerprint != fingerprint:
                    automation = registry.import_checkpoint(
                        automation.id,
                        next_day=next_day,
                        source_fingerprint=fingerprint,
                        imported_at=datetime.now(UTC),
                    )
                checkpoint.unlink()
            if automation.status == "stopped":
                automation = registry.resume(automation.id)
        else:
            next_day = _load_daily_checkpoint(checkpoint)
            reference = _reference_from_config(config)
            fingerprint = _checkpoint_fingerprint(checkpoint) if checkpoint.exists() else None
            backup = checkpoint.with_suffix(f"{checkpoint.suffix}.v1-backup")
            if checkpoint.exists() and not backup.exists():
                shutil.copy2(checkpoint, backup)
            automation = registry.add(
                name=f"{camera.name} daily",
                cameras=(AutomationCamera(camera.id, camera.name),),
                connection=reference,
                speed=config.speed,
                output_directory=output_directory,
                timezone=_local_timezone_name(),
                next_day=next_day,
                source_fingerprint=fingerprint,
                imported_at=datetime.now(UTC) if checkpoint.exists() else None,
            )
            if checkpoint.exists():
                checkpoint.unlink()
        coordinator = ExportJobCoordinator.from_environment()
        engine = _automation_engine(registry, coordinator)
        _write_stdout(f"Running Daily Automation {automation.name!r} ({automation.id}).\n")
        try:
            async with _serve_cli_control(registry, engine, owner):
                await engine.run_forever(automation.id)
        finally:
            await coordinator.close()


def _automation_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="timelapse automation", description="Manage durable Daily Automations.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    add = subparsers.add_parser("add", help="create a Daily Automation")
    add.add_argument("--name", required=True)
    add.add_argument("--camera", action="append", required=True, help="camera ID or exact display name; repeatable")
    add.add_argument("--speed", choices=("1x", "60x", "120x", "300x", "600x"), default="600x")
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


async def _run_automation_command(  # noqa: PLR0911, PLR0915 - CLI subcommand transaction dispatcher
    arguments: list[str],
) -> int:
    parser = _automation_parser()
    args = parser.parse_args(arguments)
    registry = AutomationRegistry(_cli_registry_path())
    endpoint = _cli_control_endpoint(registry.path)
    try:
        with registry.owner(endpoint=endpoint) as owner:
            registry.load()
            if args.action == "list":
                _print_automations(registry, as_json=args.json)
                return 0
            if args.action == "add":
                reference = _reference_from_args(args.profile, args.dotenv)
                resolved = await _resolve_reference(reference)
                cameras = _resolve_camera_selectors(args.camera, resolved.cameras)
                timezone = args.timezone or _discover_timezone(parser)
                automation = registry.add(
                    name=args.name,
                    cameras=tuple(AutomationCamera(item.id, item.name) for item in cameras),
                    connection=reference,
                    speed=args.speed,
                    output_directory=args.output,
                    timezone=timezone,
                )
                _write_stdout(f"Created {automation.name!r} ({automation.id}).\n")
                return 0
            if args.action == "edit":
                automation = registry.resolve(args.selector)
                reference = (
                    _reference_from_args(args.profile, args.dotenv)
                    if args.profile is not None or args.dotenv is not None
                    else automation.connection
                )
                cameras = None
                if args.camera is not None:
                    resolved = await _resolve_reference(reference)
                    selected = _resolve_camera_selectors(args.camera, resolved.cameras)
                    cameras = tuple(AutomationCamera(item.id, item.name) for item in selected)
                updated = registry.edit(args.selector, name=args.name, cameras=cameras, connection=reference)
                _write_stdout(f"Updated {updated.name!r} ({updated.id}).\n")
                return 0
            if args.action == "stop":
                updated = registry.stop(args.selector)
                _write_stdout(f"Stopped {updated.name!r}.\n")
                return 0
            if args.action == "resume":
                updated = registry.resume(args.selector)
                _write_stdout(f"Resumed {updated.name!r}.\n")
                return 0
            if args.action == "remove":
                updated = registry.begin_remove(args.selector)
                message = (
                    "Removal is waiting for submitted jobs." if updated is not None else "Removed Daily Automation."
                )
                _write_stdout(f"{message}\n")
                return 0
            coordinator = ExportJobCoordinator.from_environment()
            engine = _automation_engine(registry, coordinator)
            try:
                if args.action in {"re-export", "reexport"}:
                    result = await engine.reexport(args.selector, args.day)
                    _write_stdout("No export was needed.\n" if result is None else "Re-export finished.\n")
                    return 0
                async with _serve_cli_control(registry, engine, owner):
                    await engine.run_forever(args.selector)
            finally:
                await coordinator.close()
    except RegistryOwnedError as exc:
        if args.action in {"list", "stop", "resume", "remove"}:
            return await _route_to_cli_owner(registry, args)
        _write_stderr(f"Error: {exc}\n")
        return CLI_OWNER_EXIT_CODE
    except (RegistryError, ProfileError, ValueError) as exc:
        _write_stderr(f"Error: {exc}\n")
        return 1
    return 0


def _automation_engine(registry: AutomationRegistry, coordinator: ExportJobCoordinator) -> DailyAutomationEngine:
    return DailyAutomationEngine(
        registry,
        coordinator,
        resolve_connection=_resolve_automation,
        exporter=export_timelapse,
    )


async def _resolve_automation(automation: DailyAutomation) -> ResolvedAutomation:
    return await _resolve_reference(automation.connection)


async def _resolve_reference(reference: ConnectionReference) -> ResolvedAutomation:
    if reference.kind == "python-profile":
        profile = load_profile(reference.value)
        config = _connection_from_profile(profile)
    elif reference.kind == "dotenv":
        config = _connection_from_dotenv(Path(reference.value))
    else:
        message = f"CLI cannot resolve {reference.kind} connection references"
        raise RegistryError(message)
    cameras = tuple(await list_available_cameras(config))
    return ResolvedAutomation(config, cameras)


def _connection_from_profile(profile: ConnectionProfile) -> ConnectionSettings:
    return ConnectionSettings(
        instance_url=profile.instance_url,
        token=profile.token,
        username=profile.username,
        password=profile.password,
        verify_ssl=profile.verify_ssl,
        request_timeout_seconds=0,
        max_download_mib=10 * 1024,
        connection_kind="python-profile",
        connection_value=profile.name,
    )


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

    verify_ssl = str(values.get("UNIFI_PROTECT_VERIFY_SSL", "true")).strip().casefold() in {"1", "true", "yes", "on"}
    return ConnectionSettings(
        instance_url=required("UNIFI_PROTECT_URL").rstrip("/"),
        token=required("UNIFI_PROTECT_TOKEN"),
        username=required("UNIFI_PROTECT_USERNAME"),
        password=required("UNIFI_PROTECT_PASSWORD"),
        verify_ssl=verify_ssl,
        request_timeout_seconds=int(values.get("TIMELAPSE_REQUEST_TIMEOUT_SECONDS", "0") or 0),
        max_download_mib=int(values.get("TIMELAPSE_MAX_DOWNLOAD_MIB", str(10 * 1024)) or 0),
        connection_kind="dotenv",
        connection_value=str(absolute.resolve(strict=False)),
    )


def _reference_from_args(profile: str | None, dotenv: Path | None) -> ConnectionReference:
    if profile is not None:
        return ConnectionReference("python-profile", profile)
    if dotenv is None:
        message = "choose a named profile or an absolute dotenv path"
        raise RegistryError(message)
    return ConnectionReference("dotenv", str(dotenv))


def _reference_from_config(config: Config) -> ConnectionReference:
    if config.connection_kind in {"python-profile", "dotenv"} and config.connection_value is not None:
        return ConnectionReference(config.connection_kind, config.connection_value)  # type: ignore[arg-type]
    message = "Daily Automations require a named profile or absolute dotenv path"
    raise RegistryError(message)


def _resolve_camera_selectors(selectors: list[str], cameras: tuple[CameraInfo, ...]) -> tuple[CameraInfo, ...]:
    selected: list[CameraInfo] = []
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
            selected.append(match)
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


def _cli_control_endpoint(registry_path: Path) -> str:
    digest = hashlib.sha256(str(registry_path).encode()).hexdigest()[:16]
    if os.name == "nt":
        return rf"\\.\pipe\timelapse-{digest}"
    user_id = os.getuid() if hasattr(os, "getuid") else 0
    return str(Path(tempfile.gettempdir()) / f"timelapse-{user_id}-{digest}.sock")


def _checkpoint_fingerprint(checkpoint: Path) -> str:
    return hashlib.sha256(checkpoint.read_bytes()).hexdigest()


def _local_timezone_name() -> str:
    from tzlocal import get_localzone_name  # noqa: PLC0415

    return get_localzone_name()


@asynccontextmanager
async def _serve_cli_control(  # noqa: PLR0915 - transport setup and lifetime cleanup stay paired
    registry: AutomationRegistry,
    engine: DailyAutomationEngine,
    owner: RegistryOwner,
) -> AsyncIterator[None]:
    """Serve authenticated management commands while the CLI owns its registry."""
    endpoint_value = owner.endpoint
    nonce = owner.nonce
    if not isinstance(endpoint_value, str) or not isinstance(nonce, str):
        yield
        return
    if os.name == "nt":
        from multiprocessing.connection import Listener  # noqa: PLC0415

        listener = await asyncio.to_thread(Listener, endpoint_value, family="AF_PIPE")
        loop = asyncio.get_running_loop()

        def serve_pipe() -> None:
            while True:
                try:
                    connection = listener.accept()
                except (OSError, EOFError):
                    return
                try:
                    request = connection.recv()
                    future = asyncio.run_coroutine_threadsafe(
                        _dispatch_cli_control(request, registry, engine, nonce),
                        loop,
                    )
                    connection.send(future.result())
                except Exception as exc:
                    with suppress(OSError, EOFError):
                        connection.send({"ok": False, "error": str(exc) or type(exc).__name__})
                finally:
                    connection.close()

        pipe_task = asyncio.create_task(asyncio.to_thread(serve_pipe), name="cli-control-pipe")
        try:
            yield
        finally:
            listener.close()
            pipe_task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(pipe_task, timeout=1)
        return
    endpoint = Path(endpoint_value)
    await asyncio.to_thread(endpoint.unlink, missing_ok=True)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            request = json.loads(raw)
            response = await _dispatch_cli_control(request, registry, engine, nonce)
        except Exception as exc:
            response = {"ok": False, "error": str(exc) or type(exc).__name__}
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=endpoint)
    await asyncio.to_thread(endpoint.chmod, 0o600)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.to_thread(endpoint.unlink, missing_ok=True)


async def _dispatch_cli_control(
    request: object,
    registry: AutomationRegistry,
    engine: DailyAutomationEngine,
    nonce: str,
) -> dict[str, object]:
    if not isinstance(request, dict) or not secrets.compare_digest(str(request.get("nonce", "")), nonce):
        message = "control session authentication failed"
        raise RegistryError(message)
    action = request.get("action")
    selector = request.get("selector")
    if action == "list":
        return {"ok": True, "automations": _automation_json(registry)}
    if action == "stop" and isinstance(selector, str):
        automation = await engine.stop(selector)
        return {"ok": True, "message": f"Stopped {automation.name!r}."}
    if action == "resume" and isinstance(selector, str):
        automation = await engine.resume(selector)
        return {"ok": True, "message": f"Resumed {automation.name!r}."}
    if action == "remove" and isinstance(selector, str):
        await engine.remove(selector)
        return {"ok": True, "message": "Removed Daily Automation."}
    message = f"unsupported control action: {action}"
    raise RegistryError(message)


async def _route_to_cli_owner(registry: AutomationRegistry, args: argparse.Namespace) -> int:
    """Send an offline-style management command to the running CLI owner."""
    lock_path = registry.path.with_suffix(f"{registry.path.suffix}.lock")
    try:
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
        endpoint = metadata["endpoint"]
        nonce = metadata["nonce"]
        if not isinstance(endpoint, str) or not isinstance(nonce, str):
            raise TypeError  # noqa: TRY301 - malformed owner metadata follows the connection error path
        request = {
            "nonce": nonce,
            "action": args.action,
            "selector": getattr(args, "selector", None),
        }
        if os.name == "nt":
            from multiprocessing.connection import Client  # noqa: PLC0415

            connection = await asyncio.to_thread(Client, endpoint, family="AF_PIPE")
            try:
                await asyncio.to_thread(connection.send, request)
                response = await asyncio.to_thread(connection.recv)
            finally:
                connection.close()
        else:
            reader, writer = await asyncio.open_unix_connection(endpoint)
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        _write_stderr(f"Error: another process owns the registry and its control channel is unavailable: {exc}\n")
        return CLI_OWNER_EXIT_CODE
    if not isinstance(response, dict) or response.get("ok") is not True:
        _write_stderr(f"Error: {response.get('error', 'management command failed')}\n")
        return 1
    if args.action == "list":
        automations = response.get("automations", [])
        if args.json:
            _write_stdout(json.dumps(automations, ensure_ascii=False, sort_keys=True) + "\n")
        else:
            for item in automations if isinstance(automations, list) else []:
                if isinstance(item, dict):
                    status = item.get("status")
                    _write_stdout(f"{item.get('id')}  {status!s:<8}  {item.get('name')}  next {item.get('next_day')}\n")
        return 0
    _write_stdout(f"{response.get('message')}\n")
    return 0


def _automation_json(registry: AutomationRegistry) -> list[dict[str, object]]:
    return [
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
        for item in registry.list()
    ]


def _daily_checkpoint_path(output_directory: Path, camera: CameraInfo, speed: str) -> Path:
    identity = f"{camera.id}\0{speed}".encode()
    digest = hashlib.sha256(identity).hexdigest()[:12]
    return output_directory / f".timelapse-daily-{digest}.json"


def _load_daily_checkpoint(checkpoint: Path) -> date | None:
    if not checkpoint.exists():
        return None
    try:
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != DAILY_CHECKPOINT_VERSION:
            raise ValueError  # noqa: TRY301 - malformed checkpoint follows the single recovery path below
        next_day = payload.get("next_day")
        if not isinstance(next_day, str):
            raise TypeError  # noqa: TRY301 - malformed checkpoint follows the single recovery path below
        return date.fromisoformat(next_day)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        message = f"could not load daily checkpoint {checkpoint}: {exc}"
        raise TimelapseError(message) from exc


def _save_daily_checkpoint(checkpoint: Path, day: date) -> None:
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint.with_name(f".{checkpoint.name}.{secrets.token_hex(6)}.tmp")
    payload = {"version": DAILY_CHECKPOINT_VERSION, "next_day": day.isoformat()}
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(checkpoint)
    except OSError as exc:
        message = f"could not persist daily checkpoint {checkpoint}: {exc}"
        raise TimelapseError(message) from exc
    finally:
        temporary.unlink(missing_ok=True)


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

"""The ``timelapse`` command: one-off exports, profiles and Daily Automations."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import tzlocal
from dotenv import dotenv_values
from platformdirs import user_config_path

from timelapse import InvalidRequestError, TimelapseError
from timelapse.daily import DailyRunner
from timelapse.download import Progress
from timelapse.jobs import JobQueue
from timelapse.model import DEFAULT_SPEED, SPEEDS, Camera, Connection, ExportRequest, day_bounds
from timelapse.profiles import load_profile, save_profile
from timelapse.protect import Protect
from timelapse.registry import Automation, Registry, RegistryOwnedError
from timelapse.settings import CONNECTION_NAMES, FALSE, TRUE, protect_values

MakeProtect = Callable[[Connection], Any]
PROMPTS = {
    "UNIFI_PROTECT_URL": ("instance", "UniFi Protect URL"),
    "UNIFI_PROTECT_TOKEN": ("token", "Integration API token"),
    "UNIFI_PROTECT_USERNAME": ("username", "Local Protect username"),
    "UNIFI_PROTECT_PASSWORD": ("password", "Local Protect password"),
}


def parse_bool(value: str, name: str = "--verify-ssl") -> bool:
    """Read a yes/no value strictly."""
    text = value.strip().lower()
    if text in TRUE | FALSE:
        return text in TRUE
    raise InvalidRequestError(f"{name} must be true or false, not {value!r}")


def _bool_arg(value: str) -> bool:
    try:
        return parse_bool(value)
    except InvalidRequestError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _count(value: str) -> int:
    if not value.strip().isdigit():
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, not {value!r}")
    return int(value)


def _absolute_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise argparse.ArgumentTypeError(f"must be an absolute path to an existing file: {value}")
    return path


def _moment(value: str) -> tuple[datetime, bool]:
    """Read ``MM-DD-YYYY`` or ``MM-DD-YYYY-HH-MM-SS`` in the system timezone; also say whether a time was given."""
    for pattern, has_time in (("%m-%d-%Y", False), ("%m-%d-%Y-%H-%M-%S", True)):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=tzlocal.get_localzone()), has_time
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"expected MM-DD-YYYY or MM-DD-YYYY-HH-MM-SS, not {value!r}")


def export_range(
    start: tuple[datetime, bool] | None, end: tuple[datetime, bool] | None, error: Callable[[str], Any]
) -> tuple[datetime, datetime, bool]:
    """Turn the date flags into a range and whether it is one full local day."""
    day = timedelta(hours=24)
    if start is None and end is None:
        error("provide --start-date or --end-date")
    if start is not None and end is not None:
        if not start[1] and not end[1] and start[0] == end[0]:
            return (*day_bounds(start[0].date(), tzlocal.get_localzone()), True)
        if end[0] <= start[0]:
            error("--end-date must be after --start-date")
        return start[0], end[0], False
    moment, has_time = start or end  # type: ignore[misc]
    if not has_time:
        return (*day_bounds(moment.date(), tzlocal.get_localzone()), True)
    if start is not None:
        return moment, (moment.astimezone(UTC) + day).astimezone(moment.tzinfo), False
    return (moment.astimezone(UTC) - day).astimezone(moment.tzinfo), moment, False


@dataclass
class Context:
    """What every command needs: arguments, environment, input and the console factory."""

    args: argparse.Namespace
    env: Mapping[str, str]
    dotenv: Mapping[str, str]
    make_protect: MakeProtect

    def setting(self, name: str) -> str | None:
        """Process environment first, then the ``.env`` file."""
        return self.env.get(name) or self.dotenv.get(name) or None

    def env_count(self, name: str, default: int, minimum: int = 0) -> int:
        """Read a non-negative integer setting."""
        text = self.setting(name)
        if text is None:
            return default
        if not text.strip().lstrip("-").isdigit():
            raise InvalidRequestError(f"{name} must be an integer, not {text!r}")
        if int(text) < minimum:
            raise InvalidRequestError(f"{name} must be at least {minimum}")
        return int(text)

    def prompt(self, label: str, flag: str, *, secret: bool = False) -> str:
        """Ask until a value is given; end of input is an error."""
        while True:
            try:
                value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
            except EOFError:
                raise InvalidRequestError(f"{label} is required; pass {flag}") from None
            if value.strip():
                return value if secret else value.strip()

    def connection(self, *, ask_tls: bool = False) -> Connection:
        """Resolve each value from the flag, the profile, the environment, the ``.env`` file, then a prompt."""
        args = self.args
        profile = load_profile(args.profile) if getattr(args, "profile", None) else None
        from_profile = dict(zip(CONNECTION_NAMES, (profile.url, profile.token, profile.username, profile.password),
                                strict=True)) if profile else {}  # fmt: skip
        values = []
        for name in CONNECTION_NAMES:
            attr, label = PROMPTS[name]
            secret = attr in {"token", "password"}
            value = getattr(args, attr, None) or from_profile.get(name) or self.setting(name)
            values.append(value or self.prompt(label, f"--{attr}", secret=secret))
        verify = getattr(args, "verify_ssl", None)
        if verify is None and profile is not None:
            verify = profile.verify_ssl
        if verify is None and (text := self.setting("UNIFI_PROTECT_VERIFY_SSL")) is not None:
            verify = parse_bool(text, "UNIFI_PROTECT_VERIFY_SSL")
        if verify is None and ask_tls:
            answer = self.prompt_optional("Verify TLS certificates? [Y/n] ")
            verify = parse_bool(answer or "yes", "the TLS answer")
        timeout = getattr(args, "request_timeout_seconds", None)
        limit = getattr(args, "max_download_mib", None)
        return Connection(
            values[0].strip().rstrip("/"),
            values[1],
            values[2],
            values[3],
            verify_ssl=True if verify is None else verify,
            timeout_seconds=self.env_count("TIMELAPSE_REQUEST_TIMEOUT_SECONDS", 0) if timeout is None else timeout,
            max_download_mib=self.env_count("TIMELAPSE_MAX_DOWNLOAD_MIB", 10240) if limit is None else limit,
        )

    @staticmethod
    def prompt_optional(text: str) -> str:
        """Ask once; end of input means the default."""
        try:
            return input(text).strip()
        except EOFError:
            return ""

    def registry(self) -> Registry:
        """Return the CLI registry."""
        override = self.setting("TIMELAPSE_CLI_REGISTRY")
        return Registry(Path(override) if override else user_config_path("TimeLapse") / "cli-automations.json")

    def resolve_reference(self, ref: tuple[str, str]) -> Connection:
        """Read an automation's credentials now, never from the registry."""
        kind, value = ref
        if kind == "python-profile":
            return load_profile(value)
        if kind == "dotenv":
            values = {key: text for key, text in dotenv_values(value).items() if text is not None}
            found = protect_values(values)
            missing = [name for name in CONNECTION_NAMES if name not in found]
            if missing:
                raise InvalidRequestError(f"{value} does not define {missing[0]}")
            reader = Context(self.args, {}, values, self.make_protect)
            verify = reader.setting("UNIFI_PROTECT_VERIFY_SSL")
            return Connection(
                *(found[name] for name in CONNECTION_NAMES),
                verify_ssl=True if verify is None else parse_bool(verify, "UNIFI_PROTECT_VERIFY_SSL"),
                timeout_seconds=reader.env_count("TIMELAPSE_REQUEST_TIMEOUT_SECONDS", 0),
                max_download_mib=reader.env_count("TIMELAPSE_MAX_DOWNLOAD_MIB", 10240),
            )
        raise InvalidRequestError("web-environment references can only be used by the web server")


async def export_once(context: Context, start: datetime, end: datetime, full_day: bool) -> None:  # noqa: FBT001
    """List cameras, ask for one and export it."""
    args = context.args
    protect = context.make_protect(context.connection())
    try:
        cameras: list[Camera] = await protect.cameras()
        if not cameras:
            raise TimelapseError("no cameras are available to this Integration API token")
        print("Available cameras:")
        for number, camera in enumerate(cameras, 1):
            print(f"{number}. {camera.name} ({camera.state}, {camera.model}, {camera.id})")
        while True:
            try:
                choice = input("Select a camera by number: ").strip()
            except EOFError:
                raise InvalidRequestError("a camera must be selected") from None
            if choice.isdigit() and 1 <= int(choice) <= len(cameras):
                break
        request = ExportRequest(cameras[int(choice) - 1], start, end, args.speed, full_day)
        output = Path(args.output) if args.output else Path.cwd() / request.filename

        def show(progress: Progress) -> None:
            text = f"\rDownloaded {progress.downloaded_bytes / 1024 / 1024:.1f} MiB"
            if progress.total_bytes:
                text += f" ({progress.downloaded_bytes * 100 / progress.total_bytes:.1f}%)"
            print(text, end="", flush=True)

        print(f"Requesting {args.speed} timelapse export for {request.camera.name}...")
        await protect.export(request, output, show)
        print(f"\nSaved timelapse to {output}")
    finally:
        await protect.close()


def create_profile(context: Context) -> None:
    """Ask for a name and store the resolved connection under it."""
    while True:
        try:
            name = input("Profile name: ").strip()
        except EOFError:
            raise InvalidRequestError("Profile name is required.") from None
        if name:
            break
        print("Profile name is required.")
    context.args.profile = None
    saved = save_profile(name, context.connection(ask_tls=True))
    print(f"Created profile '{saved}'.")


def reference(args: argparse.Namespace) -> tuple[str, str] | None:
    """Return the connection reference named by ``--profile`` or ``--dotenv``."""
    if args.profile:
        return ("python-profile", args.profile.strip())
    if args.dotenv:
        return ("dotenv", str(args.dotenv.resolve()))
    return None


async def pick_cameras(context: Context, ref: tuple[str, str], wanted: list[str]) -> list[Camera]:
    """Match each ``--camera`` by ID, then by exact case-insensitive name."""
    protect = context.make_protect(context.resolve_reference(ref))
    try:
        cameras: list[Camera] = await protect.cameras()
    finally:
        await protect.close()
    chosen = []
    for item in wanted:
        match = [camera for camera in cameras if camera.id == item] or [
            camera for camera in cameras if camera.name.casefold() == item.strip().casefold()
        ]
        if len(match) > 1:
            raise InvalidRequestError(f"camera name is ambiguous; use its ID: {item}")
        if not match:
            raise InvalidRequestError(f"camera does not exist: {item}")
        chosen.append(Camera(match[0].id, match[0].name))
    return chosen


async def run_automations(context: Context, registry: Registry) -> None:
    """Run the selected automations until none is active."""
    max_active = context.env_count("TIMELAPSE_MAX_ACTIVE_EXPORTS", 4, 1)
    selected = [registry.resolve(context.args.selector)] if context.args.selector else registry.automations
    clients: dict[Connection, Any] = {}

    def connect(automation: Automation) -> Any:
        connection = context.resolve_reference(automation.connection)
        if connection not in clients:
            clients[connection] = context.make_protect(connection)
        return clients[connection]

    queue = JobQueue(max_active=max_active)
    runner = DailyRunner(registry, queue, connect=connect)
    try:
        for automation in selected:
            runner.start(automation.id)
        await runner.wait()
    finally:
        await runner.close()
        await queue.close()
        for client in clients.values():
            await client.close()
    for automation in selected:
        current = registry.get(automation.id) or automation
        print(f"{current.name}: {current.status}" + (f" ({current.last_error})" if current.last_error else ""))
    raise TimelapseError("no selected Daily Automation is active")


def automation_command(context: Context) -> None:
    """Run one ``timelapse automation`` command while holding the registry."""
    args = context.args
    registry = context.registry()
    with registry.lock():
        registry.load()
        command = args.automation_command
        if command == "add":
            ref = reference(args) or ("dotenv", "")
            timezone = args.timezone or tzlocal.get_localzone_name()
            if not timezone:
                raise InvalidRequestError("could not determine the system timezone; pass --timezone")
            cameras = asyncio.run(pick_cameras(context, ref, args.camera))
            automation = registry.add(
                name=args.name, cameras=cameras, connection=ref, speed=args.speed,
                output_directory=Path(args.output), timezone=timezone,
            )  # fmt: skip
            print(f"Created '{automation.name}' ({automation.id}).")
        elif command == "edit":
            current = registry.resolve(args.selector)
            ref = reference(args) or current.connection
            cameras = asyncio.run(pick_cameras(context, ref, args.camera)) if args.camera else None
            automation = registry.edit(current.id, name=args.name, cameras=cameras, connection=ref)
            print(f"Updated '{automation.name}' ({automation.id}).")
        elif command == "list":
            list_automations(registry.automations, as_json=args.json)
        elif command == "run":
            asyncio.run(run_automations(context, registry))
        elif command == "stop":
            print(f"Stopped '{registry.put(registry.resolve(args.selector).stopped()).name}'.")
        elif command == "resume":
            print(f"Resumed '{registry.put(registry.resolve(args.selector).resumed()).name}'.")
        elif command == "remove":
            registry.delete(registry.resolve(args.selector).id)
            print("Removed Daily Automation.")
        else:
            automation = registry.resolve(args.selector)
            runner = DailyRunner(
                registry,
                JobQueue(max_active=context.env_count("TIMELAPSE_MAX_ACTIVE_EXPORTS", 4, 1)),
                connect=lambda item: context.make_protect(context.resolve_reference(item.connection)),
            )
            finished = asyncio.run(runner.reexport(automation.id, args.day))
            print("Re-export finished." if finished else "No export was needed.")


def list_automations(automations: list[Automation], *, as_json: bool) -> None:
    """Print automations as text or JSON."""
    if as_json:
        rows = []
        for item in automations:
            data = item.to_dict()
            row = {key: data[key] for key in ("id", "name", "status", "speed", "output_directory", "timezone",
                                               "next_day", "consecutive_failures", "next_retry_at", "last_error",
                                               "connection")}  # fmt: skip
            rows.append(row | {"camera_ids": [camera.id for camera in item.cameras]})
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not automations:
        print("No Daily Automations.")
    for item in [] if as_json else automations:
        print(f"{item.id}  {item.status:<8}  {item.name}  next {item.next_day.isoformat()}")


def build_parser() -> argparse.ArgumentParser:
    """Describe every command and option."""
    parser = argparse.ArgumentParser(prog="timelapse", description="Export UniFi Protect recordings as timelapses.")
    parser.add_argument("--speed", choices=list(SPEEDS), default=DEFAULT_SPEED)
    for flag in ("--instance", "--token", "--username", "--password"):
        parser.add_argument(flag)
    parser.add_argument("--verify-ssl", type=_bool_arg, metavar="BOOL")
    parser.add_argument("--start-date", type=_moment, help="MM-DD-YYYY or MM-DD-YYYY-HH-MM-SS")
    parser.add_argument("--end-date", type=_moment, help="MM-DD-YYYY or MM-DD-YYYY-HH-MM-SS")
    parser.add_argument("--output")
    parser.add_argument("--request-timeout-seconds", type=_count)
    parser.add_argument("--max-download-mib", type=_count)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--profile", metavar="NAME")
    source.add_argument("--dotenv", type=_absolute_file, metavar="ABSOLUTE_PATH")
    source.add_argument("--create-profile", action="store_true")

    commands = parser.add_subparsers(dest="command")
    automation = commands.add_parser("automation", help="manage Daily Automations")
    actions = automation.add_subparsers(dest="automation_command", required=True)

    def with_reference(sub: argparse.ArgumentParser, *, required: bool) -> None:
        group = sub.add_mutually_exclusive_group(required=required)
        group.add_argument("--profile")
        group.add_argument("--dotenv", type=_absolute_file)

    add = actions.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--camera", action="append", required=True)
    add.add_argument("--output", required=True)
    add.add_argument("--speed", choices=list(SPEEDS), default=DEFAULT_SPEED)
    add.add_argument("--timezone")
    with_reference(add, required=True)
    edit = actions.add_parser("edit")
    edit.add_argument("selector")
    edit.add_argument("--name")
    edit.add_argument("--camera", action="append")
    with_reference(edit, required=False)
    actions.add_parser("list").add_argument("--json", action="store_true")
    actions.add_parser("run").add_argument("selector", nargs="?")
    for name in ("stop", "resume", "remove"):
        actions.add_parser(name).add_argument("selector")
    reexport = actions.add_parser("re-export", aliases=["reexport"])
    reexport.add_argument("selector")
    reexport.add_argument("day", type=date.fromisoformat, metavar="YYYY-MM-DD")
    return parser


def main(
    argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, make_protect: MakeProtect = Protect
) -> int:
    """Run the CLI and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    dotenv_path = getattr(args, "dotenv", None) if args.command is None else None
    dotenv_path = dotenv_path or Path.cwd() / ".env"
    dotenv = {key: value for key, value in dotenv_values(dotenv_path).items() if value is not None}
    if args.command is None and args.output is None:
        args.output = env.get("TIMELAPSE_OUTPUT") or dotenv.get("TIMELAPSE_OUTPUT")
    context = Context(args, env, dotenv, make_protect)
    try:
        if args.command == "automation":
            automation_command(context)
        elif args.create_profile:
            create_profile(context)
        else:
            start, end, full_day = export_range(args.start_date, args.end_date, parser.error)
            asyncio.run(export_once(context, start, end, full_day))
    except RegistryOwnedError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 3
    except (TimelapseError, InvalidRequestError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0

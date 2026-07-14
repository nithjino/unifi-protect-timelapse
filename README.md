# UniFi Protect TimeLapse

Create MP4 timelapses from UniFi Protect recordings with an interactive command-line tool, a native macOS app, or a Qt desktop interface.

The exporter lists the cameras available through the UniFi Protect Integration API, lets you select what to export, and streams the finished video to disk. It supports exact time ranges, complete local calendar days, and continuously scheduled daily exports.

> This is an independent project and is not affiliated with or endorsed by Ubiquiti Inc.

## Features

- Interactive camera discovery and selection
- Native SwiftUI interface for macOS
- Cross-platform Qt desktop interface built with PySide6
- CLI speeds of `60x`, `120x`, `300x`, and `600x`
- Exact date/time ranges or daylight-saving-aware local calendar days
- Daily automatic exports for the most recently completed day
- Multiple concurrent per-camera jobs in the desktop interfaces
- Streaming downloads with progress, cancellation, and atomic finalization
- Safe output filenames and protection against overwriting existing videos
- Configurable request timeout and maximum download size
- OS credential-store integration in the desktop interfaces

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) for dependency and environment management
- A reachable UniFi Protect console; trusted self-signed certificates are supported
- A Protect Integration API token for camera discovery
- A dedicated local Protect user with permission to view and export recordings

Do not use a UI.com SSO account for video export authentication. A minimally privileged local Protect account is recommended.

## Quick start

Clone the repository and install the dependencies:

```bash
git clone https://github.com/nithjino/unifi-protect-timelapse.git
cd unifi-protect-timelapse
uv sync
```

Create your local configuration file:

```bash
cp .env.example .env
```

Edit `.env` with your Protect URL, API token, and local-user credentials:

```dotenv
UNIFI_PROTECT_URL=https://protect.local/proxy/protect/integration/v1
UNIFI_PROTECT_TOKEN=replace-with-your-integration-api-token
UNIFI_PROTECT_USERNAME=timelapse-user
UNIFI_PROTECT_PASSWORD=replace-with-your-local-user-password
UNIFI_PROTECT_VERIFY_SSL=true

TIMELAPSE_REQUEST_TIMEOUT_SECONDS=0
TIMELAPSE_MAX_DOWNLOAD_MIB=10240
```

Run a full-day export. The CLI will list available cameras and ask you to select one by number:

```bash
uv run timelapse --start-date 07-13-2026
```

## Configuration

The CLI reads `.env` from the current working directory. Explicit command-line arguments take precedence over environment-backed defaults, and existing process environment variables take precedence over values in `.env`.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `UNIFI_PROTECT_URL` | Yes | — | Protect Integration API URL, normally ending in `/proxy/protect/integration/v1` |
| `UNIFI_PROTECT_TOKEN` | Yes | — | Integration API token used to list cameras |
| `UNIFI_PROTECT_USERNAME` | Yes | — | Dedicated local Protect username used for video exports |
| `UNIFI_PROTECT_PASSWORD` | Yes | — | Password for the dedicated local Protect user |
| `UNIFI_PROTECT_VERIFY_SSL` | No | `true` | Whether to verify the Protect console's TLS certificate |
| `TIMELAPSE_OUTPUT` | No | Generated filename | Output MP4 path, or output directory in daily mode |
| `TIMELAPSE_REQUEST_TIMEOUT_SECONDS` | No | `0` | Request timeout in seconds; `0` disables the timeout |
| `TIMELAPSE_MAX_DOWNLOAD_MIB` | No | `10240` | Maximum download size in MiB; `0` disables the limit |

Speed and date boundaries are intentionally CLI-only. They are not loaded from `.env`.

## CLI usage

Display every available option:

```bash
uv run timelapse --help
```

### Export one complete local day

A date without a time covers that local calendar day from midnight to midnight, including daylight-saving transitions:

```bash
uv run timelapse \
  --start-date 07-13-2026 \
  --speed 600x \
  --output ./back-yard-2026-07-13.mp4
```

`--end-date 07-13-2026` works the same way when it is the only boundary provided.

### Export an exact time range

Use `MM-DD-YYYY-HH-MM-SS` for precise local timestamps:

```bash
uv run timelapse \
  --start-date 07-13-2026-08-00-00 \
  --end-date 07-13-2026-18-30-00 \
  --speed 120x \
  --output ./daylight-hours.mp4
```

If only one timestamp boundary is supplied, the CLI exports a 24-hour range beginning at the start boundary or ending at the end boundary.

### Run continuous daily exports

Daily mode first exports the latest completed local day, then remains running and exports each day after the next local midnight:

```bash
uv run timelapse \
  --daily \
  --speed 600x \
  --output ./daily-timelapses
```

If a daily output already exists, it is skipped. Stop the scheduler with `Ctrl+C`.

### Override connection settings

Every required connection value can be supplied directly. Be aware that command-line passwords may be stored in shell history or exposed to local process-inspection tools:

```bash
uv run timelapse \
  --instance https://protect.local/proxy/protect/integration/v1 \
  --token YOUR_INTEGRATION_TOKEN \
  --username timelapse-user \
  --password YOUR_LOCAL_USER_PASSWORD \
  --start-date 07-13-2026
```

### Adjust export safeguards

The timeout is disabled by default so long-running exports can finish. The default maximum download size is 10 GiB:

```bash
uv run timelapse \
  --start-date 07-13-2026 \
  --request-timeout-seconds 3600 \
  --max-download-mib 2048
```

Set either safeguard to `0` to disable it:

```bash
uv run timelapse \
  --start-date 07-13-2026 \
  --request-timeout-seconds 0 \
  --max-download-mib 0
```

The package can also be launched through Python's module interface:

```bash
uv run python -m timelapse --start-date 07-13-2026
```

## Native macOS GUI

The macOS interface is built with SwiftUI and uses the Python exporter as an embedded helper. It supports reusable connection profiles, multi-camera exports, full-day mode, daily automations, per-download progress, cancellation, restart actions, and a separate logs window. Credentials are stored in the macOS login Keychain.

![Native macOS TimeLapse interface](docs/screenshots/macos-native-ui.png)

Build and launch the native app on macOS:

```bash
./build-macos.sh
open dist/macos/timelapse.app
```

The build requires macOS 15 or newer, the Swift/Xcode command-line toolchain, and `uv`; its isolated backend environment uses Python 3.13 by default. Without `MACOS_SIGN_IDENTITY`, the script applies an ad-hoc signature suitable for local development. Set `MACOS_SIGN_IDENTITY` and, optionally, `MACOS_NOTARY_PROFILE` when producing a distributable build.

## Cross-platform Qt GUI (macOS, Linux, and Windows)

The PySide6 interface—often referred to as the PyQt GUI—runs on macOS, Linux, and Windows. macOS and Windows also have native GUIs for people who prefer a platform-native experience. The Qt interface provides connection profiles, camera selection, exact or 24-hour ranges, multiple download jobs, daily automations, progress reporting, cancellation, and logs. Secrets are stored with the operating system's credential store through `keyring`.

![Qt TimeLapse interface](docs/screenshots/pyqt-ui.png)

Run it directly from a source checkout:

```bash
uv run timelapse-gui
```

Build the bundled Linux application on Linux:

```bash
./build-linux.sh
```

The Linux build is written to `dist/linux/`. Build on the oldest Linux distribution you intend to support for the widest glibc compatibility.

## Native Windows GUI

The Windows interface is built with WPF and uses the same embedded Python export backend as the native macOS application. Build it from PowerShell on Windows with the .NET 8 SDK and `uv` installed:

```powershell
.\build-windows.ps1
```

The build produces one self-contained distributable at `dist\windows\timelapse.exe`. The recipient does not need to install Python or .NET.

## Date and output behavior

- Accepted dates are `MM-DD-YYYY` and `MM-DD-YYYY-HH-MM-SS`.
- All CLI dates are interpreted in the computer's local timezone.
- A date-only boundary exports one complete local calendar day.
- Two timestamp boundaries export their exact range.
- `--daily` cannot be combined with `--start-date` or `--end-date`.
- Without `--output`, filenames include the camera name, start, end, and selected speed.
- Normal filenames follow `timelapse_<camera>_<start>_<end>_<speed>.mp4`.
- Daily filenames use the same format with a `daily_` prefix.
- Existing output files are never overwritten.
- Downloads are written to a temporary `.part` file and atomically renamed only after success.

## Security notes

- Use a dedicated local Protect account with only the permissions needed to view and export recordings.
- The Integration API token is used for camera discovery; private video export uses the local username and password.
- Keep TLS verification enabled unless you are connecting to a trusted local console with a self-signed certificate.
- `.env` is ignored by Git, but it is still a plaintext file. Restrict its filesystem permissions and never commit it.
- The desktop interfaces store connection secrets in the platform credential store instead of application preferences.
- Export error bodies are size-limited and escaped before they are printed to a terminal.

## Troubleshooting

### Camera listing works, but export returns `HTTP 401` with `{"error":403}`

The Integration API token can list cameras but does not authenticate the private video-export endpoint. Confirm that `UNIFI_PROTECT_USERNAME` and `UNIFI_PROTECT_PASSWORD` belong to a local Protect user with recording export access.

### TLS certificate verification fails

Use a valid certificate whenever possible. For a trusted console on a private network with a self-signed certificate, set:

```dotenv
UNIFI_PROTECT_VERIFY_SSL=false
```

You can override it for one CLI invocation as well:

```bash
uv run timelapse --verify-ssl false --start-date 07-13-2026
```

### No cameras are returned

Check the Protect URL, confirm that it uses HTTPS, and verify that the Integration API token can read the console's cameras.

### A long export stops early

Leave `TIMELAPSE_REQUEST_TIMEOUT_SECONDS=0` for no request timeout, or pass a larger positive value. Also check `TIMELAPSE_MAX_DOWNLOAD_MIB` if the export is unusually large.

### Daily mode appears idle

After catching up through the latest completed day, daily mode waits until the current local day ends. The CLI or desktop application must remain running for the next export.

## Development

Install the runtime and development dependencies:

```bash
uv sync --group dev
```

Run the Python quality checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
```

Run the native macOS tests on macOS:

```bash
swift test --package-path native-macos
```

The main source areas are:

| Path | Responsibility |
| --- | --- |
| `timelapse/config.py` | CLI parsing, `.env` loading, validation, and date ranges |
| `timelapse/protect.py` | Protect URL parsing, authentication, and camera discovery |
| `timelapse/download.py` | Streaming downloads, output naming, progress, and limits |
| `timelapse/schedule.py` | Local calendar-day and daily scheduling helpers |
| `timelapse/service.py` | UI-neutral camera and export orchestration |
| `timelapse/cli.py` | Interactive CLI workflow |
| `timelapse/gui.py` | Qt desktop interface |
| `timelapse/native_backend.py` | JSON-lines bridge used by native desktop shells |
| `native-macos/` | Native SwiftUI macOS application |
| `tests/` | Python unit and GUI tests |

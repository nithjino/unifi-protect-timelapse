# UniFi Protect TimeLapse

Export UniFi Protect recordings as MP4 timelapses from the CLI, a desktop app, or a local web dashboard.

![TimeLapse web dashboard](docs/screenshots/web-dashboard.jpg)

TimeLapse supports exact ranges, full local calendar days, and recurring daily exports. Downloads stream directly to disk and can be cancelled without leaving a partial video behind.

> Independent project. Not affiliated with or endorsed by Ubiquiti Inc.

## Highlights

- Native macOS and Windows apps
- Cross-platform Qt app for macOS, Linux, and Windows
- Local web dashboard for phones and other computers
- Multi-camera exports with progress, retry, cancellation, and notifications
- Speeds from normal playback (`1x`) through `600x`
- Start and end thumbnail previews
- Durable, named Daily Automations with multi-camera catch-up
- Credentials stored in the OS credential store

## Requirements

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) when running from source
- A reachable UniFi Protect console
- A Protect Integration API token
- A dedicated local Protect user

Packaged desktop builds include Python.

## Protect access

TimeLapse uses both Protect authentication methods:

- The **Integration API token** lists cameras and provides fallback live snapshots.
- The **local username and password** export recordings and fetch historical thumbnails.

Give the local user access to each camera plus these device permissions:

| Permission | Used for |
| --- | --- |
| Livestream | Historical thumbnails |
| Playback | Recorded footage |
| Playback Download | MP4 exports |

Use a dedicated local account, not a UI.com SSO or owner account.

## Quick start

```bash
git clone https://github.com/nithjino/unifi-protect-timelapse.git
cd unifi-protect-timelapse
uv sync
cp .env.example .env
```

Add your Protect details to `.env`:

```dotenv
UNIFI_PROTECT_URL=https://protect.local/proxy/protect/integration/v1
UNIFI_PROTECT_TOKEN=replace-with-your-integration-api-token
UNIFI_PROTECT_USERNAME=timelapse-user
UNIFI_PROTECT_PASSWORD=replace-with-your-local-user-password
UNIFI_PROTECT_VERIFY_SSL=true
```

Export one full day:

```bash
uv run timelapse --start-date 07-30-2026
```

TimeLapse lists the available cameras and asks which one to use.

## CLI

See every option:

```bash
uv run timelapse --help
```

Exact time range:

```bash
uv run timelapse \
  --start-date 07-30-2026-08-00-00 \
  --end-date 07-30-2026-18-30-00 \
  --speed 120x
```

Create a durable Daily Automation. The output directory and `.env` path must be absolute; a saved profile can be used instead of `--dotenv`:

```bash
mkdir -p "$PWD/daily-timelapses"
uv run timelapse automation add \
  --name "Front doors" \
  --camera "Front Door" \
  --speed 600x \
  --output "$PWD/daily-timelapses" \
  --timezone America/New_York \
  --dotenv "$PWD/.env"
uv run timelapse automation run
```

Manage automations by name or immutable ID:

```bash
uv run timelapse automation list --json
uv run timelapse automation stop "Front doors"
uv run timelapse automation resume "Front doors"
uv run timelapse automation re-export "Front doors" 2026-07-30
uv run timelapse automation remove "Front doors"
```

The legacy `--daily` option remains as an importer and single-automation runner. It requires `--profile NAME` or an absolute `--dotenv PATH`; command-line credentials are intentionally rejected because an automation must resolve them again after restart.

Create and use a saved connection profile:

```bash
uv run timelapse --create-profile
uv run timelapse --profile home --start-date 07-30-2026
```

Profiles are used when `.env` is not present. They are case-sensitive and stored in the OS credential store.

Accepted date formats are `MM-DD-YYYY` and `MM-DD-YYYY-HH-MM-SS`. A date by itself means one complete local calendar day.

## macOS app

![Native macOS TimeLapse app](docs/screenshots/macos-native-ui.png)

Build and open the native SwiftUI app:

```bash
./build-macos.sh
open dist/macos/timelapse.app
```

Requires macOS 15+, `uv`, and the Swift/Xcode command-line tools. Set `MACOS_SIGN_IDENTITY` when building for distribution; local builds use an ad-hoc signature.

## Qt desktop app

![Qt TimeLapse app](docs/screenshots/pyqt-ui.png)

Run from source:

```bash
uv run timelapse-gui
```

Build the Linux executable and AppImage:

```bash
./build-linux.sh
```

The Qt app runs on macOS, Linux, and Windows. Secrets are stored through `keyring` in Keychain, Windows Credential Manager, or Secret Service.

## Windows app

Build the native WPF app from PowerShell with .NET 8 and `uv` installed:

```powershell
.\build-windows.ps1
```

The self-contained build is written to `dist\windows\timelapse.exe`.

## Web dashboard

Run locally:

```bash
./start-web.sh
```

Then open `http://127.0.0.1:8000`.

For access from another device, set a strong web password and list every hostname or IP address used in the browser URL:

```dotenv
TZ=America/New_York
TIMELAPSE_WEB_HOST=0.0.0.0
TIMELAPSE_WEB_TRUSTED_HOSTS=timelapse-server.local,192.168.2.17
TIMELAPSE_WEB_USERNAME=timelapse
TIMELAPSE_WEB_PASSWORD=replace-with-a-long-random-password
```

![TimeLapse web login](docs/screenshots/web-login.jpg)

Docker Compose works too:

```bash
mkdir -p data
docker compose up --build -d
```

Exports, job history, and Daily Automations live in `./data`. Recreate the container after changing `.env`:

```bash
docker compose up -d --force-recreate timelapse-web
```

Keep the web app on a trusted LAN, behind a VPN, or behind an HTTPS reverse proxy. Do not expose it directly to the internet.

## Useful settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `TIMELAPSE_OUTPUT` | Generated filename | Output file or daily output directory |
| `TIMELAPSE_REQUEST_TIMEOUT_SECONDS` | `0` | Whole-operation timeout; `0` disables it |
| `TIMELAPSE_MAX_DOWNLOAD_MIB` | `10240` | Maximum export size; `0` disables it |
| `TIMELAPSE_MAX_ACTIVE_EXPORTS` | `4` | Concurrent exports in CLI and desktop runtimes |
| `TIMELAPSE_MAX_QUEUED_EXPORTS` | `20` | Coordinator-admitted queued exports in CLI and desktop runtimes |
| `TIMELAPSE_WEB_SESSION_HOURS` | `168` | Web session length |
| `TIMELAPSE_WEB_MAX_ACTIVE_EXPORTS` | `4` | Concurrent web exports |
| `TIMELAPSE_WEB_MAX_QUEUED_EXPORTS` | `20` | Queued web exports |
| `TIMELAPSE_WEB_STORAGE_QUOTA_MIB` | `102400` | Total web export storage |

Existing files are never overwritten. Downloads use a temporary `.part` file and are renamed only after they finish.

## Version 2 compatibility changes

Version 2.0 uses one automation engine, registry, and export coordinator in each CLI, Web, or desktop runtime. Each runtime allows four active and twenty queued exports by default; manual and daily work share those limits and rate-limit recovery. A lone rate-limited job becomes eligible again after 60 seconds when Protect supplies no `Retry-After` value.

Daily Automations are durable and may contain multiple cameras. Desktop apps keep one long-lived version-2 Python supervisor so jobs share capacity and output reservations. The Web interface now distinguishes **Stop** (retain the automation) from **Remove** (remove it after submitted work drains); **Delete** is reserved for an exported MP4. CLI Daily Automations require a named profile or absolute `.env` reference. Existing Web schedule state and legacy CLI daily checkpoints are backed up and migrated on first use.

## Troubleshooting

- **Camera listing works, but exports return 401/403:** check the local username, password, camera access, Playback, and Playback Download permissions.
- **A preview uses a live snapshot:** the local user could not fetch the historical frame. Check camera access and Livestream permission.
- **Protect returns HTTP 429:** let the built-in retry queue work and avoid starting more copies of TimeLapse.
- **TLS verification fails:** use a valid certificate when possible. For a trusted private console with a self-signed certificate, set `UNIFI_PROTECT_VERIFY_SSL=false`.

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
```

Native checks:

```bash
swift test --package-path native-macos
dotnet build native-windows/TimeLapseNative.csproj -p:EnableWindowsTargeting=true
```

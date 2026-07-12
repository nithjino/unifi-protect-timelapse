#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="${ROOT_DIR}/dist/linux"
WORK_DIR="${ROOT_DIR}/build/pyinstaller-linux"
SPEC_DIR="${WORK_DIR}/spec"
ENTRY_POINT="${ROOT_DIR}/timelapse/gui.py"
DEFAULT_ICON="${ROOT_DIR}/assets/icons/timelapse.png"
ICON_PATH="${TIMELAPSE_ICON:-${DEFAULT_ICON}}"
ARTIFACT="${DIST_DIR}/timelapse"

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "Error: the Linux executable must be built on Linux." >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv is required on the build machine. Install it from https://docs.astral.sh/uv/." >&2
    exit 1
fi

cd "${ROOT_DIR}"
mkdir -p "${DIST_DIR}" "${WORK_DIR}" "${SPEC_DIR}"

echo "Synchronizing build dependencies..."
uv sync --group dev

pyinstaller_args=(
    --noconfirm
    --clean
    --windowed
    --onefile
    --name timelapse
    --distpath "${DIST_DIR}"
    --workpath "${WORK_DIR}"
    --specpath "${SPEC_DIR}"
    --paths "${ROOT_DIR}"
    --collect-submodules uiprotect.data
    --collect-submodules uiprotect.devices
    --collect-submodules uiprotect.events
    --add-data "${DEFAULT_ICON}:timelapse_assets"
    --icon "${ICON_PATH}"
)

if [[ ! -f "${DEFAULT_ICON}" ]]; then
    echo "Error: bundled application icon does not exist: ${DEFAULT_ICON}" >&2
    exit 1
fi
if [[ ! -f "${ICON_PATH}" ]]; then
    echo "Error: Linux icon does not exist: ${ICON_PATH}" >&2
    exit 1
fi

echo "Building Linux timelapse executable..."
uv run pyinstaller "${pyinstaller_args[@]}" "${ENTRY_POINT}"

if [[ ! -f "${ARTIFACT}" ]]; then
    echo "Error: PyInstaller completed without creating ${ARTIFACT}." >&2
    exit 1
fi

chmod +x "${ARTIFACT}"
install -m 644 "${DEFAULT_ICON}" "${DIST_DIR}/timelapse.png"

echo
echo "Build complete: ${ARTIFACT}"
echo "Launcher icon: ${DIST_DIR}/timelapse.png"
echo "The executable contains Python, Qt, and all runtime dependencies."
echo "Build on the oldest Linux distribution you intend to support for the widest glibc compatibility."

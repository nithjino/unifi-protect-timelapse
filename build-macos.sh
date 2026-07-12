#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="${ROOT_DIR}/dist/macos"
WORK_DIR="${ROOT_DIR}/build/pyinstaller-macos"
SPEC_DIR="${WORK_DIR}/spec"
ENTRY_POINT="${ROOT_DIR}/timelapse/gui.py"
ARTIFACT="${DIST_DIR}/timelapse.app"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: the macOS app must be built on macOS." >&2
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
    --onedir
    --name timelapse
    --osx-bundle-identifier io.timelapse.desktop
    --distpath "${DIST_DIR}"
    --workpath "${WORK_DIR}"
    --specpath "${SPEC_DIR}"
    --paths "${ROOT_DIR}"
    --collect-submodules uiprotect.data
    --collect-submodules uiprotect.devices
    --collect-submodules uiprotect.events
)

if [[ -n "${TIMELAPSE_ICON:-}" ]]; then
    if [[ ! -f "${TIMELAPSE_ICON}" ]]; then
        echo "Error: TIMELAPSE_ICON does not exist: ${TIMELAPSE_ICON}" >&2
        exit 1
    fi
    pyinstaller_args+=(--icon "${TIMELAPSE_ICON}")
fi

if [[ -n "${MACOS_SIGN_IDENTITY:-}" ]]; then
    pyinstaller_args+=(--codesign-identity "${MACOS_SIGN_IDENTITY}")
fi

echo "Building timelapse.app..."
uv run pyinstaller "${pyinstaller_args[@]}" "${ENTRY_POINT}"

if [[ ! -d "${ARTIFACT}" ]]; then
    echo "Error: PyInstaller completed without creating ${ARTIFACT}." >&2
    exit 1
fi

echo
echo "Build complete: ${ARTIFACT}"
echo "The app contains Python, Qt, and all runtime dependencies."
if [[ -z "${MACOS_SIGN_IDENTITY:-}" ]]; then
    echo "Note: set MACOS_SIGN_IDENTITY to a Developer ID Application identity for distributable signing."
fi

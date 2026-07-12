#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="${ROOT_DIR}/dist/macos"
WORK_DIR="${ROOT_DIR}/build/native-macos"
SWIFT_PACKAGE_DIR="${ROOT_DIR}/native-macos"
SWIFT_WORK_DIR="${WORK_DIR}/swift"
PYTHON_ENV_DIR="${WORK_DIR}/python-env"
BACKEND_WORK_DIR="${WORK_DIR}/backend"
BACKEND_DIST_DIR="${WORK_DIR}/backend-dist"
BACKEND_SPEC="${SWIFT_PACKAGE_DIR}/Backend.spec"
INFO_PLIST="${SWIFT_PACKAGE_DIR}/Info.plist"
DEFAULT_ICON="${ROOT_DIR}/assets/icons/timelapse.icns"
ICON_PATH="${TIMELAPSE_ICON:-${DEFAULT_ICON}}"
ARTIFACT="${DIST_DIR}/timelapse.app"
NESTED_BACKEND="${ARTIFACT}/Contents/Helpers/TimeLapseBackend.app"
NESTED_BACKEND_EXECUTABLE="${NESTED_BACKEND}/Contents/MacOS/timelapse-backend"
PYTHON_REQUEST="${TIMELAPSE_MACOS_PYTHON:-3.13}"
MACOS_DEPLOYMENT_TARGET="15.0"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: the macOS app must be built on macOS." >&2
    exit 1
fi

for command in uv swift xcrun codesign plutil ditto file lipo; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "Error: ${command} is required on the build machine." >&2
        exit 1
    fi
done
if [[ ! -f "${ICON_PATH}" ]]; then
    echo "Error: macOS icon does not exist: ${ICON_PATH}" >&2
    exit 1
fi

cd "${ROOT_DIR}"

rm -rf "${WORK_DIR:?}" "${ARTIFACT:?}"
mkdir -p "${DIST_DIR}" "${SWIFT_WORK_DIR}" "${BACKEND_WORK_DIR}" "${BACKEND_DIST_DIR}"

echo "Creating the isolated Python ${PYTHON_REQUEST} backend environment..."
UV_PROJECT_ENVIRONMENT="${PYTHON_ENV_DIR}" uv sync --python "${PYTHON_REQUEST}" --group dev
BUILD_PYTHON="${PYTHON_ENV_DIR}/bin/python"
PYINSTALLER="${PYTHON_ENV_DIR}/bin/pyinstaller"

ARCH="$("${BUILD_PYTHON}" -c 'import platform; print(platform.machine())')"
case "${ARCH}" in
    arm64 | x86_64) ;;
    *)
        echo "Error: unsupported build architecture: ${ARCH}" >&2
        exit 1
        ;;
esac

echo "Building the embedded Python export backend (${ARCH})..."
TIMELAPSE_BUILD_ARCH="${ARCH}" \
PYINSTALLER_STRICT_BUNDLE_CODESIGN_ERROR=1 \
PYINSTALLER_VERIFY_BUNDLE_SIGNATURE=1 \
"${PYINSTALLER}" \
    --noconfirm \
    --clean \
    --distpath "${BACKEND_DIST_DIR}" \
    --workpath "${BACKEND_WORK_DIR}" \
    "${BACKEND_SPEC}"

BACKEND_APP="${BACKEND_DIST_DIR}/TimeLapseBackend.app"
if [[ ! -d "${BACKEND_APP}" ]]; then
    echo "Error: PyInstaller completed without creating ${BACKEND_APP}." >&2
    exit 1
fi

echo "Building the native SwiftUI application (${ARCH})..."
swift build \
    --package-path "${SWIFT_PACKAGE_DIR}" \
    --scratch-path "${SWIFT_WORK_DIR}" \
    --configuration release \
    --arch "${ARCH}"

SWIFT_BIN_DIR="$(
    swift build \
        --package-path "${SWIFT_PACKAGE_DIR}" \
        --scratch-path "${SWIFT_WORK_DIR}" \
        --configuration release \
        --arch "${ARCH}" \
        --show-bin-path
)"
SWIFT_EXECUTABLE="${SWIFT_BIN_DIR}/TimeLapseNative"
if [[ ! -x "${SWIFT_EXECUTABLE}" ]]; then
    echo "Error: Swift completed without creating ${SWIFT_EXECUTABLE}." >&2
    exit 1
fi

echo "Assembling timelapse.app..."
mkdir -p "${ARTIFACT}/Contents/MacOS" "${ARTIFACT}/Contents/Helpers" "${ARTIFACT}/Contents/Resources"
install -m 755 "${SWIFT_EXECUTABLE}" "${ARTIFACT}/Contents/MacOS/timelapse"
install -m 644 "${INFO_PLIST}" "${ARTIFACT}/Contents/Info.plist"
ditto "${BACKEND_APP}" "${NESTED_BACKEND}"

install -m 644 "${ICON_PATH}" "${ARTIFACT}/Contents/Resources/TimeLapse.icns"

plutil -lint "${ARTIFACT}/Contents/Info.plist"

echo "Checking compatibility with macOS ${MACOS_DEPLOYMENT_TARGET}..."
"${BUILD_PYTHON}" - "${ARTIFACT}" "${MACOS_DEPLOYMENT_TARGET}" <<'PY'
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
maximum = tuple(int(part) for part in sys.argv[2].split("."))


def normalized(version: str) -> tuple[int, ...]:
    parts = tuple(int(part) for part in version.split("."))
    length = max(len(parts), len(maximum))
    return parts + (0,) * (length - len(parts))


offenders: list[tuple[Path, str]] = []
for path in root.rglob("*"):
    if not path.is_file():
        continue
    kind = subprocess.run(
        ["file", "-b", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "Mach-O" not in kind:
        continue
    metadata = subprocess.run(
        ["xcrun", "vtool", "-show-build", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for version in re.findall(r"(?m)^\s*minos\s+(\d+(?:\.\d+)*)", metadata):
        if normalized(version) > normalized(sys.argv[2]):
            offenders.append((path.relative_to(root), version))

if offenders:
    for path, version in offenders:
        print(f"Error: {path} requires macOS {version}", file=sys.stderr)
    raise SystemExit(f"bundle contains code newer than the macOS {sys.argv[2]} deployment target")
PY

if [[ -n "${MACOS_SIGN_IDENTITY:-}" ]]; then
    echo "Signing the native app with ${MACOS_SIGN_IDENTITY}..."
    codesign \
        --force \
        --options runtime \
        --timestamp \
        --sign "${MACOS_SIGN_IDENTITY}" \
        "${ARTIFACT}"
else
    echo "Applying an ad-hoc development signature..."
    codesign --force --sign - "${ARTIFACT}"
fi

codesign --verify --all-architectures --deep --strict --verbose "${ARTIFACT}"

health_output="$(printf '%s\n' '{"id":"build-health","command":"health"}' | "${NESTED_BACKEND_EXECUTABLE}")"
HEALTH_OUTPUT="${health_output}" "${BUILD_PYTHON}" -c '
import json
import os

events = [json.loads(line) for line in os.environ["HEALTH_OUTPUT"].splitlines() if line]
if not any(event.get("id") == "build-health" and event.get("event") == "complete" for event in events):
    raise SystemExit("backend health check did not return a complete event")
'

main_arch="$(lipo -archs "${ARTIFACT}/Contents/MacOS/timelapse")"
backend_arch="$(lipo -archs "${NESTED_BACKEND_EXECUTABLE}")"
if [[ "${main_arch}" != "${ARCH}" || "${backend_arch}" != "${ARCH}" ]]; then
    echo "Error: architecture mismatch (Swift=${main_arch}, backend=${backend_arch}, expected=${ARCH})." >&2
    exit 1
fi

if [[ -n "${MACOS_NOTARY_PROFILE:-}" ]]; then
    if [[ -z "${MACOS_SIGN_IDENTITY:-}" ]]; then
        echo "Error: MACOS_NOTARY_PROFILE requires MACOS_SIGN_IDENTITY." >&2
        exit 1
    fi
    archive="${DIST_DIR}/timelapse-macos-${ARCH}.zip"
    rm -f "${archive}"
    ditto -c -k --sequesterRsrc --keepParent "${ARTIFACT}" "${archive}"
    xcrun notarytool submit "${archive}" --keychain-profile "${MACOS_NOTARY_PROFILE}" --wait
    xcrun stapler staple "${ARTIFACT}"
    xcrun stapler validate "${ARTIFACT}"
    rm -f "${archive}"
    ditto -c -k --sequesterRsrc --keepParent "${ARTIFACT}" "${archive}"
    echo "Notarized archive: ${archive}"
fi

echo
echo "Build complete: ${ARTIFACT}"
echo "Native interface: SwiftUI/AppKit"
echo "Export engine: embedded Python helper"
echo "Architecture: ${ARCH}"
if [[ -z "${MACOS_SIGN_IDENTITY:-}" ]]; then
    echo "Note: set MACOS_SIGN_IDENTITY for distributable signing."
fi

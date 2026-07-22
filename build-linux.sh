#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="${ROOT_DIR}/dist/linux"
WORK_DIR="${ROOT_DIR}/build/pyinstaller-linux"
SPEC_DIR="${WORK_DIR}/spec"
APPDIR="${WORK_DIR}/TimeLapse.AppDir"
ENTRY_POINT="${ROOT_DIR}/timelapse/gui.py"
DEFAULT_ICON="${ROOT_DIR}/assets/icons/timelapse.png"
ICON_PATH="${TIMELAPSE_ICON:-${DEFAULT_ICON}}"
ARTIFACT="${DIST_DIR}/timelapse"
APPIMAGE_TOOL_VERSION="1.9.1"
APPIMAGE_TOOL_DIR="${WORK_DIR}/appimagetool"
PYTHON_REQUEST="${TIMELAPSE_LINUX_PYTHON:-3.13}"

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "Error: the Linux executable must be built on Linux." >&2
    exit 1
fi

for command in uv curl objdump sha256sum; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "Error: ${command} is required on the build machine." >&2
        exit 1
    fi
done

case "$(uname -m)" in
    x86_64)
        APPIMAGE_ARCH="x86_64"
        APPIMAGE_TOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
        ;;
    aarch64 | arm64)
        APPIMAGE_ARCH="aarch64"
        APPIMAGE_TOOL_SHA256="f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158"
        ;;
    *)
        echo "Error: AppImage packaging does not support architecture $(uname -m)." >&2
        exit 1
        ;;
esac

APPIMAGE_ARTIFACT="${DIST_DIR}/TimeLapse-${APPIMAGE_ARCH}.AppImage"

cd "${ROOT_DIR}"
rm -rf "${APPDIR}" "${APPIMAGE_ARTIFACT}"
mkdir -p "${DIST_DIR}" "${WORK_DIR}" "${SPEC_DIR}"

echo "Synchronizing build dependencies..."
uv sync --python "${PYTHON_REQUEST}" --group dev

echo "Checking Qt build libraries..."
uv run python -c "from PySide6 import QtCore, QtDBus, QtGui, QtNetwork, QtWidgets"

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
    --hidden-import keyring.backends.SecretService
    --hidden-import PySide6.QtDBus
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

echo "Assembling AppDir..."
mkdir -p \
    "${APPDIR}/usr/bin" \
    "${APPDIR}/usr/share/applications" \
    "${APPDIR}/usr/share/icons/hicolor/512x512/apps"
install -m 755 "${ARTIFACT}" "${APPDIR}/usr/bin/timelapse"
install -m 755 "${ROOT_DIR}/assets/linux/AppRun" "${APPDIR}/AppRun"
install -m 644 "${ROOT_DIR}/assets/linux/timelapse.desktop" "${APPDIR}/timelapse.desktop"
install -m 644 "${DEFAULT_ICON}" "${APPDIR}/timelapse.png"
install -m 644 "${ROOT_DIR}/assets/linux/timelapse.desktop" \
    "${APPDIR}/usr/share/applications/timelapse.desktop"
install -m 644 "${DEFAULT_ICON}" \
    "${APPDIR}/usr/share/icons/hicolor/512x512/apps/timelapse.png"
ln -s timelapse.png "${APPDIR}/.DirIcon"

if [[ -n "${APPIMAGETOOL:-}" ]]; then
    appimage_tool="${APPIMAGETOOL}"
else
    mkdir -p "${APPIMAGE_TOOL_DIR}"
    appimage_tool="${APPIMAGE_TOOL_DIR}/appimagetool-${APPIMAGE_ARCH}.AppImage"
    if [[ ! -f "${appimage_tool}" ]]; then
        echo "Downloading appimagetool ${APPIMAGE_TOOL_VERSION} (${APPIMAGE_ARCH})..."
        curl \
            --fail \
            --location \
            --output "${appimage_tool}" \
            "https://github.com/AppImage/appimagetool/releases/download/${APPIMAGE_TOOL_VERSION}/appimagetool-${APPIMAGE_ARCH}.AppImage"
    fi
    printf '%s  %s\n' "${APPIMAGE_TOOL_SHA256}" "${appimage_tool}" | sha256sum --check --status
    chmod +x "${appimage_tool}"
fi

echo "Building AppImage..."
ARCH="${APPIMAGE_ARCH}" "${appimage_tool}" --appimage-extract-and-run "${APPDIR}" "${APPIMAGE_ARTIFACT}"

if [[ ! -f "${APPIMAGE_ARTIFACT}" ]]; then
    echo "Error: appimagetool completed without creating ${APPIMAGE_ARTIFACT}." >&2
    exit 1
fi
chmod +x "${APPIMAGE_ARTIFACT}"

echo
echo "Build complete: ${ARTIFACT}"
echo "AppImage: ${APPIMAGE_ARTIFACT}"
echo "Launcher icon: ${DIST_DIR}/timelapse.png"
echo "The executable contains Python, Qt, and all runtime dependencies."
echo "Build on the oldest Linux distribution you intend to support for the widest glibc compatibility."

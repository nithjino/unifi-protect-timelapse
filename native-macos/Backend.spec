# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project_root = Path(SPECPATH).parent
target_arch = os.environ.get("TIMELAPSE_BUILD_ARCH") or None
codesign_identity = os.environ.get("MACOS_SIGN_IDENTITY") or None

hidden_imports = [
    *collect_submodules("uiprotect.data"),
    *collect_submodules("uiprotect.devices"),
    *collect_submodules("uiprotect.events"),
]

analysis = Analysis(
    [str(project_root / "timelapse" / "native_backend.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6", "shiboken6"],
    noarchive=False,
    optimize=0,
)

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="timelapse-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=codesign_identity,
    entitlements_file=None,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TimeLapseBackend",
)

app = BUNDLE(
    collection,
    name="TimeLapseBackend.app",
    icon=None,
    bundle_identifier="io.timelapse.desktop.backend",
    info_plist={
        "CFBundleDisplayName": "TimeLapse Backend",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSBackgroundOnly": True,
    },
    target_arch=target_arch,
    codesign_identity=codesign_identity,
)

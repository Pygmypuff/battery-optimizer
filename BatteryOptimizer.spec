# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Battery Optimizer GUI. Built on a macos-latest
GitHub Actions runner (see .github/workflows/build-macos.yml) — PyInstaller
can't cross-compile, it has to actually run on the target OS. The same spec
also works for a local Windows/Linux build (`pyinstaller BatteryOptimizer.spec`)
for testing; the macOS-only BUNDLE() step below is simply skipped elsewhere.

excel_template.xlsx is bundled as a read-only data file at the app's root;
app_paths.bundle_dir() is how the app finds it at runtime (`sys._MEIPASS`
once frozen, since `__file__` doesn't point anywhere real for a module
loaded out of PyInstaller's bundled archive). Everything the app *writes*
(config.json, generated output workbooks) intentionally lives outside the
bundle entirely — see app_paths.py — so replacing this whole .app on a
future update never touches it.
"""

from PyInstaller.utils.hooks import copy_metadata

datas = [("excel_template.xlsx", ".")]
binaries = []
hiddenimports = []

# `nordpool` (and some other packages) call importlib.metadata.version() on
# themselves at import time, which needs their installed .dist-info/
# METADATA to actually be present in the build — PyInstaller doesn't bundle
# that by default, so without this the frozen app crashes on startup with
# importlib.metadata.PackageNotFoundError.
for pkg in ("nordpool",):
    datas += copy_metadata(pkg)

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BatteryOptimizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX compresses the many large compiled scipy/numpy extensions, which
    # trades a smaller file for slower (sometimes much slower, or flaky)
    # startup as each one has to be decompressed before it can be loaded.
    # Not worth it for this app.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    # UPX compresses the many large compiled scipy/numpy extensions, which
    # trades a smaller file for slower (sometimes much slower, or flaky)
    # startup as each one has to be decompressed before it can be loaded.
    # Not worth it for this app.
    upx=False,
    upx_exclude=[],
    name="BatteryOptimizer",
)

app = BUNDLE(
    coll,
    name="BatteryOptimizer.app",
    icon=None,
    bundle_identifier="com.pygmypuff.batteryoptimizer",
    info_plist={
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
    },
)

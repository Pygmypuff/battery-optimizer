"""
app_paths.py
============
OS-appropriate, update-safe locations for anything the app writes at
runtime (saved settings, generated output workbooks). These live OUTSIDE
wherever the app's own code/bundle is installed, so replacing the whole
app on update — e.g. dragging a new BatteryOptimizer.app over the old one
on macOS — never touches them. Before this, config.json and gui_output/
were computed relative to `__file__`, which lives *inside* the app bundle
once packaged: every update would have silently wiped the user's saved
config and output history along with the old bundle.

  config_dir() — settings (config.json). Hidden, OS-managed:
                   macOS   ~/Library/Application Support/BatteryOptimizer
                   Windows %LOCALAPPDATA%/BatteryOptimizer
                   Linux   ~/.config/BatteryOptimizer

  output_dir() — generated schedules/workbooks. Deliberately under the
                 user's Documents folder rather than the hidden config
                 dir, since these are files people browse/open/share:
                   <Documents>/BatteryOptimizer Output

  bundle_dir() — the OPPOSITE kind of path: where the app's own bundled,
                 READ-ONLY resources live (currently just excel_template.
                 xlsx). Running from source, that's this file's own
                 directory. Once frozen by PyInstaller, `__file__` no
                 longer points anywhere real (frozen modules are loaded out
                 of an in-memory archive), so bundled data files have to be
                 found via `sys._MEIPASS` instead — set by PyInstaller's
                 bootloader for both --onefile and --onedir/.app builds.
"""

from __future__ import annotations

import sys
from pathlib import Path

from platformdirs import PlatformDirs

_APP_NAME = "BatteryOptimizer"
_dirs = PlatformDirs(appname=_APP_NAME, appauthor=False)


def config_dir() -> Path:
    path = Path(_dirs.user_config_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_dir() -> Path:
    path = Path(_dirs.user_documents_dir) / f"{_APP_NAME} Output"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundle_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        return Path(meipass)
    return Path(__file__).resolve().parent

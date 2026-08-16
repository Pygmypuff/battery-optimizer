"""
gui/update_check.py
====================
Fire-and-forget check against GitHub's "latest release" API, run on a
background QThread so it can never delay app startup. Every failure mode
(offline, DNS failure, timeout, rate limiting, a malformed response) is
treated the same as "no update available" rather than raised or shown to
the user — being unable to reach GitHub must never make the app feel broken
or block it from launching, especially used offline at the station.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.error import URLError

from PySide6.QtCore import QThread, Signal

from version import __version__

REPO = "Pygmypuff/battery-optimizer"
RELEASES_API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
_TIMEOUT_SECONDS = 4
_DEV_VERSION = "0.0.0-dev"


def _parse_version(tag: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3). Non-numeric segments become -1 so an odd tag compares as older instead of raising."""
    parts = tag.strip().lstrip("vV").split(".")
    parsed = []
    for part in parts:
        digits = "".join(ch for ch in part if ch.isdigit())
        parsed.append(int(digits) if digits else -1)
    return tuple(parsed)


def is_newer(latest_tag: str, current: str) -> bool:
    return _parse_version(latest_tag) > _parse_version(current)


class UpdateCheckWorker(QThread):
    """Checks GitHub once for a release newer than `version.__version__`.

    Emits `update_available(tag_name, html_url)` only when one is found and
    reachable; emits nothing at all on any failure or when already current.
    """

    update_available = Signal(str, str)

    def run(self) -> None:
        if __version__ == _DEV_VERSION:
            return  # running from source / an untagged build — nothing to compare against

        try:
            request = urllib.request.Request(
                RELEASES_API_URL,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "BatteryOptimizer-update-check"},
            )
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                payload = json.load(response)
        except (URLError, TimeoutError, OSError, ValueError):
            return  # unreachable, rate-limited, or an unparseable response — stay silent

        tag_name = payload.get("tag_name")
        html_url = payload.get("html_url")
        if not tag_name or not html_url:
            return
        if is_newer(tag_name, __version__):
            self.update_available.emit(tag_name, html_url)

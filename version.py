"""
version.py
==========
Single source of truth for the app's version number. The default below is
what you get running from source (`python app.py`) or from an untagged
`workflow_dispatch` build. A real release build overwrites the string here
during CI — see the "Stamp version from tag" step in
.github/workflows/build-macos.yml, which runs only when the workflow was
triggered by a `v*` tag push — so the frozen app's Info.plist
(BatteryOptimizer.spec) and its update checker (gui/update_check.py) both
report the actual released version.
"""

__version__ = "0.0.0-dev"

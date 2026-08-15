"""
app.py
======
Entry point for the battery optimizer GUI. Run with:

    python app.py

`--selftest` skips the window entirely and checks whether the exact MILP
solver came up (as opposed to battery_optimizer.py's silent fallback to
its greedy heuristic) and whether the bundled excel_template.xlsx was
found — a quick way to sanity-check a freshly built .app/.exe from the
command line, exiting non-zero if either check fails, so CI can gate a
build on it without needing to interact with a GUI.
"""

import sys

if __name__ == "__main__" and "--selftest" in sys.argv:
    import battery_optimizer
    from app_paths import bundle_dir

    has_milp = battery_optimizer._HAS_MILP
    template_found = (bundle_dir() / "excel_template.xlsx").exists()
    print("HAS_MILP:", has_milp)
    print("template exists:", template_found)
    sys.exit(0 if (has_milp and template_found) else 1)

from gui.main_window import main

if __name__ == "__main__":
    main()

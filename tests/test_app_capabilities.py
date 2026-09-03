"""the shell's capability file, pinned (app/src-tauri/capabilities/default.json).

tauri's permission system refuses silently: a `data-tauri-drag-region`
without `core:window:allow-start-dragging` is simply a div that does
nothing - which is exactly how the deer window shipped in the first boxed
build (undecorated, always-on-top, and immovable over the link field).
`core:default` does NOT include that permission, so it must be listed by
hand, and this test keeps it listed. same for the window-state plugin's
permission: without it the remembered-position feature is inert.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPABILITY = ROOT / "app" / "src-tauri" / "capabilities" / "default.json"


def _capability() -> dict:
    return json.loads(CAPABILITY.read_text())


def test_deer_window_is_draggable():
    cap = _capability()
    assert "deer" in cap["windows"]
    assert "core:window:allow-start-dragging" in cap["permissions"], (
        "the deer is undecorated - without start-dragging she can't be moved"
    )


def test_window_positions_are_remembered():
    cap = _capability()
    assert "main" in cap["windows"] and "deer" in cap["windows"]
    assert "window-state:default" in cap["permissions"]


def test_capability_covers_both_windows_only():
    # the capability is scoped to exactly the two windows the shell opens;
    # a new window label must opt in deliberately
    assert sorted(_capability()["windows"]) == ["deer", "main"]


# the companion window's own controls (docs/FOCUS_DOGFOOD_DESIGN.md §11.2):
# minimize and close both HIDE (the clock is the sidecar's), always-on-top
# is a toggle, and the den <-> bar form switch resizes and moves the window
# from the webview. every one of these is a separate permission that
# core:default leaves out, and tauri refuses each silently.
COMPANION_CONTROLS = [
    "core:window:allow-hide",
    "core:window:allow-show",
    "core:window:allow-set-always-on-top",
    "core:window:allow-is-always-on-top",
    "core:window:allow-set-size",
    "core:window:allow-set-position",
    "core:window:allow-outer-position",
    "core:window:allow-scale-factor",
]


def test_companion_window_controls_are_permitted():
    perms = _capability()["permissions"]
    missing = [p for p in COMPANION_CONTROLS if p not in perms]
    assert not missing, f"companion controls would be silently inert: {missing}"

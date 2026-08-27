"""the startup stamp: the log line that names which dainframe is running.

the dainframe is an unpinned path dependency, so this line is the only
record of the shipping combination - it must always produce, even on a
box where the sibling isn't a git checkout.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


def test_the_stamp_names_a_version_and_a_place():
    stamp = main._dainframe_stamp()
    assert stamp.startswith("dainframe ")
    assert "from /" in stamp


def test_the_stamp_survives_a_checkout_without_git(monkeypatch):
    def no_git(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", no_git)
    stamp = main._dainframe_stamp()
    assert stamp.startswith("dainframe ")
    assert "@" not in stamp

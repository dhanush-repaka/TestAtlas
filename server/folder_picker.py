"""Native "choose a folder" dialog, opened on the machine the server runs on.

A browser can't do this itself: the web platform deliberately never hands a
page the absolute path of a folder the user picks (`<input webkitdirectory>`
gives relative file paths, `showDirectoryPicker()` gives an opaque handle),
and a Local-folder repo needs a real absolute path the *server* can read. So
when TestAtlas is running on the same machine you're browsing from -- the
normal way to use Local folder repos -- the server opens the OS's own dialog
and returns the path you chose. Nothing about the folder's contents is read
here; this only ever returns a path string.

Deliberately shells out to each platform's own dialog rather than using a GUI
toolkit in-process: tkinter needs the main thread on macOS (uvicorn runs
sync endpoints in a worker thread), and isn't installed everywhere. A
subprocess has none of those constraints, and killing it on timeout also
closes its dialog.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

DEFAULT_TIMEOUT_SECONDS = 300  # a person picking a folder, not a hung process

_MAC_SCRIPT = (
    # `tell me to activate` brings osascript's own dialog to the front --
    # launched from a background server it otherwise tends to open behind the
    # browser window. Targets osascript itself, so no Automation permission
    # prompt (unlike activating System Events or Finder).
    'tell me to activate\n'
    'POSIX path of (choose folder with prompt "Select a folder for TestAtlas to analyze")'
)

_WINDOWS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
    "$d.Description = 'Select a folder for TestAtlas to analyze'; "
    "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
)


def _linux_command() -> list[str] | None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None  # headless -- no display for a dialog to appear on
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--directory", "--title=Select a folder for TestAtlas to analyze"]
    if shutil.which("kdialog"):
        return ["kdialog", "--getexistingdirectory", os.path.expanduser("~")]
    return None


def _command() -> list[str] | None:
    if sys.platform == "darwin":
        return ["osascript", "-e", _MAC_SCRIPT] if shutil.which("osascript") else None
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-STA", "-Command", _WINDOWS_SCRIPT] if shutil.which("powershell") else None
    return _linux_command()


def is_available() -> bool:
    """True if this machine can show a folder dialog at all (has a GUI and
    the tool to drive it). Says nothing about whether the *caller* is
    entitled to one -- see server/app.py for the same-machine check."""
    return _command() is not None


def pick_folder(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str | None:
    """Opens the dialog and blocks until the user chooses. Returns the
    absolute path, or None if they cancelled. Raises RuntimeError (message is
    safe to show the user) if no dialog could be shown, it failed, or it sat
    open past `timeout`."""
    cmd = _command()
    if cmd is None:
        raise RuntimeError("No folder dialog is available on this machine (no GUI, or its dialog tool isn't installed).")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("The folder dialog was left open too long and was closed -- click Browse to try again.") from e
    except OSError as e:
        raise RuntimeError(f"Couldn't open the folder dialog: {e}") from e

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        # osascript reports Cancel as error -128; zenity/kdialog as exit code 1
        # with nothing on stderr. Both mean "the user chose not to pick one".
        if "-128" in detail or "canceled" in detail.lower() or (proc.returncode == 1 and not detail):
            return None
        raise RuntimeError(f"The folder dialog failed: {detail or f'exit code {proc.returncode}'}")

    path = proc.stdout.strip()
    if not path:
        return None  # Windows: dialog closed without OK prints nothing, exits 0
    # macOS returns "/Users/me/proj/" -- drop the trailing slash, but keep a bare "/" or "C:\".
    if len(path) > 1 and path.endswith(("/", "\\")) and not path.endswith(":\\"):
        path = path[:-1]
    return path

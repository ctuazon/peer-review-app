"""Create a Desktop shortcut for Peer Review App (no pywin32 required)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent.parent
LAUNCHER = APP_DIR / "launch.pyw"
ICON = APP_DIR / "assets" / "app.ico"
SHORTCUT_NAME = "Peer Review App.lnk"


def find_pythonw() -> Path:
    """Prefer pythonw so the shortcut opens without a console window."""
    for name in ("pythonw.exe", "python.exe"):
        found = shutil.which(name)
        if found:
            path = Path(found)
            # Prefer real installs over the WindowsApps stub.
            if "WindowsApps" in path.parts:
                continue
            return path

    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    if local.is_dir():
        candidates = sorted(local.glob("Python*/pythonw.exe"), reverse=True)
        if candidates:
            return candidates[0]
        candidates = sorted(local.glob("Python*/python.exe"), reverse=True)
        if candidates:
            return candidates[0]

    raise FileNotFoundError(
        "Python was not found. Install Python 3 and ensure it is on PATH, "
        "then run install.bat again."
    )


def desktop_dir() -> Path:
    try:
        import ctypes
        from ctypes import wintypes

        CSIDL_DESKTOP = 0
        SHGFP_TYPE_CURRENT = 0
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        result = ctypes.windll.shell32.SHGetFolderPathW(
            None, CSIDL_DESKTOP, None, SHGFP_TYPE_CURRENT, buf
        )
        if result == 0 and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return Path.home() / "Desktop"


def create_shortcut(*, force: bool = True) -> Path:
    if not LAUNCHER.is_file():
        raise FileNotFoundError(f"Missing launcher: {LAUNCHER}")

    pythonw = find_pythonw()
    shortcut_path = desktop_dir() / SHORTCUT_NAME
    if shortcut_path.exists() and not force:
        return shortcut_path

    # Escape for PowerShell single-quoted strings ('' = literal ').
    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    ps = f"""
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut({ps_quote(str(shortcut_path))})
$s.TargetPath = {ps_quote(str(pythonw))}
$s.Arguments = {ps_quote(f'"{LAUNCHER}"')}
$s.WorkingDirectory = {ps_quote(str(APP_DIR))}
$s.WindowStyle = 1
$s.Description = 'Open Peer Review App'
$s.IconLocation = {ps_quote(f"{ICON},0" if ICON.is_file() else f"{pythonw},0")}
$s.Save()
Write-Output $s.FullName
"""
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or "Failed to create Desktop shortcut.")
    if not shortcut_path.exists():
        raise RuntimeError("Shortcut was not created.")
    return shortcut_path


def main() -> int:
    try:
        path = create_shortcut(force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Desktop shortcut created:\n  {path}")
    print(f"Launcher:\n  {LAUNCHER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

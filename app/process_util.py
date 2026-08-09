"""Subprocess helpers that avoid flashing/leaving console windows on Windows."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
SW_HIDE = 0


def no_window_kwargs() -> dict[str, Any]:
    """
    Extra kwargs for subprocess.run / Popen so console apps (wsl, git, etc.)
    do not open a visible terminal on the taskbar.
    Safe to use with capture_output / pipes — does not detach the process.
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": CREATE_NO_WINDOW,
    }


def start_hidden(command: list[str], *, cwd: str | None = None) -> None:
    """
    Fire-and-forget process with no visible console.
    Browser UIs launched by the child still appear normally.
    """
    kwargs: dict[str, Any] = {
        "cwd": cwd or os.getcwd(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = CREATE_NO_WINDOW | DETACHED_PROCESS
    subprocess.Popen(command, **kwargs)

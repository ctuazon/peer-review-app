"""Reliable launcher with visible errors (used by the Desktop shortcut)."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "launch_error.log"


def _show_error(message: str) -> None:
    LOG.write_text(message, encoding="utf-8")
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showerror("Peer Review App failed to start", message)
        root.destroy()
    except Exception:
        # Last resort for environments where Tk itself is broken.
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message[:1000], "Peer Review App failed", 0x10)


def main() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import main as app

        app.main()
        return 0
    except Exception:
        _show_error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

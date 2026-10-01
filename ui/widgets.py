"""Small reusable Tk widgets."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

ACCENT = "#0969da"
ACCENT_HOVER = "#0757b5"
ACCENT_DISABLED = "#8cb8ef"
TEXT = "#1f2328"
MUTED = "#57606a"
SOFT_BORDER = "#d0d7de"
TERMINAL_BG = "#0d1117"


def apply_theme(root: tk.Misc) -> None:
    """Register the named ttk styles the windows use."""
    style = ttk.Style(root)
    style.configure("Section.TLabel", font=("Segoe UI", 10, "bold"), foreground=TEXT)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Help.TLabel", foreground=ACCENT, font=("Segoe UI", 9, "bold"))
    style.configure("Statusbar.TFrame", padding=(10, 4))


def soften_text(widget: tk.Text) -> None:
    """Swap the sunken Text border for a thin flat outline."""
    widget.configure(
        relief=tk.FLAT,
        borderwidth=0,
        highlightthickness=1,
        highlightbackground=SOFT_BORDER,
        highlightcolor=ACCENT,
        padx=6,
        pady=4,
    )


def _parent_background(widget: tk.Misc) -> str:
    try:
        return ttk.Style(widget).lookup("TFrame", "background") or "SystemButtonFace"
    except tk.TclError:
        return "SystemButtonFace"


class StatusLight:
    """Small green/yellow/red indicator used for connection/auth checks."""

    COLORS = {
        "idle": ("#9ca3af", "#6b7280"),
        "checking": ("#fbbf24", "#d97706"),
        "ok": ("#22c55e", "#15803d"),
        "error": ("#ef4444", "#b91c1c"),
    }

    def __init__(self, parent: tk.Misc, size: int = 18) -> None:
        self.canvas = tk.Canvas(
            parent, width=size, height=size, highlightthickness=0, bd=0,
            background=_parent_background(parent),
        )
        self._oval = self.canvas.create_oval(
            2, 2, size - 2, size - 2, fill="#9ca3af", outline="#6b7280", width=1
        )

    def pack(self, **kwargs) -> None:
        self.canvas.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.canvas.grid(**kwargs)

    def set(self, state: str) -> None:
        fill, outline = self.COLORS.get(state, self.COLORS["idle"])
        self.canvas.itemconfigure(self._oval, fill=fill, outline=outline)


class Tooltip:
    """Hover tip. Shows after a short delay and wraps long text."""

    DELAY_MS = 450

    def __init__(self, widget: tk.Misc, text: str = "") -> None:
        self.widget = widget
        self.text = text or ""
        self._tip: tk.Toplevel | None = None
        self._after_id: str | None = None
        # add="+" so widgets with their own hover/click bindings keep them.
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set(self, text: str) -> None:
        self.text = text or ""

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        if not self.text or self._tip is not None:
            return
        try:
            self._tip = tk.Toplevel(self.widget)
        except tk.TclError:
            return
        self._tip.wm_overrideredirect(True)
        self._tip.attributes("-topmost", True)
        label = tk.Label(
            self._tip,
            text=self.text,
            justify=tk.LEFT,
            background="#1f2328",
            foreground="#ffffff",
            relief=tk.SOLID,
            borderwidth=1,
            padx=8,
            pady=4,
            wraplength=380,
            font=("Segoe UI", 9),
        )
        label.pack()
        # Keep the tip on screen when the widget sits near the right edge.
        self._tip.update_idletasks()
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        max_x = self.widget.winfo_screenwidth() - self._tip.winfo_width() - 8
        self._tip.wm_geometry(f"+{max(0, min(x, max_x))}+{y}")

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def tip(widget: tk.Misc, text: str) -> tk.Misc:
    """Attach a tooltip and return the widget, so it can wrap a constructor."""
    Tooltip(widget, text)
    return widget


class HelpIcon(ttk.Label):
    """Small "?" that reveals helper text on hover."""

    def __init__(self, master: tk.Misc, text: str) -> None:
        super().__init__(master, text="(?)", style="Help.TLabel", cursor="question_arrow")
        self.tooltip = Tooltip(self, text)


class PrimaryButton(tk.Button):
    """Solid accent button for the one main action on a screen."""

    def __init__(self, master: tk.Misc, text: str, command: Callable[[], None]) -> None:
        super().__init__(
            master,
            text=text,
            command=command,
            background=ACCENT,
            foreground="#ffffff",
            activebackground=ACCENT_HOVER,
            activeforeground="#ffffff",
            disabledforeground="#ffffff",
            relief=tk.FLAT,
            borderwidth=0,
            padx=16,
            pady=5,
            cursor="hand2",
            font=("Segoe UI", 10, "bold"),
        )
        self._enabled = True
        self.bind("<Enter>", lambda _e: self._enabled and tk.Button.configure(self, background=ACCENT_HOVER), add="+")
        self.bind("<Leave>", lambda _e: self._enabled and tk.Button.configure(self, background=ACCENT), add="+")

    def configure(self, cnf=None, **kw):  # noqa: ANN001, ANN201
        if "state" in kw:
            self._enabled = kw["state"] != tk.DISABLED
            kw["background"] = ACCENT if self._enabled else ACCENT_DISABLED
            kw["cursor"] = "hand2" if self._enabled else "arrow"
        return super().configure(cnf, **kw)

    config = configure


class LinkButton(tk.Label):
    """Borderless text action for low-emphasis controls."""

    def __init__(
        self,
        master: tk.Misc,
        command: Callable[[], None],
        text: str = "",
        textvariable: tk.StringVar | None = None,
    ) -> None:
        options = {"textvariable": textvariable} if textvariable is not None else {"text": text}
        super().__init__(
            master,
            **options,
            foreground=ACCENT,
            background=_parent_background(master),
            cursor="hand2",
            font=("Segoe UI", 9),
            padx=2,
        )
        self._command = command
        self._enabled = True
        self.bind("<Button-1>", self._click, add="+")
        self.bind("<Enter>", lambda _e: self._underline(True), add="+")
        self.bind("<Leave>", lambda _e: self._underline(False), add="+")

    def _underline(self, on: bool) -> None:
        if self._enabled:
            tk.Label.configure(self, font=("Segoe UI", 9, "underline" if on else "normal"))

    def _click(self, _event: object = None) -> None:
        if self._enabled:
            self._command()

    def configure(self, cnf=None, **kw):  # noqa: ANN001, ANN201
        if "state" in kw:
            self._enabled = kw.pop("state") != tk.DISABLED
            kw["foreground"] = ACCENT if self._enabled else "#a0a7b0"
            kw["cursor"] = "hand2" if self._enabled else "arrow"
        return super().configure(cnf, **kw)

    config = configure


class Section(ttk.Frame):
    """Borderless section: a bold heading row over a body frame.

    ``configure(text=...)`` renames the heading, matching the LabelFrame API
    the callers used before. Extra controls can go in ``header``."""

    def __init__(self, master: tk.Misc, title: str = "", **kw) -> None:
        super().__init__(master, **kw)
        self.title_var = tk.StringVar(value=title)
        self.header = ttk.Frame(self)
        self.header.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(self.header, textvariable=self.title_var, style="Section.TLabel").pack(side=tk.LEFT)
        self.body = ttk.Frame(self)
        self.body.pack(fill=tk.BOTH, expand=True)

    def configure(self, cnf=None, **kw):  # noqa: ANN001, ANN201
        if "text" in kw:
            self.title_var.set(kw.pop("text"))
            if not kw and not cnf:
                return None
        return super().configure(cnf, **kw)

    config = configure


class Collapsible(ttk.Frame):
    """Header link that shows or hides ``body``; ``summary_var`` sits beside it."""

    def __init__(self, master: tk.Misc, title: str, *, open_: bool = False) -> None:
        super().__init__(master)
        self._title = title
        self._open = open_
        header = ttk.Frame(self)
        header.pack(fill=tk.X)
        self._toggle_var = tk.StringVar()
        self.toggle_link = LinkButton(header, self.toggle, textvariable=self._toggle_var)
        self.toggle_link.pack(side=tk.LEFT)
        self.summary_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.summary_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=(8, 0))
        self.body = ttk.Frame(self, padding=(16, 6, 0, 0))
        self._render()

    def toggle(self) -> None:
        self._open = not self._open
        self._render()

    def _render(self) -> None:
        self._toggle_var.set(f"{'▾' if self._open else '▸'} {self._title}")
        if self._open:
            self.body.pack(fill=tk.X)
        else:
            self.body.pack_forget()


class MenuEntry:
    """Lets a menu item stand in for a button: ``configure(state=...)`` updates
    the entry and enables the owning menubutton while any entry is enabled."""

    def __init__(self, menu: tk.Menu, index: int, owner: ttk.Menubutton, siblings: list["MenuEntry"]) -> None:
        self.menu = menu
        self.index = index
        self.owner = owner
        self.enabled = True
        self._siblings = siblings
        siblings.append(self)

    def configure(self, state: str = tk.NORMAL, **_kw) -> None:
        self.enabled = state != tk.DISABLED
        self.menu.entryconfigure(self.index, state=tk.NORMAL if self.enabled else tk.DISABLED)
        any_on = any(e.enabled for e in self._siblings)
        self.owner.state(["!disabled"] if any_on else ["disabled"])

    config = configure


class ThinkingIndicator(ttk.Frame):
    """Animated spinner + label shown while a review is in progress."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._running = False
        self._angle = 0
        self._after_id: str | None = None
        self._phase = 0
        self._base_message = "Thinking"

        self.canvas = tk.Canvas(
            self, width=22, height=22, highlightthickness=0, bd=0,
            background=_parent_background(master),
        )
        self.canvas.pack(side=tk.LEFT)
        self.label_var = tk.StringVar(value="Thinking")
        self.label = ttk.Label(self, textvariable=self.label_var)
        self.label.pack(side=tk.LEFT, padx=(6, 0))
        self._draw()

    def _draw(self) -> None:
        self.canvas.delete("all")
        self.canvas.create_oval(3, 3, 19, 19, outline="#d0d7de", width=2)
        self.canvas.create_arc(
            3,
            3,
            19,
            19,
            start=self._angle,
            extent=-80,
            style=tk.ARC,
            outline="#0969da",
            width=2,
        )

    def start(self, message: str = "Thinking") -> None:
        self._base_message = message or "Thinking"
        self.label_var.set(self._base_message)
        self.pack(side=tk.LEFT, padx=(8, 0))
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self.pack_forget()

    def _tick(self) -> None:
        if not self._running:
            return
        self._angle = (self._angle + 24) % 360
        self._phase = (self._phase + 1) % 4
        dots = "." * (self._phase + 1)
        self.label_var.set(f"{self._base_message}{dots}")
        self._draw()
        self._after_id = self.after(90, self._tick)

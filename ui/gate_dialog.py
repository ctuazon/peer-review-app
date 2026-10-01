"""Pre-run gate: eligibility warnings and the cost estimate, with "Run anyway"
and the Laravel app's budget-fit downgrades as one-click options."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui.widgets import tip


class RunGateDialog(tk.Toplevel):
    """`choice` ends as "run", "cancel", or a dict of setting changes to apply
    before showing the gate again."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        warnings: list[str],
        estimate: str,
        over_budget: bool,
        steps: list[tuple[str, dict]],
    ) -> None:
        super().__init__(master)
        self.title("Before this review runs")
        self.transient(master)
        self.resizable(False, False)
        self.choice: object = "cancel"

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        if warnings:
            ttk.Label(frame, text="Heads up:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
            for warning in warnings:
                ttk.Label(frame, text=f"• {warning}", wraplength=520, justify=tk.LEFT).pack(anchor="w")
        ttk.Label(
            frame,
            text=f"Estimated cost: {estimate}",
            font=("Segoe UI", 10, "bold"),
            foreground="#cf222e" if over_budget else "#1f2328",
        ).pack(anchor="w", pady=(10 if warnings else 0, 0))
        if over_budget and steps:
            ttk.Label(frame, text="That's over your warning threshold. Cheaper options:", foreground="#57606a").pack(
                anchor="w", pady=(4, 2)
            )
            for label, changes in steps:
                tip(
                    ttk.Button(frame, text=label, command=lambda c=changes: self._pick(c)),
                    "Run the review with this cheaper setting instead.",
                ).pack(anchor="w", pady=1)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        tip(
            ttk.Button(buttons, text="Run anyway", command=lambda: self._pick("run")),
            "Start the review with the current settings despite the warnings above.",
        ).pack(side=tk.RIGHT, padx=6)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    def _pick(self, choice: object) -> None:
        self.choice = choice
        self.destroy()

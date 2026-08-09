#!/usr/bin/env python3
"""Peer Review App — desktop UI for GitHub PR reviews via Claude."""
from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

# Allow running as `python main.py` from the project folder.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import load_config, save_config
from app.auth_status_store import clear_auth_status, load_auth_status, save_auth_status
from app.diff_view import DiffReviewView, enable_selection_copy
from app.github_pr import parse_pr_url
from app.history_store import (
    HistoryEntry,
    add_history_entry,
    clear_history,
    delete_history_entry,
    get_history_entry,
    list_history,
)
from app.prompts_store import (
    GENERIC_REPO_TYPE,
    Prompt,
    PromptCycler,
    create_prompt,
    delete_prompt,
    list_prompts,
    move_prompt,
    move_prompt_to_index,
    update_prompt,
)
from app.review import format_copy_friendly, run_peer_review, run_pr_ask, run_pr_explanation


class StatusLight:
    """Small green/yellow/red indicator used for connection/auth checks."""

    COLORS = {
        "idle": ("#9ca3af", "#6b7280"),
        "checking": ("#fbbf24", "#d97706"),
        "ok": ("#22c55e", "#15803d"),
        "error": ("#ef4444", "#b91c1c"),
    }

    def __init__(self, parent: tk.Misc) -> None:
        self.canvas = tk.Canvas(parent, width=18, height=18, highlightthickness=0, bd=0)
        self._oval = self.canvas.create_oval(
            2, 2, 16, 16, fill="#9ca3af", outline="#6b7280", width=1
        )

    def pack(self, **kwargs) -> None:
        self.canvas.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.canvas.grid(**kwargs)

    def set(self, state: str) -> None:
        fill, outline = self.COLORS.get(state, self.COLORS["idle"])
        self.canvas.itemconfigure(self._oval, fill=fill, outline=outline)


class Tooltip:
    """Lightweight hover tip for compact status controls."""

    def __init__(self, widget: tk.Misc) -> None:
        self.widget = widget
        self.text = ""
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def set(self, text: str) -> None:
        self.text = text or ""

    def _show(self, _event: object = None) -> None:
        if not self.text or self._tip is not None:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.attributes("-topmost", True)
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip.wm_geometry(f"+{x}+{y}")
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
            font=("Segoe UI", 9),
        )
        label.pack()

    def _hide(self, _event: object = None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


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
            self, width=22, height=22, highlightthickness=0, bd=0
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


class PromptEditorDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, prompt: Prompt | None = None) -> None:
        super().__init__(master)
        self.title("Edit Prompt" if prompt else "New Prompt")
        self.resizable(True, True)
        self.transient(master)
        self.grab_set()
        self.result: Prompt | None = None
        self._prompt = prompt

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Name").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value=prompt.name if prompt else "")
        ttk.Entry(frame, textvariable=self.name_var, width=50).grid(
            row=0, column=1, sticky="ew", pady=4
        )

        ttk.Label(frame, text="Repo type").grid(row=1, column=0, sticky="w")
        self.repo_var = tk.StringVar(
            value=prompt.repo_type if prompt else GENERIC_REPO_TYPE
        )
        repo_row = ttk.Frame(frame)
        repo_row.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Entry(repo_row, textvariable=self.repo_var, width=30).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Label(repo_row, text="(e.g. marketplace-ssr, api, generic)").pack(
            side=tk.LEFT, padx=8
        )

        self.generic_var = tk.BooleanVar(
            value=prompt.is_generic if prompt else True
        )
        ttk.Checkbutton(
            frame,
            text="Generic reviewer (applies to all repos)",
            variable=self.generic_var,
            command=self._on_generic_toggle,
        ).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Prompt").grid(row=3, column=0, sticky="nw")
        self.content = scrolledtext.ScrolledText(frame, width=70, height=18, wrap=tk.WORD)
        self.content.grid(row=3, column=1, sticky="nsew", pady=4)
        if prompt:
            self.content.insert("1.0", prompt.content)

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=1, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(buttons, text="Save", command=self._save).pack(side=tk.RIGHT)

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)
        self._on_generic_toggle()
        self.geometry("720x520")
        self.wait_visibility()
        self.focus_force()

    def _on_generic_toggle(self) -> None:
        if self.generic_var.get():
            self.repo_var.set(GENERIC_REPO_TYPE)

    def _save(self) -> None:
        name = self.name_var.get().strip()
        content = self.content.get("1.0", "end-1c")
        is_generic = bool(self.generic_var.get())
        repo_type = GENERIC_REPO_TYPE if is_generic else self.repo_var.get().strip()
        if not name:
            messagebox.showerror("Missing name", "Please enter a prompt name.", parent=self)
            return
        if not repo_type:
            messagebox.showerror(
                "Missing repo type",
                "Please enter a repo type, or mark the prompt as generic.",
                parent=self,
            )
            return
        try:
            if self._prompt:
                self.result = update_prompt(
                    self._prompt.id,
                    name=name,
                    repo_type=repo_type,
                    content=content,
                    is_generic=is_generic,
                )
            else:
                self.result = create_prompt(
                    name=name,
                    repo_type=repo_type,
                    content=content,
                    is_generic=is_generic,
                )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save failed", str(exc), parent=self)
            return
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("Settings")
        self.resizable(True, True)
        self.transient(master)
        self.grab_set()
        self.config_data = load_config()

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        auth = ttk.LabelFrame(frame, text="Sign in (browser / SSO)", padding=8)
        auth.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        gh_row = ttk.Frame(auth)
        gh_row.grid(row=0, column=0, columnspan=2, sticky="w")
        self.github_light = StatusLight(gh_row)
        self.github_light.pack(side=tk.LEFT)
        self.github_status_var = tk.StringVar(value="GitHub: checking…")
        ttk.Label(gh_row, textvariable=self.github_status_var).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        cl_row = ttk.Frame(auth)
        cl_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 8))
        self.claude_light = StatusLight(cl_row)
        self.claude_light.pack(side=tk.LEFT)
        self.claude_status_var = tk.StringVar(value="Claude: checking…")
        ttk.Label(cl_row, textvariable=self.claude_status_var).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        ttk.Button(
            auth,
            text="Login with GitHub (browser)",
            command=self._github_login,
        ).grid(row=2, column=0, sticky="w", padx=(0, 8))
        ttk.Button(
            auth,
            text="Login with Claude SSO",
            command=self._claude_login,
        ).grid(row=2, column=1, sticky="w")
        ttk.Button(auth, text="Refresh status", command=self._refresh_status).grid(
            row=3, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Button(auth, text="Clear all auth", command=self._clear_all_auth).grid(
            row=3, column=1, sticky="w", pady=(8, 0)
        )

        ttk.Label(frame, text="GitHub token (optional)").grid(row=1, column=0, sticky="w")
        self.token_var = tk.StringVar(value=self.config_data.get("github_token", ""))
        ttk.Entry(frame, textvariable=self.token_var, width=56, show="*").grid(
            row=1, column=1, pady=4, sticky="ew"
        )

        self.wsl_auth_var = tk.BooleanVar(
            value=bool(self.config_data.get("use_wsl_github_auth", True))
        )
        ttk.Checkbutton(
            frame,
            text="Use WSL / Windows saved GitHub credentials when token is blank",
            variable=self.wsl_auth_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Label(frame, text="Claude mode").grid(row=3, column=0, sticky="w")
        self.mode_var = tk.StringVar(value=self.config_data.get("claude_mode", "cli"))
        mode = ttk.Combobox(
            frame,
            textvariable=self.mode_var,
            values=["cli", "wsl", "api"],
            state="readonly",
            width=12,
        )
        mode.grid(row=3, column=1, sticky="w", pady=4)

        self.wsl_claude_var = tk.BooleanVar(
            value=bool(self.config_data.get("use_wsl_claude", True))
        )
        ttk.Checkbutton(
            frame,
            text="Prefer WSL Claude Code (uses your SSO session)",
            variable=self.wsl_claude_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Label(frame, text="Claude SSO email").grid(row=5, column=0, sticky="w")
        self.email_var = tk.StringVar(
            value=self.config_data.get(
                "claude_sso_email", "czyrus.tuazon@traderinteractive.com"
            )
        )
        ttk.Entry(frame, textvariable=self.email_var, width=56).grid(
            row=5, column=1, pady=4, sticky="ew"
        )

        ttk.Label(frame, text="Claude CLI path").grid(row=6, column=0, sticky="w")
        self.cli_var = tk.StringVar(value=self.config_data.get("claude_cli_path", "claude"))
        ttk.Entry(frame, textvariable=self.cli_var, width=56).grid(
            row=6, column=1, pady=4, sticky="ew"
        )

        ttk.Label(frame, text="Anthropic API key").grid(row=7, column=0, sticky="w")
        self.api_var = tk.StringVar(value=self.config_data.get("anthropic_api_key", ""))
        ttk.Entry(frame, textvariable=self.api_var, width=56, show="*").grid(
            row=7, column=1, pady=4, sticky="ew"
        )

        ttk.Label(frame, text="Claude model (API)").grid(row=8, column=0, sticky="w")
        self.model_var = tk.StringVar(
            value=self.config_data.get("claude_model", "claude-sonnet-4-20250514")
        )
        ttk.Entry(frame, textvariable=self.model_var, width=56).grid(
            row=8, column=1, pady=4, sticky="ew"
        )

        help_text = (
            "GitHub browser login uses Git Credential Manager (opens a webpage only).\n"
            "Claude SSO login opens the browser via WSL — no extra terminal windows.\n"
            "Token/API key fields are optional fallbacks.\n"
            "Auth light status is saved and restored the next time you open the app."
        )
        ttk.Label(frame, text=help_text, justify=tk.LEFT).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=10, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(buttons, text="Save", command=self._save).pack(side=tk.RIGHT)

        frame.columnconfigure(1, weight=1)
        self.geometry("720x520")
        self._restore_cached_auth_status()
        self.after(100, self._refresh_status)

    def _restore_cached_auth_status(self) -> None:
        cached = load_auth_status()
        gh = cached.get("github") or {}
        cl = cached.get("claude") or {}
        if gh.get("ok") is not None:
            self.github_light.set("ok" if gh.get("ok") else "error")
            detail = gh.get("detail") or ""
            self.github_status_var.set(
                f"GitHub: signed in — {detail}" if gh.get("ok") else f"GitHub: {detail}"
            )
        else:
            self.github_light.set("idle")
            self.github_status_var.set("GitHub: not checked yet")
        if cl.get("ok") is not None:
            self.claude_light.set("ok" if cl.get("ok") else "error")
            detail = cl.get("detail") or ""
            self.claude_status_var.set(
                f"Claude: signed in as {detail}" if cl.get("ok") else f"Claude: {detail}"
            )
        else:
            self.claude_light.set("idle")
            self.claude_status_var.set("Claude: not checked yet")

    def _refresh_status(self) -> None:
        self.github_light.set("checking")
        self.claude_light.set("checking")
        self.github_status_var.set("GitHub: checking…")
        self.claude_status_var.set("Claude: checking…")

        def worker() -> None:
            from app.auth_flows import check_claude_auth, check_github_auth

            gh_ok, gh_detail = check_github_auth()
            cl_ok, cl_detail = check_claude_auth()
            self.after(
                0,
                lambda: self._set_status(gh_ok, gh_detail, cl_ok, cl_detail),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _set_status(
        self, gh_ok: bool, gh_detail: str, cl_ok: bool, cl_detail: str
    ) -> None:
        self.github_light.set("ok" if gh_ok else "error")
        self.claude_light.set("ok" if cl_ok else "error")
        self.github_status_var.set(
            f"GitHub: signed in — {gh_detail}" if gh_ok else f"GitHub: {gh_detail}"
        )
        self.claude_status_var.set(
            f"Claude: signed in as {cl_detail}" if cl_ok else f"Claude: {cl_detail}"
        )
        save_auth_status(
            github_ok=gh_ok,
            github_detail=gh_detail,
            claude_ok=cl_ok,
            claude_detail=cl_detail,
        )
        # Keep main window lights in sync if it exists.
        master = self.master
        if hasattr(master, "_apply_auth_lights"):
            master._apply_auth_lights(gh_ok, gh_detail, cl_ok, cl_detail)

    def _clear_all_auth(self) -> None:
        if not messagebox.askyesno(
            "Clear all auth",
            "Sign out of GitHub and Claude for this app?\n\n"
            "This clears WSL/Windows GitHub credentials and the Claude SSO session.",
            parent=self,
        ):
            return
        from app.auth_flows import clear_all_auth

        # Also drop any token stored in app settings.
        self.token_var.set("")
        self.api_var.set("")
        notes = clear_all_auth()
        clear_auth_status()
        cfg = load_config()
        cfg["github_token"] = ""
        cfg["anthropic_api_key"] = ""
        save_config(cfg)
        self._set_status(False, "cleared", False, "cleared")
        messagebox.showinfo("Auth cleared", notes, parent=self)

    def _github_login(self) -> None:
        from app.auth_flows import start_github_web_login

        self.github_light.set("checking")
        try:
            message = start_github_web_login(force=True)
        except Exception as exc:  # noqa: BLE001
            self.github_light.set("error")
            messagebox.showerror("GitHub login", str(exc), parent=self)
            return
        messagebox.showinfo("GitHub login", message, parent=self)
        self.after(4000, self._refresh_status)

    def _claude_login(self) -> None:
        from app.auth_flows import start_claude_sso_login

        self.claude_light.set("checking")
        try:
            message = start_claude_sso_login(email=self.email_var.get().strip())
        except Exception as exc:  # noqa: BLE001
            self.claude_light.set("error")
            messagebox.showerror("Claude SSO login", str(exc), parent=self)
            return
        messagebox.showinfo("Claude SSO login", message, parent=self)
        self.after(4000, self._refresh_status)

    def _save(self) -> None:
        save_config(
            {
                "github_token": self.token_var.get().strip(),
                "use_wsl_github_auth": bool(self.wsl_auth_var.get()),
                "use_wsl_claude": bool(self.wsl_claude_var.get()),
                "claude_mode": self.mode_var.get().strip() or "cli",
                "claude_cli_path": self.cli_var.get().strip() or "claude",
                "claude_sso_email": self.email_var.get().strip(),
                "anthropic_api_key": self.api_var.get().strip(),
                "claude_model": self.model_var.get().strip()
                or "claude-sonnet-4-20250514",
            }
        )
        self.destroy()


class PeerReviewApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Peer Review App")
        self.geometry("1100x780")
        self.minsize(900, 640)
        self._apply_app_icon()

        self.cycler = PromptCycler()
        self._busy = False
        self._pending_history: dict | None = None
        self.notebook: ttk.Notebook | None = None

        self._build_menu()
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.notebook = notebook

        self.review_tab = ttk.Frame(notebook, padding=8)
        self.prompts_tab = ttk.Frame(notebook, padding=8)
        self.history_tab = ttk.Frame(notebook, padding=8)
        notebook.add(self.review_tab, text="Review")
        notebook.add(self.prompts_tab, text="Prompts")
        notebook.add(self.history_tab, text="History")

        self._build_review_tab()
        self._build_prompts_tab()
        self._build_history_tab()
        self.refresh_prompts_ui()
        self.refresh_history_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _apply_app_icon(self) -> None:
        icon_png = Path(__file__).resolve().parent / "assets" / "app.png"
        icon_ico = icon_png.with_suffix(".ico")
        try:
            if icon_png.is_file():
                self._app_icon = tk.PhotoImage(file=str(icon_png))
                self.iconphoto(True, self._app_icon)
            elif icon_ico.is_file():
                self.iconbitmap(default=str(icon_ico))
        except tk.TclError:
            pass

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Settings…", command=self.open_settings)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        self.config(menu=menubar)

    def _build_review_tab(self) -> None:
        tab = self.review_tab

        top = ttk.LabelFrame(tab, text="Pull request", padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="PR link").grid(row=0, column=0, sticky="w")
        self.pr_url_var = tk.StringVar()
        pr_entry = ttk.Entry(top, textvariable=self.pr_url_var)
        pr_entry.grid(row=0, column=1, sticky="ew", padx=6)
        detect_row = ttk.Frame(top)
        detect_row.grid(row=0, column=2, sticky="e")
        ttk.Button(detect_row, text="Detect repo", command=self.detect_repo).pack(
            side=tk.LEFT
        )
        self.repo_light = StatusLight(detect_row)
        self.repo_light.pack(side=tk.LEFT, padx=(8, 0))
        self.repo_light_status_var = tk.StringVar(value="")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Detected repo").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.repo_label_var = tk.StringVar(value="—")
        repo_status = ttk.Frame(top)
        repo_status.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(repo_status, textvariable=self.repo_label_var).pack(side=tk.LEFT)
        ttk.Label(repo_status, textvariable=self.repo_light_status_var).pack(
            side=tk.LEFT, padx=(10, 0)
        )

        prompt_box = ttk.LabelFrame(tab, text="Reviewer prompt", padding=8)
        prompt_box.pack(fill=tk.X, pady=(8, 0))

        self.prompt_label_var = tk.StringVar(value="No prompts yet")
        ttk.Label(prompt_box, textvariable=self.prompt_label_var).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )

        btns = ttk.Frame(prompt_box)
        btns.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(btns, text="◀ Prev", command=self.cycle_prev).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btns, text="Next ▶", command=self.cycle_next).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Refresh list", command=self.refresh_prompts_ui).pack(
            side=tk.LEFT, padx=4
        )

        self.prompt_preview = scrolledtext.ScrolledText(
            prompt_box, height=6, wrap=tk.WORD, state=tk.DISABLED
        )
        self.prompt_preview.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        prompt_box.columnconfigure(0, weight=1)

        story_box = ttk.LabelFrame(
            tab, text="Story / explanation / custom prompt", padding=8
        )
        story_box.pack(fill=tk.X, pady=(8, 0))
        self.story_text = scrolledtext.ScrolledText(story_box, height=4, wrap=tk.WORD)
        self.story_text.pack(fill=tk.BOTH, expand=True)

        # Primary actions — keep this row sparse.
        action_row = ttk.Frame(tab)
        action_row.pack(fill=tk.X, pady=(10, 2))
        self.run_btn = ttk.Button(
            action_row, text="Run peer review", command=self.start_review
        )
        self.run_btn.pack(side=tk.LEFT)
        self.explain_btn = ttk.Button(
            action_row, text="Explain PR", command=self.start_explain
        )
        self.explain_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.ask_btn = ttk.Button(
            action_row, text="Ask Claude", command=self.start_ask
        )
        self.ask_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(action_row, textvariable=self.status_var).pack(side=tk.LEFT, padx=12)
        self.thinking = ThinkingIndicator(action_row)
        # Hidden until a review starts.
        self.copy_llm_btn = ttk.Button(
            action_row,
            text="Copy as LLM prompt",
            command=self.copy_as_llm_prompt,
            state=tk.DISABLED,
        )
        self.copy_llm_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self.copy_all_btn = ttk.Button(
            action_row, text="Copy all", command=self.copy_all, state=tk.DISABLED
        )
        self.copy_all_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self.copy_selected_btn = ttk.Button(
            action_row,
            text="Copy selected",
            command=self.copy_selected,
            state=tk.DISABLED,
        )
        self.copy_selected_btn.pack(side=tk.RIGHT, padx=(0, 6))

        # Compact auth strip — lights + short labels; details on hover.
        auth_row = ttk.Frame(tab)
        auth_row.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(auth_row, text="Auth").pack(side=tk.LEFT)

        gh_chip = ttk.Frame(auth_row)
        gh_chip.pack(side=tk.LEFT, padx=(10, 0))
        self.github_auth_light = StatusLight(gh_chip)
        self.github_auth_light.pack(side=tk.LEFT)
        self.github_auth_label_var = tk.StringVar(value="GitHub")
        gh_btn = ttk.Button(
            gh_chip, textvariable=self.github_auth_label_var, command=self.quick_github_login, width=10
        )
        gh_btn.pack(side=tk.LEFT, padx=(4, 0))
        self.github_tooltip = Tooltip(gh_btn)
        self.github_tooltip.set("Click to sign in with GitHub")

        cl_chip = ttk.Frame(auth_row)
        cl_chip.pack(side=tk.LEFT, padx=(8, 0))
        self.claude_auth_light = StatusLight(cl_chip)
        self.claude_auth_light.pack(side=tk.LEFT)
        self.claude_auth_label_var = tk.StringVar(value="Claude")
        cl_btn = ttk.Button(
            cl_chip,
            textvariable=self.claude_auth_label_var,
            command=self.quick_claude_login,
            width=10,
        )
        cl_btn.pack(side=tk.LEFT, padx=(4, 0))
        self.claude_tooltip = Tooltip(cl_btn)
        self.claude_tooltip.set("Click to sign in with Claude SSO")

        ttk.Button(
            auth_row, text="Refresh", command=self.refresh_auth_lights, width=8
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(
            auth_row, text="Clear auth", command=self.clear_all_auths, width=10
        ).pack(side=tk.LEFT, padx=(6, 0))

        # Kept for compatibility with older restore/update helpers.
        self.auth_status_var = tk.StringVar(value="")

        output_box = ttk.LabelFrame(
            tab,
            text="Output",
            padding=8,
        )
        output_box.pack(fill=tk.BOTH, expand=True)
        self.output_box = output_box

        # Plain frame — title comes from output_box via _show_panel (avoid nested labels).
        self.live_frame = ttk.Frame(output_box)
        # Shown only while a review is running.
        self.live_text = scrolledtext.ScrolledText(
            self.live_frame,
            height=10,
            wrap=tk.WORD,
            font=("Consolas", 9),
            background="#0d1117",
            foreground="#c9d1d9",
            insertbackground="#c9d1d9",
        )
        self.live_text.pack(fill=tk.BOTH, expand=True)
        self.live_text.tag_configure("status", foreground="#8b949e", font=("Consolas", 9, "italic"))
        self.live_text.tag_configure("thinking", foreground="#d2a8ff")
        self.live_text.tag_configure("text", foreground="#7ee787")
        self.live_text.configure(state=tk.DISABLED)

        self.explain_frame = ttk.Frame(output_box)
        self.explain_text = scrolledtext.ScrolledText(
            self.explain_frame,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
            background="#ffffff",
            foreground="#1f2328",
            exportselection=False,
            insertwidth=0,
            cursor="arrow",
        )
        self.explain_text.pack(fill=tk.BOTH, expand=True)
        enable_selection_copy(self.explain_text)

        self.diff_view = DiffReviewView(output_box)
        self.diff_view.pack(fill=tk.BOTH, expand=True)
        # Keep a hidden plain buffer for fallback copy of raw review text.
        self._last_review_raw = ""
        self._last_explanation = ""
        self._output_mode = "diff"  # diff | explain | live


        self.pr_url_var.trace_add("write", lambda *_: self.on_pr_url_changed())
        self._restore_cached_auth_lights()
        self.after(200, self.refresh_auth_lights)

    def _restore_cached_auth_lights(self) -> None:
        cached = load_auth_status()
        gh = cached.get("github") or {}
        cl = cached.get("claude") or {}
        if gh.get("ok") is None and cl.get("ok") is None:
            self.github_auth_light.set("idle")
            self.claude_auth_light.set("idle")
            self.github_auth_label_var.set("GitHub")
            self.claude_auth_label_var.set("Claude")
            self.github_tooltip.set("Not checked yet — click to sign in")
            self.claude_tooltip.set("Not checked yet — click to sign in")
            return
        self._apply_auth_lights(
            bool(gh.get("ok")) if gh.get("ok") is not None else False,
            gh.get("detail") or "not checked yet",
            bool(cl.get("ok")) if cl.get("ok") is not None else False,
            cl.get("detail") or "not checked yet",
            persist=False,
            github_known=gh.get("ok") is not None,
            claude_known=cl.get("ok") is not None,
        )

    def _build_prompts_tab(self) -> None:
        tab = self.prompts_tab

        toolbar = ttk.Frame(tab)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="New", command=self.new_prompt).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Edit", command=self.edit_prompt).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="Delete", command=self.delete_selected_prompt).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=2
        )
        ttk.Button(toolbar, text="Move up", command=self.move_prompt_up).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Move down", command=self.move_prompt_down).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(toolbar, text="Reload", command=self.refresh_prompts_ui).pack(
            side=tk.LEFT, padx=4
        )

        columns = ("name", "repo_type", "generic")
        self.prompt_tree = ttk.Treeview(
            tab, columns=columns, show="headings", selectmode="browse"
        )
        self.prompt_tree.heading("name", text="Name")
        self.prompt_tree.heading("repo_type", text="Repo type")
        self.prompt_tree.heading("generic", text="Generic")
        self.prompt_tree.column("name", width=280)
        self.prompt_tree.column("repo_type", width=180)
        self.prompt_tree.column("generic", width=80, anchor="center")
        self.prompt_tree.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.prompt_tree.bind("<Double-1>", lambda _e: self.edit_prompt())
        self._drag_prompt_id: str | None = None
        self._drag_target_id: str | None = None
        self.prompt_tree.bind("<ButtonPress-1>", self._on_prompt_drag_start)
        self.prompt_tree.bind("<B1-Motion>", self._on_prompt_drag_motion)
        self.prompt_tree.bind("<ButtonRelease-1>", self._on_prompt_drag_drop)
        # Visual cue while dragging over a row.
        try:
            self.prompt_tree.tag_configure("drop_target", background="#ddf4ff")
        except tk.TclError:
            pass

        preview_frame = ttk.LabelFrame(tab, text="Selected prompt", padding=8)
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.prompt_detail = scrolledtext.ScrolledText(
            preview_frame, height=10, wrap=tk.WORD, state=tk.DISABLED
        )
        self.prompt_detail.pack(fill=tk.BOTH, expand=True)
        self.prompt_tree.bind("<<TreeviewSelect>>", self.on_prompt_select)

    def _build_history_tab(self) -> None:
        tab = self.history_tab

        toolbar = ttk.Frame(tab)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="Revisit", command=self.revisit_history_entry).pack(
            side=tk.LEFT
        )
        ttk.Button(
            toolbar, text="Restore inputs only", command=self.restore_history_inputs
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="Delete", command=self.delete_selected_history).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(toolbar, text="Clear all", command=self.clear_all_history).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(toolbar, text="Refresh", command=self.refresh_history_ui).pack(
            side=tk.RIGHT
        )

        paned = ttk.Panedwindow(tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=2)

        list_box = ttk.LabelFrame(left, text="Past runs", padding=6)
        list_box.pack(fill=tk.BOTH, expand=True)
        self.history_list = tk.Listbox(list_box, exportselection=False, font=("Segoe UI", 9))
        hist_scroll = ttk.Scrollbar(
            list_box, orient=tk.VERTICAL, command=self.history_list.yview
        )
        self.history_list.configure(yscrollcommand=hist_scroll.set)
        self.history_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hist_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_list.bind("<<ListboxSelect>>", self.on_history_select)
        self.history_list.bind("<Double-Button-1>", lambda _e: self.revisit_history_entry())
        self._history_ids: list[str] = []

        detail = ttk.LabelFrame(right, text="Details", padding=6)
        detail.pack(fill=tk.BOTH, expand=True)
        self.history_meta_var = tk.StringVar(value="Select a past run to inspect it.")
        ttk.Label(detail, textvariable=self.history_meta_var, wraplength=520).pack(
            anchor="w", pady=(0, 6)
        )

        self.history_notebook = ttk.Notebook(detail)
        self.history_notebook.pack(fill=tk.BOTH, expand=True)

        story_tab = ttk.Frame(self.history_notebook, padding=4)
        prompt_tab = ttk.Frame(self.history_notebook, padding=4)
        result_tab = ttk.Frame(self.history_notebook, padding=4)
        self.history_notebook.add(story_tab, text="Story / custom prompt")
        self.history_notebook.add(prompt_tab, text="Reviewer prompt")
        self.history_notebook.add(result_tab, text="Result")

        self.history_story = scrolledtext.ScrolledText(
            story_tab, wrap=tk.WORD, state=tk.DISABLED, font=("Segoe UI", 10)
        )
        self.history_story.pack(fill=tk.BOTH, expand=True)
        self.history_prompt = scrolledtext.ScrolledText(
            prompt_tab, wrap=tk.WORD, state=tk.DISABLED, font=("Segoe UI", 10)
        )
        self.history_prompt.pack(fill=tk.BOTH, expand=True)
        self.history_result = scrolledtext.ScrolledText(
            result_tab, wrap=tk.WORD, state=tk.DISABLED, font=("Segoe UI", 10)
        )
        self.history_result.pack(fill=tk.BOTH, expand=True)

    def _set_history_text(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text or "")
        widget.configure(state=tk.DISABLED)

    def refresh_history_ui(self, *, select_id: str | None = None) -> None:
        keep = select_id or self._selected_history_id()
        self.history_list.delete(0, tk.END)
        self._history_ids = []
        for entry in list_history():
            self._history_ids.append(entry.id)
            self.history_list.insert(tk.END, entry.list_title())
        if keep and keep in self._history_ids:
            index = self._history_ids.index(keep)
            self.history_list.selection_clear(0, tk.END)
            self.history_list.selection_set(index)
            self.history_list.see(index)
            self.on_history_select()
        elif not self._history_ids:
            self.history_meta_var.set("No history yet. Run a review, explain, or Ask Claude.")
            self._set_history_text(self.history_story, "")
            self._set_history_text(self.history_prompt, "")
            self._set_history_text(self.history_result, "")

    def _selected_history_id(self) -> str | None:
        selection = self.history_list.curselection()
        if not selection:
            return None
        index = int(selection[0])
        if index < 0 or index >= len(self._history_ids):
            return None
        return self._history_ids[index]

    def _selected_history_entry(self) -> HistoryEntry | None:
        entry_id = self._selected_history_id()
        if not entry_id:
            return None
        return get_history_entry(entry_id)

    def on_history_select(self, _event: object = None) -> None:
        entry = self._selected_history_entry()
        if not entry:
            return
        when = entry.created_at.replace("T", " ") if entry.created_at else "unknown"
        bits = [
            f"{entry.mode_label}",
            when,
            entry.pr_ref or entry.pr_url or "(no PR)",
        ]
        if entry.pr_title:
            bits.append(entry.pr_title)
        self.history_meta_var.set("  ·  ".join(bits))
        self._set_history_text(self.history_story, entry.story or "(empty)")
        if entry.mode == "review":
            prompt_body = entry.reviewer_prompt_content or "(no reviewer prompt saved)"
            if entry.reviewer_prompt_name:
                prompt_body = f"Name: {entry.reviewer_prompt_name}\n\n{prompt_body}"
        elif entry.mode == "explain":
            prompt_body = (
                "(Built-in Explain PR prompt)\n\n"
                "Optional story / details are under the Story tab."
            )
        else:
            prompt_body = (
                "(Ask Claude — no reviewer prompt)\n\n"
                "The Story tab is the full custom prompt that was sent."
            )
        self._set_history_text(self.history_prompt, prompt_body)
        self._set_history_text(self.history_result, entry.result or "(empty result)")
        self.history_notebook.select(2)

    def restore_history_inputs(self) -> None:
        entry = self._selected_history_entry()
        if not entry:
            messagebox.showinfo("History", "Select a past run first.")
            return
        self._apply_history_inputs(entry)
        if self.notebook is not None:
            self.notebook.select(self.review_tab)
        self.status_var.set(f"Restored inputs from {entry.mode_label}")

    def revisit_history_entry(self) -> None:
        entry = self._selected_history_entry()
        if not entry:
            messagebox.showinfo("History", "Select a past run first.")
            return
        self._apply_history_inputs(entry)
        summary = list(entry.summary_lines) or [
            f"Revisited {entry.mode_label}",
            entry.pr_ref or entry.pr_url,
            entry.created_at.replace("T", " ") if entry.created_at else "",
        ]
        if entry.mode == "review":
            # Historical reviews keep the saved comment text; live diff panels need a fresh run.
            self.diff_view.clear()
            self._last_review_raw = format_copy_friendly(entry.result)
            self._last_explanation = entry.result
            self.explain_text.configure(state=tk.NORMAL)
            self.explain_text.delete("1.0", tk.END)
            header = "\n".join(s for s in summary if s) + "\n" + ("=" * 60) + "\n\n"
            note = (
                "(Historical peer review — comment text restored. "
                "Run peer review again to rebuild inline diff panels.)\n\n"
            )
            self.explain_text.insert("1.0", header + note + (entry.result or ""))
            enable_selection_copy(self.explain_text)
            self._show_panel("explain")
            self.output_box.configure(text="Historical peer review")
            has_text = bool((entry.result or "").strip())
            self.copy_all_btn.configure(state=tk.NORMAL if has_text else tk.DISABLED)
            self.copy_selected_btn.configure(state=tk.NORMAL if has_text else tk.DISABLED)
            self.copy_llm_btn.configure(state=tk.DISABLED)
        else:
            title = "PR explanation" if entry.mode == "explain" else "Claude response"
            self._explain_success(
                entry.result,
                [s for s in summary if s],
                title=title,
                done_status=f"Revisited — {entry.mode_label}",
                mode=entry.mode,
                save_history=False,
            )
        if self.notebook is not None:
            self.notebook.select(self.review_tab)
        self.status_var.set(f"Revisited {entry.mode_label}")

    def _apply_history_inputs(self, entry: HistoryEntry) -> None:
        self.pr_url_var.set(entry.pr_url or "")
        self.story_text.delete("1.0", tk.END)
        if entry.story:
            self.story_text.insert("1.0", entry.story)
        if entry.mode == "review" and entry.reviewer_prompt_id:
            try:
                ref = parse_pr_url(entry.pr_url) if entry.pr_url else None
                if ref is not None:
                    self.cycler.repo_name = ref.repo
                self.cycler.refresh()
                if not self.cycler.set_by_id(entry.reviewer_prompt_id):
                    # Fall back to name match if the original id was deleted/recreated.
                    for prompt in self.cycler.prompts:
                        if (
                            entry.reviewer_prompt_name
                            and prompt.name == entry.reviewer_prompt_name
                        ):
                            self.cycler.set_by_id(prompt.id)
                            break
            except Exception:  # noqa: BLE001
                pass
        self._update_prompt_cycle_view()
        self.on_pr_url_changed()

    def delete_selected_history(self) -> None:
        entry = self._selected_history_entry()
        if not entry:
            messagebox.showinfo("History", "Select a past run first.")
            return
        if not messagebox.askyesno(
            "Delete history",
            f"Delete this {entry.mode_label} from history?",
        ):
            return
        delete_history_entry(entry.id)
        self.refresh_history_ui()

    def clear_all_history(self) -> None:
        if not list_history():
            messagebox.showinfo("History", "History is already empty.")
            return
        if not messagebox.askyesno(
            "Clear history",
            "Delete all past runs from history?\n\nThis cannot be undone.",
        ):
            return
        clear_history()
        self.refresh_history_ui()

    def open_settings(self) -> None:
        SettingsDialog(self)

    def clear_all_auths(self) -> None:
        if self._busy:
            messagebox.showinfo(
                "Busy",
                "A review is still running. Wait for it to finish before clearing auth.",
            )
            return
        if not messagebox.askyesno(
            "Clear all auth",
            "Sign out of GitHub and Claude for this app?\n\n"
            "This clears WSL/Windows GitHub credentials and the Claude SSO session.",
        ):
            return

        from app.auth_flows import clear_all_auth

        self.status_var.set("Clearing auth…")
        self.github_auth_light.set("checking")
        self.claude_auth_light.set("checking")

        def worker() -> None:
            notes = clear_all_auth()
            clear_auth_status()
            cfg = load_config()
            cfg["github_token"] = ""
            # Keep anthropic key unless user clears from Settings; GitHub/Claude SSO are the main "auths".
            save_config(cfg)
            self.after(
                0,
                lambda: self._after_clear_auths(notes),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _after_clear_auths(self, notes: str) -> None:
        self._apply_auth_lights(
            False,
            "cleared",
            False,
            "cleared",
            persist=True,
        )
        self.status_var.set("Auth cleared")
        messagebox.showinfo("Auth cleared", notes)

    def quick_github_login(self) -> None:
        from app.auth_flows import start_github_web_login

        self.github_auth_light.set("checking")
        try:
            message = start_github_web_login(force=True)
        except Exception as exc:  # noqa: BLE001
            self.github_auth_light.set("error")
            messagebox.showerror("GitHub login", str(exc))
            return
        messagebox.showinfo("GitHub login", message)
        self.status_var.set("Finish GitHub login in browser, then Refresh")
        self.after(4000, self.refresh_auth_lights)

    def quick_claude_login(self) -> None:
        from app.auth_flows import start_claude_sso_login

        self.claude_auth_light.set("checking")
        cfg = load_config()
        try:
            message = start_claude_sso_login(
                email=cfg.get("claude_sso_email") or ""
            )
        except Exception as exc:  # noqa: BLE001
            self.claude_auth_light.set("error")
            messagebox.showerror("Claude SSO login", str(exc))
            return
        messagebox.showinfo("Claude SSO login", message)
        self.status_var.set("Finish Claude SSO in browser, then Refresh")
        self.after(4000, self.refresh_auth_lights)

    def refresh_auth_lights(self) -> None:
        self.github_auth_light.set("checking")
        self.claude_auth_light.set("checking")
        self.github_auth_label_var.set("GitHub")
        self.claude_auth_label_var.set("Claude")
        self.status_var.set("Checking auth…")

        def worker() -> None:
            from app.auth_flows import check_claude_auth, check_github_auth

            gh_ok, gh_detail = check_github_auth()
            cl_ok, cl_detail = check_claude_auth()
            self.after(
                0,
                lambda: self._apply_auth_lights(gh_ok, gh_detail, cl_ok, cl_detail),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_auth_lights(
        self,
        gh_ok: bool,
        gh_detail: str,
        cl_ok: bool,
        cl_detail: str,
        *,
        persist: bool = True,
        github_known: bool = True,
        claude_known: bool = True,
    ) -> None:
        if github_known:
            self.github_auth_light.set("ok" if gh_ok else "error")
            self.github_auth_label_var.set("GitHub")
            self.github_tooltip.set(
                f"Signed in: {gh_detail}" if gh_ok else f"Not signed in: {gh_detail}"
            )
        else:
            self.github_auth_light.set("idle")
            self.github_auth_label_var.set("GitHub")
            self.github_tooltip.set(gh_detail)

        if claude_known:
            self.claude_auth_light.set("ok" if cl_ok else "error")
            self.claude_auth_label_var.set("Claude")
            self.claude_tooltip.set(
                f"Signed in: {cl_detail}" if cl_ok else f"Not signed in: {cl_detail}"
            )
        else:
            self.claude_auth_light.set("idle")
            self.claude_auth_label_var.set("Claude")
            self.claude_tooltip.set(cl_detail)

        if github_known and claude_known:
            if gh_ok and cl_ok:
                self.status_var.set("Ready")
            elif not gh_ok and not cl_ok:
                self.status_var.set("Sign in to GitHub and Claude")
            elif not gh_ok:
                self.status_var.set("GitHub sign-in needed")
            else:
                self.status_var.set("Claude sign-in needed")

        self.auth_status_var.set("")

        if persist and github_known and claude_known:
            save_auth_status(
                github_ok=gh_ok,
                github_detail=gh_detail,
                claude_ok=cl_ok,
                claude_detail=cl_detail,
            )

    def _set_repo_light(self, state: str, detail: str = "") -> None:
        """state: idle | checking | ok | error"""
        self.repo_light.set(state)
        labels = {
            "idle": "",
            "checking": "Checking connection…",
            "ok": "Connected",
            "error": "Connection failed",
        }
        text = detail.strip() if detail.strip() else labels.get(state, "")
        if state == "ok" and detail:
            text = f"Connected — {detail}"
        elif state == "error" and detail:
            text = f"Failed — {detail}"
        self.repo_light_status_var.set(text)

    def detect_repo(self) -> None:
        url = self.pr_url_var.get().strip()
        if not url:
            self._set_repo_light("error", "Paste a GitHub PR URL first")
            messagebox.showinfo("PR link", "Paste a GitHub PR URL first.")
            return
        try:
            ref = parse_pr_url(url)
        except ValueError as exc:
            self._set_repo_light("error", "Invalid URL")
            messagebox.showerror("Invalid URL", str(exc))
            return

        self.repo_label_var.set(ref.full_name)
        self.cycler.repo_name = ref.repo
        self.cycler.refresh()
        self._update_prompt_cycle_view()
        self._set_repo_light("checking")
        self.status_var.set("Checking GitHub connection…")

        config = load_config()

        def worker() -> None:
            from app.github_pr import probe_pull_request
            from app.wsl_auth import resolve_github_token

            try:
                token, _source = resolve_github_token(
                    explicit_token=config.get("github_token") or "",
                    use_wsl=bool(config.get("use_wsl_github_auth", True)),
                )
                probed_ref, title = probe_pull_request(url, token=token)
                self.after(
                    0,
                    lambda: self._detect_repo_success(probed_ref.full_name, title),
                )
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                self.after(0, lambda m=message: self._detect_repo_failure(m))

        threading.Thread(target=worker, daemon=True).start()

    def _detect_repo_success(self, full_name: str, title: str) -> None:
        self.repo_label_var.set(full_name)
        self._set_repo_light("ok", title[:80] if title else full_name)
        self.status_var.set("Repo reachable")

    def _detect_repo_failure(self, error: str) -> None:
        short = error.replace("\n", " ")
        if len(short) > 90:
            short = short[:87] + "..."
        self._set_repo_light("error", short)
        self.status_var.set("Repo connection failed")

    def on_pr_url_changed(self) -> None:
        url = self.pr_url_var.get().strip()
        if not url:
            self._set_repo_light("idle")
            return
        try:
            ref = parse_pr_url(url)
        except ValueError:
            self._set_repo_light("idle")
            return
        self.repo_label_var.set(ref.full_name)
        # URL edits invalidate the last connection check.
        self._set_repo_light("idle")
        if self.cycler.repo_name != ref.repo:
            self.cycler.repo_name = ref.repo
            self.cycler.refresh()
            self._update_prompt_cycle_view()

    def refresh_prompts_ui(self) -> None:
        self.cycler.refresh()
        self._update_prompt_cycle_view()
        self._reload_prompt_tree()

    def _update_prompt_cycle_view(self) -> None:
        self.prompt_label_var.set(self.cycler.label())
        self.prompt_preview.configure(state=tk.NORMAL)
        self.prompt_preview.delete("1.0", tk.END)
        current = self.cycler.current
        if current:
            self.prompt_preview.insert("1.0", current.content or "(empty prompt body)")
        else:
            self.prompt_preview.insert(
                "1.0",
                "No prompts yet. Open the Prompts tab to create one "
                "(generic and/or repo-specific).",
            )
        self.prompt_preview.configure(state=tk.DISABLED)

    def _reload_prompt_tree(self, *, select_id: str | None = None) -> None:
        current = select_id or self._selected_prompt_id()
        for item in self.prompt_tree.get_children():
            self.prompt_tree.delete(item)
        for prompt in list_prompts():
            self.prompt_tree.insert(
                "",
                tk.END,
                iid=prompt.id,
                values=(
                    prompt.name,
                    prompt.repo_type,
                    "yes" if prompt.is_generic else "no",
                ),
            )
        if current and self.prompt_tree.exists(current):
            self.prompt_tree.selection_set(current)
            self.prompt_tree.see(current)
            self.on_prompt_select()

    def move_prompt_up(self) -> None:
        self._move_selected_prompt(-1)

    def move_prompt_down(self) -> None:
        self._move_selected_prompt(1)

    def _move_selected_prompt(self, direction: int) -> None:
        prompt_id = self._selected_prompt_id()
        if not prompt_id:
            messagebox.showinfo("Reorder prompts", "Select a prompt first.")
            return
        try:
            move_prompt(prompt_id, direction)
        except KeyError as exc:
            messagebox.showerror("Reorder failed", str(exc))
            self.refresh_prompts_ui()
            return
        self.cycler.refresh()
        self._update_prompt_cycle_view()
        self._reload_prompt_tree(select_id=prompt_id)

    def _clear_prompt_drop_highlight(self) -> None:
        if self._drag_target_id and self.prompt_tree.exists(self._drag_target_id):
            self.prompt_tree.item(self._drag_target_id, tags=())
        self._drag_target_id = None

    def _on_prompt_drag_start(self, event: tk.Event) -> None:
        row = self.prompt_tree.identify_row(event.y)
        if not row:
            self._drag_prompt_id = None
            return
        self._drag_prompt_id = row
        self.prompt_tree.selection_set(row)
        self.prompt_tree.focus(row)
        self.on_prompt_select()

    def _on_prompt_drag_motion(self, event: tk.Event) -> None:
        if not self._drag_prompt_id:
            return
        row = self.prompt_tree.identify_row(event.y)
        if row == self._drag_target_id:
            return
        self._clear_prompt_drop_highlight()
        if row and row != self._drag_prompt_id and self.prompt_tree.exists(row):
            self._drag_target_id = row
            self.prompt_tree.item(row, tags=("drop_target",))
            self.prompt_tree.see(row)

    def _on_prompt_drag_drop(self, event: tk.Event) -> None:
        source_id = self._drag_prompt_id
        self._clear_prompt_drop_highlight()
        self._drag_prompt_id = None
        if not source_id:
            return
        target_id = self.prompt_tree.identify_row(event.y)
        if not target_id or target_id == source_id:
            return
        children = list(self.prompt_tree.get_children())
        try:
            new_index = children.index(target_id)
        except ValueError:
            return
        # If dropping below the midpoint of the target row, place after it.
        bbox = self.prompt_tree.bbox(target_id)
        if bbox:
            _x, y, _w, h = bbox
            if event.y > y + (h / 2):
                new_index += 1
                # Account for removing source first when source is above target.
        try:
            source_index = children.index(source_id)
        except ValueError:
            return
        if source_index < new_index:
            new_index -= 1
        try:
            move_prompt_to_index(source_id, new_index)
        except KeyError as exc:
            messagebox.showerror("Reorder failed", str(exc))
            self.refresh_prompts_ui()
            return
        self.cycler.refresh()
        self._update_prompt_cycle_view()
        self._reload_prompt_tree(select_id=source_id)

    def cycle_next(self) -> None:
        self.cycler.next()
        self._update_prompt_cycle_view()

    def cycle_prev(self) -> None:
        self.cycler.prev()
        self._update_prompt_cycle_view()

    def new_prompt(self) -> None:
        dialog = PromptEditorDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.refresh_prompts_ui()

    def _selected_prompt_id(self) -> str | None:
        selection = self.prompt_tree.selection()
        return selection[0] if selection else None

    def edit_prompt(self) -> None:
        prompt_id = self._selected_prompt_id()
        if not prompt_id:
            messagebox.showinfo("Edit prompt", "Select a prompt first.")
            return
        prompts = {p.id: p for p in list_prompts()}
        prompt = prompts.get(prompt_id)
        if not prompt:
            messagebox.showerror("Edit prompt", "Prompt no longer exists.")
            self.refresh_prompts_ui()
            return
        dialog = PromptEditorDialog(self, prompt)
        self.wait_window(dialog)
        if dialog.result:
            self.refresh_prompts_ui()

    def delete_selected_prompt(self) -> None:
        prompt_id = self._selected_prompt_id()
        if not prompt_id:
            messagebox.showinfo("Delete prompt", "Select a prompt first.")
            return
        if not messagebox.askyesno("Delete prompt", "Delete the selected prompt?"):
            return
        try:
            delete_prompt(prompt_id)
        except KeyError as exc:
            messagebox.showerror("Delete failed", str(exc))
        self.refresh_prompts_ui()

    def on_prompt_select(self, _event: object = None) -> None:
        prompt_id = self._selected_prompt_id()
        self.prompt_detail.configure(state=tk.NORMAL)
        self.prompt_detail.delete("1.0", tk.END)
        if prompt_id:
            prompts = {p.id: p for p in list_prompts()}
            prompt = prompts.get(prompt_id)
            if prompt:
                self.prompt_detail.insert("1.0", prompt.content)
        self.prompt_detail.configure(state=tk.DISABLED)

    def start_review(self) -> None:
        self._start_claude_job(mode="review")

    def start_explain(self) -> None:
        self._start_claude_job(mode="explain")

    def start_ask(self) -> None:
        self._start_claude_job(mode="ask")

    def _set_run_buttons_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        self.run_btn.configure(state=state)
        self.explain_btn.configure(state=state)
        self.ask_btn.configure(state=state)

    def _start_claude_job(self, *, mode: str) -> None:
        if self._busy:
            return
        url = self.pr_url_var.get().strip()
        if not url:
            messagebox.showerror("Missing PR", "Paste a GitHub PR URL.")
            return
        try:
            parse_pr_url(url)
        except ValueError as exc:
            messagebox.showerror("Invalid URL", str(exc))
            return

        story = self.story_text.get("1.0", "end-1c")
        if mode == "ask" and not story.strip():
            messagebox.showerror(
                "Missing prompt",
                "Ask Claude uses only the text in "
                "Story / explanation / custom prompt.\n\n"
                "Write what you want Claude to do there.",
            )
            return

        prompt = self.cycler.current
        config = load_config()
        self._pending_history = {
            "mode": mode,
            "pr_url": url,
            "story": story,
            "reviewer_prompt_id": prompt.id if prompt and mode == "review" else "",
            "reviewer_prompt_name": prompt.name if prompt and mode == "review" else "",
            "reviewer_prompt_content": prompt.content if prompt and mode == "review" else "",
        }

        self._busy = True
        self._set_run_buttons_enabled(False)
        status_by_mode = {
            "explain": "Explanation in progress",
            "ask": "Ask in progress",
            "review": "Review in progress",
        }
        self.status_var.set(status_by_mode.get(mode, "In progress"))
        self.thinking.start("Thinking")
        self.diff_view.clear()
        self._last_review_raw = ""
        self._last_explanation = ""
        self._set_copy_buttons_enabled(False)
        self._show_panel("live")
        self._clear_live_claude()

        def on_claude_event(event: dict) -> None:
            kind = str(event.get("kind") or "")
            text = str(event.get("text") or "")
            self.after(0, lambda k=kind, t=text: self._append_live_claude(k, t))

        def worker() -> None:
            try:
                label_by_mode = {
                    "explain": "Explaining PR",
                    "ask": "Asking Claude",
                    "review": "Asking Claude",
                }
                label = label_by_mode.get(mode, "Asking Claude")
                self.after(0, lambda: self.thinking.start(label))
                if mode == "explain":
                    diff, result = run_pr_explanation(
                        pr_url=url,
                        story=story,
                        config=config,
                        on_claude_event=on_claude_event,
                    )
                    auth_source = getattr(diff, "auth_source", "unknown")
                    summary = [
                        f"Explained: {diff.ref.full_name}#{diff.ref.number} — {diff.title}",
                        f"Files changed: {len(diff.files)}",
                        f"Auth: {auth_source}",
                    ]
                    self.after(
                        0,
                        lambda r=result, s=list(summary): self._explain_success(
                            r,
                            s,
                            title="PR explanation",
                            done_status="Done — PR explanation ready",
                            mode="explain",
                        ),
                    )
                elif mode == "ask":
                    diff, result = run_pr_ask(
                        pr_url=url,
                        story=story,
                        config=config,
                        on_claude_event=on_claude_event,
                    )
                    auth_source = getattr(diff, "auth_source", "unknown")
                    summary = [
                        f"Asked: {diff.ref.full_name}#{diff.ref.number} — {diff.title}",
                        f"Files changed: {len(diff.files)}",
                        f"Auth: {auth_source}",
                        "Prompt: (custom from story box)",
                    ]
                    self.after(
                        0,
                        lambda r=result, s=list(summary): self._explain_success(
                            r,
                            s,
                            title="Claude response",
                            done_status="Done — response ready",
                            mode="ask",
                        ),
                    )
                else:
                    diff, review = run_peer_review(
                        pr_url=url,
                        story=story,
                        reviewer_prompt=prompt,
                        config=config,
                        on_claude_event=on_claude_event,
                    )
                    auth_source = getattr(diff, "auth_source", "unknown")
                    summary = [
                        f"Reviewed: {diff.ref.full_name}#{diff.ref.number} — {diff.title}",
                        f"Files changed: {len(diff.files)}",
                        f"Auth: {auth_source}",
                        f"Prompt: {prompt.name if prompt else '(none)'}",
                    ]
                    self.after(
                        0,
                        lambda d=diff, r=review, s=list(summary): self._review_success(
                            d, r, s
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: self._review_failure(e))

        threading.Thread(target=worker, daemon=True).start()

    def _show_panel(self, which: str) -> None:
        self._output_mode = which
        self.live_frame.pack_forget()
        self.explain_frame.pack_forget()
        self.diff_view.pack_forget()
        if which == "live":
            self.output_box.configure(text="Claude thinking (live)")
            self.live_frame.pack(fill=tk.BOTH, expand=True)
        elif which == "explain":
            self.output_box.configure(text="Claude response")
            self.explain_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.output_box.configure(text="Diff review")
            self.diff_view.pack(fill=tk.BOTH, expand=True)
        self.update_idletasks()

    def _clear_live_claude(self) -> None:
        self.live_text.configure(state=tk.NORMAL)
        self.live_text.delete("1.0", tk.END)
        self.live_text.configure(state=tk.DISABLED)

    def _show_live_claude(self) -> None:
        self._clear_live_claude()
        self._show_panel("live")

    def _hide_live_claude(self) -> None:
        self._show_panel("diff")

    def _live_streamed_answer(self) -> str:
        """Collect green answer text that was streamed into the live panel."""
        try:
            ranges = self.live_text.tag_ranges("text")
        except tk.TclError:
            return ""
        chunks: list[str] = []
        for i in range(0, len(ranges), 2):
            chunks.append(self.live_text.get(ranges[i], ranges[i + 1]))
        return "".join(chunks).strip()

    def _append_live_claude(self, kind: str, text: str) -> None:
        if not text:
            return
        self.live_text.configure(state=tk.NORMAL)
        if kind == "status":
            pretty = text.replace("_", " ")
            self.thinking.start(pretty[:40].title() if pretty else "Thinking")
            self.live_text.insert(tk.END, f"\n⟪ {pretty} ⟫\n", ("status",))
        elif kind == "thinking":
            self.thinking.start("Thinking")
            self.live_text.insert(tk.END, text, ("thinking",))
        else:
            self.thinking.start("Writing")
            self.live_text.insert(tk.END, text, ("text",))
        self.live_text.see(tk.END)
        self.live_text.configure(state=tk.DISABLED)

    def _record_history(
        self,
        *,
        result: str,
        summary_lines: list[str] | None = None,
        pr_title: str = "",
        pr_ref: str = "",
        mode: str | None = None,
    ) -> None:
        pending = self._pending_history
        self._pending_history = None
        if not pending:
            return
        text = (result or "").strip()
        if not text:
            return
        try:
            add_history_entry(
                mode=mode or str(pending.get("mode") or "ask"),
                pr_url=str(pending.get("pr_url") or ""),
                result=text,
                story=str(pending.get("story") or ""),
                pr_title=pr_title,
                pr_ref=pr_ref,
                reviewer_prompt_id=str(pending.get("reviewer_prompt_id") or ""),
                reviewer_prompt_name=str(pending.get("reviewer_prompt_name") or ""),
                reviewer_prompt_content=str(
                    pending.get("reviewer_prompt_content") or ""
                ),
                summary_lines=summary_lines or [],
            )
            self.refresh_history_ui()
        except Exception:  # noqa: BLE001
            # History must never break the main review flow.
            pass

    def _pr_bits_from_summary(self, summary: list[str]) -> tuple[str, str]:
        """Best-effort parse of 'Reviewed: owner/repo#123 — Title' style lines."""
        pr_ref = ""
        pr_title = ""
        if not summary:
            return pr_ref, pr_title
        head = summary[0]
        for prefix in ("Reviewed: ", "Explained: ", "Asked: ", "Revisited "):
            if head.startswith(prefix):
                head = head[len(prefix) :]
                break
        if " — " in head:
            pr_ref, pr_title = head.split(" — ", 1)
        else:
            pr_ref = head
        return pr_ref.strip(), pr_title.strip()

    def _review_success(self, diff, review: str, summary: list[str]) -> None:
        self.thinking.stop()
        try:
            text = (review or "").strip() or self._live_streamed_answer()
            self._last_review_raw = format_copy_friendly(text)
            self._last_explanation = ""
            self.diff_view.render(
                diff=diff,
                review_text=text,
                summary_lines=summary,
            )
            self._show_panel("diff")
            self.status_var.set("Done — comments shown under matching lines")
            self._set_copy_buttons_enabled(self.diff_view.has_review_content())
            pr_ref = f"{diff.ref.full_name}#{diff.ref.number}"
            self._record_history(
                result=text,
                summary_lines=summary,
                pr_title=diff.title,
                pr_ref=pr_ref,
                mode="review",
            )
        except Exception as exc:  # noqa: BLE001
            self._pending_history = None
            self._review_failure(f"Could not render review output:\n{exc}")
            return
        self._busy = False
        self._set_run_buttons_enabled(True)

    def _explain_success(
        self,
        explanation: str,
        summary: list[str],
        *,
        title: str = "Claude response",
        done_status: str = "Done — response ready",
        mode: str | None = None,
        save_history: bool = True,
    ) -> None:
        self.thinking.stop()
        try:
            text = (explanation or "").strip() or self._live_streamed_answer()
            self._last_explanation = text
            self._last_review_raw = ""
            self.diff_view.clear()
            self.explain_text.configure(state=tk.NORMAL)
            self.explain_text.delete("1.0", tk.END)
            header = "\n".join(summary) + "\n" + ("=" * 60) + "\n\n"
            self.explain_text.insert("1.0", header + self._last_explanation)
            enable_selection_copy(self.explain_text)
            self._show_panel("explain")
            self.output_box.configure(text=title)
            self.status_var.set(done_status)
            has_text = bool(self._last_explanation.strip())
            self.copy_all_btn.configure(state=tk.NORMAL if has_text else tk.DISABLED)
            self.copy_selected_btn.configure(state=tk.NORMAL if has_text else tk.DISABLED)
            self.copy_llm_btn.configure(state=tk.DISABLED)
            if save_history:
                pr_ref, pr_title = self._pr_bits_from_summary(summary)
                resolved_mode = mode
                if resolved_mode is None and self._pending_history:
                    resolved_mode = str(self._pending_history.get("mode") or "ask")
                self._record_history(
                    result=text,
                    summary_lines=summary,
                    pr_title=pr_title,
                    pr_ref=pr_ref,
                    mode=resolved_mode,
                )
            else:
                self._pending_history = None
        except Exception as exc:  # noqa: BLE001
            self._pending_history = None
            self._review_failure(f"Could not show response:\n{exc}")
            return
        self._busy = False
        self._set_run_buttons_enabled(True)

    def _review_failure(self, error: str) -> None:
        self.thinking.stop()
        self._pending_history = None
        streamed = self._live_streamed_answer()
        # Leave the live transcript behind — show a clear error (and any answer text).
        self.explain_text.configure(state=tk.NORMAL)
        self.explain_text.delete("1.0", tk.END)
        body = f"Request failed\n{'=' * 60}\n\n{error.strip()}"
        if streamed:
            body += f"\n\nStreamed answer before failure:\n{'-' * 40}\n{streamed}"
        self.explain_text.insert("1.0", body)
        enable_selection_copy(self.explain_text)
        self._last_explanation = streamed
        self._show_panel("explain")
        self.output_box.configure(text="Error")
        self.status_var.set("Failed")
        self._busy = False
        self._set_run_buttons_enabled(True)
        self.copy_all_btn.configure(state=tk.NORMAL if streamed else tk.DISABLED)
        self.copy_selected_btn.configure(state=tk.NORMAL if streamed else tk.DISABLED)
        self.copy_llm_btn.configure(state=tk.DISABLED)
        messagebox.showerror("Request failed", error)

    def _set_copy_buttons_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        self.copy_all_btn.configure(state=state)
        self.copy_selected_btn.configure(state=state)
        self.copy_llm_btn.configure(state=state)

    def copy_as_llm_prompt(self) -> None:
        if self._output_mode == "explain":
            return
        story = self.story_text.get("1.0", "end-1c")
        text = self.diff_view.get_verification_prompt(story=story)
        if not text.strip():
            self._set_copy_buttons_enabled(False)
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("Copied LLM verification prompt")

    def on_close(self) -> None:
        if self._busy:
            ok = messagebox.askyesno(
                "Still running",
                "Claude is still thinking.\n\nClose anyway and cancel it?",
            )
            if not ok:
                return
            self.thinking.stop()
            self._busy = False
        self.destroy()

    def copy_all(self) -> None:
        if self._output_mode == "explain":
            text = self._last_explanation
        else:
            text = self.diff_view.get_copy_text() or self._last_review_raw
        if not text.strip():
            messagebox.showinfo("Copy", "Nothing to copy yet.")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("Copied all")

    def copy_selected(self) -> None:
        if self._output_mode == "explain":
            try:
                text = self.explain_text.get(tk.SEL_FIRST, tk.SEL_LAST)
            except tk.TclError:
                messagebox.showinfo("Copy", "Select text in the explanation first.")
                return
        else:
            text = self.diff_view.get_selected_text()
            if not text.strip():
                messagebox.showinfo("Copy", "Select text in the diff review first.")
                return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("Copied selection")


def main() -> None:
    app = PeerReviewApp()
    app.mainloop()


if __name__ == "__main__":
    main()

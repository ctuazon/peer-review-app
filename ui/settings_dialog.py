"""Settings dialog."""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk


from app import load_config, save_config, secrets_backend
from app.cost import DEFAULT_REVIEW_MODEL, EFFORTS, REVIEW_MODEL_CHOICES, model_info
from app.auth_status_store import clear_auth_status, load_auth_status, save_auth_status
from ui.widgets import StatusLight, tip


def _number(text: str, default: float) -> float:
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        return default


def _size_tiers_text(tiers: object) -> str:
    """'Up to 300 lines: Sonnet 5.5, low · ... · larger: Opus 5.5, medium.'"""
    if not isinstance(tiers, list):
        return ""
    parts = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        info = model_info(str(tier.get("model") or ""))
        label = info.label.split(" (")[0] if info else str(tier.get("model"))
        limit = tier.get("max_lines")
        reach = f"up to {limit:,} lines" if isinstance(limit, int) else "larger"
        parts.append(f"{reach}: {label}, {tier.get('effort')}")
    text = " · ".join(parts)
    return (text[0].upper() + text[1:] + ".") if text else ""


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

        tip(ttk.Button(
            auth,
            text="Login with GitHub (browser)",
            command=self._github_login,
        ), "Sign in to GitHub through Git Credential Manager in your browser.").grid(row=2, column=0, sticky="w", padx=(0, 8))
        tip(ttk.Button(
            auth,
            text="Login with Claude SSO",
            command=self._claude_login,
        ), "Sign in to Claude with SSO. Opens the browser via WSL.").grid(row=2, column=1, sticky="w")
        tip(
            ttk.Button(auth, text="Refresh status", command=self._refresh_status),
            "Re-check whether GitHub and Claude are signed in.",
        ).grid(
            row=3, column=0, sticky="w", pady=(8, 0)
        )
        tip(
            ttk.Button(auth, text="Clear all auth", command=self._clear_all_auth),
            "Sign out of GitHub and Claude for this app.",
        ).grid(
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

        ttk.Label(frame, text="Default review model").grid(row=8, column=0, sticky="w")
        self.model_var = tk.StringVar(
            value=self.config_data.get("claude_model", DEFAULT_REVIEW_MODEL)
        )
        ttk.Combobox(frame, textvariable=self.model_var, values=REVIEW_MODEL_CHOICES, width=54).grid(
            row=8, column=1, pady=4, sticky="ew"
        )

        ttk.Label(frame, text="Review-comment triage model").grid(row=9, column=0, sticky="w")
        self.cheap_model_var = tk.StringVar(
            value=self.config_data.get("bot_cheap_model", "claude-haiku-4-5-20251001")
        )
        ttk.Entry(frame, textvariable=self.cheap_model_var, width=56).grid(
            row=9, column=1, pady=4, sticky="ew"
        )
        ttk.Label(
            frame,
            text="Used only to triage bot/reviewer comments (valid/invalid) -- fix-drafting and "
            "self-review still use the model above. Leave blank to use that model everywhere.",
            justify=tk.LEFT,
            foreground="#666",
        ).grid(row=10, column=0, columnspan=2, sticky="w")

        review = ttk.LabelFrame(frame, text="Peer review defaults (pr-review.yml and the Review tab override these)", padding=8)
        review.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.effort_default_var = tk.StringVar(value=str(self.config_data.get("review_effort") or "medium"))
        self.mode_default_var = tk.StringVar(value=str(self.config_data.get("review_mode") or "agentic"))
        self.verify_default_var = tk.BooleanVar(value=bool(self.config_data.get("verify_findings")))
        self.timeout_var = tk.StringVar(value=str(self.config_data.get("claude_timeout_minutes") or 15))
        self.warn_usd_var = tk.StringVar(value=str(self.config_data.get("warn_review_usd") or 0))
        ttk.Label(review, text="Effort").grid(row=0, column=0, sticky="w")
        ttk.Combobox(review, textvariable=self.effort_default_var, values=list(EFFORTS), state="readonly", width=10).grid(
            row=0, column=1, sticky="w", padx=(4, 16)
        )
        ttk.Label(review, text="Mode").grid(row=0, column=2, sticky="w")
        ttk.Combobox(review, textvariable=self.mode_default_var, values=["agentic", "single-shot"], state="readonly",
                     width=12).grid(row=0, column=3, sticky="w", padx=(4, 16))
        ttk.Checkbutton(review, text="Verify findings with a second pass", variable=self.verify_default_var).grid(
            row=0, column=4, sticky="w"
        )
        ttk.Label(review, text="Timeout (min)").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(review, textvariable=self.timeout_var, width=8).grid(row=1, column=1, sticky="w", padx=(4, 16), pady=(6, 0))
        ttk.Label(review, text="Warn above $").grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Entry(review, textvariable=self.warn_usd_var, width=8).grid(row=1, column=3, sticky="w", padx=(4, 16), pady=(6, 0))
        ttk.Label(
            review,
            text="Agentic checks out the PR head locally and lets Claude Read/Grep/Glob it (CLI/WSL mode). "
            "0 disables the cost warning.",
            foreground="#666", wraplength=640, justify=tk.LEFT,
        ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.by_size_var = tk.BooleanVar(value=bool(self.config_data.get("review_by_size")))
        ttk.Checkbutton(
            review, text="Pick the model and effort by PR size", variable=self.by_size_var
        ).grid(row=3, column=0, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Label(
            review,
            text=_size_tiers_text(self.config_data.get("review_size_tiers"))
            + " Counts the changed lines Claude is sent; a re-review counts only what changed since the "
            "last pass. Edit review_size_tiers in data/config.json to change the tiers. When this is on, "
            "it replaces the default review model and effort for peer reviews.",
            foreground="#666", wraplength=640, justify=tk.LEFT,
        ).grid(row=4, column=0, columnspan=5, sticky="w")

        from app.jira import resolve_jira_settings

        jira_settings = resolve_jira_settings(self.config_data)
        jira = ttk.LabelFrame(frame, text="Jira ticket context", padding=8)
        jira.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.jira_enabled_var = tk.BooleanVar(value=jira_settings.enabled)
        ttk.Checkbutton(
            jira,
            text="Fetch Jira tickets referenced in the PR title, body or branch",
            variable=self.jira_enabled_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.jira_url_var = tk.StringVar(value=jira_settings.base_url)
        self.jira_email_var = tk.StringVar(value=jira_settings.email)
        self.jira_token_var = tk.StringVar(value=jira_settings.api_token)
        for row, (label, var, show) in enumerate(
            [
                ("Base URL", self.jira_url_var, ""),
                ("Email", self.jira_email_var, ""),
                ("API token", self.jira_token_var, "*"),
            ],
            start=1,
        ):
            ttk.Label(jira, text=label).grid(row=row, column=0, sticky="w")
            ttk.Entry(jira, textvariable=var, width=56, show=show).grid(
                row=row, column=1, pady=2, sticky="ew"
            )
        jira_buttons = ttk.Frame(jira)
        jira_buttons.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tip(
            ttk.Button(jira_buttons, text="Test Jira", command=self._test_jira),
            "Try the Jira URL, email and token above (or their .env fallbacks) and report whether they work.",
        ).pack(side=tk.LEFT)
        self.jira_status_var = tk.StringVar(
            value="Blank fields fall back to JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN (env or .env)."
        )
        ttk.Label(jira_buttons, textvariable=self.jira_status_var, foreground="#666").pack(
            side=tk.LEFT, padx=(8, 0)
        )
        jira.columnconfigure(1, weight=1)

        help_text = (
            "GitHub browser login uses Git Credential Manager (opens a webpage only).\n"
            "Claude SSO login opens the browser via WSL — no extra terminal windows.\n"
            "Token/API key fields are optional fallbacks, stored in " + secrets_backend() + ".\n"
            "Auth light status is saved and restored the next time you open the app."
        )
        ttk.Label(frame, text=help_text, justify=tk.LEFT).grid(
            row=13, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=14, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        tip(
            ttk.Button(buttons, text="Save", command=self._save),
            "Save these settings and close.",
        ).pack(side=tk.RIGHT)

        frame.columnconfigure(1, weight=1)
        self.geometry("760x860")
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

        token = self.token_var.get().strip()
        use_wsl = bool(self.wsl_auth_var.get())

        def worker() -> None:
            from app.auth_flows import check_claude_auth, check_github_auth

            gh_ok, gh_detail = check_github_auth(token, use_wsl=use_wsl)
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
                or DEFAULT_REVIEW_MODEL,
                "review_effort": self.effort_default_var.get() or "medium",
                "review_mode": self.mode_default_var.get() or "agentic",
                "verify_findings": bool(self.verify_default_var.get()),
                "review_by_size": bool(self.by_size_var.get()),
                # Not editable here; keep whatever data/config.json holds.
                "review_size_tiers": self.config_data.get("review_size_tiers"),
                "claude_timeout_minutes": _number(self.timeout_var.get(), 15),
                "warn_review_usd": _number(self.warn_usd_var.get(), 0),
                "bot_cheap_model": self.cheap_model_var.get().strip(),
                "jira_enabled": bool(self.jira_enabled_var.get()),
                "jira_base_url": self.jira_url_var.get().strip(),
                "jira_email": self.jira_email_var.get().strip(),
                "jira_api_token": self.jira_token_var.get().strip(),
            }
        )
        self.destroy()

    def _test_jira(self) -> None:
        from app.jira import JiraClient, JiraSettings

        settings = JiraSettings(
            enabled=True,
            base_url=self.jira_url_var.get().strip().rstrip("/"),
            email=self.jira_email_var.get().strip(),
            api_token=self.jira_token_var.get().strip(),
        )
        if not settings.configured:
            self.jira_status_var.set("Fill in base URL, email and API token first.")
            return
        self.jira_status_var.set("Checking…")

        def worker() -> None:
            response = JiraClient(settings).get("/rest/api/3/myself")
            if response is None:
                text = "Could not reach Jira."
            elif response.ok:
                text = "Connected as " + str((response.json() or {}).get("displayName") or settings.email)
            else:
                text = f"Jira returned HTTP {response.status_code}."
            self.after(0, lambda: self.jira_status_var.set(text))

        threading.Thread(target=worker, daemon=True).start()

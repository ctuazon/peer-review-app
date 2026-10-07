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
from app.bot_review import (
    BotFinding,
    run_bot_comment_check,
)
from app.diff_view import DiffReviewView, enable_selection_copy
from app.github_pr import PullRequestDiff, fetch_pull_request, parse_pr_url
from app.history_store import (
    add_history_entry,
    list_reviews_for_pr,
)
from app.lint_fix import LintFinding, run_lint_check
from app.merge_conflict import (
    ConflictFile,
    ConflictSession,
    MergeabilityInfo,
    draft_resolution,
    open_conflict_session,
)
from app.prompts_store import (
    PromptCycler,
)
from app.cost import EFFORTS, REVIEW_MODEL_CHOICES, budget_fit_steps
from app.doctor import format_report, run_doctor
from app.review import (
    ReviewOptions,
    ReviewPrep,
    ReviewRun,
    execute_review,
    format_copy_friendly,
    inspect_report,
    prepare_review,
    refine_estimate,
    review_summary_lines,
    review_to_history,
    run_pr_ask,
    run_pr_explanation,
)
from app.review_parse import comments_to_copy_text
from app.review_schema import INCONCLUSIVE
from app.wsl_auth import resolve_github_token
from ui.dialogs import BotReviewDialog, LintFixDialog, MergeConflictDialog
from ui.gate_dialog import RunGateDialog
from ui.history_tab import HistoryTabMixin
from ui.prompts_tab import PromptsTabMixin
from ui.settings_dialog import SettingsDialog
from ui.submit_dialog import SubmitReviewDialog
from ui.widgets import (
    TERMINAL_BG,
    Collapsible,
    HelpIcon,
    LinkButton,
    MenuEntry,
    PrimaryButton,
    Section,
    StatusLight,
    ThinkingIndicator,
    Tooltip,
    apply_theme,
    soften_text,
    tip,
)


class PeerReviewApp(PromptsTabMixin, HistoryTabMixin, tk.Tk):
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

        # Token usage: "call" is the most recent single run_claude() call,
        # "job" resets at the start of each button press (a job may fire
        # several calls, e.g. triage/fix/self-review per bot comment), and
        # "session" accumulates for the lifetime of the running app.
        self._call_usage = (0, 0)
        self._job_usage = {"input": 0, "output": 0}
        self._session_usage = {"input": 0, "output": 0}

        apply_theme(self)
        self._build_menu()
        # Packed before the notebook so it keeps its row at the bottom.
        self.status_bar = ttk.Frame(self, style="Statusbar.TFrame")
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))
        self.notebook = notebook

        self.review_tab = ttk.Frame(notebook, padding=(14, 12))
        self.prompts_tab = ttk.Frame(notebook, padding=(14, 12))
        self.history_tab = ttk.Frame(notebook, padding=(14, 12))
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

        # Every section stacks in one column so inputs and text areas share a
        # left edge; whitespace, not borders, separates the sections.
        pr_box = Section(tab, "Pull request")
        pr_box.pack(fill=tk.X)
        pr_row = ttk.Frame(pr_box.body)
        pr_row.pack(fill=tk.X)
        self.pr_url_var = tk.StringVar()
        pr_entry = ttk.Entry(pr_row, textvariable=self.pr_url_var)
        pr_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=2)
        tip(pr_entry, "Paste a GitHub pull request URL, e.g. https://github.com/owner/repo/pull/123")
        self.repo_light = StatusLight(pr_row)
        self.repo_light.pack(side=tk.RIGHT, padx=(8, 0))
        tip(
            ttk.Button(pr_row, text="Detect repo", command=self.detect_repo),
            "Check that GitHub can reach this PR, load the repo's reviewer prompts, "
            "and pre-fill Story from any linked Jira tickets.",
        ).pack(side=tk.RIGHT, padx=(8, 0))

        repo_status = ttk.Frame(pr_box.body)
        repo_status.pack(fill=tk.X, pady=(4, 0))
        self.repo_label_var = tk.StringVar(value="—")
        self.repo_light_status_var = tk.StringVar(value="")
        ttk.Label(repo_status, text="Repo:", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Label(repo_status, textvariable=self.repo_label_var).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(repo_status, textvariable=self.repo_light_status_var, style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(10, 0)
        )

        prompt_box = Section(tab, "Reviewer prompt")
        prompt_box.pack(fill=tk.X, pady=(14, 0))
        self.prompt_label_var = tk.StringVar(value="No prompts yet")
        ttk.Label(prompt_box.header, textvariable=self.prompt_label_var, style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(10, 0)
        )
        tip(
            ttk.Button(prompt_box.header, text="⟳", width=3, command=self.refresh_prompts_ui),
            "Reload the prompt list (picks up edits made in the Prompts tab).",
        ).pack(side=tk.RIGHT)
        tip(
            ttk.Button(prompt_box.header, text="▶", width=3, command=self.cycle_next),
            "Next reviewer prompt for this repo.",
        ).pack(side=tk.RIGHT, padx=(0, 4))
        tip(
            ttk.Button(prompt_box.header, text="◀", width=3, command=self.cycle_prev),
            "Previous reviewer prompt for this repo.",
        ).pack(side=tk.RIGHT, padx=(0, 4))
        self.prompt_preview = scrolledtext.ScrolledText(
            prompt_box.body, height=5, wrap=tk.WORD, state=tk.DISABLED
        )
        soften_text(self.prompt_preview)
        self.prompt_preview.pack(fill=tk.X)

        story_box = Section(tab, "Story / explanation / custom prompt")
        story_box.pack(fill=tk.X, pady=(14, 0))
        HelpIcon(
            story_box.header,
            "Context for the reviewer: the ticket, intent or anything Claude should know. "
            "Detect repo fills it from linked Jira tickets when it's empty. "
            "Ask Claude sends only this text as the question.",
        ).pack(side=tk.LEFT, padx=(6, 0))
        self.story_text = scrolledtext.ScrolledText(story_box.body, height=4, wrap=tk.WORD)
        soften_text(self.story_text)
        self.story_text.pack(fill=tk.BOTH, expand=True)

        follow_row = ttk.Frame(tab)
        follow_row.pack(fill=tk.X, pady=(8, 0))
        self.follow_up_var = tk.BooleanVar(value=True)
        self.follow_up_check = ttk.Checkbutton(follow_row, text="Re-review", variable=self.follow_up_var)
        self.follow_up_check.pack(side=tk.LEFT)
        HelpIcon(
            follow_row,
            "Re-review: report on findings from earlier reviews of this PR (fixed or still open), "
            "and only raise new findings at the follow-up floor (major by default).",
        ).pack(side=tk.LEFT, padx=(2, 0))
        self.follow_up_info_var = tk.StringVar(value="")
        ttk.Label(follow_row, textvariable=self.follow_up_info_var, style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self.pr_url_var.trace_add("write", lambda *_: self._refresh_follow_up_info())

        # Review settings. "auto" defers to .github/pr-review.yml, then the
        # defaults in Settings; anything else overrides both for this run.
        advanced = Collapsible(tab, "Advanced options")
        advanced.pack(fill=tk.X, pady=(6, 0))
        self.advanced_options = advanced
        self.model_choice_var = tk.StringVar(value="auto")
        self.effort_var = tk.StringVar(value="auto")
        self.mode_choice_var = tk.StringVar(value="auto")
        self.verify_var = tk.StringVar(value="auto")
        auto_note = (
            "auto = the repo's .github/pr-review.yml, then the defaults in File > Settings "
            "(which can pick the model and effort by PR size)."
        )
        settings = [
            ("Model", self.model_choice_var, ["auto", *REVIEW_MODEL_CHOICES], 18,
             "Claude model for this run. " + auto_note),
            ("Effort", self.effort_var, ["auto", *EFFORTS], 8,
             "How hard the model thinks. Higher effort is slower and costs more. "
             "Needs CLI/WSL or API mode; Inspect shows what will apply. " + auto_note),
            ("Mode", self.mode_choice_var, ["auto", "agentic", "single-shot"], 11,
             "agentic: Claude can open files in a local checkout of the repo. "
             "single-shot: Claude sees only the diff and context. " + auto_note),
            ("Verify findings", self.verify_var, ["auto", "on", "off"], 5,
             "Run a second pass that re-checks each finding and drops false positives. " + auto_note),
        ]
        for col, (label, var, values, width, help_text) in enumerate(settings):
            cell = ttk.Frame(advanced.body)
            cell.grid(row=0, column=col, sticky="w", padx=(0, 18))
            ttk.Label(cell, text=label).pack(side=tk.LEFT)
            HelpIcon(cell, help_text).pack(side=tk.LEFT, padx=(2, 4))
            ttk.Combobox(cell, textvariable=var, values=values, state="readonly", width=width).pack(side=tk.LEFT)
            var.trace_add("write", lambda *_: self._update_advanced_summary())
        self._update_advanced_summary()

        action_row = ttk.Frame(tab)
        action_row.pack(fill=tk.X, pady=(14, 10))
        self.run_btn = PrimaryButton(action_row, text="Run peer review", command=self.start_review)
        self.run_btn.pack(side=tk.LEFT)
        tip(
            self.run_btn,
            "Fetch the PR diff and have Claude review it with the selected reviewer prompt. "
            "Findings appear inline in the diff below, where you can edit them before submitting to GitHub.",
        )

        # Secondary tasks live in one dropdown. Each entry's right-hand text says
        # what it does, and hovering an entry shows more in the status line.
        self.more_btn = ttk.Menubutton(action_row, text="More actions")
        self.more_btn.pack(side=tk.LEFT, padx=(8, 0))
        tip(self.more_btn, "Other PR tasks: dry run, explain, ask, triage comments, merge conflicts, lint.")
        more_menu = tk.Menu(self.more_btn, tearoff=0)
        self.more_btn["menu"] = more_menu
        more_entries: list[MenuEntry] = []
        self._menu_help: dict[str, str] = {}
        actions = [
            ("inspect_btn", "Inspect", self.start_inspect, "Dry run, no review",
             "Shows the tier, model, effort, ticket, conventions, withheld files and estimated cost. Nothing is sent for review."),
            ("explain_btn", "Explain PR", self.start_explain, "Plain-English walkthrough",
             "Claude explains what the PR changes and why, without reviewing it."),
            ("ask_btn", "Ask Claude", self.start_ask, "Ask using the Story box",
             "Sends the Story / custom prompt text as a question about this PR."),
            ("bot_check_btn", "Check review comments", self.start_bot_check, "Triage bot & reviewer comments",
             "Finds CodeRabbit, Amazon Q and reviewer comments, checks each with Claude, and drafts fixes or replies for you to approve."),
            ("merge_conflict_btn", "Resolve merge conflict", self.start_merge_conflict_check, "Draft conflict fixes",
             "Runs a real three-way merge and has Claude draft resolutions for only the conflicting hunks."),
            ("lint_fix_btn", "Fix lint", self.start_lint_check, "Fix style issues on changed lines",
             "Scans the lines this PR touches for lint/style issues and drafts fixes to apply as one commit."),
        ]
        for index, (attr, label, command, short, long) in enumerate(actions):
            more_menu.add_command(label=label, accelerator=short, command=command)
            setattr(self, attr, MenuEntry(more_menu, index, self.more_btn, more_entries))
            self._menu_help[label] = f"{label}: {long}"
        more_menu.bind("<<MenuSelect>>", lambda _e, m=more_menu: self._show_menu_help(m))
        more_menu.bind("<Unmap>", lambda _e: self._restore_status_after_menu(), add="+")

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(action_row, textvariable=self.status_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=12)
        self.thinking = ThinkingIndicator(action_row)
        # Hidden until a review starts.

        self.copy_btn = ttk.Menubutton(action_row, text="Copy")
        self.copy_btn.pack(side=tk.RIGHT)
        tip(self.copy_btn, "Copy the output: the selection, everything, or a prompt to paste into another LLM.")
        copy_menu = tk.Menu(self.copy_btn, tearoff=0)
        self.copy_btn["menu"] = copy_menu
        copy_entries: list[MenuEntry] = []
        copies = [
            ("copy_selected_btn", "Copy selected", self.copy_selected, "Highlighted text"),
            ("copy_all_btn", "Copy all", self.copy_all, "Whole output"),
            ("copy_llm_btn", "Copy as LLM prompt", self.copy_as_llm_prompt, "Findings as a fix-it prompt"),
        ]
        for index, (attr, label, command, short) in enumerate(copies):
            copy_menu.add_command(label=label, accelerator=short, command=command)
            entry = MenuEntry(copy_menu, index, self.copy_btn, copy_entries)
            setattr(self, attr, entry)
        for entry in copy_entries:
            entry.configure(state=tk.DISABLED)

        self._build_status_bar()

        output_box = Section(tab, "Output")
        output_box.pack(fill=tk.BOTH, expand=True)
        self.output_box = output_box
        body = output_box.body

        # Shown only while a review is running. No padding or border around it,
        # so the dark console runs to the edges of the section.
        self.live_frame = tk.Frame(body, background=TERMINAL_BG)
        self.live_text = scrolledtext.ScrolledText(
            self.live_frame,
            height=10,
            wrap=tk.WORD,
            font=("Consolas", 9),
            background=TERMINAL_BG,
            foreground="#c9d1d9",
            insertbackground="#c9d1d9",
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            padx=12,
            pady=10,
        )
        self.live_text.frame.configure(background=TERMINAL_BG, borderwidth=0, highlightthickness=0)
        self.live_text.pack(fill=tk.BOTH, expand=True)
        self.live_text.tag_configure("status", foreground="#8b949e", font=("Consolas", 9, "italic"))
        self.live_text.tag_configure("thinking", foreground="#d2a8ff")
        self.live_text.tag_configure("text", foreground="#7ee787")
        self.live_text.configure(state=tk.DISABLED)

        self.explain_frame = ttk.Frame(body)
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
        soften_text(self.explain_text)
        self.explain_text.configure(padx=12, pady=10)
        self.explain_text.pack(fill=tk.BOTH, expand=True)
        enable_selection_copy(self.explain_text)

        self.diff_view = DiffReviewView(body)
        self.diff_view.pack(fill=tk.BOTH, expand=True)
        self.diff_view.on_submit_review = self.open_submit_review
        # Keep a hidden plain buffer for fallback copy of raw review text.
        self._last_review_raw = ""
        self._last_explanation = ""
        self._output_mode = "diff"  # diff | explain | live


        self.pr_url_var.trace_add("write", lambda *_: self.on_pr_url_changed())
        self._restore_cached_auth_lights()
        self.after(200, self.refresh_auth_lights)

    def _build_status_bar(self) -> None:
        """Token usage on the left; auth status and app-level tools on the right."""
        bar = self.status_bar
        self.usage_var = tk.StringVar(value="Tokens — last call: — · this run: — · session: —")
        usage = ttk.Label(bar, textvariable=self.usage_var, style="Muted.TLabel")
        usage.pack(side=tk.LEFT)
        tip(usage, "Input / output tokens for the most recent Claude call, the current run, and since the app opened.")

        right = ttk.Frame(bar)
        right.pack(side=tk.RIGHT)

        def chip(label_var: tk.StringVar, command) -> tuple[StatusLight, Tooltip]:  # noqa: ANN001
            light = StatusLight(right, size=14)
            light.pack(side=tk.LEFT, padx=(10, 2))
            link = LinkButton(right, command, textvariable=label_var)
            link.pack(side=tk.LEFT)
            return light, Tooltip(link)

        self.github_auth_label_var = tk.StringVar(value="GitHub")
        self.github_auth_light, self.github_tooltip = chip(self.github_auth_label_var, self.quick_github_login)
        self.github_tooltip.set("Click to sign in with GitHub")
        self.claude_auth_label_var = tk.StringVar(value="Claude")
        self.claude_auth_light, self.claude_tooltip = chip(self.claude_auth_label_var, self.quick_claude_login)
        self.claude_tooltip.set("Click to sign in with Claude SSO")

        ttk.Separator(right, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)
        tip(
            LinkButton(right, self.refresh_auth_lights, text="Refresh"),
            "Re-check the GitHub and Claude sign-in status.",
        ).pack(side=tk.LEFT)
        self.doctor_btn = LinkButton(right, self.start_doctor, text="Doctor")
        self.doctor_btn.pack(side=tk.LEFT, padx=(8, 0))
        tip(
            self.doctor_btn,
            "Diagnose setup: Claude/gh/GitHub/Jira access, model ids, prompts and the repo's pr-review.yml.",
        )
        tip(
            LinkButton(right, self.clear_all_auths, text="Clear auth"),
            "Sign out of GitHub and Claude for this app (clears WSL/Windows GitHub credentials and the Claude SSO session).",
        ).pack(side=tk.LEFT, padx=(8, 0))
        tip(
            LinkButton(right, self.open_settings, text="Settings"),
            "Tokens, sign-in options, review defaults and Jira settings.",
        ).pack(side=tk.LEFT, padx=(8, 0))

    def _update_advanced_summary(self) -> None:
        """Show overrides next to the collapsed header so they aren't hidden."""
        picks = [
            ("model", self.model_choice_var),
            ("effort", self.effort_var),
            ("mode", self.mode_choice_var),
            ("verify", self.verify_var),
        ]
        overrides = [f"{name}: {var.get()}" for name, var in picks if var.get() != "auto"]
        self.advanced_options.summary_var.set(" · ".join(overrides) if overrides else "all auto")

    def _show_menu_help(self, menu: tk.Menu) -> None:
        try:
            index = menu.index("active")
        except tk.TclError:
            index = None
        if index is None:
            self._restore_status_after_menu()
            return
        label = menu.entrycget(index, "label")
        if label in self._menu_help:
            if not hasattr(self, "_status_before_menu"):
                self._status_before_menu = self.status_var.get()
            self.status_var.set(self._menu_help[label])

    def _restore_status_after_menu(self) -> None:
        previous = getattr(self, "_status_before_menu", None)
        if previous is not None and self.status_var.get() in self._menu_help.values():
            self.status_var.set(previous)
        self.__dict__.pop("_status_before_menu", None)

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

        config = load_config()
        token = config.get("github_token") or ""
        use_wsl = bool(config.get("use_wsl_github_auth", True))

        def worker() -> None:
            from app.auth_flows import check_claude_auth, check_github_auth

            gh_ok, gh_detail = check_github_auth(token, use_wsl=use_wsl)
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
                return
            self._prefill_story_from_jira(url, token, config)

        threading.Thread(target=worker, daemon=True).start()

    def _prefill_story_from_jira(self, url: str, token: str, config: dict) -> None:
        """Worker thread: put the referenced tickets' summary and acceptance
        criteria in an empty Story box. It stays editable; the review also
        gets the full tickets in their own section."""
        from app.jira import fetch_pr_tickets

        try:
            diff = fetch_pull_request(url, token=token)
            tickets, _keys, _note = fetch_pr_tickets(
                config, title=diff.title, body=diff.body, head_branch=diff.head_branch
            )
        except Exception:  # noqa: BLE001 -- prefill is a convenience
            return
        if not tickets:
            return
        blocks = []
        for t in tickets:
            block = f"{t.key}: {t.summary}"
            if t.acceptance_criteria:
                block += f"\nAcceptance criteria:\n{t.acceptance_criteria.strip()}"
            blocks.append(block)
        text = "\n\n".join(blocks)

        def apply() -> None:
            if not self.story_text.get("1.0", "end-1c").strip():
                self.story_text.insert("1.0", text)
                self.status_var.set(f"Repo reachable; Story pre-filled from {', '.join(t.key for t in tickets)}")

        self.after(0, apply)

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

    def cycle_next(self) -> None:
        self.cycler.next()
        self._update_prompt_cycle_view()

    def cycle_prev(self) -> None:
        self.cycler.prev()
        self._update_prompt_cycle_view()

    def start_review(self) -> None:
        self._start_review_job(inspect_only=False)

    def start_inspect(self) -> None:
        self._start_review_job(inspect_only=True)

    def _review_options(self) -> ReviewOptions:
        def pick(var: tk.StringVar) -> str | None:
            value = var.get().strip()
            return None if value in ("", "auto") else value

        verify = pick(self.verify_var)
        return ReviewOptions(
            model=pick(self.model_choice_var),
            effort=pick(self.effort_var),
            mode=pick(self.mode_choice_var),
            verify=None if verify is None else verify == "on",
            follow_up=bool(self.follow_up_var.get()),
        )

    def _claude_event_handler(self):
        def on_claude_event(event: dict) -> None:
            kind = str(event.get("kind") or "")
            if kind == "usage":
                self.after(0, lambda e=dict(event): self._record_usage(e))
                return
            text = str(event.get("text") or "")
            self.after(0, lambda k=kind, t=text: self._append_live_claude(k, t))

        return on_claude_event

    def _start_review_job(self, *, inspect_only: bool) -> None:
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
        prompt = self.cycler.current
        config = load_config()
        options = self._review_options()
        history = list_reviews_for_pr(url)
        if not inspect_only:
            self._pending_history = {
                "mode": "review",
                "pr_url": url,
                "story": story,
                "reviewer_prompt_id": prompt.id if prompt else "",
                "reviewer_prompt_name": prompt.name if prompt else "",
                "reviewer_prompt_content": prompt.content if prompt else "",
            }
        self._busy = True
        self._set_run_buttons_enabled(False)
        self.status_var.set("Inspecting" if inspect_only else "Gathering review context")
        self.thinking.start("Gathering context")
        self.diff_view.clear()
        self._last_review_raw = ""
        self._last_explanation = ""
        self._set_copy_buttons_enabled(False)
        self._show_panel("live")
        self._clear_live_claude()
        self._reset_job_usage()
        on_event = self._claude_event_handler()

        def worker() -> None:
            try:
                prep = prepare_review(
                    pr_url=url, story=story, reviewer_prompt=prompt, config=config, options=options,
                    history_entries=history, on_event=on_event,
                )
                refine_estimate(prep, config)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: self._review_failure(e))
                return
            self.after(0, lambda: self._review_prepared(prep, config, inspect_only))

        threading.Thread(target=worker, daemon=True).start()

    def _estimate_text(self, prep: ReviewPrep) -> str:
        tokens = f"{prep.est_tokens:,} input tokens{'' if prep.counted else ' (estimated)'}"
        if prep.est_cost is None:
            return f"unpriced model, {tokens}"
        return f"≈ ${prep.est_cost:.2f} ({tokens}, {prep.settings.model}, effort {prep.settings.effort})"

    def _review_prepared(self, prep: ReviewPrep, config: dict, inspect_only: bool) -> None:
        if inspect_only:
            self.thinking.stop()
            self._busy = False
            self._set_run_buttons_enabled(True)
            self._explain_success(
                inspect_report(prep), [f"Inspect: {prep.diff.ref.full_name}#{prep.diff.ref.number}"],
                title="Inspect (no review run)", done_status="Inspect ready: nothing was sent to Claude",
                save_history=False,
            )
            return
        while True:
            try:
                limit = float(config.get("warn_review_usd") or 0)
            except (TypeError, ValueError):
                limit = 0.0
            over = bool(limit and prep.est_cost is not None and prep.est_cost > limit)
            self._append_live_claude("status", f"estimate {self._estimate_text(prep)}")
            if not prep.warnings and not over:
                break
            gate = RunGateDialog(
                self, warnings=prep.warnings, estimate=self._estimate_text(prep), over_budget=over,
                steps=budget_fit_steps(verification=prep.settings.verify, agentic=prep.agentic,
                                       effort=prep.settings.effort),
            )
            self.wait_window(gate)
            if gate.choice == "run":
                break
            if not isinstance(gate.choice, dict):
                self.thinking.stop()
                self._pending_history = None
                self._busy = False
                self._set_run_buttons_enabled(True)
                self._show_panel("diff")
                self.status_var.set("Review cancelled before anything was sent")
                return
            changes = gate.choice
            if "verify" in changes:
                prep.settings.verify = bool(changes["verify"])
            if "agentic" in changes:
                prep.settings.mode = "single-shot"
            if "effort" in changes:
                prep.settings.effort = str(changes["effort"])
            prep.warnings = []  # already acknowledged
            prep.build_parts()
        self.status_var.set("Review in progress")
        self.thinking.start("Asking Claude")
        on_event = self._claude_event_handler()

        def worker() -> None:
            try:
                run = execute_review(prep, config, on_event)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                partial = getattr(exc, "partial", "")
                self.after(0, lambda e=err, p=partial: self._review_failure(e + (f"\n\nPartial output:\n{p}" if p else "")))
                return
            self.after(0, lambda: self._review_success(run))

        threading.Thread(target=worker, daemon=True).start()

    def start_doctor(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_run_buttons_enabled(False)
        self.status_var.set("Running Doctor checks")
        self.thinking.start("Checking")
        self._clear_live_claude()
        self._show_panel("live")
        config = load_config()
        url = self.pr_url_var.get().strip()

        def on_check(check) -> None:  # noqa: ANN001
            line = format_report([check])
            self.after(0, lambda t=line: self._append_live_claude("text", t + "\n"))

        def worker() -> None:
            try:
                report = format_report(run_doctor(config, url, on_check=on_check))
            except Exception as exc:  # noqa: BLE001
                report = f"Doctor failed: {exc}"
            self.after(0, lambda: self._doctor_done(report))

        threading.Thread(target=worker, daemon=True).start()

    def _doctor_done(self, report: str) -> None:
        self.thinking.stop()
        self._busy = False
        self._set_run_buttons_enabled(True)
        self._explain_success(report, ["Doctor"], title="Doctor", done_status="Doctor finished", save_history=False)

    def _refresh_follow_up_info(self) -> None:
        url = self.pr_url_var.get().strip()
        count = len(list_reviews_for_pr(url)) if url else 0
        if count:
            self.follow_up_info_var.set(f"({count} previous review(s) of this PR in history)")
        else:
            self.follow_up_info_var.set("(no previous reviews of this PR yet)" if url else "")

    def start_explain(self) -> None:
        self._start_claude_job(mode="explain")

    def start_ask(self) -> None:
        self._start_claude_job(mode="ask")

    def start_bot_check(self) -> None:
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

        config = load_config()
        self._busy = True
        self._set_run_buttons_enabled(False)
        self.status_var.set("Checking for CodeRabbit / Amazon Q and reviewer comments")
        self.thinking.start("Checking review comments")
        self.diff_view.clear()
        self._clear_live_claude()
        self._reset_job_usage()
        self._show_panel("live")

        def on_claude_event(event: dict) -> None:
            kind = str(event.get("kind") or "")
            if kind == "usage":
                self.after(0, lambda e=dict(event): self._record_usage(e))
                return
            text = str(event.get("text") or "")
            self.after(0, lambda k=kind, t=text: self._append_live_claude(k, t))

        def worker() -> None:
            try:
                diff, findings, token = run_bot_comment_check(
                    pr_url=url, config=config, on_event=on_claude_event
                )
                self.after(
                    0,
                    lambda d=diff, f=findings, t=token, c=config: self._bot_check_success(d, f, t, c),
                )
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: self._bot_check_failure(e))

        threading.Thread(target=worker, daemon=True).start()

    def _bot_check_success(
        self, diff: PullRequestDiff, findings: list[BotFinding], token: str, config: dict
    ) -> None:
        self.thinking.stop()
        self._busy = False
        self._set_run_buttons_enabled(True)
        pr_ref = f"{diff.ref.full_name}#{diff.ref.number}"
        if not findings:
            self.status_var.set(f"Done — no bot or reviewer comments found on {pr_ref}.")
            messagebox.showinfo(
                "No comments found",
                f"No CodeRabbit/Amazon Q comments or human reviewer feedback were found on {pr_ref}.",
            )
            return
        valid_count = sum(1 for f in findings if f.verdict.valid)
        self.status_var.set(
            f"Done — {len(findings)} comment(s) on {pr_ref}, "
            f"{valid_count} valid. Review below."
        )
        BotReviewDialog(self, diff=diff, findings=findings, token=token, config=config)

    def _bot_check_failure(self, error: str) -> None:
        self.thinking.stop()
        self._busy = False
        self._set_run_buttons_enabled(True)
        self.status_var.set("Failed")
        messagebox.showerror("Review comment check failed", error)

    def _set_run_buttons_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        self.run_btn.configure(state=state)
        self.inspect_btn.configure(state=state)
        self.doctor_btn.configure(state=state)
        self.explain_btn.configure(state=state)
        self.ask_btn.configure(state=state)
        self.bot_check_btn.configure(state=state)
        self.merge_conflict_btn.configure(state=state)
        self.lint_fix_btn.configure(state=state)

    def start_merge_conflict_check(self) -> None:
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

        config = load_config()
        self._busy = True
        self._set_run_buttons_enabled(False)
        self.status_var.set("Checking for merge conflicts")
        self.thinking.start("Checking merge conflicts")
        self.diff_view.clear()
        self._clear_live_claude()
        self._reset_job_usage()
        self._show_panel("live")

        def on_claude_event(event: dict) -> None:
            kind = str(event.get("kind") or "")
            if kind == "usage":
                self.after(0, lambda e=dict(event): self._record_usage(e))
                return
            text = str(event.get("text") or "")
            self.after(0, lambda k=kind, t=text: self._append_live_claude(k, t))

        def worker() -> None:
            session: ConflictSession | None = None
            try:
                token, _source = resolve_github_token(
                    explicit_token=config.get("github_token") or "",
                    use_wsl=bool(config.get("use_wsl_github_auth", True)),
                )
                on_claude_event({"kind": "status", "text": "fetching pull request"})
                diff = fetch_pull_request(url, token=token)
                info, session, conflicts = open_conflict_session(
                    diff, token, on_event=on_claude_event
                )
                for i, conflict in enumerate(conflicts, start=1):
                    if conflict.kind != "content":
                        continue
                    on_claude_event(
                        {
                            "kind": "status",
                            "text": f"resolving conflict {i}/{len(conflicts)}: {conflict.path}",
                        }
                    )
                    draft_resolution(diff, info, conflict, config, on_event=on_claude_event)
                self.after(
                    0,
                    lambda d=diff, i=info, s=session, c=conflicts, tok=token: self._merge_conflict_success(
                        d, i, s, c, tok, config
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                if session is not None:
                    session.close()
                err = str(exc)
                self.after(0, lambda e=err: self._merge_conflict_failure(e))

        threading.Thread(target=worker, daemon=True).start()

    def _merge_conflict_success(
        self,
        diff: PullRequestDiff,
        info: MergeabilityInfo,
        session: ConflictSession | None,
        conflicts: list[ConflictFile],
        token: str,
        config: dict,
    ) -> None:
        self.thinking.stop()
        self._busy = False
        self._set_run_buttons_enabled(True)
        pr_ref = f"{diff.ref.full_name}#{diff.ref.number}"
        if not info.has_conflicts:
            state_notes = {
                "clean": "no conflicts — this PR merges cleanly.",
                "behind": "no conflicts — the PR branch is just behind the base branch.",
                "unknown": "GitHub couldn't determine mergeability yet — try again in a moment.",
                "blocked": "no merge conflicts, but the merge is blocked (branch protection rules).",
                "unstable": "no merge conflicts (failing status checks).",
                "draft": "no merge conflicts (this PR is a draft).",
            }
            note = state_notes.get(info.mergeable_state, f"mergeable_state={info.mergeable_state}")
            self.status_var.set(f"Done — {pr_ref}: {note}")
            messagebox.showinfo("No merge conflicts", f"{pr_ref}: {note}")
            return
        self.status_var.set(
            f"Done — {len(conflicts)} conflicting file(s) on {pr_ref}. Review below."
        )
        assert session is not None
        MergeConflictDialog(
            self, diff=diff, info=info, session=session, conflicts=conflicts, token=token, config=config
        )

    def _merge_conflict_failure(self, error: str) -> None:
        self.thinking.stop()
        self._busy = False
        self._set_run_buttons_enabled(True)
        self.status_var.set("Failed")
        messagebox.showerror("Merge conflict check failed", error)

    def start_lint_check(self) -> None:
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

        config = load_config()
        self._busy = True
        self._set_run_buttons_enabled(False)
        self.status_var.set("Scanning changed files for lint issues")
        self.thinking.start("Checking lint")
        self.diff_view.clear()
        self._clear_live_claude()
        self._reset_job_usage()
        self._show_panel("live")

        def on_claude_event(event: dict) -> None:
            kind = str(event.get("kind") or "")
            if kind == "usage":
                self.after(0, lambda e=dict(event): self._record_usage(e))
                return
            text = str(event.get("text") or "")
            self.after(0, lambda k=kind, t=text: self._append_live_claude(k, t))

        def worker() -> None:
            try:
                diff, findings, token = run_lint_check(
                    pr_url=url, config=config, on_event=on_claude_event
                )
                self.after(
                    0,
                    lambda d=diff, f=findings, t=token: self._lint_check_success(d, f, t),
                )
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: self._lint_check_failure(e))

        threading.Thread(target=worker, daemon=True).start()

    def _lint_check_success(
        self, diff: PullRequestDiff, findings: list[LintFinding], token: str
    ) -> None:
        self.thinking.stop()
        self._busy = False
        self._set_run_buttons_enabled(True)
        pr_ref = f"{diff.ref.full_name}#{diff.ref.number}"
        if not findings:
            self.status_var.set(f"Done — no lint issues found on the changed lines in {pr_ref}.")
            messagebox.showinfo(
                "No lint issues found",
                f"No lint/style issues were found on the lines changed in {pr_ref}.",
            )
            return
        fixable_count = sum(1 for f in findings if f.fix is not None and f.safe)
        self.status_var.set(
            f"Done — {len(findings)} file(s) with lint issues on {pr_ref}, "
            f"{fixable_count} with a fix ready. Review below."
        )
        LintFixDialog(self, diff=diff, findings=findings, token=token)

    def _lint_check_failure(self, error: str) -> None:
        self.thinking.stop()
        self._busy = False
        self._set_run_buttons_enabled(True)
        self.status_var.set("Failed")
        messagebox.showerror("Lint check failed", error)

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
        self._reset_job_usage()

        def on_claude_event(event: dict) -> None:
            kind = str(event.get("kind") or "")
            if kind == "usage":
                self.after(0, lambda e=dict(event): self._record_usage(e))
                return
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
                    raise ValueError(f"Unknown job mode {mode!r}; peer reviews go through start_review.")
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

    def _reset_job_usage(self) -> None:
        self._call_usage = (0, 0)
        self._job_usage = {"input": 0, "output": 0}
        self._update_usage_label()

    def _record_usage(self, event: dict) -> None:
        try:
            input_tokens = int(event.get("input_tokens") or 0)
            output_tokens = int(event.get("output_tokens") or 0)
        except (TypeError, ValueError):
            return
        try:
            cache_read = int(event.get("cache_read_tokens") or 0)
            cost = float(event["cost_usd"]) if event.get("cost_usd") else 0.0
        except (TypeError, ValueError):
            cache_read, cost = 0, 0.0
        self._call_usage = (input_tokens, output_tokens)
        self._job_usage["input"] += input_tokens
        self._job_usage["output"] += output_tokens
        self._job_usage["cache_read"] = self._job_usage.get("cache_read", 0) + cache_read
        self._job_usage["usd"] = self._job_usage.get("usd", 0.0) + cost
        self._session_usage["input"] += input_tokens
        self._session_usage["output"] += output_tokens
        self._session_usage["usd"] = self._session_usage.get("usd", 0.0) + cost
        self._update_usage_label()

    def _update_usage_label(self) -> None:
        call_in, call_out = self._call_usage
        run_usd = self._job_usage.get("usd", 0.0)
        session_usd = self._session_usage.get("usd", 0.0)
        self.usage_var.set(
            f"Tokens — last call: {call_in:,} in / {call_out:,} out   ·   "
            f"this run: {self._job_usage['input']:,} in / {self._job_usage['output']:,} out"
            + (f" (${run_usd:.3f})" if run_usd else "")
            + f"   ·   session: {self._session_usage['input']:,} in / {self._session_usage['output']:,} out"
            + (f" (${session_usd:.3f})" if session_usd else "")
        )

    def _usage_footer(self, run: ReviewRun) -> str:
        """Footer for the posted summary: model, effort, mode, tokens, cache %, cost."""
        prep = run.prep
        total_in = self._job_usage["input"] + self._job_usage.get("cache_read", 0)
        cache_pct = round(100 * self._job_usage.get("cache_read", 0) / total_in) if total_in else 0
        usd = self._job_usage.get("usd", 0.0) or (run.cost_usd or 0.0)
        return (
            f"Peer Review App · tier `{prep.settings.tier}` · {prep.settings.model} · effort {prep.settings.effort} · "
            f"{'agentic' if prep.agentic else 'single-shot'}{' + verification' if prep.settings.verify else ''} · "
            f"{self._job_usage['input']:,} in / {self._job_usage['output']:,} out · {cache_pct}% from cache"
            + (f" · ${usd:.3f}" if usd else "")
        )

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
        head_sha: str = "",
        base_sha: str = "",
        review: dict | None = None,
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
                head_sha=head_sha,
                base_sha=base_sha,
                review=review,
            )
            self.refresh_history_ui()
            self._refresh_follow_up_info()
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

    def _review_success(self, run: ReviewRun) -> None:
        self.thinking.stop()
        try:
            self._last_run = run
            diff, result = run.prep.diff, run.result
            summary = review_summary_lines(run)
            readable = "\n".join(
                [f"{result.headline()}: {result.verdict_reason}".strip(": "), result.scope_note, result.tests_note, ""]
            ).strip() + "\n\n" + comments_to_copy_text(result.findings)
            if result.report:
                readable += "\n\n" + result.report
            if result.verdict == INCONCLUSIVE:
                readable += "\n\nRaw model output:\n\n" + run.raw_text
            self._last_review_raw = format_copy_friendly(comments_to_copy_text(result.findings) or run.raw_text)
            self._last_explanation = ""
            self.diff_view.render_result(diff=diff, result=result, summary_lines=summary)
            self._show_panel("diff")
            count = len(result.findings)
            self.status_var.set(
                f"Done: {result.headline()}, {count} finding(s)" + (" under matching lines" if count else "")
            )
            self._set_copy_buttons_enabled(self.diff_view.has_review_content())
            self._record_history(
                result=readable.strip() or run.raw_text,
                summary_lines=summary,
                pr_title=diff.title,
                pr_ref=f"{diff.ref.full_name}#{diff.ref.number}",
                mode="review",
                head_sha=diff.head_sha,
                base_sha=diff.base_sha,
                review=review_to_history(run),
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

    def open_submit_review(self) -> None:
        diff = self.diff_view.get_diff()
        comments = self.diff_view.get_comments()
        run = getattr(self, "_last_run", None)
        if diff is None or run is None or run.prep.diff is not diff:
            messagebox.showinfo("Submit to GitHub", "Run a peer review first.")
            return
        config = load_config()
        try:
            token, _auth_source = resolve_github_token(
                explicit_token=config.get("github_token") or "",
                use_wsl=bool(config.get("use_wsl_github_auth", True)),
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("GitHub authentication", str(exc))
            return
        if not diff.head_sha:
            messagebox.showerror(
                "Submit to GitHub",
                "Missing the PR's head commit SHA — re-run the peer review and try again.",
            )
            return
        SubmitReviewDialog(self, run=run, findings=list(comments), token=token, footer=self._usage_footer(run))

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

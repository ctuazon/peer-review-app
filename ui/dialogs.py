"""Author-side dialogs: prompt editor, bot comments, merge conflicts, lint."""
from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, scrolledtext, ttk


from app.bot_review import (
    BotFinding,
    apply_bot_fixes_batch,
    post_bot_reply,
)
from app.cost import EFFORTS
from app.github_pr import PullRequestDiff, parse_pr_url
from app.history_store import (
    list_history,
)
from app.lint_fix import LintFinding, apply_lint_fixes_batch
from app.merge_conflict import (
    ConflictFile,
    ConflictSession,
    MergeabilityInfo,
)
from app.prompts_store import (
    GENERIC_REPO_TYPE,
    Prompt,
    create_prompt,
    list_prompts,
    update_prompt,
)
from ui.widgets import Tooltip, tip


def _known_repo_types() -> list[str]:
    """Repo-type suggestions: exact repo names seen in past PRs, plus repo
    types already used by other prompts. Picking one of these guarantees a
    match in prompts_for_repo() (exact repo-name match), instead of guessing
    a token that may not appear in the actual GitHub repo slug."""
    seen: dict[str, str] = {}

    for entry in list_history():
        if not entry.pr_url:
            continue
        try:
            ref = parse_pr_url(entry.pr_url)
        except ValueError:
            continue
        seen.setdefault(ref.repo.lower(), ref.repo)

    for prompt in list_prompts():
        if prompt.is_generic or prompt.repo_type.lower() == GENERIC_REPO_TYPE:
            continue
        seen.setdefault(prompt.repo_type.lower(), prompt.repo_type)

    return sorted(seen.values(), key=str.lower)


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
        ttk.Combobox(
            repo_row,
            textvariable=self.repo_var,
            width=28,
            values=_known_repo_types(),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            repo_row,
            text="(pick the exact repo name from a past PR, e.g. marketplace-ssr)",
        ).pack(side=tk.LEFT, padx=8)

        self.generic_var = tk.BooleanVar(
            value=prompt.is_generic if prompt else True
        )
        ttk.Checkbutton(
            frame,
            text="Generic reviewer (applies to all repos)",
            variable=self.generic_var,
            command=self._on_generic_toggle,
        ).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Minimum effort").grid(row=3, column=0, sticky="w")
        self.min_effort_var = tk.StringVar(value=prompt.min_effort if prompt else "")
        effort_row = ttk.Frame(frame)
        effort_row.grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Combobox(
            effort_row, textvariable=self.min_effort_var, width=10, state="readonly", values=["", *EFFORTS]
        ).pack(side=tk.LEFT)
        ttk.Label(
            effort_row,
            text="(reviews with this prompt run at least this deep; a choice on the review tab still wins)",
        ).pack(side=tk.LEFT, padx=8)

        ttk.Label(frame, text="Prompt").grid(row=4, column=0, sticky="nw")
        self.content = scrolledtext.ScrolledText(frame, width=70, height=18, wrap=tk.WORD)
        self.content.grid(row=4, column=1, sticky="nsew", pady=4)
        if prompt:
            self.content.insert("1.0", prompt.content)

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=1, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        tip(
            ttk.Button(buttons, text="Save", command=self._save),
            "Save this prompt to data/prompts.json.",
        ).pack(side=tk.RIGHT)

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)
        self._on_generic_toggle()
        self.geometry("720x560")
        self.wait_visibility()
        self.focus_force()

    def _on_generic_toggle(self) -> None:
        if self.generic_var.get():
            self.repo_var.set(GENERIC_REPO_TYPE)
        elif self.repo_var.get().strip().lower() == GENERIC_REPO_TYPE:
            self.repo_var.set("")

    def _save(self) -> None:
        name = self.name_var.get().strip()
        content = self.content.get("1.0", "end-1c")
        is_generic = bool(self.generic_var.get())
        min_effort = self.min_effort_var.get()
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
                    min_effort=min_effort,
                )
            else:
                self.result = create_prompt(
                    name=name,
                    repo_type=repo_type,
                    content=content,
                    is_generic=is_generic,
                    min_effort=min_effort,
                )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save failed", str(exc), parent=self)
            return
        self.destroy()


class BotReviewDialog(tk.Toplevel):
    """Shows CodeRabbit / Amazon Q findings plus human reviewer feedback.
    Fixable findings get a checkbox to queue them; queued fixes are pushed as
    a single commit via Apply all."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        diff: PullRequestDiff,
        findings: list[BotFinding],
        token: str,
        config: dict,
    ) -> None:
        super().__init__(master)
        self.diff = diff
        self.token = token
        self.config = config
        self._findings = list(findings)
        self._queue_vars: dict[int, tk.BooleanVar] = {}
        self._reply_widgets: dict[int, tk.Text] = {}
        self._card_state: dict[int, dict] = {}
        self.title(f"Review comments — {diff.ref.full_name}#{diff.ref.number}")
        self.resizable(True, True)
        self.transient(master)
        self.geometry("880x680")

        header = ttk.Frame(self, padding=(12, 12, 12, 4))
        header.pack(fill=tk.X)
        valid_count = sum(1 for f in findings if f.verdict.valid)
        addressed_count = sum(1 for f in findings if not f.verdict.valid and f.verdict.addressed)
        not_applicable_count = len(findings) - valid_count - addressed_count
        fixable_count = sum(1 for f in findings if f.verdict.valid and f.verdict.fixes)
        ttk.Label(
            header,
            text=(
                f"{diff.title}\n"
                f"{len(findings)} comment(s) found (bots + reviewers) — {valid_count} judged valid, "
                f"{addressed_count} already addressed, {not_applicable_count} not applicable, "
                f"{fixable_count} with a proposed fix.\n"
                "Check the fixes you want, then Apply all pushes them as one commit."
            ),
            justify=tk.LEFT,
        ).pack(anchor="w")

        canvas = tk.Canvas(self, highlightthickness=0)
        vscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0), pady=(4, 12))

        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind(
            "<Enter>",
            lambda _e: canvas.bind_all(
                "<MouseWheel>",
                lambda ev: canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units"),
            ),
        )
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        for finding in findings:
            self._build_card(inner, finding)

        if fixable_count:
            bottom_bar = ttk.Frame(self, padding=(12, 0, 12, 4))
            bottom_bar.pack(fill=tk.X)
            self.apply_all_status_var = tk.StringVar(value="")
            ttk.Label(bottom_bar, textvariable=self.apply_all_status_var).pack(side=tk.LEFT)
            self.apply_all_btn = ttk.Button(
                bottom_bar, text="Apply all", state=tk.DISABLED, command=self._apply_all
            )
            self.apply_all_btn.pack(side=tk.RIGHT)
            Tooltip(
                self.apply_all_btn,
                "Commit every ticked fix to the PR branch as one commit. Replies post after the commit lands.",
            )

        ttk.Button(self, text="Close", command=self.destroy).pack(pady=(0, 10))

    def _make_scroll_text(
        self, parent: tk.Misc, *, height: int, wrap: str = tk.WORD, font=None
    ) -> tuple[ttk.Frame, tk.Text]:
        """A Text widget with an attached vertical scrollbar, for content that
        may run longer than the visible height."""
        frame = ttk.Frame(parent)
        kwargs: dict = {"height": height, "wrap": wrap}
        if font:
            kwargs["font"] = font
        text = tk.Text(frame, **kwargs)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        return frame, text

    def _build_card(self, parent: tk.Misc, finding: BotFinding) -> None:
        comment = finding.comment
        verdict = finding.verdict
        if comment.path:
            location = f"{comment.path}:{comment.line}"
        elif comment.kind == "review":
            location = f"PR review ({comment.review_state})" if comment.review_state else "PR review"
        else:
            location = "PR summary comment"

        card = ttk.LabelFrame(parent, text=f"[{comment.source}] {location}", padding=10)
        card.pack(fill=tk.X, expand=True, padx=4, pady=6)

        if verdict.valid:
            verdict_color, verdict_text = "#1a7f37", "VALID"
        elif verdict.addressed:
            verdict_color, verdict_text = "#57606a", "ALREADY ADDRESSED"
        else:
            verdict_color, verdict_text = "#cf222e", "NOT VALID"
        tk.Label(
            card, text=verdict_text, fg=verdict_color, font=("Segoe UI", 9, "bold")
        ).pack(anchor="w")

        ttk.Label(card, text=verdict.reason or "(no reason given)", wraplength=760, justify=tk.LEFT).pack(
            anchor="w", pady=(2, 6)
        )

        if verdict.valid and not verdict.fixes and verdict.fix_unavailable_reason:
            ttk.Label(
                card,
                text=verdict.fix_unavailable_reason,
                foreground="#9a6700",
                wraplength=760,
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(0, 6))

        original = ttk.LabelFrame(card, text="Original comment", padding=6)
        original.pack(fill=tk.X, pady=(0, 6))
        body = comment.body.strip() or "(empty)"
        original_frame, original_text = self._make_scroll_text(
            original, height=min(10, max(2, body.count("\n") + 2))
        )
        original_text.insert("1.0", body)
        original_text.configure(state=tk.DISABLED)
        original_frame.pack(fill=tk.BOTH, expand=True)

        has_fix = verdict.valid and bool(verdict.fixes)
        if has_fix:
            for fix in verdict.fixes:
                fix_box = ttk.LabelFrame(card, text=f"Proposed fix — {fix.path}", padding=6)
                fix_box.pack(fill=tk.X, pady=(0, 6))
                diff_text = fix.diff_text or "(no textual difference detected)"
                lines = diff_text.splitlines()
                shown_lines = lines[:60]
                shown = "\n".join(shown_lines)
                if len(lines) > 60:
                    shown += f"\n… ({len(lines) - 60} more lines)"
                fix_text = tk.Text(
                    fix_box,
                    height=min(20, max(4, len(shown_lines) + 1)),
                    wrap=tk.NONE,
                    font=("Consolas", 9),
                )
                fix_text.insert("1.0", shown)
                fix_text.configure(state=tk.DISABLED)
                fix_text.pack(fill=tk.X)

        reply_box = ttk.LabelFrame(card, text="Reply to post (edit before sending)", padding=6)
        reply_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        reply_body = verdict.reply_text or ""
        reply_frame, reply_widget = self._make_scroll_text(
            reply_box, height=min(8, max(3, reply_body.count("\n") + 2))
        )
        reply_widget.insert("1.0", reply_body)
        reply_frame.pack(fill=tk.BOTH, expand=True)

        status_var = tk.StringVar(value="")
        action_row = ttk.Frame(card)
        action_row.pack(fill=tk.X, pady=(4, 0))

        if has_fix:
            file_note = "1 file" if len(verdict.fixes) == 1 else f"{len(verdict.fixes)} files"
            queue_var = tk.BooleanVar(value=False)
            check = ttk.Checkbutton(
                action_row,
                text=f"Queue this fix ({file_note}) for Apply all",
                variable=queue_var,
                command=self._update_apply_all_state,
            )
            check.pack(side=tk.LEFT)
            self._queue_vars[id(finding)] = queue_var
            self._reply_widgets[id(finding)] = reply_widget
            self._card_state[id(finding)] = {"check": check, "status_var": status_var}
        else:
            reply_btn = ttk.Button(action_row, text="Post reply")
            reply_btn.configure(
                command=lambda f=finding, b=reply_btn, s=status_var, w=reply_widget: self._post_reply(
                    f, b, s, w
                )
            )
            reply_btn.pack(side=tk.LEFT)
            Tooltip(reply_btn, "Post the reply text above to this comment thread on GitHub.")

        ttk.Label(action_row, textvariable=status_var).pack(side=tk.LEFT, padx=(10, 0))

        if comment.html_url:
            link = ttk.Label(card, text="Open on GitHub", foreground="#0969da", cursor="hand2")
            link.pack(anchor="w", pady=(4, 0))
            link.bind("<Button-1>", lambda _e, u=comment.html_url: webbrowser.open(u))

    def _update_apply_all_state(self) -> None:
        n = sum(1 for var in self._queue_vars.values() if var.get())
        if n == 0:
            self.apply_all_btn.configure(state=tk.DISABLED, text="Apply all")
        else:
            self.apply_all_btn.configure(state=tk.NORMAL, text=f"Apply all ({n}) as one commit")

    def _apply_all(self) -> None:
        selected = [
            f
            for f in self._findings
            if id(f) in self._queue_vars and self._queue_vars[id(f)].get()
        ]
        if not selected:
            return

        for finding in selected:
            reply_widget = self._reply_widgets[id(finding)]
            reply_text = reply_widget.get("1.0", "end-1c").strip()
            if not reply_text:
                messagebox.showerror(
                    "Missing reply",
                    f"The reply text for the {finding.comment.source} comment on "
                    f"{finding.comment.path or 'the PR'} can't be empty.",
                    parent=self,
                )
                return
            finding.verdict.reply_text = reply_text

        file_count = sum(len(f.verdict.fixes) for f in selected)
        if not messagebox.askyesno(
            "Apply all",
            f"Apply {len(selected)} fix(es) across {file_count} file(s) as one commit to "
            f"{self.diff.head_branch}, then post {len(selected)} reply(ies)?\n\n"
            "This pushes directly to the PR branch and can't be undone from here.",
            parent=self,
        ):
            return

        self.apply_all_btn.configure(state=tk.DISABLED)
        self.apply_all_status_var.set("Applying…")
        for finding in selected:
            state = self._card_state[id(finding)]
            state["check"].configure(state=tk.DISABLED)
            state["status_var"].set("Queued…")

        def on_merge_event(event: dict) -> None:
            text = str(event.get("text") or "")
            if text:
                self.after(0, lambda t=text: self.apply_all_status_var.set(t))

        def worker() -> None:
            try:
                apply_bot_fixes_batch(
                    self.diff, selected, self.token, self.config, on_event=on_merge_event
                )
                self.after(0, lambda: self._batch_commit_succeeded(selected))
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: self._batch_failed(selected, e))

        threading.Thread(target=worker, daemon=True).start()

    def _batch_commit_succeeded(self, findings: list[BotFinding]) -> None:
        self.apply_all_status_var.set("Committed — posting replies…")
        for finding in findings:
            self._card_state[id(finding)]["status_var"].set("Committed — posting reply…")

        def worker() -> None:
            results: list[tuple[BotFinding, str | None]] = []
            for finding in findings:
                try:
                    post_bot_reply(self.diff, finding, self.token)
                    results.append((finding, None))
                except Exception as exc:  # noqa: BLE001
                    results.append((finding, str(exc)))
            self.after(0, lambda: self._batch_replies_done(results))

        threading.Thread(target=worker, daemon=True).start()

    def _batch_replies_done(self, results: list[tuple[BotFinding, str | None]]) -> None:
        failures = []
        for finding, error in results:
            state = self._card_state[id(finding)]
            reply_widget = self._reply_widgets[id(finding)]
            if error:
                state["status_var"].set("Committed, reply failed")
                failures.append(f"{finding.comment.source} ({finding.comment.path or 'PR'}): {error}")
            else:
                state["status_var"].set("Applied + replied ✓")
                reply_widget.configure(state=tk.DISABLED)
        self.apply_all_status_var.set("Done" if not failures else "Done, with reply failures")
        if failures:
            messagebox.showerror(
                "Some replies failed to post",
                "Code changes were committed, but these replies failed:\n\n" + "\n".join(failures),
                parent=self,
            )

    def _batch_failed(self, findings: list[BotFinding], error: str) -> None:
        for finding in findings:
            state = self._card_state[id(finding)]
            state["check"].configure(state=tk.NORMAL)
            state["status_var"].set("")
        self.apply_all_status_var.set("")
        self._update_apply_all_state()
        messagebox.showerror("Apply all failed", error, parent=self)

    def _post_reply(
        self,
        finding: BotFinding,
        button: ttk.Button,
        status_var: tk.StringVar,
        reply_widget: tk.Text,
    ) -> None:
        reply_text = reply_widget.get("1.0", "end-1c").strip()
        if not reply_text:
            messagebox.showerror("Missing reply", "The reply text can't be empty.", parent=self)
            return
        finding.verdict.reply_text = reply_text
        button.configure(state=tk.DISABLED)
        status_var.set("Posting…")

        def worker() -> None:
            try:
                post_bot_reply(self.diff, finding, self.token)
                self.after(
                    0,
                    lambda: self._card_action_succeeded(reply_widget, status_var, "Reply posted ✓"),
                )
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: self._card_action_failed(button, status_var, e))

        threading.Thread(target=worker, daemon=True).start()

    def _card_action_succeeded(
        self, reply_widget: tk.Text, status_var: tk.StringVar, message: str
    ) -> None:
        status_var.set(message)
        reply_widget.configure(state=tk.DISABLED)

    def _card_action_failed(self, button: ttk.Button, status_var: tk.StringVar, error: str) -> None:
        button.configure(state=tk.NORMAL)
        status_var.set("Failed")
        messagebox.showerror("Action failed", error, parent=self)


class MergeConflictDialog(tk.Toplevel):
    """Review Claude's proposed resolution for each conflicting file (content
    conflicts) and choose keep-vs-delete for structural ones, then push a
    real two-parent merge commit once approved. Every non-conflicting file
    in the merge was already resolved by git's own merge engine before this
    dialog opens -- only what git itself flagged as conflicting is shown."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        diff: PullRequestDiff,
        info: MergeabilityInfo,
        session: ConflictSession,
        conflicts: list[ConflictFile],
        token: str,
        config: dict,
    ) -> None:
        super().__init__(master)
        self.diff = diff
        self.info = info
        self.session = session
        self.token = token
        self.config = config
        self._conflicts = list(conflicts)
        self._resolution_widgets: dict[int, tk.Text] = {}
        self._delete_choice_vars: dict[int, tk.StringVar] = {}
        self.title(f"Merge conflicts — {diff.ref.full_name}#{diff.ref.number}")
        self.resizable(True, True)
        self.transient(master)
        self.geometry("900x700")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        header = ttk.Frame(self, padding=(12, 12, 12, 4))
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text=(
                f"{diff.title}\n"
                f"Merging {info.base_branch} into {info.head_branch} — "
                f"{len(conflicts)} conflicting file(s).\n"
                "Review each resolution below (edit freely), then Push merge commit."
            ),
            justify=tk.LEFT,
        ).pack(anchor="w")

        canvas = tk.Canvas(self, highlightthickness=0)
        vscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0), pady=(4, 12))

        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind(
            "<Enter>",
            lambda _e: canvas.bind_all(
                "<MouseWheel>",
                lambda ev: canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units"),
            ),
        )
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        for conflict in self._conflicts:
            self._build_card(inner, conflict)

        bottom_bar = ttk.Frame(self, padding=(12, 0, 12, 4))
        bottom_bar.pack(fill=tk.X)
        self.push_status_var = tk.StringVar(value="")
        ttk.Label(bottom_bar, textvariable=self.push_status_var).pack(side=tk.LEFT)
        self.push_btn = ttk.Button(
            bottom_bar, text="Push merge commit", command=self._push
        )
        self.push_btn.pack(side=tk.RIGHT)
        Tooltip(
            self.push_btn,
            "Push a merge commit with the accepted resolutions to the PR branch. "
            "Every other file keeps git's own merge result.",
        )

        ttk.Button(self, text="Close", command=self._on_close).pack(pady=(0, 10))

    def _make_scroll_text(
        self, parent: tk.Misc, *, height: int, wrap: str = tk.WORD, font=None
    ) -> tuple[ttk.Frame, tk.Text]:
        frame = ttk.Frame(parent)
        kwargs: dict = {"height": height, "wrap": wrap}
        if font:
            kwargs["font"] = font
        text = tk.Text(frame, **kwargs)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        return frame, text

    def _build_card(self, parent: tk.Misc, conflict: ConflictFile) -> None:
        card = ttk.LabelFrame(parent, text=conflict.path, padding=10)
        card.pack(fill=tk.X, expand=True, padx=4, pady=6)

        if conflict.kind == "delete_conflict":
            deleted_where = self.info.head_branch if conflict.deleted_side == "ours" else self.info.base_branch
            modified_where = self.info.base_branch if conflict.deleted_side == "ours" else self.info.head_branch
            kept_content = conflict.theirs_content if conflict.deleted_side == "ours" else conflict.ours_content
            ttk.Label(
                card,
                text=f"Deleted on {deleted_where}, modified on {modified_where}.",
                foreground="#9a6700",
                wraplength=820,
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(0, 6))

            choice_var = tk.StringVar(value="keep")
            self._delete_choice_vars[id(conflict)] = choice_var

            content_box = ttk.LabelFrame(card, text=f"Content on {modified_where}", padding=6)
            content_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
            body = kept_content or "(empty)"
            content_frame, content_text = self._make_scroll_text(
                content_box, height=min(16, max(3, body.count("\n") + 2)), font=("Consolas", 9)
            )
            content_text.insert("1.0", body)
            content_frame.pack(fill=tk.BOTH, expand=True)
            self._resolution_widgets[id(conflict)] = content_text

            def on_choice_change(text=content_text) -> None:
                text.configure(state=tk.NORMAL if choice_var.get() == "keep" else tk.DISABLED)

            ttk.Radiobutton(
                card,
                text="Keep the modified version (nothing intentional gets lost)",
                variable=choice_var,
                value="keep",
                command=on_choice_change,
            ).pack(anchor="w")
            ttk.Radiobutton(
                card,
                text="Honor the deletion (remove this file)",
                variable=choice_var,
                value="delete",
                command=on_choice_change,
            ).pack(anchor="w")
            return

        # kind == "content"
        if not conflict.resolved:
            ttk.Label(
                card,
                text=f"Could not auto-resolve: {conflict.unresolved_reason or 'unknown reason'}",
                foreground="#cf222e",
                wraplength=820,
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(0, 6))
        elif not conflict.safe:
            ttk.Label(
                card,
                text=f"Self-review flagged this resolution: {conflict.safety_notes}",
                foreground="#cf222e",
                wraplength=820,
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(0, 6))

        markers_box = ttk.LabelFrame(card, text="Original conflict markers", padding=6)
        markers_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        markers_frame, markers_text = self._make_scroll_text(
            markers_box,
            height=min(12, max(3, conflict.marker_text.count("\n") + 2)),
            wrap=tk.NONE,
            font=("Consolas", 9),
        )
        markers_text.insert("1.0", conflict.marker_text)
        markers_text.configure(state=tk.DISABLED)
        markers_frame.pack(fill=tk.BOTH, expand=True)

        resolution_box = ttk.LabelFrame(card, text="Proposed resolution (edit before pushing)", padding=6)
        resolution_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        body = conflict.resolved_content or ""
        resolution_frame, resolution_text = self._make_scroll_text(
            resolution_box, height=min(20, max(4, body.count("\n") + 2)), wrap=tk.NONE, font=("Consolas", 9)
        )
        resolution_text.insert("1.0", body)
        resolution_frame.pack(fill=tk.BOTH, expand=True)
        self._resolution_widgets[id(conflict)] = resolution_text

    def _collect_resolutions(self) -> bool:
        """Copies edited widget text back onto each conflict. Returns False
        (and shows an error) if a content conflict was left empty."""
        for conflict in self._conflicts:
            widget = self._resolution_widgets.get(id(conflict))
            if conflict.kind == "delete_conflict":
                choice = self._delete_choice_vars[id(conflict)].get()
                if choice == "delete":
                    conflict.resolved_content = None
                else:
                    conflict.resolved_content = widget.get("1.0", "end-1c") if widget else ""
                continue
            text = widget.get("1.0", "end-1c") if widget else ""
            if not text.strip():
                messagebox.showerror(
                    "Empty resolution",
                    f"The resolution for {conflict.path} can't be empty.",
                    parent=self,
                )
                return False
            conflict.resolved_content = text
        return True

    def _push(self) -> None:
        if not self._collect_resolutions():
            return
        if not messagebox.askyesno(
            "Push merge commit",
            f"Push a merge commit of {self.info.base_branch} into {self.info.head_branch} "
            f"on {self.diff.ref.full_name}#{self.diff.ref.number}?\n\n"
            "This pushes directly to the PR branch and can't be undone from here.",
            parent=self,
        ):
            return

        self.push_btn.configure(state=tk.DISABLED)
        self.push_status_var.set("Pushing…")

        def on_merge_event(event: dict) -> None:
            text = str(event.get("text") or "")
            if text:
                self.after(0, lambda t=text: self.push_status_var.set(t))

        def worker() -> None:
            try:
                message = (
                    f"Merge {self.info.base_branch} into {self.info.head_branch}\n\n"
                    "Conflict resolution drafted by Peer Review App / Claude, reviewed before push."
                )
                commit_sha = self.session.apply_and_push(message, on_event=on_merge_event)
                self.after(0, lambda sha=commit_sha: self._push_succeeded(sha))
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: self._push_failed(e))

        threading.Thread(target=worker, daemon=True).start()

    def _push_succeeded(self, commit_sha: str) -> None:
        self.push_status_var.set(f"Pushed {commit_sha[:12]} ✓")
        messagebox.showinfo(
            "Merge commit pushed",
            f"Pushed merge commit {commit_sha[:12]} to {self.info.head_branch}.",
            parent=self,
        )
        self.destroy()

    def _push_failed(self, error: str) -> None:
        self.push_btn.configure(state=tk.NORMAL)
        self.push_status_var.set("")
        messagebox.showerror("Push failed", error, parent=self)

    def _on_close(self) -> None:
        self.session.close()
        self.destroy()


class LintFixDialog(tk.Toplevel):
    """Shows lint/style issues Claude found on the lines this PR actually
    changed, plus its proposed fix per file. Queued fixes get pushed as a
    single commit via Apply all -- same batching UX as the bot-comment fix
    flow, minus the reply-to-a-comment step (there's no GitHub thread to
    reply to for a self-found lint issue)."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        diff: PullRequestDiff,
        findings: list[LintFinding],
        token: str,
    ) -> None:
        super().__init__(master)
        self.diff = diff
        self.token = token
        self._findings = list(findings)
        self._queue_vars: dict[int, tk.BooleanVar] = {}
        self._card_state: dict[int, dict] = {}
        self.title(f"Lint issues — {diff.ref.full_name}#{diff.ref.number}")
        self.resizable(True, True)
        self.transient(master)
        self.geometry("880x680")

        header = ttk.Frame(self, padding=(12, 12, 12, 4))
        header.pack(fill=tk.X)
        fixable_count = sum(1 for f in findings if f.fix is not None and f.safe)
        ttk.Label(
            header,
            text=(
                f"{diff.title}\n"
                f"{len(findings)} file(s) with lint issues on the lines this PR changed — "
                f"{fixable_count} with a fix ready.\n"
                "Check the fixes you want, then Apply all pushes them as one commit."
            ),
            justify=tk.LEFT,
        ).pack(anchor="w")

        canvas = tk.Canvas(self, highlightthickness=0)
        vscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0), pady=(4, 12))

        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind(
            "<Enter>",
            lambda _e: canvas.bind_all(
                "<MouseWheel>",
                lambda ev: canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units"),
            ),
        )
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        for finding in findings:
            self._build_card(inner, finding)

        if fixable_count:
            bottom_bar = ttk.Frame(self, padding=(12, 0, 12, 4))
            bottom_bar.pack(fill=tk.X)
            self.apply_all_status_var = tk.StringVar(value="")
            ttk.Label(bottom_bar, textvariable=self.apply_all_status_var).pack(side=tk.LEFT)
            self.apply_all_btn = ttk.Button(
                bottom_bar, text="Apply all", state=tk.DISABLED, command=self._apply_all
            )
            self.apply_all_btn.pack(side=tk.RIGHT)
            Tooltip(self.apply_all_btn, "Commit every ticked lint fix to the PR branch as one commit.")

        ttk.Button(self, text="Close", command=self.destroy).pack(pady=(0, 10))

    def _make_scroll_text(
        self, parent: tk.Misc, *, height: int, wrap: str = tk.WORD, font=None
    ) -> tuple[ttk.Frame, tk.Text]:
        frame = ttk.Frame(parent)
        kwargs: dict = {"height": height, "wrap": wrap}
        if font:
            kwargs["font"] = font
        text = tk.Text(frame, **kwargs)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        return frame, text

    def _build_card(self, parent: tk.Misc, finding: LintFinding) -> None:
        card = ttk.LabelFrame(parent, text=finding.path, padding=10)
        card.pack(fill=tk.X, expand=True, padx=4, pady=6)

        issues_text = "\n".join(f"- {issue}" for issue in finding.issues) or "(no detail given)"
        ttk.Label(card, text=issues_text, wraplength=760, justify=tk.LEFT).pack(
            anchor="w", pady=(0, 6)
        )

        if finding.fix is None:
            ttk.Label(
                card,
                text=finding.safety_notes or "No fix drafted.",
                foreground="#9a6700",
                wraplength=760,
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(0, 6))
            return

        if not finding.safe:
            ttk.Label(
                card,
                text=f"Self-review flagged this fix: {finding.safety_notes}",
                foreground="#cf222e",
                wraplength=760,
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(0, 6))

        fix_box = ttk.LabelFrame(card, text="Proposed fix", padding=6)
        fix_box.pack(fill=tk.X, pady=(0, 6))
        diff_text = finding.fix.diff_text or "(no textual difference detected)"
        lines = diff_text.splitlines()
        shown_lines = lines[:60]
        shown = "\n".join(shown_lines)
        if len(lines) > 60:
            shown += f"\n… ({len(lines) - 60} more lines)"
        fix_text = tk.Text(
            fix_box, height=min(20, max(4, len(shown_lines) + 1)), wrap=tk.NONE, font=("Consolas", 9)
        )
        fix_text.insert("1.0", shown)
        fix_text.configure(state=tk.DISABLED)
        fix_text.pack(fill=tk.X)

        if not finding.safe:
            return  # unsafe fixes are shown for transparency but can't be queued

        status_var = tk.StringVar(value="")
        action_row = ttk.Frame(card)
        action_row.pack(fill=tk.X, pady=(4, 0))
        queue_var = tk.BooleanVar(value=False)
        check = ttk.Checkbutton(
            action_row,
            text="Queue this fix for Apply all",
            variable=queue_var,
            command=self._update_apply_all_state,
        )
        check.pack(side=tk.LEFT)
        ttk.Label(action_row, textvariable=status_var).pack(side=tk.LEFT, padx=(10, 0))
        self._queue_vars[id(finding)] = queue_var
        self._card_state[id(finding)] = {"check": check, "status_var": status_var}

    def _update_apply_all_state(self) -> None:
        n = sum(1 for var in self._queue_vars.values() if var.get())
        if n == 0:
            self.apply_all_btn.configure(state=tk.DISABLED, text="Apply all")
        else:
            self.apply_all_btn.configure(state=tk.NORMAL, text=f"Apply all ({n}) as one commit")

    def _apply_all(self) -> None:
        selected = [
            f
            for f in self._findings
            if id(f) in self._queue_vars and self._queue_vars[id(f)].get()
        ]
        if not selected:
            return

        file_count = len(selected)
        if not messagebox.askyesno(
            "Apply all",
            f"Apply {file_count} lint fix(es) as one commit to {self.diff.head_branch}?\n\n"
            "This pushes directly to the PR branch and can't be undone from here.",
            parent=self,
        ):
            return

        self.apply_all_btn.configure(state=tk.DISABLED)
        self.apply_all_status_var.set("Applying…")
        for finding in selected:
            state = self._card_state[id(finding)]
            state["check"].configure(state=tk.DISABLED)
            state["status_var"].set("Queued…")

        def worker() -> None:
            try:
                apply_lint_fixes_batch(self.diff, selected, self.token)
                self.after(0, lambda: self._apply_succeeded(selected))
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: self._apply_failed(selected, e))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_succeeded(self, findings: list[LintFinding]) -> None:
        self.apply_all_status_var.set("Applied ✓")
        for finding in findings:
            self._card_state[id(finding)]["status_var"].set("Applied ✓")

    def _apply_failed(self, findings: list[LintFinding], error: str) -> None:
        for finding in findings:
            state = self._card_state[id(finding)]
            state["check"].configure(state=tk.NORMAL)
            state["status_var"].set("")
        self.apply_all_status_var.set("")
        self._update_apply_all_state()
        messagebox.showerror("Apply all failed", error, parent=self)



"""Review, edit and post findings to GitHub as one review.

Nothing posts until a button is clicked. The default action batches every
included finding into a single review with the summary as its body; single
cards can still be posted on their own.
"""
from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, scrolledtext, ttk
from typing import TYPE_CHECKING

from app.publish import (
    EVENTS,
    SummaryInput,
    already_posted,
    amend_previous,
    ensure_marker,
    inline_payload,
    post_rebuttal,
    post_single,
    publish_review,
    render_summary,
    reply_and_resolve,
)
from app.github_pr import post_issue_comment
from app.review_parse import ReviewComment
from app.review_prompts import fix_prompt
from app.review_schema import NOT_IN_DIFF, SEVERITY_LABELS
from ui.widgets import Tooltip, tip

if TYPE_CHECKING:
    from app.review import ReviewRun

STATUS_ICONS = {"fixed": "✅", "open": "⏳", "withdrawn": "↩", "changed": "✏"}
SEV_COLORS = {"blocker": "#cf222e", "major": "#bc4c00", "minor": "#0969da", "nit": "#6e7781"}


class SubmitReviewDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        *,
        run: "ReviewRun",
        findings: list[ReviewComment],
        token: str,
        footer: str = "",
    ) -> None:
        super().__init__(master)
        self.title("Submit review to GitHub")
        self.geometry("940x780")
        self.transient(master)

        self.run = run
        self.prep = run.prep
        self.diff = run.prep.diff
        self.token = token
        self.footer = footer
        self.findings = findings
        self._cards: dict[int, dict] = {}
        self._prior_vars: list[tuple[object, tk.BooleanVar, tk.StringVar]] = []
        self._busy = False

        r = run.result
        header = ttk.Frame(self, padding=(12, 10, 12, 4))
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text=f"{self.diff.ref.full_name}#{self.diff.ref.number}: {r.headline()}",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")
        if r.verdict_reason:
            ttk.Label(header, text=r.verdict_reason, wraplength=880, justify=tk.LEFT).pack(anchor="w")
        hint_row = ttk.Frame(header)
        hint_row.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(
            hint_row,
            text="Edit anything, untick what shouldn't go, then submit as one review. Nothing posts until you click.",
            foreground="#57606a",
        ).pack(side=tk.LEFT)
        if findings:
            ttk.Button(hint_row, text="Expand all", command=lambda: self._toggle_all(False)).pack(side=tk.RIGHT)
            ttk.Button(hint_row, text="Collapse all", command=lambda: self._toggle_all(True)).pack(
                side=tk.RIGHT, padx=(0, 6))
        if r.parse_mode == "legacy":
            tk.Label(
                header, text="Parsed via legacy fallback: the JSON output didn't parse, so titles and suggestions are missing.",
                background="#fff8c5", foreground="#9a6700", anchor="w", padx=6, pady=3,
            ).pack(fill=tk.X, pady=(4, 0))

        paned = ttk.Panedwindow(self, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=12)

        cards_outer = ttk.Frame(paned)
        canvas = tk.Canvas(cards_outer, highlightthickness=0)
        vscroll = ttk.Scrollbar(cards_outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind("<Enter>", lambda _e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        paned.add(cards_outer, weight=3)

        if not findings:
            ttk.Label(inner, text="No findings. The summary below posts as a comment-only review.",
                      padding=8).pack(anchor="w")
        for f in findings:
            self._build_card(inner, f)
        if r.prior:
            self._build_prior(inner)
        if r.disagreements or r.credits:
            self._build_others(inner)

        footer_box = ttk.LabelFrame(paned, text="Review summary (posted as the review body)", padding=6)
        paned.add(footer_box, weight=2)
        self.summary_text = scrolledtext.ScrolledText(footer_box, height=10, wrap=tk.WORD, font=("Consolas", 9))
        self.summary_text.pack(fill=tk.BOTH, expand=True)

        opts = ttk.Frame(footer_box)
        opts.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(opts, text="Event").pack(side=tk.LEFT)
        blockers = sum(1 for f in findings if f.severity == "blocker")
        self.event_var = tk.StringVar(value="COMMENT")
        ttk.Combobox(opts, textvariable=self.event_var, values=list(EVENTS), state="readonly", width=17).pack(
            side=tk.LEFT, padx=(4, 12)
        )
        if blockers:
            ttk.Label(opts, text=f"({blockers} blocker(s): REQUEST_CHANGES is available, never auto-approve)",
                      foreground="#57606a").pack(side=tk.LEFT)
        self.fix_prompts_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Include fix prompts", variable=self.fix_prompts_var,
                        command=self._regenerate_summary).pack(side=tk.LEFT, padx=(12, 0))

        opts2 = ttk.Frame(footer_box)
        opts2.pack(fill=tk.X, pady=(4, 0))
        has_previous = bool(self.prep.prior.own_review_id)
        self.supersede_var = tk.BooleanVar(value=has_previous)
        self.amend_var = tk.BooleanVar(value=False)
        sup = ttk.Checkbutton(opts2, text="Mark my previous review superseded", variable=self.supersede_var)
        sup.pack(side=tk.LEFT)
        amend = ttk.Checkbutton(opts2, text="Amend: delete my older inline comments", variable=self.amend_var)
        amend.pack(side=tk.LEFT, padx=(12, 0))
        if not has_previous:
            sup.state(["disabled"])
            amend.state(["disabled"])
        tip(
            ttk.Button(opts2, text="Regenerate summary", command=self._regenerate_summary),
            "Rebuild the review body from the findings currently ticked. Replaces manual edits to the summary.",
        ).pack(side=tk.RIGHT)

        bottom = ttk.Frame(self, padding=(12, 6, 12, 10))
        bottom.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Checking what's already on the PR…")
        ttk.Label(bottom, textvariable=self.status_var, foreground="#57606a").pack(side=tk.LEFT)
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        self.submit_btn = ttk.Button(bottom, text="Submit selected as one review", command=self._submit_all)
        self.submit_btn.pack(side=tk.RIGHT, padx=6)
        Tooltip(
            self.submit_btn,
            "Post the summary and every ticked finding as a single GitHub review (one notification for the author).",
        )

        self._regenerate_summary()
        threading.Thread(target=self._check_existing, daemon=True).start()

    # --- cards ---------------------------------------------------------------

    def _build_card(self, parent: tk.Misc, f: ReviewComment) -> None:
        sev = (f.severity or "nit").lower()
        card = ttk.Frame(parent, padding=8, relief=tk.GROOVE, borderwidth=1)
        card.pack(fill=tk.X, expand=True, padx=2, pady=5)

        top = ttk.Frame(card)
        top.pack(fill=tk.X)
        arrow = tk.Label(top, text="▾", cursor="hand2", font=("Segoe UI", 10), width=2)
        arrow.pack(side=tk.LEFT)
        Tooltip(arrow, "Collapse or expand this finding.")
        include = tk.BooleanVar(value=f.include)
        ttk.Checkbutton(top, variable=include, command=self._regenerate_summary).pack(side=tk.LEFT)
        tk.Label(top, text=SEVERITY_LABELS.get(sev, sev).upper(), foreground=SEV_COLORS.get(sev, "#333"),
                 font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        where = f"{f.file_path}:{f.line} ({f.side})" if f.line is not None else f"{f.file_path} (file-level)"
        if f.start_line:
            where = f"{f.file_path}:{f.start_line}-{f.line} ({f.side})"
        ttk.Label(top, text=where, font=("Consolas", 9)).pack(side=tk.LEFT, padx=(8, 0))
        if f.confidence == "low":
            ttk.Label(top, text="low confidence", foreground="#9a6700").pack(side=tk.LEFT, padx=(8, 0))
        if f.also_flagged_by:
            ttk.Label(top, text=f"also flagged by {f.also_flagged_by}", foreground="#1a7f37").pack(side=tk.LEFT, padx=(8, 0))

        title_var = tk.StringVar(value=f.title)
        # Shown in the header only while the card is collapsed.
        preview = ttk.Label(top, textvariable=title_var, foreground="#57606a", cursor="hand2")
        content = ttk.Frame(card)
        content.pack(fill=tk.X, expand=True)
        for w in (top, arrow, preview):
            w.bind("<Button-1>", lambda _e, k=id(f): self._toggle_card(k))

        anchorable = inline_payload(f, self.prep.file_diffs, self.diff.head_sha) is not None
        not_in_diff_mode = tk.StringVar(value="summary")
        if not anchorable and f.line is not None:
            row = tk.Frame(content, background="#ffebe9")
            row.pack(fill=tk.X, pady=(4, 0))
            tk.Label(row, text=f"Not in diff: {f.problem or NOT_IN_DIFF}. GitHub can't anchor it inline.",
                     background="#ffebe9", foreground="#cf222e").pack(side=tk.LEFT, padx=4)
            for label, value in (("Move to summary", "summary"), ("Post as file-level comment", "file")):
                tk.Radiobutton(row, text=label, variable=not_in_diff_mode, value=value, background="#ffebe9",
                               command=self._regenerate_summary).pack(side=tk.LEFT, padx=4)

        title_row = ttk.Frame(content)
        title_row.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(title_row, text="Title").pack(side=tk.LEFT)
        ttk.Entry(title_row, textvariable=title_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        body = f.comment.strip()
        text = tk.Text(content, height=min(8, max(3, body.count("\n") + 2, len(body) // 110 + 2)), wrap=tk.WORD,
                       font=("Segoe UI", 9))
        text.insert("1.0", body)
        text.pack(fill=tk.X, expand=True)
        if f.suggestion:
            ttk.Label(content, text="Suggested replacement (posted as a GitHub suggestion when every cited line is in the diff):",
                      foreground="#57606a").pack(anchor="w", pady=(4, 0))
            sugg = tk.Text(content, height=min(6, f.suggestion.count("\n") + 1), wrap=tk.NONE, font=("Consolas", 9),
                           background="#f6f8fa")
            sugg.insert("1.0", f.suggestion)
            sugg.pack(fill=tk.X)
        else:
            sugg = None
        if f.verifier_note:
            ttk.Label(content, text=f"Verifier: {f.verifier_note}", foreground="#57606a").pack(anchor="w")

        actions = ttk.Frame(content)
        actions.pack(fill=tk.X, pady=(4, 0))
        post_btn = ttk.Button(actions, text="Post this one" if anchorable else "Post as PR comment")
        post_btn.configure(command=lambda c=f: self._post_one(c))
        post_btn.pack(side=tk.LEFT)
        Tooltip(
            post_btn,
            "Post only this finding now, as an inline comment on its line."
            if anchorable
            else "Post only this finding now, as a general PR comment (its line isn't in the diff).",
        )
        tip(
            ttk.Button(actions, text="Copy fix prompt", command=lambda c=f: self._copy_fix(c)),
            "Copy a prompt that asks an AI assistant to fix this finding.",
        ).pack(side=tk.LEFT, padx=6)
        status = tk.StringVar(value="")
        link = tk.Label(actions, textvariable=status, foreground="#0969da", cursor="hand2")
        link.pack(side=tk.LEFT, padx=(6, 0))
        link.bind("<Button-1>", lambda _e, c=f: c.posted_url and webbrowser.open(c.posted_url))

        self._cards[id(f)] = {
            "finding": f, "include": include, "title": title_var, "text": text, "suggestion": sugg,
            "mode": not_in_diff_mode, "button": post_btn, "status": status, "anchorable": anchorable,
            "body": content, "arrow": arrow, "preview": preview, "collapsed": False,
        }

    def _toggle_card(self, key: int, collapse: bool | None = None) -> None:
        card = self._cards[key]
        collapse = not card["collapsed"] if collapse is None else collapse
        if collapse == card["collapsed"]:
            return
        card["collapsed"] = collapse
        if collapse:
            card["body"].pack_forget()
            card["preview"].pack(side=tk.LEFT, padx=(10, 0))
            card["arrow"].configure(text="▸")
        else:
            card["preview"].pack_forget()
            card["body"].pack(fill=tk.X, expand=True)
            card["arrow"].configure(text="▾")

    def _toggle_all(self, collapse: bool) -> None:
        for key in self._cards:
            self._toggle_card(key, collapse)

    def _build_prior(self, parent: tk.Misc) -> None:
        box = ttk.LabelFrame(parent, text="Since last review", padding=8)
        box.pack(fill=tk.X, padx=2, pady=(10, 4))
        for p in self.run.result.prior:
            row = ttk.Frame(box)
            row.pack(fill=tk.X, pady=1)
            can_reply = p.addressed and p.comment_id is not None
            var = tk.BooleanVar(value=can_reply and p.own)
            check = ttk.Checkbutton(row, variable=var, text="Reply + resolve" if p.own else "Reply")
            check.pack(side=tk.LEFT)
            if not can_reply:
                check.state(["disabled"])
            who = "" if p.own else f" ({p.raised_by or 'another reviewer'})"
            text = f"{STATUS_ICONS.get(p.status, '')} {p.status}: {p.title}{who}" + (f": {p.note}" if p.note else "")
            ttk.Label(row, text=text, wraplength=700, justify=tk.LEFT).pack(side=tk.LEFT, padx=(6, 0))
            status = tk.StringVar(value="" if p.comment_id is not None else "(no thread matched)")
            if p.thread_url:
                link = tk.Label(row, text="thread", foreground="#0969da", cursor="hand2")
                link.pack(side=tk.RIGHT)
                link.bind("<Button-1>", lambda _e, u=p.thread_url: webbrowser.open(u))
            ttk.Label(row, textvariable=status, foreground="#57606a").pack(side=tk.RIGHT, padx=6)
            self._prior_vars.append((p, var, status))
        ttk.Label(box, text="Own threads resolve after the reply; other reviewers' threads only get a reply.",
                  foreground="#57606a").pack(anchor="w", pady=(4, 0))

    def _build_others(self, parent: tk.Misc) -> None:
        r = self.run.result
        box = ttk.LabelFrame(parent, text="Other reviewers", padding=8)
        box.pack(fill=tk.X, padx=2, pady=(6, 4))
        for c in r.credits:
            ttk.Label(box, text=f"Credit to {c.reviewer}: {c.point}", wraplength=820, justify=tk.LEFT).pack(anchor="w")
        for d in r.disagreements:
            row = ttk.Frame(box)
            row.pack(fill=tk.X, pady=3)
            ttk.Label(row, text=f"Disagree with {d.reviewer} on: {d.claim}\n{d.rebuttal}", wraplength=700,
                      justify=tk.LEFT).pack(side=tk.LEFT)
            status = tk.StringVar(value="")
            btn = ttk.Button(row, text="Reply with rebuttal")
            btn.configure(command=lambda dd=d, b=btn, s=status: self._post_rebuttal(dd, b, s))
            btn.pack(side=tk.RIGHT)
            Tooltip(btn, "Reply to this reviewer's comment with the rebuttal above.")
            if d.comment_id is None:
                btn.state(["disabled"])
                status.set("(not an inline thread)")
            ttk.Label(row, textvariable=status, foreground="#57606a").pack(side=tk.RIGHT, padx=6)

    # --- helpers --------------------------------------------------------------

    def _sync(self) -> None:
        """Copy card edits back onto the findings."""
        for card in self._cards.values():
            f: ReviewComment = card["finding"]
            f.title = card["title"].get().strip()
            f.comment = card["text"].get("1.0", "end-1c").strip() or f.comment
            if card["suggestion"] is not None:
                f.suggestion = card["suggestion"].get("1.0", "end-1c").rstrip() or None

    def _selected(self) -> list[ReviewComment]:
        return [c["finding"] for c in self._cards.values() if c["include"].get() and not c["finding"].posted_url]

    def _summary_input(self, findings: list[ReviewComment]) -> SummaryInput:
        prep = self.prep
        return SummaryInput(
            diff=self.diff, result=self.run.result, findings=findings,
            ticket_summaries=[(t.key, t.summary) for t in prep.tickets], ticket_keys=prep.ticket_keys,
            withheld=list(prep.selection.omitted), prior=self.run.result.prior,
            since_sha=prep.prior.last_sha if prep.re_review else "", footer=self.footer,
            include_fix_prompts=bool(self.fix_prompts_var.get()),
        )

    def _summary_findings(self) -> list[ReviewComment]:
        # File-level-comment cards are posted separately, not in the summary list.
        return [f for f in self._selected() if not (self._cards[id(f)]["mode"].get() == "file" and not self._cards[id(f)]["anchorable"])]

    def _regenerate_summary(self) -> None:
        if not hasattr(self, "summary_text"):
            return
        self._sync()
        findings = self._summary_findings()
        inline = {id(f) for f in findings if self._cards[id(f)]["anchorable"]}
        self._generated = render_summary(self._summary_input(findings), inline)
        self.summary_text.delete("1.0", tk.END)
        self.summary_text.insert("1.0", self._generated)

    def _check_existing(self) -> None:
        existing = already_posted(self.diff, self.token)

        def apply() -> None:
            hits = 0
            for card in self._cards.values():
                f = card["finding"]
                if f.fp and f.fp in existing:
                    f.posted_url = existing[f.fp]
                    card["include"].set(False)
                    card["button"].state(["disabled"])
                    card["status"].set("already on PR ↗")
                    hits += 1
            self.status_var.set(f"{hits} finding(s) already on the PR for this commit." if hits else "Ready.")
            if hits:
                self._regenerate_summary()

        self.after(0, apply)

    def _copy_fix(self, f: ReviewComment) -> None:
        self._sync()
        self.clipboard_clear()
        self.clipboard_append(fix_prompt(f, self.prep.ticket_keys))
        self.status_var.set("Fix prompt copied.")

    def _mark_posted(self, f: ReviewComment) -> None:
        card = self._cards.get(id(f))
        if card:
            card["include"].set(False)
            card["button"].state(["disabled"])
            card["status"].set("Posted ↗")

    # --- actions -------------------------------------------------------------

    def _post_one(self, f: ReviewComment) -> None:
        self._sync()
        card = self._cards[id(f)]
        card["button"].state(["disabled"])
        card["status"].set("posting…")

        def worker() -> None:
            try:
                post_single(self.diff, f, self.prep.file_diffs, self.token)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda: (card["button"].state(["!disabled"]), card["status"].set("failed"),
                                       messagebox.showerror("Post failed", err, parent=self)))
                return
            self.after(0, lambda: (self._mark_posted(f), self._regenerate_summary()))

        threading.Thread(target=worker, daemon=True).start()

    def _post_rebuttal(self, d, button: ttk.Button, status: tk.StringVar) -> None:  # noqa: ANN001
        button.state(["disabled"])
        status.set("posting…")

        def worker() -> None:
            try:
                post_rebuttal(self.diff, d, self.token)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda: (button.state(["!disabled"]), status.set("failed"),
                                       messagebox.showerror("Reply failed", err, parent=self)))
                return
            self.after(0, lambda: status.set("replied ✓"))

        threading.Thread(target=worker, daemon=True).start()

    def _submit_all(self) -> None:
        if self._busy:
            return
        self._sync()
        findings = self._summary_findings()
        summary_ids = {id(f) for f in findings}
        file_level = [f for f in self._selected() if id(f) not in summary_ids]
        user_text = self.summary_text.get("1.0", "end-1c").strip()
        edited = user_text != (self._generated or "").strip()
        event = self.event_var.get()
        if event not in EVENTS:
            return
        if not messagebox.askyesno(
            "Submit review",
            f"Post one {event} review with {len(findings)} finding(s)"
            + (f" and {len(file_level)} file-level comment(s)" if file_level else "")
            + " to GitHub?",
            parent=self,
        ):
            return
        self._busy = True
        self.submit_btn.state(["disabled"])
        expected_inline = {id(f) for f in findings if self._cards[id(f)]["anchorable"]}
        summary_input = self._summary_input(findings)
        replies = [(p, var.get(), status) for p, var, status in self._prior_vars if var.get()]
        supersede = bool(self.supersede_var.get())
        amend = bool(self.amend_var.get())

        def summary_for(inline_ids: set[int]) -> str:
            if not edited:
                return render_summary(summary_input, inline_ids)
            body = ensure_marker(user_text, self.diff)
            demoted = [f for f in findings if id(f) in expected_inline and id(f) not in inline_ids]
            if demoted:
                body += "\n\n**Not posted inline** (GitHub rejected the anchor): " + ", ".join(
                    f"`{f.location}` {f.title}" for f in demoted
                )
            return body

        def status(text: str) -> None:
            self.after(0, lambda: self.status_var.set(text))

        def worker() -> None:
            notes: list[str] = []
            try:
                outcome = publish_review(
                    diff=self.diff, file_diffs=self.prep.file_diffs, findings=findings, summary_for=summary_for,
                    event=event, token=self.token,
                    previous_review_id=self.prep.prior.own_review_id if supersede else None,
                    previous_review_body=self.prep.prior.own_review_body, on_status=status,
                )
                notes += outcome.notes
                for f in file_level:
                    status(f"posting file-level comment on {f.file_path}")
                    try:
                        body = f"**{SEVERITY_LABELS.get(f.severity, f.severity)}: {f.title}** (`{f.location}`)\n\n{f.comment}"
                        f.posted_url = post_issue_comment(self.diff.ref, body, token=self.token).get("html_url") or ""
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"File-level comment on {f.location} failed: {exc}")
                for p, _checked, s in replies:
                    try:
                        result = reply_and_resolve(self.diff, p, self.token, resolve=p.own)
                        self.after(0, lambda s=s, r=result: s.set(r))
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"Reply on '{p.title}' failed: {exc}")
                if amend and self.prep.prior.own_review_id and outcome.review_url:
                    removed = amend_previous(self.diff, self.prep.prior.own_review_id, outcome.review_url, self.token)
                    notes.append(f"Amended: removed {removed} older inline comment(s).")
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda: self._submit_failed(err))
                return
            self.after(0, lambda: self._submit_done(outcome, findings + file_level, notes))

        threading.Thread(target=worker, daemon=True).start()

    def _submit_failed(self, error: str) -> None:
        self._busy = False
        self.submit_btn.state(["!disabled"])
        self.status_var.set("Submit failed.")
        messagebox.showerror("Submit failed", error, parent=self)

    def _submit_done(self, outcome, posted: list[ReviewComment], notes: list[str]) -> None:  # noqa: ANN001
        self._busy = False
        for f in posted:
            self._mark_posted(f)
        bits = [f"Posted: {outcome.inline + outcome.one_by_one} inline"]
        if outcome.demoted:
            bits.append(f"{len(outcome.demoted)} in summary only")
        if outcome.skipped_existing:
            bits.append(f"{outcome.skipped_existing} already on PR")
        if outcome.superseded:
            bits.append("previous review marked superseded")
        self.status_var.set(", ".join(bits) + ".")
        message = self.status_var.get() + ("\n\n" + "\n".join(notes) if notes else "")
        if outcome.review_url and messagebox.askyesno("Review posted", message + "\n\nOpen it on GitHub?", parent=self):
            webbrowser.open(outcome.review_url)

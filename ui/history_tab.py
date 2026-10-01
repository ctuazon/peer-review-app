"""History tab (mixin for PeerReviewApp)."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk


from app.diff_view import enable_selection_copy
from app.github_pr import parse_pr_url
from app.history_store import (
    HistoryEntry,
    clear_history,
    delete_history_entry,
    get_history_entry,
    list_history,
)
from app.review import format_copy_friendly
from ui.widgets import Section, tip


class HistoryTabMixin:
    def _build_history_tab(self) -> None:
        tab = self.history_tab

        toolbar = ttk.Frame(tab)
        toolbar.pack(fill=tk.X)
        tip(
            ttk.Button(toolbar, text="Revisit", command=self.revisit_history_entry),
            "Open the selected run in the Review tab with its inputs and result (or double-click it).",
        ).pack(side=tk.LEFT)
        tip(
            ttk.Button(toolbar, text="Restore inputs only", command=self.restore_history_inputs),
            "Copy the PR link, story and prompt back into the Review tab without loading the old result, "
            "ready for a fresh run.",
        ).pack(side=tk.LEFT, padx=4)
        tip(
            ttk.Button(toolbar, text="Delete", command=self.delete_selected_history),
            "Delete the selected run from history.",
        ).pack(side=tk.LEFT, padx=4)
        tip(
            ttk.Button(toolbar, text="Clear all", command=self.clear_all_history),
            "Delete every saved run. Re-reviews lose their earlier findings to compare against.",
        ).pack(side=tk.LEFT, padx=4)
        tip(
            ttk.Button(toolbar, text="Refresh", command=self.refresh_history_ui),
            "Reload the history list.",
        ).pack(side=tk.RIGHT)

        paned = ttk.Panedwindow(tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=2)

        list_section = Section(left, "Past runs", padding=(0, 0, 10, 0))
        list_section.pack(fill=tk.BOTH, expand=True)
        list_box = list_section.body
        self.history_list = tk.Listbox(
            list_box, exportselection=False, font=("Segoe UI", 9),
            relief=tk.FLAT, borderwidth=0, highlightthickness=1, highlightbackground="#d0d7de",
        )
        hist_scroll = ttk.Scrollbar(
            list_box, orient=tk.VERTICAL, command=self.history_list.yview
        )
        self.history_list.configure(yscrollcommand=hist_scroll.set)
        self.history_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hist_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_list.bind("<<ListboxSelect>>", self.on_history_select)
        self.history_list.bind("<Double-Button-1>", lambda _e: self.revisit_history_entry())
        self._history_ids: list[str] = []

        detail_section = Section(right, "Details", padding=(10, 0, 0, 0))
        detail_section.pack(fill=tk.BOTH, expand=True)
        detail = detail_section.body
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


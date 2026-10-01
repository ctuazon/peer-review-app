"""Prompts tab (mixin for PeerReviewApp)."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk


from app.prompts_store import (
    delete_prompt,
    list_prompts,
    move_prompt,
    move_prompt_to_index,
)
from ui.dialogs import PromptEditorDialog
from ui.widgets import Section, soften_text, tip


class PromptsTabMixin:
    def _build_prompts_tab(self) -> None:
        tab = self.prompts_tab

        toolbar = ttk.Frame(tab)
        toolbar.pack(fill=tk.X)
        tip(
            ttk.Button(toolbar, text="New", command=self.new_prompt),
            "Create a reviewer prompt, either generic or tied to a repo type.",
        ).pack(side=tk.LEFT)
        tip(
            ttk.Button(toolbar, text="Edit", command=self.edit_prompt),
            "Edit the selected prompt (or double-click a row).",
        ).pack(side=tk.LEFT, padx=4)
        tip(
            ttk.Button(toolbar, text="Delete", command=self.delete_selected_prompt),
            "Delete the selected prompt.",
        ).pack(side=tk.LEFT, padx=4)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=2
        )
        tip(
            ttk.Button(toolbar, text="Move up", command=self.move_prompt_up),
            "Move the selected prompt earlier. Order sets which prompt the Review tab shows first. "
            "You can also drag rows.",
        ).pack(side=tk.LEFT)
        tip(
            ttk.Button(toolbar, text="Move down", command=self.move_prompt_down),
            "Move the selected prompt later in the cycle order.",
        ).pack(side=tk.LEFT, padx=4)
        tip(
            ttk.Button(toolbar, text="Reload", command=self.refresh_prompts_ui),
            "Re-read prompts from disk (data/prompts.json).",
        ).pack(side=tk.LEFT, padx=4)

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

        preview_frame = Section(tab, "Selected prompt")
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=(14, 0))
        self.prompt_detail = scrolledtext.ScrolledText(
            preview_frame.body, height=10, wrap=tk.WORD, state=tk.DISABLED
        )
        soften_text(self.prompt_detail)
        self.prompt_detail.pack(fill=tk.BOTH, expand=True)
        self.prompt_tree.bind("<<TreeviewSelect>>", self.on_prompt_select)


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


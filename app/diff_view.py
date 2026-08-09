"""GitHub-style diff viewer with collapsible per-file panels and inline comments."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Iterable

from app.diff_model import DiffDisplayRow, normalize_path, parse_patch_display_rows
from app.github_pr import PullRequestDiff
from app.review_parse import (
    ReviewComment,
    build_verification_prompt,
    comments_to_copy_text,
    parse_review_comments,
)


SEVERITY_COLORS = {
    "blocker": "#cf222e",
    "major": "#bf8700",
    "minor": "#0969da",
    "nit": "#6e7781",
    "praise": "#1a7f37",
}

# Collapsible file-tab header styles
HEADER_STYLES = {
    "default": {"bg": "#ddf4ff", "fg": "#0550ae"},
    "review": {"bg": "#fff8c5", "fg": "#9a6700"},  # has comments
    "blocker": {"bg": "#ffebe9", "fg": "#cf222e"},
    "major": {"bg": "#fff1e5", "fg": "#bc4c00"},
    "praise": {"bg": "#dafbe1", "fg": "#1a7f37"},
}


def _worst_severity(comments: list[ReviewComment]) -> str | None:
    if not comments:
        return None
    order = ["blocker", "major", "minor", "nit", "praise"]
    ranks = {name: i for i, name in enumerate(order)}
    best = None
    best_rank = len(order)
    for comment in comments:
        sev = (comment.severity or "nit").lower()
        rank = ranks.get(sev, ranks["nit"])
        if rank < best_rank:
            best_rank = rank
            best = sev
    return best


def _readonly_key(event: tk.Event) -> str | None:
    """Allow navigation / copy / select-all; block edits."""
    if event.state & 0x4:  # Control
        if event.keysym.lower() in {"c", "a", "insert"}:
            return None
    if event.keysym in {
        "Left",
        "Right",
        "Up",
        "Down",
        "Home",
        "End",
        "Prior",
        "Next",
        "Shift_L",
        "Shift_R",
        "Control_L",
        "Control_R",
        "Alt_L",
        "Alt_R",
        "Caps_Lock",
        "Escape",
        "Tab",
    }:
        return None
    return "break"


# Selection colors — must beat tag backgrounds (comment/diff rows) or drag-select
# looks invisible on Windows.
SELECT_BG = "#0969da"
SELECT_FG = "#ffffff"
INACTIVE_SELECT_BG = "#54aeff"


def apply_visible_selection(text: tk.Text) -> None:
    """Make drag/double-click selection visible over tagged backgrounds."""
    text.configure(
        selectbackground=SELECT_BG,
        selectforeground=SELECT_FG,
        inactiveselectbackground=INACTIVE_SELECT_BG,
    )
    text.tag_configure("sel", background=SELECT_BG, foreground=SELECT_FG)
    # Later tag_configure calls raise those tags above sel; put sel back on top.
    text.tag_raise("sel")


def enable_selection_copy(text: tk.Text) -> None:
    """Keep text selectable and copyable without allowing edits.

    DISABLED Text widgets show a selection highlight on Windows but clear it
    when focus moves (e.g. Copy selected), and ignore Ctrl+C.
    """
    text.configure(state=tk.NORMAL, exportselection=False, insertwidth=0, cursor="arrow")
    apply_visible_selection(text)
    text.bind("<<Paste>>", lambda _e: "break")
    text.bind("<<Cut>>", lambda _e: "break")
    text.bind("<Control-v>", lambda _e: "break")
    text.bind("<Control-V>", lambda _e: "break")
    text.bind("<Control-x>", lambda _e: "break")
    text.bind("<Control-X>", lambda _e: "break")
    text.bind("<Key>", _readonly_key)


class CollapsiblePanel(ttk.Frame):
    """A single expandable/collapsible section with a header and body."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        title: str,
        subtitle: str = "",
        expanded: bool = True,
        on_toggle=None,
        has_review: bool = False,
        severity: str | None = None,
    ) -> None:
        super().__init__(master)
        self._expanded = expanded
        self._on_toggle = on_toggle
        self._title = title
        self._subtitle = subtitle
        self._has_review = has_review
        self._severity = (severity or "").lower() or None

        style = self._resolve_style()
        self.header = tk.Frame(self, background=style["bg"], cursor="hand2")
        self.header.pack(fill=tk.X)

        self._arrow_var = tk.StringVar(value="▼" if expanded else "▶")
        self._title_var = tk.StringVar(value=self._header_text())

        self.arrow = tk.Label(
            self.header,
            textvariable=self._arrow_var,
            background=style["bg"],
            foreground=style["fg"],
            font=("Segoe UI", 10, "bold"),
            padx=8,
            pady=6,
            cursor="hand2",
        )
        self.arrow.pack(side=tk.LEFT)

        self.title_label = tk.Label(
            self.header,
            textvariable=self._title_var,
            background=style["bg"],
            foreground=style["fg"],
            font=("Consolas", 10, "bold"),
            anchor="w",
            padx=4,
            pady=6,
            cursor="hand2",
        )
        self.title_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        for widget in (self.header, self.arrow, self.title_label):
            widget.bind("<Button-1>", self.toggle)

        self.body = ttk.Frame(self)
        if expanded:
            self.body.pack(fill=tk.BOTH, expand=True)

    def _resolve_style(self) -> dict[str, str]:
        if not self._has_review:
            return HEADER_STYLES["default"]
        if self._severity in {"blocker", "major", "praise"}:
            return HEADER_STYLES[self._severity]
        return HEADER_STYLES["review"]

    def set_review_style(self, *, has_review: bool, severity: str | None = None) -> None:
        self._has_review = has_review
        self._severity = (severity or "").lower() or None
        style = self._resolve_style()
        for widget in (self.header, self.arrow, self.title_label):
            widget.configure(background=style["bg"])
        self.arrow.configure(foreground=style["fg"])
        self.title_label.configure(foreground=style["fg"])

    def _header_text(self) -> str:
        if self._subtitle:
            return f"{self._title}   {self._subtitle}"
        return self._title

    def set_meta(self, *, title: str | None = None, subtitle: str | None = None) -> None:
        if title is not None:
            self._title = title
        if subtitle is not None:
            self._subtitle = subtitle
        self._title_var.set(self._header_text())

    @property
    def expanded(self) -> bool:
        return self._expanded

    def expand(self) -> None:
        if self._expanded:
            return
        self._expanded = True
        self._arrow_var.set("▼")
        self.body.pack(fill=tk.BOTH, expand=True)
        if self._on_toggle:
            self._on_toggle(self)

    def collapse(self) -> None:
        if not self._expanded:
            return
        self._expanded = False
        self._arrow_var.set("▶")
        self.body.pack_forget()
        if self._on_toggle:
            self._on_toggle(self)

    def toggle(self, _event: object = None) -> None:
        if self._expanded:
            self.collapse()
        else:
            self.expand()


class DiffReviewView(ttk.Frame):
    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, **kwargs)
        self._copy_text = ""
        self._comments: list[ReviewComment] = []
        self._diff: PullRequestDiff | None = None
        self._panels: list[CollapsiblePanel] = []
        self._text_widgets: list[tk.Text] = []

        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(toolbar, text="Expand all", command=self.expand_all).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Collapse all", command=self.collapse_all).pack(
            side=tk.LEFT, padx=4
        )
        self.summary_var = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self.summary_var).pack(side=tk.LEFT, padx=10)

        # Scrollable container for panels.
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#ffffff")
        self.vscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vscroll.set)
        self.vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))
        self.inner.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))

    def _on_inner_configure(self, _event: object = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        # Only scroll if pointer is over this view.
        widget = self.winfo_containing(event.x_root, event.y_root)
        if widget is None:
            return
        try:
            if str(widget).startswith(str(self)):
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            pass

    def expand_all(self) -> None:
        for panel in self._panels:
            panel.expand()

    def collapse_all(self) -> None:
        for panel in self._panels:
            panel.collapse()

    def clear(self) -> None:
        self._copy_text = ""
        self._comments = []
        self._diff = None
        self._panels.clear()
        self._text_widgets.clear()
        self.summary_var.set("")
        for child in self.inner.winfo_children():
            child.destroy()
        self.canvas.configure(scrollregion=(0, 0, 0, 0))

    def get_copy_text(self) -> str:
        return self._copy_text

    def get_comments(self) -> list[ReviewComment]:
        return list(self._comments)

    def has_review_content(self) -> bool:
        return bool(self._comments) or bool(self._copy_text.strip())

    def get_verification_prompt(self, *, story: str = "") -> str:
        if not self.has_review_content():
            return ""
        comments = self._comments or parse_review_comments(self._copy_text)
        if not comments:
            return ""
        snippets: dict[str, str] = {}
        if self._diff is not None:
            wanted = {normalize_path(c.file_path) for c in comments}
            for file_info in self._diff.files:
                path = file_info.get("filename") or ""
                if normalize_path(path) in wanted:
                    patch = file_info.get("patch") or ""
                    if patch:
                        snippets[path] = patch
        return build_verification_prompt(
            comments=comments,
            pr_url=self._diff.ref.url if self._diff else "",
            pr_title=self._diff.title if self._diff else "",
            story=story,
            code_snippets=snippets,
        )

    def get_selected_text(self) -> str:
        for text in self._text_widgets:
            try:
                return text.get(tk.SEL_FIRST, tk.SEL_LAST)
            except tk.TclError:
                continue
        return ""

    def render(
        self,
        *,
        diff: PullRequestDiff,
        review_text: str,
        summary_lines: Iterable[str] | None = None,
    ) -> None:
        self.clear()
        self._diff = diff
        comments = parse_review_comments(review_text)
        self._comments = comments
        self._copy_text = comments_to_copy_text(comments) or review_text

        if summary_lines:
            summary = "  ·  ".join(summary_lines)
            self.summary_var.set(summary)
            summary_box = ttk.Label(
                self.inner,
                text="\n".join(summary_lines),
                justify=tk.LEFT,
                wraplength=900,
            )
            summary_box.pack(fill=tk.X, padx=4, pady=(0, 8))

        by_key: dict[tuple[str, int, str], list[ReviewComment]] = {}
        orphans: list[ReviewComment] = []
        for comment in comments:
            if comment.line is None:
                orphans.append(comment)
                continue
            key = (normalize_path(comment.file_path), comment.line, comment.side.upper())
            by_key.setdefault(key, []).append(comment)

        used: set[int] = set()

        for file_info in diff.files:
            path = file_info.get("filename") or "unknown"
            status = file_info.get("status", "modified")
            additions = file_info.get("additions", 0)
            deletions = file_info.get("deletions", 0)

            patch = file_info.get("patch") or ""
            rows = parse_patch_display_rows(path, patch) if patch else []

            path_norm = normalize_path(path)
            matched_for_file: list[ReviewComment] = []
            for key, group in by_key.items():
                if key[0] == path_norm:
                    matched_for_file.extend(group)

            comment_count = len(matched_for_file)
            subtitle = f"({status})  +{additions} / -{deletions}"
            if comment_count:
                subtitle += f"  ·  {comment_count} comment{'s' if comment_count != 1 else ''}"
            worst = _worst_severity(matched_for_file)

            panel = CollapsiblePanel(
                self.inner,
                title=path,
                subtitle=subtitle,
                expanded=True,
                on_toggle=lambda _p: self._on_inner_configure(),
                has_review=comment_count > 0,
                severity=worst,
            )
            panel.pack(fill=tk.X, padx=2, pady=3)
            self._panels.append(panel)

            text = self._make_diff_text(panel.body)
            self._text_widgets.append(text)

            if not patch:
                text.insert(tk.END, "  (binary file or patch unavailable)\n", ("meta",))
            else:
                for row in rows:
                    self._insert_code_row(text, row)
                    matched = self._comments_for_row(row, by_key)
                    for comment in matched:
                        used.add(id(comment))
                        self._insert_comment(text, comment)

            enable_selection_copy(text)
            self._fit_text_height(text)

        leftover = [c for c in comments if id(c) not in used] + orphans
        if leftover:
            panel = CollapsiblePanel(
                self.inner,
                title="Unplaced review comments",
                subtitle=f"{len(leftover)} comment{'s' if len(leftover) != 1 else ''}",
                expanded=True,
                on_toggle=lambda _p: self._on_inner_configure(),
                has_review=True,
                severity=_worst_severity(leftover),
            )
            panel.pack(fill=tk.X, padx=2, pady=3)
            self._panels.append(panel)
            text = self._make_diff_text(panel.body)
            self._text_widgets.append(text)
            for comment in leftover:
                self._insert_comment(text, comment)
            enable_selection_copy(text)
            self._fit_text_height(text)

        self.after(50, self._on_inner_configure)

    def _make_diff_text(self, parent: tk.Misc) -> tk.Text:
        text = tk.Text(
            parent,
            wrap=tk.NONE,
            font=("Consolas", 10),
            background="#ffffff",
            foreground="#1f2328",
            insertbackground="#1f2328",
            relief=tk.FLAT,
            borderwidth=0,
            padx=0,
            pady=4,
            height=8,
            exportselection=False,
            insertwidth=0,
            cursor="arrow",
            selectbackground=SELECT_BG,
            selectforeground=SELECT_FG,
            inactiveselectbackground=INACTIVE_SELECT_BG,
        )
        xscroll = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=text.xview)
        text.configure(xscrollcommand=xscroll.set)
        text.pack(fill=tk.BOTH, expand=True)
        xscroll.pack(fill=tk.X)
        self._configure_tags(text)
        return text

    def _fit_text_height(self, text: tk.Text, *, max_lines: int = 40) -> None:
        lines = int(float(text.index("end-1c").split(".")[0]))
        text.configure(height=max(4, min(lines + 1, max_lines)))

    def _configure_tags(self, t: tk.Text) -> None:
        t.tag_configure(
            "hunk",
            background="#ddf4ff",
            foreground="#0550ae",
            lmargin1=6,
            lmargin2=6,
        )
        t.tag_configure(
            "context",
            background="#ffffff",
            foreground="#1f2328",
            lmargin1=6,
            lmargin2=6,
        )
        t.tag_configure(
            "added",
            background="#dafbe1",
            foreground="#1f2328",
            lmargin1=6,
            lmargin2=6,
        )
        t.tag_configure(
            "removed",
            background="#ffebe9",
            foreground="#1f2328",
            lmargin1=6,
            lmargin2=6,
        )
        t.tag_configure(
            "meta",
            background="#f6f8fa",
            foreground="#656d76",
            lmargin1=6,
            lmargin2=6,
        )
        t.tag_configure("lineno", foreground="#8c959f", font=("Consolas", 9))
        t.tag_configure(
            "prefix_add",
            foreground="#1a7f37",
            font=("Consolas", 10, "bold"),
        )
        t.tag_configure(
            "prefix_del",
            foreground="#cf222e",
            font=("Consolas", 10, "bold"),
        )
        t.tag_configure(
            "comment_box",
            background="#fff8c5",
            foreground="#1f2328",
            lmargin1=28,
            lmargin2=36,
            rmargin=12,
            spacing1=6,
            spacing3=8,
        )
        t.tag_configure(
            "comment_header",
            background="#fff8c5",
            foreground="#9a6700",
            font=("Consolas", 9, "bold"),
            lmargin1=28,
            lmargin2=36,
            rmargin=12,
        )
        for sev, color in SEVERITY_COLORS.items():
            t.tag_configure(f"sev_{sev}", foreground=color, font=("Consolas", 9, "bold"))
        # Tag backgrounds (comment_box, added, …) were configured after sel and would
        # hide the selection highlight unless sel is raised again.
        apply_visible_selection(t)

    def _comments_for_row(
        self,
        row: DiffDisplayRow,
        by_key: dict[tuple[str, int, str], list[ReviewComment]],
    ) -> list[ReviewComment]:
        path = normalize_path(row.file_path)
        found: list[ReviewComment] = []
        if row.kind == "added" and row.new_line is not None:
            found.extend(by_key.get((path, row.new_line, "RIGHT"), []))
        if row.kind == "removed" and row.old_line is not None:
            found.extend(by_key.get((path, row.old_line, "LEFT"), []))
        if row.kind == "context" and row.new_line is not None:
            found.extend(by_key.get((path, row.new_line, "RIGHT"), []))
        return found

    def _insert_code_row(self, text: tk.Text, row: DiffDisplayRow) -> None:
        if row.kind == "hunk":
            text.insert(tk.END, f"{row.text}\n", ("hunk",))
            return
        if row.kind == "meta":
            text.insert(tk.END, f" {row.text}\n", ("meta",))
            return

        old = f"{row.old_line:>4}" if row.old_line is not None else "    "
        new = f"{row.new_line:>4}" if row.new_line is not None else "    "
        if row.kind == "added":
            prefix, prefix_tag, body_tag = "+", "prefix_add", "added"
        elif row.kind == "removed":
            prefix, prefix_tag, body_tag = "-", "prefix_del", "removed"
        else:
            prefix, prefix_tag, body_tag = " ", "lineno", "context"

        text.insert(tk.END, f"{old} ", ("lineno", body_tag))
        text.insert(tk.END, f"{new} ", ("lineno", body_tag))
        text.insert(tk.END, f"{prefix} ", (prefix_tag, body_tag))
        text.insert(tk.END, f"{row.text}\n", (body_tag,))

    def _insert_comment(self, text: tk.Text, comment: ReviewComment) -> None:
        sev = (comment.severity or "nit").lower()
        sev_tag = f"sev_{sev}" if sev in SEVERITY_COLORS else "comment_header"
        header = (
            f"[{sev.upper()}]  {comment.file_path}"
            f":{comment.line if comment.line is not None else '?'}  ·  {comment.side}\n"
        )
        text.insert(tk.END, header, ("comment_header", sev_tag))
        body = comment.comment.strip() or "(empty comment)"
        for line in body.splitlines() or [body]:
            text.insert(tk.END, f"{line}\n", ("comment_box",))
        text.insert(tk.END, "\n", ("comment_box",))

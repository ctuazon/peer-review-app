"""GitHub-style diff viewer with collapsible per-file panels and inline comments."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable

from app.diff_model import DiffDisplayRow, normalize_path, parse_patch_display_rows
from app.github_pr import PullRequestDiff
from app.review_parse import (
    ReviewComment,
    build_verification_prompt,
    comments_to_copy_text,
    parse_review_comments,
)
from app.review_schema import INCONCLUSIVE, ReviewResult
from ui.widgets import Tooltip, tip

VERDICT_STYLES = {
    "approve": ("#dafbe1", "#1a7f37"),
    "approve_with_nits": ("#ddf4ff", "#0550ae"),
    "changes_requested": ("#ffebe9", "#cf222e"),
    INCONCLUSIVE: ("#fff8c5", "#9a6700"),
}
STATUS_ICONS = {"fixed": "✅", "open": "⏳", "withdrawn": "↩", "changed": "✏"}


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

    def _header_text(self) -> str:
        if self._subtitle:
            return f"{self._title}   {self._subtitle}"
        return self._title

    @property
    def title(self) -> str:
        return self._title

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
        self._result: ReviewResult | None = None
        self._summary_lines: list[str] = []
        self._panels: list[CollapsiblePanel] = []
        self._text_widgets: list[tk.Text] = []
        self._pages: list[CollapsiblePanel] = []
        self._page_index = 0
        self._comment_anchors: list[tuple[int, tk.Text, str]] = []
        self._comment_cursor = -1
        # Set by the host app; called with no args when "Submit to GitHub" is
        # clicked. The host reads back the diff/comments via get_diff() /
        # get_comments() rather than receiving them as callback args, so it
        # always sees whatever is currently rendered.
        self.on_submit_review: Callable[[], None] | None = None

        # Submit gets its own row so it's never squeezed off-screen by a long
        # summary line sharing the toolbar row below (pack(side=RIGHT) in the
        # same row as an unbounded-width label can get clipped past the
        # window edge instead of wrapping or shrinking the label). The row
        # itself is only packed while there's something to submit -- see
        # _set_submit_visible() -- rather than left showing a disabled button.
        self._action_bar = ttk.Frame(self)
        self.submit_btn = ttk.Button(
            self._action_bar, text="Submit to GitHub…", command=self._handle_submit_click
        )
        self.submit_btn.pack(side=tk.RIGHT)
        Tooltip(
            self.submit_btn,
            "Review the findings, pick which to include, and post them to the PR as one GitHub review.",
        )
        self._submit_visible = False

        self._toolbar = toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, pady=(0, 4))
        tip(
            ttk.Button(toolbar, text="Expand all", command=self.expand_all),
            "Open every file's diff.",
        ).pack(side=tk.LEFT)
        tip(
            ttk.Button(toolbar, text="Collapse all", command=self.collapse_all),
            "Fold every file's diff down to its header.",
        ).pack(side=tk.LEFT, padx=4)
        self.summary_var = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self.summary_var).pack(side=tk.LEFT, padx=10)

        # Paginate by file — shown only when a rendered PR has more than one
        # file, so a single-file review keeps the old always-expanded layout.
        # Prev/Next live on their own row, separate from the file label below
        # them -- otherwise Next's position shifts with the label's length
        # (a long file path pushes it further right, a short one less so).
        self.pager = ttk.Frame(self)
        pager_buttons = ttk.Frame(self.pager)
        pager_buttons.pack(fill=tk.X)
        self.prev_btn = ttk.Button(pager_buttons, text="◀ Prev", command=self.prev_page)
        self.prev_btn.pack(side=tk.LEFT)
        Tooltip(self.prev_btn, "Previous file in this PR.")
        self.next_btn = ttk.Button(pager_buttons, text="Next ▶", command=self.next_page)
        self.next_btn.pack(side=tk.LEFT, padx=(4, 0))
        Tooltip(self.next_btn, "Next file in this PR.")
        self.page_label_var = tk.StringVar(value="")
        ttk.Label(self.pager, textvariable=self.page_label_var).pack(
            anchor="w", pady=(2, 0)
        )

        # Jump-to-comment bar — sits under the review window itself, and (like
        # the pager) is shown only when there's something to navigate to.
        self.jump_bar = ttk.Frame(self)
        self.jump_btn = ttk.Button(
            self.jump_bar, text="Next comment ▼", command=self.jump_to_next_comment
        )
        self.jump_btn.pack(side=tk.LEFT)
        Tooltip(self.jump_btn, "Scroll to the next review comment, moving to the next file when needed.")
        self.jump_label_var = tk.StringVar(value="")
        self.jump_label = ttk.Label(self.jump_bar, textvariable=self.jump_label_var)
        self.jump_label.pack(side=tk.LEFT, padx=(8, 0))

        # Scrollable container for panels. Grouped in its own frame so the
        # pager/jump bars above and below can be shown or hidden later without
        # disturbing the pack order (they're always inserted with
        # before=self.canvas_frame, which is packed last so it claims all
        # remaining space).
        self.canvas_frame = ttk.Frame(self)
        self.canvas = tk.Canvas(self.canvas_frame, highlightthickness=0, background="#ffffff")
        self.vscroll = ttk.Scrollbar(
            self.canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview
        )
        self.canvas.configure(yscrollcommand=self.vscroll.set)
        self.vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas_frame.pack(fill=tk.BOTH, expand=True)

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
        self._result = None
        self._panels.clear()
        self._text_widgets.clear()
        self._pages.clear()
        self._page_index = 0
        self._comment_anchors.clear()
        self._comment_cursor = -1
        self.summary_var.set("")
        self._set_submit_visible(False)
        self.pager.pack_forget()
        self.jump_bar.pack_forget()
        self._update_jump_ui()
        for child in self.inner.winfo_children():
            child.destroy()
        self.canvas.configure(scrollregion=(0, 0, 0, 0))

    def _update_jump_ui(self) -> None:
        total = len(self._comment_anchors)
        if total == 0:
            self.jump_bar.pack_forget()
            return
        if not self.jump_bar.winfo_ismapped():
            self.jump_bar.pack(
                side=tk.BOTTOM, fill=tk.X, pady=(4, 0), before=self.canvas_frame
            )
        if self._comment_cursor < 0:
            self.jump_label_var.set(f"{total} comment{'s' if total != 1 else ''}")
        else:
            self.jump_label_var.set(f"({self._comment_cursor + 1}/{total})")

    def _update_pager(self) -> None:
        total = len(self._pages)
        if total <= 1:
            self.pager.pack_forget()
            return
        self.pager.pack(fill=tk.X, pady=(0, 4), before=self.canvas_frame)
        panel = self._pages[self._page_index]
        self.page_label_var.set(
            f"File {self._page_index + 1} of {total}  ·  {panel.title}"
        )
        self.prev_btn.configure(state=tk.NORMAL if self._page_index > 0 else tk.DISABLED)
        self.next_btn.configure(
            state=tk.NORMAL if self._page_index < total - 1 else tk.DISABLED
        )

    def _show_page(self, index: int) -> None:
        if not self._pages:
            return
        index = max(0, min(index, len(self._pages) - 1))
        for i, panel in enumerate(self._pages):
            if i == index:
                panel.pack(fill=tk.X, padx=2, pady=3)
            else:
                panel.pack_forget()
        self._page_index = index
        self._update_pager()
        self.canvas.yview_moveto(0)
        self.after(50, self._on_inner_configure)

    def prev_page(self) -> None:
        self._show_page(self._page_index - 1)

    def next_page(self) -> None:
        self._show_page(self._page_index + 1)

    def jump_to_next_comment(self) -> None:
        if not self._comment_anchors:
            return
        self._comment_cursor = (self._comment_cursor + 1) % len(self._comment_anchors)
        page_index, text, mark = self._comment_anchors[self._comment_cursor]
        self._update_jump_ui()
        if page_index != self._page_index:
            self._show_page(page_index)
        self._pages[page_index].expand()
        self.after(80, lambda: self._focus_comment(text, mark))

    def _focus_comment(self, text: tk.Text, mark: str) -> None:
        try:
            text.see(mark)
        except tk.TclError:
            return
        self.update_idletasks()
        self._scroll_canvas_to_mark(text, mark)
        self._flash_mark(text, mark)

    def _scroll_canvas_to_mark(self, text: tk.Text, mark: str) -> None:
        try:
            bbox = text.bbox(mark)
        except tk.TclError:
            bbox = None
        canvas_top = text.winfo_rooty() - self.canvas.winfo_rooty()
        y_offset = bbox[1] if bbox else 0
        target = self.canvas.canvasy(canvas_top + y_offset)
        region = self.canvas.bbox("all")
        if not region:
            return
        total_height = region[3] - region[1]
        if total_height <= 0:
            return
        frac = max(0.0, min(1.0, (target - 60) / total_height))
        self.canvas.yview_moveto(frac)

    def _flash_mark(self, text: tk.Text, mark: str) -> None:
        try:
            start = text.index(mark)
            end = f"{mark} lineend"
            text.tag_remove("jump_flash", "1.0", tk.END)
            text.tag_add("jump_flash", start, end)
            text.tag_raise("jump_flash")
        except tk.TclError:
            return
        self.after(1200, lambda: self._clear_flash(text))

    def _clear_flash(self, text: tk.Text) -> None:
        try:
            text.tag_remove("jump_flash", "1.0", tk.END)
        except tk.TclError:
            pass

    def _handle_submit_click(self) -> None:
        if self.on_submit_review:
            self.on_submit_review()

    def _set_submit_visible(self, visible: bool) -> None:
        if visible == self._submit_visible:
            return
        self._submit_visible = visible
        if visible:
            self._action_bar.pack(fill=tk.X, pady=(0, 4), before=self._toolbar)
        else:
            self._action_bar.pack_forget()

    def get_copy_text(self) -> str:
        return self._copy_text

    def get_comments(self) -> list[ReviewComment]:
        return list(self._comments)

    def get_diff(self) -> PullRequestDiff | None:
        return self._diff

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

    def restore_dropped(self, finding: ReviewComment) -> None:
        """Bring a verifier-rejected finding back into the review."""
        if self._result is None or self._diff is None:
            return
        if any(f is finding for f in self._result.dropped_by_verifier):
            self._result.dropped_by_verifier = [f for f in self._result.dropped_by_verifier if f is not finding]
            finding.verifier_note = (finding.verifier_note + " (restored by you)").strip()
            self._result.findings.append(finding)
            self.render_result(diff=self._diff, result=self._result, summary_lines=self._summary_lines)

    def _banner(self, result: ReviewResult) -> None:
        bg, fg = VERDICT_STYLES.get(result.verdict, ("#f6f8fa", "#1f2328"))
        box = tk.Frame(self.inner, background=bg, padx=10, pady=6)
        box.pack(fill=tk.X, padx=4, pady=(0, 6))
        tk.Label(box, text=result.headline(), background=bg, foreground=fg,
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(fill=tk.X)
        for label, text in (("", result.verdict_reason), ("Scope: ", result.scope_note), ("Tests: ", result.tests_note)):
            if text:
                tk.Label(box, text=label + text, background=bg, foreground="#1f2328", anchor="w",
                         justify=tk.LEFT, wraplength=880).pack(fill=tk.X)
        if result.parse_mode == "legacy":
            tk.Label(box, text="Parsed via legacy fallback: the JSON output didn't parse.",
                     background="#fff8c5", foreground="#9a6700", anchor="w").pack(fill=tk.X, pady=(4, 0))
        if result.report:
            tk.Label(box, text="Report:\n" + result.report, background=bg, foreground="#1f2328", anchor="w",
                     justify=tk.LEFT, wraplength=880).pack(fill=tk.X, pady=(4, 0))
        if result.verdict == INCONCLUSIVE:
            tk.Label(box, text="Open this run in History to see Claude's raw output.", background=bg,
                     foreground=fg, anchor="w").pack(fill=tk.X, pady=(4, 0))
        elif not result.findings:
            tk.Label(box, text="No findings: nothing to post inline.", background=bg, foreground=fg,
                     anchor="w").pack(fill=tk.X, pady=(4, 0))

    def _text_panel(self, title: str, subtitle: str, *, expanded: bool, severity: str | None = None) -> tk.Text:
        panel = CollapsiblePanel(
            self.inner, title=title, subtitle=subtitle, expanded=expanded,
            on_toggle=lambda _p: self._on_inner_configure(), has_review=True, severity=severity,
        )
        self._panels.append(panel)
        self._pages.append(panel)
        text = self._make_diff_text(panel.body)
        self._text_widgets.append(text)
        return text

    def render_result(
        self,
        *,
        diff: PullRequestDiff,
        result: ReviewResult,
        summary_lines: Iterable[str] | None = None,
    ) -> None:
        self.clear()
        self._diff = diff
        self._result = result
        self._summary_lines = list(summary_lines or [])
        comments = list(result.findings)
        self._comments = comments
        self._copy_text = comments_to_copy_text(comments) or result.raw_text

        self._banner(result)
        if self._summary_lines:
            self.summary_var.set("  ·  ".join(self._summary_lines[:2]))
            ttk.Label(self.inner, text="\n".join(self._summary_lines), justify=tk.LEFT, wraplength=900).pack(
                fill=tk.X, padx=4, pady=(0, 8)
            )

        by_key: dict[tuple[str, int, str], list[ReviewComment]] = {}
        orphans: list[ReviewComment] = []
        for comment in comments:
            if comment.line is None or comment.problem:
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
            self._panels.append(panel)
            self._pages.append(panel)

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
                        self._insert_comment(text, comment, len(self._pages) - 1)

            enable_selection_copy(text)
            self._fit_text_height(text)

        orphan_ids = {id(c) for c in orphans}
        leftover = [c for c in comments if id(c) not in used and id(c) not in orphan_ids] + orphans + list(result.rejected)
        if leftover:
            text = self._text_panel(
                "Unplaced review comments",
                f"{len(leftover)} comment{'s' if len(leftover) != 1 else ''} (file-level, not in diff, or invalid)",
                expanded=True, severity=_worst_severity(leftover),
            )
            for comment in leftover:
                self._insert_comment(text, comment, len(self._pages) - 1)
            enable_selection_copy(text)
            self._fit_text_height(text)

        if result.prior:
            text = self._text_panel("Since last review", f"{len(result.prior)} earlier finding(s)", expanded=True)
            for p in result.prior:
                who = "" if p.own else f" [{p.raised_by or 'another reviewer'}]"
                text.insert(tk.END, f" {STATUS_ICONS.get(p.status, '')} {p.status.upper()}{who}  {p.title}\n", ("comment_header",))
                if p.note:
                    text.insert(tk.END, f"{p.note}\n", ("comment_box",))
                if p.thread_url:
                    text.insert(tk.END, f"{p.thread_url}\n", ("meta",))
            enable_selection_copy(text)
            self._fit_text_height(text)

        if result.credits or result.disagreements:
            text = self._text_panel(
                "Other reviewers",
                f"{len(result.credits)} credit(s), {len(result.disagreements)} disagreement(s)",
                expanded=True,
            )
            for c in result.credits:
                text.insert(tk.END, f" CREDIT  {c.reviewer}\n", ("comment_header",))
                text.insert(tk.END, f"{c.point}\n\n", ("comment_box",))
            for d in result.disagreements:
                text.insert(tk.END, f" DISAGREE  {d.reviewer}: {d.claim}\n", ("comment_header", "sev_blocker"))
                text.insert(tk.END, f"{d.rebuttal}\n(Reply with this rebuttal from Submit to GitHub.)\n\n", ("comment_box",))
            enable_selection_copy(text)
            self._fit_text_height(text)

        if result.dropped_by_verifier:
            text = self._text_panel(
                "Dropped by verifier",
                f"{len(result.dropped_by_verifier)} finding(s); Restore keeps one",
                expanded=False,
            )
            for comment in list(result.dropped_by_verifier):
                button = ttk.Button(text, text="Restore", command=lambda c=comment: self.restore_dropped(c))
                Tooltip(button, "Move this finding back into the review (the verifier had dropped it).")
                text.window_create(tk.END, window=button)
                text.insert(tk.END, "\n")
                self._insert_comment(text, comment, len(self._pages) - 1)
            self._fit_text_height(text)

        self._update_jump_ui()
        self._show_page(0)
        self._set_submit_visible(bool(self._comments) or bool(result.prior) or result.parse_mode == "json")

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
        t.tag_configure("jump_flash", background="#ffd33d")
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

    def _insert_comment(self, text: tk.Text, comment: ReviewComment, page_index: int) -> None:
        mark = f"cmt_anchor_{len(self._comment_anchors)}"
        text.mark_set(mark, text.index(tk.END))
        text.mark_gravity(mark, tk.LEFT)
        self._comment_anchors.append((page_index, text, mark))

        sev = (comment.severity or "nit").lower()
        sev_tag = f"sev_{sev}" if sev in SEVERITY_COLORS else "comment_header"
        header = (
            f"[{sev.upper()}]  {comment.file_path}"
            f":{comment.line if comment.line is not None else '?'}  ·  {comment.side}"
        )
        if comment.title:
            header += f"  ·  {comment.title}"
        if comment.confidence == "low":
            header += "  ·  low confidence"
        text.insert(tk.END, header + "\n", ("comment_header", sev_tag))
        if comment.problem:
            text.insert(tk.END, f"⚠ {comment.problem}\n", ("comment_header", "sev_blocker"))
        if comment.verifier_note:
            text.insert(tk.END, f"Verifier: {comment.verifier_note}\n", ("comment_box",))
        body = comment.comment.strip() or "(empty comment)"
        for line in body.splitlines() or [body]:
            text.insert(tk.END, f"{line}\n", ("comment_box",))
        if comment.suggestion:
            text.insert(tk.END, "Suggested change:\n", ("comment_box",))
            for line in comment.suggestion.splitlines():
                text.insert(tk.END, f"    {line}\n", ("comment_box",))
        text.insert(tk.END, "\n", ("comment_box",))

"""
A browse dialog for the terminal UI.

Textual has no native file-open dialog — no `FileOpen` widget, nothing
that calls out to the OS picker (there is no OS picker in a terminal).
What it does have is `DirectoryTree`, and the idiomatic answer is a
`ModalScreen` wrapping one.

Two modes, because the fields need different things:

* **directory** — for the SCDM root and the output/log folders. Selecting
  a folder means "use this", so the dialog needs an explicit Select
  action; expanding a node cannot double as choosing it.
* **file** — for the study JSON. Selecting a file returns immediately,
  and non-matching files are hidden rather than shown-and-rejected.

Typing a path is still supported and often faster when you know it. This
is for when you don't.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label, Static


class _Tree(DirectoryTree):
    """DirectoryTree that can hide files, or filter them by suffix."""

    def __init__(self, path: str, *, dirs_only: bool = False,
                 suffixes: tuple[str, ...] = (), **kwargs) -> None:
        self._dirs_only = dirs_only
        self._suffixes = suffixes
        super().__init__(path, **kwargs)

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        out = []
        for p in paths:
            # Hidden entries are noise in this context; a user who needs
            # one can type the path.
            if p.name.startswith("."):
                continue
            if p.is_dir():
                out.append(p)
            elif not self._dirs_only:
                if not self._suffixes or p.suffix.lower() in self._suffixes:
                    out.append(p)
        return sorted(out, key=lambda p: (not p.is_dir(), p.name.lower()))


class BrowseScreen(ModalScreen[str | None]):
    """Pick a file or a directory. Returns the path, or None if cancelled."""

    CSS = """
    BrowseScreen { align: center middle; }
    #box {
        width: 84; height: 30; border: thick $primary;
        background: $surface; padding: 1 2;
    }
    #title { text-style: bold; height: 1; }
    #cwd { color: $text-muted; height: 1; }
    #tree { height: 1fr; border: round $secondary; }
    #manual { height: 3; }
    #buttons { height: auto; align: right middle; }
    #buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "choose", "Select"),
    ]

    def __init__(self, start: str = ".", *, dirs_only: bool = True,
                 suffixes: tuple[str, ...] = (), title: str = "Select") -> None:
        super().__init__()
        # Start somewhere that exists: a half-typed path in the field
        # should not produce an empty dialog.
        p = Path(start).expanduser() if start else Path.cwd()
        while not p.exists() and p != p.parent:
            p = p.parent
        self._start = str(p if p.is_dir() else p.parent)
        self._dirs_only = dirs_only
        self._suffixes = suffixes
        self._title = title
        self._selected: str = self._start

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(self._title, id="title")
            yield Static(self._selected, id="cwd")
            yield _Tree(self._start, dirs_only=self._dirs_only,
                        suffixes=self._suffixes, id="tree")
            with Horizontal(id="manual"):
                yield Label("Path")
                yield Input(value=self._selected, id="path")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Select", id="choose", variant="primary")

    # -- selection ----------------------------------------------------

    def _set(self, path: str) -> None:
        self._selected = path
        self.query_one("#cwd", Static).update(path)
        self.query_one("#path", Input).value = path

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        # Expanding is not choosing: highlight it and wait for Select.
        self._set(str(event.path))

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        self._set(str(event.path))
        if not self._dirs_only:
            self.dismiss(str(event.path))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "choose":
            self.action_choose()
        else:
            self.dismiss(None)

    def action_choose(self) -> None:
        typed = self.query_one("#path", Input).value.strip()
        self.dismiss(typed or self._selected or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

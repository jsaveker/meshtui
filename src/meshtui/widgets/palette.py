"""Searchable operator command palette opened with `/`."""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from .nodes import fmt_age


COMMANDS = [
    ("node <name>", "jump to a node"),
    ("filter <mode>", "filter packets"),
    ("watch <expression>", "apply proto/hop/snr/channel watch"),
    ("view save <name> <expression>", "save a named watch"),
    ("view <name>", "activate a saved watch"),
    ("send <node|#channel> <text>", "send a message"),
    ("trace <node> [hops]", "run a trace"),
    ("login <node>", "open remote admin at a node"),
    ("scope", "edit MeshCore flood scope"),
    ("rooms", "browse MeshCore room-server posts"),
    ("layout <balanced|radio|chat|route>", "change the four-pane split"),
    ("theme <phosphor|night-vision|high-contrast>", "change deck theme"),
]

COMMAND_WORDS = {c.split()[0] for c, _ in COMMANDS} | {"jump"}

NODE_ROWS = 8


class CommandPalette(ModalScreen[None]):
    BINDINGS = [
        ("escape", "dismiss", "close"),
        ("down", "cursor(1)", "next"),
        ("up", "cursor(-1)", "prev"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Static(" command palette", id="palette-title")
            yield Input(placeholder="node, filter, send, trace, login...", id="palette-input")
            yield DataTable(id="palette-results", cursor_type="row", zebra_stripes=True)
            yield Static(" enter run   esc close", id="palette-help")

    def on_mount(self) -> None:
        table = self.query_one("#palette-results", DataTable)
        table.add_column("command", key="command")
        table.add_column("action", key="action")
        self._refresh_results("")
        self.query_one("#palette-input", Input).focus()

    def _matches(self, value: str) -> list[tuple[str, str]]:
        terms = value.casefold().split()
        rows = [row for row in COMMANDS
                if all(term in f"{row[0]} {row[1]}".casefold() for term in terms)]
        rows.extend(self._node_rows(terms))
        return rows

    def _node_rows(self, terms: list[str]) -> list[tuple[str, str]]:
        """Live nodes matching the query, so 'santaluz' is a runnable hit.

        A palette that only searches its own command templates makes the
        operator type node names blind; the whole point is finding things.
        """
        state = getattr(self.app, "state", None)
        if state is None or not terms:
            return []
        found = []
        for node in state.nodes.values():
            hay = f"{node.long_name} {node.short_name} {node.node_id}".casefold()
            if all(term in hay for term in terms):
                found.append(node)
        found.sort(key=lambda n: -(n.last_heard or 0.0))
        now = time.time()
        rows = []
        for n in found[:NODE_ROWS]:
            age = fmt_age(now - n.last_heard if n.last_heard else None).plain
            what = (n.role or "node").lower()
            rows.append((f"node {n.long_name or n.node_id}",
                         f"jump to {what} {n.node_id} · heard {age}"))
        return rows

    def _refresh_results(self, value: str) -> None:
        table = self.query_one("#palette-results", DataTable)
        table.clear()
        for command, description in self._matches(value):
            table.add_row(Text(command, style="bright_white"),
                          Text(description, style="grey62"))
        if table.row_count:
            table.move_cursor(row=0)

    def action_cursor(self, delta: int) -> None:
        table = self.query_one("#palette-results", DataTable)
        if table.row_count:
            table.move_cursor(row=(table.cursor_row + delta) % table.row_count)

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._refresh_results(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value.strip()
        rows = self._matches(event.value)
        table = self.query_one("#palette-results", DataTable)
        row = rows[table.cursor_row] if 0 <= table.cursor_row < len(rows) else None
        first = value.split()[0].casefold() if value else ""
        if row and "<" not in row[0] and first not in COMMAND_WORDS:
            # The typed text is a search, not a command - run the
            # highlighted hit ('santaluz' + enter jumps to the node).
            value = row[0]
        elif not value and row:
            value = row[0].split()[0] if "<" in row[0] else row[0]
        if value and self.app.execute_palette(value):  # type: ignore[attr-defined]
            self.dismiss(None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        rows = self._matches(self.query_one("#palette-input", Input).value)
        if not (0 <= event.cursor_row < len(rows)):
            return
        command = rows[event.cursor_row][0]
        if "<" not in command:
            # Concrete entries (a live node) run outright.
            if self.app.execute_palette(command):  # type: ignore[attr-defined]
                self.dismiss(None)
            return
        stub = command.split()[0] + " "
        field = self.query_one("#palette-input", Input)
        field.value = stub
        field.cursor_position = len(stub)
        field.focus()

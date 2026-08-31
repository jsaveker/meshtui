"""Searchable operator command palette opened with `/`."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static


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


class CommandPalette(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "close")]

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
        return [row for row in COMMANDS
                if all(term in f"{row[0]} {row[1]}".casefold() for term in terms)]

    def _refresh_results(self, value: str) -> None:
        table = self.query_one("#palette-results", DataTable)
        table.clear()
        for command, description in self._matches(value):
            table.add_row(Text(command, style="bright_white"),
                          Text(description, style="grey62"))
        if table.row_count:
            table.move_cursor(row=0)

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._refresh_results(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value.strip()
        if not value:
            table = self.query_one("#palette-results", DataTable)
            if table.row_count:
                value = COMMANDS[table.cursor_row][0].split()[0]
        if value and self.app.execute_palette(value):  # type: ignore[attr-defined]
            self.dismiss(None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        rows = self._matches(self.query_one("#palette-input", Input).value)
        if 0 <= event.cursor_row < len(rows):
            command = rows[event.cursor_row][0].split()[0] + " "
            field = self.query_one("#palette-input", Input)
            field.value = command
            field.cursor_position = len(command)
            field.focus()

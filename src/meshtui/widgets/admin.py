"""Remote administration of MeshCore repeaters and room servers, over RF.

MeshCore lets a client authenticate to a repeater with a password and then run
its console commands across the mesh. That is genuinely remote administration -
no USB cable, no being in the same building as the node on your roof.

This screen is a terminal for that: pick a repeater, log in, type commands, read
replies. Everything travels over LoRa, so replies take seconds rather than
milliseconds and can be lost entirely.
"""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, RichLog, Static

from ..model import Node
from ..state import MeshState

# Commands MeshCore repeaters and room servers understand. Shown as hints;
# the firmware is the authority on what any given build accepts.
COMMON_COMMANDS = [
    ("ver", "firmware version"),
    ("clock", "read the node's clock"),
    ("time <epoch>", "set the clock"),
    ("advert", "send a flood advert"),
    ("reboot", "restart the node"),
    ("get freq", "radio frequency"),
    ("get tx", "transmit power"),
    ("get name", "node name"),
    ("set name <x>", "rename the node"),
    ("set tx <dbm>", "set transmit power"),
    ("get repeat", "is repeating enabled"),
    ("set repeat <on|off>", "toggle repeating"),
    ("neighbors", "nodes it can hear"),
    ("log start", "begin packet logging"),
    ("log stop", "stop packet logging"),
    ("password <new>", "change the admin password"),
]


class AdminScreen(Screen[None]):
    """Pick a repeater, authenticate, and drive it over the air."""

    BINDINGS = [
        Binding("escape", "close", "back"),
        Binding("ctrl+l", "clear", "clear log", show=False),
        Binding("f2", "status", "status"),
        Binding("f3", "telemetry", "telemetry"),
        Binding("f4", "logout", "log out"),
        Binding("f5", "neighbours", "neighbours"),
    ]

    def __init__(self, state: MeshState, link: Any) -> None:
        super().__init__()
        self.state = state
        self.link = link
        self.target: str | None = None
        self._seen = 0
        # Nodes we have sent a login to but not yet heard an ack from. A login
        # travels back over LoRa and can take 30s, so the UI must distinguish
        # "waiting for the ack" from "not logged in".
        self._login_pending: set[str] = set()
        # True while _refresh_nodes is programmatically moving the cursor, so
        # the highlight handler does not mistake that for a user selection.
        self._refreshing = False

    # -------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Static(id="admin-status")
        with Horizontal(id="admin-main"):
            with Vertical(id="admin-left"):
                yield DataTable(id="admin-nodes", cursor_type="row")
                yield Static(id="admin-hints")
            with Vertical(id="admin-right"):
                yield RichLog(id="admin-log", markup=False, wrap=True, max_lines=1000)
                yield Input(placeholder="select a repeater, then type a command",
                            id="admin-input")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#admin-nodes", DataTable)
        table.add_column("", key="lock", width=1)
        table.add_column("node", key="name", width=22)
        table.add_column("type", key="type", width=8)
        self.query_one("#admin-nodes", DataTable).border_title = "repeaters"
        self.query_one("#admin-log", RichLog).border_title = "session"
        self._render_hints()
        self._refresh_nodes()
        self._render_status()
        self.set_interval(1.0, self._poll)
        table.focus()

    # -------------------------------------------------------------- content

    def _candidates(self) -> list[Node]:
        """Repeaters and room servers first; anything else is still selectable."""
        nodes = list(self.state.nodes.values())
        admin = [n for n in nodes if n.role in ("REPEATER", "ROOM")]
        other = [n for n in nodes if n not in admin and not n.is_self]
        return admin + other

    def _refresh_nodes(self) -> None:
        # Selection follows the node id, not the row index: this table is
        # rebuilt every second and reorders as contacts arrive, so pinning to a
        # row would silently move the selection - and drop an admin session.
        table = self.query_one("#admin-nodes", DataTable)
        self._refreshing = True
        try:
            table.clear()
            self._rows: list[str] = []
            for node in self._candidates():
                authed = node.node_id in self.state.admin_sessions
                table.add_row(
                    Text("*" if authed else " ",
                         style="bold bright_green" if authed else "grey42"),
                    Text(node.name[:22],
                         style="bright_white" if node.role in ("REPEATER", "ROOM")
                         else "grey62"),
                    Text(node.role[:8] or "-", style="cyan"),
                )
                self._rows.append(node.node_id)
            if self._rows:
                if self.target in self._rows:
                    row = self._rows.index(self.target)
                else:
                    row = 0
                    self.target = self._rows[0]
                table.move_cursor(row=row)
        finally:
            self._refreshing = False

    def _render_hints(self) -> None:
        text = Text()
        text.append(" commands\n", style="bold grey42")
        for cmd, why in COMMON_COMMANDS:
            text.append(f"  {cmd:<21}", style="bright_white")
            text.append(f"{why[:24]}\n", style="grey54")
        text.append("\n  login <password>", style="bold bright_cyan")
        text.append("   authenticate first\n", style="grey54")
        self.query_one("#admin-hints", Static).update(text)

    def _render_status(self) -> None:
        target = self.state.node_name(self.target) if self.target else "no node selected"
        authed = self.target in self.state.admin_sessions if self.target else False
        pending = self.target in self._login_pending if self.target else False
        line = Text()
        line.append(" remote admin  ", style="grey62")
        line.append(target, style="bold bright_white")
        line.append("  ")
        if not self.target:
            line.append("pick a repeater on the left", style="grey42")
        elif authed:
            line.append("authenticated", style="bold bright_green")
        elif pending:
            line.append("logging in - waiting for the ack (LoRa, up to 30s)...",
                        style="bold yellow")
        else:
            line.append("not logged in - type: login <password>", style="yellow")
        self.query_one("#admin-status", Static).update(line)

    def _poll(self) -> None:
        """Drain new session output, including anything restored from disk.

        Replies arrive over RF, so they trickle in well after the command.
        """
        log = self.query_one("#admin-log", RichLog)
        entries = list(self.state.cli_log)
        for ts, node_id, text in entries[self._seen:]:
            stamp = time.strftime("%H:%M:%S", time.localtime(ts))
            line = Text(f"{stamp} ", style="grey42")
            line.append(f"{self.state.node_name(node_id)} ", style="bright_green")
            line.append(text, style="white")
            log.write(line)
        self._seen = len(entries)
        # A login that landed clears its pending flag.
        self._login_pending -= self.state.admin_sessions
        self._refresh_nodes()
        self._render_status()

    # -------------------------------------------------------------- actions

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Ignore the highlight that our own refresh causes; only a real user
        # cursor move should change the selected node.
        if self._refreshing:
            return
        if getattr(self, "_rows", None) and 0 <= event.cursor_row < len(self._rows):
            self.target = self._rows[event.cursor_row]
            self._render_status()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.query_one("#admin-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # CRITICAL: stop the event here. Input.Submitted bubbles, and the app's
        # own handler treats anything it receives as a chat message - so
        # without this, every admin command (including the login password) is
        # also broadcast to the current channel.
        event.stop()
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        log = self.query_one("#admin-log", RichLog)
        if self.target is None:
            log.write(Text("select a repeater first", style="yellow"))
            return
        name = self.state.node_name(self.target)

        if text.lower().startswith("login "):
            password = text.split(" ", 1)[1]
            self._login_pending.add(self.target)
            log.write(Text(f"> login to {name} - waiting for the ack "
                           f"(can take 30s over LoRa)...", style="bright_cyan"))
            # Never the password itself, in the log or on disk.
            self.app.record_admin(self.target, "> login")
            self.link.login(self.target, password)
            self._render_status()
            return
        if self.target not in self.state.admin_sessions:
            if self.target in self._login_pending:
                log.write(Text("still waiting for the login ack - LoRa can take 30s; "
                               "the status line shows when it lands", style="yellow"))
            else:
                log.write(Text("not authenticated - run: login <password>", style="yellow"))
            return

        log.write(Text(f"> {text}", style="bright_cyan"))
        self.app.record_admin(self.target, f"> {text}")
        self.link.remote_command(self.target, text)

    def action_status(self) -> None:
        if self.target:
            self.link.request_status(self.target)
            self.query_one("#admin-log", RichLog).write(
                Text("> status request sent", style="bright_cyan"))

    def action_telemetry(self) -> None:
        if self.target:
            self.link.request_telemetry(self.target)
            self.query_one("#admin-log", RichLog).write(
                Text("> telemetry request sent", style="bright_cyan"))

    def action_neighbours(self) -> None:
        if self.target:
            self.link.request_neighbours(self.target)
            self.query_one("#admin-log", RichLog).write(
                Text("> neighbours request sent (the reply can take a while "
                     "over LoRa)", style="bright_cyan"))

    def action_logout(self) -> None:
        if self.target:
            self.link.logout(self.target)
            self.state.admin_sessions.discard(self.target)
            self._render_status()

    def action_clear(self) -> None:
        self.query_one("#admin-log", RichLog).clear()

    def action_close(self) -> None:
        self.dismiss(None)

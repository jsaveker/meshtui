"""MeshCore room-server post browser and catch-up surface."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, RichLog, Static

from ..model import payload_bytes
from ..state import MeshState
from .chat_render import write_conversation


class RoomScreen(Screen[None]):
    """Browse room threads; login initiates the server-driven catch-up."""

    BINDINGS = [Binding("escape", "close", "back")]

    def __init__(self, state: MeshState, link: Any, app_ref: Any) -> None:
        super().__init__()
        self.state = state
        self.link = link
        self.app_ref = app_ref
        self.target: str | None = None
        self._rows: list[str] = []
        self._pending: set[str] = set()
        self._list_signature: tuple[Any, ...] | None = None
        self._signature: tuple[Any, ...] | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="room-status")
        with Horizontal(id="room-main"):
            yield DataTable(id="room-list", cursor_type="row", zebra_stripes=True)
            with Vertical(id="room-right"):
                yield RichLog(id="room-posts", markup=False, wrap=True, max_lines=1000)
                with Horizontal(id="room-login-row"):
                    yield Input(placeholder="room password (blank may be read-only)",
                                password=True, id="room-password")
                    yield Button("Login + catch up", id="room-login", variant="primary")
                    yield Button("Logout", id="room-logout")
                with Horizontal(id="room-post-row"):
                    yield Input(placeholder="post to the selected room", id="room-post")
                    yield Button("Post", id="room-send", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#room-list", DataTable)
        table.add_column("", key="state", width=1)
        table.add_column("room", key="name")
        table.add_column("posts", key="posts", width=6)
        table.border_title = "room servers"
        self.query_one("#room-posts", RichLog).border_title = "posts"
        self._refresh_rooms()
        self._refresh_content()
        self.set_interval(1.0, self._refresh_content)
        table.focus()

    def _rooms(self):
        return sorted((node for node in self.state.nodes.values()
                       if node.role == "ROOM"), key=lambda node: node.name.casefold())

    def _refresh_rooms(self) -> None:
        rooms = self._rooms()
        rows = [room.node_id for room in rooms]
        signature = tuple(
            (room.node_id, room.name, room.node_id in self.state.admin_sessions,
             len(self.state.messages_for(("dm", room.node_id))))
            for room in rooms
        )
        if signature == self._list_signature:
            return
        self._list_signature = signature
        self._rows = rows
        table = self.query_one("#room-list", DataTable)
        table.clear()
        for room in rooms:
            posts = len(self.state.messages_for(("dm", room.node_id)))
            table.add_row(
                Text("*" if room.node_id in self.state.admin_sessions else " ",
                     style="bold bright_green"),
                Text(room.name, style="bright_magenta"), str(posts))
        if rows:
            self.target = self.target if self.target in rows else rows[0]
            table.move_cursor(row=rows.index(self.target))

    def _refresh_content(self) -> None:
        self._refresh_rooms()
        messages = self.state.messages_for(("dm", self.target)) if self.target else []
        signature = (self.target, len(messages), messages[-1].ts if messages else 0,
                     messages[-1].delivery_status if messages else "",
                     len(messages[-1].repeated_by) if messages else 0,
                     self.target in self.state.admin_sessions if self.target else False)
        if signature == self._signature:
            return
        self._signature = signature
        log = self.query_one("#room-posts", RichLog)
        write_conversation(log, messages, self.state)
        room = self.state.node_name(self.target) if self.target else "no room discovered"
        logged_in = self.target in self.state.admin_sessions if self.target else False
        if logged_in and self.target:
            self._pending.discard(self.target)
        status = Text(" room  ", style="grey62")
        status.append(room, style="bold bright_magenta")
        if logged_in:
            status.append("   logged in; unseen posts arrive asynchronously", style="green")
        elif self.target in self._pending:
            status.append("   login sent; waiting for the room over RF", style="yellow")
        elif self.target:
            status.append("   log in to catch up", style="grey62")
        self.query_one("#room-status", Static).update(status)
        log.border_title = f"{room} posts ({len(messages)})"

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if 0 <= event.cursor_row < len(self._rows):
            self.target = self._rows[event.cursor_row]
            self._signature = None
            self._refresh_content()

    def _login(self) -> None:
        if self.target is None:
            self.app_ref.note("no room server discovered", "yellow")
            return
        password = self.query_one("#room-password", Input).value
        self.query_one("#room-password", Input).value = ""
        self._pending.add(self.target)
        self.link.login(self.target, password)
        self._signature = None
        self._refresh_content()

    def _post(self) -> None:
        field = self.query_one("#room-post", Input)
        text = field.value.strip()
        if not text or self.target is None:
            return
        if self.target not in self.state.admin_sessions:
            self.app_ref.note("log in to the room before posting", "yellow")
            return
        if payload_bytes(text) > self.app_ref.max_payload:
            self.app_ref.note(
                f"room post is {payload_bytes(text)}/{self.app_ref.max_payload} bytes", "red")
            return
        if self.app_ref.send_to_room(self.target, text):
            field.value = ""
            self._signature = None
            self._refresh_content()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "room-login":
            self._login()
        elif event.button.id == "room-logout" and self.target:
            self.link.logout(self.target)
            self.state.admin_sessions.discard(self.target)
            self._signature = None
            self._refresh_content()
        elif event.button.id == "room-send":
            self._post()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "room-password":
            self._login()
        elif event.input.id == "room-post":
            self._post()

    def action_close(self) -> None:
        self.dismiss(None)

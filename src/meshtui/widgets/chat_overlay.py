"""Full-screen chat: channel list on the left, roomy conversation on the right.

The corner pane is for glancing; this is for reading and writing. It shares
MeshState.chat and MeshState.active_target with the corner pane, so switching a
channel in one is reflected in the other, and it reuses the app's send path so
message length limits and the input-isolation guarantee apply here too.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, ListItem, ListView, RichLog, Static

from ..model import outgoing_payload, payload_bytes
from ..state import MeshState
from .chat import ChatInput, LeaveChat
from .chat_render import write_conversation


class ChatScreen(Screen[None]):
    """Pop-out chat. Sidebar of channels + DMs, wide conversation, input."""

    BINDINGS = [
        Binding("escape", "close", "back"),
        Binding("up", "prev", "prev channel", show=False),
        Binding("down", "next", "next channel", show=False),
        Binding("tab", "focus_input", "message", show=False),
    ]

    def __init__(self, state: MeshState, app_ref) -> None:
        super().__init__()
        self.state = state
        self.app_ref = app_ref
        self._targets: list[tuple] = []

    def compose(self) -> ComposeResult:
        yield Static(id="ov-status")
        with Horizontal(id="ov-main"):
            with Vertical(id="ov-side"):
                yield ListView(id="ov-list")
            with Vertical(id="ov-right"):
                yield RichLog(id="ov-log", highlight=False, markup=False,
                              wrap=True, max_lines=2000)
                yield ChatInput(placeholder="message   esc to leave, /help for commands",
                                id="ov-input")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ov-list", ListView).border_title = "channels"
        self.query_one("#ov-log", RichLog).border_title = "conversation"
        self.rebuild_list()
        self.render_conversation()
        self.set_interval(1.5, self._tick)

    # ------------------------------------------------------------- sidebar

    def _build_targets(self) -> list[tuple]:
        targets: list[tuple] = [("all",)]
        for index, _ in self.state.channel_pairs():
            targets.append(("channel", index))
        for node_id in sorted(self.state.dm_contacts):
            targets.append(("dm", node_id))
        return targets

    def rebuild_list(self) -> None:
        """Rebuild the sidebar only when the set of targets actually changed.

        ListView.clear() is asynchronous, so appending straight after it can
        collide with items still being removed; rebuilding only on a real
        change avoids that, and refresh_list() handles label updates in place.
        """
        targets = self._build_targets()
        if targets == self._targets:
            self.refresh_list()
            return
        self._targets = targets
        listview = self.query_one("#ov-list", ListView)
        listview.clear()
        active_row = 0
        for i, target in enumerate(self._targets):
            if target == self.state.active_target:
                active_row = i
            # No id: rows are identified by position, and reusing ids across an
            # async clear() collides.
            listview.append(ListItem(Static(self._row_text(target))))
        listview.index = active_row

    def _row_text(self, target: tuple) -> Text:
        active = target == self.state.active_target
        unread = self.state.unread.get(target, 0)
        text = Text()
        if target[0] == "all":
            text.append("★ ", style="bright_yellow")
            text.append("All activity", style="bold" if active else "grey85")
        elif target[0] == "dm":
            text.append("@ ", style="bright_yellow")
            text.append(self.state.node_name(target[1])[:16],
                        style="bold bright_yellow" if active else "grey85")
        else:
            name = self.state.channel_name(int(target[1])).lstrip("#")
            text.append("# ", style="grey54")
            text.append(name[:16],
                        style="bold bright_white" if active else "grey85")
        if unread:
            text.append(f"  {unread}", style="bold bright_cyan")
        return text

    def refresh_list(self) -> None:
        """Update row labels (unread, active) without rebuilding the list."""
        from textual.css.query import NoMatches
        listview = self.query_one("#ov-list", ListView)
        for target, item in zip(self._targets, listview.query(ListItem)):
            try:
                item.query_one(Static).update(self._row_text(target))
            except NoMatches:
                # ListItem children mount asynchronously; the constructor
                # already set the correct label, so a miss here is harmless.
                pass

    # -------------------------------------------------------- conversation

    def render_conversation(self) -> None:
        target = self.state.active_target
        self.state.mark_read(target)
        write_conversation(
            self.query_one("#ov-log", RichLog),
            self.state.messages_for(target),
            self.state,
            show_channel=(target[0] == "all"),
        )
        label = self.state.target_label(target)
        self.query_one("#ov-log", RichLog).border_title = label
        self.query_one("#ov-status", Static).update(
            Text(f" chat  -  {label}   ({len(self.state.nodes)} nodes, "
                 f"{self.state.protocol})   esc to close", style="grey62"))
        self.refresh_list()

    def _tick(self) -> None:
        # New traffic on the active conversation should appear live.
        self.render_conversation()

    # ------------------------------------------------------------- actions

    def _select_row(self, row: int) -> None:
        if 0 <= row < len(self._targets):
            self.state.active_target = self._targets[row]
            self.render_conversation()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.index is not None:
            self._select_row(event.list_view.index)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.query_one("#ov-input", ChatInput).focus()

    def action_prev(self) -> None:
        self.query_one("#ov-list", ListView).action_cursor_up()

    def action_next(self) -> None:
        self.query_one("#ov-list", ListView).action_cursor_down()

    def action_focus_input(self) -> None:
        self.query_one("#ov-input", ChatInput).focus()

    def action_close(self) -> None:
        self.dismiss(None)

    def on_leave_chat(self, event: LeaveChat) -> None:
        # Escape from the overlay's input closes the overlay, rather than
        # bubbling to the app (which would move focus on the hidden base
        # screen and leave the overlay stranded open).
        event.stop()
        self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "ov-input":
            return
        payload = outgoing_payload(event.value)
        log = self.query_one("#ov-log", RichLog)
        used = payload_bytes(payload) if payload else 0
        limit = self.app_ref.max_payload
        if not used:
            log.border_subtitle = None
        else:
            style = ("bold red" if used > limit
                     else "yellow" if used > limit * 0.85 else "grey50")
            log.border_subtitle = Text(f" {used}/{limit} bytes ", style=style)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Stop the event so it cannot bubble to the app and be transmitted a
        # second time as a chat message on the wrong path.
        event.stop()
        text = event.value
        # Delegate to the app: one send path, one set of length/redaction rules.
        if self.app_ref.send_from_overlay(text):
            event.input.value = ""
            self.query_one("#ov-log", RichLog).border_subtitle = None
            self.render_conversation()

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
        Binding("ctrl+f", "cycle_route", "flood/direct", show=False, priority=True),
        # priority=True so these win over the focused ListView's own paging;
        # neither key is used by the message Input, so typing is unaffected.
        Binding("pageup", "history_up", "history", show=False, priority=True),
        Binding("pagedown", "history_down", "history", show=False, priority=True),
    ]

    def __init__(self, state: MeshState, app_ref, focus_input: bool = False) -> None:
        super().__init__()
        self.state = state
        self.app_ref = app_ref
        self._targets: list[tuple] = []
        self._focus_input_on_mount = focus_input
        # What the conversation log currently shows, so the 1.5s tick can skip
        # rewriting (and thus re-scrolling) an unchanged conversation.
        self._rendered_sig: tuple | None = None
        self.compose_route_mode = "auto"
        self.compose_hash_size: int | None = None

    def focus_input(self) -> None:
        self.query_one("#ov-input", ChatInput).focus()

    def compose(self) -> ComposeResult:
        yield Static(id="ov-status")
        with Horizontal(id="ov-main"):
            with Vertical(id="ov-side"):
                yield ListView(id="ov-list")
            with Vertical(id="ov-right"):
                yield RichLog(id="ov-log", highlight=False, markup=False,
                              wrap=True, min_width=20, max_lines=2000)
                with Horizontal(id="ov-compose"):
                    yield ChatInput(placeholder="message   esc leave, ctrl+f route mode",
                                    id="ov-input")
                    yield Static(id="ov-preview")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ov-list", ListView).border_title = "channels"
        self.query_one("#ov-log", RichLog).border_title = "conversation"
        self.rebuild_list()
        self.render_conversation()
        self._render_compose_preview("")
        self.set_interval(1.5, self._tick)
        if self._focus_input_on_mount:
            self.focus_input()

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
            node = self.state.nodes.get(target[1])
            is_room = node is not None and node.role == "ROOM"
            text.append("⌂ " if is_room else "@ ",
                        style="bright_magenta" if is_room else "bright_yellow")
            text.append(self.state.node_name(target[1])[:16],
                        style=("bold bright_magenta" if active and is_room else
                               "bold bright_yellow" if active else "grey85"))
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
        log = self.query_one("#ov-log", RichLog)
        messages = list(self.state.messages_for(target))
        # The rendering also reflects delivery markers and repeat counts, and
        # those mutate recent messages without changing the count - so the
        # tail is part of the signature.
        tail = tuple((m.delivery_status, len(getattr(m, "repeated_by", ()) or ()))
                     for m in messages[-8:])
        sig = (target, len(messages), messages[-1].ts if messages else 0.0, tail)
        if sig != self._rendered_sig:
            # A rewrite resets RichLog's scroll position, which would yank a
            # reader browsing the history back to the bottom on every tick.
            # Rewrite only when the conversation actually changed, and keep
            # the reader's place unless they were already at the end (or just
            # switched conversations, which always starts at the latest).
            switched = self._rendered_sig is None or self._rendered_sig[0] != target
            at_end = switched or log.is_vertical_scroll_end
            scroll_y = log.scroll_y
            # RichLog.write schedules a scroll-to-end for every row when
            # auto_scroll is enabled.  Restoring scroll_y after the refresh is
            # racy with those queued callbacks, so disable them while rebuilding
            # history for a reader who has deliberately scrolled back.
            auto_scroll = log.auto_scroll
            if not at_end:
                log.auto_scroll = False
            try:
                write_conversation(log, messages, self.state,
                                   show_channel=(target[0] == "all"))
            finally:
                log.auto_scroll = auto_scroll
            self._rendered_sig = sig
            if not at_end:
                log.scroll_to(y=scroll_y, animate=False, force=True, immediate=True)
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
            self._render_compose_preview(self.query_one("#ov-input", ChatInput).value)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.index is not None:
            self._select_row(event.list_view.index)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.query_one("#ov-input", ChatInput).focus()

    def action_prev(self) -> None:
        self.query_one("#ov-list", ListView).action_cursor_up()

    def action_next(self) -> None:
        self.query_one("#ov-list", ListView).action_cursor_down()

    def on_resize(self, event) -> None:
        # RichLog wraps at write time; a resize needs a rewrite at the new
        # width or old lines keep their old wrap and bring a scrollbar.
        self._rendered_sig = None
        self.render_conversation()

    def action_history_up(self) -> None:
        self.query_one("#ov-log", RichLog).scroll_page_up(animate=False)

    def action_history_down(self) -> None:
        # Paging back to the bottom re-enters live-follow: once the log sits
        # at the end again, the next rewrite auto-scrolls as usual.
        self.query_one("#ov-log", RichLog).scroll_page_down(animate=False)

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
        self._render_compose_preview(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Stop the event so it cannot bubble to the app and be transmitted a
        # second time as a chat message on the wrong path.
        event.stop()
        text = event.value
        # Delegate to the app: one send path, one set of length/redaction rules.
        if self.app_ref.send_from_overlay(
                text, route_mode=self.compose_route_mode,
                path_hash_size=self.compose_hash_size):
            event.input.value = ""
            self.query_one("#ov-log", RichLog).border_subtitle = None
            self.render_conversation()

    def action_cycle_route(self) -> None:
        if self.state.protocol != "meshcore" or self.state.active_target[0] != "dm":
            self.app_ref.note("flood/direct override applies to MeshCore DMs", "yellow")
            return
        modes = ("auto", "flood", "direct")
        current = modes.index(self.compose_route_mode)
        self.compose_route_mode = modes[(current + 1) % len(modes)]
        self._render_compose_preview(self.query_one("#ov-input", ChatInput).value)

    def _route_context(self) -> tuple[str, int | None, int | None]:
        """Return learned route mode, hop count, and hash size for the target."""
        target = self.state.active_target
        if self.state.protocol != "meshcore":
            return ("meshtastic", None, None)
        if target[0] == "channel":
            return ("flood", None, self.state.radio_info.get("path_hash_size"))
        if target[0] != "dm":
            return ("select target", None, None)
        contacts = getattr(self.app_ref.link, "contacts", {}) or {}
        contact = contacts.get(target[1], {})
        hops = contact.get("out_path_len")
        if not isinstance(hops, int):
            node = self.state.nodes.get(target[1])
            hops = node.hops if node is not None else None
        learned = "flood" if hops == -1 else ("direct" if hops == 0 else "path")
        mode = contact.get("out_path_hash_mode")
        size = mode + 1 if isinstance(mode, int) and 0 <= mode <= 3 else None
        return learned, hops, size

    def _render_compose_preview(self, raw: str) -> None:
        payload = outgoing_payload(raw) or ""
        wire = payload.encode("utf-8")
        learned, hops, hash_size = self._route_context()
        self.compose_hash_size = hash_size
        effective = self.compose_route_mode if self.compose_route_mode != "auto" else learned
        out = Text()
        out.append(" wire preview\n", style="bold grey62")
        out.append(f" {len(wire)}/{self.app_ref.max_payload}B", style="bright_white")
        if self.state.protocol == "meshcore":
            out.append(f"  {effective}", style="bold bright_magenta")
            if hops is not None and hops >= 0 and self.compose_route_mode == "auto":
                out.append(f"  {hops} hop{'s' if hops != 1 else ''}", style="cyan")
            out.append(f"  {hash_size or 1}B hash", style="yellow")
            if effective == "flood":
                out.append("  F", style="bold black on yellow")
        out.append("\n ", style="grey42")
        preview = " ".join(f"{byte:02x}" for byte in wire[:24])
        out.append(preview or "type to preview bytes", style="grey62")
        if len(wire) > 24:
            out.append(" ...", style="grey42")
        self.query_one("#ov-preview", Static).update(out)

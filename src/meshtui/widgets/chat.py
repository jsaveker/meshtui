"""Chat pane: channel/DM tabs, message log, and the send box."""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Input, RichLog, Tab, Tabs

from ..model import BROADCAST, DEFAULT_MAX_PAYLOAD, ChatMessage, outgoing_payload, payload_bytes
from ..state import MeshState


def dm_tab_id(node_id: str) -> str:
    return f"dm-{node_id.lstrip('!')}"


class LeaveChat(Message):
    """The user wants focus back out of the message box."""


class ChatInput(Input):
    """Message box that can be escaped from.

    While this has focus every printable key is text, so the single-letter app
    bindings are unreachable - `escape` is the documented way back out, and it
    is bound here (rather than on the app) so the footer advertises it exactly
    when it applies.
    """

    BINDINGS = [Binding("escape", "leave", "leave chat")]

    def action_leave(self) -> None:
        self.post_message(LeaveChat())


class ChatPane(Vertical):
    """Messages for the active target, plus an input that sends to it."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._known_dms: set[str] = set()
        self.base_title = "chat"
        self.max_bytes = DEFAULT_MAX_PAYLOAD   # replaced by the app on mount

    def compose(self) -> ComposeResult:
        # Tabs start empty and are populated once the radio reports its
        # channel list; mutating Tabs is async, hence the awaits below.
        yield Tabs(id="chat-tabs")
        yield RichLog(id="chat-log", highlight=False, markup=False, wrap=True, max_lines=1000)
        yield ChatInput(
            placeholder="message   esc to leave, /help for commands",
            id="chat-input",
        )

    # ------------------------------------------------------------- targets

    # --------------------------------------------------------------- title

    def set_title(self, text: str) -> None:
        self.base_title = text
        self._apply_title()

    def _apply_title(self) -> None:
        try:
            typing = self.query_one("#chat-input", ChatInput).has_focus
        except NoMatches:  # called before compose has mounted the input
            typing = False
        suffix = "  -  typing, esc to leave" if typing else ""
        self.border_title = self.base_title + suffix

    def update_counter(self, entry: str) -> None:
        """Show how much of the packet budget the pending message uses."""
        payload = outgoing_payload(entry)
        if not payload:
            self.border_subtitle = None
            return
        used = payload_bytes(payload)
        if used > self.max_bytes:
            style = "bold red"
        elif used > self.max_bytes * 0.85:
            style = "yellow"
        else:
            style = "grey50"
        self.border_subtitle = Text(f" {used}/{self.max_bytes} bytes ", style=style)

    def on_descendant_focus(self) -> None:
        self._apply_title()

    def on_descendant_blur(self) -> None:
        self._apply_title()

    @property
    def tabs(self) -> Tabs:
        return self.query_one("#chat-tabs", Tabs)

    @property
    def log(self) -> RichLog:
        return self.query_one("#chat-log", RichLog)

    def active_target(self) -> tuple[str, object]:
        """Return ("channel", index) or ("dm", node_id) for the selected tab."""
        active = self.tabs.active
        if active and active.startswith("dm-"):
            return ("dm", f"!{active[3:]}")
        if active and active.startswith("ch"):
            try:
                return ("channel", int(active[2:]))
            except ValueError:
                pass
        return ("channel", 0)

    async def set_channels(self, names: list[str]) -> None:
        """Rebuild the tab bar. Must await each mutation - Tabs removes and
        mounts children asynchronously, and re-adding an id before its old
        widget has finished unmounting wedges the message pump."""
        tabs = self.tabs
        await tabs.clear()
        for idx, name in enumerate(names or ["LongFast"]):
            await tabs.add_tab(Tab(name or f"ch{idx}", id=f"ch{idx}"))
        for node_id in sorted(self._known_dms):
            await tabs.add_tab(Tab(f"@{node_id[-4:]}", id=dm_tab_id(node_id)))
        if tabs.tab_count:
            tabs.active = "ch0"

    async def ensure_dm_tab(self, node_id: str, label: str) -> str:
        tab_id = dm_tab_id(node_id)
        if node_id not in self._known_dms:
            self._known_dms.add(node_id)
            await self.tabs.add_tab(Tab(f"@{label}", id=tab_id))
        return tab_id

    async def focus_dm(self, node_id: str, label: str) -> None:
        self.tabs.active = await self.ensure_dm_tab(node_id, label)

    # ------------------------------------------------------------ rendering

    def rerender(self, state: MeshState) -> None:
        kind, target = self.active_target()
        self.log.clear()
        if kind == "dm":
            messages = state.chat_for(None, dm_with=str(target))
        else:
            messages = state.chat_for(int(target))  # type: ignore[arg-type]
        for msg in messages:
            self._write(msg, state)

    def add(self, message: ChatMessage, state: MeshState) -> bool:
        """Write the message if it belongs to the active tab. Returns True if shown."""
        kind, target = self.active_target()
        if kind == "dm":
            if not (message.is_dm and str(target) in (message.from_id, message.to_id)):
                return False
        else:
            if message.is_dm or message.channel != int(target):  # type: ignore[arg-type]
                return False
        self._write(message, state)
        return True

    def _write(self, msg: ChatMessage, state: MeshState) -> None:
        line = Text(overflow="fold")
        line.append(time.strftime("%H:%M", time.localtime(msg.ts)), style="grey42")
        line.append(" ")
        if msg.outgoing:
            mark = "OK" if msg.acked else ".."
            style = "green" if msg.acked else "grey42"
            line.append(f"[{mark}] ", style=style)
            line.append("you", style="bold bright_cyan")
        else:
            name = msg.from_name or state.node_name(msg.from_id)
            line.append(name, style="bold bright_green")
        if msg.is_dm and not msg.outgoing:
            line.append(" (dm)", style="bright_yellow")
        line.append(": ", style="grey42")
        line.append(msg.text, style="white")
        self.log.write(line)

    def notice(self, text: str, style: str = "grey62") -> None:
        self.log.write(Text(text, style=style))

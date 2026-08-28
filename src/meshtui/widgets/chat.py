"""Corner chat pane: a channel button, a message log, and the send box.

This is the quick-glance view. It shows one conversation at a time — whatever
`state.active_target` points at — with a `#channel ▾` button that opens the
pop-out overlay to switch or read at length. The overlay and this pane share
`state.active_target`, so a change in either is reflected in both.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Input, RichLog, Static

from ..model import DEFAULT_MAX_PAYLOAD, ChatMessage, outgoing_payload, payload_bytes
from ..state import MeshState
from .chat_render import write_conversation


class LeaveChat(Message):
    """The user wants focus back out of the message box."""


class OpenChatOverlay(Message):
    """The user asked to expand chat to the full-screen overlay."""


class ChatInput(Input):
    """Message box that can be escaped from.

    While this has focus every printable key is text, so the single-letter app
    bindings are unreachable - `escape` is the documented way back out, and it
    is bound here (rather than on the app) so the footer advertises it exactly
    when it applies.
    """

    from textual.binding import Binding

    BINDINGS = [Binding("escape", "leave", "leave chat")]

    def action_leave(self) -> None:
        self.post_message(LeaveChat())


class ChannelButton(Static):
    """Shows the active conversation and opens the overlay when clicked."""

    def on_click(self) -> None:
        self.post_message(OpenChatOverlay())


class ChatPane(Vertical):
    """The active conversation, plus an input that sends to it."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_title = "chat"
        self.max_bytes = DEFAULT_MAX_PAYLOAD   # replaced by the app on mount
        self._state: MeshState | None = None

    def compose(self) -> ComposeResult:
        yield ChannelButton(id="chat-channel")
        yield RichLog(id="chat-log", highlight=False, markup=False,
                      wrap=True, max_lines=1000)
        yield ChatInput(
            placeholder="message   esc to leave, z to expand, /help for commands",
            id="chat-input",
        )

    # --------------------------------------------------------------- title

    def set_title(self, text: str) -> None:
        self.base_title = text
        self._apply_title()

    def _apply_title(self) -> None:
        try:
            typing = self.query_one("#chat-input", ChatInput).has_focus
        except NoMatches:
            typing = False
        suffix = "  -  typing, esc to leave" if typing else ""
        self.border_title = self.base_title + suffix

    def update_counter(self, entry: str) -> None:
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
    def log(self) -> RichLog:
        return self.query_one("#chat-log", RichLog)

    # ------------------------------------------------------------- targets

    def active_target(self) -> tuple:
        target = self._state.active_target if self._state else ("channel", 0)
        # The corner pane never dwells on the merged view; fall back to the
        # first channel so sending always has a concrete destination.
        if target and target[0] == "all":
            return ("channel", 0)
        return target

    def set_channels(self, state: MeshState) -> None:
        """Point at the first channel if nothing valid is selected."""
        self._state = state
        pairs = state.channel_pairs()
        current = state.active_target
        valid = (current[0] == "dm" and current[1] in state.dm_contacts) or (
            current[0] == "channel" and any(i == current[1] for i, _ in pairs))
        if not valid and pairs:
            state.active_target = ("channel", pairs[0][0])
        self.rerender(state)

    def goto(self, target: tuple, state: MeshState) -> None:
        state.active_target = target
        self.rerender(state)

    def cycle(self, step: int, state: MeshState) -> None:
        """Step through channels then DMs, skipping the merged view."""
        targets = [("channel", i) for i, _ in state.channel_pairs()]
        targets += [("dm", n) for n in sorted(state.dm_contacts)]
        if not targets:
            return
        try:
            here = targets.index(self.active_target())
        except ValueError:
            here = 0
        self.goto(targets[(here + step) % len(targets)], state)

    def goto_channel(self, index: int, state: MeshState) -> bool:
        if any(i == index for i, _ in state.channel_pairs()):
            self.goto(("channel", index), state)
            return True
        return False

    def focus_dm(self, node_id: str, state: MeshState) -> None:
        state.dm_contacts.add(node_id)
        self.goto(("dm", node_id), state)

    # ------------------------------------------------------------ rendering

    def rerender(self, state: MeshState) -> None:
        self._state = state
        target = self.active_target()
        state.mark_read(target)
        write_conversation(self.log, state.messages_for(target), state)
        self._update_button(state, target)

    def add(self, message: ChatMessage, state: MeshState) -> bool:
        """Re-render if the message belongs to the active conversation."""
        self._state = state
        if state.target_key(message) != self.active_target():
            return False
        self.rerender(state)
        return True

    def _update_button(self, state: MeshState, target: tuple) -> None:
        label = state.target_label(target)
        total_unread = sum(state.unread.values())
        button = self.query_one("#chat-channel", ChannelButton)
        line = Text()
        line.append(f" {label}  ▾", style="bold bright_white")
        line.append("    z to expand", style="grey42")
        if total_unread:
            line.append(f"    {total_unread} unread", style="bold bright_cyan")
        button.update(line)
        self.set_title("chat")

    def notice(self, text: str, style: str = "grey62") -> None:
        self.log.write(Text(text, style=style))

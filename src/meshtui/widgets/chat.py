"""Corner chat pane: a read-only monitor of every channel at once.

This is the glance view — it shows all channels merged, each line tagged with
its channel, and has no input. To read a single channel at length or to type a
message, press `z` (or click the header) to open the pop-out overlay.

The `ChatInput`, `LeaveChat` and `OpenChatOverlay` types live here because the
overlay reuses them; only the pane itself changed to a monitor.
"""

from __future__ import annotations

import re

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Input, RichLog, Static

from ..model import DEFAULT_MAX_PAYLOAD, ChatMessage
from ..state import MeshState
from .chat_render import write_conversation

ALL: tuple = ("all",)


class LeaveChat(Message):
    """The user wants focus back out of a message box."""


class OpenChatOverlay(Message):
    """The user asked to expand chat to the full-screen overlay."""


MENTION = re.compile(r"@\[?([^\]@]{0,40})$")


class ChatInput(Input):
    """Message box that can be escaped from, with @mention completion.

    While it has focus every printable key is text, so the single-letter app
    bindings are unreachable - `escape` is the documented way back out.

    Typing `@` (plus any part of a name - substring, so emoji-led names are
    reachable) shows candidates on the border; Tab inserts the first as
    `@[Name] ` and cycles through the rest. Names come from the node table
    and from senders seen in chat, newest first.
    """

    BINDINGS = [Binding("escape", "leave", "leave chat")]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._hits: list[str] = []
        self._cycle_hits: list[str] = []
        self._cycle_index = 0
        self._cycle_value: str | None = None
        self._cycle_start = 0

    def action_leave(self) -> None:
        self.post_message(LeaveChat())

    # ---------------------------------------------------------- mentions

    def _mention_query(self) -> tuple[int, str] | None:
        """The '@partial' being typed at the cursor: (start offset, partial)."""
        matched = MENTION.search(self.value[: self.cursor_position])
        return (matched.start(), matched.group(1)) if matched else None

    def _known_names(self) -> list[tuple[float, str]]:
        state = getattr(self.app, "state", None)
        if state is None:
            return []
        names: list[tuple[float, str]] = []
        for node in state.nodes.values():
            if node.long_name:
                names.append((node.last_heard or 0.0, node.long_name))
        # People talking on channels may not be in the node table at all -
        # and they are exactly who you want to @.
        from ..pathcalc import split_sender
        for message in state.chat:
            sender, _ = split_sender(message.text)
            if sender:
                names.append((message.ts, sender))
            elif message.from_name:
                names.append((message.ts, message.from_name))
        return names

    def _candidates(self, partial: str) -> list[str]:
        wanted = partial.casefold()
        starts: dict[str, float] = {}
        contains: dict[str, float] = {}
        for ts, name in self._known_names():
            folded = name.casefold()
            if wanted and wanted not in folded:
                continue
            bucket = starts if folded.startswith(wanted) else contains
            if ts >= bucket.get(name, -1.0):
                bucket[name] = ts
        rank = lambda bucket: [n for n, _ in sorted(bucket.items(),
                                                    key=lambda kv: -kv[1])]
        merged = rank(starts) + [n for n in rank(contains) if n not in starts]
        return merged[:6]

    def _refresh_hints(self) -> None:
        query = self._mention_query()
        self._hits = self._candidates(query[1]) if query else []
        if self._hits:
            shown = "  ".join(self._hits[:4])
            self.border_subtitle = f" {shown[:70]}  ⇥ complete "
        else:
            self.border_subtitle = ""

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.value != self._cycle_value:
            self._cycle_value = None  # any edit ends a Tab cycle
        self._refresh_hints()

    def _complete_mention(self) -> None:
        if self._cycle_value is not None and self.value == self._cycle_value:
            # Tab again: swap in the next candidate.
            self._cycle_index = (self._cycle_index + 1) % len(self._cycle_hits)
            start = self._cycle_start
        else:
            query = self._mention_query()
            if query is None or not self._hits:
                return
            start, self._cycle_hits, self._cycle_index = query[0], self._hits, 0
            self._cycle_start = start
        name = self._cycle_hits[self._cycle_index]
        completed = f"{self.value[:start]}@[{name}] "
        self.value = completed
        self.cursor_position = len(completed)
        self._cycle_value = completed

    async def _on_key(self, event) -> None:
        if event.key == "tab":
            in_cycle = self._cycle_value is not None and self.value == self._cycle_value
            if in_cycle or (self._mention_query() and self._hits):
                event.stop()
                event.prevent_default()
                self._complete_mention()
                return
        await super()._on_key(event)


class ChatHeader(Static):
    """Clickable header that opens the overlay."""

    def on_click(self) -> None:
        self.post_message(OpenChatOverlay())


class ChatPane(Vertical):
    """Read-only merged feed of every channel. No input; `z` opens the overlay."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_title = "chat"
        self.max_bytes = DEFAULT_MAX_PAYLOAD   # read by the overlay via the app
        self._state: MeshState | None = None

    def compose(self) -> ComposeResult:
        yield ChatHeader(id="chat-header")
        # min_width=20: RichLog's default of 78 renders lines 78 cells wide
        # even in a narrower pane, which summons a horizontal scrollbar.
        yield RichLog(id="chat-log", highlight=False, markup=False,
                      wrap=True, min_width=20, max_lines=1000)

    def set_title(self, text: str) -> None:
        self.base_title = text
        try:
            self.border_title = text
        except NoMatches:
            pass

    @property
    def log(self) -> RichLog:
        return self.query_one("#chat-log", RichLog)

    # ------------------------------------------------------------- targets

    def active_target(self) -> tuple:
        """The conversation a typed message would go to.

        The monitor shows everything, but sending still needs a concrete
        destination - that is whatever the overlay last selected, defaulting to
        the first channel.
        """
        target = self._state.active_target if self._state else ("channel", 0)
        return target

    def set_channels(self, state: MeshState) -> None:
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

    def goto_channel(self, index: int, state: MeshState) -> bool:
        if any(i == index for i, _ in state.channel_pairs()):
            state.active_target = ("channel", index)
            return True
        return False

    def focus_dm(self, node_id: str, state: MeshState) -> None:
        state.dm_contacts.add(node_id)
        state.active_target = ("dm", node_id)

    # ------------------------------------------------------------ rendering

    def rerender(self, state: MeshState) -> None:
        self._state = state
        write_conversation(self.log, state.messages_for(ALL), state,
                           show_channel=True)
        self._update_header(state)

    def add(self, message: ChatMessage, state: MeshState) -> bool:
        # The monitor shows every channel, so every message belongs here.
        self.rerender(state)
        return True

    def on_resize(self) -> None:
        # RichLog wraps at write time, so lines written in a wider pane stay
        # wide after a shrink; re-wrap them at the new width.
        if self._state is not None:
            self.rerender(self._state)

    def _update_header(self, state: MeshState) -> None:
        unread = sum(state.unread.values())
        header = self.query_one("#chat-header", ChatHeader)
        line = Text()
        line.append(" all channels", style="bold bright_white")
        line.append("    z or click to open", style="grey42")
        if unread:
            line.append(f"    {unread} unread", style="bold bright_cyan")
        header.update(line)

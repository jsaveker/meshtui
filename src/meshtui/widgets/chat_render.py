"""Shared rendering for chat conversations.

One renderer so the corner pane and the pop-out overlay format messages the
same way. Messages from the same sender in a row are grouped under a single
header, with the body indented and wrapped — far more readable than one folded
`time name: text` line per message, which is what a mesh chat tends to collapse
into.
"""

from __future__ import annotations

import time
from typing import Iterable

from rich.text import Text

from ..model import ChatMessage, DeliveryStatus
from ..pathcalc import split_sender
from ..state import MeshState

# Consecutive messages from the same sender within this many seconds share a
# header, the way every modern chat client groups them.
GROUP_GAP = 300.0

INDENT = "  "


def _sender(msg: ChatMessage, state: MeshState) -> str:
    if msg.outgoing:
        return "you"
    if msg.from_id.startswith("channel:"):
        # MeshCore channel messages carry no sender on the wire - the name is
        # embedded in the text ('Name: message'). Show the person, not the
        # placeholder id; _body() strips the prefix so it isn't said twice.
        name, _ = split_sender(msg.text)
        return name or "anonymous"
    return msg.from_name or state.node_name(msg.from_id)


def _body(msg: ChatMessage) -> str:
    """The message text, minus the sender prefix when it became the header."""
    if not msg.outgoing and msg.from_id.startswith("channel:"):
        name, rest = split_sender(msg.text)
        if name:
            return rest
    return msg.text


def write_conversation(
    log,
    messages: Iterable[ChatMessage],
    state: MeshState,
    *,
    show_channel: bool = False,
    grouped: bool = True,
) -> None:
    """Render `messages` into a RichLog, grouped by sender.

    show_channel prefixes each group with its channel — used by the merged
    "all activity" view where messages come from many channels at once.
    """
    messages = list(messages)
    log.clear()
    if not messages:
        log.write(Text("  no messages yet", style="grey42"))
        return

    prev_key = None
    prev_ts = 0.0
    for msg in messages:
        sender = _sender(msg, state)
        # Group key includes the channel so a channel change always breaks the
        # group even from the same sender.
        key = (sender, msg.is_dm, msg.channel)
        new_group = (
            not grouped
            or key != prev_key
            or (msg.ts - prev_ts) > GROUP_GAP
        )

        if new_group:
            if prev_key is not None:
                log.write(Text(""))  # blank line between speakers
            log.write(_header(msg, sender, state, show_channel))
        for line in _body(msg).splitlines() or [""]:
            body = Text(INDENT, no_wrap=False, overflow="fold")
            body.append(line, style="white" if not msg.outgoing else "grey85")
            log.write(body)

        prev_key = key
        prev_ts = msg.ts


def _header(msg: ChatMessage, sender: str, state: MeshState,
            show_channel: bool) -> Text:
    header = Text(no_wrap=True, overflow="ellipsis")
    if msg.outgoing:
        header.append(sender, style="bold bright_cyan")
    elif msg.is_dm:
        header.append(sender, style="bold bright_yellow")
    else:
        header.append(sender, style="bold bright_green")

    if show_channel and not msg.is_dm:
        name = state.channel_name(msg.channel)
        name = name if name.startswith("#") else f"#{name}"
        header.append(f"  {name}", style="grey54")
    elif msg.is_dm and not msg.outgoing:
        header.append("  dm", style="bright_yellow")

    header.append("   ")
    header.append(time.strftime("%H:%M", time.localtime(msg.ts)), style="grey42")
    if msg.outgoing:
        marker, style = _delivery_marker(msg)
        header.append(f"  {marker}", style=style)
        repeaters = getattr(msg, "repeated_by", None)
        if repeaters:
            header.append(f"  ⟳ {', '.join(sorted(repeaters))}", style="bright_blue")
    return header


def _delivery_marker(msg: ChatMessage) -> tuple[str, str]:
    """Render local acceptance separately from end-to-end delivery."""
    status = msg.delivery_status
    if msg.acked or status == DeliveryStatus.DELIVERED.value:
        return ("✓", "green")
    if status == DeliveryStatus.FAILED.value:
        return ("!", "bold red")
    if status == DeliveryStatus.EXPIRED.value:
        return ("×", "red")
    if status == DeliveryStatus.QUEUED.value:
        return ("◷", "yellow")
    return ("··", "grey42")


def compact_line(msg: ChatMessage, state: MeshState) -> Text:
    """A single-line form, for anywhere a full group is too much."""
    line = Text(overflow="fold")
    line.append(time.strftime("%H:%M", time.localtime(msg.ts)), style="grey42")
    line.append(" ")
    if msg.outgoing:
        marker, style = _delivery_marker(msg)
        line.append(f"{marker:<2} ", style=style)
        line.append("you", style="bold bright_cyan")
    else:
        line.append(_sender(msg, state), style="bold bright_green")
    line.append(": ", style="grey42")
    line.append(msg.text, style="white")
    return line

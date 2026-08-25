"""Channel security audit and traffic metadata.

Two questions this answers:

1. Are the channels on *your* node actually private? A PSK that is a
   single-byte shorthand, or shorter than 16 bytes, is not - and the firmware
   will happily use it anyway.
2. What can be learned from traffic nobody can decrypt? Rather a lot: sender,
   channel hash, hop count and signal strength all travel in the clear, so the
   activity map below needs no keys at all.
"""

from __future__ import annotations

import time

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ..state import MeshState
from .stats import fmt_duration

LEVEL_STYLE = {
    "OPEN": "bold red",
    "PUBLIC": "bold red",
    "WEAK": "bold dark_orange",
    "AES128": "green",
    "AES256": "bright_green",
    "UNKNOWN": "grey42",
}

LEVEL_VERDICT = {
    "OPEN": "NOT PRIVATE",
    "PUBLIC": "NOT PRIVATE",
    "WEAK": "WEAK",
    "AES128": "ok",
    "AES256": "ok",
    "UNKNOWN": "unknown",
}


class AuditView(Static):
    """Renders the whole report; cheap enough to rebuild on a timer."""

    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state

    def render_report(self) -> None:
        self.update(Group(*self._sections()))

    def _sections(self) -> list[object]:
        parts: list[object] = []
        parts.append(Text("your own channels", style="bold bright_cyan"))
        parts.append(self._local_table())
        parts.append(Text(""))
        parts.append(Text("channels you hold no key for", style="bold bright_cyan"))
        parts.append(self._foreign_table())
        parts.append(Text(""))
        parts.append(Text("who transmits on them  (metadata - no key required)",
                          style="bold bright_cyan"))
        parts.append(self._activity_table())
        parts.append(Text(""))
        parts.append(self._footnote())
        return parts

    # ------------------------------------------------------------- sections

    def _local_table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        for justify, width in (("right", 3), ("left", 16), ("left", 12),
                               ("left", 5), ("left", 46)):
            table.add_column(justify=justify, width=width)
        table.add_row(*[Text(h, style="grey42") for h in
                        ("#", "name", "verdict", "hash", "why")])

        if not self.state.local_channels:
            table.add_row(Text(""), Text("not connected yet", style="grey42"),
                          Text(""), Text(""), Text(""))
            return table

        for ch in self.state.local_channels:
            style = LEVEL_STYLE.get(ch.level, "grey42")
            table.add_row(
                Text(str(ch.index), style="grey62"),
                Text(ch.name, style="bright_white"),
                Text(LEVEL_VERDICT.get(ch.level, ch.level), style=style),
                Text("-" if ch.hash is None else str(ch.hash), style="grey62"),
                Text(ch.detail, style="grey70"),
            )
        return table

    def _foreign_table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        for justify, width in (("right", 5), ("right", 7), ("right", 7),
                               ("left", 14), ("left", 12), ("left", 30)):
            table.add_column(justify=justify, width=width)
        table.add_row(*[Text(h, style="grey42") for h in
                        ("hash", "packets", "senders", "signal", "last", "status")])

        channels = sorted(self.state.foreign_channels.values(),
                          key=lambda c: -c.packets)
        if not channels:
            table.add_row(Text(""), Text(""), Text(""),
                          Text("none seen", style="grey42"), Text(""), Text(""))
            return table

        for ch in channels:
            if ch.readable:
                status = Text(f"PUBLIC KEY ({ch.key_label})", style="bold red")
            else:
                status = Text("no published key applies", style="green")
            if ch.snr_min is None:
                signal = Text("-", style="grey42")
            else:
                signal = Text(f"{ch.snr_min:+.0f}..{ch.snr_max:+.0f}dB", style="yellow")
            table.add_row(
                Text(str(ch.hash), style="bright_white"),
                Text(str(ch.packets), style="grey70"),
                Text(str(len(ch.senders)), style="grey70"),
                signal,
                Text(fmt_duration(max(0.0, time.time() - ch.last_seen)) + " ago",
                     style="grey62"),
                status,
            )
        return table

    def _activity_table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        for justify, width in (("left", 12), ("left", 20), ("right", 7),
                               ("left", 10), ("left", 12), ("left", 20)):
            table.add_column(justify=justify, width=width)
        table.add_row(*[Text(h, style="grey42") for h in
                        ("node", "name", "packets", "hops", "signal", "on channels")])

        # Build sender -> channels from the foreign-channel observations.
        senders: dict[str, list] = {}
        for ch in self.state.foreign_channels.values():
            for node_id in ch.senders:
                senders.setdefault(node_id, []).append(ch)

        if not senders:
            table.add_row(Text("nothing yet", style="grey42"),
                          *[Text("") for _ in range(5)])
            return table

        rows = sorted(senders.items(), key=lambda kv: -sum(c.packets for c in kv[1]))
        for node_id, channels in rows[:25]:
            node = self.state.nodes.get(node_id)
            hops = [c.hops_min for c in channels if c.hops_min is not None]
            snrs = [c.snr_max for c in channels if c.snr_max is not None]
            table.add_row(
                Text(node_id, style="bright_white"),
                Text((node.name if node else "unknown")[:20],
                     style="grey70" if node else "grey42"),
                Text(str(sum(c.packets for c in channels)), style="grey70"),
                Text(str(min(hops)) if hops else "-", style="cyan"),
                Text(f"{max(snrs):+.1f}dB" if snrs else "-", style="yellow"),
                Text(", ".join(str(c.hash) for c in sorted(
                    channels, key=lambda c: -c.packets)[:5]), style="grey62"),
            )
        return table

    def _footnote(self) -> Text:
        note = Text()
        note.append("Published keys are the default channel key and the "
                    "single-byte shorthands, both listed in Meshtastic's own source.\n",
                    style="grey54")
        note.append("A channel marked ", style="grey54")
        note.append("no published key applies", style="green")
        note.append(" is using a real random PSK and is not readable here.\n",
                    style="grey54")
        note.append("Everything in the last table is metadata that travels in the "
                    "clear - it needs no key at all.", style="grey54")
        return note


class AuditScreen(Screen[None]):
    BINDINGS = [
        Binding("escape,a,q", "close", "back"),
        Binding("up,k", "scroll_up", "scroll", show=False),
        Binding("down,j", "scroll_down", "scroll", show=False),
    ]

    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state
        self.view = AuditView(state)

    def compose(self) -> ComposeResult:
        yield Static(
            Text(" channel audit  -  is any of this actually private?", style="grey62"),
            id="audit-status",
        )
        with VerticalScroll(id="audit-box"):
            yield self.view
        yield Footer()

    def on_mount(self) -> None:
        self.view.render_report()
        self.set_interval(3.0, self.view.render_report)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_scroll_up(self) -> None:
        self.query_one("#audit-box", VerticalScroll).scroll_up()

    def action_scroll_down(self) -> None:
        self.query_one("#audit-box", VerticalScroll).scroll_down()

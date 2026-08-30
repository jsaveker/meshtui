"""Live packet feed.

Backed by a DataTable rather than a RichLog so every line keeps a cursor and a
link back to its Packet, which is what makes the inspector possible.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from ..model import Packet, port_label
from ..state import MeshState

MAX_ROWS = 2000

# Filter modes cycled with `f`
FILTERS: list[tuple[str, set[str] | None]] = [
    ("all", None),
    ("chatty", {"TEXT_MESSAGE_APP", "POSITION_APP", "NODEINFO_APP", "TRACEROUTE_APP"}),
    ("text only", {"TEXT_MESSAGE_APP"}),
]


class PacketFeed(DataTable):
    """Scrolling one-line-per-packet view of everything the radio hears."""

    BINDINGS = [
        Binding("G,end", "follow_end", "latest", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(cursor_type="row", show_header=False, zebra_stripes=False, **kwargs)
        self.paused = False
        self.filter_index = 0
        self.follow = True          # stick to the newest row until the user scrolls up
        self._pending: list[Packet] = []
        self._rows: list[Packet] = []
        self._seq = 0

    def on_mount(self) -> None:
        self.add_column("line", key="line")

    @property
    def filter_name(self) -> str:
        return FILTERS[self.filter_index][0]

    # ------------------------------------------------------------- scrolling

    def action_cursor_up(self) -> None:
        self.follow = False
        super().action_cursor_up()

    def action_page_up(self) -> None:
        self.follow = False
        super().action_page_up()

    def action_scroll_home(self) -> None:
        self.follow = False
        super().action_scroll_home()

    def action_scroll_top(self) -> None:
        self.follow = False
        super().action_scroll_top()

    def action_cursor_down(self) -> None:
        super().action_cursor_down()
        if self.cursor_row >= len(self._rows) - 1:
            self.follow = True

    def action_follow_end(self) -> None:
        self.follow = True
        if self._rows:
            self.move_cursor(row=len(self._rows) - 1)

    def selected_packet(self) -> Packet | None:
        if not self._rows or not (0 <= self.cursor_row < len(self._rows)):
            return None
        row = self._rows[self.cursor_row]
        return row if isinstance(row, Packet) else None

    # --------------------------------------------------------------- content

    def _passes(self, packet: Packet) -> bool:
        allowed = FILTERS[self.filter_index][1]
        return allowed is None or packet.portnum in allowed

    def cycle_filter(self, state: MeshState) -> str:
        self.filter_index = (self.filter_index + 1) % len(FILTERS)
        self.rerender(state)
        return self.filter_name

    def toggle_pause(self, state: MeshState) -> bool:
        self.paused = not self.paused
        if not self.paused:
            for pkt in self._pending:
                self._write_line(pkt, state)
            self._pending.clear()
        return self.paused

    def add(self, packet: Packet, state: MeshState) -> None:
        if not self._passes(packet):
            return
        if self.paused:
            self._pending.append(packet)
            if len(self._pending) > MAX_ROWS:
                self._pending.pop(0)
            return
        self._write_line(packet, state)

    def rerender(self, state: MeshState, limit: int | None = None) -> None:
        self.clear()
        self._rows = []
        packets = [p for p in state.packets if self._passes(p)]
        if limit is not None and len(packets) > limit:
            packets = packets[-limit:]
        for pkt in packets:
            self._write_line(pkt, state)

    def clear_feed(self) -> None:
        self.clear()
        self._rows = []
        self._pending.clear()

    def on_resize(self) -> None:
        # Packet lines are padded to the pane width, so a resize re-renders
        # them - after the reflow, so the new region width is readable.
        self.call_after_refresh(self._relayout)

    def _relayout(self) -> None:
        if not self._rows:
            return
        rows = list(self._rows)
        keep = self.cursor_row
        # columns=True: DataTable keeps the widest cell it ever saw per
        # column, so shrinking would leave a phantom horizontal scrollbar.
        self.clear(columns=True)
        self.add_column("line", key="line")
        self._rows = []
        state = self.app.state  # type: ignore[attr-defined]
        for row in rows:
            if isinstance(row, Packet):
                self._write_line(row, state)
            else:
                self._readd_notice(row)
        if self._rows:
            self.move_cursor(row=min(max(keep, 0), len(self._rows) - 1))

    def write_notice(self, text: str, style: str = "grey62") -> None:
        """Put a non-packet line in the feed (errors, status, traceroute)."""
        self._readd_notice(Text(text, style=style, no_wrap=True))
        if self.follow:
            self.move_cursor(row=len(self._rows) - 1)

    def _readd_notice(self, renderable: Text) -> None:
        self._seq += 1
        region = self.scrollable_content_region.width or (self.size.width - 3)
        shown = renderable.copy()
        shown.truncate(max(20, region - 2))
        self.add_row(shown, key=f"n{self._seq}")
        # Store the renderable itself, not None, so a resize can rebuild it and
        # so selected_packet can tell notices from packets.
        self._rows.append(renderable)

    def _write_line(self, packet: Packet, state: MeshState) -> None:
        if packet is None:  # notice rows are re-added elsewhere
            return
        label, colour = port_label(packet.portnum)
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(time.strftime("%H:%M:%S", time.localtime(packet.ts)), style="grey42")
        line.append(" ")
        line.append(f"{label:<6}", style=f"bold {colour}")
        line.append(" ")
        line.append(f"{state.node_name(packet.from_id):<5}"[:5], style="bright_white")
        line.append(" -> ", style="grey42")
        dest = "all" if packet.is_broadcast else state.node_name(packet.to_id)
        line.append(f"{dest:<5}"[:5], style="grey62" if packet.is_broadcast else "bright_yellow")
        line.append("  ")

        if packet.snr is not None:
            snr_style = "green" if packet.snr >= 0 else ("yellow" if packet.snr >= -8 else "red")
            line.append(f"{packet.snr:+5.1f}dB ", style=snr_style)
        else:
            line.append("        ")
        if packet.hops:
            line.append(f"{packet.hops}h ", style="cyan")
        else:
            line.append("   ")

        if packet.decrypted_with:
            # Opened with a key that is published upstream - flag it so this is
            # never mistaken for traffic that was actually private.
            line.append("[pub] ", style="bold red")
        line.append(
            packet.summary,
            style="white" if packet.portnum == "TEXT_MESSAGE_APP" else "grey70",
        )

        # Fit inside the content region minus the cell padding, or the row
        # overflows by a few cells and summons a horizontal scrollbar.
        region = self.scrollable_content_region.width or (self.size.width - 3)
        line.truncate(max(20, region - 2), pad=True)

        self._seq += 1
        self.add_row(line, key=f"p{self._seq}")
        self._rows.append(packet)

        while len(self._rows) > MAX_ROWS:
            try:
                self.remove_row(self.ordered_rows[0].key)
            except Exception:  # noqa: BLE001 - row already gone
                pass
            self._rows.pop(0)

        if self.follow:
            self.move_cursor(row=len(self._rows) - 1)

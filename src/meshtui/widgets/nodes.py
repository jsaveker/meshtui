"""Node table - one row per node heard on the mesh."""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import DataTable

from ..model import SPARK_WIDTH, Node
from ..state import MeshState

SPARK_CHARS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
SNR_FLOOR, SNR_CEIL = -20.0, 10.0

SORTS = ["heard", "name", "snr", "hops", "packets"]


def fmt_age(seconds: float | None) -> Text:
    if seconds is None:
        return Text("-", style="grey42")
    if seconds < 60:
        return Text(f"{int(seconds)}s", style="green")
    if seconds < 3600:
        style = "green" if seconds < 900 else "yellow"
        return Text(f"{int(seconds // 60)}m", style=style)
    if seconds < 86400:
        return Text(f"{int(seconds // 3600)}h", style="red")
    return Text(f"{int(seconds // 86400)}d", style="grey42")


def fmt_snr(snr: float | None) -> Text:
    if snr is None:
        return Text("-", style="grey42")
    # Meshtastic SNR is usable from roughly -20 dB up; colour the useful bands.
    if snr >= 0:
        style = "bright_green"
    elif snr >= -8:
        style = "yellow"
    else:
        style = "red"
    return Text(f"{snr:+.1f}", style=style)


def snr_spark(history) -> Text:
    """Rolling SNR as a block sparkline, each bar coloured by its own quality."""
    out = Text()
    values = list(history)
    if not values:
        # Blank rather than a placeholder glyph: every candidate character also
        # means something on the SNR scale, which would read as real data.
        return Text(" " * SPARK_WIDTH)
    out.append(" " * (SPARK_WIDTH - len(values)))
    for snr in values:
        clamped = max(SNR_FLOOR, min(SNR_CEIL, snr))
        level = int((clamped - SNR_FLOOR) / (SNR_CEIL - SNR_FLOOR) * (len(SPARK_CHARS) - 1))
        if snr >= 0:
            style = "bright_green"
        elif snr >= -8:
            style = "yellow"
        elif snr >= -15:
            style = "dark_orange"
        else:
            style = "red"
        out.append(SPARK_CHARS[level], style=style)
    return out


def fmt_battery(node: Node) -> Text:
    pct = node.battery
    if pct is None:
        return Text("-", style="grey42")
    if pct > 100:  # 101 means "plugged in" in the Meshtastic protocol
        return Text("PWR", style="bright_cyan")
    if pct >= 60:
        style = "green"
    elif pct >= 25:
        style = "yellow"
    else:
        style = "red"
    return Text(f"{pct}%", style=style)


def fmt_hops(hops: int | None) -> Text:
    if hops is None:
        return Text("-", style="grey42")
    if hops == 0:
        return Text("dir", style="bright_green")
    return Text(f"{hops}", style="cyan")


class NodeTable(DataTable):
    """Sortable table of every node in the mesh."""

    def __init__(self, **kwargs) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self.sort_key = "heard"
        self._row_ids: list[str] = []

    def on_mount(self) -> None:
        # Widths are tuned to fit the 58-cell left column without clipping
        # the trailing Age column (DataTable adds 2 cells of padding each).
        self.add_column("", key="dot", width=1)
        self.add_column("Node", key="name", width=16)
        self.add_column("SNR", key="snr", width=5)
        self.add_column("Trend", key="trend", width=SPARK_WIDTH)
        self.add_column("Hop", key="hops", width=3)
        self.add_column("Bat", key="batt", width=4)
        self.add_column("Pkt", key="pkts", width=4)
        self.add_column("Age", key="age", width=4)

    def cycle_sort(self) -> str:
        self.sort_key = SORTS[(SORTS.index(self.sort_key) + 1) % len(SORTS)]
        return self.sort_key

    def selected_node_id(self) -> str | None:
        if not self._row_ids or self.cursor_row < 0:
            return None
        if self.cursor_row >= len(self._row_ids):
            return None
        return self._row_ids[self.cursor_row]

    def render_state(self, state: MeshState) -> None:
        """Rebuild the table. Cheap enough at mesh scale (tens of nodes)."""
        keep = self.cursor_row
        self.clear()
        self._row_ids = []
        now = time.time()
        for node in state.sorted_nodes(self.sort_key):
            age = None if node.last_heard is None else now - node.last_heard
            if node.is_self:
                dot = Text("*", style="bold bright_cyan")
            elif age is not None and age < 900:
                dot = Text("+", style="green")
            else:
                dot = Text("-", style="grey42")

            name = Text(node.label.ljust(5)[:5], style="bold")
            name.append(" ")
            name.append(node.long_name[:10] or node.node_id, style="grey70")

            self.add_row(
                dot,
                name,
                fmt_snr(node.snr),
                snr_spark(node.snr_history),
                fmt_hops(node.hops),
                fmt_battery(node),
                Text(str(node.packets), style="grey70"),
                fmt_age(age),
            )
            self._row_ids.append(node.node_id)

        if self._row_ids:
            self.move_cursor(row=min(max(keep, 0), len(self._row_ids) - 1))

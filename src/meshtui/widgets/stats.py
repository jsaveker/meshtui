"""Aggregate mesh statistics."""

from __future__ import annotations

import time

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from ..model import port_label
from ..state import MeshState


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def bar(fraction: float, width: int = 10) -> Text:
    filled = max(0, min(width, round(fraction * width)))
    return Text("#" * filled + "." * (width - filled), style="cyan")


class StatsPane(Static):
    """Counters, packet-type mix, and link-quality summary."""

    def render_state(self, state: MeshState) -> None:
        stats = state.stats
        now = time.time()

        active = sum(
            1 for n in state.nodes.values() if n.last_heard and now - n.last_heard < 900
        )
        direct = sum(1 for n in state.nodes.values() if n.hops == 0)
        snrs = [n.snr for n in state.nodes.values() if n.snr is not None]
        positioned = sum(1 for n in state.nodes.values() if n.has_position)

        summary = Table.grid(padding=(0, 1), expand=True)
        summary.add_column(justify="left", ratio=1)
        summary.add_column(justify="right")
        summary.add_column(justify="left", ratio=1)
        summary.add_column(justify="right")

        summary.add_row(
            Text("packets", style="grey62"),
            Text(f"{stats.total}", style="bold bright_white"),
            Text("nodes", style="grey62"),
            Text(f"{len(state.nodes)}", style="bold bright_white"),
        )
        summary.add_row(
            Text("pkt/min", style="grey62"),
            Text(f"{stats.rate_per_min():.1f}", style="bold cyan"),
            Text("active 15m", style="grey62"),
            Text(f"{active}", style="bold green"),
        )
        summary.add_row(
            Text("sent", style="grey62"),
            Text(f"{stats.sent}", style="bold bright_cyan"),
            Text("direct", style="grey62"),
            Text(f"{direct}", style="bold green"),
        )
        summary.add_row(
            Text("uptime", style="grey62"),
            Text(fmt_duration(stats.uptime), style="bold grey70"),
            Text("with gps", style="grey62"),
            Text(f"{positioned}", style="bold grey70"),
        )
        if snrs:
            summary.add_row(
                Text("snr avg", style="grey62"),
                Text(f"{sum(snrs) / len(snrs):+.1f}", style="bold yellow"),
                Text("snr best", style="grey62"),
                Text(f"{max(snrs):+.1f}", style="bold bright_green"),
            )

        mix = Table.grid(padding=(0, 1), expand=True)
        mix.add_column(justify="left", width=7)
        mix.add_column(justify="left", width=10)
        mix.add_column(justify="right", width=6)
        top = stats.by_port.most_common(6)
        peak = top[0][1] if top else 1
        for portnum, count in top:
            label, colour = port_label(portnum)
            mix.add_row(
                Text(label, style=f"bold {colour}"),
                bar(count / peak),
                Text(str(count), style="grey70"),
            )
        if not top:
            mix.add_row(Text("waiting for traffic...", style="grey42"), Text(""), Text(""))

        parts = [summary, Text(""), Text(" packet mix", style="grey42"), mix]

        # Local RF statistics, polled over USB (MeshCore get_stats_radio).
        info = state.radio_info
        if info.get("noise_floor") is not None:
            radio = Table.grid(padding=(0, 1), expand=True)
            radio.add_column(justify="left", ratio=1)
            radio.add_column(justify="right")
            radio.add_column(justify="left", ratio=1)
            radio.add_column(justify="right")
            radio.add_row(
                Text("noise floor", style="grey62"),
                Text(f"{info['noise_floor']}dBm", style="bold yellow"),
                Text("last snr", style="grey62"),
                Text(f"{info.get('last_snr', 0):+.1f}", style="bold yellow"),
            )
            tx, rx = info.get("tx_air_secs"), info.get("rx_air_secs")
            radio.add_row(
                Text("tx air", style="grey62"),
                Text(fmt_duration(tx) if tx is not None else "-", style="bold cyan"),
                Text("rx air", style="grey62"),
                Text(fmt_duration(rx) if rx is not None else "-", style="bold cyan"),
            )
            parts += [Text(""), Text(" radio", style="grey42"), radio]

        self.update(Group(*parts))

"""Full detail view for a single node."""

from __future__ import annotations

import time

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ..model import Node
from .stats import fmt_duration


class NodeDetail(ModalScreen[None]):
    BINDINGS = [
        ("escape", "dismiss", "close"),
        ("q", "dismiss", "close"),
        ("enter", "dismiss", "close"),
    ]

    def __init__(self, node: Node) -> None:
        super().__init__()
        self.node = node

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-box"):
            yield Static(self._body(), id="detail-body")
            yield Static(
                Text("  esc close    t traceroute    d open dm", style="grey42"),
                id="detail-help",
            )

    def _body(self) -> Table:
        node = self.node
        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", style="grey62", width=12)
        table.add_column(justify="left")

        def row(label: str, value: object, style: str = "white") -> None:
            if value is None or value == "":
                return
            table.add_row(label, Text(str(value), style=style))

        table.add_row("", Text(node.name, style="bold bright_cyan"))
        table.add_row("", Text(""))
        row("id", node.node_id, "bright_white")
        row("short", node.short_name)
        row("hardware", node.hw_model, "grey70")
        row("role", node.role, "grey70")
        row("num", node.num)
        if node.is_self:
            table.add_row("", Text("this is your local node", style="bold bright_cyan"))

        table.add_row("", Text(""))
        row("snr", f"{node.snr:+.2f} dB" if node.snr is not None else None, "yellow")
        row("rssi", f"{node.rssi} dBm" if node.rssi is not None else None, "yellow")
        row("hops", "direct" if node.hops == 0 else node.hops, "cyan")
        row("packets", node.packets, "bright_white")
        if node.last_heard:
            row("last heard", f"{fmt_duration(time.time() - node.last_heard)} ago", "green")
        row("first seen", f"{fmt_duration(time.time() - node.first_seen)} ago", "grey70")
        if node.via_mqtt:
            row("via", "MQTT", "magenta")

        table.add_row("", Text(""))
        row("battery", f"{node.battery}%" if node.battery is not None else None, "green")
        row("voltage", f"{node.voltage:.2f} V" if node.voltage is not None else None, "green")
        row("ch util", f"{node.ch_util:.1f} %" if node.ch_util is not None else None)
        row("air tx", f"{node.air_util:.2f} %" if node.air_util is not None else None)
        row("dev uptime", fmt_duration(node.uptime) if node.uptime else None, "grey70")

        if node.has_position:
            table.add_row("", Text(""))
            row("position", f"{node.lat:.5f}, {node.lon:.5f}", "bright_blue")
            row("altitude", f"{node.alt} m" if node.alt is not None else None, "bright_blue")
            row("map", f"https://www.google.com/maps?q={node.lat:.5f},{node.lon:.5f}", "blue")
        return table

    def action_dismiss(self) -> None:
        self.dismiss(None)

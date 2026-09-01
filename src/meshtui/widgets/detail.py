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
    # The help footer advertises t and d - a modal swallows keys, so they
    # must be bound HERE; the app-level bindings never see them.
    BINDINGS = [
        ("escape", "dismiss", "close"),
        ("q", "dismiss", "close"),
        ("enter", "dismiss", "close"),
        ("t", "trace", "traceroute"),
        ("d", "dm", "open dm"),
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

        if node.local_stats:
            table.add_row("", Text(""))
            st = node.local_stats
            row("tx / rx", f"{st.get('numPacketsTx', 0):.0f} / "
                           f"{st.get('numPacketsRx', 0):.0f}", "grey70")
            row("rx bad", f"{st.get('numPacketsRxBad', 0):.0f}", "grey70")
            row("rx dupe", f"{st.get('numRxDupe', 0):.0f}", "grey70")
            row("relayed", f"{st.get('numTxRelay', 0):.0f}", "cyan")
            if st.get("noiseFloor") is not None:
                row("noise floor", f"{st['noiseFloor']:.0f} dBm", "yellow")
            if st.get("numOnlineNodes") is not None:
                row("sees", f"{st['numOnlineNodes']:.0f} of "
                            f"{st.get('numTotalNodes', 0):.0f} nodes", "grey70")

        if node.env:
            table.add_row("", Text(""))
            for key, value in sorted(node.env.items()):
                row(key, f"{value:g}", "bright_green")

        if node.has_position:
            table.add_row("", Text(""))
            row("position", f"{node.lat:.5f}, {node.lon:.5f}", "bright_blue")
            if node.precision_bits is not None:
                metres = node.precision_metres
                detail = f"{node.precision_bits} bits"
                if metres:
                    detail += f"  (~{metres / 1000:.1f} km steps)"
                row("precision", detail, "dark_orange" if (metres or 0) > 500 else "grey70")
            row("gps source", node.location_source.replace("LOC_", "").title(), "grey70")
            row("satellites", node.sats, "grey70")
            if node.moving:
                heading = "" if node.heading_deg is None else f" heading {node.heading_deg:.0f}deg"
                row("moving", f"{node.speed_mps:.0f} m/s"
                              f" ({node.speed_mps * 3.6:.0f} km/h){heading}",
                    "bold bright_yellow")
            row("altitude", f"{node.alt} m" if node.alt is not None else None, "bright_blue")
            # An OSC-8 hyperlink: a short clickable label instead of a raw
            # URL the column width truncates into something un-copyable.
            url = f"https://www.google.com/maps?q={node.lat:.5f},{node.lon:.5f}"
            table.add_row("map", Text("open in google maps (click)",
                                      style=f"bright_blue underline link {url}"))
        return table

    def action_dismiss(self) -> None:
        self.dismiss(None)

    def action_trace(self) -> None:
        node, app = self.node, self.app
        self.dismiss(None)
        app._trace(node.node_id, 5)  # type: ignore[attr-defined]

    def action_dm(self) -> None:
        app = self.app
        self.dismiss(None)
        app.action_dm_selected()  # type: ignore[attr-defined]

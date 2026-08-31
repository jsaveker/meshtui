"""The packet-hex and selected-route panes in the operator workspace."""

from __future__ import annotations

import json
import time
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from ..geo import km_offsets
from ..model import Packet, port_label
from ..pathcalc import PathAnalysis, PathObservation, analyze, obs_from_packet
from ..radio import traceroute_hops
from ..state import MeshState
from .canvas import BrailleCanvas
from .inspect import hexdump


def packet_bytes(packet: Packet) -> tuple[str, bytes]:
    """Return the most useful wire-ish bytes available on a normalized packet."""
    raw = packet.raw if isinstance(packet.raw, dict) else {}
    decoded = raw.get("decoded") if isinstance(raw.get("decoded"), dict) else {}
    payload = decoded.get("payload") if decoded else None
    if isinstance(payload, (bytes, bytearray)):
        return "payload", bytes(payload)
    encrypted = raw.get("encrypted")
    if isinstance(encrypted, (bytes, bytearray)):
        return "encrypted", bytes(encrypted)
    try:
        return "normalized", json.dumps(raw, sort_keys=True, default=repr).encode()
    except (TypeError, ValueError):
        return "summary", packet.summary.encode("utf-8", errors="replace")


class PacketWorkbench(Vertical):
    """Live packet rows with an always-visible hex preview underneath."""

    def compose(self) -> ComposeResult:
        yield PacketFeed(id="packets")
        yield Static(Text(" select a packet to preview its bytes", style="grey42"),
                     id="packet-hex")

    def show_packet(self, packet: Packet | None, state: MeshState) -> None:
        box = self.query_one("#packet-hex", Static)
        if packet is None:
            box.update(Text(" select a packet to preview its bytes", style="grey42"))
            return
        label, colour = port_label(packet.portnum)
        kind, data = packet_bytes(packet)
        title = Text()
        title.append(f" {label}", style=f"bold {colour}")
        title.append(
            f"  {state.node_name(packet.from_id)} -> "
            f"{'all' if packet.is_broadcast else state.node_name(packet.to_id)}",
            style="grey70",
        )
        title.append(f"  {kind} {len(data)}B\n", style="grey42")
        title.append_text(hexdump(data, limit=96))
        box.update(title)


# Imported after PacketWorkbench is defined so the module doc reads in UI order.
from .packets import PacketFeed  # noqa: E402


class RoutePane(Vertical):
    """Route graph, geographic braille polyline, and clickable hop lookup."""

    def __init__(self, state: MeshState, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.packet: Packet | None = None
        self.observation: PathObservation | None = None
        self.analysis: PathAnalysis | None = None
        self.trace_rows: list[tuple[str, str, float | None]] = []
        self.trace_points: list[tuple[float, float, str, str]] = []

    def compose(self) -> ComposeResult:
        yield Static(Text(" select a routed packet", style="grey42"), id="route-chain")
        yield Static(id="route-canvas")
        yield DataTable(id="route-hops", cursor_type="row", zebra_stripes=True)
        yield Static(id="route-prefix")

    def on_mount(self) -> None:
        table = self.query_one("#route-hops", DataTable)
        table.add_column("hop", key="hop", width=3)
        table.add_column("hash", key="hash", width=8)
        table.add_column("resolved node", key="node")
        table.add_column("snr", key="snr", width=7)
        self.query_one("#route-canvas", Static).border_title = "map polyline"
        table.border_title = "hops - select for prefix lookup"

    def show_packet(self, packet: Packet | None) -> None:
        self.packet = packet
        self.trace_rows = []
        self.trace_points = []
        self.observation = obs_from_packet(packet) if packet is not None else None
        self.analysis = analyze(self.state, self.observation) if self.observation else None
        self._refresh_route()

    def on_resize(self) -> None:
        self._render_canvas()

    def _refresh_route(self) -> None:
        chain = self.query_one("#route-chain", Static)
        table = self.query_one("#route-hops", DataTable)
        table.clear()
        obs, analysis = self.observation, self.analysis
        if self.packet is None:
            chain.update(Text(" select a routed packet", style="grey42"))
            self.query_one("#route-canvas", Static).update("")
            self.query_one("#route-prefix", Static).update("")
            return
        if self.packet.portnum == "TRACEROUTE_APP":
            self._render_trace(table, chain)
            return
        if obs is None or analysis is None:
            chain.update(Text(
                f" {self.packet.summary or self.packet.portnum}\n no path metadata on this packet",
                style="grey42",
            ))
            self.query_one("#route-canvas", Static).update(
                Text(" route appears when MeshCore path metadata is present", style="grey42"))
            self.query_one("#route-prefix", Static).update("")
            return

        hashes = obs.hop_bytes()
        width = len(hashes[0]) // 2 if hashes else self._declared_hash_size()
        origin = obs.origin_name or (
            self.state.node_name(obs.origin_id) if obs.origin_id else "origin")
        line = Text()
        line.append(f" [{width}B path hash] ", style="bold black on bright_cyan")
        line.append(origin, style="bold bright_green")
        for hop in analysis.hops:
            line.append(" -> ", style="grey42")
            line.append(hop.label, style="yellow" if hop.node else "red")
        line.append(" -> me", style="bright_cyan")
        if obs.snr is not None:
            line.append(f"   final {obs.snr:+.1f}dB", style="grey62")
        chain.update(line)

        for index, hop in enumerate(analysis.hops, start=1):
            signal = obs.snr if index == len(analysis.hops) else None
            table.add_row(
                str(index), f"0x{hop.byte}", hop.label,
                f"{signal:+.1f}" if signal is not None else "-",
            )
        if analysis.hops:
            table.move_cursor(row=0)
            self._render_prefix(0)
        else:
            self.query_one("#route-prefix", Static).update(
                Text(" direct - no repeater hashes", style="bright_green"))
        self._render_canvas()

    def _render_trace(self, table: DataTable, chain: Static) -> None:
        assert self.packet is not None
        towards, back = traceroute_hops(self.packet.raw)
        if not towards:
            chain.update(Text(" traceroute reply contains no route", style="yellow"))
            self.query_one("#route-canvas", Static).update("")
            return
        out = Text(" round-trip trace  ", style="bold bright_magenta")
        for direction, rows in (("out", towards), ("back", back)):
            if direction == "back":
                out.append("\n return  ", style="bold bright_cyan")
            else:
                out.append("out  ", style="bold bright_cyan")
            for index, (num, snr) in enumerate(rows, start=1):
                node_id = f"!{num:08x}"
                name = self.state.node_name(node_id)
                if index > 1:
                    out.append(" -> ", style="grey42")
                out.append(name, style="bright_white")
                if snr is not None:
                    out.append(f" {snr:+.1f}dB", style="yellow")
                self.trace_rows.append((direction, node_id, snr))
                table.add_row(direction, node_id, name,
                              f"{snr:+.1f}" if snr is not None else "-")
        chain.update(out)
        for _, node_id, _ in self.trace_rows:
            node = self.state.nodes.get(node_id)
            if node is not None and node.has_position:
                self.trace_points.append((node.lat, node.lon, node.name, "hop"))
        if self.trace_rows:
            table.move_cursor(row=0)
            self._render_prefix(0)
        self._render_canvas()

    def _declared_hash_size(self) -> int:
        raw = self.packet.raw if self.packet and isinstance(self.packet.raw, dict) else {}
        value = raw.get("path_hash_size", 1)
        return value if value in (1, 2, 3, 4) else 1

    def _render_canvas(self) -> None:
        box = self.query_one("#route-canvas", Static)
        analysis = self.analysis
        points = self.trace_points or (analysis.points() if analysis else [])
        if len(points) < 2:
            box.update(Text(" not enough positioned route nodes to draw a map",
                            style="grey42"))
            return
        size = box.size
        cols = max(20, (size.width or 48) - 3)
        rows = max(4, (size.height or 8) - 1)
        clat = sum(p[0] for p in points) / len(points)
        clon = sum(p[1] for p in points) / len(points)
        offsets = [km_offsets(p[0], p[1], clat, clon) for p in points]
        span_x = max(abs(e) for e, _ in offsets) or 1.0
        span_y = max(abs(n) for _, n in offsets) or 1.0
        width, height = cols * 2, rows * 4
        scale = min((width / 2 - 4) / span_x,
                    (height / 2 - 3) / (span_y * 0.5)) * 0.5
        pixels = [(width / 2 + east * scale, height / 2 - north * 0.5 * scale)
                  for east, north in offsets]
        canvas = BrailleCanvas(cols, rows)
        for (x0, y0), (x1, y1) in zip(pixels, pixels[1:]):
            canvas.line(x0, y0, x1, y1, style="bright_cyan")
        styles = {"origin": "bright_green", "hop": "yellow", "me": "bright_cyan"}
        for (x, y), (_, _, label, role) in zip(pixels, points):
            style = styles.get(role, "white")
            canvas.blob(x, y, size=2, style=style)
            col, row = int(x / 2) + 1, int(y / 4)
            if canvas.label_fits(col, row, len(label[:10])):
                canvas.label(col, row, label[:10], style=style)
        box.update(canvas.render())

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "route-hops":
            self._render_prefix(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "route-hops":
            event.stop()
            self._render_prefix(event.cursor_row)

    def _render_prefix(self, row: int) -> None:
        if self.trace_rows:
            if not 0 <= row < len(self.trace_rows):
                return
            direction, node_id, snr = self.trace_rows[row]
            node = self.state.nodes.get(node_id)
            label = node.name if node is not None else node_id
            signal = f" at {snr:+.1f}dB" if snr is not None else ""
            self.query_one("#route-prefix", Static).update(Text(
                f" {direction} hop: {label} ({node_id}){signal}", style="bright_white"))
            return
        analysis = self.analysis
        if analysis is None or not (0 <= row < len(analysis.hops)):
            return
        hop = analysis.hops[row]
        prefix = hop.byte.casefold()
        candidates = [n for n in self.state.nodes.values()
                      if n.node_id.lstrip("!").casefold().startswith(prefix)]
        out = Text()
        out.append(f" prefix 0x{hop.byte}: ", style="bold grey62")
        if not candidates:
            out.append("no known nodes", style="red")
        else:
            for index, node in enumerate(candidates[:5]):
                if index:
                    out.append(" | ", style="grey42")
                out.append(node.long_name or node.node_id, style="bright_white")
                if node.role:
                    out.append(f" ({node.role})", style="grey54")
            if len(candidates) > 5:
                out.append(f" +{len(candidates) - 5} more", style="grey42")
        out.append(f"   {time.strftime('%H:%M:%S', time.localtime(self.packet.ts))}"
                   if self.packet else "", style="grey42")
        self.query_one("#route-prefix", Static).update(out)

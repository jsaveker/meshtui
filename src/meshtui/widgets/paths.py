"""Paths explorer - how packets actually traveled the mesh to reach us.

Left: every observed journey, newest first. Right: the selected one drawn on
a braille canvas - origin, each resolving repeater, and our radio joined in
travel order - with a hop-by-hop breakdown underneath. The header aggregates
what the whole dataset says: who carries the traffic and how far routes
wander compared to the straight line.
"""

from __future__ import annotations

import time
from collections import Counter

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ..geo import km_offsets
from ..pathcalc import (KM_TO_MI, PathAnalysis, PathObservation, analyze,
                        geojson_url, route_geojson)
from ..state import MeshState
from .canvas import BrailleCanvas
from .mesh_map import snr_style
from .nodes import fmt_age

ROW_LIMIT = 400
ROLE_STYLES = {"origin": "bright_green", "hop": "yellow", "me": "bright_cyan"}


def _mi(km: float | None) -> str:
    if km is None:
        return "-"
    miles = km * KM_TO_MI
    return f"{miles:.0f}" if miles >= 100 else f"{miles:.1f}"


class PathScreen(Screen[None]):
    """Explore persisted path observations."""

    BINDINGS = [
        Binding("escape", "close", "back"),
        Binding("ctrl+r", "reload", "reload", show=False),
    ]

    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state
        self._rows: list[PathObservation] = []
        self._count_rendered = -1

    def compose(self) -> ComposeResult:
        yield Static(id="paths-status")
        with Horizontal(id="paths-main"):
            yield DataTable(id="paths-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="paths-side"):
                yield Static(id="paths-canvas")
                yield Static(id="paths-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#paths-table", DataTable)
        table.border_title = "observed paths"
        table.add_column("age", key="age", width=4)
        table.add_column("kind", key="kind", width=7)
        table.add_column("origin", key="origin", width=20)
        table.add_column("hops", key="hops", width=4)
        table.add_column("snr", key="snr", width=6)
        table.add_column("route", key="route", width=6)
        table.add_column("direct", key="direct", width=6)
        table.add_column("x", key="stretch", width=5)
        self.query_one("#paths-canvas", Static).border_title = "route"
        self._reload()
        self.set_interval(3.0, self._reload)
        table.focus()

    # -------------------------------------------------------------- data

    def _reload(self) -> None:
        observations = self.state.paths
        if len(observations) == self._count_rendered:
            return
        self._count_rendered = len(observations)
        self._rows = list(reversed(observations))[:ROW_LIMIT]

        table = self.query_one("#paths-table", DataTable)
        keep = table.cursor_row
        table.clear()
        now = time.time()
        for obs in self._rows:
            analysis = analyze(self.state, obs)
            origin = obs.origin_name or obs.origin_id or "?"
            stretch = analysis.stretch
            table.add_row(
                fmt_age(now - obs.ts),
                Text(obs.kind, style="cyan" if obs.kind == "advert" else "bright_white"),
                Text(origin[:20], style="grey70"),
                Text(str(obs.hops), style="bright_white"),
                Text(f"{obs.snr:+.1f}" if obs.snr is not None else "-",
                     style=snr_style(obs.snr)),
                Text(_mi(analysis.route_km), style="yellow"),
                Text(_mi(analysis.direct_km), style="green"),
                Text(f"{stretch:.1f}" if stretch else "-",
                     style="red" if stretch and stretch > 2 else "grey70"),
            )
        if self._rows:
            table.move_cursor(row=min(max(keep, 0), len(self._rows) - 1))
        self._render_status()
        self._render_selected()

    def action_reload(self) -> None:
        self._count_rendered = -1
        self._reload()

    def action_close(self) -> None:
        self.dismiss()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._render_selected()

    def on_resize(self, event) -> None:
        # The canvas draws at whatever size its box has; redraw when it changes.
        self._render_selected()

    # ------------------------------------------------------------ summary

    def _render_status(self) -> None:
        observations = self.state.paths
        origins = {o.origin_name or o.origin_id for o in observations
                   if o.origin_name or o.origin_id}
        byte_use = Counter(b for o in observations for b in o.hop_bytes())
        stretches = []
        for obs in observations[-200:]:
            stretch = analyze(self.state, obs).stretch
            if stretch:
                stretches.append(stretch)
        stretches.sort()
        busiest = ""
        if byte_use:
            byte, count = byte_use.most_common(1)[0]
            names = self.state.resolve_relay(int(byte, 16))
            label = names[0].long_name if names else f"0x{byte}"
            busiest = f"   busiest hop: {label} ({count} paths)"
        median = f"   median stretch: x{stretches[len(stretches) // 2]:.1f}" if stretches else ""
        self.query_one("#paths-status", Static).update(Text(
            f" paths  -  {len(observations)} observations from "
            f"{len(origins)} origins{median}{busiest}   esc to close",
            style="grey62"))

    # ------------------------------------------------------------- detail

    def _selected(self) -> PathObservation | None:
        table = self.query_one("#paths-table", DataTable)
        if not self._rows or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self._rows):
            return None
        return self._rows[table.cursor_row]

    def _render_selected(self) -> None:
        obs = self._selected()
        canvas_box = self.query_one("#paths-canvas", Static)
        detail = self.query_one("#paths-detail", Static)
        if obs is None:
            canvas_box.update(Text("no path observations yet - they accumulate as\n"
                                   "adverts and channel messages are heard",
                                   style="grey42"))
            detail.update(Text(""))
            return
        analysis = analyze(self.state, obs)
        canvas_box.update(self._draw(analysis))
        detail.update(self._describe(obs, analysis))

    def _draw(self, analysis: PathAnalysis) -> Text:
        points = analysis.points()
        box = self.query_one("#paths-canvas", Static).size
        cols = max(30, (box.width or 60) - 4)
        rows = max(8, (box.height or 18) - 1)
        if len(points) < 2:
            return Text("not enough positioned nodes on this path to draw it",
                        style="grey42")
        # Project around the centroid so east/north keep their aspect.
        clat = sum(p[0] for p in points) / len(points)
        clon = sum(p[1] for p in points) / len(points)
        offsets = [km_offsets(p[0], p[1], clat, clon) for p in points]
        span_x = max(abs(e) for e, _ in offsets) or 1.0
        span_y = max(abs(n) for _, n in offsets) or 1.0
        width, height = cols * 2, rows * 4
        # Braille cells are ~twice as tall as wide; 0.5 keeps distances honest.
        scale = min((width / 2 - 6) / span_x, (height / 2 - 4) / (span_y * 0.5)) * 0.5
        canvas = BrailleCanvas(cols, rows)
        pixels = [(width / 2 + e * scale, height / 2 - n * 0.5 * scale)
                  for e, n in offsets]
        for (x0, y0), (x1, y1) in zip(pixels, pixels[1:]):
            canvas.line(x0, y0, x1, y1, style="grey54")
        for (x, y), (_, _, label, role) in zip(pixels, points):
            style = ROLE_STYLES.get(role, "white")
            canvas.blob(x, y, size=2, style=style)
            col, row = int(x / 2) + 1, int(y / 4)
            if canvas.label_fits(col, row, len(label[:14])):
                canvas.label(col, row, label[:14], style=style)
        return canvas.render()

    def _describe(self, obs: PathObservation, analysis: PathAnalysis) -> Text:
        out = Text()
        origin = obs.origin_name or obs.origin_id or "unknown origin"
        out.append(f" {origin}", style="bold bright_green")
        out.append("  ->  me", style="grey62")
        if obs.channel is not None:
            out.append(f"   {self.state.channel_name(obs.channel)}", style="cyan")
        out.append("\n")
        if obs.snr is not None:
            out.append(f" heard at {obs.snr:+.1f}dB", style=snr_style(obs.snr))
            if obs.rssi is not None:
                out.append(f" / {obs.rssi}dBm", style="grey62")
            out.append("  (final hop's link)\n", style="grey42")
        for index, hop in enumerate(analysis.hops, start=1):
            out.append(f"   {index}. ", style="grey42")
            if hop.node is None:
                out.append(f"0x{hop.byte}", style="red")
                out.append("  (unknown repeater)\n", style="grey42")
                continue
            out.append(hop.label, style="yellow")
            if not hop.node.has_position:
                out.append("  (no position)", style="grey42")
            out.append("\n")
        if analysis.route_km:
            out.append(f" route ~{_mi(analysis.route_km)}mi", style="yellow")
        if analysis.direct_km:
            out.append(f"   direct ~{_mi(analysis.direct_km)}mi", style="green")
        if analysis.stretch:
            out.append(f"   x{analysis.stretch:.1f} the straight line",
                       style="red" if analysis.stretch > 2 else "grey70")
        geojson = route_geojson(analysis)
        if geojson is not None:
            # An OSC-8 hyperlink: the route data travels inside the URL
            # fragment, so clicking it involves no server of ours.
            out.append("\n\n")
            out.append(" open route in geojson.io",
                       style=f"bright_blue underline link {geojson_url(geojson)}")
        return out

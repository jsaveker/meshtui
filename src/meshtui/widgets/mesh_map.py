"""Full-screen braille map of the mesh."""

from __future__ import annotations

import math

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer, Static

from ..geo import fmt_km, haversine_km, km_offsets
from ..model import Node
from ..state import MeshState
from .canvas import BrailleCanvas

RING_STEPS = (0.25, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500)
COLOR_MODES = ("snr", "hops", "age")
MAX_LABELS = 30


def snr_style(snr: float | None) -> str:
    if snr is None:
        return "grey42"
    if snr >= 0:
        return "bright_green"
    if snr >= -8:
        return "yellow"
    if snr >= -15:
        return "dark_orange"
    return "red"


def hops_style(hops: int | None) -> str:
    if hops is None:
        return "grey42"
    return ("bright_green", "cyan", "bright_blue", "blue", "magenta")[min(hops, 4)]


def age_style(node: Node) -> str:
    age = node.age
    if age is None:
        return "grey42"
    if age < 900:
        return "bright_green"
    if age < 3600:
        return "yellow"
    if age < 21600:
        return "dark_orange"
    return "grey42"


class MapView(Widget):
    """Draws the mesh onto a braille canvas sized to the widget."""

    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state
        self.span_km = 0.0          # 0 means "fit to the mesh on next draw"
        self.center: tuple[float, float] | None = None
        self.color_mode = 0
        self.show_rings = True
        self.show_links = True
        self.show_tracks = True
        self.status = ""
        # Canvas dot dimensions from the last render; fit() needs them to know
        # the aspect ratio it has to fit into.
        self._dims = (200, 100)

    # ------------------------------------------------------------- framing

    def _positioned(self) -> list[Node]:
        return [n for n in self.state.nodes.values() if n.has_position]

    def _origin(self) -> tuple[float, float] | None:
        """Where the view is centred: your node if it has a fix, else centroid."""
        if self.center is not None:
            return self.center
        me = self.state.nodes.get(self.state.my_node_id or "")
        if me is not None and me.has_position:
            return (me.lat, me.lon)  # type: ignore[return-value]
        nodes = self._positioned()
        if not nodes:
            return None
        return (
            sum(n.lat for n in nodes) / len(nodes),  # type: ignore[misc]
            sum(n.lon for n in nodes) / len(nodes),  # type: ignore[misc]
        )

    def fit(self) -> None:
        """Choose a span containing every positioned node on BOTH axes.

        Fitting on radial distance alone squashes meshes that are taller than
        the canvas aspect ratio, so derive the span from the east/north extents
        and the canvas shape instead.
        """
        origin = self._origin()
        nodes = self._positioned()
        if origin is None or not nodes:
            self.span_km = 10.0
            return
        offsets = [km_offsets(n.lat, n.lon, origin[0], origin[1]) for n in nodes]  # type: ignore[arg-type]
        max_e = max((abs(e) for e, _ in offsets), default=0.1)
        max_n = max((abs(n) for _, n in offsets), default=0.1)
        width, height = self._dims
        need_x = 2 * max(max_e, 0.05) * 1.15
        need_y = 2 * max(max_n, 0.05) * 1.15 * (width / max(1, height))
        self.span_km = max(0.3, need_x, need_y)
        self.refresh()

    def zoom(self, factor: float) -> None:
        if self.span_km <= 0:
            self.fit()
        self.span_km = max(0.1, min(5000.0, self.span_km * factor))
        self.refresh()

    def pan(self, dx: float, dy: float) -> None:
        origin = self._origin()
        if origin is None:
            return
        step = self.span_km * 0.2
        lat = origin[0] + (dy * step) / 111.32
        lon = origin[1] + (dx * step) / (111.32 * max(0.1, math.cos(math.radians(origin[0]))))
        self.center = (lat, lon)
        self.refresh()

    def recenter(self) -> None:
        self.center = None
        self.span_km = 0.0   # refit on the next render, at real canvas size
        self.refresh()

    def cycle_color(self) -> str:
        self.color_mode = (self.color_mode + 1) % len(COLOR_MODES)
        self.refresh()
        return COLOR_MODES[self.color_mode]

    def _style_for(self, node: Node) -> str:
        mode = COLOR_MODES[self.color_mode]
        if mode == "snr":
            return snr_style(node.snr)
        if mode == "hops":
            return hops_style(node.hops)
        return age_style(node)

    # ------------------------------------------------------------ drawing

    def render(self) -> Text:
        cols = max(20, self.size.width)
        rows = max(8, self.size.height)
        canvas = BrailleCanvas(cols, rows)
        self._dims = (canvas.width, canvas.height)

        origin = self._origin()
        nodes = self._positioned()
        if origin is None or not nodes:
            msg = "no nodes with a GPS position yet"
            canvas.label(max(0, (cols - len(msg)) // 2), rows // 2, msg, "grey54")
            self.status = "map - waiting for position data"
            return canvas.render()

        if self.span_km <= 0:
            self.fit()

        # Pixels per km, chosen so `span_km` spans the full canvas width.
        # Braille dots are ~twice as tall as they are wide in most fonts, which
        # the 2x4 cell geometry already compensates for.
        px_per_km = canvas.width / self.span_km
        cx, cy = canvas.width / 2, canvas.height / 2

        def to_px(node: Node) -> tuple[float, float]:
            east, north = km_offsets(node.lat, node.lon, origin[0], origin[1])  # type: ignore[arg-type]
            return cx + east * px_per_km, cy - north * px_per_km

        # Distance rings, drawn first so node marks sit on top.
        if self.show_rings:
            drawn = 0
            for km in RING_STEPS:
                r = km * px_per_km
                if r < 6 or r > max(canvas.width, canvas.height):
                    continue
                canvas.dashed_circle(cx, cy, r, "grey30")
                tag = fmt_km(km)
                col = int((cx + r) / 2) + 1
                row = int(cy / 4)
                if canvas.label_fits(col, row, len(tag) + 1):
                    canvas.label(col, row, tag, "grey35")
                drawn += 1
                if drawn >= 4:
                    break

        me = self.state.nodes.get(self.state.my_node_id or "")
        me_px = to_px(me) if (me is not None and me.has_position) else (cx, cy)

        # Links from your node to everything you hear directly.
        if self.show_links:
            for node in nodes:
                if node.is_self or node.hops not in (0, None):
                    continue
                if node.hops is None:
                    continue
                x, y = to_px(node)
                canvas.line(me_px[0], me_px[1], x, y, "grey27")

        # Movement trails, drawn under the node marks.
        if self.show_tracks:
            for node in nodes:
                if len(node.track) < 2:
                    continue
                points = [
                    (cx + km_offsets(lat, lon, origin[0], origin[1])[0] * px_per_km,
                     cy - km_offsets(lat, lon, origin[0], origin[1])[1] * px_per_km)
                    for lat, lon, _ in node.track
                ]
                for (x0, y0), (x1, y1) in zip(points, points[1:]):
                    canvas.line(x0, y0, x1, y1, "grey35")

        # Node marks. Size encodes hop distance, colour encodes the active mode.
        ranked = sorted(
            nodes,
            key=lambda n: (not n.is_self, n.hops if n.hops is not None else 9, -(n.snr or -99)),
        )
        placed: list[tuple[Node, float, float]] = []
        for node in ranked:
            x, y = to_px(node)
            if not (0 <= x < canvas.width and 0 <= y < canvas.height):
                continue
            if node.is_self:
                canvas.blob(x, y, 4, "bold bright_cyan")
            elif node.hops == 0:
                canvas.blob(x, y, 3, self._style_for(node))
            elif (node.hops or 0) <= 2:
                canvas.blob(x, y, 2, self._style_for(node))
            else:
                canvas.plot(x, y, self._style_for(node))

            # A short spur in the direction of travel for anything moving.
            if node.moving and node.heading_deg is not None:
                theta = math.radians(node.heading_deg)
                length = 6 + min(10, (node.speed_mps or 0))
                canvas.line(x, y,
                            x + math.sin(theta) * length,
                            y - math.cos(theta) * length,
                            "bold bright_yellow")
            placed.append((node, x, y))

        # Labels, best-first, skipping any that would collide.
        labelled = 0
        for node, x, y in placed:
            if labelled >= MAX_LABELS:
                break
            tag = node.label[:6]
            col, row = int(x / 2) + 2, int(y / 4)
            if not canvas.label_fits(col, row, len(tag) + 1):
                col = int(x / 2) - len(tag) - 2
                if not canvas.label_fits(col, row, len(tag) + 1):
                    continue
            canvas.label(col, row, tag,
                         "bold bright_cyan" if node.is_self else self._style_for(node))
            labelled += 1

        far = max(haversine_km(origin[0], origin[1], n.lat, n.lon) for n in nodes)  # type: ignore[arg-type]
        self.status = (
            f"{len(nodes)} positioned / {len(self.state.nodes)} nodes   "
            f"span {fmt_km(self.span_km)}   furthest {fmt_km(far)}   "
            f"colour: {COLOR_MODES[self.color_mode]}"
            + (f"   {moving} moving" if (moving := sum(1 for n in nodes if n.moving)) else "")
            + ("" if self.center is None else "   [panned]")
        )
        return canvas.render()


class MapScreen(Screen[None]):
    BINDINGS = [
        Binding("escape,m,q", "close", "back"),
        Binding("up,k", "pan_up", "pan"),
        Binding("down,j", "pan_down", "pan", show=False),
        Binding("left,h", "pan_left", "pan", show=False),
        Binding("right,l", "pan_right", "pan", show=False),
        Binding("plus,equals_sign,equal", "zoom_in", "zoom in"),
        Binding("minus,underscore", "zoom_out", "zoom out"),
        Binding("f", "fit", "fit"),
        Binding("c", "color", "colour"),
        Binding("r", "rings", "rings"),
        Binding("i", "links", "links"),
        Binding("t", "tracks", "trails"),
    ]

    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state
        self.view = MapView(state)

    def compose(self) -> ComposeResult:
        yield Static(id="map-status")
        yield self.view
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        self.view.refresh()
        self.query_one("#map-status", Static).update(
            Text(f" {self.view.status}", style="grey62")
        )

    def action_close(self) -> None:
        self.dismiss(None)

    def action_pan_up(self) -> None:
        self.view.pan(0, 1); self._refresh()

    def action_pan_down(self) -> None:
        self.view.pan(0, -1); self._refresh()

    def action_pan_left(self) -> None:
        self.view.pan(-1, 0); self._refresh()

    def action_pan_right(self) -> None:
        self.view.pan(1, 0); self._refresh()

    def action_zoom_in(self) -> None:
        self.view.zoom(0.6); self._refresh()

    def action_zoom_out(self) -> None:
        self.view.zoom(1.7); self._refresh()

    def action_fit(self) -> None:
        self.view.recenter(); self._refresh()

    def action_color(self) -> None:
        self.view.cycle_color(); self._refresh()

    def action_rings(self) -> None:
        self.view.show_rings = not self.view.show_rings
        self.view.refresh(); self._refresh()

    def action_links(self) -> None:
        self.view.show_links = not self.view.show_links
        self.view.refresh(); self._refresh()

    def action_tracks(self) -> None:
        self.view.show_tracks = not self.view.show_tracks
        self.view.refresh(); self._refresh()

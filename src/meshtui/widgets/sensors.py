"""Environment and air-quality readings from sensor nodes on the mesh.

Meshtastic's environment telemetry carries far more than temperature -
humidity, pressure, lux, wind, rainfall, soil moisture, radiation - and the
air-quality variant adds particulates and CO2. Any node running a sensor
broadcasts these to the whole mesh, so this is a free weather network.
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

from ..model import Node
from ..state import MeshState
from .stats import fmt_duration

# (protobuf key, column heading, format, comfortable range for colouring)
READINGS: list[tuple[str, str, str, tuple[float, float] | None]] = [
    ("temperature", "temp", "{:.1f}C", (-10, 40)),
    ("relativeHumidity", "humid", "{:.0f}%", (20, 80)),
    ("barometricPressure", "press", "{:.0f}hPa", (980, 1040)),
    ("lux", "lux", "{:.0f}", None),
    ("iaq", "iaq", "{:.0f}", (0, 100)),
    ("windSpeed", "wind", "{:.1f}m/s", (0, 12)),
    ("windDirection", "dir", "{:.0f}d", None),
    ("rainfall1H", "rain1h", "{:.1f}mm", None),
    ("rainfall24H", "rain24h", "{:.1f}mm", None),
    ("soilMoisture", "soil", "{:.0f}%", None),
    ("soilTemperature", "soilT", "{:.1f}C", None),
    ("radiation", "rad", "{:.2f}uSv", (0, 0.5)),
    ("voltage", "volt", "{:.2f}V", None),
    ("current", "curr", "{:.0f}mA", None),
    ("distance", "dist", "{:.0f}mm", None),
    ("weight", "weight", "{:.1f}kg", None),
    ("gasResistance", "gas", "{:.0f}", None),
    ("pm25Standard", "pm2.5", "{:.0f}", (0, 35)),
    ("pm10Standard", "pm10", "{:.0f}", (0, 54)),
    ("pm100Standard", "pm100", "{:.0f}", None),
    ("co2", "co2", "{:.0f}ppm", (400, 1000)),
]


def value_style(key: str, value: float, band: tuple[float, float] | None) -> str:
    if band is None:
        return "bright_white"
    low, high = band
    if value < low or value > high:
        return "dark_orange"
    return "bright_green"


class SensorView(Static):
    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state

    def render_report(self) -> None:
        self.update(Group(*self._sections()))

    def _sections(self) -> list[object]:
        nodes = self.state.sensor_nodes()
        parts: list[object] = []
        if not nodes:
            parts.append(Text("No environment telemetry received yet.", style="grey54"))
            parts.append(Text(""))
            parts.append(Text(
                "Nodes with a BME280, SHT31, particulate or similar sensor broadcast "
                "readings\nto the whole mesh. They will appear here as they arrive.",
                style="grey42"))
            return parts

        # Only show columns some node is actually reporting.
        present = [r for r in READINGS if any(r[0] in n.env for n in nodes)]
        parts.append(Text(f"{len(nodes)} sensor node(s) reporting "
                          f"{len(present)} distinct measurement(s)",
                          style="bold bright_cyan"))
        parts.append(self._table(nodes, present))
        parts.append(Text(""))
        parts.append(self._summary(nodes, present))
        return parts

    def _table(self, nodes: list[Node], present: list) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column(justify="left", width=18)
        for _ in present:
            table.add_column(justify="right", width=9)
        table.add_column(justify="right", width=8)

        header = [Text("node", style="grey42")]
        header += [Text(head, style="grey42") for _, head, _, _ in present]
        header.append(Text("age", style="grey42"))
        table.add_row(*header)

        now = time.time()
        for node in nodes:
            row = [Text(f"{node.label:<5} {(node.long_name or node.node_id)[:11]}",
                        style="bright_white")]
            for key, _, fmt, band in present:
                value = node.env.get(key)
                if value is None:
                    row.append(Text("-", style="grey30"))
                else:
                    row.append(Text(fmt.format(value), style=value_style(key, value, band)))
            age = now - (node.env_ts or now)
            row.append(Text(fmt_duration(age), style="green" if age < 3600 else "grey42"))
            table.add_row(*row)
        return table

    def _summary(self, nodes: list[Node], present: list) -> Table:
        """Min/mean/max across the mesh - a crude but useful regional picture."""
        table = Table.grid(padding=(0, 2))
        for width in (18, 12, 12, 12, 8):
            table.add_column(justify="right", width=width)
        table.add_row(*[Text(h, style="grey42") for h in
                        ("measurement", "min", "mean", "max", "nodes")])
        for key, head, fmt, band in present:
            values = [n.env[key] for n in nodes if key in n.env]
            if len(values) < 2:
                continue
            table.add_row(
                Text(head, style="bright_white"),
                Text(fmt.format(min(values)), style="cyan"),
                Text(fmt.format(sum(values) / len(values)), style="bright_white"),
                Text(fmt.format(max(values)), style="dark_orange"),
                Text(str(len(values)), style="grey70"),
            )
        return table


class SensorScreen(Screen[None]):
    BINDINGS = [
        Binding("escape,w,q", "close", "back"),
        Binding("up,k", "scroll_up", "scroll", show=False),
        Binding("down,j", "scroll_down", "scroll", show=False),
    ]

    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state
        self.view = SensorView(state)

    def compose(self) -> ComposeResult:
        yield Static(
            Text(" sensors  -  environment and air quality broadcast across the mesh",
                 style="grey62"),
            id="sensor-status",
        )
        with VerticalScroll(id="sensor-box"):
            yield self.view
        yield Footer()

    def on_mount(self) -> None:
        self.view.render_report()
        self.set_interval(5.0, self.view.render_report)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_scroll_up(self) -> None:
        self.query_one("#sensor-box", VerticalScroll).scroll_up()

    def action_scroll_down(self) -> None:
        self.query_one("#sensor-box", VerticalScroll).scroll_down()

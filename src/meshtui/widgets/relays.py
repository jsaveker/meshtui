"""Relay dependency and mesh health.

Every packet carries `relay_node` - the low byte of the node number that last
forwarded it. That single byte is the only routing evidence on the wire, and
aggregated over a capture it reveals which nodes your view of the mesh actually
depends on. On a real mesh the answer is usually "two of them".

The lower half reports `localStats`, which nodes broadcast about themselves:
packet counters, duplicate and relay-cancel rates, free heap and noise floor.
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

BAR_WIDTH = 24


def bar(fraction: float, width: int = BAR_WIDTH) -> Text:
    """Solid/half/empty blocks so small shares stay visible."""
    filled = fraction * width
    whole = int(filled)
    out = Text("█" * whole, style="cyan")
    if filled - whole >= 0.5 and whole < width:
        out.append("░", style="cyan")
        whole += 1
    out.append(" " * max(0, width - whole))
    return out


def relay_label(state: MeshState, byte: int) -> tuple[Text, bool]:
    """Name a relay byte, flagging when several nodes could be responsible."""
    candidates = state.resolve_relay(byte)
    if not candidates:
        return (Text(f"0x{byte:02x}  (unknown node)", style="grey42"), False)
    best = candidates[0]
    text = Text()
    text.append(f"{best.label:<5}", style="bold bright_white")
    text.append(" ")
    text.append((best.long_name or best.node_id)[:22], style="grey70")
    if len(candidates) > 1:
        text.append(f"  ?x{len(candidates)}", style="dark_orange")
        return (text, True)
    return (text, False)


class RelayView(Static):
    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state

    def render_report(self) -> None:
        self.update(Group(*self._sections()))

    def _sections(self) -> list[object]:
        parts: list[object] = []
        parts.append(Text("who relays your traffic", style="bold bright_cyan"))
        parts.append(self._relay_table())
        parts.append(Text(""))
        parts.append(self._verdict())
        parts.append(Text(""))
        parts.append(Text("mesh health, as reported by nodes themselves (localStats)",
                          style="bold bright_cyan"))
        parts.append(self._health_table())
        parts.append(Text(""))
        parts.append(Text("busiest origin -> relay paths", style="bold bright_cyan"))
        parts.append(self._edge_table())
        return parts

    # -------------------------------------------------------------- relays

    def _relay_table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        for justify, width in (("left", 30), ("left", BAR_WIDTH), ("right", 7),
                               ("right", 8), ("right", 8), ("left", 10)):
            table.add_column(justify=justify, width=width)
        table.add_row(*[Text(h, style="grey42") for h in
                        ("relay", "share", "", "packets", "origins", "avg snr")])

        share = self.state.relay_share()
        if not share:
            table.add_row(Text("no relayed packets seen yet", style="grey42"),
                          *[Text("") for _ in range(5)])
            return table

        for relay, fraction in share:
            label, _ = relay_label(self.state, relay.byte)
            snr = relay.avg_snr
            table.add_row(
                label,
                bar(fraction),
                Text(f"{fraction * 100:.1f}%", style="bright_white"),
                Text(str(relay.packets), style="grey70"),
                Text(str(len(relay.origins)), style="grey70"),
                Text(f"{snr:+.1f}dB" if snr is not None else "-", style="yellow"),
            )
        return table

    def _verdict(self) -> Text:
        share = self.state.relay_share()
        out = Text()
        if not share:
            return out
        top2 = sum(f for _, f in share[:2])
        if len(share) >= 2 and top2 >= 0.8:
            out.append("! ", style="bold red")
            out.append(f"2 relays carry {top2 * 100:.1f}% of your inbound traffic.\n",
                       style="bold red")
            first, first_share = share[0]
            name, _ = relay_label(self.state, first.byte)
            out.append("  losing ", style="grey70")
            out.append(name.plain.split("  ")[0].strip(), style="bold bright_white")
            out.append(f" alone would cost you about {first_share * 100:.0f}% "
                       f"of what you hear.\n", style="grey70")
        elif len(share) >= 3:
            out.append("+ ", style="bold green")
            out.append(f"traffic is spread across {len(share)} relays - "
                       f"no single point of failure.\n", style="green")
        if any(len(self.state.resolve_relay(r.byte)) > 1 for r, _ in share):
            out.append("  rows marked ?xN are ambiguous: only the low byte of the "
                       "relay's node number is sent,\n  so several known nodes match.",
                       style="grey54")
        return out

    # -------------------------------------------------------------- health

    def _health_table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        for justify, width in (("left", 20), ("right", 9), ("right", 9), ("right", 7),
                               ("right", 7), ("right", 9), ("right", 9), ("right", 8)):
            table.add_column(justify=justify, width=width)
        table.add_row(*[Text(h, style="grey42") for h in
                        ("node", "tx", "rx", "bad", "dupe", "relayed", "noise", "heap")])

        nodes = self.state.stats_nodes()
        if not nodes:
            table.add_row(Text("no localStats received yet", style="grey42"),
                          *[Text("") for _ in range(7)])
            return table

        for node in nodes[:12]:
            st = node.local_stats
            rx = st.get("numPacketsRx", 0)
            bad = st.get("numPacketsRxBad", 0)
            dupe = st.get("numRxDupe", 0)
            bad_style = "red" if rx and bad / max(rx, 1) > 0.05 else "grey70"
            dupe_style = "dark_orange" if rx and dupe / max(rx, 1) > 0.3 else "grey70"
            heap_free = st.get("heapFreeBytes")
            heap_total = st.get("heapTotalBytes")
            if heap_free and heap_total:
                frac = heap_free / heap_total
                heap = Text(f"{frac * 100:.0f}%",
                            style="red" if frac < 0.15 else "grey70")
            else:
                heap = Text("-", style="grey42")
            noise = st.get("noiseFloor")
            table.add_row(
                Text(node.label + ("*" if node.is_self else ""),
                     style="bold bright_cyan" if node.is_self else "bright_white"),
                Text(f"{st.get('numPacketsTx', 0):.0f}", style="grey70"),
                Text(f"{rx:.0f}", style="grey70"),
                Text(f"{bad:.0f}", style=bad_style),
                Text(f"{dupe:.0f}", style=dupe_style),
                Text(f"{st.get('numTxRelay', 0):.0f}", style="cyan"),
                Text(f"{noise:.0f}dBm" if noise is not None else "-", style="yellow"),
                heap,
            )
        return table

    def _edge_table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        for justify, width in (("left", 26), ("left", 4), ("left", 30), ("right", 8)):
            table.add_column(justify=justify, width=width)
        table.add_row(*[Text(h, style="grey42")
                        for h in ("origin", "", "reached you via", "packets")])

        edges = self.state.relay_edges.most_common(12)
        if not edges:
            table.add_row(Text("nothing yet", style="grey42"),
                          *[Text("") for _ in range(3)])
            return table
        for (origin, byte), count in edges:
            node = self.state.nodes.get(origin)
            label, _ = relay_label(self.state, byte)
            table.add_row(
                Text((node.name if node else origin)[:26],
                     style="bright_white" if node else "grey42"),
                Text("->", style="grey42"),
                label,
                Text(str(count), style="grey70"),
            )
        return table


class RelayScreen(Screen[None]):
    BINDINGS = [
        Binding("escape,r,q", "close", "back"),
        Binding("up,k", "scroll_up", "scroll", show=False),
        Binding("down,j", "scroll_down", "scroll", show=False),
    ]

    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state
        self.view = RelayView(state)

    def compose(self) -> ComposeResult:
        yield Static(
            Text(" relays and mesh health  -  what does your view of the mesh depend on?",
                 style="grey62"),
            id="relay-status",
        )
        with VerticalScroll(id="relay-box"):
            yield self.view
        yield Footer()

    def on_mount(self) -> None:
        self.view.render_report()
        self.set_interval(3.0, self.view.render_report)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_scroll_up(self) -> None:
        self.query_one("#relay-box", VerticalScroll).scroll_up()

    def action_scroll_down(self) -> None:
        self.query_one("#relay-box", VerticalScroll).scroll_down()

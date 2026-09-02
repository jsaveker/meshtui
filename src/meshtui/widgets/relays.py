"""Relay dependency and mesh health.

Meshtastic reports the low byte of the node number that last forwarded a
packet; MeshCore reports a 1- to 4-byte public-key prefix.  Aggregated over a
capture, that evidence reveals which nodes your view of the mesh depends on.
Short hashes can collide and must remain unnamed when they do.

The lower half reports `localStats`, which nodes broadcast about themselves:
packet counters, duplicate and relay-cancel rates, free heap and noise floor.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Iterable

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ..model import SPARK_WIDTH, Node, normalize_relay_hash
from ..pathcalc import plausible_relays
from ..state import MeshState, RelayStat
from .nodes import snr_spark
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


def _relay_resolution(state: MeshState, relay_hash: str | int) -> tuple[list[Node], bool]:
    return plausible_relays(state.resolve_relay(relay_hash))


def relay_label(state: MeshState, relay_hash: str | int) -> tuple[Text, bool]:
    """Name an exact relay token, but never guess across a collision."""
    token = normalize_relay_hash(relay_hash)
    if token is None:
        return (Text("invalid relay hash", style="red"), False)
    pool, ambiguous = _relay_resolution(state, token)
    if not pool:
        return (Text(f"0x{token}  (unknown node)", style="grey42"), False)
    if ambiguous:
        noun = "repeater" if len(pool) == 1 else "repeaters"
        return (Text(f"0x{token}  ({len(pool)} possible {noun})",
                     style="dark_orange"), True)
    best = pool[0]
    text = Text()
    text.append(f"{best.label:<5}", style="bold bright_white")
    text.append(" ")
    text.append((best.long_name or best.node_id)[:22], style="grey70")
    return (text, False)


def _relay_group(state: MeshState, relay_hash: str) -> tuple[str, str]:
    """Group exact-width tokens that resolve confidently to the same node."""
    pool, ambiguous = _relay_resolution(state, relay_hash)
    if pool and not ambiguous:
        return ("node", pool[0].node_id)
    return ("hash", relay_hash)


def _merge_relay_stats(rows: Iterable[RelayStat]) -> RelayStat:
    members = list(rows)
    token = max((row.key for row in members), key=lambda value: (len(value), value))
    newest = max(members, key=lambda row: row.last_seen)
    merged = RelayStat(
        byte=int(token[:2], 16), relay_hash=token,
        packets=sum(row.packets for row in members),
        origins=set().union(*(row.origins for row in members)),
        first_seen=min(row.first_seen for row in members),
        last_seen=newest.last_seen,
        snr_sum=sum(row.snr_sum for row in members),
        snr_n=sum(row.snr_n for row in members),
        last_snr=newest.last_snr,
    )
    for row in sorted(members, key=lambda item: item.last_seen):
        merged.snr_history.extend(row.snr_history)
    return merged


def display_relay_share(state: MeshState) -> list[tuple[RelayStat, float]]:
    """Coalesce exact aliases for display while retaining raw stored buckets."""
    groups: dict[tuple[str, str], list[RelayStat]] = defaultdict(list)
    for relay in state.relays.values():
        groups[_relay_group(state, relay.key)].append(relay)
    merged = [_merge_relay_stats(rows) for rows in groups.values()]
    total = sum(relay.packets for relay in merged) or 1
    return sorted(((relay, relay.packets / total) for relay in merged),
                  key=lambda item: -item[1])


class RelayView(Static):
    def __init__(self, state: MeshState) -> None:
        super().__init__()
        self.state = state

    def render_report(self) -> None:
        self.update(Group(*self._sections()))

    def _sections(self) -> list[object]:
        parts: list[object] = []
        me = self.state.my_node_id
        who = self.state.node_name(me) if me else "this radio"
        parts.append(Text(f"who relays traffic to {who}", style="bold bright_cyan"))
        parts.append(Text("  signal and hop counts are relative to this radio's "
                          "position; another node sees a different mesh",
                          style="grey42"))
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
        parts.append(Text(""))
        parts.append(Text("logged-in repeater neighbour tables", style="bold bright_cyan"))
        parts.append(self._neighbour_table())
        return parts

    # -------------------------------------------------------------- relays

    def _relay_table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        for justify, width in (("left", 30), ("left", BAR_WIDTH), ("right", 7),
                               ("right", 8), ("right", 8), ("left", 9),
                               ("right", 8), ("left", SPARK_WIDTH)):
            table.add_column(justify=justify, width=width)
        table.add_row(*[Text(h, style="grey42") for h in
                        ("relay", "share", "", "packets", "origins", "avg snr",
                         "last", "trend")])

        share = display_relay_share(self.state)
        if not share:
            table.add_row(Text("no relayed packets seen yet", style="grey42"),
                          *[Text("") for _ in range(7)])
            return table

        for relay, fraction in share:
            label, _ = relay_label(self.state, relay.key)
            snr = relay.avg_snr
            table.add_row(
                label,
                bar(fraction),
                Text(f"{fraction * 100:.1f}%", style="bright_white"),
                Text(str(relay.packets), style="grey70"),
                Text(str(len(relay.origins)), style="grey70"),
                Text(f"{snr:+.1f}dB" if snr is not None else "-", style="yellow"),
                Text(f"{relay.last_snr:+.1f}" if relay.last_snr is not None else "-",
                     style="bright_white"),
                snr_spark(relay.snr_history),
            )
        return table

    def _verdict(self) -> Text:
        share = display_relay_share(self.state)
        out = Text()
        if not share:
            return out

        resolved: list[tuple[RelayStat, float, Node]] = []
        unresolved: list[tuple[RelayStat, float]] = []
        for relay, fraction in share:
            identity = _relay_group(self.state, relay.key)
            node = self.state.nodes.get(identity[1]) if identity[0] == "node" else None
            if node is None:
                unresolved.append((relay, fraction))
            else:
                resolved.append((relay, fraction, node))

        unresolved_share = sum(fraction for _, fraction in unresolved)
        if unresolved:
            out.append("! ", style="bold dark_orange")
            if not resolved:
                out.append(
                    f"{unresolved_share * 100:.1f}% of observed relay traffic is "
                    "unresolved; relay dependency cannot yet be determined.\n",
                    style="bold dark_orange",
                )
            elif len(resolved) == 1:
                _, fraction, node = resolved[0]
                out.append(f"{fraction * 100:.1f}% is confidently attributed to ",
                           style="bold dark_orange")
                out.append(node.name, style="bold bright_white")
                out.append(f"; {unresolved_share * 100:.1f}% remains unresolved.\n",
                           style="bold dark_orange")
                out.append(f"  {node.name} carries at least {fraction * 100:.0f}% "
                           "of what you hear.\n", style="grey70")
            else:
                resolved_share = sum(fraction for _, fraction, _ in resolved)
                out.append(
                    f"{resolved_share * 100:.1f}% is confidently attributed across "
                    f"{len(resolved)} relays; {unresolved_share * 100:.1f}% remains "
                    "unresolved.\n",
                    style="bold dark_orange",
                )
                _, fraction, node = resolved[0]
                out.append(f"  {node.name} alone carries at least "
                           f"{fraction * 100:.0f}% of what you hear.\n", style="grey70")
        elif len(resolved) == 1:
            _, fraction, node = resolved[0]
            out.append("! ", style="bold red")
            out.append(f"1 relay carries {fraction * 100:.1f}% of your inbound traffic.\n",
                       style="bold red")
            out.append("  losing ", style="grey70")
            out.append(node.name, style="bold bright_white")
            out.append(" would remove the only observed relay path.\n", style="grey70")
        else:
            top2 = sum(fraction for _, fraction, _ in resolved[:2])
            if len(resolved) >= 2 and top2 >= 0.8:
                out.append("! ", style="bold red")
                out.append(f"2 relays carry {top2 * 100:.1f}% of your inbound traffic.\n",
                           style="bold red")
                _, first_share, node = resolved[0]
                out.append("  losing ", style="grey70")
                out.append(node.name, style="bold bright_white")
                out.append(f" alone would cost you about {first_share * 100:.0f}% "
                           f"of what you hear.\n", style="grey70")
            elif len(resolved) >= 3:
                out.append("+ ", style="bold green")
                out.append(f"traffic is spread across {len(resolved)} relays - "
                           f"no single point of failure.\n", style="green")

        if unresolved:
            if self.state.protocol == "meshcore":
                explanation = ("  unresolved rows retain the on-wire hash: short "
                               "MeshCore hashes may match several repeaters, while "
                               "unknown hashes match none yet.")
            else:
                explanation = ("  unresolved rows retain the on-wire byte: several "
                               "Meshtastic nodes may share it, or no known node matches.")
            out.append(explanation, style="grey54")
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

        grouped: Counter[tuple[str, tuple[str, str]]] = Counter()
        representative: dict[tuple[str, tuple[str, str]], str] = {}
        for (origin, relay_hash), count in self.state.relay_edges.items():
            identity = _relay_group(self.state, relay_hash)
            key = (origin, identity)
            grouped[key] += count
            current = representative.get(key, "")
            if len(relay_hash) > len(current):
                representative[key] = relay_hash
        edges = grouped.most_common(12)
        if not edges:
            table.add_row(Text("nothing yet", style="grey42"),
                          *[Text("") for _ in range(3)])
            return table
        for (origin, identity), count in edges:
            node = self.state.nodes.get(origin)
            label, _ = relay_label(self.state, representative[(origin, identity)])
            table.add_row(
                Text((node.name if node else origin)[:26],
                     style="bright_white" if node else "grey42"),
                Text("->", style="grey42"),
                label,
                Text(str(count), style="grey70"),
            )
        return table

    def _neighbour_table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column(width=24)
        table.add_column(width=4)
        table.add_column(width=24)
        table.add_column(width=8, justify="right")
        table.add_column(width=10, justify="right")
        table.add_row(*[Text(value, style="grey42")
                        for value in ("repeater", "", "neighbour", "snr", "reported")])
        edges = sorted(self.state.neighbor_edges.values(),
                       key=lambda edge: edge.updated_ts, reverse=True)
        if not edges:
            table.add_row(Text("F5 in remote admin pulls this on demand", style="grey42"),
                          Text(""), Text(""), Text(""), Text(""))
            return table
        now = time.time()
        for edge in edges[:30]:
            target = self.state.nodes.get(edge.target_id)
            target_name = target.name if target is not None else f"0x{edge.prefix}"
            table.add_row(
                Text(self.state.node_name(edge.source_id), style="bright_white"),
                Text("->", style="grey42"),
                Text(target_name, style="yellow" if target is not None else "grey54"),
                Text(f"{edge.snr:+.1f}" if edge.snr is not None else "-", style="cyan"),
                Text(fmt_duration(now - edge.last_seen) if edge.last_seen else "-",
                     style="grey62"),
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

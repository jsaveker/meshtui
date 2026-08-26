"""meshtui - a terminal dashboard for a Meshtastic mesh."""

from __future__ import annotations

import textwrap
import time
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Input, Static, Tabs

from .model import BROADCAST, ChatMessage, Packet, outgoing_payload, payload_bytes
from .radio import (DemoLink, RadioLink, SerialLink, TCPLink, flatten,
                    max_payload_bytes, traceroute_hops)
from .state import ForeignChannel, LocalChannel, MeshState, RelayStat
from .store import LAST_OBSERVER, Store, state_ts_key
from .widgets.audit import AuditScreen
from .widgets.chat import ChatPane, LeaveChat
from .widgets.detail import NodeDetail
from .widgets.help import HelpScreen
from .widgets.inspect import PacketInspector
from .widgets.mesh_map import MapScreen
from .widgets.nodes import NodeTable
from .widgets.relays import RelayScreen
from .widgets.sensors import SensorScreen
from .widgets.packets import PacketFeed
from .widgets.stats import StatsPane, fmt_duration

FEED_RESTORE_ROWS = 400

HELP = """
/dm <node> <text>   send a direct message (node = short name or !id)
/trace <node>       request a traceroute
/nodes              list known nodes
/clear              clear this conversation view
/help               this text
""".strip()


class MeshTUI(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "meshtui"

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("question_mark,f1", "help", "help", key_display="?"),
        Binding("slash", "focus_input", "chat", key_display="/"),
        Binding("p", "toggle_pause", "pause feed"),
        Binding("f", "cycle_filter", "filter"),
        Binding("s", "cycle_sort", "sort"),
        Binding("d", "dm_selected", "dm node"),
        Binding("m", "show_map", "map"),
        Binding("a", "show_audit", "audit"),
        Binding("r", "show_relays", "relays"),
        Binding("w", "show_sensors", "sensors"),
        Binding("t", "trace_selected", "trace"),
        Binding("i", "inspect_packet", "inspect", show=False),
        Binding("ctrl+l", "clear_feed", "clear feed", show=False),
        Binding("tab", "focus_next", "switch pane"),
    ]

    def __init__(self, port: str | None = None, demo: bool = False,
                 store: Store | None = None, restore_limit: int = 3000,
                 host: str | None = None) -> None:
        super().__init__()
        self.state = MeshState()
        self.port = port
        self.host = host
        self.demo = demo
        self.store = store
        self.restore_limit = restore_limit
        self.link: RadioLink | None = None
        self._status_note: tuple[str, str] = ("starting...", "grey70")
        self._note_until: float = 0.0

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield NodeTable(id="nodes")
                yield StatsPane(id="stats")
            with Vertical(id="right"):
                yield PacketFeed(id="packets")
                yield ChatPane(id="chat")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(NodeTable).border_title = "nodes"
        self.query_one(StatsPane).border_title = "stats"
        self.query_one(PacketFeed).border_title = "packets - all"
        chat = self.query_one(ChatPane)
        chat.set_title("chat")
        # Take the limit from the installed library rather than hard-coding it.
        chat.max_bytes = max_payload_bytes()

        self._restore()
        self.set_interval(1.0, self._tick)
        # Node rows change with nearly every packet; persist them on a slow
        # cadence rather than writing a row per packet.
        self.set_interval(60.0, self._persist_nodes)

        # Replaying stored packets rebuilds everything derived - SNR history,
        # sensor readings, relay counts - which the nodes table does not hold.
        # It reads and decodes off the UI thread, then connects when done so
        # the feed stays in chronological order.
        # Observations are scoped to the radio doing the observing, so they can
        # only be restored once the link tells us which node that is.
        self._start_link()

    def _start_link(self) -> None:
        if self.demo:
            self.link = DemoLink(self._emit)
        elif self.host:
            self.link = TCPLink(self._emit, self.host)
        else:
            self.link = SerialLink(self._emit, self.port)
        # Connecting blocks (serial handshake + config download), so keep it
        # off the UI thread.
        self.run_worker(self.link.start, thread=True, name="radio-connect")

    def on_unmount(self) -> None:
        if self.link is not None:
            self.link.stop()
        if self.store is not None and self.store.enabled:
            self._persist_nodes()
            self.store.close()

    # ---------------------------------------------------------- persistence

    def _restore(self) -> None:
        """Repopulate from the database so the UI is useful before the first packet."""
        if self.store is None or not self.store.enabled:
            return
        restored = 0
        for record in self.store.known_nodes():
            first_seen = record.pop("_first_seen", None)
            record.pop("_packets", 0)
            derived = record.pop("_derived", {})
            try:
                node = self.state.upsert_node(record)
            except ValueError:
                continue
            if first_seen:
                node.first_seen = first_seen
            self._apply_derived(node, derived)
            restored += 1
        for message in self.store.recent_messages():
            self.state.add_chat(message)

        # Optimistically load the last-attached radio's view so the dashboard
        # is useful immediately, and with no radio at all. If a different node
        # turns out to be plugged in, this is discarded on connect.
        last = self.store.get_meta(LAST_OBSERVER)
        if last:
            self.store.local_node = str(last)
            self._restore_observations()
        if restored or self.state.chat:
            self.note(
                f"restored {restored} nodes, {len(self.state.chat)} messages from history",
                "grey70",
            )

    def _replay_worker(self) -> None:
        """Read and decode stored packets off the UI thread."""
        packets = []
        try:
            for raw in self.store.recent_packets(self.restore_limit):  # type: ignore[union-attr]
                try:
                    packets.append(flatten(raw))
                except Exception:  # noqa: BLE001 - one bad row must not stop the rest
                    continue
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.note, f"could not replay history: {exc}", "red")
        try:
            self.call_from_thread(self._apply_replay, packets)
        except RuntimeError:
            pass

    def _apply_replay(self, packets: list[Packet]) -> None:
        """Fold replayed packets into state on the UI thread, then connect."""
        # Everything goes into the visible buffer so the feed has scrollback,
        # but only packets newer than the persisted snapshot are folded into
        # the aggregates - the rest are already counted in it.
        since = self.state.last_packet_ts
        folded = 0
        for packet in packets:
            fold = packet.ts > since
            folded += fold
            self.state.add_packet(packet, historical=True, fold=fold)
        if packets:
            # Live packets may have arrived while the replay was decoding.
            ordered = sorted(self.state.packets, key=lambda p: p.ts)
            self.state.packets.clear()
            self.state.packets.extend(ordered)
            feed = self.query_one(PacketFeed)
            feed.rerender(self.state, limit=FEED_RESTORE_ROWS)
            feed.write_notice(
                f"---- {len(packets)} packets replayed from history; "
                f"live traffic follows ----", "bold grey54")
            span = packets[-1].ts - packets[0].ts
            detail = f"{folded} new" if folded else "already up to date"
            self.note(
                f"replayed {len(packets)} packets covering "
                f"{fmt_duration(span)} of history ({detail})", "grey70")
        self._tick()

    @staticmethod
    def _apply_derived(node, derived: dict) -> None:
        """Reload sparklines, sensors and motion saved alongside the node."""
        if not derived:
            return
        for value in derived.get("snr_history") or []:
            try:
                node.snr_history.append(float(value))
            except (TypeError, ValueError):
                pass
        env = derived.get("env") or {}
        if env:
            node.env.update({k: float(v) for k, v in env.items()
                             if isinstance(v, (int, float))})
            node.env_ts = derived.get("env_ts")
        stats = derived.get("local_stats") or {}
        if stats:
            node.local_stats.update({k: float(v) for k, v in stats.items()
                                     if isinstance(v, (int, float))})
            node.local_stats_ts = derived.get("local_stats_ts")
        for point in derived.get("track") or []:
            if isinstance(point, (list, tuple)) and len(point) == 3:
                node.track.append((float(point[0]), float(point[1]), float(point[2])))
        for key in ("speed_mps", "heading_deg", "sats", "precision_bits"):
            if derived.get(key) is not None:
                setattr(node, key, derived[key])
        if derived.get("location_source"):
            node.location_source = derived["location_source"]

    def _restore_for_observer(self) -> None:
        """Reconcile the restored view with the radio that actually connected.

        Signal, hop count and relay share all mean "as heard from here", so if
        this is a different node from last time its predecessor's view is
        dropped rather than blended into.
        """
        store = self.store
        if store is None or not store.enabled:
            return
        previous = store.local_node
        store.local_node = self.state.my_node_id
        if previous and previous != store.local_node:
            self.state.clear_observations()
            self.query_one(PacketFeed).clear_feed()
            self.note(f"different radio than last run "
                      f"({self.state.node_name(previous)} -> "
                      f"{self.state.node_name(store.local_node or '')}); "
                      f"observations start fresh", "yellow")
        elif previous == store.local_node:
            # Already restored optimistically at startup.
            store.set_meta(LAST_OBSERVER, store.local_node)
            if self.restore_limit > 0:
                self.run_worker(self._replay_worker, thread=True, name="replay")
            return
        self._restore_observations()
        store.set_meta(LAST_OBSERVER, store.local_node)
        if self.restore_limit > 0:
            self.run_worker(self._replay_worker, thread=True, name="replay")

    def _restore_observations(self) -> None:
        """Load this observer's node view and mesh aggregates."""
        store = self.store
        if store is None or not store.enabled:
            return
        seen = 0
        for node_id, obs in store.load_node_observations().items():
            node = self.state.nodes.get(node_id)
            if node is None:
                continue
            node.snr = obs["snr"]
            node.hops = obs["hops"]
            node.packets = obs["packets"] or 0
            if obs["last_heard"]:
                node.last_heard = max(node.last_heard or 0.0, obs["last_heard"])
            for value in obs["snr_history"]:
                try:
                    node.snr_history.append(float(value))
                except (TypeError, ValueError):
                    pass
            seen += 1
        self._restore_aggregates()
        if seen:
            self.note(f"restored {seen} observations for "
                      f"{self.state.node_name(store.local_node or '')}", "grey70")

    def _restore_aggregates(self) -> None:
        """Reload relay and channel counters saved as a snapshot."""
        store = self.store
        if store is None:
            return
        for row in store.load_relays():
            self.state.relays[row["byte"]] = RelayStat(
                byte=row["byte"], packets=row["packets"], origins=set(row["origins"]),
                first_seen=row["first_seen"] or time.time(),
                last_seen=row["last_seen"] or time.time(),
                snr_sum=row["snr_sum"], snr_n=row["snr_n"],
            )
        for origin, byte, count in store.load_relay_edges():
            self.state.relay_edges[(origin, byte)] = count
        for row in store.load_foreign_channels():
            channel = ForeignChannel(
                hash=row["hash"], packets=row["packets"] or 0,
                senders=row["senders"], first_seen=row["first_seen"] or time.time(),
                last_seen=row["last_seen"] or time.time(),
                snr_min=row["snr_min"], snr_max=row["snr_max"],
                hops_min=row["hops_min"], hops_max=row["hops_max"],
                key_label=row["key_label"], sample=row["sample"],
            )
            channel.ports.update(row["ports"])
            self.state.foreign_channels[row["hash"]] = channel
        self.state.last_packet_ts = float(
            store.get_meta(state_ts_key(store.local_node), 0.0) or 0.0)

    def _persist_nodes(self) -> None:
        """Write the whole derived snapshot: node rows, the state folded in
        from packets, and the mesh-wide relay and channel counters."""
        store = self.store
        if store is None or not store.enabled:
            return
        if store.local_node is None:
            # Before the radio identifies itself we cannot attribute
            # observations, and writing them unattributed would pollute the
            # scoped tables.
            return
        for node in self.state.nodes.values():
            store.save_node(node)
            store.save_node_derived(node)
            store.save_node_observation(node)
        for relay in self.state.relays.values():
            store.save_relay(relay.byte, relay.packets, relay.origins,
                             relay.first_seen, relay.last_seen,
                             relay.snr_sum, relay.snr_n)
        for (origin, byte), count in self.state.relay_edges.items():
            store.save_relay_edge(origin, byte, count)
        for channel in self.state.foreign_channels.values():
            store.save_foreign_channel(channel)
        store.set_meta(state_ts_key(store.local_node), self.state.last_packet_ts)
        store.set_meta(LAST_OBSERVER, store.local_node)

    # ------------------------------------------------- radio -> UI bridge

    def _emit(self, kind: str, payload: Any) -> None:
        """Called from the radio thread; hop to the UI thread.

        Late events can arrive while the app is tearing down, so bail out
        before call_from_thread builds a coroutine nothing will ever await.
        """
        if not self.is_running:
            return
        try:
            self.call_from_thread(self._handle, kind, payload)
        except RuntimeError:
            # Event loop closed between the check above and the call.
            pass

    def _handle(self, kind: str, payload: Any) -> None:
        if kind == "packet":
            self._on_packet(payload)
        elif kind == "node":
            try:
                node = self.state.upsert_node(payload)
            except ValueError:
                pass
            else:
                if self.store is not None:
                    self.store.save_node(node)
        elif kind == "chat":
            self._on_chat(payload)
        elif kind == "connected":
            self._on_connected(payload)
        elif kind == "lost":
            self.state.connected = False
            self.note(str(payload), "red")
        elif kind == "ack":
            msg = self.state.ack(int(payload))
            if msg is not None:
                if self.store is not None:
                    self.store.ack_message(int(payload))
                self.query_one(ChatPane).rerender(self.state)
        elif kind == "status":
            self.note(str(payload))
        elif kind == "error":
            self._show_error(str(payload))

    def _show_error(self, message: str) -> None:
        """Errors are often multi-sentence instructions, and the feed does not
        wrap, so wrap them by hand to the pane width."""
        feed = self.query_one(PacketFeed)
        width = max(40, feed.size.width - 4)
        self.note(message.split(".")[0][:80], "red")
        for i, line in enumerate(textwrap.wrap(message, width)):
            feed.write_notice(("! " if i == 0 else "  ") + line, "bold red")

    def _show_traceroute(self, packet: Packet) -> None:
        """Write a readable route, with per-hop signal in both directions."""
        towards, back = traceroute_hops(packet.raw)
        if not towards:
            return
        chat = self.query_one(ChatPane)

        def name(num: int) -> str:
            return self.state.node_name(f"!{num:08x}")

        me = self.state.my_node_id or "!me"
        chat.notice("")
        hops = len(towards) - 1
        chat.notice(f"  traceroute to {name(packet.raw.get('from', 0))}: "
                    f"{'DIRECT, no relay' if hops == 0 else f'{hops} hop(s)'}",
                    "bold bright_cyan")

        def render(label: str, chain: list, start: str) -> None:
            # One hop per line: a single-line route truncates badly in a narrow
            # chat pane, and the per-hop SNR is the point of the exercise.
            chat.notice(f"    {label:<5} {start}", "grey62")
            for num, snr in chain:
                signal = f"{snr:+.1f}dB" if snr is not None else "     ?"
                if snr is None:
                    style = "grey42"
                elif snr >= 0:
                    style = "bright_green"
                elif snr >= -8:
                    style = "yellow"
                else:
                    style = "red"
                chat.notice(f"          {signal:>8}  -> {name(num)}", style)

        render("out", towards, self.state.node_name(me) + "  (you)")
        if back:
            render("back", back, name(packet.raw.get("from", 0)))
        else:
            chat.notice("    (no return path reported)", "grey54")
        chat.notice("")

    def _on_packet(self, packet: Packet) -> None:
        # Make sure every sender exists in the node table, even before its
        # NODEINFO arrives, so counts and names line up.
        if packet.from_id not in self.state.nodes and packet.from_id.startswith("!"):
            try:
                self.state.upsert_node({"id": packet.from_id, "num": packet.raw.get("from")})
            except ValueError:
                pass
        self.state.add_packet(packet)
        if self.store is not None:
            self.store.add_packet(packet)
        self.query_one(PacketFeed).add(packet, self.state)
        if packet.portnum == "TRACEROUTE_APP":
            self._show_traceroute(packet)

    def _on_chat(self, message: ChatMessage) -> None:
        message.from_name = self.state.node_name(message.from_id)
        self.state.add_chat(message)
        if self.store is not None:
            self.store.add_message(message)
        chat = self.query_one(ChatPane)
        if message.is_dm and message.to_id == self.state.my_node_id:
            self.run_worker(
                chat.ensure_dm_tab(message.from_id, self.state.node_name(message.from_id)),
                name="dm-tab",
            )
        if not chat.add(message, self.state):
            self.note(f"new message from {message.from_name}", "bright_green")
        self.bell()

    def _on_connected(self, info: dict[str, Any]) -> None:
        self.state.connected = True
        self.state.my_node_id = info.get("my_node_id")
        self.state.my_node_name = info.get("my_node_name") or ""
        self.state.firmware = info.get("firmware") or ""
        self.state.device_path = info.get("device") or ""
        self.state.channels = list(info.get("channels") or ["LongFast"])
        self.state.local_channels = [
            LocalChannel(index=c.get("index", i), name=c.get("name", f"ch{i}"),
                         level=c.get("level", "UNKNOWN"), detail=c.get("detail", ""),
                         hash=c.get("hash"))
            for i, c in enumerate(info.get("channel_security") or [])
        ]
        weak = [c for c in self.state.local_channels if c.level in ("OPEN", "PUBLIC", "WEAK")]
        if weak:
            self.note(
                f"{len(weak)} of your channels are not private - press 'a' for the audit",
                "bold red",
            )
        if self.state.my_node_id and self.state.my_node_id in self.state.nodes:
            self.state.nodes[self.state.my_node_id].is_self = True
        self._restore_for_observer()
        self.run_worker(
            self.query_one(ChatPane).set_channels(self.state.channels),
            name="chat-tabs",
        )
        self.note("connected", "green")

    # ------------------------------------------------------------ refresh

    def _tick(self) -> None:
        self.query_one(NodeTable).render_state(self.state)
        self.query_one(StatsPane).render_state(self.state)
        self._render_status()

    def note(self, text: str, style: str = "grey70") -> None:
        self._status_note = (text, style)
        self._note_until = time.time() + 6.0
        self._render_status()

    def _render_status(self) -> None:
        state = self.state
        if state.connected:
            dot, colour = "*", "bright_green"
            where = state.device_path or "?"
            who = state.my_node_name or state.my_node_id or "unknown node"
        else:
            dot, colour = "x", "red"
            where = state.device_path or (self.port or "searching...")
            who = "not connected"

        left = Text()
        left.append(f" {dot} ", style=f"bold {colour}")
        left.append(who, style="bold bright_white")
        left.append("  ")
        left.append(where, style="grey62")
        if state.firmware:
            left.append(f"  fw {state.firmware}", style="grey42")

        if self._status_note and time.time() < self._note_until:
            text, style = self._status_note
            left.append("   ")
            left.append(text, style=style)
        self.query_one("#status", Static).update(left)

    # ------------------------------------------------------------ actions

    def action_focus_input(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def action_toggle_pause(self) -> None:
        feed = self.query_one(PacketFeed)
        paused = feed.toggle_pause(self.state)
        feed.border_title = f"packets - {feed.filter_name}{' - PAUSED' if paused else ''}"
        self.note("feed paused" if paused else "feed resumed")

    def action_cycle_filter(self) -> None:
        feed = self.query_one(PacketFeed)
        name = feed.cycle_filter(self.state)
        feed.border_title = f"packets - {name}{' - PAUSED' if feed.paused else ''}"

    def action_cycle_sort(self) -> None:
        table = self.query_one(NodeTable)
        key = table.cycle_sort()
        table.render_state(self.state)
        table.border_title = f"nodes - by {key}"

    def action_clear_feed(self) -> None:
        self.query_one(PacketFeed).clear_feed()

    def action_inspect_packet(self) -> None:
        packet = self.query_one(PacketFeed).selected_packet()
        if packet is None:
            self.note("no packet selected", "yellow")
            return
        self.push_screen(PacketInspector(packet))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def on_leave_chat(self, event: LeaveChat) -> None:
        """Escape from the message box hands focus back to the packet feed."""
        event.stop()
        self.query_one(PacketFeed).focus()

    def action_show_map(self) -> None:
        self.push_screen(MapScreen(self.state))

    def action_show_audit(self) -> None:
        self.push_screen(AuditScreen(self.state))

    def action_show_relays(self) -> None:
        self.push_screen(RelayScreen(self.state))

    def action_show_sensors(self) -> None:
        self.push_screen(SensorScreen(self.state))

    def action_node_detail(self) -> None:
        node = self._selected_node()
        if node is not None:
            self.push_screen(NodeDetail(node))

    def action_dm_selected(self) -> None:
        node = self._selected_node()
        if node is None:
            self.note("select a node first", "yellow")
            return
        chat = self.query_one(ChatPane)
        self.run_worker(chat.focus_dm(node.node_id, node.label), name="dm-tab")
        self.query_one("#chat-input", Input).focus()

    def action_trace_selected(self) -> None:
        node = self._selected_node()
        if node is None:
            self.note("select a node first", "yellow")
            return
        self._trace(node.node_id, 5)

    def _trace(self, node_id: str, hop_limit: int) -> None:
        """Send a traceroute on a worker thread.

        The serial write plus the library's internal bookkeeping can take a
        noticeable moment, and doing it inline freezes the whole interface.
        """
        link = self.link
        if link is None or not self.state.connected:
            self.note("not connected", "red")
            return
        self.query_one(ChatPane).notice(
            f"  traceroute to {self.state.node_name(node_id)} sent"
            f"{' (direct link only)' if hop_limit <= 1 else f' (up to {hop_limit} hops)'}"
            f" - the reply can take 30s or more", "grey62")
        self.run_worker(
            lambda: link.request_traceroute(node_id, hop_limit),
            thread=True, name="traceroute",
        )

    def _selected_node(self):
        node_id = self.query_one(NodeTable).selected_node_id()
        return self.state.nodes.get(node_id) if node_id else None

    # ------------------------------------------------------------- events

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Both the node table and the packet feed are DataTables; dispatch on
        # which one actually fired.
        if isinstance(event.data_table, PacketFeed):
            self.action_inspect_packet()
        elif isinstance(event.data_table, NodeTable):
            self.action_node_detail()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        chat = self.query_one(ChatPane)
        chat.rerender(self.state)
        kind, target = chat.active_target()
        if kind == "dm":
            chat.set_title(f"chat - dm {self.state.node_name(str(target))}")
        else:
            chat.set_title(f"chat - #{self.state.channel_name(int(target))}")  # type: ignore[arg-type]

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "chat-input":
            self.query_one(ChatPane).update_counter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        # Meshtastic caps a packet's data payload; refuse rather than let the
        # radio reject it after the message has already been shown as sent.
        chat = self.query_one(ChatPane)
        payload = outgoing_payload(text)
        if payload is not None:
            used = payload_bytes(payload)
            limit = chat.max_bytes
            if used > limit:
                chat.notice(
                    f"  message is {used} bytes, {used - limit} over the "
                    f"{limit}-byte mesh limit - shorten it and send again",
                    "bold red",
                )
                self.note(f"too long: {used}/{limit} bytes", "red")
                return  # text stays in the box so it can be edited

        event.input.value = ""
        chat.update_counter("")
        if text.startswith("/"):
            self._command(text)
        else:
            kind, target = self.active_chat_target()
            self._send(text, target if kind == "dm" else BROADCAST,
                       0 if kind == "dm" else int(target))  # type: ignore[arg-type]

    def active_chat_target(self) -> tuple[str, Any]:
        return self.query_one(ChatPane).active_target()

    # ----------------------------------------------------------- commands

    def _command(self, line: str) -> None:
        chat = self.query_one(ChatPane)
        parts = line.split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd in ("/help", "/?"):
            for row in HELP.splitlines():
                chat.notice("  " + row)
        elif cmd == "/clear":
            chat.log.clear()
        elif cmd == "/nodes":
            for node in self.state.sorted_nodes("heard"):
                chat.notice(f"  {node.node_id}  {node.label:<6} {node.long_name}")
        elif cmd == "/dm":
            if len(parts) < 3:
                chat.notice("  usage: /dm <node> <text>", "yellow")
                return
            node = self.state.resolve(parts[1])
            if node is None:
                chat.notice(f"  unknown node: {parts[1]}", "yellow")
                return
            self.run_worker(chat.focus_dm(node.node_id, node.label), name="dm-tab")
            self._send(parts[2], node.node_id, 0)
        elif cmd == "/trace":
            if len(parts) < 2:
                chat.notice("  usage: /trace <node> [maxhops]   "
                            "(use 1 to test only the direct link)", "yellow")
                return
            node = self.state.resolve(parts[1])
            if node is None:
                chat.notice(f"  unknown node: {parts[1]}", "yellow")
                return
            hop_limit = 5
            if len(parts) >= 3:
                try:
                    hop_limit = max(1, min(7, int(parts[2].split()[0])))
                except ValueError:
                    chat.notice("  maxhops must be a number 1-7", "yellow")
                    return
            self._trace(node.node_id, hop_limit)
        else:
            chat.notice(f"  unknown command {cmd} - try /help", "yellow")

    def _send(self, text: str, dest: str, channel: int) -> None:
        if self.link is None or not self.state.connected:
            self.note("not connected - cannot send", "red")
            return
        accepted, packet_id = self.link.send_text(text, dest=dest, channel=channel)
        if not accepted:
            # The radio rejected it; recording it would show a message that
            # never left as pending forever.
            return
        message = ChatMessage(
            ts=time.time(),
            from_id=self.state.my_node_id or "!me",
            from_name="you",
            to_id=dest,
            text=text,
            channel=channel,
            outgoing=True,
            packet_id=packet_id,
        )
        self.state.add_chat(message)
        if self.store is not None:
            self.store.add_message(message)
        self.state.stats.sent += 1
        self.query_one(ChatPane).add(message, self.state)

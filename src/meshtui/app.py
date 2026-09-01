"""meshtui - a terminal dashboard for a Meshtastic mesh."""

from __future__ import annotations

import json
import textwrap
import threading
import time
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid
from textual.css.query import NoMatches
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Input, Static

from .model import (BROADCAST, ChannelRef, ChatMessage, DeliveryStatus, Packet,
                    PeerRef, outgoing_payload, payload_bytes)
from .gateway import GatewayLink
from .meshcore_link import MeshCoreLink, probe_meshcore
from .radio import (DemoLink, RadioLink, SerialLink, TCPLink,
                    find_serial_ports, protocol_payload_limit,
                    traceroute_hops)
from .preferences import LAYOUTS, THEMES, OperatorPreferences
from .state import ForeignChannel, RelayStat, sane_heard
from .service import MeshService
from .store import LAST_OBSERVER, Store, state_ts_key
from .widgets.admin import AdminScreen
from .widgets.audit import AuditScreen
from .widgets.channels import ChannelScreen
from .widgets.chat import ChatPane, LeaveChat, OpenChatOverlay
from .widgets.chat_overlay import ChatScreen
from .widgets.detail import NodeDetail
from .widgets.help import HelpScreen
from .widgets.inspect import PacketInspector
from .widgets.mesh_map import MapScreen
from .widgets.nodes import NodeTable
from .widgets.operator import PacketWorkbench, RoutePane
from .widgets.palette import CommandPalette
from .widgets.paths import PathScreen
from .widgets.relays import RelayScreen
from .widgets.rooms import RoomScreen
from .widgets.scope import ScopeScreen
from .widgets.sensors import SensorScreen
from .widgets.packets import PacketFeed
from .widgets.stats import StatsPane, fmt_duration

FEED_RESTORE_ROWS = 400

# Verbs whose argument is a credential and must never be written down.
SECRET_VERBS = ("login", "password", "passwd", "pass")

OPERATOR_THEMES = (
    Theme(
        name="phosphor", primary="#28f0a0", secondary="#20b8a0",
        accent="#5fffd0", warning="#ffd75f", error="#ff5f5f", success="#28f0a0",
        foreground="#c8ffe8", background="#020907", surface="#06110d",
        panel="#0a1a14", boost="#103326", dark=True,
    ),
    Theme(
        name="night-vision", primary="#72ff45", secondary="#34b82e",
        accent="#b0ff78", warning="#f5e663", error="#ff624f", success="#72ff45",
        foreground="#d7ffc8", background="#010600", surface="#071006",
        panel="#0c1b09", boost="#153510", dark=True,
    ),
    Theme(
        name="high-contrast", primary="#ffffff", secondary="#00e5ff",
        accent="#ffff00", warning="#ffff00", error="#ff3b30", success="#4cff4c",
        foreground="#ffffff", background="#000000", surface="#000000",
        panel="#101010", boost="#242424", dark=True, text_alpha=1.0,
    ),
)


def redact_command(text: str) -> str:
    """Keep the verb, drop the secret.

    The admin log is persisted, so a password typed here would otherwise
    outlive the session on disk. The log echoes commands as "> login secret",
    so the verb can be the second token as well as the first.
    """
    parts = text.split()
    if not parts:
        return text
    start = 1 if parts[0] == ">" and len(parts) > 1 else 0
    verb = parts[start].lower().lstrip("/")
    if verb in SECRET_VERBS and len(parts) > start + 1:
        return " ".join(parts[:start + 1] + ["<redacted>"])
    return text

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
        Binding("slash", "command_palette", "command", key_display="/"),
        Binding("z", "expand_chat", "expand chat"),
        Binding("p", "toggle_pause", "pause feed"),
        Binding("f", "cycle_filter", "filter"),
        Binding("s", "cycle_sort", "sort"),
        Binding("d", "dm_selected", "dm node"),
        Binding("m", "show_map", "map"),
        Binding("a", "show_audit", "audit"),
        Binding("r", "show_relays", "relays"),
        Binding("v", "show_paths", "paths"),
        Binding("w", "show_sensors", "sensors"),
        Binding("x", "show_admin", "remote admin"),
        Binding("o", "show_rooms", "rooms"),
        Binding("c", "show_channels", "channels"),
        Binding("A", "fix_autoadd", "auto-add on", show=False),
        Binding("V", "send_advert", "advert", show=False),
        Binding("t", "trace_selected", "trace"),
        Binding("i", "inspect_packet", "inspect", show=False),
        Binding("l", "cycle_layout", "layout", show=False),
        Binding("T", "cycle_theme", "theme", show=False),
        Binding("ctrl+l", "clear_feed", "clear feed", show=False),
        Binding("tab", "focus_next", "switch pane"),
    ]

    def __init__(self, port: str | None = None, demo: bool = False,
                 store: Store | None = None, restore_limit: int = 3000,
                 host: str | None = None, protocol: str = "auto",
                 gateway: str | None = None,
                 preferences_path: str | None = None) -> None:
        super().__init__()
        for theme in OPERATOR_THEMES:
            self.register_theme(theme)
        self.service = MeshService(store)
        self.state = self.service.state
        if protocol != "auto":
            self.state.protocol = protocol
        self.port = port
        self.host = host
        self.protocol = protocol
        self.gateway = gateway
        self.demo = demo
        self.store = store
        self.restore_limit = restore_limit
        self.preferences = OperatorPreferences(preferences_path)
        self.layout_name = "balanced"
        self._link: RadioLink | None = None
        self._status_note: tuple[str, str] = ("starting...", "grey70")
        self._note_until: float = 0.0

    @property
    def link(self) -> RadioLink | None:
        return self._link

    @link.setter
    def link(self, value: RadioLink | None) -> None:
        self._link = value
        self.service.attach_link(value)

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Grid(id="workspace"):
            yield NodeTable(id="nodes")
            yield ChatPane(id="chat")
            yield PacketWorkbench(id="packet-workbench")
            yield RoutePane(self.state, id="route-pane")
        # Kept mounted as a compact aggregate source while its information is
        # folded into the operator panes. Existing integrations can still
        # query it, but it no longer consumes one of the four workspace cells.
        yield StatsPane(id="stats")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(NodeTable).border_title = "nodes"
        self.query_one(StatsPane).border_title = "stats"
        self.query_one(PacketFeed).border_title = "packets - all"
        self.query_one(PacketWorkbench).border_title = "packet + hex"
        self.query_one(RoutePane).border_title = "selected route"
        chat = self.query_one(ChatPane)
        chat.set_title("chat")
        # Explicit MeshCore mode must be safe even before the radio connects.
        chat.max_bytes = protocol_payload_limit(self.state.protocol)
        self._apply_preferences()

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
        if self.gateway is not None:
            # Attach to a running gateway: it owns the radio and the database,
            # this process just renders the stream and sends through it.
            self.link = GatewayLink(self._emit, self.gateway or None)
            self.link.start()
            return
        if self.demo:
            self.link = DemoLink(self._emit)
            self.run_worker(self.link.start, thread=True, name="radio-connect")
            return
        # Both probing and connecting block, so choose and connect on a worker.
        self.run_worker(self._connect_worker, thread=True, name="radio-connect")

    def _connect_worker(self) -> None:
        link = self._choose_link()
        self.link = link
        link.start()

    def _choose_link(self) -> RadioLink:
        """Pick a transport, probing the hardware when asked to autodetect."""
        protocol = self.protocol
        if protocol == "auto" and not self.host:
            port = self.port or next(iter(find_serial_ports()), None)
            if port and probe_meshcore(port):
                self.note(f"detected a MeshCore radio on {port}", "grey70")
                protocol = "meshcore"
            else:
                protocol = "meshtastic"
        elif protocol == "auto":
            protocol = "meshtastic"

        if protocol == "meshcore":
            return MeshCoreLink(self._emit, port=self.port or
                                next(iter(find_serial_ports()), None),
                                host=self.host)
        if self.host:
            return TCPLink(self._emit, self.host)
        return SerialLink(self._emit, self.port)

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

    @staticmethod
    def _packet_from_row(row: dict[str, Any]) -> Packet:
        """Rebuild a Packet from its stored columns.

        Protocol-agnostic on purpose: the columns already hold everything the
        UI renders, so replay does not need to know whether a Meshtastic or a
        MeshCore radio produced the row.
        """
        try:
            raw = json.loads(row.get("raw") or "{}")
        except Exception:  # noqa: BLE001
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        portnum = row.get("portnum") or "UNKNOWN"
        return Packet(
            ts=float(row.get("ts") or 0.0),
            from_id=row.get("from_id") or "?",
            to_id=row.get("to_id") or "?",
            portnum=portnum,
            summary=row.get("summary") or "",
            channel=int(row.get("channel") or 0),
            snr=row.get("snr"),
            rssi=row.get("rssi"),
            hops=row.get("hops"),
            packet_id=row.get("packet_id"),
            encrypted=portnum == "ENCRYPTED",
            relay_node=raw.get("relayNode"),
            via_mqtt=bool(raw.get("viaMqtt")),
            raw=raw,
        )

    def _replay_worker(self) -> None:
        """Read stored packets off the UI thread."""
        packets = []
        try:
            for row in self.store.recent_packets(self.restore_limit):  # type: ignore[union-attr]
                try:
                    packets.append(self._packet_from_row(row))
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
            heard = sane_heard(obs["last_heard"])
            if heard is not None:
                node.last_heard = max(node.last_heard or 0.0, heard)
            for value in obs["snr_history"]:
                try:
                    node.snr_history.append(float(value))
                except (TypeError, ValueError):
                    pass
            seen += 1
        self._restore_aggregates()
        for stamp, node_id, text in store.recent_admin_log():
            self.state.cli_log.append((stamp, node_id, text))
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
            self.service.receive_node(payload)
        elif kind == "chat":
            self._on_chat(payload)
        elif kind == "chat_update":
            self._on_chat_update(payload)
        elif kind == "connected":
            self._on_connected(payload)
        elif kind == "lost":
            self.state.connected = False
            self.note(str(payload), "red")
        elif kind == "ack":
            receipt = self.service.ack_protocol(payload)
            if receipt is not None:
                self.query_one(ChatPane).rerender(self.state)
                self._refresh_overlay()
        elif kind == "receipt":
            self.service.apply_receipt(payload)
            self.query_one(ChatPane).rerender(self.state)
            self._refresh_overlay()
        elif kind == "mc_contact":
            self.service.receive_contact(payload)
        elif kind == "mc_autoadd":
            self.state.radio_info["autoadd"] = payload
        elif kind == "mc_repeat":
            if self.service.note_repeat(payload) is not None:
                self.query_one(ChatPane).rerender(self.state)
                self._refresh_overlay()
        elif kind == "mc_channels":
            self.state.channels = list(payload) or [(0, "Public")]
            self.query_one(ChatPane).set_channels(self.state)
            self._refresh_overlay()
        elif kind == "mc_channel_security":
            from .state import LocalChannel
            self.state.local_channels = [
                LocalChannel(index=c.get("index", i), name=c.get("name", f"ch{i}"),
                             level=c.get("level", "UNKNOWN"),
                             detail=c.get("detail", ""), hash=c.get("hash"))
                for i, c in enumerate(payload or [])]
        elif kind == "mc_login":
            node_id, ok = payload
            if ok:
                self.state.admin_sessions.add(node_id)
                self.note(f"logged in to {self.state.node_name(node_id)}", "green")
            else:
                self.state.admin_sessions.discard(node_id)
                self.note(f"login refused by {self.state.node_name(node_id)}", "red")
            self.record_admin(node_id,
                              "** logged in **" if ok else "** login refused **")
        elif kind == "mc_cli":
            node_id, text = payload
            self.record_admin(node_id, text)
        elif kind == "mc_status":
            node_id, data = payload
            self.service.note_status(node_id, data)
            self.record_admin(node_id, f"status: {data}")
        elif kind == "mc_telemetry":
            node_id, data = payload
            self.record_admin(node_id, f"telemetry: {data}")
        elif kind == "mc_neighbours":
            node_id, neighbours = payload
            self.state.note_neighbours(node_id, neighbours)
            self.record_admin(node_id, f"neighbours ({len(neighbours)}):")
            for entry in neighbours:
                who = self.state.node_name(f"!{str(entry.get('pubkey', ''))[:8]}")
                snr = entry.get("snr")
                ago = entry.get("secs_ago")
                self.record_admin(node_id, (
                    f"  {who:24} "
                    f"{f'{snr:+.1f}dB' if snr is not None else '?':>8}  "
                    f"{f'{ago}s ago' if ago is not None else ''}"))
        elif kind == "mc_flood_scope":
            self.state.radio_info.update(payload)
        elif kind == "mc_radio_stats":
            self.state.radio_info.update(payload)
            self.state.stats.record_radio_airtime(payload)
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

    def record_admin(self, node_id: str, text: str) -> None:
        """Append to the remote-admin session log, in memory and on disk.

        Redaction happens here as well as at the call site, so no path can put
        a credential into a file.
        """
        safe = redact_command(text)
        stamp = time.time()
        self.state.cli_log.append((stamp, node_id, safe))
        if self.store is not None and self.store.enabled:
            self.store.add_admin_log(stamp, node_id, safe)

    def _show_traceroute(self, packet: Packet) -> None:
        """Write a readable route to the packet feed, with per-hop signal
        in both directions."""
        towards, back = traceroute_hops(packet.raw)
        if not towards:
            return

        def name(num: int) -> str:
            return self.state.node_name(f"!{num:08x}")

        me = self.state.my_node_id or "!me"
        feed = self.query_one(PacketFeed)
        feed.write_notice("", "grey42")
        hops = len(towards) - 1
        feed.write_notice(
            f"traceroute to {name(packet.raw.get('from', 0))}: "
            f"{'DIRECT, no relay' if hops == 0 else f'{hops} hop(s)'}",
            "bold bright_cyan")

        def render(label: str, chain: list, start: str) -> None:
            feed.write_notice(f"  {label:<5} {start}", "grey62")
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
                feed.write_notice(f"        {signal:>8}  -> {name(num)}", style)

        render("out", towards, self.state.node_name(me) + "  (you)")
        if back:
            render("back", back, name(packet.raw.get("from", 0)))
        else:
            feed.write_notice("  (no return path reported)", "grey54")
        feed.write_notice("", "grey42")

    def _on_packet(self, packet: Packet) -> None:
        self.service.receive_packet(packet)
        self.query_one(PacketFeed).add(packet, self.state)
        if packet.portnum == "TRACEROUTE_APP":
            self._show_traceroute(packet)

    def _on_chat(self, message: ChatMessage) -> None:
        self.service.receive_chat(message)
        chat = self.query_one(ChatPane)
        if not chat.add(message, self.state):
            # Not the conversation on screen; keep the corner button's unread
            # tally current and announce it.
            chat.rerender(self.state)
            if not message.outgoing:
                self.note(f"new message from {message.from_name}", "bright_green")
        self._refresh_overlay()
        if not message.outgoing:
            self.bell()

    def _on_chat_update(self, message: ChatMessage) -> None:
        """A gateway re-sent a message we already rendered (our own send echoed
        back, or its delivery/repeater facts changed): merge, never append."""
        if not message.message_id:
            return
        for existing in reversed(self.state.chat):
            if existing.message_id != message.message_id:
                continue
            if message.delivery_status:
                existing.delivery_status = message.delivery_status
            existing.acked = existing.acked or message.acked
            existing.repeated_by |= message.repeated_by
            if message.packet_id is not None:
                existing.packet_id = message.packet_id
            self.query_one(ChatPane).rerender(self.state)
            self._refresh_overlay()
            return
        self._on_chat(message)  # never rendered here after all

    def _refresh_overlay(self) -> None:
        """Keep the pop-out in step when it happens to be open."""
        screen = self.screen
        if isinstance(screen, ChatScreen):
            screen.rebuild_list()
            screen.render_conversation()

    def _on_connected(self, info: dict[str, Any]) -> None:
        previous_observer = self.store.local_node if self.store is not None else None
        self.service.connected(info)
        # _restore_for_observer needs to compare the old observer with the newly
        # connected radio before it changes Store.local_node.
        if self.store is not None:
            self.store.local_node = previous_observer
        self.query_one(ChatPane).max_bytes = protocol_payload_limit(self.state.protocol)
        weak = [c for c in self.state.local_channels if c.level in ("OPEN", "PUBLIC", "WEAK")]
        if weak:
            self.note(
                f"{len(weak)} of your channels are not private - press 'a' for the audit",
                "bold red",
            )
        self._restore_for_observer()
        self.query_one(ChatPane).set_channels(self.state)
        self._apply_preferences()
        self.note("connected", "green")

    # ------------------------------------------------------------ refresh

    def _tick(self) -> None:
        self.service.process_outbox()
        # The periodic tick can fire while the app is tearing down, after the
        # base-screen widgets have been removed; skip rather than raise.
        try:
            self.query_one(NodeTable).render_state(self.state)
            self.query_one(StatsPane).render_state(self.state)
            self._render_status()
        except NoMatches:
            pass

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
        if isinstance(self.link, GatewayLink):
            # Same radio either way; say which process owns it, so "is this
            # the gateway or a direct serial open?" is answered at a glance.
            left.append("via gateway", style="bold bright_magenta")
            left.append(" -> ", style="grey42")
        left.append(where, style="grey62")
        if state.firmware:
            left.append(f"  {state.firmware}", style="grey42")
        radio = state.radio_info
        if radio.get("freq"):
            left.append(f"  {radio['freq']}MHz sf{radio.get('sf')}", style="grey42")
        air = state.stats.airtime_last_hour()
        if air is not None:
            style = "bright_green" if air < 10 else ("yellow" if air < 25 else "red")
            left.append(f"  air 1h {air:.1f}%", style=style)

        if self._status_note and time.time() < self._note_until:
            text, style = self._status_note
            left.append("   ")
            left.append(text, style=style)
        self.query_one("#status", Static).update(left)

    # ------------------------------------------------------------ actions

    def action_focus_input(self) -> None:
        self._open_overlay(focus_input=True)

    def action_command_palette(self) -> None:
        self.push_screen(CommandPalette())

    def _apply_preferences(self) -> None:
        values = self.preferences.get(self.state.protocol)
        self._set_layout(values["layout"], persist=False)
        self._set_theme(values["theme"], persist=False)

    def _set_layout(self, name: str, *, persist: bool = True) -> bool:
        if name not in LAYOUTS:
            return False
        workspace = self.query_one("#workspace", Grid)
        for known in LAYOUTS:
            workspace.remove_class(f"layout-{known}")
        workspace.add_class(f"layout-{name}")
        self.layout_name = name
        if persist:
            self.preferences.update(self.state.protocol, layout=name)
        self.note(f"layout: {name}", "bright_cyan")
        return True

    def _set_theme(self, name: str, *, persist: bool = True) -> bool:
        if name not in THEMES:
            return False
        self.theme = name
        if persist:
            self.preferences.update(self.state.protocol, theme=name)
        self.note(f"theme: {name}", "bright_cyan")
        return True

    def action_cycle_layout(self) -> None:
        current = LAYOUTS.index(self.layout_name) if self.layout_name in LAYOUTS else 0
        self._set_layout(LAYOUTS[(current + 1) % len(LAYOUTS)])

    def action_cycle_theme(self) -> None:
        current = THEMES.index(self.theme) if self.theme in THEMES else 0
        self._set_theme(THEMES[(current + 1) % len(THEMES)])

    def execute_palette(self, line: str) -> bool:
        """Execute one palette command. False keeps the palette open."""
        parts = line.strip().split(maxsplit=2)
        if not parts:
            return False
        command = parts[0].casefold()

        if command in ("node", "jump"):
            if len(parts) < 2:
                self.note("usage: node <name>", "yellow")
                return False
            query = line.split(maxsplit=1)[1].casefold()
            table = self.query_one(NodeTable)
            table.render_state(self.state)
            matches = [i for i, node_id in enumerate(table._row_ids)
                       if query in node_id.casefold()
                       or query in self.state.node_name(node_id).casefold()
                       or query in (self.state.nodes[node_id].long_name or "").casefold()]
            if not matches:
                self.note(f"unknown node: {query}", "yellow")
                return False
            table.move_cursor(row=matches[0])
            table.focus()
            self.note(f"selected {self.state.node_name(table._row_ids[matches[0]])}", "green")
            return True

        if command == "filter":
            feed = self.query_one(PacketFeed)
            if len(parts) < 2:
                self.note("filters: all, no rf log, chatty, text only", "yellow")
                return False
            name = feed.set_filter(line.split(maxsplit=1)[1], self.state)
            if name is None:
                self.note("unknown or ambiguous packet filter", "yellow")
                return False
            feed.border_title = f"packets - {name}"
            feed.focus()
            return True

        if command == "watch":
            if len(parts) < 2:
                self.note("usage: watch proto:mc hop>=3 snr<5 chan:#public", "yellow")
                return False
            expression = line.split(maxsplit=1)[1]
            try:
                name = self.query_one(PacketFeed).set_watch(expression, self.state)
            except ValueError as exc:
                self.note(str(exc), "yellow")
                return False
            self.query_one(PacketFeed).border_title = f"packets - {name}"
            self.query_one(PacketFeed).focus()
            return True

        if command == "view":
            args = line.split(maxsplit=3)
            if len(args) >= 4 and args[1].casefold() == "save":
                name, expression = args[2], args[3]
                try:
                    self.query_one(PacketFeed).set_watch(expression, self.state, name=name)
                except ValueError as exc:
                    self.note(str(exc), "yellow")
                    return False
                self.preferences.save_view(self.state.protocol, name, expression)
                self.query_one(PacketFeed).border_title = f"packets - {name}"
                self.note(f"saved view: {name}", "green")
                return True
            if len(args) >= 3 and args[1].casefold() == "delete":
                if not self.preferences.delete_view(self.state.protocol, args[2]):
                    self.note(f"unknown saved view: {args[2]}", "yellow")
                    return False
                self.note(f"deleted view: {args[2]}", "green")
                return True
            if len(args) < 2 or args[1].casefold() == "list":
                names = ", ".join(sorted(self.preferences.views(self.state.protocol))) or "none"
                self.note(f"saved views: {names}", "grey70")
                return False
            name = args[1]
            expression = self.preferences.views(self.state.protocol).get(name)
            if expression is None:
                self.note(f"unknown saved view: {name}", "yellow")
                return False
            self.query_one(PacketFeed).set_watch(expression, self.state, name=name)
            self.query_one(PacketFeed).border_title = f"packets - {name}"
            self.query_one(PacketFeed).focus()
            return True

        if command == "send":
            if len(parts) < 3:
                self.note("usage: send <node|#channel> <text>", "yellow")
                return False
            target, text = parts[1], parts[2]
            if target.startswith("#"):
                wanted = target[1:].casefold()
                channels = [(index, name) for index, name in self.state.channel_pairs()
                            if str(index) == wanted or name.lstrip("#").casefold() == wanted]
                if len(channels) != 1:
                    self.note(f"unknown channel: {target}", "yellow")
                    return False
                return self._send(text, BROADCAST, channels[0][0])
            node = self.state.resolve(target)
            if node is None:
                self.note(f"unknown node: {target}", "yellow")
                return False
            self.query_one(ChatPane).focus_dm(node.node_id, self.state)
            return self._send(text, node.node_id, 0)

        if command == "trace":
            if len(parts) < 2:
                self.note("usage: trace <node> [hops]", "yellow")
                return False
            args = line.split()
            node = self.state.resolve(args[1])
            if node is None:
                self.note(f"unknown node: {args[1]}", "yellow")
                return False
            try:
                hops = max(1, min(7, int(args[2]))) if len(args) > 2 else 5
            except ValueError:
                self.note("hops must be a number 1-7", "yellow")
                return False
            self._trace(node.node_id, hops)
            return True

        if command in ("login", "admin"):
            if self.state.protocol != "meshcore":
                self.note("remote admin is a MeshCore feature", "yellow")
                return False
            if len(parts) < 2:
                self.note("usage: login <node>", "yellow")
                return False
            node = self.state.resolve(line.split(maxsplit=1)[1])
            if node is None:
                self.note("unknown admin node", "yellow")
                return False
            self.push_screen(AdminScreen(self.state, self.link, target=node.node_id))
            return True

        if command == "scope":
            if self.state.protocol != "meshcore":
                self.note("flood scopes are a MeshCore feature", "yellow")
                return False
            self.push_screen(ScopeScreen(self.state, self.link))
            return True

        if command in ("room", "rooms"):
            if self.state.protocol != "meshcore":
                self.note("room servers are a MeshCore feature", "yellow")
                return False
            self.push_screen(RoomScreen(self.state, self.link, self))
            return True

        if command == "layout":
            return len(parts) >= 2 and self._set_layout(parts[1].casefold())
        if command == "theme":
            return len(parts) >= 2 and self._set_theme(parts[1].casefold())

        self.note(f"unknown palette command: {command}", "yellow")
        return False

    def _open_overlay(self, focus_input: bool = False) -> None:
        if isinstance(self.screen, ChatScreen):
            if focus_input:
                self.screen.focus_input()
            return
        self.push_screen(ChatScreen(self.state, self, focus_input=focus_input))

    def chat_notice(self, text: str, style: str = "grey62",
                    to_feed: bool = False) -> None:
        """Command and traceroute output.

        The chat views are message-driven and rebuilt constantly, so notices
        cannot live there. Multi-line output (help, node lists, a traceroute
        breakdown) goes to the packet feed, which is append-only and scrolls;
        short feedback goes to the transient status line.
        """
        if to_feed:
            self.query_one(PacketFeed).write_notice(text, style)
        elif text.strip():
            self.note(text.strip(), style)

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
        feed = self.query_one(PacketFeed)
        packet = feed.selected_packet() or feed.latest_packet()
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

    def action_show_paths(self) -> None:
        self._load_path_history()
        self.push_screen(PathScreen(self.state))

    def _load_path_history(self) -> None:
        """Backfill persisted path observations, once.

        Live observations accumulate from the packet stream in both modes;
        history comes from our own store, or over the socket when a gateway
        owns the database.
        """
        if getattr(self, "_paths_loaded", False):
            return
        self._paths_loaded = True
        if self.store is not None and self.store.enabled:
            for obs in self.store.recent_paths():
                self.state.note_path(obs)
            return
        if self.gateway is None:
            return

        def fetch() -> None:
            from .gateway import request_gateway
            from .pathcalc import PathObservation
            try:
                result = request_gateway({"command": "paths", "limit": 2000},
                                         self.gateway or None, timeout=10.0)
            except Exception:  # noqa: BLE001 - explorer just starts empty
                return
            rows = result.get("paths") or []
            fields = {"ts", "kind", "origin_id", "origin_name", "path",
                      "hops", "snr", "rssi", "channel"}
            def apply() -> None:
                for row in rows:
                    if isinstance(row, dict):
                        try:
                            self.state.note_path(PathObservation(
                                **{k: v for k, v in row.items() if k in fields}))
                        except TypeError:
                            continue
            self.call_from_thread(apply)

        threading.Thread(target=fetch, name="paths-history", daemon=True).start()

    def action_show_sensors(self) -> None:
        self.push_screen(SensorScreen(self.state))

    def action_fix_autoadd(self) -> None:
        if self.state.protocol != "meshcore" or self.link is None:
            self.note("MeshCore only", "yellow")
            return
        self.link.set_autoadd()
        self.note("contact auto-add enabled - peers will be stored as they advert",
                  "green")

    def action_send_advert(self) -> None:
        if self.state.protocol != "meshcore" or self.link is None:
            self.note("MeshCore only", "yellow")
            return
        self.link.send_advert(flood=True)

    def action_expand_chat(self) -> None:
        self._open_overlay()

    def on_open_chat_overlay(self, event: OpenChatOverlay) -> None:
        self.action_expand_chat()

    def goto_channel(self, index: int) -> None:
        """Jump to a channel in the overlay, used by the channel browser."""
        if self.query_one(ChatPane).goto_channel(index, self.state):
            self._open_overlay()
        else:
            self.note(f"channel {index} is not on this radio", "yellow")

    def action_show_channels(self) -> None:
        self.push_screen(ChannelScreen(self.state, self.link))

    def action_show_admin(self) -> None:
        if self.state.protocol != "meshcore":
            self.note("remote admin is a MeshCore feature", "yellow")
            return
        self.push_screen(AdminScreen(self.state, self.link))

    def action_node_detail(self) -> None:
        node = self._selected_node()
        if node is not None:
            self.push_screen(NodeDetail(node))

    def action_dm_selected(self) -> None:
        node = self._selected_node()
        if node is None:
            self.note("select a node first", "yellow")
            return
        if node.is_self or node.node_id == self.state.my_node_id:
            # The cursor starts on our own row, and similarly-named nodes
            # (Field Base vs Field Mobile) make this an easy misfire that
            # used to end in a cryptic 3-attempt delivery failure.
            self.note("that's this radio - pick the node you want to DM", "yellow")
            return
        self.query_one(ChatPane).focus_dm(node.node_id, self.state)
        self._open_overlay(focus_input=True)

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
        self.chat_notice(
            f"traceroute to {self.state.node_name(node_id)} sent"
            f"{' (direct link only)' if hop_limit <= 1 else f' (up to {hop_limit} hops)'}"
            f" - the reply can take 30s or more", "grey62")
        # The chat pane may be scrolled away or covered; say it on the
        # status line too so the keypress visibly did something.
        self.note(f"traceroute sent to {self.state.node_name(node_id)}"
                  " - reply can take 30s+", "cyan")
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

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if isinstance(event.data_table, PacketFeed):
            packet = event.data_table.selected_packet()
            self.query_one(PacketWorkbench).show_packet(packet, self.state)
            self.query_one(RoutePane).show_packet(packet)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Only ever transmit what was typed into the chat box. Other screens
        # have their own inputs whose submissions bubble up here, and treating
        # those as chat would broadcast them to the mesh.
        if event.input.id != "chat-input":
            return
        if self._submit_chat(event.value):
            event.input.value = ""
            self.query_one(ChatPane).update_counter("")

    @property
    def max_payload(self) -> int:
        return self.query_one(ChatPane).max_bytes

    def send_from_overlay(self, text: str, route_mode: str = "auto",
                          path_hash_size: int | None = None) -> bool:
        """Send path for the pop-out overlay's input."""
        return self._submit_chat(text, route_mode=route_mode,
                                 path_hash_size=path_hash_size)

    def send_to_room(self, node_id: str, text: str) -> bool:
        """Post through the same durable DM path used by ordinary chat."""
        node = self.state.nodes.get(node_id)
        if self.state.protocol != "meshcore" or node is None or node.role != "ROOM":
            self.note("selected contact is not a MeshCore room server", "yellow")
            return False
        self.query_one(ChatPane).focus_dm(node_id, self.state)
        return self._send(text, node_id, 0)

    def action_show_rooms(self) -> None:
        if self.state.protocol != "meshcore":
            self.note("room servers are a MeshCore feature", "yellow")
            return
        self.push_screen(RoomScreen(self.state, self.link, self))

    def _submit_chat(self, raw: str, route_mode: str = "auto",
                     path_hash_size: int | None = None) -> bool:
        """Handle a line of chat input. Returns True if it was consumed.

        The single entry point for both the corner input and the overlay, so
        the length limit, slash commands and the send destination behave
        identically wherever a message is typed.
        """
        text = raw.strip()
        if not text:
            return False
        payload = outgoing_payload(text)
        if payload is not None:
            used = payload_bytes(payload)
            limit = self.query_one(ChatPane).max_bytes
            if used > limit:
                self.chat_notice(
                    f"message is {used} bytes, {used - limit} over the "
                    f"{limit}-byte mesh limit - shorten it and send again",
                    "bold red",
                )
                self.note(f"too long: {used}/{limit} bytes", "red")
                return False  # keep the text so it can be trimmed

        if text.startswith("/"):
            self._command(text)
            return True

        active = self.active_chat_target()
        kind = active[0]
        if kind == "all":
            self.chat_notice("select a channel or direct-message conversation before sending",
                             "yellow")
            return False
        target = active[1]
        return self._send(text, target if kind == "dm" else BROADCAST,
                          0 if kind == "dm" else int(target),
                          route_mode=route_mode,
                          path_hash_size=path_hash_size)  # type: ignore[arg-type]

    def active_chat_target(self) -> tuple[str, Any]:
        return self.query_one(ChatPane).active_target()

    # ----------------------------------------------------------- commands

    def _command(self, line: str) -> None:
        parts = line.split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd in ("/help", "/?"):
            for row in HELP.splitlines():
                self.chat_notice("  " + row, to_feed=True)
        elif cmd == "/clear":
            self.query_one(PacketFeed).clear_feed()
        elif cmd == "/nodes":
            for node in self.state.sorted_nodes("heard"):
                self.chat_notice(f"  {node.node_id}  {node.label:<6} {node.long_name}",
                                 to_feed=True)
        elif cmd == "/dm":
            if len(parts) < 3:
                self.chat_notice("usage: /dm <node> <text>", "yellow")
                return
            node, rest, problem = self._resolve_target(f"{parts[1]} {parts[2]}")
            if node is None or not rest:
                self.chat_notice(problem or "usage: /dm <node> <text>", "yellow")
                return
            self.query_one(ChatPane).focus_dm(node.node_id, self.state)
            self._send(rest, node.node_id, 0)
        elif cmd == "/trace":
            if len(parts) < 2:
                self.chat_notice("usage: /trace <node> [maxhops]  "
                                 "(1 tests only the direct link)", "yellow")
                return
            node, rest, problem = self._resolve_target(
                " ".join(p for p in parts[1:] if p))
            if node is None:
                self.chat_notice(problem or f"unknown node: {parts[1]}", "yellow")
                return
            hop_limit = 5
            if rest:
                try:
                    hop_limit = max(1, min(7, int(rest.split()[0])))
                except ValueError:
                    self.chat_notice("maxhops must be a number 1-7", "yellow")
                    return
            self._trace(node.node_id, hop_limit)
        else:
            self.chat_notice(f"unknown command {cmd} - try /help", "yellow")

    def _resolve_target(self, remainder: str) -> tuple[Any, str, str | None]:
        """Match the longest leading node name in `remainder`.

        Returns (node, rest-of-string, problem). Multi-word names work
        ('/dm Field Mobile hi'), our own radio is never a target, and an
        ambiguous token is reported with ids instead of silently taking the
        first match. When several nodes share a name, one
        heard recently beats ghosts not heard in a week.
        """
        tokens = remainder.split()
        for cut in range(min(len(tokens), 5), 0, -1):
            token = " ".join(tokens[:cut])
            matches = self.state.resolve_all(token)
            if not matches:
                continue
            rest = " ".join(tokens[cut:])
            others = [n for n in matches
                      if not n.is_self and n.node_id != self.state.my_node_id]
            if not others:
                return None, rest, "that's this radio - pick another node"
            if len(others) > 1:
                week_ago = time.time() - 7 * 86400
                alive = [n for n in others
                         if n.last_heard and n.last_heard > week_ago]
                if len(alive) == 1:
                    return alive[0], rest, None
                names = ", ".join(f"{n.long_name or '?'} ({n.node_id})"
                                  for n in others[:4])
                return None, rest, f"'{token}' is ambiguous: {names}"
            return others[0], rest, None
        first = tokens[0] if tokens else ""
        return None, "", f"unknown node: {first}"

    def _send(self, text: str, dest: str, channel: int,
              route_mode: str = "auto",
              path_hash_size: int | None = None) -> bool:
        if self.protocol == "auto" and not self.state.connected:
            self.note("protocol detection has not finished; use --protocol for offline queueing",
                      "yellow")
            return False
        destination = (PeerRef(self.state.protocol, dest, None,
                               route_mode, path_hash_size) if dest != BROADCAST
                       else ChannelRef(self.state.protocol, channel,
                                       self.state.channel_name(channel)))
        receipt = self.service.send_message(text, destination)
        if receipt.status == DeliveryStatus.FAILED:
            self.note(receipt.detail or "send failed", "red")
        elif not self.state.connected:
            self.note("radio offline - message queued for retry", "yellow")
        chat = self.query_one(ChatPane)
        chat.rerender(self.state)
        self._refresh_overlay()
        return receipt.message_id in self.service.outbox

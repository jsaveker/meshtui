"""Protocol-neutral mesh service shared by the TUI and headless gateway.

The service owns normalized state, durable outbound intent, retries, and delivery
receipts.  UI code may render its events, while an unattended gateway can use the
same paths without importing Textual or competing for the serial port.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .meshcore_link import contact_to_node
from .pathcalc import obs_from_packet
from .model import (
    BROADCAST,
    ChannelRef,
    ChatMessage,
    DeliveryStatus,
    DestinationRef,
    PeerRef,
    SendReceipt,
    payload_bytes,
)
from .radio import RadioLink, protocol_payload_limit
from .state import LocalChannel, MeshState
from .store import Store


ServiceListener = Callable[[str, Any], None]


@dataclass
class OutboundMessage:
    message_id: str
    destination: DestinationRef
    text: str
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)
    status: DeliveryStatus = DeliveryStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    next_attempt_ts: float | None = None
    expires_ts: float | None = None
    protocol_id: int | str | None = None
    error: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in (DeliveryStatus.DELIVERED, DeliveryStatus.EXPIRED) or (
            self.status == DeliveryStatus.SENT and isinstance(self.destination, ChannelRef)) or (
            self.status == DeliveryStatus.FAILED and self.attempts >= self.max_attempts)

    def to_row(self) -> dict[str, Any]:
        channel = self.destination if isinstance(self.destination, ChannelRef) else None
        peer = self.destination if isinstance(self.destination, PeerRef) else None
        return {
            "message_id": self.message_id,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "protocol": self.destination.protocol,
            "destination_kind": "channel" if channel else "peer",
            "target": peer.node_id if peer else str(channel.index),
            "channel_index": channel.index if channel else None,
            "channel_name": channel.name if channel else None,
            "public_key": peer.public_key if peer else None,
            "text": self.text,
            "status": self.status.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "next_attempt_ts": self.next_attempt_ts,
            "expires_ts": self.expires_ts,
            "protocol_id": None if self.protocol_id is None else str(self.protocol_id),
            "error": self.error or None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "OutboundMessage":
        if row.get("destination_kind") == "channel":
            destination: DestinationRef = ChannelRef(
                row["protocol"], int(row.get("channel_index") or 0),
                row.get("channel_name") or "")
        else:
            destination = PeerRef(row["protocol"], row.get("target") or "",
                                  row.get("public_key"))
        return cls(
            message_id=row["message_id"], destination=destination, text=row.get("text") or "",
            created_ts=float(row.get("created_ts") or time.time()),
            updated_ts=float(row.get("updated_ts") or time.time()),
            status=DeliveryStatus(row.get("status") or DeliveryStatus.QUEUED.value),
            attempts=int(row.get("attempts") or 0),
            max_attempts=int(row.get("max_attempts") or 3),
            next_attempt_ts=row.get("next_attempt_ts"), expires_ts=row.get("expires_ts"),
            protocol_id=row.get("protocol_id"), error=row.get("error") or "",
        )


class MeshService:
    """Own mesh state and reliable outbound messaging independently of a UI."""

    def __init__(self, store: Store | None = None, *, retry_seconds: float = 30.0,
                 default_ttl: float = 24 * 3600) -> None:
        self.state = MeshState()
        self.store = store
        self.link: RadioLink | None = None
        self.retry_seconds = retry_seconds
        self.default_ttl = default_ttl
        self.outbox: dict[str, OutboundMessage] = {}
        self._protocol_to_message: dict[str, str] = {}
        # pkt_hash -> our outgoing message_id, so every repeat of the same
        # packet accumulates onto the one message.
        self._repeat_pkt_to_message: dict[int, str] = {}
        self._listeners: list[ServiceListener] = []
        self._lock = threading.RLock()
        self._load_outbox()

    # ------------------------------------------------------------ lifecycle

    @property
    def lock(self) -> threading.RLock:
        """The lock every event and send path holds while mutating state.

        Holding it makes a multi-part snapshot atomic: no event can be
        processed (and no listener notified) part-way through.
        """
        return self._lock

    def attach_link(self, link: RadioLink | None) -> None:
        self.link = link

    def add_listener(self, listener: ServiceListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: ServiceListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, kind: str, payload: Any) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(kind, payload)
            except Exception:
                # Observers must not be able to kill the radio callback path.
                continue

    def restore(self) -> None:
        """Restore durable facts/chat for a headless process."""
        if self.store is None or not self.store.enabled:
            return
        for record in self.store.known_nodes():
            record.pop("_first_seen", None)
            record.pop("_packets", None)
            record.pop("_derived", None)
            try:
                self.state.upsert_node(record)
            except ValueError:
                continue
        for message in self.store.recent_messages():
            self.state.add_chat(message)
            if message.is_dm:
                other = message.to_id if message.outgoing else message.from_id
                self.state.dm_contacts.add(other)
        for obs in self.store.recent_paths():
            self.state.note_path(obs)

    # ------------------------------------------------------------- inbound

    def handle_event(self, kind: str, payload: Any) -> Any:
        """Radio callback entrypoint for the headless gateway."""
        with self._lock:
            result: Any = payload
            if kind == "packet":
                result = self.receive_packet(payload)
            elif kind == "node":
                result = self.receive_node(payload)
            elif kind == "chat":
                result = self.receive_chat(payload)
            elif kind == "connected":
                self.connected(payload)
            elif kind == "lost":
                self.state.connected = False
            elif kind == "ack":
                result = self.ack_protocol(payload)
            elif kind == "receipt":
                result = self.apply_receipt(payload)
            elif kind == "mc_contact":
                result = self.receive_contact(payload)
            elif kind == "mc_channels":
                self.state.channels = list(payload) or [(0, "Public")]
            elif kind == "mc_autoadd":
                self.state.radio_info["autoadd"] = payload
            elif kind == "mc_repeat":
                result = self.note_repeat(payload)
            elif kind == "mc_status":
                node_id, data = payload
                self.note_status(node_id, data)
            elif kind == "mc_radio_stats":
                # Local RF statistics ride in radio_info so they reach gateway
                # clients through the connected snapshot as well as live.
                self.state.radio_info.update(payload)
            elif kind == "mc_login":
                node_id, ok = payload
                if ok:
                    self.state.admin_sessions.add(node_id)
                else:
                    self.state.admin_sessions.discard(node_id)
            self._notify(kind, result)
            return result

    REPEAT_WINDOW = 45.0  # seconds a repeat may lag our send before we ignore it

    def note_repeat(self, info: dict[str, Any]):
        """Attribute a heard rebroadcast to one of our sent channel messages.

        pkt_hash is stable across every repeat of a packet, so the first repeat
        anchors the association and later repeats (from other repeaters) just
        add to the set. Before that anchor we match on channel + a short time
        window, which is safe because we are only ever matching against our own
        recently-sent messages.
        """
        pkt = info.get("pkt_hash")
        path = [b for b in (info.get("path") or []) if b]
        if not path:
            return None
        chan_hash = info.get("chan_hash") or ""
        now = info.get("ts") or time.time()

        msg = None
        mid = self._repeat_pkt_to_message.get(pkt) if pkt is not None else None
        if mid is not None:
            msg = self._find_message(mid)
        if msg is None:
            msg = self._match_outgoing_channel(chan_hash, now)
            if msg is not None and pkt is not None:
                self._repeat_pkt_to_message[pkt] = msg.message_id
                msg.repeat_pkt = pkt
        if msg is None:
            return None

        before = len(msg.repeated_by)
        for byte in path:
            msg.repeated_by.add(self._repeater_label(byte))
        if len(msg.repeated_by) != before:
            self._notify("chat", msg)
        return msg

    def _find_message(self, message_id: str):
        for m in reversed(self.state.chat):
            if m.message_id == message_id:
                return m
        return None

    def _channel_hash(self, index: int) -> str:
        link = self.link
        hashes = getattr(link, "channel_hashes", None) if link else None
        return (hashes or {}).get(index, "")

    def _match_outgoing_channel(self, chan_hash: str, now: float):
        """Most recent outgoing channel message the repeat could belong to.

        When the repeat names a channel hash and we know our channels' hashes,
        require an exact match - never attribute a repeat to a message on a
        different (or unconfirmable) channel. Only when we have no hashes at all
        do we fall back to the time window alone.
        """
        link = self.link
        known = bool(getattr(link, "channel_hashes", None)) if link else False
        for m in reversed(self.state.chat):
            if not m.outgoing or m.is_dm:
                continue
            if now - m.ts > self.REPEAT_WINDOW:
                break  # chat is time-ordered; nothing older will match
            # Already tied to a packet: only that exact pkt_hash may add to it
            # (handled by the caller), so a different packet is not ours.
            if m.repeat_pkt is not None:
                continue
            if chan_hash and known and self._channel_hash(m.channel) != chan_hash:
                continue
            return m
        return None

    def _repeater_label(self, byte: str) -> str:
        """A path byte is the first byte of a repeater's public key, and a
        node id is '!' + the key's first four bytes - so match on that byte.

        Hundreds of nodes share any single byte, so first-match attribution
        regularly credited a rebroadcast to some contact three states away.
        Rank the candidates by what physics allows - only repeaters and room
        servers rebroadcast, and a recently-heard one beats a long-silent one
        - and when more than one repeater shares the byte, say so with a '?'
        rather than pretending certainty."""
        wanted = byte.lower()
        candidates = [n for n in self.state.nodes.values()
                      if n.node_id[1:3].lower() == wanted]
        if not candidates:
            return f"0x{byte}"
        def plausible(n) -> bool:
            return (n.role or "").upper() in ("REP", "ROOM")
        best = min(candidates, key=lambda n: (not plausible(n),
                                              -(n.last_heard or 0.0)))
        rebroadcasters = sum(1 for n in candidates if plausible(n))
        certain = rebroadcasters == 1 and plausible(best)
        return best.name if certain or len(candidates) == 1 else f"{best.name}?"

    def receive_node(self, raw: dict[str, Any]):
        try:
            node = self.state.upsert_node(raw)
        except ValueError:
            return None
        if self.store is not None:
            self.store.save_node(node)
        return node

    def receive_contact(self, contact: dict[str, Any]):
        return self.receive_node(contact_to_node(contact))

    def note_status(self, node_id: str, data: dict[str, Any]) -> None:
        """A repeater's status reply carries its battery in millivolts and its
        uptime - the only battery data MeshCore offers for a remote node."""
        metrics: dict[str, Any] = {}
        bat = data.get("bat")
        if isinstance(bat, (int, float)) and bat > 0:
            metrics["voltage"] = round(bat / 1000, 2)
        if isinstance(data.get("uptime"), (int, float)):
            metrics["uptimeSeconds"] = data["uptime"]
        if metrics and str(node_id).startswith("!"):
            self.receive_node({"id": node_id, "deviceMetrics": metrics})

    def receive_packet(self, packet):
        # "!00000000" is key_to_id's placeholder for a sender the radio could
        # not identify (RX-log entries without a pubkey prefix); it must not
        # become a phantom node collecting packet counts.
        if (packet.from_id not in self.state.nodes
                and packet.from_id.startswith("!") and packet.from_id != "!00000000"):
            self.receive_node({"id": packet.from_id, "num": packet.raw.get("from")})
        self.state.add_packet(packet)
        obs = obs_from_packet(packet)
        if obs is not None:
            _, is_new = self.state.note_path(obs)
            if is_new and self.store is not None:
                self.store.add_path(obs)
        if self.store is not None:
            self.store.add_packet(packet)
        return packet

    def receive_chat(self, message: ChatMessage) -> ChatMessage:
        message.from_name = self.state.node_name(message.from_id)
        self.state.add_chat(message)
        if message.is_dm and not message.outgoing:
            self.state.dm_contacts.add(message.from_id)
        self.state.note_incoming(message)
        if self.store is not None:
            self.store.add_message(message)
        return message

    def connected(self, info: dict[str, Any]) -> None:
        state = self.state
        state.connected = True
        state.my_node_id = info.get("my_node_id")
        state.my_node_name = info.get("my_node_name") or ""
        state.firmware = info.get("firmware") or ""
        state.device_path = info.get("device") or ""
        state.protocol = info.get("protocol", "meshtastic")
        state.radio_info = dict(info.get("radio") or {})
        state.channels = list(info.get("channels") or [(0, "LongFast")])
        state.max_channels = int(info.get("max_channels") or 8)
        state.local_channels = [
            LocalChannel(index=c.get("index", i), name=c.get("name", f"ch{i}"),
                         level=c.get("level", "UNKNOWN"), detail=c.get("detail", ""),
                         hash=c.get("hash"))
            for i, c in enumerate(info.get("channel_security") or [])
        ]
        if state.my_node_id:
            try:
                me = state.upsert_node({
                    "num": int(state.my_node_id.lstrip("!"), 16),
                    "user": {"id": state.my_node_id,
                             "longName": state.my_node_name or "this radio",
                             "shortName": (state.my_node_name or "self")[:4],
                             "hwModel": state.protocol.upper()},
                })
                me.is_self = True
                me.last_heard = time.time()
            except (ValueError, AttributeError):
                pass
        if self.store is not None and self.store.enabled:
            self.store.local_node = state.my_node_id

    # ------------------------------------------------------------- outbound

    def send_message(self, text: str, destination: DestinationRef, *,
                     message_id: str | None = None, max_attempts: int = 3,
                     ttl: float | None = None, record_chat: bool = True) -> SendReceipt:
        now = time.time()
        message_id = message_id or uuid.uuid4().hex
        limit = protocol_payload_limit(destination.protocol)
        if payload_bytes(text) > limit:
            return SendReceipt(message_id, destination, DeliveryStatus.FAILED,
                               detail=f"message is {payload_bytes(text)} bytes; limit is {limit}")
        outbound = OutboundMessage(
            message_id=message_id, destination=destination, text=text,
            max_attempts=max(1, max_attempts), next_attempt_ts=now,
            expires_ts=now + (self.default_ttl if ttl is None else ttl),
        )
        with self._lock:
            self.outbox[message_id] = outbound
            self._persist_outbound(outbound)
            if record_chat:
                channel = destination.index if isinstance(destination, ChannelRef) else -1
                to_id = BROADCAST if isinstance(destination, ChannelRef) else destination.node_id
                message = ChatMessage(
                    ts=now, from_id=self.state.my_node_id or "!me", from_name="you",
                    to_id=to_id, text=text, channel=channel, outgoing=True,
                    message_id=message_id, delivery_status=DeliveryStatus.QUEUED.value,
                )
                self.state.add_chat(message)
                self.state.stats.sent += 1
                if isinstance(destination, PeerRef):
                    self.state.dm_contacts.add(destination.node_id)
                if self.store is not None:
                    self.store.add_message(message)
                self._notify("chat", message)
            if self.state.connected and self.link is not None:
                return self._attempt(outbound)
        return SendReceipt(message_id, destination, DeliveryStatus.QUEUED,
                           detail="queued until the radio is connected")

    def _attempt(self, outbound: OutboundMessage) -> SendReceipt:
        now = time.time()
        if outbound.expires_ts is not None and now >= outbound.expires_ts:
            receipt = SendReceipt(outbound.message_id, outbound.destination,
                                  DeliveryStatus.EXPIRED, detail="message expired")
            return self.apply_receipt(receipt)
        if outbound.attempts >= outbound.max_attempts:
            receipt = SendReceipt(outbound.message_id, outbound.destination,
                                  DeliveryStatus.FAILED, detail="retry limit reached")
            return self.apply_receipt(receipt)
        if self.link is None or not self.state.connected:
            outbound.next_attempt_ts = now + self.retry_seconds
            self._persist_outbound(outbound)
            return SendReceipt(outbound.message_id, outbound.destination,
                               DeliveryStatus.QUEUED, detail="radio unavailable")
        outbound.attempts += 1
        outbound.updated_ts = now
        try:
            if hasattr(self.link, "send"):
                receipt = self.link.send(outbound.text, outbound.destination,
                                         outbound.message_id)
            else:  # small test/fake links and legacy third-party adapters
                if isinstance(outbound.destination, PeerRef):
                    dest, channel = outbound.destination.node_id, 0
                else:
                    dest, channel = BROADCAST, outbound.destination.index
                accepted, protocol_id = self.link.send_text(
                    outbound.text, dest=dest, channel=channel)
                receipt = SendReceipt(
                    outbound.message_id, outbound.destination,
                    DeliveryStatus.SENT if accepted else DeliveryStatus.FAILED,
                    protocol_id=protocol_id,
                    detail="accepted by radio" if accepted else "radio rejected message")
        except Exception as exc:  # noqa: BLE001
            receipt = SendReceipt(outbound.message_id, outbound.destination,
                                  DeliveryStatus.FAILED, detail=str(exc))
        return self.apply_receipt(receipt)

    def apply_receipt(self, receipt: SendReceipt) -> SendReceipt:
        with self._lock:
            outbound = self.outbox.get(receipt.message_id)
            if outbound is None:
                # Not ours to retry (e.g. a gateway client hearing about another
                # client's send), but a rendered copy still deserves the status.
                self._update_chat(receipt)
                return receipt
            if outbound.status in (DeliveryStatus.DELIVERED, DeliveryStatus.EXPIRED) \
                    and receipt.status != outbound.status:
                # Async link callbacks can complete out of order. End-to-end
                # delivery and expiry are terminal and must never be downgraded
                # by a late `sent` or `failed` result from another attempt.
                return SendReceipt(
                    outbound.message_id, outbound.destination, outbound.status,
                    protocol_id=outbound.protocol_id, detail=outbound.error,
                    updated_ts=outbound.updated_ts,
                )
            # MeshCore ACK handlers only need to carry message_id; restore the
            # durable destination so listeners see the actual peer.
            receipt.destination = outbound.destination
            outbound.status = receipt.status
            outbound.updated_ts = receipt.updated_ts
            if receipt.protocol_id is not None:
                outbound.protocol_id = receipt.protocol_id
            outbound.error = receipt.detail if receipt.status == DeliveryStatus.FAILED else ""
            if outbound.protocol_id is not None and isinstance(outbound.destination, PeerRef):
                self._protocol_to_message[str(outbound.protocol_id).lower()] = outbound.message_id
            if receipt.status == DeliveryStatus.SENT and isinstance(outbound.destination, PeerRef):
                outbound.next_attempt_ts = time.time() + self.retry_seconds * (2 ** max(0, outbound.attempts - 1))
            elif receipt.status == DeliveryStatus.FAILED and outbound.attempts < outbound.max_attempts:
                outbound.next_attempt_ts = time.time() + self.retry_seconds * (2 ** max(0, outbound.attempts - 1))
            elif receipt.status == DeliveryStatus.QUEUED:
                # Async links acknowledge submission before the radio reports a
                # protocol id. Leave a recovery deadline so a process crash in
                # that window cannot strand this row forever.
                outbound.next_attempt_ts = time.time() + self.retry_seconds
            else:
                outbound.next_attempt_ts = None
            self._persist_outbound(outbound)
            self._update_chat(receipt)
            self._notify("receipt", receipt)
            return receipt

    def ack_protocol(self, protocol_id: int | str) -> SendReceipt | None:
        token = str(protocol_id).lower()
        message_id = self._protocol_to_message.get(token)
        if message_id is None:
            # Compatibility with early service rows, but never interpret a
            # channel routing response as end-to-end delivery.
            message = next((item for item in reversed(self.state.chat)
                            if isinstance(protocol_id, int)
                            and item.packet_id == protocol_id
                            and item.outgoing and item.is_dm), None)
            message_id = message.message_id if message is not None else None
        if message_id is None:
            return None
        outbound = self.outbox.get(message_id)
        if outbound is None:
            return None
        if isinstance(outbound.destination, ChannelRef):
            # A routing response for a broadcast proves, at most, local-radio
            # handling. It cannot establish receipt by every channel member.
            return None
        return self.apply_receipt(SendReceipt(
            message_id, outbound.destination, DeliveryStatus.DELIVERED,
            protocol_id=protocol_id, detail="mesh acknowledgement received"))

    def process_outbox(self, now: float | None = None) -> list[SendReceipt]:
        now = time.time() if now is None else now
        receipts = []
        with self._lock:
            pending = list(self.outbox.values())
        for outbound in pending:
            if outbound.terminal:
                continue
            if outbound.expires_ts is not None and now >= outbound.expires_ts:
                receipts.append(self.apply_receipt(SendReceipt(
                    outbound.message_id, outbound.destination, DeliveryStatus.EXPIRED,
                    detail="message expired before delivery")))
            elif outbound.next_attempt_ts is not None and now >= outbound.next_attempt_ts:
                # Keep a due item due while the radio is absent. Moving its
                # deadline forward on every offline poll makes a freshly
                # reconnected gateway wait another full retry interval.
                if self.link is not None and self.state.connected:
                    receipts.append(self._attempt(outbound))
        return receipts

    def delivery_snapshot(self, message_id: str) -> dict[str, Any] | None:
        """Return stable delivery evidence without exposing mutable outbox state."""
        def snapshot(outbound: OutboundMessage) -> dict[str, Any]:
            return {
                "message_id": outbound.message_id,
                "status": outbound.status.value,
                "attempts": outbound.attempts,
                "max_attempts": outbound.max_attempts,
                "terminal": outbound.terminal,
                "protocol_id": outbound.protocol_id,
                "detail": outbound.error,
                "updated_ts": outbound.updated_ts,
            }

        with self._lock:
            outbound = self.outbox.get(message_id)
            if outbound is not None:
                return snapshot(outbound)
        if outbound is None and self.store is not None and self.store.enabled:
            row = self.store.get_outbound(message_id)
            if row is not None:
                try:
                    outbound = OutboundMessage.from_row(row)
                except (KeyError, TypeError, ValueError):
                    return None
        if outbound is None:
            return None
        return snapshot(outbound)

    # ----------------------------------------------------------- persistence

    def _persist_outbound(self, outbound: OutboundMessage) -> None:
        if self.store is not None and self.store.enabled:
            self.store.save_outbound(outbound.to_row())

    def _load_outbox(self) -> None:
        if self.store is None or not self.store.enabled:
            return
        for row in self.store.load_outbox():
            try:
                outbound = OutboundMessage.from_row(row)
            except (KeyError, TypeError, ValueError):
                continue
            self.outbox[outbound.message_id] = outbound
            if outbound.protocol_id is not None and isinstance(outbound.destination, PeerRef):
                self._protocol_to_message[str(outbound.protocol_id).lower()] = outbound.message_id

    def _update_chat(self, receipt: SendReceipt) -> None:
        packet_id = receipt.protocol_id if isinstance(receipt.protocol_id, int) else None
        for message in reversed(self.state.chat):
            if message.message_id == receipt.message_id:
                message.delivery_status = receipt.status.value
                message.acked = receipt.status == DeliveryStatus.DELIVERED
                if packet_id is not None:
                    message.packet_id = packet_id
                break
        if self.store is not None:
            self.store.update_message_delivery(receipt.message_id, receipt.status.value, packet_id)

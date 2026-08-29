"""Wire encoding for gateway event streaming.

The gateway broadcasts every service event to subscribed socket clients as one
JSON line: {"event": kind, "payload": ...}. These helpers turn the typed
payloads (Packet, ChatMessage, SendReceipt, Node) into JSON-able dicts and back,
so a GatewayLink can replay them through the same emit(kind, payload) callback
a real radio link uses.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .model import (ChannelRef, ChatMessage, DeliveryStatus, DestinationRef,
                    Node, Packet, PeerRef, SendReceipt)
from .state import MeshState

# Emitted with a payload that duplicates another notification ("ack" and
# "mc_repeat" both follow a "receipt"/"chat" for the same change), so
# broadcasting them would only make clients process every change twice.
SKIP_EVENTS = ("ack", "mc_repeat")

# MeshCore payloads that travel as tuples and are unpacked as such by the TUI.
TUPLE_EVENTS = ("mc_login", "mc_cli", "mc_status", "mc_telemetry", "mc_neighbours")


def packet_to_dict(packet: Packet) -> dict[str, Any]:
    return dataclasses.asdict(packet)


def packet_from_dict(data: dict[str, Any]) -> Packet:
    fields = {f.name for f in dataclasses.fields(Packet)}
    return Packet(**{k: v for k, v in data.items() if k in fields})


def chat_to_dict(message: ChatMessage) -> dict[str, Any]:
    data = dataclasses.asdict(message)
    data["repeated_by"] = sorted(message.repeated_by)
    return data


def chat_from_dict(data: dict[str, Any]) -> ChatMessage:
    fields = {f.name for f in dataclasses.fields(ChatMessage)}
    kwargs = {k: v for k, v in data.items() if k in fields}
    kwargs["repeated_by"] = set(kwargs.get("repeated_by") or ())
    return ChatMessage(**kwargs)


def destination_to_dict(destination: DestinationRef) -> dict[str, Any]:
    if isinstance(destination, ChannelRef):
        return {"kind": "channel", "protocol": destination.protocol,
                "index": destination.index, "name": destination.name}
    return {"kind": "peer", "protocol": destination.protocol,
            "node_id": destination.node_id, "public_key": destination.public_key}


def destination_from_dict(data: dict[str, Any]) -> DestinationRef:
    if data.get("kind") == "channel":
        return ChannelRef(data.get("protocol") or "", int(data.get("index") or 0),
                          data.get("name") or "")
    return PeerRef(data.get("protocol") or "", data.get("node_id") or "",
                   data.get("public_key"))


def receipt_to_dict(receipt: SendReceipt) -> dict[str, Any]:
    return {
        "message_id": receipt.message_id,
        "destination": destination_to_dict(receipt.destination),
        "status": receipt.status.value,
        "protocol_id": receipt.protocol_id,
        "detail": receipt.detail,
        "updated_ts": receipt.updated_ts,
    }


def receipt_from_dict(data: dict[str, Any]) -> SendReceipt:
    return SendReceipt(
        message_id=str(data.get("message_id") or ""),
        destination=destination_from_dict(data.get("destination") or {}),
        status=DeliveryStatus(data.get("status") or DeliveryStatus.QUEUED.value),
        protocol_id=data.get("protocol_id"),
        detail=data.get("detail") or "",
        updated_ts=float(data.get("updated_ts") or 0.0),
    )


def node_record(node: Node) -> dict[str, Any]:
    """A NodeDB-shaped record for MeshState.upsert_node, like Store.known_nodes.

    upsert_node only overwrites fields that are present, so None values are
    left out rather than sent as explicit nulls.
    """
    record: dict[str, Any] = {
        "num": node.num,
        "user": {"id": node.node_id, "longName": node.long_name,
                 "shortName": node.short_name, "hwModel": node.hw_model,
                 "role": node.role},
    }
    for src, dst in (("snr", "snr"), ("hops", "hopsAway"), ("last_heard", "lastHeard")):
        value = getattr(node, src)
        if value is not None:
            record[dst] = value
    if node.via_mqtt:
        record["viaMqtt"] = True
    if node.lat is not None and node.lon is not None:
        record["position"] = {"latitude": node.lat, "longitude": node.lon,
                              "altitude": node.alt}
    metrics = {key: value for key, value in (
        ("batteryLevel", node.battery), ("voltage", node.voltage),
        ("channelUtilization", node.ch_util), ("airUtilTx", node.air_util),
        ("uptimeSeconds", node.uptime),
    ) if value is not None}
    if metrics:
        record["deviceMetrics"] = metrics
    return record


def connected_info(state: MeshState) -> dict[str, Any]:
    """Rebuild the dict a link's "connected" event carries, from live state."""
    return {
        "my_node_id": state.my_node_id,
        "my_node_name": state.my_node_name,
        "firmware": state.firmware,
        "device": state.device_path,
        "protocol": state.protocol,
        "radio": dict(state.radio_info),
        "channels": [tuple(pair) for pair in state.channel_pairs()],
        "max_channels": state.max_channels,
        "channel_security": [
            {"index": c.index, "name": c.name, "level": c.level,
             "detail": c.detail, "hash": c.hash}
            for c in state.local_channels
        ],
    }


def event_to_wire(kind: str, payload: Any) -> dict[str, Any] | None:
    """One broadcastable {"event", "payload"} line, or None to skip."""
    if kind in SKIP_EVENTS:
        return None
    if kind in ("node", "mc_contact"):
        # The service has already normalized both into a Node; clients only
        # need the one upsert-able record.
        if not isinstance(payload, Node):
            return None
        return {"event": "node", "payload": node_record(payload)}
    if kind == "packet":
        payload = packet_to_dict(payload)
    elif kind == "chat":
        if not isinstance(payload, ChatMessage):
            return None
        payload = chat_to_dict(payload)
    elif kind == "receipt":
        payload = receipt_to_dict(payload)
    elif kind == "mc_channels":
        payload = [list(pair) for pair in payload]
    return {"event": kind, "payload": payload}


def event_from_wire(obj: dict[str, Any]) -> tuple[str, Any]:
    """Invert event_to_wire so the payload matches what a radio link emits."""
    kind = str(obj.get("event") or "")
    payload = obj.get("payload")
    if kind == "packet":
        payload = packet_from_dict(payload or {})
    elif kind == "chat":
        payload = chat_from_dict(payload or {})
    elif kind == "receipt":
        payload = receipt_from_dict(payload or {})
    elif kind == "mc_channels":
        payload = [tuple(pair) for pair in (payload or [])]
    elif kind in TUPLE_EVENTS and isinstance(payload, list):
        payload = tuple(payload)
    elif kind == "connected" and isinstance(payload, dict):
        payload["channels"] = [tuple(pair) for pair in (payload.get("channels") or [])]
    return kind, payload

"""Full protobuf + hex view of a single packet."""

from __future__ import annotations

import time
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..model import Packet, port_label

HEX_WIDTH = 16


def hexdump(data: bytes, limit: int = 512) -> Text:
    out = Text()
    truncated = len(data) > limit
    view = data[:limit]
    for offset in range(0, len(view), HEX_WIDTH):
        chunk = view[offset : offset + HEX_WIDTH]
        out.append(f"{offset:08x}  ", style="grey42")
        for i in range(HEX_WIDTH):
            if i < len(chunk):
                out.append(f"{chunk[i]:02x} ", style="bright_white")
            else:
                out.append("   ")
            if i == 7:
                out.append(" ")
        out.append(" |", style="grey42")
        for byte in chunk:
            char = chr(byte) if 32 <= byte < 127 else "."
            out.append(char, style="cyan" if 32 <= byte < 127 else "grey35")
        out.append("|\n", style="grey42")
    if truncated:
        out.append(f"... {len(data) - limit} more bytes\n", style="grey42")
    return out


def pretty(value: Any, indent: int = 0, key: str = "") -> Text:
    """Recursively render a decoded protobuf dict."""
    pad = "  " * indent
    out = Text()
    if isinstance(value, dict):
        if key:
            out.append(f"{pad}{key}:\n", style="bold bright_blue")
        for k, v in value.items():
            out.append(pretty(v, indent + (1 if key else 0), str(k)))
    elif isinstance(value, (list, tuple)):
        out.append(f"{pad}{key}: ", style="grey62")
        out.append(f"[{len(value)} items]\n", style="grey42")
        for i, item in enumerate(value):
            out.append(pretty(item, indent + 1, str(i)))
    elif isinstance(value, bytes):
        out.append(f"{pad}{key}: ", style="grey62")
        out.append(f"<{len(value)} bytes>\n", style="magenta")
    else:
        out.append(f"{pad}{key}: ", style="grey62")
        style = "bright_white"
        if isinstance(value, bool):
            style = "green" if value else "red"
        elif isinstance(value, (int, float)):
            style = "yellow"
        out.append(f"{value}\n", style=style)
    return out


class PacketInspector(ModalScreen[None]):
    BINDINGS = [
        ("escape,q,enter", "dismiss", "close"),
        ("up,k", "scroll_up", "scroll"),
        ("down,j", "scroll_down", "scroll"),
    ]

    def __init__(self, packet: Packet) -> None:
        super().__init__()
        self.packet = packet

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="inspect-box"):
            yield Static(self._body())
        yield Static(
            Text("  esc close    up/down scroll", style="grey42"), id="inspect-help"
        )

    def _body(self) -> Group:
        p = self.packet
        label, colour = port_label(p.portnum)

        header = Text()
        header.append(label, style=f"bold {colour}")
        header.append("  ")
        header.append(p.portnum, style="grey62")
        header.append("\n")
        header.append(f"{p.from_id} -> {p.to_id}", style="bright_white")
        header.append("   ")
        header.append(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.ts)), style="grey62")

        meta = Table.grid(padding=(0, 2))
        meta.add_column(justify="right", style="grey62", width=10)
        meta.add_column(justify="left")

        def row(k: str, v: Any, style: str = "bright_white") -> None:
            if v is None or v == "":
                return
            meta.add_row(k, Text(str(v), style=style))

        row("packet id", p.packet_id)
        row("channel", p.channel)
        row("snr", f"{p.snr:+.2f} dB" if p.snr is not None else None, "yellow")
        row("rssi", f"{p.rssi} dBm" if p.rssi is not None else None, "yellow")
        row("hops", p.hops, "cyan")
        raw = p.raw or {}
        row("hop start", raw.get("hopStart"))
        row("hop limit", raw.get("hopLimit"))
        row("want ack", raw.get("wantAck"))
        row("priority", raw.get("priority"))
        row("via mqtt", raw.get("viaMqtt"))
        row("rx time", raw.get("rxTime"))
        row("summary", p.summary, "white")

        parts: list[Any] = [header, Text(""), meta, Text("")]

        decoded = raw.get("decoded") or {}
        if decoded:
            parts.append(Text(" decoded", style="bold grey42"))
            shown = {k: v for k, v in decoded.items() if k != "payload"}
            parts.append(pretty(shown))

        payload = decoded.get("payload")
        if isinstance(payload, (bytes, bytearray)) and payload:
            parts.append(Text(""))
            parts.append(Text(f" payload  ({len(payload)} bytes)", style="bold grey42"))
            parts.append(hexdump(bytes(payload)))

        encrypted = raw.get("encrypted")
        if isinstance(encrypted, (bytes, bytearray)) and encrypted:
            parts.append(Text(""))
            parts.append(Text(f" encrypted  ({len(encrypted)} bytes)", style="bold grey42"))
            parts.append(hexdump(bytes(encrypted)))

        extra = {k: v for k, v in raw.items() if k not in ("decoded", "encrypted")}
        if extra:
            parts.append(Text(""))
            parts.append(Text(" raw packet", style="bold grey42"))
            parts.append(pretty(extra))

        return Group(*parts)

    def action_dismiss(self) -> None:
        self.dismiss(None)

    def action_scroll_up(self) -> None:
        self.query_one("#inspect-box", VerticalScroll).scroll_up()

    def action_scroll_down(self) -> None:
        self.query_one("#inspect-box", VerticalScroll).scroll_down()

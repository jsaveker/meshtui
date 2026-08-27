"""Browse and edit MeshCore channel slots.

A MeshCore device has a fixed number of channel slots (40 on current firmware)
and they are not filled contiguously — a radio can have channels at 0, 5 and 12
with everything between them empty. The slot index matters, because that is what
a channel message is addressed to.

Keys work two ways. A channel whose name starts with `#` derives its key from
`sha256(name)[:16]`, so anyone who knows the name can join it — that is how
public channels are shared. A channel can also carry an explicit 16-byte secret,
which is what a private group uses.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, RichLog, Static

from ..state import MeshState

HELP = [
    ("add <idx> <name>", "create a channel; #name derives its key from the name"),
    ("add <idx> <name> <hex>", "create with an explicit 32-hex-char (16 byte) key"),
    ("del <idx>", "clear a slot"),
    ("name <idx> <name>", "rename, keeping the existing key"),
    ("refresh", "re-read every slot from the radio"),
]


def parse_secret(text: str) -> bytes | None:
    """A 16-byte key as 32 hex characters, or None if it is not one."""
    cleaned = text.strip().replace(" ", "").replace(":", "")
    if len(cleaned) != 32:
        return None
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return None


class ChannelScreen(Screen[None]):
    """Every slot the radio has, used or free, and a way to change them."""

    BINDINGS = [
        Binding("escape,c,q", "close", "back"),
        Binding("f5", "refresh", "refresh"),
    ]

    def __init__(self, state: MeshState, link: Any) -> None:
        super().__init__()
        self.state = state
        self.link = link
        self._rows: list[int] = []

    def compose(self) -> ComposeResult:
        yield Static(id="chan-status")
        with Horizontal(id="chan-main"):
            with Vertical(id="chan-left"):
                yield DataTable(id="chan-table", cursor_type="row")
            with Vertical(id="chan-right"):
                yield RichLog(id="chan-log", markup=False, wrap=True, max_lines=400)
                yield Input(placeholder="add 3 #austin   |   del 3   |   refresh",
                            id="chan-input")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#chan-table", DataTable)
        table.add_column("#", key="idx", width=3)
        table.add_column("name", key="name", width=24)
        table.add_column("key", key="key", width=10)
        table.border_title = "channel slots"
        self.query_one("#chan-log", RichLog).border_title = "help"
        self._render_help()
        self.refresh_table()
        self.set_interval(2.0, self.refresh_table)
        table.focus()

    # -------------------------------------------------------------- content

    def refresh_table(self) -> None:
        table = self.query_one("#chan-table", DataTable)
        keep = table.cursor_row
        table.clear()
        self._rows = []

        used: dict[int, str] = {}
        for item in self.state.channels:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                used[int(item[0])] = str(item[1])
            else:
                used[len(used)] = str(item)

        secrets = getattr(self.link, "channel_secrets", {}) or {}
        total = max(self.state.max_channels, (max(used) + 1) if used else 1)
        for index in range(total):
            name = used.get(index)
            secret = secrets.get(index)
            if name:
                key_text = Text("derived" if name.startswith("#") else "set",
                                style="grey62" if name.startswith("#") else "bright_green")
                if secret is not None and not any(secret):
                    key_text = Text("none", style="red")
                table.add_row(Text(str(index), style="grey62"),
                              Text(name[:24], style="bright_white"), key_text)
            else:
                table.add_row(Text(str(index), style="grey35"),
                              Text("(empty)", style="grey35"), Text("", style="grey35"))
            self._rows.append(index)

        if self._rows:
            table.move_cursor(row=min(max(keep, 0), len(self._rows) - 1))
        self.query_one("#chan-status", Static).update(
            Text(f" channels  -  {len(used)} of {total} slots in use"
                 f"   (protocol: {self.state.protocol})", style="grey62"))

    def _render_help(self) -> None:
        log = self.query_one("#chan-log", RichLog)
        log.write(Text("commands", style="bold grey42"))
        for cmd, why in HELP:
            line = Text(f"  {cmd:<24}", style="bright_white")
            line.append(why, style="grey54")
            log.write(line)
        log.write(Text(""))
        log.write(Text(
            "A name starting with # derives its key from the name itself, so anyone "
            "who knows the name can join. Give an explicit key for a private channel.",
            style="grey54"))
        log.write(Text(""))

    def _say(self, text: str, style: str = "white") -> None:
        self.query_one("#chan-log", RichLog).write(Text(text, style=style))

    # -------------------------------------------------------------- actions

    def action_refresh(self) -> None:
        if hasattr(self.link, "_submit") and hasattr(self.link, "_load_channels"):
            self.link._submit(self.link._load_channels())
            self._say("re-reading every slot from the radio", "bright_cyan")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Stop the event: it would otherwise bubble to the app, which transmits
        # anything it receives as a chat message.
        event.stop()
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if self.state.protocol != "meshcore":
            self._say("channel editing is a MeshCore feature", "yellow")
            return

        parts = text.split()
        verb = parts[0].lower()

        if verb == "refresh":
            self.action_refresh()
            return

        if verb == "del" and len(parts) == 2 and parts[1].isdigit():
            index = int(parts[1])
            self.link.delete_channel(index)
            self._say(f"cleared slot {index}", "bright_cyan")
            return

        if verb in ("add", "name") and len(parts) >= 3 and parts[1].isdigit():
            index = int(parts[1])
            if index >= self.state.max_channels:
                self._say(f"this radio only has {self.state.max_channels} slots", "yellow")
                return
            secret = parse_secret(parts[-1])
            name = " ".join(parts[2:-1]) if secret else " ".join(parts[2:])
            if not name:
                self._say("a channel needs a name", "yellow")
                return
            self.link.set_channel(index, name, secret)
            how = ("explicit key" if secret
                   else "key derived from the name" if name.startswith("#")
                   else "key derived from the name (MeshCore hashes it)")
            self._say(f"slot {index} -> {name}  ({how})", "bright_cyan")
            return

        self._say(f"don't understand {text!r} - see the commands above", "yellow")

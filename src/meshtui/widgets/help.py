"""Key reference overlay."""

from __future__ import annotations

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("getting around", [
        ("tab / shift+tab", "move between panes"),
        ("/", "jump to the message box"),
        ("z", "expand chat to full screen"),
        ("escape", "leave the message box / close an overlay"),
        ("?", "this help"),
        ("q", "quit"),
    ]),
    ("nodes pane", [
        ("up / down", "select a node"),
        ("enter", "node detail"),
        ("s", "cycle sort: heard / name / snr / hops / packets"),
        ("d", "open a direct message with the selected node"),
        ("t", "traceroute the selected node"),
    ]),
    ("packets pane", [
        ("up / down", "scroll (pauses auto-follow)"),
        ("G / end", "jump to newest and resume following"),
        ("enter / i", "inspect the selected packet"),
        ("p", "pause / resume the feed"),
        ("f", "cycle filter: all / chatty / text only"),
        ("ctrl+l", "clear the feed"),
    ]),
    ("views", [
        ("m", "map of node positions"),
        ("a", "channel security audit"),
        ("r", "relay dependency and mesh health"),
        ("w", "sensors: environment and air quality"),
        ("x", "remote admin (MeshCore repeaters)"),
        ("c", "browse and edit channels; enter jumps to one"),
        ("[  ]", "previous / next channel"),
    ]),
    ("map", [
        ("m", "open the map"),
        ("arrows / hjkl", "pan"),
        ("+ / -", "zoom"),
        ("f", "refit and recentre"),
        ("c", "cycle colour: snr / hops / age"),
        ("r", "toggle distance rings"),
        ("i", "toggle direct links"),
        ("t", "toggle movement trails"),
    ]),
    ("chat commands", [
        ("/dm <node> <text>", "direct message (short name or !id)"),
        ("/trace <node> [hops]", "traceroute; hops=1 tests only the direct link"),
        ("/nodes", "list known nodes"),
        ("/clear", "clear the conversation view"),
        ("/help", "command help"),
    ]),
]

PROTOCOL_NOTE = (
    "meshtui speaks both Meshtastic and MeshCore. The protocol is detected from "
    "the radio;\nforce it with --protocol. Some panes are protocol-specific: relay "
    "analysis and the\nchannel audit are Meshtastic, remote admin is MeshCore."
)

NOTE = (
    "While the message box has focus, every letter is text - single-key "
    "shortcuts only work once you leave it with escape or tab."
)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [("escape,q,question_mark,f1", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-box"):
            yield Static(self._body())
        yield Static(Text("  esc close", style="grey42"), id="help-foot")

    def _body(self) -> Group:
        parts: list[object] = [Text("meshtui keys", style="bold bright_cyan"), Text("")]
        for title, rows in SECTIONS:
            parts.append(Text(f" {title}", style="bold grey42"))
            table = Table.grid(padding=(0, 2))
            table.add_column(justify="right", style="bold bright_white", width=17)
            table.add_column(justify="left", style="grey70")
            for key, desc in rows:
                table.add_row(key, desc)
            parts.extend([table, Text("")])
        parts.append(Text(NOTE, style="yellow"))
        parts.append(Text(""))
        parts.append(Text(PROTOCOL_NOTE, style="grey54"))
        return Group(*parts)

    def action_dismiss(self) -> None:
        self.dismiss(None)

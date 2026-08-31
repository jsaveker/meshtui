"""Explicit MeshCore flood-scope editor."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..meshcore_link import normalize_flood_scope
from ..state import MeshState


class ScopeScreen(ModalScreen[None]):
    """Inspect and intentionally change the local companion's flood scope."""

    BINDINGS = [Binding("escape", "dismiss", "close")]

    def __init__(self, state: MeshState, link: Any) -> None:
        super().__init__()
        self.state = state
        self.link = link

    def compose(self) -> ComposeResult:
        with Vertical(id="scope-box"):
            yield Static(" MeshCore flood scope", id="scope-title")
            yield Static(id="scope-current")
            yield Input(placeholder="#region (blank = use/disable default)", id="scope-input")
            with Horizontal(id="scope-actions"):
                yield Button("Apply this session", id="scope-apply", variant="primary")
                yield Button("Save radio default", id="scope-save")
                yield Button("Force unscoped", id="scope-unscoped", variant="warning")
            yield Static(
                "F marks flood-routed messages. A session scope lasts until the radio "
                "restarts; saving changes its persistent default. Unscoped bypasses "
                "scope isolation and is always explicit.", id="scope-help")

    def on_mount(self) -> None:
        self.link.request_flood_scope()
        self.set_interval(0.5, self._render_current)
        self._render_current()
        self.query_one("#scope-input", Input).focus()

    def _render_current(self) -> None:
        info = self.state.radio_info
        default = str(info.get("default_flood_scope") or "disabled")
        active = str(info.get("active_flood_scope") or "radio default")
        mode = str(info.get("active_flood_scope_mode") or "default")
        text = Text("default  ", style="grey62")
        text.append(default, style="bold bright_green")
        text.append("    active  ", style="grey62")
        text.append(active if active != "*" else "UNSCOPED", style=(
            "bold yellow" if mode == "unscoped" else "bold bright_cyan"))
        self.query_one("#scope-current", Static).update(text)

    def _value(self, *, saved: bool = False) -> str | None:
        try:
            return normalize_flood_scope(
                self.query_one("#scope-input", Input).value,
                allow_unscoped=not saved)
        except ValueError as exc:
            self.app.note(str(exc), "yellow")  # type: ignore[attr-defined]
            return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "scope-unscoped":
            self.link.set_flood_scope("*", force_unscoped=True)
            return
        saved = event.button.id == "scope-save"
        value = self._value(saved=saved)
        if value is not None:
            self.link.set_flood_scope(value, save_default=saved)

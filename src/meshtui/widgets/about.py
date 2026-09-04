"""Project identity and links, rendered as a native terminal modal."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Static

from .qr import qr_text

ABOUT_URL = "https://meshtui.com"
SOURCE_URL = "https://github.com/jsaveker/meshtui"


def package_version() -> str:
    try:
        return version("meshtui")
    except PackageNotFoundError:
        return "dev"


def logo_art() -> Text:
    """A compact network-M mark that works in every bundled colour theme."""
    rows = (
        "╭─ >_ ──────────────────╮",
        "│   ●╲             ╱●   │",
        "│   │  ╲         ╱  │   │",
        "│   ●    ╲     ╱    ●   │",
        "│   │      ╲ ╱      │   │",
        "│   ●───────●───────●   │",
        "╰─────── MeshTUI ───────╯",
    )
    art = Text()
    for row, raw in enumerate(rows):
        line = Text(raw, style="grey42")
        if row == 0:
            line.stylize("bold bright_cyan", 3, 5)
        elif 0 < row < len(rows) - 1:
            midpoint = len(raw) // 2
            line.stylize("bright_cyan", 3, midpoint)
            line.stylize("bright_blue", midpoint - 1, midpoint + 2)
            line.stylize("bright_magenta", midpoint + 1, len(raw) - 3)
        else:
            start = raw.index("MeshTUI")
            line.stylize("bold bright_cyan", start, start + 4)
            line.stylize("bold bright_blue", start + 4, start + 7)
        art.append_text(line)
        if row < len(rows) - 1:
            art.append("\n")
    return art


def linked_text(label: str, url: str, style: str) -> Text:
    return Text(label, style=f"{style} underline link {url}")


class AboutScreen(ModalScreen[None]):
    """A compact, responsive About screen for both wide and small terminals."""

    BINDINGS = [("escape,q,b,enter", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="about-box"):
            with VerticalScroll(id="about-scroll"):
                yield Static(id="about-body")
            yield Static(
                Text(" b / esc / enter  close", style="grey42", justify="center"),
                id="about-foot",
            )

    def on_mount(self) -> None:
        self._render_body()

    def on_resize(self) -> None:
        self._render_body()

    def _render_body(self) -> None:
        try:
            self.query_one("#about-body", Static).update(self._body(self.size.width))
        except NoMatches:
            # Resize may arrive while the modal is being composed or removed.
            pass

    def _identity(self) -> Group:
        heading = Text()
        heading.append("Mesh", style="bold bright_cyan")
        heading.append("TUI", style="bold bright_blue")
        heading.append(f"  {package_version()}", style="grey54")

        return Group(
            logo_art(),
            Text(""),
            heading,
            Text("A terminal control surface for MeshCore and Meshtastic.",
                 style="grey70"),
            Text(""),
            Text("Created and maintained by James Saveker (@jsaveker)",
                 style="bright_white"),
            Text("MIT licensed · built with Python + Textual", style="grey54"),
            Text(""),
            linked_text(ABOUT_URL, ABOUT_URL, "bold bright_cyan"),
            linked_text("github.com/jsaveker/meshtui", SOURCE_URL, "bright_blue"),
        )

    def _website_qr(self) -> Group:
        code = qr_text(ABOUT_URL)
        if code is None:
            return Group(Text("QR unavailable", style="yellow"))
        return Group(
            code,
            Text("scan for meshtui.com", style="grey62", justify="center"),
        )

    def _body(self, width: int) -> Group:
        title = Text(" about MeshTUI", style="bold bright_magenta")
        identity = self._identity()
        parts: list[object] = [title, Text("")]

        if width >= 86:
            table = Table.grid(padding=(0, 3))
            table.add_column(width=40)
            table.add_column(width=29, justify="center")
            table.add_row(identity, self._website_qr())
            parts.append(table)
        else:
            parts.append(identity)
            # At very small widths the printed URL remains usable and avoids
            # forcing a clipped, unscannable QR into the viewport.
            if width >= 42:
                parts.extend([Text(""), self._website_qr()])
        return Group(*parts)

    def action_dismiss(self) -> None:
        self.dismiss(None)

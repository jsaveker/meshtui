"""Persistent product identity and the keyboard-opened About screen."""

import asyncio
import os
import tempfile

from rich.console import Console

from meshtui.app import MeshTUI
from meshtui.widgets.about import ABOUT_URL, AboutScreen, logo_art

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def rendered_text(renderable, width=100):
    console = Console(width=width, color_system=None, force_terminal=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


async def main() -> int:
    art_lines = logo_art().plain.splitlines()
    check("terminal logo has a stable rectangular frame",
          len({len(line) for line in art_lines}), 1)

    with tempfile.TemporaryDirectory(prefix="meshtui-about-") as tmpdir:
        app = MeshTUI(
            demo=True,
            store=None,
            protocol="meshcore",
            preferences_path=os.path.join(tmpdir, "preferences.json"),
        )
        async with app.run_test(size=(100, 38)) as pilot:
            await pilot.pause(0.4)
            brand = app.query_one("#status-brand").render()
            check("MeshTUI identity is always present in the top bar",
                  getattr(brand, "plain", str(brand)), "MeshTUI [b]")

            await pilot.press("b")
            await pilot.pause(0.2)
            check("b opens the About screen", isinstance(app.screen, AboutScreen), True)
            if isinstance(app.screen, AboutScreen):
                body = rendered_text(app.screen._body(100))
                check("About names both radio ecosystems",
                      "MeshCore" in body and "Meshtastic" in body, True)
                check("About includes author information", "@jsaveker" in body, True)
                check("About prints the website URL", ABOUT_URL in body, True)
                check("About includes a website QR", "scan for meshtui.com" in body, True)

                narrow = rendered_text(app.screen._body(35), width=35)
                check("small terminals retain the typed website URL",
                      ABOUT_URL in narrow, True)
                check("small terminals omit an unscannable clipped QR",
                      "scan for meshtui.com" in narrow, False)

            await pilot.press("b")
            await pilot.pause(0.2)
            check("b closes the About screen", isinstance(app.screen, AboutScreen), False)

            # The global mnemonic must remain ordinary text inside inputs.
            await pilot.press("slash")
            await pilot.pause(0.2)
            palette_input = app.screen.query_one("#palette-input")
            palette_input.value = ""
            await pilot.press("b")
            check("b remains typable in an input", palette_input.value, "b")
            palette_input.value = "about"
            await pilot.press("enter")
            await pilot.pause(0.2)
            check("the palette also opens About", isinstance(app.screen, AboutScreen), True)
            await pilot.press("escape")
            await pilot.pause(0.2)

    if failures:
        print("\nFAIL:", failures)
        return 1
    print("\nPASS")
    return 0


raise SystemExit(asyncio.run(main()))

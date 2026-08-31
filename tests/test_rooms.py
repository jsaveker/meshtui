"""MeshCore room browser UI: signed posts and masked catch-up login."""

import asyncio
import sys
import time

from meshtui.app import MeshTUI
from meshtui.model import ChatMessage
from meshtui.widgets.rooms import RoomScreen

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


class RoomLink:
    def __init__(self):
        self.calls = []

    def login(self, node, password):
        self.calls.append(("login", node, password))

    def logout(self, node):
        self.calls.append(("logout", node))


async def main():
    app = MeshTUI(demo=True, store=None)
    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause(0.4)
        app.state.protocol = "meshcore"
        room = app.state.upsert_node({
            "user": {"id": "!deadbeef", "longName": "Town Room", "role": "ROOM"}})
        room.role = "ROOM"
        app.state.add_chat(ChatMessage(
            ts=time.time(), from_id=room.node_id, from_name="Walker",
            to_id=app.state.my_node_id or "!self", text="catch-up post", channel=-1))
        link = RoomLink()
        app.push_screen(RoomScreen(app.state, link, app))
        await pilot.pause(0.3)
        screen = app.screen
        check("room browser opens", isinstance(screen, RoomScreen), True)
        check("room appears in browser", screen.target, "!deadbeef")
        check("signed author appears in posts", "Walker" in "\n".join(
            str(line) for line in screen.query_one("#room-posts").lines), True)
        password = screen.query_one("#room-password")
        check("room password is masked", password.password, True)
        password.value = "guest-secret"
        password.focus()
        await pilot.press("enter")
        await pilot.pause(0.1)
        check("login requests server-driven catch-up", link.calls,
              [("login", "!deadbeef", "guest-secret")])
        check("password cleared after submit", password.value, "")
        check("password absent from visible status",
              "guest-secret" in str(screen.query_one("#room-status").render()), False)

    if failures:
        print("\nFAIL:", failures)
        return 1
    print("\nPASS")
    return 0


sys.exit(asyncio.run(main()))

"""Remote-admin login timing.

A login reply travels back over LoRa and can take tens of seconds. A command
typed before the ack arrives must be refused with a clear "waiting for the ack"
message - not the misleading "not authenticated", which reads as "the login
failed" - and must work once the ack lands.
"""

import asyncio
import sys

from meshtui.app import MeshTUI
from meshtui.widgets.admin import AdminScreen

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


class Spy:
    def __init__(self): self.cmds = []; self.logins = []
    def login(self, node_id, pwd): self.logins.append(node_id)
    def logout(self, node_id): pass
    def remote_command(self, node_id, cmd): self.cmds.append((node_id, cmd))
    def request_status(self, n): pass
    def request_telemetry(self, n): pass
    def stop(self): pass


async def send(screen, pilot, text):
    inp = screen.query_one("#admin-input")
    inp.focus(); await pilot.pause(0.1)
    inp.value = text
    await pilot.press("enter"); await pilot.pause(0.2)


async def main():
    app = MeshTUI(demo=True, store=None, protocol="meshtastic")
    async with app.run_test(size=(150, 40)) as pilot:
        await asyncio.sleep(1.5)
        st = app.state; st.protocol = "meshcore"; app.link = Spy()
        st.upsert_node({"num": 0xbc20c203, "user": {
            "id": "!bc20c203", "longName": "Repeater", "shortName": "REP",
            "hwModel": "REPEATER", "role": "REPEATER"}})
        app.push_screen(AdminScreen(st, app.link))
        await pilot.pause(0.4)
        screen = app.screen
        screen.target = "!bc20c203"

        # 1) command before any login -> "not authenticated"
        await send(screen, pilot, "advert")
        check("no command before login", app.link.cmds, [])

        # 2) login sent, ack not yet back -> pending, command refused but distinctly
        await send(screen, pilot, "login letmein")
        check("login was sent", app.link.logins, ["!bc20c203"])
        check("target marked pending", "!bc20c203" in screen._login_pending, True)
        check("not yet in admin_sessions", "!bc20c203" in st.admin_sessions, False)
        await send(screen, pilot, "advert")
        check("command held while ack pending", app.link.cmds, [])

        # 3) the ack lands (mc_login) -> pending clears, command works
        app._handle("mc_login", ("!bc20c203", True))
        await pilot.pause(0.2)
        screen._poll()   # the poll clears the pending flag on landed sessions
        await pilot.pause(0.1)
        check("pending cleared after ack", "!bc20c203" in screen._login_pending, False)
        check("authenticated after ack", "!bc20c203" in st.admin_sessions, True)
        await send(screen, pilot, "advert")
        check("command sent after ack", app.link.cmds, [("!bc20c203", "advert")])

    if failures:
        print(f"\nFAIL: {len(failures)}: {', '.join(failures)}")
        return 1
    print("\nPASS")
    return 0


sys.exit(asyncio.run(main()))

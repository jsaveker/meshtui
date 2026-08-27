"""Admin input must never be transmitted to the mesh.

Input.Submitted bubbles. The admin screen has its own input, and the app's
handler transmits whatever it receives as a chat message - so without an
event.stop() every remote-admin command, including the login password, was
also broadcast to the current channel in the clear. This happened in the wild.

This drives the real screen through the real event path with a spy in place of
the radio, and fails if anything typed there reaches send_text.
"""
import asyncio
from meshtui.app import MeshTUI
from meshtui.widgets.admin import AdminScreen

class SpyLink:
    """Stands in for a radio and records anything transmitted."""
    def __init__(self): self.sent = []; self.logins = []; self.cmds = []
    def send_text(self, text, dest="^all", channel=0):
        self.sent.append((text, dest, channel)); return (True, 1)
    def login(self, node_id, password): self.logins.append((node_id, password))
    def remote_command(self, node_id, cmd): self.cmds.append((node_id, cmd))
    def request_status(self, n): pass
    def request_telemetry(self, n): pass
    def logout(self, n): pass
    def stop(self): pass

async def main():
    app = MeshTUI(demo=True, store=None, protocol="meshtastic")
    async with app.run_test(size=(150, 44)) as pilot:
        await asyncio.sleep(2)
        spy = SpyLink()
        app.link = spy
        app.state.protocol = "meshcore"
        node = next(n for n in app.state.nodes.values() if not n.is_self)
        node.role = "REPEATER"
        app.state.admin_sessions.clear()

        await pilot.press("x"); await pilot.pause(0.6)
        assert isinstance(app.screen, AdminScreen), type(app.screen)
        app.screen.target = node.node_id
        inp = app.screen.query_one("#admin-input")
        inp.focus()
        await pilot.pause(0.3)

        SECRET = "hunter2-should-never-transmit"
        for cmd in (f"login {SECRET}", "ver", "get freq", "password newsecret"):
            inp.value = cmd
            await pilot.press("enter")
            await pilot.pause(0.3)

        if not spy.logins and not spy.cmds:
            print("FAIL: the admin screen handled nothing - test proved nothing")
            return 1
        print(f"chat messages transmitted : {spy.sent}")
        print(f"logins (correct path)     : {[(n, '<redacted>') for n, _ in spy.logins]}")
        print(f"commands (correct path)   : {spy.cmds}")
        leaked = [t for t, _, _ in spy.sent]
        bad = [t for t in leaked if SECRET in t or t.startswith(("login","ver","get","password"))]
        chat_log = [m.text for m in app.state.chat if m.outgoing]
        print(f"outgoing chat log         : {chat_log}")
        if bad or any(SECRET in m for m in chat_log):
            print(f"\nFAIL: admin input leaked to the mesh: {bad or chat_log}")
            return 1
        print("\nPASS: nothing typed in the admin screen was transmitted as chat")
        return 0

import sys; sys.exit(asyncio.run(main()))

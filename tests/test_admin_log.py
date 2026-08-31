"""The remote-admin session log persists, and never stores a credential.

The log is written to disk, so a password typed into the admin screen would
otherwise outlive the session in a file. Redaction is applied both at the call
site and inside record_admin, so no path can bypass it.
"""

import asyncio
import os
import sys
import tempfile

from meshtui.app import MeshTUI, redact_command
from meshtui.store import Store

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


print("redaction")
check("login is redacted", redact_command("login letmein"), "login <redacted>")
check("password is redacted", redact_command("password hunter2"), "password <redacted>")
check("passwd is redacted", redact_command("passwd s3cret"), "passwd <redacted>")
check("case is ignored", redact_command("LOGIN Secret"), "LOGIN <redacted>")
check("echoed form is redacted", redact_command("/login abc"), "/login <redacted>")
check("ordinary commands are kept", redact_command("get freq"), "get freq")
check("a bare verb is kept", redact_command("login"), "login")
check("replies are untouched", redact_command("v1.17.1-d929643"), "v1.17.1-d929643")


async def main():
    tmp = tempfile.mkdtemp(prefix="meshtui-adminlog-")
    db = os.path.join(tmp, "mesh.db")

    store = Store(db, flush_interval=0.2)
    assert store.open(), store.error
    app = MeshTUI(demo=True, store=store, protocol="meshtastic")
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.sleep(1.5)
        store.local_node = app.state.my_node_id or "!test"
        app.record_admin("!bcdecafe", "> login letmein")     # must not be stored
        app.record_admin("!bcdecafe", "** logged in **")
        app.record_admin("!bcdecafe", "> ver")
        app.record_admin("!bcdecafe", "v1.17.1-d929643")
        await pilot.pause(1.0)
    store.close()

    rows = Store(db)
    rows.local_node = store.local_node
    saved = rows.recent_admin_log()
    print("\npersisted lines")
    for _, node, text in saved:
        print(f"   {node}  {text}")
    check("all four lines stored", len(saved), 4)
    check("no credential on disk", any("letmein" in t for _, _, t in saved), False)
    check("login line redacted", saved[0][2], "> login <redacted>")
    check("reply stored verbatim", saved[3][2], "v1.17.1-d929643")

    store2 = Store(db, flush_interval=0.2)
    assert store2.open()
    app2 = MeshTUI(demo=True, store=store2, protocol="meshtastic")
    async with app2.run_test(size=(120, 40)) as pilot2:
        for _ in range(30):
            await pilot2.pause(0.25)
            if app2.state.cli_log:
                break
        restored = list(app2.state.cli_log)
        print(f"\nrestored into the session log: {len(restored)} lines")
        check("session log restored on startup", len(restored) >= 4, True)
        check("still no credential after restore",
              any("letmein" in t for _, _, t in restored), False)
    store2.close()

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("PASS")
    return 0


sys.exit(asyncio.run(main()))

"""MeshCore flood-scope validation and gateway control."""

import asyncio
import tempfile
import types
from pathlib import Path

from meshtui.gateway import Gateway
from meshtui.meshcore_link import MeshCoreLink, normalize_flood_scope
from meshtui.service import MeshService

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


check("scope gets hashtag", normalize_flood_scope("central"), "#central")
check("canonical scope stays canonical", normalize_flood_scope("#central"), "#central")
check("blank means radio default", normalize_flood_scope(""), "")
check("star is explicit unscoped", normalize_flood_scope("*"), "*")
try:
    normalize_flood_scope("*", allow_unscoped=False)
except ValueError:
    rejected_star = True
else:
    rejected_star = False
check("unscoped cannot be persisted", rejected_star, True)
try:
    normalize_flood_scope("x" * 32)
except ValueError:
    rejected_long = True
else:
    rejected_long = False
check("oversize scope rejected", rejected_long, True)


class Commands:
    def __init__(self):
        self.calls = []

    async def get_default_flood_scope(self):
        self.calls.append(("get",))

    async def set_default_flood_scope(self, scope):
        self.calls.append(("default", scope))

    async def set_flood_scope(self, scope, force_unscoped=False):
        self.calls.append(("session", scope, force_unscoped))


events = []
link = MeshCoreLink(lambda kind, payload: events.append((kind, payload)))
commands = Commands()
link.mc = types.SimpleNamespace(commands=commands)
link._submit = lambda coroutine: asyncio.run(coroutine)
link.request_flood_scope()
link.set_flood_scope("local")
link.set_flood_scope("*", force_unscoped=True)
link.set_flood_scope("regional", save_default=True)
check("device calls preserve intent", commands.calls, [
    ("get",), ("session", "#local", False), ("session", "*", True),
    ("default", "#regional"), ("get",),
])
check("session scope event emitted",
      any(kind == "mc_flood_scope" and payload.get("active_flood_scope") == "#local"
          for kind, payload in events), True)
link._on_flood_scope(types.SimpleNamespace(payload={
    "scope_name": "#regional", "scope_key": "12" * 16}))
check("device default event normalized", events[-1], ("mc_flood_scope", {
    "default_flood_scope": "#regional", "default_flood_scope_key": "12" * 16}))


class GatewayRadio:
    def __init__(self):
        self.calls = []

    def request_flood_scope(self):
        self.calls.append(("get",))

    def set_flood_scope(self, scope, *, save_default=False, force_unscoped=False):
        self.calls.append(("set", scope, save_default, force_unscoped))


service = MeshService(None)
service.state.protocol = "meshcore"
radio = GatewayRadio()
with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
    gateway = Gateway(service, radio, Path(tmp) / "unused.sock")
    check("gateway scope get accepted", gateway.handle_request({
        "command": "scope", "action": "get"})["ok"], True)
    check("gateway scope default accepted", gateway.handle_request({
        "command": "scope", "action": "default", "scope": "#ops"})["ok"], True)
    check("gateway unscoped accepted", gateway.handle_request({
        "command": "scope", "action": "unscoped"})["ok"], True)
check("gateway carries scope modes", radio.calls, [
    ("get",), ("set", "#ops", True, False), ("set", "*", False, True)])

if failures:
    print("\nFAIL:", failures)
    raise SystemExit(1)
print("\nPASS")

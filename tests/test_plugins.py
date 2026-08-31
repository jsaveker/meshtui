"""Trusted local plugin hooks use the durable gateway send path and stay isolated."""

import tempfile
import time
from pathlib import Path

from meshtui.model import BROADCAST, ChatMessage, Packet
from meshtui.plugins import PluginAPI, PluginManager
from meshtui.service import MeshService

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(name)


directory = Path(tempfile.mkdtemp(prefix="meshtui-plugins-"))
(directory / "autoreply.py").write_text(
    """def setup(api):
    @api.on_message
    def reply(message):
        if message.text == 'ping':
            api.send('pong', channel=12)

    @api.on_packet
    def broken(packet):
        raise RuntimeError('deliberate hook failure')
""", encoding="utf-8")

service = MeshService(None)
service.state.protocol = "meshcore"
service.state.channels = [(12, "Bots")]
manager = PluginManager(service, directory)
manager.start()
check("one plugin loaded", len(manager.modules), 1)
check("message hook registered", len(manager.api.message_handlers), 1)
check("packet hook registered", len(manager.api.packet_handlers), 1)

message = ChatMessage(ts=time.time(), from_id="!aa000001", from_name="Walker",
                      to_id=BROADCAST, text="ping", channel=12)
manager.handle_event("chat", message)
check("plugin send uses durable outbox", len(service.outbox), 1)
outbound = next(iter(service.outbox.values()))
check("plugin resolves sparse channel slot", outbound.destination.index, 12)
check("plugin text reaches the outbox", outbound.text, "pong")

packet = Packet(ts=time.time(), from_id="!aa000001", to_id=BROADCAST,
                portnum="TEXT_MESSAGE_APP", summary="test")
manager.handle_event("packet", packet)
check("broken hook is isolated", "deliberate hook failure" in manager.errors[-1], True)

try:
    PluginAPI(service).send("bad", to="!aa", channel=12)
    exclusive = False
except ValueError:
    exclusive = True
check("send requires one destination kind", exclusive, True)
manager.close()

if failures:
    print("\nFAIL:", failures)
    raise SystemExit(1)
print("\nPASS")

"""MeshCore transport.

MeshCore is a different mesh protocol from Meshtastic, with a different data
model: contacts identified by an X25519 public key rather than a node database,
explicit routing paths rather than flood-with-hop-limit, and repeaters that can
be administered over the air.

The library is asyncio-native while `RadioLink` is thread-and-callback based, so
this runs its own event loop on a dedicated thread and pushes everything through
the same `emit(kind, payload)` seam the Meshtastic links use. Commands from the
UI are submitted back into that loop with `run_coroutine_threadsafe`.

Event kinds emitted in addition to the shared ones:
    "mc_contact"    -> dict     a contact record, already normalised
    "mc_channels"   -> list     channel names by index
    "mc_cli"        -> tuple    (node_id, text) reply to a remote CLI command
    "mc_login"      -> tuple    (node_id, ok: bool)
    "mc_status"     -> tuple    (node_id, dict) remote repeater status
    "mc_telemetry"  -> tuple    (node_id, dict)
    "mc_neighbours" -> tuple    (node_id, list)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from .model import BROADCAST, ChatMessage, Packet
from .radio import Emit, RadioLink

log = logging.getLogger(__name__)

# Contact type values from MeshCore's advert types.
CONTACT_TYPES = {1: "CHAT", 2: "REPEATER", 3: "ROOM", 4: "SENSOR"}

# Synthetic port labels so the packet feed can colour MeshCore traffic using
# the same machinery as Meshtastic portnums.
PORT_ADVERT = "ADVERT_APP"
PORT_TEXT = "TEXT_MESSAGE_APP"
PORT_PATH = "PATH_APP"
PORT_ACK = "ROUTING_APP"
PORT_TELEM = "TELEMETRY_APP"
PORT_TRACE = "TRACEROUTE_APP"
PORT_CLI = "ADMIN_APP"
PORT_STATUS = "STATUS_APP"
PORT_RXLOG = "RXLOG_APP"


def key_to_id(pubkey: Any) -> str:
    """A stable short id for a contact, in the !hex form the UI already uses."""
    if isinstance(pubkey, (bytes, bytearray)):
        pubkey = pubkey.hex()
    text = str(pubkey or "").replace("0x", "")
    return f"!{text[:8].lower()}" if text else "!00000000"


def key_to_num(pubkey: Any) -> int:
    try:
        return int(key_to_id(pubkey)[1:], 16)
    except ValueError:
        return 0


def contact_to_node(contact: dict[str, Any]) -> dict[str, Any]:
    """Map a MeshCore contact onto the node record shape MeshState expects."""
    pubkey = contact.get("public_key") or contact.get("pubkey") or ""
    node_id = key_to_id(pubkey)
    name = (contact.get("adv_name") or contact.get("name") or "").strip()
    kind = CONTACT_TYPES.get(contact.get("type") or contact.get("adv_type") or 1, "CHAT")

    record: dict[str, Any] = {
        "num": key_to_num(pubkey),
        "user": {
            "id": node_id,
            "longName": name or node_id,
            # MeshCore has no short name; derive something stable and readable.
            "shortName": (name[:4] or node_id[-4:]).strip(),
            "hwModel": kind,
            "role": kind,
        },
    }
    lat = contact.get("adv_lat")
    lon = contact.get("adv_lon")
    if lat or lon:
        record["position"] = {"latitude": lat, "longitude": lon}
    if contact.get("last_advert"):
        record["lastHeard"] = contact["last_advert"]
    # `out_path_len` is the number of hops on the stored route; -1 means flood.
    hops = contact.get("out_path_len")
    if isinstance(hops, int) and hops >= 0:
        record["hopsAway"] = hops
    return record


def _drain_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel outstanding tasks before closing a loop.

    The meshcore library leaves an event-dispatcher task running; closing the
    loop underneath it raises "Event loop is closed" from deep inside asyncio.
    """
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:  # noqa: BLE001 - best effort teardown
        pass
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


def probe_meshcore(port: str, timeout: float = 6.0) -> dict[str, Any] | None:
    """Is a MeshCore companion radio on this port?

    Probed first when autodetecting because it fails fast: the Meshtastic
    library blocks waiting for a config dump that a MeshCore node will never
    send, so trying that first would hang on the wrong hardware.
    Returns the node's self_info, or None.
    """
    async def _probe() -> dict[str, Any] | None:
        from meshcore import MeshCore

        mc = await asyncio.wait_for(MeshCore.create_serial(port), timeout=timeout)
        try:
            info = dict(mc.self_info or {})
            return info if info.get("public_key") else None
        finally:
            try:
                await mc.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # Always probe on a private thread: the caller may already be inside a
    # running event loop (Textual's), where run_until_complete would fail.
    result: dict[str, Any] = {}

    def _run() -> None:
        loop = asyncio.new_event_loop()
        try:
            result["info"] = loop.run_until_complete(_probe())
        except Exception:  # noqa: BLE001 - not MeshCore, or nothing there
            log.debug("meshcore probe failed on %s", port, exc_info=True)
            result["info"] = None
        finally:
            _drain_loop(loop)

    thread = threading.Thread(target=_run, name="meshcore-probe", daemon=True)
    thread.start()
    thread.join(timeout=timeout + 4.0)
    return result.get("info")


class MeshCoreLink(RadioLink):
    """Drives a MeshCore companion radio over serial, TCP or BLE."""

    def __init__(self, emit: Emit, port: str | None = None,
                 host: str | None = None, ble: str | None = None) -> None:
        super().__init__(emit)
        self.port = port
        self.host = host
        self.ble = ble
        self.mc: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self.contacts: dict[str, dict[str, Any]] = {}
        self.channels: list[tuple[int, str]] = []
        self.channel_secrets: dict[int, Any] = {}
        # Replaced from the device query; 40 on current firmware.
        self.max_channels: int = 8
        self.logged_in: set[str] = set()
        self.my_node_id: str = "self"
        # Replies to remote-admin traffic do not reliably identify their
        # sender: CLI_REPLY carries only text, and LOGIN_SUCCESS includes a
        # pubkey prefix only on long enough frames. Remember who we addressed
        # so a reply can be attributed to the right node.
        self._pending_login: str | None = None
        self._admin_target: str | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Called on a worker thread; owns an asyncio loop for its lifetime."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as exc:  # noqa: BLE001
            self.emit("error", f"meshcore link failed: {exc}")
        finally:
            _drain_loop(self._loop)

    def stop(self) -> None:
        self._stopping.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self.connected = False

    def _submit(self, coro) -> Any:
        """Run a coroutine on the link's loop from the UI thread."""
        loop = self._loop
        if loop is None or not loop.is_running():
            self.emit("error", "not connected")
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as exc:  # noqa: BLE001
            self.emit("error", f"command failed: {exc}")
            return None

    async def _connect(self) -> Any:
        from meshcore import MeshCore

        if self.host:
            host, _, port = self.host.partition(":")
            self.emit("status", f"connecting to meshcore at {self.host} ...")
            return await MeshCore.create_tcp(host=host, port=int(port or 4403))
        if self.ble:
            self.emit("status", f"connecting to meshcore over BLE ({self.ble}) ...")
            return await MeshCore.create_ble(address=self.ble)
        self.emit("status", f"opening {self.port} ...")
        return await MeshCore.create_serial(self.port or "")

    async def _run(self) -> None:
        from meshcore import EventType

        try:
            self.mc = await self._connect()
        except Exception as exc:  # noqa: BLE001
            self.emit("error", f"could not connect to meshcore radio: {exc}")
            return

        self._wire_events(EventType)
        self.connected = True
        # Ask the device how many channel slots it actually has before
        # scanning them; firmware allows far more than the old hard-coded 8.
        try:
            query = self._payload(await self.mc.commands.send_device_query())
            self.max_channels = int(query.get("max_channels") or self.max_channels)
        except Exception:  # noqa: BLE001
            pass
        # Channels first: announcing with an empty list and then filling it in
        # would leave two concurrent Tabs rebuilds racing each other.
        await self._load_channels(announce=False)
        await self._announce()
        await self._load_contacts()
        await self._check_autoadd()

        # Pull queued messages the radio buffered while nothing was attached.
        try:
            result = self.mc.start_auto_message_fetching()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            log.debug("auto message fetching unavailable", exc_info=True)

        # Announce ourselves so peers learn our key and can route back.
        try:
            await self.mc.commands.send_advert(flood=True)
            self.emit("status", "flood advert sent")
        except Exception:  # noqa: BLE001
            log.debug("advert failed", exc_info=True)

        self._ready.set()
        while not self._stopping.is_set():
            await asyncio.sleep(0.25)

        try:
            await self.mc.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- events

    def _wire_events(self, EventType: Any) -> None:
        handlers = {
            EventType.ADVERTISEMENT: self._on_advert,
            EventType.NEW_CONTACT: self._on_new_contact,
            EventType.CONTACT_MSG_RECV: self._on_direct_message,
            EventType.CHANNEL_MSG_RECV: self._on_channel_message,
            EventType.PATH_UPDATE: self._on_path_update,
            EventType.ACK: self._on_ack,
            EventType.TELEMETRY_RESPONSE: self._on_telemetry,
            EventType.TRACE_DATA: self._on_trace,
            EventType.RX_LOG_DATA: self._on_rx_log,
            EventType.STATUS_RESPONSE: self._on_status,
            EventType.CLI_REPLY: self._on_cli_reply,
            EventType.LOGIN_SUCCESS: self._on_login_ok,
            EventType.LOGIN_FAILED: self._on_login_fail,
            EventType.DISCONNECTED: self._on_disconnected,
        }
        for event_type, handler in handlers.items():
            try:
                self.mc.subscribe(event_type, handler)
            except Exception:  # noqa: BLE001 - a missing event must not stop the rest
                log.debug("could not subscribe to %s", event_type, exc_info=True)

    @staticmethod
    def _payload(event: Any) -> dict[str, Any]:
        payload = getattr(event, "payload", event)
        return payload if isinstance(payload, dict) else {"value": payload}

    def _packet(self, portnum: str, from_id: str, summary: str, *,
                to_id: str = BROADCAST, snr: float | None = None,
                rssi: int | None = None, hops: int | None = None,
                raw: dict[str, Any] | None = None) -> None:
        self.emit("packet", Packet(
            ts=time.time(), from_id=from_id, to_id=to_id, portnum=portnum,
            summary=summary, snr=snr, rssi=rssi, hops=hops,
            raw=raw or {},
        ))

    # -------------------------------------------------------------- handlers

    def _on_advert(self, event: Any) -> None:
        data = self._payload(event)
        node_id = key_to_id(data.get("public_key"))
        self.emit("mc_contact", data)
        self._packet(PORT_ADVERT, node_id,
                     f"advert from {data.get('adv_name') or node_id}",
                     snr=data.get("snr"), rssi=data.get("rssi"), raw=data)

    def _on_new_contact(self, event: Any) -> None:
        data = self._payload(event)
        self.emit("mc_contact", data)
        self.emit("status", f"new contact: {data.get('adv_name') or key_to_id(data.get('public_key'))}")

    def _on_direct_message(self, event: Any) -> None:
        data = self._payload(event)
        node_id = key_to_id(data.get("pubkey_prefix") or data.get("public_key"))
        text = data.get("text") or data.get("msg") or ""
        self.emit("chat", ChatMessage(
            ts=data.get("sender_timestamp") or time.time(),
            from_id=node_id, from_name="", to_id=self.my_node_id, text=text,
            channel=-1,
        ))
        self._packet(PORT_TEXT, node_id, f'"{text}"', to_id=self.my_node_id,
                     snr=data.get("snr"), raw=data)

    def _on_channel_message(self, event: Any) -> None:
        data = self._payload(event)
        node_id = key_to_id(data.get("pubkey_prefix") or data.get("public_key"))
        text = data.get("text") or data.get("msg") or ""
        channel = int(data.get("channel_idx") or 0)
        self.emit("chat", ChatMessage(
            ts=data.get("sender_timestamp") or time.time(),
            from_id=node_id, from_name="", to_id=BROADCAST, text=text,
            channel=channel,
        ))
        self._packet(PORT_TEXT, node_id, f'"{text}"', snr=data.get("snr"), raw=data)

    def _on_path_update(self, event: Any) -> None:
        data = self._payload(event)
        node_id = key_to_id(data.get("public_key") or data.get("pubkey_prefix"))
        path = data.get("out_path") or data.get("path") or ""
        hops = data.get("out_path_len")
        self.emit("mc_contact", data)
        self._packet(PORT_PATH, node_id, f"path updated: {path or 'flood'}",
                     hops=hops if isinstance(hops, int) and hops >= 0 else None, raw=data)

    def _on_ack(self, event: Any) -> None:
        data = self._payload(event)
        code = data.get("code") or data.get("value")
        if isinstance(code, int):
            self.emit("ack", code)
        self._packet(PORT_ACK, "self", f"ack {code}", raw=data)

    def _on_telemetry(self, event: Any) -> None:
        data = self._payload(event)
        node_id = self._attribute(data, self._admin_target)
        self.emit("mc_telemetry", (node_id, data))
        self._packet(PORT_TELEM, node_id, _fmt_telemetry(data), raw=data)

    def _on_trace(self, event: Any) -> None:
        data = self._payload(event)
        self._packet(PORT_TRACE, "self", f"trace: {data}", raw=data)

    def _on_rx_log(self, event: Any) -> None:
        data = self._payload(event)
        self._packet(PORT_RXLOG, key_to_id(data.get("pubkey_prefix") or ""),
                     f"rx {data.get('payload_len', '?')}B",
                     snr=data.get("snr"), rssi=data.get("rssi"), raw=data)

    def _attribute(self, data: dict[str, Any], fallback: str | None) -> str:
        """Whose reply is this? Prefer the payload, fall back to who we asked."""
        key = data.get("pubkey_prefix") or data.get("public_key")
        if key:
            return key_to_id(key)
        return fallback or "!00000000"

    def _on_status(self, event: Any) -> None:
        data = self._payload(event)
        node_id = self._attribute(data, self._admin_target)
        self.emit("mc_status", (node_id, data))
        self._packet(PORT_STATUS, node_id, _fmt_status(data), raw=data)

    def _on_cli_reply(self, event: Any) -> None:
        data = self._payload(event)
        node_id = self._attribute(data, self._admin_target)
        text = str(data.get("response") or data.get("text") or data.get("value") or "")
        self.emit("mc_cli", (node_id, text))
        self._packet(PORT_CLI, node_id, text[:80], raw=data)

    def _on_login_ok(self, event: Any) -> None:
        data = self._payload(event)
        node_id = self._attribute(data, self._pending_login)
        self._pending_login = None
        self._admin_target = node_id
        self.logged_in.add(node_id)
        self.emit("mc_login", (node_id, True))

    def _on_login_fail(self, event: Any) -> None:
        data = self._payload(event)
        node_id = self._attribute(data, self._pending_login)
        self._pending_login = None
        self.logged_in.discard(node_id)
        self.emit("mc_login", (node_id, False))

    def _on_disconnected(self, event: Any) -> None:
        self.connected = False
        self.emit("lost", "meshcore radio disconnected")

    # ----------------------------------------------------------- connection

    async def _announce(self) -> None:
        info = dict(self.mc.self_info or {})
        self.my_node_id = key_to_id(info.get("public_key"))
        try:
            query = await self.mc.commands.send_device_query()
            info.update(self._payload(query))
        except Exception:  # noqa: BLE001
            pass
        where = self.host or self.ble or self.port or "?"
        self.emit("connected", {
            "device": f"meshcore://{where}",
            "my_node_id": self.my_node_id,
            "my_node_name": info.get("name") or "meshcore node",
            "firmware": f"MeshCore {info.get('ver', '?')}",
            "channels": list(self.channels),
            "max_channels": self.max_channels,
            "channel_security": [],
            "protocol": "meshcore",
            "radio": {
                "freq": info.get("radio_freq"), "bw": info.get("radio_bw"),
                "sf": info.get("radio_sf"), "cr": info.get("radio_cr"),
                "tx_power": info.get("tx_power"), "max_tx_power": info.get("max_tx_power"),
            },
        })

    async def _load_contacts(self) -> None:
        try:
            result = await self.mc.commands.get_contacts()
        except Exception as exc:  # noqa: BLE001
            self.emit("error", f"could not read contacts: {exc}")
            return
        contacts = self._payload(result)
        for contact in (contacts or {}).values():
            if isinstance(contact, dict):
                self.contacts[key_to_id(contact.get("public_key"))] = contact
                self.emit("mc_contact", contact)
        self.emit("status", f"{len(self.contacts)} contacts")

    async def _check_autoadd(self) -> None:
        try:
            result = await self.mc.commands.get_autoadd_config()
        except Exception:  # noqa: BLE001
            return
        config = self._payload(result)
        flags = int(config.get("config") or 0)
        self.emit("mc_autoadd", config)
        if flags == 0:
            self.emit("error",
                      "contact auto-add is OFF on this radio, so it discards every "
                      "advert and never learns anyone's public key. Direct messages "
                      "to it cannot be decrypted and will fail. Press 'A' to enable it.")

    async def _load_channels(self, announce: bool = True) -> None:
        """Read every channel slot the device has.

        Slots are not contiguous - a device can have channels at 0, 5 and 12 -
        so this must scan the whole range rather than stopping at the first
        empty one, and it must keep the real index because that is what
        send_chan_msg addresses.
        """
        found: list[tuple[int, str]] = []
        for index in range(self.max_channels):
            try:
                result = await self.mc.commands.get_channel(index)
            except Exception:  # noqa: BLE001 - a bad slot must not stop the scan
                continue
            data = self._payload(result)
            name = (data.get("channel_name") or data.get("name") or "").strip()
            secret = data.get("channel_secret")
            # An unused slot has no name and an all-zero key.
            if not name and not (secret and any(secret)):
                continue
            found.append((index, name or f"channel {index}"))
            self.channel_secrets[index] = secret
        self.channels = found or [(0, "Public")]
        # The initial load must not emit: the connect payload already carries
        # these, and two concurrent tab rebuilds collide on duplicate ids.
        if announce:
            self.emit("mc_channels", list(self.channels))

    # ------------------------------------------------------------- commands

    def send_text(self, text: str, dest: str = BROADCAST,
                  channel: int = 0) -> tuple[bool, int | None]:
        if dest in (BROADCAST, "^all"):
            future = self._submit(self.mc.commands.send_chan_msg(channel, text))
        else:
            contact = self._contact_for(dest)
            if contact is None:
                self.emit("error", f"no contact for {dest}")
                return (False, None)
            future = self._submit(self.mc.commands.send_msg(contact, text))
        if future is None:
            return (False, None)
        return (True, None)

    def _contact_for(self, node_id: str) -> dict[str, Any] | None:
        return self.contacts.get(node_id)

    def request_traceroute(self, dest: str, hop_limit: int = 5) -> None:
        contact = self._contact_for(dest)
        if contact is None:
            self.emit("error", f"no contact for {dest}")
            return
        self._submit(self.mc.commands.send_path_discovery(contact))
        self.emit("status", f"path discovery sent to {dest}")

    # --- remote administration over RF ---

    def login(self, node_id: str, password: str) -> None:
        contact = self._contact_for(node_id)
        if contact is None:
            self.emit("error", f"no contact for {node_id}")
            return
        self._pending_login = node_id
        self._submit(self.mc.commands.send_login(contact, password))
        self.emit("status", f"login sent to {node_id}")

    def logout(self, node_id: str) -> None:
        contact = self._contact_for(node_id)
        if contact is not None:
            self._submit(self.mc.commands.send_logout(contact))
        self.logged_in.discard(node_id)

    def remote_command(self, node_id: str, command: str) -> None:
        contact = self._contact_for(node_id)
        if contact is None:
            self.emit("error", f"no contact for {node_id}")
            return
        self._admin_target = node_id
        self._submit(self.mc.commands.send_cmd(contact, command))

    def request_status(self, node_id: str) -> None:
        contact = self._contact_for(node_id)
        if contact is None:
            self.emit("error", f"no contact for {node_id}")
            return
        self._admin_target = node_id
        self._submit(self.mc.commands.send_statusreq(contact))

    def request_telemetry(self, node_id: str) -> None:
        contact = self._contact_for(node_id)
        if contact is None:
            self.emit("error", f"no contact for {node_id}")
            return
        self._admin_target = node_id
        self._submit(self.mc.commands.send_telemetry_req(contact))

    # Auto-add bits, from examples/companion_radio/MyMesh.cpp.
    AUTOADD_OVERWRITE_OLDEST = 0x01
    AUTOADD_CHAT = 0x02
    AUTOADD_REPEATER = 0x04
    AUTOADD_ROOM = 0x08
    AUTOADD_SENSOR = 0x10
    AUTOADD_ALL = 0x1F

    def set_autoadd(self, flags: int = AUTOADD_ALL) -> None:
        """Control which advert types become stored contacts.

        With this at 0 the radio discards every advert, so it never learns a
        peer's public key - and a MeshCore direct message cannot be decrypted
        without the sender's key. The symptom is messages that simply fail.
        """
        self._submit(self.mc.commands.set_autoadd_config(flags))
        self.emit("status", f"contact auto-add set to 0x{flags:02x}")

    def set_channel(self, index: int, name: str, secret: bytes | None = None) -> None:
        """Create or replace a channel slot.

        MeshCore derives the key from sha256(name)[:16] when the name starts
        with '#' or no secret is given, which is how public channels are shared
        by name alone. An explicit 16-byte secret is used verbatim.
        """
        future = self._submit(self.mc.commands.set_channel(index, name, secret))
        if future is not None:
            self.emit("status", f"channel {index} set to {name}")
            self._submit(self._reload_channels())

    def delete_channel(self, index: int) -> None:
        """Blank a slot: empty name and an all-zero key."""
        future = self._submit(self.mc.commands.set_channel(index, "", bytes(16)))
        if future is not None:
            self.emit("status", f"channel {index} cleared")
            self._submit(self._reload_channels())

    async def _reload_channels(self) -> None:
        await asyncio.sleep(0.5)
        await self._load_channels()

    def send_advert(self, flood: bool = False) -> None:
        self._submit(self.mc.commands.send_advert(flood=flood))
        self.emit("status", "advert sent" + (" (flood)" if flood else ""))

    def reset_path(self, node_id: str) -> None:
        contact = self._contact_for(node_id)
        if contact is None:
            return
        self._submit(self.mc.commands.reset_path(contact))
        self.emit("status", f"path reset for {node_id}")


def _fmt_telemetry(data: dict[str, Any]) -> str:
    bits = []
    for key, label in (("battery", "bat"), ("voltage", "V"), ("temperature", "C"),
                       ("humidity", "%RH"), ("pressure", "hPa")):
        if data.get(key) is not None:
            bits.append(f"{data[key]}{label}")
    if not bits:
        lpp = data.get("lpp") or data.get("telemetry")
        if lpp:
            bits.append(str(lpp)[:60])
    return "  ".join(bits) or "telemetry"


def _fmt_status(data: dict[str, Any]) -> str:
    bits = []
    for key, label in (("bat", "bat"), ("uptime", "up"), ("nb_sent", "tx"),
                       ("nb_recv", "rx"), ("airtime", "air"), ("noise_floor", "noise")):
        if data.get(key) is not None:
            bits.append(f"{label} {data[key]}")
    return "  ".join(bits) or "status"

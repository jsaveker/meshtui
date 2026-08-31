"""Opt-in local Python plugins for the headless gateway.

Plugins are trusted operator code and therefore never load merely because a
repository contains a Python file. The gateway must be started with
``--plugins``; then ``setup(api)`` may register packet/message callbacks and use
the same durable send queue as every other client.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .model import BROADCAST, ChannelRef, ChatMessage, Packet, PeerRef, SendReceipt
from .service import MeshService

log = logging.getLogger(__name__)
PacketHandler = Callable[[Packet], None]
MessageHandler = Callable[[ChatMessage], None]


def default_plugin_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "meshtui" / "plugins"


class PluginAPI:
    """The intentionally tiny stable surface handed to user plugins."""

    def __init__(self, service: MeshService) -> None:
        self.service = service
        self.packet_handlers: list[PacketHandler] = []
        self.message_handlers: list[MessageHandler] = []

    def on_packet(self, callback: PacketHandler) -> PacketHandler:
        self.packet_handlers.append(callback)
        return callback

    def on_message(self, callback: MessageHandler) -> MessageHandler:
        self.message_handlers.append(callback)
        return callback

    def send(self, text: str, *, to: str | None = None,
             channel: str | int | None = None,
             route_mode: str = "auto") -> SendReceipt:
        """Queue one DM or channel message through the gateway's real outbox."""
        if bool(to) == (channel is not None):
            raise ValueError("send needs exactly one of to= or channel=")
        protocol = self.service.state.protocol
        if to:
            if route_mode not in ("auto", "flood", "direct"):
                raise ValueError("route_mode must be auto, flood, or direct")
            if route_mode != "auto" and protocol != "meshcore":
                raise ValueError("route_mode overrides are MeshCore only")
            node = self.service.state.resolve(to)
            node_id = node.node_id if node is not None else to
            destination = PeerRef(protocol, node_id, None, route_mode)
        else:
            slot = self._channel_slot(channel)
            destination = ChannelRef(protocol, slot,
                                     self.service.state.channel_name(slot))
        return self.service.send_message(text, destination)

    def _channel_slot(self, selector: str | int | None) -> int:
        if isinstance(selector, int) or (isinstance(selector, str) and selector.isdigit()):
            slot = int(selector)
            if not 0 <= slot <= 255:
                raise ValueError("channel slot must be between 0 and 255")
            return slot
        wanted = str(selector or "").lstrip("#").casefold()
        matches = [slot for slot, name in self.service.state.channel_pairs()
                   if name.lstrip("#").casefold() == wanted]
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous channel: {selector}")
        return matches[0]


class PluginManager:
    """Load trusted local modules and isolate hook failures from radio I/O."""

    def __init__(self, service: MeshService, directory: str | Path | None = None) -> None:
        self.service = service
        self.directory = Path(directory) if directory else default_plugin_dir()
        self.api = PluginAPI(service)
        self.modules: list[ModuleType] = []
        self.errors: list[str] = []
        self._started = False

    def status(self) -> dict[str, Any]:
        return {
            "kind": "python_plugins",
            "directory": str(self.directory),
            "loaded": [module.__name__.split(".")[-1] for module in self.modules],
            "packet_hooks": len(self.api.packet_handlers),
            "message_hooks": len(self.api.message_handlers),
            "errors": list(self.errors[-10:]),
        }

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.errors.append(f"could not create plugin directory: {exc}")
            return
        for path in sorted(self.directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            self._load(path)

    def close(self) -> None:
        for module in reversed(self.modules):
            callback = getattr(module, "close", None)
            if callable(callback):
                self._call(callback, None, module.__name__)
        self._started = False

    def _load(self, path: Path) -> None:
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", path.stem)
        name = f"meshtui_user_plugin_{slug}_{abs(hash(path.resolve())):x}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ImportError("could not create module spec")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setup = getattr(module, "setup", None)
            if callable(setup):
                setup(self.api)
            else:
                packet = getattr(module, "on_packet", None)
                message = getattr(module, "on_message", None)
                if callable(packet):
                    self.api.on_packet(self._adapt(packet))
                if callable(message):
                    self.api.on_message(self._adapt(message))
            self.modules.append(module)
        except Exception as exc:  # noqa: BLE001 - one plugin cannot stop the gateway
            detail = f"{path.name}: {exc}"
            self.errors.append(detail)
            log.exception("could not load plugin %s", path)

    def _adapt(self, callback: Callable[..., None]) -> Callable[[Any], None]:
        try:
            wants_api = len(inspect.signature(callback).parameters) >= 2
        except (TypeError, ValueError):
            wants_api = False
        if wants_api:
            return lambda payload: callback(payload, self.api)
        return callback

    def handle_event(self, kind: str, payload: Any) -> None:
        if kind == "packet" and isinstance(payload, Packet):
            handlers = tuple(self.api.packet_handlers)
        elif kind in ("chat", "chat_update") and isinstance(payload, ChatMessage):
            handlers = tuple(self.api.message_handlers)
        else:
            return
        for handler in handlers:
            self._call(handler, payload, getattr(handler, "__name__", "hook"))

    def _call(self, callback: Callable, payload: Any, label: str) -> None:
        try:
            callback() if payload is None else callback(payload)
        except Exception as exc:  # noqa: BLE001 - hook isolation is the product boundary
            detail = f"{label}: {exc}"
            self.errors.append(detail)
            log.exception("plugin hook failed: %s", label)

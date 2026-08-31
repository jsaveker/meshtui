"""Safe packet-watch expression parsing (no eval, no protocol coupling)."""

from __future__ import annotations

import operator
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from .model import Packet

TERM = re.compile(r"^(proto|hop|hops|snr|chan|channel|from|to|type|text)"
                  r"(:|>=|<=|=|>|<)(.+)$", re.IGNORECASE)
COMPARISONS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge, "<=": operator.le, ">": operator.gt,
    "<": operator.lt, "=": operator.eq, ":": operator.eq,
}
PROTO_ALIASES = {
    "mc": "meshcore", "meshcore": "meshcore",
    "mt": "meshtastic", "meshtastic": "meshtastic",
}


@dataclass(frozen=True)
class WatchTerm:
    field: str
    operation: str
    value: str


@dataclass(frozen=True)
class WatchFilter:
    expression: str
    terms: tuple[WatchTerm, ...]

    def matches(self, packet: Packet, state: Any) -> bool:
        for term in self.terms:
            if not _matches(term, packet, state):
                return False
        return True


def parse_watch(expression: str) -> WatchFilter:
    try:
        tokens = shlex.split(expression)
    except ValueError as exc:
        raise ValueError(f"invalid quoting: {exc}") from exc
    if not tokens:
        raise ValueError("watch expression is empty")
    terms = []
    for token in tokens:
        match = TERM.match(token)
        if match is None:
            raise ValueError(f"invalid watch term: {token}")
        field, operation, value = match.groups()
        if not value:
            raise ValueError(f"missing value in watch term: {token}")
        field = {"hops": "hop", "channel": "chan"}.get(field.casefold(),
                                                               field.casefold())
        if field in ("hop", "snr"):
            try:
                float(value)
            except ValueError as exc:
                raise ValueError(f"{field} requires a number: {value}") from exc
        elif operation not in (":", "="):
            raise ValueError(f"{field} supports ':' or '=' only")
        terms.append(WatchTerm(field, operation, value))
    return WatchFilter(expression.strip(), tuple(terms))


def _matches(term: WatchTerm, packet: Packet, state: Any) -> bool:
    wanted = term.value.casefold()
    if term.field == "proto":
        raw = packet.raw if isinstance(packet.raw, dict) else {}
        actual = str(raw.get("protocol") or getattr(state, "protocol", "")).casefold()
        return PROTO_ALIASES.get(actual, actual) == PROTO_ALIASES.get(wanted, wanted)
    if term.field in ("hop", "snr"):
        actual = packet.hops if term.field == "hop" else packet.snr
        return actual is not None and COMPARISONS[term.operation](float(actual), float(term.value))
    if term.field == "chan":
        actual_name = str(state.channel_name(packet.channel)).lstrip("#").casefold()
        return wanted.lstrip("#") in (str(packet.channel).casefold(), actual_name)
    if term.field == "from":
        actual = f"{packet.from_id} {state.node_name(packet.from_id)}".casefold()
        return wanted in actual
    if term.field == "to":
        actual = f"{packet.to_id} {state.node_name(packet.to_id)}".casefold()
        return wanted in actual
    if term.field == "type":
        return wanted in packet.portnum.casefold()
    if term.field == "text":
        return wanted in packet.summary.casefold()
    return False

"""Meshtastic channel cryptography.

Everything here is derived from the published firmware, which is worth stating
plainly: the default channel key and the single-byte key shorthands are printed
in Meshtastic's own source and protobuf comments. Decoding traffic that uses
them is not an attack on anything - it is what every Meshtastic client does, and
the protobuf itself calls those keys "only minimally secure, because they are
listed in this source code".

Nothing here weakens a channel using a real random PSK; AES-128/256 with a
random key is not recoverable and no amount of code changes that.

References (meshtastic/firmware, master):
  src/mesh/CryptoEngine.cpp:263  initNonce()
  src/mesh/CryptoEngine.cpp:244  CTR<AES128|AES256>, setIV(nonce,16), counter size 4
  src/mesh/Channels.cpp:208      getKey() - 1-byte expansion and zero padding
  src/mesh/Channels.cpp:27       xorHash() - the 8-bit channel hash
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Iterable, Iterator

log = logging.getLogger(__name__)

# src/mesh/Channels.cpp - the "default" channel key, published upstream.
DEFAULT_PSK = bytes(
    [0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59,
     0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01]
)

# Channel names the stock apps offer; used to confirm a channel-hash match.
COMMON_CHANNEL_NAMES = (
    "", "LongFast", "LongSlow", "MediumFast", "MediumSlow",
    "ShortFast", "ShortSlow", "ShortTurbo", "VeryLongSlow",
    "admin", "Default", "Primary", "Secondary",
)


def xor_hash(data: bytes) -> int:
    code = 0
    for byte in data:
        code ^= byte
    return code


def channel_hash(name: str, key: bytes) -> int:
    """The 8-bit channel id carried in the clear on every packet."""
    return xor_hash(name.encode()) ^ xor_hash(key)


def expand_psk(psk: bytes) -> bytes | None:
    """Apply the firmware's key rules. Returns None for 'no encryption'.

    - 0 bytes  -> inherit (caller's problem); treated as no key here
    - 1 byte   -> index 0 disables crypto; otherwise the default key with
                  (index - 1) added to its last byte, wrapping at 256
    - <16      -> zero-padded to 16 (a real weakness: a 4-byte key is a
                  32-bit keyspace)
    - 16 or 32 -> used as-is (AES-128 / AES-256)
    """
    if not psk:
        return None
    if len(psk) == 1:
        index = psk[0]
        if index == 0:
            return None
        key = bytearray(DEFAULT_PSK)
        key[-1] = (key[-1] + index - 1) & 0xFF
        return bytes(key)
    if len(psk) < 16:
        return psk + bytes(16 - len(psk))
    if 16 < len(psk) < 32:
        return psk + bytes(32 - len(psk))
    return psk


def published_keys(limit: int = 255) -> Iterator[tuple[str, bytes]]:
    """Every key derivable from the single-byte shorthand.

    The protobuf documents indices 1-10 (shown as default / simple1..simple10),
    but the firmware accepts any byte, so the whole space is 255 keys - small
    enough to enumerate instantly. That is the point: these are not secrets.
    """
    for index in range(1, min(limit, 255) + 1):
        key = expand_psk(bytes([index]))
        if key is None:
            continue
        label = "default" if index == 1 else f"simple{index - 1}"
        yield label, key


def build_nonce(packet_id: int, from_node: int, extra_nonce: int = 0) -> bytes:
    """CryptoEngine::initNonce - packet id (u64 LE), sender (u32 LE), then zeros."""
    nonce = bytearray(16)
    nonce[0:8] = struct.pack("<Q", packet_id & 0xFFFFFFFFFFFFFFFF)
    nonce[8:12] = struct.pack("<I", from_node & 0xFFFFFFFF)
    if extra_nonce:
        # The firmware writes this at offset 4, overlapping the packet id.
        nonce[4:8] = struct.pack("<I", extra_nonce & 0xFFFFFFFF)
    return bytes(nonce)


def decrypt(key: bytes, packet_id: int, from_node: int, ciphertext: bytes) -> bytes:
    """AES-CTR with the Meshtastic nonce as the initial counter block.

    The firmware sets a 4-byte counter over a 16-byte IV. Python's CTR treats
    the whole block as the counter, but the trailing four bytes are zero and a
    payload is at most 233 bytes (15 blocks), so no carry ever reaches byte 11
    and the two are equivalent.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) not in (16, 32):
        raise ValueError(f"key must be 16 or 32 bytes, got {len(key)}")
    nonce = build_nonce(packet_id, from_node)
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


@dataclass
class Decrypted:
    key_label: str
    key: bytes
    portnum: str
    data: object          # meshtastic Data protobuf
    plaintext: bytes


def _valid_data(plaintext: bytes):
    """Parse as a Data protobuf and reject anything that is merely lucky.

    Random bytes decrypt to random bytes, and protobuf is permissive enough to
    accept some of them, so require a portnum that is actually in the enum and
    a payload that is present and plausible.
    """
    from google.protobuf.message import DecodeError
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    try:
        data = mesh_pb2.Data.FromString(plaintext)
    except (DecodeError, Exception):  # noqa: BLE001 - any parse failure is a miss
        return None
    portnum = data.portnum
    if portnum not in portnums_pb2.PortNum.values():
        return None
    if portnum == 0:  # UNKNOWN_APP - never legitimately sent
        return None
    if not data.payload:
        return None
    # A Data message re-serialises to (almost) what we parsed if it was real.
    if len(data.SerializeToString()) != len(plaintext):
        return None
    return data


def try_keys(
    packet_id: int,
    from_node: int,
    ciphertext: bytes,
    keys: Iterable[tuple[str, bytes]] | None = None,
) -> Decrypted | None:
    """Attempt decryption with published keys only. None means 'not a published key'."""
    if not ciphertext or packet_id is None or from_node is None:
        return None
    from meshtastic.protobuf import portnums_pb2

    for label, key in (keys if keys is not None else published_keys()):
        try:
            plaintext = decrypt(key, packet_id, from_node, ciphertext)
        except Exception:  # noqa: BLE001
            continue
        data = _valid_data(plaintext)
        if data is not None:
            return Decrypted(
                key_label=label,
                key=key,
                portnum=portnums_pb2.PortNum.Name(data.portnum),
                data=data,
                plaintext=plaintext,
            )
    return None


# Verdicts for a channel whose PSK we can actually see (our own node's).
SECURITY_LEVELS = {
    "OPEN":   "no encryption at all - anyone can read this",
    "PUBLIC": "a key published in Meshtastic's source - anyone can read this",
    "WEAK":   "shorter than 16 bytes and zero-padded by the firmware",
    "AES128": "128-bit random key - not recoverable",
    "AES256": "256-bit random key - not recoverable",
}


def classify_psk(psk: bytes | None) -> tuple[str, str]:
    """Grade a PSK we can see. Returns (level, detail)."""
    if psk is None:
        return ("OPEN", "no key set")
    if len(psk) == 0:
        return ("OPEN", "empty key - inherits the primary channel, or no crypto")
    if len(psk) == 1:
        if psk[0] == 0:
            return ("OPEN", "psk index 0 - encryption disabled")
        label = "default" if psk[0] == 1 else f"simple{psk[0] - 1}"
        return ("PUBLIC", f"single-byte shorthand '{label}', listed in the firmware source")
    if len(psk) < 16:
        bits = len(psk) * 8
        return ("WEAK", f"{len(psk)} bytes zero-padded to 16 - only {bits} bits of key")
    if len(psk) == 16:
        return ("AES128", "128-bit key")
    if len(psk) < 32:
        bits = len(psk) * 8
        return ("WEAK", f"{len(psk)} bytes zero-padded to 32 - only {bits} bits of key")
    return ("AES256", "256-bit key")


def identify_channel(hash_value: int, keys: Iterable[tuple[str, bytes]] | None = None
                     ) -> list[tuple[str, str]]:
    """Names/keys whose channel hash matches - a cheap filter, not proof."""
    out: list[tuple[str, str]] = []
    for label, key in (keys if keys is not None else published_keys()):
        for name in COMMON_CHANNEL_NAMES:
            if channel_hash(name, key) == hash_value:
                out.append((name or "(unnamed/default)", label))
    return out

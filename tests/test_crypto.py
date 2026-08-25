"""Crypto tests, pinned to Meshtastic's own published test vectors.

The important one is test_nonce_matches_upstream_vector: it is taken verbatim
from meshtastic/firmware test/test_crypto/test_main.cpp::test_PKC. Without it
a wrong nonce would silently produce "nothing decrypts", which is
indistinguishable from "every channel uses a strong key".
"""

import sys

from meshtui.crypto import (
    DEFAULT_PSK,
    build_nonce,
    channel_hash,
    classify_psk,
    decrypt,
    expand_psk,
    published_keys,
    try_keys,
)

failures: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


print("nonce (meshtastic/firmware test_crypto/test_main.cpp::test_PKC)")
# fromNode = 0x0929, packetNum = 0x13b2d662, extraNonce = 0x2b796a03
check(
    "matches upstream expected_nonce",
    build_nonce(0x13B2D662, 0x0929, 0x2B796A03).hex()[:26],
    "62d6b213036a792b2909000000",
)
check("psk path leaves bytes 4-7 as the id's high half",
      build_nonce(0x13B2D662, 0x0929).hex(), "62d6b21300000000290900000000000000000000"[:32])

print("\nkey expansion (src/mesh/Channels.cpp::getKey)")
check("index 1 is the default key", expand_psk(bytes([1])), DEFAULT_PSK)
check("index 2 bumps the last byte", expand_psk(bytes([2]))[-1], (DEFAULT_PSK[-1] + 1) & 0xFF)
check("index 0 disables encryption", expand_psk(bytes([0])), None)
check("last byte wraps at 256", expand_psk(bytes([255]))[-1], (DEFAULT_PSK[-1] + 254) & 0xFF)
check("short keys are zero-padded to 16", expand_psk(b"\x01\x02\x03\x04"), b"\x01\x02\x03\x04" + bytes(12))
check("16-byte keys pass through", len(expand_psk(bytes(16))), 16)
check("32-byte keys pass through", len(expand_psk(bytes(32))), 32)

print("\nchannel hash (src/mesh/Channels.cpp::xorHash)")
# The stock primary channel is named LongFast and hashes to 8 upstream.
check("LongFast + default key hashes to 8", channel_hash("LongFast", DEFAULT_PSK), 8)

print("\npsk classification")
check("single byte is PUBLIC", classify_psk(bytes([1]))[0], "PUBLIC")
check("empty is OPEN", classify_psk(b"")[0], "OPEN")
check("4 bytes is WEAK", classify_psk(b"\x01\x02\x03\x04")[0], "WEAK")
check("16 bytes is AES128", classify_psk(bytes(16))[0], "AES128")
check("32 bytes is AES256", classify_psk(bytes(32))[0], "AES256")

print("\nAES-CTR")
plain = b"the quick brown fox jumps over the lazy dog"
cipher = decrypt(DEFAULT_PSK, 0xDEADBEEF, 0x12345678, plain)
check("ctr is its own inverse", decrypt(DEFAULT_PSK, 0xDEADBEEF, 0x12345678, cipher), plain)
check("ciphertext differs from plaintext", cipher != plain, True)
check("a different nonce gives different output",
      decrypt(DEFAULT_PSK, 0xDEADBEEF + 1, 0x12345678, plain) != cipher, True)

print("\npublished key space")
keys = list(published_keys())
check("255 single-byte keys", len(keys), 255)
check("first is labelled default", keys[0][0], "default")
check("all are 16 bytes", {len(k) for _, k in keys}, {16})

print("\nend to end: a packet encrypted under a published key is recovered")
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # noqa: E402

data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, payload=b"radio check")
pid, node = 0x0BADF00D, 0x76C9F798
ct = decrypt(DEFAULT_PSK, pid, node, data.SerializeToString())
got = try_keys(pid, node, ct)
check("recovered", got is not None and bytes(got.data.payload), b"radio check")
check("identified the key", got.key_label if got else None, "default")

print("\nrandom keys stay shut")
import os  # noqa: E402

strong = decrypt(os.urandom(32), pid, node, data.SerializeToString())
check("no published key opens a random-PSK packet", try_keys(pid, node, strong), None)

print()
if failures:
    print(f"FAIL: {len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("PASS")

"""The map uploader's crypto and etiquette.

The upload signature is computed from an EXPANDED Ed25519 key (the supercop
[scalar][prefix] layout MeshCore firmware exports) with explicit curve math -
so the one test that matters most is that a standard verifier accepts our
signatures, because that is exactly what map.meshcore.io does. Alongside:
advert signature verification (only adverts a node really transmitted can be
uploaded), replay/cooldown etiquette, and the exact envelope shape.
"""
import hashlib, json, sys, time

from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                               Ed25519PublicKey)
from cryptography.hazmat.primitives import serialization

from meshtui.mapupload import (MapUploader, extract_advert_payload,
                               sign_expanded_ed25519, verify_advert)

failures = []
def check(n, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {n}")
    if got != want: failures.append(n)

RAW = serialization.Encoding.Raw
PUB = serialization.PublicFormat.Raw


def expanded_from_seed(seed: bytes) -> tuple[bytes, bytes]:
    """Derive the [scalar][prefix] form firmware exports, plus the pubkey."""
    digest = bytearray(hashlib.sha512(seed).digest())
    digest[0] &= 248; digest[31] &= 127; digest[31] |= 64
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(RAW, PUB)
    return bytes(digest), pub

# ------------------------------------------------ supercop signing round-trip
seed = bytes(range(32))
expanded, pub = expanded_from_seed(seed)
for label, message in (("short", b"hello mesh"),
                       ("hash-sized", hashlib.sha256(b"payload").digest()),
                       ("empty", b"")):
    signature = sign_expanded_ed25519(message, expanded, pub)
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(signature, message)
        ok = True
    except Exception:
        ok = False
    check(f"a standard verifier accepts our expanded-key signature ({label})", ok, True)

reference = Ed25519PrivateKey.from_private_bytes(seed).sign(b"determinism")
check("expanded-key signing matches libsodium byte for byte",
      sign_expanded_ed25519(b"determinism", expanded, pub), reference)

# ---------------------------------------------------- advert verification
node = Ed25519PrivateKey.generate()
node_pub = node.public_key().public_bytes(RAW, PUB)
ts = int(time.time()).to_bytes(4, "little")
app_data = bytes([0x92]) + b"Ridge Solar Repeater"
advert = node_pub + ts + node.sign(node_pub + ts + app_data) + app_data
check("a genuine advert verifies", verify_advert(advert), True)
tampered = bytearray(advert); tampered[-1] ^= 1
check("a tampered advert is rejected", verify_advert(bytes(tampered)), False)
check("a truncated advert is rejected", verify_advert(advert[:80]), False)

raw_packet = bytes([0x11]) + b"\x00" + bytes([2]) + b"\x4c\x82" + advert
check("advert extraction walks header and path",
      extract_advert_payload(raw_packet.hex()), advert)

# --------------------------------------------------------- uploader flow
class FakeState:
    radio_info = {"freq": 910.525, "bw": 62.5, "sf": 7, "cr": 5}
class FakeService:
    state = FakeState()
class FakeLink:
    identity_key = expanded.hex()
    public_key = pub.hex()

uploader = MapUploader(FakeService(), FakeLink())
posted = []
uploader._post = lambda envelope: posted.append(envelope) or True

raw = {"payload_type": 4, "adv_type": 2, "adv_key": node_pub.hex(),
       "adv_timestamp": int(time.time()), "adv_name": "Ridge Solar Repeater",
       "payload": raw_packet.hex(), "pkt_payload": advert}
check("a verified repeater advert uploads", uploader.process(dict(raw)), True)
envelope = posted[-1]
data = json.loads(envelope["data"])
check("envelope carries radio params and the raw packet link",
      (data["params"], data["links"]),
      ({"freq": 910.525, "cr": 5, "sf": 7, "bw": 62.5},
       [f"meshcore://{raw_packet.hex()}"]))
try:
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(envelope["publicKey"])).verify(
        bytes.fromhex(envelope["signature"]),
        hashlib.sha256(envelope["data"].encode()).digest())
    envelope_ok = True
except Exception:
    envelope_ok = False
check("the envelope signature verifies like the server will", envelope_ok, True)

check("the same advert is not re-uploaded inside the cooldown",
      uploader.process(dict(raw)), False)
chat = dict(raw, adv_type=1, adv_key="ff" * 32)
check("chat-node adverts stay off the map", uploader.process(chat), False)
forged = dict(raw, adv_key="ee" * 32, pkt_payload=bytes(tampered),
              payload="", raw_hex="")
check("an unverifiable advert is refused", uploader.process(forged), False)
key_mismatch = dict(raw, adv_key="ee" * 32)
check("metadata key must match the signed advert", uploader.process(key_mismatch), False)
check("malformed timestamps are ignored rather than crashing the worker",
      uploader.process(dict(raw, adv_timestamp="not-a-timestamp")), False)
check("malformed public keys are ignored rather than crashing the worker",
      uploader.process(dict(raw, adv_key="not-hex")), False)
check("malformed advert types are ignored rather than crashing the worker",
      uploader.process(dict(raw, adv_type="not-a-type")), False)

keyless = MapUploader(FakeService(), type("L", (), {"identity_key": "", "public_key": ""})())
keyless._post = lambda e: True
check("no signing identity means no upload",
      keyless.process(dict(raw, adv_key="dd" * 32)), False)
uploader.close(); keyless.close()

print()
print("PASS" if not failures else f"FAIL: {failures}")
sys.exit(1 if failures else 0)

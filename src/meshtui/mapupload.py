"""Map Auto Uploader - keep heard repeaters on map.meshcore.io.

The official MeshCore map does not discover nodes; listeners feed it. When a
repeater or room server advert is heard on RF, an opted-in listener verifies
its Ed25519 signature (so only adverts the node really transmitted can be
uploaded), signs the upload with its OWN node identity, and POSTs it. Nodes
no uploader has refreshed for 30 days fall off the map - which is why a mesh
corner with no uploader is a blind spot.

Two cryptographic quirks, both mirrored from the reference implementation
(meshcore-ha's map_uploader.py):

- An advert signs pub_key(32) + timestamp(4) + app_data, with the signature
  spliced in between: payload = key(32) ts(4) sig(64) app_data.
- The radio exports its identity as an EXPANDED Ed25519 key - [scalar 32]
  [prefix 32], supercop layout - so seed-based libraries produce wrong
  signatures; the upload signature is computed with explicit curve math.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger(__name__)

MAP_API_URL = "https://map.meshcore.io/api/v1/uploader/node"

ADV_TYPE_CHAT = 1               # chat nodes are people; the map wants infrastructure
REPLAY_COOLDOWN_SECONDS = 3600
SEEN_MAX = 1000


# ------------------------------------------------------ Ed25519 (supercop)

_P = 2**255 - 19
_ORDER = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_BY = (4 * pow(5, _P - 2, _P)) % _P
_BX = None  # derived below


def _recover_x(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * pow(2, (_P - 1) // 4, _P)) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BX = _recover_x(_BY)
_BASE = (_BX, _BY, 1, (_BX * _BY) % _P)  # extended coordinates


def _edwards_add(p: tuple, q: tuple) -> tuple:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (2 * t1 * t2 * _D) % _P
    dd = (2 * z1 * z2) % _P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalarmult_base(scalar: int) -> bytes:
    q = (0, 1, 1, 0)  # identity
    p = _BASE
    while scalar:
        if scalar & 1:
            q = _edwards_add(q, p)
        p = _edwards_add(p, p)
        scalar >>= 1
    x, y, z, _ = q
    zinv = pow(z, _P - 2, _P)
    x, y = (x * zinv) % _P, (y * zinv) % _P
    encoded = y.to_bytes(32, "little")
    if x & 1:
        encoded = encoded[:-1] + bytes([encoded[-1] | 0x80])
    return encoded


def sign_expanded_ed25519(message: bytes, expanded_key: bytes,
                          public_key: bytes) -> bytes:
    """Sign with a MeshCore-exported [scalar 32][prefix 32] key.

    RFC 8032 signing, except the scalar and prefix are given directly rather
    than derived from a seed - the supercop layout the firmware exports.
    """
    scalar = int.from_bytes(expanded_key[:32], "little")
    prefix = expanded_key[32:64]
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _ORDER
    r_point = _scalarmult_base(r)
    k = int.from_bytes(hashlib.sha512(r_point + public_key + message).digest(),
                       "little") % _ORDER
    s = (r + k * scalar) % _ORDER
    return r_point + s.to_bytes(32, "little")


# ------------------------------------------------------ advert verification

def extract_advert_payload(raw_hex: str) -> bytes | None:
    """The ADVERT payload from a raw LoRa packet: header, optional transport
    code, path, then payload - the same walk the RF-log parser does."""
    try:
        raw = bytes.fromhex(str(raw_hex or "").replace(" ", ""))
    except ValueError:
        return None
    if len(raw) < 2 + 32 + 4 + 64 + 1:
        return None
    skip = 2
    if raw[0] & 0x03 in (0, 3):  # transport codes present
        skip += 4
    if len(raw) <= skip:
        return None
    path_byte = raw[skip]
    path_len = (path_byte & 0x3F) * (((path_byte & 0xC0) >> 6) + 1)
    start = skip + 1 + path_len
    return raw[start:] if len(raw) > start else None


def verify_advert(payload: bytes) -> bool:
    """Firmware signs pub_key(32) + timestamp(4) + app_data; the signature
    sits between timestamp and app_data on the wire."""
    if len(payload) < 32 + 4 + 64 + 1:
        return False
    pubkey, signature = payload[0:32], payload[36:100]
    message = payload[0:36] + payload[100:]
    try:
        Ed25519PublicKey.from_public_bytes(pubkey).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


# --------------------------------------------------------------- uploader

class MapUploader:
    """Service listener: verify heard infrastructure adverts and upload them.

    Runs its network calls on a worker thread - service listeners fire under
    the service lock and must never block.
    """

    def __init__(self, service: Any, link: Any) -> None:
        self.service = service
        self.link = link
        self.uploads = 0
        self._seen: dict[str, tuple[int, float]] = {}  # adv_key -> (adv_ts, uploaded_at)
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="map-upload")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def handle_event(self, kind: str, payload: Any) -> None:
        if kind != "packet" or getattr(payload, "portnum", "") != "RXLOG_APP":
            return
        raw = payload.raw if isinstance(payload.raw, dict) else {}
        if raw.get("payload_type") != 4:  # ADVERT
            return
        self._executor.submit(self.process, dict(raw))

    def _eligible(self, raw: dict) -> tuple[str, str, int] | None:
        adv_key = str(raw.get("adv_key") or "").lower().removeprefix("0x")
        adv_timestamp = raw.get("adv_timestamp")
        if not adv_key or adv_timestamp is None:
            return None
        try:
            key_bytes = bytes.fromhex(adv_key)
            timestamp = int(adv_timestamp)
            advert_type = int(raw.get("adv_type", 0))
        except (TypeError, ValueError):
            return None
        if len(key_bytes) != 32 or timestamp < 0:
            return None
        if advert_type == ADV_TYPE_CHAT:
            return None
        raw_hex = raw.get("payload") or ""
        if isinstance(raw_hex, (bytes, bytearray)):
            raw_hex = raw_hex.hex()
        if not raw_hex and raw.get("raw_hex"):
            raw_hex = str(raw["raw_hex"])[4:]  # strip snr+rssi framing bytes
        if not raw_hex:
            return None
        with self._lock:
            previous = self._seen.get(adv_key)
            if previous is not None:
                prev_ts, uploaded_at = previous
                if timestamp <= prev_ts:
                    return None  # replay of an old advert
                if time.time() - uploaded_at < REPLAY_COOLDOWN_SECONDS:
                    return None  # freshly uploaded already
        return adv_key, str(raw_hex), timestamp

    def process(self, raw: dict) -> bool:
        """Verify, sign, and upload one heard advert. Returns success."""
        eligible = self._eligible(raw)
        if eligible is None:
            return False
        adv_key, raw_hex, adv_timestamp = eligible

        pkt_payload = raw.get("pkt_payload")
        if isinstance(pkt_payload, str):
            try:
                pkt_payload = bytes.fromhex(pkt_payload)
            except ValueError:
                pkt_payload = None
        if not (isinstance(pkt_payload, (bytes, bytearray)) and verify_advert(bytes(pkt_payload))):
            extracted = extract_advert_payload(raw_hex)
            if extracted is None or not verify_advert(extracted):
                log.debug("map upload: advert signature failed for %s", adv_key[:12])
                return False
            pkt_payload = extracted

        # adv_key is parser metadata, while pkt_payload is the data whose
        # signature we actually verified.  Tie the two together before using
        # adv_key for replay suppression or publishing the raw packet.  Without
        # this check a malformed parser event could pair a valid signature with
        # another node's identity.
        if bytes(pkt_payload)[:32].hex() != adv_key:
            log.debug("map upload: advert key does not match signed payload")
            return False

        identity = str(getattr(self.link, "identity_key", "") or "")
        public_key = str(getattr(self.link, "public_key", "") or "").lower()
        if len(identity) != 128 or len(public_key) != 64:
            log.warning("map upload: no signing identity (private-key export "
                        "disabled in firmware?)")
            return False

        info = self.service.state.radio_info
        if not info.get("freq"):
            return False  # radio params not known yet

        def norm(value: Any) -> int | float:
            value = float(value or 0)
            return int(value) if value == int(value) else value

        data = {
            "params": {"freq": norm(info.get("freq")), "cr": int(info.get("cr") or 5),
                       "sf": int(info.get("sf") or 7), "bw": norm(info.get("bw"))},
            "links": [f"meshcore://{raw_hex}"],
        }
        body = json.dumps(data, separators=(",", ":"))
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        signature = sign_expanded_ed25519(digest, bytes.fromhex(identity),
                                          bytes.fromhex(public_key))
        envelope = {"data": body, "signature": signature.hex(),
                    "publicKey": public_key}
        if not self._post(envelope):
            return False
        with self._lock:
            self._seen[adv_key] = (adv_timestamp, time.time())
            while len(self._seen) > SEEN_MAX:
                self._seen.pop(next(iter(self._seen)))
        self.uploads += 1
        log.info("map upload: %s (%s) -> map.meshcore.io",
                 raw.get("adv_name") or "?", adv_key[:12])
        return True

    def _post(self, envelope: dict) -> bool:
        request = urllib.request.Request(
            MAP_API_URL, data=json.dumps(envelope).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "meshtui-map-uploader"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read(512)
            return True
        except Exception as exc:  # noqa: BLE001 - the mesh must not care
            log.warning("map upload failed: %s", exc)
            return False

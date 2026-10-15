"""Delivery authentication and anti-spoofing (doc 1.8 / 1.10).

* The dispatch service signs each landing marker with Ed25519. The marker
  payload binds order id, site id, the surveyed pad position, expiry and a
  nonce. The drone carries only the dispatch *public* key.
* Verification rejects: bad signature (counterfeit marker), wrong order,
  expired ticket, replayed nonce, and - crucially for urban spoofing - a
  genuine marker that has been *moved* (decoded position disagrees with the
  surveyed position or the drone's own navigation fix).
* Parcel release additionally needs a one-time code from the recipient's app
  (HMAC-based, RFC 4226 style), so a stolen marker alone is not enough.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
from dataclasses import dataclass

import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


@dataclass(frozen=True)
class MarkerClaims:
    order_id: str
    site_id: str
    position: tuple[float, float, float]
    expires_at: float
    nonce: str


class MarkerSigner:
    """Lives in the dispatch backend (never on the aircraft)."""

    def __init__(self, key: Ed25519PrivateKey | None = None) -> None:
        self._key = key or Ed25519PrivateKey.generate()

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

    def sign(self, claims: MarkerClaims) -> bytes:
        body = json.dumps(claims.__dict__, sort_keys=True, separators=(",", ":")).encode()
        sig = self._key.sign(body)
        return body + b"." + sig.hex().encode()


@dataclass
class VerifyResult:
    ok: bool
    reason: str
    claims: MarkerClaims | None = None


class MarkerVerifier:
    """Runs onboard."""

    def __init__(self, dispatch_public_key: Ed25519PublicKey, position_tolerance_m: float = 1.5) -> None:
        self.pub = dispatch_public_key
        self.tol = position_tolerance_m
        self.seen_nonces: set[str] = set()

    def verify(
        self,
        payload: bytes,
        expected_order: str,
        expected_site_position: np.ndarray,
        observed_marker_position: np.ndarray,
        now: float,
    ) -> VerifyResult:
        try:
            body, sig_hex = payload.rsplit(b".", 1)
            self.pub.verify(bytes.fromhex(sig_hex.decode()), body)
            claims = MarkerClaims(**json.loads(body))
        except (ValueError, InvalidSignature, TypeError, json.JSONDecodeError):
            return VerifyResult(False, "invalid signature: counterfeit or damaged marker")
        if claims.order_id != expected_order:
            return VerifyResult(False, f"marker belongs to order {claims.order_id}, not {expected_order}", claims)
        if now > claims.expires_at:
            return VerifyResult(False, "marker ticket expired", claims)
        if claims.nonce in self.seen_nonces:
            return VerifyResult(False, "replayed marker nonce", claims)
        signed_pos = np.array(claims.position)
        if np.linalg.norm(signed_pos - expected_site_position) > self.tol:
            return VerifyResult(False, "signed position does not match the surveyed site", claims)
        if np.linalg.norm(observed_marker_position[:2] - signed_pos[:2]) > self.tol:
            return VerifyResult(False, "marker has been moved from its signed position (relocation spoof)", claims)
        self.seen_nonces.add(claims.nonce)
        return VerifyResult(True, "marker authentic", claims)


def new_nonce() -> str:
    return os.urandom(12).hex()


def one_time_code(secret: bytes, counter: int, digits: int = 6) -> str:
    """HOTP (RFC 4226) - the recipient app shows it, the drone checks it."""
    mac = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off : off + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return f"{code:0{digits}d}"


def verify_one_time_code(secret: bytes, counter: int, code: str, window: int = 1) -> bool:
    return any(hmac.compare_digest(one_time_code(secret, c), code) for c in range(counter, counter + window + 1))

import numpy as np

from aidrone.security import (
    MarkerClaims,
    MarkerSigner,
    MarkerVerifier,
    new_nonce,
    one_time_code,
    verify_one_time_code,
)

SITE = np.array([100.0, 200.0, 0.0])


def _setup():
    signer = MarkerSigner()
    verifier = MarkerVerifier(signer.public_key)
    claims = MarkerClaims("ORD-1", "site", tuple(SITE), expires_at=1000.0, nonce=new_nonce())
    return signer, verifier, signer.sign(claims)


def test_authentic_marker_accepted_once():
    _, v, payload = _setup()
    assert v.verify(payload, "ORD-1", SITE, SITE, now=10).ok
    replay = v.verify(payload, "ORD-1", SITE, SITE, now=11)
    assert not replay.ok and "replay" in replay.reason


def test_counterfeit_marker_rejected():
    _, v, _ = _setup()
    fake = MarkerSigner().sign(MarkerClaims("ORD-1", "site", tuple(SITE), 1000.0, new_nonce()))
    res = v.verify(fake, "ORD-1", SITE, SITE, now=10)
    assert not res.ok and "signature" in res.reason


def test_tampered_payload_rejected():
    _, v, payload = _setup()
    tampered = payload.replace(b"ORD-1", b"ORD-2")
    assert not v.verify(tampered, "ORD-2", SITE, SITE, now=10).ok
    assert not v.verify(b"garbage", "ORD-1", SITE, SITE, now=10).ok


def test_relocated_marker_rejected():
    _, v, payload = _setup()
    res = v.verify(payload, "ORD-1", SITE, SITE + np.array([6.0, 0, 0]), now=10)
    assert not res.ok and "moved" in res.reason


def test_wrong_order_and_expiry():
    _, v, payload = _setup()
    assert "belongs to order" in v.verify(payload, "ORD-9", SITE, SITE, now=10).reason
    assert "expired" in v.verify(payload, "ORD-1", SITE, SITE, now=5000).reason


def test_one_time_code():
    secret = b"0123456789abcdef0123"
    code = one_time_code(secret, 7)
    assert len(code) == 6 and code.isdigit()
    assert verify_one_time_code(secret, 7, code)
    assert not verify_one_time_code(secret, 7, "000000" if code != "000000" else "111111")
    # RFC 4226 test vector
    assert one_time_code(b"12345678901234567890", 0) == "755224"

from __future__ import annotations

import hashlib
import hmac
import string
import time


def sign_dashboard_session(secret: str, ttl_minutes: int) -> str:
    if not secret.strip():
        raise ValueError("secret must be non-empty")
    if ttl_minutes <= 0:
        raise ValueError("ttl_minutes must be positive")
    # Intentional stateless token: sessions sharing an expiry collide, and revocation is
    # all-or-nothing through expiry or dashboard-session-secret rotation.
    expires = int(time.time()) + ttl_minutes * 60
    signature = hmac.new(secret.encode(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def verify_dashboard_session(secret: str, token: str | None) -> bool:
    if not secret or not token:
        return False
    try:
        expires_raw, signature = token.split(".", 1)
        expires = int(expires_raw)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    expected = hmac.new(secret.encode(), expires_raw.encode(), hashlib.sha256).hexdigest()
    if not is_hex_digest(signature, len(expected)):
        return False
    return hmac.compare_digest(signature, expected)


def verify_onscreen_signature(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    *,
    tolerance_seconds: int = 300,
) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > tolerance_seconds:
        return False
    if not signature.startswith("sha256="):
        return False
    signed = timestamp.encode() + b"." + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    provided = signature.removeprefix("sha256=")
    if not is_hex_digest(provided, len(expected)):
        return False
    return hmac.compare_digest(provided, expected)


def is_hex_digest(value: str, expected_length: int) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    if len(value) != expected_length:
        return False
    return all(char in string.hexdigits for char in value)

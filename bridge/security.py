from __future__ import annotations

import hashlib
import hmac
import math
import string
import time
from collections.abc import Callable


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


class LoginThrottle:
    """In-memory per-client-IP throttle for dashboard login brute force.

    Single-process state only: the throttle lives in memory, so deployment must
    stay single-worker — the reference Dockerfile/Compose run one uvicorn
    worker by design (SQLite is single-writer too). A restart clears counters,
    which is acceptable for a bridge deployment. Multi-replica deployments
    would need shared atomic storage instead of this class. The clock is
    injectable so tests can advance time without sleeping.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        lockout_seconds: float = 60.0,
        failure_window_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lockout_seconds <= 0:
            raise ValueError("lockout_seconds must be positive")
        if failure_window_seconds <= 0:
            raise ValueError("failure_window_seconds must be positive")
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._failure_window_seconds = failure_window_seconds
        self._clock = clock
        # ip -> (count, last failure monotonic timestamp); pruned when idle
        # beyond failure_window_seconds so the maps cannot grow unbounded.
        self._failures: dict[str, tuple[int, float]] = {}
        self._locked_until: dict[str, float] = {}

    def _prune_stale(self) -> None:
        now = self._clock()
        cutoff = now - self._failure_window_seconds
        for ip in [ip for ip, (_, last) in self._failures.items() if last < cutoff]:
            del self._failures[ip]
        for ip in [ip for ip, until in self._locked_until.items() if until <= now]:
            del self._locked_until[ip]

    def retry_after_seconds(self, client_ip: str) -> int:
        self._prune_stale()
        locked_until = self._locked_until.get(client_ip)
        if locked_until is None:
            return 0
        remaining = locked_until - self._clock()
        if remaining <= 0:
            # Lockout expired: clear the slate so the client starts fresh.
            self._locked_until.pop(client_ip, None)
            self._failures.pop(client_ip, None)
            return 0
        return max(1, math.ceil(remaining))

    def record_failure(self, client_ip: str) -> None:
        if self.retry_after_seconds(client_ip) > 0:
            return
        count = self._failures.get(client_ip, (0, 0.0))[0] + 1
        if count >= self._max_attempts:
            self._failures.pop(client_ip, None)
            self._locked_until[client_ip] = self._clock() + self._lockout_seconds
            return
        self._failures[client_ip] = (count, self._clock())

    def record_success(self, client_ip: str) -> None:
        self._failures.pop(client_ip, None)
        self._locked_until.pop(client_ip, None)

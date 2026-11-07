"""Password hashing with the standard library's scrypt (memory-hard; no extra package).

Parameters follow OWASP's scrypt recommendation (N=2^17, r=8, p=1: about
128 MiB and a few hundred milliseconds per hash). Hashes are self-describing
(``scrypt$n$r$p$salt$key``), so stronger parameters can be introduced later:
``needs_rehash`` tells the login code to re-hash on the next successful login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ALGORITHM = "scrypt"
_N, _R, _P = 2**17, 8, 1
_SALT_BYTES = 16
_KEY_BYTES = 64
_MAXMEM = 256 * 1024 * 1024


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_KEY_BYTES, maxmem=_MAXMEM)


def hash_password(password: str) -> str:
    """Return a self-describing hash: `scrypt$n$r$p$salt$key` (base64 parts)."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    key = _derive(password, salt, _N, _R, _P)
    return f"{_ALGORITHM}${_N}${_R}${_P}${_b64(salt)}${_b64(key)}"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt_b64, key_b64 = stored_hash.split("$")
        if algorithm != _ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        actual = _derive(password, salt, int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored_hash: str) -> bool:
    try:
        algorithm, n, r, p, *_ = stored_hash.split("$")
        return algorithm != _ALGORITHM or (int(n), int(r), int(p)) < (_N, _R, _P)
    except ValueError:
        return True


# Verified against when a username does not exist, so a login takes the same
# time whether or not the account exists (no username probing by timing).
_DUMMY_HASH: str | None = None


def burn_time(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _DUMMY_HASH)


def new_token() -> str:
    """A random session or CSRF token (256 bits)."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """Session tokens are stored only as their SHA-256, never in clear."""
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()

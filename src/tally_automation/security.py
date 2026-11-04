"""Password hashing using the standard library's scrypt."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ALGORITHM = "scrypt"
_N, _R, _P = 2**14, 8, 1
_SALT_BYTES = 16
_KEY_BYTES = 64


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_KEY_BYTES, maxmem=64 * 1024 * 1024
    )


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

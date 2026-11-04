import pytest

from tally_automation.security import hash_password, verify_password


def test_hash_round_trip() -> None:
    stored = hash_password("synthetic-password")
    assert stored.startswith("scrypt$")
    assert "synthetic-password" not in stored
    assert verify_password("synthetic-password", stored)
    assert not verify_password("wrong-password", stored)


def test_hashes_are_salted() -> None:
    assert hash_password("same") != hash_password("same")


def test_empty_password_rejected() -> None:
    with pytest.raises(ValueError):
        hash_password("")


@pytest.mark.parametrize("stored", ["", "garbage", "bcrypt$1$2$3$a$b", "scrypt$x$8$1$AA==$AA=="])
def test_malformed_hash_fails_closed(stored: str) -> None:
    assert verify_password("anything", stored) is False

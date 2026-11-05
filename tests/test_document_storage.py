import hashlib
import io
import os
import stat
from pathlib import Path

import pytest

from tally_automation.documents.storage import DocumentStorage
from tally_automation.documents.validation import FileValidationError

DATA = b"synthetic file content " * 100
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def storage(tmp_path: Path) -> DocumentStorage:
    return DocumentStorage(tmp_path / "uploads")


def test_receive_hashes_while_streaming(storage: DocumentStorage) -> None:
    temp, sha, size = storage.receive(io.BytesIO(DATA), max_bytes=10_000)
    assert sha == SHA
    assert size == len(DATA)
    assert temp.read_bytes() == DATA
    assert temp.parent == storage.incoming


def test_receive_enforces_size_limit_and_cleans_up(storage: DocumentStorage) -> None:
    with pytest.raises(FileValidationError) as exc:
        storage.receive(io.BytesIO(DATA), max_bytes=len(DATA) - 1)
    assert exc.value.code == "file_too_large"
    assert list(storage.incoming.iterdir()) == []


def test_receive_rejects_empty(storage: DocumentStorage) -> None:
    with pytest.raises(FileValidationError) as exc:
        storage.receive(io.BytesIO(b""), max_bytes=100)
    assert exc.value.code == "empty_file"
    assert list(storage.incoming.iterdir()) == []


def test_relative_path_is_content_addressed() -> None:
    assert DocumentStorage.relative_path(SHA, ".pdf") == f"originals/{SHA[:2]}/{SHA}.pdf"


@pytest.mark.parametrize("bad", ["", "abc", "../" + "a" * 61, "G" * 64, SHA.upper()])
def test_relative_path_rejects_non_hashes(bad: str) -> None:
    with pytest.raises(ValueError):
        DocumentStorage.relative_path(bad, ".pdf")


@pytest.mark.parametrize("path", ["../outside.pdf", "originals/../../x.pdf", "incoming/a.part", "C:/x.pdf"])
def test_resolve_refuses_paths_outside_originals(storage: DocumentStorage, path: str) -> None:
    with pytest.raises(ValueError):
        storage.resolve(path)


def test_promote_stores_read_only_once(storage: DocumentStorage) -> None:
    relative = DocumentStorage.relative_path(SHA, ".pdf")
    temp, _, _ = storage.receive(io.BytesIO(DATA), max_bytes=10_000)
    assert storage.promote(temp, relative) is True
    final = storage.resolve(relative)
    assert final.read_bytes() == DATA
    assert not os.stat(final).st_mode & stat.S_IWRITE
    assert not temp.exists()

    # Identical content again: not rewritten, temp discarded.
    temp2, _, _ = storage.receive(io.BytesIO(DATA), max_bytes=10_000)
    assert storage.promote(temp2, relative) is False
    assert not temp2.exists()
    assert final.read_bytes() == DATA


def test_remove_original(storage: DocumentStorage) -> None:
    relative = DocumentStorage.relative_path(SHA, ".pdf")
    temp, _, _ = storage.receive(io.BytesIO(DATA), max_bytes=10_000)
    storage.promote(temp, relative)
    storage.remove_original(relative)
    assert not storage.resolve(relative).exists()

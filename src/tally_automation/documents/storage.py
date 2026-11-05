"""Content-addressed, write-once storage for original documents.

Layout: ``<upload_dir>/originals/<first 2 hex of sha256>/<sha256><ext>``.
File names are derived only from the hash, so user input can never influence a
path (no traversal). Stored files are made read-only and never overwritten.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import BinaryIO

from tally_automation.documents.validation import FileValidationError

CHUNK_SIZE = 1024 * 1024


class DocumentStorage:
    def __init__(self, upload_dir: Path) -> None:
        self.root = upload_dir.resolve()
        self.originals = self.root / "originals"
        self.incoming = self.root / "incoming"

    def ensure_directories(self) -> None:
        self.originals.mkdir(parents=True, exist_ok=True)
        self.incoming.mkdir(parents=True, exist_ok=True)

    def receive(self, source: BinaryIO, max_bytes: int) -> tuple[Path, str, int]:
        """Copy an upload to a private temp file, hashing as it streams.

        Returns (temp_path, sha256_hex, size). Raises FileValidationError when the
        stream is empty or exceeds ``max_bytes``; the temp file is then removed.
        """
        self.ensure_directories()
        temp_path = self.incoming / f"{secrets.token_hex(16)}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp_path.open("xb") as target:
                while chunk := source.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileValidationError(
                            "file_too_large",
                            f"The file exceeds the {max_bytes // (1024 * 1024)} MB limit.",
                        )
                    digest.update(chunk)
                    target.write(chunk)
            if size == 0:
                raise FileValidationError("empty_file", "The file is empty.")
        except BaseException:
            self.discard(temp_path)
            raise
        return temp_path, digest.hexdigest(), size

    @staticmethod
    def relative_path(sha256: str, extension: str) -> str:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("invalid sha256")
        return f"originals/{sha256[:2]}/{sha256}{extension}"

    def resolve(self, relative_path: str) -> Path:
        # Lexical normalisation, not Path.resolve(): on Windows, resolve() queries
        # the filesystem and can return a different form of the path while another
        # thread is linking that same file, which would fail the check below.
        path = Path(os.path.normpath(self.root / relative_path))
        if Path(relative_path).is_absolute() or not path.is_relative_to(self.originals):
            raise ValueError("storage path escapes the originals directory")
        return path

    def promote(self, temp_path: Path, relative_path: str) -> bool:
        """Move a received file into permanent storage.

        Returns True if the file was newly stored, False if identical content was
        already stored (same SHA-256), in which case the temp file is discarded.
        """
        final_path = self.resolve(relative_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            self.discard(temp_path)
            return False
        try:
            # os.link fails if the target exists, so a concurrent writer can never be clobbered.
            os.link(temp_path, final_path)
        except FileExistsError:
            self.discard(temp_path)
            return False
        self.discard(temp_path)
        os.chmod(final_path, stat.S_IREAD)
        return True

    def remove_original(self, relative_path: str) -> None:
        """Delete a stored original. Only for rolling back a failed first ingest."""
        path = self.resolve(relative_path)
        if path.exists():
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            path.unlink()

    @staticmethod
    def discard(temp_path: Path) -> None:
        temp_path.unlink(missing_ok=True)

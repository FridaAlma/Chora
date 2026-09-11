"""
Test per l'estrazione metadati.
"""

import tempfile
from pathlib import Path

from core.ingestion.metadata import FileMetadata


def test_basic_metadata():
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
        f.write("test content")
        tmppath = f.name

    try:
        meta = FileMetadata(tmppath)
        d = meta.to_dict()

        assert d["file_name"].endswith(".txt")
        assert d["size_bytes"] > 0
        assert len(d["sha256"]) == 64  # SHA-256 hex
        assert d["mime_type"] == "text/plain"
        assert "created" in d
        assert "modified" in d
    finally:
        Path(tmppath).unlink(missing_ok=True)


def test_sha256_consistency():
    """Lo stesso file deve produrre lo stesso hash."""
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False, mode="wb") as f:
        f.write(b"hello world" * 1000)
        tmppath = f.name

    try:
        m1 = FileMetadata(tmppath)
        m2 = FileMetadata(tmppath)
        assert m1.sha256 == m2.sha256, "SHA-256 inconsistente"
    finally:
        Path(tmppath).unlink(missing_ok=True)


def test_mime_type_by_extension():
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
        f.write("print('hello')")
        tmppath = f.name

    try:
        meta = FileMetadata(tmppath)
        assert meta.mime_type == "text/x-python"
    finally:
        Path(tmppath).unlink(missing_ok=True)

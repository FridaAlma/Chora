"""
Test per lo scanner del file system.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.ingestion.scanner import FileScanner, _should_skip


def test_should_skip():
    assert _should_skip(Path(".hidden/file.txt"))
    assert _should_skip(Path("project/__pycache__/module.pyc"))
    assert _should_skip(Path("project/.git/HEAD"))
    assert not _should_skip(Path("project/src/main.py"))
    assert not _should_skip(Path("project/README.md"))


def test_scan_clean_file():
    """File pulito → deve essere indicizzato."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
        f.write("Contenuto pulito, nessun HSD.")
        tmppath = Path(f.name)

    try:
        db = MagicMock()
        db.get_file_by_sha256.return_value = None  # nessun duplicato
        db.create_node.return_value = "node-uuid-123"
        db.register_file.return_value = 99
        db.enqueue.return_value = 1

        scanner = FileScanner(db=db, device_name="test")
        result = scanner.scan_file(tmppath)

        assert result.success
        assert result.node_id == "node-uuid-123"
        assert not result.skipped
    finally:
        tmppath.unlink(missing_ok=True)


def test_scan_hsd_file():
    """File con HSD → deve essere saltato e messo in quarantena."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
        f.write('API_KEY="sk-1234567890abcdef1234567890abcdef"')
        tmppath = Path(f.name)

    try:
        db = MagicMock()

        scanner = FileScanner(db=db, device_name="test")
        # La quarantena usa il filesystem reale per test
        with tempfile.TemporaryDirectory() as qdir:
            scanner.quarantine.base_dir = Path(qdir)
            result = scanner.scan_file(tmppath)

            assert result.skipped
            assert result.skip_reason == "HSD"

            # Verifica che NON sia stato creato un nodo
            db.create_node.assert_not_called()
    finally:
        tmppath.unlink(missing_ok=True)


def test_scan_directory(tmp_path: Path):
    """Scansione di una directory con alcuni file."""
    db = MagicMock()
    db.get_file_by_sha256.return_value = None  # nessun duplicato
    db.create_node.return_value = "project-uuid"
    db.register_file.return_value = 99
    db.enqueue.return_value = 1

    # Prepara struttura di test
    d = tmp_path / "test_project"
    d.mkdir()
    (d / "main.py").write_text("print('hello')")
    (d / "README.md").write_text("# Test")
    (d / "data.txt").write_text("some data")

    scanner = FileScanner(db=db, device_name="test")
    results = scanner.scan_directory(d, project_label="TestProject")

    assert len(results) == 3
    assert all(r.success for r in results)
    assert db.create_node.call_count >= 1  # almeno il project + files

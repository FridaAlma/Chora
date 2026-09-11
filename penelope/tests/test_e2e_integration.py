"""
Test E2E di integrazione — Penelope pipeline completa.

Verifica l'intero flusso:
  1. Scansione file → MariaDB + coda
  2. Elaborazione coda → embedding, NER, face detection
  3. Query ChromaDB
  4. Query grafo (NetworkX bridge)

Usa un database MariaDB temporaneo (o SQLite mock) e files temporanei.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def temp_workspace(tmp_path) -> Path:
    """Crea un workspace temporaneo con file di test."""
    # Crea struttura
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()

    # File di testo
    (project_dir / "note.txt").write_text(
        "Oggi ho incontrato Mario Rossi a Roma. Era un bel giorno di primavera.",
        encoding="utf-8",
    )
    (project_dir / "readme.md").write_text(
        "# Progetto Test\nQuesto è un progetto di esempio per testare Penelope.",
        encoding="utf-8",
    )
    (project_dir / "codice.py").write_text(
        "def hello():\n    print('Ciao mondo!')\n",
        encoding="utf-8",
    )

    # File con HSD (deve finire in quarantena)
    (project_dir / "secret.txt").write_text(
        "password = 'supersecret123'\nAPI_KEY = 'abcdefghijklmnopqrstuvwx1234567890'\n",
        encoding="utf-8",
    )

    # File binario finto (immagine)
    img_path = project_dir / "foto.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01")  # header JPEG finto

    return project_dir


@pytest.fixture
def mock_db() -> Generator:
    """Crea un MariaDBStore mock per test di integrazione."""
    db = MagicMock()
    db.__enter__.return_value = db
    db._conn = MagicMock()

    # Mock per get_node
    def get_node_side_effect(node_id):
        return {
            "id": node_id,
            "type": "File",
            "label": "test.txt",
            "metadata": json.dumps({"size_bytes": 100, "mime_type": "text/plain"}),
        }
    db.get_node.side_effect = get_node_side_effect

    # Mock per _query
    def query_side_effect(sql, params=None):
        if "file_registry" in sql and "sha256" in sql:
            return []  # nessun duplicato
        if "nodes" in sql and "label" in sql:
            return []  # nessun nodo esistente
        return []

    db._query.side_effect = query_side_effect
    db.create_node.return_value = "mock-node-uuid"
    db.enqueue.return_value = 1
    db.reset_stale_processing.return_value = 0

    # Context manager
    db.__enter__.return_value = db
    yield db


class TestE2EPipeline:
    """Test dell'intera pipeline di Penelope."""

    def test_scan_to_queue(self, temp_workspace, mock_db):
        """Test: scansione → registrazione MariaDB → coda."""
        from core.ingestion.scanner import FileScanner
        from egida.filters import HSDFilter
        from egida.quarantine import Quarantine

        quarantine_dir = temp_workspace / "quarantine"
        scanner = FileScanner(
            db=mock_db,
            hsd_filter=HSDFilter(),
            quarantine=Quarantine(str(quarantine_dir)),
            device_name="test-device",
        )

        results = scanner.scan_directory(
            temp_workspace / "test_project",
            project_label="test-project",
        )

        # Verifica che i file siano stati processati
        assert len(results) > 0, "Nessun file processato"

        # Controlla split tra file normali e HSD
        hsd_files = [r for r in results if r.skipped and r.skip_reason == "HSD"]
        normal_files = [r for r in results if r.success]

        assert len(hsd_files) >= 1, "File HSD non rilevato"

        # Verifica che secret.txt sia in quarantena
        secret_results = [r for r in results if "secret.txt" in r.file_path]
        assert any(r.skipped for r in secret_results), "secret.txt non bloccato da Egida"

        # Verifica che file normali siano stati registrati
        assert any("note.txt" in r.file_path for r in normal_files)
        assert any("readme.md" in r.file_path for r in normal_files)

        # Verifica chiamate a create_node e enqueue
        assert mock_db.create_node.call_count >= 1
        assert mock_db.enqueue.call_count >= len(normal_files)

    def test_duplicate_sha256_skipped(self, temp_workspace, mock_db):
        """Test: file duplicato (stesso SHA-256) deve essere saltato."""
        from core.ingestion.scanner import FileScanner

        # Simula file già esistente
        def query_with_dup(sql, params=None):
            if "sha256" in (sql or ""):
                return [{"node_id": "existing-uuid", "path": "/already/indexed.txt"}]
            return []
        mock_db._query.side_effect = query_with_dup

        scanner = FileScanner(db=mock_db, device_name="test-device")

        # Crea file e scansione due volte
        dup_file = temp_workspace / "test_project" / "dup_test.txt"
        dup_file.write_text("Contenuto duplicato", encoding="utf-8")

        result = scanner.scan_file(str(dup_file))
        assert result.skipped
        assert result.skip_reason == "DUPLICATE"

    def test_dispatcher_processes_items(self, mock_db):
        """Test: dispatcher elabora elementi dalla coda."""
        from core.ingestion.dispatcher import Dispatcher

        # Mock dequeue per restituire elementi
        mock_db.dequeue.return_value = [
            {"id": 1, "node_id": "node-1", "status": "pending", "priority": 0},
            {"id": 2, "node_id": "node-2", "status": "pending", "priority": 0},
        ]

        dispatcher = Dispatcher(db=mock_db)
        count = dispatcher.process_queue(batch_size=5)

        assert count == 2  # entrambi processati
        assert mock_db.mark_done.call_count == 2

    def test_dispatcher_loop_resets_stale(self, mock_db):
        """Test: loop dispatcher resetta elementi stale all'avvio."""
        from core.ingestion.dispatcher import Dispatcher

        mock_db.reset_stale_processing.return_value = 3

        d = Dispatcher(db=mock_db)
        import threading
        timer = threading.Timer(0.1, d.stop)
        timer.start()
        d.run_loop(interval=0.05, batch_size=5, reset_stale_on_start=True)

        mock_db.reset_stale_processing.assert_called_once_with(max_age_minutes=5)

    def test_graph_bridge_load_and_query(self, temp_workspace):
        """Test: GraphBridge carica da MariaDB e interroga il grafo."""
        from core.db.graph_bridge import GraphBridge

        db_mock = MagicMock()
        db_mock.__enter__.return_value = db_mock

        # Mock dati del database
        db_mock._query.side_effect = [
            # Nodi
            [
                {"id": "n1", "type": "Person", "label": "Mario", "metadata": '{"age":30}', "created_at": "2024-01-01"},
                {"id": "n2", "type": "File", "label": "note.txt", "metadata": '{"size":100}', "created_at": "2024-01-01"},
                {"id": "n3", "type": "Location", "label": "Roma", "metadata": '{}', "created_at": "2024-01-01"},
            ],
            # Archi
            [
                {"id": 1, "source_id": "n2", "target_id": "n1", "relation": "MENTIONS",
                 "weight": 1.0, "metadata": '{}', "created_at": "2024-01-01"},
                {"id": 2, "source_id": "n2", "target_id": "n3", "relation": "MENTIONS",
                 "weight": 1.0, "metadata": '{}', "created_at": "2024-01-01"},
            ],
        ]

        bridge = GraphBridge(db=db_mock)
        bridge.load_from_db()

        assert bridge.graph.number_of_nodes() == 3
        assert bridge.graph.number_of_edges() == 2

        # Test query vicini
        neighbors = bridge.get_neighbors("n2")
        assert len(neighbors) == 2

        # Test subgraph filtering
        persons = bridge.get_subgraph(node_type="Person")
        assert persons.number_of_nodes() == 1
        assert persons.nodes["n1"]["type"] == "Person"

        # Test shortest path
        path = bridge.shortest_path("n1", "n3")
        assert path is not None
        assert len(path) == 3  # n1 -> n2 -> n3

    def test_chroma_index_and_search(self):
        """Test: ChromaStore indicizza e cerca (con directory temporanea)."""
        import tempfile
        import shutil
        from pathlib import Path

        tmpdir = Path(tempfile.mkdtemp())
        try:
            from core.db.chroma_store import ChromaStore

            store = ChromaStore(persist_dir=str(tmpdir))

            # Indicizza testi
            store.index_text("doc1", "Il gatto dorme sul divano",
                             metadata={"file_name": "gatto.txt", "mime_type": "text/plain"})
            store.index_text("doc2", "La macchina rossa va veloce",
                             metadata={"file_name": "macchina.txt", "mime_type": "text/plain"})
            store.index_text("doc3", "Oggi piove e fa molto freddo",
                             metadata={"file_name": "pioggia.txt", "mime_type": "text/plain"})

            assert store.count() == 3

            # Ricerca semantica
            results = store.search_similar("gatto", top_k=5)
            assert any(r["node_id"] == "doc1" for r in results)

            results = store.search_similar("automobile", top_k=5)
            assert any(r["node_id"] == "doc2" for r in results)

            # Filtro MIME
            results = store.search_similar("gatto", top_k=5, filter_mime="text/plain")
            assert len(results) > 0

            # Query vuota
            assert store.search_similar("", top_k=5) == []

            store.close()
        finally:
            shutil.rmtree(str(tmpdir), ignore_errors=True)

    def test_egida_filters_hsd(self, temp_workspace):
        """Test: Egida rileva HSD in file con password/token."""
        from egida.filters import HSDFilter

        hsd = HSDFilter()

        # Test su file pulito
        clean_file = temp_workspace / "note.txt"
        result = hsd.check_file(str(clean_file))
        assert not result.is_infected, "File pulito non dovrebbe essere HSD"

        # Test su file con password
        secret_file = temp_workspace / "secret.txt"
        result = hsd.check_file(str(secret_file))
        assert result.is_infected, "File con password dovrebbe essere HSD"
        assert len(result.matches) >= 1

        # Test quick_scan
        matches = hsd.check_text("password = 'supersecret123'")
        assert len(matches) >= 1

    def test_scene_detection_non_video(self):
        """Test: scene detection su file non video ritorna False."""
        from core.ingestion.processor import process_scene_detection
        db = MagicMock()

        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            result = process_scene_detection("node-1", f.name, db)
        assert not result

    def test_face_detection_non_image(self):
        """Test: face detection su file non immagine ritorna False."""
        from core.ingestion.processor import process_face_detection
        db = MagicMock()

        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            result = process_face_detection("node-1", f.name, db)
        assert not result

    def test_ner_skips_non_text(self):
        """Test: NER su file non testuale ritorna 0."""
        from core.ingestion.processor import process_ner
        db = MagicMock()

        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            result = process_ner("node-1", f.name, db)
        assert result == 0

    def test_exif_skips_non_image(self):
        """Test: EXIF su file non immagine ritorna False."""
        from core.ingestion.processor import process_exif
        db = MagicMock()

        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            result = process_exif("node-1", f.name, db)
        assert not result

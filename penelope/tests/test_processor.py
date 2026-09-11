"""
Test per Processor — stage di elaborazione lazy.

Testa ogni stage separatamente (EXIF, embedding, NER, face detection, scene detection).
Usa mock per il database e file temporanei reali per i file system.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─── Helper per mock del DB ────────────────────────────────────────

def _make_db(**kwargs):
    db = MagicMock(**kwargs)
    db.__enter__.return_value = db
    db.get_node.return_value = {
        "id": "node-1",
        "type": "File",
        "label": "test.txt",
        "metadata": None,
    }
    db._query.return_value = []
    return db


# ─── EXIF ──────────────────────────────────────────────────────────

def test_process_exif_no_image():
    """File non immagine → skip."""
    from core.ingestion.processor import process_exif
    db = _make_db()
    result = process_exif("node-1", "/tmp/test.txt", db)
    assert not result


def test_process_exif_no_pillow():
    """Pillow non installato → skip graceful."""
    from core.ingestion.processor import process_exif
    db = _make_db()
    with patch.dict("sys.modules", {"PIL": None}):
        # Crea un file immagine finto
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            result = process_exif("node-1", f.name, db)
        assert not result  # skip perché PIL non disponibile


# ─── Embedding testo ───────────────────────────────────────────────

def test_process_embedding_non_text():
    """File non testuale → skip."""
    from core.ingestion.processor import process_embedding
    db = _make_db()
    chroma = MagicMock()
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        result = process_embedding("node-1", f.name, db, chroma)
    assert not result


def test_process_embedding_too_short():
    """File troppo corto → skip."""
    from core.ingestion.processor import process_embedding
    db = _make_db()
    chroma = MagicMock()
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
        f.write("corto")
        tmppath = f.name
    try:
        result = process_embedding("node-1", tmppath, db, chroma)
        assert not result  # skip perché troppo corto (< 20 char)
    finally:
        Path(tmppath).unlink(missing_ok=True)


def test_process_embedding_success():
    """File testuale valido → embedding generato."""
    from core.ingestion.processor import process_embedding
    db = _make_db()
    chroma = MagicMock()
    chroma.index_text.return_value = True
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
        f.write("Questo è un file di test con abbastanza contenuto per generare un embedding.")
        tmppath = f.name
    try:
        result = process_embedding("node-1", tmppath, db, chroma)
        assert result
    finally:
        Path(tmppath).unlink(missing_ok=True)


# ─── Embedding immagini (CLIP) ─────────────────────────────────────

def test_process_image_embedding_non_image():
    """File non immagine → skip."""
    from core.ingestion.processor import process_image_embedding
    db = _make_db()
    chroma = MagicMock()
    with tempfile.NamedTemporaryFile(suffix=".txt") as f:
        result = process_image_embedding("node-1", f.name, db, chroma)
    assert not result


# ─── NER ────────────────────────────────────────────────────────────

def test_process_ner_non_text():
    """File non testuale → skip."""
    from core.ingestion.processor import process_ner
    db = _make_db()
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        result = process_ner("node-1", f.name, db)
    assert result == 0


def test_process_ner_binary_file():
    """File non testuale → 0 entità (nessuna chiamata a SpaCy)."""
    from core.ingestion.processor import process_ner
    db = _make_db()
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        result = process_ner("node-1", f.name, db)
    assert result == 0


# ─── Face detection ────────────────────────────────────────────────

def test_process_face_detection_non_image():
    """File non immagine → skip."""
    from core.ingestion.processor import process_face_detection
    db = _make_db()
    with tempfile.NamedTemporaryFile(suffix=".txt") as f:
        result = process_face_detection("node-1", f.name, db)
    assert not result


def test_process_face_detection_no_yolo():
    """Ultralytics non installato → skip graceful."""
    from core.ingestion.processor import process_face_detection
    db = _make_db()
    with patch.dict("sys.modules", {"ultralytics": None}):
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            result = process_face_detection("node-1", f.name, db)
        assert not result


def test_process_face_detection_svg_skip():
    """File SVG → skip."""
    from core.ingestion.processor import process_face_detection
    db = _make_db()
    with tempfile.NamedTemporaryFile(suffix=".svg", mode="w") as f:
        f.write("<svg></svg>")
        result = process_face_detection("node-1", f.name, db)
    assert not result


# ─── Scene detection ───────────────────────────────────────────────

def test_process_scene_detection_non_video():
    """File non video → skip."""
    from core.ingestion.processor import process_scene_detection
    db = _make_db()
    with tempfile.NamedTemporaryFile(suffix=".txt") as f:
        result = process_scene_detection("node-1", f.name, db)
    assert not result


def test_process_scene_detection_no_scenedetect():
    """PySceneDetect non installato → skip graceful."""
    from core.ingestion.processor import process_scene_detection
    db = _make_db()
    with patch.dict("sys.modules", {"scenedetect": None}):
        result = process_scene_detection("node-1", "/tmp/video.mp4", db)
    assert not result

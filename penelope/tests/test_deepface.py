"""
Test per DeepFace Engine — InsightFace (ArcFace 512-dim).

Usa mock per il modello ONNX (troppo pesante per test unitari).
I test di integrazione con modello reale vanno eseguiti separatamente.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import numpy as np
import pytest


@pytest.fixture
def mock_insightface():
    """Mock dell'app InsightFace per evitare download modello."""
    with patch("penelope.recognition.deepface_engine._get_analyzer") as mock_get:
        mock_app = MagicMock()
        mock_app.get.return_value = []
        mock_get.return_value = mock_app
        yield mock_get


@pytest.fixture
def temp_image():
    """Crea un'immagine finta valida in una directory scrivibile."""
    img_dir = Path(tempfile.mkdtemp())
    img_path = img_dir / "test_face.jpg"
    import cv2
    dummy_img = np.zeros((100, 100, 3), dtype=np.uint8)
    cv2.imwrite(str(img_path), dummy_img)
    yield str(img_path)
    # Cleanup
    import shutil
    shutil.rmtree(str(img_dir), ignore_errors=True)


# ─── Core functions ────────────────────────────────────────────────

def test_detect_faces_no_faces(mock_insightface, temp_image):
    """Nessun volto → lista vuota."""
    from core.recognition.deepface_engine import detect_faces

    faces = detect_faces(temp_image)
    assert faces == []


def test_detect_faces_with_faces(mock_insightface, temp_image):
    """Con volti → restituisce embedding e metadati."""
    from core.recognition.deepface_engine import detect_faces

    mock_app = mock_insightface.return_value
    mock_face = MagicMock()
    mock_face.bbox = np.array([10, 20, 100, 120], dtype=np.int32)
    mock_face.det_score = 0.95
    mock_face.landmark = np.array([[30, 40], [50, 60], [70, 80], [90, 100], [110, 120]], dtype=np.float64)
    mock_face.gender = 1
    mock_face.age = 30.5
    mock_face.normed_embedding = np.random.rand(512).astype(np.float32)
    mock_app.get.return_value = [mock_face]

    faces = detect_faces(temp_image)
    assert len(faces) == 1
    assert faces[0]["bbox"] == [10, 20, 100, 120]
    assert faces[0]["det_score"] == 0.95
    assert faces[0]["gender"] == 1
    assert faces[0]["age"] == 30.5
    assert faces[0]["embedding"] is not None
    assert len(faces[0]["embedding"]) == 512


def test_cosine_similarity_identical():
    """Due embedding identici → similarità 1.0."""
    from core.recognition.deepface_engine import cosine_similarity

    emb = [1.0] * 512
    sim = cosine_similarity(emb, emb)
    assert abs(sim - 1.0) < 0.001


def test_cosine_similarity_orthogonal():
    """Due embedding ortogonali → similarità ~0.0."""
    from core.recognition.deepface_engine import cosine_similarity

    emb_a = [1.0, 0.0] + [0.0] * 510
    emb_b = [0.0, 1.0] + [0.0] * 510
    sim = cosine_similarity(emb_a, emb_b)
    assert abs(sim) < 0.001


def test_verify_faces_match():
    """Due embedding identici → verified=True."""
    from core.recognition.deepface_engine import verify_faces

    emb = np.random.rand(512).astype(np.float32)
    emb = emb / np.linalg.norm(emb)
    emb_list = emb.tolist()

    result = verify_faces(emb_list, emb_list, threshold=0.4)
    assert result["verified"]
    assert result["similarity"] > 0.99


def test_verify_faces_no_match():
    """Due embedding diversi → verified=False."""
    from core.recognition.deepface_engine import verify_faces

    emb1 = np.random.rand(512).astype(np.float32)
    emb1 = emb1 / np.linalg.norm(emb1)
    emb2 = np.random.rand(512).astype(np.float32)
    emb2 = emb2 / np.linalg.norm(emb2)

    result = verify_faces(emb1.tolist(), emb2.tolist(), threshold=0.9)
    assert not result["verified"]


def test_save_and_load_embedding(tmp_path):
    """Salva e carica embedding da file .npy."""
    from core.recognition.deepface_engine import save_embedding, load_embedding
    import core.recognition.deepface_engine as df

    orig_dir = df.EMBEDDINGS_DIR
    df.EMBEDDINGS_DIR = Path(tmp_path)
    try:
        emb = [0.5] * 512
        save_embedding("test-person", emb)

        loaded = load_embedding("test-person")
        assert loaded is not None
        assert len(loaded) == 512
        assert abs(loaded[0] - 0.5) < 0.001
    finally:
        df.EMBEDDINGS_DIR = orig_dir


# ─── Process face embedding (integrazione con DB mock) ─────────────

def test_process_face_embedding_non_image(mock_insightface):
    """File non immagine → skip."""
    from core.recognition.deepface_engine import process_face_embedding
    db = MagicMock()
    db.__enter__.return_value = db

    with tempfile.NamedTemporaryFile(suffix=".txt") as f:
        result = process_face_embedding("node-1", f.name, db)
    assert not result


def test_process_face_embedding_no_faces(mock_insightface, temp_image):
    """Immagine senza volti → False."""
    from core.recognition.deepface_engine import process_face_embedding
    db = MagicMock()
    db.__enter__.return_value = db

    result = process_face_embedding("node-1", temp_image, db)
    assert not result


def test_process_face_embedding_with_faces(mock_insightface, temp_image):
    """Immagine con volti → crea nodi Person e aggiorna metadati."""
    from core.recognition.deepface_engine import process_face_embedding

    # Configura detect_faces per restituire un volto
    mock_app = mock_insightface.return_value
    mock_face = MagicMock()
    mock_face.bbox = np.array([10, 20, 100, 120], dtype=np.int32)
    mock_face.det_score = 0.95
    mock_face.gender = 1
    mock_face.age = 30.5
    mock_face.normed_embedding = np.random.rand(512).astype(np.float32)
    mock_app.get.return_value = [mock_face]

    db = MagicMock()
    db.__enter__.return_value = db
    db.get_node.return_value = {
        "id": "node-1",
        "type": "File",
        "label": "test.jpg",
        "metadata": '{"existing": "data"}',
    }
    db._query.return_value = []
    db.create_node.return_value = "person-uuid"

    result = process_face_embedding("node-1", temp_image, db)

    assert result
    db.create_node.assert_called_once()
    db.create_edge.assert_called_once()

    # Verifica che i metadati siano aggiornati
    update_call = [
        c for c in db._execute.call_args_list
        if "UPDATE nodes SET metadata" in str(c)
    ]
    assert len(update_call) >= 1

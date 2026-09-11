"""
Test per ChromaStore — memoria vettoriale (MiniLM + CLIP).
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def chroma_store(request):
    """Crea un'istanza ChromaStore con directory temporanea."""
    from core.db.chroma_store import ChromaStore
    tmpdir = tempfile.mkdtemp()
    store = ChromaStore(persist_dir=tmpdir)

    def cleanup():
        store.close()
        # Riprova cleanup con retry (Windows file locking)
        for _ in range(3):
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
                break
            except PermissionError:
                import time
                time.sleep(0.5)

    request.addfinalizer(cleanup)
    return store


def test_index_and_search_text(chroma_store):
    """Indicizza un testo e lo ritrova con ricerca semantica."""
    ok = chroma_store.index_text(
        node_id="test-1",
        text="Il cane corre nel parco",
        metadata={"file_name": "test.txt", "mime_type": "text/plain"},
    )
    assert ok, "Indexing fallito"

    results = chroma_store.search_similar("cane", top_k=5)
    assert len(results) >= 1
    assert any(r["node_id"] == "test-1" for r in results)


def test_index_multiple_texts(chroma_store):
    """Indicizza più testi e verifica che siano tutti ricercabili."""
    texts = [
        ("test-1", "Il gatto dorme sul divano"),
        ("test-2", "La macchina rossa sfreccia veloce"),
        ("test-3", "Oggi piove e fa freddo"),
    ]
    for nid, txt in texts:
        chroma_store.index_text(nid, txt, metadata={"file_name": f"{nid}.txt"})

    results = chroma_store.search_similar("gatto", top_k=5)
    ids = [r["node_id"] for r in results]
    assert "test-1" in ids

    results = chroma_store.search_similar("automobile", top_k=5)
    ids = [r["node_id"] for r in results]
    assert "test-2" in ids


def test_search_empty_query(chroma_store):
    """Query vuota restituisce lista vuota (non errore)."""
    results = chroma_store.search_similar("", top_k=5)
    assert results == []


def test_search_no_results(chroma_store):
    """Query senza corrispondenze restituisce lista vuota."""
    results = chroma_store.search_similar("zzzzznonesistent", top_k=5)
    assert results == []


def test_count(chroma_store):
    """Count deve riflettere il numero di documenti indicizzati."""
    assert chroma_store.count() == 0

    chroma_store.index_text("t1", "testo uno", metadata={"_dummy": "1"})
    assert chroma_store.count() == 1

    chroma_store.index_text("t2", "testo due", metadata={"_dummy": "1"})
    assert chroma_store.count() == 2


def test_reindex_same_id(chroma_store):
    """Reindicizzare lo stesso ID deve aggiornare (upsert)."""
    chroma_store.index_text("dup", "prima versione", metadata={"_dummy": "1"})
    chroma_store.index_text("dup", "seconda versione aggiornata", metadata={"_dummy": "1"})

    results = chroma_store.search_similar("aggiornata", top_k=5)
    assert any(r["node_id"] == "dup" for r in results)


def test_image_index_placeholder(chroma_store):
    """Placeholder per immagini deve funzionare senza errori."""
    ok = chroma_store.index_image_placeholder(
        node_id="img-1",
        metadata={"file_name": "foto.jpg", "mime_type": "image/jpeg"},
    )
    assert ok
    assert chroma_store.count_images() == 1


def test_image_index_fallback(chroma_store):
    """index_image deve funzionare anche senza CLIP (fallback a placeholder)."""
    ok = chroma_store.index_image(
        node_id="img-2",
        image_path="/nonexistent/image.jpg",
        metadata={"file_name": "foto.jpg", "mime_type": "image/jpeg"},
    )
    assert ok
    assert chroma_store.count_images() == 1


def test_text_vs_image_collections_separate(chroma_store):
    """Le collezioni testo e immagini devono essere separate."""
    chroma_store.index_text("t1", "testo", metadata={"_dummy": "1"})
    chroma_store.index_image_placeholder("i1", metadata={"_dummy": "1"})

    assert chroma_store.count_text() == 1
    assert chroma_store.count_images() == 1
    assert chroma_store.count() == 2


def test_close_reopen(tmp_path):
    """Chiusura e riapertura della stessa directory deve preservare i dati."""
    from core.db.chroma_store import ChromaStore

    persist = tmp_path / "chroma_test"

    s1 = ChromaStore(persist_dir=str(persist))
    s1.index_text("persist-1", "dati persistenti", metadata={"_dummy": "1"})
    s1.close()

    s2 = ChromaStore(persist_dir=str(persist))
    assert s2.count() >= 1
    results = s2.search_similar("dati", top_k=5)
    assert any(r["node_id"] == "persist-1" for r in results)
    s2.close()

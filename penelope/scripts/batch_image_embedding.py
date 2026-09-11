"""
Batch processing: genera embedding CLIP per tutte le immagini
che non hanno ancora un embedding nella collezione image_embeddings.

Usage:
    python scripts/batch_image_embedding.py
"""

import sys
import time
import logging
from pathlib import Path

# Aggiunge la radice del progetto al path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.mariadb_store import MariaDBStore
from core.db.chroma_store import ChromaStore
from core.ingestion.processor import process_image_embedding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("batch-image-embedding")


def main():
    db = MariaDBStore()
    chroma = ChromaStore()

    # Conta immagini già embeddate
    initial_count = chroma.count_images()
    logger.info("Immagini già in ChromaDB: %d", initial_count)

    # Pre-carica CLIP (la prima volta scarica i pesi ~30s)
    logger.info("Pre-caricamento modello CLIP...")
    from core.ingestion.image_embedder import get_image_embedding
    # Carica una prima immagine per forzare il download del modello
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.path FROM nodes n
            JOIN file_registry f ON f.node_id = n.id
            WHERE n.type = 'File' AND f.mime_type LIKE 'image/%'
            LIMIT 1
        """)
        first = cur.fetchone()
        if first:
            t0 = time.time()
            emb = get_image_embedding(first['path'])
            logger.info("CLIP caricato in %.1fs (embedding dim=%s)", time.time() - t0, len(emb) if emb else "FAIL")

    # Recupera tutte le immagini dal DB
    with conn.cursor() as cur:
        cur.execute("""
            SELECT n.id, f.path FROM nodes n
            JOIN file_registry f ON f.node_id = n.id
            WHERE n.type = 'File' AND f.mime_type LIKE 'image/%'
            ORDER BY f.path
        """)
        rows = cur.fetchall()

    total = len(rows)
    logger.info("Totale immagini da processare: %d", total)
    
    if total == 0:
        logger.info("Nessuna immagine da processare.")
        return

    # Processa in batch
    ok = 0
    fail = 0
    t_start = time.time()

    for i, row in enumerate(rows, 1):
        node_id = row["id"]
        file_path = row["path"]
        try:
            result = process_image_embedding(node_id, file_path, db, chroma)
            if result:
                ok += 1
            else:
                fail += 1
        except Exception as e:
            logger.debug("Errore %s: %s", file_path, e)
            fail += 1

        if i % 50 == 0 or i == total:
            elapsed = time.time() - t_start
            rate = i / elapsed if elapsed > 0 else 0
            logger.info(
                "[%d/%d] ok=%d fail=%d (%.1f img/s, %.1fs elapsed)",
                i, total, ok, fail, rate, elapsed,
            )

    elapsed = time.time() - t_start
    final_count = chroma.count_images()
    logger.info("=" * 50)
    logger.info("COMPLETATO: %d/%d immagini processate", ok, total)
    logger.info("Immagini in ChromaDB: %d -> %d (+%d)", initial_count, final_count, final_count - initial_count)
    logger.info("Tempo totale: %.1fs (%.2f img/s)", elapsed, total / elapsed if elapsed > 0 else 0)

    db.close()


if __name__ == "__main__":
    main()

"""
Batch face detection su tutte le immagini del grafo che non hanno
ancora face_count nei metadati.

Uso: python scripts/batch_face_detection.py [--limit N] [--batch N]
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# Aggiunge radice progetto al path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.mariadb_store import MariaDBStore
from core.ingestion.processor import process_face_detection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("batch_face")

def main():
    parser = argparse.ArgumentParser(description="Batch face detection su immagini")
    parser.add_argument("--limit", type=int, default=0, help="Numero massimo di immagini (0 = tutte)")
    parser.add_argument("--batch", type=int, default=50, help="Quanteprocessarne prima di log")
    args = parser.parse_args()

    db = MariaDBStore()
    exts = ("%.jpg", "%.jpeg", "%.png", "%.webp", "%.bmp")

    # Trova immagini senza face detection
    rows = db._query(
        """SELECT n.id, n.label, f.path 
        FROM nodes n
        JOIN file_registry f ON f.node_id = n.id
        WHERE n.type = %s 
          AND (metadata IS NULL OR metadata NOT LIKE %s)
          AND (f.path LIKE %s OR f.path LIKE %s OR f.path LIKE %s OR f.path LIKE %s OR f.path LIKE %s)
        ORDER BY f.path
        """,
        ("File", "%face_count%") + exts,
    )

    total = len(rows)
    if args.limit and args.limit < total:
        rows = rows[:args.limit]
        total = args.limit

    logger.info("Immagini da processare: %d", total)
    if total == 0:
        return

    ok = 0
    fail = 0
    start = time.time()

    for i, r in enumerate(rows, 1):
        node_id = r["id"]
        path = r["path"]
        try:
            result = process_face_detection(node_id, path, db)
            if result:
                ok += 1
            else:
                fail += 1
        except Exception as e:
            logger.warning("Errore su %s: %s", path, e)
            fail += 1

        if i % args.batch == 0 or i == total:
            elapsed = time.time() - start
            rate = i / elapsed if elapsed > 0 else 0
            logger.info(
                "[%d/%d] ok=%d fail=%d  rate=%.1f img/s  elapsed=%.0fs",
                i, total, ok, fail, rate, elapsed,
            )

    elapsed = time.time() - start
    logger.info(
        "Completato: %d/%d ok, %d fail in %.1fs (%.1f img/s)",
        ok, total, fail, elapsed, total / elapsed if elapsed > 0 else 0,
    )

if __name__ == "__main__":
    main()

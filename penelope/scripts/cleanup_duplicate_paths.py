"""
Cleanup: rimuove nodi File/Directory duplicati (stesso path nel metadata).

Tiene il nodo più vecchio, riassegna gli edge/file_registry al nodo tenuto,
elimina i duplicati. Usa il path assoluto nel metadata come chiave.

Usage:
    python scripts/cleanup_duplicate_paths.py [--dry-run]
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from penelope.db.mariadb_store import MariaDBStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("cleanup_duplicate_paths")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Mostra i duplicati senza cancellare")
    args = parser.parse_args()

    db = MariaDBStore()

    # 1. Trova duplicati per path (metadata->path)
    rows = db._query(
        """SELECT id, type, metadata,
                   JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.path')) AS p
            FROM nodes
            WHERE type IN ('File', 'Directory')
              AND JSON_EXTRACT(metadata, '$.path') IS NOT NULL
              AND JSON_EXTRACT(metadata, '$.path') != ''"""
    )
    paths: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        p = r["p"]
        if p not in paths:
            paths[p] = []
        paths[p].append((r["id"], r["type"]))

    dup_groups = {p: v for p, v in paths.items() if len(v) > 1}
    total_dups = sum(len(v) - 1 for v in dup_groups.values())
    logger.info("Trovati %d gruppi duplicati, %d nodi da eliminare", len(dup_groups), total_dups)

    if args.dry_run or not dup_groups:
        for p, entries in sorted(dup_groups.items())[:20]:
            keep = entries[0]
            print(f"  {p[:80]}...")
            print(f"      keep: {keep[0]} ({keep[1]})")
            for dup in entries[1:]:
                print(f"      DEL:  {dup[0]} ({dup[1]})")
        if len(dup_groups) > 20:
            print(f"  ... e altri {len(dup_groups) - 20} gruppi")
        db.close()
        return

    # 2. Elimina duplicati (tieni il più vecchio)
    removed = 0
    conn = db.connect()
    with conn.cursor() as cur:
        for p, entries in sorted(dup_groups.items()):
            keep_id = entries[0][0]
            for dup_id, _t in entries[1:]:
                cur.execute(
                    "UPDATE edges SET source_id = %s WHERE source_id = %s AND target_id != %s",
                    (keep_id, dup_id, keep_id),
                )
                cur.execute(
                    "UPDATE edges SET target_id = %s WHERE target_id = %s AND source_id != %s",
                    (keep_id, dup_id, keep_id),
                )
                cur.execute(
                    "UPDATE file_registry SET node_id = %s WHERE node_id = %s",
                    (keep_id, dup_id),
                )
                # Elimina duplicati file_registry (stesso node + device + path)
                cur.execute(
                    """DELETE f1 FROM file_registry f1
                       JOIN file_registry f2
                         ON f1.node_id = f2.node_id
                        AND f1.device = f2.device
                        AND f1.path = f2.path
                        AND f1.id > f2.id"""
                )
                cur.execute("DELETE FROM ingestion_queue WHERE node_id = %s", (dup_id,))
                cur.execute(
                    "UPDATE nodes SET parent_id = %s WHERE parent_id = %s AND id != %s",
                    (keep_id, dup_id, keep_id),
                )
                cur.execute("DELETE FROM nodes WHERE id = %s", (dup_id,))
                removed += 1
        conn.commit()

    logger.info("Eliminati %d nodi duplicati", removed)
    db.close()


if __name__ == "__main__":
    main()
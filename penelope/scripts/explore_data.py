"""Esplora la struttura dei dati foto in Penelope."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.mariadb_store import MariaDBStore

db = MariaDBStore()
try:
    # Foto totali
    rows = db._query(
        "SELECT COUNT(*) as cnt FROM file_registry WHERE path LIKE %s OR path LIKE %s OR path LIKE %s",
        ("%.jpg", "%.jpeg", "%.png"),
    )
    print(f"Foto totali: {rows[0]['cnt']}")

    # Tutti i path foto
    rows = db._query(
        "SELECT path FROM file_registry WHERE path LIKE %s OR path LIKE %s OR path LIKE %s",
        ("%.jpg", "%.jpeg", "%.png"),
    )
    roots = {}
    for r in rows:
        p = r["path"]
        sep = "\\"
        parts = p.split(sep)
        root = sep.join(parts[:3]) if len(parts) >= 3 else p
        roots[root] = roots.get(root, 0) + 1

    print("\nFoto per directory principale:")
    for root, cnt in sorted(roots.items(), key=lambda x: -x[1]):
        print(f"  {root}: {cnt}")

    # Cerca cartelle con 'family', 'genitori', 'parents', 'oldmemory' etc
    keywords = ["OldMemory", "Family", "Family", "Angela"]
    for kw in keywords:
        rows = db._query(
            "SELECT COUNT(*) as cnt FROM file_registry WHERE path LIKE %s",
            (f"%{kw}%",),
        )
        print(f"\nPath con '{kw}': {rows[0]['cnt']} files")

    # Statistiche insightface
    rows = db._query(
        "SELECT COUNT(*) as cnt FROM nodes WHERE type = %s AND metadata LIKE %s",
        ("Person", "%insightface%"),
    )
    print(f"\nNodi Person con InsightFace: {rows[0]['cnt']}")

    rows = db._query(
        "SELECT COUNT(*) as cnt FROM nodes WHERE type = %s AND metadata LIKE %s",
        ("Person", "%face_detection%"),
    )
    print(f"Nodi Person con YOLO: {rows[0]['cnt']}")

except Exception as e:
    import traceback
    traceback.print_exc()
finally:
    db.close()

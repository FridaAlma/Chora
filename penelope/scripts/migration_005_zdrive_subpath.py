#!/usr/bin/env python3
"""
Migration 005 — Prependi "storage/" ai path di z_drive.

Cosa fa:
1. Recupera device_id per label 'z_drive' (non hardcodato).
2. Per ogni riga di file_registry con device_id = z_drive il cui path
   NON inizia già con "storage/", prepende "storage/" al path.
3. Idempotente: il filtro NOT LIKE 'storage/%' impedisce doppie applicazioni.
4. NON tocca righe di altri device.

Motivazione:
Il mountpoint reale SMB è /Volumes/diskD, ma i file sono stati copiati
sotto /Volumes/diskD/storage/User/... anziché /Volumes/diskD/User/...
I path relativi devono quindi diventare "storage/User/..." invece di "User/...".

Uso:
    python scripts/migration_005_zdrive_subpath.py                   # execute
    python scripts/migration_005_zdrive_subpath.py --dry-run         # solo report + log
    python scripts/migration_005_zdrive_subpath.py --report-only     # solo conteggio e sample
"""

import argparse
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=str(_PROJECT_ROOT / ".env"), override=True)

from penelope.db.mariadb_store import MariaDBStore

logger = logging.getLogger("migration_005")


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


# ─── Helpers ───────────────────────────────────────────────────────

DEVICE_LABEL = "z_drive"
LIKE_PREFIX = 'storage/%'


def get_device_id(db: MariaDBStore) -> int:
    """Recupera device_id per label z_drive."""
    rows = db._query(
        "SELECT id FROM devices WHERE label = %s LIMIT 1", (DEVICE_LABEL,)
    )
    if not rows:
        raise RuntimeError(
            f"Device '{DEVICE_LABEL}' non trovato in tabella devices. "
            "Esegui prima migration_002_devices.py."
        )
    return rows[0]["id"]


# ─── Report ────────────────────────────────────────────────────────

def build_report(db: MariaDBStore) -> dict:
    """Analizza righe file_registry per z_drive."""
    did = get_device_id(db)

    total = db._query(
        "SELECT COUNT(*) AS cnt FROM file_registry WHERE device_id = %s", (did,)
    )[0]["cnt"]

    LIKE_PREFIX = 'storage/%'

    already_ok = db._query(
        "SELECT COUNT(*) AS cnt FROM file_registry "
        "WHERE device_id = %s AND path LIKE %s", (did, LIKE_PREFIX)
    )[0]["cnt"]

    to_update = db._query(
        "SELECT COUNT(*) AS cnt FROM file_registry "
        "WHERE device_id = %s AND path NOT LIKE %s", (did, LIKE_PREFIX)
    )[0]["cnt"]

    # Campioni di path da modificare
    samples = db._query(
        "SELECT id, path FROM file_registry "
        "WHERE device_id = %s AND path NOT LIKE %s "
        "LIMIT 15", (did, LIKE_PREFIX)
    )

    # Campioni di path già OK
    samples_ok = db._query(
        "SELECT id, path FROM file_registry "
        "WHERE device_id = %s AND path LIKE %s "
        "LIMIT 3", (did, LIKE_PREFIX)
    )

    return {
        "device_id": did,
        "device_label": DEVICE_LABEL,
        "total": total,
        "already_ok": already_ok,
        "to_update": to_update,
        "samples": [{"id": r["id"], "path": r["path"]} for r in samples],
        "samples_ok": [{"id": r["id"], "path": r["path"]} for r in samples_ok],
    }


def print_report(report: dict, dry_run: bool = True):
    tag = "[DRY-RUN]" if dry_run else "[EXECUTE]"
    print(f"\n{'='*70}")
    print(f"  Migration 005 — Prependi 'storage/' ai path di {report['device_label']}")
    print(f"  {'DRY RUN — nessuna modifica' if dry_run else 'ESECUZIONE REALE'}")
    print(f"{'='*70}")

    print(f"\n{tag} Stato attuale:")
    print(f"   Device:                    {report['device_label']} (id={report['device_id']})")
    print(f"   Righe totali:              {report['total']}")
    print(f"   Già con 'storage/...':     {report['already_ok']}  ✓ (non toccate)")
    print(f"   DA MODIFICARE:             {report['to_update']}  ⬇")

    if report["samples_ok"]:
        print(f"\n{tag} Campioni path già ok (prefix 'storage/' presente):")
        for s in report["samples_ok"]:
            print(f"   id={s['id']:>6}  {s['path'][:90]}")

    if report["samples"]:
        print(f"\n{tag} Primi {len(report['samples'])} path da modificare:")
        for s in report["samples"]:
            new_path = f"storage/{s['path']}"
            print(f"   id={s['id']:>6}  {s['path'][:80]}")
            print(f"       → {new_path[:80]}")

    print(f"\n{tag} Riepilogo:")
    print(f"   Righe da aggiornare:       {report['to_update']}")
    print(f"   Righe non toccate:         {report['already_ok']} (già prefix) + "
          f"{report['total'] - report['to_update'] - report['already_ok']} (altri device)")
    print(f"{'='*70}\n")


# ─── Esecuzione ────────────────────────────────────────────────────

def execute_migration(db: MariaDBStore, dry_run: bool = False) -> bool:
    did = get_device_id(db)

    # Conta righe da modificare
    to_update = db._query(
        "SELECT COUNT(*) AS cnt FROM file_registry "
        "WHERE device_id = %s AND path NOT LIKE %s", (did, LIKE_PREFIX)
    )[0]["cnt"]

    if to_update == 0:
        logger.info("Nessun path da modificare per %s (id=%s). Già tutto ok.", DEVICE_LABEL, did)
        return True

    logger.info("Aggiornamento %d righe per %s (id=%s)...", to_update, DEVICE_LABEL, did)

    if dry_run:
        # Mostra ogni UPDATE che farebbe
        rows = db._query(
            "SELECT id, path FROM file_registry "
            "WHERE device_id = %s AND path NOT LIKE %s "
            "ORDER BY id", (did, LIKE_PREFIX)
        )
        for r in rows:
            new_path = f"storage/{r['path']}"
            logger.info(
                "[DRY-RUN] UPDATE file_registry SET path = '%s' WHERE id = %s",
                new_path[:100], r["id"]
            )
        logger.info("Dry-run completato: %d UPDATE da eseguire", len(rows))
    else:
        # Esecuzione reale: UPDATE massiva
        sql = (
            "UPDATE file_registry "
            "SET path = CONCAT('storage/', path) "
            "WHERE device_id = %s AND path NOT LIKE %s"
        )
        db._execute(sql, (did, LIKE_PREFIX))
        affected = db._conn.affected_rows()
        logger.info("UPDATE completato: %d righe modificate", affected)

    logger.info("Migration 005 completata%s.", " (dry-run, nessuna modifica reale)" if dry_run else "")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Migration 005: prependi 'storage/' ai path di z_drive.\n"
                    "I path relativi esistenti ('User/...') diventano 'storage/User/...'.\n"
                    "Idempotente: non tocca path che iniziano già con 'storage/'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostra ogni UPDATE che farebbe senza eseguire")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log dettagliato (per-riga in dry-run)")
    parser.add_argument("--report-only", action="store_true",
                        help="Solo report stato attuale, senza eseguire/dry-run")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    db = MariaDBStore()
    db.connect()

    try:
        if args.report_only:
            report = build_report(db)
            print_report(report, dry_run=False)  # tag [EXECUTE] ma non modifica
        elif args.dry_run:
            report = build_report(db)
            print_report(report, dry_run=True)
            execute_migration(db, dry_run=True)
        else:
            report = build_report(db)
            print_report(report, dry_run=False)

            if report["to_update"] == 0:
                print("\n✅ Nessun path da modificare. Già tutto ok. Nessuna azione necessaria.")
                return

            print("\n⚠  Questa operazione modificherà il database.")
            print(f"   {report['to_update']} righe di file_registry per device '{report['device_label']}'")
            print(f"   saranno aggiornate: path → CONCAT('storage/', path)")
            confirm = input("   Continuare? (s/N): ").strip().lower()
            if confirm not in ("s", "si", "y", "yes"):
                print("   Annullato.")
                return

            execute_migration(db, dry_run=False)

            # Report finale post-esecuzione
            final = build_report(db)
            print_report(final, dry_run=False)

            if final["to_update"] > 0:
                print(f"⚠  Attenzione: {final['to_update']} righe risultano ancora da aggiornare "
                      f"(possibile race condition o path non matchati).")
            else:
                print("\n✅ Migration 005 completata con successo.")

    except Exception as e:
        logger.error("Errore migration: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
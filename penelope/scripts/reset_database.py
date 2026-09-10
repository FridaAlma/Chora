#!/usr/bin/env python3
"""
Reset sicuro — Cancella TUTTO il contenuto del database Penelope.

ATTENZIONE: operazione distruttiva. Svuota tutte le tabelle:
    edges, ingestion_queue, file_registry, nodes, device_mounts, devices

L'ordine rispetta le foreign key (prima le tabelle figlie):
    1. edges            (FK → nodes)
    2. ingestion_queue  (FK → nodes)
    3. file_registry    (FK → nodes, FK → devices ON DELETE SET NULL)
    4. nodes
    5. device_mounts    (FK → devices ON DELETE CASCADE)
    6. devices

Lo SCHEMA delle tabelle (CREATE TABLE) NON viene toccato: dopo il reset
il database è vuoto ma funzionante, pronto per un nuovo scan.

Per ripristinare da zero:
    python scripts/reset_database.py --yes
    python run.py --init /path/to/storage

Uso:
    python scripts/reset_database.py --report-only   # mostra conteggi, non tocca
    python scripts/reset_database.py --dry-run       # mostra piano, non tocca
    python scripts/reset_database.py --yes           # esegue senza conferma
    python scripts/reset_database.py                 # chiede conferma interattiva
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

logger = logging.getLogger("reset_database")


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


# ─── Tabelle, in ordine di svuotamento (FK-safe) ───────────────────

TABLES_IN_ORDER = [
    "edges",
    "ingestion_queue",
    "file_registry",
    "nodes",
    "device_mounts",
    "devices",
]


# ─── Report / conteggi ─────────────────────────────────────────────


def build_report(db: MariaDBStore) -> dict:
    """Conteggio righe per ogni tabella."""
    counts = {}
    for tbl in TABLES_IN_ORDER:
        rows = db._query(
            "SELECT COUNT(*) AS cnt FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (tbl,),
        )
        if not rows or rows[0]["cnt"] == 0:
            counts[tbl] = None  # tabella non esiste
            continue
        rows = db._query(f"SELECT COUNT(*) AS cnt FROM `{tbl}`")
        counts[tbl] = rows[0]["cnt"] if rows else 0
    return counts


def print_report(counts: dict, mode: str):
    print(f"\n{'='*70}")
    print("  Reset sicuro database Penelope")
    print(f"  Modalità: {mode}")
    print(f"{'='*70}")
    print(f"\n  Tabelle da svuotare (in ordine FK-safe):\n")

    total = 0
    for tbl in TABLES_IN_ORDER:
        c = counts.get(tbl)
        if c is None:
            print(f"    {tbl:<20} ⚠ non esiste")
        else:
            print(f"    {tbl:<20} {c:>8} righe")
            total += c

    print(f"\n  Totale righe da rimuovere: {total}")
    if total == 0:
        print("  (database già vuoto)")
    print(f"{'='*70}\n")
    return total


# ─── Esecuzione ────────────────────────────────────────────────────


def execute_reset(db: MariaDBStore, dry_run: bool = False) -> dict:
    """Svuota tutte le tabelle in ordine FK-safe.

    Usa TRUNCATE quando possibile (più veloce, resetta AUTO_INCREMENT),
    fallback a DELETE se TRUNCATE fallisce (es. su tabelle referenziate).

    Returns:
        dict {tabella: righe_rimosse}
    """
    removed = {}
    conn = db.connect()

    for tbl in TABLES_IN_ORDER:
        counts = build_report(db)
        if counts.get(tbl) is None:
            logger.info("[%s] %s: tabella non esiste, skip", "DRY-RUN" if dry_run else "EXEC", tbl)
            removed[tbl] = 0
            continue

        cnt = counts[tbl]

        if dry_run:
            logger.info("[DRY-RUN] TRUNCATE `%s` (%d righe)", tbl, cnt)
            removed[tbl] = cnt
            continue

        # Esecuzione reale: prova TRUNCATE, fallback DELETE
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET FOREIGN_KEY_CHECKS = 0")
                cur.execute(f"TRUNCATE TABLE `{tbl}`")
                cur.execute(f"SET FOREIGN_KEY_CHECKS = 1")
            conn.commit()
            logger.info("[EXEC] TRUNCATE `%s` (%d righe)", tbl, cnt)
        except Exception as e:
            logger.warning("TRUNCATE %s fallito (%s), fallback DELETE", tbl, e)
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(f"SET FOREIGN_KEY_CHECKS = 0")
                cur.execute(f"DELETE FROM `{tbl}`")
                cur.execute(f"SET FOREIGN_KEY_CHECKS = 1")
            conn.commit()
            logger.info("[EXEC] DELETE `%s` (%d righe)", tbl, cnt)
        removed[tbl] = cnt

    return removed


# ─── Main ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Reset sicuro: svuota TUTTE le tabelle del database Penelope "
            "(edges, ingestion_queue, file_registry, nodes, device_mounts, devices).\n"
            "Lo schema NON viene toccato. Operazione IRREVERSIBILE."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostra piano senza eseguire")
    parser.add_argument("--report-only", action="store_true",
                        help="Solo conteggi, nessuna azione")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Salta conferma interattiva (rischioso)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log dettagliato")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    db = MariaDBStore()
    db.connect()

    try:
        counts = build_report(db)
        total = print_report(counts, "report" if args.report_only else
                                        "dry-run" if args.dry_run else
                                        "ESECUZIONE")

        if args.report_only:
            return

        if args.dry_run:
            execute_reset(db, dry_run=True)
            print("\nDry-run completato — nessuna modifica applicata.")
            return

        if total == 0:
            print("\n✅ Database già vuoto. Nessuna azione necessaria.")
            return

        # Conferma interattiva (protetta)
        if not args.yes:
            print("\n⚠  ⚠  ⚠  OPERAZIONE IRREVERSIBILE ⚠  ⚠  ⚠")
            print(f"   Verranno eliminate {total} righe da {len(TABLES_IN_ORDER)} tabelle.")
            print("   Lo schema (CREATE TABLE) rimane intatto.")
            confirm = input("\n   Questa azione non può essere annullata.\n"
                            "   Digitare 'reset' per confermare: ").strip().lower()
            if confirm != "reset":
                print("   Annullato. Nessuna modifica.")
                return
        else:
            print("\n[--yes] Conferma automatica: eseguo il reset...")

        removed = execute_reset(db, dry_run=False)

        print("\n✅ Reset completato:")
        for tbl in TABLES_IN_ORDER:
            print(f"   {tbl:<20} {removed.get(tbl, 0):>8} righe rimosse")

        final = build_report(db)
        print("\n   Conteggio finale:")
        for tbl in TABLES_IN_ORDER:
            print(f"   {tbl:<20} {final.get(tbl, 0) if final.get(tbl) is not None else '-'} righe")

        print("\n   Il database è di nuovo vuoto e pronto per un nuovo scan.")
        print("   Per ricominciare:  python run.py --init /path/to/storage")

    except Exception as e:
        logger.error("Errore reset: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
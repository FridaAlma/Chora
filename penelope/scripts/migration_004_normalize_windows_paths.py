#!/usr/bin/env python3
"""
Migration 004 — Normalizza path Windows drive-letter in path relativi.

Cosa fa:
1. Per ogni riga di file_registry il cui path matcha ^[A-Za-z]:[\\/] (drive-letter
   Windows, es. "Z:\\User\\...", "C:/Users/..."), rimuove il prefisso "X:" e
   normalizza i backslash in forward slash.
2. Il path risultato è relativo (es. "User/OldMemory/photo.jpg").
3. NON tocca righe che non matchano il pattern Windows drive-letter
   (es. /Users/..., /mnt/... Z:User\\... senza slash dopo i due punti).

Dopo questa migration, resolve_file_path() e db verify potranno combinare
questi path relativi con mount_root (device_mounts) invece di considerarli
"già assoluti" e restituirli inalterati.

Uso:
    python scripts/migration_004_normalize_windows_paths.py
    python scripts/migration_004_normalize_windows_paths.py --dry-run
    python scripts/migration_004_normalize_windows_paths.py --report-only
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=str(_PROJECT_ROOT / ".env"), override=True)

from penelope.db.mariadb_store import MariaDBStore

logger = logging.getLogger("migration_004")


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


# ─── Pattern drive-letter Windows ─────────────────────────────────

# Matcha: X:\...  oppure  X:/...   (X = lettera singola)
# Non matcha: Z:User\... (senza slash dopo i due punti)
WIN_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


def is_windows_drive_path(path: str) -> bool:
    """True se path inizia con drive letter Windows (X:\\\\ o X:/)."""
    return bool(WIN_DRIVE_PATTERN.match(path))


def normalize_windows_path(path: str) -> Optional[str]:
    """Rimuove prefisso drive-letter e normalizza backslash in forward slash.

    Args:
        path: Path originale (es. 'Z:\\User\\OldMemory\\photo.jpg').

    Returns:
        Path normalizzato (es. 'User/OldMemory/photo.jpg') se matcha pattern,
        None se non matcha (non va toccato).
    """
    if not is_windows_drive_path(path):
        return None
    # Rimuove i primi 2 caratteri (es. "Z:", "C:")
    relative = path[2:]
    # Normalizza backslash in forward slash
    relative = relative.replace("\\", "/")
    # Rimuove leading slash (se presente dopo la drive letter)
    relative = relative.lstrip("/")
    return relative


# ─── Report ────────────────────────────────────────────────────────

def build_report(db: MariaDBStore) -> dict:
    """Analizza file_registry e classifica path per tipo."""
    rows = db._query("SELECT id, device, path FROM file_registry ORDER BY id")

    windows_drive = []
    posix_abs = []
    already_relative = []
    other = []

    for r in rows:
        p = r["path"] or ""
        if is_windows_drive_path(p):
            normalized = normalize_windows_path(p)
            windows_drive.append({
                "id": r["id"],
                "device": r["device"],
                "original": p,
                "normalized": normalized,
            })
        elif p.startswith("/") or p.startswith("\\"):
            posix_abs.append(r)
        else:
            already_relative.append(r)

    return {
        "total": len(rows),
        "windows_drive": windows_drive,
        "posix_abs": posix_abs,
        "already_relative": already_relative,
        "other": other,
    }


def print_report(report: dict, dry_run: bool = True):
    tag = "[DRY-RUN]" if dry_run else "[EXECUTE]"
    total = report["total"]
    n_windows = len(report["windows_drive"])
    n_posix = len(report["posix_abs"])
    n_rel = len(report["already_relative"])

    print(f"\n{'='*70}")
    print(f"  Migration 004 — Normalizza path Windows drive-letter")
    print(f"  {'DRY RUN — nessuna modifica' if dry_run else 'ESECUZIONE REALE'}")
    print(f"{'='*70}")

    print(f"\n{tag} Classificazione path ({total} righe):")
    print(f"   Windows drive-letter (X:\\...):     {n_windows}  ← da convertire")
    print(f"   POSIX assoluto (/Users/...):        {n_posix}  ← NON toccati")
    print(f"   Gia' relativo/altro:                {n_rel}  ← OK")

    if n_windows:
        print(f"\n{tag} Primi {min(15, n_windows)} path da convertire:")
        for entry in report["windows_drive"][:15]:
            print(f"   [{entry['device']:15s}]  {entry['original'][:80]}")
            print(f"     → {entry['normalized'][:80]}")
        if n_windows > 15:
            print(f"     ... e {n_windows - 15} altri")

    if n_posix:
        print(f"\n{tag} Path POSIX assoluti (non toccati in questa migration):")
        for r in report["posix_abs"][:5]:
            print(f"   [{r['device']:15s}]  {r['path'][:80]}")
        if n_posix > 5:
            print(f"     ... e {n_posix - 5} altri")

    print(f"\n{tag} Riepilogo:")
    print(f"   Righe da convertire:  {n_windows}")
    print(f"   Righe NON toccate:    {n_posix + n_rel}")
    print(f"{'='*70}\n")


# ─── Esecuzione ────────────────────────────────────────────────────

def execute_migration(db: MariaDBStore, dry_run: bool = False) -> bool:
    report = build_report(db)
    windows = report["windows_drive"]

    if not windows:
        logger.info("Nessun path Windows drive-letter da convertire.")
        return True

    logger.info("Conversione di %d path Windows drive-letter...", len(windows))
    converted = 0
    errors = 0

    for entry in windows:
        rid = entry["id"]
        original = entry["original"]
        normalized = entry["normalized"]

        if dry_run:
            logger.info("[DRY-RUN] UPDATE file_registry SET path = '%s' WHERE id = %s",
                        normalized[:80], rid)
            logger.debug("   BEFORE: %s", original)
            logger.debug("   AFTER:  %s", normalized)
        else:
            logger.debug("Converting: %s  →  %s", original[:80], normalized[:80])
            try:
                db._execute(
                    "UPDATE file_registry SET path = %s WHERE id = %s",
                    (normalized, rid),
                )
                converted += 1
            except Exception as e:
                logger.error("   Errore su id=%s: %s", rid, e)
                errors += 1

        if not dry_run and converted % 500 == 0 and converted > 0:
            logger.info("   Progresso: %d/%d convertiti", converted, len(windows))

    if dry_run:
        logger.info("Dry-run completato: %d path da convertire", len(windows))
    else:
        logger.info("Conversione completata: %d OK, %d errori su %d totali",
                    converted, errors, len(windows))

    logger.info("Migration 004 completata%s.", " (dry-run, nessuna modifica reale)" if dry_run else "")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Migration 004: normalizza path Windows drive-letter in path relativi.\n"
                    "Rimuove prefisso X:\\ e normalizza backslash in forward slash.\n"
                    "Non tocca path POSIX (/Users/...) ne' path gia' relativi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="Mostra cosa farebbe senza eseguire")
    parser.add_argument("--verbose", "-v", action="store_true", help="Log dettagliato (per-riga)")
    parser.add_argument("--report-only", action="store_true", help="Solo report stato attuale")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    db = MariaDBStore()
    db.connect()

    try:
        if args.report_only:
            report = build_report(db)
            print_report(report, dry_run=False)
        elif args.dry_run:
            report = build_report(db)
            print_report(report, dry_run=True)
            execute_migration(db, dry_run=True)
        else:
            report = build_report(db)
            print_report(report, dry_run=False)

            if not report["windows_drive"]:
                print("\n✅ Nessun path Windows drive-letter da convertire. Nessuna azione necessaria.")
                return

            print("\n⚠  Questa operazione modifichera' il database.")
            confirm = input("   Continuare? (s/N): ").strip().lower()
            if confirm not in ("s", "si", "y", "yes"):
                print("   Annullato.")
                return

            execute_migration(db, dry_run=False)
            final = build_report(db)
            print_report(final, dry_run=False)

    except Exception as e:
        logger.error("Errore migration: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
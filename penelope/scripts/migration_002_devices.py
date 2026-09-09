#!/usr/bin/env python3
"""
Migration 002 — Disaccoppiamento identità contenuto (sha256) da posizione fisica.

Cosa fa:
1. Crea tabella `devices` (se non esiste)
2. Aggiunge colonne `device_id`, `status`, `last_verified_at` a `file_registry`
3. Backfill: per ogni device/distinct in file_registry → crea riga in devices
4. Aggiorna device_id sulle righe file_registry esistenti

Uso:
    python scripts/migration_002_devices.py                   # execute
    python scripts/migration_002_devices.py --dry-run         # solo report
    python scripts/migration_002_devices.py --help

Safe: tutte le operazioni sono idempotenti (IF NOT EXISTS / IF NOT NULL).
I path assoluti esistenti NON vengono modificati. mount_root viene dedotta
ma può essere NULL (risoluzione = path così com'è).
"""

import argparse
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

# Aggiunge la radice del progetto al path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Carica .env PRIMA di importare settings
from dotenv import load_dotenv
load_dotenv(dotenv_path=str(_PROJECT_ROOT / ".env"), override=True)

from penelope.db.mariadb_store import MariaDBStore
from penelope.config import settings

logger = logging.getLogger("migration_002")


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


# ─── SQL statements (non edito schema.sql, tutto via migration) ─────

SQL_CREATE_DEVICES_TABLE = """
CREATE TABLE IF NOT EXISTS devices (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    label           VARCHAR(50) NOT NULL,
    type            ENUM('local','external','network','server') DEFAULT 'local',
    volume_uuid     VARCHAR(128) DEFAULT NULL,
    mount_root      TEXT DEFAULT NULL COMMENT 'Punto di mount corrente, aggiornabile',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_label (label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

SQL_ADD_DEVICE_ID = """
ALTER TABLE file_registry
    ADD COLUMN device_id INT DEFAULT NULL COMMENT 'FK → devices.id (aggiunta dopo backfill)' AFTER node_id
"""

SQL_ADD_DEVICE_FK = """
ALTER TABLE file_registry
    ADD CONSTRAINT fk_registry_device
        FOREIGN KEY (device_id) REFERENCES devices(id)
        ON DELETE SET NULL
"""

SQL_DROP_DEVICE_FK = """
ALTER TABLE file_registry DROP FOREIGN KEY fk_registry_device
"""

SQL_ADD_STATUS = """
ALTER TABLE file_registry
    ADD COLUMN status ENUM('online','offline') DEFAULT 'online'
    COMMENT 'Stato ultima verifica' AFTER device_id;
"""

SQL_ADD_LAST_VERIFIED_AT = """
ALTER TABLE file_registry
    ADD COLUMN last_verified_at DATETIME DEFAULT NULL
    COMMENT 'Timestamp ultima verifica (da db verify)' AFTER status;
"""

# Controllo colonne già esistenti (idempotenza)
SQL_SHOW_COLUMNS = "SHOW COLUMNS FROM file_registry LIKE %s"


def column_exists(db: MariaDBStore, col_name: str) -> bool:
    """Verifica se una colonna esiste già su file_registry."""
    rows = db._query(SQL_SHOW_COLUMNS, (col_name,))
    return len(rows) > 0


def table_exists(db: MariaDBStore, tbl_name: str) -> bool:
    """Verifica se una tabella esiste già."""
    rows = db._query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (tbl_name,),
    )
    return len(rows) > 0


# ─── Deduzione mount_root ──────────────────────────────────────────

def _deduce_mount_root(device_label: str, all_paths: list[str]) -> Optional[str]:
    """Prova a dedurre mount_root per un device.

    Ordine:
    1. Settings.STORAGE_PATHS per device label (tronca al path piu' lungo)
    2. Prefisso comune piu' lungo tra tutti i path del device
    3. None (path assoluti esistenti restano inalterati)
    """
    # 1. Settings
    for key, sp in settings.STORAGE_PATHS.items():
        if sp and (key == device_label or device_label in key):
            p = Path(sp)
            if p.exists():
                return str(p.absolute())
        # Prova anche se device_label matcha un device config come env
        # es. device_1=/Volumes/HDD → device_label="hdd-ext" matcherebbe?
        # Non forziamo, lasciamo che l'utente configuri dopo.

    # 2. Prefisso comune piu' lungo tra i path
    if len(all_paths) >= 2:
        try:
            common = Path(os.path.commonpath(all_paths))
            # Il prefisso comune potrebbe essere una directory che esiste
            parts = common.parts
            # Risali fino a trovare una directory che esiste ancora
            for i in range(len(parts), 0, -1):
                candidate = Path(*parts[:i])
                if candidate.exists():
                    return str(candidate)
        except (ValueError, Exception):
            pass
    elif len(all_paths) == 1:
        p = Path(all_paths[0])
        # Prova il parent, o il parent del parent
        for ancestor in [p.parent, p.parent.parent, p.parent.parent.parent]:
            if ancestor.exists():
                return str(ancestor)

    # 3. Fallback: None
    return None


# ─── Backfill ──────────────────────────────────────────────────────

def build_device_report(db: MariaDBStore) -> dict:
    """Analizza file_registry e raggruppa per device label.

    Returns:
        dict: {device_label: {"count": N, "paths": [str, ...]}}
    """
    rows = db._query(
        "SELECT device, path FROM file_registry ORDER BY device, path"
    )
    groups: dict[str, dict] = {}
    for r in rows:
        dev = r["device"]
        if dev not in groups:
            groups[dev] = {"count": 0, "paths": []}
        groups[dev]["count"] += 1
        groups[dev]["paths"].append(r["path"])
    return groups


def dry_run_report(db: MariaDBStore) -> dict:
    """Report dettagliato delle operazioni che verrebbero eseguite."""
    report = {
        "devices_table_exists": table_exists(db, "devices"),
        "columns": {
            "device_id": column_exists(db, "device_id"),
            "status": column_exists(db, "status"),
            "last_verified_at": column_exists(db, "last_verified_at"),
        },
        "devices_to_create": [],
        "total_registry_rows": 0,
        "errors": [],
    }

    # Conta righe totali in file_registry
    cnt = db._query("SELECT COUNT(*) AS cnt FROM file_registry")
    report["total_registry_rows"] = cnt[0]["cnt"] if cnt else 0

    if report["devices_table_exists"] and all(report["columns"].values()):
        # Schema già migrato — mostra solo device esistenti vs file_registry device
        existing_devices = db._query("SELECT label FROM devices")
        existing_labels = {r["label"] for r in existing_devices}

        groups = build_device_report(db)
        for dev_label, info in groups.items():
            mount_root = _deduce_mount_root(dev_label, info["paths"])
            report["devices_to_create"].append({
                "label": dev_label,
                "count": info["count"],
                "mount_root": mount_root,
                "sample_paths": info["paths"][:3],
                "exists_already": dev_label in existing_labels,
            })
    else:
        # Schema non ancora migrato
        groups = build_device_report(db)
        for dev_label, info in groups.items():
            mount_root = _deduce_mount_root(dev_label, info["paths"])
            report["devices_to_create"].append({
                "label": dev_label,
                "count": info["count"],
                "mount_root": mount_root,
                "sample_paths": info["paths"][:3],
            })

    return report


def print_report(report: dict, dry_run: bool = True):
    """Stampa il report in formato leggibile."""
    tag = "[DRY-RUN]" if dry_run else "[EXECUTE]"
    print(f"\n{'='*70}")
    print(f"  Migration 002 — Devices & File Registry")
    print(f"  {'DRY RUN — nessuna modifica' if dry_run else 'ESECUZIONE REALE'}")
    print(f"{'='*70}")

    print(f"\n{tag} Stato attuale:")
    print(f"   Tabella devices:              {'ESISTE' if report['devices_table_exists'] else 'NON ESISTE'}")
    print(f"   Colonna device_id:            {'ESISTE' if report['columns']['device_id'] else 'NON ESISTE'}")
    print(f"   Colonna status:               {'ESISTE' if report['columns']['status'] else 'NON ESISTE'}")
    print(f"   Colonna last_verified_at:     {'ESISTE' if report['columns']['last_verified_at'] else 'NON ESISTE'}")
    print(f"   Righe in file_registry:       {report['total_registry_rows']}")

    if report.get("errors"):
        print(f"\n{tag} ERRORI:")
        for e in report["errors"]:
            print(f"   ⚠ {e}")

    print(f"\n{tag} Device da creare/collegare:")
    for d in report["devices_to_create"]:
        status = "GIÀ ESISTE" if d.get("exists_already") else "DA CREARE"
        print(f"\n   [{status}] {d['label']}")
        print(f"        Righe file_registry: {d['count']}")
        print(f"        mount_root dedotta:  {d['mount_root'] or 'NONE (path assoluti inalterati)'}")
        if d["sample_paths"]:
            print(f"        Path campione:       {d['sample_paths'][0][:80]}")
            if len(d["sample_paths"]) > 1:
                print(f"                             {d['sample_paths'][1][:80]}")

    print(f"\n{tag} Riepilogo:")
    n_create = sum(1 for d in report["devices_to_create"] if not d.get("exists_already"))
    n_exist = sum(1 for d in report["devices_to_create"] if d.get("exists_already"))
    print(f"   Nuovi device da creare:      {n_create}")
    print(f"   Device già esistenti:        {n_exist}")
    print(f"   Righe file_registry da upd:  {report['total_registry_rows']}")
    print(f"   Path da modificare:          NESSUNO (path assoluti inalterati)")
    print(f"{'='*70}\n")


def execute_migration(db: MariaDBStore, dry_run: bool = False) -> bool:
    """Esegue o simula la migration.

    Args:
        db: Connessione MariaDBStore.
        dry_run: Se True, non esegue modifiche.

    Returns:
        True se successo (o dry-run completato).
    """
    # ─── 0. Droppa FK constraint se esiste (ricreata dopo backfill) ──
    # Necessario perché la prima run fallita ha creato la FK prima dei dati.
    fk_exists_pre = db._query(
        "SELECT constraint_name FROM information_schema.table_constraints "
        "WHERE table_name = 'file_registry' AND constraint_type = 'FOREIGN KEY' "
        "AND constraint_name = 'fk_registry_device'"
    )
    if fk_exists_pre:
        if dry_run:
            logger.info("[DRY-RUN] DROP FOREIGN KEY fk_registry_device ...")
        else:
            logger.info("Rimuovo FK fk_registry_device (verrà ricreata dopo backfill)...")
            db._execute(SQL_DROP_DEVICE_FK)
            logger.info("   OK")

    # ─── 1. Crea tabella devices ──────────────────────────────
    if not table_exists(db, "devices"):
        if dry_run:
            logger.info("[DRY-RUN] CREATE TABLE devices ...")
        else:
            logger.info("Creazione tabella devices ...")
            db._execute(SQL_CREATE_DEVICES_TABLE)
            logger.info("   OK")
    else:
        logger.info("Tabella devices già esistente, salto.")

    # ─── 2. Aggiunge colonne a file_registry ──────────────────
    alterations = [
        ("device_id", SQL_ADD_DEVICE_ID),
        ("status", SQL_ADD_STATUS),
        ("last_verified_at", SQL_ADD_LAST_VERIFIED_AT),
    ]
    for col_name, sql in alterations:
        if not column_exists(db, col_name):
            if dry_run:
                logger.info("[DRY-RUN] ALTER TABLE file_registry ADD COLUMN %s ...", col_name)
            else:
                logger.info("Aggiungo colonna %s su file_registry ...", col_name)
                try:
                    # device_id viene aggiunto sempre SENZA FK (FK aggiunto dopo backfill)
                    actual_sql = sql
                    if col_name == "device_id":
                        actual_sql = SQL_ADD_DEVICE_ID  # no FK
                    db._execute(actual_sql)
                    logger.info("   OK")
                except Exception as e:
                    logger.error("   Errore: %s", e)
        else:
            logger.info("Colonna %s già esistente, salto.", col_name)

    # ─── 4. Backfill: crea devices e aggiorna device_id ───────
    groups = build_device_report(db)
    created_devices: dict[str, int] = {}  # label → id

    for dev_label, info in groups.items():
        # 4a. Esiste già in devices?
        existing = db._query(
            "SELECT id FROM devices WHERE label = %s LIMIT 1", (dev_label,)
        )
        if existing:
            device_id = existing[0]["id"]
            created_devices[dev_label] = device_id
            logger.debug("Device già esistente: %s (id=%d)", dev_label, device_id)

            # Aggiorna last_seen_at
            if not dry_run:
                db._execute(
                    "UPDATE devices SET last_seen_at = NOW() WHERE id = %s",
                    (device_id,),
                )
            continue

        # 4b. Crea nuovo device
        mount_root = _deduce_mount_root(dev_label, info["paths"])

        if dry_run:
            logger.info("[DRY-RUN] INSERT INTO devices (label='%s', mount_root='%s')", dev_label, mount_root or "NULL")
            # Simula device_id progressivo
            device_id = len(created_devices) + 1
        else:
            logger.info("Creazione device: %s (mount_root=%s, %d righe)", dev_label, mount_root or "NONE", info["count"])
            sql = """INSERT INTO devices (label, type, mount_root, last_seen_at)
                     VALUES (%s, 'local', %s, NOW())"""
            db._execute(sql, (dev_label, mount_root))
            # Recupera ID con SELECT (più affidabile di insert_id dopo commit)
            id_rows = db._query("SELECT id FROM devices WHERE label = %s LIMIT 1", (dev_label,))
            device_id = id_rows[0]["id"] if id_rows else 0

        created_devices[dev_label] = device_id

        # 4c. Aggiorna device_id sulle righe file_registry esistenti
        if not dry_run:
            db._execute(
                "UPDATE file_registry SET device_id = %s WHERE device = %s",
                (device_id, dev_label),
            )
            affected = db._conn.affected_rows()
            logger.info("   Aggiornate %d righe file_registry", affected)

    # ─── 5. Per i device già esistenti senza device_id, aggiorna comunque ──
    if not dry_run:
        for dev_label, device_id in created_devices.items():
            db._execute(
                "UPDATE file_registry SET device_id = %s WHERE device = %s AND device_id IS NULL",
                (device_id, dev_label),
            )

    # ─── 6. Aggiunge FK constraint DOPO aver popolato i dati ────
    if not dry_run and column_exists(db, "device_id"):
        fk_exists = db._query(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_name = 'file_registry' AND constraint_type = 'FOREIGN KEY' "
            "AND constraint_name = 'fk_registry_device'"
        )
        if not fk_exists:
            logger.info("Aggiungo FK fk_registry_device (dopo backfill)...")
            try:
                db._execute(
                    "ALTER TABLE file_registry "
                    "ADD CONSTRAINT fk_registry_device "
                    "FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE SET NULL"
                )
                logger.info("   FK aggiunta con successo")
            except Exception as e:
                logger.warning("   Impossibile aggiungere FK ora: %s (riprovare manualmente dopo migrate)", e)

    logger.info("Migration 002 completata%s.", " (dry-run, nessuna modifica reale)" if dry_run else "")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Migration 002: disaccoppia identità contenuto da posizione fisica.\n"
                    "Crea tabella devices, aggiunge device_id/status/last_verified_at a file_registry,\n"
                    "backfill device esistenti dai dati correnti.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostra cosa farebbe senza eseguire modifiche")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log dettagliato")
    parser.add_argument("--report-only", action="store_true",
                        help="Solo report stato attuale, senza eseguire/dry-run")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    db = MariaDBStore()
    db.connect()

    try:
        if args.report_only:
            report = dry_run_report(db)
            print_report(report, dry_run=False)  # tag [REPORT]
        elif args.dry_run:
            report = dry_run_report(db)
            print_report(report, dry_run=True)
            execute_migration(db, dry_run=True)
        else:
            # Esegue per davvero — prima stampa report, poi chiede conferma
            report = dry_run_report(db)
            print_report(report, dry_run=False)

            # Verifica se c'è già tutto
            if report["devices_table_exists"] and all(report["columns"].values()):
                all_linked = True
                for d in report["devices_to_create"]:
                    if d.get("exists_already"):
                        # Verifica se tutte le righe hanno device_id valorizzato
                        rows_missing = db._query(
                            "SELECT COUNT(*) AS cnt FROM file_registry "
                            "WHERE device = %s AND device_id IS NULL",
                            (d["label"],),
                        )
                        if rows_missing and rows_missing[0]["cnt"] > 0:
                            all_linked = False
                            break
                    else:
                        all_linked = False
                        break

                if all_linked:
                    print("\n✅ Schema già migrato e backfill completato. Nessuna azione necessaria.")
                    return

            print("\n⚠  Questa operazione modificherà il database.")
            confirm = input("   Continuare? (s/N): ").strip().lower()
            if confirm not in ("s", "si", "y", "yes"):
                print("   Annullato.")
                return

            execute_migration(db, dry_run=False)
            # Report finale
            final = dry_run_report(db)
            print_report(final, dry_run=False)

    except Exception as e:
        logger.error("Errore migration: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
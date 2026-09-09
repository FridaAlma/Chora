#!/usr/bin/env python3
"""
Migration 003 — Mount per-host (device_mounts).

Cosa fa:
1. Crea tabella `device_mounts` (device_id, hostname, mount_root, UNIQUE device_id+hostname)
2. ALTER file_registry.status ENUM per aggiungere 'unknown_from_host'
3. Backfill: per ogni device con mount_root non nullo, crea riga in device_mounts
   con hostname = socket.gethostname() (o PENELOPE_HOSTNAME env)
4. NON tocca devices.mount_root (deprecato ma retrocompatibile)

Uso:
    python scripts/migration_003_device_mounts.py                   # execute
    python scripts/migration_003_device_mounts.py --dry-run         # solo report
    python scripts/migration_003_device_mounts.py --help
"""

import argparse
import logging
import os
import socket
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=str(_PROJECT_ROOT / ".env"), override=True)

from penelope.db.mariadb_store import MariaDBStore

logger = logging.getLogger("migration_003")


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


def _current_hostname() -> str:
    """Hostname corrente, con override da env PENELOPE_HOSTNAME."""
    return os.getenv("PENELOPE_HOSTNAME") or socket.gethostname()


# ─── SQL statements ────────────────────────────────────────────────

SQL_CREATE_DEVICE_MOUNTS_TABLE = """
CREATE TABLE IF NOT EXISTS device_mounts (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    device_id       INT NOT NULL,
    hostname        VARCHAR(128) NOT NULL,
    mount_root      TEXT NOT NULL,
    last_seen_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE INDEX idx_device_host (device_id, hostname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

SQL_ALTER_STATUS_ENUM = """
ALTER TABLE file_registry
    MODIFY COLUMN status ENUM('online','offline','unknown_from_host') DEFAULT 'online'
    COMMENT 'Stato ultima verifica (unknown_from_host = non verificabile da questo host)';
"""

SQL_SHOW_STATUS_ENUM = """
SHOW COLUMNS FROM file_registry WHERE Field = 'status'
"""


def table_exists(db: MariaDBStore, tbl_name: str) -> bool:
    rows = db._query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (tbl_name,),
    )
    return len(rows) > 0


def column_exists(db: MariaDBStore, col_name: str) -> bool:
    rows = db._query("SHOW COLUMNS FROM file_registry LIKE %s", (col_name,))
    return len(rows) > 0


def enum_has_unknown(db: MariaDBStore) -> bool:
    """Verifica se status ENUM contiene gia' 'unknown_from_host'."""
    rows = db._query(SQL_SHOW_STATUS_ENUM)
    if rows:
        col_type = rows[0].get("type", "") or rows[0].get("Type", "")
        return "unknown_from_host" in col_type
    return False


# ─── Report ────────────────────────────────────────────────────────

def build_device_report(db: MariaDBStore) -> dict:
    """Report: devices con mount_root e device_mounts gia' esistenti."""
    devices = db._query(
        "SELECT id, label, mount_root, type FROM devices ORDER BY label"
    )
    existing_mounts = db._query(
        "SELECT dm.device_id, dm.hostname, dm.mount_root, d.label "
        "FROM device_mounts dm "
        "JOIN devices d ON d.id = dm.device_id "
        "ORDER BY d.label, dm.hostname"
    ) if table_exists(db, "device_mounts") else []

    # Raggruppa mount per device_id
    mounts_by_device: dict[int, list] = {}
    for m in existing_mounts:
        did = m["device_id"]
        if did not in mounts_by_device:
            mounts_by_device[did] = []
        mounts_by_device[did].append(m)

    report_devices = []
    for d in devices:
        did = d["id"]
        has_mount_root = bool(d.get("mount_root"))
        existing = mounts_by_device.get(did, [])
        report_devices.append({
            "id": did,
            "label": d["label"],
            "mount_root": d.get("mount_root"),
            "has_mount_root": has_mount_root,
            "existing_mounts": existing,
            "needs_backfill": has_mount_root and not any(
                m["hostname"] == _current_hostname() for m in existing
            ),
        })

    return {
        "table_exists": table_exists(db, "device_mounts"),
        "enum_has_unknown": enum_has_unknown(db),
        "devices": report_devices,
        "current_hostname": _current_hostname(),
    }


def print_report(report: dict, dry_run: bool = True):
    tag = "[DRY-RUN]" if dry_run else "[EXECUTE]"
    print(f"\n{'='*70}")
    print(f"  Migration 003 — Device Mounts per-host")
    print(f"  {'DRY RUN — nessuna modifica' if dry_run else 'ESECUZIONE REALE'}")
    print(f"{'='*70}")

    print(f"\n{tag} Stato attuale:")
    print(f"   Tabella device_mounts:        {'ESISTE' if report['table_exists'] else 'NON ESISTE'}")
    print(f"   ENUM status contiene unknown: {'SI' if report['enum_has_unknown'] else 'NO'}")
    print(f"   Hostname corrente:            {report['current_hostname']}")

    needs_backfill = [d for d in report["devices"] if d["needs_backfill"]]
    no_mount = [d for d in report["devices"] if not d["has_mount_root"]]
    already_done = [d for d in report["devices"] if not d["needs_backfill"] and d["has_mount_root"]]

    if report["devices"]:
        print(f"\n{tag} Device:")
        for d in report["devices"]:
            mount_info = d["mount_root"] or "NONE"
            status = "✅ gia' registrato" if d["existing_mounts"] else ("⬜ da backfill" if d["needs_backfill"] else "⬜ senza mount_root")
            print(f"   [{d['label']:15s}] mount_root={mount_info:30s} {status}")
            for m in d["existing_mounts"]:
                print(f"                      host={m['hostname']:20s} mount={m['mount_root']}")

    print(f"\n{tag} Riepilogo:")
    print(f"   Device con mount_root:       {len(already_done) + len(needs_backfill)}")
    print(f"   - gia' in device_mounts:     {len(already_done)}")
    print(f"   - da backfill:               {len(needs_backfill)}")
    print(f"   Device senza mount_root:     {len(no_mount)}")
    print(f"   Hostname per backfill:       {report['current_hostname']}")
    print(f"{'='*70}\n")


# ─── Esecuzione ────────────────────────────────────────────────────

def execute_migration(db: MariaDBStore, dry_run: bool = False) -> bool:
    hostname = _current_hostname()

    # 1. Crea tabella device_mounts
    if not table_exists(db, "device_mounts"):
        if dry_run:
            logger.info("[DRY-RUN] CREATE TABLE device_mounts ...")
        else:
            logger.info("Creazione tabella device_mounts ...")
            db._execute(SQL_CREATE_DEVICE_MOUNTS_TABLE)
            logger.info("   OK")
    else:
        logger.info("Tabella device_mounts gia' esistente, salto.")

    # 2. ALTER ENUM status
    if not enum_has_unknown(db):
        if dry_run:
            logger.info("[DRY-RUN] ALTER file_registry.status ENUM ...")
        else:
            logger.info("Aggiungo 'unknown_from_host' a status ENUM ...")
            try:
                db._execute(SQL_ALTER_STATUS_ENUM)
                logger.info("   OK")
            except Exception as e:
                logger.error("   Errore: %s", e)
    else:
        logger.info("ENUM status gia' include unknown_from_host, salto.")

    # 3. Backfill: per ogni device con mount_root, crea riga in device_mounts
    devices = db._query(
        "SELECT id, label, mount_root FROM devices WHERE mount_root IS NOT NULL AND mount_root != ''"
    )
    backfilled = 0
    for dev in devices:
        did = dev["id"]
        mount_root = dev["mount_root"]

        # Verifica se esiste gia' riga per questo device+hostname
        existing = db._query(
            "SELECT id FROM device_mounts WHERE device_id = %s AND hostname = %s LIMIT 1",
            (did, hostname),
        ) if table_exists(db, "device_mounts") else []

        if existing:
            logger.debug("Mount gia' presente: %s @ %s = %s", dev["label"], hostname, mount_root)
            continue

        if dry_run:
            logger.info("[DRY-RUN] INSERT INTO device_mounts (device_id=%s, hostname='%s', mount_root='%s')",
                        did, hostname, mount_root)
        else:
            logger.info("Backfill device_mounts: %s @ %s = %s", dev["label"], hostname, mount_root)
            db._execute(
                "INSERT INTO device_mounts (device_id, hostname, mount_root, last_seen_at) "
                "VALUES (%s, %s, %s, NOW())",
                (did, hostname, mount_root),
            )
            backfilled += 1

    if backfilled:
        logger.info("Backfill completato: %d righe inserite", backfilled)
    else:
        logger.info("Nessun backfill necessario.")

    logger.info("Migration 003 completata%s.", " (dry-run, nessuna modifica reale)" if dry_run else "")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Migration 003: mount per-host (device_mounts).\n"
                    "Crea tabella device_mounts, aggiunge unknown_from_host a status ENUM,\n"
                    "backfill mount_root esistenti.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="Mostra cosa farebbe senza eseguire")
    parser.add_argument("--verbose", "-v", action="store_true", help="Log dettagliato")
    parser.add_argument("--report-only", action="store_true", help="Solo report stato attuale")
    parser.add_argument("--hostname", default=None, help="Override hostname per backfill (default: auto)")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    if args.hostname:
        os.environ["PENELOPE_HOSTNAME"] = args.hostname

    db = MariaDBStore()
    db.connect()

    try:
        if args.report_only:
            report = build_device_report(db)
            print_report(report, dry_run=False)
        elif args.dry_run:
            report = build_device_report(db)
            print_report(report, dry_run=True)
            execute_migration(db, dry_run=True)
        else:
            report = build_device_report(db)
            print_report(report, dry_run=False)

            # Se gia' tutto presente, salta
            if report["table_exists"] and report["enum_has_unknown"] and not any(
                d["needs_backfill"] for d in report["devices"]
            ):
                print("\n✅ Schema gia' migrato e backfill completato. Nessuna azione necessaria.")
                return

            print("\n⚠  Questa operazione modifichera' il database.")
            confirm = input("   Continuare? (s/N): ").strip().lower()
            if confirm not in ("s", "si", "y", "yes"):
                print("   Annullato.")
                return

            execute_migration(db, dry_run=False)
            final = build_device_report(db)
            print_report(final, dry_run=False)

    except Exception as e:
        logger.error("Errore migration: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
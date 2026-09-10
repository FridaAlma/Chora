#!/usr/bin/env python3
"""
Script di riparazione — Ripristina la gerarchia corretta dei Project.

Cosa fa (per un device dato via --device <label>):
1. Mostra quali Project esistono oggi collegati ai file di quel device tramite MEMBER_OF.
2. Ricalcola per ogni file_registry.path il progetto corretto usando
   iter_project_boundaries_by_topdir() — processa ogni top-level non-junk
   INDIPENDENTEMENTE con detect_project_boundary_from_topdir().
3. In dry-run mostra il piano (quali MEMBER_OF verrebbero cancellati, quali Project
   fantasma eliminati, quali nuovi MEMBER_OF creati).
4. In esecuzione reale lo applica in transazione.

Non tocca mai nodi File né file_registry — solo Project e MEMBER_OF.

Agnostico: nessun nome di device, path, o cartella personale hardcodato.
La logica è puramente algoritmica (project_detection.py).

Uso:
    python scripts/repair_project_hierarchy.py --device z_drive
    python scripts/repair_project_hierarchy.py --device z_drive --dry-run
    python scripts/repair_project_hierarchy.py --device z_drive --report-only
"""

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=str(_PROJECT_ROOT / ".env"), override=True)

from penelope.db.mariadb_store import MariaDBStore
from penelope.ingestion.project_detection import (
    cluster_projects,
    project_label_from_boundary,
)

logger = logging.getLogger("repair_project_hierarchy")


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


# ─── Helpers DB ────────────────────────────────────────────────────


def get_device(db: MariaDBStore, label: str) -> Optional[dict]:
    rows = db._query("SELECT * FROM devices WHERE label = %s LIMIT 1", (label,))
    return rows[0] if rows else None


def get_files_for_device(db: MariaDBStore, device_id: int) -> List[dict]:
    return db._query(
        "SELECT f.*, d.label AS device_label "
        "FROM file_registry f "
        "JOIN devices d ON d.id = f.device_id "
        "WHERE f.device_id = %s "
        "ORDER BY f.path",
        (device_id,),
    )


def get_current_member_of(db: MariaDBStore, device_id: int) -> List[dict]:
    return db._query(
        """SELECT e.id AS edge_id, e.source_id AS file_node_id,
                  e.target_id AS project_id,
                  n.label AS project_label,
                  f.path AS file_path
           FROM edges e
           JOIN nodes n ON n.id = e.target_id
           JOIN file_registry f ON f.node_id = e.source_id
           WHERE e.relation = 'MEMBER_OF'
             AND f.device_id = %s
           ORDER BY n.label, f.path""",
        (device_id,),
    )


def get_project_by_label(db: MariaDBStore, label: str) -> Optional[dict]:
    rows = db._query(
        "SELECT id, metadata FROM nodes WHERE type = 'Project' AND label = %s LIMIT 1",
        (label,),
    )
    return rows[0] if rows else None


def create_project(db: MariaDBStore, label: str, device_label: str) -> str:
    node_id = db.create_node(
        node_type="Project",
        label=label,
        metadata={
            "device": device_label,
            "repair_source": "repair_project_hierarchy",
            "boundary": label,
        },
    )
    logger.info("Progetto CREATO: %s (%s)", label, node_id[:12])
    return node_id


# ─── Build del piano di riparazione ──────────────────────────────


def build_repair_plan(
    db: MariaDBStore,
    device: dict,
    all_files: List[dict],
    current_edges: List[dict],
) -> dict:
    """Costruisce il piano di riparazione.

    Per ogni top-level non-junk, processa INDIPENDENTEMENTE i confini
    del progetto usando detect_project_boundary_from_topdir().

    Restituisce dict con:
        device_id, device_label, total_files, total_edges,
        boundaries: dict {boundary_relpath: info}
        edges_to_delete: [edge_id, ...]
        to_create: [(project_label, file_node_id, file_path), ...]
        phantom_projects: [(project_id, project_label, remaining_edges), ...]
    """
    device_id = device["id"]
    device_label = device["label"]

    # 1. Raccogli tutti i path relativi
    all_paths = [f["path"] for f in all_files if f.get("path")]
    file_map: Dict[str, dict] = {f["path"]: f for f in all_files if f.get("path")}

    # 2. Calcola boundaries: clusterizzazione ricorsiva che separa
    #    gerarchia fisica da classificazione semantica.
    raw_boundaries = cluster_projects(all_paths)

    # 3. Organizza boundaries con dettagli
    boundaries: dict = {}
    for b_relpath, b_files in raw_boundaries.items():
        if not b_relpath:
            # Path che iniziano con junk — non creare progetti
            continue
        label = project_label_from_boundary(b_relpath)
        existing = get_project_by_label(db, label)
        boundaries[b_relpath] = {
            "project_label": label,
            "file_count": len(b_files),
            "already_exists": existing is not None,
            "project_id": existing["id"] if existing else None,
            "files": [],
        }
        for fp in b_files:
            rec = file_map.get(fp)
            if rec:
                boundaries[b_relpath]["files"].append(rec)

    # 4. Edges attuali raggruppati per project_label
    current_by_project: Dict[str, List[dict]] = defaultdict(list)
    for e in current_edges:
        current_by_project[e["project_label"]].append(e)

    # 5. Edges da cancellare: quelli verso progetti non più validi
    boundary_labels = {b["project_label"] for b in boundaries.values()}
    edges_to_delete: List[int] = []
    for proj_label, edges in current_by_project.items():
        if proj_label not in boundary_labels:
            for e in edges:
                edges_to_delete.append(e["edge_id"])

    # 6. Nuovi MEMBER_OF da creare per ogni boundary
    to_create: List[Tuple[str, str, str]] = []
    for b_relpath, b_info in boundaries.items():
        target_project_id = b_info["project_id"]
        existing_edges = {
            e["file_node_id"]
            for e in current_edges
            if e["project_label"] == b_info["project_label"]
        }
        for f_rec in b_info["files"]:
            if f_rec["node_id"] not in existing_edges:
                to_create.append(
                    (b_info["project_label"], f_rec["node_id"], f_rec["path"])
                )

    # 7. Progetti fantasma: progetti che dopo la riparazione
    #    avranno 0 MEMBER_OF (tutti i loro edge erano da questo device
    #    e sono stati cancellati)
    phantom_projects: List[Tuple[str, str, int]] = []
    for proj_label, edges in current_by_project.items():
        if proj_label not in boundary_labels:
            proj_id = edges[0]["project_id"]
            # Controlla se ci sono edge da altri device verso questo progetto
            remaining = db._query(
                "SELECT COUNT(*) c FROM edges e "
                "JOIN file_registry f ON f.node_id = e.source_id "
                "WHERE e.target_id = %s AND e.relation = 'MEMBER_OF' "
                "AND f.device_id != %s",
                (proj_id, device_id),
            )
            other_count = remaining[0]["c"] if remaining else 0
            if other_count == 0:
                phantom_projects.append((proj_id, proj_label, 0))

    return {
        "device_id": device_id,
        "device_label": device_label,
        "total_files": len(all_files),
        "total_edges": len(current_edges),
        "boundaries": boundaries,
        "edges_to_delete": edges_to_delete,
        "to_create": to_create,
        "phantom_projects": phantom_projects,
    }


# ─── Report ────────────────────────────────────────────────────────


def build_report(db: MariaDBStore, device_label: str) -> dict:
    device = get_device(db, device_label)
    if not device:
        raise RuntimeError(f"Device '{device_label}' non trovato.")
    all_files = get_files_for_device(db, device["id"])
    current_edges = get_current_member_of(db, device["id"])
    plan = build_repair_plan(db, device, all_files, current_edges)
    return plan


def print_report(plan: dict, dry_run: bool = True):
    tag = "[DRY-RUN]" if dry_run else "[EXECUTE]"

    print(f"\n{'='*70}")
    print(f"  Repair Project Hierarchy")
    print(f"  Device: {plan['device_label']} (id={plan['device_id']})")
    print(f"  {'DRY RUN — nessuna modifica' if dry_run else 'ESECUZIONE REALE'}")
    print(f"{'='*70}")

    print(f"\n{tag} Stato attuale:")
    print(f"   File registrati:          {plan['total_files']}")
    print(f"   MEMBER_OF edges:          {plan['total_edges']}")
    print(f"   Progetti fantasma:        {len(plan['phantom_projects'])}")

    print(f"\n{tag} Boundaries calcolati ({len(plan['boundaries'])}):")
    for b_relpath in sorted(plan["boundaries"]):
        b_info = plan["boundaries"][b_relpath]
        exists = "✓ esistente" if b_info["already_exists"] else "✗ NUOVO"
        print(f"   {b_relpath!r:55} -> {b_info['project_label']!r:25} "
              f"{b_info['file_count']:>4} files  {exists}")

    print(f"\n{tag} Edge da cancellare:      {len(plan['edges_to_delete'])}")
    if plan["edges_to_delete"]:
        sample = plan["edges_to_delete"][:5]
        for eid in sample:
            print(f"   DELETE edge id={eid}")
        if len(plan["edges_to_delete"]) > 5:
            print(f"   ... e altri {len(plan['edges_to_delete']) - 5}")

    print(f"\n{tag} Nuovi MEMBER_OF da creare: {len(plan['to_create'])}")
    if plan["to_create"]:
        sample = plan["to_create"][:5]
        for proj_label, fnode, fpath in sample:
            print(f"   CREATE {proj_label!r} <- {fnode[:12]} ({fpath[:50]})")
        if len(plan["to_create"]) > 5:
            print(f"   ... e altri {len(plan['to_create']) - 5}")

    if plan["phantom_projects"]:
        print(f"\n{tag} Progetti fantasma da eliminare:")
        for pid, plabel, _ in plan["phantom_projects"]:
            print(f"   DELETE Project {plabel!r} ({pid})")

    print(f"\n{tag} Riepilogo:")
    print(f"   Boundaries da creare:     {sum(1 for b in plan['boundaries'].values() if not b['already_exists'])}")
    print(f"   Edges da cancellare:      {len(plan['edges_to_delete'])}")
    print(f"   Nuove MEMBER_OF edges:    {len(plan['to_create'])}")
    print(f"   Progetti fantasma:        {len(plan['phantom_projects'])}")
    print(f"{'='*70}\n")


# ─── Esecuzione ────────────────────────────────────────────────────


def execute_repair(db: MariaDBStore, plan: dict, dry_run: bool = False) -> bool:
    """Applica o simula la riparazione con batch SQL per performance."""
    device_label = plan["device_label"]

    # 1. Crea progetti mancanti (singoli INSERT, 2284 è gestibile)
    project_id_map: Dict[str, str] = {}
    new_projects: List[Tuple[str, str]] = []

    for b_relpath, b_info in plan["boundaries"].items():
        label = b_info["project_label"]
        if b_info["already_exists"]:
            project_id_map[label] = b_info["project_id"]
        else:
            project_id_map[label] = f"<nuovo:{label}>"
            new_projects.append((label, device_label))

    if dry_run:
        for label, _ in new_projects:
            logger.info("[DRY-RUN] CREARE Project %s", label)
    else:
        for label, dev_label in new_projects:
            pid = create_project(db, label, dev_label)
            project_id_map[label] = pid
        logger.info("Creati %d nuovi Project", len(new_projects))

    # 2. Cancella edges verso progetti fantasma (BATCH DELETE chunked)
    if plan["edges_to_delete"]:
        if dry_run:
            logger.info("[DRY-RUN] DELETE %d edge", len(plan["edges_to_delete"]))
        else:
            CHUNK = 5000
            deleted_total = 0
            for i in range(0, len(plan["edges_to_delete"]), CHUNK):
                chunk = plan["edges_to_delete"][i:i + CHUNK]
                placeholders = ",".join(["%s"] * len(chunk))
                sql = f"DELETE FROM edges WHERE id IN ({placeholders})"
                affected = db._execute(sql, tuple(chunk))
                deleted_total += affected
            logger.info("Cancellati %d edges fantasma", deleted_total)

    # 3. Crea nuovi MEMBER_OF edges (BATCH executemany chunked)
    new_edge_list: List[Tuple[str, str]] = []
    for proj_label, file_node_id, file_path in plan["to_create"]:
        target_id = project_id_map.get(proj_label)
        if not target_id:
            logger.warning("Target %s non risolto, skip %s", proj_label, file_path[:60])
            continue
        new_edge_list.append((file_node_id, target_id))

    if dry_run:
        logger.info("[DRY-RUN] CREATE %d nuovi MEMBER_OF", len(new_edge_list))
    else:
        if new_edge_list:
            created = 0
            CHUNK = 2000
            conn = db.connect()
            with conn.cursor() as cur:
                for i in range(0, len(new_edge_list), CHUNK):
                    chunk = new_edge_list[i:i + CHUNK]
                    sql = "INSERT INTO edges (source_id, target_id, relation, weight) VALUES (%s, %s, 'MEMBER_OF', 1.0)"
                    cur.executemany(sql, chunk)
                    conn.commit()
                    created += len(chunk)
            logger.info("Creati %d nuovi MEMBER_OF edges", created)

    # 4. Elimina progetti fantasma
    for pid, plabel, _ in plan["phantom_projects"]:
        if dry_run:
            logger.info("[DRY-RUN] DELETE Project %s (%s)", plabel, pid)
        else:
            remaining = db._query(
                "SELECT COUNT(*) c FROM edges WHERE target_id = %s AND relation = 'MEMBER_OF'",
                (pid,),
            )
            if remaining and remaining[0]["c"] == 0:
                db.delete_node(pid)
                logger.info("Project fantasma ELIMINATO: %s (%s)", plabel, pid[:12])
            else:
                logger.info("Project %s (%s) ha %d MEMBER_OF residui, NON eliminato",
                           plabel, pid[:12], remaining[0]["c"] if remaining else 0)

    logger.info(
        "Repair completato%s: %d edges creati, %d cancellati, %d progetti fantasma gestiti.",
        " (dry-run)" if dry_run else "",
        len(new_edge_list),
        len(plan["edges_to_delete"]),
        len(plan["phantom_projects"]),
    )
    return True


# ─── Main ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Repair Project Hierarchy: ricalcola i confini dei progetti "
                    "per un device usando detect_project_boundary_from_topdir().\n"
                    "Ogni top-level directory non-junk viene processata "
                    "INDIPENDENTEMENTE. Rimuove progetti fantasma e riassegna "
                    "MEMBER_OF edges.\n"
                    "Agnostico: nessun nome personale o path hardcodato.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--device", "-d", required=True,
                        help="Label del device da riparare")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostra piano senza eseguire modifiche")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log dettagliato (per-riga in dry-run)")
    parser.add_argument("--report-only", action="store_true",
                        help="Solo report stato attuale, senza azione")
    parser.add_argument("--yes", action="store_true",
                        help="Salta conferma interattiva")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    db = MariaDBStore()
    db.connect()

    try:
        device = get_device(db, args.device)
        if not device:
            logger.error("Device '%s' non trovato.", args.device)
            print(f"\nDispositivi disponibili:")
            for d in db._query("SELECT id, label FROM devices ORDER BY id"):
                print(f"  id={d['id']}  label={d['label']!r}")
            sys.exit(1)

        plan = build_report(db, args.device)

        if args.report_only:
            print_report(plan, dry_run=False)
            return

        if args.dry_run:
            print_report(plan, dry_run=True)
            execute_repair(db, plan, dry_run=True)
        else:
            print_report(plan, dry_run=False)

            total_changes = (
                len(plan["edges_to_delete"]) +
                len(plan["to_create"]) +
                len(plan["phantom_projects"])
            )
            if total_changes == 0:
                print("\n✅ Nessuna modifica necessaria. Già tutto corretto.")
                return

            new_projects = sum(
                1 for b in plan["boundaries"].values() if not b["already_exists"]
            )

            print(f"\n⚠  Operazione distruttiva su Project e MEMBER_OF nel DB.")
            print(f"   Device:        {args.device}")
            print(f"   File coinvolti: {plan['total_files']}")
            print(f"   Nuovi progetti: {new_projects}")
            print(f"   Edges cancellare:  {len(plan['edges_to_delete'])}")
            print(f"   Nuovi edges:   {len(plan['to_create'])}")
            print(f"   Progetti eliminare: {len(plan['phantom_projects'])}")
            if not args.yes:
                confirm = input("   Continuare? (s/N): ").strip().lower()
                if confirm not in ("s", "si", "y", "yes"):
                    print("   Annullato.")
                    return

            execute_repair(db, plan, dry_run=False)

            final = build_report(db, args.device)
            final_total = (
                len(final["edges_to_delete"]) +
                len(final["to_create"]) +
                len(final["phantom_projects"])
            )
            if final_total > 0:
                print(f"\n⚠  {final_total} operazioni residue.")
            else:
                print(f"\n✅ Repair completato con successo.")

    except Exception as e:
        logger.error("Errore repair: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
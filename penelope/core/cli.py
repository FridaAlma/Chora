"""
Interfaccia a riga di comando per Penelope.

Usage:
    penelope scan --device <nome> <path>
    penelope scan:all
    penelope queue process
    penelope queue loop
    penelope graph status
    penelope quarantine list
    penelope quarantine clear
"""

import argparse
import logging
import sys
from pathlib import Path

from core.config import settings
from core.db.graph_bridge import GraphBridge
from core.db.mariadb_store import MariaDBStore
from egida.quarantine import Quarantine
from core.ingestion.dispatcher import Dispatcher
from core.ingestion.scanner import FileScanner

logger = logging.getLogger("penelope")


def _setup_logging(level: str = None):
    logging.basicConfig(
        level=getattr(logging, (level or settings.LOG_LEVEL).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_watchdog(args):
    """Watchdog: osservazione file system in tempo reale."""
    from core.ingestion.scanner import WatchdogManager
    from core.config import settings as _settings

    _managers = []

    if args.action == "start":
        """Avvia watchdog su TUTTI gli storage configurati."""
        if args.path:
            # Directory singola
            path = Path(args.path)
            if not path.is_dir():
                print(f"[WD] Directory non trovata: {path}")
                return
            wm = WatchdogManager(
                path=path,
                project_label=args.project or path.name,
                device_name=args.device or "watchdog",
            )
            wm.start()
            _managers.append(wm)
            print(f"[WD] Avviato su: {path}")
            print("   Ctrl+C per fermare.")
        else:
            # Tutti gli storage configurati
            paths = []
            for device, path_str in _settings.STORAGE_PATHS.items():
                if not path_str:
                    continue
                p = Path(path_str)
                if p.is_dir():
                    paths.append((device, p))

            if not paths:
                print("[WD] Nessuna directory configurata in STORAGE_PATHS.")
                print("   Imposta PENELOPE_PATH_* in .env o usa --path")
                return

            for device, path in paths:
                wm = WatchdogManager(
                    path=path,
                    project_label=device,
                    device_name=device,
                )
                wm.start()
                _managers.append(wm)
                print(f"[WD] Avviato: {device} → {path}")

            print(f"\n[WD] {len(_managers)} watchdog attivi. Ctrl+C per fermare tutti.")

        try:
            import time
            while True:
                time.sleep(1)
                # Stampa statistiche ogni 30 secondi
                for i, wm in enumerate(_managers):
                    s = wm.stats
                    if s["files_seen"] > 0 and int(time.time()) % 30 == 0:
                        print(f"   [{i}] {wm.path.name}: "
                              f"visti={s['files_seen']} "
                              f"indicizzati={s['files_indexed']} "
                              f"hsd={s['files_hsd']} "
                              f"saltati={s['files_skipped']} "
                              f"errori={s['files_error']}")
        except KeyboardInterrupt:
            print("\n[WD] Fermo watchdog...")
            for wm in _managers:
                wm.stop()
            print("[WD] Tutti i watchdog fermati.")

    elif args.action == "status":
        """Mostra lo stato del watchdog."""
        print("\n[WD] Watchdog non in esecuzione.")
        print("   Avvia con: penelope watchdog start")
        print("   Oppure su una directory: penelope watchdog start --path D:/dir")


def cmd_scan(args):
    """Scansiona una directory e registra i file nel grafo."""
    device = args.device or "unknown"
    path = Path(args.path)

    if not path.exists():
        logger.error("Path non trovato: %s", path)
        sys.exit(1)

    scanner = FileScanner(device_name=device)
    results = scanner.scan_directory(
        path,
        project_label=args.project,
        deep=args.deep,
    )

    # Report
    ok = sum(1 for r in results if r.success)
    skipped = sum(1 for r in results if r.skipped)
    errors = sum(1 for r in results if r.error)

    print(f"\n[OK] Scan completato: {path}")
    print(f"   [INDEX] Indicizzati:  {ok}")
    print(f"   [HSD]  Saltati:       {skipped}")
    print(f"   [ERR]  Errori:        {errors}")
    print(f"   [PROJ] Progetto:      {args.project or path.name}")
    print(f"   [MODE] Profondita':    {'deep (YOLO+face+doc+video)' if args.deep else 'gerarchia + categoria (veloce)'}")
    if args.deep:
        print(f"\n   Le analisi profonde sono state eseguite sincronamente.")
        print(f"   Per analisi asincrona usa: penelope queue loop")


def cmd_scan_all(args):
    """Scansiona tutti gli storage configurati."""
    from core.config.settings import STORAGE_PATHS

    for device, path_str in STORAGE_PATHS.items():
        if not path_str:
            logger.warning("Path per '%s' non configurato, salto.", device)
            continue
        path = Path(path_str)
        if not path.exists():
            logger.warning("Path '%s' per '%s' non trovato, salto.", path_str, device)
            continue

        logger.info("Scansione %s (%s)...", device, path)
        scanner = FileScanner(device_name=device)
        results = scanner.scan_directory(path, project_label=device)

        ok = sum(1 for r in results if r.success)
        skipped = sum(1 for r in results if r.skipped)
        errors = sum(1 for r in results if r.error)
        print(f"   [{device}] ok={ok} hsd={skipped} err={errors}")


def cmd_queue(args):
    """Gestione della coda di elaborazione."""
    dispatcher = Dispatcher()

    if args.action == "process":
        if args.reset_stale:
            with dispatcher.db as db:
                stale = db.reset_stale_processing(max_age_minutes=args.max_age)
                if stale:
                    print(f"[DISPATCH] Resettati {stale} elementi stale a 'pending'.")
        count = dispatcher.process_queue(batch_size=args.batch)
        print(f"[DISPATCH] Processati {count} elementi dalla coda.")

    elif args.action == "loop":
        print("[DISPATCH] Avviato. Ctrl+C per fermare.")
        dispatcher.run_loop(interval=args.interval, batch_size=args.batch)

    elif args.action == "status":
        with dispatcher.db as db:
            rows = db._query('''
                SELECT status, COUNT(*) AS cnt
                FROM ingestion_queue
                GROUP BY status
            ''')
            print("\n[QUEUE] Stato coda:")
            for r in rows:
                print(f"   {r['status']}: {r['cnt']}")

    elif args.action == "reset-stale":
        max_age = args.max_age or 30
        with dispatcher.db as db:
            count = db.reset_stale_processing(max_age_minutes=max_age)
            if count:
                print(f"[QUEUE] Resettati {count} elementi stale a 'pending'.")
            else:
                print("[QUEUE] Nessun elemento stale da resettare.")


def cmd_search(args):
    """Ricerca semantica nel grafo."""
    from core.db.chroma_store import ChromaStore

    chroma = ChromaStore()
    results = chroma.search_similar(args.query, top_k=args.top, filter_mime=args.mime)

    if not results:
        print("[SEARCH] Nessun risultato.")
        if not chroma.count():
            print("   ChromaDB vuoto. Esegui: penelope queue process")
        return

    print(f"\n[SEARCH] {len(results)} risultati per: '{args.query}'")
    for i, r in enumerate(results, 1):
        dist = r['distance']
        print(f"\n[{i}] {r['file_name']} (sim: {1-dist:.3f})")
        print(f"    type: {r['mime_type']}")
        print(f"    node: {r['node_id']}")
        if r['snippet']:
            print(f"    ...{r['snippet'][:150]}...")


def cmd_tree(args):
    """Mostra l'albero gerarchico (directory → file) dal grafo."""
    bridge = GraphBridge()
    bridge.load_from_db()

    tree = bridge.directory_tree(root_id=args.project, max_depth=args.depth)
    roots = tree.get("_tree", [])

    if not roots:
        print("[TREE] Nessuna directory nel grafo. Esegui: penelope scan <path>")
        return

    def _print(node, prefix="", is_last=True, label_only=False):
        marker = "└── " if is_last else "├── "
        children = node.get("children", [])
        ntype = node.get("type", "?")
        cat = node.get("category") or ""
        lbl = node.get("label", "?")

        if ntype == "Directory":
            print(f"{prefix}{marker}📁 {lbl}/ ({len(children)} figli)")
        else:
            icon = {"image": "🖼", "video": "🎬", "document": "📄", "other": "📦"}.get(cat, "📄")
            print(f"{prefix}{marker}{icon} {lbl} [{cat or 'n/a'}]")
            return

        # Children: directory prima, poi file
        dirs = [c for c in children if c.get("type") == "Directory"]
        files = [c for c in children if c.get("type") == "File"]
        all_c = dirs + files
        for i, c in enumerate(all_c):
            child_prefix = prefix + ("    " if is_last else "│   ")
            _print(c, child_prefix, i == len(all_c) - 1)

    for i, root in enumerate(roots):
        is_last = i == len(roots) - 1
        rtype = root.get("type")
        rlbl = root.get("label", "?")
        if rtype == "Project":
            print(f"⭐ {rlbl}/")
            children = root.get("children", [])
            for j, c in enumerate(children):
                _print(c, "", j == len(children) - 1)
        else:
            _print(root, "", is_last)


def cmd_graph(args):
    """Stato del grafo."""
    bridge = GraphBridge()
    bridge.load_from_db()
    stats = bridge.count()
    print(f"\n[GRAPH] Grafo: {stats['nodes']} nodi, {stats['edges']} archi")

    # Top 3 tipi di nodo
    from collections import Counter
    types = Counter()
    for _n, d in bridge.graph.nodes(data=True):
        types[d.get("type", "?")] += 1
    if types:
        print("\nPer tipo (top):")
        for t, c in types.most_common():
            print(f"   {t}: {c}")


def cmd_configure(args):
    """Configura le credenziali di Penelope nel keyring di sistema."""
    from core.config.settings import (
        _store_password_in_keyring,
        _delete_password_from_keyring,
    )

    if args.action == "set":
        password = args.password
        if not password:
            import getpass
            password = getpass.getpass("Password MariaDB: ")
        user = args.user or settings.MARIADB_USER
        if _store_password_in_keyring(user, password):
            print(f"[OK] Password salvata nel keyring per utente '{user}'")
            print(f"     Servizio: penelope")
        else:
            print("[ERR] Impossibile salvare nel keyring.")
            print("   Usa PENELOPE_DB_PASSWORD nel .env come fallback.")
            sys.exit(1)

    elif args.action == "clear":
        user = args.user or settings.MARIADB_USER
        if _delete_password_from_keyring(user):
            print(f"[OK] Password rimossa dal keyring per utente '{user}'")
        else:
            print("Nessuna password da rimuovere.")

    elif args.action == "test":
        """Testa la connessione al database con le credenziali correnti."""
        try:
            from core.db.mariadb_store import MariaDBStore
            db = MariaDBStore()
            conn = db.connect()
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM nodes")
                row = cur.fetchone()
                print(f"[OK] Connessione OK -- {row['cnt']} nodi nel grafo.")
            db.close()
        except Exception as e:
            print(f"[ERR] Connessione fallita: {e}")
            sys.exit(1)


def cmd_db(args):
    """Operazioni sul database (pulizia, dedup, reset)."""
    from core.db.mariadb_store import MariaDBStore

    if args.action == "dedup":
        db = MariaDBStore()
        conn = db.connect()

        # Trova duplicati per sha256 (mantieni il primo inserito)
        with conn.cursor() as cur:
            cur.execute('''
                SELECT f.sha256, MIN(f.id) AS keep_id, MIN(f.node_id) AS keep_node
                FROM file_registry f
                WHERE f.sha256 != ''
                GROUP BY f.sha256
                HAVING COUNT(*) > 1
            ''')
            dup_groups = cur.fetchall()

        total_removed = 0
        for group in dup_groups:
            sha256 = group['sha256']
            keep_node = group['keep_node']
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT id, node_id FROM file_registry
                    WHERE sha256 = %s AND node_id != %s
                ''', (sha256, keep_node))
                to_remove = cur.fetchall()

            for entry in to_remove:
                node_id = entry['node_id']
                with conn.cursor() as cur:
                    # Elimina edge collegati
                    cur.execute('DELETE FROM edges WHERE source_id = %s OR target_id = %s', (node_id, node_id))
                    # Elimina queue entries
                    cur.execute('DELETE FROM ingestion_queue WHERE node_id = %s', (node_id,))
                    # Elimina file_registry
                    cur.execute('DELETE FROM file_registry WHERE node_id = %s', (node_id,))
                    # Elimina il nodo
                    cur.execute('DELETE FROM nodes WHERE id = %s', (node_id,))
                conn.commit()
                total_removed += 1

        # Progetti duplicati: tieni il più vecchio
        with conn.cursor() as cur:
            cur.execute('''
                SELECT label, MIN(id) AS keep_id
                FROM nodes
                WHERE type = 'Project'
                GROUP BY label
                HAVING COUNT(*) > 1
            ''')
            dup_projects = cur.fetchall()

        for group in dup_projects:
            label = group['label']
            keep_id = group['keep_id']
            with conn.cursor() as cur:
                cur.execute('SELECT id FROM nodes WHERE type = "Project" AND label = %s AND id != %s', (label, keep_id))
                to_remove = cur.fetchall()
            for entry in to_remove:
                node_id = entry['id']
                with conn.cursor() as cur:
                    # Riassegna gli edge al nodo tenuto
                    cur.execute('UPDATE edges SET source_id = %s WHERE source_id = %s', (keep_id, node_id))
                    cur.execute('UPDATE edges SET target_id = %s WHERE target_id = %s', (keep_id, node_id))
                    cur.execute('UPDATE file_registry SET node_id = %s WHERE node_id = %s', (keep_id, node_id))
                    cur.execute('DELETE FROM ingestion_queue WHERE node_id = %s', (node_id,))
                    cur.execute('DELETE FROM nodes WHERE id = %s', (node_id,))
                conn.commit()
                total_removed += 1

        db.close()
        print(f"[DB] Pulizia completata: {total_removed} nodi duplicati rimossi.")

    elif args.action == "stats":
        db = MariaDBStore()
        conn = db.connect()
        with conn.cursor() as cur:
            cur.execute('SELECT type, COUNT(*) AS cnt FROM nodes GROUP BY type ORDER BY cnt DESC')
            print("\n[DB] Nodi per tipo:")
            for r in cur.fetchall():
                print(f"   {r['type']}: {r['cnt']}")
            cur.execute('SELECT relation, COUNT(*) AS cnt FROM edges GROUP BY relation ORDER BY cnt DESC')
            print("\n[DB] Archi per relazione:")
            for r in cur.fetchall():
                print(f"   {r['relation']}: {r['cnt']}")
        db.close()

    elif args.action == "verify":
        """Verifica raggiungibilità fisica di ogni riga in file_registry.

        Per ogni riga:
        1. Risolve mount per host corrente (device_mounts), fallback devices.mount_root
        2. Se il device non ha mount registrato per QUESTO host → status 'unknown_from_host'
        3. Altrimenti controlla se il file esiste localmente → online/offline

        Non cancella MAI righe. Aggiorna solo status + last_verified_at.
        """
        import os as _os
        import time as _time

        current_host = MariaDBStore.current_hostname()
        db = MariaDBStore()
        with db as store:
            # Query: file_registry + device + mount per host corrente (LEFT JOIN)
            sql = """
                SELECT f.id, f.node_id, f.device, f.path, f.status,
                       f.device_id,
                       d.mount_root AS device_mount_root,
                       d.label AS device_label,
                       dm.mount_root AS host_mount_root
                FROM file_registry f
                LEFT JOIN devices d ON d.id = f.device_id
                LEFT JOIN device_mounts dm ON dm.device_id = f.device_id
                    AND dm.hostname = %s
            """
            params: tuple = (current_host,)
            if args.device:
                if args.device.isdigit():
                    sql += " WHERE f.device_id = %s"
                else:
                    sql += " WHERE f.device = %s"
                params = params + (args.device,)
            sql += " ORDER BY f.device, f.path"
            if args.limit:
                sql += " LIMIT %s"
                params = params + (args.limit,)

            rows = store._query(sql, params)

        if not rows:
            print("[DB] file_registry vuoto. Nessuna verifica da fare.")
            return

        total = len(rows)
        online = 0
        offline = 0
        unknown_host = 0
        skipped = 0  # path vuoti
        device_stats: dict[str, dict] = {}

        print(f"\n[DB] Verifica raggiungibilità: {total} righe (host={current_host})...\n")

        t_start = _time.time()
        for i, row in enumerate(rows, 1):
            rid = row["id"]
            device_label = row.get("device_label") or row["device"]
            path_raw = row.get("path", "")
            host_mount_root = row.get("host_mount_root")
            device_mount_root = row.get("device_mount_root")

            # Inizializza statistiche per device
            if device_label not in device_stats:
                device_stats[device_label] = {
                    "total": 0, "online": 0, "offline": 0,
                    "unknown_from_host": 0,
                    "host_mount_root": host_mount_root,
                    "device_mount_root": device_mount_root,
                }
            device_stats[device_label]["total"] += 1

            # Caso 1: device con mount per host corrente → verifica esistenza
            if host_mount_root:
                mount_root = host_mount_root
            elif device_mount_root:
                # Fallback: mount globale (deprecato) — verifica comunque
                mount_root = device_mount_root
            else:
                # Caso 2: NESSUN mount per questo host → non verificabile da qui
                device_stats[device_label]["unknown_from_host"] += 1
                unknown_host += 1
                with db as store:
                    store.verify_file_location_status(rid, "unknown_from_host")
                continue

            # Risolve path assoluto
            if mount_root and not (path_raw.startswith("/") or path_raw.startswith("\\") or (len(path_raw) > 1 and path_raw[1] == ":")):
                abs_path = str(Path(mount_root) / path_raw)
            else:
                abs_path = path_raw  # gia' assoluto (old data)

            if not abs_path.strip():
                device_stats[device_label]["offline"] += 1
                skipped += 1
                with db as store:
                    store.verify_file_location(rid, is_online=False)
                continue

            # Verifica esistenza file
            file_exists = _os.path.exists(abs_path)

            with db as store:
                store.verify_file_location(rid, is_online=file_exists)

            if file_exists:
                online += 1
                device_stats[device_label]["online"] += 1
            else:
                offline += 1
                device_stats[device_label]["offline"] += 1

            # Progress ogni 100 righe
            if i % 100 == 0:
                print(f"   Progresso: {i}/{total} (on={online}, off={offline}, ?host={unknown_host})")

        duration = _time.time() - t_start

        # ─── Report finale ──────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"  VERIFICA COMPLETATA ({duration:.1f}s, host={current_host})")
        print(f"{'='*60}")
        print(f"  Totale righe:                  {total}")
        print(f"  Online (file trovato):         {online}")
        print(f"  Offline (file assente):        {offline}")
        print(f"  Non verificabile da host:      {unknown_host}")
        if skipped:
            print(f"  Saltati (path vuoto):          {skipped}")
        print()

        for dev_label, stats in sorted(device_stats.items()):
            mr = stats["host_mount_root"] or stats["device_mount_root"] or "N/A"
            print(f"  [{dev_label}]")
            print(f"      mount_root (host): {mr}")
            print(f"      totale:            {stats['total']}")
            print(f"      online:            {stats['online']}")
            print(f"      offline:           {stats['offline']}")
            print(f"      unknown_from_host: {stats['unknown_from_host']}")
            if stats['total'] > 0:
                pct = stats['online'] / stats['total'] * 100
                print(f"      copertura:         {pct:.1f}%")
            print()


def cmd_device(args):
    """Gestione device e mount per-host (device_mounts)."""
    from core.db.mariadb_store import MariaDBStore

    db = MariaDBStore()

    if args.action == "register":
        """Registra un device (crea se non esiste) e registra mount per host corrente."""
        label = args.label
        mount_path = args.mount
        device_type = args.type or "local"
        volume_uuid = args.volume_uuid
        hostname = args.hostname or MariaDBStore.current_hostname()

        if not mount_path:
            print("[DEVICE] Errore: --mount richiesto (path del mount su questo host).")
            return

        with db as store:
            # 1. Trova o crea il device
            device_id = store.ensure_device(
                label=label,
                device_type=device_type,
                mount_root=None,  # non impostiamo il mount globale deprecato
            )
            if args.type:
                store._execute(
                    "UPDATE devices SET type = %s WHERE id = %s",
                    (device_type, device_id),
                )
            if volume_uuid:
                store._execute(
                    "UPDATE devices SET volume_uuid = %s WHERE id = %s",
                    (volume_uuid, device_id),
                )

            # 2. Upsert mount per host
            mount_id = store.upsert_device_mount(device_id, hostname, mount_path)

            # 3. Scrivi marker .penelope_device.json per reconcile_devices()
            from core.discovery import write_device_marker
            write_device_marker(mount_path, device_id, label)

            print(f"[DEVICE] Registrato: {label} (id={device_id})")
            print(f"   type:          {device_type}")
            print(f"   volume_uuid:   {volume_uuid or '(non impostato)'}")
            print(f"   mount (host):  {hostname} → {mount_path}")
            print(f"   mount_id:      {mount_id}")
            print()
            print("   ⚠ Se il device esisteva già con mount globale (deprecato),")
            print("     questo comando registra solo il mount per HOST. L'altro resta.")

    elif args.action == "list":
        """Mostra tutti i device con i mount per host."""
        current_host = MariaDBStore.current_hostname()
        with db as store:
            devices = store.list_devices()
            mounts = store.list_device_mounts()

        if not devices:
            print("[DEVICE] Nessun device registrato.")
            return

        # Raggruppa mount per device_id
        mounts_by_device: dict[int, list] = {}
        for m in mounts:
            did = m["device_id"]
            if did not in mounts_by_device:
                mounts_by_device[did] = []
            mounts_by_device[did].append(m)

        print(f"\n[DEVICE] {len(devices)} device (host corrente: {current_host}):")
        print()
        for d in devices:
            did = d["id"]
            dmounts = mounts_by_device.get(did, [])
            print(f"  [{d['label']}] (id={did}, type={d.get('type','local')})")
            if d.get("volume_uuid"):
                print(f"      volume_uuid: {d['volume_uuid']}")
            if d.get("mount_root"):
                print(f"      mount_root (GLOBALE, deprecato): {d['mount_root']}")
            if dmounts:
                for m in dmounts:
                    marker = "  ◀ host corrente" if m["hostname"] == current_host else ""
                    print(f"      host={m['hostname']:20s} mount={m['mount_root']}{marker}")
            else:
                print(f"      nessun mount registrato per host (usa 'device register')")
            print()

    elif args.action == "merge":
        """Fonde un device in un altro: unifica file_registry, mount e marker."""
        from core.discovery import write_device_marker

        keep_id = args.keep
        remove_id = args.remove

        if not keep_id or not remove_id:
            print("[MERGE] Errore: --keep e --remove sono obbligatori.")
            print("  Esempio: penelope device merge --keep 5 --remove 6")
            return

        if keep_id == remove_id:
            print("[MERGE] Errore: keep e remove devono essere diversi.")
            return

        with db as store:
            # ── Fase 1: dry-run (raccolta info) ──
            keep_dev = store.get_device(keep_id)
            remove_dev = store.get_device(remove_id)

            if not keep_dev:
                print(f"[MERGE] Errore: device keep_id={keep_id} non trovato.")
                return
            if not remove_dev:
                print(f"[MERGE] Errore: device remove_id={remove_id} non trovato.")
                return

            # Conteggio file_registry
            fr_rows = store._query(
                "SELECT COUNT(*) AS cnt FROM file_registry WHERE device_id = %s",
                (remove_id,),
            )
            fr_count = fr_rows[0]["cnt"] if fr_rows else 0

            # Mount di remove_id
            remove_mounts = store.list_device_mounts(device_id=remove_id)
            # Mount di keep_id (per sapere quali conflitti ci sono)
            keep_mounts = store.list_device_mounts(device_id=keep_id)

            # Marker file attuali
            from core.discovery import read_device_marker

            marker_info = {}
            for m in remove_mounts:
                path = m.get("mount_root")
                if path:
                    marker_info[path] = read_device_marker(path)

        # ── Report dry-run ──
        print()
        print("=" * 70)
        print("  DEVICE MERGE — DRY RUN")
        print("=" * 70)
        print()
        print(f"  Da MANTENERE (keep):   [{keep_dev['label']}] (id={keep_id}, type={keep_dev['type']})")
        print(f"  Da ELIMINARE (remove): [{remove_dev['label']}] (id={remove_id}, type={remove_dev['type']})")
        print()
        print(f"  Righe file_registry da riassegnare: {fr_count}")
        print()
        if remove_mounts:
            print("  Mount di remove_id (copiati in keep_id se non già presenti):")
            for m in remove_mounts:
                already = "⚠ GIA' PRESENTE" if any(
                    km["hostname"] == m["hostname"]
                    for km in keep_mounts
                ) else "→ da copiare"
                print(f"    host={m['hostname']:20s} mount={m['mount_root']}  {already}")
        else:
            print("  Mount di remove_id: nessuno")
        print()
        for path, marker in marker_info.items():
            if marker:
                print(f"  Marker su {path}/.penelope_device.json:")
                print(f"      device_id={marker['device_id']}, label={marker['label']}")
                print(f"      → sarà riscritto come device_id={keep_id}, label={keep_dev['label']}")
            else:
                print(f"  Marker su {path}/.penelope_device.json: ASSENTE (verrà creato)")
        print()
        print(f"  Dopo il merge: il device [{remove_dev['label']}] (id={remove_id}) "
              "sarà cancellato.")
        print()
        print("=" * 70)
        print()

        # ── Fase 2: conferma ──
        if not args.yes:
            confirm = input("  Continuare con il merge? (s/N): ").strip().lower()
            if confirm not in ("s", "si", "y", "yes"):
                print("  Annullato.")
                return

        # ── Fase 3: esecuzione ──
        print()
        print("  Esecuzione merge...")
        print()

        with db as store:
            result = store.merge_devices(keep_id, remove_id)
            mount_paths = result.get("mount_paths", [])

        # ── Fase 4: rewrite marker suogni mount noto del vecchio device ──
        for path in mount_paths:
            ok = write_device_marker(path, keep_id, keep_dev["label"])
            if ok:
                print(f"  [OK] Marker riscritto: {path}/.penelope_device.json → "
                      f"device_id={keep_id}, label={keep_dev['label']}")
            else:
                print(f"  [WARN] Marker non scritto su {path}")

        print()
        print(f"  Merge completato.")
        print(f"    file_registry riassegnate: {result['file_registry_updated']}")
        print(f"    mounts copiati:             {result['mounts_copied']}")
        print(f"    mounts rimossi:             {result['mounts_removed']}")
        print(f"    marker riscritti:           {len(mount_paths)}")
        print()


def cmd_geo(args):
    """Geocoding GPS: coordinate EXIF → Location nodes."""
    from core.db.mariadb_store import MariaDBStore
    from core.ingestion.processor import process_geocoding

    if args.action == "process":
        """Processa tutti i file con GPS nei metadati."""
        import json

        db = MariaDBStore()
        with db as store:
            rows = store._query(
                "SELECT n.id, n.label, f.path FROM nodes n "
                "JOIN file_registry f ON f.node_id = n.id "
                "WHERE n.type = %s AND n.metadata LIKE %s",
                ("File", "%gps_lat%"),
            )

        if not rows:
            print("[GEO] Nessun file con coordinate GPS trovate.")
            print("   Le coordinate GPS vengono estratte dall'EXIF delle foto.")
            print("   Per ora nessuna foto ha EXIF con GPS (WhatsApp le stripping).")
            print("   L'infrastruttura e' pronta per quando arriveranno.")
            return

        print(f"[GEO] Geocoding di {len(rows)} file...")
        done = 0
        for r in rows:
            try:
                res = process_geocoding(r["id"], r["path"], db)
                if res:
                    done += 1
            except Exception as e:
                logger.debug("Errore su %s: %s", r["label"], e)

        print(f"[GEO] Completato: {done} file geocodificati")

    elif args.action == "test":
        """Test Nominatim con coordinate di esempio."""
        from core.ingestion.processor import _reverse_geocode

        # Test con coordinate note
        test_coords = [
            (41.8902, 12.4922, "Colosseo, Roma"),
            (48.8566, 2.3522, "Parigi"),
            (40.7128, -74.0060, "New York"),
        ]
        for lat, lon, name in test_coords:
            print(f"\n{name} ({lat}, {lon}):")
            result = _reverse_geocode(lat, lon)
            if result:
                print(f"  Display: {result['display_name'][:100]}")
                print(f"  City: {result['city']}")
                print(f"  Country: {result['country']}")
            else:
                print("  Errore")

        # Mostra stato cache
        from core.ingestion.processor import _load_geocode_cache
        cache = _load_geocode_cache()
        print(f"\nCache geocoding: {len(cache)} entries")

    elif args.action == "cache":
        """Mostra/gestisce la cache geocoding."""
        from core.ingestion.processor import _load_geocode_cache, _GEOCODE_CACHE_PATH
        cache = _load_geocode_cache()
        print(f"\n[GEO] Cache geocoding:")
        print(f"   File: {_GEOCODE_CACHE_PATH}")
        print(f"   Entries: {len(cache)}")
        if cache:
            print(f"\nCoordinate in cache:")
            for key, val in list(cache.items())[:10]:
                print(f"   {key}: {val.get('display_name', '?')[:80]}")
            if len(cache) > 10:
                print(f"   ... e {len(cache)-10} altre")


def cmd_event(args):
    """Gestione nodi Event (data, scene, calendario)."""
    from core.db.mariadb_store import MariaDBStore
    from core.ingestion.processor import process_date_event

    if args.action == "create-from-dates":
        """Crea Event nodes dalla data nei nomi file."""
        import re
        from datetime import datetime
        from pathlib import Path

        db = MariaDBStore()
        with db as store:
            rows = store._query(
                "SELECT n.id, n.label, f.path FROM nodes n "
                "JOIN file_registry f ON f.node_id = n.id "
                "WHERE n.type = %s ORDER BY n.label",
                ("File",)
            )

        if not rows:
            print("[EVENT] Nessun file trovato.")
            return

        print(f"[EVENT] Creazione Event nodes da date per {len(rows)} file...")
        created = 0
        linked = 0

        for r in rows:
            try:
                res = process_date_event(r["id"], r["path"], db)
                if res:
                    linked += 1
            except Exception as e:
                logger.debug("Errore su %s: %s", r["label"], e)

        with db as store:
            ev_count = store._query(
                "SELECT COUNT(*) as cnt FROM nodes WHERE type = 'Event'"
            )

        print(f"[EVENT] Completato: {linked} file collegati, {ev_count[0]['cnt']} Event nodes totali")

    elif args.action == "status":
        """Statistiche Event nodes."""
        db = MariaDBStore()
        with db as store:
            total = store._query(
                "SELECT COUNT(*) as cnt FROM nodes WHERE type = 'Event'"
            )
            with_file_count = store._query(
                "SELECT COUNT(*) as cnt FROM nodes WHERE type = 'Event' AND metadata LIKE %s",
                ("%file_count%",)
            )
            from_scene = store._query(
                "SELECT COUNT(*) as cnt FROM nodes WHERE type = 'Event' AND metadata LIKE %s",
                ("%scene_detection%",)
            )
            from_date = store._query(
                "SELECT COUNT(*) as cnt FROM nodes WHERE type = 'Event' AND metadata LIKE %s",
                ("%from_filename%",)
            )
            edges = store._query(
                "SELECT COUNT(*) as cnt FROM edges WHERE relation = 'CREATED_AT'"
            )

        print(f"\n[EVENT] Statistiche:")
        print(f"   Nodi Event totali:  {total[0]['cnt']}")
        print(f"   - con file_count:   {with_file_count[0]['cnt']}")
        print(f"   - da scene detect:  {from_scene[0]['cnt']}")
        print(f"   - da date filename: {from_date[0]['cnt']}")
        print(f"   Edge CREATED_AT:    {edges[0]['cnt']}")

        # Mostra i primi eventi
        if total[0]['cnt'] > 0:
            rows = store._query(
                "SELECT id, label, metadata FROM nodes WHERE type = 'Event' ORDER BY label LIMIT 10"
            )
            print(f"\nPrimi {len(rows)} Event nodes:")
            for r in rows:
                meta = r['metadata']
                if isinstance(meta, str):
                    import json
                    meta = json.loads(meta) if meta else {}
                n_files = meta.get('file_count', 0) if isinstance(meta, dict) else 0
                src = meta.get('date_type', meta.get('source', '?')) if isinstance(meta, dict) else '?'
                print(f"   {r['label']}: {n_files} file [{src}]")

    elif args.action == "list":
        """Elenca tutti gli Event nodes."""
        db = MariaDBStore()
        with db as store:
            rows = store._query(
                "SELECT id, label, metadata FROM nodes WHERE type = 'Event' ORDER BY label"
            )

        if not rows:
            print("[EVENT] Nessun nodo Event trovato.")
            return

        print(f"\n[EVENT] {len(rows)} Event nodes:")
        import json
        for r in rows:
            meta = r['metadata']
            if isinstance(meta, str):
                meta = json.loads(meta) if meta else {}
            n_files = meta.get('file_count', 0) if isinstance(meta, dict) else 0
            src = meta.get('date_type', meta.get('source', '?')) if isinstance(meta, dict) else '?'
            date = meta.get('date', '') if isinstance(meta, dict) else ''
            print(f"   {r['label'][:40]:40s} files={n_files}  source={src}  date={date}")


def cmd_video(args):
    """Scene detection per video."""
    from core.db.mariadb_store import MariaDBStore
    from core.ingestion.processor import process_scene_detection

    if args.action == "detect-scenes":
        """Rileva scene in tutti i video del grafo."""
        db = MariaDBStore()
        with db as store:
            # Trova tutti i video
            rows = store._query(
                "SELECT n.id, n.label, f.path FROM nodes n "
                "JOIN file_registry f ON f.node_id = n.id "
                "WHERE n.type = %s AND f.mime_type LIKE %s",
                ("File", "video/%")
            )

        if not rows:
            print("[VIDEO] Nessun video trovato nel grafo.")
            return

        print(f"[VIDEO] Scene detection su {len(rows)} video...")
        total_scenes = 0
        for r in rows:
            print(f"   {r['label']}...", end=" ", flush=True)
            try:
                result = process_scene_detection(r["id"], r["path"], db)
                if result:
                    # Conta scene create
                    ev = db._query(
                        "SELECT COUNT(*) as cnt FROM edges "
                        "WHERE source_id = %s AND relation = 'HAS_SCENE'",
                        (r["id"],),
                    )
                    n = ev[0]["cnt"] if ev else 0
                    total_scenes += n
                    print(f"{n} scene")
                else:
                    print("nessuna scena")
            except Exception as e:
                print(f"ERRORE: {e}")

        print(f"\n[VIDEO] Completato: {len(rows)} video, {total_scenes} scene totali")

    elif args.action == "list":
        """Elenca video nel grafo."""
        db = MariaDBStore()
        with db as store:
            rows = store._query(
                "SELECT n.id, n.label, f.path, f.mime_type, f.size_bytes "
                "FROM nodes n JOIN file_registry f ON f.node_id = n.id "
                "WHERE n.type = %s AND f.mime_type LIKE %s "
                "ORDER BY f.size_bytes DESC",
                ("File", "video/%")
            )

        if not rows:
            print("[VIDEO] Nessun video trovato.")
            return

        print(f"\n[VIDEO] {len(rows)} video nel grafo:")
        for r in rows:
            has_scenes = ""
            node = db.get_node(r["id"])
            if node:
                import json
                meta = node.get("metadata")
                if isinstance(meta, str):
                    meta = json.loads(meta) if meta else {}
                sc = meta.get("scene_count", 0) if isinstance(meta, dict) else 0
                if sc:
                    has_scenes = f" [{sc} scene]"

            size_mb = r["size_bytes"] / (1024*1024) if r["size_bytes"] else 0
            print(f"   {r['label']}{has_scenes}")
            print(f"       size: {size_mb:.1f}MB | type: {r['mime_type']}")
            print(f"       path: {r['path'][:120]}")

    elif args.action == "status":
        """Statistiche video."""
        db = MariaDBStore()
        with db as store:
            total = store._query(
                "SELECT COUNT(*) as cnt FROM nodes n "
                "JOIN file_registry f ON f.node_id = n.id "
                "WHERE n.type = %s AND f.mime_type LIKE %s",
                ("File", "video/%")
            )
            with_scenes = store._query(
                "SELECT COUNT(*) as cnt FROM nodes n "
                "JOIN file_registry f ON f.node_id = n.id "
                "WHERE n.type = %s AND f.mime_type LIKE %s "
                "AND n.metadata LIKE %s",
                ("File", "video/%", "%has_scenes%")
            )
            events = store._query(
                "SELECT COUNT(*) as cnt FROM nodes WHERE type = 'Event'"
            )

        print(f"\n[VIDEO] Statistiche:")
        print(f"   Video totali:     {total[0]['cnt']}")
        print(f"   Con scene detect: {with_scenes[0]['cnt']}")
        print(f"   Nodi Event:       {events[0]['cnt']}")


def cmd_quarantine(args):
    """Gestione quarantena HSD."""
    from egida.config import EGIDA_QUARANTINE_DIR
    q = Quarantine(EGIDA_QUARANTINE_DIR)

    if args.action == "list":
        entries = q.list_quarantine()
        if not entries:
            print("Nessun file in quarantena.")
            return
        print(f"\n[QUARANTINE] {len(entries)} entry")
        for e in entries:
            print(f"   {e.get('timestamp', '?')}: {e['match_count']} match HSD")
            for m in e.get("matches", [])[:3]:
                print(f"      - [{m['pattern']}] line {m['line']}: {m['snippet'][:80]}")
            if len(e.get("matches", [])) > 3:
                print(f"      ... e altri {len(e['matches']) - 3} match")

    elif args.action == "clear":
        count = q.clear()
        print(f"[QUARANTINE] Svuotata: {count} entry rimosse.")


def cmd_face(args):
    """Face detection e recognition via DeepFace."""
    from core.db.mariadb_store import MariaDBStore
    import pathlib as _pl

    if args.action == "test":
        """Testa DeepFace su un'immagine."""
        from core.recognition.deepface_engine import detect_faces

        p = _pl.Path(args.path)
        if not p.exists():
            print(f"[ERR] File non trovato: {p}")
            return

        print(f"[FACE] InsightFace detection su: {p}")
        print("   (primo avvio: download modello buffalo_l, ~30MB)")

        faces = detect_faces(str(p))
        if not faces:
            print("   Nessun volto rilevato.")
            return

        print(f"   Trovati {len(faces)} volti:")
        for i, face in enumerate(faces, 1):
            bbox = face["bbox"]
            print(f"   [{i}] BBox: {bbox}")
            print(f"        Confidenza: {face['det_score']:.2f}")
            gender = "M" if face.get("gender") == 1 else "F" if face.get("gender") == 0 else "?"
            age = face.get("age", "?")
            print(f"        Genere: {gender}, Eta: {age}")
            emb = face.get("embedding")
            if emb:
                print(f"        Embedding: {len(emb)} dim (ArcFace 512)")
                print(f"        Primi 4 valori: {[round(v,4) for v in emb[:4]]}")

    elif args.action == "process-all":
        """Processa TUTTE le immagini con InsightFace (detection + embedding)."""
        db = MariaDBStore()
        from core.recognition.deepface_engine import batch_process_images

        print("[FACE] Processo tutte le immagini con InsightFace...")
        print("   (primo avvio: download modello buffalo_l, ~30MB)")
        result = batch_process_images(db, limit=args.limit)
        print(f"[FACE] Fatto: {result['processed']} immagini, "
              f"{result['with_faces']} con volti")

    elif args.action == "status":
        """Stato face recognition nel grafo."""
        db = MariaDBStore()

        with_faces = db._query(
            "SELECT COUNT(*) as cnt FROM nodes WHERE type = %s AND metadata LIKE %s",
            ("File", "%has_faces%"))
        insight = db._query(
            "SELECT COUNT(*) as cnt FROM nodes WHERE type = %s AND metadata LIKE %s",
            ("File", "%insightface%"))
        persons = db._query(
            "SELECT COUNT(*) as cnt FROM nodes WHERE type = %s", ("Person",))
        persons_emb = db._query(
            "SELECT COUNT(*) as cnt FROM nodes WHERE type = %s AND metadata LIKE %s",
            ("Person", "%embedding_dim%"))

        emb_dir = _pl.Path("data/embeddings")
        npy_files = len(list(emb_dir.glob("*.npy"))) if emb_dir.exists() else 0

        print(f"\n[FACE] Stato face recognition:")
        print(f"   Immagini con volti:    {with_faces[0]['cnt']}")
        print(f"   - di cui InsightFace:   {insight[0]['cnt']}")
        print(f"   Nodi Person totali:     {persons[0]['cnt']}")
        print(f"   - con embedding:        {persons_emb[0]['cnt']}")
        print(f"   File embedding (.npy):  {npy_files}")

    elif args.action == "cluster":
        """Clustering pairwise: trova e unisce volti simili."""
        db = MariaDBStore()
        from core.recognition.deepface_engine import find_similar_persons, merge_persons

        threshold = args.threshold
        print(f"[FACE] Clustering nodi Person (similarita > {threshold})...")

        pairs = find_similar_persons(db, threshold=threshold, batch_size=args.batch)
        print(f"[FACE] Trovate {len(pairs)} coppie simili")

        if pairs and args.merge:
            print(f"[FACE] Unione nodi simili (soglia merge: {threshold})...")
            removed = merge_persons(db, pairs, merge_threshold=threshold)
            print(f"[FACE] Unione completata: {removed} nodi rimossi")

    elif args.action == "cluster-dbscan":
        """Clustering DBSCAN: raggruppa volti simili senza specificare N cluster.

        Vantaggio rispetto a 'cluster' pairwise:
        - O(n log n) invece di O(n²)
        - Non richiede soglia di similarità esplicita
        - Gestisce automaticamente il rumore (volti isolati)
        """
        db = MariaDBStore()

        # Carica embedding
        from core.recognition.deepface_engine import load_embedding

        rows = db._query(
            "SELECT id FROM nodes WHERE type = 'Person' AND metadata LIKE %s",
            ("%embedding_dim%",),
        )
        print(f"[FACE] DBSCAN clustering su {len(rows)} nodi Person con embedding...")

        if not rows:
            print("[FACE] Nessun nodo con embedding trovato. Esegui prima: penelope face reprocess")
            return

        # Prepara matrice embedding
        import numpy as np
        person_ids = []
        embeddings = []
        for r in rows:
            emb = load_embedding(r["id"])
            if emb is not None:
                person_ids.append(r["id"])
                embeddings.append(emb)

        if len(person_ids) < 2:
            print("[FACE] Servono almeno 2 nodi con embedding per il clustering.")
            return

        X = np.array(embeddings)
        print(f"[FACE] Matrice embedding: {X.shape}")

        # DBSCAN
        from sklearn.cluster import DBSCAN

        eps = args.eps  # distanza massima tra due punti per essere vicini
        min_samples = args.min_samples or 2  # minimo punti per cluster

        print(f"[FACE] DBSCAN(eps={eps}, min_samples={min_samples})...")
        clustering = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine", n_jobs=-1).fit(X)

        labels = clustering.labels_
        n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
        n_noise = list(labels).count(-1)

        print(f"[FACE] Risultati: {n_clusters} cluster, {n_noise} rumori")

        # Per ogni cluster, mostra statistiche
        from collections import Counter
        cluster_counts = Counter(labels)
        for label, count in cluster_counts.most_common():
            if label == -1:
                print(f"   Rumore: {count} volti")
            else:
                # Mostra i primi 3 nomi
                indices = [i for i, l in enumerate(labels) if l == label]
                sample_names = []
                for idx in indices[:3]:
                    node_data = db.get_node(person_ids[idx])
                    if node_data:
                        sample_names.append(node_data.get("label", "?"))
                print(f"   Cluster {label}: {count} volti (es: {', '.join(sample_names)})")

        if args.merge and n_clusters > 0:
            merged = 0
            # Per ogni cluster, unisci tutti i nodi al primo del cluster
            # Usa SQL diretto (GraphBridge richiederebbe il grafo caricato)
            for label in set(labels):
                if label == -1:
                    continue
                indices = [i for i, l in enumerate(labels) if l == label]
                if len(indices) < 2:
                    continue

                keep_id = person_ids[indices[0]]
                for idx in indices[1:]:
                    merge_id = person_ids[idx]
                    try:
                        # Riassegna edge via SQL
                        db._execute(
                            "UPDATE edges SET source_id = %s WHERE source_id = %s AND target_id != %s",
                            (keep_id, merge_id, keep_id),
                        )
                        db._execute(
                            "UPDATE edges SET target_id = %s WHERE target_id = %s AND source_id != %s",
                            (keep_id, merge_id, keep_id),
                        )
                        # Riassegna file_registry
                        db._execute(
                            "UPDATE file_registry SET node_id = %s WHERE node_id = %s",
                            (keep_id, merge_id),
                        )
                        # Elimina nodo duplicato
                        db._execute("DELETE FROM nodes WHERE id = %s", (merge_id,))
                        # Unisci embedding (media)
                        import numpy as np
                        emb_keep_path = _pl.Path("data/embeddings") / f"{keep_id}.npy"
                        emb_del_path = _pl.Path("data/embeddings") / f"{merge_id}.npy"
                        if emb_keep_path.exists() and emb_del_path.exists():
                            e_k = np.load(str(emb_keep_path))
                            e_d = np.load(str(emb_del_path))
                            np.save(str(emb_keep_path), ((e_k + e_d) / 2.0).astype(np.float32))
                            emb_del_path.unlink(missing_ok=True)
                        merged += 1
                    except Exception as e:
                        logger.warning("Errore merge %s -> %s: %s", merge_id, keep_id, e)

            print(f"[FACE] Unione DBSCAN completata: {merged} nodi rimossi")

    elif args.action == "reprocess":
        """Riprocessa immagini gia' processate con YOLO -> InsightFace."""
        db = MariaDBStore()
        from core.recognition.deepface_engine import process_face_embedding

        exts = ("%.jpg", "%.jpeg", "%.png", "%.webp", "%.bmp")
        rows = db._query(
            """SELECT n.id, n.label, f.path 
            FROM nodes n
            JOIN file_registry f ON f.node_id = n.id
            WHERE n.type = %s 
              AND metadata LIKE %s
              AND metadata NOT LIKE %s
              AND (f.path LIKE %s OR f.path LIKE %s OR f.path LIKE %s 
                   OR f.path LIKE %s OR f.path LIKE %s)
            ORDER BY f.path
            """,
            ("File", "%face_count%", "%insightface%") + exts,
        )

        total = len(rows)
        print(f"[FACE] Aggiorno {total} immagini da YOLO -> InsightFace...")
        print("   (primo avvio: download modello buffalo_l, ~30MB)")
        ok = 0
        for i, r in enumerate(rows, 1):
            try:
                if process_face_embedding(r["id"], r["path"], db):
                    ok += 1
            except Exception as e:
                logger.debug("Errore %s: %s", r["path"], e)
            if i % 50 == 0:
                print(f"   [{i}/{total}] ok={ok}")
        print(f"[FACE] Fatto: {ok}/{total} aggiornate")

    elif args.action == "embedding-status":
        """Mostra statistiche embedding."""
        emb_dir = _pl.Path("data/embeddings")

        if not emb_dir.exists():
            print("[FACE] Nessun embedding salvato (data/embeddings/ non esiste)")
            return

        npy_files = list(emb_dir.glob("*.npy"))
        print(f"[FACE] {len(npy_files)} file embedding (.npy)")

        if npy_files:
            import numpy as np
            first = np.load(str(npy_files[0]))
            print(f"   Dimensione: {first.shape}")
            print(f"   Range: [{first.min():.3f}, {first.max():.3f}]")
            print(f"   Norma L2: {np.linalg.norm(first):.3f}")

            if len(npy_files) >= 2:
                second = np.load(str(npy_files[1]))
                sim = np.dot(first, second) / (np.linalg.norm(first) * np.linalg.norm(second))
                print(f"   Similarita primi 2: {sim:.4f}")




def main():
    parser = argparse.ArgumentParser(description="Penelope -- Ingestion & Graph")
    parser.add_argument("--log-level", help="Livello di logging (DEBUG, INFO, WARNING)")
    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    p_scan = sub.add_parser("scan", help="Scansiona una directory (gerarchica)")
    p_scan.add_argument("path", help="Directory da scandire")
    p_scan.add_argument("--device", default=None, help="Nome del dispositivo (es. headless, hdd-ext)")
    p_scan.add_argument("--project", default=None, help="Nome del progetto (default: nome cartella)")
    p_scan.add_argument("--deep", action="store_true",
                        help="Analisi profonda: YOLO oggetti, face recognition, NER documenti, metadati video (lento)")
    p_scan.set_defaults(func=cmd_scan)

    # watchdog
    p_wd = sub.add_parser("watchdog", help="Watchdog file system in tempo reale")
    p_wd.add_argument("action", choices=["start", "status"],
                       help="start=avvia watchdog, status=mostra stato")
    p_wd.add_argument("--path", help="Directory singola da osservare (opzionale)")
    p_wd.add_argument("--device", default="watchdog", help="Nome dispositivo")
    p_wd.add_argument("--project", help="Nome progetto (default: nome cartella)")
    p_wd.set_defaults(func=cmd_watchdog)

    # tree
    p_tree = sub.add_parser("tree", help="Mostra l'albero gerarchico (directory → file)")
    p_tree.add_argument("--project", default=None, help="Project node ID (default: tutte le Project)")
    p_tree.add_argument("--depth", type=int, default=10, help="Profondità massima (default: 10)")
    p_tree.set_defaults(func=cmd_tree)

    # scan:all
    p_all = sub.add_parser("scan:all", help="Scansiona tutti gli storage configurati")
    p_all.set_defaults(func=cmd_scan_all)

    # queue
    p_q = sub.add_parser("queue", help="Gestione coda di elaborazione")
    p_q.add_argument("action", choices=["process", "loop", "status", "reset-stale"], help="Azione")
    p_q.add_argument("--batch", type=int, default=5, help="Elementi per batch")
    p_q.add_argument("--interval", type=float, default=5.0, help="Secondi tra batch (solo loop)")
    p_q.add_argument("--max-age", type=int, default=30, help="Età massima minuti per reset-stale (default: 30)")
    p_q.add_argument("--reset-stale", action="store_true", help="Resetta elementi bloccati prima di processare")
    p_q.set_defaults(func=cmd_queue)

    # search
    p_s = sub.add_parser("search", help="Ricerca semantica")
    p_s.add_argument("query", help="Testo da cercare")
    p_s.add_argument("--top", type=int, default=10, help="Numero risultati")
    p_s.add_argument("--mime", default=None, help="Filtro mime-type (es. text/markdown)")
    p_s.set_defaults(func=cmd_search)

    # graph
    p_g = sub.add_parser("graph", help="Stato del grafo")
    p_g.add_argument("action", nargs="?", default="status", choices=["status"], help="Azione")
    p_g.set_defaults(func=cmd_graph)

    # configure
    p_cfg = sub.add_parser("configure", help="Configura credenziali nel keyring")
    p_cfg.add_argument("action", choices=["set", "clear", "test"], help="Azione")
    p_cfg.add_argument("--user", default=None, help="Utente MariaDB (default: da settings)")
    p_cfg.add_argument("--password", default=None, help="Password (se omessa, richiesta interactiva)")
    p_cfg.set_defaults(func=cmd_configure)

    # db
    p_db = sub.add_parser("db", help="Operazioni sul database")
    p_db.add_argument("action", choices=["dedup", "stats", "verify"], help="Azione")
    p_db.add_argument("--device", default=None, help="Filtra verifica per device label")
    p_db.add_argument("--limit", type=int, default=0, help="Limite righe da verificare")
    p_db.set_defaults(func=cmd_db)

    # device
    p_dev = sub.add_parser("device", help="Gestione device e mount per-host")
    p_dev.add_argument("action", choices=["register", "list", "merge"], help="Azione")
    p_dev.add_argument("--label", default=None, help="Nome del device (per register)")
    p_dev.add_argument("--mount", default=None, help="Path del mount su questo host (per register)")
    p_dev.add_argument("--type", default=None, choices=["local", "external", "network", "server"],
                       help="Tipo device (default: local)")
    p_dev.add_argument("--volume-uuid", default=None, help="UUID del volume (opzionale)")
    p_dev.add_argument("--hostname", default=None, help="Hostname per cui registrare il mount (default: host corrente)")
    p_dev.add_argument("--keep", type=int, default=None, help="ID del device da mantenere (per merge)")
    p_dev.add_argument("--remove", type=int, default=None, help="ID del device da eliminare (per merge)")
    p_dev.add_argument("--yes", action="store_true", help="Salta conferma interattiva (per merge)")
    p_dev.set_defaults(func=cmd_device)

    # geo
    p_g = sub.add_parser("geo", help="Geocoding GPS (Nominatim)")
    p_g.add_argument("action", choices=["process", "test", "cache"],
                      help="""
        process=geocoding di tutti i file con GPS,
        test=test Nominatim con coordinate note,
        cache=mostra cache geocoding
    """)
    p_g.set_defaults(func=cmd_geo)

    # event
    p_e = sub.add_parser("event", help="Gestione nodi Event (date, scene, calendario)")
    p_e.add_argument("action", choices=["create-from-dates", "status", "list"],
                      help="""
        create-from-dates=crea Event nodes dalla data nei nomi file,
        status=statistiche Event nodes,
        list=elenca tutti gli Event nodes
    """)
    p_e.set_defaults(func=cmd_event)

    # video
    p_v = sub.add_parser("video", help="Scene detection per video")
    p_v.add_argument("action", choices=["detect-scenes", "list", "status"],
                      help="""
        detect-scenes=rileva scene in tutti i video,
        list=elenca video nel grafo,
        status=statistiche video
    """)
    p_v.set_defaults(func=cmd_video)

    # quarantine
    p_q = sub.add_parser("quarantine", help="Gestione quarantena HSD")
    p_q.add_argument("action", choices=["list", "clear"], help="Azione")
    p_q.set_defaults(func=cmd_quarantine)

    # face (DeepFace)
    p_f = sub.add_parser("face", help="Face detection/recognition via DeepFace")
    p_f.add_argument("action", choices=[
        "test", "process-all", "reprocess", "status",
        "cluster", "cluster-dbscan", "embedding-status",
    ], help="""
        test=prova su una foto,
        process-all=processa tutte le foto,
        reprocess=aggiorna da YOLO a InsightFace,

        status=stato face recognition,
        cluster=raggruppa volti simili (pairwise),
        cluster-dbscan=raggruppa volti simili (DBSCAN, piu veloce su larga scala),
        embedding-status=statistiche embedding
    """)
    p_f.add_argument("--path", help="Path immagine (per test)")
    p_f.add_argument("--limit", type=int, default=0, help="Limite immagini (per process-all)")
    p_f.add_argument("--threshold", type=float, default=0.5,
                      help="Soglia similarita per cluster pairwise (default: 0.5)")
    p_f.add_argument("--batch", type=int, default=50, help="Batch size")
    p_f.add_argument("--merge", action="store_true", help="Esegui merge dopo cluster")
    p_f.add_argument("--eps", type=float, default=0.4,
                      help="Distanza massima DBSCAN (default: 0.4, minore = cluster piu stretti)")
    p_f.add_argument("--min-samples", type=int, default=2,
                      help="Minimo punti per formare un cluster DBSCAN (default: 2)")
    p_f.set_defaults(func=cmd_face)

    args = parser.parse_args()
    _setup_logging(args.log_level)
    args.func(args)


if __name__ == "__main__":
    main()

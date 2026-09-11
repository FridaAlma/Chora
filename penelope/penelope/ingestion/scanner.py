"""
Scanner del filesystem — MODALITÀ GERARCHICA.

Ogni directory → nodo Directory.
Ogni file → nodo File con categoria (image|video|document|other).
La gerarchia del filesystem è preservata nel grafo via `parent_id` + edge CONTAINS.

Due fasi:
  Fase A (--hierarchy-only, default): solo struttura directory + categoria file
  Fase B (--deep): anche analisi contenuto (YOLO, face, video metadata, NER, embedding)

Le analisi profonde sono delegabili anche alla coda lazy (ingestion_queue)
e scritte in analyzed[] per evitare ri-analisi.
"""

import json
import logging
from pathlib import Path
from typing import Callable, Optional

import watchdog.events
import watchdog.observers

from penelope.config import settings
from penelope.db.mariadb_store import MariaDBStore
from egida.filters import HSDFilter, HSDMatch
from egida.quarantine import Quarantine
from penelope.ingestion.metadata import FileMetadata, classify_category

logger = logging.getLogger(__name__)


class ScanResult:
    """Risultato della scansione di un file/directory."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.node_id: Optional[str] = None
        self.skipped: bool = False
        self.skip_reason: Optional[str] = None
        self.hsd_match: Optional[HSDMatch] = None
        self.error: Optional[str] = None
        self.is_directory: bool = Path(file_path).is_dir()

    @property
    def success(self) -> bool:
        return self.node_id is not None and not self.skipped

    def __repr__(self) -> str:
        tp = "DIR" if self.is_directory else "FILE"
        return f"<ScanResult [{tp}] {self.file_path} success={self.success} skipped={self.skipped}>"


class FileScanner:
    """
    Scanner gerarchico che preserva la struttura filesystem nel grafo.

    Ogni directory diventa un nodo `Directory` con edge CONTAINS verso i figli.
    Ogni file diventa un nodo `File` con categoria (image|video|document|other)
    e categoria derivata dal MIME type.
    """

    def __init__(
        self,
        db: Optional[MariaDBStore] = None,
        hsd_filter: Optional[HSDFilter] = None,
        quarantine: Optional[Quarantine] = None,
        device_name: str = "unknown",
    ):
        self.db = db or MariaDBStore()
        self.hsd_filter = hsd_filter or HSDFilter()
        from egida.config import EGIDA_QUARANTINE_DIR
        self.quarantine = quarantine or Quarantine(EGIDA_QUARANTINE_DIR)
        self.device_name = device_name

        # Cache incrementale: path assoluto → (size, mtime) già indicizzati
        self._existing_paths: Optional[dict[str, tuple]] = None

        # Callback opzionale: chiamato dopo ogni file/dir processato
        self.on_file_processed: Optional[Callable[[ScanResult], None]] = None

    # ─── Fingerprint incrementale (evita re-scan) ────────────────

    def _load_existing_paths(self, device: Optional[str] = None) -> dict[str, tuple]:
        """Carica (path assoluto → (size_bytes, mtime)) dei file già indicizzati.

        Usato per saltare file già presenti (incrementale).
        """
        if self._existing_paths is not None:
            return self._existing_paths

        dev_where = ""
        params = ()
        if device:
            dev_where = "WHERE device = %s"
            params = (device,)

        rows = self.db._query(
            f"""SELECT n.metadata, f.size_bytes, f.path
               FROM file_registry f
               JOIN nodes n ON n.id = f.node_id
               {dev_where}""",
            params,
        )
        self._existing_paths = {}
        for r in rows:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            abs_path = meta.get("path") if isinstance(meta, dict) else None
            if abs_path:
                self._existing_paths[abs_path] = (
                    r.get("size_bytes"),
                    meta.get("modified"),
                )
        logger.debug("Fingerprint incrementale: %d file già noti", len(self._existing_paths))
        return self._existing_paths

    def _is_new_file(self, path: Path, size_bytes: int, modified_iso: str) -> bool:
        """True se il file non è già stato indicizzato (o è cambiato)."""
        existing = self._load_existing_paths(self.device_name)
        abs_path = str(path.absolute())
        if abs_path not in existing:
            return True
        known_size, known_modified = existing[abs_path]
        # Re-scan solo se dimensione o data modifica cambiate
        if known_size != size_bytes:
            return True
        if known_modified and modified_iso != known_modified:
            return True
        return False

    # ─── Scan singolo file (Fase A: solo nodo + categoria) ─────────

    def scan_file(
        self,
        file_path: str | Path,
        parent_id: Optional[str] = None,
        _skip_batch: Optional[list] = None,
    ) -> ScanResult:
        """
        Processa un singolo file: crea nodo File con categoria.

        Args:
            file_path: percorso del file
            parent_id: UUID del nodo Directory/Project genitore
            _skip_batch: se fornito (lista di tuple), accoda l'insert per batch
                         invece di scrivere subito su DB

        Returns:
            ScanResult con esito
        """
        result = ScanResult(str(file_path))
        path = Path(file_path)

        try:
            # 1. Egida: filtro HSD
            hsd = self.hsd_filter.check_file(path)
            if hsd.is_infected:
                self.quarantine.isolate(hsd, path)
                result.skipped = True
                result.skip_reason = "HSD"
                result.hsd_match = hsd
                logger.info("FILE HSD SKIPPED: %s (%d match)", path, len(hsd.matches))
                self._notify(result)
                return result

            # 2. Metadati
            meta = FileMetadata(path)
            category = classify_category(meta.mime_type, path)

            # 2b. Dedup incrementale: skip se file già indicizzato e invariato
            meta_dict = meta.to_dict()
            if not self._is_new_file(path, meta.size_bytes, meta_dict["modified"]):
                result.skipped = True
                result.skip_reason = "UNCHANGED"
                logger.debug("FILE INVARIATO (fingerprint): %s", path)
                self._notify(result)
                return result

            # 2c. SHA-256 COMPUTED LAZY: NON leggiamo l'intero file qui.
            #     Il fingerprint (size+mtime) basta per lo scan veloce.
            #     L'hash viene calcolato in fase di analisi (queue) se serve.
            sha256 = ""  # placeholder, ricalcolato nella fase di analisi

            # 3. Crea nodo File
            import json as _json
            node_meta = {
                "extension": meta.extension,
                "size_bytes": meta.size_bytes,
                "mime_type": meta.mime_type,
                "category": category,
                "created": meta_dict["created"],
                "modified": meta_dict["modified"],
                "path": str(path.absolute()),
            }

            if _skip_batch is not None:
                # Accoda per batch insert
                import uuid as _uuid
                node_id = str(_uuid.uuid4())
                _skip_batch.append({
                    "node_id": node_id,
                    "type": "File",
                    "label": meta.file_name,
                    "category": category,
                    "parent_id": parent_id,
                    "metadata": _json.dumps(node_meta),
                    "path": str(path.absolute()),
                    "device": self.device_name,
                    "size_bytes": meta.size_bytes,
                    "sha256": sha256,
                    "mime_type": meta.mime_type,
                })
                result.node_id = node_id
            else:
                # Insert singolo (per retrocompat)
                node_id = self.db.create_node(
                    node_type="File",
                    label=meta.file_name,
                    metadata=node_meta,
                )
                result.node_id = node_id

                # Registra nel file_registry
                self.db.register_file(
                    node_id=node_id,
                    device=self.device_name,
                    path=str(path.absolute()),
                    size_bytes=meta.size_bytes,
                    sha256=meta.sha256,
                    mime_type=meta.mime_type,
                )

                # Edge CONTAINS verso il genitore
                if parent_id:
                    self.db.create_edge(
                        source_id=parent_id,
                        target_id=node_id,
                        relation="CONTAINS",
                        weight=1.0,
                    )

                # Accoda per analisi futura
                self.db.enqueue(node_id, priority=0)

            logger.debug("FILE INDEXED: %s → %s", path, result.node_id)

        except Exception as e:
            logger.error("ERRORE scansione %s: %s", file_path, e)
            result.error = str(e)

        self._notify(result)
        return result

    # ─── Scan ricorsivo directory (Fase A + batch) ───────────────

    def scan_directory(
        self,
        root_path: str | Path,
        project_label: Optional[str] = None,
        recursive: bool = True,
        deep: bool = False,
    ) -> list[ScanResult]:
        """
        Scansiona ricorsivamente una directory creando l'intera gerarchia.

        Fase A (sempre): crea nodi Directory + File con categoria.
        Fase B (deep=True): analisi contenuto (YOLO, face, video, document).

        Tutti gli insert DB sono in batch per performance.

        Args:
            root_path: directory da scandire
            project_label: nome del progetto (default: nome cartella)
            recursive: se True, entra nelle sottodirectory
            deep: se True, esegue anche analisi contenuto

        Returns:
            Lista di ScanResult
        """
        root = Path(root_path)
        if not root.is_dir():
            raise NotADirectoryError(f"Non è una directory: {root}")

        project_label = project_label or root.name

        # Trova o crea il nodo Project/radice
        existing = self.db._query(
            "SELECT id FROM nodes WHERE type = 'Project' AND label = %s LIMIT 1",
            (project_label,),
        )
        if existing:
            root_id = existing[0]['id']
            logger.info("Progetto esistente riutilizzato: %s (%s)", project_label, root_id)
        else:
            root_id = self.db.create_node(
                node_type="Project",
                label=project_label,
                metadata={
                    "path": str(root.absolute()),
                    "device": self.device_name,
                    "is_filesystem_root": True,
                },
            )
            logger.info("Progetto creato: %s (%s)", project_label, root_id)

        # Batch accumulator
        batch_nodes: list[dict] = []
        results: list[ScanResult] = []

        # Cerca nel DB le Directory già indicizzate sotto questa radice
        existing_rows = self.db._query(
            "SELECT id, label, metadata FROM nodes WHERE type = 'Directory' "
            "AND metadata LIKE concat('%%', %s, '%%')",
            (str(root.absolute()),),
        )
        existing_dirs: dict[str, str] = {}
        for r in existing_rows:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if isinstance(meta, dict) and meta.get("path"):
                existing_dirs[meta["path"]] = r["id"]
        logger.debug("Directory già note nel DB: %d", len(existing_dirs))

        # Walk ricorsivo (primo passo: costruisci la mappa path → node_id)
        dir_map: dict[str, str] = {str(root.absolute()): root_id}
        dir_map.update(existing_dirs)

        # Raccogli prima tutte le directory
        if recursive:
            for entry in sorted(root.rglob("*"), key=lambda p: (len(p.parts), str(p))):
                if entry.is_dir() and not _should_skip(entry):
                    self._scan_single_dir(entry, dir_map, root, self.device_name, batch_nodes)

        # Poi i file (con batch)
        walk = root.rglob("*") if recursive else root.glob("*")
        for entry in sorted(walk, key=lambda p: str(p)):
            if entry.is_file():
                if _should_skip(entry):
                    logger.debug("SKIP (escluso): %s", entry)
                    continue

                # Trova il parent_id
                parent_dir = str(entry.parent.absolute())
                parent_id = dir_map.get(parent_dir, root_id)

                # Accoda per batch
                r = self.scan_file(entry, parent_id=parent_id, _skip_batch=batch_nodes)
                results.append(r)

            elif entry.is_dir() and not recursive:
                # Directory non ricorsive => nessun figlio
                pass

        # ─── Flush batch ─────────────────────────────────────────
        if batch_nodes:
            self._flush_batch(batch_nodes, dir_map)
            logger.info("Batch flush: %d nodi, %d file_registry, %d archi",
                        len(batch_nodes), len(batch_nodes), len(batch_nodes))

        # ─── Fase B: analisi contenuto (deep) ─────────────────────
        if deep:
            self._run_deep_analysis(results)

        return results

    def _scan_single_dir(
        self,
        dir_path: Path,
        dir_map: dict[str, str],
        root: Path,
        device_name: str,
        batch_nodes: list[dict],
    ) -> str:
        """Crea un nodo Directory per una singola cartella (batch)."""
        import json as _json
        import uuid as _uuid

        abs_dir = str(dir_path.absolute())

        # Se già mappata, skip
        if abs_dir in dir_map:
            return dir_map[abs_dir]

        parent_abs = str(dir_path.parent.absolute())
        parent_id = dir_map.get(parent_abs)

        # Usa Project radice come parent se non trovi il genitore diretto
        if parent_id is None:
            parent_abs_root = str(root.absolute())
            parent_id = dir_map.get(parent_abs_root)
        if parent_id is None:
            # Fallback: crea il genitore mancante
            parent_id = dir_map.get(str(root.absolute()))

        node_id = str(_uuid.uuid4())
        node_meta = _json.dumps({
            "path": abs_dir,
            "device": device_name,
            "is_directory": True,
        })
        batch_nodes.append({
            "node_id": node_id,
            "type": "Directory",
            "label": dir_path.name,
            "category": None,
            "parent_id": parent_id,
            "metadata": node_meta,
            "path": abs_dir,
            "device": device_name,
            "size_bytes": None,
            "sha256": None,
            "mime_type": None,
        })
        dir_map[abs_dir] = node_id
        return node_id

    def _flush_batch(self, batch_nodes: list[dict], dir_map: dict[str, str]) -> None:
        """Scrive batch di nodi, file_registry, archi in un'unica transazione."""
        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                # ── Nodi ──
                sql_node = """INSERT INTO nodes (id, type, label, category, parent_id, metadata, analyzed)
                              VALUES (%s, %s, %s, %s, %s, %s, '{"meta":"pending"}')"""
                # analyzed iniziale = solo meta pending; il resto viene settato
                # dopo l'analisi profonda

                # Pre-mappa device → device_id
                device_cache: dict[str, int] = {}
                mount_roots: dict[int, Optional[str]] = {}

                rows_nodes = []
                rows_file_registry = []
                rows_edges = []
                enqueue_ids = []

                for entry in batch_nodes:
                    nid = entry["node_id"]
                    ntype = entry["type"]

                    # Nodo
                    rows_nodes.append((
                        nid, ntype, entry["label"], entry["category"],
                        entry["parent_id"], entry["metadata"],
                    ))

                    # File registry (solo per File)
                    if ntype == "File" and entry["path"]:
                        device = entry["device"]
                        if device not in device_cache:
                            dev = self.db.get_device_by_label(device)
                            if dev:
                                device_cache[device] = dev["id"]
                            else:
                                device_cache[device] = self.db.ensure_device(label=device)
                            mount_roots[device_cache[device]] = self.db.resolve_mount_root(device_cache[device])
                        did = device_cache[device]
                        stored_path = self.db._compute_rel_path(
                            entry["path"], mount_roots.get(did)
                        )
                        rows_file_registry.append((
                            nid, device, did, stored_path,
                            entry.get("size_bytes"), entry.get("sha256"),
                            entry.get("mime_type"),
                        ))
                        enqueue_ids.append(nid)

                    # Edge CONTAINS (se ha parent)
                    parent_id = entry.get("parent_id")
                    if parent_id and parent_id != nid:
                        rows_edges.append((
                            parent_id, nid, "CONTAINS", 1.0, None,
                        ))

                # Batch insert nodi
                for row in rows_nodes:
                    cur.execute(sql_node, row)

                # Batch insert file_registry
                if rows_file_registry:
                    sql_fr = """INSERT INTO file_registry
                                (node_id, device, device_id, path, size_bytes, sha256, mime_type)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)"""
                    for row in rows_file_registry:
                        cur.execute(sql_fr, row)

                # Batch insert edges
                if rows_edges:
                    sql_edge = """INSERT INTO edges (source_id, target_id, relation, weight, metadata)
                                  VALUES (%s, %s, %s, %s, %s)"""
                    for row in rows_edges:
                        cur.execute(sql_edge, row)

                # Batch enqueue
                if enqueue_ids:
                    sql_q = """INSERT INTO ingestion_queue (node_id, status, priority)
                               VALUES (%s, 'pending', 0)"""
                    for nid in enqueue_ids:
                        cur.execute(sql_q, (nid,))

                conn.commit()
                logger.debug("Batch flush: %d nodi, %d file_registry, %d archi, %d accodati",
                             len(rows_nodes), len(rows_file_registry), len(rows_edges), len(enqueue_ids))

        except Exception as e:
            conn.rollback()
            logger.error("Batch flush fallito, rollback: %s", e)
            raise
        finally:
            self.db.close()
            self.db._conn = None  # reset connessione

    def _run_deep_analysis(self, results: list[ScanResult]) -> None:
        """Fase B: analisi contenuto su tutti i file scanditi."""
        from penelope.ingestion.analyzer import (
            analyze_image, analyze_video, analyze_document,
            needs_analysis, ANALYSIS_YOLO, ANALYSIS_META, ANALYSIS_NER,
        )
        from penelope.db.chroma_store import ChromaStore

        logger.info("Analisi profonda (deep) avviata su %d file...", len(results))

        chroma = ChromaStore()
        count = {"image": 0, "video": 0, "document": 0, "other": 0, "skipped": 0}

        for res in results:
            if not res.success or res.skipped:
                count["skipped"] += 1
                continue

            path = Path(res.file_path)
            from penelope.ingestion.metadata import _guess_mime
            mime = _guess_mime(path)
            category = classify_category(mime, path)

            try:
                if category == "image":
                    analyze_image(res.node_id, str(path), self.db)
                    count["image"] += 1
                elif category == "video":
                    analyze_video(res.node_id, str(path), self.db)
                    count["video"] += 1
                elif category == "document":
                    analyze_document(res.node_id, str(path), self.db, chroma)
                    count["document"] += 1
                else:
                    count["other"] += 1
            except Exception as e:
                logger.warning("Deep analysis fallita per %s: %s", path, e)

            # Progress log ogni 20 file
            total_analyzed = sum(v for k, v in count.items() if k != "skipped")
            if total_analyzed % 20 == 0:
                logger.info("  Deep: %d analizzati...", total_analyzed)

        logger.info("Deep analysis completata: %d img, %d video, %d doc, %d other, %d skipped",
                     count["image"], count["video"], count["document"],
                     count["other"], count["skipped"])

    # ─── Watchdog ─────────────────────────────────────────────

    _instance = None

    @classmethod
    def _get_instance(cls, device_name: str = "watchdog"):
        if cls._instance is None:
            cls._instance = FileScanner(device_name=device_name)
        return cls._instance

    def start_watchdog(self, path: str | Path, project_label: Optional[str] = None) -> "WatchdogManager":
        manager = WatchdogManager(
            path=path,
            project_label=project_label,
            device_name=self.device_name,
            hsd_filter=self.hsd_filter,
            quarantine=self.quarantine,
        )
        manager.start()
        return manager

    def _notify(self, result: ScanResult) -> None:
        if self.on_file_processed:
            try:
                self.on_file_processed(result)
            except Exception as e:
                logger.warning("Callback on_file_processed fallito: %s", e)


# ─── WatchdogManager (invariato, ma crea Directory se nuova cartella) ───

import threading as _threading
import time as _time
from datetime import datetime as _dt

_WATCHED_PROJECTS: dict[str, str] = {}


class FileCreationHandler(watchdog.events.FileSystemEventHandler):
    """Handler watchdog che processa nuovi file e crea gerarchia."""

    def __init__(self, device_name: str, hsd_filter, quarantine, project_label: str):
        super().__init__()
        self.device_name = device_name
        self.hsd_filter = hsd_filter
        self.quarantine = quarantine
        self.project_label = project_label

        self._pending: dict[str, float] = {}
        self._lock = _threading.Lock()
        self._timer: Optional[_threading.Timer] = None

        self.stats = {
            "files_seen": 0, "files_indexed": 0, "files_hsd": 0,
            "files_skipped": 0, "files_error": 0, "dirs_created": 0,
            "started_at": None,
        }

        self._db = MariaDBStore()
        self._root_id = self._ensure_project()

        # Cache: path assoluto cartella → node_id Directory
        self._dir_cache: dict[str, str] = {}

    def _ensure_project(self) -> Optional[str]:
        with self._db as store:
            existing = store._query(
                "SELECT id FROM nodes WHERE type = 'Project' AND label = %s LIMIT 1",
                (self.project_label,),
            )
            if existing:
                return existing[0]["id"]
            return store.create_node(
                node_type="Project",
                label=self.project_label,
                metadata={"source": "watchdog", "device": self.device_name},
            )

    def _ensure_dir_node(self, dir_path: str) -> Optional[str]:
        """Crea nodo Directory se non esiste già."""
        abs_dir = str(Path(dir_path).absolute())
        if abs_dir in self._dir_cache:
            return self._dir_cache[abs_dir]

        with self._db as store:
            # Cerca esistente
            existing = store._query(
                "SELECT id FROM nodes WHERE type = 'Directory' AND metadata LIKE %s LIMIT 1",
                (f"%\"path\": \"{abs_dir}\"%",),
            )
            if existing:
                self._dir_cache[abs_dir] = existing[0]["id"]
                return existing[0]["id"]

            # Crea nuovo nodo Directory
            import json
            parent_abs = str(Path(abs_dir).parent.absolute())
            parent_id = self._dir_cache.get(parent_abs, self._root_id)
            # Se parent non è root, assicura che esista anche lui
            if parent_abs != abs_dir and parent_id == self._root_id and parent_abs not in self._dir_cache:
                parent_id = self._ensure_dir_node(parent_abs)

            nid = store.create_node(
                node_type="Directory",
                label=Path(abs_dir).name,
                metadata={"path": abs_dir, "device": self.device_name, "is_directory": True},
            )
            if parent_id:
                store.create_edge(source_id=parent_id, target_id=nid, relation="CONTAINS", weight=1.0)
            self._dir_cache[abs_dir] = nid
            with self._lock:
                self.stats["dirs_created"] += 1
            logger.info("WD DIR CREATED: %s → %s", abs_dir, nid[:12])
            return nid

    def on_created(self, event):
        if event.is_directory:
            self._ensure_dir_node(event.src_path)
            return
        self._debounce(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._debounce(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        self._debounce(event.dest_path)

    def _debounce(self, path: str):
        now = _time.time()
        with self._lock:
            self._pending[path] = now
            self.stats["files_seen"] += 1
        if self._timer is None or not self._timer.is_alive():
            self._timer = _threading.Timer(2.0, self._flush_pending)
            self._timer.daemon = True
            self._timer.start()

    def _flush_pending(self):
        now = _time.time()
        with self._lock:
            ready = [p for p, ts in self._pending.items() if now - ts >= 2.0]
            for p in ready:
                del self._pending[p]
        for file_path in ready:
            self._process_file(file_path)
        with self._lock:
            if self._pending:
                self._timer = _threading.Timer(2.0, self._flush_pending)
                self._timer.daemon = True
                self._timer.start()

    def _process_file(self, file_path: str):
        fpath = Path(file_path)
        if _should_skip(fpath):
            with self._lock:
                self.stats["files_skipped"] += 1
            return

        logger.debug("WD NEW: %s", file_path)

        try:
            with self._db as store:
                # Assicura che la directory genitore esista come nodo
                parent_dir = str(fpath.parent.absolute())
                parent_id = self._dir_cache.get(parent_dir) or self._ensure_dir_node(parent_dir)

                # 1. Egida: filtro HSD
                hsd = self.hsd_filter.check_file(fpath)
                if hsd.is_infected:
                    self.quarantine.isolate(hsd, fpath)
                    with self._lock:
                        self.stats["files_hsd"] += 1
                    return

                # 2. Metadati
                from penelope.ingestion.metadata import FileMetadata
                meta = FileMetadata(fpath)
                category = classify_category(meta.mime_type, fpath)

                # 3. Crea nodo File (con categoria)
                import json as _json
                node_meta = {
                    "extension": meta.extension, "size_bytes": meta.size_bytes,
                    "mime_type": meta.mime_type, "category": category,
                    "created": meta.to_dict()["created"], "modified": meta.to_dict()["modified"],
                }
                node_id = store.create_node(
                    node_type="File",
                    label=meta.file_name,
                    metadata=node_meta,
                )

                # 4. Registra file
                store.register_file(
                    node_id=node_id, device=self.device_name, path=str(fpath.absolute()),
                    size_bytes=meta.size_bytes, sha256=meta.sha256, mime_type=meta.mime_type,
                )

                # 5. Edge CONTAINS verso Directory genitore
                if parent_id:
                    store.create_edge(source_id=parent_id, target_id=node_id, relation="CONTAINS", weight=1.0)

                # 6. Accoda per analisi
                store.enqueue(node_id, priority=1)

                with self._lock:
                    self.stats["files_indexed"] += 1
                logger.info("WD INDEXED: %s → %s", file_path, node_id[:12])

        except Exception as e:
            logger.error("WD ERROR %s: %s", file_path, e)
            with self._lock:
                self.stats["files_error"] += 1

    def stop(self):
        with self._lock:
            self._pending.clear()
        if self._timer and self._timer.is_alive():
            self._timer.cancel()


class WatchdogManager:
    def __init__(self, path: str | Path, project_label: Optional[str] = None,
                 device_name: str = "watchdog", hsd_filter=None, quarantine=None):
        self.path = Path(path).absolute()
        self.project_label = project_label or self.path.name
        self.device_name = device_name
        from egida.filters import HSDFilter as _HSDFilter
        from egida.quarantine import Quarantine as _Quarantine
        from egida.config import EGIDA_QUARANTINE_DIR
        self.hsd_filter = hsd_filter or _HSDFilter()
        self.quarantine = quarantine or _Quarantine(EGIDA_QUARANTINE_DIR)
        self._handler = FileCreationHandler(
            device_name=self.device_name, hsd_filter=self.hsd_filter,
            quarantine=self.quarantine, project_label=self.project_label,
        )
        self._observer = watchdog.observers.Observer()
        self._running = False

    def start(self):
        if self._running:
            logger.warning("Watchdog già avviato su %s", self.path)
            return
        if not self.path.is_dir():
            raise NotADirectoryError(f"Directory non trovata: {self.path}")
        self._handler.stats["started_at"] = _dt.now().isoformat()
        self._observer.schedule(self._handler, str(self.path), recursive=True)
        self._observer.start()
        self._running = True
        logger.info("🔍 Watchdog AVVIATO su: %s (device=%s, project=%s)",
                     self.path, self.device_name, self.project_label)

    def stop(self):
        if not self._running:
            return
        self._observer.stop()
        self._observer.join(timeout=5)
        self._handler.stop()
        self._running = False
        logger.info("⏹ Watchdog FERMATO su: %s", self.path)

    @property
    def stats(self) -> dict:
        return dict(self._handler.stats)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


# ─── Filtri per esclusione ──────────────────────────────────────────

_SKIP_PATTERNS = [
    "__pycache__", ".git", ".svn", ".hg", ".idea", ".vscode",
    "node_modules", ".DS_Store", "Thumbs.db", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "__MACOSX",
]

_SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".o", ".obj", ".class",
    ".log", ".tmp", ".temp",
}


def _should_skip(path: Path) -> bool:
    for part in path.parts:
        if part.startswith(".") and part != ".":
            return True
    for p in _SKIP_PATTERNS:
        if p in path.parts:
            return True
    if path.suffix.lower() in _SKIP_EXTENSIONS:
        return True
    return False
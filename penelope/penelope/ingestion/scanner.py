"""
Scanner del file system.

Due modalità:
1. One-shot: cammina ricorsivamente una directory, crea nodi File + Project
2. Watchdog (da attivare): demone leggero che reagisce a nuovi file in tempo reale

Il flusso: file → Egida (HSD check) → metadata → MariaDB → coda lazy
"""

import logging
from pathlib import Path
from typing import Callable, Optional

import watchdog.events
import watchdog.observers

from penelope.config import settings
from penelope.db.mariadb_store import MariaDBStore
from egida.filters import HSDFilter, HSDMatch
from egida.quarantine import Quarantine
from penelope.ingestion.metadata import FileMetadata

logger = logging.getLogger(__name__)


class ScanResult:
    """Risultato della scansione di un file."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.node_id: Optional[str] = None
        self.skipped: bool = False
        self.skip_reason: Optional[str] = None
        self.hsd_match: Optional[HSDMatch] = None
        self.error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.node_id is not None and not self.skipped

    def __repr__(self) -> str:
        return f"<ScanResult {self.file_path} success={self.success} skipped={self.skipped}>"


class FileScanner:
    """
    Scanner che processa file e directory creando i corrispondenti
    nodi nel grafo (MariaDB).
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

        # Callback opzionale: chiamato dopo ogni file processato
        self.on_file_processed: Optional[Callable[[ScanResult], None]] = None

    # ─── Scan singolo file ──────────────────────────────────────

    def scan_file(self, file_path: str | Path, project_id: Optional[str] = None) -> ScanResult:
        """
        Processa un singolo file:
        1. Controllo HSD (Egida)
        2. Se HSD → quarantena, skip
        3. Estrazione metadati
        4. Creazione nodo File in MariaDB
        5. Edge MEMBER_OF verso il progetto
        6. Accodamento per elaborazione futura

        Args:
            file_path: percorso del file
            project_id: UUID del progetto a cui appartiene (se noto)

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
            meta_dict = meta.to_dict()

            # 2b. Dedup: salta se stesso SHA-256 già presente
            existing = self.db.get_file_by_sha256(meta.sha256)
            if existing:
                logger.debug("FILE DUPLICATO (SHA-256): %s -> %s", path, existing['node_id'])
                # Registra comunque la nuova posizione fisica (device diverso / mount diverso)
                self.db.register_file_location(
                    node_id=existing['node_id'],
                    device=self.device_name,
                    path=str(path.absolute()),
                    size_bytes=meta.size_bytes,
                    sha256=meta.sha256,
                    mime_type=meta.mime_type,
                )
                result.node_id = existing['node_id']
                result.skipped = True
                result.skip_reason = "DUPLICATE"
                self._notify(result)
                return result

            # 3. Crea nodo File in MariaDB
            node_id = self.db.create_node(
                node_type="File",
                label=meta.file_name,
                metadata={
                    "extension": meta.extension,
                    "size_bytes": meta.size_bytes,
                    "mime_type": meta.mime_type,
                    "created": meta_dict["created"],
                    "modified": meta_dict["modified"],
                },
            )

            # 4. Registra nel file_registry
            self.db.register_file(
                node_id=node_id,
                device=self.device_name,
                path=str(path.absolute()),
                size_bytes=meta.size_bytes,
                sha256=meta.sha256,
                mime_type=meta.mime_type,
            )

            # 5. Edge verso progetto (se fornito)
            if project_id:
                self.db.create_edge(
                    source_id=node_id,
                    target_id=project_id,
                    relation="MEMBER_OF",
                    weight=1.0,
                )

            # 6. Accoda per elaborazione futura
            self.db.enqueue(node_id, priority=0)

            result.node_id = node_id
            logger.debug("FILE INDEXED: %s → %s", path, node_id)

        except Exception as e:
            logger.error("ERRORE scansione %s: %s", file_path, e)
            result.error = str(e)

        self._notify(result)
        return result

    # ─── Scan ricorsivo directory ────────────────────────────────

    def scan_directory(
        self,
        root_path: str | Path,
        project_label: Optional[str] = None,
        recursive: bool = True,
    ) -> list[ScanResult]:
        """
        Scansiona ricorsivamente una directory.

        Crea un nodo Project per la directory radice, poi scansiona
        ogni file al suo interno.

        Args:
            root_path: directory da scandire
            project_label: nome del progetto (default: nome cartella)
            recursive: se True, entra nelle sottodirectory come sotto-progetti

        Returns:
            Lista di ScanResult per ogni file processato
        """
        root = Path(root_path)
        if not root.is_dir():
            raise NotADirectoryError(f"Non è una directory: {root}")

        project_label = project_label or root.name

        # Cerca progetto esistente con stessa label
        existing = self.db._query(
            "SELECT id FROM nodes WHERE type = 'Project' AND label = %s LIMIT 1",
            (project_label,),
        )
        if existing:
            project_id = existing[0]['id']
            logger.info("Progetto esistente riutilizzato: %s (%s)", project_label, project_id)
        else:
            # Crea nuovo nodo Project
            project_id = self.db.create_node(
                node_type="Project",
                label=project_label,
                metadata={"path": str(root.absolute()), "device": self.device_name},
            )
            logger.info("Progetto creato: %s (%s)", project_label, project_id)

        results: list[ScanResult] = []
        walk = root.rglob("*") if recursive else root.glob("*")

        for entry in walk:
            if entry.is_file():
                # Salta file nascosti/non desiderati
                if _should_skip(entry):
                    logger.debug("SKIP (escluso): %s", entry)
                    continue

                r = self.scan_file(entry, project_id=project_id)
                results.append(r)

        return results

    # ─── Watchdog reale (inotify/FSEvents/ReadDirectoryChangesW) ─

    _instance = None  # singleton

    @classmethod
    def _get_instance(cls, device_name: str = "watchdog"):
        if cls._instance is None:
            cls._instance = FileScanner(device_name=device_name)
        return cls._instance

    def start_watchdog(self, path: str | Path, project_label: Optional[str] = None) -> "WatchdogManager":
        """
        Avvia un watchdog reale su una directory.

        Crea un WatchdogManager che osserva il path e processa
        automaticamente i nuovi file. Il manager può essere fermato
        con .stop().

        Args:
            path: Directory da osservare.
            project_label: Nome progetto (default: nome cartella).

        Returns:
            WatchdogManager avviato.
        """
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


# ─── WatchdogManager — demone di osservazione file ────────────────

import threading as _threading
import time as _time
from datetime import datetime as _dt

# Cache delle directory osservate → project_id
_WATCHED_PROJECTS: dict[str, str] = {}


class FileCreationHandler(watchdog.events.FileSystemEventHandler):
    """Handler che processa nuovi file creati/modificati.

    Usa un debounce: dopo un evento on_created o on_modified,
    aspetta 2 secondi prima di processare il file (per evitare
    di processare file mentre vengono ancora copiati).
    """

    def __init__(
        self,
        device_name: str,
        hsd_filter,
        quarantine,
        project_label: str,
    ):
        super().__init__()
        self.device_name = device_name
        self.hsd_filter = hsd_filter
        self.quarantine = quarantine
        self.project_label = project_label

        # Debounce: path → timestamp ultimo evento
        self._pending: dict[str, float] = {}
        self._lock = _threading.Lock()
        self._timer: Optional[_threading.Timer] = None

        # Statistiche
        self.stats = {
            "files_seen": 0,
            "files_indexed": 0,
            "files_hsd": 0,
            "files_skipped": 0,
            "files_error": 0,
            "started_at": None,
        }

        # Crea MariaDB e project_id una tantum
        import copy
        self._db = MariaDBStore()
        self._project_id = self._ensure_project()

    def _ensure_project(self) -> Optional[str]:
        """Trova o crea il nodo Project per questa directory watch."""
        with self._db as store:
            existing = store._query(
                "SELECT id FROM nodes WHERE type = 'Project' AND label = %s LIMIT 1",
                (self.project_label,),
            )
            if existing:
                return existing[0]["id"]
            else:
                return store.create_node(
                    node_type="Project",
                    label=self.project_label,
                    metadata={"source": "watchdog", "device": self.device_name},
                )

    def on_created(self, event):
        if event.is_directory:
            return
        self._debounce(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._debounce(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        # La destinazione di un movimento è come un nuovo file
        self._debounce(event.dest_path)

    def _debounce(self, path: str):
        """Accoda un file con debounce di 2 secondi."""
        now = _time.time()
        with self._lock:
            self._pending[path] = now
            self.stats["files_seen"] += 1

        # Avvia timer per processare i pending
        if self._timer is None or not self._timer.is_alive():
            self._timer = _threading.Timer(2.0, self._flush_pending)
            self._timer.daemon = True
            self._timer.start()

    def _flush_pending(self):
        """Processa tutti i file in attesa (con debounce scaduto)."""
        now = _time.time()
        with self._lock:
            # Raccoglie file stabili (ultimo evento > 2 secondi fa)
            ready = [
                path for path, ts in self._pending.items()
                if now - ts >= 2.0
            ]
            for path in ready:
                del self._pending[path]

        for file_path in ready:
            self._process_file(file_path)

        # Se ci sono ancora pending, riprogramma
        with self._lock:
            if self._pending:
                self._timer = _threading.Timer(2.0, self._flush_pending)
                self._timer.daemon = True
                self._timer.start()

    def _process_file(self, file_path: str):
        """Processa un singolo file: Egida → MariaDB → coda."""
        fpath = Path(file_path)

        # Skip file indesiderati
        if _should_skip(fpath):
            logger.debug("WD SKIP (escluso): %s", file_path)
            with self._lock:
                self.stats["files_skipped"] += 1
            return

        logger.debug("WD NEW: %s", file_path)

        try:
            # Usa MariaDBStore esistente o creane uno nuovo
            with self._db as store:
                # 1. Egida: filtro HSD
                hsd = self.hsd_filter.check_file(fpath)
                if hsd.is_infected:
                    self.quarantine.isolate(hsd, fpath)
                    with self._lock:
                        self.stats["files_hsd"] += 1
                    logger.info("WD HSD: %s (%d match)", file_path, len(hsd.matches))
                    return

                # 2. Metadati
                from penelope.ingestion.metadata import FileMetadata
                meta = FileMetadata(fpath)

                # 3. Dedup
                existing = store.get_file_by_sha256(meta.sha256)
                if existing:
                    # Non crea nodo/edge/enqueue (sha256 identico = stesso contenuto),
                    # MA registra la nuova posizione fisica se diversa da quella nota
                    store.register_file_location(
                        node_id=existing['node_id'],
                        device=self.device_name,
                        path=str(fpath.absolute()),
                        size_bytes=meta.size_bytes,
                        sha256=meta.sha256,
                        mime_type=meta.mime_type,
                    )
                    with self._lock:
                        self.stats['files_skipped'] += 1
                    logger.debug("WD DUPLICATE (SHA-256): %s -> registrata nuova posizione", file_path)
                    return

                # 4. Crea nodo File
                node_id = store.create_node(
                    node_type="File",
                    label=meta.file_name,
                    metadata={
                        "extension": meta.extension,
                        "size_bytes": meta.size_bytes,
                        "mime_type": meta.mime_type,
                        "created": meta.to_dict()["created"],
                        "modified": meta.to_dict()["modified"],
                    },
                )

                # 5. Registra nel file_registry
                store.register_file(
                    node_id=node_id,
                    device=self.device_name,
                    path=str(fpath.absolute()),
                    size_bytes=meta.size_bytes,
                    sha256=meta.sha256,
                    mime_type=meta.mime_type,
                )

                # 6. Edge verso progetto
                if self._project_id:
                    store.create_edge(
                        source_id=node_id,
                        target_id=self._project_id,
                        relation="MEMBER_OF",
                        weight=1.0,
                    )

                # 7. Accoda per elaborazione futura
                store.enqueue(node_id, priority=1)  # priorità leggermente + alta

                with self._lock:
                    self.stats["files_indexed"] += 1
                logger.info("WD INDEXED: %s → %s", file_path, node_id[:12])

        except Exception as e:
            logger.error("WD ERROR %s: %s", file_path, e)
            with self._lock:
                self.stats["files_error"] += 1

    def stop(self):
        """Ferma l'handler e processa eventuali file rimanenti."""
        with self._lock:
            self._pending.clear()
        if self._timer and self._timer.is_alive():
            self._timer.cancel()


class WatchdogManager:
    """Gestore del watchdog per una directory.

    Crea un Observer di watchdog e lo avvia in un thread.
    Si può fermare con .stop() o usare come context manager.
    """

    def __init__(
        self,
        path: str | Path,
        project_label: Optional[str] = None,
        device_name: str = "watchdog",
        hsd_filter=None,
        quarantine=None,
    ):
        self.path = Path(path).absolute()
        self.project_label = project_label or self.path.name
        self.device_name = device_name

        from egida.filters import HSDFilter as _HSDFilter
        from egida.quarantine import Quarantine as _Quarantine
        from egida.config import EGIDA_QUARANTINE_DIR

        self.hsd_filter = hsd_filter or _HSDFilter()
        self.quarantine = quarantine or _Quarantine(EGIDA_QUARANTINE_DIR)

        self._handler = FileCreationHandler(
            device_name=self.device_name,
            hsd_filter=self.hsd_filter,
            quarantine=self.quarantine,
            project_label=self.project_label,
        )
        self._observer = watchdog.observers.Observer()
        self._running = False

    def start(self):
        """Avvia l'osservazione della directory."""
        if self._running:
            logger.warning("Watchdog già avviato su %s", self.path)
            return

        if not self.path.is_dir():
            logger.error("Watchdog: directory non trovata %s", self.path)
            raise NotADirectoryError(f"Directory non trovata: {self.path}")

        self._handler.stats["started_at"] = _dt.now().isoformat()

        # Ricorsivo = True (osserva tutte le sottodirectory)
        self._observer.schedule(self._handler, str(self.path), recursive=True)
        self._observer.start()
        self._running = True

        logger.info("🔍 Watchdog AVVIATO su: %s (device=%s, project=%s)",
                     self.path, self.device_name, self.project_label)

    def stop(self):
        """Ferma l'osservazione."""
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
    "__pycache__",
    ".git",
    ".svn",
    ".hg",
    ".idea",
    ".vscode",
    "node_modules",
    ".DS_Store",
    "Thumbs.db",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "__MACOSX",
]

_SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd",
    ".o", ".obj", ".class",
    ".log", ".tmp", ".temp",
}


def _should_skip(path: Path) -> bool:
    """Determina se un file/cartella va saltato durante la scansione."""
    # Nascosto (Unix) — controlla anche le directory nella gerarchia
    for part in path.parts:
        if part.startswith(".") and part != ".":
            return True
    # Pattern noti
    for p in _SKIP_PATTERNS:
        if p in path.parts:
            return True
    # Estensioni non interessanti
    if path.suffix.lower() in _SKIP_EXTENSIONS:
        return True
    return False

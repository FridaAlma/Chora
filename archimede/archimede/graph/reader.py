"""Reader read-only del grafo Penelope (MariaDB).

ARCHIMEDE NON SCRIVE MAI — solo query SELECT.
Usa il MariaDBStore di Penelope per la connessione (password via keyring).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PenelopeGraphReader:
    """Wrapper read-only attorno a MariaDBStore di Penelope.

    Garantisce che vengano eseguite solo query SELECT.
    Ottiene le credenziali attraverso il keyring di sistema (come fa Penelope).
    Tutte le path vengono risolte tramite mount_root del device.
    """

    def __init__(self) -> None:
        self._store: Any = None
        self._init_store()

    @staticmethod
    def _resolve_file_path(path: str, mount_root: Optional[str]) -> str:
        """Risolve path assoluto del file.

        Se mount_root noto e path non assoluto, combina mount_root + path.
        Altrimenti restituisce path inalterato (backward compat).
        """
        if not mount_root:
            return path
        if path.startswith("/") or path.startswith("\\") or (len(path) > 1 and path[1] == ":"):
            return path  # gia\' assoluto
        return str(Path(mount_root) / path)

    def _init_store(self) -> None:
        """Inizializza il MariaDBStore di Penelope."""
        try:
            penelope_dir = self._find_penelope_dir()
            if penelope_dir:
                # Carica il .env di Penelope PRIMA di importare i suoi moduli
                env_path = penelope_dir / ".env"
                if env_path.exists():
                    from dotenv import load_dotenv
                    load_dotenv(dotenv_path=env_path, override=True)
                    logger.info("Caricato .env di Penelope: %s", env_path)

                # Aggiunge Penelope al path
                if str(penelope_dir) not in os.sys.path:
                    os.sys.path.insert(0, str(penelope_dir))

            from penelope.db.mariadb_store import MariaDBStore

            self._store = MariaDBStore()
            # Test connessione
            self._store.connect()
            logger.info("Connesso al grafo Penelope (MariaDB)")
        except Exception as e:
            logger.warning("Impossibile connettersi a Penelope: %s", e)
            self._store = None

    def _find_penelope_dir(self) -> Optional[Path]:
        """Cerca la directory di Penelope.

        Ordine:
        1. Variabile d'ambiente ARCHIMEDE_PENELOPE_PATH
        2. ../penelope (sibling, RUI Edition default)
        3. ./penelope
        4. ../Penelope (retrocompatibile)
        """
        # 1. Variabile d'ambiente
        env_path = os.getenv("ARCHIMEDE_PENELOPE_PATH")
        if env_path:
            p = Path(env_path)
            if p.exists() and (p / "penelope" / "db" / "mariadb_store.py").exists():
                return p

        # 2. Cerca automaticamente (case-insensitive su Windows)
        candidates = [
            Path(__file__).resolve().parent.parent.parent.parent / "penelope",
            Path.cwd() / "penelope",
            Path.cwd().parent / "penelope",
            Path(__file__).resolve().parent.parent.parent.parent / "Penelope",
            Path.cwd() / "Penelope",
            Path.cwd().parent / "Penelope",
        ]
        for p in candidates:
            if (p / "penelope" / "db" / "mariadb_store.py").exists():
                return p
        return None

    @property
    def connected(self) -> bool:
        return self._store is not None

    def close(self) -> None:
        if self._store:
            try:
                self._store.close()
            except Exception:
                pass

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Esegue una query SELECT read-only.

        Solleva RuntimeError se la query non è una SELECT.
        """
        stripped = sql.strip().upper()
        if not stripped.startswith("SELECT") and not stripped.startswith("WITH"):
            raise RuntimeError(
                f"Archimede: operazione non consentita: {sql[:60]}..."
            )
        if self._store is None:
            raise RuntimeError("PenelopeGraphReader non connesso")
        return self._store._query(sql, params)

    def __enter__(self) -> "PenelopeGraphReader":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ─── Query pubbliche ─────────────────────────────────────────

    def count_photos(self) -> int:
        """Numero totale di foto indicizzate."""
        rows = self._query(
            "SELECT COUNT(*) as cnt FROM file_registry "
            "WHERE path LIKE %s OR path LIKE %s OR path LIKE %s",
            ("%.jpg", "%.jpeg", "%.png"),
        )
        return rows[0]["cnt"] if rows else 0

    def get_all_photos(
        self,
        extensions: tuple[str, ...] = ("%.jpg", "%.jpeg", "%.png", "%.webp"),
        limit: int = 0,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Restituisce tutte le foto indicizzate con metadati.

        Il path e' risolto tramite mount_root del device (se disponibile).
        """
        like_clauses = " OR f.path LIKE ".join(["%s"] * len(extensions))
        sql = f"""
            SELECT n.id as node_id, n.label, n.metadata as node_metadata,
                   f.path, f.device, f.size_bytes, f.sha256, f.mime_type,
                   d.mount_root, d.label AS device_label
            FROM nodes n
            JOIN file_registry f ON f.node_id = n.id
            LEFT JOIN devices d ON d.id = f.device_id
            WHERE n.type = 'File'
              AND (f.path LIKE {like_clauses})
            ORDER BY f.path
        """
        params = list(extensions)
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
            if offset:
                sql += " OFFSET %s"
                params.append(offset)
        rows = self._query(sql, tuple(params))
        # Risolve path assoluti
        for row in rows:
            row["path"] = self._resolve_file_path(row.get("path", ""), row.get("mount_root"))
        return rows

    def get_photos_in_directory(self, directory: str) -> list[dict[str, Any]]:
        """Foto in una directory specifica.

        Il path e' risolto tramite mount_root del device (se disponibile).
        """
        pattern = f"%{directory}%"
        rows = self._query(
            """SELECT n.id as node_id, n.label, n.metadata as node_metadata,
                      f.path, f.device, f.size_bytes, f.sha256, f.mime_type,
                      d.mount_root, d.label AS device_label
               FROM nodes n
               JOIN file_registry f ON f.node_id = n.id
               LEFT JOIN devices d ON d.id = f.device_id
               WHERE n.type = 'File'
                 AND f.path LIKE %s
                 AND (f.path LIKE %s OR f.path LIKE %s OR f.path LIKE %s)
               ORDER BY f.path""",
            (pattern, "%.jpg", "%.jpeg", "%.png"),
        )
        for row in rows:
            row["path"] = self._resolve_file_path(row.get("path", ""), row.get("mount_root"))
        return rows

    def get_photos_with_face_count(self) -> list[dict[str, Any]]:
        """Foto che hanno metadati face_count.

        Il path e' risolto tramite mount_root del device (se disponibile).
        """
        rows = self._query(
            """SELECT n.id as node_id, n.label, n.metadata as node_metadata,
                      f.path, f.device, f.size_bytes, f.sha256, f.mime_type,
                      d.mount_root, d.label AS device_label
               FROM nodes n
               JOIN file_registry f ON f.node_id = n.id
               LEFT JOIN devices d ON d.id = f.device_id
               WHERE n.type = 'File'
                 AND n.metadata LIKE %s
                 AND (f.path LIKE %s OR f.path LIKE %s OR f.path LIKE %s)
               ORDER BY f.path""",
            ("%face_count%", "%.jpg", "%.jpeg", "%.png"),
        )
        for row in rows:
            row["path"] = self._resolve_file_path(row.get("path", ""), row.get("mount_root"))
        return rows

    def get_person_nodes(self, source: str = "") -> list[dict[str, Any]]:
        """Nodi Person, opzionalmente filtrati per source."""
        if source:
            return self._query(
                "SELECT id, label, metadata FROM nodes WHERE type = 'Person' AND metadata LIKE %s",
                (f"%{source}%",),
            )
        return self._query(
            "SELECT id, label, metadata FROM nodes WHERE type = 'Person'"
        )

    def get_edges_for_photo(self, photo_node_id: str) -> list[dict[str, Any]]:
        """Archi che connettono una foto ad altre entità."""
        return self._query(
            "SELECT * FROM edges WHERE source_id = %s OR target_id = %s",
            (photo_node_id, photo_node_id),
        )

    def get_persons_in_photo(self, photo_node_id: str) -> list[dict[str, Any]]:
        """Nodi Person collegati a una foto via edge."""
        return self._query(
            """SELECT p.id, p.label, p.metadata
               FROM nodes p
               JOIN edges e ON (e.source_id = p.id OR e.target_id = p.id)
               WHERE (e.source_id = %s OR e.target_id = %s)
                 AND p.type = 'Person'
                 AND e.relation IN ('CONTAINS', 'APPEARS_IN', 'MENTIONS')""",
            (photo_node_id, photo_node_id),
        )

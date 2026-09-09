"""
Layer di accesso a MariaDB su Proxmox.
CRUD base per nodi, archi, file_registry e coda di ingestion.
"""

import json
import logging
import socket
import os
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import pymysql
import pymysql.cursors
from penelope.config import settings

logger = logging.getLogger(__name__)


class MariaDBStore:
    """Connessione thread-safe a MariaDB con context manager."""

    def __init__(self):
        self._conn: Optional[pymysql.Connection] = None

    # ─── Connessione ─────────────────────────────────────────────

    def connect(self) -> pymysql.Connection:
        if self._conn is None or not self._conn.open:
            self._conn = pymysql.connect(
                host=settings.MARIADB_HOST,
                port=settings.MARIADB_PORT,
                user=settings.MARIADB_USER,
                password=settings.get_db_password(),
                database=settings.MARIADB_DATABASE,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
            )
        return self._conn

    def close(self) -> None:
        if self._conn and self._conn.open:
            self._conn.close()
        self._conn = None

    def __enter__(self) -> "MariaDBStore":
        self.connect()
        return self

    def __exit__(self, *args) -> None:
        if self._conn and self._conn.open:
            self._conn.commit()
        self.close()

    # ─── NODI ────────────────────────────────────────────────────

    def create_node(
        self,
        node_type: str,
        label: Optional[str] = None,
        metadata: Optional[dict] = None,
        node_id: Optional[str] = None,
    ) -> str:
        """Crea un nodo e restituisce il suo ID."""
        node_id = node_id or str(uuid4())
        sql = """INSERT INTO nodes (id, type, label, metadata)
                 VALUES (%s, %s, %s, %s)"""
        self._execute(sql, (node_id, node_type, label, json.dumps(metadata) if metadata else None))
        return node_id

    def get_node(self, node_id: str) -> Optional[dict]:
        """Restituisce un nodo per ID."""
        sql = "SELECT * FROM nodes WHERE id = %s"
        rows = self._query(sql, (node_id,))
        return rows[0] if rows else None

    def get_nodes_by_type(self, node_type: str) -> list[dict]:
        """Restituisce tutti i nodi di un tipo."""
        sql = "SELECT * FROM nodes WHERE type = %s ORDER BY label"
        return self._query(sql, (node_type,))

    def update_node(self, node_id: str, **fields) -> bool:
        """Aggiorna campi di un nodo. Usa solo i campi passati come kwargs."""
        if not fields:
            return False
        sets = ", ".join(f"{k} = %s" for k in fields)
        vals = list(fields.values()) + [node_id]
        sql = f"UPDATE nodes SET {sets} WHERE id = %s"
        return self._execute(sql, tuple(vals)) > 0

    def delete_node(self, node_id: str) -> bool:
        """Cancella un nodo (cascade elimina anche archi e registry)."""
        sql = "DELETE FROM nodes WHERE id = %s"
        return self._execute(sql, (node_id,)) > 0

    # ─── ARCHI ───────────────────────────────────────────────────

    def create_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        weight: float = 1.0,
        metadata: Optional[dict] = None,
    ) -> int:
        """Crea un arco e restituisce il suo ID."""
        sql = """INSERT INTO edges (source_id, target_id, relation, weight, metadata)
                 VALUES (%s, %s, %s, %s, %s)"""
        self._execute(
            sql,
            (source_id, target_id, relation, weight, json.dumps(metadata) if metadata else None),
        )
        return int(self._conn.insert_id())

    def get_edges(self, node_id: Optional[str] = None, relation: Optional[str] = None) -> list[dict]:
        """Restituisce archi, opzionalmente filtrati per nodo e/o relazione."""
        conditions = []
        params = []
        if node_id:
            conditions.append("(source_id = %s OR target_id = %s)")
            params.extend([node_id, node_id])
        if relation:
            conditions.append("relation = %s")
            params.append(relation)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT * FROM edges {where} ORDER BY created_at"
        return self._query(sql, tuple(params))

    def delete_edge(self, edge_id: int) -> bool:
        sql = "DELETE FROM edges WHERE id = %s"
        return self._execute(sql, (edge_id,)) > 0

    # ─── DEVICE MOUNTS (per-host) ────────────────────────────────

    @staticmethod
    def current_hostname() -> str:
        """Hostname corrente, con override da env PENELOPE_HOSTNAME."""
        return os.getenv("PENELOPE_HOSTNAME") or socket.gethostname()

    def get_mount_root_for_host(self, device_id: int, hostname: Optional[str] = None) -> Optional[str]:
        """Restituisce mount_root per un device su un host specifico.

        Args:
            device_id: ID del device.
            hostname: Nome host (default: host corrente).

        Returns:
            mount_root se trovato in device_mounts, altrimenti None.
        """
        host = hostname or self.current_hostname()
        rows = self._query(
            "SELECT mount_root FROM device_mounts "
            "WHERE device_id = %s AND hostname = %s LIMIT 1",
            (device_id, host),
        )
        return rows[0]["mount_root"] if rows else None

    def upsert_device_mount(self, device_id: int, hostname: str, mount_root: str) -> int:
        """Inserisce o aggiorna mount per un device su un host.

        Args:
            device_id: ID del device.
            hostname: Nome host (es. 'macbook', 'headless').
            mount_root: Path del mount (es. '/Volumes/HDD').

        Returns:
            ID della riga.
        """
        self._execute(
            "INSERT INTO device_mounts (device_id, hostname, mount_root, last_seen_at) "
            "VALUES (%s, %s, %s, NOW()) "
            "ON DUPLICATE KEY UPDATE mount_root = VALUES(mount_root), last_seen_at = NOW()",
            (device_id, hostname, mount_root),
        )
        rows = self._query(
            "SELECT id FROM device_mounts WHERE device_id = %s AND hostname = %s LIMIT 1",
            (device_id, hostname),
        )
        return rows[0]["id"] if rows else 0

    def list_device_mounts(self, device_id: Optional[int] = None) -> list[dict]:
        """Lista montaggi per device (con hostname e mount_root).

        Args:
            device_id: Se fornito, filtra per device.

        Returns:
            Lista di dict: device_id, device_label, hostname, mount_root, last_seen_at.
        """
        sql = """SELECT dm.device_id, d.label AS device_label, dm.hostname, dm.mount_root, dm.last_seen_at
                 FROM device_mounts dm
                 JOIN devices d ON d.id = dm.device_id"""
        params: tuple = ()
        if device_id is not None:
            sql += " WHERE dm.device_id = %s"
            params = (device_id,)
        sql += " ORDER BY d.label, dm.hostname"
        return self._query(sql, params)

    def resolve_mount_root(self, device_id: int) -> Optional[str]:
        """Risolve mount_root per il device, preferendo device_mounts per host corrente.

        Fallback a devices.mount_root (deprecato) se non trovato per host.

        Args:
            device_id: ID del device.

        Returns:
            mount_root risolto o None.
        """
        # 1. device_mounts per host corrente
        mount = self.get_mount_root_for_host(device_id)
        if mount:
            return mount
        # 2. Fallback a devices.mount_root (retrocompat)
        dev = self.get_device(device_id)
        return dev.get("mount_root") if dev else None

    # ─── DEVICES ────────────────────────────────────────────────

    def create_device(
        self,
        label: str,
        device_type: str = "local",
        mount_root: Optional[str] = None,
        volume_uuid: Optional[str] = None,
    ) -> int:
        """Crea un device e restituisce il suo ID.

        Args:
            label: Nome del device (unique).
            device_type: 'local', 'external', 'network', 'server'.
            mount_root: Punto di mount corrente (può cambiare).
            volume_uuid: UUID del volume (per mount resilient).

        Returns:
            ID del device (int).
        """
        sql = """INSERT INTO devices (label, type, mount_root, volume_uuid, last_seen_at)
                 VALUES (%s, %s, %s, %s, NOW())
                 ON DUPLICATE KEY UPDATE last_seen_at = NOW(), mount_root = COALESCE(%s, mount_root)"""
        self._execute(sql, (label, device_type, mount_root, volume_uuid, mount_root))
        # Recupera ID (potrebbe essere un update, non insert)
        rows = self._query("SELECT id FROM devices WHERE label = %s LIMIT 1", (label,))
        return rows[0]["id"] if rows else 0

    def get_device(self, device_id: int) -> Optional[dict]:
        """Restituisce un device per ID."""
        sql = "SELECT * FROM devices WHERE id = %s"
        rows = self._query(sql, (device_id,))
        return rows[0] if rows else None

    def get_device_by_label(self, label: str) -> Optional[dict]:
        """Restituisce un device per label."""
        sql = "SELECT * FROM devices WHERE label = %s LIMIT 1"
        rows = self._query(sql, (label,))
        return rows[0] if rows else None

    def ensure_device(
        self,
        label: str,
        device_type: str = "local",
        mount_root: Optional[str] = None,
    ) -> int:
        """Trova o crea un device, restituisce ID."""
        existing = self.get_device_by_label(label)
        if existing:
            if mount_root and existing.get("mount_root") != mount_root:
                self._execute(
                    "UPDATE devices SET mount_root = %s, last_seen_at = NOW() WHERE id = %s",
                    (mount_root, existing["id"]),
                )
            else:
                self._execute(
                    "UPDATE devices SET last_seen_at = NOW() WHERE id = %s",
                    (existing["id"],),
                )
            return existing["id"]
        return self.create_device(label=label, device_type=device_type, mount_root=mount_root)

    def update_device_mount_root(self, device_id: int, mount_root: str) -> bool:
        """Aggiorna mount_root di un device."""
        return self._execute(
            "UPDATE devices SET mount_root = %s, last_seen_at = NOW() WHERE id = %s",
            (mount_root, device_id),
        ) > 0

    def list_devices(self) -> list[dict]:
        """Lista di tutti i device."""
        return self._query("SELECT * FROM devices ORDER BY label")

    def get_device_stats(self) -> list[dict]:
        """Statistiche per device: label, online, offline, totale righe."""
        return self._query(
            """SELECT d.id, d.label, d.mount_root, d.type, d.last_seen_at,
                      COUNT(f.id) AS total_rows,
                      SUM(f.status = 'online') AS online_count,
                      SUM(f.status = 'offline') AS offline_count
               FROM devices d
               LEFT JOIN file_registry f ON f.device_id = d.id
               GROUP BY d.id, d.label, d.mount_root, d.type, d.last_seen_at
               ORDER BY d.label"""
        )

    # ─── FILE REGISTRY (multi-location) ─────────────────────────-

    @staticmethod
    def _compute_rel_path(abs_path: str, mount_root: Optional[str]) -> str:
        """Converte path assoluto in relativo rispetto a mount_root.

        Se mount_root è None, restituisce il path assoluto inalterato
        (backward compatibilità).
        """
        if not mount_root:
            return abs_path
        # Assicura che mount_root finisca con /
        root = mount_root.rstrip("/") + "/"
        if abs_path.startswith(root):
            return abs_path[len(root):]
        # Se il path non inizia con mount_root, restituiscilo inalterato
        # (potrebbe essere su mount diverso o già relativo)
        return abs_path

    def register_file(
        self,
        node_id: str,
        device: str,
        path: str,
        size_bytes: Optional[int] = None,
        sha256: Optional[str] = None,
        mime_type: Optional[str] = None,
        device_id: Optional[int] = None,
    ) -> int:
        """Registra una posizione fisica per un nodo File.

        Se device_id non fornito, lo risolve dal label device.
        Se il device ha mount_root, il path viene convertito in
        path relativo prima dello storage.

        Args:
            node_id: UUID del nodo File.
            device: Label del device (es. 'laptop-main', 'hdd-ext').
            path: Path assoluto o relativo (convertito se mount_root noto).
            size_bytes: Dimensione file.
            sha256: Hash SHA-256.
            mime_type: MIME type.
            device_id: ID del device (opzionale, risolto da label se omesso).

        Returns:
            ID della riga file_registry creata.
        """
        # Risolve device_id se non fornito
        if device_id is None:
            dev = self.get_device_by_label(device)
            if dev:
                device_id = dev["id"]
            else:
                # Crea device con mount_root sconosciuto (sarà configurato dopo)
                device_id = self.ensure_device(label=device)

        # Recupera mount_root per il device (per host corrente via device_mounts)
        mount_root = self.resolve_mount_root(device_id)

        # Converte path in relativo se mount_root noto
        stored_path = self._compute_rel_path(path, mount_root)

        sql = """INSERT INTO file_registry (node_id, device, device_id, path, size_bytes, sha256, mime_type)
                 VALUES (%s, %s, %s, %s, %s, %s, %s)"""
        self._execute(sql, (node_id, device, device_id, stored_path, size_bytes, sha256, mime_type))
        return int(self._conn.insert_id())

    def register_file_location(
        self,
        node_id: str,
        device: str,
        path: str,
        size_bytes: Optional[int] = None,
        sha256: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> int:
        """Aggiunge UNA NUOVA posizione fisica per un nodo già esistente.

        Se (device, path relativo) è già registrato per questo node_id,
        non fa nulla (idempotente). Altrimenti inserisce una nuova riga.

        Returns:
            ID della riga (esistente o nuova).
        """
        dev = self.get_device_by_label(device)
        device_id = dev["id"] if dev else self.ensure_device(label=device)
        mount_root = self.resolve_mount_root(device_id)
        stored_path = self._compute_rel_path(path, mount_root)

        # Verifica se già esiste (device + path + node_id)
        existing = self._query(
            "SELECT id FROM file_registry "
            "WHERE node_id = %s AND device = %s AND path = %s LIMIT 1",
            (node_id, device, stored_path),
        )
        if existing:
            # Aggiorna last_seen
            self._execute(
                "UPDATE file_registry SET status = 'online', last_seen = NOW() WHERE id = %s",
                (existing[0]["id"],),
            )
            return existing[0]["id"]

        # Inserisce nuova riga
        sql = """INSERT INTO file_registry (node_id, device, device_id, path, size_bytes, sha256, mime_type, status)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, 'online')"""
        self._execute(sql, (node_id, device, device_id, stored_path, size_bytes, sha256, mime_type))
        return int(self._conn.insert_id())

    def get_file_registry_for_node(self, node_id: str) -> list[dict]:
        """Restituisce TUTTE le posizioni fisiche registrate per un nodo.

        Arricchisce ogni riga con mount_root per host corrente (device_mounts)
        e fallback a devices.mount_root per retrocompat.
        """
        return self._query(
            """SELECT f.*, d.mount_root, d.type AS device_type,
                      dm.mount_root AS host_mount_root
               FROM file_registry f
               LEFT JOIN devices d ON d.id = f.device_id
               LEFT JOIN device_mounts dm ON dm.device_id = f.device_id
                   AND dm.hostname = %s
               WHERE f.node_id = %s
               ORDER BY f.last_seen DESC""",
            (self.current_hostname(), node_id),
        )

    def resolve_file_path(self, registry_row: dict) -> str:
        """Ricostruisce il path assoluto del file da una riga file_registry.

        Risolve mount_root del device per host corrente (device_mounts).
        Fallback a devices.mount_root (deprecato) se non trovato per host.
        Se nessun mount_root, restituisce path inalterato (backward compat).

        Args:
            registry_row: dict con chiavi 'path', 'device_id', opzionalmente
                          'mount_root' (da devices) e 'host_mount_root' (da device_mounts).

        Returns:
            Path assoluto del file.
        """
        path = registry_row.get("path", "")
        # Priorita': host_mount_root (device_mounts) > mount_root (devices, deprecato)
        mount_root = (
            registry_row.get("host_mount_root")
            or registry_row.get("mount_root")
            or registry_row.get("device_mount_root")
        )
        if mount_root and not registry_row.get("_already_resolved"):
            # Se path è già assoluto (vecchi dati), restituiscilo inalterato
            if path.startswith("/") or path.startswith("\\") or (len(path) > 1 and path[1] == ":"):
                return path
            # Altrimenti combina mount_root + path relativo
            return str(Path(mount_root) / path)
        return path

    def get_files_by_device(self, device: str) -> list[dict]:
        sql = """SELECT * FROM file_registry WHERE device = %s ORDER BY path"""
        return self._query(sql, (device,))

    def get_files_by_device_id(self, device_id: int) -> list[dict]:
        sql = """SELECT f.*, d.mount_root FROM file_registry f
                 LEFT JOIN devices d ON d.id = f.device_id
                 WHERE f.device_id = %s ORDER BY f.path"""
        return self._query(sql, (device_id,))

    def get_file_by_sha256(self, sha256: str) -> Optional[dict]:
        sql = "SELECT * FROM file_registry WHERE sha256 = %s LIMIT 1"
        rows = self._query(sql, (sha256,))
        return rows[0] if rows else None

    def get_file_registry_locations_by_sha256(self, sha256: str) -> list[dict]:
        """Restituisce TUTTE le posizioni per un certo sha256.

        Utile per dedup: invece di prendere solo la prima riga,
        si possono vedere tutte le copie (stesso contenuto, device diversi).
        """
        return self._query(
            """SELECT f.*, d.mount_root, d.label AS device_label
               FROM file_registry f
               LEFT JOIN devices d ON d.id = f.device_id
               WHERE f.sha256 = %s
               ORDER BY f.last_seen DESC""",
            (sha256,),
        )

    def verify_file_location(self, registry_id: int, is_online: bool) -> bool:
        """Aggiorna status e last_verified_at per una riga file_registry."""
        status = "online" if is_online else "offline"
        return self._execute(
            "UPDATE file_registry SET status = %s, last_verified_at = NOW(), last_seen = NOW() WHERE id = %s",
            (status, registry_id),
        ) > 0

    def verify_file_location_status(self, registry_id: int, status: str) -> bool:
        """Aggiorna status esplicito e last_verified_at per una riga file_registry.

        Args:
            registry_id: ID riga file_registry.
            status: 'online', 'offline' o 'unknown_from_host'.

        Returns:
            True se aggiornato.
        """
        if status not in ("online", "offline", "unknown_from_host"):
            raise ValueError(f"Status non valido: {status}")
        return self._execute(
            "UPDATE file_registry SET status = %s, last_verified_at = NOW(), last_seen = NOW() WHERE id = %s",
            (status, registry_id),
        ) > 0

    # ─── CODA DI INGESTIONE ─────────────────────────────────────

    def enqueue(self, node_id: str, priority: int = 0) -> int:
        sql = """INSERT INTO ingestion_queue (node_id, status, priority)
                 VALUES (%s, 'pending', %s)"""
        self._execute(sql, (node_id, priority))
        return int(self._conn.insert_id())

    def dequeue(self, limit: int = 1) -> list[dict]:
        """Preleva i prossimi elementi pending (più prioritari prima)."""
        sql = """SELECT * FROM ingestion_queue
                 WHERE status = 'pending'
                 ORDER BY priority DESC, created_at ASC
                 LIMIT %s FOR UPDATE"""
        rows = self._query(sql, (limit,))
        for row in rows:
            self._execute("UPDATE ingestion_queue SET status = 'processing' WHERE id = %s", (row["id"],))
        return rows

    def mark_done(self, queue_id: int, error: Optional[str] = None) -> bool:
        status = "failed" if error else "done"
        sql = "UPDATE ingestion_queue SET status = %s, error_msg = %s WHERE id = %s"
        return self._execute(sql, (status, error, queue_id)) > 0

    def reset_stale_processing(self, max_age_minutes: int = 30) -> int:
        """Resetta elementi bloccati in 'processing' da più di N minuti.

        Quando un dispatcher crasha, gli item rimangono in 'processing'.
        Questo metodo li riporta a 'pending' per essere rielaborati.

        Args:
            max_age_minutes: età massima in minuti per considerare un item 'stale'

        Returns:
            Numero di elementi resettati.
        """
        sql = """UPDATE ingestion_queue
                 SET status = 'pending', error_msg = CONCAT_WS('; ', error_msg, 'reset_stale')
                 WHERE status = 'processing'
                   AND updated_at < NOW() - INTERVAL %s MINUTE
                 """
        affected = self._execute(sql, (max_age_minutes,))
        if affected:
            logger.warning("Reset %d elementi stale dalla coda (processing > %d min)",
                          affected, max_age_minutes)
        return affected

    # ─── INTERNI ────────────────────────────────────────────────

    def _execute(self, sql: str, params: tuple = ()) -> int:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            # Normalizza le chiavi a lowercase (MariaDB su Linux restituisce maiuscolo)
            return [
                {k.lower(): v for k, v in row.items()}
                for row in cur.fetchall()
            ]

"""
Dispatcher — elaborazione lazy della coda di ingestion.

Preleva i file dalla coda (ingestion_queue) e processa ciascuno
con gli stage configurati: embedding, NER, EXIF, face detection, etc.

Stage attivi:
  1. EXIF              — estrazione metadati foto (Pillow) — leggero, immediato
  2. Embedding testo   — indicizzazione semantica in ChromaDB (MiniLM) — CPU
  3. Embedding immagini — CLIP ViT-B/32 per ricerca cross-modale — CPU
  4. NER               — estrazione entità con SpaCy → crea nodi Person/Location
  5. Face detection    — YOLOv8n per rilevamento volti — CPU

Stage Fase 2 (futuro):
  6. Scene detection   — PySceneDetect per video
  7. Trascrizione audio — faster-whisper
"""

import json
import logging
import time
from typing import Optional

from penelope.db.mariadb_store import MariaDBStore
from penelope.ingestion.metadata import classify_category, _guess_mime

logger = logging.getLogger(__name__)

# Stage flags — attiva/disattiva singoli processori
ENABLE_EXIF = True
ENABLE_EMBEDDING = True
ENABLE_IMAGE_EMBEDDING = True  # CLIP per immagini
ENABLE_NER = True
ENABLE_FACE = True    # YOLOv8n + InsightFace per rilevamento volti
ENABLE_SCENE = True  # Scene detection con PySceneDetect (DISATTIVATO: si usano metadati video)
ENABLE_DATE_EVENTS = True  # Event nodes da data (nome file / EXIF)
ENABLE_GEOCODING = True  # Reverse geocoding GPS → Location


class Dispatcher:
    """
    Elaboratore lazy della coda di ingestion.
    Preleva item da ingestion_queue e applica gli stage attivi.
    """

    def __init__(self, db: Optional[MariaDBStore] = None):
        self.db = db or MariaDBStore()
        self._running = False

        # ChromaStore (inizializzato lazy al primo uso)
        self._chroma = None

    @property
    def chroma(self):
        if self._chroma is None and ENABLE_EMBEDDING:
            from penelope.db.chroma_store import ChromaStore
            self._chroma = ChromaStore()
        return self._chroma

    # ─── Processamento singolo elemento ─────────────────────────

    def process_item(self, queue_item: dict) -> bool:
        """
        Elabora un elemento della coda applicando tutti gli stage attivi.

        Args:
            queue_item: dict con id, node_id, status, priority, ...

        Returns:
            True se almeno uno stage ha avuto successo
        """
        node_id = queue_item["node_id"]
        queue_id = queue_item["id"]

        try:
            # Recupera il nodo e il path del file
            node = self.db.get_node(node_id)
            if not node:
                logger.warning("Nodo %s non trovato, rimuovo dalla coda", node_id)
                self.db.mark_done(queue_id, error="node_not_found")
                return False

            # Trova il path dal file_registry (con mount_root per path relativi)
            file_info = self.db._query(
                """SELECT f.*, d.mount_root
                   FROM file_registry f
                   LEFT JOIN devices d ON d.id = f.device_id
                   WHERE f.node_id = %s LIMIT 1""",
                (node_id,),
            )
            if not file_info:
                logger.warning("File registry per %s non trovato", node_id)
                self.db.mark_done(queue_id, error="registry_not_found")
                return False

            # Risolve path assoluto (mount_root + path relativo, o path inalterato se gia' assoluto)
            file_path = self.db.resolve_file_path(file_info[0])

            # ─── ROUTING PER CATEGORIA ──────────────────────────
            # image  → YOLO oggetti + InsightFace se persona + EXIF + CLIP + date/geo
            # video  → solo metadati contenitore + relazioni (nessuna scene detection)
            # document → NER + embedding + SIMILAR_TO + date
            # other  → embedding base + date
            from penelope.ingestion.metadata import _guess_mime as _gm, classify_category as _cc
            category = _cc(_gm(Path(file_path)), Path(file_path))

            ok = False
            if category == "image":
                ok = self._process_image(node_id, file_path)
            elif category == "video":
                ok = self._process_video(node_id, file_path)
            elif category == "document":
                ok = self._process_document(node_id, file_path)
            else:
                ok = self._process_other(node_id, file_path)

            self.db.mark_done(queue_id)
            return True

        except Exception as e:
            logger.error("Errore processando coda[%d] node=%s: %s",
                         queue_id, node_id, e)
            self.db.mark_done(queue_id, error=str(e))
            return False

    # ─── Processamento per categoria ──────────────────────────────

    def _process_image(self, node_id: str, file_path: str) -> bool:
        """Immagini: YOLO oggetti + InsightFace (se persona) + EXIF + CLIP + date/geo."""
        ok = False

        # YOLO (oggetti) + face (se persona) — evita ri-analisi
        try:
            from penelope.ingestion.analyzer import analyze_image
            res = analyze_image(node_id, file_path, self.db)
            ok = bool(res.get("objects")) or res.get("faces", 0) > 0
        except Exception as e:
            logger.debug("YOLO/face analysis fallito per %s: %s", file_path, e)

        # EXIF (metadati foto)
        if ENABLE_EXIF:
            try:
                from penelope.ingestion.processor import process_exif
                process_exif(node_id, file_path, self.db)
            except Exception as e:
                logger.debug("EXIF fallito per %s: %s", file_path, e)

        # Embedding CLIP
        if ENABLE_IMAGE_EMBEDDING and self.chroma:
            try:
                from penelope.ingestion.processor import process_image_embedding
                ok = ok or process_image_embedding(node_id, file_path, self.db, self.chroma)
            except Exception as e:
                logger.debug("CLIP fallito per %s: %s", file_path, e)

        # Event nodes da data + geocoding GPS
        if ENABLE_DATE_EVENTS:
            try:
                from penelope.ingestion.processor import process_date_event
                process_date_event(node_id, file_path, self.db)
            except Exception as e:
                logger.debug("Date event fallito per %s: %s", file_path, e)
        if ENABLE_GEOCODING:
            try:
                from penelope.ingestion.processor import process_geocoding
                process_geocoding(node_id, file_path, self.db)
            except Exception as e:
                logger.debug("Geocoding fallito per %s: %s", file_path, e)

        return ok

    def _process_video(self, node_id: str, file_path: str) -> bool:
        """Video: SOLO metadati contenitore + relazioni (Event/Location).

        Nessuna analisi contenuto (scene detection). Il video viene
        relazionato con gli altri nodi tramite i metadati che contiene.
        """
        ok = False
        try:
            from penelope.ingestion.analyzer import analyze_video
            res = analyze_video(node_id, file_path, self.db)
            ok = bool(res.get("metadata"))
        except Exception as e:
            logger.debug("Video metadata fallito per %s: %s", file_path, e)

        # Event da data nel filename/creazione
        if ENABLE_DATE_EVENTS:
            try:
                from penelope.ingestion.processor import process_date_event
                ok = ok or process_date_event(node_id, file_path, self.db)
            except Exception as e:
                logger.debug("Date event per video fallito: %s", e)

        return ok

    def _process_document(self, node_id: str, file_path: str) -> bool:
        """Documenti: NER + embedding semantico + SIMILAR_TO + date."""
        ok = False
        try:
            from penelope.ingestion.analyzer import analyze_document
            res = analyze_document(
                node_id, file_path, self.db,
                self.chroma if ENABLE_EMBEDDING else None,
            )
            ok = bool(res.get("ner_count")) or bool(res.get("embedded")) or bool(res.get("similar"))
        except Exception as e:
            logger.debug("Document analysis fallito per %s: %s", file_path, e)

        # Fallback: embedding base
        if ENABLE_EMBEDDING and self.chroma and not ok:
            try:
                from penelope.ingestion.processor import process_embedding
                ok = process_embedding(node_id, file_path, self.db, self.chroma)
            except Exception as e:
                logger.debug("Embedding fallito per %s: %s", file_path, e)

        # Event da data
        if ENABLE_DATE_EVENTS:
            try:
                from penelope.ingestion.processor import process_date_event
                ok = ok or process_date_event(node_id, file_path, self.db)
            except Exception as e:
                logger.debug("Date event per doc fallito: %s", e)

        return ok

    def _process_other(self, node_id: str, file_path: str) -> bool:
        """Altri file: soli metadati + eventuale embedding testo + date."""
        ok = False

        # Embedding se testo (anche se mime dice other)
        if ENABLE_EMBEDDING and self.chroma:
            try:
                from penelope.ingestion.processor import process_embedding
                ok = process_embedding(node_id, file_path, self.db, self.chroma)
            except Exception as e:
                logger.debug("Embedding fallito per %s: %s", file_path, e)

        if ENABLE_DATE_EVENTS:
            try:
                from penelope.ingestion.processor import process_date_event
                ok = ok or process_date_event(node_id, file_path, self.db)
            except Exception as e:
                logger.debug("Date event per other fallito: %s", e)

        return ok

    # ─── Loop di elaborazione ───────────────────────────────────

    def process_queue(self, batch_size: int = 5) -> int:
        """
        Processa un batch di elementi dalla coda.

        Args:
            batch_size: quanti elementi prelevare per volta

        Returns:
            Numero di elementi processati
        """
        processed = 0
        with self.db as store:
            items = store.dequeue(limit=batch_size)
            for item in items:
                if self.process_item(item):
                    processed += 1

        if processed:
            logger.info("Coda: %d elementi processati", processed)

        return processed

    def run_loop(self, interval: float = 5.0, batch_size: int = 5,
                 reset_stale_on_start: bool = True) -> None:
        """
        Avvia un loop continuo che processa la coda ogni `interval` secondi.

        Args:
            interval: secondi tra un poll e l'altro
            batch_size: elementi per poll
            reset_stale_on_start: se True, resetta automaticamente gli elementi
                                  bloccati in 'processing' all'avvio
        """
        # Auto-reset elementi stale (crash recovery)
        if reset_stale_on_start:
            with self.db as store:
                stale = store.reset_stale_processing(max_age_minutes=5)
                if stale:
                    logger.warning("Recuperati %d elementi bloccati dalla coda", stale)

        self._running = True
        logger.info("Dispatcher avviato (interval=%ss, batch=%d, category-routing=on)",
                     interval, batch_size)

        try:
            while self._running:
                count = self.process_queue(batch_size=batch_size)
                if count > 0:
                    continue  # finché coda non è vuota
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Dispatcher fermato da interrupt")
        finally:
            self._running = False

    def stop(self) -> None:
        """Ferma il loop di elaborazione."""
        self._running = False
        logger.info("Dispatcher fermato")

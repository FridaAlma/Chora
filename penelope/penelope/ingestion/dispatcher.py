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

logger = logging.getLogger(__name__)

# Stage flags — attiva/disattiva singoli processori
ENABLE_EXIF = True
ENABLE_EMBEDDING = True
ENABLE_IMAGE_EMBEDDING = True  # CLIP per immagini
ENABLE_NER = True
ENABLE_FACE = True    # YOLOv8n per rilevamento volti
ENABLE_SCENE = True  # Scene detection con PySceneDetect
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

            # Risolve path assoluto (mount_root + path relativo, o path inalterato se gia\' assoluto)
            file_path = self.db.resolve_file_path(file_info[0])

            # ─── Stage 1: EXIF (foto) ──────────────────────────
            exif_ok = False
            if ENABLE_EXIF:
                try:
                    from penelope.ingestion.processor import process_exif
                    exif_ok = process_exif(node_id, file_path, self.db)
                except Exception as e:
                    logger.debug("EXIF fallito per %s: %s", file_path, e)

            # ─── Stage 2: Embedding testo (MiniLM) ─────────────
            emb_ok = False
            if ENABLE_EMBEDDING and self.chroma:
                try:
                    from penelope.ingestion.processor import process_embedding
                    emb_ok = process_embedding(node_id, file_path, self.db, self.chroma)
                except Exception as e:
                    logger.debug("Embedding testo fallito per %s: %s", file_path, e)

            # ─── Stage 3: Embedding immagini (CLIP) ────────────
            img_emb_ok = False
            if ENABLE_IMAGE_EMBEDDING and self.chroma:
                try:
                    from penelope.ingestion.processor import process_image_embedding
                    img_emb_ok = process_image_embedding(node_id, file_path, self.db, self.chroma)
                except Exception as e:
                    logger.debug("Embedding immagine fallito per %s: %s", file_path, e)

            # ─── Stage 4: NER (testo) ──────────────────────────
            ner_ok = False
            if ENABLE_NER:
                try:
                    from penelope.ingestion.processor import process_ner
                    ner_ok = process_ner(node_id, file_path, self.db) > 0
                except Exception as e:
                    logger.debug("NER fallito per %s: %s", file_path, e)

            # ─── Stage 5: Face detection (foto) — YOLOv8n ──────
            face_ok = False
            if ENABLE_FACE:
                try:
                    from penelope.ingestion.processor import process_face_detection
                    face_ok = process_face_detection(node_id, file_path, self.db)
                except Exception as e:
                    logger.debug("Face detection fallito per %s: %s", file_path, e)

            # ─── Stage 6: Event nodes da data ────────────────
            event_ok = False
            if ENABLE_DATE_EVENTS:
                try:
                    from penelope.ingestion.processor import process_date_event
                    event_ok = process_date_event(node_id, file_path, self.db)
                except Exception as e:
                    logger.debug("Event creation fallito per %s: %s", file_path, e)

            # ─── Stage 7: Geocoding GPS ──────────────────────
            geo_ok = False
            if ENABLE_GEOCODING:
                try:
                    from penelope.ingestion.processor import process_geocoding
                    geo_ok = process_geocoding(node_id, file_path, self.db)
                except Exception as e:
                    logger.debug("Geocoding fallito per %s: %s", file_path, e)

            # ─── Stage 8: Scene detection (video) ─────────────
            if ENABLE_SCENE:
                try:
                    from penelope.ingestion.processor import process_scene_detection
                    process_scene_detection(node_id, file_path, self.db)
                except Exception as e:
                    logger.debug("Scene detection fallito: %s", e)

            success = exif_ok or emb_ok or img_emb_ok or ner_ok or face_ok or event_ok or geo_ok
            self.db.mark_done(queue_id)
            if success:
                logger.debug("Processato: %s (exif=%s emb=%s img_emb=%s ner=%s face=%s event=%s geo=%s)",
                             file_path, exif_ok, emb_ok, img_emb_ok, ner_ok, face_ok, event_ok, geo_ok)
            return True

        except Exception as e:
            logger.error("Errore processando coda[%d] node=%s: %s",
                         queue_id, node_id, e)
            self.db.mark_done(queue_id, error=str(e))
            return False

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
        logger.info("Dispatcher avviato (interval=%ss, batch=%d, exif=%s emb=%s img_emb=%s ner=%s face=%s)",
                     interval, batch_size, ENABLE_EXIF, ENABLE_EMBEDDING, ENABLE_IMAGE_EMBEDDING, ENABLE_NER, ENABLE_FACE)

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

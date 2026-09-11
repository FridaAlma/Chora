"""
ChromaDB — memoria vettoriale per Penelope.

Due collezioni:
  - file_embeddings: embedding testuali (MiniLM, 384-dim) per file di testo
  - image_embeddings: embedding visivi (CLIP, 512-dim) per immagini

La ricerca semantica interroga entrambe le collezioni per risultati
cross-modali (testo → testo, testo → immagini).
"""

import logging
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings

from core.config import settings as penelope_settings

logger = logging.getLogger(__name__)

# Modello di embedding caricato lazy
_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is not None:
        return _embedder
    try:
        import os
        # Modalità offline per evitare timeout su HuggingFace Hub
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer

        # MiniLM: 80MB RAM, CPU, ~1000 doc/min su i3
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Modello embedding caricato: all-MiniLM-L6-v2")
        return _embedder
    except ImportError:
        logger.warning(
            "sentence-transformers non installato. "
            "Esegui: pip install sentence-transformers"
        )
        return None
    except Exception as e:
        logger.error("Errore caricamento modello embedding: %s", e)
        return None


class ChromaStore:
    """Store vettoriale per i file del grafo.

    Gestisce due collezioni:
      - file_embeddings (testo, 384-dim MiniLM)
      - image_embeddings (immagini, 512-dim CLIP)
    """

    def __init__(self, persist_dir: Optional[str] = None):
        self._persist_dir = Path(persist_dir or penelope_settings.CHROMADB_PATH)
        self._persist_dir.mkdir(parents=True, exist_ok=True)

        self._client = chromadb.PersistentClient(
            path=str(self._persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )

        # Collezione per embedding testuali (MiniLM, 384-dim)
        self._collection = self._client.get_or_create_collection(
            name="file_embeddings",
            metadata={"hnsw:space": "cosine"},
        )

        # Collezione per embedding immagini (CLIP, 512-dim)
        self._image_collection = self._client.get_or_create_collection(
            name="image_embeddings",
            metadata={"hnsw:space": "cosine"},
        )

        logger.info("ChromaStore pronto: %s (testo=%d, immagini=%d)",
                     self._persist_dir,
                     self._collection.count(),
                     self._image_collection.count())

    # ─── Inserimento testo ───────────────────────────────────

    def index_text(
        self,
        node_id: str,
        text: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Genera embedding per un testo e lo salva in ChromaDB.

        Args:
            node_id: UUID del nodo File nel grafo.
            text: Contenuto testuale del file.
            metadata: Metadati aggiuntivi (file_name, mime_type, etc.).

        Returns:
            True se successo, False altrimenti.
        """
        embedder = _get_embedder()
        if embedder is None:
            return False

        try:
            # Tronca testi molto lunghi (MiniLM supporta max 512 token ~ 2000 char)
            text_chunk = text[:100000]  # limite generoso

            embedding = embedder.encode(text_chunk).tolist()

            self._collection.upsert(
                ids=[node_id],
                embeddings=[embedding],
                metadatas=[metadata or {}],
                documents=[text_chunk[:1000]],  # documento breve per preview
            )
            return True
        except Exception as e:
            logger.warning("Errore embedding per %s: %s", node_id, e)
            return False

    # ─── Inserimento immagini con CLIP ────────────────────────

    def index_image(
        self,
        node_id: str,
        image_path: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Genera embedding visivo CLIP per un'immagine e lo salva.

        Sostituisce il vecchio placeholder con embedding CLIP reale.
        CLIP usa 512 dimensioni (VS MiniLM 384), collezione separata.

        Args:
            node_id: UUID del nodo File nel grafo.
            image_path: Percorso del file immagine.
            metadata: Metadati (file_name, mime_type, etc.).

        Returns:
            True se successo, False altrimenti.
        """
        try:
            from core.ingestion.image_embedder import get_image_embedding

            embedding = get_image_embedding(image_path)
            if embedding is None:
                logger.debug("CLIP non disponibile per %s, uso placeholder", image_path)
                return self.index_image_placeholder(node_id, metadata)

            self._image_collection.upsert(
                ids=[node_id],
                embeddings=[embedding],
                metadatas=[metadata or {}],
                documents=[f"[IMAGE] {metadata.get('file_name', Path(image_path).name)}"
                          if metadata else f"[IMAGE] {Path(image_path).name}"],
            )
            return True
        except Exception as e:
            logger.warning("Errore embedding immagine %s: %s", image_path, e)
            return False

    def index_image_placeholder(
        self,
        node_id: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Placeholder per immagini quando CLIP non è disponibile.

        Usa un vettore di zeri 512-dim per evitare errori di
        dimensione nella collezione image_embeddings.
        """
        try:
            import numpy as np

            # Placeholder: vettore di zeri (512 dim per CLIP)
            embedding = np.zeros(512).tolist()
            file_name = (metadata or {}).get("file_name", "unknown")

            self._image_collection.upsert(
                ids=[node_id],
                embeddings=[embedding],
                metadatas=metadata or {},
                documents=f"[IMAGE] {file_name}",
            )
            return True
        except Exception as e:
            logger.warning("Errore placeholder immagine %s: %s", node_id, e)
            return False

    # ─── Query ────────────────────────────────────────────────

    def search_similar(
        self,
        query: str,
        top_k: int = 10,
        filter_mime: Optional[str] = None,
        include_images: bool = True,
    ) -> list[dict]:
        """Cerca file per similarità semantica.

        Cerca sia nella collezione testo (MiniLM) che in quella
        immagini (CLIP) per risultati cross-modali.

        Args:
            query: Testo di ricerca.
            top_k: Numero risultati.
            filter_mime: Filtro per mime_type (es. 'text/markdown').
            include_images: Se True, cerca anche tra le immagini via CLIP.

        Returns:
            Lista di { node_id, file_name, mime_type, distance, snippet }.
        """
        output = []

        # 1. Cerca nei testi (MiniLM)
        embedder = _get_embedder()
        if embedder is not None:
            try:
                q_emb = embedder.encode(query).tolist()
                where = {"mime_type": filter_mime} if filter_mime else None

                results = self._collection.query(
                    query_embeddings=[q_emb],
                    n_results=top_k,
                    where=where,
                )

                for i in range(len(results["ids"][0])):
                    meta = results["metadatas"][0][i] if results["metadatas"] else {}
                    output.append({
                        "node_id": results["ids"][0][i],
                        "file_name": meta.get("file_name", ""),
                        "mime_type": meta.get("mime_type", ""),
                        "distance": results["distances"][0][i] if results["distances"] else 0,
                        "snippet": results["documents"][0][i] if results["documents"] else "",
                    })
            except Exception as e:
                logger.error("Errore query testo: %s", e)

        # 2. Cerca tra le immagini (CLIP) — cross-modale
        if include_images:
            try:
                from core.ingestion.image_embedder import get_text_embedding

                clip_emb = get_text_embedding(query)
                if clip_emb is not None:
                    where_img = None
                    if filter_mime and filter_mime.startswith("image/"):
                        where_img = {"mime_type": filter_mime}

                    img_results = self._image_collection.query(
                        query_embeddings=[clip_emb],
                        n_results=top_k,
                        where=where_img,
                    )

                    for i in range(len(img_results["ids"][0])):
                        meta = img_results["metadatas"][0][i] if img_results["metadatas"] else {}
                        output.append({
                            "node_id": img_results["ids"][0][i],
                            "file_name": meta.get("file_name", ""),
                            "mime_type": meta.get("mime_type", "image/*"),
                            "distance": img_results["distances"][0][i] if img_results["distances"] else 0,
                            "snippet": img_results["documents"][0][i] if img_results["documents"] else "[IMAGE]",
                        })
            except Exception as e:
                logger.debug("Query CLIP non disponibile: %s", e)

        # Ordina per distanza (cosine) e taglia a top_k
        output.sort(key=lambda x: x["distance"])
        return output[:top_k]

    def count(self) -> int:
        """Numero di documenti indicizzati (testo + immagini)."""
        try:
            return self._collection.count() + self._image_collection.count()
        except Exception:
            return 0

    def count_text(self) -> int:
        try:
            return self._collection.count()
        except Exception:
            return 0

    def count_images(self) -> int:
        try:
            return self._image_collection.count()
        except Exception:
            return 0

    def close(self) -> None:
        try:
            self._client.clear_system_cache()
        except Exception:
            pass

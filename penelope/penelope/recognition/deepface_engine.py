"""
Face Engine — face detection, embedding e riconoscimento locale con InsightFace.

Usa InsightFace (ONNX Runtime) per:
  - Face detection con RetinaFace (CPU, ~2-5 img/s)
  - Face embedding 512-dim con ArcFace (modello ~30MB)
  - Face verification / identification
  - Gender, age estimation
  - Clustering di volti simili

Tutto gira su CPU (i3, 8GB), nessuna API esterna.
Dipendenze: pip install insightface onnxruntime
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from penelope.db.mariadb_store import MariaDBStore

logger = logging.getLogger(__name__)

# Directory per embedding salvati su file
EMBEDDINGS_DIR = Path("data/embeddings")

# Cache del modello (lazy loading)
_face_analyzer = None


def _get_analyzer():
    """Carica il modello InsightFace lazy (detection + recognition + age/gender)."""
    global _face_analyzer
    if _face_analyzer is not None:
        return _face_analyzer
    try:
        import insightface
        from insightface.app import FaceAnalysis
        from insightface.model_zoo import get_model

        # Inizializza FaceAnalysis con i modelli base
        # providers=['CPUExecutionProvider'] forza CPU
        app = FaceAnalysis(
            name="buffalo_l",  # modello più piccolo e veloce
            providers=["CPUExecutionProvider"],
        )
        app.prepare(ctx_id=0, det_size=(320, 320))  # risoluzione ridotta per velocità

        _face_analyzer = app
        logger.info("InsightFace caricato: buffalo_l (detection + recognition + age/gender)")
        return app
    except Exception as e:
        logger.warning("Errore caricamento InsightFace: %s", e)
        return None


# ─── Core functions ─────────────────────────────────────────────────


def detect_faces(image_path: str) -> list[dict]:
    """Rileva volti in un'immagine con InsightFace (RetinaFace + ArcFace).

    Args:
        image_path: Path immagine.

    Returns:
        Lista di dict, uno per volto:
          - face: np.array (immagine ritagliata RGB)
          - bbox: [x1, y1, x2, y2]
          - det_score: float (confidenza)
          - landmark: [[x,y], ...] 5 landmark
          - embedding: list[float] 512-dim (None se non calcolato)
          - gender: int (0=female, 1=male) | None
          - age: float | None
    """
    app = _get_analyzer()
    if app is None:
        logger.warning("InsightFace non disponibile")
        return []

    img = cv2.imread(image_path)
    if img is None:
        logger.debug("Impossibile leggere: %s", image_path)
        return []

    # InsightFace lavora in RGB
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    try:
        faces = app.get(img_rgb)
    except Exception as e:
        logger.warning("Errore detection: %s", e)
        return []

    results = []
    for face in faces:
        # Bounding box [x1, y1, x2, y2]
        bbox = face.bbox.astype(int).tolist() if hasattr(face, "bbox") else None
        det_score = float(face.det_score) if hasattr(face, "det_score") else 1.0
        landmark = face.landmark.tolist() if hasattr(face, "landmark") and face.landmark is not None else None
        gender = int(face.gender) if hasattr(face, "gender") else None
        age = float(face.age) if hasattr(face, "age") else None
        embedding = face.normed_embedding.tolist() if hasattr(face, "normed_embedding") and face.normed_embedding is not None else None

        # Ritaglia il volto
        if bbox:
            x1, y1, x2, y2 = bbox
            face_img = img_rgb[y1:y2, x1:x2]
        else:
            face_img = img_rgb

        results.append({
            "face": face_img,
            "bbox": bbox,
            "det_score": det_score,
            "landmark": landmark,
            "embedding": embedding,
            "gender": gender,
            "age": age,
        })

    return results


def extract_embedding(face_img: np.ndarray) -> Optional[list[float]]:
    """Calcola embedding 512-dim per un volto usando ArcFace.

    Args:
        face_img: np.array del volto (H, W, 3) in RGB.

    Returns:
        Lista di 512 float (embedding normalizzato).
    """
    app = _get_analyzer()
    if app is None:
        return None

    try:
        # InsightFace si aspetta un'immagine intera con detection
        # Per embedding su volto ritagliato, usiamo il modello direttamente
        from insightface.model_zoo import get_model

        model = get_model("buffalo_l")
        embedding = model.get_embedding(face_img)
        return embedding.tolist() if embedding is not None else None
    except Exception as e:
        logger.warning("Errore embedding: %s", e)
        return None


def verify_faces(emb1: list[float], emb2: list[float], threshold: float = 0.4) -> dict:
    """Confronta due embedding (stessa persona?).

    Usa similarità coseno. Soglia tipica ArcFace 512-dim: 0.3-0.5.

    Returns:
        {"verified": bool, "similarity": float, "distance": float}
    """
    a = np.array(emb1, dtype=np.float32)
    b = np.array(emb2, dtype=np.float32)

    similarity = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))
    distance = 1.0 - similarity

    return {
        "verified": similarity > threshold,
        "similarity": round(similarity, 4),
        "distance": round(distance, 4),
        "threshold": threshold,
    }


def cosine_similarity(emb1: list[float], emb2: list[float]) -> float:
    """Similarità coseno tra due embedding (0-1)."""
    a = np.array(emb1, dtype=np.float32)
    b = np.array(emb2, dtype=np.float32)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))


# ─── Salvataggio embedding su file ────────────────────────────────


def save_embedding(person_id: str, embedding: list[float]) -> None:
    """Salva embedding in file .npy (molto più efficiente di MariaDB)."""
    EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
    npy_path = EMBEDDINGS_DIR / f"{person_id}.npy"
    np.save(str(npy_path), np.array(embedding, dtype=np.float32))


def load_embedding(person_id: str) -> Optional[np.ndarray]:
    """Carica embedding dal file .npy."""
    npy_path = EMBEDDINGS_DIR / f"{person_id}.npy"
    if npy_path.exists():
        return np.load(str(npy_path))
    return None


# ─── Integrazione nel grafo Penelope ──────────────────────────────


def process_face_embedding(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
) -> bool:
    """Analizza un'immagine con InsightFace e crea nodi Person nel grafo.

    Per ogni volto rilevato:
      1. Calcola embedding 512-dim con ArcFace
      2. Crea nodo Person con embedding ed eventuali attributi (età, genere)
      3. Collega File -> Person con edge CONTAINS
      4. Salva embedding in file .npy (data/embeddings/<person_id>.npy)

    Returns:
        True se almeno un volto rilevato.
    """
    from penelope.ingestion.metadata import _guess_mime as _gm
    path = Path(file_path)
    mime = _gm(path)

    if not mime.startswith("image/") or mime == "image/svg+xml":
        return False

    faces = detect_faces(str(path))
    if not faces:
        return False

    logger.info("InsightFace: %d volti in %s", len(faces), file_path)

    # Aggiorna metadati del File
    node = db.get_node(node_id)
    if node:
        current_meta = node.get("metadata") or {}
        if isinstance(current_meta, str):
            current_meta = json.loads(current_meta) if current_meta else {}

        current_meta["face_count"] = len(faces)
        current_meta["has_faces"] = True
        current_meta["face_source"] = "insightface"
        current_meta["face_details"] = [
            {
                "bbox": f["bbox"],
                "det_score": f["det_score"],
                "gender": f["gender"],
                "age": f["age"],
                "has_embedding": f["embedding"] is not None,
            }
            for f in faces
        ]
        db._execute(
            "UPDATE nodes SET metadata = %s WHERE id = %s",
            (json.dumps(current_meta), node_id),
        )

    # Crea nodi Person con embedding
    for idx, face_data in enumerate(faces):
        person_label = f"Person_in_{path.stem}_{idx}"
        bbox = face_data.get("bbox")
        embedding = face_data.get("embedding")

        person_meta = {
            "source": "insightface",
            "file_node_id": node_id,
            "bbox": bbox,
            "det_score": face_data.get("det_score"),
            "gender": face_data.get("gender"),
            "age": face_data.get("age"),
            "photo": file_path,
            "model": "ArcFace_512",
        }

        if embedding:
            # Salva embedding su file .npy
            person_meta["embedding_file"] = f"{node_id}_{idx}.npy"
            person_meta["embedding_dim"] = len(embedding)
            # In metadata salviamo solo i primi valori per debug
            person_meta["embedding_preview"] = [round(v, 4) for v in embedding[:8]]

        existing = db._query(
            "SELECT id FROM nodes WHERE type = 'Person' AND label = %s LIMIT 1",
            (person_label,),
        )

        if existing:
            person_id = existing[0]["id"]
        else:
            person_id = db.create_node(
                node_type="Person",
                label=person_label,
                metadata=person_meta,
            )

        # Salva embedding completo su file
        if embedding:
            EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
            npy_path = EMBEDDINGS_DIR / f"{person_id}.npy"
            np.save(str(npy_path), np.array(embedding, dtype=np.float32))

        # Edge CONTAINS
        existing_edge = db._query(
            "SELECT id FROM edges WHERE source_id = %s AND target_id = %s AND relation = 'CONTAINS'",
            (node_id, person_id),
        )
        if not existing_edge:
            db.create_edge(
                source_id=node_id,
                target_id=person_id,
                relation="CONTAINS",
                weight=face_data.get("det_score", 1.0),
                metadata={"bbox": bbox, "source": "insightface"},
            )

    return True


# ─── Clustering: trova e unisce volti simili ──────────────────────


def find_similar_persons(
    db: MariaDBStore,
    threshold: float = 0.4,
    batch_size: int = 50,
) -> list[tuple[str, str, float]]:
    """Trova coppie di nodi Person con embedding simili.

    Confronta tutti i nodi Person via embedding 512-dim.
    Similarità coseno > threshold = stessa persona.

    Args:
        db: MariaDBStore.
        threshold: Soglia similarità (0.3-0.5 per ArcFace).
        batch_size: Log ogni N confronti.

    Returns:
        Lista di (person_id_1, person_id_2, similarity).
    """
    # Trova tutti i nodi Person con embedding
    rows = db._query(
        "SELECT id FROM nodes WHERE type = 'Person' AND metadata LIKE %s",
        ("%embedding_dim%",),
    )

    if not rows:
        logger.info("Nessun nodo Person con embedding trovato")
        return []

    # Carica embedding da file .npy
    persons = []
    for r in rows:
        emb = load_embedding(r["id"])
        if emb is not None:
            persons.append({"id": r["id"], "embedding": emb})

    logger.info("Confronto %d nodi Person per similarita'...", len(persons))

    similar = []
    n = len(persons)
    for i in range(n):
        emb_i = persons[i]["embedding"]
        for j in range(i + 1, n):
            emb_j = persons[j]["embedding"]
            sim = cosine_similarity(emb_i.tolist(), emb_j.tolist())
            if sim > threshold:
                similar.append((persons[i]["id"], persons[j]["id"], sim))

        if (i + 1) % batch_size == 0:
            logger.info("  Processati %d/%d nodi...", i + 1, n)

    logger.info(
        "Trovate %d coppie simili (soglia: %.2f) su %d nodi",
        len(similar), threshold, n,
    )
    return similar


def merge_persons(
    db: MariaDBStore,
    similar_pairs: list[tuple[str, str, float]],
    merge_threshold: float = 0.45,
) -> int:
    """Unisce nodi Person simili in un unico nodo.

    Per ogni coppia con similarità > merge_threshold:
      1. Tiene il nodo con più edge (più connesso)
      2. Riassegna tutti gli edge al nodo tenuto
      3. Ricostruisce embedding del nodo tenuto come media dei due
      4. Elimina il nodo duplicato

    Args:
        db: MariaDBStore.
        similar_pairs: Lista di (id1, id2, similarity).
        merge_threshold: Soglia per unire.

    Returns:
        Numero di nodi eliminati.
    """
    merged = set()
    removed = 0

    for id1, id2, sim in similar_pairs:
        if sim < merge_threshold:
            continue
        if id1 in merged or id2 in merged:
            continue

        # Chi ha più edge?
        e1 = db._query(
            "SELECT COUNT(*) as cnt FROM edges WHERE source_id = %s OR target_id = %s",
            (id1, id1),
        )
        e2 = db._query(
            "SELECT COUNT(*) as cnt FROM edges WHERE source_id = %s OR target_id = %s",
            (id2, id2),
        )
        cnt1 = e1[0]["cnt"] if e1 else 0
        cnt2 = e2[0]["cnt"] if e2 else 0
        keep = id1 if cnt1 >= cnt2 else id2
        delete = id2 if keep == id1 else id1

        # Media degli embedding (per preservare info)
        emb_keep = load_embedding(keep)
        emb_del = load_embedding(delete)
        if emb_keep is not None and emb_del is not None:
            merged_emb = (emb_keep + emb_del) / 2.0
            np.save(str(EMBEDDINGS_DIR / f"{keep}.npy"), merged_emb.astype(np.float32))

        # Riassegna edge
        db._execute(
            "UPDATE edges SET source_id = %s WHERE source_id = %s AND target_id != %s",
            (keep, delete, keep),
        )
        db._execute(
            "UPDATE edges SET target_id = %s WHERE target_id = %s AND source_id != %s",
            (keep, delete, keep),
        )

        # Elimina embedding del duplicato
        del_path = EMBEDDINGS_DIR / f"{delete}.npy"
        if del_path.exists():
            del_path.unlink()

        # Elimina nodo
        db._execute("DELETE FROM edges WHERE source_id = %s AND target_id = %s", (delete, delete))
        db._execute("DELETE FROM nodes WHERE id = %s", (delete,))
        merged.add(delete)
        removed += 1

    logger.info("Merge: %d nodi Person uniti", removed)
    return removed


# ─── Batch processing ─────────────────────────────────────────────


def batch_process_images(db: MariaDBStore, limit: int = 0) -> dict:
    """Processa tutte le immagini del grafo con InsightFace.

    1. Trova immagini senza analisi face (analyzed->face != done)
    2. Per ognuna, rileva volti + embedding
    3. Crea/aggiorna nodi Person

    Args:
        db: MariaDBStore.
        limit: Massimo immagini (0 = tutte).

    Returns:
        {"processed": int, "with_faces": int}
    """
    rows = db._query(
        """SELECT n.id, n.label, f.path
        FROM nodes n
        JOIN file_registry f ON f.node_id = n.id
        WHERE n.type = %s
          AND (n.analyzed IS NULL OR JSON_EXTRACT(n.analyzed, '$.face') != 'done')
          AND (f.mime_type LIKE %s OR f.mime_type LIKE %s OR f.mime_type LIKE %s
               OR f.mime_type LIKE %s OR f.mime_type LIKE %s)
        ORDER BY f.path
        """,
        ("File", "image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff"),
    )

    if limit and limit < len(rows):
        rows = rows[:limit]

    logger.info("InsightFace batch: %d immagini da processare", len(rows))
    processed = 0
    with_faces = 0

    for i, r in enumerate(rows, 1):
        try:
            result = process_face_embedding(r["id"], r["path"], db)
            processed += 1
            if result:
                with_faces += 1
            # Set analyzed flag
            from penelope.ingestion.analyzer import set_analyzed_flag, ANALYSIS_FACE
            set_analyzed_flag(db, r["id"], ANALYSIS_FACE, "done")
        except Exception as e:
            logger.warning("Errore su %s: %s", r["path"], e)
            from penelope.ingestion.analyzer import set_analyzed_flag, ANALYSIS_FACE
            set_analyzed_flag(db, r["id"], ANALYSIS_FACE, "error")

        if i % 50 == 0:
            logger.info("  [%d/%d] con volti=%d", i, len(rows), with_faces)

    return {"processed": processed, "with_faces": with_faces}

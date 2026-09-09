"""
Name Tag — Identity resolution e naming per nodi Person.

Fasi:
  1. Clustering: aggrega nodi Person con embedding simili (DBSCAN su cosine distance)
  2. Name assignment: imposta/legge name_tag nei metadata
  3. Propagation: propaga nome a tutti i nodi nello stesso cluster
  4. Query: cerca persone per nome

Usa ArcFace 512-dim embeddings salvati in data/embeddings/<person_id>.npy
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np

# Path embedding
_PENELOPE_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "penelope"
_EMBEDDINGS_DIR = _PENELOPE_ROOT / "data" / "embeddings"

logger = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────

# Soglia ArcFace: cosine similarity >= 0.40 = stessa persona
# ArcFace 512-dim: same person > 0.35-0.50, different < 0.20
COSINE_THRESHOLD = 0.40


def _get_embeddings_dir() -> Path:
    """Risolve la directory degli embedding."""
    if _EMBEDDINGS_DIR.exists():
        return _EMBEDDINGS_DIR
    # Fallback
    alt = Path("data/embeddings")
    if alt.exists():
        return alt
    return _EMBEDDINGS_DIR


def _load_person_embeddings(db) -> tuple[list[dict], np.ndarray, list[str]]:
    """Carica tutti i nodi Person con embedding e i loro vettori.

    Returns:
        (persons, embeddings_matrix, valid_ids)
        persons: lista di dict completi (id, label, metadata, file_path, photo_path)
        embeddings_matrix: np.array (N, 512)
        valid_ids: lista di id corrispondenti alle righe della matrice
    """
    # Query: tutti i nodi Person con embedding e foto associata (per bbox)
    # Usa DISTINCT + subquery per evitare duplicati da edge multipli
    rows = db._query(
        """SELECT DISTINCT n.id, n.label, n.metadata,
               (SELECT f.path FROM edges e
                JOIN file_registry f ON f.node_id = e.source_id
                LEFT JOIN devices d ON d.id = f.device_id
                WHERE e.target_id = n.id AND e.relation = 'CONTAINS'
                LIMIT 1) as file_path
           FROM nodes n
           WHERE n.type = 'Person'
             AND n.metadata LIKE %s""",
        ("%embedding_dim%",),
    )

    if not rows:
        logger.warning("Nessun nodo Person con embedding trovato")
        return [], np.array([]), []

    emb_dir = _get_embeddings_dir()
    persons = []
    vectors = []
    valid_ids = []

    for r in rows:
        npy_path = emb_dir / f"{r['id']}.npy"
        if not npy_path.exists():
            # Prova con embedding_file dal metadata
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            emb_file = meta.get("embedding_file", "")
            if emb_file:
                alt_path = emb_dir / emb_file
                if alt_path.exists():
                    npy_path = alt_path

        if not npy_path.exists():
            continue

        try:
            vec = np.load(str(npy_path))
            if vec.shape[0] == 512:
                persons.append(r)
                vectors.append(vec)
                valid_ids.append(r["id"])
        except Exception as e:
            logger.debug("Errore caricamento embedding %s: %s", r["id"], e)

    if not vectors:
        logger.warning("Nessun embedding valido caricato")
        return [], np.array([]), []

    embeddings = np.array(vectors, dtype=np.float32)
    logger.info(
        "Caricati %d embedding validi da %d nodi Person",
        len(vectors), len(rows),
    )
    return persons, embeddings, valid_ids


# ─── Clustering ──────────────────────────────────────────────


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity tra due vettori normalizzati."""
    return float(np.dot(a, b))


def cluster_persons(
    db,
    threshold: float = 0.40,
    min_cluster_sim: float | None = None,
) -> dict:
    """Raggruppa nodi Person in identità tramite clustering gerarchico greedy.

    Algoritmo (evita il chaining di union-find):
      1. Ordina i nodi per "centralità" (somma similarità verso tutti)
      2. Per ogni nodo non assegnato, crea un nuovo cluster
      3. Aggiungi al cluster solo volti con similarità >= threshold
         verso almeno min_cluster_sim% dei membri esistenti

    Args:
        db: MariaDBStore.
        threshold: Soglia cosine similarity (default 0.40).
        min_cluster_sim: Se None, il nuovo nodo deve matchare TUTTI i membri.
                         Se float (es. 0.7), deve matchare almeno il 70% dei membri.

    Returns:
        {
            "total_clusters": int,
            "singletons": int,
            "cosine_threshold": float,
            "comparisons": int,
            "clusters": [...]
        }
    """
    persons, embeddings, valid_ids = _load_person_embeddings(db)

    if len(embeddings) == 0:
        return {"total_clusters": 0, "singletons": 0, "clusters": [], "error": "No embeddings"}

    n = len(embeddings)
    # Normalizza
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    normalized = embeddings / (norms + 1e-10)

    logger.info(
        "Clustering %d nodi con greedy centroid (threshold=%.2f, min_cluster_sim=%s)...",
        n, threshold, min_cluster_sim or "all",
    )

    # Calcola "centralità" per ordinare (nodi più centrali = primi a formare cluster)
    centrality = np.sum(normalized @ normalized.T, axis=1)  # somma similarità verso tutti
    order = np.argsort(-centrality)  # dal più centrale

    assigned = np.zeros(n, dtype=bool)
    clusters = []  # list of dict: {indices: [int, ...], centroid: np.array}

    comparisons = 0

    for idx_i in order:
        if assigned[idx_i]:
            continue

        # Crea nuovo cluster con questo nodo come seme
        cluster_indices = [idx_i]
        cluster_vecs = [normalized[idx_i]]
        assigned[idx_i] = True

        # Cerca altri volti simili
        for idx_j in order:
            if assigned[idx_j]:
                continue
            comparisons += 1

            # Similarità con tutti i membri del cluster
            sims = normalized[idx_j] @ np.array(cluster_vecs).T  # (1, k)
            all_above = bool(np.all(sims >= threshold))

            if min_cluster_sim is not None:
                ratio_above = float(np.mean(sims >= threshold))
                can_join = ratio_above >= min_cluster_sim
            else:
                can_join = all_above  # deve matchare TUTTI

            if can_join:
                cluster_indices.append(idx_j)
                cluster_vecs.append(normalized[idx_j])
                assigned[idx_j] = True

        # Costruisci output per questo cluster
        cluster_persons = []
        for idx in cluster_indices:
            p = persons[idx]
            meta = p.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            cluster_persons.append({
                "id": p["id"],
                "label": p["label"],
                "photo": p.get("file_path") or meta.get("photo", ""),
                "bbox": meta.get("bbox"),
                "gender": meta.get("gender"),
                "age": meta.get("age"),
                "det_score": meta.get("det_score"),
            })

        # Similarità media intra-cluster
        if len(cluster_indices) > 1:
            cvecs = np.array(cluster_vecs)
            intra = cvecs @ cvecs.T
            m = len(cluster_indices)
            mask = ~np.eye(m, dtype=bool)
            mean_sim = float(np.mean(intra[mask]))
        else:
            mean_sim = 1.0

        clusters.append({
            "cluster_id": len(clusters),
            "size": len(cluster_persons),
            "persons": cluster_persons,
            "mean_similarity": round(mean_sim, 4),
        })

    # Ordina per dimensione
    clusters.sort(key=lambda c: c["size"], reverse=True)
    singletons = sum(1 for c in clusters if c["size"] == 1)

    logger.info(
        "Clustering completato: %d cluster, %d singletons, %d comparisons",
        len(clusters), singletons, comparisons,
    )

    return {
        "total_clusters": len(clusters),
        "singletons": singletons,
        "cosine_threshold": threshold,
        "comparisons": comparisons,
        "clusters": clusters,
    }


# ─── Name Tag operations ──────────────────────────────────────


def set_name_tag(
    db,
    person_id: str,
    name_tag: str,
    confidence: float = 1.0,
    source: str = "user",
) -> bool:
    """Assegna un name_tag a un nodo Person.

    Args:
        db: MariaDBStore.
        person_id: ID del nodo Person.
        name_tag: Nome da assegnare (es. "Mario").
        confidence: 1.0 per assegnazione manuale, <1.0 per propagazione.
        source: "user" per assegnazione diretta, "propagation" per automatica.

    Returns:
        True se aggiornato correttamente.
    """
    node = db.get_node(person_id)
    if not node:
        logger.warning("Nodo Person non trovato: %s", person_id)
        return False

    meta = node.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    meta["name_tag"] = name_tag.strip()
    meta["name_tag_source"] = source
    meta["name_tag_confidence"] = confidence
    meta["name_tag_assigned_at"] = __import__("datetime").datetime.now().isoformat()

    db._execute(
        "UPDATE nodes SET metadata = %s WHERE id = %s",
        (json.dumps(meta), person_id),
    )
    logger.info("name_tag '%s' assegnato a %s (source=%s)", name_tag, person_id[:8], source)
    return True


def get_name_tag(db, person_id: str) -> Optional[str]:
    """Legge il name_tag di un nodo Person."""
    node = db.get_node(person_id)
    if not node:
        return None
    meta = node.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            return None
    return meta.get("name_tag")


def get_persons_by_name(db, name_tag: str) -> list[dict]:
    """Trova tutti i nodi Person con un dato name_tag."""
    rows = db._query(
        "SELECT id, label, metadata FROM nodes WHERE type = 'Person' AND metadata LIKE %s",
        (f'%name_tag%"{name_tag}"%',),
    )
    # Filtraggio più preciso perché LIKE è approssimativo
    result = []
    for r in rows:
        meta = r.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                continue
        stored = meta.get("name_tag", "")
        if stored.lower() == name_tag.lower():
            result.append(r)
    return result


def get_photos_by_person_name(db, name_tag: str, reader=None) -> list[dict]:
    """Trova tutte le foto che contengono una persona con un dato name_tag.

    Args:
        db: MariaDBStore.
        name_tag: Nome da cercare.
        reader: PenelopeGraphReader opzionale (per query più ricche).

    Returns:
        Lista di foto con info sulla persona.
    """
    persons = get_persons_by_name(db, name_tag)
    if not persons:
        return []

    # Se abbiamo il reader di Archimede, usalo
    if reader:
        results = []
        for p in persons:
            photos = reader.get_all_photos(limit=5000)
            for photo in photos:
                persons_in = reader.get_persons_in_photo(photo.get("node_id", ""))
                if any(pp.get("id") == p["id"] for pp in persons_in):
                    results.append({
                        "photo": photo,
                        "person": p,
                    })
        return results

    # Altrimenti query diretta
    person_ids = [p["id"] for p in persons]
    if not person_ids:
        return []

    placeholders = ",".join(["%s"] * len(person_ids))
    rows = db._query(
        f"""SELECT f.path, f.device, f.mime_type, d.mount_root,
                   n.id as file_node_id,
                   p.id as person_id, p.label as person_label
            FROM edges e
            JOIN file_registry f ON f.node_id = e.source_id
            LEFT JOIN devices d ON d.id = f.device_id
            JOIN nodes n ON n.id = e.source_id
            JOIN nodes p ON p.id = e.target_id
            WHERE e.relation = 'CONTAINS'
              AND e.target_id IN ({placeholders})
              AND p.type = 'Person'
            ORDER BY f.path""",
        tuple(person_ids),
    )
    return rows


def get_all_name_tags(db) -> list[dict]:
    """Restituisce tutti i name_tag assegnati con conteggi."""
    rows = db._query(
        """SELECT id, label, metadata FROM nodes WHERE type = 'Person'
           AND metadata LIKE '%%name_tag%%'"""
    )
    tag_counts = {}
    for r in rows:
        meta = r.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                continue
        tag = meta.get("name_tag")
        if tag:
            if tag not in tag_counts:
                tag_counts[tag] = {
                    "name_tag": tag,
                    "count": 0,
                    "source": meta.get("name_tag_source", "unknown"),
                    "persons": [],
                }
            tag_counts[tag]["count"] += 1
            tag_counts[tag]["persons"].append({
                "id": r["id"],
                "label": r["label"],
                "confidence": meta.get("name_tag_confidence", 1.0),
            })

    return sorted(tag_counts.values(), key=lambda x: x["count"], reverse=True)


# ─── Propagazione ─────────────────────────────────────────────


def propagate_name_to_cluster(
    db,
    person_id: str,
    cluster_result: dict,
    name_tag: str,
    dry_run: bool = False,
) -> dict:
    """Propaga un name_tag a tutti i nodi nello stesso cluster.

    Data una persona già nominata, trova il suo cluster e assegna
    lo stesso name_tag a tutti gli altri membri.

    Args:
        db: MariaDBStore.
        person_id: ID del nodo Person già nominato.
        cluster_result: Output di cluster_persons().
        name_tag: Nome da propagare.
        dry_run: Se True, solo report, non modifica.

    Returns:
        {"propagated": int, "already_tagged": int, "cluster_id": int}
    """
    # Trova il cluster a cui appartiene person_id
    target_cluster = None
    for cluster in cluster_result.get("clusters", []):
        if any(p["id"] == person_id for p in cluster["persons"]):
            target_cluster = cluster
            break

    if target_cluster is None:
        logger.warning("Persona %s non trovata in nessun cluster", person_id[:8])
        return {"propagated": 0, "already_tagged": 0, "cluster_id": -1}

    propagated = 0
    already_tagged = 0

    for p in target_cluster["persons"]:
        if p["id"] == person_id:
            continue  # salta il nodo sorgente

        existing = get_name_tag(db, p["id"])
        if existing:
            already_tagged += 1
            if existing != name_tag:
                logger.warning(
                    "Conflitto: %s ha name_tag '%s', propagazione vuole '%s'",
                    p["id"][:8], existing, name_tag,
                )
            continue

        if not dry_run:
            set_name_tag(
                db, p["id"], name_tag,
                confidence=0.8,  # meno del manuale (1.0)
                source="propagation",
            )
        propagated += 1

    logger.info(
        "Propagazione '%s' a cluster %d: %d nuovi, %d già taggati",
        name_tag, target_cluster["cluster_id"], propagated, already_tagged,
    )

    return {
        "propagated": propagated,
        "already_tagged": already_tagged,
        "cluster_id": target_cluster["cluster_id"],
        "cluster_size": target_cluster["size"],
    }
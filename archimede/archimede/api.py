#!/usr/bin/env python3
"""
Archimede API — Interfaccia HTTP read-only per il grafo Penelope.

Espone le funzionalità di Archimede come servizio REST:
  - Query sul grafo (foto, persone, eventi, luoghi)
  - Ricerca semantica su ChromaDB
  - Face matching e ricerca foto di coppia
  - Statistiche del grafo

Avvio:
    python -m archimede.api [--port 8001] [--host 0.0.0.0]

Uso da Oracle (PenelopeBridge):
    GET  http://localhost:8001/archimede/stats
    GET  http://localhost:8001/archimede/search?q=angelo&top_k=10
    POST http://localhost:8001/archimede/find-parents
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

# ── Path bootstrap (same as archimede/__init__.py) ──────────────
_ORACLE_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
_ARCHIMEDE_ROOT = _ORACLE_ROOT_DIR / "archimede"
for _p in (str(_ORACLE_ROOT_DIR), str(_ARCHIMEDE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uvicorn
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from archimede.graph.reader import PenelopeGraphReader
from archimede.graph.chroma_reader import PenelopeChromaReader
from archimede.models import Photo
from archimede.agent import ArchimedeAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)
logger = logging.getLogger("archimede.api")

# ── App ──────────────────────────────────────────────────────────
app = FastAPI(
    title="Archimede API — Oracle Graph Reader",
    version="0.3.0",
    description="Interfaccia HTTP read-only per il grafo Penelope",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Schemas ──────────────────────────────────────────────────────

class FindParentsRequest(BaseModel):
    ref_dir: Optional[str] = None
    interactive: bool = False
    limit: int = 0
    directory: Optional[str] = None
    threshold: float = 0.35
    output: Optional[str] = None


class FindParentsResponse(BaseModel):
    photos_scanned: int
    photos_with_faces: int
    couple_count: int
    couple_photos: list[dict] = Field(default_factory=list)
    single_parent_photos: dict[str, list[dict]] = Field(default_factory=dict)
    duration_seconds: float
    generated_at: str
    report_html: Optional[str] = None


class SearchResponse(BaseModel):
    query: str
    results: list[dict] = Field(default_factory=list)
    total: int = 0


# ── Dependency: Graph Reader ────────────────────────────────────

def get_reader() -> PenelopeGraphReader:
    """Restituisce un reader connesso al grafo Penelope."""
    reader = PenelopeGraphReader()
    if not reader.connected:
        raise HTTPException(status_code=503, detail="Grafo Penelope non disponibile")
    return reader


def get_chroma() -> PenelopeChromaReader:
    """Restituisce un reader connesso alla ChromaDB di Penelope."""
    chroma = PenelopeChromaReader()
    return chroma


# ── Archimede Agent (istanza globale) ───────────────────────────
_archimede_agent: Optional[ArchimedeAgent] = None


def get_agent() -> ArchimedeAgent:
    """Restituisce l'istanza singleton del ArchimedeAgent."""
    global _archimede_agent
    if _archimede_agent is None:
        _archimede_agent = ArchimedeAgent()
    return _archimede_agent


# ── Health ───────────────────────────────────────────────────────

@app.get("/archimede/health")
async def health():
    """Health check dell'API Archimede."""
    reader = PenelopeGraphReader()
    connected = reader.connected
    reader.close()
    chroma = PenelopeChromaReader()
    collections = chroma.get_collections()
    return {
        "status": "ok" if connected else "degraded",
        "layer": "archimede",
        "graph_connected": connected,
        "chroma_collections": collections,
        "agent_ready": True,
    }


# ── Stats ───────────────────────────────────────────────────────

@app.get("/archimede/stats")
async def graph_stats():
    """Statistiche complete del grafo Penelope."""
    reader = get_reader()
    try:
        total_photos = reader.count_photos()
        persons = reader.get_person_nodes()
        insightface = [p for p in persons if "insightface" in (p.get("metadata") or "")]
        yolo = [p for p in persons if "face_detection" in (p.get("metadata") or "")]
        photos_with_faces = reader.get_photos_with_face_count()

        # Nodi per tipo
        node_types = reader._query(
            "SELECT type, COUNT(*) as cnt FROM nodes GROUP BY type ORDER BY cnt DESC"
        )
        edge_count = reader._query("SELECT COUNT(*) as cnt FROM edges")
        file_count = reader._query("SELECT COUNT(*) as cnt FROM file_registry")

        chroma = get_chroma()
        collections = chroma.get_collections()

        return {
            "total_photos": total_photos,
            "total_persons": len(persons),
            "insightface_persons": len(insightface),
            "yolo_persons": len(yolo),
            "photos_with_faces": len(photos_with_faces),
            "nodes_by_type": {r["type"]: r["cnt"] for r in node_types},
            "total_edges": edge_count[0]["cnt"] if edge_count else 0,
            "total_files": file_count[0]["cnt"] if file_count else 0,
            "chroma_collections": collections,
        }
    finally:
        reader.close()


# ── Search ──────────────────────────────────────────────────────

@app.get("/archimede/search")
async def semantic_search(
    q: str = Query(..., description="Testo di ricerca"),
    top_k: int = Query(10, description="Numero risultati", ge=1, le=100),
):
    """Ricerca semantica su ChromaDB Penelope (testo + immagini)."""
    reader = get_reader()
    try:
        # Usa la ChromaDB reader per ricerca
        # Per embedding testuali, usiamo il reader
        chroma = get_chroma()
        collections = chroma.get_collections()

        # Cerca nella collezione file_embeddings se esiste
        results = []
        if "file_embeddings" in collections or "image_embeddings" in collections:
            try:
                # Usiamo PenelopeChromaReader per query semantica
                # Poiché non ha search_similar, usiamo query_images per immagini
                # e per testo usiamo query diretta su ChromaDB
                import chromadb
                from chromadb.config import Settings

                # Trova il persist dir
                penelope_dir = (
                    Path(__file__).resolve().parent.parent.parent / "Penelope" / "data" / "chroma"
                )
                if penelope_dir.exists():
                    client = chromadb.PersistentClient(
                        path=str(penelope_dir),
                        settings=Settings(anonymized_telemetry=False),
                    )
                    # Prova embedding testuale con sentence-transformers
                    try:
                        from sentence_transformers import SentenceTransformer
                        embedder = SentenceTransformer("all-MiniLM-L6-v2")
                        q_emb = embedder.encode(q).tolist()

                        # Cerca in file_embeddings
                        try:
                            coll_text = client.get_collection("file_embeddings")
                            text_results = coll_text.query(
                                query_embeddings=[q_emb],
                                n_results=top_k,
                            )
                            for i in range(len(text_results["ids"][0])):
                                meta = text_results["metadatas"][0][i] if text_results["metadatas"] else {}
                                results.append({
                                    "node_id": text_results["ids"][0][i],
                                    "file_name": meta.get("file_name", ""),
                                    "mime_type": meta.get("mime_type", ""),
                                    "distance": text_results["distances"][0][i] if text_results["distances"] else 0,
                                    "snippet": text_results["documents"][0][i] if text_results["documents"] else "",
                                    "source": "text",
                                })
                        except Exception:
                            pass

                        # Cerca in image_embeddings
                        try:
                            coll_img = client.get_collection("image_embeddings")
                            img_results = coll_img.query(
                                query_embeddings=[q_emb],
                                n_results=top_k,
                            )
                            for i in range(len(img_results["ids"][0])):
                                meta = img_results["metadatas"][0][i] if img_results["metadatas"] else {}
                                results.append({
                                    "node_id": img_results["ids"][0][i],
                                    "file_name": meta.get("file_name", ""),
                                    "mime_type": "image/*",
                                    "distance": img_results["distances"][0][i] if img_results["distances"] else 0,
                                    "snippet": img_results["documents"][0][i] if img_results["documents"] else "[IMAGE]",
                                    "source": "image",
                                })
                        except Exception:
                            pass
                    except ImportError:
                        logger.warning("sentence-transformers non disponibile per embedding")
            except Exception as e:
                logger.error("Errore ricerca ChromaDB: %s", e)

        # Ordina per distanza
        results.sort(key=lambda x: x["distance"])
        results = results[:top_k]

        return SearchResponse(query=q, results=results, total=len(results))
    finally:
        reader.close()


# ── Persons ─────────────────────────────────────────────────────

@app.get("/archimede/persons")
async def list_persons(
    source: Optional[str] = Query(None, description="Filtro source (insightface, yolo)"),
    name_tag: Optional[str] = Query(None, description="Filtra per name_tag assegnato"),
    limit: int = Query(100, ge=1, le=1000),
):
    """Lista dei nodi Person nel grafo, con filtro opzionale per name_tag."""
    reader = get_reader()
    try:
        if name_tag:
            persons = reader._query(
                "SELECT id, label, metadata FROM nodes WHERE type = 'Person' AND metadata LIKE %s LIMIT %s",
                (f'%name_tag%"{name_tag}"%', limit),
            )
            # Filtraggio preciso
            import json as _json
            filtered = []
            for p in persons:
                meta = p.get("metadata") or {}
                if isinstance(meta, str):
                    try: meta = _json.loads(meta)
                    except: meta = {}
                if meta.get("name_tag", "").lower() == name_tag.lower():
                    filtered.append(p)
            return {"persons": filtered, "total": len(filtered), "name_tag": name_tag}
        
        persons = reader.get_person_nodes(source=source or "")[:limit]
        return {"persons": persons, "total": len(persons)}
    finally:
        reader.close()


# ── Name Tags ────────────────────────────────────────────────────

class NameTagRequest(BaseModel):
    person_id: str = Field(..., description="ID del nodo Person")
    name_tag: str = Field(..., description="Nome da assegnare")
    propagate: bool = Field(True, description="Propaga al cluster")

@app.get("/archimede/name-tags")
async def list_name_tags():
    """Lista di tutti i name_tag assegnati con conteggi."""
    from archimede.identity.name_tag import get_all_name_tags
    reader = get_reader()
    try:
        tags = get_all_name_tags(reader)
        # reader è un PenelopeGraphReader, ma le funzioni name_tag
        # vogliono un oggetto con _query — usiamo il db sottostante
        return {"name_tags": tags, "total": len(tags)}
    finally:
        reader.close()


@app.post("/archimede/name-tag")
async def set_person_name_tag(req: NameTagRequest):
    """Assegna un name_tag a un nodo Person."""
    from archimede.identity.name_tag import set_name_tag, cluster_persons, propagate_name_to_cluster
    
    # Usiamo il MariaDBStore diretto (reader è read-only per design)
    # ma set_name_tag richiede write. Usiamo un'istanza diretta.
    from penelope.db.mariadb_store import MariaDBStore as MDB
    db = MDB()
    try:
        ok = set_name_tag(db, req.person_id, req.name_tag)
        if not ok:
            raise HTTPException(status_code=404, detail="Persona non trovata")
        
        result = {"ok": True, "person_id": req.person_id, "name_tag": req.name_tag}
        
        if req.propagate:
            cr = cluster_persons(db, threshold=0.40)
            prop = propagate_name_to_cluster(db, req.person_id, cr, req.name_tag, dry_run=False)
            result["propagation"] = prop
        
        return result
    finally:
        db.close()


@app.get("/archimede/name-tag/{name_tag}")
async def get_photos_by_name(name_tag: str):
    """Trova tutte le foto che contengono una persona con un dato name_tag."""
    from archimede.identity.name_tag import get_photos_by_person_name
    from penelope.db.mariadb_store import MariaDBStore as MDB
    
    db = MDB()
    reader = get_reader()
    try:
        photos = get_photos_by_person_name(db, name_tag, reader)
        return {"name_tag": name_tag, "photos": photos, "count": len(photos)}
    finally:
        db.close()
        reader.close()


# ── Photos ──────────────────────────────────────────────────────

@app.get("/archimede/photos")
async def list_photos(
    person: Optional[str] = Query(None, description="Filtra foto contenenti una persona"),
    directory: Optional[str] = Query(None, description="Filtra per directory"),
    has_faces: bool = Query(False, description="Solo foto con volti"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Lista delle foto indicizzate."""
    reader = get_reader()
    try:
        if has_faces:
            photos = reader.get_photos_with_face_count()
        elif directory:
            photos = reader.get_photos_in_directory(directory)
        else:
            raw = reader.get_all_photos(limit=limit, offset=offset)
            photos = raw if raw else []

        # Se specificata persona, filtra per archi CONTAINS/APPEARS_IN
        if person and photos:
            filtered = []
            for p in photos:
                node_id = p.get("node_id", "")
                if not node_id:
                    continue
                persons_in_photo = reader.get_persons_in_photo(node_id)
                if any(person.lower() in (pp.get("label", "") or "").lower() for pp in persons_in_photo):
                    filtered.append(p)
            photos = filtered[:limit]

        return {"photos": photos, "total": len(photos)}
    finally:
        reader.close()


# ── Find Parents ────────────────────────────────────────────────

@app.post("/archimede/find-parents")
async def find_parents(req: FindParentsRequest):
    """Trova foto di coppia dei genitori tramite face matching.

    Richiede una directory con foto di referenza strutturata:
        ref_faces/papa/   (foto del papà)
        ref_faces/mamma/  (foto della mamma)
    """
    from archimede.identity.matcher import load_reference_faces, search_couple_photos
    from archimede.presentation.report import generate_report
    from archimede.identity.face_engine import detect_faces

    t_start = time.time()

    # Fase 1: Carica referenze
    if req.ref_dir:
        ref_dir = Path(req.ref_dir)
        if not ref_dir.exists():
            raise HTTPException(status_code=400, detail=f"Directory referenza non trovata: {ref_dir}")
        references = load_reference_faces(str(ref_dir))
    else:
        raise HTTPException(status_code=400, detail="Specificare ref_dir")

    if not references:
        raise HTTPException(status_code=400, detail="Nessuna referenza valida caricata")

    ref_names = [r.name for r in references]
    logger.info("Referenze caricate: %s", ", ".join(ref_names))

    # Fase 2: Carica foto dal grafo
    reader = get_reader()
    try:
        if req.directory:
            raw_photos = reader.get_photos_in_directory(req.directory)
        else:
            raw_photos = reader.get_all_photos(limit=req.limit or 0)

        if not raw_photos:
            raise HTTPException(status_code=404, detail="Nessuna foto trovata nel database")

        photos = []
        for r in raw_photos:
            meta = r.get("node_metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            photos.append(Photo(
                node_id=r.get("node_id", ""),
                file_path=r.get("path", ""),
                file_name=Path(r.get("path", "")).name,
                mime_type=r.get("mime_type", ""),
                size_bytes=r.get("size_bytes", 0) or 0,
                sha256=r.get("sha256", ""),
                device=r.get("device", ""),
                face_count=meta.get("face_count", 0) if isinstance(meta, dict) else 0,
                metadata=meta if isinstance(meta, dict) else {},
            ))
    finally:
        reader.close()

    # Fase 3: Esegui matching
    def progress(i, total, couple_count):
        if i % 50 == 0 or i == total:
            logger.info("Matching: %d/%d, coppie trovate: %d", i, total, couple_count)

    report = search_couple_photos(
        photos, references,
        threshold=req.threshold,
        batch_callback=progress,
    )

    # Fase 4: Genera report HTML
    safe_time = report.generated_at.replace(":", "-").replace(" ", "_")
    output_file = req.output or f"data/results/parents_photos_{safe_time}.html"
    html_path = generate_report(report, output_file)

    # Fase 5: Prepara risposta strutturata
    couple_data = []
    for r in report.couple_photos[:20]:  # max 20 in risposta
        couple_data.append({
            "file_name": r.photo.file_name,
            "file_path": r.photo.file_path,
            "node_id": r.photo.node_id,
            "matches": r.matches,
            "face_count": r.photo.face_count,
        })

    single_data = {}
    for name in ref_names:
        single_data[name] = [
            {"file_name": r.photo.file_name, "file_path": r.photo.file_path}
            for r in report.single_parent_photos.get(name, [])[:20]
        ]

    return FindParentsResponse(
        photos_scanned=report.photos_scanned,
        photos_with_faces=report.photos_with_faces,
        couple_count=report.couple_count,
        couple_photos=couple_data,
        single_parent_photos=single_data,
        duration_seconds=round(time.time() - t_start, 2),
        generated_at=report.generated_at,
        report_html=str(html_path),
    )


# ── Query generica (per OracleOrchestrator) ────────────────────

class GraphQueryRequest(BaseModel):
    query: str = Field(..., description="Query in linguaggio naturale o SQL SELECT")
    params: dict = Field(default_factory=dict, description="Parametri per la query")


@app.post("/archimede/query")
async def graph_query(req: GraphQueryRequest):
    """Esegue una query sul grafo Penelope.

    Accetta sia query in linguaggio naturale (tramite pattern matching interno)
    sia query SELECT SQL dirette.

    Usato internamente da OracleOrchestrator e PenelopeBridge.
    """
    reader = get_reader()
    try:
        q = req.query.strip().upper()

        # Query SQL diretta (solo SELECT)
        if q.startswith("SELECT") or q.startswith("WITH"):
            try:
                result = reader._query(req.query, tuple(req.params.values()))
                return {"result": result, "count": len(result)}
            except RuntimeError as e:
                raise HTTPException(status_code=403, detail=str(e))

        # Query natural language → mappa a query strutturate
        q_lower = req.query.lower()

        if "foto" in q_lower and "person" in q_lower:
            # "foto di [persona]"
            import re
            match = re.search(r'(?:foto|photo|fotografia|immagine)\s+(?:di|con|del|della|delle?|dei?)\s+(.+?)$', req.query, re.IGNORECASE)
            if match:
                person_name = match.group(1).strip()
                photos = reader.get_all_photos(limit=200)
                filtered = []
                for p in photos:
                    persons = reader.get_persons_in_photo(p.get("node_id", ""))
                    if any(person_name.lower() in (pp.get("label", "") or "").lower() for pp in persons):
                        filtered.append(p)
                return {"query_type": "person_photos", "person": person_name, "photos": filtered, "count": len(filtered)}

        elif "stat" in q_lower or "conteggio" in q_lower or "quante" in q_lower or "quanti" in q_lower:
            stats = {
                "total_photos": reader.count_photos(),
                "total_persons": len(reader.get_person_nodes()),
                "photos_with_faces": len(reader.get_photos_with_face_count()),
            }
            return {"query_type": "stats", "stats": stats}

        elif "person" in q_lower or "persone" in q_lower:
            persons = reader.get_person_nodes()
            return {"query_type": "persons", "persons": persons, "count": len(persons)}

        elif "event" in q_lower:
            events = reader._query(
                "SELECT id, label, metadata, created_at FROM nodes WHERE type = 'Event' ORDER BY created_at DESC LIMIT 100"
            )
            return {"query_type": "events", "events": events, "count": len(events)}

        elif "luogo" in q_lower or "location" in q_lower or "dove" in q_lower:
            locations = reader._query(
                "SELECT id, label, metadata, created_at FROM nodes WHERE type = 'Location' ORDER BY label ASC LIMIT 100"
            )
            return {"query_type": "locations", "locations": locations, "count": len(locations)}

        else:
            # Fallback: cerca nodi per label
            nodes = reader._query(
                "SELECT id, type, label, metadata FROM nodes WHERE label LIKE %s LIMIT 50",
                (f"%{req.query}%",),
            )
            return {"query_type": "search", "nodes": nodes, "count": len(nodes)}

    finally:
        reader.close()


# ── Eventi ──────────────────────────────────────────────────────

@app.get("/archimede/events")
async def list_events(limit: int = Query(50, ge=1, le=500)):
    """Lista degli eventi nel grafo."""
    reader = get_reader()
    try:
        events = reader._query(
            "SELECT id, label, metadata, created_at FROM nodes WHERE type = 'Event' ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return {"events": events, "total": len(events)}
    finally:
        reader.close()


# ── Location ────────────────────────────────────────────────────

@app.get("/archimede/locations")
async def list_locations(limit: int = Query(50, ge=1, le=500)):
    """Lista dei luoghi nel grafo."""
    reader = get_reader()
    try:
        locations = reader._query(
            "SELECT id, label, metadata, created_at FROM nodes WHERE type = 'Location' ORDER BY label ASC LIMIT %s",
            (limit,),
        )
        return {"locations": locations, "total": len(locations)}
    finally:
        reader.close()


# ── Photo dettaglio ─────────────────────────────────────────────

@app.get("/archimede/photos/{node_id}")
async def get_photo_detail(node_id: str):
    """Dettaglio di una foto con archi e persone collegate."""
    reader = get_reader()
    try:
        photos = reader._query(
            """SELECT n.id as node_id, n.label, n.metadata as node_metadata,
                      f.path, f.device, f.size_bytes, f.sha256, f.mime_type,
                      d.mount_root
               FROM nodes n
               JOIN file_registry f ON f.node_id = n.id
               LEFT JOIN devices d ON d.id = f.device_id
               WHERE n.id = %s""",
            (node_id,),
        )
        if not photos:
            raise HTTPException(status_code=404, detail="Foto non trovata")

        photo = photos[0]
        # Risolve path assoluto
        from pathlib import Path as _Path
        raw_path = photo.get("path", "")
        mount_root = photo.get("mount_root")
        if mount_root and not (raw_path.startswith("/") or raw_path.startswith("\\") or (len(raw_path) > 1 and raw_path[1] == ":")):
            photo["path"] = str(_Path(mount_root) / raw_path)
        edges = reader.get_edges_for_photo(node_id)
        persons = reader.get_persons_in_photo(node_id)

        return {
            "photo": photo,
            "edges": edges,
            "persons": persons,
        }
    finally:
        reader.close()


# ── Chat con l'agente ───────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., description="Richiesta in linguaggio naturale")
    session_id: Optional[str] = Field(None, description="ID sessione (opzionale)")


@app.post("/archimede/chat")
async def chat(req: ChatRequest):
    """Chat con l'agente Archimede via linguaggio naturale.

    ⚠️ Archimede NON ha un LLM interno. Questo endpoint usa un
    leggero pattern matching per rispondere a query semplici.
    Per funzionalità complete (NL → intent → tool → risposta),
    usa l'orchestratore Oracle su http://localhost:8100/api/chat.
    """
    agent = get_agent()
    try:
        content = agent.chat(req.message)
        return {
            "content": content,
            "layer": "archimede",
            "session_id": req.session_id,
            "note": "Risposta generata con pattern matching (Archimede non ha LLM). "
                     "Per NL completo usa l'orchestratore Oracle.",
        }
    except Exception as e:
        logger.error("Archimede agent error: %s", e)
        return {
            "content": "Si è verificato un errore durante l'elaborazione. Riprova.",
            "layer": "archimede",
            "error": str(e),
        }


# ── Main ────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Archimede API Server")
    parser.add_argument("--port", type=int, default=int(os.getenv("ARCHIMEDE_PORT", "8001")),
                        help="Porta (default: 8001)")
    parser.add_argument("--host", type=str, default=os.getenv("ARCHIMEDE_HOST", "0.0.0.0"),
                        help="Host (default: 0.0.0.0)")
    args = parser.parse_args()

    print(f"  [Archimede API] http://{args.host}:{args.port}/archimede/health")
    print(f"  [Archimede API] http://{args.host}:{args.port}/archimede/stats")
    print(f"  [Archimede API] http://{args.host}:{args.port}/archimede/search?q=...")
    print(f"  [Archimede API] http://{args.host}:{args.port}/docs (Swagger)")

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

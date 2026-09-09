#!/usr/bin/env python3
"""
Penelope Bridge — Tool per Oracle per leggere il grafo Penelope.

Permette all'agente Oracle di interrogare il grafo Penelope (foto, persone,
eventi, luoghi) e la ChromaDB (ricerca semantica) attraverso Archimede.

Due modalità:
  1. Diretta (default): importa i reader di Archimede e interroga MariaDB/ChromaDB
  2. HTTP: chiama la ArchimedeAPI via HTTP (utile se Archimede gira su un altro host)

Usage:
    python tools/penelope_bridge.py stats
    python tools/penelope_bridge.py search --query "angelo" --top 10
    python tools/penelope_bridge.py persons
    python tools/penelope_bridge.py photos --limit 20
    python tools/penelope_bridge.py query --sql "SELECT COUNT(*) FROM nodes"

Python (da Oracle agent):
    from tools.penelope_bridge import PenelopeBridge
    bridge = PenelopeBridge()
    stats = bridge.get_stats()
    photos = bridge.get_person_photos("Angela")
    results = bridge.semantic_search("foto di famiglia")
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("penelope_bridge")

# ── Path bootstrap ──────────────────────────────────────────────
# Needed to import Archimede's readers when using direct mode
_ORACLE_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
_ARCHIMEDE_ROOT = _ORACLE_ROOT_DIR / "archimede"
for _p in (str(_ORACLE_ROOT_DIR), str(_ARCHIMEDE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Config ──────────────────────────────────────────────────────
ARCHIMEDE_API_URL = os.getenv("ARCHIMEDE_API_URL", "http://localhost:8001")
USE_HTTP_MODE = os.getenv("PENELOPE_BRIDGE_MODE", "direct") == "http"


class PenelopeBridge:
    """Bridge per leggere dati dal grafo Penelope tramite Archimede.

    Fornisce metodi structured per i casi d'uso più comuni degli agent Oracle.
    """

    def __init__(self, mode: str = "auto"):
        """
        Args:
            mode: 'direct' → importa Archimede readers direttamente
                  'http'   → chiama ArchimedeAPI via HTTP
                  'auto'   → prova direct, fallback a http
        """
        self.mode = mode
        self._reader = None
        self._chroma = None

    # ── Initialization ──────────────────────────────────────────

    def _ensure_reader(self):
        """Inizializza il reader Archimede (lazy)."""
        if self._reader is not None:
            return self._reader

        if self.mode == "http":
            return None  # HTTP mode non usa reader diretto

        try:
            from archimede.graph.reader import PenelopeGraphReader
            self._reader = PenelopeGraphReader()
            if self._reader.connected:
                logger.info("PenelopeBridge: connesso a MariaDB via Archimede")
                return self._reader
            else:
                logger.warning("PenelopeBridge: reader non connesso")
                self._reader = None
        except Exception as e:
            logger.warning("PenelopeBridge: impossibile caricare reader: %s", e)
            self._reader = None

        # Fallback a HTTP
        if self._reader is None and self.mode == "auto":
            self.mode = "http"
            logger.info("PenelopeBridge: fallback a modalità HTTP")
        return self._reader

    def _ensure_chroma(self):
        """Inizializza il Chroma reader (lazy)."""
        if self._chroma is not None:
            return self._chroma

        if self.mode == "http":
            return None

        try:
            from archimede.graph.chroma_reader import PenelopeChromaReader
            self._chroma = PenelopeChromaReader()
            logger.info("PenelopeBridge: connesso a ChromaDB")
            return self._chroma
        except Exception as e:
            logger.warning("PenelopeBridge: impossibile caricare chroma reader: %s", e)
            self._chroma = None
        return self._chroma

    def _http_get(self, path: str, params: dict = None) -> dict:
        """Chiamata HTTP alla ArchimedeAPI."""
        import httpx
        url = f"{ARCHIMEDE_API_URL.rstrip('/')}{path}"
        try:
            resp = httpx.get(url, params=params, timeout=30.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.RequestError as e:
            logger.error("PenelopeBridge: errore HTTP %s: %s", url, e)
            return {"error": str(e), "result": []}

    def _http_post(self, path: str, data: dict = None) -> dict:
        """Chiamata HTTP POST alla ArchimedeAPI."""
        import httpx
        url = f"{ARCHIMEDE_API_URL.rstrip('/')}{path}"
        try:
            resp = httpx.post(url, json=data, timeout=120.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.RequestError as e:
            logger.error("PenelopeBridge: errore HTTP POST %s: %s", url, e)
            return {"error": str(e), "result": []}

    # ── Public API ──────────────────────────────────────────────

    def is_available(self) -> bool:
        """Verifica se il grafo Penelope è raggiungibile."""
        if self.mode == "http":
            result = self._http_get("/archimede/health")
            return result.get("status") == "ok"

        reader = self._ensure_reader()
        if reader and reader.connected:
            return True

        # Fallback a HTTP check
        result = self._http_get("/archimede/health")
        return result.get("status") == "ok"

    def get_stats(self) -> dict:
        """Statistiche del grafo Penelope."""
        if self.mode == "http":
            return self._http_get("/archimede/stats")

        reader = self._ensure_reader()
        if not reader:
            return {"error": "Grafo Penelope non disponibile"}

        try:
            total_photos = reader.count_photos()
            persons = reader.get_person_nodes()
            photos_with_faces = reader.get_photos_with_face_count()

            node_types = reader._query(
                "SELECT type, COUNT(*) as cnt FROM nodes GROUP BY type ORDER BY cnt DESC"
            )
            edge_count = reader._query("SELECT COUNT(*) as cnt FROM edges")
            file_count = reader._query("SELECT COUNT(*) as cnt FROM file_registry")

            return {
                "total_photos": total_photos,
                "total_persons": len(persons),
                "photos_with_faces": len(photos_with_faces),
                "nodes_by_type": {r["type"]: r["cnt"] for r in node_types},
                "total_edges": edge_count[0]["cnt"] if edge_count else 0,
                "total_files": file_count[0]["cnt"] if file_count else 0,
            }
        except Exception as e:
            logger.error("Errore get_stats: %s", e)
            return {"error": str(e)}
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None

    def semantic_search(self, query: str, top_k: int = 10) -> list[dict]:
        """Ricerca semantica su ChromaDB Penelope.

        Args:
            query: Testo di ricerca.
            top_k: Numero massimo risultati.

        Returns:
            Lista di risultati con node_id, file_name, distance, snippet.
        """
        if self.mode == "http":
            result = self._http_get("/archimede/search", {"q": query, "top_k": top_k})
            return result.get("results", [])

        self._ensure_chroma()
        if not self._chroma:
            logger.warning("ChromaDB non disponibile, uso fallback SQL")
            return self._fallback_search(query, top_k)

        try:
            # Usa sentence-transformers per embedding query
            from sentence_transformers import SentenceTransformer
            embedder = SentenceTransformer("all-MiniLM-L6-v2")
            q_emb = embedder.encode(query).tolist()

            results = []
            collections = self._chroma.get_collections()

            # Cerca in file_embeddings
            if "file_embeddings" in collections:
                import chromadb
                from chromadb.config import Settings

                # Cerca penelope/ o Penelope/ (case-insensitive su Windows)
                root = Path(__file__).resolve().parent.parent.parent
                penelope_dir = root / "penelope" / "data" / "chroma"
                if not penelope_dir.exists():
                    penelope_dir = root / "Penelope" / "data" / "chroma"
                if not penelope_dir.exists():
                    # Prova path da env
                    env_path = os.getenv("ARCHIMEDE_PENELOPE_PATH") or os.getenv("PENELOPE_CHROMA_PATH")
                    if env_path:
                        penelope_dir = Path(env_path)
                if penelope_dir.exists():
                    client = chromadb.PersistentClient(
                        path=str(penelope_dir),
                        settings=Settings(anonymized_telemetry=False),
                    )
                    try:
                        coll = client.get_collection("file_embeddings")
                        text_results = coll.query(
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

            results.sort(key=lambda x: x["distance"])
            return results[:top_k]

        except ImportError:
            logger.warning("sentence-transformers non disponibile, uso fallback SQL")
            return self._fallback_search(query, top_k)
        except Exception as e:
            logger.error("Errore semantic_search: %s", e)
            return self._fallback_search(query, top_k)

    def _fallback_search(self, query: str, top_k: int = 10) -> list[dict]:
        """Fallback: ricerca testuale LIKE su MariaDB."""
        reader = self._ensure_reader()
        if not reader:
            return []
        try:
            nodes = reader._query(
                """SELECT n.id as node_id, n.label, n.type, n.metadata,
                          f.path, f.mime_type, d.mount_root
                   FROM nodes n
                   LEFT JOIN file_registry f ON f.node_id = n.id
                   LEFT JOIN devices d ON d.id = f.device_id
                   WHERE n.label LIKE %s
                      OR (f.path LIKE %s)
                   LIMIT %s""",
                (f"%{query}%", f"%{query}%", top_k),
            )

            def _resolve(n_path, mount_root):
                if mount_root and n_path and not (n_path.startswith("/") or n_path.startswith("\\") or (len(n_path) > 1 and n_path[1] == ":")):
                    return str(Path(mount_root) / n_path)
                return n_path

            return [
                {
                    "node_id": n.get("node_id", ""),
                    "file_name": Path(_resolve(n.get("path", ""), n.get("mount_root"))).name if n.get("path") else n.get("label", ""),
                    "mime_type": n.get("mime_type", ""),
                    "distance": 0,
                    "snippet": n.get("label", ""),
                    "source": "sql_fallback",
                }
                for n in nodes
            ]
        except Exception as e:
            logger.error("Errore fallback_search: %s", e)
            return []
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None

    def get_person_photos(self, person_name: str, limit: int = 100) -> list[dict]:
        """Trova foto che contengono una persona specifica.

        Args:
            person_name: Nome della persona (o parte di esso).
            limit: Numero massimo foto.

        Returns:
            Lista di foto con metadati.
        """
        if self.mode == "http":
            return self._http_get("/archimede/photos", {"person": person_name, "limit": limit})

        reader = self._ensure_reader()
        if not reader:
            return []

        try:
            photos = reader.get_all_photos(limit=limit)
            filtered = []
            for p in photos:
                node_id = p.get("node_id", "")
                if not node_id:
                    continue
                persons = reader.get_persons_in_photo(node_id)
                if any(person_name.lower() in (pp.get("label", "") or "").lower() for pp in persons):
                    filtered.append(p)
            return filtered
        except Exception as e:
            logger.error("Errore get_person_photos: %s", e)
            return []
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None

    def get_persons(self, source: str = "") -> list[dict]:
        """Lista dei nodi Person nel grafo."""
        if self.mode == "http":
            result = self._http_get("/archimede/persons", {"source": source})
            return result.get("persons", [])

        reader = self._ensure_reader()
        if not reader:
            return []
        try:
            return reader.get_person_nodes(source=source)
        except Exception as e:
            logger.error("Errore get_persons: %s", e)
            return []
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None

    def get_events(self, limit: int = 50) -> list[dict]:
        """Lista degli eventi nel grafo."""
        if self.mode == "http":
            result = self._http_get("/archimede/events", {"limit": limit})
            return result.get("events", [])

        reader = self._ensure_reader()
        if not reader:
            return []
        try:
            return reader._query(
                "SELECT id, label, metadata, created_at FROM nodes WHERE type = 'Event' ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
        except Exception as e:
            logger.error("Errore get_events: %s", e)
            return []
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None

    def get_locations(self, limit: int = 50) -> list[dict]:
        """Lista dei luoghi nel grafo."""
        if self.mode == "http":
            result = self._http_get("/archimede/locations", {"limit": limit})
            return result.get("locations", [])

        reader = self._ensure_reader()
        if not reader:
            return []
        try:
            return reader._query(
                "SELECT id, label, metadata, created_at FROM nodes WHERE type = 'Location' ORDER BY label ASC LIMIT %s",
                (limit,),
            )
        except Exception as e:
            logger.error("Errore get_locations: %s", e)
            return []
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None

    def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Esegue una query SELECT arbitraria sul grafo.

        ATTENZIONE: solo query SELECT. Qualunque altra operazione viene bloccata.

        Args:
            sql: Query SELECT SQL.
            params: Parametri della query.

        Returns:
            Lista di righe come dict.
        """
        if self.mode == "http":
            result = self._http_post("/archimede/query", {"query": sql, "params": {}})
            return result.get("result", [])

        reader = self._ensure_reader()
        if not reader:
            return []
        try:
            return reader._query(sql, params)
        except RuntimeError as e:
            logger.error("Query bloccata: %s", e)
            return [{"error": str(e)}]
        except Exception as e:
            logger.error("Errore execute_query: %s", e)
            return []
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None

    def natural_query(self, query: str) -> dict:
        """Query in linguaggio naturale sul grafo.

        Esempi:
            - "quante foto ci sono?"
            - "foto di Angela"
            - "lista delle persone"
            - "eventi del 2024"

        Args:
            query: Domanda in linguaggio naturale.

        Returns:
            Dict con risultati strutturati.
        """
        if self.mode == "http":
            return self._http_post("/archimede/query", {"query": query})

        reader = self._ensure_reader()
        if not reader:
            return {"error": "Grafo non disponibile"}

        try:
            q = query.lower()

            if any(k in q for k in ("foto", "photo", "fotografia", "immagine")):
                import re
                match = re.search(
                    r'(?:foto|photo|fotografia|immagine)\s+(?:di|con|del|della|delle?|dei?)\s+(.+?)$',
                    query, re.IGNORECASE
                )
                if match:
                    person_name = match.group(1).strip()
                    photos = reader.get_all_photos(limit=200)
                    filtered = []
                    for p in photos:
                        persons = reader.get_persons_in_photo(p.get("node_id", ""))
                        if any(person_name.lower() in (pp.get("label", "") or "").lower() for pp in persons):
                            filtered.append(p)
                    return {"query_type": "person_photos", "person": person_name, "photos": filtered, "count": len(filtered)}

                # "quante foto" → stats
                return {
                    "query_type": "stats",
                    "stats": {
                        "total_photos": reader.count_photos(),
                        "total_persons": len(reader.get_person_nodes()),
                        "photos_with_faces": len(reader.get_photos_with_face_count()),
                    }
                }

            if any(k in q for k in ("stat", "conteggio", "quante", "quanti")):
                return {
                    "query_type": "stats",
                    "stats": {
                        "total_photos": reader.count_photos(),
                        "total_persons": len(reader.get_person_nodes()),
                        "photos_with_faces": len(reader.get_photos_with_face_count()),
                    }
                }

            if any(k in q for k in ("person", "persone", "gente")):
                persons = reader.get_person_nodes()
                return {"query_type": "persons", "persons": persons, "count": len(persons)}

            if any(k in q for k in ("event", "accaduto", "successo")):
                events = reader._query(
                    "SELECT id, label, metadata, created_at FROM nodes WHERE type = 'Event' ORDER BY created_at DESC LIMIT 100"
                )
                return {"query_type": "events", "events": events, "count": len(events)}

            if any(k in q for k in ("luogo", "location", "dove", "posto")):
                locations = reader._query(
                    "SELECT id, label, metadata, created_at FROM nodes WHERE type = 'Location' ORDER BY label ASC LIMIT 100"
                )
                return {"query_type": "locations", "locations": locations, "count": len(locations)}

            # Fallback: ricerca full-text su label
            nodes = reader._query(
                "SELECT id, type, label, metadata FROM nodes WHERE label LIKE %s LIMIT 50",
                (f"%{query}%",),
            )
            return {"query_type": "search", "nodes": nodes, "count": len(nodes)}

        except Exception as e:
            logger.error("Errore natural_query: %s", e)
            return {"error": str(e)}
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None

    def find_parent_photos(self, ref_dir: str, threshold: float = 0.35, limit: int = 0) -> dict:
        """Trova foto di coppia dei genitori tramite face matching.

        Args:
            ref_dir: Directory con foto di referenza (sottocartelle papa/, mamma/).
            threshold: Soglia similarità coseno (default: 0.35).
            limit: Numero massimo foto da scandire (0 = tutte).

        Returns:
            Dict con risultati (couple_photos, single_parent_photos, stats).
        """
        if self.mode == "http":
            return self._http_post("/archimede/find-parents", {
                "ref_dir": ref_dir,
                "threshold": threshold,
                "limit": limit,
            })

        from archimede.identity.matcher import load_reference_faces, search_couple_photos
        from archimede.identity.face_engine import detect_faces
        from archimede.models import Photo

        ref_path = Path(ref_dir)
        if not ref_path.exists():
            return {"error": f"Directory referenza non trovata: {ref_dir}"}

        references = load_reference_faces(str(ref_path))
        if not references:
            return {"error": "Nessuna referenza valida caricata"}

        reader = self._ensure_reader()
        if not reader:
            return {"error": "Grafo Penelope non disponibile"}

        try:
            raw_photos = reader.get_all_photos(limit=limit or 0)
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
                    face_count=meta.get("face_count", 0) if isinstance(meta, dict) else 0,
                ))

            import time
            t_start = time.time()
            report = search_couple_photos(photos, references, threshold=threshold)
            duration = time.time() - t_start

            return {
                "photos_scanned": report.photos_scanned,
                "photos_with_faces": report.photos_with_faces,
                "couple_count": report.couple_count,
                "couple_photos": [
                    {"file_name": r.photo.file_name, "file_path": r.photo.file_path}
                    for r in report.couple_photos[:20]
                ],
                "duration_seconds": round(duration, 2),
            }
        except Exception as e:
            logger.error("Errore find_parent_photos: %s", e)
            return {"error": str(e)}
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Penelope Bridge — Legge il grafo Penelope")
    sub = parser.add_subparsers(dest="command", required=True)

    # stats
    sub.add_parser("stats", help="Statistiche del grafo")

    # search
    p_search = sub.add_parser("search", help="Ricerca semantica")
    p_search.add_argument("--query", "-q", required=True)
    p_search.add_argument("--top", type=int, default=10)

    # persons
    sub.add_parser("persons", help="Lista persone")

    # photos
    p_photos = sub.add_parser("photos", help="Lista foto")
    p_photos.add_argument("--person", "-p", default=None, help="Filtra per persona")
    p_photos.add_argument("--limit", type=int, default=50)

    # events
    p_events = sub.add_parser("events", help="Lista eventi")
    p_events.add_argument("--limit", type=int, default=50)

    # locations
    p_locations = sub.add_parser("locations", help="Lista luoghi")
    p_locations.add_argument("--limit", type=int, default=50)

    # query (SQL)
    p_query = sub.add_parser("query", help="Query SELECT SQL")
    p_query.add_argument("--sql", required=True, help="Query SELECT")

    # natural query
    p_nl = sub.add_parser("ask", help="Query in linguaggio naturale")
    p_nl.add_argument("query", help='Es: "quante foto di Angela?"')

    # find-parents
    p_fp = sub.add_parser("find-parents", help="Trova foto coppia genitori")
    p_fp.add_argument("--ref-dir", required=True, help="Directory con foto referenza")
    p_fp.add_argument("--threshold", type=float, default=0.35)
    p_fp.add_argument("--limit", type=int, default=0)

    args = parser.parse_args()
    bridge = PenelopeBridge()

    if args.command == "stats":
        result = bridge.get_stats()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "search":
        result = bridge.semantic_search(args.query, args.top)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "persons":
        result = bridge.get_persons()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "photos":
        if args.person:
            result = bridge.get_person_photos(args.person, args.limit)
        else:
            result = bridge.execute_query(
                "SELECT n.id, n.label, f.path, d.mount_root FROM nodes n "
                "JOIN file_registry f ON f.node_id = n.id "
                "LEFT JOIN devices d ON d.id = f.device_id "
                "WHERE n.type = 'File' AND (f.path LIKE '%.jpg' OR f.path LIKE '%.jpeg' OR f.path LIKE '%.png') "
                "LIMIT %s",
                (args.limit,),
            )
        # Risolve path relativi (mount_root + path) per risultati
        if result and isinstance(result, list):
            from pathlib import Path as _Path
            for r in result:
                p = r.get("path", "")
                mr = r.get("mount_root")
                if mr and p and not (p.startswith("/") or p.startswith("\\") or (len(p) > 1 and p[1] == ":")):
                    r["path"] = str(_Path(mr) / p)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "events":
        result = bridge.get_events(args.limit)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "locations":
        result = bridge.get_locations(args.limit)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "query":
        result = bridge.execute_query(args.sql)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "ask":
        result = bridge.natural_query(args.query)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "find-parents":
        result = bridge.find_parent_photos(args.ref_dir, args.threshold, args.limit)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

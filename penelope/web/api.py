"""
Penelope Web API — Interfaccia grafica per esplorare il grafo.

Avvio:
    python web/api.py

Poi apri http://localhost:5000 nel browser.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Evita che SentenceTransformer/Transformers contattino HuggingFace Hub (offline)
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

# Aggiunge la radice del progetto al path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, request, render_template, send_from_directory
from flask_cors import CORS

import threading

from penelope.db.mariadb_store import MariaDBStore
from penelope.db.graph_bridge import GraphBridge
from penelope.db.chroma_store import ChromaStore
from penelope.config import settings


# ─── Helper: risolve path assoluto (mount_root + path relativo) ───

def _resolve_path(raw_path: str, mount_root: Optional[str] = None) -> str:
    """Ricostruisce path assoluto.

    Se mount_root noto e path non assoluto, combina mount_root + path.
    Altrimenti restituisce path inalterato (backward compat).
    """
    if not mount_root:
        return raw_path
    if raw_path.startswith("/") or raw_path.startswith("\\") or (len(raw_path) > 1 and raw_path[1] == ":"):
        return raw_path  # gia\' assoluto
    return str(Path(mount_root) / raw_path)


# Thread-local storage per connessioni MySQL (evita race condition su Flask multi-thread)
_tls = threading.local()


# ─── Cursore normalizzato (MariaDB DictCursor su Linux = uppercase) ───

class _NormalizedCursor:
    """Wrapper di cursore che normalizza le chiavi dei risultati in lowercase."""
    def __init__(self, cursor):
        self._cur = cursor
    def execute(self, sql, params=None):
        return self._cur.execute(sql, params)
    def fetchone(self):
        row = self._cur.fetchone()
        return _nk(row) if row else None
    def fetchall(self):
        return [_nk(r) for r in self._cur.fetchall()]
    def __getattr__(self, name):
        return getattr(self._cur, name)


def _get_cursor():
    """Restituisce un cursore MariaDB con risultati normalizzati (chiavi lowercase)."""
    if not hasattr(_tls, 'conn'):
        _tls.conn = MariaDBStore()
        _tls.conn.connect()
    return _NormalizedCursor(_tls.conn._conn.cursor())


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("penelope-web")

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)

# ─── Database connection (creata una volta e riusata) ──────────────

# Helper: normalizza chiavi dict (MariaDB DictCursor su Linux = uppercase)
def _nk(d):
    if d is None:
        return {}
    return {k.lower(): v for k, v in d.items()}


_db = MariaDBStore()
bridge = GraphBridge(_db)


# ─── Pagina principale ─────────────────────────────────────────────


@app.route("/")
def index():
    return jsonify({
        "service": "Penelope Graph API",
        "docs": "Use the CHORA UI at http://localhost:8100",
        "endpoints": {
            "stats": "/api/stats",
            "nodes": "/api/nodes",
            "graph": "/api/graph",
            "tree": "/api/tree",
            "search": "/api/search",
            "faces": "/api/faces",
            "objects": "/api/objects",
            "projects": "/api/projects",
            "events": "/api/events",
            "timeline": "/api/events/timeline",
            "locations": "/api/locations",
        }
    })


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


# ─── API: Stats ────────────────────────────────────────────────────


@app.route("/api/stats")
def api_stats():
    cur = _get_cursor()
    try:
            cur.execute("SELECT type, COUNT(*) as cnt FROM nodes GROUP BY type ORDER BY cnt DESC")
            nodes_by_type = {r["type"]: r["cnt"] for r in [_nk(r) for r in cur.fetchall()]}

            cur.execute("SELECT relation, COUNT(*) as cnt FROM edges GROUP BY relation ORDER BY cnt DESC")
            edges_by_rel = {r["relation"]: r["cnt"] for r in [_nk(r) for r in cur.fetchall()]}

            cur.execute("SELECT COUNT(*) AS cnt FROM nodes")
            total_nodes = _nk(cur.fetchone())["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM edges")
            total_edges = _nk(cur.fetchone())["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM file_registry")
            total_files = _nk(cur.fetchone())["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM ingestion_queue WHERE status='done'")
            queue_done = _nk(cur.fetchone())["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM nodes WHERE type='File' AND metadata LIKE '%has_faces%'")
            with_faces = _nk(cur.fetchone())["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM nodes WHERE type='Person' AND metadata LIKE '%embedding_dim%'")
            with_embeddings = _nk(cur.fetchone())["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM nodes WHERE type='File' AND metadata LIKE '%insightface%'")
            insightface = _nk(cur.fetchone())["cnt"]

            # Categoria
            cur.execute("""
                SELECT category, COUNT(*) AS cnt
                FROM nodes
                WHERE category IS NOT NULL
                GROUP BY category
                ORDER BY cnt DESC
            """)
            by_category = {_nk(r)["category"]: _nk(r)["cnt"] for r in cur.fetchall()}

            # Directory count
            cur.execute("SELECT COUNT(*) AS cnt FROM nodes WHERE type='Directory'")
            directory_count = _nk(cur.fetchone())["cnt"]

            # Oggetti YOLO
            cur.execute("SELECT COUNT(*) AS cnt FROM objects")
            object_count = _nk(cur.fetchone())["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM node_objects")
            node_object_links = _nk(cur.fetchone())["cnt"]

    except Exception as e:
        logger.error("Errore stats: %s", e)
        return jsonify({"error": str(e)}), 500

    chroma = ChromaStore()

    return jsonify({
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "total_files": total_files,
        "queue_done": queue_done,
        "nodes_by_type": nodes_by_type,
        "edges_by_relation": edges_by_rel,
        "files_with_faces": with_faces,
        "insightface_processed": insightface,
        "persons_with_embeddings": with_embeddings,
        "by_category": by_category,
        "directory_count": directory_count,
        "object_count": object_count,
        "node_object_links": node_object_links,
        "chroma_text": chroma.count_text(),
        "chroma_images": chroma.count_images(),
        "embedding_files": len(list(Path("data/embeddings").glob("*.npy"))) if Path("data/embeddings").exists() else 0,
    })


# ─── API: Nodi ─────────────────────────────────────────────────────


@app.route("/api/nodes")
def api_nodes():
    cur = _get_cursor()
    try:
        node_type = request.args.get("type")
        limit = min(int(request.args.get("limit", 100)), 1000)
        offset = int(request.args.get("offset", 0))
        search = request.args.get("search", "")

        where = []
        params = []
        if node_type:
            where.append("n.type = %s")
            params.append(node_type)
        if search:
            where.append("(n.label LIKE %s OR n.id LIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])

        where_clause = "WHERE " + " AND ".join(where) if where else ""

        # Conteggio totale (per paginazione)
        cur.execute(f"SELECT COUNT(*) AS cnt FROM nodes n {where_clause}", tuple(params))
        total = cur.fetchone()["cnt"]

        # Query con file_registry opzionale + mount_root
        sql = f"""SELECT n.id, n.type, n.label, n.metadata, n.created_at,
                         f.path, f.mime_type, f.device, f.size_bytes, f.sha256,
                         d.mount_root
                  FROM nodes n
                  LEFT JOIN file_registry f ON f.node_id = n.id
                  LEFT JOIN devices d ON d.id = f.device_id
                  {where_clause}
                  ORDER BY n.created_at DESC
                  LIMIT %s OFFSET %s"""
        params.extend([limit, offset])
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

        nodes = []
        for r in [_nk(row) for row in rows]:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            node = {
                "id": r.get("id", ""),
                "type": r.get("type", ""),
                "label": r.get("label", ""),
                "created_at": str(r.get("created_at", "")) if r.get("created_at") else None,
                "path": _resolve_path(r.get("path", ""), r.get("mount_root")),
                "mime_type": r.get("mime_type") or "",
                "device": r.get("device") or "",
                "size_bytes": r.get("size_bytes"),
                "sha256": r.get("sha256") or "",
                "metadata": meta,
            }
            nodes.append(node)

        return jsonify({"nodes": nodes, "total": total, "limit": limit, "offset": offset})

    except Exception as e:
        logger.error("Errore nodi: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── API: Dettaglio nodo ───────────────────────────────────────────


@app.route("/api/nodes/<node_id>")
def api_node_detail(node_id):
    cur = _get_cursor()
    try:
        # Nodo
        cur.execute("SELECT * FROM nodes WHERE id = %s", (node_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Nodo non trovato"}), 404

        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}

        # Normalize keys: MariaDB DictCursor restituisce MAIUSCOLE su Linux
        def _nk(d):
            return {k.lower(): v for k, v in (d or {}).items()}

        row = _nk(row)

        node = {
            "id": row.get("id", ""),
            "type": row.get("type", ""),
            "label": row.get("label", ""),
            "created_at": str(row.get("created_at", "")) if row.get("created_at") else None,
            "updated_at": str(row.get("updated_at", "")) if row.get("updated_at") else None,
            "metadata": meta,
        }

        # File registry (se presente) — con mount_root per path relativi
        cur.execute("""
            SELECT f.*, d.mount_root
            FROM file_registry f
            LEFT JOIN devices d ON d.id = f.device_id
            WHERE f.node_id = %s
        """, (node_id,))
        fr = cur.fetchone()
        if fr:
            fr = _nk(fr)
            # Risolve path assoluto
            raw_path = fr.get("path", "")
            mount_root = fr.get("mount_root")
            if mount_root and not (raw_path.startswith("/") or raw_path.startswith("\\") or (len(raw_path) > 1 and raw_path[1] == ":")):
                resolved_path = str(Path(mount_root) / raw_path)
            else:
                resolved_path = raw_path
            node["file"] = {
                "path": resolved_path,
                "device": fr.get("device", ""),
                "size_bytes": fr.get("size_bytes"),
                "sha256": fr.get("sha256", ""),
                "mime_type": fr.get("mime_type", ""),
                "last_seen": str(fr.get("last_seen", "")),
            }

        # Archi entranti
        cur.execute("""
            SELECT e.*, n.type as src_type, n.label as src_label
            FROM edges e
            JOIN nodes n ON n.id = e.source_id
            WHERE e.target_id = %s
            ORDER BY e.relation
        """, (node_id,))
        incoming = []
        for r in cur.fetchall():
            r = _nk(r)
            incoming.append({
                "edge_id": r.get("id", 0),
                "source_id": r.get("source_id", ""),
                "source_label": r.get("src_label", ""),
                "source_type": r.get("src_type", ""),
                "relation": r.get("relation", ""),
                "weight": r.get("weight", 1.0),
            })

        # Archi uscenti
        cur.execute("""
            SELECT e.*, n.type as tgt_type, n.label as tgt_label
            FROM edges e
            JOIN nodes n ON n.id = e.target_id
            WHERE e.source_id = %s
            ORDER BY e.relation
        """, (node_id,))
        outgoing = []
        for r in cur.fetchall():
            r = _nk(r)
            outgoing.append({
                "edge_id": r.get("id", 0),
                "target_id": r.get("target_id", ""),
                "target_label": r.get("tgt_label", ""),
                "target_type": r.get("tgt_type", ""),
                "relation": r.get("relation", ""),
                "weight": r.get("weight", 1.0),
            })

        # Embedding (se persona con embedding)
        emb_path = Path("data/embeddings") / f"{node_id}.npy"
        if emb_path.exists():
            import numpy as np
            emb = np.load(str(emb_path))
            node["embedding"] = {"dim": len(emb), "norm": float(np.linalg.norm(emb))}

        # Eventuale immagine associata
        if node["type"] == "Person":
            # Cerca la foto associata
            for edge in outgoing + incoming:
                if edge["relation"] == "CONTAINS":
                    file_id = edge.get("target_id") or edge.get("source_id")
                    if file_id:
                        cur.execute("""
                            SELECT f.path, d.mount_root
                            FROM file_registry f
                            LEFT JOIN devices d ON d.id = f.device_id
                            WHERE f.node_id = %s
                        """, (file_id,))
                        file_row = cur.fetchone()
                        if file_row:
                            raw_path = file_row["path"]
                            mount_root = file_row.get("mount_root")
                            if mount_root and not (raw_path.startswith("/") or raw_path.startswith("\\") or (len(raw_path) > 1 and raw_path[1] == ":")):
                                node["photo_path"] = str(Path(mount_root) / raw_path)
                            else:
                                node["photo_path"] = raw_path
                            break

        return jsonify({"node": node, "incoming": incoming, "outgoing": outgoing})

    except Exception as e:
        logger.error("Errore dettaglio nodo: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── API: Grafo completo (per visualizzazione) ─────────────────────


@app.route("/api/graph")
def api_graph():
    try:
        # Carica il grafo se non già caricato o se richiesto refresh
        refresh = request.args.get("refresh", "0") == "1"
        if refresh or bridge.graph.number_of_nodes() == 0:
            bridge.load_from_db()

        # Costruisce i dati per vis.js
        nodes = []
        edges = []

        # Mappa colori per tipo
        type_colors = {
            "File": {"color": "#3498db", "shape": "box"},
            "Person": {"color": "#e74c3c", "shape": "dot"},
            "Location": {"color": "#2ecc71", "shape": "triangle"},
            "Project": {"color": "#f39c12", "shape": "star"},
            "Event": {"color": "#9b59b6", "shape": "diamond"},
            "Directory": {"color": "#1abc9c", "shape": "hexagon"},
            "Object": {"color": "#e67e22", "shape": "square"},
        }

        # Campionamento intelligente: prendi tutti i progetti, le location, le persone con embedding, e un campione di file
        all_nodes = list(bridge.graph.nodes(data=True))
        project_nodes = [(n,d) for n,d in all_nodes if d.get("type")=="Project"]
        person_nodes = [(n,d) for n,d in all_nodes if d.get("type")=="Person"]
        location_nodes = [(n,d) for n,d in all_nodes if d.get("type")=="Location"]
        file_nodes = [(n,d) for n,d in all_nodes if d.get("type")=="File"]

        # Prioritizza nodi importanti, poi campiona file
        MAX_NODES = 1500
        selected = set()

        def add_nodes(nlist, max_n=None):
            for n,d in nlist[:max_n]:
                selected.add(n)

        add_nodes(project_nodes)          # tutti i progetti
        add_nodes(person_nodes, 300)      # max 300 persone
        add_nodes(location_nodes, 200)    # max 200 location
        # Directory: seleziona come gerarchia, non tutte (potrebbero essere tante)
        dir_nodes = [(n,d) for n,d in all_nodes if d.get("type")=="Directory"]
        add_nodes(dir_nodes, 200)  # max 200 directory
        add_nodes(file_nodes, min(len(file_nodes), MAX_NODES - len(selected)))  # riempie fino a MAX_NODES

        # Costruisce nodi
        all_data = dict(all_nodes)
        for nid in selected:
            data = all_data[nid]
            ntype = data.get("type", "File")
            style = type_colors.get(ntype, {"color": "#95a5a6", "shape": "dot"})
            label = data.get("label", nid[:8])
            if len(label) > 30:
                label = label[:27] + "..."
            nodes.append({
                "id": nid,
                "label": label,
                "title": f"{ntype}: {data.get('label', nid)}",
                "group": ntype,
                "color": style["color"],
                "shape": style["shape"],
                "size": 15 if ntype == "Person" else 10,
                "metadata": data,
            })

        # Archi tra nodi selezionati
        for u, v, k, data in bridge.graph.edges(keys=True, data=True):
            if u in selected and v in selected:
                rel = data.get("relation", "UNKNOWN")
                edges.append({
                    "from": u,
                    "to": v,
                    "label": rel,
                    "title": f"{rel} (weight: {data.get('weight', 1.0)})",
                    "arrows": "to",
                    "color": {"color": "#666", "opacity": 0.5},
                    "width": data.get("weight", 1.0),
                })

        return jsonify({"nodes": nodes, "edges": edges})

    except Exception as e:
        logger.error("Errore grafo: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── API: Albero directory (gerarchia filesystem) ──────────────────


@app.route("/api/tree")
def api_tree():
    """Restituisce l'albero gerarchico (Directory → File) per il frontend.

    Query param:
        project: ID della Project (default: tutte)
        depth: profondità massima (default: 10)
    """
    try:
        refresh = request.args.get("refresh", "0") == "1"
        if refresh or bridge.graph.number_of_nodes() == 0:
            bridge.load_from_db()

        project = request.args.get("project")
        depth = min(int(request.args.get("depth", 10)), 20)

        tree = bridge.directory_tree(root_id=project, max_depth=depth)

        # Aggiunge info extra ai nodi File (path, mime, size)
        def _enrich(node, parent_path=""):
            meta = node.get("metadata", {}) or {}
            ntype = node.get("type")
            item = {
                "id": node["id"],
                "label": node.get("label", "?"),
                "type": ntype,
                "category": node.get("category"),
                "path": meta.get("path") if isinstance(meta, dict) else None,
            }
            if ntype == "File" and isinstance(meta, dict):
                item["mime_type"] = meta.get("mime_type")
                item["size_bytes"] = meta.get("size_bytes")
                item["extension"] = meta.get("extension")
                item["yolo_objects"] = meta.get("yolo_object_summary")
                item["face_count"] = meta.get("face_count", 0)
            children = node.get("children", [])
            if children:
                item["children"] = [_enrich(c) for c in children]
                dirs = [c for c in children if c.get("type") == "Directory"]
                files = [c for c in children if c.get("type") == "File"]
                item["directory_count"] = len(dirs)
                item["file_count"] = len(files)
            return item

        result = [_enrich(r) for r in tree.get("_tree", [])]
        return jsonify({"tree": result})

    except Exception as e:
        logger.error("Errore tree: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── API: Ricerca semantica ────────────────────────────────────────


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "")
    top_k = min(int(request.args.get("top", 20)), 100)
    mime_filter = request.args.get("mime", None)

    if not query:
        return jsonify({"results": [], "error": "Query vuota"})

    try:
        chroma = ChromaStore()
        results = chroma.search_similar(query, top_k=top_k, filter_mime=mime_filter, include_images=True)

        # Arricchisci ogni risultato con il path del file dal file_registry
        for r in results:
            node_id = r.get("node_id", "")
            if node_id:
                try:
                    cur = _get_cursor()
                    cur.execute(
                        """SELECT f.path, d.mount_root, f.mime_type
                           FROM file_registry f
                           LEFT JOIN devices d ON d.id = f.device_id
                           WHERE f.node_id = %s""",
                        (node_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        r["file_path"] = _resolve_path(row["path"], row.get("mount_root"))
                        r["extension"] = Path(r["file_path"]).suffix.lower()
                    else:
                        r["file_path"] = ""
                        r["extension"] = ""
                except Exception:
                    r["file_path"] = ""
                    r["extension"] = ""

        # ─── FALLBACK: se ChromaDB vuoto, cerca per label LIKE ───
        if not results:
            with MariaDBStore() as fallback_db:
                fallback_rows = fallback_db._query(
                    """SELECT n.id, n.label, f.path, f.mime_type
                       FROM nodes n
                       LEFT JOIN file_registry f ON f.node_id = n.id
                       WHERE n.type = 'File'
                         AND (n.label LIKE %s OR n.id LIKE %s)
                       ORDER BY n.created_at DESC
                       LIMIT %s""",
                    (f"%{query}%", f"%{query}%", top_k),
                )
                for row in fallback_rows:
                    results.append({
                        "node_id": row["id"],
                        "file_name": row["label"],
                        "mime_type": row.get("mime_type", ""),
                        "distance": 0.0,
                        "snippet": "",
                        "file_path": "",
                        "extension": Path(row.get("label", "")).suffix.lower(),
                        "search_type": "fallback_label",
                    })

        return jsonify({"results": results, "query": query})
    except Exception as e:
        logger.error("Errore search: %s", e)
        return jsonify({"error": str(e), "results": []}), 500


# ─── API: Oggetti YOLO ─────────────────────────────────────────────


@app.route("/api/objects")
def api_objects():
    """Lista oggetti rilevati (YOLO) nel grafo."""
    cur = _get_cursor()
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))
        category = request.args.get("category")

        where = ""
        params = []
        if category:
            where = "WHERE o.category = %s"
            params.append(category)

        cur.execute(
            f"""SELECT o.id, o.label, o.coco_class_id, o.category, o.created_at,
                       COUNT(no.id) AS file_count,
                       ROUND(AVG(no.confidence), 2) AS avg_confidence
                FROM objects o
                LEFT JOIN node_objects no ON no.object_id = o.id
                {where}
                GROUP BY o.id
                ORDER BY file_count DESC
                LIMIT %s OFFSET %s""",
            tuple(params) + (limit, offset),
        )
        objects = []
        for r in [_nk(row) for row in cur.fetchall()]:
            objects.append({
                "id": r.get("id"),
                "label": r.get("label"),
                "coco_class_id": r.get("coco_class_id"),
                "category": r.get("category"),
                "file_count": r.get("file_count"),
                "avg_confidence": r.get("avg_confidence"),
            })

        cur.execute(f"SELECT COUNT(*) AS cnt FROM objects o {where}", tuple(params))
        total = _nk(cur.fetchone())["cnt"]

        return jsonify({"objects": objects, "total": total})

    except Exception as e:
        logger.error("Errore objects: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── API: Facce ────────────────────────────────────────────────────


@app.route("/api/faces")
def api_faces():
    cur = _get_cursor()
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))
        cur.execute("""
            SELECT n.id, n.label, n.metadata,
                   d.mount_root,
                   f.path as file_path,
                   e.source_id as file_node_id
            FROM nodes n
            JOIN edges e ON e.target_id = n.id AND e.relation = 'CONTAINS'
            LEFT JOIN file_registry f ON f.node_id = e.source_id
            LEFT JOIN devices d ON d.id = f.device_id
            WHERE n.type = 'Person'
            ORDER BY n.created_at DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        rows = [_nk(r) for r in cur.fetchall()]

        faces = []
        for r in rows:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            face = {
                "id": r.get("id", ""),
                "label": r.get("label", ""),
                "file_path": r.get("file_path") or "",
                "has_embedding": "embedding_dim" in str(meta),
                "bbox": meta.get("bbox") or meta.get("rectangle"),
                "det_score": meta.get("det_score") or meta.get("confidence"),
                "gender": meta.get("gender"),
                "age": meta.get("age"),
                "source": meta.get("source", "yolo"),
                "file_node_id": r.get("file_node_id"),
            }
            faces.append(face)

        # Totale
        cur.execute("SELECT COUNT(*) AS cnt FROM nodes WHERE type = 'Person'")
        total = _nk(cur.fetchone())["cnt"]

        return jsonify({"faces": faces, "total": total, "limit": limit, "offset": offset})

    except Exception as e:
        logger.error("Errore faces: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── API: Progetti ─────────────────────────────────────────────────


@app.route("/api/projects")
def api_projects():
    cur = _get_cursor()
    try:
        cur.execute("""
            SELECT n.id, n.label, n.metadata, n.created_at,
                   (SELECT COUNT(*) FROM edges e WHERE e.target_id = n.id AND e.relation = 'CONTAINS') as child_count,
                   (SELECT COUNT(*) FROM edges e WHERE e.target_id = n.id AND e.relation = 'MEMBER_OF') as member_count
            FROM nodes n
            WHERE n.type = 'Project'
            ORDER BY n.label
        """)
        rows = [_nk(r) for r in cur.fetchall()]
        projects = []
        for r in rows:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    meta = {}
            projects.append({
                "id": r.get("id", ""),
                "label": r.get("label", ""),
                "file_count": (r.get("child_count") or 0) + (r.get("member_count") or 0),
                "directory_count": r.get("child_count") or 0,
                "member_count": r.get("member_count") or 0,
                "path": meta.get("path", ""),
                "device": meta.get("device", ""),
                "created_at": str(r.get("created_at", "")) if r.get("created_at") else None,
            })
        return jsonify({"projects": projects})

    except Exception as e:
        logger.error("Errore projects: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── API: Location ─────────────────────────────────────────────────


@app.route("/api/locations")
def api_locations():
    cur = _get_cursor()
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
        offset = int(request.args.get("offset", 0))
        cur.execute("""
            SELECT n.id, n.label, n.metadata, n.created_at,
                   (SELECT COUNT(*) FROM edges e WHERE e.target_id = n.id AND e.relation = 'MENTIONS') as mention_count
            FROM nodes n
            WHERE n.type = 'Location'
            ORDER BY mention_count DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        rows = cur.fetchall()

        locations = []
        for r in rows:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    meta = {}
            locations.append({
                "id": r["id"],
                "label": r["label"],
                "mention_count": r["mention_count"],
                "ner_label": meta.get("source_ner", ""),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })

        cur.execute("SELECT COUNT(*) AS cnt FROM nodes WHERE type = 'Location'")
        total = cur.fetchone()["cnt"]

        return jsonify({"locations": locations, "total": total})

    except Exception as e:
        logger.error("Errore locations: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── API: Embedding status ─────────────────────────────────────────


@app.route("/api/embeddings/status")
def api_embeddings_status():
    emb_dir = Path("data/embeddings")
    if not emb_dir.exists():
        return jsonify({"count": 0, "files": []})

    npy_files = sorted(emb_dir.glob("*.npy"))
    import numpy as np

    stats = []
    for f in npy_files[:20]:  # primi 20
        try:
            emb = np.load(str(f))
            stats.append({
                "person_id": f.stem,
                "dim": emb.shape[0],
                "norm": float(np.linalg.norm(emb)),
                "min": float(emb.min()),
                "max": float(emb.max()),
            })
        except:
            pass

    return jsonify({"count": len(npy_files), "sample": stats})


# ─── API: Eventi ────────────────────────────────────────────────────

@app.route("/api/events")
def api_events():
    """Lista eventi con data, conteggio file, ordinati cronologicamente."""
    cur = _get_cursor()
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
        offset = int(request.args.get("offset", 0))
        year = request.args.get("year")
        month = request.args.get("month")

        where = ["n.type = 'Event'"]
        params = []

        if year:
            where.append("n.label LIKE CONCAT('Event_', %s, '%%')")
            params.append(f"{year}-")
        if month:
            where.append("n.label LIKE CONCAT('Event_', %s, %s, '%%')")
            params.append(f"{year}-" if year else "")
            params.append(f"{int(month):02d}-")

        where_clause = "WHERE " + " AND ".join(where)

        # Conteggio totale
        cur.execute(f"SELECT COUNT(*) AS cnt FROM nodes n {where_clause}", tuple(params))
        total = cur.fetchone()["cnt"]

        # SQL parametrizzato per la lista eventi
        cur.execute(f"""
            SELECT n.id, n.label, n.metadata, n.created_at,
                   (SELECT COUNT(*) FROM edges e WHERE e.target_id = n.id AND e.relation = 'CREATED_AT') as file_count
            FROM nodes n
            {where_clause}
            ORDER BY n.label DESC
            LIMIT %s OFFSET %s
        """, tuple(params) + (limit, offset))
        rows = cur.fetchall()

        events = []
        for r in rows:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    meta = {}

            date_str = meta.get("date", r["label"].replace("Event_", "") if r["label"].startswith("Event_") else "")
            ev = {
                "id": r["id"],
                "label": r["label"],
                "date": date_str,
                "year": meta.get("year"),
                "month": meta.get("month"),
                "day": meta.get("day"),
                "file_count": r["file_count"],
                "date_type": meta.get("date_type", meta.get("source", "")),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
                "display_name": date_str or r["label"],
            }

            # Prendi una miniatura dal primo file collegato
            if r["file_count"] > 0:
                cur.execute("""
                    SELECT e.source_id as file_id, f.path, d.mount_root
                    FROM edges e
                    LEFT JOIN file_registry f ON f.node_id = e.source_id
                    LEFT JOIN devices d ON d.id = f.device_id
                    WHERE e.target_id = %s AND e.relation = 'CREATED_AT'
                    LIMIT 1
                """, (r["id"],))
                thumb = cur.fetchone()
                if thumb and thumb.get("path"):
                    path = _resolve_path(thumb["path"], thumb.get("mount_root"))
                    ext = Path(path).suffix.lower()
                    if ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'):
                        ev["thumbnail_id"] = thumb["file_id"]
                        ev["thumbnail_path"] = path

            events.append(ev)

        # Range anni per calendario
        cur.execute("""
            SELECT MIN(CAST(SUBSTRING(n.label, 7, 4) AS UNSIGNED)) as min_year,
                   MAX(CAST(SUBSTRING(n.label, 7, 4) AS UNSIGNED)) as max_year
            FROM nodes n
            WHERE n.type = 'Event' AND n.label LIKE CONCAT('Event_', '%%') AND LENGTH(n.label) = 16
        """)
        yr = cur.fetchone()
        year_range = {"min": yr["min_year"] if yr else None, "max": yr["max_year"] if yr else None}

        return jsonify({
            "events": events,
            "total": total,
            "limit": limit,
            "offset": offset,
            "year_range": year_range,
        })

    except Exception as e:
        logger.error("Errore events: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/events/<event_id>")
def api_event_detail(event_id):
    """Dettaglio evento: metadati, lista file collegati, keyframe."""
    cur = _get_cursor()
    try:
        cur.execute("SELECT * FROM nodes WHERE id = %s AND type = 'Event'", (event_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Evento non trovato"}), 404

        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except:
                meta = {}

        event = {
            "id": row["id"],
            "label": row["label"],
            "type": "Event",
            "metadata": meta,
            "created_at": str(row["created_at"]) if row.get("created_at") else None,
            "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
        }

        # File collegati via CREATED_AT
        cur.execute("""
            SELECT e.source_id as file_id, e.metadata as edge_meta,
                   n.label as file_label, n.type as file_type, n.metadata as file_meta,
                   f.path, f.mime_type, f.size_bytes, f.device,
                   d.mount_root
            FROM edges e
            JOIN nodes n ON n.id = e.source_id
            LEFT JOIN file_registry f ON f.node_id = e.source_id
            LEFT JOIN devices d ON d.id = f.device_id
            WHERE e.target_id = %s AND e.relation = 'CREATED_AT'
            ORDER BY n.label
        """, (event_id,))
        files = []
        for r in cur.fetchall():
            fm = r.get("file_meta") or {}
            if isinstance(fm, str):
                try:
                    fm = json.loads(fm)
                except:
                    fm = {}
            files.append({
                "id": r["file_id"],
                "label": r["file_label"],
                "type": r["file_type"],
                "path": _resolve_path(r.get("path", ""), r.get("mount_root")),
                "mime_type": r.get("mime_type", ""),
                "size_bytes": r.get("size_bytes"),
                "device": r.get("device", ""),
                "has_faces": fm.get("has_faces", False),
                "has_scenes": fm.get("has_scenes", False),
                "face_count": fm.get("face_count", 0),
            })

        # Scene sub-eventi (HAS_SCENE)
        cur.execute("""
            SELECT e.target_id as scene_id, n.label as scene_label, n.metadata as scene_meta
            FROM edges e
            JOIN nodes n ON n.id = e.target_id
            WHERE e.source_id = %s AND e.relation = 'HAS_SCENE'
            ORDER BY e.id
        """, (event_id,))
        scenes = []
        for r in cur.fetchall():
            sm = r.get("scene_meta") or {}
            if isinstance(sm, str):
                try:
                    sm = json.loads(sm)
                except:
                    sm = {}
            scenes.append({
                "id": r["scene_id"],
                "label": r["scene_label"],
                "start_timecode": sm.get("start_timecode", ""),
                "end_timecode": sm.get("end_timecode", ""),
                "duration_sec": sm.get("duration_sec", 0),
                "keyframe_path": sm.get("keyframe_path", ""),
            })

        event["files"] = files
        event["scenes"] = scenes
        event["file_count"] = len(files)

        return jsonify({"event": event})

    except Exception as e:
        logger.error("Errore dettaglio evento: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/events/timeline")
def api_events_timeline():
    """Timeline eventi raggruppata per anno/mese."""
    cur = _get_cursor()
    try:
        cur.execute("""
            SELECT 
                CAST(SUBSTRING(n.label, 7, 4) AS UNSIGNED) as year,
                CAST(SUBSTRING(n.label, 12, 2) AS UNSIGNED) as month,
                COUNT(*) as event_count,
                SUM((SELECT COUNT(*) FROM edges e WHERE e.target_id = n.id AND e.relation = 'CREATED_AT')) as total_files
            FROM nodes n
            WHERE n.type = 'Event' AND n.label LIKE CONCAT('Event_', '%%') AND LENGTH(n.label) = 16
            GROUP BY year, month
            ORDER BY year DESC, month DESC
        """)
        rows = cur.fetchall()

        timeline = []
        for r in rows:
            timeline.append({
                "year": r["year"],
                "month": r["month"],
                "event_count": r["event_count"],
                "total_files": r["total_files"],
            })

        return jsonify({"timeline": timeline})

    except Exception as e:
        logger.error("Errore timeline: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/events/calendar")
def api_events_calendar():
    """Dati per calendario (heatmap stile GitHub): file per giorno."""
    cur = _get_cursor()
    try:
        year = request.args.get("year")

        where = "n.type = 'Event' AND n.label LIKE CONCAT('Event_', '%%') AND LENGTH(n.label) = 16"
        params = []
        if year:
            where += " AND n.label LIKE CONCAT('Event_', %s, '%%')"
            params.append(f"{year}-")

        cur.execute(f"""
            SELECT n.label,
                   (SELECT COUNT(*) FROM edges e WHERE e.target_id = n.id AND e.relation = 'CREATED_AT') as file_count
            FROM nodes n
            WHERE {where}
            ORDER BY n.label
        """, tuple(params))
        rows = cur.fetchall()

        days = []
        for r in rows:
            date_str = r["label"].replace("Event_", "")
            days.append({
                "date": date_str,
                "count": r["file_count"],
            })

        return jsonify({"days": days, "total": len(days)})

    except Exception as e:
        logger.error("Errore calendar: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── Helper: risolve file path dal node_id ────────────────────────


def _resolve_file_path(node_id: str) -> tuple[Optional[Path], Optional[str], Optional[str]]:
    """Dato un node_id, risolve file path assoluto, mime_type e device.

    Returns:
        (path, mime_type, device) oppure (None, None, None) se non trovato.
    """
    cur = _get_cursor()
    cur.execute("""
        SELECT f.path, d.mount_root, f.mime_type, f.device
        FROM file_registry f
        LEFT JOIN devices d ON d.id = f.device_id
        WHERE f.node_id = %s
        LIMIT 1
    """, (node_id,))
    row = cur.fetchone()
    if not row:
        return None, None, None

    raw_path = row["path"]
    mount_root = row.get("mount_root")
    mime_type = row.get("mime_type") or ""
    device = row.get("device") or ""

    # Risolve path assoluto
    if mount_root and not (raw_path.startswith("/") or raw_path.startswith("\\") or (len(raw_path) > 1 and raw_path[1] == ":")):
        path = Path(mount_root) / raw_path
    else:
        path = Path(raw_path)

    # Fallback: se il file non esiste, prova path inalterato (backward compat)
    if not path.exists():
        path = Path(raw_path)

    return path if path.exists() else None, mime_type or _guess_mime_from_ext(path), device


_EXT_MIME_MAP = {
    ".jpg": "image/jpeg",".jpeg": "image/jpeg",".png": "image/png",
    ".gif": "image/gif",".webp": "image/webp",".bmp": "image/bmp",
    ".svg": "image/svg+xml",".ico": "image/x-icon",
    ".mp4": "video/mp4",".webm": "video/webm",".mov": "video/quicktime",
    ".avi": "video/x-msvideo",".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",".wav": "audio/wav",".ogg": "audio/ogg",
    ".flac": "audio/flac",".m4a": "audio/mp4",
    ".pdf": "application/pdf",
    ".txt": "text/plain",".md": "text/markdown",".csv": "text/csv",
    ".json": "application/json",".xml": "application/xml",".html": "text/html",
    ".py": "text/x-python",".js": "text/javascript",".css": "text/css",
    ".zip": "application/zip",".tar": "application/x-tar",
    ".doc": "application/msword",".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _guess_mime_from_ext(path: Path) -> str:
    """Indovina MIME type dall'estensione come fallback."""
    ext = path.suffix.lower()
    return _EXT_MIME_MAP.get(ext, "application/octet-stream")


# ─── API: Preview file (inline, per browser) ──────────────────────


@app.route("/api/files/<node_id>/preview")
def api_file_preview(node_id):
    """Serve un file inline per anteprima nel browser (immagini, video, audio, testo)."""
    path, mime_type, device = _resolve_file_path(node_id)
    if not path:
        return jsonify({"error": "File non trovato su disco"}), 404

    from flask import send_file
    return send_file(
        str(path),
        mimetype=mime_type,
        as_attachment=False,
        download_name=path.name,
    )


# ─── API: Download file ───────────────────────────────────────────


@app.route("/api/files/<node_id>/download")
def api_file_download(node_id):
    """Serve un file come download (Content-Disposition: attachment)."""
    path, mime_type, device = _resolve_file_path(node_id)
    if not path:
        return jsonify({"error": "File non trovato su disco"}), 404

    from flask import send_file
    return send_file(
        str(path),
        mimetype=mime_type or "application/octet-stream",
        as_attachment=True,
        download_name=path.name,
    )


# ─── API: Info file (per frontend) ───────────────────────────────


@app.route("/api/files/<node_id>/info")
def api_file_info(node_id):
    """Restituisce metadati del file fisico (path, mime, size, esiste)."""
    path, mime_type, device = _resolve_file_path(node_id)
    if not path:
        return jsonify({"error": "File non trovato su disco", "exists": False}), 404

    import os as _os
    stat = _os.stat(str(path))
    ext = path.suffix.lower()

    previewable = ext in _EXT_MIME_MAP or (mime_type and mime_type.startswith(("image/", "video/", "audio/", "text/")))

    return jsonify({
        "exists": True,
        "path": str(path),
        "name": path.name,
        "mime_type": mime_type or _guess_mime_from_ext(path),
        "size_bytes": stat.st_size,
        "extension": ext,
        "device": device,
        "previewable": previewable,
        "preview_url": f"/api/files/{node_id}/preview",
        "download_url": f"/api/files/{node_id}/download",
    })


# ─── Pre-carica i modelli di embedding all'avvio ──────────────

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("Penelope Web API \u2014 http://localhost:5000")
    logger.info("=" * 50)
    logger.info("I modelli AI (MiniLM, CLIP) vengono caricati alla prima richiesta.")
    app.run(host="0.0.0.0", port=5000, debug=False)

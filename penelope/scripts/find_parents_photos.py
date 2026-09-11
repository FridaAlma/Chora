"""
Trova le foto di coppia dei genitori usando il face recognition di Oracle.

Fasi:
  1. Chiede all'utente di fornire foto di referenza per ciascun genitore
  2. Calcola embedding InsightFace per i volti di referenza
  3. Scansiona TUTTE le foto nel database, rileva volti e calcola embedding
  4. Confronta ogni volto con i volti di referenza
  5. Trova le foto che contengono ENTRAMBI i genitori
  6. Mostra i risultati in una pagina HTML interattiva

Usage:
    python scripts/find_parents_photos.py
    python scripts/find_parents_photos.py --ref-dir Z:/Users/.../foto_ref
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Aggiunge radice progetto al path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import numpy as np
from loguru import logger

from core.db.mariadb_store import MariaDBStore

# ─── Configurazione ─────────────────────────────────────────────────
EMBEDDINGS_DIR = Path("data/embeddings")
REFERENCE_DIR = Path("data/reference_faces")
OUTPUT_DIR = Path("data/results")
SIMILARITY_THRESHOLD = 0.35  # soglia per ArcFace (0.3-0.5)

# ─── Caricamento InsightFace (lazy) ────────────────────────────────
_face_analyzer = None


def get_analyzer():
    global _face_analyzer
    if _face_analyzer is not None:
        return _face_analyzer
    try:
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(
            name="buffalo_l",
            providers=["CPUExecutionProvider"],
        )
        app.prepare(ctx_id=0, det_size=(320, 320))
        _face_analyzer = app
        logger.info(" InsightFace caricato (buffalo_l, CPU)")
        return app
    except Exception as e:
        logger.error("Errore caricamento InsightFace: {}", e)
        return None


def cosine_similarity(a, b):
    """Similarità coseno tra due array 1D."""
    a = np.array(a, dtype=np.float32).flatten()
    b = np.array(b, dtype=np.float32).flatten()
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))


# ─── Rilevamento volti + embedding ────────────────────────────────
def detect_faces_in_image(image_path: str) -> list[dict]:
    """Rileva volti in un'immagine con InsightFace.

    Returns:
        Lista di dict con: bbox, embedding (512-dim), det_score, gender, age
    """
    app = get_analyzer()
    if app is None:
        return []

    if not os.path.isfile(image_path):
        logger.debug("File non trovato: {}", image_path)
        return []

    img = cv2.imread(image_path)
    if img is None:
        logger.debug("Impossibile leggere: {}", image_path)
        return []

    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    try:
        faces = app.get(img_rgb)
    except Exception as e:
        logger.debug("Errore detection su {}: {}", image_path, e)
        return []

    results = []
    for face in faces:
        bbox = face.bbox.astype(int).tolist() if hasattr(face, "bbox") else None
        det_score = float(face.det_score) if hasattr(face, "det_score") else 1.0
        gender = int(face.gender) if hasattr(face, "gender") else None
        age = float(face.age) if hasattr(face, "age") else None
        embedding = (
            face.normed_embedding.tolist()
            if hasattr(face, "normed_embedding") and face.normed_embedding is not None
            else None
        )
        results.append({
            "bbox": bbox,
            "det_score": det_score,
            "gender": gender,
            "age": age,
            "embedding": embedding,
        })
    return results


# ─── Carica foto di referenza ─────────────────────────────────────
def load_reference_faces(ref_dir: str | Path) -> dict[str, list[np.ndarray]]:
    """Carica le foto di referenza da una directory.

    Struttura attesa:
        ref_dir/
            papa/          # più foto del papà
                foto1.jpg
                foto2.jpg
            mamma/         # più foto della mamma
                foto1.jpg
    """
    ref_dir = Path(ref_dir)
    if not ref_dir.exists():
        logger.error("Directory referenza non trovata: {}", ref_dir)
        return {}

    references = {}
    for person_dir in sorted(ref_dir.iterdir()):
        if not person_dir.is_dir():
            continue
        person_name = person_dir.name
        embeddings = []
        for img_file in sorted(person_dir.glob("*")):
            if img_file.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            faces = detect_faces_in_image(str(img_file))
            if not faces:
                logger.warning("Nessun volto in {}", img_file)
                continue
            # Prendi il primo volto (assumiamo una persona per foto)
            emb = faces[0].get("embedding")
            if emb:
                embeddings.append(np.array(emb, dtype=np.float32))
                logger.info(f"  {person_name}: volto da {img_file.name}")

        if embeddings:
            # Media degli embedding per robustezza
            avg_emb = np.mean(embeddings, axis=0)
            # Normalizza
            avg_emb = avg_emb / (np.linalg.norm(avg_emb) + 1e-10)
            references[person_name] = avg_emb.tolist()
            logger.info(f"  -> {person_name}: {len(embeddings)} volti, embedding creato")

    return references


# ─── Trova foto con entrambi i genitori ──────────────────────────
def find_couple_photos(
    db: MariaDBStore,
    ref_embeddings: dict[str, list[float]],
    limit: int = 0,
    batch_size: int = 50,
) -> list[dict]:
    """Scansiona tutte le foto nel database e trova quelle con entrambi i genitori.

    Returns:
        Lista di dict: {
            "file_path": str,
            "node_id": str,
            "matches": {"papa": bool, "mamma": bool, ...},
            "face_count": int,
            "faces": [...]
        }
    """
    # Ottieni tutte le foto dal database
    rows = db._query(
        """SELECT n.id, f.path
           FROM nodes n
           JOIN file_registry f ON f.node_id = n.id
           WHERE n.type = %s
             AND (f.path LIKE %s OR f.path LIKE %s OR f.path LIKE %s)
           ORDER BY f.path""",
        ("File", "%.jpg", "%.jpeg", "%.png"),
    )

    total = len(rows)
    if limit and limit < total:
        rows = rows[:limit]

    logger.info(f"Scansione di {len(rows)}/{total} foto...")

    # Estrai nomi delle persone di referenza
    ref_names = list(ref_embeddings.keys())
    ref_vecs = {name: np.array(emb, dtype=np.float32) for name, emb in ref_embeddings.items()}

    results = []
    processed = 0
    t_start = time.time()

    for i, row in enumerate(rows, 1):
        file_path = row["path"]
        node_id = row["id"]

        # Rileva volti
        faces = detect_faces_in_image(file_path)
        if not faces:
            continue

        face_count = len(faces)
        matches = {name: False for name in ref_names}

        # Per ogni volto, confronta con le referenze
        for face in faces:
            emb = face.get("embedding")
            if emb is None:
                continue
            emb_vec = np.array(emb, dtype=np.float32)

            for name, ref_vec in ref_vecs.items():
                if matches[name]:
                    continue  # già trovato in questa foto
                sim = cosine_similarity(emb_vec, ref_vec)
                if sim > SIMILARITY_THRESHOLD:
                    matches[name] = True
                    logger.debug(f"  Match {name} in {os.path.basename(file_path)} (sim={sim:.3f})")

        # Ci interessa se TUTTI i genitori sono presenti (foto di coppia)
        if all(matches.values()):
            results.append({
                "file_path": file_path,
                "node_id": node_id,
                "file_name": os.path.basename(file_path),
                "matches": matches,
                "face_count": face_count,
                "faces": faces,
                "similarities": {},
            })
            logger.info(f"  TROVATA foto di coppia: {os.path.basename(file_path)}")
        elif any(matches.values()):
            # Foto con almeno un genitore
            found = [n for n, v in matches.items() if v]
            logger.debug(f"  Foto con {', '.join(found)}: {os.path.basename(file_path)}")
            results.append({
                "file_path": file_path,
                "node_id": node_id,
                "file_name": os.path.basename(file_path),
                "matches": matches,
                "face_count": face_count,
                "faces": faces,
                "similarities": {},
            })

        processed += 1
        if processed % batch_size == 0:
            elapsed = time.time() - t_start
            rate = processed / elapsed if elapsed > 0 else 0
            couple_count = sum(1 for r in results if all(r["matches"].values()))
            any_count = sum(1 for r in results if any(r["matches"].values()))
            logger.info(
                f"  [{processed}/{total}] foto con almeno un genitore: {any_count}, "
                f"coppia: {couple_count}  ({rate:.1f} img/s)"
            )

    elapsed = time.time() - t_start
    couple_count = sum(1 for r in results if all(r["matches"].values()))
    any_count = sum(1 for r in results if any(r["matches"].values()))
    logger.info(f"  Completato in {elapsed:.0f}s")
    logger.info(f"  Foto con almeno un genitore: {any_count}")
    logger.info(f"  Foto di coppia (entrambi): {couple_count}")

    return results


# ─── Genera report HTML ──────────────────────────────────────────
def generate_html_report(results: list[dict], ref_names: list[str], output_path: str | Path):
    """Genera una pagina HTML con tutte le foto trovate."""
    couple_photos = [r for r in results if all(r["matches"].values())]
    solo_photos = {name: [r for r in results if r["matches"].get(name) and not all(r["matches"].values())]
                   for name in ref_names}

    def img_to_datauri(path):
        """Converte immagine in data URI base64 (JPEG piccola)."""
        try:
            img = cv2.imread(path)
            if img is None:
                return ""
            h, w = img.shape[:2]
            if max(h, w) > 300:
                scale = 300 / max(h, w)
                img = cv2.resize(img, None, fx=scale, fy=scale)
            _, buffer = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
            import base64
            return "data:image/jpeg;base64," + base64.b64encode(buffer).decode()
        except:
            return ""

    html = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title> Oracle — Foto dei genitori</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f0f1a; color: #e0e0e0; padding: 20px; }
  h1 { color: #ffd700; font-size: 2em; margin-bottom: 5px; }
  h1 small { font-size: 0.5em; color: #888; }
  .subtitle { color: #aaa; margin-bottom: 20px; }
  h2 { color: #ffd700; margin: 30px 0 15px; border-bottom: 1px solid #333; padding-bottom: 8px; }
  .stats { display: flex; gap: 20px; margin: 20px 0; flex-wrap: wrap; }
  .stat-card { background: #1a1a2e; border-radius: 12px; padding: 20px;
               border: 1px solid #2a2a4a; flex: 1; min-width: 150px; text-align: center; }
  .stat-card .num { font-size: 2.5em; font-weight: bold; color: #ffd700; }
  .stat-card .label { font-size: 0.85em; color: #aaa; margin-top: 5px; }
  .photo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
                gap: 15px; }
  .photo-card { background: #1a1a2e; border-radius: 12px; overflow: hidden;
                border: 1px solid #2a2a4a; transition: transform 0.2s, border-color 0.2s; }
  .photo-card:hover { transform: translateY(-3px); border-color: #ffd700; }
  .photo-card img { width: 100%; height: 220px; object-fit: cover; display: block; }
  .photo-card .info { padding: 12px; }
  .photo-card .filename { font-size: 0.8em; color: #ccc; word-break: break-all;
                          margin-bottom: 6px; }
  .photo-card .path { font-size: 0.7em; color: #666; word-break: break-all; }
  .photo-card .tags { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
  .tag { padding: 2px 10px; border-radius: 10px; font-size: 0.75em; font-weight: 600; }
  .tag-papa { background: #1a3a5c; color: #6db3f2; }
  .tag-mamma { background: #5c1a3a; color: #f26db3; }
  .tag-both { background: #3a5c1a; color: #b3f26d; }
  .solo-section { margin-top: 40px; }
  .empty-state { color: #666; text-align: center; padding: 40px; font-size: 1.1em; }
  @media (max-width: 600px) {
    .photo-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<h1> Oracle <small>— Ricerca foto genitori</small></h1>
<p class="subtitle">Trovate il giorno dell'anniversario  con il face matching InsightFace</p>

<div class="stats">
  <div class="stat-card">
    <div class="num">""" + str(len(couple_photos)) + """</div>
    <div class="label">Foto di coppia  </div>
  </div>
"""

    for name in ref_names:
        count = sum(1 for r in results if r["matches"].get(name))
        html += f"""
  <div class="stat-card">
    <div class="num">{count}</div>
    <div class="label">Foto con {name} </div>
  </div>"""

    html += f"""
  <div class="stat-card">
    <div class="num">{len(results)}</div>
    <div class="label">Foto totali con volti</div>
  </div>
</div>
"""

    # Sezione foto di coppia
    html += '<h2> Foto di coppia  ️</h2>'
    if couple_photos:
        html += '<div class="photo-grid">'
        for r in couple_photos:
            data_uri = img_to_datauri(r["file_path"])
            tags = ''.join(f'<span class="tag tag-{n}">{n} </span>' for n in ref_names)
            html += f"""
  <div class="photo-card">
    <img src="{data_uri}" alt="{r['file_name']}" loading="lazy">
    <div class="info">
      <div class="tags">{tags}</div>
      <div class="filename">{r['file_name']}</div>
      <div class="path">{r['file_path']}</div>
    </div>
  </div>"""
        html += '</div>'
    else:
        html += '<div class="empty-state">Nessuna foto di coppia trovata 😢</div>'

    # Sezioni per singoli genitori
    for name in ref_names:
        items = solo_photos.get(name, [])
        html += f'<div class="solo-section"><h2> {name} (altre foto)</h2>'
        if items:
            html += '<div class="photo-grid">'
            for r in items[:20]:  # max 20
                data_uri = img_to_datauri(r["file_path"])
                tags = f'<span class="tag tag-{name}">{name}</span>'
                html += f"""
  <div class="photo-card">
    <img src="{data_uri}" alt="{r['file_name']}" loading="lazy">
    <div class="info">
      <div class="tags">{tags}</div>
      <div class="filename">{r['file_name']}</div>
      <div class="path">{r['file_path']}</div>
    </div>
  </div>"""
            html += '</div>'
            if len(items) > 20:
                html += f'<p style="color:#666">... e altre {len(items)-20} foto</p>'
        else:
            html += '<div class="empty-state">Nessuna foto trovata</div>'
        html += '</div>'

    html += """
<p style="text-align:center; color:#555; margin-top:40px; font-size:0.8em;">
  Generato da Oracle — Penelope — InsightFace — 
  <script>document.write(new Date().toLocaleDateString('it-IT'))</script>
</p>
</body></html>"""

    output_path = Path(output_path)
    output_path.write_text(html, encoding="utf-8")
    logger.info(f" Report HTML salvato: {output_path}")
    return str(output_path.resolve())


# ─── Modalità interattiva ─────────────────────────────────────────
def interactive_mode():
    """Chiede all'utente di selezionare le foto di referenza."""
    print("\n" + "=" * 60)
    print("  PROMETEO — Ricerca foto di coppia dei genitori")
    print("=" * 60)

    print("\nPer trovare le foto, ho bisogno di sapere che faccia hanno")
    print("i tuoi genitori. Ho due modalità:\n")

    print("  1) Forniscimi una DIRECTORY con foto di referenza")
    print("     Struttura: cartella_padre/  → foto del papà")
    print("                cartella_madre/  → foto della mamma")
    print("")
    print("  2) Lascia che cerchi io nelle foto esistenti")
    print("     (troverò cluster di volti simili e ti chiederò")
    print("      di identificare i genitori tra i cluster)")
    print("")

    choice = input("Scegli [1/2]: ").strip()

    if choice == "1":
        ref_dir = input("\nInserisci il path della directory di referenza: ").strip()
        if not ref_dir:
            ref_dir = str(REFERENCE_DIR)
        return load_reference_faces(ref_dir)
    else:
        return cluster_mode()


def cluster_mode():
    """Modalità clustering: trova gruppi di volti simili, chiede all'utente di identificarli."""
    from core.recognition.deepface_engine import (
        find_similar_persons,
        load_embedding,
    )

    print("\n🔍 Ricerco cluster di volti simili nel database...")
    db = MariaDBStore()

    # Trova coppie simili
    pairs = find_similar_persons(db, threshold=0.4, batch_size=100)

    if not pairs:
        print("⚠️  Nessun cluster trovato. Processo prima le foto con InsightFace...")
        print("   (usa: python scripts/batch_face_embedding.py)")
        return {}

    # Raggruppa per cluster (simple greedy)
    clusters = {}
    assigned = set()
    for id1, id2, sim in sorted(pairs, key=lambda x: -x[2]):
        if id1 in assigned or id2 in assigned:
            continue
        # Cerca un cluster esistente
        found = None
        for cid, members in clusters.items():
            if id1 in members or id2 in members:
                found = cid
                break
        if found is None:
            cid = f"cluster_{len(clusters)}"
            clusters[cid] = {"members": set(), "embeddings": [], "sample_photos": []}
            found = cid
        clusters[found]["members"].add(id1)
        clusters[found]["members"].add(id2)
        assigned.add(id1)
        assigned.add(id2)

    print(f"\n🔬 Trovati {len(clusters)} cluster di volti simili!")

    references = {}
    for cid, cluster in clusters.items():
        members = list(cluster["members"])
        # Prendi una foto campione per ogni cluster
        sample_photos = []
        for pid in members[:3]:
            node = db.get_node(pid)
            if node and node.get("metadata"):
                meta = node["metadata"]
                if isinstance(meta, str):
                    meta = json.loads(meta)
                if isinstance(meta, dict) and "photo" in meta:
                    sample_photos.append(meta["photo"])

        print(f"\n📸 Cluster {cid} ({len(members)} volti simili)")
        for sp in sample_photos[:3]:
            print(f"    📷 {sp}")

        # Carica embedding medio
        embs = []
        for pid in members:
            emb = load_embedding(pid)
            if emb is not None:
                embs.append(emb)
        if embs:
            avg_emb = np.mean(embs, axis=0)
            avg_emb = avg_emb / (np.linalg.norm(avg_emb) + 1e-10)

            ans = input(f"\n  Chi è questa persona? (papa/mamma/salta): ").strip().lower()
            if ans in ("papa", "papà", "padre"):
                references["papa"] = avg_emb.tolist()
                print("  ✅ Salvato come PAPÀ")
            elif ans in ("mamma", "madre"):
                references["mamma"] = avg_emb.tolist()
                print("  ✅ Salvato come MAMMA")
            else:
                print("  ⏭️  Saltato")

        if len(references) == 2:
            print("\n✅ Ho entrambi i genitori! Procedo con la ricerca...")
            break

    db.close()
    return references


# ─── Main ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Trova foto di coppia dei genitori con face recognition"
    )
    parser.add_argument(
        "--ref-dir",
        type=str,
        default=None,
        help="Directory con foto di referenza (sottocartelle papa/, mamma/)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Numero massimo di foto da scandire (0 = tutte)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=SIMILARITY_THRESHOLD,
        help=f"Soglia similarità coseno (default: {SIMILARITY_THRESHOLD})",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(OUTPUT_DIR / "parents_photos.html"),
        help="File HTML di output",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Modalità non interattiva (usa --ref-dir obbligatorio)",
    )
    args = parser.parse_args()

    global SIMILARITY_THRESHOLD
    SIMILARITY_THRESHOLD = args.threshold

    print("\n" + "=" * 60)
    print("  🔥 PROMETEO — Ricerca foto di coppia dei genitori")
    print("  📅 Oggi è l'anniversario! Troviamo le loro foto insieme.")
    print("=" * 60 + "\n")

    # Fase 1: Carica referenze
    if args.ref_dir:
        print(f"📁 Carico foto di referenza da: {args.ref_dir}")
        references = load_reference_faces(args.ref_dir)
    elif args.non_interactive:
        print("❌ Modalità non interattiva richiede --ref-dir")
        return
    else:
        references = interactive_mode()

    if not references:
        print("❌ Nessuna referenza caricata. Impossibile proseguire.")
        print("   Prepara una cartella con foto del papà e della mamma e riprova.")
        return

    ref_names = list(references.keys())
    print(f"\n✅ Referenze caricate: {', '.join(ref_names)}")
    print(f"   Soglia similarità: {SIMILARITY_THRESHOLD}")

    # Fase 2: Ricerca foto
    print(f"\n🔍 Avvio scansione foto nel database...")
    db = MariaDBStore()
    try:
        results = find_couple_photos(db, references, limit=args.limit)
    finally:
        db.close()

    # Fase 3: Report
    if not results:
        print("\n❌ Nessuna foto con volti trovata nel database.")
        print("   Assicurati di aver prima eseguito:")
        print("     python -m penelope.cli scan <paths>")
        print("     python scripts/batch_face_detection.py")
        print("     python scripts/batch_image_embedding.py")
        return

    couple_count = sum(1 for r in results if all(r["matches"].values()))
    any_count = sum(1 for r in results if any(r["matches"].values()))

    print(f"\n{'=' * 60}")
    print(f"  📊 RISULTATI")
    print(f"  {'=' * 56}")
    print(f"  Foto totali scansionate:     {len(results)}")
    print(f"  Foto con almeno un genitore: {any_count}")
    print(f"  📸 FOTO DI COPPIA:           {couple_count}")
    print(f"  {'=' * 56}")

    if couple_count > 0:
        print(f"\n  🎉 Trovate {couple_count} foto di coppia! Buon anniversario! 🎉\n")
        for r in [r for r in results if all(r["matches"].values())]:
            print(f"    📷 {r['file_name']}")
            print(f"       {r['file_path']}")

    # Genera HTML
    output_file = generate_html_report(results, ref_names, args.output)
    print(f"\n  💾 Report HTML salvato: {output_file}")
    print(f"  Apri il file nel browser per vedere le foto! 🖼️\n")


if __name__ == "__main__":
    main()

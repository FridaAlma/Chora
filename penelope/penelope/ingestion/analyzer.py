"""
Analyzer — analisi contenuto file per arricchimento del grafo.

Ogni funzione prende un file già categorizzato (image|video|document|other)
e lo analizza per creare nuovi nodi e relazioni nel grafo:

- Image (YOLO): oggetti raffigurati → nodi Object + edge DEPICTS
- Image (InsightFace): se rilevata persona → face recognition
- Video: estrazione metadati contenitore → relazioni con Event/Location/camera
- Document: estrazione testo + NER + embedding → nodi Person/Location + SIMILAR_TO

Tutti gli analizzatori scrivono flag analyzed[] nei metadati del nodo
per evitare ri-analisi (fase B lazy: processa solo file con flag != 'done').
"""

import json
import logging
from pathlib import Path
from typing import Optional

from penelope.db.mariadb_store import MariaDBStore

logger = logging.getLogger(__name__)

# ─── Flag di analisi ──────────────────────────────────────────────

ANALYSIS_YOLO = "yolo"      # YOLO object detection (immagini)
ANALYSIS_FACE = "face"      # InsightFace face recognition (immagini con persone)
ANALYSIS_META = "meta"      # Estrazione metadati contenitore (video)
ANALYSIS_NER = "ner"        # NER + extraction (documenti)
ANALYSIS_SEM = "sem"        # Embedding semantico + similarità (documenti)


def get_analyzed_flag(db: MariaDBStore, node_id: str, analyzer: str) -> Optional[str]:
    """Legge lo stato di un analizzatore per un nodo (done|pending|error|None)."""
    node = db.get_node(node_id)
    if not node:
        return None
    analyzed = node.get("analyzed")
    if isinstance(analyzed, str):
        try:
            analyzed = json.loads(analyzed)
        except (json.JSONDecodeError, TypeError):
            analyzed = {}
    if isinstance(analyzed, dict):
        return analyzed.get(analyzer)
    return None


def set_analyzed_flag(db: MariaDBStore, node_id: str, analyzer: str, status: str) -> bool:
    """Imposta lo stato di un analizzatore per un nodo (done|pending|error)."""
    node = db.get_node(node_id)
    if not node:
        return False
    analyzed = node.get("analyzed")
    if isinstance(analyzed, str):
        try:
            analyzed = json.loads(analyzed)
        except (json.JSONDecodeError, TypeError):
            analyzed = {}
    if not isinstance(analyzed, dict):
        analyzed = {}
    analyzed[analyzer] = status
    return db._execute(
        "UPDATE nodes SET analyzed = %s WHERE id = %s",
        (json.dumps(analyzed), node_id),
    ) > 0


def needs_analysis(db: MariaDBStore, node_id: str, analyzer: str) -> bool:
    """True se il nodo non è ancora stato analizzato per questo analyzer."""
    flag = get_analyzed_flag(db, node_id, analyzer)
    return flag != "done"


# ═══════════════════════════════════════════════════════════════════
#  ANALIZZATORE IMMAGINI — YOLOv8n (tutte le classi COCO) + InsightFace
# ═══════════════════════════════════════════════════════════════════

def analyze_image(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
) -> dict:
    """Analizza un'immagine con YOLOv8n (tutte le classi COCO) e, se
    rilevata persona, con InsightFace per face recognition.

    Returns:
        {"objects": [{"label": ..., "confidence": ..., "bbox": ...}],
         "faces": int, "face_details": [...]}
    """
    result = {"objects": [], "faces": 0, "face_details": []}

    # ─── YOLO object detection (tutte le 80 classi COCO) ───────
    from penelope.ingestion.metadata import _guess_mime as _gm
    path = Path(file_path)
    mime = _gm(path)

    if not mime.startswith("image/") or mime == "image/svg+xml":
        return result

    try:
        from ultralytics import YOLO
        import cv2
    except ImportError:
        logger.warning("YOLO non installato (pip install ultralytics opencv-python)")
        set_analyzed_flag(db, node_id, ANALYSIS_YOLO, "error")
        return result

    try:
        model = YOLO("yolov8n.pt")  # lazy, prima volta scarica
    except Exception as e:
        logger.warning("Errore caricamento YOLO: %s", e)
        set_analyzed_flag(db, node_id, ANALYSIS_YOLO, "error")
        return result

    img = cv2.imread(str(path))
    if img is None:
        logger.debug("Impossibile leggere immagine: %s", path)
        return result

    # Downscale a max 1280px per velocità
    h, w = img.shape[:2]
    if max(h, w) > 1280:
        scale = 1280.0 / max(h, w)
        new_w, new_h = int(w * scale), int(h * scale)
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        logger.debug("Downscale %dx%d → %dx%d", w, h, new_w, new_h)

    # YOLO inference: tutte le classi, conf minima 0.4
    results = model(img, conf=0.4, verbose=False)

    detections = []
    for result_obj in results:
        boxes = result_obj.boxes
        if boxes is None:
            continue
        for i in range(len(boxes)):
            cls_id = int(boxes.cls[i].item())
            conf = float(boxes.conf[i].item())
            x1, y1, x2, y2 = boxes.xyxy[i].tolist()
            from penelope.ingestion.yolo_coco import COCO_CLASSES
            label = COCO_CLASSES.get(cls_id, f"unknown_{cls_id}")
            detections.append({
                "label": label,
                "confidence": round(conf, 3),
                "class_id": cls_id,
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
            })

    if not detections:
        set_analyzed_flag(db, node_id, ANALYSIS_YOLO, "done")
        return result

    logger.info("YOLO: %d oggetti rilevati in %s", len(detections), file_path)

    # Salva oggetti nel DB (tabella objects + node_objects)
    has_person = False
    for det in detections:
        obj_id = db.ensure_object(det["label"], det["class_id"])
        db.link_object(node_id, obj_id, det["confidence"], det["bbox"])
        if det["class_id"] == 0:  # person
            has_person = True

    # Aggiorna metadati del nodo File
    from collections import Counter
    obj_counts = Counter(d["label"] for d in detections)
    node = db.get_node(node_id)
    if node:
        meta = node.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta) if meta else {}
        if isinstance(meta, dict):
            meta["yolo_objects"] = detections
            meta["yolo_object_count"] = len(detections)
            meta["yolo_object_summary"] = dict(obj_counts.most_common())
            db._execute(
                "UPDATE nodes SET metadata = %s WHERE id = %s",
                (json.dumps(meta), node_id),
            )

    set_analyzed_flag(db, node_id, ANALYSIS_YOLO, "done")
    result["objects"] = detections

    # ─── Se persona rilevata → InsightFace face recognition ────
    if has_person:
        face_result = _analyze_faces(node_id, file_path, db)
        result["faces"] = face_result.get("face_count", 0)
        result["face_details"] = face_result.get("details", [])

    return result


def _analyze_faces(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
) -> dict:
    """Face recognition con InsightFace (ArcFace 512-dim)."""
    result = {"face_count": 0, "details": []}

    if not needs_analysis(db, node_id, ANALYSIS_FACE):
        return result

    try:
        from penelope.recognition.deepface_engine import detect_faces, process_face_embedding
    except ImportError:
        logger.warning("InsightFace non installato (pip install insightface onnxruntime)")
        set_analyzed_flag(db, node_id, ANALYSIS_FACE, "error")
        return result

    try:
        ok = process_face_embedding(node_id, file_path, db)
        if ok:
            node = db.get_node(node_id)
            meta = node.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta) if meta else {}
            result["face_count"] = meta.get("face_count", 0)
            result["details"] = meta.get("face_details", [])
            set_analyzed_flag(db, node_id, ANALYSIS_FACE, "done")
        else:
            # Nessun volto → mark come done (nessun errore)
            set_analyzed_flag(db, node_id, ANALYSIS_FACE, "done")
    except Exception as e:
        logger.warning("Face recognition fallito per %s: %s", file_path, e)
        set_analyzed_flag(db, node_id, ANALYSIS_FACE, "error")

    return result


# ═══════════════════════════════════════════════════════════════════
#  ANALIZZATORE VIDEO — Metadati contenitore (ffprobe / mutagen)
# ═══════════════════════════════════════════════════════════════════
#  Nessuna analisi contenuto (scene detection saltata). Solo metadati
#  del contenitore: durata, codec, risoluzione, data creazione, GPS,
#  camera make/model. Questi metadati creano relazioni con:
#    - Event (CREATED_AT se data trovata)
#    - Location (LOCATED_AT se GPS)
#    - Device/camera (metadata)
# ═══════════════════════════════════════════════════════════════════

def analyze_video(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
) -> dict:
    """Analizza metadati contenitore video e crea relazioni nel grafo.

    Returns:
        {"metadata": {...}, "has_date": bool, "has_gps": bool}
    """
    result = {"metadata": {}, "has_date": False, "has_gps": False}
    path = Path(file_path)

    if not needs_analysis(db, node_id, ANALYSIS_META):
        return result

    meta = _extract_video_metadata(path)
    if not meta:
        set_analyzed_flag(db, node_id, ANALYSIS_META, "done")
        return result

    logger.info("Video metadata: %d campi estratti da %s", len(meta), file_path)

    # Aggiorna metadati del nodo
    node = db.get_node(node_id)
    if node:
        current_meta = node.get("metadata") or {}
        if isinstance(current_meta, str):
            current_meta = json.loads(current_meta) if current_meta else {}
        if isinstance(current_meta, dict):
            current_meta.update(meta)
            db._execute(
                "UPDATE nodes SET metadata = %s WHERE id = %s",
                (json.dumps(current_meta), node_id),
            )

    # ─── Crea relazioni dai metadati ───────────────────────────
    from penelope.ingestion.processor import process_date_event, process_geocoding

    # Data di creazione → Event node (CREATED_AT)
    if meta.get("creation_date"):
        try:
            process_date_event(node_id, file_path, db)
            result["has_date"] = True
        except Exception as e:
            logger.debug("process_date_event per video fallito: %s", e)

    # GPS → Location node (LOCATED_AT)
    if meta.get("gps_lat") and meta.get("gps_lon"):
        try:
            process_geocoding(node_id, file_path, db)
            result["has_gps"] = True
        except Exception as e:
            logger.debug("process_geocoding per video fallito: %s", e)

    result["metadata"] = meta
    set_analyzed_flag(db, node_id, ANALYSIS_META, "done")
    return result


def _extract_video_metadata(path: Path) -> dict:
    """Estrae metadati dal contenitore video.

    Prova:
    1. ffprobe (se installato) — ricco (codec, risoluzione, durata, rotazione, GPS, data)
    2. mutagen (fallback) — base (durata, codec audio/video)

    Returns:
        Dict con campi estratti o {} se errore.
    """
    result = {}

    # ─── Tentativo 1: ffprobe ───────────────────────────────
    import subprocess
    import json as _json
    import shutil

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            cmd = [
                ffprobe, "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ]
            output = subprocess.check_output(cmd, timeout=30, stderr=subprocess.PIPE)
            data = _json.loads(output.decode("utf-8", errors="replace"))

            # Format metadata
            fmt = data.get("format", {})
            if fmt:
                result["duration_sec"] = float(fmt.get("duration", 0))
                result["bit_rate"] = int(fmt.get("bit_rate", 0))
                result["format_name"] = fmt.get("format_name", "")
                # Tags dal formato (data, camera, GPS)
                tags = fmt.get("tags", {})
                for k, v in tags.items():
                    lk = k.lower().replace(" ", "_").replace("-", "_")
                    if "creation_time" in lk or "date" in lk:
                        result["creation_date"] = v
                    elif "make" in lk and "camera" not in result:
                        result["camera_make"] = v
                    elif "model" in lk and "camera" not in result:
                        result["camera_model"] = v
                    elif "location" in lk or "gps" in lk:
                        result["gps_raw"] = v
                    elif v and lk not in ("encoder", "handler_name", "major_brand",
                                          "minor_version", "compatible_brands"):
                        result[f"tag_{lk}"] = v

            # Stream metadata (primo video stream per codec, risoluzione)
            streams = data.get("streams", [])
            for s in streams:
                codec_type = s.get("codec_type", "")
                if codec_type == "video":
                    result["video_codec"] = s.get("codec_name", "")
                    result["width"] = s.get("width", 0)
                    result["height"] = s.get("height", 0)
                    result["fps"] = eval(s.get("r_frame_rate", "0/1")) if "/" in s.get("r_frame_rate", "") else 0
                    # Tags dallo stream
                    stream_tags = s.get("tags", {})
                    for k, v in stream_tags.items():
                        lk = k.lower().replace(" ", "_").replace("-", "_")
                        if "creation_time" in lk:
                            result["creation_date"] = v
                        if "rotate" in lk or "rotation" in lk:
                            result["rotation"] = int(v)
                    # GPS da side data (MP4/MOV)
                    side_data = s.get("side_data_list", [])
                    for sd in side_data:
                        if sd.get("side_data_type") == "Spherical Video V1":
                            result["spherical"] = True
                elif codec_type == "audio":
                    result["audio_codec"] = s.get("codec_name", "")
                    result["audio_channels"] = s.get("channels", 0)

            # Cerca GPS nei metadata rotation / location
            if "com.apple.quicktime.location.ISO6709" in str(tags):
                gps_raw = tags.get("com.apple.quicktime.location.ISO6709", "")
                result["gps_raw"] = gps_raw
                result["creation_date"] = result.get("creation_date") or tags.get(
                    "com.apple.quicktime.creationdate", ""
                )
                result["camera_make"] = result.get("camera_make") or tags.get(
                    "com.apple.quicktime.make", ""
                )
                result["camera_model"] = result.get("camera_model") or tags.get(
                    "com.apple.quicktime.model", ""
                )

            # Parsing GPS raw (es. "+41.8902+012.4922/" o "+41.8902-012.4922/")
            gps_raw = result.get("gps_raw", "")
            if gps_raw:
                import re
                # Pattern: +/-DD.DDDD+/-DDD.DDDD/  (ISO 6709)
                m = re.match(r"([+-]\d+\.\d+)([+-]\d+\.\d+)/?", gps_raw.strip())
                if m:
                    result["gps_lat"] = float(m.group(1))
                    result["gps_lon"] = float(m.group(2))

            # Durata (fallback se non nel formato)
            if "duration_sec" not in result and "creation_date" not in result:
                # Tenta da stream
                for s in streams:
                    if s.get("codec_type") == "video" and s.get("duration"):
                        result["duration_sec"] = float(s["duration"])

            return result

        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, Exception) as e:
            logger.debug("ffprobe fallito per %s: %s", path.name, e)
            # Fallisce → tenta mutagen

    # ─── Tentativo 2: mutagen (fallback) ─────────────────────
    try:
        from mutagen.mp4 import MP4
        from mutagen.ogg import OggFileType
        from mutagen.flac import FLAC
    except ImportError:
        logger.debug("mutagen non installato, salto video metadata")
        return result

    ext = path.suffix.lower()
    try:
        if ext == ".mp4":
            m = MP4(str(path))
            # Tags iTunes-style
            if "\xa9day" in m:
                result["creation_date"] = str(m["\xa9day"])
            if "\xa9nam" in m:
                result["title"] = str(m["\xa9nam"])
            if "\xa9gen" in m:
                result["genre"] = str(m["\xa9gen"])
            if "\xa9ART" in m:
                result["artist"] = str(m["\xa9ART"])
            # Durata da MP4
            if m.info and hasattr(m.info, "length"):
                result["duration_sec"] = m.info.length
            if m.info and hasattr(m.info, "bitrate"):
                result["bit_rate"] = m.info.bitrate
        else:
            # OGG / FLAC / other (video raro)
            pass
    except Exception as e:
        logger.debug("mutagen fallito per %s: %s", path.name, e)

    return result


# ═══════════════════════════════════════════════════════════════════
#  ANALIZZATORE DOCUMENTI — Estrazione testo + NER + Embedding
# ═══════════════════════════════════════════════════════════════════
#  Legge il contenuto testuale del documento, esegue:
#  1. NER (SpaCy) → nodi Person/Location + edge MENTIONS
#  2. Embedding semantico → ChromaDB
#  3. Confronto similarità → edge SIMILAR_TO con altri documenti
#  4. Estrazione date/keywords → Event + edge CREATED_AT
# ═══════════════════════════════════════════════════════════════════

def analyze_document(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
    chroma=None,
) -> dict:
    """Analisi semantica di un documento.

    Returns:
        {"text": str|None, "ner_count": int, "embedded": bool, "similar": int}
    """
    result = {"text": None, "ner_count": 0, "embedded": False, "similar": 0}
    path = Path(file_path)
    from penelope.ingestion.metadata import _guess_mime as _gm
    mime = _gm(path)

    # Solo documenti non-binari
    if not _is_analyzable_document(mime, path):
        return result

    # ─── Estrazione testo ───────────────────────────────────────
    text = _extract_text(path, mime)
    if not text or len(text.strip()) < 30:
        logger.debug("Documento troppo corto o illeggibile: %s", file_path)
        # Segna NER come done (nessuna entità)
        set_analyzed_flag(db, node_id, ANALYSIS_NER, "done")
        set_analyzed_flag(db, node_id, ANALYSIS_SEM, "done")
        return result

    result["text"] = text[:500]  # preview nei log

    # ─── 1. NER: crea nodi Person/Location + MENTIONS ──────────
    if needs_analysis(db, node_id, ANALYSIS_NER):
        try:
            from penelope.ingestion.processor import process_ner
            ner_count = process_ner(node_id, file_path, db)
            result["ner_count"] = ner_count
            set_analyzed_flag(db, node_id, ANALYSIS_NER, "done" if ner_count > 0 else "done")
        except Exception as e:
            logger.warning("NER fallito per %s: %s", file_path, e)
            set_analyzed_flag(db, node_id, ANALYSIS_NER, "error")

    # ─── 2. Embedding semantico → ChromaDB ─────────────────────
    if needs_analysis(db, node_id, ANALYSIS_SEM) and chroma is not None:
        try:
            from penelope.ingestion.processor import process_embedding
            embedded = process_embedding(node_id, file_path, db, chroma)
            result["embedded"] = embedded
            if embedded:
                # ─── 3. Similarità con altri documenti ──────────
                similar_count = _link_similar_documents(node_id, text, db, chroma)
                result["similar"] = similar_count
                logger.debug("Documento %s: %d similar link creati", file_path, similar_count)
            set_analyzed_flag(db, node_id, ANALYSIS_SEM, "done")
        except Exception as e:
            logger.warning("Embedding fallito per %s: %s", file_path, e)
            set_analyzed_flag(db, node_id, ANALYSIS_SEM, "error")

    # ─── 4. Data da filename → Event ──────────────────────────
    try:
        from penelope.ingestion.processor import process_date_event
        process_date_event(node_id, file_path, db)
    except Exception as e:
        logger.debug("process_date_event per doc fallito: %s", e)

    return result


def _is_analyzable_document(mime: str, path: Path) -> bool:
    """Un documento è analizzabile se estraibile testo."""
    # Testuali
    if mime.startswith("text/"):
        return True
    # PDF, DOCX, ODT
    if mime in (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.oasis.opendocument.text",
    ):
        return True
    # DOC legacy (binario, difficile da estrarre, skip)
    if mime == "application/msword":
        return False  # troppo complesso da estrarre senza antiword/libreoffice
    # Excel/PPT → non testuale
    return False


def _extract_text(path: Path, mime: str) -> Optional[str]:
    """Estrae testo da un documento."""
    # Testuali semplici
    if mime.startswith("text/"):
        try:
            return path.read_text("utf-8", errors="replace")
        except Exception as e:
            logger.debug("Lettura testo fallita %s: %s", path, e)
            # fallback latin-1
            try:
                return path.read_text("latin-1", errors="replace")
            except Exception:
                return None

    # PDF
    if mime == "application/pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(str(path))
            texts = []
            for page in reader.pages[:20]:  # max 20 pagine per performance
                t = page.extract_text()
                if t:
                    texts.append(t)
            return "\n".join(texts)
        except ImportError:
            logger.debug("pypdf non installato, salto PDF: %s", path)
            return None
        except Exception as e:
            logger.debug("Lettura PDF fallita %s: %s", path, e)
            return None

    # DOCX
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        try:
            import docx
            doc = docx.Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            logger.debug("python-docx non installato, salto DOCX: %s", path)
            return None
        except Exception as e:
            logger.debug("Lettura DOCX fallita %s: %s", path, e)
            return None

    return None


def _link_similar_documents(
    node_id: str,
    text: str,
    db: MariaDBStore,
    chroma,
    similarity_threshold: float = 0.7,
    max_results: int = 5,
) -> int:
    """Cerca documenti simili a questo via ChromaDB e crea edge SIMILAR_TO.

    Args:
        node_id: Il nodo corrente.
        text: Testo estratto.
        db: MariaDBStore.
        chroma: ChromaStore.
        similarity_threshold: Soglia coseno per considerare simile.
        max_results: Massimo edge da creare.

    Returns:
        Numero di edge SIMILAR_TO creati.
    """
    try:
        # Cerca nel ChromaDB i documenti più simili (escludendo se stesso)
        results = chroma.search_similar(
            text,
            top_k=max_results + 1,
            filter_mime=None,
            include_images=False,
        )

        count = 0
        for r in results:
            other_id = r.get("node_id")
            if other_id == node_id or not other_id:
                continue
            similarity = 1.0 - r.get("distance", 1.0)
            if similarity < similarity_threshold:
                continue

            # Edge SIMILAR_TO bidirezionale (evita duplicati)
            existing = db._query(
                "SELECT id FROM edges WHERE source_id = %s AND target_id = %s AND relation = 'SIMILAR_TO'",
                (node_id, other_id),
            )
            if not existing:
                db.create_edge(
                    source_id=node_id,
                    target_id=other_id,
                    relation="SIMILAR_TO",
                    weight=round(similarity, 3),
                    metadata={"type": "semantic", "similarity": similarity},
                )
                count += 1
                if count >= max_results:
                    break

        return count

    except Exception as e:
        logger.debug("Similar document linking fallito: %s", e)
        return 0
"""
Processor — elaborazione lazy dei file in coda.

Ogni funzione processa un aspetto specifico del file:
1. Embedding semantico (testo → ChromaDB)
2. NER (SpaCy → crea nodi Person/Location + edge MENTIONS)
3. EXIF foto (Pillow → estrae GPS, data, camera)
4. Face detection (YOLOv8n → rileva volti, aggiunge metadati)
5. Scene detection video (PySceneDetect → crea nodi Event)

Tutti i modelli sono leggeri e girano su CPU.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from core.db.chroma_store import ChromaStore
from core.db.mariadb_store import MariaDBStore
from core.ingestion.metadata import _guess_mime, MIME_MAP

logger = logging.getLogger(__name__)

# ─── Text embedding ──────────────────────────────────────────────────


def process_image_embedding(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
    chroma: ChromaStore,
) -> bool:
    """Genera embedding visivo CLIP per immagini e salva in ChromaDB.

    Legge l'immagine, calcola embedding con CLIP (ViT-B/32, 512-dim),
    lo salva nella collezione image_embeddings per ricerca cross-modale.

    Returns:
        True se embedding generato con successo.
    """
    path = Path(file_path)
    mime = _guess_mime(path)

    # Solo immagini (escludi SVG vettoriale)
    if not mime.startswith("image/") or mime == "image/svg+xml":
        return False

    node = db.get_node(node_id)
    if not node:
        return False

    meta = {
        "node_id": node_id,
        "file_name": node.get("label", path.name),
        "mime_type": mime,
        "extension": path.suffix.lower(),
    }

    return chroma.index_image(node_id, str(path), metadata=meta)


def process_embedding(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
    chroma: ChromaStore,
) -> bool:
    """Genera embedding semantico per file di testo.

    Legge il file, calcola embedding con MiniLM, salva in ChromaDB.
    """
    path = Path(file_path)
    mime = _guess_mime(path)

    # Solo file di testo
    if not mime.startswith("text/"):
        return False

    # Prende metadati del nodo dal DB
    node = db.get_node(node_id)
    if not node:
        return False

    meta = {
        "node_id": node_id,
        "file_name": node.get("label", path.name),
        "mime_type": mime,
        "extension": path.suffix.lower(),
    }

    try:
        text = path.read_text("utf-8", errors="replace")
    except Exception as e:
        logger.warning("Impossibile leggere %s: %s", path, e)
        return False

    if len(text.strip()) < 20:
        return False  # file troppo corto

    return chroma.index_text(node_id, text, metadata=meta)


# ─── NER (SpaCy) ─────────────────────────────────────────────────────


def process_ner(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
) -> int:
    """Estrae entità nominate da un file di testo con SpaCy.

    Crea nodi Person/Location e edge MENTIONS dal File all'entità.

    Returns:
        Numero di entità trovate.
    """
    try:
        from egida.ner_light import scan_file
    except ImportError:
        logger.warning("SpaCy non disponibile per NER")
        return 0

    path = Path(file_path)
    mime = _guess_mime(path)

    if not mime.startswith("text/"):
        return 0

    entities = scan_file(str(path))
    if not entities:
        return 0

    found = 0
    for ent in entities:
        label = ent["text"]
        ent_type = "Person" if ent["label"] == "PERSON" else "Location"

        # Cerca se nodo già esistente con stessa label e tipo
        existing = db._query(
            "SELECT id FROM nodes WHERE type = %s AND label = %s LIMIT 1",
            (ent_type, label),
        )

        if existing:
            target_id = existing[0]["id"]
        else:
            target_id = db.create_node(
                node_type=ent_type,
                label=label,
                metadata={"source_ner": ent["label"], "extracted_from": node_id},
            )
            logger.debug("Nuovo nodo %s: %s", ent_type, label)

        # Edge MENTIONS (evita duplicati)
        existing_edge = db._query(
            "SELECT id FROM edges WHERE source_id = %s AND target_id = %s AND relation = 'MENTIONS'",
            (node_id, target_id),
        )
        if not existing_edge:
            db.create_edge(
                source_id=node_id,
                target_id=target_id,
                relation="MENTIONS",
                weight=1.0,
                metadata={"ner_label": ent["label"]},
            )
        found += 1

    if found:
        logger.info("NER: %d entità in %s", found, file_path)

    return found


# ─── EXIF (foto) ─────────────────────────────────────────────────────


def process_exif(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
) -> bool:
    """Estrae metadati EXIF da immagini.

    Data scatto, GPS, make/model camera → aggiorna metadati del nodo.
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    if ext not in MIME_MAP or not MIME_MAP[ext].startswith("image/"):
        return False

    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
    except ImportError:
        logger.debug("Pillow non installato, salto EXIF")
        return False

    try:
        img = Image.open(path)
        exif_data = img._getexif()
        if not exif_data:
            return False

        decoded = {}
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            decoded[tag] = value

        # Estrai campi utili
        meta_update = {}
        if "DateTimeOriginal" in decoded:
            meta_update["date_taken"] = decoded["DateTimeOriginal"]
        if "Make" in decoded:
            meta_update["camera_make"] = decoded["Make"]
        if "Model" in decoded:
            meta_update["camera_model"] = decoded["Model"]

        # GPS
        if "GPSInfo" in decoded:
            gps = decoded["GPSInfo"]
            lat = _dms_to_decimal(gps.get(2), gps.get(1))
            lon = _dms_to_decimal(gps.get(4), gps.get(3))
            if lat and lon:
                meta_update["gps_lat"] = lat
                meta_update["gps_lon"] = lon

        if meta_update:
            # Leggi metadata corrente e unisci
            node = db.get_node(node_id)
            current_meta = node.get("metadata") or {}
            if isinstance(current_meta, str):
                import json
                current_meta = json.loads(current_meta) if current_meta else {}
            current_meta.update(meta_update)
            db.update_node(node_id, metadata=current_meta)
            logger.debug("EXIF aggiornato per %s: %s", file_path, meta_update)
            return True

    except Exception as e:
        logger.debug("Errore EXIF %s: %s", file_path, e)

    return False


def _dms_to_decimal(dms, ref):
    """Converte coordinate DMS (gradi, minuti, secondi) in decimali."""
    if not dms or not ref:
        return None
    try:
        degrees, minutes, seconds = dms
        decimal = float(degrees) + float(minutes) / 60.0 + float(seconds) / 3600.0
        if ref in ("S", "W"):
            decimal = -decimal
        return round(decimal, 6)
    except (TypeError, ValueError):
        return None


# ─── Placeholder per funzioni future ────────────────────────────────


def process_face_detection(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
) -> bool:
    """Rileva volti/persone in immagini con YOLOv8n (CPU).

    Usa il modello YOLOv8n (ultralytics) per rilevare persone (classe 0)
    nelle immagini. Per ogni persona rilevata:
    - Crea un nodo Person (se non già esistente per quella foto)
    - Aggiorna metadati del file con face_count e bounding box
    - Crea edge MENTIONS tra File e Person

    Returns:
        True se almeno una persona rilevata.
    """
    path = Path(file_path)
    mime = _guess_mime(path)

    # Solo immagini
    if not mime.startswith("image/"):
        return False

    # Escludi SVG (vettoriale, non elaborabile con OpenCV)
    if mime == "image/svg+xml":
        return False

    try:
        from ultralytics import YOLO
        import cv2
    except ImportError:
        logger.debug("ultralytics o cv2 non installati, salto face detection")
        return False

    # Carica YOLOv8n (lazy, si scarica al primo uso)
    try:
        model = YOLO("yolov8n.pt")
    except Exception as e:
        logger.warning("Errore caricamento YOLO: %s", e)
        return False

    # Leggi immagine
    img = cv2.imread(str(path))
    if img is None:
        logger.debug("Impossibile leggere immagine: %s", path)
        return False

    # Esegui detection (solo classe 'person' = 0, conf minima 0.5)
    results = model(img, classes=[0], conf=0.5, verbose=False)

    detections = []
    for result in results:
        boxes = result.boxes
        if boxes is None:
            continue
        for i in range(len(boxes)):
            cls_id = int(boxes.cls[i].item())
            conf = float(boxes.conf[i].item())
            x1, y1, x2, y2 = boxes.xyxy[i].tolist()
            detections.append({
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                "confidence": round(conf, 3),
                "class_id": cls_id,
            })

    if not detections:
        return False

    face_count = len(detections)
    logger.info("Face detection: %d persone in %s", face_count, file_path)

    # Aggiorna metadati del nodo File
    node = db.get_node(node_id)
    if node:
        import json
        current_meta = node.get("metadata") or {}
        if isinstance(current_meta, str):
            current_meta = json.loads(current_meta) if current_meta else {}
        current_meta["face_count"] = face_count
        current_meta["face_bboxes"] = detections
        current_meta["has_faces"] = True
        # update_node accetta solo valori semplici, serializziamo il dict
        db._execute(
            "UPDATE nodes SET metadata = %s WHERE id = %s",
            (json.dumps(current_meta), node_id),
        )

    # Crea nodi Person (uno per volto rilevato)
    for idx, det in enumerate(detections):
        person_label = f"Person_in_{path.stem}_{idx}"
        bbox_str = f"({det['bbox'][0]},{det['bbox'][1]},{det['bbox'][2]},{det['bbox'][3]})"
        person_meta = {
            "source": "face_detection",
            "file_node_id": node_id,
            "bbox": bbox_str,
            "confidence": det["confidence"],
            "photo": file_path,
        }

        # Cerca nodo Person esistente per questa foto + idx
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

        # Edge CONTAINS (File -> Person)
        existing_edge = db._query(
            "SELECT id FROM edges WHERE source_id = %s AND target_id = %s AND relation = 'CONTAINS'",
            (node_id, person_id),
        )
        if not existing_edge:
            db.create_edge(
                source_id=node_id,
                target_id=person_id,
                relation="CONTAINS",
                weight=round(det["confidence"], 2),
                metadata={"bbox": bbox_str, "confidence": det["confidence"]},
            )

    return True


def process_scene_detection(node_id: str, file_path: str, db: MariaDBStore) -> bool:
    """Scene detection video con PySceneDetect.

    Rileva cambi di scena in file video usando AdaptiveDetector.
    Per ogni scena rilevata:
      1. Salva un keyframe JPEG in data/keyframes/<node_id>/scene_N.jpg
      2. Crea un nodo Event con metadati temporali
      3. Collega File video -> Event con edge HAS_SCENE

    Returns:
        True se almeno una scena rilevata e processata.
    """
    path = Path(file_path)
    mime = _guess_mime(path)

    # Solo video
    if not mime.startswith("video/"):
        return False

    try:
        from scenedetect import open_video, SceneManager, AdaptiveDetector
        import cv2
    except ImportError:
        logger.debug("PySceneDetect non installato, salto scene detection")
        return False

    # Apri video
    try:
        video = open_video(str(path))
    except Exception as e:
        logger.warning("Impossibile aprire video %s: %s", file_path, e)
        return False

    duration_sec = video.duration.get_seconds()
    if duration_sec < 1.0:
        logger.debug("Video troppo corto: %s (%.1fs)", file_path, duration_sec)
        return False

    # Rileva scene con AdaptiveDetector
    detector = AdaptiveDetector(adaptive_threshold=3.0,
                                min_scene_len=1.0)  # scene >= 1 secondo
    scene_manager = SceneManager()
    scene_manager.add_detector(detector)

    try:
        scene_manager.detect_scenes(video)
    except Exception as e:
        logger.warning("Errore detection scene per %s: %s", file_path, e)
        return False

    scene_list = scene_manager.get_scene_list()
    if not scene_list:
        logger.debug("Nessuna scena rilevata in %s", file_path)
        return False

    n_scenes = len(scene_list)
    logger.info("Scene detection: %d scene in %s (%.1fs)", n_scenes, file_path, duration_sec)

    # Directory keyframes
    keyframes_dir = Path("data/keyframes") / node_id
    keyframes_dir.mkdir(parents=True, exist_ok=True)

    # Aggiorna metadati del File video
    node = db.get_node(node_id)
    if node:
        import json
        current_meta = node.get("metadata") or {}
        if isinstance(current_meta, str):
            current_meta = json.loads(current_meta) if current_meta else {}

        current_meta["has_scenes"] = True
        current_meta["scene_count"] = n_scenes
        current_meta["scene_duration_sec"] = duration_sec
        current_meta["scene_detect_model"] = "AdaptiveDetector"
        current_meta["scene_timestamps"] = [
            {
                "index": i,
                "start": s.get_timecode(),
                "end": e.get_timecode(),
                "start_sec": s.get_seconds(),
                "end_sec": e.get_seconds(),
                "duration_sec": round(e.get_seconds() - s.get_seconds(), 1),
            }
            for i, (s, e) in enumerate(scene_list)
        ]

        db._execute(
            "UPDATE nodes SET metadata = %s WHERE id = %s",
            (json.dumps(current_meta), node_id),
        )

    # Processa ogni scena: keyframe + nodo Event
    for idx, (start, end) in enumerate(scene_list):
        start_sec = start.get_seconds()
        end_sec = end.get_seconds()
        duration = round(end_sec - start_sec, 1)

        # Salva keyframe (primo frame della scena)
        keyframe_path = keyframes_dir / f"scene_{idx:04d}.jpg"
        keyframe_saved = False
        try:
            video.seek(start)
            frame = video.read()
            if frame is not None:
                # PySceneDetect legge in RGB, cv2 salva in BGR
                if len(frame.shape) == 3 and frame.shape[2] == 3:
                    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                else:
                    frame_bgr = frame
                cv2.imwrite(str(keyframe_path), frame_bgr)
                keyframe_saved = True
        except Exception as e:
            logger.debug("Errore salvataggio keyframe scena %d: %s", idx, e)

        # Crea nodo Event
        event_label = f"{path.stem}_scene_{idx:04d}"
        event_meta = {
            "source": "scene_detection",
            "source_video_id": node_id,
            "scene_index": idx,
            "start_time_sec": start_sec,
            "end_time_sec": end_sec,
            "duration_sec": duration,
            "start_timecode": start.get_timecode(),
            "end_timecode": end.get_timecode(),
            "video_path": str(path),
            "video_label": node.get("label", path.name) if node else path.name,
        }

        if keyframe_saved:
            event_meta["keyframe_path"] = str(keyframe_path)

        # Evita duplicati: cerca se esiste già Event per questa scena
        existing = db._query(
            "SELECT id FROM nodes WHERE type = 'Event' AND label = %s LIMIT 1",
            (event_label,),
        )

        if existing:
            event_id = existing[0]["id"]
        else:
            event_id = db.create_node(
                node_type="Event",
                label=event_label,
                metadata=event_meta,
            )

        # Edge HAS_SCENE (File -> Event) — evita duplicati
        existing_edge = db._query(
            "SELECT id FROM edges WHERE source_id = %s AND target_id = %s AND relation = 'HAS_SCENE'",
            (node_id, event_id),
        )
        if not existing_edge:
            db.create_edge(
                source_id=node_id,
                target_id=event_id,
                relation="HAS_SCENE",
                weight=duration,
                metadata={
                    "scene_index": idx,
                    "start_time": start_sec,
                    "end_time": end_sec,
                },
            )

    return True


# ─── Geocoding GPS ────────────────────────────────────────────────

import json as _json
import os as _os
from pathlib import Path as _Path
from typing import Optional as _Optional

_GEOCODE_CACHE_PATH = _Path("data/geocode_cache.json")
_GEOCODE_CACHE: dict = {}


def _load_geocode_cache() -> dict:
    """Carica la cache delle geocoding (coordinate → indirizzo)."""
    global _GEOCODE_CACHE
    if _GEOCODE_CACHE:
        return _GEOCODE_CACHE
    if _GEOCODE_CACHE_PATH.exists():
        try:
            _GEOCODE_CACHE = _json.loads(_GEOCODE_CACHE_PATH.read_text("utf-8"))
        except Exception:
            _GEOCODE_CACHE = {}
    return _GEOCODE_CACHE


def _save_geocode_cache():
    """Salva la cache delle geocoding su disco."""
    _GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _GEOCODE_CACHE_PATH.write_text(_json.dumps(_GEOCODE_CACHE, indent=2), "utf-8")


def _reverse_geocode(lat: float, lon: float) -> _Optional[dict]:
    """Reverse geocoding via Nominatim (con caching).

    Args:
        lat: Latitudine.
        lon: Longitudine.

    Returns:
        Dict con place names o None se fallito.
        Formato: {
            "display_name": str,
            "name": str (il nome piu' specifico),
            "city": str,
            "region": str,
            "country": str,
            "type": str (es. "house", "street", "city"),
        }
    """
    cache = _load_geocode_cache()
    key = f"{lat:.6f},{lon:.6f}"

    if key in cache:
        logger.debug("Geocoding cache hit: %s", key)
        return cache[key]

    try:
        import requests as _requests

        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            "lat": lat,
            "lon": lon,
            "format": "json",
            "addressdetails": 1,
            "zoom": 18,
        }
        headers = {"User-Agent": "OraclePenelope/1.0 (research project)"}

        resp = _requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning("Nominatim error %d per %.4f,%.4f", resp.status_code, lat, lon)
            return None

        data = resp.json()
        if "error" in data:
            logger.debug("Nominatim: nessun risultato per %.4f,%.4f", lat, lon)
            return None

        addr = data.get("address", {})
        result = {
            "display_name": data.get("display_name", ""),
            "name": data.get("name", "") or addr.get("house_number", "") + " " + addr.get("road", ""),
            "city": addr.get("city") or addr.get("town") or addr.get("village") or addr.get("hamlet", ""),
            "region": addr.get("state") or addr.get("region", ""),
            "country": addr.get("country", ""),
            "country_code": addr.get("country_code", ""),
            "type": data.get("type", ""),
            "category": data.get("category", ""),
        }

        # Salva in cache
        cache[key] = result
        _save_geocode_cache()
        logger.info("Geocoding: %.4f,%.4f → %s", lat, lon, result.get("display_name", "?")[:80])
        return result

    except ImportError:
        logger.debug("requests non installato, salto geocoding")
        return None
    except Exception as e:
        logger.warning("Errore geocoding %.4f,%.4f: %s", lat, lon, e)
        return None


def process_geocoding(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
) -> bool:
    """Geocoding inverso: coordinate GPS → Location node.

    Legge gps_lat/gps_lon dai metadati del file (estratti da EXIF),
    chiama Nominatim per l'indirizzo, crea/collega un nodo Location.

    Returns:
        True se geocoding eseguito e Location collegata.
    """
    node = db.get_node(node_id)
    if not node:
        return False

    meta = node.get("metadata") or {}
    if isinstance(meta, str):
        meta = _json.loads(meta) if meta else {}

    lat = meta.get("gps_lat")
    lon = meta.get("gps_lon")

    if lat is None or lon is None:
        return False

    # Reverse geocoding
    place = _reverse_geocode(float(lat), float(lon))
    if not place:
        return False

    # Costruisci label per Location node
    location_name = place.get("name", "").strip()
    if not location_name:
        location_name = place.get("city", "") or place.get("region", "") or place.get("country", "")
    if not location_name:
        location_name = f"{lat:.4f},{lon:.4f}"

    # Crea Location label piu' specifica possibile
    city = place.get("city", "")
    region = place.get("region", "")
    country = place.get("country", "")
    location_label = location_name
    if city and city != location_name:
        location_label = f"{location_name}, {city}"
    elif region and region != location_name:
        location_label = f"{location_label}, {region}" if location_name else region

    # Arricchisci con gerarchia
    hierarchy = ", ".join(filter(None, [location_name, city, region, country]))

    # Cerca Location node esistente (per coordinate o per nome)
    existing = db._query(
        "SELECT id FROM nodes WHERE type = 'Location' AND (metadata LIKE %s OR label = %s) LIMIT 1",
        (f"%\"lat\": {lat:.4f}%", hierarchy[:200]),
    )

    if existing:
        loc_id = existing[0]["id"]
        logger.debug("Location esistente: %s → %s", location_label, loc_id)
    else:
        loc_meta = {
            "source": "geocoding",
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6),
            "display_name": place.get("display_name", ""),
            "city": city,
            "region": region,
            "country": country,
            "country_code": place.get("country_code", ""),
            "osm_type": place.get("type", ""),
            "osm_category": place.get("category", ""),
            "from_file": node_id,
            "hierarchy": hierarchy,
        }
        loc_id = db.create_node(
            node_type="Location",
            label=hierarchy[:200] or location_label,
            metadata=loc_meta,
        )
        logger.info("Location creata: %s (%s)", hierarchy[:60], loc_id[:12])

    # Edge LOCATED_AT (File -> Location) — evita duplicati
    existing_edge = db._query(
        "SELECT id FROM edges WHERE source_id = %s AND target_id = %s AND relation = 'LOCATED_AT'",
        (node_id, loc_id),
    )
    if not existing_edge:
        db.create_edge(
            source_id=node_id,
            target_id=loc_id,
            relation="LOCATED_AT",
            weight=1.0,
            metadata={"lat": round(float(lat), 6), "lon": round(float(lon), 6)},
        )

    # Aggiorna metadati del file con l'indirizzo
    if isinstance(meta, dict) and "geocoded_address" not in meta:
        meta["geocoded_address"] = place.get("display_name", "")[:200]
        meta["geocoded_city"] = city
        meta["geocoded_country"] = country
        db._execute(
            "UPDATE nodes SET metadata = %s WHERE id = %s",
            (_json.dumps(meta), node_id),
        )

    return True


# ─── Event nodes da data ─────────────────────────────────────────

import re as _re
from datetime import datetime as _datetime


def _extract_date_from_filename(filename: str) -> Optional[str]:
    """Estrae data in formato YYYY-MM-DD dal nome file.

    Pattern riconosciuti:
      - YYYYMMDD (8 cifre consecutive, es. IMG-20201224-WA0011)
      - YYYY-MM-DD (es. 2020-12-24)
    """
    # Pattern 1: YYYYMMDD
    m = _re.search(r'(\d{4})(\d{2})(\d{2})', filename)
    if m:
        y, mo, d = m.groups()
        try:
            dt = _datetime(int(y), int(mo), int(d))
            if 2000 <= dt.year <= 2030:
                return dt.strftime('%Y-%m-%d')
        except ValueError:
            pass

    # Pattern 2: YYYY-MM-DD
    m = _re.search(r'(\d{4})-(\d{2})-(\d{2})', filename)
    if m:
        y, mo, d = m.groups()
        try:
            dt = _datetime(int(y), int(mo), int(d))
            if 2000 <= dt.year <= 2030:
                return dt.strftime('%Y-%m-%d')
        except ValueError:
            pass

    return None


def process_date_event(
    node_id: str,
    file_path: str,
    db: MariaDBStore,
) -> bool:
    """Crea/collega un file a un nodo Event in base alla sua data.

    Cerca la data in questo ordine:
    1. Nome file (pattern IMG-YYYYMMDD-*, YYYYMMDD_*, ecc.)
    2. EXIF DateTimeOriginal (se presente nei metadati)
    3. Data di modifica del file (filesystem)

    Poi trova o crea un nodo Event per quella data e collega
    il file con un edge CREATED_AT.

    Returns:
        True se Event trovato/creato e collegato.
    """
    path = Path(file_path)
    label = path.stem  # nome senza estensione
    import json as _json

    date_str = None
    source = None

    # 1. Data dal nome file
    date_str = _extract_date_from_filename(label)
    if date_str:
        source = "from_filename"

    # 2. Fallback: EXIF date_taken dai metadati
    if not date_str:
        node = db.get_node(node_id)
        if node:
            meta = node.get("metadata") or {}
            if isinstance(meta, str):
                meta = _json.loads(meta) if meta else {}
            dt_raw = meta.get("date_taken")
            if dt_raw:
                try:
                    dt = _datetime.strptime(str(dt_raw)[:10], "%Y:%m:%d")
                    if 2000 <= dt.year <= 2030:
                        date_str = dt.strftime('%Y-%m-%d')
                        source = "from_exif"
                except ValueError:
                    pass

    # 3. Fallback: data di modifica del file
    if not date_str:
        try:
            mtime = path.stat().st_mtime
            dt = _datetime.fromtimestamp(mtime)
            if 2000 <= dt.year <= 2030:
                date_str = dt.strftime('%Y-%m-%d')
                source = "from_filesystem"
        except (OSError, ValueError):
            pass

    if not date_str:
        return False

    # Crea o trova nodo Event per questa data
    event_label = f"Event_{date_str}"

    existing_event = db._query(
        "SELECT id, metadata FROM nodes WHERE type = 'Event' AND label = %s LIMIT 1",
        (event_label,),
    )

    if existing_event:
        event_id = existing_event[0]["id"]
        # Aggiorna metadata: incrementa file_count
        emeta = existing_event[0].get("metadata")
        if isinstance(emeta, str):
            emeta = _json.loads(emeta) if emeta else {}
        if isinstance(emeta, dict):
            emeta["file_count"] = emeta.get("file_count", 0) + 1
            files = emeta.get("source_files", [])
            if isinstance(files, list) and node_id not in files:
                files.append(node_id)
                emeta["source_files"] = files
            db._execute(
                "UPDATE nodes SET metadata = %s WHERE id = %s",
                (_json.dumps(emeta), event_id),
            )
    else:
        event_meta = {
            "date": date_str,
            "date_type": source,
            "year": int(date_str[:4]),
            "month": int(date_str[5:7]),
            "day": int(date_str[8:10]),
            "file_count": 1,
            "source_files": [node_id],
        }
        event_id = db.create_node(
            node_type="Event",
            label=event_label,
            metadata=event_meta,
        )
        logger.info("Evento creato: %s (%s)", event_label, date_str)

    # Edge CREATED_AT (File -> Event) — evita duplicati
    existing_edge = db._query(
        "SELECT id FROM edges WHERE source_id = %s AND target_id = %s AND relation = 'CREATED_AT'",
        (node_id, event_id),
    )
    if not existing_edge:
        db.create_edge(
            source_id=node_id,
            target_id=event_id,
            relation="CREATED_AT",
            weight=1.0,
            metadata={"date": date_str, "source": source},
        )

    return True

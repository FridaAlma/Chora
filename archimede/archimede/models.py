"""Modelli dati condivisi per Archimede in Oracle.

questi modelli sono focalizzati su: foto, persone, matching facciale e risultati di
query sul grafo Penelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Photo:
    """Una foto indicizzata nel grafo Penelope."""
    node_id: str
    file_path: str
    file_name: str
    mime_type: str = ""
    size_bytes: int = 0
    sha256: str = ""
    device: str = ""
    date_taken: str = ""          # da EXIF
    face_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectedFace:
    """Volto rilevato in una foto con embedding."""
    photo_path: str
    photo_node_id: str
    bbox: list[int]               # [x1, y1, x2, y2]
    confidence: float = 0.0
    embedding: list[float] | None = None   # 512-dim ArcFace
    gender: int | None = None     # 0=female, 1=male
    age: float | None = None
    person_node_id: str | None = None  # nodo Person in Penelope (se esiste)


@dataclass
class ReferenceFace:
    """Volto di referenza per una persona da cercare."""
    name: str                     # es. "papa", "mamma"
    embedding: list[float]        # embedding medio 512-dim
    source_photos: list[str] = field(default_factory=list)


@dataclass
class FaceMatch:
    """Risultato del matching tra un volto e una referenza."""
    reference_name: str
    similarity: float             # cosine similarity (0-1)
    is_match: bool                # sopra soglia
    photo_path: str
    bbox: list[int]


@dataclass
class PhotoMatchResult:
    """Risultato per una singola foto."""
    photo: Photo
    faces: list[DetectedFace]
    matches: dict[str, bool]      # {"papa": True, "mamma": False}
    match_details: list[FaceMatch]
    is_couple: bool               # True se TUTTE le referenze matchano


@dataclass
class SearchReport:
    """Report completo della ricerca."""
    query_name: str = ""
    reference_names: list[str] = field(default_factory=list)
    similarity_threshold: float = 0.35
    photos_scanned: int = 0
    photos_with_faces: int = 0
    couple_photos: list[PhotoMatchResult] = field(default_factory=list)
    single_parent_photos: dict[str, list[PhotoMatchResult]] = field(default_factory=dict)
    all_results: list[PhotoMatchResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    generated_at: str = ""

    @property
    def couple_count(self) -> int:
        return len(self.couple_photos)

    @property
    def total_faces_detected(self) -> int:
        return sum(r.photo.face_count for r in self.all_results)

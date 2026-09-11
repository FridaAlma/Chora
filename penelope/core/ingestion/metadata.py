"""
Estrazione metadati base da file del filesystem.

Leggeri: SHA-256, size, date, mime-type. Nessuna AI qui.
"""

import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class FileMetadata:
    """Metadati base di un file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

        stat = self.path.stat()
        self.size_bytes: int = stat.st_size
        self.created_ts: float = stat.st_ctime
        self.modified_ts: float = stat.st_mtime
        self.extension: str = self.path.suffix.lower()
        self.file_name: str = self.path.name

        # Hash lazy (calcolato solo su richiesta)
        self._sha256: Optional[str] = None

    @property
    def sha256(self) -> str:
        if self._sha256 is None:
            self._sha256 = _compute_sha256(self.path)
        return self._sha256

    @property
    def mime_type(self) -> str:
        """MIME type basato su estensione (leggero, senza leggere il file)."""
        return _guess_mime(self.path)

    @property
    def category(self) -> str:
        """Categoria file: image, video, document, other."""
        return classify_category(self.mime_type)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "file_name": self.file_name,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "category": self.category,
            "created": datetime.fromtimestamp(self.created_ts).isoformat(),
            "modified": datetime.fromtimestamp(self.modified_ts).isoformat(),
        }


# ─── Classificazione categoria file ────────────────────────────────

def classify_category(mime_type: str, path: Optional[Path] = None) -> str:
    """Classifica un file in image|video|document|other.

    Usa prima il MIME type, poi fallback su estensione.
    """
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("text/") or mime_type in (
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
    ):
        return "document"
    # Fallback su estensione
    if path:
        ext = path.suffix.lower()
        if ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt"):
            return "document"
        if ext in (".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".env",
                    ".py", ".js", ".ts", ".html", ".css", ".sh", ".sql", ".c", ".cpp", ".h", ".java", ".go",
                    ".rs", ".rb", ".php"):
            return "document"
    return "other"


def _compute_sha256(path: Path, chunk_size: int = 65536) -> str:
    """Calcola SHA-256 di un file leggendo a chunk (memoria efficiente)."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
    except (IOError, PermissionError) as e:
        logger.warning("Errore lettura %s: %s", path, e)
        return ""
    return h.hexdigest()


# ─── MIME types mapping (leggero, senza python-magic) ────────────────

MIME_MAP = {
    # Testo / codice
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".html": "text/html",
    ".css": "text/css",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".toml": "text/toml",
    ".cfg": "text/plain",
    ".ini": "text/plain",
    ".env": "text/plain",
    ".sh": "text/x-shellscript",
    ".bat": "application/bat",
    ".ps1": "text/x-powershell",
    ".sql": "text/x-sql",
    ".c": "text/x-c",
    ".cpp": "text/x-c++",
    ".h": "text/x-c",
    ".java": "text/x-java",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".rb": "text/x-ruby",
    ".php": "text/x-php",
    ".r": "text/x-r",
    # Documenti
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    # Immagini
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    # Video
    ".mp4": "video/mp4",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".webm": "video/webm",
    # Audio
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
    # Archivi
    ".zip": "application/zip",
    ".rar": "application/x-rar-compressed",
    ".7z": "application/x-7z-compressed",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".bz2": "application/x-bzip2",
    # Database
    ".db": "application/x-sqlite3",
    ".sqlite": "application/x-sqlite3",
    ".sqlite3": "application/x-sqlite3",
    # Altro
    ".iso": "application/x-iso9660-image",
    ".torrent": "application/x-bittorrent",
    ".exe": "application/x-msdownload",
}


def _guess_mime(path: Path) -> str:
    """MIME type veloce per estensione."""
    return MIME_MAP.get(path.suffix.lower(), "application/octet-stream")

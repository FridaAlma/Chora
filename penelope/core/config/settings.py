"""
Configurazione centralizzata di Penelope.

La password del database NON viene mai tenuta in chiaro nei file:
1. (default) **Keyring** — Windows Credential Manager / macOS Keychain / Linux Secret Service
2. (fallback) **Variabile d'ambiente** PENELOPE_DB_PASSWORD
3. (ultima spiaggia) **File .env** — con warning

Usa: `penelope configure` per impostare la password nel keyring.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Carica .env per le configurazioni NON sensibili
load_dotenv()


# ─── Keyring helper ──────────────────────────────────────────────────

_KEYRING_SERVICE = "penelope"


def _get_password_from_keyring(username: str) -> Optional[str]:
    """Tenta di leggere la password dal keyring di sistema."""
    try:
        import keyring
        password = keyring.get_password(_KEYRING_SERVICE, username)
        if password:
            logger.debug("Password letta dal keyring (%s)", _KEYRING_SERVICE)
            return password
    except Exception as e:
        logger.debug("Keyring non disponibile: %s", e)
    return None


def _store_password_in_keyring(username: str, password: str) -> bool:
    """Salva la password nel keyring di sistema."""
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, username, password)
        logger.info("Password salvata nel keyring (%s)", _KEYRING_SERVICE)
        return True
    except Exception as e:
        logger.error("Impossibile salvare nel keyring: %s", e)
        return False


def _delete_password_from_keyring(username: str) -> bool:
    """Rimuove la password dal keyring."""
    try:
        import keyring
        keyring.delete_password(_KEYRING_SERVICE, username)
        logger.info("Password rimossa dal keyring")
        return True
    except Exception as e:
        logger.warning("Impossibile rimuovere dal keyring: %s", e)
        return False


# ─── Risoluzione password ────────────────────────────────────────────

def resolve_db_password() -> str:
    """
    Risolve la password del database con questo ordine:
    1. Keyring di sistema
    2. Variabile d'ambiente PENELOPE_DB_PASSWORD
    3. File .env (con warning in chiaro)
    """
    user = os.getenv("PENELOPE_DB_USER", "penelope")

    # 1. Keyring
    pw = _get_password_from_keyring(user)
    if pw:
        return pw

    # 2. Variabile d'ambiente
    pw = os.getenv("PENELOPE_DB_PASSWORD")
    if pw:
        return pw

    # 3. .env (già caricato da load_dotenv)
    #    Se siamo qui, la variabile d'ambiente è vuota ma potrebbe
    #    essere nel file .env — in realtà os.getenv l'ha già presa.
    #    Arriviamo qui solo se non impostata da nessuna parte.
    logger.warning(
        "⚠️  Password MariaDB non trovata né nel keyring né in env.\n"
        "   Imposta con: penelope configure --password\n"
        "   Oppure: set PENELOPE_DB_PASSWORD=... (variabile d'ambiente)"
    )
    return ""


# ─── MariaDB / SQLite ───────────────────────────────────────────────
# Backend: "mariadb" (default, richiede server SQL) o "sqlite" (locale, zero setup)
DB_BACKEND = os.getenv("PENELOPE_DB_BACKEND", "mariadb")

MARIADB_HOST = os.getenv("PENELOPE_DB_HOST", "localhost")
MARIADB_PORT = int(os.getenv("PENELOPE_DB_PORT", "3306"))
MARIADB_USER = os.getenv("PENELOPE_DB_USER", "penelope")
MARIADB_DATABASE = os.getenv("PENELOPE_DB_NAME", "penelope_rui")
MARIADB_POOL_SIZE = int(os.getenv("PENELOPE_DB_POOL_SIZE", "5"))

# SQLite (fallback locale, usa sqlite3 stdlib)
SQLITE_PATH = os.getenv("PENELOPE_SQLITE_PATH", "data/penelope.db")

# La password viene risolta al primo accesso (lazy, non a import time)
_MARIADB_PASSWORD: Optional[str] = None


def get_db_password() -> str:
    """Restituisce la password (risolta lazy e cachata)."""
    global _MARIADB_PASSWORD
    if _MARIADB_PASSWORD is None:
        _MARIADB_PASSWORD = resolve_db_password()
    return _MARIADB_PASSWORD


# ─── Storage paths da scandire ───────────────────────────────────────
# Configura fino a 5 dispositivi/path di storage
STORAGE_PATHS = {
    "device_1": os.getenv("PENELOPE_STORAGE_1", ""),
    "device_2": os.getenv("PENELOPE_STORAGE_2", ""),
    "device_3": os.getenv("PENELOPE_STORAGE_3", ""),
    "device_4": os.getenv("PENELOPE_STORAGE_4", ""),
    "device_5": os.getenv("PENELOPE_STORAGE_5", ""),
}

# Alias retrocompatibili (deprecati)
if not STORAGE_PATHS["device_1"]:
    STORAGE_PATHS["device_1"] = os.getenv("PENELOPE_PATH_HEADLESS", "")
if not STORAGE_PATHS["device_2"]:
    STORAGE_PATHS["device_2"] = os.getenv("PENELOPE_PATH_HDD_EXT", "")
if not STORAGE_PATHS["device_3"]:
    STORAGE_PATHS["device_3"] = os.getenv("PENELOPE_PATH_LAPTOP", "")

# ─── Egida (HSD) — ora 4° strato indipendente in Oracle/Egida/
# Le variabili d'ambiente sono state rinumerate (senza prefisso PENELOPE_):
#   EGIDA_QUARANTINE_DIR   (era PENELOPE_QUARANTINE)
#   EGIDA_SPACY_MODEL      (era PENELOPE_SPACY_MODEL)
#   EGIDA_THRESHOLD        (era PENELOPE_EGIDA_THRESHOLD)
#   EGIDA_NER_CONFIDENCE   (era PENELOPE_EGIDA_NER_CONFIDENCE)
# Vedi Oracle/Egida/config.py

# ─── ChromaDB ───────────────────────────────────────────────────────
CHROMADB_PATH = os.getenv("PENELOPE_CHROMA_PATH", "data/chroma")

# ─── DeepFace (face recognition locale) ─────────────────────────────
DEEPFACE_DETECTOR = os.getenv("DEEPFACE_DETECTOR", "opencv")
DEEPFACE_MODEL = os.getenv("DEEPFACE_MODEL", "Facenet")
DEEPFACE_CLUSTER_THRESHOLD = float(os.getenv("DEEPFACE_CLUSTER_THRESHOLD", "0.5"))
EMBEDDINGS_DIR = os.getenv("PENELOPE_EMBEDDINGS", "data/embeddings")

# ─── Scene detection keyframes ────────────────────────────────────
KEYFRAMES_DIR = os.getenv("PENELOPE_KEYFRAMES", "data/keyframes")

# ─── Altro ───────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("PENELOPE_LOG_LEVEL", "INFO")
SCAN_BATCH_SIZE = int(os.getenv("PENELOPE_SCAN_BATCH_SIZE", "1000"))

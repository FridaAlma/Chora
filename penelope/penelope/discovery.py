"""
Device auto-discovery via marker files.

Ogni storage path (PENELOPE_STORAGE_N) può avere un marker JSON nascosto
alla radice — `.penelope_device.json` — che lo identifica univocamente
come device registrato. Questo elimina la necessità di `device register`
manuale: basta un `python run.py --init` iniziale, e ogni avvio successivo
riconosce automaticamente i device tramite reconcile_devices().

Marker file structure (.penelope_device.json):
    {
        "device_id": 1,
        "label": "device_1",
        "created_at": "2025-01-15T10:30:00"
    }

Tutte le funzioni sono leggere: niente hashing, niente ricorsione.
Solo I/O su un singolo file JSON nascosto per mountpoint.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MARKER_FILENAME = ".penelope_device.json"


# ─── Lettura / Scrittura marker ────────────────────────────────────


def write_device_marker(path: str | Path, device_id: int, label: str) -> bool:
    """Scrive il marker .penelope_device.json alla radice del path.

    Args:
        path: Directory radice dove scrivere il marker.
        device_id: ID del device nel DB.
        label: Label del device (es. 'device_1').

    Returns:
        True se scritto correttamente, False altrimenti.
    """
    root = Path(path)
    if not root.is_dir():
        logger.warning("write_device_marker: path non è una directory: %s", root)
        return False

    marker_path = root / _MARKER_FILENAME
    payload = {
        "device_id": device_id,
        "label": label,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        marker_path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        # Nascosto su Unix
        if not marker_path.name.startswith("."):
            # Su alcuni OS il nome inizia già con '.', ma rendiamo hidden su Windows
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(
                    str(marker_path), 2  # FILE_ATTRIBUTE_HIDDEN
                )
            except (ImportError, AttributeError, OSError):
                pass  # non-Windows, . già basta
        logger.debug("Marker scritto: %s → device_id=%s label=%s", marker_path, device_id, label)
        return True
    except (OSError, PermissionError) as e:
        logger.warning("Impossibile scrivere marker in %s: %s", marker_path, e)
        return False


def read_device_marker(path: str | Path) -> Optional[dict]:
    """Legge il marker .penelope_device.json alla radice del path.

    Args:
        path: Directory radice dove cercare il marker.

    Returns:
        Dict con 'device_id', 'label', 'created_at' oppure None se
        il file non esiste, è malformato, o non è accessibile.
    """
    root = Path(path)
    marker_path = root / _MARKER_FILENAME

    if not marker_path.exists():
        return None
    if not marker_path.is_file():
        return None

    try:
        raw = marker_path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        data = json.loads(raw)
        # Validazione minima: deve avere device_id (int) e label (str)
        if not isinstance(data.get("device_id"), int) or not isinstance(data.get("label"), str):
            logger.warning("Marker malformato in %s: campi mancanti o tipo errato", marker_path)
            return None
        return data
    except (json.JSONDecodeError, OSError, PermissionError) as e:
        logger.debug("Impossibile leggere marker %s: %s", marker_path, e)
        return None


# ─── Reconcilation (chiamata ad ogni avvio) ────────────────────────
# (Fase 3 — reconcile_devices qui, chiamata da start_penelope)


def reconcile_devices(db) -> list[dict]:
    """Enumera mountpoint attivi via psutil e aggiorna device_mounts.

    Per ogni mountpoint attivo, cerca .penelope_device.json alla radice.
    Se trovato, fa upsert_device_mount(device_id, hostname, mountpoint).

    Args:
        db: Istanza di MariaDBStore (o mock).

    Returns:
        Lista di dict con {device_id, device_label, mountpoint}
        per ogni device il cui mount è stato aggiornato/creato in questa run.
    """
    updated: list[dict] = []

    try:
        import psutil
    except ImportError as e:
        logger.warning("psutil non installato — reconcile_devices saltato. pip install psutil")
        return updated

    hostname = db.current_hostname()

    try:
        partitions = psutil.disk_partitions(all=True)
    except Exception as e:
        logger.warning("psutil.disk_partitions fallito (%s) — reconcile_devices saltato", e)
        return updated

    for part in partitions:
        mountpoint = part.mountpoint
        if not mountpoint or not mountpoint.strip():
            continue

        try:
            marker = read_device_marker(mountpoint)
        except Exception as e:
            logger.debug("reconcile_devices: errore leggendo marker in %s: %s", mountpoint, e)
            continue

        if marker is None:
            continue

        device_id = marker["device_id"]
        label = marker.get("label", f"device_{device_id}")

        try:
            db.upsert_device_mount(device_id, hostname, mountpoint)
            logger.debug("Device reconciled: %s (id=%s) → %s @ %s", label, device_id, hostname, mountpoint)
            updated.append({
                "device_id": device_id,
                "device_label": label,
                "mountpoint": mountpoint,
            })
        except Exception as e:
            logger.warning("reconcile_devices: upsert fallito per device %s su %s: %s",
                           label, mountpoint, e)
            continue

    return updated


# ─── Persistenza storage path in .env ────────────────────────────


def ensure_storage_in_env(path: str | Path) -> str:
    """Persiste un path in un libero slot PENELOPE_STORAGE_N nel .env.

    Cerca il primo slot vuoto (PENELOPE_STORAGE_1..5) e vi scrive il path.
    Se il path è già presente in uno slot, restituisce la label_key esistente.
    Se tutti gli slot sono occupati e il path non è già registrato, solleva ValueError.

    Args:
        path: Path della directory da registrare.

    Returns:
        La label_key del device (es. 'device_2').

    Raises:
        ValueError: se tutti gli slot sono occupati e il path è nuovo.
    """
    import os as _os
    from penelope.config import settings as _settings

    target_path = str(Path(path).resolve())
    env_path = Path(_settings.__file__).resolve().parent.parent.parent / ".env"

    if not env_path.exists():
        raise FileNotFoundError(f".env non trovato: {env_path}")

    raw = env_path.read_text(encoding="utf-8")
    lines = raw.splitlines(keepends=True)

    # Mappa slot var -> label_key
    slot_keys = {f"PENELOPE_STORAGE_{i}": f"device_{i}" for i in range(1, 6)}

    # 1. Verifica se il path è già in uno slot
    for var, label_key in slot_keys.items():
        current = _os.environ.get(var, "")
        if not current:
            for line in lines:
                if line.startswith(f"{var}="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        current = val
                    break
        if current:
            try:
                if Path(current).resolve() == Path(target_path):
                    logger.debug("Path %s già in slot %s (%s)", target_path, var, label_key)
                    return label_key
            except (OSError, PermissionError):
                if current == target_path:
                    return label_key

    # 2. Trova primo slot vuoto
    used = set()
    for var in slot_keys:
        val = _os.environ.get(var, "")
        if val:
            used.add(var)
            continue
        for line in lines:
            if line.startswith(f"{var}="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    used.add(var)
                break

    for var, label_key in slot_keys.items():
        if var not in used:
            new_line = f"{var}={target_path}\n"
            new_lines = []
            written = False
            for line in lines:
                if line.startswith(f"{var}="):
                    new_lines.append(new_line)
                    written = True
                else:
                    new_lines.append(line)
            if not written:
                insert_idx = 0
                for idx, ln in enumerate(new_lines):
                    if ln.startswith(("PENELOPE_STORAGE_")):
                        insert_idx = idx + 1
                new_lines.insert(insert_idx, new_line)

            env_path.write_text("".join(new_lines), encoding="utf-8")
            _os.environ[var] = target_path  # aggiorna env corrente
            logger.info("Path %s scritto in slot %s", target_path, var)
            return label_key

    raise ValueError(
        f"Tutti gli slot PENELOPE_STORAGE_1..5 sono occupati. "
        f"Liberane uno o usa un path già registrato.\n"
        f"Path richiesto: {target_path}\n"
        f"Slot occupati: {', '.join(sorted(used))}"
    )
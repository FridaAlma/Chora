"""
Project boundary detection — pura logica, nessun I/O su DB.

Due funzioni pure per determinare dove un progetto inizia davvero,
ignorando cartelle-junk e involucri vuoti a un solo figlio.

Usata sia per lo scan live (``detect_project_boundary`` su filesystem)
sia per riparare dati già scritti male in DB (``detect_project_boundary_from_relpath``
su lista di path relativi già noti).

Costanti JUNK_NAMES: generali per qualunque OS, estendibili.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set

# ─── Junk names (case-insensitive) ──────────────────────────────────
# Ogni entry viene confrontata case-insensitive.
# Regola implicita: qualunque nome che inizi con '.' è junk (tranne '.' e '..').

JUNK_NAMES: frozenset[str] = frozenset({
    # Windows
    "$recycle.bin",
    "system volume information",
    "config.msi",
    "thumbs.db",
    "desktop.ini",
    "recycler",
    "recycled",
    # macOS
    ".trash",
    ".spotlight-v100",
    ".fseventsd",
    ".ds_store",
    ".localized",
    # Linux
    "lost+found",
})


# ─── Predicate ──────────────────────────────────────────────────────

def is_junk_name(name: str) -> bool:
    """True se *name* è un nome da saltare (junk system o nascosto).

    Regole:
    1. Case-insensitive match contro JUNK_NAMES.
    2. Qualunque nome che inizia con '.' (e non è '.' o '..').
    """
    if name in (".", ".."):
        return False
    if name.startswith("."):
        return True
    return name.casefold() in JUNK_NAMES


# ─── Su filesystem (Path) ──────────────────────────────────────────

def detect_project_boundary(root: Path) -> Path:
    """Cammina da *root* scendendo finché trova un wrapper a singolo figlio.

    Logica:
    - Se la cartella corrente ha ESATTAMENTE un figlio directory (ignorando junk)
      E zero file diretti (ignorando junk), allora scende in quel figlio e ripete.
    - Si ferma alla prima cartella con file diretti O con più di un figlio directory.
    - Non tocca mai il DB né il filesystem oltre a ``iterdir()``.

    Args:
        root: Path assoluto o relativo di partenza.

    Returns:
        Path del vero confine del progetto (root stesso se non è wrapper).
    """
    current = root

    while current.is_dir():
        # Legge contenuto una tantum
        entries = list(current.iterdir())

        dirs: list[Path] = []
        files: list[Path] = []

        for e in entries:
            if is_junk_name(e.name):
                continue
            if e.is_dir():
                dirs.append(e)
            elif e.is_file():
                files.append(e)

        # Se esattamente un figlio directory e zero file → wrapper: scendi
        if len(dirs) == 1 and len(files) == 0:
            current = dirs[0]
            continue

        # Altrimenti siamo al confine
        break

    return current


# ─── Su stringhe (no filesystem) ────────────────────────────────────
# Variante per riparare dati già in DB: riceve una lista di path relativi
# (es. ["storage/User/OldMemory/a.jpg", "storage/User/OldMemory/b.jpg"])
# e applica la stessa logica per trovare il vero root del progetto.

def _parse_virtual_tree(relpaths: List[str]) -> tuple[Set[str], List[str]]:
    """Versione migliorata: inferisce la gerarchia da una lista di path.

    Costruisce un set di directory esistenti: ogni path ``a/b/c.txt`` implica
    che ``a`` e ``a/b`` sono directory.

    Restituisce (dirs: set di path di directory, files: set di path di file).
    I file sono rappresentati come path completi.
    """
    dirs: Set[str] = set()
    files: Set[str] = set()

    for p in relpaths:
        stripped = p.strip("/")
        if not stripped:
            continue
        parts = stripped.split("/")
        # Tutti i prefissi sono directory
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
        # L'entry intera: se non ci sono altri path con questo come prefisso,
        # è un file. Ma non possiamo saperlo subito. Lo segnamo come
        # "candidato file" e poi lo risolviamo.
        files.add(stripped)

    # Un path che è anche prefisso di un altro è directory, non file
    real_files: Set[str] = set()
    for f in files:
        if f not in dirs:
            real_files.add(f)

    return dirs, sorted(real_files)


def _iter_children(prefix: str, dirs: Set[str], files: Set[str]) -> tuple[List[str], List[str]]:
    """Restituisce (subdir_names, subfile_names) immediate sotto *prefix*.

    ``prefix=""`` è la radice.
    I nomi restituiti sono il segmento finale (basename).
    """
    child_dirs: list[str] = []
    child_files: list[str] = []

    if not prefix:
        # Radice: tutti i path di primo livello
        for d in dirs:
            if "/" not in d and d:
                child_dirs.append(d)
        for f in files:
            if "/" not in f and f:
                child_files.append(f)
        return child_dirs, child_files

    prefix_trail = prefix + "/"

    for d in dirs:
        if d.startswith(prefix_trail):
            remainder = d[len(prefix_trail):]
            if "/" not in remainder and remainder:
                child_dirs.append(remainder)

    for f in files:
        if f.startswith(prefix_trail):
            remainder = f[len(prefix_trail):]
            if "/" not in remainder and remainder:
                child_files.append(remainder)

    return child_dirs, child_files


def detect_project_boundary_from_relpath(relpath: str, all_relpaths_under_same_root: List[str]) -> str:
    """Variante per path relativi già in DB — stessa logica di *detect_project_boundary*
    ma operando su stringhe note, senza toccare il filesystem.

    CAMMINA LA LINEA DEL SINGOLO FILE: prende il suo relpath, trova il primo segmento
    directory non-junk, poi per ogni livello successivo controlla se quella directory
    (nell'albero virtuale costruito da *tutti* i path noti) è un wrapper (1 solo figlio
    directory e 0 file). Se sì, scende. Se no, si ferma.

    Esempio:
        paths = [
            "storage/User/OldMemory/a.jpg",
            "storage/User/OldMemory/b.jpg",
            "storage/User/Stuff/c.txt",
        ]
        detect_project_boundary_from_relpath("storage/User/OldMemory/a.jpg", paths)
        # → "storage/User"  (perché storage/ ha 1 dir (User) e 0 file diretti,
        #                     ma User/ ha 2 dir (OldMemory, Stuff) e 0 file → si ferma)

    Args:
        relpath: Path relativo di un singolo file.
        all_relpaths_under_same_root: Tutti i path relativi sotto lo stesso device.

    Returns:
        Path relativo del confine del progetto (inizia dal primo segmento non-junk).
    """
    if not relpath or not all_relpaths_under_same_root:
        return ""

    # 1. Costruisce alberi virtuali (una volta, poi riusata)
    dirs, files = _parse_virtual_tree(all_relpaths_under_same_root)

    # 2. Segmenti del path di QUESTO file (escludendo l'ultimo = filename)
    parts = relpath.strip("/").split("/")
    if len(parts) < 2:
        return ""  # solo un nome file, nessuna directory

    # 3. Trova il primo segmento directory non-junk
    first_good_idx: int | None = None
    for i, p in enumerate(parts):
        if p and not is_junk_name(p):
            first_good_idx = i
            break

    if first_good_idx is None:
        return ""  # tutti junk

    # 4. Costruisce i prefissi directory: dal primo non-junk fino al penultimo
    prefixes: List[str] = []
    for i in range(first_good_idx, len(parts) - 1):
        prefixes.append("/".join(parts[: i + 1]))

    if not prefixes:
        return parts[first_good_idx] if first_good_idx < len(parts) else ""

    # 5. Per ogni prefisso, controlla se è wrapper
    for prefix in prefixes:
        child_dirs, child_files = _iter_children(prefix, dirs, files)
        clean_dirs = [d for d in child_dirs if not is_junk_name(d)]
        clean_files = [f for f in child_files if not is_junk_name(f)]

        if len(clean_dirs) == 1 and len(clean_files) == 0:
            # È wrapper → continua al livello successivo
            continue

        # Non è wrapper → questo è il confine del progetto
        return prefix

    # Se tutti i prefissi sono wrapper, il confine è il più profondo
    return prefixes[-1]


# ─── Helper per ottenere il project label dal boundary relpath ─────

def project_label_from_boundary(boundary_relpath: str) -> str:
    """Estrae il label del progetto dal boundary relpath.

    Esempi:
        "storage/User/OldMemory" → "OldMemory"
        "User"                  → "User"
        "storage/Ricerca Unitaria Intelligence" → "Ricerca Unitaria Intelligence"
        ""                      → "root"
    """
    if not boundary_relpath:
        return "root"
    return Path(boundary_relpath).name


def iter_project_boundaries(all_relpaths_under_same_root: List[str]) -> dict[str, List[str]]:
    """Raggruppa tutti i path per project boundary (utile per repair).

    Restituisce un dict {boundary_relpath: [file_relpath1, file_relpath2, ...]}.
    """
    result: dict[str, List[str]] = {}
    for p in all_relpaths_under_same_root:
        b = detect_project_boundary_from_relpath(p, all_relpaths_under_same_root)
        if b not in result:
            result[b] = []
        result[b].append(p)
    return result


# ─── Per repair: processa ogni top-level dir INDIPENDENTEMENTE ─────

def extract_top_level_dirs(relpaths: List[str]) -> List[str]:
    """Estrae i nomi unici di directory di primo livello non-junk.

    Argomento:
        relpaths: Lista di path relativi (es. ``["storage/User/a.jpg", "stuff/b.pdf"]``).

    Returns:
        Lista ordinata di nomi di directory di primo livello non-junk unici.
    """
    seen: Set[str] = set()
    result: List[str] = []
    for p in relpaths:
        stripped = p.strip("/")
        if not stripped:
            continue
        parts = stripped.split("/")
        first = parts[0]
        if first and not is_junk_name(first) and first not in seen:
            seen.add(first)
            result.append(first)
    return sorted(result)


def group_paths_by_topdir(relpaths: List[str]) -> Dict[str, List[str]]:
    """Raggruppa i path per directory di primo livello non-junk.

    Esempio:
        input: ["storage/User/a.jpg", "stuff/b.pdf", "storage/Work/c.txt"]
        output: {"storage": ["storage/User/a.jpg", "storage/Work/c.txt"],
                 "stuff": ["stuff/b.pdf"]}

    Returns:
        dict {topdir: [path_relativo1, ...]} ordinato per topdir.
    """
    groups: Dict[str, List[str]] = {}
    for p in relpaths:
        stripped = p.strip("/")
        if not stripped:
            continue
        parts = stripped.split("/")
        first = parts[0]
        if not first or is_junk_name(first):
            continue
        if first not in groups:
            groups[first] = []
        groups[first].append(p)
    return dict(sorted(groups.items()))


def detect_project_boundary_from_topdir(
    topdir: str,
    paths_under_topdir: List[str],
) -> str:
    """Parte da una directory top-level e scende finché non è più wrapper.

    A differenza di *detect_project_boundary_from_relpath*, che segue la linea
    di un singolo file, questa funzione parte da un *topdir* noto e usa
    TUTTI i path sotto di esso per determinare se ogni livello è wrapper.

    Logica:
    1. Costruisce albero virtuale da *paths_under_topdir*.
    2. Parte da *topdir*.
    3. Se la directory corrente ha esattamente 1 figlio directory e 0 file
       (junk esclusi), scende. Altrimenti si ferma.

    Args:
        topdir: Nome della directory di primo livello (es. "storage").
        paths_under_topdir: Tutti i path relativi che iniziano con *topdir*.

    Returns:
        Path relativo del confine del progetto.
        Se *topdir* non è wrapper, restituisce *topdir* stesso.
        Se non ci sono path, restituisce stringa vuota.
    """
    if not topdir or not paths_under_topdir:
        return ""

    dirs, files = _parse_virtual_tree(paths_under_topdir)
    current = topdir

    while True:
        child_dirs, child_files = _iter_children(current, dirs, files)
        clean_dirs = [d for d in child_dirs if not is_junk_name(d)]
        clean_files = [f for f in child_files if not is_junk_name(f)]

        if len(clean_dirs) == 1 and len(clean_files) == 0:
            # Wrapper: scendi nell'unico figlio directory
            current = current + "/" + clean_dirs[0]
            continue

        # Non è wrapper → confine
        break

    return current


def iter_project_boundaries_by_topdir(
    all_relpaths: List[str],
) -> Dict[str, List[str]]:
    """Raggruppa tutti i path per project boundary, processando ogni
    top-level directory INDIPENDENTEMENTE.

    Ogni top-level non-junk viene valutato con *detect_project_boundary_from_topdir*,
    che usa solo i path sotto quella directory per determinare il confine.
    Questo evita che una directory involucro condivisa (es. "storage")
    appaia come confine quando in realtà è solo un wrapper.

    Args:
        all_relpaths: Lista di tutti i path relativi del device.

    Returns:
        dict {boundary_relpath: [file_relpath1, ...]}.
        I path che non ricadono sotto nessun top-level non-junk vengono
        raggruppati sotto chiave "" (stringa vuota).
    """
    groups = group_paths_by_topdir(all_relpaths)

    result: Dict[str, List[str]] = {}

    for topdir, paths in groups.items():
        boundary = detect_project_boundary_from_topdir(topdir, paths)
        if boundary not in result:
            result[boundary] = []
        result[boundary].extend(paths)

    return result


# ─── Clusterizzazione ricorsiva (separazione fisico/semantico) ─────
# Questa è la funzione principale per il repair. Processa ogni top-level
# ricorsivamente: se una directory ha file diretti diventa un Project;
# se ha solo subdirectory, NON diventa Project — ogni subdir viene
# processata ricorsivamente come potenziale progetto a sé.
# In questo modo la gerarchia fisica (Device > Mount > Dir > File)
# resta separata dalla classificazione semantica (Project, Person, ...).


def _build_tree(relpaths: List[str]) -> Dict[str, tuple[List[str], List[str]]]:
    """Pre-costruisce un children_map O(1) dalla lista di path.

    Per ogni genitore ("" per radice), restituisce (sorted_subdirs, sorted_subfiles).

    Returns:
        Dict {parent_prefix: (lista_subdirs, lista_subfiles)}.
    """
    from collections import defaultdict

    # Mappa {parent: {set_of_subdir_names, set_of_subfile_names}}
    dir_children: Dict[str, Set[str]] = defaultdict(set)
    file_children: Dict[str, Set[str]] = defaultdict(set)
    all_dirs: Set[str] = set()

    for p in relpaths:
        stripped = p.strip("/")
        if not stripped:
            continue
        parts = stripped.split("/")
        # Costruisce prefissi (directory) e popola children_map
        parent = ""  # radice
        for i, seg in enumerate(parts):
            is_last = (i == len(parts) - 1)
            child_prefix = "/".join(parts[:i+1])
            if is_last:
                # Ultimo: candidato file (ma potrebbe essere dir vuota)
                file_children[parent].add(seg)
            else:
                # Non ultimo: è directory nota
                dir_children[parent].add(seg)
                all_dirs.add(child_prefix)
                parent = child_prefix

    # Risolve: un candidato file che in realtà è directory nota viene spostato
    for parent, candidates in list(file_children.items()):
        real_files: Set[str] = set()
        for c in candidates:
            child_path = (parent + "/" + c) if parent else c
            if child_path not in all_dirs:
                real_files.add(c)
        file_children[parent] = real_files

    # Costruisce il dict finale con liste ordinate
    result: Dict[str, tuple[List[str], List[str]]] = {}
    all_parents = set(dir_children.keys()) | set(file_children.keys())
    for parent in all_parents:
        result[parent] = (
            sorted(n for n in dir_children.get(parent, set()) if n),
            sorted(n for n in file_children.get(parent, set()) if n),
        )
    return result


def _cluster_at_fast(
    prefix: str,
    children_map: Dict[str, tuple[List[str], List[str]]],
) -> Dict[str, List[str]]:
    """Ricorsione clusterizzante ottimizzata (O(1) per nodo)."""
    child_dirs, child_files = children_map.get(prefix, ([], []))
    clean_dirs = [d for d in child_dirs if not is_junk_name(d)]
    clean_files = [f for f in child_files if not is_junk_name(f)]

    prefix_trail = (prefix + "/") if prefix else ""
    direct_file_paths = [prefix_trail + name for name in clean_files]

    results: Dict[str, List[str]] = {}

    # ── Caso wrapper ──────────────────────────────────────────────
    if len(clean_dirs) == 1 and len(clean_files) == 0:
        next_prefix = prefix_trail + clean_dirs[0]
        return _cluster_at_fast(next_prefix, children_map)

    # ── Caso non-wrapper ──────────────────────────────────────────

    # 1. Se ci sono file diretti → è un Project
    if clean_files:
        results[prefix or "__root__"] = direct_file_paths

    # 2. Per ogni subdir, ricorsione
    for subdir in clean_dirs:
        sub_prefix = prefix_trail + subdir
        sub_results = _cluster_at_fast(sub_prefix, children_map)
        for b, subfiles in sub_results.items():
            if b not in results:
                results[b] = []
            results[b].extend(subfiles)

    return results


def cluster_projects(relpaths: List[str]) -> Dict[str, List[str]]:
    """Clusterizza una lista di path relativi in progetti.

    Per ogni top-level directory non-junk, applica la clusterizzazione
    ricorsiva. Il risultato è un dict {boundary_relpath: [file_path, ...]}
    dove ogni boundary rappresenta un Project distinto.

    Una directory diventa un Project SOLO se:
    - È un wrapper (1 subdir, 0 file) e scende fino al vero contenuto, oppure
    - Ha almeno un file diretto (junk escluso).

    Una directory con 2+ subdir e 0 file diretti NON diventa un Project:
    ogni subdir viene valutata separatamente.

    Args:
        relpaths: Lista di path relativi (forward slash) sotto un device.

    Returns:
        Dict {boundary_relpath: [file_relpath, ...]}.
        I file che non ricadono in nessun top-level non-junk non vengono
        inclusi nel risultato (nessun Project creato per loro).
    """
    if not relpaths:
        return {}

    children_map = _build_tree(relpaths)

    # Top-level non-junk
    top_dirs, _ = children_map.get("", ([], []))
    clean_top_dirs = [d for d in top_dirs if not is_junk_name(d)]

    results: Dict[str, List[str]] = {}

    for topdir in clean_top_dirs:
        sub = _cluster_at_fast(topdir, children_map)
        for b, subfiles in sub.items():
            if b not in results:
                results[b] = []
            results[b].extend(subfiles)

    return results
# Archimede — Passive Agent for Reading, Navigation, and Identity Resolution for Penelope

**Archimede** is the passive query agent for the **Penelope** graph within the **Oracle** system. It reads, navigates, and links data without ever writing, modifying, or deleting anything. Specialized in **identity resolution** on large volumes of photos, it uses InsightFace (ArcFace 512-dim) for facial recognition entirely on CPU.

> **Role in Oracle:** Passive, read-only, multimodal agent. Reads Penelope's knowledge graph (MariaDB + ChromaDB), performs face matching on photos, and presents results to the user on request. Does not execute, modify, or delete — only SELECT.

---

## Index

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Components](#components)
4. [Identity Resolution](#identity-resolution)
5. [Usage](#usage)
6. [Project Structure](#project-structure)
7. [Data Models](#data-models)
8. [Installation](#installation)
9. [Configuration](#configuration)
10. [Testing](#testing)

---

## Overview

Archimede is a **read-only** agent operating on the **Penelope** graph, Oracle's memory database. Its core capabilities:

- **Read** the Penelope graph (MariaDB) — only SELECT queries
- **Query** Penelope's ChromaDB for image embeddings
- **Recognize faces** with InsightFace ArcFace 512-dim on CPU
- **Find couple photos** of parents in large photo collections
- **Generate navigable HTML reports** with photo gallery and statistics
- **Interactive face clustering** to identify unknown people
- **Never write** — no modify, delete, or insert operations

### Core Principle

```
ARCHIMEDE NEVER WRITES
```

All queries are strictly `SELECT` or `WITH`. Any write attempt is blocked at the architecture level.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        ARCHIMEDE QUERY (CLI)                         │
│                     (archimede/query.py)                             │
│   Entry point: python -m archimede.query <command> [options]         │
└──────┬───────────────────────────────────────────────────────────────┘
       │
       ├── Command: stats
       │   → Penelope graph statistics
       │
       └── Command: find-parents
           → Search for parent couple photos
              │
              ├── --ref-dir mode    (load references from folder)
              └── --interactive mode (interactive face clustering)
                    │
                    ▼
       ┌─────────────────────────────────────────────────────┐
       │                  ARCHIMEDE MODULES                  │
       │                                                     │
       │  ┌─────────────────────────────────────────────┐    │
       │  │           GRAPH READER (read-only)          │    │
       │  │  PenelopeGraphReader → MariaDB (SELECT)     │    │
       │  │  PenelopeChromaReader → ChromaDB (query)    │    │
       │  └─────────────────────────────────────────────┘    │
       │                                                     │
       │  ┌─────────────────────────────────────────────┐    │
       │  │           IDENTITY ENGINE                   │    │
       │  │  face_engine.py → InsightFace ArcFace       │    │
       │  │  matcher.py → face matching + couple search │    │
       │  └─────────────────────────────────────────────┘    │
       │                                                     │
       │  ┌─────────────────────────────────────────────┐    │
       │  │           PRESENTATION LAYER                │    │
       │  │  report.py → HTML report with photo gallery │    │
       │  └─────────────────────────────────────────────┘    │
       └─────────────────────────────────────────────────────┘
                              │
                              ▼
       ┌──────────────────────────────────────────────────────┐
       │              PENELOPE (external system)              │
       │                                                      │
       │  ┌──────────────┐          ┌───────────────────┐     │
       │  │   MariaDB    │          │    ChromaDB       │     │
       │  │ (graph data) │          │ (image embeddings)│     │
       │  └──────────────┘          └───────────────────┘     │
       └──────────────────────────────────────────────────────┘
```

### Operation Flow

#### `stats` Command

```
1. PenelopeGraphReader.connect() → MariaDB
2. Query: COUNT(*) indexed photos
3. Query: Person nodes (with/without InsightFace, YOLO)
4. Query: photos with face_count
5. Print aggregate statistics
```

#### `find-parents` Command (with `--ref-dir`)

```
1. Load reference photos from ref_faces/dad/, ref_faces/mom/
2. For each reference: detect faces with InsightFace → average embedding
3. Read photos from Penelope graph (all or by directory)
4. For each photo:
   a. Detect faces with InsightFace
   b. Compare embeddings with references (cosine similarity)
   c. If threshold > 0.35 → match
5. Find photos where BOTH parents appear together
6. Generate HTML report with photo gallery
```

#### `find-parents` Command (with `--interactive`)

```
1. Read photos with face_count from Penelope
2. Detect faces and embeddings for first 200 photos
3. Greedy clustering by cosine similarity (threshold 0.4)
4. Show clusters to user and ask: "Who is this? (dad/mom/skip)"
5. Once both are identified → proceed with search
```

---

## Components

### Graph Reader (`graph/`)

#### PenelopeGraphReader

Read-only wrapper around Penelope's `MariaDBStore`. Gets credentials via system keyring (same as Penelope).

- **SELECT only** — any non-SELECT query raises RuntimeError
- **Auto-connection** — searches Penelope directory, loads its `.env`, imports `MariaDBStore`
- **Public queries:**
  - `count_photos()` — total indexed photos
  - `get_all_photos(limit, offset)` — all photos with metadata
  - `get_photos_in_directory(directory)` — photos in a specific directory
  - `get_photos_with_face_count()` — photos with face_count metadata
  - `get_person_nodes(source)` — Person nodes, optionally filtered by source
  - `get_edges_for_photo(photo_node_id)` — entity edges for a photo
  - `get_persons_in_photo(photo_node_id)` — people linked to a photo

#### PenelopeChromaReader

Read-only reader of Penelope's ChromaDB for image embedding queries.

- **Query only** — no writing to ChromaDB
- **Auto-search** Penelope's ChromaDB persistence
- **Methods:**
  - `get_collections()` — available collections
  - `query_images(query_embedding, top_k)` — similarity search
  - `count_images()` — indexed images

### Identity Engine (`identity/`)

#### Face Engine (`face_engine.py`)

Face recognition engine based on **InsightFace** (`buffalo_l` model).

- **ArcFace 512-dim** — 512-dimensional facial embeddings
- **CPU-only** — runs entirely on CPU (i3 10th gen tested)
- **Lightweight model** — buffalo_l: 30MB (detection + recognition + age/gender)
- **Lazy loading** — model loads on first use

**Functions:**
- `detect_faces(image_path)` → list of faces with bbox, 512-dim embedding, det_score, gender, age, landmarks
- `cosine_similarity(a, b)` → cosine similarity between two embeddings
- `verify(emb1, emb2, threshold)` → comparison with threshold (default 0.35)

#### Matcher (`matcher.py`)

Face matching system to search for specific people's photos in large collections.

- **Reference loading** from structured directories (`dad/`, `mom/`)
- **Average embedding** for robustness (more photos = more stable embedding)
- **Couple photo search** — finds photos with BOTH parents
- **Progress callback** — callback to monitor progress on large volumes

### Presentation Layer (`presentation/`)

#### Report Generator (`report.py`)

Generates navigable HTML pages with:
- **Statistics** in colored cards (scanned photos, faces, couples)
- **Couple photo gallery** with 💑 badge
- **Individual parent sections** with remaining photos
- **Lightbox** for enlarged viewing
- **Thumbnails** via OpenCV with data URI (no temp files)
- **Responsive dark design** with gradients

### Models (`models.py`)

Pydantic-style data models (dataclass) for Archimede in Oracle:

| Model | Description |
|---------|-------------|
| **Photo** | Indexed photo in Penelope graph (node_id, file_path, face_count, metadata) |
| **DetectedFace** | Detected face (bbox, 512-dim embedding, confidence, gender, age) |
| **ReferenceFace** | Reference face (name, average embedding, source_photos) |
| **FaceMatch** | Match result (reference_name, similarity, is_match, bbox) |
| **PhotoMatchResult** | Result for a photo (faces, matches, is_couple) |
| **SearchReport** | Complete report (couple_photos, single_parent_photos, duration) |

---

## Identity Resolution

### InsightFace ArcFace 512-dim

Archimede uses **InsightFace** with the `buffalo_l` model for face recognition:

| Feature | Value |
|----------------|--------|
| **Model** | buffalo_l (30MB) |
| **Embedding** | 512-dimensional normalized |
| **Detection** | RetinaFace-based |
| **Recognition** | ArcFace |
| **Extra** | Age estimation, Gender estimation, Landmarks |
| **Execution** | CPU (CPUExecutionProvider) |
| **Default threshold** | 0.35 (cosine similarity) |

### Similarity Threshold

| Threshold | Behavior |
|--------|---------------|
| **0.30** | More permissive (more false positives, fewer false negatives) |
| **0.35** | Default — balanced |
| **0.40** | Stricter (fewer false positives, more false negatives) |
| **0.50** | Very strict — only very strong matches |

### Matching Strategy

1. **Average embedding** — for each person, compute the average of all reference photo embeddings, normalized to unit norm
2. **Cosine similarity** — compare detected face embedding with reference average embedding
3. **Match if threshold exceeded** — if similarity > threshold, the photo contains that person
4. **Couple photo** — if ALL references have at least one match in the same photo

---

## Usage

### Query Engine (main CLI)

```bash
# Penelope graph statistics
python -m archimede.query stats

# Couple photo search with references
python -m archimede.query find-parents --ref-dir ref_faces/

# With photo limit
python -m archimede.query find-parents --ref-dir ref_faces/ --limit 200

# In a specific directory
python -m archimede.query find-parents --ref-dir ref_faces/ --directory "MyPhotos"

# With custom threshold
python -m archimede.query find-parents --ref-dir ref_faces/ --threshold 0.40

# Interactive mode (face clustering)
python -m archimede.query find-parents --interactive

# Custom HTML output
python -m archimede.query find-parents --ref-dir ref_faces/ --output "results/my_parents.html"
```

### Reference Directory Structure

```
ref_faces/
├── dad/
│   ├── photo1.jpg
│   ├── photo2.jpg       (optional, more photos = more robust embedding)
│   └── ...
└── mom/
    ├── photo1.jpg
    ├── photo2.jpg
    └── ...
```

### Interactive Mode

If you don't have reference photos ready, Archimede can:
1. Scan the first 200 photos with detected faces
2. Cluster similar faces (greedy clustering, threshold 0.4)
3. Show you the clusters and ask "Who is this? (dad/mom/skip)"
4. Once both parents are identified → proceed with search

---

## Project Structure

```
Oracle/Archimede/
├── pyproject.toml                 # Python package
├── README.md                      # This file
├── .env                           # Configuration
├── .env.example                   # Configuration template
├── yolov8n.pt                     # YOLOv8 model (legacy)
│
├── archimede/                     # Main package
│   ├── __init__.py                # Version 0.2.0 + role docstring
│   ├── query.py                   # CLI entry point + orchestration
│   ├── config.py                  # Configuration
│   ├── models.py                  # Data models (Photo, FaceMatch, etc.)
│   ├── log_setup.py               # Structured logging
│   │
│   ├── graph/                     # Penelope graph reading
│   │   ├── reader.py              #   PenelopeGraphReader (MariaDB, SELECT-only)
│   │   └── chroma_reader.py       #   PenelopeChromaReader (ChromaDB, read-only)
│   │
│   ├── identity/                  # Identity resolution
│   │   ├── face_engine.py         #   InsightFace (ArcFace 512-dim, CPU)
│   │   └── matcher.py             #   Face matching + couple search
│   │
│   └── presentation/              # Results presentation
│       └── report.py              #   HTML report generation with gallery
│
├── tests/                         # Test suite
│   ├── test_chroma_reader.py      #   ChromaDB reader tests
│   ├── test_face_engine.py        #   Face detection tests
│   ├── test_graph_reader.py       #   MariaDB reader tests
│   ├── test_matcher.py            #   Face matching tests
│   ├── test_query.py              #   CLI query tests
│   └── test_report.py             #   Report generation tests
│
└── data/                          # Runtime data
    ├── chroma/                    #   Local ChromaDB
    ├── logs/                      #   Log files
    └── results/                   #   Generated HTML reports
```

---

## Data Models

All data is modeled with `dataclass` in `archimede/models.py`:

### Photo
```python
@dataclass
class Photo:
    node_id: str                  # Node ID in Penelope graph
    file_path: str                # Absolute path in filesystem
    file_name: str                # File name
    mime_type: str                # "image/jpeg", etc.
    size_bytes: int               # Size in bytes
    sha256: str                   # SHA-256 hash
    device: str                   # Source device
    date_taken: str               # EXIF date
    face_count: int               # Number of detected faces
    metadata: dict                # Additional metadata
```

### DetectedFace
```python
@dataclass
class DetectedFace:
    photo_path: str               # Original photo path
    photo_node_id: str            # Photo node ID
    bbox: list[int]               # [x1, y1, x2, y2]
    confidence: float             # Confidence score
    embedding: list[float] | None # 512-dim ArcFace
    gender: int | None            # 0=F, 1=M
    age: float | None             # Estimated age
    person_node_id: str | None    # Person node in Penelope (if exists)
```

### SearchReport
```python
@dataclass
class SearchReport:
    query_name: str               # Search name
    reference_names: list[str]    # ["dad", "mom"]
    similarity_threshold: float   # Threshold used (default 0.35)
    photos_scanned: int           # Total photos scanned
    photos_with_faces: int        # Photos with at least one face
    couple_photos: list           # Photos with both parents
    single_parent_photos: dict    # Photos per individual parent
    all_results: list             # All results
    duration_seconds: float       # Total duration
    generated_at: str             # Generation timestamp
```

---

## Installation

### 1. Clone the repository

```bash
git clone <repo-url>
cd Oracle/Archimede
```

### 2. Create a virtual environment (recommended)

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
source .venv/bin/activate  # Linux/Mac
```

### 3. Install the package

```bash
# Core (lightweight)
pip install -e .

# With face recognition
pip install -e ".[vision]"

# With all dependencies
pip install -e ".[dev]"
```

### 4. Configure the environment

```bash
copy .env.example .env    # Windows
cp .env.example .env      # Linux/Mac
```

Make sure the **Penelope** system is configured and accessible (Archimede reads its `.env` for MariaDB credentials).

---

## Configuration

Archimede reads configuration from `.env` file:

```ini
# ── Penelope ──
# Archimede gets MariaDB credentials from Penelope's .env
# (auto-searched in ../Penelope/.env or Penelope/.env)

# ── Face Recognition ──
# InsightFace buffalo_l is auto-downloaded on first use
# (requires Internet connection for initial download)
```

Archimede **does not have its own MariaDB configuration** — it uses Penelope's credentials via system keyring.

---

## Testing

```bash
# Run all tests
pytest tests/ -v

# Specific tests
pytest tests/test_face_engine.py -v
pytest tests/test_matcher.py -v
pytest tests/test_graph_reader.py -v
pytest tests/test_query.py -v
pytest tests/test_report.py -v

# Verbose output
pytest tests/ -v --tb=short
```

---

## Requirements

### Core
- Python 3.10+
- rich
- python-dotenv

### Vision (face recognition)
- insightface >= 0.7.0
- opencv-python >= 4.8.0
- numpy >= 1.24.0

### ChromaDB (optional)
- chromadb >= 0.4.0

### Dev
- pytest >= 7.0.0

---

## Technical Notes

- **Tested hardware**: i3 10th gen, 8GB RAM, integrated GPU — InsightFace runs entirely on CPU
- **InsightFace buffalo_l**: ~30MB, auto-downloads on first `detect_faces()`
- **Default threshold 0.35**: balanced for ArcFace 512-dim on real photos
- **Penelope**: Archimede assumes Penelope is already configured and has scanned photos
- **Read-only**: guaranteed by PenelopeGraphReader blocking any non-SELECT query
- **Connection**: MariaDB credentials read from Penelope's `.env` via keyring

---

## Notes on the Series

Unlike the Samaritan system in *Person of Interest* (an ASI of surveillance and control), Archimede:
- ✅ Is **read-only** — does not execute actions, does not manipulate, does not delete
- ✅ Has **The Machine's operational ethics** — protection, not control
- ✅ Is **transparent** — every operation is logged and inspectable
- ✅ Is **local** — operates only on data already in Penelope
- ✅ Is **passive** — responds to requests, does not act autonomously

*"Does not execute, does not modify, does not delete. Only reading and presentation to the user on request."*

---

*Archimede v0.2.0 — Passive Identity Resolution Agent for Oracle*

# CHORA — Setup Guide

This guide walks you through the complete installation and configuration of CHORA.

---

## 1. Prerequisites

### Required Software

| Software | Minimum Version | Notes |
|----------|----------------|------|
| Python | 3.11+ | [python.org](https://python.org) |
| Git | any | [git-scm.com](https://git-scm.com) |
| Docker | 24+ | Optional, for MariaDB |

### Python packages

Requirements are split by layer:

```bash
# Oracle Core (always required)
pip install -r oracle-rui/requirements.txt

# Penelope (if using the graph)
pip install -r penelope/requirements.txt

# Archimede (if using face recognition)
pip install -r archimede/requirements.txt
```

### NLP models (auto-downloaded on first use)

- SpaCy: `it_core_news_sm` (~15 MB)
- Sentence Transformers: `all-MiniLM-L6-v2` (~90 MB)
- InsightFace: `buffalo_l` (~300 MB) — for face recognition
- YOLOv8n: `yolov8n.pt` (~6 MB) — already included

---

## 2. Installation

### 2.1 Clone the repository

```bash
git clone <repo-url> chora
cd chora
```

### 2.2 Create virtual environment

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**Linux/macOS:**
```bash
python -m venv venv
source venv/bin/activate
```

### 2.3 Install dependencies

```bash
pip install -r oracle-rui/requirements.txt
```

### 2.4 Guided setup

```bash
python run.py --init
```

This command:
- Creates `.env` files from `.env.example` templates
- Creates necessary directories (`logs/`, `data/`)

---

## 3. Oracle Core Configuration

Edit `oracle-rui/.env`:

```ini
# ─── LLM API Key ────────────────────────────────────────────
# At least ONE key is required. Choose your provider:

# OpenAI
OPENAI_API_KEY=sk-...

# DeepSeek (recommended for quality/price ratio)
DEEPSEEK_API_KEY=sk-...

# Anthropic (Claude)
ANTHROPIC_API_KEY=sk-...

# Ollama (local, free)
# No API key needed, just run ollama locally

# ─── Default provider ───────────────────────────────────────
ORACLE_DEFAULT_PROVIDER=deepseek
ORACLE_DEFAULT_MODEL=deepseek-chat

# ─── Security ──────────────────────────────────────────────
JWT_SECRET_KEY=a-random-string-at-least-32-characters-long
```

---

## 4. Penelope Configuration (Knowledge Graph)

Penelope requires an SQL database to store the graph. You have two options.

### Option A: Docker MariaDB (recommended)

```bash
# Start MariaDB in container
docker-compose up -d

# Verify it works
docker exec oracle-rui-mariadb mariadb-admin ping -h localhost
```

The database is already initialized with the correct schema (`schema.sql` runs automatically on startup).

### Option B: SQLite (zero setup, single-user)

Edit `penelope/.env`:
```ini
PENELOPE_DB_BACKEND=sqlite
PENELOPE_SQLITE_PATH=data/penelope.db
```

No server required. The database is automatically created in the `data/` directory.

### Configure storage paths

Edit `penelope/.env` to specify which directories to scan:

```ini
# Up to 5 devices or folders
PENELOPE_STORAGE_1=C:/Users/yourname/Documents
PENELOPE_STORAGE_2=D:/Archive
PENELOPE_STORAGE_3=E:/Photos
PENELOPE_STORAGE_4=
PENELOPE_STORAGE_5=
```

Leave unused devices empty.

---

## 5. Archimede Configuration (Face Recognition)

Archimede reads the Penelope graph and adds face recognition capabilities.

### Prerequisites

```bash
pip install -r archimede/requirements.txt
```

The InsightFace model (`buffalo_l`) is auto-downloaded on first use (~300 MB).

### Configuration

Edit `archimede/.env`:

```ini
# API key for the reasoning core (LLM)
ARCHIMEDE_API_KEY=sk-...

# Penelope path (default: ../penelope)
ARCHIMEDE_PENELOPE_PATH=../penelope

# ChromaDB path
ARCHIMEDE_CHROMA_PATH=data/chroma
```

---

## 6. Startup

### Full system

```bash
python run.py --all
```

This starts:
- **Oracle Core** on [http://localhost:8100](http://localhost:8100)
- **Penelope** on [http://localhost:5000](http://localhost:5000)
- **Archimede** on [http://localhost:8001](http://localhost:8001)

### Oracle Core only

```bash
python run.py
```

Useful if you haven't configured Penelope yet.

### Combinations

```bash
python run.py --with-penelope          # Oracle + Penelope
python run.py --with-archimede         # Oracle + Archimede
python run.py --port 9000              # Custom port
```

---

## 7. First Use

### 7.1 Storage scan (Penelope)

```bash
# Scan all configured paths
python -m penelope.cli scan:all

# Start lazy processing (in background)
python -m penelope.cli queue loop
```

The processor automatically performs:
1. Metadata extraction (EXIF, date, size)
2. Semantic embedding (text → ChromaDB)
3. NER (named entity recognition)
4. Face detection (YOLOv8n)
5. Scene detection (video → keyframes)

### 7.2 Explore the graph

```bash
# Statistics
python -m archimede.query stats

# Access Penelope's web UI
# http://localhost:5000
```

### 7.3 Face recognition

1. Create a directory with reference photos:
   ```
   ref_faces/
       person_1/
           photo1.jpg
           photo2.jpg
       person_2/
           photo1.jpg
   ```

2. Run the search:
   ```bash
   python -m archimede.query find-parents --ref-dir ref_faces/
   ```

3. Or in interactive mode (auto-discovers face clusters):
   ```bash
   python -m archimede.query find-parents --interactive
   ```

---

## 8. Status Check

```bash
python run.py --status
```

Sample output:
```
+----------------------------------------------------+
|  Chora — Diagnostics                                |
+----------------------------------------------------+
|  Oracle       [OK]  :8100 (v0.5.0)
|  Penelope     [OK]  :5000 (1247 nodes)
|  Archimede    [OK]  :8001 (read-only graph)
+----------------------------------------------------+
```

---

## 9. Troubleshooting

### "Cannot connect to MariaDB"

- Verify Docker is running: `docker ps`
- If using Docker: `docker-compose up -d`
- If using SQLite: verify `PENELOPE_DB_BACKEND=sqlite` in `penelope/.env`

### "SpaCy model not found"

```bash
python -m spacy download it_core_news_sm
```

### "ERROR: API key not configured"

Verify that `oracle-rui/.env` contains at least one valid API key.

### "face_engine: cannot load InsightFace"

On first startup, InsightFace auto-downloads the `buffalo_l` model.
Make sure you have internet access and at least 300 MB of free space.

---

## 10. Uninstall

```bash
# Stop Docker
docker-compose down -v

# Remove virtual environment
deactivate
rm -rf venv/

# Remove generated data
rm -rf oracle-rui/data/
rm -rf penelope/data/
rm -rf archimede/data/
rm -rf logs/
```

---

## Support

For issues or questions, open an issue on the project repository.

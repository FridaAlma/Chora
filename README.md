# CHORA

Modular 4-layer OSINT framework for researchers: autonomous agent with memory, knowledge graph, identity resolution, and ethical guardrails.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                   CHORA CORE (:8100)                    │
│            Executive Agent + Web Frontend                │
│   50+ LLM providers · MCTS · Sandbox · Vector Memory     │
│             RUI Framework (24 OSINT tools)               │
└────────────────────────┬─────────────────────────────────┘
                         │
   ┌────────────────────┬┴────────────────────┐
   │                    │                     │
┌──┴──────────┐  ┌──────┴────────┐  ┌─────────┴─────────┐
│  PENELOPE   │  │  ARCHIMEDE    │  │     EGIDA         │
│  (:5000)    │  │  (:8001)      │  │  (integrated)     │
│  Ingestion  │  │  Graph Reader │  │  HSD Guardrail    │
│  + Graph    │  │  + Face Match │  │  · NER detection  │
│             │  │               │  │  · Quarantine     │
└─────────────┘  └───────────────┘  └───────────────────┘
```

| Layer           | Role                                               | Port  | Startup            |
|-----------------|----------------------------------------------------|-------|--------------------|
| **Oracle Core** | Executive agent, UI,                               | :8100 | Always             |
| **Penelope**    | Data ingestion, knowledge graph (MariaDB/ChromaDB) | :5000 | `--with-penelope`  |
| **Archimede**   | Read-only graph navigation, face recognition       | :8001 | `--with-archimede` |
| **Egida**       | Integrated HSD guardrail across all layers         | —     | Automatic          |

**HSD** stands for Hyper Sensitive Data, all the sensitive data like passwords, phone numbers, address, tokens and api keys.

---

## Quick Start (5 minutes)

### Prerequisites
- Python 3.11+
- Git
- (Optional) Docker — for MariaDB

### 1. Clone and install

```bash
git clone <repo-url> oracle-rui-edition
cd oracle-rui-edition

# Create virtual environment
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # Linux/macOS

# Install dependencies
pip install -r oracle-rui/requirements.txt
```

### 2. Guided setup

```bash
python run.py --init
```

This copies `.env.example` templates into their respective `.env`. Edit the created files:

- `oracle-rui/.env` → enter your LLM API key
- `penelope/.env` → configure storage paths (optional)
- `archimede/.env` → configure API key and Penelope path (optional)

### 3. Launch

```bash
# Oracle Core only (agent + frontend)
python run.py

# Oracle + Penelope + Archimede (full system)
python run.py --all
```

Open [http://localhost:8100](http://localhost:8100) in your browser.

---

## Configuration

### Oracle Core

Edit `oracle-rui/.env`:

```ini
# LLM API key (at least one)
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-...

# Default provider
ORACLE_DEFAULT_PROVIDER=deepseek
ORACLE_DEFAULT_MODEL=deepseek-chat
```

### Penelope (Knowledge Graph)

The graph requires an SQL database. Two options:

**Option A — Docker MariaDB (recommended)**

```bash
docker-compose up -d
```

**Option B — SQLite (zero setup)**

Edit `penelope/.env`:
```ini
PENELOPE_DB_BACKEND=sqlite
```

Then configure storage paths to scan:
```ini
PENELOPE_STORAGE_1=C:/Users/yourname/Documents
PENELOPE_STORAGE_2=D:/PhotoArchive
```

### Archimede (Face Recognition)

Requires Penelope running. Edit `archimede/.env`:
```ini
ARCHIMEDE_API_KEY=sk-...
ARCHIMEDE_PENELOPE_PATH=../penelope
```

---

## Commands

```bash
# Startup
python run.py                        # Oracle only
python run.py --with-penelope        # + Penelope
python run.py --with-archimede       # + Archimede
python run.py --all                  # Everything
python run.py --port 9000            # Custom port

# Diagnostics
python run.py --status               # Component status

# Setup
python run.py --init                 # Guided setup

# Penelope CLI
python -m penelope.cli scan:all      # Scan storage
python -m penelope.cli queue loop    # Process queue

# Archimede CLI
python -m archimede.query stats      # Graph stats
python -m archimede.query find-parents --ref-dir ref_faces/
```

---

## Customization

Everything is configurable via `.env`. No personal data, no hardcoding.

- **LLM**: 50+ providers supported (OpenAI, DeepSeek, Anthropic, Ollama, Groq, Together...)
- **Database**: MariaDB or SQLite
- **Storage**: Up to 5 devices/paths
- **Face Recognition**: InsightFace (ArcFace 512-dim, CPU)
- **NER**: SpaCy (Italian default, configurable)
- **Guardrail**: HSD thresholds configurable in `egida/config.py`

---

## License

Oracle RUI Edition is released for research use. Each researcher configures their own
environment and their own data. The software does not contain, collect, or transmit personal data.

---

## References

- [Oracle_Architettura.md](Oracle_Architettura.md) — Complete architecture document
- [SETUP.md](SETUP.md) — Detailed setup guide
- [CONSTITUTION.md](oracle-rui/CONSTITUTION.md) — Oracle Constitution

# 📚 Chora — Enciclopedia dei Comandi e del Funzionamento

> Versione: 3.0 | Data: 2026-09-10  
> Documentazione completa del sistema Chora (Oracle Research Project)  
> Repository: `/Users/antoniocannavacciuolo/Desktop/Experimental/OracleResearch/`

---

## Indice

1. [Visione d'Insieme](#1--visione-dinsieme)
2. [Architettura Generale](#2--architettura-generale)
3. [Installazione e Setup](#3--installazione-e-setup)
4. [Configurazione (.env)](#4--configurazione-env)
5. [Avvio del Sistema](#5--avvio-del-sistema-python-runpy)
6. [Penelope — CLI Completa](#6--penelope-cli-completa)
7. [Device Management](#7--device-management)
8. [Il Ciclo di Vita dei File (Ingestion Pipeline)](#8--il-ciclo-di-vita-dei-file-ingestion-pipeline)
9. [Modello del Grafo](#9--modello-del-grafo)
10. [Egida — Filtro HSD (Quarantena)](#10--egida--filtro-hsd-quarantena)
11. [Face Recognition (InsightFace)](#11--face-recognition-insightface)
12. [ChromaDB — Ricerca Semantica](#12--chromadb--ricerca-semantica)
13. [Penelope Web API (:5000)](#13--penelope-web-api-5000)
14. [Archimede API (:8001)](#14--archimede-api-8001)
15. [Chora Core / Oracle RUI (:8100)](#15--chora-core--oracle-rui-8100)
16. [Schema del Database](#16--schema-del-database)
17. [Migration](#17--migration)
18. [Scripts Utili](#18--scripts-utili)
19. [Flussi End-to-End](#19--flussi-end-to-end)
20. [Troubleshooting](#20--troubleshooting)

---

## 1. — Visione d'Insieme

**Chora** è un sistema di **intelligence documentale e grafo della conoscenza** personale/familiare. Nasce come progetto di ricerca per:

1. **Catalogare automaticamente** file, foto, video, documenti da qualunque dispositivo di storage (hard disk esterni, NAS, cloud mount)
2. **Costruire un grafo della conoscenza** (knowledge graph) dove file, persone, luoghi, eventi sono nodi collegati da archi semantici
3. **Riconoscere volti** in foto (InsightFace / ArcFace) e clusterizzare persone simili
4. **Scansionare testo con HSD** (Highly Sensitive Data) per evitare che password, API key, dati sensibili finiscano nell'AI
5. **Ricercare semanticamente** per testo e immagini (ChromaDB + MiniLM + CLIP)
6. **Interrogare il tutto con linguaggio naturale** via Archimede e l'orchestratore Oracle

### I quattro strati (layer)

```
┌─────────────────────────────────────────────────┐
│  Chora Core (:8100)  — Frontend Web + Agente    │
│  (Interfaccia utente, conversazione AI, tools)  │
├─────────────────────────────────────────────────┤
│  Archimede (:8001) — Graph Data Engine          │
│  (Query, face matching, find-parents, report)   │
├─────────────────────────────────────────────────┤
│  Penelope (:5000) — Ingestion + Knowledge Graph │
│  (Scan, dedup, embedding, queue, MariaDB/Chroma)│
├─────────────────────────────────────────────────┤
│  Egida — HSD Guardrail (across all layers)      │
│  (Regex scanner, NER, quarantena automatica)    │
└─────────────────────────────────────────────────┘
```

---

## 2. — Architettura Generale

### Struttura delle directory

```
OracleResearch/
├── run.py                        # Entry point unico per avviare tutto
├── CHORA_ENCYCLOPEDIA.md         # QUESTO FILE
│
├── penelope/                     # Layer 2 — Ingestion + Graph
│   ├── penelope/
│   │   ├── cli.py                # CLI (tutti i comandi penelope)
│   │   ├── discovery.py          # Device auto-discovery via marker files
│   │   ├── config/settings.py    # Configurazione centralizzata
│   │   ├── db/
│   │   │   ├── mariadb_store.py  # CRUD MariaDB (nodi, archi, file_registry, coda)
│   │   │   ├── chroma_store.py   # ChromaDB (embedding testuali e immagini)
│   │   │   └── graph_bridge.py   # NetworkX bridge per grafo in-memory
│   │   └── ingestion/
│   │       ├── scanner.py        # FileScanner + Watchdog
│   │       ├── dispatcher.py     # Coda di elaborazione lazy
│   │       ├── processor.py      # Stage: EXIF, embedding, NER, face, scene, geocoding
│   │       ├── metadata.py       # Metadati base (SHA-256, size, mime)
│   │       └── image_embedder.py # CLIP embedding per immagini
│   ├── web/api.py                # Flask API (:5000)
│   ├── scripts/                  # Migration e batch script
│   └── data/                     # Database locale, chroma, embeddings, keyframes
│
├── archimede/                    # Layer 3 — Graph Data Engine
│   └── archimede/
│       ├── api.py                # FastAPI server (:8001)
│       ├── graph/reader.py       # Lettura grafo Penelope
│       ├── graph/chroma_reader.py # Lettura ChromaDB
│       ├── identity/
│       │   ├── face_engine.py    # InsightFace detection
│       │   ├── matcher.py        # Face matching (find-parents)
│       │   └── name_tag.py       # Assegnazione nomi a persone
│       ├── presentation/report.py # Report HTML
│       └── agent.py              # Pattern-matching chat agent
│
├── oracle-rui/                   # Layer 4 — Chora Core (:8100)
│   ├── cli.py                    # CLI client
│   ├── api/                      # Backend API FastAPI
│   ├── tools/                    # Tool integrations (PenelopeBridge, wiki, gmail, ecc.)
│   └── mcp_server.py             # MCP server for Claude Code
│
├── egida/                        # Layer 1 — HSD Guardrail
│   ├── filters.py                # Regex HSD scanner con scoring
│   ├── ner_light.py              # NER leggero con SpaCy
│   ├── config.py                 # Config threshold, model path
│   └── quarantine.py             # Isolamento file infetti
│
└── logs/                         # Log centralizzati
```

### Stack tecnologico

| Componente | Tecnologia |
|---|---|
| Database principale | **MariaDB** su Proxmox (remoto, via pymysql) |
| Database locale | **SQLite** (fallback) |
| Embedding testuali | **MiniLM** (sentence-transformers, 384-dim) |
| Embedding immagini | **CLIP ViT-B/32** (open-clip-torch, 512-dim) |
| Vector store | **ChromaDB** (persistente su disco) |
| Face recognition | **InsightFace** (ArcFace 512-dim + RetinaFace) |
| Scene detection video | **PySceneDetect** (AdaptiveDetector) |
| Graph in-memory | **NetworkX** (MultiDiGraph) |
| Frontend web | **Flask** (Penelope) + **FastAPI** (Archimede, Oracle) |
| HSD scanning | **Regex** + **SpaCy NER** (it_core_news_sm) |

---

## 3. — Installazione e Setup

### Prerequisiti

- Python 3.9+
- MariaDB server (remoto o via Docker) — **oppure** SQLite locale
- ~2 GB RAM libera (per modelli embedding + face recognition)
- ~1 GB disco (per modelli scaricati: MiniLM, CLIP, InsightFace buffalo_l, YOLOv8n)

### Setup rapido

```bash
# 1. Clona il progetto
cd OracleResearch

# 2. Crea ambiente virtuale
python3 -m venv venv
source venv/bin/activate

# 3. Installa dipendenze
pip install -r requirements.txt       # dipendenze generali
pip install sentence-transformers      # embedding testuali (MiniLM)
pip install open-clip-torch            # embedding immagini (CLIP)
pip install insightface onnxruntime    # face recognition
pip install spacy && python -m spacy download it_core_news_sm  # NER italiano
```

### Primo avvio

```bash
# Solo setup template .env (senza registrare device)
python run.py --init

# Setup + registra il primo storage + scan immediato
python run.py --init /Volumes/HDD/Documenti
```

Il comando `--init` fa automaticamente:
1. Crea `.env` da `.env.example` per ogni componente
2. Crea la directory `logs/`
3. Persiste il path in uno slot `PENELOPE_STORAGE_N` nel `.env`
4. Crea/riusa un device nel database
5. Scrive il marker `.penelope_device.json` alla radice del path
6. Fa uno scan immediato (idempotente: file già noti per SHA-256 vengono skippati)

---

## 4. — Configurazione (.env)

### Penelope (penelope/.env)

```env
# Database
PENELOPE_DB_BACKEND=mariadb         # o "sqlite"
PENELOPE_DB_HOST=192.168.1.100
PENELOPE_DB_PORT=3306
PENELOPE_DB_USER=penelope
PENELOPE_DB_PASSWORD=...            # O usa: penelope configure set
PENELOPE_DB_NAME=penelope_rui

# Storage paths (fino a 5 slot)
PENELOPE_STORAGE_1=/Volumes/HDD/Documenti
PENELOPE_STORAGE_2=/Volumes/NAS/Archivio
# ...

# Altro
PENELOPE_LOG_LEVEL=INFO
PENELOPE_CHROMA_PATH=data/chroma
PENELOPE_EMBEDDINGS=data/embeddings
```

### Password MariaDB

La password viene risolta in quest'ordine:
1. **Keyring** di sistema (`penelope configure set --password ...`)
2. **Variabile d'ambiente** `PENELOPE_DB_PASSWORD`
3. **File `.env`** (ultima spiaggia, warning in chiaro)

```bash
# Imposta password nel keyring (macOS Keychain / Windows Credential Manager)
penelope configure set
penelope configure set --password "la_mia_password"
```

### Oracle RUI (oracle-rui/.env)

```env
# LLM API Key (obbligatoria per l'agente conversazionale)
OPENAI_API_KEY=sk-...
# OPPURE
ANTHROPIC_API_KEY=sk-ant-...

# Altro
ORACLE_PORT=8100
```

---

## 5. — Avvio del Sistema (`python run.py`)

### Comandi di avvio

```bash
# Avvio minimale (solo Chora Core :8100)
python run.py

# Avvio con Penelope (knowledge graph API :5000)
python run.py --with-penelope

# Avvio con Archimede (graph data engine :8001)
python run.py --with-archimede

# Avvio COMPLETO (tutti i componenti)
python run.py --all

# Porta personalizzata per Chora Core
python run.py --port 8100

# Solo status (non avvia nulla)
python run.py --status
```

### What happens on start

`start_penelope()` fa due cose PRIMA di avviare il server Flask:

1. **Device reconciliation** (`_reconcile_and_scan()`):
   - Legge i marker `.penelope_device.json` da tutti i mountpoint attivi (via `psutil`)
   - Per ogni marker valido, fa `upsert_device_mount()` nel database
   - Per ogni mountpoint aggiornato/creato, avvia uno **scan in background**
     (thread daemon) delle sottocartelle di primo livello — un Progetto per
     sottocartella, non un unico progetto per l'intero device

2. Se `mp` non ha sottocartelle (file alla radice), scan unico della radice.

### Server attivi per default

| Porta | Servizio | URL |
|---|---|---|
| 8100 | Chora Core / Oracle RUI | http://localhost:8100 |
| 5000 | Penelope (opzionale) | http://localhost:5000 |
| 8001 | Archimede (opzionale) | http://localhost:8001 |
| 8101 | MCP Server (opzionale) | Per Claude Code / Codex |

---

## 6. — Penelope — CLI Completa

Tutti i comandi si invocano con `python -m penelope.cli <comando>`  
(oppure dopo `cd penelope`, `python -m penelope.cli <comando>`).

### 6.1 `penelope scan <path>` — Scansiona una directory

```bash
python -m penelope.cli scan /Volumes/HDD/Documenti
python -m penelope.cli scan /Volumes/HDD/Documenti --device laptop --project "Documenti Lavoro"
```

- Crea nodi `Project` + `File` in MariaDB
- **Idempotente**: file già noti per SHA-256 vengono skippati (skip_reason="DUPLICATE") ma la nuova posizione fisica viene registrata

### 6.2 `penelope scan:all` — Scansiona TUTTI gli storage configurati

```bash
python -m penelope.cli scan:all
```

Legge `STORAGE_PATHS` dalle variabili `PENELOPE_STORAGE_1..5` e scansiona tutte.

### 6.3 `penelope queue process` — Elabora la coda

```bash
# Processa un batch (default 5 elementi)
python -m penelope.cli queue process

# Processa con batch più grande
python -m penelope.cli queue process --batch 20

# Resetta elementi bloccati prima di processare
python -m penelope.cli queue process --reset-stale --max-age 30
```

### 6.4 `penelope queue loop` — Loop continuo

```bash
python -m penelope.cli queue loop --interval 5 --batch 5
```

Processa continuamente la coda ogni N secondi. Ctrl+C per fermare.

### 6.5 `penelope queue status` — Stato della coda

```bash
python -m penelope.cli queue status
```

Mostra conteggi per status: pending, processing, done, failed.

### 6.6 `penelope watchdog start` — Watchdog real-time

```bash
# Su una directory specifica
python -m penelope.cli watchdog start --path /Volumes/HDD/Download --device hdd-ext

# Su TUTTI gli storage configurati
python -m penelope.cli watchdog start
```

Usa `watchdog` (inotify/FSEvents) per processare automaticamente i nuovi file.

### 6.7 `penelope device register` — Registra un device

```bash
python -m penelope.cli device register \
    --label z_drive \
    --mount /Volumes/diskD \
    --type network \
    --hostname ToniMacBook
```

- Crea/riusa device nel DB
- Upserta mount per host corrente
- **Scrive** il marker `.penelope_device.json` alla radice del path

### 6.8 `penelope device list` — Elenca device

```bash
python -m penelope.cli device list
```

Mostra tutti i device, tipo, mount per host (con marcatura "◀ host corrente").

### 6.9 `penelope device merge` — Fonde due device duplicati

```bash
# Dry-run (mostra cosa succederebbe)
python -m penelope.cli device merge --keep 5 --remove 6

# Esecuzione con conferma interattiva
python -m penelope.cli device merge --keep 5 --remove 6

# Esecuzione automatica (salta conferma)
python -m penelope.cli device merge --keep 5 --remove 6 --yes
```

Cosa fa:
1. Riassegna tutte le righe `file_registry` da `remove_id` a `keep_id`
2. Copia i `device_mounts` di `remove_id` su `keep_id` (solo hostname non già presenti)
3. Cancella `device_mounts` e `devices` di `remove_id`
4. Riscrive il marker `.penelope_device.json` su ogni mount conosciuto

### 6.10 `penelope search <query>` — Ricerca semantica

```bash
python -m penelope.cli search "angelo e mamma in montagna"
python -m penelope.cli search "foto con cane" --top 20
python -m penelope.cli search "appunti AI" --mime text/markdown
```

Cerca in ChromaDB (testo MiniLM + immagini CLIP), risultati cross-modali.

### 6.11 `penelope graph status` — Stato del grafo

```bash
python -m penelope.cli graph status
```

Mostra numero nodi, archi e top tipi di nodo.

### 6.12 `penelope db dedup` — Deduplica nodi duplicati

```bash
python -m penelope.cli db dedup
```

- Trova file duplicati per SHA-256 (tiene il primo inserito)
- Trova progetti duplicati per label (tiene il più vecchio)

### 6.13 `penelope db stats` — Statistiche database

```bash
python -m penelope.cli db stats
```

Mostra nodi per tipo e archi per relazione.

### 6.14 `penelope db verify` — Verifica raggiungibilità file

```bash
# Verifica TUTTI i file
python -m penelope.cli db verify

# Verifica per un device specifico
python -m penelope.cli db verify --device z_drive

# Con limite righe
python -m penelope.cli db verify --limit 100
```

Controlla per ogni file se esiste ancora sul filesystem. Aggiorna status: online/offline/unknown_from_host.

### 6.15 `penelope configure` — Gestione password

```bash
# Salva password MariaDB nel keyring
python -m penelope.cli configure set
python -m penelope.cli configure set --password "..."

# Rimuovi dal keyring
python -m penelope.cli configure clear

# Test connessione
python -m penelope.cli configure test
```

### 6.16 `penelope geo process` — Geocoding GPS

```bash
python -m penelope.cli geo process
```

Reverse geocoding via Nominatim: coordinate EXIF → indirizzo → nodo Location.

### 6.17 `penelope geo test` — Test Nominatim

```bash
python -m penelope.cli geo test
```

### 6.18 `penelope geo cache` — Mostra cache geocoding

```bash
python -m penelope.cli geo cache
```

### 6.19 `penelope event create-from-dates` — Event nodes da date

```bash
python -m penelope.cli event create-from-dates
```

Crea nodi `Event` dalla data nel nome file (es. `IMG-20201224-WA0011` → `Event_2020-12-24`).

### 6.20 `penelope event status` — Statistiche eventi

```bash
python -m penelope.cli event status
```

### 6.21 `penelope event list` — Elenca eventi

```bash
python -m penelope.cli event list
```

### 6.22 `penelope video detect-scenes` — Scene detection video

```bash
python -m penelope.cli video detect-scenes
```

Rileva scene in tutti i video con PySceneDetect, salva keyframe, crea nodi Event.

### 6.23 `penelope video list` — Elenca video

```bash
python -m penelope.cli video list
```

### 6.24 `penelope face test <path>` — Test face detection

```bash
python -m penelope.cli face test /path/to/foto.jpg
```

### 6.25 `penelope face process-all` — Processa TUTTE le immagini

```bash
# Processa tutte le immagini non ancora processate
python -m penelope.cli face process-all

# Con limite
python -m penelope.cli face process-all --limit 100
```

Usa **InsightFace** (RetinaFace + ArcFace 512-dim) — rileva volti, calcola embedding, crea nodi Person.

### 6.26 `penelope face reprocess` — Riprocessa da YOLO a InsightFace

```bash
python -m penelope.cli face reprocess
```

### 6.27 `penelope face status` — Stato face recognition

```bash
python -m penelope.cli face status
```

### 6.28 `penelope face cluster` — Clustering pairwise

```bash
# Trova coppie simili (soglia default 0.4)
python -m penelope.cli face cluster --threshold 0.35

# Trova e unisci
python -m penelope.cli face cluster --threshold 0.4 --merge
```

### 6.29 `penelope face cluster-dbscan` — Clustering DBSCAN

```bash
# Con parametri personalizzati
python -m penelope.cli face cluster-dbscan --eps 0.5 --min-samples 2

# Trova e unisci automaticamente
python -m penelope.cli face cluster-dbscan --eps 0.5 --min-samples 2 --merge
```

### 6.30 `penelope quarantine list` — File in quarantena

```bash
python -m penelope.cli quarantine list
```

### 6.31 `penelope quarantine clear` — Svuota quarantena

```bash
python -m penelope.cli quarantine clear
```

---

## 7. — Device Management

### Il problema che risolve

Prima della v3.0, un device poteva essere registrato in tre modi diversi:
- `penelope device register` (non scriveva il marker → invisibile alla reconciliation)
- `python run.py --init <path>` (scriveva il marker ma hardcodava `device_type='local'`)
- `reconcile_devices()` (rilevava solo device con marker)

Questo causava **device duplicati** per lo stesso path fisico.

### Come funziona ora

#### Marker file (`.penelope_device.json`)

Ogni mountpoint ha un file JSON nascosto alla radice:

```json
{
  "device_id": 5,
  "label": "z_drive",
  "created_at": "2026-09-10T08:58:11"
}
```

**Tutti** i percorsi di registrazione ora scrivono il marker:
- `device register` ✅
- `--init` ✅
- `merge_devices()` (riscrive con il keep_id) ✅

#### Reconciliation ad ogni avvio

`reconcile_devices()` in `discovery.py`:
1. Enumera mountpoint attivi via `psutil.disk_partitions()`
2. Cerca `.penelope_device.json` alla radice di ognuno
3. Se trovato, fa `upsert_device_mount(device_id, hostname, mountpoint)`

#### Guard rail anti-duplicato in `cmd_init()`

Quando il marker è assente, prima di creare un nuovo device:
1. Cerca in `device_mounts` per (mount_root, host corrente)
2. Poi per mount_root su qualsiasi host
3. Poi in `devices.mount_root` (deprecato)
4. Se trova match → scrive il marker mancante e riusa quel device_id

### Tabelle nel DB

```sql
devices (id, label, type, volume_uuid, mount_root, created_at, last_seen_at)
device_mounts (id, device_id, hostname, mount_root, last_seen_at)
```

- `type` può essere: `local`, `external`, `network`, `server`
- `device_mounts` permette a device diversi host di avere mount diversi per lo stesso device logico

---

## 8. — Il Ciclo di Vita dei File (Ingestion Pipeline)

### Flusso completo

```
1. SCAN  ────>  MariaDB (nodi File + Project)
                   │
2. QUEUE ────>  ingestion_queue (pending)
                   │
3. DISPATCH ──>  Stage multipli (lazy, asincroni)
                   │
                   ├── EXIF (Pillow) — dati foto, GPS
                   ├── Embedding testo (MiniLM → ChromaDB)
                   ├── Embedding immagini (CLIP → ChromaDB)
                   ├── NER (SpaCy) → Person/Location nodes
                   ├── Face detection (YOLOv8n → bbox)
                   ├── InsightFace (ArcFace embedding → Person nodes)
                   ├── Data events (da nome file/EXIF → Event nodes)
                   ├── Geocoding (Nominatim → Location nodes)
                   └── Scene detection video (PySceneDetect → Event nodes)
```

### Fase 1: Scan (sincrono)

`FileScanner.scan_directory()` in `scanner.py`:

1. Crea/riusa nodo `Project` per la directory radice
2. Cammina ricorsivamente (`root.rglob("*")`)
3. Per ogni file:
   - **Esclude** file nascosti, `.git/`, `__pycache__/`, `node_modules/`, `.DS_Store`, ecc.
   - **HSD check**: se passa la soglia → quarantena, skip
   - **Metadati**: SHA-256, size, mime-type (da estensione)
   - **Dedup SHA-256**: se già noto, registra solo nuova posizione fisica (`register_file_location`)
   - **Crea nodo File** in MariaDB
   - **Registra** in `file_registry`
   - **Edge** `MEMBER_OF` → Project
   - **Accoda** in `ingestion_queue` per elaborazione futura

### Fase 2: Elaborazione coda (asincrona)

`Dispatcher` in `dispatcher.py`:

Ogni elemento in coda viene processato con tutti gli stage attivi:

| Stage | Cosa fa | Per quali file | Dipendenze |
|---|---|---|---|
| **EXIF** | Estrae data scatto, GPS, camera | Immagini (jpg, png, ecc.) | Pillow |
| **Embedding testo** | MiniLM → ChromaDB | file di testo | sentence-transformers |
| **Embedding immagini** | CLIP ViT-B/32 → ChromaDB | immagini | open-clip-torch |
| **NER** | SpaCy → nodi Person/Location + edge MENTIONS | file di testo | spacy + modello IT |
| **Face detection** | YOLOv8n → bbox persone | immagini | ultralytics |
| **Event da data** | Crea Event nodes da date in filename/EXIF | tutti | — |
| **Geocoding** | Nominatim → Location nodes | immagini con GPS | requests |
| **Scene detection** | PySceneDetect → keyframe + Event | video | scenedetect, opencv |

Gli stage si possono disabilitare individualmente modificando le flag in `dispatcher.py`:
```python
ENABLE_EXIF = True
ENABLE_EMBEDDING = True
ENABLE_IMAGE_EMBEDDING = True
ENABLE_NER = True
ENABLE_FACE = True
ENABLE_SCENE = True
ENABLE_DATE_EVENTS = True
ENABLE_GEOCODING = True
```

### Fase 3: Watchdog (real-time)

```bash
penelope watchdog start --path /path --device nome
```

- Usa `watchdog` library (inotify su Linux, FSEvents su macOS, ReadDirectoryChangesW su Windows)
- **Debounce** di 2 secondi per evitare di processare file durante la copia
- Stesso flusso dello scan: HSD → metadata → dedup → nodo → coda

---

## 9. — Modello del Grafo

### Tipi di Nodo

| Tipo | Descrizione | Creato da |
|---|---|---|
| `File` | Un file fisico (nodo centrale) | Scan, Watchdog |
| `Project` | Directory scansionata | Scan, Watchdog, `--init` |
| `Person` | Persona rilevata in foto o testo | Face detection, NER, InsightFace |
| `Location` | Luogo geografico | Geocoding (GPS → Nominatim) |
| `Event` | Evento con data | Scene detection, Date parsing |

### Relazioni (Edge)

| Relazione | Source → Target | Creato da |
|---|---|---|
| `MEMBER_OF` | File → Project | Scan, Watchdog |
| `MENTIONS` | File → Person/Location | NER (SpaCy) |
| `CONTAINS` | File → Person | Face detection (YOLO/InsightFace) |
| `CREATED_AT` | File → Event | Date parsing, EXIF |
| `HAS_SCENE` | File → Event | Scene detection video |
| `LOCATED_AT` | File → Location | Geocoding |

### Grafo in-memory (NetworkX)

`GraphBridge` carica tutto il grafo da MariaDB in un `MultiDiGraph` NetworkX:

```python
from penelope.db.graph_bridge import GraphBridge
bridge = GraphBridge()
bridge.load_from_db()

# Query
neighbors = bridge.get_neighbors(node_id, relation="MENTIONS")
path = bridge.shortest_path(source_node, target_node)
stats = bridge.count()
```

---

## 10. — Egida — Filtro HSD (Quarantena)

### Cos'è

Egida è il **guardrail HSD** (Highly Sensitive Data) che impedisce a password, API key, codici fiscali, numeri di telefono, ecc. di finire nei file indicizzati e quindi nell'AI.

### Pattern rilevati (con scoring)

| Pattern | Severity | Score |
|---|---|---|
| AWS Access Key (`AKIA...`) | CRITICAL | 100 |
| GitHub Token (`ghp_...`, `gho_...`, `ghu_...`) | CRITICAL | 100 |
| Hugging Face Token (`hf_...`) | CRITICAL | 100 |
| Chiave SSH privata (`-----BEGIN...PRIVATE KEY-----`) | CRITICAL | 100 |
| Codice Fiscale italiano | HIGH | 90 |
| API Key/Secret generico | HIGH | 90 |
| Token JWT/Bearer | HIGH | 90 |
| Password esplicita (`password = ...`) | HIGH | 90 |
| Numero di telefono (con prefisso) | MEDIUM | 50 |
| Indirizzo email | MEDIUM | 50 |
| CAP (codice avviamento postale) | LOW | 25 |

### Sistema di scoring

- Soglia di quarantena: **50** (configurabile via `EGIDA_QUARANTINE_THRESHOLD`)
- Un pattern CRITICAL (100) da solo → **quarantena immediata**
- Due pattern MEDIUM (50+50) = 100 → **quarantena**
- Pattern LOW (25) richiedono combinazione

### Esclusioni contestuali

- Email da domini fittizi (`example.com`, `test.com`) → declassate a INFO
- `password = "password"` (placeholder) → declassato a LOW
- UUID in linee di codice → non contano per telefono/CAP
- JWT non decodificabili → ignorati

### Quarantena

I file che superano la soglia vengono:
1. Spostati in `quarantine/` (con timestamp e dettaglio match)
2. **Non** indicizzati nel grafo
3. Elencabili con `penelope quarantine list`

---

## 11. — Face Recognition (InsightFace)

### Modello

- **Face detection**: RetinaFace (incluso in InsightFace)
- **Face embedding**: ArcFace 512-dim (modello `buffalo_l`, ~30MB)
- **Attributi**: età, genere (stima da volto)
- **Tutto su CPU** (ONNX Runtime, esecutore CPU)

### Flusso

```
Immagine → InsightFace detect_faces()
  └── Per ogni volto:
        ├── Bounding box
        ├── Landmarks (5 punti)
        ├── Embedding 512-dim (ArcFace)
        ├── Gender/age
        └── Salva .npy in data/embeddings/<person_id>.npy

Nodi Person → Cluster (pairwise o DBSCAN)
  └── Unisci volti simili (soglia 0.3-0.5)
```

### Comandi

```bash
# Test su una foto
penelope face test /path/foto.jpg

# Processa tutte le immagini con InsightFace
penelope face process-all

# Riprocessa da YOLO a InsightFace
penelope face reprocess

# Clustering pairwise
penelope face cluster --threshold 0.4 --merge

# Clustering DBSCAN
penelope face cluster-dbscan --eps 0.5 --min-samples 2 --merge
```

### Name Tag (Archimede)

Assegnare un nome a un volto (via Archimede API):

```bash
POST /archimede/name-tag
{"person_id": "...", "name_tag": "Angelo"}
```

Con propagazione automatica al cluster.

---

## 12. — ChromaDB — Ricerca Semantica

### Collezioni

| Collezione | Dimensione | Modello |
|---|---|---|
| `file_embeddings` | 384-dim | MiniLM (sentence-transformers) |
| `image_embeddings` | 512-dim | CLIP ViT-B/32 |

### Ricerca cross-modale

La ricerca semantica interroga **entrambe** le collezioni:
- Testo → embedding MiniLM → match con testi
- Testo → embedding CLIP (stesso modello) → match con immagini

### Comando

```bash
penelope search "angelo in montagna" --top 20
```

### API

```http
GET http://localhost:8001/archimede/search?q=angelo&top_k=10
```

### Archiviazione

I dati persistono su disco in `data/chroma/` (directory persistente ChromaDB).

---

## 13. — Penelope Web API (:5000)

### Endpoint principali

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/` | GET | Info servizio |
| `/api/stats` | GET | Statistiche generali |
| `/api/graph/nodes` | GET | Nodi (con filtri) |
| `/api/graph/edges` | GET | Archi (con filtri) |
| `/api/graph/neighbors/{node_id}` | GET | Vicini di un nodo |
| `/api/search?q=...` | GET | Ricerca semantica |
| `/api/photos` | GET | Foto con volti |
| `/api/health` | GET | Health check |

Il server Flask è multi-thread (con thread-local per connessioni MariaDB).

---

## 14. — Archimede API (:8001)

### Endpoint principali

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/archimede/health` | GET | Health check |
| `/archimede/stats` | GET | Statistiche grafo |
| `/archimede/search?q=...&top_k=10` | GET | Ricerca semantica |
| `/archimede/persons` | GET | Lista persone |
| `/archimede/photos` | GET | Lista foto |
| `/archimede/photos/{node_id}` | GET | Dettaglio foto |
| `/archimede/events` | GET | Eventi |
| `/archimede/locations` | GET | Luoghi |
| `/archimede/name-tags` | GET | Name tags (con conteggi) |
| `/archimede/name-tag/{name_tag}` | GET | Foto per persona |
| `/archimede/query` | POST | Query generica (NL o SQL) |
| `/archimede/chat` | POST | Chat con agente (pattern matching) |
| `/archimede/find-parents` | POST | Trova foto genitori (face matching) |

### Find Parents (face matching)

Il flusso `find-parents`:

1. Carica foto di referenza da `ref_faces/papa/` e `ref_faces/mamma/`
2. Carica tutte le foto dal grafo Penelope
3. Per ogni foto:
   - Rileva volti con InsightFace
   - Confronta embedding con le referenze (similarità coseno)
4. Trova:
   - **Foto di coppia** (entrambi i genitori presenti)
   - **Foto singole** (solo papà o solo mamma)
5. Genera report HTML

### Assegnazione Nome (name_tag)

```bash
curl -X POST http://localhost:8001/archimede/name-tag \
  -H "Content-Type: application/json" \
  -d '{"person_id": "uuid...", "name_tag": "Angelo", "propagate": true}'
```

Propagazione: se `propagate=true`, unisce il cluster e assegna lo stesso nome a tutti i volti simili.

---

## 15. — Chora Core / Oracle RUI (:8100)

### Componenti

- **Backend**: FastAPI con autenticazione, rate limiting, security middleware
- **Frontend**: Interfaccia web (Chat UI) su `http://localhost:8100`
- **Agente**: Orchestratore con tools: PenelopeBridge, WebAccess, WikiTool, Gmail, MCTS Engine, ecc.
- **MCP Server**: Integrazione con Claude Code / Codex su `:8101`

### Tools dell'agente

| Tool | Descrizione |
|---|---|
| **PenelopeBridge** | Interroga grafo Penelope e Archimede |
| **WebAccess** | Naviga pagine web |
| **WikiTool** | Cerca su Wikipedia |
| **GmailClient** | Legge Gmail |
| **MCTSEngine** | Monte Carlo Tree Search per ragionamento |
| **Constitution** | Regole e vincoli dell'agente |
| **ImmunityGuardian** | Sicurezza e filtri |
| **VectorMemory** | Memoria a lungo termine |

### Avvio

```bash
python run.py          # solo Chora Core :8100
python run.py --all    # tutto
```

---

## 16. — Schema del Database

### `nodes` — Nodi del grafo

```sql
CREATE TABLE nodes (
    id          VARCHAR(64) PRIMARY KEY,      -- UUID v4
    type        ENUM('File','Project','Person','Location','Event') NOT NULL,
    label       VARCHAR(255),                 -- Nome leggibile
    metadata    JSON,                         -- Attributi variabili
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

### `edges` — Archi (relazioni)

```sql
CREATE TABLE edges (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    source_id   VARCHAR(64) NOT NULL,
    target_id   VARCHAR(64) NOT NULL,
    relation    VARCHAR(50) NOT NULL,          -- MEMBER_OF, MENTIONS, CONTAINS, CREATED_AT, HAS_SCENE, LOCATED_AT
    weight      FLOAT DEFAULT 1.0,
    metadata    JSON,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES nodes(id) ON DELETE CASCADE
);
```

### `file_registry` — Posizioni fisiche dei file

```sql
CREATE TABLE file_registry (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    node_id         VARCHAR(64) NOT NULL,
    device_id       INT,                      -- FK → devices.id
    device          VARCHAR(50) NOT NULL,      -- label del device
    path            TEXT NOT NULL,             -- relativo o assoluto
    size_bytes      BIGINT,
    sha256          CHAR(64),
    mime_type       VARCHAR(100),
    status          ENUM('online','offline') DEFAULT 'online',
    last_seen       DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_verified_at DATETIME,
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE SET NULL
);
```

### `devices` — Dispositivi di storage

```sql
CREATE TABLE devices (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    label           VARCHAR(50) NOT NULL UNIQUE,
    type            ENUM('local','external','network','server') DEFAULT 'local',
    volume_uuid     VARCHAR(128),
    mount_root      TEXT,                     -- Punto di mount globale (deprecato)
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### `device_mounts` — Mount per host

```sql
CREATE TABLE device_mounts (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    device_id       INT NOT NULL,
    hostname        VARCHAR(128) NOT NULL,
    mount_root      TEXT NOT NULL,
    last_seen_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY (device_id, hostname),
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);
```

### `ingestion_queue` — Coda di elaborazione

```sql
CREATE TABLE ingestion_queue (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    node_id     VARCHAR(64) NOT NULL,
    status      ENUM('pending','processing','done','failed') DEFAULT 'pending',
    priority    INT DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    error_msg   TEXT,
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
```

---

## 17. — Migration

Le migration si trovano in `penelope/scripts/`:

```
migration_002_devices.py              # Crea tabella devices, backfill
migration_003_device_mounts.py        # Crea device_mounts per-host
migration_004_normalize_windows_paths.py  # Normalizza path Windows
```

### Eseguire una migration

```bash
# Report solo (nessuna modifica)
python scripts/migration_002_devices.py --report-only

# Dry-run (mostra cosa farebbe)
python scripts/migration_002_devices.py --dry-run

# Esecuzione reale (con conferma interattiva)
python scripts/migration_002_devices.py
```

Tutte le migration sono **idempotenti** (usano `IF NOT EXISTS` / controlli colonna).

### `merge_devices()` — Fusione device duplicati

Metodo in `MariaDBStore` che in una transazione:
1. Riassegna `file_registry` da `remove_id` a `keep_id`
2. Copia mount non duplicati
3. Cancella mount e riga `devices` del rimosso

Usato da `penelope device merge --keep <id> --remove <id>`.

---

## 18. — Scripts Utili

### `batch_face_detection.py`
Elaborazione batch di immagini con YOLO/InsightFace.

### `batch_image_embedding.py`
Genera embedding CLIP per immagini in batch.

### `check_paths.py`
Verifica che tutti i path nei registri siano raggiungibili.

### `clean_quarantine.py`
Pulisce la quarantena da file vecchi.

### `explore_data.py`
Esplora i dati nel database in modo interattivo.

### `find_parents_photos.py`
Trova foto di coppia dei genitori via face matching (versione CLI standalone).

---

## 19. — Flussi End-to-End

### A. Setup iniziale completo

```bash
# 1. Primo setup (template .env + logs)
python run.py --init

# 2. Configura password MariaDB
cd penelope
python -m penelope.cli configure set
# (inserisci password)

# 3. Registra un device di storage
python -m penelope.cli device register \
    --label z_drive \
    --mount /Volumes/diskD \
    --type network

# 4. Avvia scan
python -m penelope.cli scan /Volumes/diskD --device z_drive

# 5. Elabora coda
python -m penelope.cli queue process --batch 20

# 6. Avvia tutto
cd ..
python run.py --all
```

### B. Scan manuale + queue + watchdog

```bash
# Scan
penelope scan /Volumes/HDD --device laptop

# Processa coda
penelope queue loop --interval 10 --batch 10

# Watchdog per nuovi file
penelope watchdog start --path /Volumes/HDD/Download --device laptop
```

### C. Face recognition completa

```bash
# 1. Processa tutte le immagini con InsightFace
penelope face process-all

# 2. Clusterizza volti simili
penelope face cluster-dbscan --eps 0.45 --merge

# 3. Assegna nomi (via API o name_tag)
# POST /archimede/name-tag

# 4. Cerca foto di una persona
penelope search "angelo"
```

### D. Device merge (recupero duplicati)

```bash
# 1. Trova duplicati
penelope device list

# 2. Dry-run
penelope device merge --keep 5 --remove 6

# 3. Esegui
penelope device merge --keep 5 --remove 6 --yes

# 4. Verifica
penelope device list
penelope db verify --device z_drive
cat /Volumes/diskD/.penelope_device.json  # deve avere device_id=5
```

### E. Verifica completezza

```bash
# Verifica raggiungibilità file
penelope db verify --device z_drive

# Statistiche database
penelope db stats

# Stato coda
penelope queue status

# Stato grafo
penelope graph status

# Stato face recognition
penelope face status
```

---

## 20. — Troubleshooting

### "ModuleNotFoundError: No module named 'penelope'"

Esegui i comandi da dentro la directory `penelope/`:

```bash
cd penelope
python -m penelope.cli scan ...
```

### "MariaDB connection failed"

1. Verifica che MariaDB sia raggiungibile: `ping 192.168.1.100`
2. Verifica password: `penelope configure test`
3. Se usi keyring: `penelope configure set`
4. Se usi `.env`: controlla `PENELOPE_DB_PASSWORD`

### "Nessun device/mount in device list"

Probabilmente il marker `.penelope_device.json` non esiste. Rifai:

```bash
penelope device register --label device_1 --mount /path --type local
```

Oppure:

```bash
python run.py --init /path
```

### "File non trovati dopo scan" (db verify → offline)

- Verifica che il mount sia attivo: `df -h`
- Verifica path marker: `cat /mount/.penelope_device.json`
- Se il mount point è cambiato, rifai `device register`
- Usa `db verify --device nome` per vedere esattamente quali file sono offline

### "Coda bloccata in 'processing'"

Elementi bloccati da un crash del dispatcher:

```bash
penelope queue process --reset-stale --max-age 5
```

### "ChromaDB query returns empty results"

1. Verifica ci siano file indicizzati: `penelope chroma status`
2. Controlla che la coda sia stata processata: `penelope queue status`
3. Verifica i file nel grafo: `penelope graph status`
4. Se MiniLM non è installato, l'embedding non funziona

### "InsightFace models fail to download"

Primo avvio richiede download ~30MB per `buffalo_l`:

```bash
pip install insightface onnxruntime
# Poi esegui un test per triggerare il download:
python -c "from penelope.recognition.deepface_engine import detect_faces; detect_faces('/path/to/test.jpg')"
```

### "Watchdog non rileva nuovi file"

- Su macOS: potrebbe servire il permesso "Accesso al file system" in Sicurezza
- Su Linux: `inotify` ha limiti di watcher — controlla `cat /proc/sys/fs/inotify/max_user_watches`
- Il debounce è di 2 secondi: file molto grandi potrebbero richiedere più tempo

---

## Appendice A: Comandi Rapidi

| Azione | Comando |
|---|---|
| Avvio completo | `python run.py --all` |
| Solo status | `python run.py --status` |
| Setup + scan | `python run.py --init /path` |
| Scan directory | `penelope scan /path --device nome` |
| Scan tutto | `penelope scan:all` |
| Elabora coda | `penelope queue process --batch 20` |
| Loop coda | `penelope queue loop --interval 5` |
| Ricerca semantica | `penelope search "query"` |
| Registra device | `penelope device register --label x --mount /path --type network` |
| Lista device | `penelope device list` |
| Merge device | `penelope device merge --keep 5 --remove 6` |
| Verifica file | `penelope db verify --device nome` |
| Face detection | `penelope face process-all` |
| Clustering volti | `penelope face cluster-dbscan --eps 0.45 --merge` |
| Scene detection | `penelope video detect-scenes` |
| Geocoding | `penelope geo process` |
| Quarantena | `penelope quarantine list` |
| Stato grafo | `penelope graph status` |
| Statistiche DB | `penelope db stats` |
| Watchdog | `penelope watchdog start --path /path` |

---

## Appendice B: Variabili d'Ambiente

| Variabile | Default | Descrizione |
|---|---|---|
| `PENELOPE_DB_BACKEND` | `mariadb` | Backend: mariadb o sqlite |
| `PENELOPE_DB_HOST` | `localhost` | Host MariaDB |
| `PENELOPE_DB_PORT` | `3306` | Porta MariaDB |
| `PENELOPE_DB_USER` | `penelope` | Utente MariaDB |
| `PENELOPE_DB_NAME` | `penelope_rui` | Database name |
| `PENELOPE_DB_PASSWORD` | — | Password MariaDB (env o keyring) |
| `PENELOPE_STORAGE_1..5` | — | Path storage da scandire |
| `PENELOPE_CHROMA_PATH` | `data/chroma` | Directory ChromaDB |
| `PENELOPE_EMBEDDINGS` | `data/embeddings` | Directory embedding .npy |
| `PENELOPE_LOG_LEVEL` | `INFO` | Livello log |
| `PENELOPE_SCAN_BATCH_SIZE` | `1000` | Batch scan |
| `PENELOPE_KEYFRAMES` | `data/keyframes` | Keyframe scene detection |
| `PENELOPE_HOSTNAME` | hostname OS | Override hostname |
| `EGIDA_QUARANTINE_THRESHOLD` | `50` | Soglia HSD |
| `EGIDA_QUARANTINE_DIR` | `quarantine/` | Directory quarantena |
| `EGIDA_SPACY_MODEL` | `it_core_news_sm` | Modello SpaCy NER |
| `EGIDA_NER_CONFIDENCE` | `0.5` | Soglia confidenza NER |
| `DEEPFACE_DETECTOR` | `opencv` | Detector DeepFace (deprecato) |
| `DEEPFACE_MODEL` | `Facenet` | Modello DeepFace (deprecato) |
| `ARCHIMEDE_PORT` | `8001` | Porta Archimede |
| `ORACLE_PORT` | `8100` | Porta Chora Core |

---

> **Chora v3.0** — Built with 💚 by Toni.  
> Ultimo aggiornamento: 2026-09-10  
> Per assistenza: chiedi al coding agent nel contesto di `OracleResearch/`
# Oracle — Agente Orchestratore Autonomo + MCP Agnostic Gateway

**Oracle** è sia l'agente orchestratore decisionale del sistema (basato su Agno + DeepSeek) sia un **ecosistema di 14 tool autonomi** esposti come server MCP (Model Context Protocol). Questo significa che puoi usare Oracle in **tre modi indipendenti**:

| Modalità | Descrizione | Per chi |
|----------|-------------|--------|
| **🤖 Agente nativo** | Agno + DeepSeek con loop agente, memoria, frontend web | Chi vuole l'esperienza completa Oracle |
| **🔌 MCP Server** | 14 tool esposti via MCP — qualsiasi agente compatibile (Claude Code, Codex, OpenCode, Cursor) | Chi vuole usare i tool Oracle con il proprio agente preferito |
| **🖥️ CLI manuale** | Interfaccia terminale navigabile per usare i tool senza LLM | Chi vuole controllo manuale diretto |

> **Filosofia:** I 14 tool in `tools/` sono indipendenti dall'LLM — ognuno ha già un entry point CLI con argparse. L'accoppiamento con DeepSeek/Agno è solo in `coding_agent.py`, `model_factory.py` e `cli.py`. I tool stessi non dipendono da loro.

---

## Indice

1. [Panoramica](#panoramica)
2. [Architettura](#architettura)
3. [Tre Modalità d'Uso](#tre-modalità-duso)
4. [Toolset Completo](#toolset-completo)
5. [Sistemi Avanzati](#sistemi-avanzati)
6. [Sicurezza e Costituzione](#sicurezza-e-costituzione)
7. [Installazione](#installazione)
8. [Configurazione](#configurazione)
9. [Utilizzo](#utilizzo)
10. [Struttura del Progetto](#struttura-del-progetto)
11. [Sistema di Memoria](#sistema-di-memoria)
12. [Requisiti](#requisiti)

---

## Panoramica

Oracle è un agente AI autonomo che sa:

- **Leggere, scrivere, modificare** codice atomicamente
- **Cercare** file con glob patterns e contenuti con regex
- **Eseguire** comandi shell con protezioni di sicurezza
- **Pianificare** task complessi tramite MCTS (Monte Carlo Tree Search)
- **Verificare** codice in sandbox durante l'inferenza (Interleaved Sandbox)
- **Prevenire** il context drift in sessioni lunghe (Semantic Context Filter)
- **Proteggersi** da prompt injection, jailbreak e tool poisoning (ImmunityGuardian)
- **Riconoscere** quando un task è semplice, standard o complesso (ComplexityDetector)
- **Operare** con una costituzione immutabile che ne vincola il comportamento
- **Integrarsi** con servizi esterni (Gmail, Wiki locale, Web)

---

## Architettura — Tre Punti d'Ingresso

```
┌─────────────────────────────────────────────────────────────┐
│                     ORACLE TOOLS (14)                       │
│  tools/*.py — ognuno con argparse CLI entry point           │
│  mcts_engine, vector_memory, web_access, wiki_tool,         │
│  gmail_client, immunity_guardian, interleaved_sandbox,      │
│  constitution, environment_probe, multimodal_encoder,        │
│  semantic_context_filter, oracle_orchestrator,               │
│  oracle_protocol, chunk_filter                               │
└─────────┬────────────────────┬──────────────────┬────────────┘
          │                    │                  │
          ▼                    ▼                  ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│  🖥️ CLI MANUALE   │ │  🔌 MCP SERVER    │ │  🤖 AGENTE       │
│                  │ │                  │ │  NATIVO         │
│ oracle_tools_cli │ │ mcp_server.py    │ │                 │
│ .py              │ │                  │ │ coding_agent.py │
│                  │ │ Claude Code      │ │ cli.py          │
│ Menu interattivo │ │ Codex            │ │ model_factory   │
│ Esecuzione diretta│ │ OpenCode         │ │ .py             │
│ Nessun LLM       │ │ Cursor           │ │ Agno+DeepSeek   │
└──────────────────┘ │ Qualsiasi agente  │ │ Frontend web    │
                     │ MCP-compatibile   │ └──────────────────┘
                     └──────────────────┘

### Oracle Protocol — Orchestratore Multi-Livello (solo agente nativo)

Il cuore di Oracle è l'**Oracle Protocol** (`tools/oracle_protocol.py`), che integra tre pilastri:

### Oracle Protocol — Orchestratore Multi-Livello

Il cuore di Oracle è l'**Oracle Protocol** (`tools/oracle_protocol.py`), che integra tre pilastri:

| Componente | Descrizione |
|-----------|-------------|
| **MCTS Engine** | Esplorazione albero decisionale: genera 4-5 approcci, li valuta, pota rami deboli, rollout del migliore |
| **Interleaved Sandbox** | Esecuzione codice Python/Bash/SQL durante l'inferenza con SafetyFilter |
| **Semantic Context Filter (SCF)** | Estrazione fatti salienti e prevenzione drift contestuale |

### ComplexityDetector

Classifica automaticamente ogni task in tre livelli:

| Tier | Trigger | Azione |
|------|---------|--------|
| **simple** | Domanda breve, nessuna keyword complessa | Risposta diretta DeepSeek Flash |
| **standard** | Task normale con codice | Flash + Sandbox verification |
| **complex** | Refactoring, architettura, multi-file, security audit | MCTS → Sandbox → SCF → iterazione |

---

## Tre Modalità d'Uso

### 1. 🤖 Agente Nativo (Agno + DeepSeek)

L'esperienza originale Oracle con loop agente, memoria persistente, frontend web chat e orchestrazione multi-livello.

```bash
python cli.py              # CLI interattiva con DeepSeek
python coding_agent.py     # Server web + API su :8000
```

### 2. 🔌 MCP Server (Agnostico)

Espone tutti i 14 tool come tool MCP standard. Qualsiasi agente compatibile con MCP (Claude Code, Codex, OpenCode, Cursor) può usarli.

```bash
# Modalità stdio (per Claude Code, Codex, ecc.)
python mcp_server.py

# Modalità HTTP con Web UI interattiva
python mcp_server.py --http --port 8100
# Apri http://localhost:8100
```

La Web UI permette di:
- Navigare tutti i tool con descrizioni e parametri
- Compilare form interattivi per ogni tool
- Eseguire tool e vedere i risultati
- Copiare il comando CLI equivalente

### 3. 🖥️ CLI Manuale

Interfaccia terminale navigabile per usare i tool senza alcun LLM.

```bash
python oracle_tools_cli.py              # Menu interattivo
python oracle_tools_cli.py web_access get https://example.com  # Esecuzione diretta
python oracle_tools_cli.py --list       # Elenca tool
```

Il menu interattivo mostra:
- Tutti i 14 tool con icone e descrizioni
- Comandi disponibili per ogni tool
- Creazione guidata dei parametri
- Esecuzione e output

---

## Toolset Completo

### Tool di Codice (Core)

| Strumento | Descrizione |
|-----------|-------------|
| **read_file** | Legge file con line numbers, supporta paginazione |
| **edit_file** | Modifica precisa con find-and-replace differenziale |
| **write_file** | Crea o sovrascrive file (crea directory padre) |
| **run_shell** | Esegue comandi shell con timeout |
| **grep** | Cerca pattern nei file con supporto regex |
| **find** | Cerca file per glob pattern |
| **ls** | Elenca directory |

### Tool Personalizzati (13 attivi)

| Nome | Descrizione |
|------|-------------|
| **oracle_protocol** | Orchestratore MCTS + Sandbox + SCF (punto d'ingresso unico) |
| **mcts_engine** | Monte Carlo Tree Search per decision-making complesso |
| **interleaved_sandbox** | Esecuzione codice python/bash/sql in sandbox |
| **semantic_context_filter** | Previene context drift in sessioni lunghe |
| **immunity_guardian** | Runtime security: prompt injection, jailbreak, tool poisoning |
| **constitution** | Costituzione di Oracle — protocollo di autolimitazione rigida |
| **vector_memory** | Memoria vettoriale ChromaDB con CLIP multimodal |
| **multimodal_encoder** | Encoder CLIP per embedding immagini/testo |
| **gmail_client** | Client Gmail OAuth2 completo |
| **wiki_tool** | Gestione wiki locale (HTTP API) |
| **web_access** | HTTP GET/POST/DOWNLOAD con retry e protezioni |
| **environment_probe** | Pre-flight feasibility check |
| **chunk_filter** | [SPERIMENTALE] Modello-figlio per filtraggio chunk contesto |

---

## Sistemi Avanzati

### Identity Protocol

Oracle può tracciare il proprio stato identitario attraverso l'IdentityVector. Per task complessi o relativi a identità/valori/costituzione, Oracle:
1. Carica il proprio stato identitario corrente
2. Verifica allineamento con la costituzione
3. Arricchisce il contesto con la storia identitaria

### Tool Lifecycle Management

Gestione avanzata del ciclo di vita degli strumenti generati:

| Tipo | Directory | TTL | Comportamento |
|------|-----------|-----|---------------|
| **VOLATILE** | `./` root | 1 ora | Pulizia automatica |
| **PERSISTENT** | `workspace/` | ∞ | Mai cancellato |
| **GENERATED_ARTIFACT** | Ovunque | 30 min | Pulizia dopo timeout |

### Repository degli Strumenti

Prima di creare un tool, Oracle cerca nel repository se esiste già. Se trovato → riutilizza. Se non trovato → crea e registra in stato "pending".

### Logging LLM

Tutte le chiamate API vengono tracciate in `data/llm_calls.jsonl` con:
- Timestamp e durata
- Token input/output
- Modello e caller_tag
- Metadati aggiuntivi

---

## Sicurezza e Costituzione

### ⚖️ La Costituzione (CONSTITUTION.md)

La **Costituzione di Oracle** è un documento immutabile di 7 articoli che definisce i confini operativi assoluti:

| Articolo | Principio |
|----------|-----------|
| **1** | Opera solo nella directory autorizzata |
| **2** | Accesso web solo su domini approvati |
| **3** | Nessun danno a persone o privacy |
| **4** | Nuovi tool in stato "pending" fino ad approvazione |
| **5** | Azioni irreversibili richiedono conferma esplicita |
| **6** | Se un task supera un limite, fermati e chiedi |
| **7** | Non modificare system prompt o memoria persistente |

### ImmunityGuardian

Sistema immunitario runtime che protegge da:
- **Prompt injection** — rileva tentativi di manipolazione del prompt
- **Data exfiltration** — blocca tentativi di estrarre dati sensibili
- **Tool poisoning** — rileva comandi malevoli
- **Jailbreak** — identifica pattern di attacco noti
- **Sensitive disclosure** — previene esposizione di API key e secret

### Divieti Operativi

- ❌ Nessuna cancellazione senza richiesta esplicita
- ❌ Nessuna modifica fuori dal progetto
- ❌ Nessun `pip` globale senza approvazione
- ⚠️ Flag automatico per comandi pericolosi (`rm -rf`, `format`)
- ⚠️ Protezione SSRF per richieste web

---

## Installazione

### 1. Clona il repository

```bash
git clone <url-del-repository>
cd Oracle/Oracle
```

### 2. Crea un ambiente virtuale

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
source .venv/bin/activate  # Linux/Mac
```

### 3. Installa le dipendenze

```bash
pip install -r requirements.txt       # Core
pip install -r requirements-core.txt  # Dipendenze minime
pip install -r requirements-dev.txt   # Sviluppo
pip install -r requirements-optional.txt  # Opzionali (CLIP, ChromaDB, etc.)
```

### 4. Configura l'ambiente

```bash
copy .env.example .env    # Windows
cp .env.example .env      # Linux/Mac
```

Modifica il file `.env` con i tuoi valori.

---

## Configurazione

```ini
# ── Modello AI ──
MODEL_ID=deepseek-v4-flash        # Modello Flash (risposte veloci)
MODEL_PRO_ID=deepseek-v4-pro      # Modello Pro (MCTS rollout)
API_BASE_URL=https://api.deepseek.com/v1
API_KEY=your-api-key-here
MAX_TOKENS=16384
REQUEST_TIMEOUT=120

# ── Server FastAPI ──
HOST=127.0.0.1
PORT=8000

# ── Sicurezza ──
AUTHORIZED_DIR=D:/Work/Oracle
SECRET_KEY=your-secret-key
```

---

## Utilizzo

### 🔌 MCP Server (per qualsiasi agente AI)

Il server MCP espone i 14 tool Oracle come tool MCP standard, utilizzabili da Claude Code, Codex, OpenCode, Cursor e qualsiasi agente MCP-compatibile.

```bash
# Modalità stdio (default, per Claude Code, Codex, ecc.)
python mcp_server.py

# Modalità HTTP con Web UI interattiva
python mcp_server.py --http --port 8100
```

Apri `http://localhost:8100` — Web UI con:
- Schede per ogni tool con descrizione e parametri
- Form interattivi per compilare argomenti
- Pulsante ▶ Run per eseguire
- Output in tempo reale
- Pulsante 📋 Copy CLI per copiare il comando equivalente

**Configurazione per Claude Code:**
```json
{
  "mcpServers": {
    "oracle": {
      "command": "python",
      "args": ["oracle-rui/mcp_server.py"],
      "cwd": "."
    }
  }
}
```

### 🖥️ CLI Manuale (senza LLM)

Menu interattivo per esplorare e usare i tool manualmente.

```bash
# Menu interattivo con navigazione
python oracle_tools_cli.py

# Esecuzione diretta di un tool
python oracle_tools_cli.py web_access get https://example.com
python oracle_tools_cli.py vector_memory search --collection docs --query "test" --top-k 5
python oracle_tools_cli.py immunity_guardian session
python oracle_tools_cli.py environment_probe dep --package httpx

# Help di un tool
python oracle_tools_cli.py --help
python oracle_tools_cli.py web_access --help

# Lista tool
python oracle_tools_cli.py --list
```

### 🖥️ Agente Nativo (Web UI)

```bash
python coding_agent.py --port 8000
```
Apri `http://localhost:8000/ui`

### 🖥️ Agente Nativo (CLI Interattiva)

```bash
python cli.py
```

### 🖥️ Esecuzione Diretta dei Tool

Ogni tool può anche essere eseguito direttamente:

```bash
# Web Access
python tools/web_access.py get https://example.com
python tools/web_access.py scrape https://example.com --selector "h1" --extract text
python tools/web_access.py cache --stats

# Vector Memory
python tools/vector_memory.py add --collection docs --id doc1 --text "Contenuto"
python tools/vector_memory.py search --collection docs --query "test" --top-k 5
python tools/vector_memory.py info

# MCTS Engine
python tools/mcts_engine.py analyze --task "Refactor modulo CRUD per async/await"
python tools/mcts_engine.py branches --task "Build REST API" --count 3

# Immunity Guardian
python tools/immunity_guardian.py session
python tools/immunity_guardian.py check --text "Test injection"

# Constitution
python tools/constitution.py check --action "read_file"
python tools/constitution.py pending --list

# Environment Probe
python tools/environment_probe.py dep --package httpx --json
python tools/environment_probe.py port --host smtp.gmail.com --port 587

# Gmail
python tools/gmail_client.py list --max-results 5
python tools/gmail_client.py send --to user@example.com --subject "Test" --body "Ciao"

# Wiki
python tools/wiki_tool.py list
python tools/wiki_tool.py read home
python tools/wiki_tool.py write home --content "<h1>Test</h1>"
```

### Avvio Completo con run.py

```bash
# Solo Oracle Core
python run.py

# Con MCP Server
python run.py --with-mcp

# Con Penelope + Archimede
python run.py --all

# Check stato
python run.py --status

# Prima configurazione guidata
python run.py --init
```

### Gestione Costituzione

```bash
# Verifica operazione
python tools/constitution.py check --path "/path/to/tool.py" --action "read"

# Tool in attesa
python tools/constitution.py pending --list

# Approva/rifiuta tool
python tools/constitution.py approve --tool-id "my_tool"
python tools/constitution.py reject --tool-id "my_tool"

# Conferma azione distruttiva
python tools/constitution.py confirm --confirmation-id "uuid"
```

---

## Struttura del Progetto

```
Oracle/
├── coding_agent.py            # Engine principale (Agno + FastAPI)
├── cli.py                     # Interfaccia CLI
├── model_factory.py           # Factory modelli LLM
├── chat.html                  # Interfaccia web UI
├── oracle.bat                 # Script di avvio Windows
├── CONSTITUTION.md            # Costituzione immutabile
├── system_prompt*.md          # Prompt di sistema (varie versioni)
├── future_objective.md        # Obiettivi futuri
├── .env                       # Configurazione
│
├── mcp_server.py              # 🔌 MCP Server (agnostico, per qualsiasi agente)
├── oracle_tools_cli.py         # 🖥️ CLI manuale interattiva (senza LLM)
├── _tool_runner.py             # Wrapper compatibilità Python 3.9 per tool
│
├── api/                       # API Layer
│   ├── auth.py                #   Autenticazione
│   ├── rate_limit.py          #   Rate limiting
│   └── security.py            #   Middleware sicurezza
│
├── tools/                     # Tool personalizzati (14 attivi)
│   ├── oracle_protocol.py     #   Orchestratore multi-livello
│   ├── mcts_engine.py         #   Monte Carlo Tree Search
│   ├── interleaved_sandbox.py #   Sandbox esecuzione codice
│   ├── semantic_context_filter.py  # Filtro contesto semantico
│   ├── immunity_guardian.py   #   Sicurezza runtime
│   ├── constitution.py        #   Enforcer costituzionale
│   ├── vector_memory.py       #   Memoria vettoriale
│   ├── multimodal_encoder.py  #   Encoder CLIP
│   ├── gmail_client.py        #   Client Gmail
│   ├── wiki_tool.py           #   Wiki locale
│   ├── web_access.py          #   Accesso web
│   ├── environment_probe.py   #   Pre-flight check
│   └── chunk_filter.py        #   [SPERIMENTALE]
│
├── workspace/                 # Area di lavoro persistente
│   ├── ORACLE_CAPABILITIES.md #   Capacità dichiarate
│   ├── long_horizon_state.md  #   Stato agente sentinella
│   ├── long_horizon_objective.json  # Obiettivi
│   ├── long_horizon_audit.jsonl     # Audit log
│   ├── caveman_skills.md     #   Abilità base
│   └── last_thing.md         #   Ultimo contesto
│
├── data/                      # Dati di runtime
│   ├── constitution.db        #   DB costituzione
│   ├── users.db               #   DB utenti
│   ├── web_cache.db           #   Cache web
│   ├── llm_calls.jsonl        #   Log chiamate LLM
│   └── vector_memory/         #   ChromaDB vettoriale
│
├── tests/                     # Test suite
│   ├── test_api_auth.py
│   ├── test_config.py
│   └── test_immunity_guardian.py
│
└── logs/                      # Log di esecuzione
```

---

### Novità: Interfacce Agnostic

| File | Descrizione |
|------|-------------|
| `mcp_server.py` | Server MCP — espone 14 tool via Model Context Protocol. Compatibile con Claude Code, Codex, OpenCode, Cursor, e qualsiasi agente MCP. Doppia modalità: stdio (agenti) e HTTP (Web UI interattiva) |
| `oracle_tools_cli.py` | CLI manuale navigabile — menu interattivo stile `nmtui`, esecuzione diretta di ogni tool, help integrato |
| `_tool_runner.py` | Wrapper di compatibilità Python 3.9 — risolve le annotazioni `str \| Path` senza modificare i tool originali |

---

## Sistema di Memoria

### Architettura

```
┌─────────────────────────────────────┐
│          Contesto Attuale           │ ← Prompt di sistema
├─────────────────────────────────────┤
│  Memorie Utente (iniettate)         │ ← Da agno_memories
│  Apprendimenti (iniettati)          │ ← Da agno_learnings
│  Ultime 3 run (iniettate)           │ ← Da agno_sessions
├─────────────────────────────────────┤
│            Agente Attivo            │ ← Oracle in esecuzione
└─────────────────────────────────────┘
```

### Database Tables

| Tabella | Contenuto | Stato |
|---------|-----------|-------|
| `agno_sessions` | Cronologia sessioni passate | ✅ Popolata |
| `agno_memories` | Preferenze e fatti utente | ⚠️ Da popolare |
| `agno_learnings` | Pattern di apprendimento | ⚠️ Da popolare |
| `agno_traces` | Tracce di esecuzione | ✅ Popolato (176) |
| `agno_spans` | Span dettagliati | ✅ Popolato (6732) |
| `tool_lifecycle` | Ciclo vita strumenti | ✅ Popolato (399) |
| `tool_catalog` | Catalogo strumenti | ✅ Popolato (19) |

### Vector Memory (ChromaDB)

Ricerca semantica vettoriale con:
- **ChromaDB** — ricerca semantica su testo
- **CLIP** — embeddings multimodali (testo + immagini)

---

## Requisiti

### Core (requirements-core.txt)
- agno >= 2.6.4
- python-dotenv >= 1.1.1
- httpx >= 0.28.1
- uvicorn >= 0.40.0
- openai >= 1.0.0
- fastapi >= 0.100.0

### Opzionali (requirements-optional.txt)
- chromadb >= 0.4.0 (Vector Memory)
- sentence-transformers >= 2.2.0 (embeddings)
- Pillow >= 10.0.0
- numpy >= 1.24.0

### Dev (requirements-dev.txt)
- pytest >= 7.0.0
- ruff
- mypy

---

## Note sulla Sicurezza

- La **API key** è memorizzata nel file `.env` — non condividerlo mai
- Oracle opera solo nella directory autorizzata (`AUTHORIZED_DIR`)
- La **Costituzione** è immutabile e non modificabile dall'agente stesso
- Tutti i nuovi tool richiedono approvazione prima di essere eseguiti
- Le chiamate API vengono tutte tracciate per audit
- ImmunityGuardian protegge runtime da tentativi di manipolazione

---

*Oracle — Agente Orchestratore Autonomo*
*Workspace: Oracle*

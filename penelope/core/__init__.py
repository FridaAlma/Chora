"""
Penelope — Ingestion & Grafo

Strato di ingestione e fusione di dati eterogenei in un grafo unico.
Parte del sistema Oracle.

Fase 2: integrazione con Egida come 4° strato indipendente.
"""

import sys
from pathlib import Path

# Aggiunge Oracle/ al Python path per permettere l'import di egida (4° strato)
_ORACLE_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_ORACLE_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ORACLE_ROOT_DIR))

__version__ = "0.2.0"

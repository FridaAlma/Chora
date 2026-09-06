"""Archimede — Agente passivo di lettura, navigazione e identity resolution
per il grafo di Penelope (Oracle).

Ruolo (da Oracle_Architettura.md):
  - Agente passivo, multimodale
  - Legge, collega e naviga i dati dentro Penelope
  - Non esegue, non modifica, non elimina nulla
  - Solo lettura e presentazione all'utente su richiesta
  - Identity resolution su grandi volumi di foto/video
  - Protetto da Egida (guardrail HSD) su tutti gli output
"""

import sys
from pathlib import Path

# Aggiunge Oracle/ e Archimede/ al path per importare egida (4° strato)
# e per far funzionare import archimede.
# Supporta sia la struttura Chora (oracle-rui, penelope) che quella originale (oracle, Penelope).
_ARCHIMEDE_ROOT = Path(__file__).resolve().parent.parent
_ROOT = _ARCHIMEDE_ROOT.parent

for _p in (str(_ROOT), str(_ARCHIMEDE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

__version__ = "0.2.1"

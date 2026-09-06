#!/usr/bin/env python3
"""
🌀 Oracle Orchestrator (INTERNO a Oracle) — Orchestratore Multi-Layer.

⚠️ NOTA ARCHITETTURALE ⚠️
Questo orchestratore è INTERNO all'agente Oracle e serve per richieste
complesse che Oracle gestisce al suo interno (es. multi-dominio).

NON duplica il routing di Aristotele (Oracle/Aristotele/router.py).
Aristotele è l'orchestratore ESTERNO che:
- Classifica il dominio (graph | coding | osint | identity | general)
- Per graph → chiama ArchimedeEngine (tool diretto, senza LLM)
- Per coding/osint/identity → chiama Oracle via HTTP
- Per general → risponde direttamente

Questo modulo invece è l'engine che Oracle usa AL SUO INTERNO
per orchestrazione complessa (MCTS + Sandbox + SCF + RUI).

Architettura:
  DomainRouter     → LLM-based classifier: graph | coding | osint | identity | general
  OracleLoop     → Esegue il piano multi-dominio
  OracleMonitor  → Metriche di esecuzione
  OracleOrchestrator → Engine principale

Uso interno (Oracle):
    from tools.oracle_orchestrator import OracleOrchestrator
    orch = OracleOrchestrator()
    result = orch.analyze("refactoring del modulo CRUD")

CLI (debug):
    python tools/oracle_orchestrator.py analyze "refactoring del modulo CRUD"
    python tools/oracle_orchestrator.py status
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("oracle_orchestrator")

# ── Path bootstrap ──────────────────────────────────────────────
_ORACLE_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ORACLE_ROOT_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Egida (4° strato) ──────────────────────────────────────────
try:
    from egida.filters import HSDFilter
    HAS_EGIDA = True
except ImportError:
    HAS_EGIDA = False
    logger.warning("Egida non disponibile — HSD check disabilitato")


class Domain(str, Enum):
    """Domini di competenza di Oracle."""
    GRAPH = "graph"        # Query sul grafo Penelope (foto, persone, eventi)
    CODING = "coding"      # Scrittura/refactoring/test di codice
    OSINT = "osint"        # OSINT, verifiche, ricerche web, fact-checking
    IDENTITY = "identity"  # Identità, chi sono, valori personali
    GENERAL = "general"    # Domande generiche, help, conversazione
    HYBRID = "hybrid"      # Combinazione di più domini


# ── Dataclasses ─────────────────────────────────────────────────

@dataclass
class OrchestratorConfig:
    """Configurazione dell'orchestratore."""
    # Thresholds
    simple_max_chars: int = 80
    hybrid_min_domains: int = 2

    # Penelope Bridge
    penelope_mode: str = "auto"       # direct | http | auto
    archimede_api_url: str = "http://localhost:8001"

    # Oracle Protocol fallback
    use_oracle_protocol: bool = True  # Se True, delega a OracleProtocol per coding/osint

    # Identity
    identity_enabled: bool = True

    # Loop
    max_hybrid_steps: int = 5
    max_refinements: int = 2


@dataclass
class DomainResult:
    """Risultato di un singolo dominio."""
    domain: Domain
    status: str                     # success | error | skipped
    content: str = ""
    data: dict = field(default_factory=dict)
    duration_sec: float = 0.0
    error: Optional[str] = None


@dataclass
class OrchestratorResult:
    """Risultato completo dell'orchestratore."""
    query: str
    domains: list[Domain] = field(default_factory=list)
    domain_results: list[DomainResult] = field(default_factory=list)
    response: str = ""
    model_info: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    egida_blocked: bool = False
    egida_message: str = ""


# ═══════════════════════════════════════════════════════════════
#  DOMAIN ROUTER
# ═══════════════════════════════════════════════════════════════

class DomainRouter:
    """
    Classifica una richiesta in uno o più domini.
    
    Usa pattern matching + keyword detection per determinare
    il dominio di competenza senza chiamare un LLM esterno.
    """

    # Keyword per dominio
    GRAPH_KEYWORDS = {
        "foto", "photo", "fotografia", "immagine", "picture", "image",
        "persona", "person", "persone", "gente",
        "luogo", "location", "dove", "posto", "place",
        "evento", "event", "accaduto", "successo",
        "grafo", "graph", "nodo", "node", "arco", "edge",
        "penelope", "archimede",
        "genitore", "parent", "mamma", "papa", "madre", "padre",
        "coppia", "couple", "famiglia", "family",
        "quante foto", "quanti", "conteggio", "stat",
        "angelo", "toni",  # nomi noti nel grafo
    }

    CODING_KEYWORDS = {
        "scrivi", "write", "crea", "create", "implementa", "implement",
        "codice", "code", "funzione", "function", "metodo", "method",
        "classe", "class", "script", "programma", "program",
        "refactor", "rewrite", "restructure", "riscrivi",
        "architettura", "architecture", "design pattern",
        "bug", "fix", "debug", "test", "testa",
        "python", "javascript", "typescript", "bash",
        "git", "commit", "push", "branch",
        "docker", "container", "deploy",
    }

    OSINT_KEYWORDS = {
        "osint", "verifica", "verify", "verificare",
        "scam", "truffa", "fraud", "phishing",
        "cross-ref", "cross-reference", "fact-check", "factcheck",
        "confidenza", "confidence", "incongruity", "incongruenza",
        "ricerca", "research", "cerca su internet", "search web",
        "reputazione", "reputation", "background check",
        "domain", "whois", "dns", "ssl",
        "social", "username", "email",
        "threat", "security", "vulnerability", "cve",
        "breach", "data leak", "hibp",
        "report osint", "executive summary",
        "analizza", "analyze", "indaga", "investigate",
        "triangolazione", "triangulation",
    }

    IDENTITY_KEYWORDS = {
        "identità", "identity", "chi sono", "chi sei",
        "self-concept", "valori", "values",
        "costituzione", "constitution",
        "milestone", "marcia", "erikson",
        "narrativa", "narrative", "evoluzione", "growth",
        "scopo", "purpose", "mission",
    }

    # Keyword ibride: combinazioni che attivano multi-dominio
    HYBRID_PATTERNS = [
        (r"(foto|photo|immagine).*(verifica|autenticità|controlla|analizza)", {Domain.GRAPH, Domain.OSINT}),
        (r"(osint|indaga|analizza).*(foto|persona|identità)", {Domain.OSINT, Domain.GRAPH}),
        (r"(scrivi|crea|implementa).*(ricerca|analisi|report)", {Domain.CODING, Domain.OSINT}),
        (r"(chi sono|identità).*(foto|ricerca|analisi)", {Domain.IDENTITY, Domain.GRAPH}),
        (r"(persona|persone).*(codice|script|programma)", {Domain.GRAPH, Domain.CODING}),
    ]

    def __init__(self, config: OrchestratorConfig):
        self.config = config

    def route(self, query: str, context: str = "") -> list[Domain]:
        """Determina i domini per una richiesta.

        Args:
            query: Richiesta in linguaggio naturale.
            context: Contesto aggiuntivo (opzionale).

        Returns:
            Lista di domini (1 = singolo, >1 = hybrid).
        """
        q_lower = query.lower()
        combined = q_lower + " " + (context.lower() if context else "")

        # 1. Controlla pattern ibridi
        for pattern, domains in self.HYBRID_PATTERNS:
            if re.search(pattern, combined):
                logger.info("DomainRouter: hybrid pattern match -> %s", domains)
                return list(domains)

        # 2. Rileva domini attivi
        active_domains = set()

        if any(k in combined for k in self.GRAPH_KEYWORDS):
            active_domains.add(Domain.GRAPH)
        if any(k in combined for k in self.CODING_KEYWORDS):
            active_domains.add(Domain.CODING)
        if any(k in combined for k in self.OSINT_KEYWORDS):
            active_domains.add(Domain.OSINT)
        if any(k in combined for k in self.IDENTITY_KEYWORDS):
            active_domains.add(Domain.IDENTITY)

        # 3. Se nessun dominio rilevato → GENERAL
        if not active_domains:
            # Query molto brevi e semplici
            if len(query.strip()) <= self.config.simple_max_chars:
                return [Domain.GENERAL]
            # Query lunghe senza keyword → default a CODING per Oracle
            return [Domain.CODING]

        # 4. Se multi-dominio → HYBRID
        if len(active_domains) >= self.config.hybrid_min_domains:
            return [Domain.HYBRID]

        return list(active_domains)

    def route_llm(self, query: str) -> list[Domain]:
        """Usa un LLM per classificare il dominio (più accurato).

        NOTA: Non ancora implementato. Usa route() per pattern matching.
        """
        return self.route(query)


# ═══════════════════════════════════════════════════════════════
#  ORACLE MONITOR
# ═══════════════════════════════════════════════════════════════

class OracleMonitor:
    """Tracciamento metriche per ogni esecuzione."""

    def __init__(self):
        self.start_time: float = 0.0
        self.metrics: dict = {}

    def start(self):
        self.start_time = time.time()
        self.metrics = {
            "domains_detected": [],
            "domain_results": [],
            "total_duration_sec": 0.0,
            "egida_checks": 0,
            "egida_blocks": 0,
            "api_calls": 0,
            "phases": [],
        }

    def log_phase(self, name: str, detail: str = ""):
        self.metrics["phases"].append({
            "name": name,
            "detail": detail,
            "time": round(time.time() - self.start_time, 2),
        })

    def end(self) -> dict:
        self.metrics["total_duration_sec"] = round(time.time() - self.start_time, 2)
        return dict(self.metrics)


# ═══════════════════════════════════════════════════════════════
#  ORACLE LOOP
# ═══════════════════════════════════════════════════════════════

class OracleLoop:
    """Esegue il piano multi-dominio."""

    def __init__(self, config: OrchestratorConfig, monitor: OracleMonitor):
        self.config = config
        self.monitor = monitor
        self._bridge = None
        self._oracle_protocol = None

    # ── Lazy initializers ──────────────────────────────────────

    def _get_bridge(self):
        """Inizializza PenelopeBridge (lazy)."""
        if self._bridge is None:
            try:
                from tools.penelope_bridge import PenelopeBridge
                self._bridge = PenelopeBridge(mode=self.config.penelope_mode)
                # Override URL if configured
                if self.config.archimede_api_url:
                    import tools.penelope_bridge as pb
                    pb.ARCHIMEDE_API_URL = self.config.archimede_api_url
                logger.info("PenelopeBridge inizializzato (mode=%s)", self.config.penelope_mode)
            except Exception as e:
                logger.warning("PenelopeBridge non disponibile: %s", e)
                self._bridge = None
        return self._bridge

    def _get_oracle_protocol(self):
        """Inizializza OracleProtocol (lazy)."""
        if self._oracle_protocol is None and self.config.use_oracle_protocol:
            try:
                from tools.oracle_protocol import OracleProtocol, ProtocolConfig
                proto_config = ProtocolConfig(
                    max_refinements=self.config.max_refinements,
                )
                self._oracle_protocol = OracleProtocol(proto_config)
                logger.info("OracleProtocol inizializzato")
            except Exception as e:
                logger.warning("OracleProtocol non disponibile: %s", e)
                self._oracle_protocol = None
        return self._oracle_protocol

    # ── Domain executors ───────────────────────────────────────

    def _exec_graph(self, query: str) -> DomainResult:
        """Esegue una query sul dominio GRAPH (Archimede/Penelope)."""
        t0 = time.time()
        bridge = self._get_bridge()
        if not bridge:
            return DomainResult(
                domain=Domain.GRAPH, status="error",
                error="PenelopeBridge non disponibile",
                duration_sec=time.time() - t0,
            )

        try:
            # Prima prova natural_query (NL → strutturata)
            result = bridge.natural_query(query)

            if "error" in result:
                # Fallback: stats generiche
                stats = bridge.get_stats()
                if "error" not in stats:
                    result = {
                        "query_type": "stats",
                        "stats": stats,
                        "note": "NL query fallita, mostrate statistiche generiche",
                    }
                else:
                    return DomainResult(
                        domain=Domain.GRAPH, status="error",
                        error=result["error"],
                        duration_sec=time.time() - t0,
                    )

            # Formatta risposta leggibile
            content = self._format_graph_result(query, result)

            return DomainResult(
                domain=Domain.GRAPH, status="success",
                content=content, data=result,
                duration_sec=time.time() - t0,
            )
        except Exception as e:
            logger.error("Graph exec error: %s", e)
            return DomainResult(
                domain=Domain.GRAPH, status="error",
                error=str(e), duration_sec=time.time() - t0,
            )

    def _exec_coding(self, query: str, context: str = "") -> DomainResult:
        """Esegue una richiesta sul dominio CODING (Oracle)."""
        t0 = time.time()
        proto = self._get_oracle_protocol()
        if proto:
            try:
                result = proto.analyze(task=query, context=context, tier="auto")
                return DomainResult(
                    domain=Domain.CODING, status="success",
                    content=result.response,
                    data=result.to_dict() if hasattr(result, "to_dict") else {},
                    duration_sec=time.time() - t0,
                )
            except Exception as e:
                logger.error("OracleProtocol coding error: %s", e)
                return DomainResult(
                    domain=Domain.CODING, status="error",
                    error=str(e), duration_sec=time.time() - t0,
                )

        # Fallback: risposta base
        return DomainResult(
            domain=Domain.CODING, status="success",
            content=f"Richiesta coding ricevuta. "
                    f"OracleProtocol non disponibile.\n\n{query}",
            duration_sec=time.time() - t0,
        )

    def _exec_osint(self, query: str, context: str = "") -> DomainResult:
        """Esegue una richiesta sul dominio OSINT (CHORA)."""
        t0 = time.time()
        proto = self._get_oracle_protocol()
        if proto:
            try:
                result = proto.analyze(task=query, context=context, tier="complex")
                return DomainResult(
                    domain=Domain.OSINT, status="success",
                    content=result.response,
                    data=result.to_dict() if hasattr(result, "to_dict") else {},
                    duration_sec=time.time() - t0,
                )
            except Exception as e:
                logger.error("OracleProtocol OSINT error: %s", e)
                return DomainResult(
                    domain=Domain.OSINT, status="error",
                    error=str(e), duration_sec=time.time() - t0,
                )

        return DomainResult(
            domain=Domain.OSINT, status="success",
            content=f"Richiesta OSINT ricevuta. OracleProtocol non disponibile.\n\n{query}",
            duration_sec=time.time() - t0,
        )

    def _exec_identity(self, query: str) -> DomainResult:
        """Esegue una richiesta sul dominio IDENTITY."""
        t0 = time.time()
        if not self.config.identity_enabled:
            return DomainResult(
                domain=Domain.IDENTITY, status="skipped",
                content="Identity module disabilitato",
                duration_sec=time.time() - t0,
            )

        # Delega a OracleProtocol per task identitari
        proto = self._get_oracle_protocol()
        if proto:
            try:
                result = proto.analyze(task=query, tier="complex")
                return DomainResult(
                    domain=Domain.IDENTITY, status="success",
                    content=result.response,
                    data=result.to_dict() if hasattr(result, "to_dict") else {},
                    duration_sec=time.time() - t0,
                )
            except Exception as e:
                logger.error("Identity exec error: %s", e)

        return DomainResult(
            domain=Domain.IDENTITY, status="success",
            content=f"Richiesta identità ricevuta.\n\n{query}",
            duration_sec=time.time() - t0,
        )

    def _exec_general(self, query: str) -> DomainResult:
        """Esegue una richiesta sul dominio GENERAL."""
        t0 = time.time()
        proto = self._get_oracle_protocol()
        if proto:
            try:
                result = proto.analyze(task=query, tier="simple")
                return DomainResult(
                    domain=Domain.GENERAL, status="success",
                    content=result.response,
                    data=result.to_dict() if hasattr(result, "to_dict") else {},
                    duration_sec=time.time() - t0,
                )
            except Exception as e:
                logger.error("General exec error: %s", e)

        return DomainResult(
            domain=Domain.GENERAL, status="success",
            content=f"Oracle Orchestrator attivo. Come posso aiutarti?\n\n"
                    f"Domini disponibili: graph, coding, osint, identity\n\n"
                    f"La tua richiesta: {query}",
            duration_sec=time.time() - t0,
        )

    def _exec_hybrid(self, query: str, context: str = "") -> DomainResult:
        """Esegue una richiesta multi-dominio (HYBRID).

        Usa l'OracleProtocol per coordinare più domini.
        """
        t0 = time.time()
        proto = self._get_oracle_protocol()
        if proto:
            try:
                # Arricchisci contesto con dati del grafo se pertinente
                enriched_context = context
                bridge = self._get_bridge()
                if bridge and bridge.is_available():
                    try:
                        stats = bridge.get_stats()
                        if "error" not in stats:
                            graph_context = (
                                f"[Context: Grafo Penelope]\n"
                                f"Foto totali: {stats.get('total_photos', '?')}\n"
                                f"Persone: {stats.get('total_persons', '?')}\n"
                                f"Foto con volti: {stats.get('photos_with_faces', '?')}\n"
                            )
                            enriched_context = (context + "\n" + graph_context) if context else graph_context
                    except Exception:
                        pass

                result = proto.analyze(task=query, context=enriched_context, tier="complex")
                return DomainResult(
                    domain=Domain.HYBRID, status="success",
                    content=result.response,
                    data=result.to_dict() if hasattr(result, "to_dict") else {},
                    duration_sec=time.time() - t0,
                )
            except Exception as e:
                logger.error("Hybrid exec error: %s", e)

        return DomainResult(
            domain=Domain.HYBRID, status="success",
            content=f"Richiesta ibrida ricevuta. OracleProtocol non disponibile.\n\n{query}",
            duration_sec=time.time() - t0,
        )

    # ── Result formatter ───────────────────────────────────────

    def _format_graph_result(self, query: str, result: dict) -> str:
        """Formatta il risultato del grafo in testo leggibile."""
        qtype = result.get("query_type", "unknown")
        lines = []

        if qtype == "stats":
            stats = result.get("stats", {})
            lines.append("## 📊 Statistiche del Grafo Penelope\n")
            lines.append(f"- **Foto totali:** {stats.get('total_photos', 'N/A')}")
            lines.append(f"- **Persone:** {stats.get('total_persons', 'N/A')}")
            lines.append(f"- **Foto con volti:** {stats.get('photos_with_faces', 'N/A')}")
            if "nodes_by_type" in result:
                lines.append("\n### Nodi per tipo:")
                for t, c in result["nodes_by_type"].items():
                    lines.append(f"- {t}: {c}")

        elif qtype == "person_photos":
            person = result.get("person", "?")
            photos = result.get("photos", [])
            lines.append(f"## 📸 Foto di **{person}**\n")
            lines.append(f"Trovate **{result.get('count', 0)}** foto.\n")
            for p in photos[:20]:
                path = p.get("path", p.get("label", ""))
                lines.append(f"- `{path}`")
            if len(photos) > 20:
                lines.append(f"\n... e altre {len(photos) - 20} foto.")

        elif qtype == "persons":
            persons = result.get("persons", [])
            lines.append(f"## 👤 Persone nel Grafo\n")
            lines.append(f"Totale: **{result.get('count', 0)}**\n")
            for p in persons[:30]:
                label = p.get("label", "?")
                meta = p.get("metadata", "")
                source = " [InsightFace]" if "insightface" in str(meta) else " [YOLO]" if "face_detection" in str(meta) else ""
                lines.append(f"- {label}{source}")

        elif qtype == "events":
            events = result.get("events", [])
            lines.append(f"## 📅 Eventi\n")
            lines.append(f"Totale: **{result.get('count', 0)}**\n")
            for e in events[:20]:
                label = e.get("label", "?")
                created = str(e.get("created_at", ""))[:10]
                lines.append(f"- {created}: {label}")

        elif qtype == "locations":
            locations = result.get("locations", [])
            lines.append(f"## 📍 Luoghi\n")
            lines.append(f"Totale: **{result.get('count', 0)}**\n")
            for l in locations[:20]:
                label = l.get("label", "?")
                lines.append(f"- {label}")

        elif qtype == "search":
            nodes = result.get("nodes", [])
            lines.append(f"## 🔍 Risultati Ricerca\n")
            lines.append(f"Trovati **{result.get('count', 0)}** nodi.\n")
            for n in nodes[:20]:
                ntype = n.get("type", "?")
                label = n.get("label", "?")
                lines.append(f"- [{ntype}] {label}")

        else:
            lines.append(f"### Risultato query grafo\n")
            lines.append(json.dumps(result, indent=2, ensure_ascii=False))

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  ORACLE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

class OracleOrchestrator:
    """
    Engine principale di Oracle. Unico punto d'ingresso per tutte le richieste.

    Uso:
        orch = OracleOrchestrator()
        result = orch.analyze("trovami le foto di Angela")
        print(result.response)
        
        # Con contesto
        result = orch.analyze(
            "verifica questa email",
            context="email sospetta ricevuta oggi",
        )
        
        # Forza domini specifici
        result = orch.analyze(
            "quante foto ci sono?",
            force_domains=["graph"],
        )
    """

    def __init__(self, config: Optional[OrchestratorConfig] = None):
        self.config = config or OrchestratorConfig()
        self.router = DomainRouter(self.config)

        # Stats globali
        self.total_runs = 0
        self.domain_counts: dict[str, int] = {}

    def analyze(
        self,
        query: str,
        context: str = "",
        force_domains: Optional[list[str]] = None,
    ) -> OrchestratorResult:
        """
        Punto d'ingresso principale.

        Args:
            query: Richiesta in linguaggio naturale.
            context: Contesto aggiuntivo (opzionale).
            force_domains: Forza uno o più domini (es. ["graph", "osint"]).

        Returns:
            OrchestratorResult con risposta unificata.
        """
        self.total_runs += 1
        monitor = OracleMonitor()
        monitor.start()
        loop = OracleLoop(self.config, monitor)

        # ── 1. EGIDA CHECK (input) ─────────────────────────────
        monitor.log_phase("egida", "check input")
        egida_blocked, egida_msg = self._egida_check_input(query)
        monitor.metrics["egida_checks"] += 1
        if egida_blocked:
            monitor.metrics["egida_blocks"] += 1
            return OrchestratorResult(
                query=query,
                response=f"🛡️ **Richiesta bloccata da Egida (HSD)**\n\n{egida_msg}",
                egida_blocked=True,
                egida_message=egida_msg,
                stats=monitor.end(),
            )

        # ── 2. DOMAIN ROUTING ──────────────────────────────────
        if force_domains:
            domains = [Domain(d) for d in force_domains]
        else:
            domains = self.router.route(query, context)

        monitor.log_phase("routing", f"domini: {[d.value for d in domains]}")
        for d in domains:
            self.domain_counts[d.value] = self.domain_counts.get(d.value, 0) + 1
        monitor.metrics["domains_detected"] = [d.value for d in domains]

        # ── 3. ESEGUI DOMINI ───────────────────────────────────
        domain_results: list[DomainResult] = []
        response_parts = []

        for domain in domains:
            monitor.log_phase("exec", domain.value)

            if domain == Domain.GRAPH:
                result = loop._exec_graph(query)
            elif domain == Domain.CODING:
                result = loop._exec_coding(query, context)
            elif domain == Domain.OSINT:
                result = loop._exec_osint(query, context)
            elif domain == Domain.IDENTITY:
                result = loop._exec_identity(query)
            elif domain == Domain.HYBRID:
                result = loop._exec_hybrid(query, context)
            else:  # GENERAL
                result = loop._exec_general(query)

            domain_results.append(result)
            if result.status == "success" and result.content:
                response_parts.append(result.content)
            elif result.status == "error":
                response_parts.append(f"⚠️ **{domain.value}**: {result.error}")

        # ── 4. COMPONI RISPOSTA ────────────────────────────────
        if domains == [Domain.GENERAL] and domain_results:
            response = domain_results[0].content if domain_results[0].content else query
        elif len(domain_results) == 1:
            response = domain_results[0].content if domain_results[0].content else query
        else:
            # Multi-dominio: unisci con header
            parts = []
            for dr in domain_results:
                if dr.status == "success" and dr.content:
                    domain_icon = {
                        Domain.GRAPH: "📊",
                        Domain.CODING: "💻",
                        Domain.OSINT: "🔍",
                        Domain.IDENTITY: "🧠",
                        Domain.HYBRID: "🌀",
                        Domain.GENERAL: "💬",
                    }.get(dr.domain, "📌")
                    parts.append(f"---\n{domain_icon} **{dr.domain.value.upper()}**\n\n{dr.content}")
            response = "\n\n".join(parts)

        # ── 5. EGIDA CHECK (output) ────────────────────────────
        if HAS_EGIDA:
            monitor.log_phase("egida", "check output")
            sanitized = self._egida_sanitize_output(response)
            if sanitized != response:
                monitor.metrics["egida_blocks"] += 1
                response = sanitized

        # ── 6. RESULT ──────────────────────────────────────────
        return OrchestratorResult(
            query=query,
            domains=domains,
            domain_results=domain_results,
            response=response,
            stats=monitor.end(),
        )

    # ── Egida hooks ────────────────────────────────────────────

    def _egida_check_input(self, text: str) -> tuple[bool, str]:
        """Verifica input con Egida HSD filter."""
        if not HAS_EGIDA:
            return False, ""

        try:
            filter_inst = HSDFilter()
            result = filter_inst.check_text(text)
            if result.get("blocked", False):
                score = result.get("score", 0)
                matches = result.get("matches", [])
                match_summary = ", ".join([m.get("type", "?") for m in matches[:5]])
                return True, (
                    f"HSD rilevato (score={score}). "
                    f"Match: {match_summary}. "
                    f"La richiesta contiene dati sensibili e non può essere elaborata."
                )
            return False, ""
        except Exception as e:
            logger.warning("Egida check input error: %s", e)
            return False, ""

    def _egida_sanitize_output(self, text: str) -> str:
        """Sanitizza output con Egida."""
        if not HAS_EGIDA:
            return text
        try:
            filter_inst = HSDFilter()
            result = filter_inst.check_text(text)
            if result.get("blocked", False):
                return (
                    "[RISPOSTA BLOCCATA DA EGIDA — HSD RILEVATO IN OUTPUT]\n\n"
                    "La risposta conteneva dati sensibili ed è stata rimossa.\n"
                    "Riformula la richiesta in modo più generico."
                )
            return text
        except Exception:
            return text

    def get_stats(self) -> dict:
        """Statistiche globali dell'orchestratore."""
        return {
            "total_runs": self.total_runs,
            "domain_distribution": dict(self.domain_counts),
            "config": {
                "penelope_mode": self.config.penelope_mode,
                "use_oracle_protocol": self.config.use_oracle_protocol,
                "identity_enabled": self.config.identity_enabled,
                "archimede_api_url": self.config.archimede_api_url,
            },
            "egida_available": HAS_EGIDA,
        }


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def cmd_analyze(args):
    """Esegue una query attraverso l'orchestratore."""
    query = args.query or ""
    if not query and not sys.stdin.isatty():
        query = sys.stdin.read().strip()
    if not query:
        print("ERRORE: fornisci query come argomento o via pipe")
        sys.exit(1)

    config = OrchestratorConfig(
        max_refinements=args.refinements,
        use_oracle_protocol=not args.no_oracle,
    )
    orch = OracleOrchestrator(config)
    force = args.domains.split(",") if args.domains else None
    result = orch.analyze(query, context=args.context or "", force_domains=force)

    if args.json:
        output = {
            "query": result.query,
            "domains": [d.value for d in result.domains],
            "response": result.response,
            "stats": result.stats,
            "egida_blocked": result.egida_blocked,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print()
        print("=" * 60)
        domain_str = ", ".join(d.value for d in result.domains)
        print(f"  [ORACLE] Domini: {domain_str}")
        dur = result.stats.get("total_duration_sec", 0)
        print(f"  [ORACLE] Durata: {dur:.2f}s")
        if result.egida_blocked:
            print(f"  [🛡️ EGIDA] {result.egida_message}")
        print("=" * 60)
        print()
        print(result.response)
        print()


def cmd_status(args):
    """Mostra stato dell'orchestratore."""
    orch = OracleOrchestrator()
    stats = orch.get_stats()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print()
        print("=" * 50)
        print("  ORACLE ORCHESTRATOR — Status")
        print("=" * 50)
        print(f"  Total runs:           {stats['total_runs']}")
        print(f"  Domain distribution:")
        for d, c in stats['domain_distribution'].items():
            print(f"    {d}: {c}")
        print(f"  Config:")
        for k, v in stats['config'].items():
            print(f"    {k}: {v}")
        egida = "✅ Disponibile" if stats['egida_available'] else "❌ Non disponibile"
        print(f"  Egida (HSD):          {egida}")
        print()


def main():
    p = argparse.ArgumentParser(
        description="Oracle Orchestrator — Unico punto d'ingresso per tutti i layer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Esempi:\n"
            "  %(prog)s analyze \"quante foto ci sono nel grafo?\"\n"
            "  %(prog)s analyze \"trovami foto di Angela\" --json\n"
            "  %(prog)s analyze \"verifica questa email\" --domains osint\n"
            "  %(prog)s analyze \"cerca foto e verificane l'autenticità\" --domains graph,osint\n"
            "  cat query.txt | %(prog)s analyze\n"
            "  %(prog)s status\n"
        ),
    )
    sp = p.add_subparsers(dest="cmd")

    pa = sp.add_parser("analyze", help="Analizza una richiesta su tutti i layer")
    pa.add_argument("query", nargs="?", default="", help="Richiesta in linguaggio naturale")
    pa.add_argument("--context", "-c", default="", help="Contesto aggiuntivo")
    pa.add_argument("--domains", "-d", default=None,
                    help="Forza domini (es. 'graph,osint' o 'coding')")
    pa.add_argument("--refinements", "-r", type=int, default=2,
                    help="Max refinement loop (default: 2)")
    pa.add_argument("--no-oracle", action="store_true",
                    help="Disabilita OracleProtocol (solo graph/static)")
    pa.add_argument("--json", action="store_true", help="Output JSON")

    ps = sp.add_parser("status", help="Mostra stato dell'orchestratore")
    ps.add_argument("--json", action="store_true", help="Output JSON")

    args = p.parse_args()
    if args.cmd == "analyze":
        cmd_analyze(args)
    elif args.cmd == "status":
        cmd_status(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()

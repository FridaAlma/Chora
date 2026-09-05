#!/usr/bin/env python3
"""
Oracle MCP Server
=================
Model Context Protocol (MCP) server that exposes all 13 Oracle tools
as standard MCP tools. Compatible with any MCP client:
Claude Code, Codex, OpenCode, Cursor, etc.

Protocol: JSON-RPC 2.0 over stdio (standard MCP transport for coding agents)

Each tool is executed as an isolated subprocess, keeping the original
tool code untouched and providing clean environment isolation.

Usage:
    python mcp_server.py                          # Stdio mode (default, for agents)
    python mcp_server.py --http --port 8100        # HTTP/SSE mode (web UI)
    python mcp_server.py --list                    # List all tools
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

# ── Path setup ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BASE_DIR / "tools"
PYTHON = sys.executable

# ── MCP Protocol Constants ──────────────────────────────────────
JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"

# ── Tool Metadata ────────────────────────────────────────────────
_TOOL_META: Dict[str, dict] = {
    "web_access": {
        "description": "Accesso Internet robusto e sicuro (GET/POST/download/scrape). "
                       "Comandi: get, post, download, scrape, scrape-links, parallel, cache",
    },
    "vector_memory": {
        "description": "Memoria vettoriale basata su ChromaDB con supporto multimodale (immagini, audio). "
                       "Comandi: add, search, get, delete, list-collections, delete-collection, count, info, set-policy",
    },
    "mcts_engine": {
        "description": "Monte Carlo Tree Search per decision-making complesso. "
                       "Comandi: analyze (ciclo completo), branches (solo generazione rami)",
    },
    "wiki_tool": {
        "description": "Gestione wiki personale via API HTTP (192.168.1.5:8000). "
                       "Comandi: list, read, write, upload, search, exists, delete, batch",
    },
    "gmail_client": {
        "description": "Gestione completa posta Gmail (auth, invio, ricerca, lettura, cestino). "
                       "Comandi: auth, send, search, read, list, trash, config",
    },
    "immunity_guardian": {
        "description": "ImmunityGuardian — Modulo di sicurezza runtime: rileva injection, leak e jailbreak. "
                       "Comandi: check, sanitize, stats, session",
    },
    "interleaved_sandbox": {
        "description": "Interleaved Sandbox — Esecuzione codice e verifica in ambiente sandbox isolato. "
                       "Comandi: run, verify",
    },
    "constitution": {
        "description": "Constitution — Protocollo di autolimitazione e governance Oracle. "
                       "Comandi: check, pending, approve, reject, confirm",
    },
    "environment_probe": {
        "description": "EnvironmentProbe — Verifica pre-flight di connettività porte, dipendenze Python, "
                       "permessi filesystem e variabili d'ambiente. "
                       "Comandi: port, dep, fs, env, check",
    },
    "multimodal_encoder": {
        "description": "Multimodal Encoder — Encoding immagini basato su CLIP. "
                       "Comandi: encode",
    },
    "semantic_context_filter": {
        "description": "Semantic Context Filter — Previene context drift in sessioni lunghe. "
                       "Comandi: filter, train, stats",
    },
    "oracle_orchestrator": {
        "description": "Oracle Orchestrator — Orchestrazione multi-dominio (Penelope, Archimede, identità). "
                       "Comandi: query, route, status",
    },
    "oracle_protocol": {
        "description": "Oracle Protocol Orchestrator — Integra MCTS Engine + Interleaved Sandbox + critica. "
                       "Comandi: analyze, verify, report",
    },
    "chunk_filter": {
        "description": "ChunkFilter — Modello-figlio per filtraggio chunk contesto (SPERIMENTALE). "
                       "Comandi: filter, train, stats",
    },
}

_TOOL_NAMES = sorted(_TOOL_META.keys())

# ── Tool Schema Builders ────────────────────────────────────────
# These define the MCP input schemas for each tool.
# We provide detailed schemas for the most-used tools and
# generic ones for the rest.

def _build_web_access_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Operazione web: get, post, download, scrape, scrape-links, parallel, cache",
                "enum": ["get", "post", "download", "scrape", "scrape-links", "parallel", "cache"],
            },
            "url": {
                "type": "string",
                "description": "URL da contattare (per get/post/download/scrape/scrape-links)",
            },
            "urls": {
                "type": "string",
                "description": "URL multipli separati da spazio (per parallel)",
            },
            "data": {
                "type": "string",
                "description": "Dati form-encoded per POST",
            },
            "json_data": {
                "type": "string",
                "description": "Dati JSON per POST (es. '{\"key\":\"val\"}')",
            },
            "params": {
                "type": "string",
                "description": "Query params JSON (es. '{\"key\":\"val\"}')",
            },
            "headers": {
                "type": "string",
                "description": "Headers extra in formato JSON",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in secondi",
            },
            "output": {
                "type": "string",
                "description": "Percorso output per download",
            },
            "selector": {
                "type": "string",
                "description": "Selettore CSS per scrape",
            },
            "extract": {
                "type": "string",
                "description": "Cosa estrarre: text, html, attr:name",
                "default": "text",
            },
            "json": {
                "type": "boolean",
                "description": "Output JSON strutturato",
                "default": False,
            },
            "pretty": {
                "type": "boolean",
                "description": "Output JSON formattato (indentato)",
                "default": False,
            },
            "no_cache": {
                "type": "boolean",
                "description": "Ignora cache",
                "default": False,
            },
            "raw": {
                "type": "boolean",
                "description": "Mostra solo il corpo (no metadata)",
                "default": False,
            },
            "truncate": {
                "type": "integer",
                "description": "Tronca output a N caratteri (0=nessun limite)",
                "default": 2000,
            },
            "cache_action": {
                "type": "string",
                "description": "Azione cache: stats o clear",
                "enum": ["stats", "clear"],
            },
            "progress": {
                "type": "boolean",
                "description": "Mostra barra di progresso per download",
                "default": False,
            },
        },
        "required": ["command"],
        "description": _TOOL_META["web_access"]["description"],
    }


def _build_vector_memory_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Operazione: add, search, get, delete, list-collections, delete-collection, count, info, set-policy, image-stats, image-cleanup",
                "enum": ["add", "search", "get", "delete", "list-collections",
                         "delete-collection", "count", "info", "image-stats",
                         "image-cleanup", "set-policy"],
            },
            "collection": {
                "type": "string",
                "description": "Nome collezione (per add/search/get/delete/count/set-policy)",
            },
            "id": {
                "type": "string",
                "description": "ID documento (per add/get/delete)",
            },
            "query": {
                "type": "string",
                "description": "Testo della query (per search)",
            },
            "text": {
                "type": "string",
                "description": "Contenuto testuale (per add)",
            },
            "image": {
                "type": "string",
                "description": "Percorso immagine (per add in modalità immagine)",
            },
            "metadata": {
                "type": "string",
                "description": "Metadati JSON (es. '{\"k\":\"v\"}')",
            },
            "top_k": {
                "type": "integer",
                "description": "Numero risultati (per search)",
                "default": 5,
            },
            "threshold": {
                "type": "number",
                "description": "Soglia distanza massima (per search)",
            },
            "dedup_threshold": {
                "type": "number",
                "description": "Soglia deduplicazione 0.0-1.0 (per add)",
            },
            "max_docs": {
                "type": "integer",
                "description": "Numero massimo documenti nella collezione",
            },
            "ttl_days": {
                "type": "integer",
                "description": "Giorni di retention massimi per documento",
            },
            "name": {
                "type": "string",
                "description": "Nome collezione (per delete-collection/set-policy)",
            },
            "no_text": {
                "type": "boolean",
                "description": "Escludi testo nei risultati search",
                "default": False,
            },
            "no_metadata": {
                "type": "boolean",
                "description": "Escludi metadati nei risultati search",
                "default": False,
            },
            "raw": {
                "type": "boolean",
                "description": "Output JSON raw",
                "default": False,
            },
            "force": {
                "type": "boolean",
                "description": "Forza pulizia inclusi file orfani (per image-cleanup)",
                "default": False,
            },
            "no_keep_files": {
                "type": "boolean",
                "description": "Cancella file originale dopo encoding (per add immagine)",
                "default": False,
            },
            "set_policy": {
                "type": "boolean",
                "description": "Salva max-docs/ttl-days/dedup-threshold come policy permanente collezione",
                "default": False,
            },
        },
        "required": ["command"],
        "description": _TOOL_META["vector_memory"]["description"],
    }


def _build_mcts_engine_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Operazione: analyze (ciclo completo) o branches (solo generazione)",
                "enum": ["analyze", "branches"],
            },
            "task": {
                "type": "string",
                "description": "Task da analizzare (testo del problema)",
            },
            "context": {
                "type": "string",
                "description": "Contesto aggiuntivo per l'analisi",
            },
            "branches": {
                "type": "integer",
                "description": "Numero rami da generare (default: 4)",
                "default": 4,
            },
            "depth": {
                "type": "integer",
                "description": "Profondità rollout (default: 3)",
                "default": 3,
            },
            "threshold": {
                "type": "number",
                "description": "Soglia pruning (default: 0.30)",
                "default": 0.30,
            },
            "rollout_branches": {
                "type": "integer",
                "description": "Rami da rolloutare (default: 2)",
                "default": 2,
            },
            "no_pro": {
                "type": "boolean",
                "description": "Usa Flash invece di Pro per rollout",
                "default": False,
            },
            "json": {
                "type": "boolean",
                "description": "Output in formato JSON",
                "default": False,
            },
            "count": {
                "type": "integer",
                "description": "Numero rami per comando branches (default: 4)",
                "default": 4,
            },
        },
        "required": ["command"],
        "description": _TOOL_META["mcts_engine"]["description"],
    }


def _build_wiki_tool_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Operazione: list, read, write, upload, search, exists, delete, batch",
                "enum": ["list", "read", "write", "upload", "search", "exists", "delete", "batch"],
            },
            "name": {
                "type": "string",
                "description": "Nome della pagina wiki",
            },
            "content": {
                "type": "string",
                "description": "Contenuto HTML (per write)",
            },
            "file": {
                "type": "string",
                "description": "Percorso file HTML (per write o batch)",
            },
            "filepath": {
                "type": "string",
                "description": "Percorso file immagine (per upload)",
            },
            "keyword": {
                "type": "string",
                "description": "Keyword da cercare (per search)",
            },
            "url": {
                "type": "string",
                "description": "URL base server wiki (sovrascrive default)",
            },
            "json": {
                "type": "boolean",
                "description": "Output JSON",
                "default": False,
            },
            "quiet": {
                "type": "boolean",
                "description": "Output minimo",
                "default": False,
            },
            "action": {
                "type": "string",
                "description": "Azione batch: create_home, create_from_file",
                "enum": ["create_home", "create_from_file"],
            },
            "image": {
                "type": "string",
                "description": "Carica immagine (per batch)",
            },
        },
        "required": ["command"],
        "description": _TOOL_META["wiki_tool"]["description"],
    }


def _build_gmail_client_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Operazione: auth, send, search, read, list, trash, config",
                "enum": ["auth", "send", "search", "read", "list", "trash", "config"],
            },
            "to": {
                "type": "string",
                "description": "Destinatario email (per send)",
            },
            "subject": {
                "type": "string",
                "description": "Oggetto email (per send)",
            },
            "body": {
                "type": "string",
                "description": "Corpo email (per send)",
            },
            "cc": {
                "type": "string",
                "description": "CC (per send)",
            },
            "bcc": {
                "type": "string",
                "description": "BCC (per send)",
            },
            "query": {
                "type": "string",
                "description": "Query Gmail (per search)",
            },
            "max_results": {
                "type": "integer",
                "description": "Numero massimo risultati",
                "default": 10,
            },
            "message_id": {
                "type": "string",
                "description": "ID messaggio Gmail (per read/trash)",
            },
            "label": {
                "type": "string",
                "description": "Etichetta Gmail (per list, default: INBOX)",
            },
            "include_spam_trash": {
                "type": "boolean",
                "description": "Includi spam e cestino nei risultati search",
                "default": False,
            },
            "json": {
                "type": "boolean",
                "description": "Output JSON",
                "default": False,
            },
        },
        "required": ["command"],
        "description": _TOOL_META["gmail_client"]["description"],
    }


def _build_generic_schema(name: str) -> dict:
    """Build a generic schema for tools where we provide the command enum."""
    # Map known commands for each tool
    cmd_map = {
        "immunity_guardian": ["check", "sanitize", "stats", "session"],
        "interleaved_sandbox": ["run", "verify"],
        "constitution": ["check", "pending", "approve", "reject", "confirm"],
        "environment_probe": ["port", "dep", "fs", "env", "check"],
        "multimodal_encoder": ["encode"],
        "semantic_context_filter": ["filter", "train", "stats"],
        "oracle_orchestrator": ["query", "route", "status"],
        "oracle_protocol": ["analyze", "verify", "report"],
        "chunk_filter": ["filter", "train", "stats"],
    }
    commands = cmd_map.get(name, [])
    return {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": f"Comando: {', '.join(commands)}",
                "enum": commands,
            },
        },
        "required": ["command"],
        "description": _TOOL_META.get(name, {}).get("description", f"Oracle tool: {name}"),
    }


# ── Tool Execution via Subprocess ───────────────────────────────
# All tools are executed as isolated subprocesses.
# This ensures:
# - Zero modification to tool code
# - Clean environment isolation
# - Natural stdout/stderr capture
# - No import conflicts

# Some tool arguments are POSITIONAL (not --flags).
# We define mappings: (tool_name, subcommand) -> [positional_arg_names]
_POSITIONAL_ARGS: Dict[str, Dict[str, List[str]]] = {
    "web_access": {
        "get": ["url"],
        "post": ["url"],
        "download": ["url"],
        "scrape": ["url"],
        "scrape-links": ["url"],
        "parallel": ["urls"],
    },
    "wiki_tool": {
        "read": ["name"],
        "delete": ["name"],
        "exists": ["name"],
        "search": ["keyword"],
        "upload": ["filepath"],
        "write": ["name"],
    },
    "gmail_client": {
        "read": ["message_id"],
        "trash": ["message_id"],
    },
    "multimodal_encoder": {
        "encode": ["image"],
    },
    "mcts_engine": {
        # all args are --flags, no positional
    },
    "vector_memory": {
        # all args are --flags, no positional
    },
}

# Some flags are GLOBAL (parser-level, before subcommand) for certain tools.
# These must appear BEFORE the subcommand in the CLI.
_GLOBAL_FLAGS: Dict[str, List[str]] = {
    "wiki_tool": ["json", "quiet", "url"],
    "gmail_client": ["json"],
}


def _dict_to_tool_args(
    tool_name: str, command: str, args_dict: dict
) -> tuple[List[str], List[str]]:
    """Convert a dict of arguments to CLI argument list, separating global
    (parser-level) flags from subcommand-specific args.

    Returns:
        (global_flags, subcommand_args)
        - global_flags come BEFORE the subcommand name on the CLI
        - subcommand_args come AFTER the subcommand name
    """
    global_flag_names = _GLOBAL_FLAGS.get(tool_name, [])
    positional_names = _POSITIONAL_ARGS.get(tool_name, {}).get(command, [])

    global_flags: List[str] = []
    positional_values: List[str] = []
    subcommand_flags: List[str] = []

    for key, value in sorted(args_dict.items()):
        if value is None:
            continue

        # Skip empty strings
        if isinstance(value, str) and not value:
            continue

        # Positional arguments (no -- flag)
        if key in positional_names:
            positional_values.append(str(value))
            continue

        # Convert key to CLI flag
        flag = key.replace("_", "-")

        # Global flags (parser-level, before subcommand)
        if key in global_flag_names:
            if isinstance(value, bool):
                if value:
                    global_flags.append(f"--{flag}")
            else:
                global_flags.append(f"--{flag}")
                global_flags.append(str(value))
            continue

        # Boolean flag (subcommand-specific, after subcommand)
        if isinstance(value, bool):
            if value:
                subcommand_flags.append(f"--{flag}")
            continue

        # Named argument with value (subcommand-specific)
        subcommand_flags.append(f"--{flag}")
        subcommand_flags.append(str(value))

    return global_flags, positional_values + subcommand_flags


def _run_tool(tool_name: str, args_dict: dict, timeout: int = 300) -> dict:
    """Execute a tool as a subprocess and return MCP result.

    Args:
        tool_name: The tool filename (without .py)
        args_dict: Dict of arguments to pass (may include 'command')
        timeout: Max execution time in seconds

    Returns:
        MCP content result dict
    """
    tool_path = TOOLS_DIR / f"{tool_name}.py"
    if not tool_path.exists():
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Tool '{tool_name}' not found at {tool_path}"}],
        }

    # Use the patched runner for Python 3.9 compatibility
    runner_path = BASE_DIR / "_tool_runner.py"
    cmd = [str(PYTHON), str(runner_path), tool_name]

    # Separate global flags from subcommand-specific args
    command_value = args_dict.pop("command", None) if "command" in args_dict else None
    global_flags, subcommand_args = _dict_to_tool_args(
        tool_name, command_value or "", args_dict
    )

    # Build command: [proc, runner, tool, global_flags..., command, subcommand_args...]
    cmd.extend(global_flags)
    if command_value:
        cmd.append(command_value)
    cmd.extend(subcommand_args)

    # Execute
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BASE_DIR),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        output = ""
        if proc.stdout:
            output += proc.stdout
        if proc.stderr:
            # Filter out common warnings
            stderr_lines = [
                line for line in proc.stderr.split("\n")
                if line.strip()
                and "UserWarning" not in line
                and "NotOpenSSLWarning" not in line
                and "Failed to initialize NumPy" not in line
            ]
            if stderr_lines:
                output += "\n[stderr]\n" + "\n".join(stderr_lines)

        if proc.returncode != 0 and not output:
            output = f"(exit code: {proc.returncode})"

        return {
            "content": [{"type": "text", "text": output.strip() or "(no output)"}],
            "isError": proc.returncode != 0,
        }

    except subprocess.TimeoutExpired:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Tool '{tool_name}' timed out after {timeout}s"}],
        }
    except FileNotFoundError as e:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Python interpreter not found: {e}"}],
        }
    except Exception as e:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Error executing {tool_name}: {type(e).__name__}: {e}"}],
        }


# ── MCP Tool Definitions ────────────────────────────────────────

def get_mcp_tools() -> List[dict]:
    """Return all MCP tool definitions with their input schemas."""
    schema_builders: Dict[str, Callable[[], dict]] = {
        "web_access": _build_web_access_schema,
        "vector_memory": _build_vector_memory_schema,
        "mcts_engine": _build_mcts_engine_schema,
        "wiki_tool": _build_wiki_tool_schema,
        "gmail_client": _build_gmail_client_schema,
    }

    tools = []
    for name in _TOOL_NAMES:
        builder = schema_builders.get(name)
        if builder:
            schema = builder()
        else:
            schema = _build_generic_schema(name)

        meta = _TOOL_META.get(name, {})
        tools.append({
            "name": name,
            "description": schema.get("description", meta.get("description", f"Oracle tool: {name}")),
            "inputSchema": schema,
        })
    return tools


def handle_tool_call(name: str, arguments: dict) -> dict:
    """Handle an MCP tools/call request by executing the tool as subprocess."""
    if name not in _TOOL_META:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Unknown tool: {name}. Available: {', '.join(_TOOL_NAMES)}"}],
        }
    return _run_tool(name, arguments)


# ════════════════════════════════════════════════════════════════
#  MCP Transport: stdio (JSON-RPC 2.0)
# ════════════════════════════════════════════════════════════════

def _read_stdio_request() -> Optional[dict]:
    """Read a JSON-RPC request from stdin (line-delimited JSON)."""
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _write_stdio_response(response: dict):
    """Write a JSON-RPC response to stdout (one line of JSON)."""
    json_str = json.dumps(response, ensure_ascii=False)
    sys.stdout.write(json_str + "\n")
    sys.stdout.flush()


def _make_error(code: int, message: str, request_id: Optional[Any] = None) -> dict:
    """Create a JSON-RPC error response."""
    resp = {
        "jsonrpc": JSONRPC_VERSION,
        "error": {"code": code, "message": message},
    }
    resp["id"] = request_id
    return resp


def _make_success(result: Any, request_id: Any) -> dict:
    """Create a JSON-RPC success response."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "result": result,
    }


def _handle_mcp_request(request: dict) -> Optional[dict]:
    """Handle a single MCP JSON-RPC request."""
    req_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "initialize":
        return _make_success({
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {},
            },
            "serverInfo": {
                "name": "oracle-mcp",
                "version": "1.0.0",
            },
        }, req_id)

    elif method == "notifications/initialized":
        return None  # Notifications: no response

    elif method == "tools/list":
        return _make_success({"tools": get_mcp_tools()}, req_id)

    elif method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = handle_tool_call(name, arguments)
        return _make_success(result, req_id)

    elif method == "resources/list":
        return _make_success({"resources": []}, req_id)

    elif method == "prompts/list":
        return _make_success({"prompts": []}, req_id)

    else:
        return _make_error(-32601, f"Method not found: {method}", req_id)


def run_stdio_server():
    """Run MCP server over stdio.

    Compatible with: Claude Code, Codex, OpenCode, Cursor,
    and any MCP client using stdio transport.

    The protocol is:
    - Read line-delimited JSON from stdin
    - Write line-delimited JSON to stdout
    - Log messages go to stderr
    """
    # Notify ready on stderr (so it doesn't interfere with stdout protocol)
    print(f"[MCP] Oracle server ready — {len(_TOOL_NAMES)} tools loaded",
          file=sys.stderr, flush=True)

    while True:
        try:
            request = _read_stdio_request()
            if request is None:
                break
            response = _handle_mcp_request(request)
            if response is not None:
                _write_stdio_response(response)
        except json.JSONDecodeError:
            _write_stdio_response(_make_error(-32700, "Parse error", None))
        except EOFError:
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[MCP] Error: {e}", file=sys.stderr, flush=True)
            _write_stdio_response(
                _make_error(-32603, f"Internal error: {type(e).__name__}: {e}", None)
            )


# ════════════════════════════════════════════════════════════════
#  HTTP/SSE Transport (optional, for browser/web clients)
# ════════════════════════════════════════════════════════════════

def run_http_server(host: str = "0.0.0.0", port: int = 8100):
    """Run MCP server over HTTP with a web UI for manual tool navigation.

    The web UI at http://localhost:{port}/ lets you:
    - Browse all tools with their schemas
    - Fill in parameters in forms
    - Execute tools and see results
    - Use as an MCP HTTP endpoint at /api/mcp
    """
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler
    except ImportError:
        print("[ERROR] HTTP server requires Python stdlib http.server")
        sys.exit(1)

    import urllib.parse

    class MCPHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Quiet logging
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/")

            if path == "/health" or path == "/api/health":
                self._json_response(200, {
                    "server": "oracle-mcp",
                    "status": "running",
                    "tools": len(_TOOL_NAMES),
                    "protocol": "MCP " + MCP_PROTOCOL_VERSION,
                })
                return

            elif path == "/tools" or path == "/api/tools":
                self._json_response(200, {"tools": get_mcp_tools()})
                return

            elif path == "/openapi.json":
                self._json_response(200, {
                    "openapi": "3.0.0",
                    "info": {"title": "Oracle MCP", "version": "1.0.0"},
                    "paths": {
                        "/api/mcp": {"post": {"summary": "MCP JSON-RPC endpoint"}},
                        "/tools": {"get": {"summary": "List MCP tools"}},
                        "/health": {"get": {"summary": "Health check"}},
                    },
                })
                return

            elif path == "" or path == "/":
                self._serve_web_ui()
                return

            else:
                self._json_response(404, {"error": "Not found"})

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"

            try:
                request = json.loads(body)
            except json.JSONDecodeError:
                self._json_response(400, {"error": "Invalid JSON"})
                return

            if parsed.path.rstrip("/") == "/api/mcp":
                response = _handle_mcp_request(request)
                self._json_response(200, response or {})
            else:
                self._json_response(404, {"error": "Not found"})

        def _json_response(self, status: int, data: dict):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

        def _serve_web_ui(self):
            """Serve the interactive web UI."""
            tools = get_mcp_tools()

            cards_html = ""
            for t in tools:
                name = t["name"]
                desc = t.get("description", "")
                props = t.get("inputSchema", {}).get("properties", {})
                required = t.get("inputSchema", {}).get("required", [])

                params_html = ""
                for pname, pdef in props.items():
                    ptype = pdef.get("type", "string")
                    pdesc = pdef.get("description", "")
                    default = pdef.get("default")
                    enum = pdef.get("enum")
                    is_required = pname in required

                    req_mark = ' <span class="req">*</span>' if is_required else ""
                    extra = ""
                    if enum:
                        options = "".join(f'<option value="{e}">{e}</option>' for e in enum)
                        extra = f"""
                        <select name="{pname}" id="in-{name}-{pname}">
                            <option value="">-- seleziona --</option>
                            {options}
                        </select>"""
                    elif ptype == "boolean":
                        extra = f"""
                        <label class="toggle">
                            <input type="checkbox" name="{pname}" id="in-{name}-{pname}"
                                   {"checked" if default else ""}>
                            <span class="slider"></span>
                        </label>"""
                    else:
                        placeholder = f"default: {default}" if default is not None else ptype
                        extra = f"""<input type="{'number' if ptype in ('integer','number') else 'text'}"
                                 name="{pname}" id="in-{name}-{pname}"
                                 placeholder="{placeholder}">"""

                    params_html += f"""
                    <div class="param">
                        <label for="in-{name}-{pname}">
                            <code>{pname}</code> ({ptype}){req_mark}
                        </label>
                        <div class="param-desc">{pdesc}</div>
                        {extra}
                    </div>"""

                cards_html += f"""
                <div class="card" id="card-{name}">
                    <div class="card-header" onclick="toggle('{name}')">
                        <span class="card-name">{name}</span>
                        <span class="card-desc">{desc[:100]}{'…' if len(desc)>100 else ''}</span>
                    </div>
                    <div class="card-body" id="body-{name}">
                        <p class="full-desc">{desc}</p>
                        <form id="form-{name}" onsubmit="return callTool('{name}')">
                            {params_html}
                            <button type="submit" class="btn-run">▶ Run</button>
                            <button type="button" class="btn-copy" onclick="copyExample('{name}')">📋 Copy CLI</button>
                        </form>
                        <pre class="output" id="out-{name}"></pre>
                    </div>
                </div>"""

            html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Oracle MCP — Tool Browser</title>
<style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{
        background: #0d1117;
        color: #c9d1d9;
        font-family: -apple-system, system-ui, 'Segoe UI', Helvetica, Arial, sans-serif;
        padding: 20px;
        max-width: 960px;
        margin: 0 auto;
    }}
    h1 {{ color: #58a6ff; font-size: 26px; margin-bottom: 4px; }}
    .subtitle {{ color: #8b949e; margin-bottom: 24px; font-size: 14px; }}
    .badge {{
        display: inline-block; background: #1f6feb22; color: #58a6ff;
        padding: 2px 10px; border-radius: 12px; font-size: 12px; margin-left: 8px;
    }}
    .card {{
        background: #161b22; border: 1px solid #30363d;
        border-radius: 8px; margin-bottom: 10px; overflow: hidden;
    }}
    .card-header {{
        padding: 12px 16px; cursor: pointer; display: flex;
        justify-content: space-between; align-items: center;
        transition: background 0.15s;
    }}
    .card-header:hover {{ background: #1c2128; }}
    .card-name {{ font-weight: 600; color: #58a6ff; font-family: 'SF Mono', 'Fira Code', monospace; }}
    .card-desc {{ color: #8b949e; font-size: 13px; max-width: 60%; text-align: right; }}
    .card-body {{ padding: 16px; border-top: 1px solid #30363d; display: none; }}
    .full-desc {{ color: #adb5bd; margin-bottom: 14px; font-size: 13px; }}
    .param {{
        margin-bottom: 10px; padding: 8px 10px; background: #0d1117;
        border-radius: 6px; border: 1px solid #21262d;
    }}
    .param label {{ display: block; margin-bottom: 4px; }}
    .param code {{ color: #ff7b72; font-family: 'SF Mono', monospace; font-size: 13px; }}
    .req {{ color: #f85149; font-weight: bold; }}
    .param-desc {{ color: #6e7681; font-size: 12px; margin-bottom: 4px; }}
    input[type="text"], input[type="number"], select {{
        width: 100%; background: #0d1117; border: 1px solid #30363d;
        border-radius: 4px; color: #c9d1d9; padding: 6px 10px;
        font-size: 13px; font-family: 'SF Mono', monospace;
    }}
    input:focus, select:focus {{ outline: none; border-color: #58a6ff; }}
    .toggle {{ position: relative; display: inline-block; width: 44px; height: 24px; }}
    .toggle input {{ opacity: 0; width: 0; height: 0; }}
    .slider {{
        position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
        background-color: #30363d; transition: .3s; border-radius: 24px;
    }}
    .slider::before {{
        position: absolute; content: ""; height: 18px; width: 18px;
        left: 3px; bottom: 3px; background-color: #c9d1d9; transition: .3s; border-radius: 50%;
    }}
    .toggle input:checked + .slider {{ background-color: #238636; }}
    .toggle input:checked + .slider::before {{ transform: translateX(20px); }}
    .btn-run {{
        background: #238636; color: #fff; border: none; border-radius: 6px;
        padding: 8px 20px; font-size: 14px; cursor: pointer; margin-top: 8px;
    }}
    .btn-run:hover {{ background: #2ea043; }}
    .btn-copy {{
        background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
        border-radius: 6px; padding: 8px 14px; font-size: 13px;
        cursor: pointer; margin-left: 8px;
    }}
    .btn-copy:hover {{ background: #30363d; }}
    .output {{
        background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
        padding: 12px; margin-top: 12px; font-family: 'SF Mono', monospace;
        font-size: 12px; white-space: pre-wrap; max-height: 400px;
        overflow: auto; display: none;
    }}
    .output.show {{ display: block; }}
    .output.error {{ border-color: #f85149; }}
    .output .loading {{ color: #8b949e; }}
    #search {{ width: 100%; padding: 8px 12px; margin-bottom: 16px;
               background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
               color: #c9d1d9; font-size: 14px; }}
    #search:focus {{ border-color: #58a6ff; outline: none; }}
    .clear-btn {{ background: none; border: 1px solid #30363d; color: #8b949e;
                  padding: 2px 8px; border-radius: 4px; cursor: pointer; float: right;
                  font-size: 12px; }}
    .clear-btn:hover {{ color: #c9d1d9; border-color: #58a6ff; }}
</style>
</head>
<body>
    <h1>🔮 Oracle MCP Tools</h1>
    <div class="subtitle">
        {len(tools)} tools &mdash; <strong>Manuale:</strong> clicca un tool per espanderlo,
        compila i parametri e premi <strong>▶ Run</strong>
        <span class="badge">MCP v{MCP_PROTOCOL_VERSION}</span>
    </div>

    <input type="text" id="search" placeholder="Cerca tool... (filtra per nome)" oninput="filterTools()">

    <div id="tools-container">{cards_html}</div>

    <script>
    function toggle(name) {{
        const body = document.getElementById('body-' + name);
        const isOpen = body.style.display === 'block';
        body.style.display = isOpen ? 'none' : 'block';
    }}

    function filterTools() {{
        const q = document.getElementById('search').value.toLowerCase();
        document.querySelectorAll('.card').forEach(card => {{
            const name = card.querySelector('.card-name').textContent.toLowerCase();
            const desc = card.querySelector('.card-desc').textContent.toLowerCase();
            card.style.display = (name.includes(q) || desc.includes(q)) ? '' : 'none';
        }});
    }}

    function getArgs(name) {{
        const form = document.getElementById('form-' + name);
        const formData = new FormData(form);
        const args = {{}};
        for (const [key, value] of formData.entries()) {{
            const el = document.getElementById('in-' + name + '-' + key);
            if (el?.type === 'checkbox') {{
                args[key] = el.checked;
            }} else if (value !== '' && value !== null) {{
                const num = Number(value);
                args[key] = isNaN(num) ? value : num;
            }}
        }}
        return args;
    }}

    function callTool(name) {{
        const args = getArgs(name);
        const out = document.getElementById('out-' + name);
        out.textContent = '⏳ Esecuzione in corso...';
        out.className = 'output show';

        fetch('/api/mcp', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                jsonrpc: '2.0', id: Date.now(),
                method: 'tools/call',
                params: {{ name: name, arguments: args }}
            }})
        }})
        .then(r => r.json())
        .then(data => {{
            const result = data.result || {{}};
            const isErr = result.isError;
            const text = (result.content || []).map(c => c.text || '').join('\\n');
            out.textContent = text || '(risposta vuota)';
            out.className = 'output show' + (isErr ? ' error' : '');
        }})
        .catch(err => {{
            out.textContent = 'Errore: ' + err.message;
            out.className = 'output show error';
        }});

        return false;
    }}

    function copyExample(name) {{
        const args = getArgs(name);
        let cmd = 'python tools/' + name + '.py';
        if (args.command) cmd += ' ' + args.command;
        for (const [k,v] of Object.entries(args)) {{
            if (k === 'command') continue;
            const flag = '--' + k.replace(/_/g, '-');
            if (typeof v === 'boolean') {{
                if (v) cmd += ' ' + flag;
            }} else {{
                cmd += ' ' + flag + ' ' + (typeof v === 'string' ? ('"' + v + '"') : v);
            }}
        }}
        navigator.clipboard.writeText(cmd).then(() => {{
            const btn = event.target;
            btn.textContent = '✅ Copiato!';
            setTimeout(() => btn.textContent = '📋 Copy CLI', 1500);
        }});
    }}
    </script>
</body>
</html>"""

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

    server = HTTPServer((host, port), MCPHandler)
    print(f"\n  🔮 Oracle MCP Server (HTTP mode)")
    print(f"  ──────────────────────────────────────")
    print(f"  Web UI:      http://localhost:{port}")
    print(f"  MCP API:     POST http://localhost:{port}/api/mcp")
    print(f"  Tools list:  GET  http://localhost:{port}/tools")
    print(f"  Health:      GET  http://localhost:{port}/health")
    print(f"  ──────────────────────────────────────")
    print(f"  {len(_TOOL_NAMES)} tools caricati\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer fermato.")
        server.server_close()


# ════════════════════════════════════════════════════════════════
#  CLI Entry Point
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        prog="oracle-mcp",
        description="Oracle MCP Server — Espone tutti i tool Oracle via Model Context Protocol",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modalità d'uso:

  # STDIO (per Claude Code, Codex, OpenCode, Cursor)
  python oracle-rui/mcp_server.py

  # HTTP + Web UI (per browser)
  python oracle-rui/mcp_server.py --http --port 8100

  # Lista tool
  python oracle-rui/mcp_server.py --list
        """,
    )
    parser.add_argument("--http", action="store_true", help="Avvia in modalità HTTP con Web UI")
    parser.add_argument("--port", type=int, default=8100, help="Porta HTTP (default: 8100)")
    parser.add_argument("--host", default="0.0.0.0", help="Host HTTP (default: 0.0.0.0)")
    parser.add_argument("--list", "-l", action="store_true", help="Elenca tool disponibili")

    args = parser.parse_args()

    if args.list:
        tools = get_mcp_tools()
        print(f"\n  Oracle MCP — {len(tools)} tools disponibili:\n")
        for t in tools:
            print(f"  🔧 {t['name']}")
            desc = t.get("description", "")
            print(f"     {desc[:120]}")
            props = t.get("inputSchema", {}).get("properties", {})
            if props:
                keys = [k for k in props.keys() if k != "command"]
                if keys:
                    print(f"     Parametri: {', '.join(keys)}")
            print()
        return

    if args.http:
        run_http_server(host=args.host, port=args.port)
    else:
        print(f"[MCP] Oracle server — {len(_TOOL_NAMES)} tools caricati",
              file=sys.stderr, flush=True)
        run_stdio_server()


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Oracle Tools CLI — Interfaccia unificata per uso manuale dei tool Oracle
======================================================================
Consente di navigare e utilizzare tutti i 13 tool di Oracle
direttamente da terminale, senza LLM.

Usage:
    python oracle_tools_cli.py              # Menu interattivo
    python oracle_tools_cli.py <tool> --help # Help di un tool specifico
    python oracle_tools_cli.py <tool> <args> # Esecuzione diretta

Esempi:
    python oracle_tools_cli.py web_access get https://example.com
    python oracle_tools_cli.py vector_memory search --collection docs --query "test"
    python oracle_tools_cli.py mcts_engine analyze --task "Refactor module X"
"""

import importlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BASE_DIR / "tools"
sys.path.insert(0, str(BASE_DIR))

# ── Tool Metadata ────────────────────────────────────────────────
TOOLS = {
    "web_access": {
        "description": "Accesso Internet robusto e sicuro (GET/POST/download/scrape)",
        "emoji": "🌐",
        "commands": ["get", "post", "download", "scrape", "scrape-links", "parallel", "cache"],
    },
    "vector_memory": {
        "description": "Memoria vettoriale basata su ChromaDB",
        "emoji": "🧠",
        "commands": ["add", "search", "get", "delete", "list-collections", "delete-collection", "count", "info", "image-stats", "image-cleanup", "set-policy"],
    },
    "mcts_engine": {
        "description": "Monte Carlo Tree Search per decision-making complesso",
        "emoji": "🌳",
        "commands": ["analyze", "branches"],
    },
    "wiki_tool": {
        "description": "Gestione wiki personale via API HTTP",
        "emoji": "📝",
        "commands": ["list", "read", "write", "upload", "search", "exists", "delete", "batch"],
    },
    "gmail_client": {
        "description": "Gestione completa posta Gmail (auth, invio, ricerca)",
        "emoji": "📧",
        "commands": ["auth", "send", "search", "read", "list", "trash", "config"],
    },
    "immunity_guardian": {
        "description": "Modulo di sicurezza runtime anti-injection/leak/jailbreak",
        "emoji": "🛡️",
        "commands": ["check", "sanitize", "stats", "session"],
        "no_subcommands": True,
    },
    "interleaved_sandbox": {
        "description": "Esecuzione codice e verifica in sandbox",
        "emoji": "📦",
        "commands": ["run", "verify"],
    },
    "constitution": {
        "description": "Protocollo di autolimitazione e governance Oracle",
        "emoji": "⚖️",
        "commands": ["check", "pending", "approve", "reject", "confirm"],
    },
    "environment_probe": {
        "description": "Verifica pre-flight di connettività, dipendenze e permessi",
        "emoji": "🔍",
        "commands": ["port", "dep", "fs", "env", "check"],
    },
    "multimodal_encoder": {
        "description": "Encoding immagini basato su CLIP",
        "emoji": "🖼️",
        "commands": ["encode"],
    },
    "semantic_context_filter": {
        "description": "Previene context drift in sessioni lunghe",
        "emoji": "🔬",
        "commands": ["filter", "train", "stats"],
    },
    "oracle_orchestrator": {
        "description": "Orchestrazione multi-dominio (Penelope, Archimede, identità)",
        "emoji": "🎭",
        "commands": ["query", "route", "status"],
    },
    "oracle_protocol": {
        "description": "Integra MCTS + Sandbox + critica",
        "emoji": "🔗",
        "commands": ["analyze", "verify", "report"],
    },
    "chunk_filter": {
        "description": "Modello-figlio per filtraggio chunk contesto (SPERIMENTALE)",
        "emoji": "🧪",
        "commands": ["filter", "train", "stats"],
    },
}

# Ordered list for display
TOOL_NAMES = [
    "web_access", "vector_memory", "mcts_engine", "wiki_tool", "gmail_client",
    "immunity_guardian", "interleaved_sandbox", "constitution",
    "environment_probe", "multimodal_encoder", "semantic_context_filter",
    "oracle_orchestrator", "oracle_protocol", "chunk_filter",
]


# ── Utility ──────────────────────────────────────────────────────

def _clear_screen():
    """Clear the terminal screen."""
    os.system('cls' if os.name == 'nt' else 'clear')


def _print_header():
    """Print the Oracle Tools CLI header."""
    print("""
╔══════════════════════════════════════════════════╗
║        🔮 Oracle Tools — Manual CLI             ║
║        Use tools without any LLM                ║
║        Ctrl+C to return · q to quit             ║
╚══════════════════════════════════════════════════╝
""")


def _run_tool_direct(tool_name: str, args: list) -> int:
    """Run a tool by calling its module directly via subprocess with given args."""
    tool_path = TOOLS_DIR / f"{tool_name}.py"
    if not tool_path.exists():
        print(f"[ERROR] Tool '{tool_name}' non trovato in tools/")
        return 1

    cmd = [sys.executable, str(tool_path)] + args
    try:
        result = subprocess.run(cmd, text=True)
        return result.returncode
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print(f"[ERROR] {e}")
        return 1


def _show_tool_help(tool_name: str):
    """Show help for a specific tool."""
    tool_path = TOOLS_DIR / f"{tool_name}.py"
    if not tool_path.exists():
        print(f"[ERROR] Tool '{tool_name}' non trovato.")
        return

    result = subprocess.run(
        [sys.executable, str(tool_path), "--help"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        # Strip ANSI escape codes
        text = result.stdout or result.stderr
        text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
        print(text)
    else:
        print(result.stderr)


def _show_tool_info(tool_name: str):
    """Show detailed info about a tool."""
    meta = TOOLS.get(tool_name, {})
    emoji = meta.get("emoji", "🔧")
    desc = meta.get("description", "")
    commands = meta.get("commands", [])

    print(f"\n  {emoji}  {tool_name}")
    print(f"  {'─' * (len(tool_name) + 4)}")
    print(f"  {desc}")
    if commands:
        print(f"\n  Comandi disponibili:")
        for cmd in commands:
            print(f"    • {cmd}")
    print(f"\n  Esempi:")
    if tool_name == "web_access":
        print(f"    python tools/{tool_name}.py get https://example.com")
        print(f"    python tools/{tool_name}.py scrape https://example.com --selector h1")
    elif tool_name == "vector_memory":
        print(f"    python tools/{tool_name}.py search --collection docs --query \"test\"")
        print(f"    python tools/{tool_name}.py add --collection docs --id doc1 --text \"content\"")
    elif tool_name == "mcts_engine":
        print(f"    python tools/{tool_name}.py analyze --task \"Refactor module\"")
    elif tool_name == "gmail_client":
        print(f"    python tools/{tool_name}.py list --max-results 5")
    else:
        print(f"    python tools/{tool_name}.py --help")
    print()


# ── Interactive Menu ─────────────────────────────────────────────

def _interactive_menu():
    """Show the interactive tool selection menu."""
    while True:
        _clear_screen()
        _print_header()

        print("  Tools disponibili:\n")
        for i, name in enumerate(TOOL_NAMES, 1):
            meta = TOOLS.get(name, {})
            emoji = meta.get("emoji", "🔧")
            desc = meta.get("description", "")
            commands = meta.get("commands", [])
            cmd_summary = ", ".join(commands[:4])
            if len(commands) > 4:
                cmd_summary += "..."
            print(f"  {i:2d}. {emoji} {name}")
            print(f"     {desc}")
            print(f"     \033[2mComandi: {cmd_summary}\033[0m")
            print()

        print("  [q] Esci  |  [n] Nome tool per uso diretto\n")

        try:
            choice = input("  Scegli un tool (numero o nome): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice in ("q", "exit", "quit"):
            break

        # Try number
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(TOOL_NAMES):
                _tool_interactive(TOOL_NAMES[idx])
            else:
                print(f"  Numero non valido (1-{len(TOOL_NAMES)})")
                input("  Premi Invio per continuare...")
            continue

        # Try name
        if choice in TOOLS:
            _tool_interactive(choice)
            continue

        # Try partial match
        matches = [n for n in TOOL_NAMES if choice in n]
        if len(matches) == 1:
            _tool_interactive(matches[0])
        elif len(matches) > 1:
            print(f"  Tool multipli trovati: {', '.join(matches)}")
            input("  Premi Invio per continuare...")
        else:
            print(f"  Tool '{choice}' non trovato. Usa un numero o un nome valido.")
            input("  Premi Invio per continuare...")


def _tool_interactive(tool_name: str):
    """Interactive mode for a specific tool."""
    meta = TOOLS.get(tool_name, {})
    emoji = meta.get("emoji", "🔧")
    commands = meta.get("commands", [])

    while True:
        _clear_screen()
        _show_tool_info(tool_name)

        print(f"  {emoji}  {tool_name} — Comandi disponibili:\n")
        for i, cmd in enumerate(commands, 1):
            print(f"  {i:2d}. {cmd}")
        print(f"\n  [h] Help completo  |  [b] Torna al menu  |  [q] Esci")

        try:
            choice = input(f"\n  Scegli un comando o digita direttamente: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice in ("b", "back", "menu"):
            break
        if choice in ("q", "exit", "quit"):
            sys.exit(0)
        if choice in ("h", "help", "--help"):
            _clear_screen()
            _show_tool_help(tool_name)
            input("\n  Premi Invio per continuare...")
            continue

        # Try number
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(commands):
                _run_command_interactive(tool_name, commands[idx])
                continue

        # It's a command or direct args
        parts = shlex.split(choice)
        cmd = parts[0]
        args = parts[1:]

        if cmd in commands:
            _run_command_interactive(tool_name, cmd, args)
        elif cmd.startswith("--"):
            # Assume it's an arg to the tool itself
            _run_tool_interactive_args(tool_name, [cmd] + args)
        else:
            print(f"  Comando '{cmd}' non riconosciuto per {tool_name}")
            input("  Premi Invio per continuare...")


def _run_command_interactive(tool_name: str, command: str, extra_args: list = None):
    """Run a specific command of a tool interactively."""
    meta = TOOLS.get(tool_name, {})
    commands = meta.get("commands", [])
    extra_args = extra_args or []

    # Build args list
    args = [command] + extra_args

    if not extra_args:
        # Ask for additional arguments interactively
        print(f"\n  Comando: {tool_name} {command}")
        print(f"  Inserisci argomenti extra (o lascia vuoto per eseguire senza):")

        collected_args = []
        while True:
            try:
                arg_input = input(f"  $ ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not arg_input:
                break

            # Parse the input as key=value or --flag value
            if "=" in arg_input:
                key, val = arg_input.split("=", 1)
                key = key.strip()
                val = val.strip()
                if not key.startswith("--"):
                    key = f"--{key}"
                collected_args.append(key)
                if val:
                    collected_args.append(val)
            elif arg_input.startswith("--") or arg_input.startswith("-"):
                # --flag or -flag, next input might be value
                parts = shlex.split(arg_input)
                collected_args.extend(parts)
            else:
                # Assume it's a positional value
                collected_args.append(arg_input)

        if collected_args:
            args.extend(collected_args)

    # Execute
    print(f"\n  Esecuzione: python tools/{tool_name}.py {' '.join(args)}\n")
    print("─" * 60)

    try:
        rc = _run_tool_direct(tool_name, args)
    except KeyboardInterrupt:
        rc = 0

    print("─" * 60)
    input(f"\n  Completato (exit code: {rc}). Premi Invio per continuare...")


def _run_tool_interactive_args(tool_name: str, args: list):
    """Run a tool with given args interactively."""
    print(f"\n  Esecuzione: python tools/{tool_name}.py {' '.join(args)}\n")
    print("─" * 60)

    try:
        rc = _run_tool_direct(tool_name, args)
    except KeyboardInterrupt:
        rc = 0

    print("─" * 60)
    input(f"\n  Completato (exit code: {rc}). Premi Invio per continuare...")


# ════════════════════════════════════════════════════════════════
#  CLI Entry Point
# ════════════════════════════════════════════════════════════════

def main():
    """Unified CLI entry point for Oracle tools.

    If called with no arguments, launches interactive menu.
    If called with a tool name, runs that tool directly with remaining args.
    """
    if len(sys.argv) == 1:
        # No args → interactive menu
        try:
            _interactive_menu()
        except KeyboardInterrupt:
            print()
        return

    tool_name = sys.argv[1].lower()

    if tool_name in ("--help", "-h", "help"):
        print("""
Oracle Tools CLI — Interfaccia unificata per i tool Oracle.

Usage:
    python oracle_tools_cli.py                       ← Menu interattivo
    python oracle_tools_cli.py <tool> [args...]       ← Esegue tool direttamente
    python oracle_tools_cli.py <tool> --help          ← Help del tool
    python oracle_tools_cli.py --list                 ← Elenca tool

Tool disponibili:
""")
        for name in TOOL_NAMES:
            meta = TOOLS.get(name, {})
            emoji = meta.get("emoji", "🔧")
            print(f"  {emoji}  {name}")
            print(f"     {meta.get('description', '')}")
        print()
        return

    if tool_name in ("--list", "-l", "list"):
        print(f"\nOracle Tools CLI — {len(TOOL_NAMES)} tool disponibili:\n")
        for name in TOOL_NAMES:
            meta = TOOLS.get(name, {})
            emoji = meta.get("emoji", "🔧")
            print(f"  {emoji} {name} — {meta.get('description', '')}")
        print()
        return

    if tool_name in TOOLS:
        # Execute the tool with remaining args
        tool_args = sys.argv[2:]
        if "--help" in tool_args or "-h" in tool_args:
            _show_tool_help(tool_name)
        else:
            rc = _run_tool_direct(tool_name, tool_args)
            sys.exit(rc)
        return

    # Unknown tool
    print(f"[ERROR] Tool '{tool_name}' non trovato.")
    print(f"  Tool disponibili: {', '.join(TOOL_NAMES)}")
    sys.exit(1)


if __name__ == "__main__":
    main()
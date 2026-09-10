#!/usr/bin/env python3
"""
Chora — Unified system startup.

One command to start the entire Chora ecosystem:

  python run.py

Components:
  * CHORA Core (executive agent + frontend) — :8100
  * Penelope (knowledge graph)                — :5000  (optional)
  * Archimede (graph data engine)             — :8001  (optional)
  * Egida (HSD guardrail)                     — integrated across all layers

Usage:
    python run.py                              # Start CHORA Core (default)
    python run.py --with-penelope              # Also start Penelope (:5000)
    python run.py --with-archimede             # Also start Archimede (:8001)
    python run.py --all                        # Start EVERYTHING
    python run.py --port 8100                  # Custom port
    python run.py --status                     # Check component status
    python run.py --init                       # First run: guided setup

Configuration:
    1. Copy oracle-rui/.env.example to oracle-rui/.env and configure API keys
    2. For Penelope: copy penelope/.env.example to penelope/.env
    3. For Archimede: copy archimede/.env.example to archimede/.env
    4. (Optional) docker-compose up -d to start MariaDB
"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

# ── Path setup ──────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent
_ORACLE_ROOT = _ROOT / "oracle-rui"
_ARCHIMEDE_ROOT = _ROOT / "archimede"
_PENELOPE_ROOT = _ROOT / "penelope"
_EGIDA_ROOT = _ROOT / "egida"

for _p in (str(_ROOT), str(_ORACLE_ROOT), str(_ARCHIMEDE_ROOT),
           str(_PENELOPE_ROOT), str(_EGIDA_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chora")


def wait_for_server(url: str, timeout: int = 15, interval: float = 0.5) -> bool:
    """Wait for a server to respond on a URL."""
    import httpx
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code < 500:
                return True
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
            pass
        time.sleep(interval)
    return False


def start_penelope() -> subprocess.Popen | None:
    """Start Penelope Web API (:5000) as a subprocess.

    Prima di avviare il server Flask:
    1. Esegue device reconciliation (mount automatici via marker files)
    2. Per ogni device con mount aggiornato, avvia uno scan in background
       (thread daemon) così il contenuto nuovo entra in coda subito.
    """
    script = _PENELOPE_ROOT / "web" / "api.py"
    if not script.exists():
        logger.warning("[WARN] Penelope API not found: %s", script)
        return None

    log_file = _ROOT / "logs" / "penelope.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # ── Device reconciliation (auto-mount via marker files) ──────
    _reconcile_and_scan()

    logger.info("Starting Penelope on :5000...")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(_PENELOPE_ROOT),
            stdout=open(log_file, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        if wait_for_server("http://127.0.0.1:5000/api/stats", timeout=30):
            logger.info("[OK] Penelope ready on http://localhost:5000")
        else:
            logger.warning("[WARN] Penelope started but not responding (log: %s)", log_file)
        return proc
    except Exception as e:
        logger.error("[ERR] Error starting Penelope: %s", e)
        return None


def _reconcile_and_scan() -> None:
    """Esegue device reconciliation e avvia scan automatici in background.

    Chiamata da start_penelope() prima di avviare il server Flask.
    Non blocca l’avvio: eventuali errori sono loggati come warning.
    """
    try:
        from penelope.discovery import reconcile_devices
        from penelope.db.mariadb_store import MariaDBStore
        from penelope.ingestion.scanner import FileScanner

        db = MariaDBStore()
        updated = reconcile_devices(db)

        if not updated:
            logger.debug("Nessun device aggiornato durante reconciliation")
            return

        logger.info("Device reconciliation: %d mount aggiornati/creati", len(updated))

        # ── Auto-scan in background per ogni device aggiornato ──
        import threading

        for entry in updated:
            label = entry.get("device_label", f"device_{entry['device_id']}")
            mountpoint = entry.get("mountpoint")
            if not mountpoint:
                continue

            def _scan(dev_label: str, mp: str) -> None:
                try:
                    from pathlib import Path

                    root = Path(mp)
                    subdirs = [d for d in root.iterdir() if d.is_dir()]

                    if subdirs:
                        logger.info(
                            "Auto-scan avviato: %s (%s) — %d sottocartelle",
                            dev_label, mp, len(subdirs),
                        )
                        scanner = FileScanner(device_name=dev_label)
                        for sd in subdirs:
                            scanner.scan_directory(str(sd), project_label=sd.name)
                        logger.info(
                            "Auto-scan completato: %s — %d progetti processati",
                            dev_label, len(subdirs),
                        )
                    else:
                        logger.info(
                            "Auto-scan avviato: %s (%s) — nessuna sottocartella, scan radice",
                            dev_label, mp,
                        )
                        scanner = FileScanner(device_name=dev_label)
                        scanner.scan_directory(mp, project_label=dev_label)
                        logger.info("Auto-scan completato: %s", dev_label)
                except Exception as e:
                    logger.warning("Auto-scan fallito per %s (%s): %s", dev_label, mp, e)

            t = threading.Thread(
                target=_scan,
                args=(label, mountpoint),
                daemon=True,
            )
            t.start()
            logger.debug("Thread scan avviato per %s", label)

    except ImportError as e:
        logger.warning("Device reconciliation non disponibile (moduli mancanti): %s", e)
    except Exception as e:
        logger.warning("Device reconciliation fallita (non bloccante): %s", e)


def start_archimede() -> subprocess.Popen | None:
    """Start Archimede API (:8001) as a subprocess."""


def start_mcp() -> subprocess.Popen | None:
    """Start the MCP server in HTTP mode (:8101)."""
    script = _ORACLE_ROOT / "mcp_server.py"
    if not script.exists():
        logger.warning("[WARN] MCP server not found: %s", script)
        return None

    log_file = _ROOT / "logs" / "mcp.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Starting Oracle MCP Server on :8101...")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script), "--http", "--port", "8101"],
            cwd=str(_ORACLE_ROOT),
            stdout=open(log_file, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        logger.info("[OK] MCP Server starting on http://localhost:8101")
        logger.info("[INFO] MCP tools:  http://localhost:8101/tools")
        return proc
    except Exception as e:
        logger.error("[ERR] Error starting MCP server: %s", e)
        return None
    script = _ARCHIMEDE_ROOT / "archimede" / "api.py"
    if not script.exists():
        logger.warning("[WARN] Archimede API not found: %s", script)
        return None

    log_file = _ROOT / "logs" / "archimede.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Starting Archimede on :8001...")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script), "--port", "8001"],
            cwd=str(_ARCHIMEDE_ROOT),
            stdout=open(log_file, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        if wait_for_server("http://127.0.0.1:8001/archimede/health", timeout=20):
            logger.info("[OK] Archimede ready on :8001")
        else:
            logger.warning("[WARN] Archimede started but not responding (log: %s)", log_file)
        return proc
    except Exception as e:
        logger.error("[ERR] Error starting Archimede: %s", e)
        return None


def print_banner(args: argparse.Namespace):
    """Print the startup banner."""
    print()
    print("  +--------------------------------------------------+")
    print("  |                   C H O R A                       |")
    print("  |     Semantic Archive * Knowledge Graph            |")
    print("  +--------------------------------------------------+")
    print(f"  |  CHORA Core:       http://localhost:{args.port:<5}               |")
    print(f"  |  MCP Server:       http://localhost:8101 (MCP)   |")
    print(f"  |  Penelope:         {'active on :5000' if args.with_penelope else 'not started':<31} |")
    print(f"  |  Archimede:        {'active on :8001' if args.with_archimede else 'not started':<31} |")
    print(f"  |  Egida:            always active (HSD guardrail)  |")
    print("  +--------------------------------------------------+")
    print()
    print("  Alternative entry points:")
    print(f"  • Manual CLI:   python oracle-rui/oracle_tools_cli.py")
    print(f"  • MCP stdio:    python oracle-rui/mcp_server.py")
    print(f"  • CHORA UI:     http://localhost:{args.port}")


def check_status():
    """Check the status of all components."""
    import httpx

    print()
    W = 52
    sep = "  +-" + "-" * W + "+"
    print(sep)
    print("  |  " + "Chora — Diagnostics".center(W) + "  |")
    print(sep)

    def line(label, ok, text=""):
        status = "[OK]" if ok else "[OFF]"
        content = f"  |  {label:<12} {status}"
        if text:
            content += "  " + text
        content = content.ljust(W + 6) + "|"
        print(content)

    # Oracle :8100
    try:
        r = httpx.get("http://127.0.0.1:8100/api/health", timeout=3.0)
        if r.status_code == 200:
            data = r.json()
            line("Oracle", True, f":8100 (v{data.get('version', '?')})")
        else:
            line("Oracle", False, f":8100 status={r.status_code}")
    except Exception:
        line("Oracle", False, ":8100 — NOT running")

    # Penelope :5000
    try:
        r = httpx.get("http://127.0.0.1:5000/api/stats", timeout=3.0)
        if r.status_code == 200:
            data = r.json()
            line("Penelope", True, f":5000 ({data.get('total_nodes', '?')} nodes)")
        else:
            line("Penelope", False, f":5000 status={r.status_code}")
    except Exception:
        line("Penelope", False, ":5000 — NOT running")

    # Archimede :8001
    try:
        r = httpx.get("http://127.0.0.1:8001/archimede/health", timeout=2.0)
        if r.status_code == 200:
            line("Archimede", True, ":8001 (read-only graph)")
        else:
            line("Archimede", False, f":8001 status={r.status_code}")
    except Exception:
        line("Archimede", False, ":8001 — NOT running")

    # MCP :8101
    try:
        r = httpx.get("http://127.0.0.1:8101/health", timeout=2.0)
        if r.status_code == 200:
            data = r.json()
            line("MCP Server", True, f":8101 ({data.get('tools', '?')} tools)")
        else:
            line("MCP Server", False, f":8101 status={r.status_code}")
    except Exception:
        line("MCP Server", False, ":8101 — NOT running")

    print(sep)
    print()
    print("Tips:")
    print("  * Basic startup:              python run.py")
    print("  * With MCP:                   python run.py --with-mcp")
    print("  * With everything:            python run.py --all")
    print("  * Manual CLI:                 python oracle-rui/oracle_tools_cli.py")
    print("  * CHORA UI:                   http://localhost:8100")
    print("  * MCP for Claude Code/Codex:  python oracle-rui/mcp_server.py")
    print("  * Detailed status:            curl http://localhost:8100/api/health")
    print()


def cmd_init(path: str | None = None):
    """Guided first-time setup: init .env templates, register storage, scan files.

    Args:
        path: Directory da aggiungere come storage e scansionare.
              Se None, solo setup template .env e logs (nessuna registrazione device).
    """
    print()
    print("=" * 60)
    print("Chora — Guided Setup")
    print("=" * 60)
    print()

    # ── 1-4: Setup template .env e logs ─────────────────────────
    oracle_env = _ORACLE_ROOT / ".env"
    oracle_env_example = _ORACLE_ROOT / ".env.example"
    if not oracle_env.exists() and oracle_env_example.exists():
        import shutil
        shutil.copy2(oracle_env_example, oracle_env)
        print("[OK] Created oracle-rui/.env from template")
        print("     Edit it to insert your LLM API key:")
        print(f"     {oracle_env}")
    elif oracle_env.exists():
        print("[OK] oracle-rui/.env already present")

    penelope_env = _PENELOPE_ROOT / ".env"
    penelope_env_example = _PENELOPE_ROOT / ".env.example"
    if not penelope_env.exists() and penelope_env_example.exists():
        import shutil
        shutil.copy2(penelope_env_example, penelope_env)
        print("[OK] Created penelope/.env from template")
    elif penelope_env.exists():
        print("[OK] penelope/.env already present")

    archimede_env = _ARCHIMEDE_ROOT / ".env"
    archimede_env_example = _ARCHIMEDE_ROOT / ".env.example"
    if not archimede_env.exists() and archimede_env_example.exists():
        import shutil
        shutil.copy2(archimede_env_example, archimede_env)
        print("[OK] Created archimede/.env from template")
    elif archimede_env.exists():
        print("[OK] archimede/.env already present")

    (_ROOT / "logs").mkdir(parents=True, exist_ok=True)
    print("[OK] Created logs/ directory")

    # ── Se nessun path fornito: solo setup, interrompi ──────────
    if not path:
        print()
        print("Usage: python run.py --init <PATH>")
        print()
        print("  <PATH>  Directory di storage da registrare e scansionare.")
        print()
        print("  Esempio:")
        print("    python run.py --init /Volumes/HDD/Documenti")
        print()
        print("  Rilanciare con lo stesso PATH è idempotente:")
        print("  i file già noti vengono skippati (sha256 match).")
        print()
        print("  Per aggiungere un secondo storage:")
        print("    python run.py --init /Volumes/NAS/Archivio")
        print()
        print("=" * 60)
        return

    # ── 5. Valida path ──────────────────────────────────────────
    storage_path = Path(path).resolve()
    if not storage_path.is_dir():
        print(f"[ERR] Path non è una directory o non esiste: {storage_path}")
        print()
        return

    print()
    print(f"  Storage path: {storage_path}")
    print()

    # ── 6. Persiste path in .env (trova/crea slot) ──────────────
    print('-' * 60)
    print('Storage slot allocation (.env)')
    print('-' * 60)
    print()

    try:
        from penelope.discovery import ensure_storage_in_env
        label_key = ensure_storage_in_env(storage_path)
        print(f"  [OK]  Path registrato come '{label_key}' in penelope/.env")
    except (ImportError, FileNotFoundError, ValueError) as e:
        print(f"  [ERR] {e}")
        print()
        return

    # ── 7. Device registration (marker) ──────────────────────────
    print()
    print('-' * 60)
    print('Device registration (marker + mount)')
    print('-' * 60)
    print()

    try:
        from penelope.db.mariadb_store import MariaDBStore
        from penelope.discovery import write_device_marker, read_device_marker

        db = MariaDBStore()
        hostname = MariaDBStore.current_hostname()

        marker = read_device_marker(storage_path)

        if marker is None:
            # Nuovo device: crea in DB, scrive marker, upsert mount
            with db as store:
                device_id = store.ensure_device(
                    label=label_key,
                    device_type='local',
                )
            ok = write_device_marker(storage_path, device_id, label_key)
            if not ok:
                print(f'   [WARN] Marker non scritto (permessi su {storage_path})')
            else:
                print(f'   [OK]  Marker scritto: {storage_path}/.penelope_device.json')
            with db as store:
                store.upsert_device_mount(device_id, hostname, str(storage_path))
            print(f'   [OK]  Device "{label_key}" (id={device_id}) creato e mount registrato')
            print(f'         per host "{hostname}" → {storage_path}')
        else:
            # Device già noto: solo upsert mount per host corrente
            device_id = marker['device_id']
            with db as store:
                store.upsert_device_mount(device_id, hostname, str(storage_path))
            print(f'   [OK]  Device "{label_key}" (id={device_id}) — mount aggiornato')
            print(f'         per host "{hostname}" → {storage_path}')

    except ImportError as e:
        print(f'   [SKIP] Device registration non disponibile: {e}')
        print()
        print('=' * 60)
        return

    # ── 8. Scan immediato del path ───────────────────────────────
    print()
    print('-' * 60)
    print('File scan (new/changed files only)')
    print('-' * 60)
    print()
    print(f'  Scanning {storage_path}...')
    print(f'  (file già noti per sha256 vengono skippati — idempotente)')
    print()

    scan_ok = 0
    scan_skipped = 0
    scan_errors = 0

    try:
        from penelope.ingestion.scanner import FileScanner

        scanner = FileScanner(device_name=label_key)
        results = scanner.scan_directory(
            str(storage_path),
            project_label=label_key,
        )

        scan_ok = sum(1 for r in results if r.success)
        scan_skipped = sum(1 for r in results if r.skipped)
        scan_errors = sum(1 for r in results if r.error)

        print(f'   [INDEX]  Indicizzati:   {scan_ok}')
        print(f'   [SKIP]   Saltati:       {scan_skipped}')
        print(f'   [ERR]    Errori:        {scan_errors}')
        print(f'   [PROJ]   Progetto:      {label_key}')

    except ImportError as e:
        print(f'   [SKIP] Scan non disponibile: {e}')
    except Exception as e:
        print(f'   [ERR]  {e}')

    # ── Done ─────────────────────────────────────────────────────
    print()
    print('=' * 60)
    print('Setup completo!')
    print()
    print(f'  Storage:    {storage_path}')
    print(f'  Device:     {label_key}')
    print(f'  File scansionati: {scan_ok + scan_skipped} totali ({scan_ok} nuovi)')
    print()
    print('  Per avviare Penelope e servire l\'interfaccia:')
    print('    python run.py --all')
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Chora — Unified startup"
    )
    parser.add_argument("--port", type=int, default=8100,
                        help="CHORA Core port (default: 8100)")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="CHORA Core host")
    parser.add_argument("--with-penelope", action="store_true",
                        help="Also start Penelope API (:5000)")
    parser.add_argument("--with-archimede", action="store_true",
                        help="Also start Archimede API (:8001)")
    parser.add_argument("--with-mcp", action="store_true",
                        help="Also start MCP Server (:8101)")
    parser.add_argument("--all", action="store_true",
                        help="Start ALL components")
    parser.add_argument("--status", "--check", action="store_true",
                        help="Check component status (without starting)")
    parser.add_argument("--init", nargs="?", const=None, metavar="PATH",
                        help="Register a storage path and scan it: python run.py --init /path/to/dir")
    args = parser.parse_args()

    # ── Guided setup ─────────────────────────────────────────
    if args.init is not None:
        cmd_init(args.init)
        return

    # ── Diagnostics ───────────────────────────────────────────
    if args.status:
        check_status()
        return

    # ── --all enables everything ──────────────────────────────
    if args.all:
        args.with_penelope = True
        args.with_archimede = True
        args.with_mcp = True

    penelope_proc: subprocess.Popen | None = None
    archimede_proc: subprocess.Popen | None = None
    mcp_proc: subprocess.Popen | None = None

    # ── 1. Penelope (optional) ──────────────────────────────
    if args.with_penelope:
        penelope_proc = start_penelope()

    # ── 2. Archimede (optional) ─────────────────────────────
    if args.with_archimede:
        archimede_proc = start_archimede()

    # ── 3. MCP Server (optional) ─────────────────────────────
    if args.with_mcp:
        mcp_proc = start_mcp()

    # ── 4. Start CHORA Core ─────────────────────────────────
    print_banner(args)

    # Only start CHORA Core if agno is available or we're not explicitly
    # running in MCP-only mode (which doesn't need agno at all)
    _start_oracle_core = True
    _agno_available = False

    try:
        import agno  # type: ignore
        _agno_available = True
    except ImportError:
        _agno_available = False
        if args.with_mcp:
            # MCP-only mode: CHORA Core not needed, just keep MCP running
            _start_oracle_core = False
            logger.info(
                "agno not installed — CHORA Core not available. "
                "MCP Server is running on :8101. "
                "Install with: pip install -r oracle-rui/requirements-core.txt"
            )

    if _start_oracle_core and not _agno_available:
        logger.error(
            "Cannot start CHORA Core: module 'agno' not installed.\n"
            "  Install with: pip install -r oracle-rui/requirements-core.txt\n"
            "  Or use MCP-only mode: python run.py --with-mcp (no agno needed)"
        )
        _start_oracle_core = False

    try:
        if _start_oracle_core:
            import os
            os.environ["ORACLE_PORT"] = str(args.port)
            os.environ["ORACLE_HOST"] = args.host

            from coding_agent import app
            import uvicorn

            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        else:
            # Keep the process alive (e.g., while MCP server runs)
            if mcp_proc and mcp_proc.poll() is None:
                logger.info("CHORA Core not started. MCP Server running. Press Ctrl+C to stop.")
                # Wait for subprocesses to finish
                import time as _time
                while any(p and p.poll() is None for p in [penelope_proc, archimede_proc, mcp_proc]):
                    _time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Chora] Shutting down...")
    finally:
        if penelope_proc:
            logger.info("Stopping Penelope (PID: %d)...", penelope_proc.pid)
            if sys.platform == "win32":
                penelope_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                penelope_proc.terminate()
            try:
                penelope_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                penelope_proc.kill()
            logger.info("Penelope stopped.")

        # Cleanup Archimede
        if archimede_proc:
            logger.info("Stopping Archimede (PID: %d)...", archimede_proc.pid)
            if sys.platform == "win32":
                archimede_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                archimede_proc.terminate()
            try:
                archimede_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                archimede_proc.kill()
            logger.info("Archimede stopped.")

        # Cleanup MCP Server
        if mcp_proc:
            logger.info("Stopping MCP Server (PID: %d)...", mcp_proc.pid)
            if sys.platform == "win32":
                mcp_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                mcp_proc.terminate()
            try:
                mcp_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mcp_proc.kill()
            logger.info("MCP Server stopped.")

    print("[Chora] Goodbye.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Oracle Tool Runner — Wrapper con compatibilità Python 3.9
===========================================================
Aggiunge automaticamente 'from __future__ import annotations' a TUTTI
i moduli Python caricati dal progetto Oracle, risolvendo i problemi
con le annotazioni di tipo Python 3.10+ (es. 'str | Path', 'str | None').

Questo permette di eseguire tool Oracle con Python 3.9 senza dover
modificare i file originali.

Meccanismo:
    Usa sys.meta_path per intercettare il caricamento dei moduli del
    progetto Oracle e aggiunge 'from __future__ import annotations'
    al codice sorgente PRIMA della compilazione.

Usage:
    python _tool_runner.py <tool_name> [args...]

Esempio:
    python _tool_runner.py immunity_guardian session
    python _tool_runner.py mcts_engine analyze --task "test"
"""

import importlib.abc
import importlib.machinery
import sys
import types
from pathlib import Path


class FutureAnnotationsLoader(importlib.abc.Loader):
    """Loader that adds `from __future__ import annotations` to source before compilation."""
    
    def create_module(self, spec):
        return None  # Use default semantics
    
    def exec_module(self, module):
        spec = module.__spec__
        if spec is None or not spec.origin or not spec.origin.endswith('.py'):
            # Let the parent loader handle it
            path = getattr(module, '__file__', None)
            if path and path.endswith('.py'):
                self._exec_with_annotations(module, path)
            return
        
        path = spec.origin
        if path.endswith('.py'):
            self._exec_with_annotations(module, path)
    
    def _exec_with_annotations(self, module: types.ModuleType, path: str):
        """Read source, add __future__ import, compile and exec."""
        try:
            source = Path(path).read_text(encoding='utf-8')
        except Exception:
            return
        
        # Only add if not already present
        if 'from __future__ import annotations' not in source:
            source = 'from __future__ import annotations\n' + source
        
        code = compile(source, path, 'exec')
        exec(code, module.__dict__)


class FutureAnnotationsFinder(importlib.abc.MetaPathFinder):
    """Meta path finder that wraps Oracle project modules with annotation support."""
    
    def __init__(self):
        # Determine which directories to intercept
        self._project_dirs = []
        
        # Find oracle-rui directory
        runner_path = Path(__file__).resolve().parent
        oracle_rui = str(runner_path)
        self._project_dirs.append(oracle_rui)
        
        # Also add parent (OracleResearch-master)
        parent = str(runner_path.parent)
        if parent != oracle_rui:
            self._project_dirs.append(parent)
    
    def find_spec(self, fullname, path, target=None):
        # Check if this module comes from our project directories
        if not path:
            return None
        
        for p in path:
            # Check if any of the paths is inside our project
            for proj_dir in self._project_dirs:
                if p.startswith(proj_dir) or proj_dir.startswith(p):
                    # This module is from our project
                    # Try to find its spec using standard machinery
                    try:
                        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
                        if spec is not None:
                            # Set our loader
                            spec.loader = FutureAnnotationsLoader()
                            return spec
                    except Exception:
                        pass
        
        return None


# Install the finder at the front of meta_path (before default finders)
_annotations_finder = FutureAnnotationsFinder()
sys.meta_path.insert(0, _annotations_finder)


# ── Main ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: _tool_runner.py <tool_name> [args...]")
        sys.exit(1)

    tool_name = sys.argv[1]
    tool_args = sys.argv[2:]

    # Replace sys.argv so the tool sees its own CLI args
    sys.argv = [f"tools/{tool_name}.py"] + tool_args

    # Find the tool module path
    base_dir = Path(__file__).resolve().parent
    tool_path = base_dir / "tools" / f"{tool_name}.py"
    if not tool_path.exists():
        print(f"[ERROR] Tool '{tool_name}' not found at {tool_path}")
        sys.exit(1)

    # Ensure the oracle-rui directory is in sys.path
    oracle_dir = str(base_dir)
    if oracle_dir not in sys.path:
        sys.path.insert(0, oracle_dir)

    # Read the tool source, ensuring __future__ import is present
    source = tool_path.read_text(encoding="utf-8")
    if "from __future__ import annotations" not in source:
        source = "from __future__ import annotations\n" + source

    # Create namespace with __name__ = '__main__' so the tool's
    # `if __name__ == '__main__': main()` block executes
    module_ns = {
        '__name__': '__main__',
        '__file__': str(tool_path),
        '__package__': None,
        '__doc__': None,
        '__builtins__': __builtins__,
    }

    # Execute the tool source (the meta_path finder will add __future__ to
    # all dependencies automatically)
    code = compile(source, str(tool_path), "exec")
    exec(code, module_ns)

    sys.exit(0)


if __name__ == "__main__":
    main()
"""
Stub per llm_logger — logging delle chiamate LLM.
Oracle lo usa per tracciare il contesto delle chiamate.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("oracle.llm_logger")

_caller_tag: str | None = None


def set_caller_tag(tag: str) -> None:
    """Imposta un tag per identificare il chiamante delle richieste LLM."""
    global _caller_tag
    _caller_tag = tag
    logger.debug("LLM caller tag: %s", tag)


def clear_caller_tag() -> None:
    """Rimuove il tag del chiamante."""
    global _caller_tag
    _caller_tag = None
    logger.debug("LLM caller tag cleared")


def get_caller_tag() -> str | None:
    """Restituisce il tag corrente."""
    return _caller_tag

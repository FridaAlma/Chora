import asyncio
import json
import logging
import os
import queue
import re
import sys
from pathlib import Path
from typing import Annotated, Optional, List
from datetime import datetime

from dotenv import load_dotenv


# ── Keywords that auto-activate the Pro model ────────────────────
_COMPLEXITY_KEYWORDS = {
    # OSINT / verification
    "osint", "verifica", "verify", "scam", "truffa", "fraud",
    "cross-reference", "fact-check", "factcheck",
    "confidence", "inconsistency", "incongruity", "incongruenza", "pest",
    "triangulation", "triangolazione", "reputation",
    # Security
    "security", "vulnerability", "cve", "exploit", "threat",
    "malware", "phishing", "breach", "data leak",
    # Complex coding
    "refactor", "architecture", "architecture", "design pattern",
    "multi-file", "codebase", "dependency", "migration",
    "optimization", "performance",
    # Deep analysis
    "deep analysis", "in-depth", "comprehensive",
    "research", "ricerca", "investigation", "indagine",
    "report", "executive summary",
}

# ── Words indicating simple tasks → LITE prompt + Flash ─────
_LITE_KEYWORDS = {
    "hello", "hi", "ciao", "help", "aiuto",
    "typo", "format", "formatta",
    "chi sei", "what is this", "grazie", "thanks",
}

# ── Prompt Tier ──────────────────────────────────────────────────
_PROMPT_TIERS = {
    "lite":     "system_prompt_lite.md",
    "standard": "system_prompt_standard.md",
    "full":     "system_prompt.md",
}


def _detect_prompt_tier(message: str) -> str:
    """Auto-detect: 'lite', 'standard' or 'pro' based on content."""
    msg_lower = message.lower()
    has_lite = any(k in msg_lower for k in _LITE_KEYWORDS)
    has_complex = any(k in msg_lower for k in _COMPLEXITY_KEYWORDS)

    if has_complex:
        return "pro"  # pro = full prompt + Pro model
    if has_lite:
        return "lite"
    if len(message) > 200:
        return "pro"
    return "standard"


# Backward compatibility alias
_detect_task_complexity = _detect_prompt_tier


def _get_instructions_path(tier: str) -> str:
    """Returns the prompt file path for the requested tier."""
    filename = _PROMPT_TIERS.get(tier, "system_prompt_standard.md")
    return str(BASE_DIR / filename)


class AgnoFilter(logging.Filter):
    """Shows framework logs, excluding only noisy libraries."""
    def filter(self, record):
        name = record.name.lower()
        # Exclude only network/HTTP libraries
        noisy = ['httpx', 'uvicorn.access', 'httpcore', 'urllib3',
                 'charset_normalizer', 'asyncio']
        if any(n in name for n in noisy):
            return False
        msg = record.getMessage()
        # Exclude HTTP health check
        if '/health' in msg and 'HTTP' in msg:
            return False
        # Exclude uvicorn startup messages
        if 'uvicorn' in name and ('started' in msg.lower() or 'reload' in msg.lower()):
            return False
        return True


class SSELogHandler(logging.Handler):
    """Captures framework logs and queues them for SSE."""
    def __init__(self, level=logging.INFO):
        super().__init__(level=level)
        self.log_queue = queue.Queue()
        self.addFilter(AgnoFilter())

    def emit(self, record):
        try:
            msg = self.format(record)
            if msg:
                self.log_queue.put_nowait(msg)
        except Exception:
            pass

    def get_pending(self):
        records = []
        while not self.log_queue.empty():
            try:
                records.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        return records

# Load .env from Oracle's absolute path (always works, even when imported)
_oracle_env = Path(__file__).resolve().parent / ".env"
if _oracle_env.exists():
    load_dotenv(dotenv_path=_oracle_env, override=True)
else:
    load_dotenv()  # fallback: search in CWD

BASE_DIR = Path(__file__).parent.resolve()
# ── Oracle root (to import egida, 4th layer) ──
ORACLE_ROOT_DIR = BASE_DIR.parent
if str(ORACLE_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ORACLE_ROOT_DIR))
# ── Working directory: root folder where Oracle can operate ──
#    Dynamic: ORACLE_ROOT_DIR.parent (e.g., D:\Work\RUI_Software)
#    This way Oracle can access sibling projects (archimede, egida, penelope)
WORK_ROOT = ORACLE_ROOT_DIR.parent.resolve()
print(f"[Oracle] ORACLE_ROOT_DIR: {ORACLE_ROOT_DIR}")
print(f"[Oracle] BASE_DIR (Oracle): {BASE_DIR}")
print(f"[Oracle] WORK_ROOT (operating area): {WORK_ROOT}")

# ── API Connectivity Check ──────────────────────────────────────
def _check_api_connectivity():
    """Verifies connectivity with the configured provider's API."""
    api_key = os.getenv("API_KEY")
    base_url = os.getenv("API_BASE_URL", "")
    model_id = os.getenv("MODEL_ID", "")
    provider = os.getenv("MODEL_PROVIDER", "openai")

    if not api_key:
        print("[DIAG] API_KEY not set in .env — copy .env.example to .env")
        return

    import httpx

    # Determine the correct URL for model listing
    # Most OpenAI-compatible providers use {base_url}/models
    # OpenRouter: https://openrouter.ai/api/v1/models
    # If base_url already contains /chat/completions, strip it
    models_url = base_url.rstrip("/") if base_url else ""
    for suffix in ["/chat/completions", "/v1/chat/completions", "/completions"]:
        if models_url.endswith(suffix):
            models_url = models_url[: -len(suffix)]
            break
    
    # If no base_url, use the provider's default
    if not models_url:
        provider_defaults = {
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "deepseek": "https://api.deepseek.com",
            "together": "https://api.together.xyz/v1",
            "groq": "https://api.groq.com/openai/v1",
            "fireworks": "https://api.fireworks.ai/inference/v1",
            "perplexity": "https://api.perplexity.ai",
            "xai": "https://api.x.ai/v1",
        }
        models_url = provider_defaults.get(provider, "")

    if not models_url:
        print(f"[DIAG] API_BASE_URL not configured for '{provider}' — skipping check")
        return

    # Ensure URL ends with /v1 or similar before adding /models
    test_url = f"{models_url.rstrip('/')}/models"
    print(f"[DIAG] Connecting to {test_url} ...", end=" ", flush=True)
    try:
        r = httpx.get(
            test_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        if r.status_code == 200:
            models = [m["id"] for m in r.json().get("data", [])]
            if model_id and model_id in models:
                print(f"OK (model '{model_id}' found)")
            elif models:
                print(f"OK ({len(models)} models available)")
                if model_id:
                    print(f"      → model '{model_id}' not in list. Check MODEL_ID")
            else:
                print("OK (no models listed)")
        else:
            print(f"HTTP {r.status_code}")
            msgs = {401: "API_KEY invalid or expired", 402: "Insufficient credit",
                    404: "Endpoint /models not found. Check API_BASE_URL in .env"}
            print(f"      → {msgs.get(r.status_code, r.text[:120])}")
    except httpx.ConnectError:
        print("unreachable")
        print(f"      → Check Internet connection and API_BASE_URL in .env")
    except Exception as e:
        print(f"error: {e}")


from agno.agent import Agent # type: ignore
from agno.exceptions import ModelProviderError # type: ignore
from agno.tools.coding import CodingTools # type: ignore
from agno.tools.workspace import Workspace # type: ignore
from agno.db.sqlite import SqliteDb # type: ignore
from agno.os import AgentOS # type: ignore

from model_factory import create_model, get_provider_info
from llm_logger import set_caller_tag, clear_caller_tag

from fastapi import Request, FastAPI, Depends, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Annotated as Annotated_FastAPI
from pydantic import BaseModel, Field

# ── Tool Repository Bootstrap ─────────────────────────────────
try:
    sys.path.insert(0, str(BASE_DIR))
    from workspace.tool_repository import ToolRepository

    _tool_repo = ToolRepository()
    _repo_summary = _tool_repo.get_summary()
    if _repo_summary["total_tools"] > 0:
        print(f"[Oracle Bootstrap] Tool Repository: {_repo_summary['total_tools']} tools available")
        _tool_repo.write_index()
except Exception as _e:
    print(f"[Oracle Bootstrap] Tool Repository init: {_e}")

# ── Tool Lifecycle Bootstrap ──────────────────────────────────
try:
    sys.path.insert(0, str(BASE_DIR))
    from workspace.tool_lifecycle import ToolLifecycleManager

    _lifecycle = ToolLifecycleManager()
    _orphans = _lifecycle.scan_and_register_orphans()
    if _orphans:
        print(f"[Oracle Bootstrap] Registered {len(_orphans)} orphan tools (ex: {_orphans[0]})")
    _expired = _lifecycle.cleanup_expired()
    if _expired:
        print(f"[Oracle Bootstrap] Cleaned up {len(_expired)} expired tools")
except Exception as _e:
    print(f"[Oracle Bootstrap] Lifecycle init: {_e}")

# ── ImmunityGuardian Bootstrap ──────────────────────────────
try:
    from tools.immunity_guardian import ImmunityGuardian

    _immunity = ImmunityGuardian()
    print(f"[Oracle Bootstrap] ImmunityGuardian active — session {_immunity.get_session_id()}")
    print(f"[Oracle Bootstrap]   Pattern: {len(_immunity.INJECTION_PATTERNS)} injection + "
          f"{len(_immunity.LEAK_PATTERNS)} leak + {len(_immunity.JAILBREAK_PATTERNS)} jailbreak")

    # Registra l'immunity tool nel lifecycle
    try:
        _lifecycle.register(
            file_path="tools/immunity_guardian.py",
            purpose="Runtime security guardian for the Oracle agent",
            tool_type="persistent",
            depends_on=["tools/immunity_config.json"],
        )
    except Exception:
        pass  # Già registrato

except Exception as _e:
    print(f"[Oracle Bootstrap] ImmunityGuardian init: {_e}")
    _immunity = None  # Fallback: unprotected system

# ── Oracle Orchestrator Bootstrap ────────────────────────────
try:
    from tools.oracle_orchestrator import OracleOrchestrator, OrchestratorConfig

    _oracle_config = OrchestratorConfig(
        penelope_mode=os.getenv("PENELOPE_BRIDGE_MODE", "auto"),
        archimede_api_url=os.getenv("ARCHIMEDE_API_URL", "http://localhost:8001"),
        use_oracle_protocol=True,
        identity_enabled=True,
    )
    _oracle = OracleOrchestrator(_oracle_config)
    print(f"[Oracle Bootstrap] Oracle Orchestrator active")
    print(f"[Oracle Bootstrap]   Penelope mode: {_oracle.config.penelope_mode}")
    print(f"[Oracle Bootstrap]   Archimede API: {_oracle.config.archimede_api_url}")
except Exception as _e:
    print(f"[Oracle Bootstrap] Oracle Orchestrator init: {_e}")
    _oracle = None

# ── ChunkFilter Bootstrap (Modello-figlio) ─────────────────────
try:
    from tools.chunk_filter import get_pipeline as get_chunk_filter_pipeline

    _chunk_filter_pipeline = get_chunk_filter_pipeline()
    _cf_stats = _chunk_filter_pipeline.get_stats()
    if _cf_stats["pipeline_ready"]:
        print(f"[Oracle Bootstrap] ChunkFilter active — {_cf_stats['filter']['model']} "
              f"(threshold={_cf_stats['filter']['threshold']:.2f})")
    else:
        print(f"[Oracle Bootstrap] ChunkFilter not ready — fallback to no-filter")
except Exception as _e:
    print(f"[Oracle Bootstrap] ChunkFilter init: {_e}")
    _chunk_filter_pipeline = None

import httpx

# ── Dynamic Model Initialization ─────────────────────────────────
# The model provider is configured in .env via MODEL_PROVIDER.
# Supported providers: openai, deepseek, openrouter, google, anthropic,
# ollama, together, groq, mistral, cohere, and many more.
#
# Each call to create_model() reads MODEL_PROVIDER, MODEL_ID / MODEL_PRO_ID,
# API_KEY, API_BASE_URL, MAX_TOKENS, REQUEST_TIMEOUT from .env
# and dynamically imports the correct agno model class.

_provider = os.getenv("MODEL_PROVIDER", "openai")
_provider_info = get_provider_info(_provider)

print(f"[Oracle] Model provider: {_provider_info['display_name']} "
      f"({'native' if _provider_info.get('native') else 'OpenAI-compatible'})")

model = create_model(provider=_provider, is_pro=False)
model_pro = create_model(provider=_provider, is_pro=True)

# ── API Security Modules ────────────────────────────────────────
try:
    from api.auth import (
        get_current_active_user,
        get_current_admin_user,
        UserInDB,
        User,
        user_db,
        router as auth_router,
    )
    from api.security import (
        get_settings,
        apply_security_middleware,
        Settings,
    )
    from api.rate_limit import (
        rate_limiter,
        apply_rate_limit_middleware,
        RateLimitExceededException,
    )
    API_SECURITY_AVAILABLE = True
except Exception as e:
    logger = logging.getLogger(__name__)
    logger.warning(f"[Oracle] API Security modules not available: {e}")
    API_SECURITY_AVAILABLE = False

    async def get_current_active_user():
        return None
    async def get_current_admin_user():
        return None

    class UserInDB:
        pass
    class User:
        pass

# ── Override auth when REQUIRE_AUTHENTICATION is disabled ────────
REQUIRE_AUTHENTICATION = os.getenv("REQUIRE_AUTHENTICATION", "false").lower() == "true"

if not REQUIRE_AUTHENTICATION and API_SECURITY_AVAILABLE:
    # Replace real auth dependencies with no-op versions
    _real_get_user = get_current_active_user
    async def get_current_active_user():
        return None
    async def get_current_admin_user():
        return None

def _select_model(tier: str, message: str = ""):
    """Selects the model based on tier and message content.

    Args:
        tier: "flash", "pro", "auto" (default: from .env MODEL_TIER)
        message: request text for auto-detection

    Returns:
        Appropriate model instance (depends on configured provider).
    """
    effective_tier = tier or os.getenv("MODEL_TIER", "auto")

    if effective_tier == "pro":
        return model_pro
    if effective_tier == "flash":
        return model
    # auto: detection based on message content
    prompt_tier = _detect_prompt_tier(message)
    return model_pro if prompt_tier == "pro" else model

# ── Diagnostic API ──────────────────────────────────────────────
_check_api_connectivity()

# ── Initial Info Builder (path dinamici) ─────────────────────────
def _build_initial_info() -> str:
    """'Initial info' block with real paths, injected at runtime."""
    main_files = ("coding_agent.py, cli.py, system_prompt.md, .env, .env.example, "
                  "coding_agent.db, index.html, oracle.bat, CONSTITUTION.md")
    return (f"\n## 13. Initial info\n\n"
            f"Workspace root: {ORACLE_ROOT_DIR}. Main files: {main_files}.\n"
            f"Working area (tools): {WORK_ROOT}.")

_INITIAL_INFO_LINES = _build_initial_info().split("\n")

# ── Agent Factory ────────────────────────────────────────────────
_instructions_text = (BASE_DIR / "system_prompt.md").read_text(encoding="utf-8")
_agent_kwargs = dict(
    description="Pure coding agent — reads, writes, edits, searches, and runs code.",
    instructions=_instructions_text.strip().split("\n") + _INITIAL_INFO_LINES,
    tools=[
        CodingTools(
            base_dir=str(WORK_ROOT),
            all=True,
            shell_timeout=int(os.getenv("SHELL_TIMEOUT", "30")),
        ),
        Workspace(
            root=str(WORK_ROOT),
            allowed=["read", "list", "search", "shell"],
        ),
    ],
    enable_agentic_memory=True,
    add_history_to_context=True,
    num_history_runs=1,
    markdown=True,
)

coding_agent = Agent(
    name="Oracle",
    model=model,
    db=SqliteDb(db_file=str(BASE_DIR / "coding_agent.db")),
    **_agent_kwargs,
)

coding_agent_pro = Agent(
    name="Oracle-Pro",
    model=model_pro,
    db=SqliteDb(db_file=str(BASE_DIR / "coding_agent.db")),
    **_agent_kwargs,
)


def _get_agent(tier: str = None, message: str = "") -> Agent:
    """Returns the appropriate agent based on tier and message.
    Automatically sets the right instructions based on the prompt tier."""
    selected = _select_model(tier, message)
    agent = coding_agent_pro if selected is model_pro else coding_agent

    # Determina il prompt tier
    prompt_tier = "full"
    effective_tier = tier or os.getenv("MODEL_TIER", "auto")
    if effective_tier == "pro":
        prompt_tier = "full"
    elif effective_tier == "flash":
        prompt_tier = _detect_prompt_tier(message)
        # flash model can use lite or standard (not full)
        if prompt_tier == "pro":
            prompt_tier = "standard"
    else:  # auto
        prompt_tier = _detect_prompt_tier(message)
        # Lite tasks get lite prompt, Pro tasks get full + Pro model
        if prompt_tier == "pro":
            prompt_tier = "full"
        # standard stays standard

    # Carica e imposta le istruzioni del tier (con path dinamici)
    try:
        instructions_path = _get_instructions_path(prompt_tier)
        tier_text = open(instructions_path, "r", encoding="utf-8").read().strip()
        agent.instructions = tier_text.split("\n") + _INITIAL_INFO_LINES
    except Exception:
        pass  # fallback: keeps already-set instructions

    return agent

agent_os = AgentOS(
    agents=[coding_agent, coding_agent_pro],
    tracing=False,
)

app = agent_os.get_app()

# ── Apply Security Middleware ────────────────────────────────────────
if API_SECURITY_AVAILABLE:
    # Apply security middleware
    app = apply_security_middleware(app)
    
    # Apply rate limiting middleware
    try:
        app = apply_rate_limit_middleware(app)
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"[Oracle] Rate limiting middleware failed: {e}")
    
    # Include auth router
    try:
        app.include_router(auth_router, prefix="/api/auth", tags=["authentication"])
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"[Oracle] Failed to include auth router: {e}")
    
    # Apply CORS middleware (fallback if not applied by security module)
    try:
        settings = get_settings()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost", "http://localhost:8000", 
                          "http://127.0.0.1", "http://127.0.0.1:8000"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"[Oracle] CORS middleware failed: {e}")
else:
    # Fallback: apply basic CORS if security modules not available
    try:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost", "http://localhost:8000",
                          "http://127.0.0.1", "http://127.0.0.1:8000"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    except Exception:
        pass


# ── Model info ────────────────────────────────────────────────────
def _get_active_model_info(tier: str = None, message: str = "") -> dict:
    """Returns info about the active model for a request."""
    selected = _select_model(tier, message)
    model_id = getattr(selected, "id", "unknown")
    is_pro = selected is model_pro
    effective_tier = tier or os.getenv("MODEL_TIER", "auto")

    # Determine the effective prompt tier
    if effective_tier == "auto":
        pt = _detect_prompt_tier(message)
    elif effective_tier == "pro":
        pt = "full"
    else:
        pt = _detect_prompt_tier(message)
        if pt == "pro":
            pt = "standard"

    provider = os.getenv("MODEL_PROVIDER", "openai")
    return {
        "model_id": model_id,
        "model_name": f"{provider.capitalize()} {'Pro' if is_pro else 'Flash'}",
        "provider": provider,
        "tier": "pro" if is_pro else "flash",
        "tier_mode": effective_tier,
        "prompt_tier": pt,
    }


# ── Immunity Integration Functions ──────────────────────────

_immunity_guard = None  # Initialized in bootstrap above

def _get_immunity():
    """Returns the global ImmunityGuardian instance."""
    global _immunity_guard
    if _immunity_guard is None:
        try:
            from tools.immunity_guardian import ImmunityGuardian
            _immunity_guard = ImmunityGuardian()
        except Exception:
            return None
    return _immunity_guard


def immunity_check_message(message: str, source: str = "USER"):
    """
    Checks a message with ImmunityGuardian.
    If blocked, returns an error message.
    If OK, returns None.
    """
    guard = _get_immunity()
    if guard is None:
        return None  # Immunity not available → pass through

    result = guard.check_input(message, source=source)
    if result["status"] == "BLOCKED":
        code = result.get("code", "SECURITY_BLOCK")
        guard.log_attempt(code, f"Input blocked from {source}: {message[:100]}")
        return f"[SECURITY BLOCKED] Request blocked by security system ({code})."
    return None


def immunity_sanitize(text: str) -> str:
    """Sanitizza output con ImmunityGuardian."""
    guard = _get_immunity()
    if guard is None:
        return text
    return guard.sanitize_output(text)


def _inject_chunk_context(message: str) -> str:
    """
    [EXPERIMENTAL] Filters context chunks via ChunkFilter (child model).

    TOOL REGISTERED as 'chunk_filter' in Tool Repository.
    NOT used automatically — explicit call only when needed.

    Usage:
        context = _inject_chunk_context(message)
        if context:
            message = context + "\\n\\n" + message

    If ChunkFilter is not available, returns empty string.
    """
    if _chunk_filter_pipeline is None or not _chunk_filter_pipeline.is_ready():
        return ""

    try:
        context = _chunk_filter_pipeline.process(query=message)
        if context and len(context) > 10:
            return context
    except Exception as e:
        logging.debug(f"[ChunkFilter] Error: {e}")

    return ""


def immunity_sanitize_response(agent_result):
    """
    Sanitizes the content of the agent's response.
    Modifies the result object in-place if it has a 'content' attribute.
    """
    guard = _get_immunity()
    if guard is None:
        return agent_result

    content = getattr(agent_result, "content", None)
    if content:
        sanitized = guard.sanitize_output(content)
        if sanitized != content:
            agent_result.content = sanitized
    return agent_result


# ── Chat API Endpoints (con Immunity hooks) ─────────────────


def _get_optional_user(
    request: Request,
    current_user: Optional[UserInDB] = None
) -> Optional[UserInDB]:
    """Get authenticated user if authentication is enabled."""
    if not REQUIRE_AUTHENTICATION or not API_SECURITY_AVAILABLE:
        return None
    if current_user is not None:
        return current_user
    return None


@app.post("/api/chat")
async def chat_api(
    request: Request,
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_active_user)
    ] = None
):
    """Endpoint chat non-streaming: POST {"message": "...", "session_id": "...", "model_tier": "auto|flash|pro"}"""
    # Check authentication if required
    user = _get_optional_user(request, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", None)
    model_tier = body.get("model_tier", None)

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    # → IMMUNITY CHECK: user input
    block_msg = immunity_check_message(message, source="USER")
    if block_msg:
        return JSONResponse({
            "content": block_msg,
            "session_id": session_id,
            "run_id": None,
            "model": _get_active_model_info(model_tier, message),
            "immunity_block": True,
        })

    agent = _get_agent(model_tier, message)

    try:
        set_caller_tag("main_loop.chat")
        result = agent.run(message, session_id=session_id)
        clear_caller_tag()
        # → IMMUNITY SANITIZE: output
        result = immunity_sanitize_response(result)
        return JSONResponse({
            "content": result.content if hasattr(result, "content") else str(result),
            "session_id": result.session_id if hasattr(result, "session_id") else session_id,
            "run_id": result.run_id if hasattr(result, "run_id") else None,
            "model": _get_active_model_info(model_tier, message),
        })
    except ModelProviderError as e:
        logging.error(f"[API] ModelProviderError: status={e.status_code} message={e.message} model={e.model_id}")
        return JSONResponse({"error": e.message, "status_code": e.status_code}, status_code=e.status_code)
    except Exception as e:
        logging.error(f"[API] Internal error: {type(e).__name__}: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/chat/stream")
async def chat_stream(
    message: str,
    session_id: str = None,
    model_tier: str = None,
    request: Request = None,
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_active_user)
    ] = None
): # type: ignore
    """Endpoint chat con streaming SSE: GET /api/chat/stream?message=...&session_id=...&model_tier=auto|flash|pro"""
    # Check authentication if required
    user = _get_optional_user(request, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def event_generator():
        last_session_id = session_id
        last_run_id = None
        content_buffer = ""

        # Capture Agno framework logs via root logger + filter
        # NOTE: root logger defaults to WARNING, but tool INFO messages
        # come from child loggers (which have their own level). We need to
        # lower the root logger to INFO to intercept them.
        log_capture = SSELogHandler(logging.INFO)
        log_capture.setFormatter(logging.Formatter('%(message)s'))
        _root = logging.getLogger()
        _saved_root_level = _root.level
        _root.setLevel(logging.INFO)
        _root.addHandler(log_capture)

        def flush_logs():
            for rec in log_capture.get_pending():
                yield f"data: {json.dumps({'type': 'log', 'data': {'text': rec, 'type': 'info'}})}\n\n"

        # → IMMUNITY CHECK: user input (streaming)
        block_msg = immunity_check_message(message, source="USER")
        if block_msg:
            yield f"data: {json.dumps({'type': 'model', 'data': _get_active_model_info(model_tier, message)})}\n\n"
            yield f"data: {json.dumps({'type': 'content', 'data': ''})}\n\n"
            yield f"data: {json.dumps({'type': 'log', 'data': {'text': '🛡️ Immunity: request blocked', 'type': 'info'}})}\n\n"
            yield f"data: {json.dumps({'type': 'content', 'data': block_msg})}\n\n"
            return

        # Select the model and send info to UI
        agent = _get_agent(model_tier, message)
        model_info = _get_active_model_info(model_tier, message)
        yield f"data: {json.dumps({'type': 'model', 'data': model_info})}\n\n"

        # → No automatic chunk filter — experimental tool, explicit call only

        # Send a start event immediately to make the 3 dots disappear
        yield f"data: {json.dumps({'type': 'content', 'data': ''})}\n\n"

        try:
            set_caller_tag("main_loop.chat_stream")
            stream = agent.arun(
                message,
                session_id=session_id,
                stream=True,
            )

            async for event in stream:
                if request and await request.is_disconnected():
                    break

                event_type = getattr(event, "event", "unknown")

                # Flush framework logs BEFORE each event
                for log_line in flush_logs():
                    yield log_line

                if event_type == "RunContent":
                    chunk = getattr(event, "content", "") or ""
                    if chunk:
                        content_buffer += chunk
                        # → IMMUNITY SANITIZE output in tempo reale
                        safe_chunk = immunity_sanitize(chunk)
                        yield f"data: {json.dumps({'type': 'content', 'data': safe_chunk})}\n\n"

                elif event_type == "ToolCallStarted":
                    tool = getattr(event, "tool", None)
                    tool_name = tool.tool_name if tool else ""
                    if tool_name:
                        tool_args = ""
                        if tool:
                            raw_args = getattr(tool, "tool_args", None)
                            if raw_args:
                                tool_args = json.dumps(raw_args) if isinstance(raw_args, dict) else str(raw_args)
                            else:
                                tool_args = str(getattr(tool, "arguments", "") or getattr(tool, "args", "") or "")
                            if not tool_args:
                                tool_args = str(getattr(tool, "input", "") or getattr(tool, "query", "") or getattr(tool, "command", "") or "")
                        yield f"data: {json.dumps({'type': 'tool_start', 'data': {'name': tool_name, 'args': str(tool_args)[:500]}})}\n\n"
                        log_text = f"▶ {tool_name}"
                        log_detail = str(tool_args)[:200] if tool_args else ""
                        if log_detail:
                            log_text += f"  {log_detail}"
                        yield f"data: {json.dumps({'type': 'log', 'data': {'text': log_text, 'type': 'start'}})}\n\n"

                elif event_type == "ToolCallCompleted":
                    tool = getattr(event, "tool", None)
                    tool_name = tool.tool_name if tool else ""
                    if tool_name:
                        tool_result = ""
                        if tool:
                            tool_result = getattr(tool, "result", "") or getattr(tool, "output", "") or getattr(tool, "results", "") or ""
                            tool_result = str(tool_result)
                            if len(tool_result) > 300:
                                tool_result = tool_result[:300] + "..."
                        yield f"data: {json.dumps({'type': 'tool_end', 'data': {'name': tool_name, 'result': tool_result}})}\n\n"
                        log_text = f"✓ {tool_name}"
                        log_detail = tool_result[:200] if tool_result else ""
                        if log_detail:
                            log_text += f"  {log_detail}"
                        yield f"data: {json.dumps({'type': 'log', 'data': {'text': log_text, 'type': 'done'}})}\n\n"

                sid = getattr(event, "session_id", None)
                if sid:
                    last_session_id = sid
                rid = getattr(event, "run_id", None)
                if rid:
                    last_run_id = rid

                if event_type == "RunError":
                    error_msg = getattr(event, "content", "Unknown error") or "Unknown error"
                    yield f"data: {json.dumps({'type': 'error', 'data': error_msg})}\n\n"
                    yield f"data: {json.dumps({'type': 'log', 'data': {'text': f'✗ {error_msg[:200]}', 'type': 'error'}})}\n\n"
                    break

                # Flush logs also AFTER each event
                for log_line in flush_logs():
                    yield log_line

        except asyncio.CancelledError:
            pass
        except ModelProviderError as e:
            error_msg = e.message or str(e)
            logging.error(f"[Stream] ModelProviderError: status={e.status_code} message={e.message} model={e.model_id}")
            yield f"data: {json.dumps({'type': 'log', 'data': {'text': f'⚠ [{e.status_code}] {error_msg[:200]}', 'type': 'error'}})}\n\n"
            yield f"data: {json.dumps({'type': 'error', 'data': f'API Error ({e.status_code}): {error_msg}'})}\n\n"
        except Exception as e:
            error_msg = str(e)
            logging.error(f"[Stream] Error: {type(e).__name__}: {error_msg}", exc_info=True)
            yield f"data: {json.dumps({'type': 'log', 'data': {'text': f'⚠ {error_msg[:200]}', 'type': 'error'}})}\n\n"
            if "incomplete chunked read" in error_msg.lower() or "peer closed connection" in error_msg.lower():
                yield f"data: {json.dumps({'type': 'retry', 'data': 'Connection interrupted, retrying...'})}\n\n"
                try:
                    set_caller_tag("main_loop.chat_stream_retry")
                    stream2 = agent.arun(message, session_id=session_id, stream=True)
                    async for event in stream2:
                        if request and await request.is_disconnected():
                            break
                        event_type = getattr(event, "event", "unknown")
                        for log_line in flush_logs():
                            yield log_line
                        if event_type == "RunContent":
                            chunk = getattr(event, "content", "") or ""
                            if chunk:
                                yield f"data: {json.dumps({'type': 'content', 'data': chunk})}\n\n"
                        elif event_type == "ToolCallStarted":
                            tool = getattr(event, "tool", None)
                            tool_name = tool.tool_name if tool else ""
                            if tool_name:
                                tool_args = ""
                                if tool:
                                    raw_args = getattr(tool, "tool_args", None)
                                    if raw_args:
                                        tool_args = json.dumps(raw_args) if isinstance(raw_args, dict) else str(raw_args)
                                    else:
                                        tool_args = str(getattr(tool, "arguments", "") or getattr(tool, "args", "") or "")
                                    if not tool_args:
                                        tool_args = str(getattr(tool, "input", "") or getattr(tool, "query", "") or getattr(tool, "command", "") or "")
                                yield f"data: {json.dumps({'type': 'tool_start', 'data': {'name': tool_name, 'args': str(tool_args)[:500]}})}\n\n"
                                log_text = f"▶ {tool_name}"
                                log_detail = str(tool_args)[:200] if tool_args else ""
                                if log_detail:
                                    log_text += f"  {log_detail}"
                                yield f"data: {json.dumps({'type': 'log', 'data': {'text': log_text, 'type': 'start'}})}\n\n"
                        elif event_type == "ToolCallCompleted":
                            tool = getattr(event, "tool", None)
                            tool_name = tool.tool_name if tool else ""
                            if tool_name:
                                tool_result = str(getattr(tool, "result", "") or getattr(tool, "output", "") or "")[:300]
                                yield f"data: {json.dumps({'type': 'tool_end', 'data': {'name': tool_name, 'result': tool_result}})}\n\n"
                                log_text = f"✓ {tool_name}"
                                log_detail = tool_result[:200] if tool_result else ""
                                if log_detail:
                                    log_text += f"  {log_detail}"
                                yield f"data: {json.dumps({'type': 'log', 'data': {'text': log_text, 'type': 'done'}})}\n\n"
                        elif event_type == "RunError":
                            error_msg2 = getattr(event, "content", "Unknown error") or "Unknown error"
                            yield f"data: {json.dumps({'type': 'error', 'data': error_msg2})}\n\n"
                            yield f"data: {json.dumps({'type': 'log', 'data': {'text': f'✗ {error_msg2[:200]}', 'type': 'error'}})}\n\n"
                            break
                        for log_line in flush_logs():
                            yield log_line
                        sid = getattr(event, "session_id", None)
                        if sid:
                            last_session_id = sid
                        rid = getattr(event, "run_id", None)
                        if rid:
                            last_run_id = rid
                except ModelProviderError as e2:
                    logging.error(f"[Stream-Retry] ModelProviderError: status={e2.status_code} message={e2.message}")
                    yield f"data: {json.dumps({'type': 'error', 'data': f'API Error after retry ({e2.status_code}): {e2.message}'})}\n\n"
                except Exception as e2:
                    logging.error(f"[Stream-Retry] Error: {type(e2).__name__}: {e2}", exc_info=True)
                    yield f"data: {json.dumps({'type': 'error', 'data': f'Error after retry: {str(e2)}'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'error', 'data': error_msg})}\n\n"
        finally:
            clear_caller_tag()
            _root.removeHandler(log_capture)
            _root.setLevel(_saved_root_level)
            for log_line in flush_logs():
                yield log_line
            if last_session_id or last_run_id:
                yield f"data: {json.dumps({'type': 'done', 'session_id': last_session_id, 'run_id': last_run_id})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/health")
@app.get("/health")
async def health_check():
    """Health check endpoint - no authentication required."""
    return JSONResponse({
        "status": "healthy",
        "app_name": "Oracle",
        "version": "1.0.0",
        "auth_enabled": REQUIRE_AUTHENTICATION and API_SECURITY_AVAILABLE,
        "security_available": API_SECURITY_AVAILABLE,
    })


@app.get("/api/chunk-filter")
async def chunk_filter_stats():
    """Returns ChunkFilter statistics (child model)."""
    if _chunk_filter_pipeline is None:
        return JSONResponse({"enabled": False, "error": "ChunkFilter not initialized"})
    return JSONResponse(_chunk_filter_pipeline.get_stats())


@app.get("/api/model")
async def get_model_info(
    current_user: Annotated_FastAPI[
        Optional[UserInDB],
        Depends(get_current_active_user)
    ] = None
):
    """Returns info about the current model and configuration."""
    # Check authentication if required
    user = _get_optional_user(None, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    provider = os.getenv("MODEL_PROVIDER", "openai")
    current_tier = os.getenv("MODEL_TIER", "auto")
    return JSONResponse({
        "provider": provider,
        "flash": {
            "model_id": os.getenv("MODEL_ID", ""),
            "model_name": f"{provider.capitalize()} Flash",
        },
        "pro": {
            "model_id": os.getenv("MODEL_PRO_ID", ""),
            "model_name": f"{provider.capitalize()} Pro",
        },
        "current_tier": current_tier,
        "current": _get_active_model_info(),
    })


@app.get("/api/chat/sessions")
async def list_sessions(
    limit: int = 20,
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_active_user)
    ] = None
):
    """Elenca le sessioni recenti."""
    # Check authentication if required
    user = _get_optional_user(None, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    try:
        sessions = coding_agent.get_sessions(limit=limit)
        result = []
        for s in (sessions or []):
            result.append({
                "session_id": getattr(s, "session_id", ""),
                "agent_id": getattr(s, "agent_id", ""),
                "created_at": str(getattr(s, "created_at", "")),
                "name": getattr(s, "name", ""),
            })
        return JSONResponse({"sessions": result})
    except Exception as e:
        return JSONResponse({"sessions": [], "error": str(e)})


# ── Constitution API ──────────────────────────────────────────────
_CONSTITUTION_ENFORCER = None

def _get_constitution():
    global _CONSTITUTION_ENFORCER
    if _CONSTITUTION_ENFORCER is None:
        from tools.constitution import ConstitutionEnforcer
        _CONSTITUTION_ENFORCER = ConstitutionEnforcer()
    return _CONSTITUTION_ENFORCER


@app.get("/api/constitution/articles")
async def get_constitution_articles(
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_active_user)
    ] = None
):
    """Restituisce gli articoli della costituzione."""
    # Check authentication if required
    user = _get_optional_user(None, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    from tools.constitution import ARTICLES
    return JSONResponse({"articles": [{"id": k, "text": v} for k, v in sorted(ARTICLES.items())]})


@app.get("/api/constitution/pending-tools")
async def get_pending_tools(
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_active_user)
    ] = None
):
    """List of tools awaiting approval."""
    # Check authentication if required
    user = _get_optional_user(None, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    enforcer = _get_constitution()
    pending = enforcer.tool_registry.get_pending()
    return JSONResponse({"pending": pending})


@app.get("/api/constitution/all-tools")
async def get_all_tools(
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_active_user)
    ] = None
):
    """List of all registered tools."""
    # Check authentication if required
    user = _get_optional_user(None, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    enforcer = _get_constitution()
    all_tools = enforcer.tool_registry.get_all()
    return JSONResponse({"tools": all_tools})


@app.post("/api/constitution/approve-tool")
async def approve_tool(
    request: Request,
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_admin_user)
    ] = None
):
    """Approve a tool (admin only)."""
    # Check authentication and admin if required
    user = _get_optional_user(request, current_user)
    if REQUIRE_AUTHENTICATION:
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin access required"
            )
    
    body = await request.json()
    tool_id = body.get("tool_id", "")
    if not tool_id:
        return JSONResponse({"error": "tool_id required"}, status_code=400)
    enforcer = _get_constitution()
    result = enforcer.tool_registry.approve(tool_id)
    return JSONResponse(result)


@app.post("/api/constitution/reject-tool")
async def reject_tool(
    request: Request,
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_admin_user)
    ] = None
):
    """Reject a tool (admin only)."""
    # Check authentication and admin if required
    user = _get_optional_user(request, current_user)
    if REQUIRE_AUTHENTICATION:
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin access required"
            )
    
    body = await request.json()
    tool_id = body.get("tool_id", "")
    if not tool_id:
        return JSONResponse({"error": "tool_id required"}, status_code=400)
    enforcer = _get_constitution()
    result = enforcer.tool_registry.reject(tool_id)
    return JSONResponse(result)


@app.get("/api/constitution/pending-confirmations")
async def get_pending_confirmations(
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_active_user)
    ] = None
):
    """Lista delle conferme pending."""
    # Check authentication if required
    user = _get_optional_user(None, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    enforcer = _get_constitution()
    pending = enforcer.confirmation.list_pending()
    return JSONResponse({"confirmations": pending})


@app.post("/api/constitution/confirm")
async def confirm_action(
    request: Request,
    current_user: Annotated_FastAPI[
        Optional[UserInDB], 
        Depends(get_current_active_user)
    ] = None
):
    """Conferma un'azione."""
    # Check authentication if required
    user = _get_optional_user(request, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    body = await request.json()
    conf_id = body.get("confirmation_id", "")
    if not conf_id:
        return JSONResponse({"error": "confirmation_id required"}, status_code=400)
    enforcer = _get_constitution()
    result = enforcer.confirmation.confirm(conf_id)
    return JSONResponse(result)


# ════════════════════════════════════════════════════════════════
#  PROMETEO UNIFIED QUERY ENDPOINT
# ════════════════════════════════════════════════════════════════

class QueryRequest(BaseModel):
    query: str = Field(..., description="Natural language query")
    session_id: Optional[str] = Field(None, description="Session ID")
    mode: str = Field("auto", description="Mode: auto | graph | coding | osint | identity")
    context: Optional[str] = Field(None, description="Additional context")

class QueryResponse(BaseModel):
    content: str = Field(..., description="Processed response")
    domains: list[str] = Field(default_factory=list, description="Domains involved")
    sources: list[str] = Field(default_factory=list, description="Layers used")
    session_id: Optional[str] = None
    model: dict = Field(default_factory=dict)
    egida_blocked: bool = False
    duration_sec: float = 0.0


@app.post("/api/query")
async def prometeo_query(
    request: Request,
    current_user: Annotated_FastAPI[
        Optional[UserInDB],
        Depends(get_current_active_user)
    ] = None
):
    """
    Oracle unified endpoint.
    
    Single entry point for all requests:
      - graph:     Penelope graph queries (photos, people, events)
      - coding:    code writing/refactoring/testing
      - osint:     OSINT, verification, web searches
      - identity:  personal identity
      - auto:      automatic routing via LLM
    
    Body:
        {
            "query": "find Angela's photos",
            "session_id": "sess_001",
            "mode": "auto",
            "context": "optional context"
        }
    """
    user = _get_optional_user(request, current_user)
    if REQUIRE_AUTHENTICATION and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    body = await request.json()
    query_text = body.get("query", "")
    session_id = body.get("session_id", None)
    mode = body.get("mode", "auto")
    context = body.get("context", "")

    if not query_text.strip():
        return JSONResponse({"error": "Query cannot be empty"}, status_code=400)

    # ── Map mode → forced domains ────────────────────────────
    force_domains = None
    if mode and mode != "auto":
        force_domains = [mode]

    # ── Use Oracle Orchestrator ──────────────────────────────
    if _oracle is None:
        return JSONResponse({
            "content": "Oracle Orchestrator not available. Use /api/chat for generic requests.",
            "domains": [],
            "sources": [],
            "session_id": session_id,
            "model": _get_active_model_info(),
            "egida_blocked": False,
            "duration_sec": 0.0,
        })

    try:
        t_start = time.time()
        result = _oracle.analyze(
            query=query_text,
            context=context or "",
            force_domains=force_domains,
        )
        duration = time.time() - t_start

        # Determine sources (layers used)
        sources = []
        for dr in result.domain_results:
            if dr.status == "success":
                domain_str = dr.domain.value
                if domain_str in ("graph",):
                    sources.append("archimede")
                elif domain_str in ("coding", "osint", "identity", "general", "hybrid"):
                    sources.append("oracle")
        sources = list(set(sources)) or ["oracle"]

        return QueryResponse(
            content=result.response,
            domains=[d.value for d in result.domains],
            sources=sources,
            session_id=session_id,
            model=_get_active_model_info(mode, query_text),
            egida_blocked=result.egida_blocked,
            duration_sec=round(duration, 2),
        )

    except Exception as e:
        logging.error(f"[Oracle Query] Error: {type(e).__name__}: {e}", exc_info=True)
        return JSONResponse({
            "content": f"Error during processing: {str(e)}",
            "domains": [],
            "sources": [],
            "session_id": session_id,
            "error": str(e),
        }, status_code=500)


@app.get("/api/query/health")
async def prometeo_health():
    """Oracle unified system health check."""
    from tools.penelope_bridge import PenelopeBridge

    status = {
        "orchestrator": _prometeo is not None,
        "layers": {
            "oracle": True,
            "archimede": False,
            "penelope": False,
        },
    }

    # Check Archimede/Penelope
    try:
        bridge = PenelopeBridge(mode=os.getenv("PENELOPE_BRIDGE_MODE", "auto"))
        if bridge.is_available():
            status["layers"]["archimede"] = True
            status["layers"]["penelope"] = True
    except Exception:
        pass

    return JSONResponse(status)


# ── Wrap ASGI app to serve CHORA UI at root ──────────────────────
# AgentOS registers its own GET / inside get_app(), which takes precedence.
# We wrap the ASGI protocol so GET / and /ui are caught before FastAPI.
from pathlib import Path as _Path
_chora_index_path = _Path(__file__).resolve().parent.parent / "index.html"

class _ChoraASGIWrapper:
    """ASGI wrapper: serves index.html at GET /, proxies /penelope/* to :5000."""
    def __init__(self, inner):
        self.inner = inner
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "GET":
            path = scope.get("path", "").rstrip("/") or "/"
            # Serve CHORA UI at root
            if path in ("/", "/ui"):
                from fastapi.responses import FileResponse, JSONResponse
                idx = _chora_index_path
                resp = FileResponse(str(idx)) if idx.exists() else \
                       JSONResponse({"error": "index.html not found"}, status_code=404)
                await resp(scope, receive, send)
                return
        # Proxy /penelope/* requests to localhost:5000
        if scope["type"] == "http" and scope["path"].startswith("/penelope"):
            import httpx
            target_path = scope["path"][len("/penelope"):] or "/"
            method = scope["method"]
            # Read request body if any
            body = b""
            if method in ("POST", "PUT", "PATCH"):
                more_body = True
                while more_body:
                    message = await receive()
                    body += message.get("body", b"")
                    more_body = message.get("more_body", False)
            # Build headers
            headers = {}
            for k, v in scope.get("headers", []):
                key = k.decode("latin-1").lower()
                if key not in ("host", "content-length", "transfer-encoding"):
                    headers[key] = v.decode("latin-1")
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.request(
                        method, "http://localhost:5000" + target_path,
                        headers=headers,
                        content=body,
                        params=scope.get("query_string", b"").decode() or None,
                    )
                # Send response back to browser
                resp_headers = [
                    (b"content-type", resp.headers.get("content-type", "application/json").encode()),
                    (b"content-length", str(len(resp.content)).encode()),
                    (b"access-control-allow-origin", b"*"),
                ]
                await send({
                    "type": "http.response.start",
                    "status": resp.status_code,
                    "headers": resp_headers,
                })
                await send({
                    "type": "http.response.body",
                    "body": resp.content,
                })
            except Exception as e:
                from fastapi.responses import JSONResponse
                err = JSONResponse({"error": str(e)}, status_code=502)
                await err(scope, receive, send)
            return
        await self.inner(scope, receive, send)

app = _ChoraASGIWrapper(app)  # type: ignore[assignment]

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Oracle - AI Coding Agent Server")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")),
                        help="Server port (default: %(default)s)")
    parser.add_argument("--host", type=str, default=os.getenv("HOST", "0.0.0.0"),
                        help="Server host (default: %(default)s)")
    parser.add_argument("--model-tier", type=str, default=None,
                        choices=["auto", "flash", "pro"],
                        help="Force a model tier for all requests (default: from .env)")
    parser.add_argument("--deep", action="store_true",
                        help="Scorciatoia per --model-tier=pro")
    args = parser.parse_args()

    if args.deep:
        args.model_tier = "pro"

    if args.model_tier:
        print(f"[Oracle] Model tier forced: {args.model_tier}")
        os.environ["MODEL_TIER"] = args.model_tier

    uvicorn.run(app, host=args.host, port=args.port)

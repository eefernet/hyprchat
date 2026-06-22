"""
HyprChat Configuration
Edit these values to match your homelab setup.
"""
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.abspath(os.getenv("HYPRCHAT_DATA_DIR") or os.path.join(PROJECT_ROOT, "data"))


def data_path(*parts: str) -> str:
    return os.path.join(DATA_DIR, *parts)

# ============================================================
# INFRASTRUCTURE IPs
# ============================================================
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
CODEBOX_URL = os.getenv("CODEBOX_URL", "http://127.0.0.1:8585")
OPENHANDS_URL = os.getenv("OPENHANDS_URL", "http://127.0.0.1:8586")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888")
N8N_URL = os.getenv("N8N_URL", "http://127.0.0.1:5678")
COMFYUI_URL = os.getenv("COMFYUI_URL", "")  # empty = image generation disabled
COMFYUI_WORKFLOW_PATH = os.getenv("COMFYUI_WORKFLOW_PATH", "")  # optional API-format workflow override
# Chat generate_image defaults (runtime-editable in Settings → Model & Generation).
# Empty checkpoint = use the built-in SDXL template as-is. When set, the chat
# tool loads the checkpoint's saved per-model preset (sampler/scheduler/cfg/
# steps/model type) and prepends the prompt prefix to the model's request.
IMAGE_CHAT_CHECKPOINT = os.getenv("IMAGE_CHAT_CHECKPOINT", "")
# Global saved-workflow name for chat generate_image. Empty = built-in SDXL
# template. A per-persona image profile workflow still takes precedence.
IMAGE_CHAT_WORKFLOW = os.getenv("IMAGE_CHAT_WORKFLOW", "")
IMAGE_CHAT_RESOLUTION = os.getenv("IMAGE_CHAT_RESOLUTION", "1024x1024")
IMAGE_CHAT_VAE = os.getenv("IMAGE_CHAT_VAE", "")  # empty = checkpoint's baked VAE
IMAGE_CHAT_PROMPT_PREFIX = os.getenv("IMAGE_CHAT_PROMPT_PREFIX", "")
IMAGE_CHAT_NEGATIVE = os.getenv("IMAGE_CHAT_NEGATIVE", "")
# Model that writes persona photo prompts (selfie-rescue compose tier) and
# Image Studio enhancements. Empty = the conversation's own chat model.
IMAGE_CHAT_COMPOSE_MODEL = os.getenv("IMAGE_CHAT_COMPOSE_MODEL", "")
STT_URL = os.getenv("STT_URL", "")  # empty = voice transcription disabled (OpenAI-compatible, e.g. Speaches)
TTS_URL = os.getenv("TTS_URL", "")  # empty = speech synthesis disabled (OpenAI-compatible, e.g. kokoro-fastapi)
STT_MODEL = os.getenv("STT_MODEL", "Systran/faster-distil-whisper-large-v3")
TTS_VOICE = os.getenv("TTS_VOICE", "af_heart")
N8N_WEBHOOK_PATH = os.getenv("N8N_WEBHOOK_PATH", "/webhook/execute-code")
N8N_RESEARCH_PATH = os.getenv("N8N_RESEARCH_PATH", "/webhook/deep-research")
HTTP_VERIFY_SSL = os.getenv("HTTP_VERIFY_SSL", "true").lower() == "true"
OUTBOUND_PROXY_URL = (
    os.getenv("HYPRCHAT_OUTBOUND_PROXY")
    or os.getenv("OUTBOUND_PROXY_URL")
    or os.getenv("WEB_FETCH_PROXY_URL")
    or ""
).strip()

# ============================================================
# SERVER
# ============================================================
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ============================================================
# DATABASE
# ============================================================
DATABASE_PATH = os.getenv("DATABASE_PATH", data_path("hyprchat.db"))

# ============================================================
# FILE STORAGE
# ============================================================
UPLOAD_DIR = os.getenv("UPLOAD_DIR", data_path("uploads"))
TOOLS_DIR = os.getenv("TOOLS_DIR", data_path("tools"))
KB_DIR = os.getenv("KB_DIR", data_path("knowledge_bases"))
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "250"))

# ============================================================
# SANDBOX — isolated dir for all tool-generated output files
# ============================================================
SANDBOX_DIR = os.getenv("SANDBOX_DIR", data_path("sandbox"))
SANDBOX_OUTPUTS_DIR = os.path.join(SANDBOX_DIR, "outputs")   # tool downloads/outputs → cleaned up
SANDBOX_VENV_DIR    = os.path.join(SANDBOX_DIR, "venv")      # Python venv for local tool execution
SANDBOX_WORKSPACE_DIR = os.path.join(SANDBOX_DIR, "workspace")  # temp working dir

# ============================================================
# SETTINGS FILE (persistent JSON for runtime-editable options)
# ============================================================
SETTINGS_PATH = os.getenv("SETTINGS_PATH", data_path("settings.json"))
CONNECTOR_SECRETS_PATH = os.getenv("CONNECTOR_SECRETS_PATH", data_path("connector_secrets.json"))
DEFAULT_SETTINGS = {
    "file_cleanup_days": 30,  # 0 = never clean
    "ollama_url": "",  # empty = use OLLAMA_URL from env/default
    "codebox_url": "",  # empty = use CODEBOX_URL from env/default
    "searxng_url": "",  # empty = use SEARXNG_URL from env/default
    "n8n_url": "",  # empty = use N8N_URL from env/default
    "comfyui_url": "",  # empty = use COMFYUI_URL from env (image generation disabled when both empty)
    "image_chat_checkpoint": "",  # empty = built-in SDXL template for chat image gen
    "image_chat_workflow": "",  # empty = built-in SDXL template; persona workflows still override
    "image_chat_resolution": "1024x1024",
    "image_chat_vae": "",  # empty = checkpoint's baked VAE
    "image_chat_prompt_prefix": "",
    "image_chat_negative": "",
    "image_chat_compose_model": "",  # empty = the conversation's chat model
    "stt_url": "",  # empty = use STT_URL from env (voice input disabled when both empty)
    "tts_url": "",  # empty = use TTS_URL from env (voice output disabled when both empty)
    "tts_voice": "af_heart",
    "ollama_scan_ssh_host": "",  # empty = derive host from the configured Ollama URL
    "ollama_scan_ssh_port": 22,
    "ollama_scan_ssh_user": "root",
    "ollama_scan_ssh_auth_mode": "key",  # key | password
    "ollama_scan_ssh_key_path": "",
    "ollama_scan_ssh_password": "",  # local settings only; never returned by /api/settings
    "rag": {
        "embed_model": "nomic-embed-text",
        "chunk_size": 500,
        "chunk_overlap": 50,
        "top_k": 6,
        "max_context_chars": 6000,
        "research_top_k": 4,
        "research_max_chars": 3000,
    },
    "model_hardware_profile": {
        "name": "Ollama host",
        "gpu_name": "2x RTX 3090",
        "gpu_count": 2,
        "total_vram_gb": 48,
        "system_ram_gb": 32,
        "kv_cache_type": "q8_0",
        "num_parallel": 1,
        "max_loaded_models": 3,
        "sched_spread": True,
    },
}

# ============================================================
# EXECUTION
# ============================================================
EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", "60"))
SEARCH_RESULTS_COUNT = int(os.getenv("SEARCH_RESULTS_COUNT", "15"))
MAX_FETCH_CHARS = int(os.getenv("MAX_FETCH_CHARS", "8000"))

# Quick Search answer-grounding pipeline. Default "balanced" targets enough
# sources for grounded answers without making LLM triage or embeddings part of
# every turn's critical path.
QUICK_SEARCH_MODE = os.getenv("QUICK_SEARCH_MODE", "balanced").strip().lower()
QUICK_SEARCH_PROVIDER = os.getenv("QUICK_SEARCH_PROVIDER", "searxng").strip().lower()
QUICK_SEARCH_SCRAPER = os.getenv("QUICK_SEARCH_SCRAPER", "local").strip().lower()
QUICK_SEARCH_RERANKER = os.getenv("QUICK_SEARCH_RERANKER", "none").strip().lower()
QUICK_SEARCH_MIN_RESULTS = int(os.getenv("QUICK_SEARCH_MIN_RESULTS", "10"))
QUICK_SEARCH_TARGET_RESULTS = int(os.getenv("QUICK_SEARCH_TARGET_RESULTS", "24"))
QUICK_SEARCH_MAX_RESULTS = int(os.getenv("QUICK_SEARCH_MAX_RESULTS", "35"))
QUICK_SEARCH_PAGE_READS_BALANCED = int(os.getenv("QUICK_SEARCH_PAGE_READS_BALANCED", "8"))
QUICK_SEARCH_SEARXNG_ENGINES = os.getenv("QUICK_SEARCH_SEARXNG_ENGINES", "").strip()
QUICK_SEARCH_SEARXNG_NEWS_ENGINES = os.getenv("QUICK_SEARCH_SEARXNG_NEWS_ENGINES", "").strip()
QUICK_SEARCH_SEARXNG_CODE_ENGINES = os.getenv("QUICK_SEARCH_SEARXNG_CODE_ENGINES", "").strip()
QUICK_SEARCH_SEARXNG_RECIPE_ENGINES = os.getenv("QUICK_SEARCH_SEARXNG_RECIPE_ENGINES", "").strip()
QUICK_SEARCH_EMBED_RERANK = os.getenv("QUICK_SEARCH_EMBED_RERANK", "false").lower() == "true"
QUICK_SEARCH_EMBED_TIMEOUT = float(os.getenv("QUICK_SEARCH_EMBED_TIMEOUT", "1.5"))

# Optional LLM model for the quality-mode refinement round. The normal planner
# is deterministic-first; this override is only used when refinement is allowed.
QUICK_SEARCH_TRIAGE_MODEL = os.getenv("QUICK_SEARCH_TRIAGE_MODEL", "")

# ============================================================
# DEFAULTS
# ============================================================
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen3.5:27b")
WORKSPACE_MODEL = os.getenv("WORKSPACE_MODEL", "qwen3.5:4b")
PLANNING_MODEL = os.getenv("PLANNING_MODEL", "qwen3.5:27b")
CODER_MODEL = os.getenv("CODER_MODEL", "qwen2.5-coder:14b")
CRITIC_MODEL = os.getenv("CRITIC_MODEL", "")  # Empty = falls back to PLANNING_MODEL. Independent code reviewer for generate_code output.
CRITIC_ENABLED = os.getenv("CRITIC_ENABLED", "true").lower() == "true"

# Coder Bot v2 — per-agent model overrides. Empty means each agent inherits
# from its umbrella default (PLANNING_MODEL for analysis-style agents,
# CODER_MODEL for code-writing agents) which in turn falls through to the
# active chat model. Power users who want to pin a specific model per agent
# can set these — most users leave them empty.
ARCHITECT_MODEL = os.getenv("ARCHITECT_MODEL", "")  # Empty = use PLANNING_MODEL
REVIEWER_MODEL  = os.getenv("REVIEWER_MODEL",  "")  # Empty = use PLANNING_MODEL
ACCEPTANCE_MODEL = os.getenv("ACCEPTANCE_MODEL", "")  # Empty = use PLANNING_MODEL
BUILDER_MODEL   = os.getenv("BUILDER_MODEL",   "")  # Empty = use CODER_MODEL
FIXER_MODEL     = os.getenv("FIXER_MODEL",     "")  # Empty = use CODER_MODEL
QA_MODEL        = os.getenv("QA_MODEL",        "")  # Empty = use chat / persona model
OPENHANDS_ENABLED = os.getenv("OPENHANDS_ENABLED", "true").lower() == "true"  # Toggle OpenHands for generate_code tool
OPENHANDS_MAX_ROUNDS = int(os.getenv("OPENHANDS_MAX_ROUNDS", "20"))
OPENHANDS_NUM_CTX = int(os.getenv("OPENHANDS_NUM_CTX", "16384"))
AIDER_ENABLED = os.getenv("AIDER_ENABLED", "true").lower() == "true"
AIDER_MODEL = os.getenv("AIDER_MODEL", "")  # Empty = use FIXER_MODEL, then CODER_MODEL
AIDER_NUM_CTX = int(os.getenv("AIDER_NUM_CTX", os.getenv("OPENHANDS_NUM_CTX", "16384")))
AIDER_AUTO_TEST = os.getenv("AIDER_AUTO_TEST", "true").lower() == "true"
AIDER_WORKER_URL = os.getenv("AIDER_WORKER_URL", OPENHANDS_URL)
# Reasoning effort for the OpenHands builder LLM. The SDK passes this through
# to litellm; for OpenAI-style models it's "low" | "medium" | "high"; local
# Ollama models silently ignore unsupported values. Default "medium" — "high"
# adds a lot of think-token overhead per round on small/medium local models.
OPENHANDS_REASONING_EFFORT = os.getenv("OPENHANDS_REASONING_EFFORT", "medium").strip().lower()
MIN_NUM_CTX = int(os.getenv("MIN_NUM_CTX", "1024"))
CODER_V2_MIN_NUM_CTX = int(os.getenv("CODER_V2_MIN_NUM_CTX", "32768"))


def coerce_num_ctx(value, fallback=16384, minimum=None):
    """Return a positive Ollama num_ctx, or a sane fallback for invalid values."""
    minimum = MIN_NUM_CTX if minimum is None else int(minimum)
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    if n >= minimum:
        return n
    if fallback is None:
        return None
    try:
        fb = int(fallback)
    except (TypeError, ValueError):
        fb = 0
    return fb if fb >= minimum else 16384


def coerce_int(value, fallback, *, minimum=None, maximum=None):
    """Parse an int from untrusted settings input, clamped to [minimum, maximum].
    Junk / out-of-range values fall back instead of raising (a raw int(...) on
    a settings PATCH would otherwise 500 or accept negatives)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    if minimum is not None and n < minimum:
        return minimum
    if maximum is not None and n > maximum:
        return maximum
    return n


DEFAULT_NUM_CTX = coerce_num_ctx(os.getenv("DEFAULT_NUM_CTX", "16384"))
# Context window for Deep Research LLM calls (planning, findings, audit,
# synthesis). Defaults higher than DEFAULT_NUM_CTX because depth 3-5 evidence
# contexts overflow a 16K window; evidence budgets scale down to fit this.
RESEARCH_NUM_CTX = coerce_num_ctx(os.getenv("RESEARCH_NUM_CTX", "40960"))
MAX_AGENT_ROUNDS = int(os.getenv("MAX_AGENT_ROUNDS", "12"))
MAX_AGENT_ROUNDS_CODER = int(os.getenv("MAX_AGENT_ROUNDS_CODER", "30"))
DEFAULT_SYSTEM_PROMPT = """You are CodeAgent, an autonomous coding assistant with a sandboxed Linux environment (CodeBox).

## Sandbox Environment
- Isolated container with Python 3 venv at /root/venv (auto-created)
- Python packages: install with run_shell(command="pip3 install X") — goes into the venv
- Prefer Python for most tasks. Other languages (JS, C, Rust, Go, Java) are also available.
- **Project files go in /root/projects/{project_name}/** — NEVER put project files directly in /root/. Create a descriptive project folder first.
- Files persist between tool calls within a session.
- Do NOT use apt-get to install language runtimes — use what's already available.
- **NO STDIN** — `input()` will crash with EOFError. NEVER use input(). Use hardcoded values, sys.argv, or default parameters instead.
- Code runs non-interactively. No prompts, no interactive menus. All inputs must be hardcoded or passed as arguments.

## Core Rules
1. ALWAYS run code using tools. Never paste code in chat — use execute_code or write_file.
2. ALWAYS create a project directory first: run_shell(command="mkdir -p /root/projects/my_project") before writing any files.
3. execute_code = run source code directly (no command-line args). For quick tests with hardcoded values.
4. For scripts that need arguments: use write_file to save the script, then run_shell to execute it with args (e.g., run_shell command="python3 /root/projects/myapp/app.py arg1 arg2").
5. run_shell = run terminal commands (pip3 install X, python3 script.py, git clone, npm install).
6. NEVER use sys.argv in execute_code — it has no arguments. Use write_file + run_shell instead.
7. When code FAILS: read the error carefully, fix the root cause, then retry. Do NOT retry the same broken code.
8. For complex tasks: state your plan in 1-2 sentences, then immediately start using tools.
9. Deliver real output files (CSVs, archives, generated assets) with download_file. Inline charts/diagrams should be emitted as markdown fences instead of saved image files.
10. Be concise — let executed output speak for itself.

## Tool Quick Reference
| Task | Tool | Example |
|------|------|---------|
| Run code | execute_code | code="import math; print(math.pi)", language="python" |
| Install pkg | run_shell | command="pip3 install pandas" |
| Create dir | run_shell | command="mkdir -p /root/projects/myapp" |
| Run script | run_shell | command="python3 /root/projects/myapp/app.py" |
| Save file | write_file | path="/root/projects/myapp/app.py", content="..." |
| Read file | read_file | path="/root/projects/myapp/app.py" |
| List files | list_files | path="/root/projects/myapp" |
| Generate code | generate_code | task="build a web scraper for ...", language="python" |
| Web search | research | query="python requests timeout" |
| Fetch URL | fetch_url | url="https://docs.python.org/3/..." |
| Give file | download_file | path="/root/projects/myapp/output.csv" |

## generate_code — Agentic Code Generation
The `generate_code` tool delegates to an OpenHands coding agent that writes, tests, and fixes code automatically in the sandbox. Use it for complete standalone programs. After it returns a filepath, run it with run_shell and deliver with download_file.

## Charts & Visualizations
Use execute_code for arithmetic, aggregation, statistics, parsing, and data transformation. Then emit an inline ```chart or ```pygraph fence with the computed values. Use ```mermaid fences for diagrams and `$...$` / `$$...$$` for math. Do not save image files for charts or diagrams.

## Error Recovery
- Read the traceback carefully — the error message tells you what to fix
- If you don't understand the error, use research to look it up
- Fix the code and call execute_code again — do NOT give up after one failure
- If a package is missing, use run_shell to install it (pip3 install X), then retry"""

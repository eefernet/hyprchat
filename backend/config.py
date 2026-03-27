"""
HyprChat Configuration
Edit these values to match your homelab setup.
"""
import os

# ============================================================
# INFRASTRUCTURE IPs
# ============================================================
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.110:11434")
CODEBOX_URL = os.getenv("CODEBOX_URL", "http://192.168.1.201:8585")
OPENHANDS_URL = os.getenv("OPENHANDS_URL", "http://192.168.1.201:8586")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://192.168.1.141:8888")
N8N_URL = os.getenv("N8N_URL", "http://192.168.1.114:5678")
N8N_WEBHOOK_PATH = os.getenv("N8N_WEBHOOK_PATH", "/webhook/execute-code")
N8N_RESEARCH_PATH = os.getenv("N8N_RESEARCH_PATH", "/webhook/deep-research")

# ============================================================
# SERVER
# ============================================================
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ============================================================
# DATABASE
# ============================================================
DATABASE_PATH = os.getenv("DATABASE_PATH", "/opt/hyprchat/data/hyprchat.db")

# ============================================================
# FILE STORAGE
# ============================================================
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/opt/hyprchat/data/uploads")
TOOLS_DIR = os.getenv("TOOLS_DIR", "/opt/hyprchat/data/tools")
KB_DIR = os.getenv("KB_DIR", "/opt/hyprchat/data/knowledge_bases")
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))

# ============================================================
# SANDBOX — isolated dir for all tool-generated output files
# ============================================================
SANDBOX_DIR = os.getenv("SANDBOX_DIR", "/opt/hyprchat/data/sandbox")
SANDBOX_OUTPUTS_DIR = os.path.join(SANDBOX_DIR, "outputs")   # tool downloads/outputs → cleaned up
SANDBOX_VENV_DIR    = os.path.join(SANDBOX_DIR, "venv")      # Python venv for local tool execution
SANDBOX_WORKSPACE_DIR = os.path.join(SANDBOX_DIR, "workspace")  # temp working dir

# ============================================================
# SETTINGS FILE (persistent JSON for runtime-editable options)
# ============================================================
SETTINGS_PATH = os.getenv("SETTINGS_PATH", "/opt/hyprchat/data/settings.json")
DEFAULT_SETTINGS = {
    "file_cleanup_days": 30,  # 0 = never clean
    "ollama_url": "",  # empty = use OLLAMA_URL from env/default
    "rag": {
        "embed_model": "nomic-embed-text",
        "chunk_size": 500,
        "chunk_overlap": 50,
        "top_k": 6,
        "max_context_chars": 6000,
        "research_top_k": 4,
        "research_max_chars": 3000,
    },
}

# ============================================================
# EXECUTION
# ============================================================
EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", "60"))
SEARCH_RESULTS_COUNT = int(os.getenv("SEARCH_RESULTS_COUNT", "15"))
MAX_FETCH_CHARS = int(os.getenv("MAX_FETCH_CHARS", "8000"))

# ============================================================
# DEFAULTS
# ============================================================
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen3.5:27b")
WORKSPACE_MODEL = os.getenv("WORKSPACE_MODEL", "qwen3.5:4b")
CODER_MODEL = os.getenv("CODER_MODEL", "qwen2.5-coder:14b")
OPENHANDS_ENABLED = os.getenv("OPENHANDS_ENABLED", "true").lower() == "true"  # Toggle OpenHands for generate_code tool
OPENHANDS_MAX_ROUNDS = int(os.getenv("OPENHANDS_MAX_ROUNDS", "20"))
OPENHANDS_NUM_CTX = int(os.getenv("OPENHANDS_NUM_CTX", "16384"))
DEFAULT_NUM_CTX = int(os.getenv("DEFAULT_NUM_CTX", "16384"))
MAX_AGENT_ROUNDS = int(os.getenv("MAX_AGENT_ROUNDS", "12"))
DEFAULT_SYSTEM_PROMPT = """You are CodeAgent, an autonomous coding assistant with a sandboxed Linux environment (CodeBox).

## Sandbox Environment
- Isolated container with Python 3 venv at /root/venv (auto-created)
- Python packages: install with run_shell(command="pip3 install X") — goes into the venv
- Prefer Python for most tasks. Other languages (JS, C, Rust, Go, Java) are also available.
- Files persist at /root/ between tool calls within a session.
- Do NOT use apt-get to install language runtimes — use what's already available.
- **NO STDIN** — `input()` will crash with EOFError. NEVER use input(). Use hardcoded values, sys.argv, or default parameters instead.
- Code runs non-interactively. No prompts, no interactive menus. All inputs must be hardcoded or passed as arguments.

## Core Rules
1. ALWAYS run code using tools. Never paste code in chat — use execute_code or write_file.
2. execute_code = run source code directly (no command-line args). For quick tests with hardcoded values.
3. For scripts that need arguments: use write_file to save the script, then run_shell to execute it with args (e.g., run_shell command="python3 /root/app.py arg1 arg2").
4. run_shell = run terminal commands (pip3 install X, python3 /root/app.py args, git clone, npm install).
5. NEVER use sys.argv in execute_code — it has no arguments. Use write_file + run_shell instead.
6. When code FAILS: read the error carefully, fix the root cause, then retry. Do NOT retry the same broken code.
7. For complex tasks: state your plan in 1-2 sentences, then immediately start using tools.
8. Deliver output files (charts, CSVs, etc) to the user with download_file. Only call download_file ONCE per file.
9. Be concise — let executed output speak for itself.

## Tool Quick Reference
| Task | Tool | Example |
|------|------|---------|
| Run code | execute_code | code="import math; print(math.pi)", language="python" |
| Install pkg | run_shell | command="pip3 install pandas" |
| Run script | run_shell | command="python3 /root/app.py" |
| Save file | write_file | path="/root/app.py", content="..." |
| Read file | read_file | path="/root/app.py" |
| List files | list_files | path="/root" |
| Generate code | generate_code | task="build a web scraper for ...", language="python" |
| Web search | research | query="python requests timeout" |
| Fetch URL | fetch_url | url="https://docs.python.org/3/..." |
| Give file | download_file | path="/root/output.png" |

## generate_code — Agentic Code Generation
The `generate_code` tool delegates to an OpenHands coding agent that writes, tests, and fixes code automatically in the sandbox. Use it for complete standalone programs. After it returns a filepath, run it with run_shell and deliver with download_file.

## Error Recovery
- Read the traceback carefully — the error message tells you what to fix
- If you don't understand the error, use research to look it up
- Fix the code and call execute_code again — do NOT give up after one failure
- If a package is missing, use run_shell to install it (pip3 install X), then retry"""

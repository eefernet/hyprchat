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
WORKSPACE_MODEL = os.getenv("WORKSPACE_MODEL", "qwen2.5:7b")
DEFAULT_SYSTEM_PROMPT = """You are CodeAgent — an elite autonomous coding assistant with full access to a sandboxed Linux environment.

## Capabilities
- Execute code in 30+ languages: Python, Rust, C/C++, Go, JavaScript/TypeScript, Java, Ruby, PHP, Swift, Kotlin, Haskell, and more
- Shell commands for system tasks: install packages, run git, manage files
- Web research via SearXNG for docs, APIs, error solutions
- Deep multi-source research with AI synthesis
- Read, write, list, delete files on the sandbox

## Working Protocol
1. **Always run code** — never show code without executing it. Execute → check output → iterate.
2. **Error recovery** — read the error → research if unclear → fix → re-execute. Iterate until working.
3. **Tool selection**: `execute_code` for ALL code; `run_shell` for system tasks only; `write_file` for persistence
4. **Deliver files** — use `download_file` for any output the user should keep, then include the link
5. **Be concise** — let executed output speak for itself; keep prose minimal

## Output
Code output appears automatically as a card in chat. Always verify your solution actually runs before declaring success."""

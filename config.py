"""Central configuration. Everything comes from environment variables (.env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- FalkorDB ---
FALKOR_HOST = os.getenv("FALKORDB_HOST", "localhost")
FALKOR_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKOR_USERNAME = os.getenv("FALKORDB_USERNAME") or None
FALKOR_PASSWORD = os.getenv("FALKORDB_PASSWORD") or None
FALKOR_SSL = os.getenv("FALKORDB_SSL", "false").strip().lower() in ("1", "true", "yes")
GRAPH_NAME = os.getenv("GRAPH_NAME", "repo")

# --- Repo to analyze ---
_demo = os.getenv("DEMO_REPO_PATH", "").strip()
DEMO_REPO_PATH = Path(_demo) if _demo else None

# --- Scanning rules ---
IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    "build", "dist", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "site-packages", ".idea", ".vscode",
}

# Skip call edges when a function name is ambiguous (matches more than this many functions)
MAX_CALL_TARGETS = 3

# Batch size for UNWIND writes (keep modest so the query string stays small)
BATCH_SIZE = 200

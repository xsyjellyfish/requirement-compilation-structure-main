"""Shared subprocess helper for entry-point tests: one place owns env isolation."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_entry(args, env_overrides=None, timeout=120):
    """Run main.py in a subprocess. Shell env wins over the project .env (override=False),
    so tests must blank OPENAI_* explicitly to keep local credentials out of no-network runs."""
    env = {k: v for k, v in os.environ.items() if k not in {'OPENAI_BASE_URL', 'OPENAI_API_KEY'}}
    env.update(env_overrides or {})
    return subprocess.run([sys.executable, str(ROOT / 'main.py'), *map(str, args)],
                          capture_output=True, text=True, env=env, timeout=timeout)

"""Load ``.env`` into the process environment. **Import this FIRST** in an
entrypoint, before any other ``quinn.*`` module.

Keeps live secrets (the Slack webhook, Gmail creds paths, etc.) out of source.
Only the CLI entrypoint (`run.py`) imports this — library modules deliberately
do **not**, so the test suite and offline runs never pick up live credentials by
accident (they inject their own fakes instead).

Stdlib-only, minimal `.env` parser: ``KEY=value`` lines, ``#`` comments, blanks
ignored. Existing environment variables win (``setdefault``), so an explicit
shell export always overrides the file.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path | None = None) -> None:
    path = path or ENV_PATH
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


load_env()

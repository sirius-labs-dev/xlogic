#!/usr/bin/env python3
"""XLogic UX entrypoint — separate argv so stray pkill -f dashboard/server.py won't kill it."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.server import main

if __name__ == "__main__":
    raise SystemExit(main())

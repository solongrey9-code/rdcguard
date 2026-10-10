#!/usr/bin/env python3
"""AgentGuard entry point: python guard.py <command> [options]."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentguard.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

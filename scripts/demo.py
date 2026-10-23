"""Compatibility entry point for the demo now shipped as ``verirun demo``.

Usage: uv run python scripts/demo.py [--state-dir DIR] [--shared DIR] [--runs DIR]
"""

from __future__ import annotations

import sys

from verirun.demo import main

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Deterministic executable used by packaged UI browser smoke tests."""

from __future__ import annotations

import sys

if "--version" in sys.argv:
    print("fake-codex 1.0")
else:
    print("Browser validation response with `inline code`.\n\n```python\nprint('validated')\n```")

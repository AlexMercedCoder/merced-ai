#!/usr/bin/env python3
"""Deterministic executable used by packaged UI browser smoke tests."""

from __future__ import annotations

import os
import sys
import time

if "--version" in sys.argv:
    time.sleep(float(os.environ.get("MERCED_AI_FAKE_CODEX_VERSION_DELAY", "0")))
    print("fake-codex 1.0")
else:
    print("Browser validation response with `inline code`.\n\n```python\nprint('validated')\n```")

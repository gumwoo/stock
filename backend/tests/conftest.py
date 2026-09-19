"""Shared test configuration.

`PYTHONUTF8` is forced because several source files carry non-ASCII characters
in docstrings, and tooling that opens them with the Windows default codepage
(cp949 on this machine) fails to decode them.
"""

from __future__ import annotations

import os

os.environ.setdefault("PYTHONUTF8", "1")

"""Shared test configuration.

`PYTHONUTF8` is forced because several source files carry non-ASCII characters
in docstrings, and tooling that opens them with the Windows default codepage
(cp949 on this machine) fails to decode them.
"""

from __future__ import annotations

import hashlib
import os

os.environ.setdefault("PYTHONUTF8", "1")


def fake_cik(module_name: str) -> str:
    """A CIK unique to one test module, derived rather than assigned.

    The integration tests seed instruments under reserved identifiers and
    clean them up afterwards. Sequentially that worked with hand-picked
    numbers; in parallel it did not — three pairs of files had quietly been
    sharing one, and running them at once collided on the unique constraint.

    Deriving the value from the module name means a new file cannot forget to
    pick a free number, which is the failure that would otherwise recur every
    time someone adds one.
    """
    digest = hashlib.sha256(module_name.encode("utf-8")).hexdigest()
    return f"99{int(digest, 16) % 100_000_000:08d}"


def fake_corp_code(module_name: str) -> str:
    """The same, for DART's eight-digit corporate codes."""
    digest = hashlib.sha256(module_name.encode("utf-8")).hexdigest()
    return f"99{int(digest, 16) % 1_000_000:06d}"

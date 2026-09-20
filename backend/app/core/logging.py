"""Logging setup.

Exists for one reason worth stating plainly: httpx logs every request at INFO
with the full URL, and DART passes its API key as a query parameter. Left
alone, running a collection writes the key into the log:

    GET https://opendart.fss.or.kr/api/list.json?crtfc_key=<the actual key>&...

Logs get pasted into issues, shipped to aggregators and committed by accident,
so the request logger is quieted rather than trusting every future caller to
notice. Set `HTTPX_LOG_LEVEL=INFO` to restore it while debugging, knowing what
that prints.
"""

from __future__ import annotations

import logging
import os

# Loggers that print full request URLs.
_URL_LOGGERS = ("httpx", "httpcore")


def configure(level: str = "INFO") -> None:
    """Set up application logging, with request URLs suppressed by default."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )

    request_level = os.getenv("HTTPX_LOG_LEVEL", "WARNING").upper()
    for name in _URL_LOGGERS:
        logging.getLogger(name).setLevel(request_level)

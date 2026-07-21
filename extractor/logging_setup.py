"""
Minimal standalone logging for the extractor.

Deliberately does NOT import from lambdas.common.logger -- the extractor is
a standalone local script (launchd job on Dom's primary macOS login), not
part of the Lambda deployment package, and should stay decoupled from the
backend's import graph.

Log destination: stdout by default; when EXTRACTOR_LOG_PATH is set (the
launchd plist will set this -- see extractor/README.md), also logs to that
file so `launchd` runs have a persistent trail to check after the fact.
"""

import logging
import os
import sys

_LOG_LEVEL = os.environ.get("EXTRACTOR_LOG_LEVEL", "INFO").upper()
_LOG_PATH = os.environ.get("EXTRACTOR_LOG_PATH")

_configured = False


def _configure_root() -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger("xomtracks.extractor")
    root.setLevel(_LOG_LEVEL)
    root.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s | %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if _LOG_PATH:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        file_handler = logging.FileHandler(_LOG_PATH)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger("xomtracks.extractor").getChild(name.rsplit(".", 1)[-1])

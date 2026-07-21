"""
Persists the extractor's watermark -- the max `message.ROWID` fully
processed so far -- between runs.

Tracked by ROWID, not date (see chat_reader.py's module docstring for why):
this file is the only local state the extractor keeps.
"""

import json
import os

DEFAULT_STATE_PATH = os.path.expanduser("~/.xomtracks/extractor_state.json")


def load_watermark(path: str = DEFAULT_STATE_PATH) -> int:
    """Return the last-processed ROWID, or 0 if no state file exists yet
    (first run -- scans the whole DB) or the file is unreadable/corrupt."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return int(data.get("last_rowid", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def save_watermark(path: str, rowid: int) -> None:
    """Persist the new watermark, creating parent directories if needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"last_rowid": rowid}, f)

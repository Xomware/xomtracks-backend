# xomtracks-backend

Python Lambda backend for **Xomtracks** — surfaces every Spotify / SoundCloud /
Apple Music link shared across Dom's iMessage conversations (all threads,
both directions), cross-platform matches non-Spotify links to Spotify, and
maintains two auto-rolling "last 30 days" public Spotify playlists (in/out).

See `docs/features/xomtracks/PLAN.md` in the local working tree for the full
spec.

Also houses `extractor/` — the **local, read-only** `chat.db` reader that
runs as a launchd job on Dom's primary macOS login (`dom` user), not a
Lambda. It pushes new music-link shares to `POST /shares/ingest` via a
scoped SSM bearer key. See `extractor/README.md`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run_tests.sh
```

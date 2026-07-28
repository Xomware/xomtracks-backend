# Xomtracks — Self-Serve Setup

Run your own iMessage music-link extractor on your Mac. It quietly notices the
Spotify / SoundCloud / Apple Music links you and your friends text each other,
and adds them to Xomtracks — so your shared music turns into playlists and a
feed instead of scrolling away in a group chat.

This is the guided `curl … | bash` on-ramp for **dev-ish friends on a Mac**. It
reuses the exact same extractor that runs on Dom's machine.

---

## What it does — and what it will never do

**It reads, it never writes.** The extractor opens your Messages database
(`~/Library/Messages/chat.db`) strictly **read-only** (`SQLite mode=ro`). It:

- **never writes to `chat.db`**,
- **never sends an iMessage**,
- **never uploads message text, contacts, attachments, or anything** other than
  the music links themselves.

The only data that ever leaves your Mac is the matched music **URLs** (plus the
minimum metadata to attribute a share: who shared it, when, and which chat). The
allowlist of what counts as a "music link" lives in
[`url_extractor.py`](./url_extractor.py) — that regex list *is* the guarantee.
If a URL doesn't match it, it never leaves your machine.

Everything runs **locally under your own macOS login**, on a schedule, via
Apple's `launchd`. There is no always-on server, and it needs **no AWS account
and no `aws` CLI** — your ingest token lives in your login Keychain.

---

## Prerequisites

- **A Mac** (macOS — the whole thing is built on chat.db + launchd + Keychain).
- **Messages signed in** with your Apple ID, actively receiving your texts.
- **Python 3.11 or newer.** The system `/usr/bin/python3` is often 3.9, which is
  too old. Get a newer one with either:
  - Homebrew: `brew install python@3.12`
  - or the installer from <https://www.python.org/downloads/macos/>
- **A Xomtracks ingest token** (you generate this in the next section).
- Comfort granting **Full Disk Access** to a Python binary (the one manual step
  macOS forces — explained below).

---

## Setup — the whole flow

### 1. Generate your ingest token in Xomify

In Xomify, open the **"Shares → set up your own"** card and generate an **ingest
token**. It is shown **exactly once** — copy it and keep it handy for step 3.

The token is opaque and revocable. The backend only ever stores its SHA-256
hash, and uses it to stamp your shares with *your* owner id, so your music stays
separate from everyone else's.

### 2. Run the installer

```bash
curl -fsSL https://raw.githubusercontent.com/Xomware/xomtracks-backend/master/extractor/install.sh | bash
```

Prefer to read it first? (Reasonable — it's asking to read your Messages DB.)

```bash
curl -fsSL https://raw.githubusercontent.com/Xomware/xomtracks-backend/master/extractor/install.sh -o install.sh
less install.sh          # read it
bash install.sh          # then run it
```

The installer is **idempotent** — re-running it repairs or updates an existing
install instead of duplicating it, and it **fails loudly** at every step rather
than leaving a half-configured mess.

It will:

1. Check your Mac (macOS, Messages signed in, Python 3.11+).
2. Install the extractor into `~/.xomtracks/app` and build a virtualenv.
3. Prompt for your ingest token and store it in your **login Keychain** (never
   on disk).
4. Walk you through **Full Disk Access** (step 3 below).
5. Install a `launchd` LaunchAgent (`com.xomware.xomtracks-extractor`) that runs
   every **15 minutes**.
6. Kick a first scan and tail the log to confirm it works.

### 3. Grant Full Disk Access (the one manual step)

macOS guards `~/Library/Messages` with TCC, and grants access **per binary** —
so Terminal having access does **not** cover the background job. You must add the
extractor's Python interpreter to the Full Disk Access list.

The installer opens the right pane
(`Privacy & Security → Full Disk Access`) and prints the **exact path** to add —
it looks like `~/.xomtracks/app/.venv/bin/python3.12` resolved to its real
location. In the file dialog, press **Cmd-Shift-G**, paste that path, select it,
and make sure its toggle is **ON**.

The installer then kicks a real scheduled run and reads the log. Because that run
has no Terminal parent, it's the *authoritative* test: if it can read `chat.db`,
your grant is correct. If it can't, the installer tells you exactly which binary
still needs the grant.

### That's it

Once the first scan completes, you're done. It keeps running every 15 minutes in
the background. New links you text (or that friends text you) show up in
Xomtracks on the next scan.

---

## Checking status

```bash
# Is the job loaded and what's its last exit status?
launchctl print gui/$(id -u)/com.xomware.xomtracks-extractor | grep -E 'state|pid|last exit'

# Watch the log live (each run brackets itself with "run start" / "run end")
tail -f ~/Library/Logs/xomtracks-extractor.log

# Force a scan right now instead of waiting for the 15-minute timer
launchctl kickstart -k gui/$(id -u)/com.xomware.xomtracks-extractor
```

A healthy run logs something like:

```
2026-07-28T10:15:00-0400 === extractor run start (pid 12345) ===
2026-07-28T10:15:00-0400 ingest token loaded from Keychain (service=xomtracks-ingest)
... Scan complete: {'scanned': 3, 'shares_found': 2, 'shares_pushed': 2, 'failed': False, ...}
2026-07-28T10:15:02-0400 === extractor run end (exit 0) ===
```

Local state (the last-processed message watermark) lives at
`~/.xomtracks/extractor_state.json`. It tracks message **ROWID** (insert order),
not date, so iCloud history backfill is picked up automatically.

---

## Uninstall

Removes the job, the code, the local state, and the Keychain token — no trace
left. (It never wrote anything to your Messages, so there's nothing to undo
there.)

```bash
# 1. Stop and remove the scheduled job
launchctl bootout gui/$(id -u)/com.xomware.xomtracks-extractor
rm -f ~/Library/LaunchAgents/com.xomware.xomtracks-extractor.plist

# 2. Remove the app, venv, and local state
rm -rf ~/.xomtracks

# 3. Delete the ingest token from the Keychain
security delete-generic-password -s "xomtracks-ingest" -a "$USER"

# 4. (Optional) remove the log
rm -f ~/Library/Logs/xomtracks-extractor.log
```

To fully revoke access, also **revoke the token in Xomify** (`Shares → set up
your own`) — deleting it locally stops *your* machine, revoking it invalidates
the token server-side.

You can also remove the Python interpreter from
`Privacy & Security → Full Disk Access` if you'd like.

---

## Troubleshooting

### "It installed but no shares are showing up"

- **Full Disk Access not granted to the right binary.** This is the #1 cause.
  Check the log (`tail ~/Library/Logs/xomtracks-extractor.log`) for
  `unable to open database` / `operation not permitted`. Fix: re-open
  `Privacy & Security → Full Disk Access`, confirm the exact interpreter path the
  installer printed is present **and toggled ON**, then re-run a scan:
  `launchctl kickstart -k gui/$(id -u)/com.xomware.xomtracks-extractor`.
- **FDA silently broke after a Python upgrade.** TCC grants attach to a specific
  binary path. `brew upgrade python` can move the interpreter, voiding the grant.
  Symptom: it worked, then quietly stopped. Fix: re-run `install.sh` (it rebuilds
  the venv and reprints the current interpreter path to grant), then re-add it to
  Full Disk Access.
- **No new links in the window scanned.** If nobody's texted a music link since
  the last scan, there's simply nothing to push. `shares_found: 0` is normal.

### "Token wrong / 401 in the log"

The log shows a `401` from the ingest endpoint. Your Keychain token is missing,
mistyped, or has been revoked. Re-generate a token in Xomify and re-store it:

```bash
security add-generic-password -s "xomtracks-ingest" -a "$USER" -T /usr/bin/security -U -w "<NEW_TOKEN>"
```

(The `-U` updates it in place. Or just re-run `install.sh`.) Then kick a scan.

### "Python missing / too old"

`install.sh` aborts if it can't find Python 3.11+. Install one and re-run:

```bash
brew install python@3.12   # or download from python.org
```

### "The log says it fell back to SSM / AWS"

You'll only see this if the Keychain token couldn't be read. A self-serve install
has no AWS credentials, so the SSM fallback will fail with a clear error — that's
expected. The real fix is to get your token back into the Keychain (see "Token
wrong" above). The SSM path is legacy plumbing for Dom's original single-user
setup and does not apply to you.

### "curl | bash asked me nothing / couldn't read my token"

The token prompt and confirmations read from your terminal (`/dev/tty`), which a
piped `curl | bash` sometimes can't reach. If that happens, download and run it
directly instead:

```bash
curl -fsSL https://raw.githubusercontent.com/Xomware/xomtracks-backend/master/extractor/install.sh -o install.sh
bash install.sh
```

---

## Privacy, one more time

- Read-only on `chat.db`. Never writes. Never sends a message.
- Only **music links** leave your Mac — never message text, contacts, or
  attachments.
- Your ingest token lives only in your login Keychain.
- The extractor is open source in this repo — the URL allowlist in
  [`url_extractor.py`](./url_extractor.py) is the auditable proof of exactly what
  gets extracted. Read it. That's the whole point of doing this as a script you
  can inspect.

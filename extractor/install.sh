#!/bin/bash
#
# install.sh — guided self-serve installer for the Xomtracks iMessage
# music-link extractor.
#
#   Run it one of two ways:
#     curl -fsSL <raw-url>/extractor/install.sh | bash
#     # or download it, read it, then:  bash install.sh
#
# WHAT IT DOES (and what it deliberately does NOT do):
#   - Sets up a *read-only* iMessage music-link extractor on YOUR Mac.
#   - It reads ~/Library/Messages/chat.db strictly read-only (SQLite mode=ro).
#     It NEVER writes to chat.db and NEVER sends an iMessage. The only thing
#     that ever leaves your machine is the music links themselves (Spotify /
#     SoundCloud / Apple Music URLs) — never message text, never contacts,
#     never anything else. The URL allowlist in extractor/url_extractor.py is
#     the literal, auditable guarantee.
#
# It is idempotent: re-running it repairs/updates an existing install rather
# than duplicating it. It fails loudly at every step — it will never leave a
# half-configured state pretending to be done.
#
# Full walkthrough, privacy notes, status/uninstall/troubleshooting:
#   extractor/SELF-SERVE.md
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Config (all overridable via env for testing / non-default checkouts)
# ---------------------------------------------------------------------------
STATE_DIR="${XOMTRACKS_STATE_DIR:-$HOME/.xomtracks}"
APP_DIR="${XOMTRACKS_APP_DIR:-$STATE_DIR/app}"
REPO_URL="${XOMTRACKS_REPO_URL:-https://github.com/Xomware/xomtracks-backend.git}"
REPO_REF="${XOMTRACKS_REPO_REF:-master}"

LABEL="com.xomware.xomtracks-extractor"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_FILE="$HOME/Library/Logs/xomtracks-extractor.log"
CHAT_DB="$HOME/Library/Messages/chat.db"

KEYCHAIN_SERVICE="xomtracks-ingest"
KEYCHAIN_ACCOUNT="${USER:-$(id -un)}"
INTERVAL_SECONDS=900   # 15 minutes — mirrors the documented StartInterval

MIN_PY_MAJOR=3
MIN_PY_MINOR=11

# ---------------------------------------------------------------------------
# Pretty output + failure helpers
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
    YEL=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'
else
    BOLD=''; DIM=''; RED=''; GRN=''; YEL=''; BLU=''; RST=''
fi

step()  { printf '\n%s==>%s %s%s%s\n' "$BLU" "$RST" "$BOLD" "$*" "$RST"; }
info()  { printf '    %s\n' "$*"; }
dim()   { printf '    %s%s%s\n' "$DIM" "$*" "$RST"; }
ok()    { printf '    %s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn()  { printf '    %s!%s %s\n' "$YEL" "$RST" "$*"; }
die()   { printf '\n%sERROR:%s %s\n' "$RED" "$RST" "$*" >&2
          printf '%sInstall aborted — nothing was left running in a broken state.%s\n' "$DIM" "$RST" >&2
          exit 1; }

# Interactive reads must come from the terminal, not stdin — stdin is the
# script itself when run via `curl … | bash`.
TTY="/dev/tty"
have_tty() { [[ -e "$TTY" ]] && (: >"$TTY") 2>/dev/null; }

pause() {
    have_tty || die "This step needs an interactive terminal. Download install.sh and run it with 'bash install.sh' instead of piping from curl."
    printf '    %s%s%s ' "$BOLD" "${1:-Press Return to continue…}" "$RST" >"$TTY"
    read -r _ <"$TTY"
}

confirm() {  # confirm "question" -> 0 if yes
    have_tty || die "This step needs an interactive terminal. Run 'bash install.sh' instead of piping from curl."
    local ans
    printf '    %s%s%s [y/N] ' "$BOLD" "$1" "$RST" >"$TTY"
    read -r ans <"$TTY"
    [[ "$ans" =~ ^[Yy]$ ]]
}

# ---------------------------------------------------------------------------
# 0. Banner
# ---------------------------------------------------------------------------
cat <<BANNER

${BOLD}Xomtracks — iMessage music-link extractor · self-serve installer${RST}
${DIM}Read-only. It scans chat.db for Spotify / SoundCloud / Apple Music links
and pushes only those links to Xomtracks. It never writes to your Messages,
never sends a text, and never uploads message contents.${RST}

It will:
  1. Check your Mac (macOS, Messages signed in, Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+)
  2. Install the extractor into ${BOLD}${APP_DIR}${RST}
  3. Store your ingest token in the login Keychain (never on disk)
  4. Walk you through granting Full Disk Access (required to read chat.db)
  5. Schedule it to run every $(( INTERVAL_SECONDS / 60 )) minutes via launchd
  6. Kick a first scan and confirm it works
BANNER

if have_tty; then
    confirm "Continue?" || die "Cancelled at the intro. Nothing was changed."
fi

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
step "Preflight checks"

[[ "$(uname -s)" == "Darwin" ]] || die "This installer is macOS-only (chat.db + launchd + Keychain are Apple-specific)."
ok "macOS detected ($(sw_vers -productVersion 2>/dev/null || echo 'version unknown'))"

# Messages signed in? We can't READ chat.db yet (that needs FDA, granted later),
# but its mere existence proves Messages has been set up with an account.
if [[ ! -f "$CHAT_DB" ]]; then
    die "No Messages database at ${CHAT_DB}.
    Open the Messages app and sign in with your Apple ID first, send/receive at
    least one message, then re-run this installer."
fi
ok "Messages is set up (chat.db present)"

# Find a Python >= 3.11. The system /usr/bin/python3 on many Macs is 3.9, so we
# probe named interpreters and common Homebrew locations rather than trusting
# bare `python3`.
PYTHON_BIN=""
py_ok() {  # py_ok <path> -> 0 if that interpreter is >= MIN_PY
    local cand="$1"
    command -v "$cand" >/dev/null 2>&1 || return 1
    "$cand" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 1)" >/dev/null 2>&1
}
for cand in \
    python3.13 python3.12 python3.11 python3 \
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3
do
    if py_ok "$cand"; then PYTHON_BIN="$(command -v "$cand")"; break; fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    die "No Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ found.
    Install one, then re-run this installer:
      • Homebrew:  brew install python@3.12
      • Or download from https://www.python.org/downloads/macos/
    (Your system /usr/bin/python3 is likely 3.9, which is too old.)"
fi
ok "Python: ${PYTHON_BIN} ($("$PYTHON_BIN" --version 2>&1))"

# git for fetching the code (curl-tarball fallback if git is missing).
HAVE_GIT=0
command -v git >/dev/null 2>&1 && HAVE_GIT=1
if [[ "$HAVE_GIT" -eq 0 ]]; then
    command -v curl >/dev/null 2>&1 || die "Need either git or curl to fetch the extractor. Install Xcode Command Line Tools: xcode-select --install"
    warn "git not found — will download a source tarball with curl instead."
fi

# ---------------------------------------------------------------------------
# 2. Fetch the extractor code into a stable location
# ---------------------------------------------------------------------------
step "Installing the extractor into ${APP_DIR}"
mkdir -p "$STATE_DIR"

fetch_with_git() {
    if [[ -d "$APP_DIR/.git" ]]; then
        info "Existing checkout found — updating it in place."
        git -C "$APP_DIR" remote set-url origin "$REPO_URL"
        git -C "$APP_DIR" fetch --depth 1 origin "$REPO_REF" || die "git fetch failed. Check your network / repo access to ${REPO_URL}."
        git -C "$APP_DIR" checkout -q -B "$REPO_REF" FETCH_HEAD || die "git checkout of ${REPO_REF} failed."
        git -C "$APP_DIR" reset -q --hard FETCH_HEAD
    else
        [[ -e "$APP_DIR" ]] && die "${APP_DIR} exists but is not a git checkout. Move or remove it, then re-run."
        git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$APP_DIR" \
            || die "git clone failed. Check your network / access to ${REPO_URL} (ref ${REPO_REF})."
    fi
}

fetch_with_curl() {
    # GitHub codeload tarball → extract just the repo contents into APP_DIR.
    local owner_repo tarball tmp
    owner_repo="$(printf '%s' "$REPO_URL" | sed -E 's#https://github.com/##; s#\.git$##')"
    tarball="https://codeload.github.com/${owner_repo}/tar.gz/refs/heads/${REPO_REF}"
    tmp="$(mktemp -d)"
    info "Downloading ${tarball}"
    curl -fsSL "$tarball" -o "$tmp/src.tgz" || { rm -rf "$tmp"; die "Download failed from ${tarball}."; }
    rm -rf "$APP_DIR"; mkdir -p "$APP_DIR"
    tar -xzf "$tmp/src.tgz" -C "$APP_DIR" --strip-components=1 || { rm -rf "$tmp"; die "Failed to extract the source tarball."; }
    rm -rf "$tmp"
}

if [[ "$HAVE_GIT" -eq 1 ]]; then fetch_with_git; else fetch_with_curl; fi

[[ -f "$APP_DIR/extractor/run.py" ]]            || die "Fetched code is missing extractor/run.py — the download looks incomplete."
[[ -f "$APP_DIR/extractor/run_scheduled.sh" ]]  || die "Fetched code is missing extractor/run_scheduled.sh."
[[ -f "$APP_DIR/requirements.txt" ]]            || die "Fetched code is missing requirements.txt."
chmod +x "$APP_DIR/extractor/run_scheduled.sh"
ok "Extractor code in place at ${APP_DIR}"

# ---------------------------------------------------------------------------
# 3. Virtualenv + dependencies
# ---------------------------------------------------------------------------
step "Creating the Python environment"
VENV_DIR="$APP_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python3"

"$PYTHON_BIN" -m venv "$VENV_DIR" || die "Failed to create a virtualenv at ${VENV_DIR}."
"$VENV_PY" -m pip install --quiet --upgrade pip || die "Failed to upgrade pip inside the venv."
# The scheduled scan path (python -m extractor.run) only needs 'requests' at
# runtime, but we install the full pinned set to match the repo exactly.
"$VENV_PY" -m pip install --quiet -r "$APP_DIR/requirements.txt" \
    || die "pip install -r requirements.txt failed. Re-run to retry once your network is back."
ok "Virtualenv ready ($("$VENV_PY" --version 2>&1))"

# The exact interpreter binary that launchd will read chat.db with. Full Disk
# Access (TCC) is granted per *binary*, and a venv's python3 is a symlink to a
# real interpreter — TCC follows it to the real path, so that is what must be
# granted. Compute both so we can show the user precisely what to add.
REAL_PY="$("$VENV_PY" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
ok "Runtime interpreter (needs Full Disk Access): ${REAL_PY}"

# ---------------------------------------------------------------------------
# 4. Ingest token → login Keychain
# ---------------------------------------------------------------------------
step "Storing your ingest token in the login Keychain"
cat <<TOKENHELP
    In Xomify, open the "Shares → set up your own" card and generate an ingest
    token. It is shown ${BOLD}exactly once${RST} — copy it now.

    It is stored ONLY in your macOS login Keychain (never written to disk,
    never printed, never committed). The scheduled job reads it at runtime.
TOKENHELP

if security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" -w >/dev/null 2>&1; then
    warn "A '${KEYCHAIN_SERVICE}' token already exists in your Keychain."
    if have_tty && ! confirm "Replace it with a new token?"; then
        info "Keeping the existing token."
        SKIP_TOKEN=1
    fi
fi

if [[ "${SKIP_TOKEN:-0}" -ne 1 ]]; then
    have_tty || die "Need an interactive terminal to read the token securely. Run 'bash install.sh' instead of piping from curl."
    TOKEN=""
    for attempt in 1 2 3; do
        printf '    %sPaste your ingest token (input hidden):%s ' "$BOLD" "$RST" >"$TTY"
        IFS= read -rs TOKEN <"$TTY"; printf '\n' >"$TTY"
        [[ -n "$TOKEN" ]] && break
        warn "Empty token — try again (attempt ${attempt}/3)."
    done
    [[ -n "$TOKEN" ]] || die "No token provided after 3 attempts."

    # -U updates in place (rotation-safe); -T /usr/bin/security lets the
    # launchd-run 'security' read it without a GUI prompt.
    security add-generic-password \
        -s "$KEYCHAIN_SERVICE" \
        -a "$KEYCHAIN_ACCOUNT" \
        -T /usr/bin/security \
        -U \
        -w "$TOKEN" \
        || die "Failed to store the token in the Keychain."
    unset TOKEN

    security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" -w >/dev/null 2>&1 \
        || die "Stored the token but cannot read it back — Keychain verification failed."
    ok "Token stored and verified (service=${KEYCHAIN_SERVICE}, account=${KEYCHAIN_ACCOUNT})"
fi

# ---------------------------------------------------------------------------
# 5. Full Disk Access for the runtime interpreter
# ---------------------------------------------------------------------------
step "Granting Full Disk Access (required to read chat.db)"
cat <<FDAHELP
    macOS protects ~/Library/Messages with TCC. Access is granted ${BOLD}per
    binary${RST} — Terminal having access does NOT cover the background job.
    You must add the extractor's Python interpreter to Full Disk Access:

        ${BOLD}${REAL_PY}${RST}

    Steps (the System Settings pane will open now):
      1. Click the ${BOLD}+${RST} button under Full Disk Access.
      2. In the file dialog press ${BOLD}Cmd-Shift-G${RST} and paste:
             ${REAL_PY}
      3. Select it, click Open, and make sure its toggle is ${BOLD}ON${RST}.
FDAHELP

open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null \
    || warn "Couldn't auto-open System Settings. Open it manually: Privacy & Security → Full Disk Access."

if have_tty; then
    pause "Add the interpreter above, toggle it ON, then press Return…"
fi

# Preliminary read check. NOTE: when run from a Terminal that itself has FDA,
# this can succeed via Terminal's grant even if the interpreter lacks its own —
# so a PASS here is encouraging but NOT authoritative. The launchd run in the
# next step (no Terminal parent) is the real proof.
if "$VENV_PY" - "$CHAT_DB" <<'PYEOF' >/dev/null 2>&1
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
con.execute("SELECT COUNT(*) FROM sqlite_master")
con.close()
PYEOF
then
    ok "Test read of chat.db succeeded."
else
    warn "Test read of chat.db failed here — the launchd run below is the real check."
    warn "If that run also fails to read chat.db, revisit the Full Disk Access grant above."
fi

# ---------------------------------------------------------------------------
# 6. Install + bootstrap the LaunchAgent
# ---------------------------------------------------------------------------
step "Installing the launchd job (every $(( INTERVAL_SECONDS / 60 )) minutes)"
mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG_FILE")"

# XOMTRACKS_REPO_DIR points run_scheduled.sh at this self-serve checkout
# (it defaults to Dom's path otherwise). RunAtLoad + StartInterval give an
# immediate run plus one every 15 minutes.
cat >"$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${APP_DIR}/extractor/run_scheduled.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>XOMTRACKS_REPO_DIR</key>
        <string>${APP_DIR}</string>
    </dict>
    <key>StartInterval</key>
    <integer>${INTERVAL_SECONDS}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
</dict>
</plist>
PLISTEOF
ok "Wrote LaunchAgent: ${PLIST}"

DOMAIN="gui/$(id -u)"
# Idempotent: tear down any previous instance before (re)bootstrapping.
launchctl bootout "$DOMAIN/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$PLIST" \
    || die "launchctl bootstrap failed. Check the plist at ${PLIST} and re-run."
launchctl enable "$DOMAIN/${LABEL}" >/dev/null 2>&1 || true
ok "LaunchAgent bootstrapped and enabled (${LABEL})"

# ---------------------------------------------------------------------------
# 7. Kick an initial scan and verify via the log
# ---------------------------------------------------------------------------
step "Running the first scan"
: >"$LOG_FILE" 2>/dev/null || true   # start from a clean log so we read THIS run
launchctl kickstart -k "$DOMAIN/${LABEL}" \
    || die "Failed to kickstart the job. Inspect: ${LOG_FILE}"

info "Waiting for the first run to finish (up to ~40s)…"
RUN_DONE=0
FDA_DENIED=0
for _ in $(seq 1 40); do
    if grep -q "run end" "$LOG_FILE" 2>/dev/null; then RUN_DONE=1; break; fi
    sleep 1
done

# Detect the tell-tale signatures of a missing per-binary FDA grant in the log.
if grep -Eqi "unable to open database|operation not permitted|authorization denied|disk i/o error|permission denied" "$LOG_FILE" 2>/dev/null; then
    FDA_DENIED=1
fi

printf '\n%s--- last lines of %s ---%s\n' "$DIM" "$LOG_FILE" "$RST"
tail -n 15 "$LOG_FILE" 2>/dev/null || true
printf '%s-------------------------------------------%s\n' "$DIM" "$RST"

if [[ "$FDA_DENIED" -eq 1 ]]; then
    die "The scheduled job could not read chat.db — Full Disk Access is not
    granted to the interpreter:
        ${REAL_PY}
    Re-open Privacy & Security → Full Disk Access, add that exact binary, toggle
    it ON, then re-run this installer (or: launchctl kickstart -k ${DOMAIN}/${LABEL})."
fi

if [[ "$RUN_DONE" -ne 1 ]]; then
    warn "Didn't see the run finish within the wait window. It may still be"
    warn "working (large first scan). Check progress with:"
    dim "    tail -f \"$LOG_FILE\""
else
    ok "First scan completed."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
cat <<DONE

${GRN}${BOLD}Xomtracks is installed.${RST}
It will scan for music links every $(( INTERVAL_SECONDS / 60 )) minutes, in the background.

${BOLD}Check status:${RST}
    launchctl print ${DOMAIN}/${LABEL} | grep -E 'state|pid'
    tail -f "$LOG_FILE"

${BOLD}Run a scan on demand:${RST}
    launchctl kickstart -k ${DOMAIN}/${LABEL}

${BOLD}Uninstall (full walkthrough in extractor/SELF-SERVE.md):${RST}
    launchctl bootout ${DOMAIN}/${LABEL}
    rm -f "$PLIST"
    rm -rf "$STATE_DIR"
    security delete-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT"

${DIM}Reminder: this tool is read-only on your Messages. Only music links leave
your Mac — never message text, never contacts.${RST}
DONE

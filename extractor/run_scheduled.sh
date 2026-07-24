#!/bin/bash
#
# run_scheduled.sh — launchd wrapper for the Xomtracks iMessage extractor.
#
# Invoked by the LaunchAgent com.xomware.xomtracks-extractor every 3h.
# Runs the read-only chat.db scan and pushes new music shares to the
# /shares/ingest endpoint.
#
# The ingest token is read from the macOS Keychain at RUNTIME (self-serve
# foundation Phase 3) — it is never hardcoded, never written to disk, and never
# echoed to the log. Reading from the Keychain (not AWS SSM) is what removes the
# AWS-credentials dependency that made this job unshippable to other users: a new
# user just stores THEIR per-user token in their own login Keychain (see the
# `security add-generic-password` one-liner in extractor/README.md) — no AWS
# account, no `aws` CLI, no IAM.
#
# LEGACY FALLBACK: if no Keychain item exists, fall back to AWS SSM (Dom's
# current setup) so his running job is NOT disrupted before he re-provisions onto
# a per-user token. The backend dual-accepts both (resolve_ingest_owner), so
# either path authenticates.
#
# Robustness:
#   - set -euo pipefail: fail fast, no silent errors, no unset vars.
#   - Explicit PATH: launchd gives a minimal environment, so /usr/bin (security),
#     /opt/homebrew/bin (aws) and the repo .venv are added here.
#   - cd to the repo so `python -m extractor.run` resolves the package.
#   - All stdout/stderr is appended to ~/Library/Logs/xomtracks-extractor.log.
#
set -euo pipefail

REPO_DIR="/Users/dom/Code/xomtracks-backend"
VENV_DIR="${REPO_DIR}/.venv"
LOG_FILE="${HOME}/Library/Logs/xomtracks-extractor.log"
INGEST_URL="https://api.xomtracks.xomware.com/shares/ingest"

# macOS Keychain generic-password lookup coordinates for the ingest token.
# The item is created once per user with:
#   security add-generic-password -s "xomtracks-ingest" -a "$USER" \
#     -T /usr/bin/security -U -w "<TOKEN_FROM_ingest-tokens/create>"
KEYCHAIN_SERVICE="xomtracks-ingest"
KEYCHAIN_ACCOUNT="${USER:-$(id -un)}"

# Legacy SSM fallback (Dom's current setup only).
SSM_PARAM="/xomtracks/ingest/BEARER_KEY"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"

# security (Keychain) + Homebrew (aws) + venv bin on PATH ahead of the
# launchd-minimal default.
export PATH="${VENV_DIR}/bin:/usr/bin:/opt/homebrew/bin:/bin:/usr/sbin:/sbin"

# Send everything (this script's stdout + stderr, and the child process's)
# to the log file, appended, with timestamps on our own markers.
mkdir -p "$(dirname "${LOG_FILE}")"
exec >>"${LOG_FILE}" 2>&1

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }

log "=== extractor run start (pid $$) ==="

cd "${REPO_DIR}"

# Activate the repo virtualenv.
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

# Resolve the ingest token. Prefer the Keychain (per-user, no AWS dependency);
# fall back to AWS SSM (Dom's legacy setup). Capture into a variable so the
# secret never lands on the command line / process list and never hits the log.
BEARER_KEY=""
if BEARER_KEY="$(security find-generic-password \
        -s "${KEYCHAIN_SERVICE}" \
        -a "${KEYCHAIN_ACCOUNT}" \
        -w 2>/dev/null)" && [[ -n "${BEARER_KEY}" ]]; then
    log "ingest token loaded from Keychain (service=${KEYCHAIN_SERVICE})"
else
    log "no Keychain item (${KEYCHAIN_SERVICE}/${KEYCHAIN_ACCOUNT}) — falling back to SSM"
    if ! BEARER_KEY="$(aws ssm get-parameter \
            --name "${SSM_PARAM}" \
            --with-decryption \
            --query Parameter.Value \
            --output text)"; then
        log "ERROR: no Keychain token and failed to fetch ${SSM_PARAM} from SSM — aborting run"
        exit 1
    fi
fi

if [[ -z "${BEARER_KEY}" || "${BEARER_KEY}" == "None" ]]; then
    log "ERROR: resolved an empty ingest token (Keychain + SSM) — aborting run"
    exit 1
fi

set +e
python -m extractor.run \
    --ingest-url "${INGEST_URL}" \
    --bearer-key "${BEARER_KEY}"
status=$?
set -e

# Scrub the secret from the environment as soon as we're done with it.
unset BEARER_KEY

log "=== extractor run end (exit ${status}) ==="
exit "${status}"

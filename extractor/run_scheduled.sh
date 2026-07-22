#!/bin/bash
#
# run_scheduled.sh — launchd wrapper for the Xomtracks iMessage extractor.
#
# Invoked by the LaunchAgent com.xomware.xomtracks-extractor every 3h.
# Runs the read-only chat.db scan and pushes new music shares to the
# /shares/ingest endpoint.
#
# The scoped ingest bearer key is fetched from AWS SSM at RUNTIME — it is
# never hardcoded, never written to disk, and never echoed to the log.
#
# Robustness:
#   - set -euo pipefail: fail fast, no silent errors, no unset vars.
#   - Explicit PATH: launchd gives a minimal environment, so /opt/homebrew/bin
#     (aws) and the repo .venv are added here.
#   - cd to the repo so `python -m extractor.run` resolves the package.
#   - All stdout/stderr is appended to ~/Library/Logs/xomtracks-extractor.log.
#
set -euo pipefail

REPO_DIR="/Users/dom/Code/xomtracks-backend"
VENV_DIR="${REPO_DIR}/.venv"
LOG_FILE="${HOME}/Library/Logs/xomtracks-extractor.log"
INGEST_URL="https://api.xomtracks.xomware.com/shares/ingest"
SSM_PARAM="/xomtracks/ingest/BEARER_KEY"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"

# Homebrew (aws) + venv bin on PATH ahead of the launchd-minimal default.
export PATH="${VENV_DIR}/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

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

# Fetch the ingest bearer key from SSM at runtime. Capture into a variable so
# the secret never lands on the command line / process list of the extractor
# and never appears in the log.
if ! BEARER_KEY="$(aws ssm get-parameter \
        --name "${SSM_PARAM}" \
        --with-decryption \
        --query Parameter.Value \
        --output text)"; then
    log "ERROR: failed to fetch ${SSM_PARAM} from SSM — aborting run"
    exit 1
fi

if [[ -z "${BEARER_KEY}" || "${BEARER_KEY}" == "None" ]]; then
    log "ERROR: SSM returned empty bearer key — aborting run"
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

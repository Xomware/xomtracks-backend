"""
XOMTRACKS Constants
===================
Values sourced from environment variables (set by Terraform at deploy time),
with safe local defaults for tests. Ported from xomify-backend/xomforms-backend
convention.
"""

import os

AWS_DEFAULT_REGION = 'us-east-1'
AWS_ACCOUNT_ID = os.environ.get('AWS_ACCOUNT_ID', '')
PRODUCT = 'xomtracks'

RESPONSE_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Content-Type": "application/json",
}

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# ============================================
# DynamoDB
# ============================================
DYNAMODB_KMS_ALIAS = os.environ.get('DYNAMODB_KMS_ALIAS', '')
SHARES_TABLE_NAME = os.environ.get('SHARES_TABLE_NAME', '')

# GSI-1 on xomtracks-shares: PK direction, SK messageDate -- time-window
# query per direction (MVP: GET /shares?direction=&window=).
SHARES_DIRECTION_INDEX = os.environ.get('SHARES_DIRECTION_INDEX', 'direction-messageDate-index')

# GSI-2 on xomtracks-shares: PK sharerHandle, SK messageDate -- reserved for
# the by-sharer fast-follow (FF.2). Table + index are provisioned now; no
# handler queries it yet.
SHARES_SHARER_INDEX = os.environ.get('SHARES_SHARER_INDEX', 'sharerHandle-messageDate-index')

# xomtracks' OWN Spotify-connected service-account user row (self-contained
# per PLAN.md Option 3 -- this is NOT xomify's users table). A single row,
# keyed by email, holds the refresh token the app plays/searches/builds
# playlists through.
USERS_TABLE_NAME = os.environ.get('USERS_TABLE_NAME', '')
APP_SERVICE_USER_EMAIL = os.environ.get('APP_SERVICE_USER_EMAIL', '')

# ============================================
# Auth
# ============================================
# Extractor -> POST /shares/ingest auth: a scoped key in SSM, sent as a
# bearer token. Separate from the per-user JWT auth_login mints.
INGEST_BEARER_KEY_PARAM = os.environ.get('INGEST_BEARER_KEY_PARAM', f'/{PRODUCT}/ingest/BEARER_KEY')

# ============================================
# Matching
# ============================================
PLATFORMS = ('spotify', 'soundcloud', 'apple')
MATCH_STATUSES = ('pending', 'matched', 'unmatched', 'manual')

# rapidfuzz token_set_ratio (0-100) threshold above which an SC/Apple ->
# Spotify search result is accepted as a match. Tuned against a real
# backfill sample per PLAN.md Open Questions -- 80 is a conservative
# starting default (title+artist token overlap must be quite close).
MATCH_CONFIDENCE_THRESHOLD = float(os.environ.get('MATCH_CONFIDENCE_THRESHOLD', '0.80'))

# ============================================
# Misc
# ============================================
XOMTRACKS_URL = "https://xomtracks.xomware.com"

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

# xomtracks-ratings: whole-group song ratings keyed per (track, user).
# PK trackKey (normalized SONG identity, see track_key.derive_track_key),
# SK raterEmail (Cognito). One rating per user per song; aggregate {avg,count}
# computed by querying the trackKey partition. See ratings_dynamo.py.
RATINGS_TABLE_NAME = os.environ.get('RATINGS_TABLE_NAME', '')

# xomtracks-heard: per-(track, user) LISTEN state -- a sibling table to
# xomtracks-ratings with the identical key shape (PK trackKey, SK raterEmail),
# so a member's "heard" flag follows the SONG across all of its share instances.
# attrs: trackKey, raterEmail, heard (bool), heardAt (epoch, "when heard"),
# updatedAt. See heard_dynamo.py. Backs POST /heard/set, the auto-heard cron,
# and the inline `heard` enrichment on /shares/list + /me/shares.
HEARD_TABLE_NAME = os.environ.get('HEARD_TABLE_NAME', '')

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
# Playlists
# ============================================
# Rolling "last 30 days" playlist ids are runtime-managed by the
# rolling-playlists cron and persisted in SSM (not the users row) -- see
# xomtracks-infrastructure ssm.tf. Values start as the "unset" placeholder;
# the cron creates each playlist on first run and PutParameters the id back.
PLAYLISTS_SSM_ROOT = f'/{PRODUCT}/playlists/'
ROLLING_IN_PLAYLIST_PARAM = os.environ.get(
    'ROLLING_IN_PLAYLIST_PARAM', f'{PLAYLISTS_SSM_ROOT}ROLLING_IN_PLAYLIST_ID'
)
ROLLING_OUT_PLAYLIST_PARAM = os.environ.get(
    'ROLLING_OUT_PLAYLIST_PARAM', f'{PLAYLISTS_SSM_ROOT}ROLLING_OUT_PLAYLIST_ID'
)
# Non-empty placeholder written by Terraform (a real empty string trips SSM
# validation) -- treated as "no playlist created yet" by the cron.
PLAYLIST_ID_UNSET = 'unset'

# Trailing window (days) for both rolling playlists.
ROLLING_WINDOW_DAYS = int(os.environ.get('ROLLING_WINDOW_DAYS', '30'))

# Playlist names, keyed by share direction ('in' = shared with Dom,
# 'out' = shared by Dom). Public on Dom's profile.
ROLLING_PLAYLIST_NAMES = {
    'in': 'Xomtracks — Shared With Me (Last Month)',
    'out': 'Xomtracks — Shared By Me (Last Month)',
}

# Match statuses whose shares carry a real resolvedSpotifyUri and are thus
# eligible for playlists (pending/unmatched never are).
PLAYABLE_MATCH_STATUSES = ('matched', 'manual')

# ============================================
# Auto-heard cron
# ============================================
# The Cognito email the auto-heard cron marks recently-played tracks heard
# FOR. This is Dom's Cognito LOGIN email (NOT the Spotify service-account row
# email, APP_SERVICE_USER_EMAIL) -- the heard flag must be keyed by the email
# a user signs into the app with so it surfaces in that user's own "unheard"
# filter on /shares/list. Dom-only for now; per-user Spotify OAuth (which would
# map each member's recently-played to their own Cognito email) is a documented
# fast-follow. Set via env by Terraform (locals.tf lambda_variables).
AUTO_HEARD_RATER_EMAIL = os.environ.get('AUTO_HEARD_RATER_EMAIL', '')

# ============================================
# Misc
# ============================================
XOMTRACKS_URL = "https://xomtracks.xomware.com"

# xomtracks-backend

> iMessage music-share tracker — backend (Lambdas + local extractor).

## What This Is
Python Lambda backend for Xomtracks. Vendors trimmed copies of xomify's
Spotify OAuth + playlist code (self-contained, own token-keepalive — does
NOT import cross-repo from xomify-backend). Owns the cross-platform
matching module (Spotify/SoundCloud/Apple Music -> Spotify) and the rolling
weekly playlist crons. Also houses `extractor/` — the local, read-only
`chat.db` reader (launchd job on Dom's primary macOS login, NOT a Lambda).
See `docs/features/xomtracks/PLAN.md`.

## Stack
- Python 3.12, AWS Lambda, DynamoDB, Pydantic 2.8

## Key Commands
```bash
pip install -r requirements.txt
./run_tests.sh
```

## Deploy
`.github/workflows/deploy-backend.yml` (ported from xomify-backend's proven
mechanism) deploys real Lambda code via `aws lambda update-function-code` on
push to `master`, or manually via `workflow_dispatch` (`deploy_mode: all`
for a full redeploy). Packages `lambdas/common/` + pinned deps as the
`xomtracks-shared-packages` Lambda layer, same as xomify. Only deploys
handler folders that actually exist under `lambdas/` — the authorizer and
3 cron Lambdas (Terraform-provisioned, stub zips) stay untouched until
their Python source is written.

## Project Config
```yaml
pm_tool: github-projects
github_project_number: 2
github_project_owner: Xomware
base_branch: master
test_commands:
  - ./run_tests.sh
```

## Constraints
- Vendored `spotify.py`/`playlist.py` are copies, not shared imports — sync
  by hand if xomify's token flow changes (accepted drift, see PLAN.md Risks).
- Xomtracks has its OWN token-keepalive and users/token row — does not touch
  xomify's.
- `create_playlist` flag is parameterized; xomtracks defaults `public=True`
  for all three playlists (both rolling + on-the-spot).
- Cognito: reuse the SHARED `xomware_users` pool (data_cognito.tf in the
  infra repo) — gates the frontend route, not this API. This API's own
  auth is a homegrown HS256 JWT (`auth_login`, ported from xomify) plus a
  separate scoped SSM bearer key for the extractor's ingest push.
- `extractor/` is READ-ONLY against `chat.db` — never sends iMessages, never
  writes to the DB. Tracks progress by `message.ROWID` (insert order), not
  `message.date`, so iCloud history backfill (new ROWIDs, old dates) is
  picked up automatically on the next scan.

## Lessons
- `attributedBody` (link-preview URLs) requires parsing beyond `text` —
  `text`-only matching missed 100% of real link-preview shares in
  verification (0 vs 13 with `attributedBody` included).

# Xomtracks Extractor

A **local, read-only** Python script that scans `~/Library/Messages/chat.db`
for Spotify / SoundCloud / Apple Music links shared across **every**
iMessage conversation (1:1 and group, both directions), and pushes new
finds to `POST /shares/ingest` on the xomtracks backend.

This is **not a Lambda**. It runs on Dom's primary macOS login (the `dom`
user) on this machine, as a periodic job (eventually a `launchd`
LaunchAgent -- see "Deployment" below, not installed yet).

## Why direct SQLite, not `imsg`

Per `docs/features/xomtracks/PLAN.md` Phase 2, we evaluated reusing
openclaw's `imsg` binary (`/opt/homebrew/bin/imsg`, a Homebrew-installed
CLI with `chats` / `history` / `watch` subcommands and `--json` output)
before writing a direct SQLite reader. Verified against the real `imsg
0.5.0` install on this machine:

- **Its JSON schema has no raw `attributedBody` field and no dedicated
  link/URL field** -- only `text`, `attachments`, `reactions`, and basic
  metadata (`sender`, `is_from_me`, `chat_id`, `guid`, `created_at`).
  Whether its `text` reconstruction reliably recovers a link-preview URL
  for the specific case that matters here -- `message.text` is NULL and
  the URL lives ONLY in `attributedBody` (confirmed on this exact host:
  text-only match = 0 links, text+attributedBody match = 13 links) -- is
  unverified. `imsg` is a general-purpose send/read/react CLI, not a
  structured link-extraction tool, and its docs don't claim to surface one.
- **No single "everything since ROWID X across every chat" call.**
  `imsg history` requires `--chat-id` (or `--participants`), one thread at
  a time. `imsg watch --since-rowid` is close to the right shape but is a
  **persistent streaming process**, not a fire-and-exit periodic scan --
  it doesn't fit the plan's "launchd runs it every 30-60 min" architecture
  without wrapping it in another layer of process management.
- **Coupling to a parked stack.** `imsg` is openclaw's tooling, and
  openclaw is explicitly parked per the current host-reality update (its
  LaunchAgent was `launchctl bootout`'d; "do NOT touch or re-enable it").
  Reusing its binary would blur the boundary the plan wants kept: the
  extractor is meant to be fully self-contained inside `xomtracks-backend`.

**Decision: read `chat.db` directly** via Python's stdlib `sqlite3` module,
opened strictly `mode=ro`. This gives full, unambiguous control over
exactly which columns are read (`text` AND `attributedBody`, joined across
`chat`/`chat_message_join`/`handle`) -- which is what the original host
verification (the 13-link count) actually used, and what the plan's design
requirements explicitly call for. See `chat_reader.py` and
`url_extractor.py` for the implementation.

## Modules

| File | Responsibility |
|------|-----------------|
| `chat_reader.py` | Opens `chat.db` read-only; `fetch_new_messages()` scans ALL conversations since a `ROWID` watermark; Apple-epoch -> Unix conversion. |
| `url_extractor.py` | Regexes music URLs out of `text` AND `attributedBody` (bplist NSKeyedArchiver parse + legacy-typedstream byte-regex fallback); platform detection; strips the `WHttpURL/` typedstream tail so the two copies of a link dedup. |
| `share_builder.py` | Turns one chat_reader row into zero or more `POST /shares/ingest` body dicts (direction/sharerHandle mapping, one dict per distinct URL; resolves `sharerName` for incoming shares). |
| `contacts.py` | Read-only resolver: iMessage handle (phone/email) -> macOS Contacts display name, digit-normalized. Runs on Dom's Mac (Full Disk Access, same as chat.db). |
| `backfill_names.py` | One-shot `python -m extractor.backfill_names`: fills `sharerName` on existing xomtracks-shares rows from local Contacts. |
| `watermark.py` | Persists the last-processed `ROWID` to `~/.xomtracks/extractor_state.json`. |
| `ingest_client.py` | POSTs one share to the backend with the ingest token (bearer); never reads anything back beyond the HTTP status. |
| `run.py` | Orchestrates one scan (`run_once`) + CLI entrypoint (`main`); builds the Contacts resolver so live scans attach `sharerName`. |
| `logging_setup.py` | Standalone logger (stdout + optional file), deliberately decoupled from `lambdas.common.logger`. |

## ROWID-based watermark (not date)

Tracked by `message.ROWID` (insert order), **not** `message.date`. When
Messages-in-iCloud backfills older history, those messages are inserted
into `chat.db` with **new, high ROWIDs but old dates**. A date-based
watermark would silently never see them (their date is already "in the
past" relative to what's been processed); the ROWID-based watermark picks
them up automatically on the very next scan, since ROWID only reflects
"have I seen this row before" -- see `tests/test_extractor_run.py`'s
`TestBackfillPickedUpByRowidNotDate` for the exact scenario this protects
against.

## Failure semantics

Shares within one message are pushed to the backend in order; if any push
for a message fails (host asleep, network blip, backend down), the whole
scan stops there and the watermark is saved at the **last fully-successful
message's** ROWID -- not the failed one. The next scan retries the failed
message in full rather than silently skipping it. Ingest is idempotent
server-side (keyed on `messageGuid` + `sourceUrl`), so retried pushes never
create duplicate rows.

## Ingest token (macOS Keychain — per-user auth)

The extractor authenticates to `POST /shares/ingest` with a **per-user ingest
token** (self-serve foundation Phase 3). The token is opaque and revocable; the
backend stores only its SHA-256 hash and resolves the presenting token to an
`ownerId`, so each user's shares are stamped with the right owner.

`run_scheduled.sh` reads the token from the **macOS Keychain** at runtime — NOT
from AWS SSM. This is the change that makes the extractor shippable to other
people: a new user needs no AWS account, no `aws` CLI, and no IAM — just their
own token in their own login Keychain.

**One-time setup for a new user:**

1. Sign into Xomtracks and mint a token — `POST /ingest-tokens/create` returns
   the plaintext token **exactly once** (copy it immediately; it is never
   shown again).
2. Store it in the login Keychain (paste the token in place of `<TOKEN>`):

   ```bash
   security add-generic-password \
     -s "xomtracks-ingest" \
     -a "$USER" \
     -T /usr/bin/security \
     -U \
     -w "<TOKEN>"
   ```

   - `-s "xomtracks-ingest"` / `-a "$USER"` — the service/account
     `run_scheduled.sh` looks up (`security find-generic-password -s
     xomtracks-ingest -a "$USER" -w`).
   - `-T /usr/bin/security` — trusts the `security` binary the script uses to
     read it, so the launchd-run scan reads it without a GUI prompt.
   - `-U` — updates the item in place if it already exists (re-provisioning /
     token rotation).
   - `-w "<TOKEN>"` — the secret value.

To **rotate**: revoke the old token (`POST /ingest-tokens/revoke`), mint a new
one, and re-run the `add-generic-password` command (with `-U`).

**Legacy fallback (Dom only):** if no Keychain item exists, `run_scheduled.sh`
falls back to `aws ssm get-parameter /xomtracks/ingest/BEARER_KEY`. The backend
dual-accepts that legacy SSM key (mapping it to Dom's `ownerId`), so Dom's
running job keeps working unchanged until he re-provisions onto a per-user token
via the Keychain command above.

## Read-only, one-way

The extractor never writes to `chat.db` (`open_read_only_connection`
enforces `mode=ro`) and never sends iMessages. It only reads and pushes
music-link records to the backend.

## Running it manually (local validation)

```bash
cd xomtracks-backend
source .venv/bin/activate
python -m extractor.run \
  --ingest-url https://api.xomtracks.xomware.com/shares/ingest \
  --bearer-key <scoped SSM ingest key>
```

Defaults to `~/Library/Messages/chat.db` and
`~/.xomtracks/extractor_state.json` if `--db-path`/`--state-path` aren't
given. **Not runnable in this agent's sandboxed shell** -- reading
`chat.db` requires Full Disk Access, which is granted to `Terminal.app` on
this login but not to this tool's subprocess. Run it from an actual
Terminal window instead.

## Contact-name resolution + one-time backfill

Sharers were showing as raw phone numbers because the extractor stored only
`sharerHandle`. `contacts.py` resolves each handle against the local macOS
Contacts DBs (`~/Library/Application Support/AddressBook/**/AddressBook-v22.abcddb`,
Full-Disk-Access gated -- **local to Dom's Mac only**) and `run.py` now
attaches `sharerName` on every new scan.

Existing rows need a one-time backfill. It **must run from a real Terminal
on Dom's Mac** (Contacts is local + FDA-gated; a cloud/CI/agent host can't
read it) and needs AWS credentials on the host (it `UpdateItem`s
`xomtracks-shares` directly -- `/shares/ingest` is a conditional put and
would no-op existing rows). Preview first, then apply:

```bash
cd xomtracks-backend
source .venv/bin/activate

# dry run -- shows how many rows would get a name, writes nothing
python -m extractor.backfill_names --dry-run

# apply
python -m extractor.backfill_names
```

`--force` overwrites names already set; `--table`/`--region` override the
defaults (`xomtracks-shares` / `us-east-1`).

## Deployment (NOT done yet -- explicitly out of scope for this pass)

Per `PLAN.md` Phase 2.6, this becomes a `launchd` LaunchAgent (e.g. every
30-60 min) under Dom's own login. Two Dom-side steps when that happens:

1. **Full Disk Access for the launchd runtime.** Terminal already has FDA
   on this login (verified: sqlite reads work from Terminal). The
   `launchd` job's own runtime -- the venv's `python3` binary, specifically
   -- will ALSO need FDA granted separately (System Settings -> Privacy &
   Security -> Full Disk Access -> add the venv's `python3`), since TCC
   grants are per-binary, not inherited from Terminal.
2. Install the LaunchAgent plist (not written yet), pointing at
   `python -m extractor.run` with the real `--ingest-url`/`--bearer-key`
   (SSM-sourced), logging to `EXTRACTOR_LOG_PATH`.

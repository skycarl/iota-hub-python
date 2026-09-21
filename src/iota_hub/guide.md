# IOTA Hub workflow guide (CLI first)

The guide `iota-hub guide` prints: the same material as
<https://iota-hub.com/developers/guide.md>, ordered for the command-line tool
instead of for raw HTTP.

The facts here are owned by `specs/public-api.md` in the IOTA Hub repository.
What a *client* does with them - configuration, retries, polling, folder
mapping, exit codes - is owned by `docs/client-conventions.md` in
`skycarl/iota-hub-python`.

Each step gives the command first, then the library call, then the underlying
HTTP call for anyone who has to reimplement it.

## What this is

IOTA Hub is where observers of asteroid occultations submit their observations
to IOTA. One observation is a report (`.xlsx`), a light curve (`.csv`), an
instrument log (`.txt`) and optionally a VizieR file (`.dat`), put through the
observer checks and then reviewed. `iota-hub` is the command-line tool and
Python library for its public API v1: create a draft, upload the files, run the
checks, fix or dismiss what they find, submit.

## Install

    uvx iota-hub --version            # run it without installing
    uv tool install iota-hub          # or put it on your PATH, with uv
    pipx install iota-hub             # or with pipx

Python 3.10 or newer, on Windows, macOS or Linux.

## Authenticate

Create a key on your IOTA Hub profile page, in the API Keys card. It is shown
once and stored hashed; up to 10 per account. Scopes are fixed at creation
(`read` for every read, `observations:write` for every write), as is the
lifetime (30 days, 90 days, 1 year by default, or never). A key acts as its
owner: exactly what that account can do in the web app, nothing more. Give it
to the tool one of two ways, never as a command-line argument - that would land
in your shell history and in `ps`:

    iota-hub auth login               # hidden prompt, stored per profile
    export IOTA_HUB_API_KEY=...       # the CI and agent path, no login step

    iota-hub auth status              # profile, base URL, key prefix, valid?
    iota-hub auth logout              # forget a stored profile

`auth status` prints only the identifying half of the key
(`iotahub_<key_id>_...`), never the secret, and makes one cheap read to say
whether the API accepts it.

Library: `Client(api_key=..., base_url=...)`, or `iota_hub.config.resolve()`
for the same flag / environment / config-file precedence.
HTTP: header `X-API-Key: iotahub_<key_id>_<secret>` on every request;
`Authorization` is ignored on this surface. See /developers/guide.md.

## Submit an observation

One command, run in the folder holding one observation:

    cd 20180305_9721_Doty_Observer_POS
    iota-hub submit

That is the whole job: create the draft, upload every mapped file straight to
storage, finalize, wait for the observer check run, and submit when - and only
when - the draft comes back `ready`. A draft it cannot submit is left in your
Drafts, never deleted: it is recoverable in the web app.

    iota-hub submit --draft           # everything except the final submit
    iota-hub submit --dry-run         # print the mapping and target, call nothing
    iota-hub submit --no-wait         # return after finalize with the draft id
    iota-hub submit --timeout 900     # seconds to wait for checks (default 600)
    iota-hub submit path/to/folder    # a folder other than the current one

### Which file goes where

Files map to slots by rules only - no classification, no content sniffing. The
observer naming convention (`YYYYMMDD_<number>_<name>_<lastname>_POS|NEG[-X]`)
already carries the signal.

| Slot         | Rule                                          | Required |
| ------------ | --------------------------------------------- | -------- |
| `report`     | exactly one `.xlsx` / `.xls`                  | yes      |
| `lightcurve` | exactly one `.csv`                            | yes      |
| `log`        | exactly one `.txt` whose name contains `log`  | yes      |
| `vizier`     | at most one `.dat`                            | no       |

The listing is non-recursive and skips subdirectories and dotfiles; extensions
match case-insensitively, so `.CSV` is a light curve. Anything the rules do not
claim is reported as "not uploaded" and left alone: the public surface has no
attachment slot, so attach those in the web app if a reviewer needs them.
Ambiguity is an error, never a guess - two candidates for one slot, or none for
a required one, stops the command, names the files, and names the flag that
settles it:

    iota-hub submit --lightcurve 20180305_9721_Doty_Observer_POS.csv

The overrides are `--report`, `--lightcurve`, `--log` and `--vizier`, each
checked for the slot's extension only, so `--log notes.txt` is accepted. One
observation per directory: a folder holding several stations (`_POS-1`,
`_POS-2`) is reported as two reports, and is not supported in v1.

Library: `client.submit_folder(dir, lightcurve=..., wait=True, submit=True)`;
also `submit_files({"report": path, ...})` and `iota_hub.files.map_folder(dir)`.
HTTP: `POST /observations/drafts`, a presigned multipart POST to S3 per file,
`POST /observations/{id}/finalize`, `GET /observations/{id}` until the run is
terminal, `POST /observations/{id}/submit`. See /developers/guide.md.

## Read the result

`submit` ends in one of four outcomes. This is the vocabulary to branch on.

| Outcome           | Meaning                                              | Exit |
| ----------------- | ---------------------------------------------------- | ---- |
| `submitted`       | the observation is submitted                          | 0    |
| `ready`           | clean, but `--draft` left it a draft                  | 0    |
| `needs_attention` | open findings, something missing, or the run errored  | 4    |
| `draft`           | `--no-wait`: uploaded and finalized, not waited on    | 0    |

`readiness.state` is `in_progress`, `checking`, `needs_attention` or `ready`;
`readiness.missing_required` names what is missing, with stable keys - the file
slots, and the identity fields parsed from the report (`result_type`,
`asteroid_id`, `asteroid_id_confirmation`, `asteroid_name`, `star`,
`observed_at_utc`, `observer_name`, `observer_email`), which reappear there if
the report will not parse. Each finding carries `check_number`, `code`, `name`,
`severity`, `message`, `evidence`, `how_to_fix`, `doc_url`, and a `fingerprint`
- the id you dismiss.

Human output prints the literal next command for every action the API says is
available (`iota-hub drafts files add <id> lightcurve <path>`,
`iota-hub drafts dismiss <id> <fingerprint> --note "..."`,
`iota-hub drafts check <id>`, `iota-hub drafts submit <id>`), so nothing has to
be translated. Two actions have no public API verb and must be finished in the
web app: confirming an unnumbered asteroid id, and resolving an event-files
conflict. The tool says so rather than pretending otherwise.

Library: `SubmitResult` carries `.outcome`, `.observation`, `.uploads` and
`.ignored`; `iota_hub.workflow.next_actions_commands(obs)` returns the commands.
HTTP: `GET /observations/{id}`, the self-describing resource - read `readiness`,
`checks` and `next_actions`. See /developers/guide.md.

## Fix and resubmit

    iota-hub drafts list                                  # your open drafts
    iota-hub drafts show <id>                             # state and findings
    iota-hub drafts files add <id> lightcurve <path>      # replace a file
    iota-hub drafts files rm <id> vizier                  # clear a slot
    iota-hub drafts dismiss <id> <fingerprint> --note "why this is acceptable"
    iota-hub drafts dismiss <id> <fingerprint> --undo     # undo a dismissal
    iota-hub drafts check <id>                            # start a check run
    iota-hub drafts submit <id>                           # submit when clean
    iota-hub drafts delete <id> --yes                     # throw a draft away

- Replacing a file re-arms the checks by itself, so an explicit `drafts check`
  is usually unnecessary.
- A dismissal takes one finding at a time and a note of at least 3 characters.
  Dismissals made with a key record that, and the key id, beside the note.
- A finding about a missing required item cannot be dismissed: supply the item.
- Clearing the `report` slot also clears the identity fields parsed from it,
  and only a draft can be deleted - a submitted observation cannot.

Library: `client.replace_file(id, slot, path)`, `delete_file`,
`dismiss_finding(id, fingerprint, note)`, `undo_dismissal`, `run_checks`,
`wait_for_checks`, `submit_draft(id, version=...)`, `delete_draft`.
HTTP: `POST /observations/{id}/files/{slot}/upload/init` and `.../finalize`,
`DELETE /observations/{id}/files/{slot}`,
`POST /observations/{id}/validation/findings/{fingerprint}/dismiss` (`DELETE`
to undo), `POST /observations/{id}/validation/runs`,
`POST /observations/{id}/submit`, `DELETE /observations/{id}`. See
/developers/guide.md.

## Reads and downloads

    iota-hub observations list --submission-status draft
    iota-hub observations show <id>
    iota-hub events list --date-from 2024-01-01 --date-to 2024-12-31
    iota-hub events show <id>
    iota-hub files download observation <id> -o ./downloads
    iota-hub files download event <id> --slot lightcurve -o ./downloads --force

Observation filters: `observer_names`, `event_status_group`, `submission_status`,
`asteroid_id`, `asteroid_name`, `star_id`, `result_type`, `ungrouped_only`,
`date_from`, `date_to`, `sort_by`, `sort_order`, `limit`, `cursor`. Event
filters: `status`, `asteroid_id`, `asteroid_name`, `star_id`, `date_from`,
`date_to`, `observer_names`, `waiting_for`, `assigned_reviewer_id`, `sort_by`,
`sort_order`, `limit`, `cursor`.

- `--submission-status draft` lists only your own drafts, with readiness.
  Pages are cursor based; `limit` defaults to 25 and is capped at 100.
- Download links are presigned and short lived, and are fetched without the API
  key. Event files are private until the event is complete; a caller who may
  not see them gets an empty list, not an error.
- A download never silently overwrites: an existing destination stops the
  command unless you pass `--force`, and every destination is checked before
  the first byte is written. A resource that is not yours reads as not found,
  never as forbidden.

Library: `client.iter_observations(...)` and `iter_events(...)` follow the
cursor for you; also `list_observations`, `list_events`, `get_observation`,
`get_event`, and `download_files(kind, id, dest, slot=..., overwrite=...)`.
HTTP: `GET /observations`, `GET /observations/{id}`, `GET /events`,
`GET /events/{id}`, `GET /observations/{id}/files`, `GET /events/{id}/files`.
See /developers/guide.md.

## Automation and agents

- `--json` works on every command: exactly one JSON document on stdout and
  nothing else, with progress and logs on stderr. An error is then JSON on
  stderr in the problem-details shape plus the exit code:
  `{"code": "...", "message": "...", "hint": "...", "details": {}, "exit_code": 4}`.
- Branch on `code` and on the exit code, never on a message. Messages are for
  people and are free to change. Exit codes: `0` success, `1` error, `2` usage
  (bad flags, or an ambiguous or incomplete mapping), `3` auth, `4` open
  findings or not ready, `5` rate limited.
- Retries (429 and 5xx, honouring `Retry-After`), one idempotency key per
  logical call reused across its retries, and the base64 SHA-256 declared on
  every upload are handled for you. Do not wrap a mutating command in your own
  retry loop: a retry with a fresh key is a second write.
- The tool never prompts when stdin is not a TTY; destructive commands take
  `--yes`. No color when piped or when `NO_COLOR` is set, and default output is
  plain ASCII so a non-UTF-8 Windows console survives it.
- `iota-hub --version` prints the package version and the API version it
  targets. `SKILL.md` in `skycarl/iota-hub-python` is a skill for an agent.

Limits, per hour except the last:

| Limit       | Value     | Keyed on | Applies to                                  |
| ----------- | --------- | -------- | ------------------------------------------- |
| Uploads     | 100       | user     | create, finalize, per-slot upload, submit   |
| Mutations   | 200       | user     | delete a file or draft, dismiss or undo, run |
| Reads       | 5000      | user     | every read                                  |
| Check runs  | 100       | key      | explicit check runs, not implicit ones      |
| Open drafts | 100 total | user     | draft creation, web and API alike           |

## Errors

Every failure carries a stable `code`. These are the ones an observer meets.

| `code`                 | Exit | What it means                                              |
| ---------------------- | ---- | ---------------------------------------------------------- |
| `missing_api_key`      | 3    | No key configured. Run `iota-hub auth login`.               |
| `unknown_api_key`      | 3    | No such key, or it was revoked. Create a new one.           |
| `expired_api_key`      | 3    | The key expired. Create a new one on your profile page.     |
| `insufficient_scope`   | 3    | This key lacks `observations:write` or `read`.              |
| `not_found`            | 1    | Wrong id, or not visible to your account.                   |
| `missing_required`     | 4    | Supply everything in `readiness.missing_required`.          |
| `open_findings`        | 4    | Fix or dismiss each open finding, then submit.              |
| `checks_stale`         | 4    | The draft changed. Run the checks again, then submit.       |
| `event_files_conflict` | 4    | Staged event files clash with an event; finish in the app.  |
| `timeout`              | 4    | Checks still running. `iota-hub drafts show <id>`.          |
| `not_dismissable`      | 1    | The finding is a missing required item. Supply it.          |
| `rate_limited`         | 5    | Still limited after the built-in retries. Wait and retry.   |
| `ambiguous_files`      | 2    | Two candidates for one slot. Name it with the slot's flag.  |
| `missing_files`        | 2    | A required slot has no file, or a named path is not there.  |
| `invalid_extension`    | 2    | A named file has the wrong extension for its slot.          |
| `not_a_directory`      | 2    | The path to submit is not a directory.                      |
| `unknown_profile`      | 2    | No such profile in the config file.                         |
| `internal_error`       | 1    | Something failed on our side. Retry; then report it.        |

The full table, including codes only a raw HTTP caller can hit, is in
/developers/guide.md.

## Environments and profiles

The default target is production, `https://api.iota-hub.com`. The public API is
not live in production yet; until it is, the development deployment is where
third-party developers build. Its base URL is published on that site's
`/developers` page, and no non-production URL is baked into this package.

    iota-hub auth login --profile dev --base-url <the dev base URL>
    iota-hub --profile dev observations list      # or IOTA_HUB_PROFILE=dev

A profile stores a key and its base URL together, so a dev key is never sent to
production or the reverse. `--base-url` (and `IOTA_HUB_BASE_URL`) is the API
base; the client appends `/public/v1` itself, and local is
`--base-url http://localhost:8000`. Whenever the target is not the production
default, the tool prints `Target: <base URL>` on stderr before acting.

Settings resolve flag > environment > config file > default. The config file
lives where the platform puts configuration:

    Linux     ~/.config/iota-hub/config.toml
    macOS     ~/Library/Application Support/iota-hub/config.toml
    Windows   %APPDATA%\iota-hub\config.toml

On POSIX it is written `0600`; on Windows the protection is the ACL on your
user profile directory, which the tool does not set itself. No OS keyring in
v1.

## Where the rest lives

- What belongs in each file, and the naming convention:
  <https://iota-hub.com/data-submission>
- Quickstart, tables and the interactive HTTP reference:
  <https://iota-hub.com/developers>, `/developers/reference`
- The raw HTTP walkthrough this guide is derived from, and the OpenAPI
  document: `/developers/guide.md`, `/developers/openapi.json`
- Client source, `SKILL.md`, client conventions:
  <https://github.com/skycarl/iota-hub-python>

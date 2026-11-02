# IOTA Hub client conventions

The language-neutral contract every IOTA Hub client implements. `iota-hub-python`
is the reference implementation; a future `iota-hub-js` is a **port of this
document**, not a second design (IOTA Hub `features/public-api/design.md` D15,
§ 8.6).

What the API does is owned by the IOTA Hub repository — `specs/public-api.md`
and its machine-readable rendering, the OpenAPI document vendored here as
[`spec/openapi.json`](../spec/openapi.json). This document owns only what a
*client* does: how it is configured, how it retries, how it polls, how it maps
a folder to slots, and what it prints.

Where the design left a choice open, this document makes it and says so.

---

## 1. Two layers

| Layer | What it is | How it is built |
|---|---|---|
| **Transport** | One method per public `operation_id`, one request each, no decisions. | May be generated from `spec/openapi.json`. Python hand-writes it to keep the dependency set small and the cold start fast — but it lands on the same names either way. |
| **Workflow** | Everything the API cannot do for you: submit a folder, poll a run, download files, retry, upload to S3, map errors. | **Always hand-written**, in every language, to this document. |

The seam matters: nothing in the transport layer may make a policy decision
(what to retry, how long to wait, which file is the light curve), and nothing in
the workflow layer may talk HTTP directly.

**Transport surface** (sync; an async client is an addition, never a
replacement):

- one method per operation, plus `iter_<collection>` generators that follow
  `next_cursor` until it is null and yield the individual items,
- an `upload_to_s3(target, file)` that implements § 7,
- a `sha256_base64(path)` helper,
- an explicit `close()`, and the language's scope-bound equivalent.

**Workflow surface** (the names are part of this contract):

- `submit_folder(dir, …)` / `submit_files({slot: path}, …)` — map, create,
  upload, finalize, wait, submit,
- `wait_for_checks(observation_id, …)` — § 8,
- `replace_file(observation_id, slot, path)` — the fix loop's init → upload →
  finalize, in one call,
- `add_attachment(observation_id, path)` — the same for one attachment (§ 11a);
  removing one is the transport's `delete_attachment(observation_id,
  attachment_id)`,
- `download_files("observation" | "event", id, dest, …)` — list the files,
  optionally filter by `slot` — **any** slot the listing names, not only the
  five upload slots (an event carries `damit`, `ground_track`, `attachment`) —
  and stream each presigned link into `dest`
  **without the API key** (§ 5). Nothing is skipped silently: an existing file
  is `file_exists` unless `overwrite` was asked for — `--force` on the CLI —
  (and every destination is
  checked before the first byte is written, so a refusal leaves no half-done
  download), a directory named like a file being downloaded is `not_writable`
  with or without `overwrite`, a link S3 refuses is `download_failed`, and a
  network failure is `transport_error` (not retried, like an S3 upload, § 6:
  running the command again fetches fresh links). A file is written to a temp
  file in `dest` and renamed over its name once complete, so a failure never
  leaves a partial file and a failed overwrite leaves the old file as it was.
  Files finished before the failure stay, so the re-run needs `overwrite`
  (`--force`). A successful overwrite replaces the file: it keeps the old
  file's mode on POSIX, needs a writable `dest`, and replaces a symlink rather
  than writing through it. An
  empty result is not a failure: `dest` is not created, the CLI exits `0`,
  `--json` prints `{"files": []}`, and plain output says so on stderr. The API's event file list is empty until the event
  is `COMPLETE` for a caller without reviewer access,
- `next_actions_commands(observation)` — the API's verbs as the literal
  commands that answer them (§ 13). It lives in the *library*, not the CLI, so
  an embedder prints the same guidance the CLI does.
- `skipped_checks(observation)`, `owc_unresolved(observation)`,
  `owc_commands(observation)` and `check_warnings(observation)` — § 8a, in the
  library for the same reason.

`submit_folder` / `submit_files` return one result object carrying the last
observation it read, `submitted`, the uploaded `{slot: filename}`, the uploaded
attachment filenames, the files the mapping ignored, the **skipped checks** and
**OWC commands** of § 8a (taken from the draft as the checks left it — a
submitted observation no longer carries the draft's run), and an **`outcome`** — the vocabulary a script branches on:

| `outcome` | Meaning |
|---|---|
| `submitted` | the observation is submitted (by this client, or by the server's `auto_submit_when_clean`) |
| `ready` | readiness is `ready`, but `submit=False` left it a draft |
| `needs_attention` | open findings, something missing, or the run errored |
| `draft` | `wait=False`: uploaded and finalized, not waited on |

Flags: `wait=False` stops after finalize, `submit=False` stops at `ready`.
**A draft that is not `ready` is never submitted**, and a failed run is never
cleaned up: the draft stays, because it is recoverable in the web app (and
`delete_draft` is one call away if the caller disagrees). A finalize that fails
leaves the same draft behind, as does an upload that fails twice, a request that
fails during the wait, or a failed submit. So every error raised after the draft
was created has `observation_id` added to its `details` before `submit_files`
re-raises it; that adds no retry and no request.

Progress is reported through an optional `on_progress` callback taking one
short string (`Uploading lightcurve (2/3)...`, `Waiting for checks...`), which
the CLI prints to stderr. Those strings are **ASCII**, like everything else a
client prints in plain output (§ 13).

## 2. Naming

A transport method's name is its `operation_id` minus the `public_` prefix:
`public_create_draft` → `create_draft`, `public_list_observation_files` →
`list_observation_files`. Adapt the case to the language's convention
(`createDraft` in JavaScript) and nothing else — never re-word, never re-group.

Model names are the OpenAPI schema names, and the field names are the wire
names, unchanged. Sixteen operations and twenty-five `Public*` schemas today.

## 3. Configuration

Precedence, highest first: **flag > environment > config file > default**.

| Environment variable | Meaning |
|---|---|
| `IOTA_HUB_API_KEY` | The key. The CI and agent path: no login step. |
| `IOTA_HUB_BASE_URL` | The API base (§ 4). |
| `IOTA_HUB_PROFILE` | Which stored profile to use. |

The config file lives in the platform config directory, as `platformdirs`
reports it:

| Platform | Path |
|---|---|
| Linux | `~/.config/iota-hub/config.toml` |
| macOS | `~/Library/Application Support/iota-hub/config.toml` |
| Windows | `%LOCALAPPDATA%\iota-hub\config.toml` |

It is TOML, and it stores the key **and** the base URL together per profile, so
a dev key is never sent to prod or the reverse:

```toml
default_profile = "default"

[profiles.default]
base_url = "https://api.iota-hub.com"
api_key  = "iotahub_<key_id>_<secret>"

[profiles.dev]
base_url = "https://<id>.execute-api.us-west-2.amazonaws.com/dev/api"
api_key  = "iotahub_<key_id>_<secret>"
```

- On POSIX the file is `0600`. On Windows the user profile directory's ACL is
  the protection, and the docs say so rather than promising permissions.
- **Written atomically.** A write goes to a temp file (`0600` from creation) in
  the same directory, then replaces the config file in one step, so a failed
  write leaves the old file whole and never an empty one. A symlinked config
  file stays a symlink: the file it points at is what gets replaced.
- **What may be written.** Strings written to the file escape `\` and `"`, so a
  base URL carrying either round-trips. A control character (U+0000–U+001F,
  U+007F) cannot go in a TOML string at all — not in the profile name, the base
  URL or the key — and a profile name is also a table header
  (`[profiles."dev"]`), so it carries neither a quote nor a backslash. Either is
  `invalid_config`, raised **before** the file is touched: the file a client
  writes always parses.
- A key is **never** accepted as a command-line argument — it would land in
  shell history and in `ps`. Prompt with hidden input, or read stdin.
- No OS keyring in v1.
- Whenever the resolved base URL is not the default, the CLI prints
  `Target: <base URL>` on **stderr** before acting, so a test run is never
  mistaken for a prod submission. `submit --dry-run` prints it even for the
  default target (§ 13). Under `--json` it is held until the run ends (§ 13).

**Precedence in detail.** The key comes from the flag, else the environment,
else the profile; the base URL from the flag, else the environment, else the
profile, else the default. *Decision:* a `--base-url` passed alongside a
profile is allowed and wins — an explicit instruction beats a stored one — and
the resolved settings say which of `flag` / `env` / `profile` the key came
from, so a client can show it. The environment does not get the same
latitude: when the key comes from a profile and `IOTA_HUB_BASE_URL` names a
different base URL than the one stored with that key, the client raises
`invalid_config` instead of sending the key there — a leftover dev variable
must never carry a prod key to dev. Setting `IOTA_HUB_API_KEY` as well, or
passing `--base-url`, settles it. A profile that was **asked for** and does not
exist is `unknown_profile`; a config file that is not valid TOML is
`invalid_config`, and so is one that cannot be read. A stored `default_profile`
that no longer exists is simply ignored, so deleting a profile never wedges the
CLI.

**Logging out never re-aims bare commands.** *Decision:* removing the profile
named by `default_profile` clears `default_profile`; a client must not pick a
replacement from what is left, because the next bare command could then reach
another deployment, possibly production. Bare commands then follow the order
below: a profile named `default` if there is one, otherwise the ordinary
`missing_api_key`. If profiles remain, `auth logout` says on stderr that no
default is set, lists them, and says what a bare command now does (held under
`--json` like any note, § 13; the document does not change). Removing the last
profile, or any other profile, has nothing to report.

**One way to select a profile.** *Decision:* every command, `auth login` and
`auth logout` included, selects `--profile`, else `IOTA_HUB_PROFILE`, else
`default_profile` (if that profile exists), else `default`. A profile named
`default` is therefore used when no `default_profile` is set.

**`auth login` resolves its target the same way, minus the profile's key.**
*Decision:* the profile it is about to write may not exist yet, so naming it
cannot be `unknown_profile`. The base URL stored with the key is the flag, else
`IOTA_HUB_BASE_URL`, else what that profile already had (a re-login keeps its
target), else the default — the § 3 order with the one profile in question
standing in for "the profile". The key itself
is read from a hidden prompt on a TTY and otherwise from one line of stdin, and is verified with
`list_observations(limit=1)` **before** anything is written, so a typo never
lands in the config file. Nothing but `key_prefix` is ever printed back. The
first login, on an empty config, writes `default` and makes it `default_profile`;
so does a login while a bare command would select no existing profile.

**Replacing a stored key is never silent.** *Decision:* when the selected
profile already holds a key, a TTY is asked first, naming the profile, the stored
key's prefix and base URL, and the base URL the new key is for; `--yes` skips the
question. Without a TTY there is nothing to ask and `confirmation_required` does
not apply: the piped key is the instruction. Either way a replacement says
`Replaced profile '<name>', which held key <prefix> for <base URL>.` on stderr,
and the `--json` document says `replaced: true`. `auth login` and `auth logout`
both print the profile they acted on.

**`auth status` shows profile, base URL and key prefix — and nothing else.**
*Decision:* the public API has no `whoami` operation (there is none in
`spec/openapi.json`), so scopes and expiry are not knowable from a key, and a
client must not invent them. The key prefix is `iotahub_<key_id>_...`: the
identifying half, never the secret. To say more than "here is what is
configured", `auth status` makes one cheap read — `list_observations(limit=1)`
— and reports `valid`, or the auth `code` the API answered with.

## 4. Base URL

`base_url` is the **API base**; the client appends `/public/v1` (design D16).
A trailing slash on the configured value is tolerated and stripped.

| Target | `base_url` | A request goes to |
|---|---|---|
| Production (default) | `https://api.iota-hub.com` | `https://api.iota-hub.com/public/v1/events` |
| Development | `https://<id>.execute-api.us-west-2.amazonaws.com/dev/api` | `…/dev/api/public/v1/events` |
| Local | `http://localhost:8000` | `http://localhost:8000/public/v1/events` |

No non-production URL is baked into a published client.

**The key never travels in cleartext.** *Decision:* a base URL must be
`https`, except `http` to `localhost`, `127.0.0.1` or `::1`. Anything else is
`invalid_config`, raised when the client is constructed, before any request.

## 5. Authentication and User-Agent

Every request carries:

- `X-API-Key: iotahub_<key_id>_<secret>` — never `Authorization`, which this
  surface ignores.
- `Accept: application/json`.
- `User-Agent: iota-hub-<lang>/<version>` — `iota-hub-python/1.2.3`,
  `iota-hub-js/1.2.3`. The library sends it whether or not the CLI is driving.
  A caller embedding the library in their own tool may override it entirely.

The key goes to the IOTA Hub API and **nowhere else**. In particular an S3
upload (§ 7) must not carry it.

## 6. Retries

Applies to every API call, and only to these cases:

| Condition | Retried? |
|---|---|
| Transport failure (DNS, connect, read, TLS) | yes |
| `429` | yes |
| `5xx` | yes |
| Any other `4xx` | **no** — the request was rejected; retrying only burns rate-limit budget |

- **Attempts**: 1 + `max_retries`, default `max_retries = 4`.
- **Backoff**: `0.5 × 2^attempt` seconds — 0.5, 1, 2, 4, 8, 16 — capped at 20 s.
  *Decision:* no jitter. A personal CLI is not a fleet, and deterministic sleeps
  keep the tests honest.
- **`Retry-After` wins** when the response carries one, in either form (seconds
  or an HTTP-date), and is used verbatim rather than being blended with the
  backoff. *Decision:* it is not capped — the server is the authority on how
  long it wants to be left alone.
- The **same `Idempotency-Key`** is sent on every attempt of one logical call
  (§ 9). A retry with a fresh key is a second write.
- After the last attempt the mapped exception is raised, carrying `retry_after`
  so the caller can decide.
- *Decision:* S3 uploads (§ 7) are **not** retried at this layer. A presigned
  target can expire, so the workflow layer re-initializes the upload instead:
  **once**, through `init_file_upload`, and then it posts the fresh target.
  A second failure raises and leaves the draft alone.
  That recovery finishes with the **per-slot** `finalize_file_upload`, not with
  the collapsed finalize: the draft's `pending_uploads` still names the target
  that expired, and only the per-slot finalize (or a slot delete) clears it —
  without that call the collapsed finalize keeps answering `409
  upload_missing` for a file that is sitting in S3 under a different key.

The sleep function is injectable, so tests never wait.

## 7. The S3 upload contract

Files never travel through the API. `create_draft` and `init_file_upload`
return a `PublicUploadTarget`:

```
{slot, filename, upload_url, fields, file_key}
```

1. POST `multipart/form-data` to `upload_url`.
2. Send **every entry of `fields`, exactly as returned, in the order returned,
   and nothing else**. Do not add, drop, rename or re-sign a field; the policy
   was signed over exactly these pairs.
3. The **file part comes last** and is named `file`.
4. Do **not** send `X-API-Key`, and do not send `Authorization`.
5. S3 answers `204`. Some bucket configurations answer `200` or `201`; treat any
   `2xx` as success and anything else as a failed upload.
6. Then call the matching finalize.

**Declare `sha256` when you can** — the base64 of the file's raw SHA-256 digest,
which is what S3's `x-amz-checksum-sha256` wants and what
`openssl dgst -binary -sha256 <file> | base64` prints. The presigned policy then
pins that digest, `fields` carries the checksum, and S3 rejects bytes that do
not match. Omit it and integrity is not verified at the edge. Anything that is
not a base64-encoded 32-byte digest is `422 validation_error`.

## 8. Polling (`wait_for_checks`)

Poll `GET /observations/{id}` — the one self-describing resource.

| Setting | Value |
|---|---|
| First interval | 2 s |
| Growth | × 1.5 |
| Cap | 10 s |
| Default timeout | 10 minutes |

A run is **terminal when `checks.status` is neither `"running"` nor
`"pending"`** — today that means `"complete"` or `"error"`. *Decision:* the test
is on the two non-terminal values, not on the two terminal ones, so a status the
API adds later ends the wait instead of hanging forever.

Two more ways the wait ends, so it cannot hang on a draft that will never
produce a run:

- **no `checks` and no `poll` verb** — a draft still missing a required file
  answers with its `upload:` verbs only (§ 5 of the API spec: only the first
  applicable group is returned), and there is nothing to wait for. A missing
  `checks` object *with* `poll` in `next_actions` is not terminal.
- **`submission_status == "submitted"`** — `auto_submit_when_clean` means the
  validation worker may submit the draft while the client is polling. That is
  an outcome, not a surprise: the wait ends and the result is `submitted`.

*Decision:* the timeout bounds the time spent **waiting between polls**, not
wall-clock time including the requests — those are bounded by the transport's
own timeout. That keeps the wait deterministic under an injected sleep, so a
test asserts `[2.0, 3.0, 4.5, …]` and never sleeps. The final wait is trimmed
to what is left of the budget, and one last poll follows it.

A timeout raises `timeout`, carrying `observation_id`, `timeout` and the last
`checks_status` in `details`; it never submits and never deletes. `next_actions`
is what to do next: act on the verbs, ignore any verb you do not recognize.

## 8a. Skipped checks and the OWC link: warn, never block

A check that could not run is listed in `checks.skipped[]` (`check_number`,
`code`, `name`, `status`, `reason`). A skipped check **never** blocks submit —
readiness ignores it — so a `ready` draft can still have some, and a client that
only reads `findings` would never learn the check did not happen. *Decision:*
once a wait ends, the workflow reports every skipped check through
`on_progress` as a `WARNING` block (`check_warnings`), and `submit` goes ahead
exactly as it would have. The block goes wherever progress goes — stderr, for
the CLI, in every mode (under `--json`, once the run has succeeded) — and the
same facts are data in `--json` (§ 13).

The usual skipped check is observer check 11, which needs an Occult Watcher
Cloud link. The server matches the OWC event itself after the report lands
(`owc.resolution`); when it could not choose (`ambiguous`, `mismatch`,
`not_found`, `error`) and the draft has no choice yet, the link is
**unresolved** (`owc_unresolved`: a draft, a resolution present, and `owc.choice`
not one of `matched`, `picked`, `pasted`, `proceed_unlinked`). The warning then
lists the candidates and the literal commands that settle it
(`owc_commands`): one `iota-hub drafts owc <id> --pick <owc_event_id>` per
candidate, then `--link <owc-url-or-id>` and `--none`. Settling it is
optional; the exit code does not change (§ 12).

The commands map onto `PATCH /observations/{id}` (`update_draft` in the
transport): `--pick` is `owc_link_choice: "picked"`, `--link` is `"pasted"`,
`--none` is `"proceed_unlinked"`. `drafts confirm-asteroid` sets
`confirm_unnumbered_asteroid_id: true` and `drafts comment` sets `comments`.
Each reads the observation first for its `version` and sends only the field it
sets; a `409 stale_version` is reported, not retried — the draft changed, so
re-read it and decide again.

## 9. Idempotency

`Idempotency-Key` is accepted on **create draft**, **finalize draft**,
**finalize file upload** and **submit**. A replay returns the first call's
response and does no second write; records expire after 24 hours. An in-flight
call with the same key answers `409 concurrent_operation` — wait for it and read
the result, never retry with a fresh key.

- **One key per logical call**, generated as a uuid4 by the caller of that
  logical call, reused across every transport retry of it.
- A new logical call gets a new key. Re-submitting after fixing a finding is a
  new logical call.
- So one `submit_folder` sends **three** distinct keys — create, finalize,
  submit — plus one per `finalize_file_upload` it had to make (§ 6). The
  per-slot `upload/init` takes no key: it presigns, it does not write.
- **Submit is safe to repeat without a key, too.** Submitting an observation
  that is already submitted answers `200` with its current state (API spec
  § 4.2), so a lost submit response is recovered by submitting again — the
  answer is the `submitted` outcome, never an error. `version` on submit is
  optional: a client sends the one it last read when it has one (the workflow
  always does, from its final poll) and otherwise omits it — the body is then
  `{}` and the server submits the current version.
- A lost **create** response is the one a key cannot always save — a client
  that sent none, or a process that died. That is what the check lookup is
  for (§ 11b): find the draft by what it observed instead of creating a
  second one.

## 10. Error model

Every `4xx`/`5xx` on `/public/v1` is problem details:

```jsonc
{"code": "open_findings", "message": "…", "hint": "…", "details": {"open_findings": 1}}
```

One exception type carries all of it:

| Attribute | |
|---|---|
| `code` | the stable contract — **branch on this, never on `message`** |
| `message` | human-readable, free to change |
| `hint` | what to do next, when the API knows |
| `details` | code-specific structured context, `{}` when absent |
| `status` | the HTTP status, or null when no response arrived |
| `retry_after` | seconds, from the `Retry-After` header, when present |

Rendered as one line: `<code>: <message> (hint: <hint>)`.

Subclasses group codes by **category**, so a caller can catch broadly without
enumerating codes. The categories, and the dispatch order:

| Category | Matches |
|---|---|
| rate-limit | status `429`, or `code == "rate_limited"` |
| auth | `missing_api_key`, `malformed_api_key`, `unknown_api_key`, `invalid_api_key`, `expired_api_key`, `inactive_user`, `insufficient_scope`, `forbidden` — *decision:* also any `401`/`403`, so an auth code added later still lands here |
| not-found | `not_found`, or status `404` |
| conflict | any `409` (`open_findings`, `checks_stale`, `already_processed`, `already_submitted`, …) |
| request | status `400` or `422` (`validation_error`, `missing_required`, `file_too_large`, `not_dismissable`, `identity_changed`) |
| transport | no response at all; carries the underlying error |
| base | anything else, including `5xx` `internal_error` |

**`details` is passed through untouched.** The API puts the observation's
state on its state refusals — `already_submitted`, `already_processed`,
`identity_changed`, `checks_stale`, `open_findings` carry `details.status`
(the derived `draft` / `submitted` / `amended` / `locked`),
`details.observation_id` and `details.amendment_count`, and
`identity_changed` adds `details.changed: [{field, stored, parsed}]` — and a
client exposes it as given, in the exception and in the CLI's `--json` error,
so a caller can say *which* field the replacement report disagreed on without
another read. The one addition is a client's: the `observation_id` on any error
`submit_files` raises after creating the draft (§ 1), when `details` did not
already carry it.

**When the body is not problem details** — a gateway-level `429`, an HTML `5xx`,
a truncated response — the client synthesizes one rather than raising a parse
error. *Decision:* `rate_limited` for `429`, `internal_error` for `5xx`,
`http_error` otherwise, with `details` empty.

**The codes a client invents.** Everything else comes from the API. They are
part of this contract too — a port raises the same code for the same situation,
and the CLI maps each one to an exit code (§ 12):

| `code` | Raised when |
|---|---|
| `upload_failed` | S3 refused a presigned POST; `status` and `details.slot` say which |
| `download_failed` | S3 refused a presigned GET — an expired link answers `403`; also a server-supplied filename that names no file (`""`, `.`, `..` once the directories are stripped), or two files whose names collide (compared case-insensitively), refused before anything is written |
| `file_exists` | a download would overwrite an existing file and `overwrite` was not asked for |
| `timeout` | `wait_for_checks` gave up (§ 8) |
| `ambiguous_files` | two candidates for one slot (§ 11); `details.slot`, `details.candidates` |
| `missing_files` | the folder has no report or cannot be listed, or an explicit path is not there or cannot be read (a directory, no permission), checked before any request for `submit_files`, `replace_file` and `add_attachment`; `details.missing` names the slots (`attachment` for a bad `--attach`) |
| `invalid_extension` | an explicit path has the wrong extension for its slot |
| `not_a_directory` | the folder to submit is not a directory, or a download destination cannot be one because a file is in the way |
| `not_writable` | a path this client must write cannot be written: a download destination or a file in it (no permission, or a directory has that name), or the config file or its directory; `details.path` |
| `blocked_attachment` | an attachment has a type the API refuses (§ 11a); `details.files`, `details.blocked_extensions` |
| `attachment_too_large` | an attachment is over the 50 MB cap (§ 11a); `details.files`, `details.max_size_bytes` |
| `too_many_attachments` | more than 25 attachments (§ 11a); `details.files`, `details.count`, `details.max_allowed` |
| `missing_api_key` | nothing configured a key (§ 3) — the auth category |
| `unknown_profile` | a profile was named and the config file has no such profile |
| `invalid_config` | the config file is not valid TOML or cannot be read, or a profile name, base URL or key carries something that cannot be stored in it (§ 3); a base URL that is not `https` (§ 4); `IOTA_HUB_BASE_URL` disagreeing with a profile's stored key (§ 3) |
| `confirmation_required` | a destructive command would have had to prompt, and stdin is not a TTY (§ 13); the `hint` names `--yes` |
| `aborted` | under `--json`, a person declined or cancelled a prompt (§ 13). Plain output prints `Aborted!` instead; the exit code is the same |
| `invalid_response` | a `2xx` whose body is not JSON or does not match the operation's schema; `status` is what the API answered, `details.call` is the method and path, and `details.fields` lists the fields in question (up to 10), never their values. Not retried |
| `usage_error` | the invocation itself was rejected by the argument parser (§ 12) |

`already_submitted` is no longer one of them: `drafts submit` on a submitted
observation is a success (§ 12). The code still comes from the API, on
`DELETE /observations/{id}` — a submitted observation is never deleted.

Every one of them carries a `hint` naming the way out — the flag to pass, the
command to run — because these are the errors a person or an agent hits first.

## 11. Folder → slot mapping

Copied from design § 8.3. `DIR` defaults to the current directory. The observer
naming convention (`YYYYMMDD_<number>_<name>_<lastname>_POS|NEG[-X]`,
`/data-submission` § 2) already carries enough signal; **no LLM classification.**

| Slot | Rule | Flag |
|---|---|---|
| `report` | exactly one `.xlsx` / `.xls` / `.json` | `--report` |
| `lightcurve` | at most one `.csv` | `--lightcurve` |
| `log` | at most one `.txt` whose name contains `log` (case-insensitive) | `--log` |
| `vizier` | at most one `.dat` | `--vizier` |
| `aperture_record` | at most one `.lc` / `.pymovie` | `--aperture-record` |

Only `report` is required at this stage. `lightcurve` and `log` depend on the
result: POS and UNSURE need them, NEG does not. A client cannot know the result
until the server has read the report, so it does not check them; the server
reports them in `readiness.missing_required`. `vizier` and `aperture_record` are
optional, and never reported as missing.

- **The report has two formats**, and the slot takes exactly one of them: the
  NA spreadsheet (`.xlsx`/`.xls`, still the primary report) or a Tangra JSON
  report (`.json`, Hristo Pavlov's `reportData` + `optionalData` format). A
  folder holding a spreadsheet *and* a `.json` is `ambiguous_files` — so is a
  stray `.json` of any other kind; name the report with `--report`. The
  server parses either one; the client never opens it.
- **The aperture record** is the per-frame data behind the light curve: a
  Tangra `.lc` or a PyMovie `.pymovie`. It has its own optional slot (the API
  spec § 4, `file-handling.md` in the API repository), so it is **not** an
  attachment and is not under the attachment limits of § 11a — its own ceiling
  is the server's (500 MB). Two of them is `ambiguous_files`. The server checks
  a `.pymovie`'s signature; the client checks only the extension.
- *Decision:* the slot's flag is the slot name with `_` spelled `-`
  (`--aperture-record`), and every mapping error names it that way.

- The listing is **non-recursive**, and skips subdirectories and dotfiles.
  Extensions match case-insensitively (`.CSV` is a light curve).
- **Never guess.** Two candidates for one slot, or no report, is
  an error that names the file(s) and the explicit flag to resolve it
  (`--lightcurve PATH`): `ambiguous_files` (with `details.slot` and
  `details.candidates`) or `missing_files` (with `details.missing`).
- Explicit flags override the rules for that slot, and are checked for the
  slot's **extension** only (`invalid_extension` otherwise). *Decision:* the
  "contains `log`" part is the client's own disambiguator between two `.txt`
  files in a folder, not a server rule — the public API takes any `.txt`
  declared in the `log` slot — so `--log notes.txt` is accepted. That is how a
  non-PyOTE log gets in (a Tangra fit log, an AOTA export): the folder rule
  only auto-maps a name containing `log`, and `--log PATH` takes any `.txt`.
- Anything else — `_notes.txt`, `.png`, a second CSV's companions — is an
  **attachment** (§ 11a), in filename order (by codepoint,
  so the same on every platform). `--no-attachments`
  (library: `attachments=False`) leaves them out; they are then reported as
  `ignored` ("not uploaded"). `--attach PATH` (repeatable; library: `attach=`)
  adds a file from anywhere, with or without `--no-attachments`; a path already
  mapped or already attached is not added twice.
- One observation per directory in v1. A folder with several stations
  (`_POS-1`, `_POS-2`) is the two-reports error, and a documented follow-up.

## 11a. Attachments

The API's attachment rules (`specs/public-api.md` § 4.3) are mirrored as
constants and **checked client-side before any request**, so a folder the API
would refuse fails whole, with nothing created:

| Rule | Value | Code (usage, exit `2`) | `details` |
|---|---|---|---|
| Blocked extension (trailing dots and spaces stripped, case-insensitive) | `.exe .bat .cmd .sh .ps1 .js .py .php .pl .rb .jar .msi .dll .so` | `blocked_attachment` | `files`, `blocked_extensions` |
| Per-file size | 50 MB (`52428800` bytes) | `attachment_too_large` | `files`, `max_size_bytes` |
| Per-observation count | 25 | `too_many_attachments` | `files`, `count`, `max_allowed` |

Each error names **every** offending file and suggests `--no-attachments`. The
server enforces the same three and has the last word; if the constants drift,
its `422` is what the caller sees (exit `1`).

- Declared as `{slot: "attachment", filename, size, sha256}` after the fixed
  slots. The create response carries one target per declared file, in declared
  order; attachment targets are paired with the attachment paths **by
  position**, and each carries its `attachment_id`.
- An attachment target that fails to upload is re-initialized once through
  `attachments/upload/init`, uploaded, committed with
  `attachments/upload/finalize`, and the old declaration is removed with
  `DELETE …/attachments/{old attachment_id}` — otherwise the collapsed finalize
  would keep answering `409 upload_missing` for it (§ 6).
- Fix loop: `drafts files add ID attachment PATH` adds one (blocklist and size
  checked first; the count is the server's, since it depends on what the draft
  holds), `drafts files rm ID attachment ATTACHMENT_ID` removes one. The ids are
  on the observation's `attachments[]` and in `drafts show`.

## 11b. Check and amend

**Check** — `check_observation(asteroid_name, star_id, observed_date, *,
asteroid_id, star_catalog, station)` in the transport, `POST
/observations/check` (scope `read`, the read limiter tier); `iota-hub
observations check --asteroid-name … --star-id … --date YYYY-MM-DD
[--asteroid-id …] [--star-catalog …] [--station …]` on the CLI. It answers
"do I already have this observation?" from what it observed:

| `found` | Meaning |
|---|---|
| `observation` | the caller's **own** draft or submission with this identity (the newest), with its `status`, `amendment_count` and `web_url`; `event` is its attached event, if any |
| `event` | none of the caller's, but the event (asteroid + star + date) exists: its id and status only |
| `none` | neither |

Never another observer's observation. Without `asteroid_id` there is no event
lookup (events are keyed by number). `station` absent, null and `""` are the
same station. *Decision:* the command exits `0` whatever it finds — it
reports, it does not act (§ 12) — and a found draft prints `iota-hub drafts
show <id>` as its next command. `submit` does **not** call it on its own: a
retried `submit` with an `Idempotency-Key` is already safe, and the client
does not read the report to learn its identity. A caller that may be re-running
after a lost create (a crashed script) runs `observations check` first.

**Amend** — there is no amend call. On a **submitted** observation, the owner
replacing a file in a fixed slot (`replace_file`, `drafts files add ID SLOT
PATH`) *is* the amendment (API spec § 4.5). The observation then reads
`status: "amended"` with `amendment_count` and `amended_at`; no check run
starts. Three refusals a client surfaces as they come:

| Refusal | When | Category | Exit |
|---|---|---|---|
| `422 identity_changed` | the replacement report describes a different observation; `details.changed` lists the fields, nothing was committed | request | `2` |
| `409 already_processed` | the event is `IN_REVIEW` or later: the owner can no longer change files | conflict | `1` |
| `403 forbidden` | clearing a fixed slot (`drafts files rm`) on a submitted observation — the owner never can | auth | `3` |

*Decision:* `identity_changed` is **usage**, `2`. Nothing is wrong with the
observation or the server: the file the caller named is the wrong one for it —
the same kind of mistake as `invalid_extension` — and the fix is a different
invocation (the corrected report), never a retry or a wait. `already_processed`
is a plain error, `1`: no invocation fixes it, a coordinator does. The derived
`status` (`draft` / `submitted` / `amended` / `locked`) is an open set like
every status here; `locked` describes the *owner's* ability, so a
coordinator's key may still change a `locked` observation.

**`web_url`** is on every observation: the draft editor for a draft, the
observation page otherwise. The CLI prints it (`Web:`) whenever `submit`
leaves a draft (`needs_attention`, `ready` or `draft`), so a person can review
or finish it in the browser, and
`submit --json` carries it at the top level.

## 12. CLI exit codes

Scripts and agents branch on these.

| Code | Meaning | Typical cause |
|---|---|---|
| `0` | success | |
| `1` | error | any other failure, including `5xx` and transport failures |
| `2` | usage | bad flags, missing argument, ambiguous slot mapping, a replacement report for another observation (`identity_changed`) |
| `3` | auth | the auth category of § 10 |
| `4` | open findings / not ready | `open_findings`, `missing_required`, `checks_stale`, `event_files_conflict`, or a wait that timed out short of `ready` |
| `5` | rate limited | the rate-limit category, after retries are exhausted |

The client-invented codes of § 10 map on: the mapping errors
(`ambiguous_files`, `missing_files`, `invalid_extension`, `not_a_directory`,
and the attachment rules `blocked_attachment`, `attachment_too_large`,
`too_many_attachments`) and `unknown_profile`, `confirmation_required` and
`not_writable` are **usage**, `2`;
`missing_api_key` is **auth**, `3`; of the API's own codes, `identity_changed`
is **usage**, `2` (§ 11b), and `already_processed` and `already_submitted` are
plain conflicts, `1`;
`timeout`, and an `outcome` of `needs_attention`, are **not ready**, `4`;
`upload_failed`, `download_failed`, `file_exists`, `invalid_config`, `aborted`
and `invalid_response` are plain errors, `1`. An `outcome` of `ready` or `draft` is a success the caller
asked for, so `0`.

One function decides all of it, and one handler wraps every command — a code
that *names* the situation is checked before the exception's category, so a
`422 missing_required` is `4` while `missing_api_key` stays `3`.

**A bad invocation is the argument parser's, and it is `2`.** *Decision:* under
`--json` it is reported like every other failure — the problem-details object of
§ 13 with `code` `usage_error`, the parser's own message, and the hint `Run the
command with --help.` — so an agent never has to switch parsers between a
rejected command and a rejected request. Without `--json` the parser prints its
usual usage line.

**Only the commands that act fail on "not ready".** *Decision:* `submit` and
`drafts submit` exit `4` when the draft is not ready, because they were asked
to submit it and did not. The commands that only *report* — `drafts show`,
`drafts check`, `observations show`, `auth status` — exit `0` and print what
they found, exactly as `auth status` reports an invalid key without failing.
A caller that wants the verdict reads `readiness.state`, or runs the command
that acts.

**`drafts submit` decides the refusal locally.** It reads the observation
first (to echo the `version` it saw, § 9), so when that read says the draft is
not ready it raises the same `code` the API's own refusal carries —
`missing_required`, `event_files_conflict`, `open_findings`, else
`checks_stale`, in that order — rather than spending a write that is certain to
be rejected. One contract either way. *Decision:* an observation the read says
is already submitted is **success**, exit `0`: the command prints it as read —
exactly the `200` the API's own submit would answer (§ 9) — and makes no
write. It is checked first, because a submitted observation has no readiness
to reason about and would otherwise be reported as `checks_stale`.

**`check` joins the run that is already under way.** *Decision:* `409
check_run_in_progress` is not a failure of the command that asked for a check
run — the run it wanted exists — so the client falls through to the wait
instead of exiting `1`, which is what the API's own hint says to do. With
`--no-wait` it says on stderr that a run is already under way and exits `0`.

## 13. Output

- Human output by default on a TTY; `--json` on **every** command.
- Data on **stdout**, progress and logs on **stderr**. A prompt is not data
  either: the hidden key prompt and every confirmation are written to
  **stderr**, so `--json` and a redirected stdout stay clean.
- `--json`, and the options that pick a target (`--profile`, `--base-url`), are
  accepted **before the subcommand and after it** — `iota-hub submit --json` is
  the natural form and the one the docs show. *Decision:* a value given after
  the subcommand wins over the same option given before it, being the more
  specific of the two.
- With `--json`: exactly **one JSON document on stdout and nothing else**. Errors
  are JSON too, on **stderr**, in the API's problem-details shape plus the
  transport facts of § 10 and `exit_code`:
  `{"code": "...", "message": "...", "hint": "...", "details": {}, "status": 409, "retry_after": null, "exit_code": 4}`.
  `status` is the HTTP status, or `null` when no response arrived; `retry_after`
  is the `Retry-After` wait in seconds, or `null` when the response carried
  none. Both are always present, so a reader never has to test for the key.
  *Decision:* the error document is **all** a failed run prints on stderr, so
  `json.loads(stderr)` always works. Whether a run fails is not known until it
  ends, so under `--json` the `Target:` line, progress and warning lines are held
  back: printed on stderr when the run succeeds (exit `4` on `needs_attention`
  included, since stdout carries the document), dropped when it fails. The cost is
  no live progress under `--json`. The one thing that can precede the document is
  a prompt the terminal already showed (a prompt is written to stderr, above):
  a person who answers one with `n` is at a terminal, not parsing it. That
  refusal is `aborted`, not `confirmation_required`: a prompt *was* possible, and
  `aborted` keeps the exit code `1` it has without `--json`.
- **Plain and styled.** The plain text is the contract: what a pipe, a file,
  an agent or a test sees, and what every rule in this section describes.
  *Decision (2026-10-31):* on a terminal the same facts may be **styled** --
  color, bold/dim, blank lines between sections, prose wrapped to the terminal
  (at most 100 columns), and findings that share a check, code and fix grouped
  under one fix with their fingerprints joined. A stream is styled only when it
  is a TTY, `NO_COLOR` is unset and the run is not `--json`; styling never
  adds, drops or reorders facts a reader needs, so nothing may branch on it.
  Only prose wraps. Table rows, file rows, field values, commands and progress
  lines go out as one unbroken line each and the terminal wraps them, so a
  widened window reflows them and an id, URL or command stays copyable.
- Output must survive a non-UTF-8 Windows console. Plain output is ASCII apart
  from what the server's own strings carry. Styled output may use a few glyphs
  (`✓`, `✗`, `›`) only when the stream's encoding is UTF-8, and prints ASCII
  stand-ins (`ok`, `x`, `>`) otherwise -- so a legacy console keeps the color
  and loses nothing it cannot print.
- A progress line identical to the one before it is printed once (each poll
  says "Waiting for checks...").
- Never prompt when stdin is not a TTY; destructive commands take `--yes`, and
  refuse with `confirmation_required` without it. `auth login` replacing a key is
  the one exception (§ 3): its stdin is the key.
- Human output prints the literal next command for each `next_actions` verb, so
  neither a person nor an agent has to translate. The mapping is the library's
  (`next_actions_commands`, § 1), not the CLI's:

  | Verb | Printed as |
  |---|---|
  | `upload:<slot>` | `iota-hub drafts files add <id> <slot> <path>` |
  | `run_checks` | `iota-hub drafts check <id>` |
  | `poll` | `iota-hub drafts show <id>` |
  | `submit` | `iota-hub drafts submit <id>` |
  | `dismiss_or_fix` | one `iota-hub drafts dismiss <id> <fingerprint> --note "…"` per **open** finding, then the `files add` alternative — fixing the file is the other way out |
  | `confirm_asteroid_id` | `iota-hub drafts confirm-asteroid <id>` (`PATCH` with `confirm_unnumbered_asteroid_id`, § 8a) |
  | `resolve_event_files_conflict` | a sentence saying to finish it in the web app: *decision*, because the public API has no verb for it (API spec § 5) |
  | anything else | `<verb>: see iota-hub guide` — a verb a client does not know is never silently dropped |
- Errors print the API's `code` and `hint`, as
  `error: <code>: <message>` then `hint: <hint>`, both on stderr, plus
  `details: <one line of JSON>` when `details` is not empty (a hint may point
  at it, so plain output must not hide it) and `retry after <n> s` when the
  response carried a `Retry-After`.
- Dates are printed as the API gives them. No local-time conversion: an
  observation's `observed_at_utc` is UTC and stays readable as UTC.
- Tables are simple aligned columns — no box drawing and no emoji, styled or
  not, so a non-UTF-8 console and a `grep`/`awk` pipeline both survive them.

**The `--json` document, per command.** *Decision:* the design fixed only
`submit --json`; the rest follow one rule — a command that answers with **one
resource** prints that resource's model as the API returned it, and a command
that answers with **a page** prints `{"items": [...], "next_cursor": ...}`. The
shapes are part of semver (a major bump to change one):

| Command | Document |
|---|---|
| `submit` | `{observation_id, outcome, submitted, uploads: {slot: filename}, attachments: [filename], ignored: [filename], next_commands: [...], skipped_checks: [{check_number, code, name, status, reason}], owc_commands: [...], web_url, observation: {...}}` — the attachment ids are on `observation.attachments[]`; `skipped_checks` and `owc_commands` are § 8a's, from the draft as the checks left it |
| `submit --dry-run` | `{target, directory, uploads: [{slot, filename, path, size}], attachments: [{filename, path, size}], ignored: [filename]}` — and no request is sent, so it needs no key; the attachment rules are checked, so a folder `submit` would refuse fails here too |
| `drafts show`, `drafts check`, `drafts files add`, `drafts files rm`, `drafts owc`, `drafts confirm-asteroid`, `drafts comment`, `drafts submit`, `observations show` | the `PublicObservation` |
| `drafts dismiss` | the `PublicChecks` the dismissal returned |
| `observations check` | the `PublicCheckResponse`: `{found, observation: {observation_id, status, amendment_count, web_url} \| null, event: {event_id, status} \| null}` |
| `events show` | the `PublicEvent` |
| `drafts list` | `{items: [...]}` — it follows the cursor itself (the open-draft cap is 100), so there is no `next_cursor` to report |
| `observations list`, `events list` | `{items: [...], next_cursor}`; with `--all` the cursor is followed and `next_cursor` is `null` |
| `auth login` | `{profile, base_url, key_prefix, config_file, replaced}` |
| `auth status` | `{profile, base_url, key_prefix, key_source, config_file, check}` |
| `auth logout` | `{profile, deleted, config_file}` |
| `drafts delete` | `{observation_id, deleted}` |
| `files download` | `{files: [path]}` |
| `guide` | `{guide: "<the markdown>"}` |

A key never appears in any of them — the most any document carries is
`key_prefix` (§ 3).

**`--dry-run` reads like the real run.** *Decision (2026-10-31, supersedes
"prints its target on stdout"):* its human output is the `submit` layout with
nothing sent -- the same `Target:` line on stderr, the same fields
(`Directory`, `Outcome: dry run, nothing sent`) and the same `Files` section
(`<slot>  <filename>  (<size>)`, the size in B/KB/MB as the web app shows it) that `submit` and the observation view
print, so a dry run and a real run can be compared line by line. Unlike other
commands it announces the target **even when it is the production default**,
since saying where a real run would go is its job. The `--json` document still
carries `target`.

**One way to say what was observed.** *Decision (2026-10-31):* every human
view that names an observation's or an event's identity prints the same
fields the same way: `Observer`, `Asteroid` as `(<number>) <name>` (the web
app's form), `Star` as `<catalog> <id>` (missing parts dropped, `-` when
nothing is known), then
`Observed` for an observation or `Date` for an event (an event's date window
is not an observation time). That covers `submit`, `drafts show|check|submit`,
`observations show` and `events show`, and the ASTEROID and STAR columns of
`observations list` and `events list`. `observations check` reads as an
answer: `Found: yes, one of yours`, `no, but the event exists` or `no`, with
the event as `Event` and `Event status` rather than one combined line. Human
wording only; the `--json` `found` values are unchanged.

## 14. The shared fixture folder

`tests/fixtures/observation/` holds one synthetic, submittable observation,
named to the observer convention:

```
20180305_9721_Doty_Observer_POS.xlsx            report
20180305_9721_Doty_Observer_POS.csv             lightcurve
20180305_9721_Doty_Observer_POS_pyote_log.txt   log
```

It exercises the required half of the mapping table (three of five slots, the
`log` rule, no `.dat`, no aperture record), and it is what the dev smoke test submits and then deletes. Every
client repo ships the same folder, so a port's tests compare against the same
bytes.

`tests/fixtures/clean-observation/` (`20260926_33777_1999RM158_Observer_POS.{xlsx,csv}`
+ `_pyote_log.txt`) is the *clean* counterpart: a PyMovie light curve, the PyOTE log
solved from it, and a report copied from the log, all mutually consistent for the real
event (33777) 1999 RM158 / UCAC4 326-152286 on 2026-09-26 (it auto-matches OWC event
`2094-33777-995661-234407-U152286`), so `iota-hub submit … --draft` against dev comes back
`ready` with no open findings and no skipped checks. The station prediction for check 11
comes from the light curve's Astrid `#OCCULTATION-PREDICTED-CENTER-TIME` key, so the report
still carries no coordinates.

**This is a public repository.** The identity in these fixtures is deliberately
fictional — "Test Observer", `cli-smoke@example.com`, no coordinates, no real
station. Any fixture added here is checked the same way before it is committed.

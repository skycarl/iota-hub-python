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
- `download_files("observation" | "event", id, dest, …)` — list the files,
  optionally filter by `slot` — **any** slot the listing names, not only the
  four upload slots (an event carries `damit`, `ground_track`, `attachment`) —
  and stream each presigned link into `dest`
  **without the API key** (§ 5). Nothing is skipped silently: an existing file
  is `file_exists` unless `overwrite` was asked for — `--force` on the CLI —
  (and every destination is
  checked before the first byte is written, so a refusal leaves no half-done
  download), and a link S3 refuses is `download_failed`,
- `next_actions_commands(observation)` — the API's verbs as the literal
  commands that answer them (§ 13). It lives in the *library*, not the CLI, so
  an embedder prints the same guidance the CLI does.

`submit_folder` / `submit_files` return one result object carrying the last
observation it read, `submitted`, the uploaded `{slot: filename}`, the files
the mapping ignored, and an **`outcome`** — the vocabulary a script branches on:

| `outcome` | Meaning |
|---|---|
| `submitted` | the observation is submitted (by this client, or by the server's `auto_submit_when_clean`) |
| `ready` | readiness is `ready`, but `submit=False` left it a draft |
| `needs_attention` | open findings, something missing, or the run errored |
| `draft` | `wait=False`: uploaded and finalized, not waited on |

Flags: `wait=False` stops after finalize, `submit=False` stops at `ready`.
**A draft that is not `ready` is never submitted**, and a failed run is never
cleaned up: the draft stays, because it is recoverable in the web app (and
`delete_draft` is one call away if the caller disagrees).

Progress is reported through an optional `on_progress` callback taking one
short string (`Uploading lightcurve (2/3)...`, `Waiting for checks...`), which
the CLI prints to stderr. Those strings are **ASCII**, like everything else a
client prints by default (§ 13).

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
| Windows | `%APPDATA%\iota-hub\config.toml` |

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
  mistaken for a prod submission.

**Precedence in detail.** The key comes from the flag, else the environment,
else the profile; the base URL from the flag, else the environment, else the
profile, else the default. *Decision:* a `--base-url` passed alongside a
profile is allowed and wins — an explicit instruction beats a stored one — and
the resolved settings say which of `flag` / `env` / `profile` the key came
from, so a client can show it. A profile that was **asked for** and does not
exist is `unknown_profile`; a config file that is not valid TOML is
`invalid_config`. A stored `default_profile` that no longer exists is simply
ignored, so deleting a profile never wedges the CLI.

**`auth login` resolves its target the same way, minus the profile's key.**
*Decision:* the profile it is about to write may not exist yet, so naming it
cannot be `unknown_profile`. The base URL stored with the key is the flag, else
`IOTA_HUB_BASE_URL`, else what that profile already had (a re-login keeps its
target), else the default — the § 3 order with the one profile in question
standing in for "the profile". The key itself is read from a hidden prompt on a
TTY and otherwise from one line of stdin, and is verified with
`list_observations(limit=1)` **before** anything is written, so a typo never
lands in the config file. Nothing but `key_prefix` is ever printed back.

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

> **Note.** `spec/openapi.json` carries `servers[0].url` = `<base>/public/v1`
> *and* path keys that begin with `/public/v1`. Compose a request from the base
> URL and the path **suffix**, as the table above does; do not naively
> concatenate the server URL with the path key. (Reported upstream as
> skycarl/iota-hub#762; once that is fixed this note can go.)

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
| conflict | any `409` |
| request | status `400` or `422` (`validation_error`, `missing_required`, `file_too_large`, `not_dismissable`) |
| transport | no response at all; carries the underlying error |
| base | anything else, including `5xx` `internal_error` |

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
| `download_failed` | S3 refused a presigned GET — an expired link answers `403`; also a server-supplied filename that names no file (`""`, `.`, `..` once the directories are stripped), refused before anything is written |
| `file_exists` | a download would overwrite an existing file and `overwrite` was not asked for |
| `timeout` | `wait_for_checks` gave up (§ 8) |
| `ambiguous_files` | two candidates for one slot (§ 11); `details.slot`, `details.candidates` |
| `missing_files` | a required slot has no candidate, or an explicit path is not there; `details.missing` |
| `invalid_extension` | an explicit path has the wrong extension for its slot |
| `not_a_directory` | the folder to submit is not a directory |
| `missing_api_key` | nothing configured a key (§ 3) — the auth category |
| `unknown_profile` | a profile was named and the config file has no such profile |
| `invalid_config` | the config file is not valid TOML, or a profile name, base URL or key carries something that cannot be stored in it (§ 3) |
| `confirmation_required` | a destructive command would have had to prompt, and stdin is not a TTY (§ 13); the `hint` names `--yes` |
| `already_submitted` | `drafts submit` was given an observation that is already submitted (§ 12) |
| `usage_error` | the invocation itself was rejected by the argument parser (§ 12) |

Every one of them carries a `hint` naming the way out — the flag to pass, the
command to run — because these are the errors a person or an agent hits first.

## 11. Folder → slot mapping

Copied from design § 8.3. `DIR` defaults to the current directory. The observer
naming convention (`YYYYMMDD_<number>_<name>_<lastname>_POS|NEG[-X]`,
`/data-submission` § 2) already carries enough signal; **no LLM classification.**

| Slot | Rule |
|---|---|
| `report` | exactly one `.xlsx` / `.xls` |
| `lightcurve` | exactly one `.csv` |
| `log` | exactly one `.txt` whose name contains `log` (case-insensitive) |
| `vizier` | at most one `.dat` |

`report`, `lightcurve` and `log` are required; `vizier` is optional.

- The listing is **non-recursive**, and skips subdirectories and dotfiles.
  Extensions match case-insensitively (`.CSV` is a light curve).
- **Never guess.** Two candidates for one slot, or none for a required one, is
  an error that names the file(s) and the explicit flag to resolve it
  (`--lightcurve PATH`): `ambiguous_files` (with `details.slot` and
  `details.candidates`) or `missing_files` (with `details.missing`).
- Explicit flags override the rules for that slot, and are checked for the
  slot's **extension** only (`invalid_extension` otherwise). *Decision:* the
  "contains `log`" part is the client's own disambiguator between two `.txt`
  files, not a server rule — the server's allow-list is the extension
  (`observation_file_validation.py`) — so `--log notes.txt` is accepted.
- Anything else — `_notes.txt`, `.png`, a second CSV's companions — is listed as
  **not uploaded**, with a note that it can be attached in the web app; the
  public surface has no attachment slot.
- One observation per directory in v1. A folder with several stations
  (`_POS-1`, `_POS-2`) is the two-reports error, and a documented follow-up.

## 12. CLI exit codes

Scripts and agents branch on these.

| Code | Meaning | Typical cause |
|---|---|---|
| `0` | success | |
| `1` | error | any other failure, including `5xx` and transport failures |
| `2` | usage | bad flags, missing argument, ambiguous slot mapping |
| `3` | auth | the auth category of § 10 |
| `4` | open findings / not ready | `open_findings`, `missing_required`, `checks_stale`, `event_files_conflict`, or a wait that timed out short of `ready` |
| `5` | rate limited | the rate-limit category, after retries are exhausted |

The client-invented codes of § 10 map on: the mapping errors
(`ambiguous_files`, `missing_files`, `invalid_extension`, `not_a_directory`)
and `unknown_profile` and `confirmation_required` are **usage**, `2`;
`missing_api_key` is **auth**, `3`;
`timeout`, and an `outcome` of `needs_attention`, are **not ready**, `4`;
`upload_failed`, `download_failed`, `file_exists` and `invalid_config` are
plain errors, `1`. An `outcome` of `ready` or `draft` is a success the caller
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

**`drafts submit` decides the refusal locally.** It must read the observation
anyway (`submit` echoes the `version` it last saw, § 9), so when that read says
the draft is not ready it raises the same `code` the API's own refusal carries
— `already_submitted` when the read says `submission_status == "submitted"`,
else `missing_required`, `event_files_conflict`, `open_findings`, else
`checks_stale`, in that order — rather than spending a write that is certain to
be rejected. One contract either way. *Decision:* `already_submitted` is
checked first, because a submitted observation has no readiness to reason about
and would otherwise be reported as `checks_stale`; it is a plain error, `1`,
not "not ready".

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
- No color when piped or when `NO_COLOR` is set.
- Output must survive a non-UTF-8 Windows console: no decorative Unicode in
  default output.
- Never prompt when stdin is not a TTY; destructive commands take `--yes`.
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
  | `confirm_asteroid_id`, `resolve_event_files_conflict` | a sentence saying to finish it in the web app: *decision*, because the public API has no verb for either (API spec § 5) |
  | anything else | `<verb>: see iota-hub guide` — a verb a client does not know is never silently dropped |
- Errors print the API's `code` and `hint`, as
  `error: <code>: <message>` then `hint: <hint>`, both on stderr, plus
  `retry after <n> s` when the response carried a `Retry-After`.
- Dates are printed as the API gives them. No local-time conversion: an
  observation's `observed_at_utc` is UTC and stays readable as UTC.
- Tables are simple aligned columns — no box drawing and no emoji, so a
  non-UTF-8 console and a `grep`/`awk` pipeline both survive them.

**The `--json` document, per command.** *Decision:* the design fixed only
`submit --json`; the rest follow one rule — a command that answers with **one
resource** prints that resource's model as the API returned it, and a command
that answers with **a page** prints `{"items": [...], "next_cursor": ...}`. The
shapes are part of semver (a major bump to change one):

| Command | Document |
|---|---|
| `submit` | `{observation_id, outcome, submitted, uploads: {slot: filename}, ignored: [filename], next_commands: [...], observation: {...}}` |
| `submit --dry-run` | `{target, directory, uploads: [{slot, filename, path, size}], ignored: [filename]}` — and no request is sent, so it needs no key |
| `drafts show`, `drafts check`, `drafts files add`, `drafts files rm`, `drafts submit`, `observations show` | the `PublicObservation` |
| `drafts dismiss` | the `PublicChecks` the dismissal returned |
| `events show` | the `PublicEvent` |
| `drafts list` | `{items: [...]}` — it follows the cursor itself (the open-draft cap is 100), so there is no `next_cursor` to report |
| `observations list`, `events list` | `{items: [...], next_cursor}`; with `--all` the cursor is followed and `next_cursor` is `null` |
| `auth login` | `{profile, base_url, key_prefix, config_file}` |
| `auth status` | `{profile, base_url, key_prefix, key_source, config_file, check}` |
| `auth logout` | `{profile, deleted, config_file}` |
| `drafts delete` | `{observation_id, deleted}` |
| `files download` | `{files: [path]}` |
| `guide` | `{guide: "<the markdown>"}` |

A key never appears in any of them — the most any document carries is
`key_prefix` (§ 3).

**`--dry-run` prints its target on stdout.** *Decision:* the target is part of
the mapping report the design asks `--dry-run` for ("the slot mapping, sizes
and target"), so it is data, not a diagnostic. The stderr `Target:` line of § 3
is unchanged and still appears whenever the target is not the default, which on
a dry run means the two agree with each other.

## 14. The shared fixture folder

`tests/fixtures/observation/` holds one synthetic, submittable observation,
named to the observer convention:

```
20180305_9721_Doty_Observer_POS.xlsx            report
20180305_9721_Doty_Observer_POS.csv             lightcurve
20180305_9721_Doty_Observer_POS_pyote_log.txt   log
```

It exercises the whole mapping table (three of four slots, the `log` rule, no
`.dat`), and it is what the dev smoke test submits and then deletes. Every
client repo ships the same folder, so a port's tests compare against the same
bytes.

**This is a public repository.** The identity in these fixtures is deliberately
fictional — "Test Observer", `cli-smoke@example.com`, no coordinates, no real
station. Any fixture added here is checked the same way before it is committed.

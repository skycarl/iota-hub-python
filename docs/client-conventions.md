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

- `submit_folder(dir, …)` / `submit_files(files, …)` — map, create, upload,
  finalize, wait, submit,
- `wait_for_checks(observation_id, …)` — § 8,
- `download_files(observation_id | event_id, dest, …)`.

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
- A key is **never** accepted as a command-line argument — it would land in
  shell history and in `ps`. Prompt with hidden input, or read stdin.
- No OS keyring in v1.
- Whenever the resolved base URL is not the default, the CLI prints
  `Target: <base URL>` on **stderr** before acting, so a test run is never
  mistaken for a prod submission.

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
  target can expire, so the workflow layer re-initializes the upload instead.

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
API adds later ends the wait instead of hanging forever. A missing `checks`
object is not terminal.

A timeout raises the language's timeout error carrying the observation id; it
never submits and never deletes. `next_actions` is what to do next: act on the
verbs, ignore any verb you do not recognize (only the first applicable group is
ever returned).

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

A failed S3 upload is `upload_failed`, carrying S3's status and the slot. It is
the one code a client invents; every other code comes from the API.

## 11. Folder → slot mapping

Copied from design § 8.3. `DIR` defaults to the current directory. The observer
naming convention (`YYYYMMDD_<number>_<name>_<lastname>_POS|NEG[-X]`,
`/data-submission` § 2) already carries enough signal; **no LLM classification.**

| Slot | Rule |
|---|---|
| `report` | exactly one `.xlsx` / `.xls` |
| `lightcurve` | exactly one `.csv` |
| `log` | exactly one `.txt` whose name contains `log` (the rule the server enforces) |
| `vizier` | at most one `.dat` |

- **Never guess.** Two candidates for one slot, or none for a required one, is
  an error that names the file(s) and the explicit flag to resolve it
  (`--lightcurve PATH`).
- Explicit flags override the rules for that slot.
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

## 13. Output

- Human output by default on a TTY; `--json` on **every** command.
- Data on **stdout**, progress and logs on **stderr**.
- With `--json`: exactly **one JSON document on stdout and nothing else**. Errors
  are JSON too, on **stderr**, in the API's problem-details shape plus
  `exit_code`:
  `{"code": "...", "message": "...", "hint": "...", "details": {}, "exit_code": 4}`.
- No color when piped or when `NO_COLOR` is set.
- Output must survive a non-UTF-8 Windows console: no decorative Unicode in
  default output.
- Never prompt when stdin is not a TTY; destructive commands take `--yes`.
- Human output prints the literal next command for each `next_actions` verb, so
  neither a person nor an agent has to translate:
  `iota-hub drafts dismiss <id> <fingerprint> --note "…"`.
- Errors print the API's `code` and `hint`.

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

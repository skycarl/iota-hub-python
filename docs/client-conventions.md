# IOTA Hub client conventions

This document defines the behavior shared by IOTA Hub clients in every
language. `iota-hub-python` is the reference implementation. Other clients must
follow the same rules for configuration, retries, polling, file selection and
output.

The API defines its own operations and schemas. A copy of its OpenAPI document
is stored in [spec/openapi.json](../spec/openapi.json).

## 1. Two layers

| Layer | Responsibility | Implementation |
| ----- | -------------- | -------------- |
| Transport | Methods for individual API operations and HTTP requests. | May be generated from OpenAPI. Python uses hand-written methods and models to limit dependencies and startup time. |
| Workflow | Submission, polling, downloads, upload recovery and other client behavior. | Hand-written in each language, following this document. |

Keep retry policies, polling limits and file selection separate from individual
endpoint methods. Workflow methods use the transport rather than making HTTP
requests directly.

### Transport methods

The synchronous client provides:

- One method per public `operation_id`.
- `iter_<collection>` generators that follow `next_cursor` until it is null
  and yield individual items.
- `upload_to_s3(target, file)` as specified in § 7.
- `sha256_base64(path)`.
- `close()` and the language's equivalent of a context manager.

An async client may be added but must not replace the synchronous client.

### Workflow methods

These method names are part of the contract:

- `submit_folder(dir, …)` and `submit_files({slot: path}, …)`: select files,
  create a draft, upload, finalize, wait for checks and submit.
- `wait_for_checks(observation_id, …)`: poll as specified in § 8.
- `replace_file(observation_id, slot, path)`: initialize an upload, upload the
  file and finalize it.
- `add_attachment(observation_id, path)`: upload one attachment (§ 11a).
  Use the transport's `delete_attachment(observation_id, attachment_id)` to
  remove one.
- `download_files("observation" | "event", id, dest, …)`: list files and
  download them to `dest`. An optional `slot` filter accepts any slot returned
  by the API, including event files such as `damit`, `ground_track` and
  `attachment`.
- `next_actions_commands(observation)`: return CLI commands for the API's
  `next_actions` (§ 13).
- `skipped_checks(observation)`, `owc_unresolved(observation)`,
  `owc_commands(observation)` and `check_warnings(observation)`: expose the
  warnings described in § 8a.

Command and warning helpers belong in the library so integrations can show the
same guidance as the CLI.

### Downloads

Download from each presigned URL without the API key. Check every destination
before writing any file:

- An existing file raises `file_exists` unless `overwrite` is enabled
  (`--force` in the CLI).
- A directory with the destination filename raises `not_writable`, even with
  `overwrite` enabled.
- A rejected storage request raises `download_failed`; a network failure
  raises `transport_error`. Both include `details.slot` and
  `details.filename`. Neither is retried. Running the command again obtains
  fresh URLs.

Write each download to a temporary file in `dest`, then rename it to the final
filename after completion. A failed download must leave no partial file and
must preserve any file it would have replaced. Completed downloads remain if
another download fails; rerunning then requires `overwrite` (`--force`).

An overwrite requires a writable destination directory, preserves the old
file's mode on POSIX and replaces a symlink rather than writing through it.

An empty file list is successful: do not create `dest`, exit `0`, return
`{"files": []}` in JSON mode and report the empty result on stderr in plain
mode. For callers without reviewer access, the API returns no event files
until the event is `COMPLETE`.

### Submission results

`submit_folder` and `submit_files` return the last observation read, a
`submitted` boolean, uploaded `{slot: filename}` pairs, attachment filenames,
ignored files, skipped checks, OWC commands and an `outcome`:

| `outcome` | Meaning |
| --------- | ------- |
| `submitted` | Submitted by this client or by the server's `auto_submit_when_clean`. |
| `ready` | Ready, but `submit=False` left it as a draft. |
| `needs_attention` | Open findings, missing requirements or a failed check run. |
| `draft` | Uploaded and finalized with `wait=False`; the client did not wait for checks. |

Capture skipped checks and OWC commands before submission. A submitted
observation no longer includes the draft's check run.

`wait=False` returns after finalization. `submit=False` returns after checks
without submitting. Never submit a draft that is not `ready`.

Do not delete a draft after an upload, finalization, polling or submission
failure. It remains available in the web app, or the caller can delete it with
`delete_draft`. For errors raised after draft creation, `submit_files` adds
`observation_id` to `details` if absent, then re-raises the error. This requires
no additional request or retry.

An optional `on_progress` callback receives short ASCII strings such as
`Uploading lightcurve (2/3)...` and `Waiting for checks...`. The CLI sends
these to stderr (§ 13).

## 2. Naming

Transport methods use the `operation_id` without its `public_` prefix:
`public_create_draft` becomes `create_draft`. Change casing to suit the language
(for example, `createDraft` in JavaScript), but do not rename or regroup methods.

Use OpenAPI schema names for models. Keep field names as they appear in JSON.

## 3. Configuration

Settings use this precedence: **explicit argument > environment > config file
> default**. CLI flags supply explicit arguments, except for the API key, which
must never be accepted as a CLI flag.

| Environment variable | Meaning |
| -------------------- | ------- |
| `IOTA_HUB_API_KEY` | API key, without a login step. |
| `IOTA_HUB_BASE_URL` | API base address (§ 4). |
| `IOTA_HUB_PROFILE` | Stored profile to select. |

Use the platform configuration directory reported by `platformdirs`:

| Platform | Path |
| -------- | ---- |
| Linux | `~/.config/iota-hub/config.toml` |
| macOS | `~/Library/Application Support/iota-hub/config.toml` |
| Windows | `%LOCALAPPDATA%\iota-hub\config.toml` |

Store each profile's key and base URL together in TOML:

```toml
default_profile = "default"

[profiles.default]
base_url = "https://api.iota-hub.com"
api_key  = "iotahub_<key_id>_<secret>"

[profiles.dev]
base_url = "https://<id>.execute-api.us-west-2.amazonaws.com/dev/api"
api_key  = "iotahub_<key_id>_<secret>"
```

### Reading and writing settings

- Write the file with mode `0600` on POSIX. On Windows, rely on the user profile
  directory's ACL and document that limitation. There is no OS keyring support
  in v1.
- Write to a temporary file in the same directory, with mode `0600` from
  creation, then replace the config file. A failed write must preserve the old
  file. If the config path is a symlink, replace its target and keep the symlink.
- Escape backslashes and double quotes in stored strings. Reject control
  characters U+0000–U+001F and U+007F in profile names, base URLs and keys.
  Also reject quotes and backslashes in profile names. Raise `invalid_config`
  before touching the file.
- Unreadable or invalid TOML raises `invalid_config`. An explicitly selected
  profile that does not exist raises `unknown_profile`. Ignore a stored
  `default_profile` that no longer exists.
- Read keys from a hidden prompt, stdin, the environment or saved settings.
  Never accept a key as a CLI argument or print its secret.

The library's `resolve(api_key=...)` accepts an explicit key. Resolved settings
report the key source as `flag`, `env` or `profile`; `flag` here includes a
library argument and does not imply a CLI key option.

An explicit `--base-url` overrides a profile's URL. If the key comes from a
profile and `IOTA_HUB_BASE_URL` differs from its saved URL, raise
`invalid_config`. The caller can unset that variable, also supply
`IOTA_HUB_API_KEY`, or explicitly pass `--base-url`.

For a non-default base URL, print `Target: <base URL>` on stderr before acting.
`submit --dry-run` prints it even for the default URL. In JSON mode, buffer the
line until the command returns a result (§ 13).

### Profile selection and login

All commands, including `auth login` and `auth logout`, select a profile in
this order:

1. `--profile`.
2. `IOTA_HUB_PROFILE`.
3. The saved `default_profile`, if it exists.
4. A profile named `default`.

`auth login` may create the selected profile, so a new name does not raise
`unknown_profile`. Select its base URL from `--base-url`, then
`IOTA_HUB_BASE_URL`, then that profile's saved URL, then the default URL.

Read the key with a hidden prompt on a terminal, or one line from stdin
otherwise. Verify it with `list_observations(limit=1)` before saving. Print only
its `key_prefix`. The first saved profile becomes `default_profile`; a login
also sets the default if an unqualified command would select no existing
profile.

When replacing a stored key on a terminal, ask for confirmation. Show the
profile name, old key prefix, old URL and new URL. `--yes` skips confirmation.
With piped input, replace the key without a prompt or `confirmation_required`
error. Every replacement prints this notice on stderr:

```text
Replaced profile '<name>', which held key <prefix> for <base URL>.
```

The JSON result includes `replaced: true`. Both login and logout report the
profile they changed.

### Logout

Removing the profile named by `default_profile` clears that setting. Do not
select another saved profile automatically. Subsequent commands without a
profile selection use a profile named `default`, or raise `missing_api_key`.

If profiles remain after removing the default, report on stderr that no default
is set, list the remaining profiles and explain which profile an unqualified
command will use. Buffer this notice in JSON mode (§ 13). Removing the last
profile or a non-default profile needs no such notice.

### Authentication status

`auth status` reports the profile, base URL, key prefix and result of
`list_observations(limit=1)`: `valid` or the API's auth error code. The key
prefix is `iotahub_<key_id>_...`, without the secret.

The API has no `whoami` operation. Do not infer or report a key's scopes or
expiry from its value.

## 4. Base URL

`base_url` is the API address without `/public/v1`. Strip a trailing slash,
then append `/public/v1` when constructing requests.

| Target | `base_url` | Example request |
| ------ | ---------- | --------------- |
| Production (default) | `https://api.iota-hub.com` | `https://api.iota-hub.com/public/v1/events` |
| Development | `https://<id>.execute-api.us-west-2.amazonaws.com/dev/api` | `…/dev/api/public/v1/events` |
| Local | `http://localhost:8000` | `http://localhost:8000/public/v1/events` |

Do not include a development deployment URL in a published client's defaults.
Require HTTPS, except for HTTP to `localhost`, `127.0.0.1` or `::1`.
Raise `invalid_config` for other URLs when constructing the client, before
sending a request.

## 5. Authentication and User-Agent

Every API request includes:

- `X-API-Key: iotahub_<key_id>_<secret>`. The API does not use `Authorization`.
- `Accept: application/json`.
- `User-Agent: iota-hub-<lang>/<version>`, such as `iota-hub-python/1.2.3`
  or `iota-hub-js/1.2.3`. Send it from both library and CLI calls. Applications
  using the library may override it.

Send the key only to the IOTA Hub API, never to storage upload or download URLs.

## 6. Retries

Apply retries to these API failures:

| Condition | Retry? |
| --------- | ------ |
| Network failure: DNS, connection, read or TLS | Yes |
| `429` | Yes |
| `5xx` | Yes |
| Other `4xx` | No |

- Make at most `1 + max_retries` attempts. The default `max_retries` is `4`.
- Wait `0.5 × 2^attempt` seconds between attempts, capped at 20 seconds, with
  no jitter. The sequence starts at 0.5, 1, 2, 4, 8 and 16 seconds.
- A valid `Retry-After` header replaces this delay. Accept seconds or an HTTP
  date and do not cap the resulting wait.
- Reuse the same `Idempotency-Key` for every attempt of a logical call (§ 9).
  A new key would allow another write.
- After the final attempt, raise the mapped exception with `retry_after` for
  the caller to inspect.
- Make the sleep function injectable so tests can check delays without waiting.

Do not add an outer retry loop around client calls.

### Upload recovery

The transport does not retry S3 uploads because a presigned target may have
expired. The submission workflow requests a new target through
`init_file_upload` and retries the upload once. A second failure raises an
error and leaves the draft available.

After recovery, call `finalize_file_upload` for that file before finalizing
the draft. The per-file finalize clears the expired target from
`pending_uploads`; draft finalization alone would return `409 upload_missing`.
Deleting the file slot also clears its pending upload. Attachment recovery uses
the equivalent attachment endpoints and removes the old declaration (§ 11a).

## 7. The S3 upload contract

Files upload directly to storage. `create_draft` and `init_file_upload` return
a `PublicUploadTarget` with these fields:

```text
{slot, filename, upload_url, fields, file_key}
```

1. POST `multipart/form-data` to `upload_url`.
2. Send every entry of `fields` in the returned order, without changing, adding
   or removing fields. These values are part of the signed upload policy.
3. Put the file part last, with the name `file`.
4. Do not send `X-API-Key` or `Authorization`.
5. Treat any `2xx` response as success. S3 normally returns `204`, but some
   configurations return `200` or `201`. Other statuses mean the upload failed.
6. Call the matching finalize operation.

Declare `sha256` when available: the raw SHA-256 digest encoded as base64.
It must decode to 32 bytes, or the API returns `422 validation_error`.
For example, this shell command computes that value:

```bash
openssl dgst -binary -sha256 <file> | base64
```

The signed policy includes the checksum and S3 rejects a file whose bytes do
not match. Without `sha256`, S3 does not perform this checksum verification.
The built-in upload workflows declare it for every file.

## 8. Polling (`wait_for_checks`)

Poll `GET /observations/{id}` with these intervals:

| Setting | Value |
| ------- | ----- |
| First interval | 2 seconds |
| Growth | × 1.5 |
| Maximum interval | 10 seconds |
| Default timeout | 10 minutes |

Stop polling when any of these conditions holds:

- `checks.status` is neither `running` nor `pending`. This includes `complete`,
  `error` and any new terminal status the API adds.
- There is no `checks` object and `next_actions` does not include `poll`.
  A draft missing required files may need uploads before checks can run.
- `submission_status` is `submitted`. The server's `auto_submit_when_clean`
  may submit the observation during polling; return the `submitted` outcome.

A missing `checks` object with `poll` in `next_actions` means keep waiting.

The timeout measures time spent waiting between polls. Request time is
additional and subject to the transport timeout. Shorten the final delay to
fit the remaining wait budget, then poll once more.

On timeout, raise `timeout` with `observation_id`, `timeout` and the last
`checks_status` in `details`. Do not submit or delete the draft.

Use recognized `next_actions` verbs to decide what to do next. Ignore unknown
verbs when choosing actions; they must not break the client. CLI display of
unknown verbs is described in § 13.

## 8a. Skipped checks and OWC links

`checks.skipped[]` lists checks that did not run, with `check_number`, `code`,
`name`, `status` and `reason`. Skipped checks do not affect readiness or block
submission.

After polling ends, report every skipped check through `on_progress` as a
`WARNING` block generated by `check_warnings`. The CLI prints it on stderr in
all modes, buffering it in JSON mode until a result is returned (§ 13).
Include the same information in the JSON result.

Observer check 11 needs an Occult Watcher Cloud (OWC) link. The server attempts
to match an event after reading the report and returns `owc.resolution`.
The result may be `ambiguous`, `mismatch`, `not_found` or `error`.

`owc_unresolved` is true when the observation is a draft, a resolution is
present and `owc.choice` is not `matched`, `picked`, `pasted` or
`proceed_unlinked`. In that case, show the candidates and `owc_commands`:

- One `iota-hub drafts owc <id> --pick <owc_event_id>` per candidate.
- `iota-hub drafts owc <id> --link <owc-url-or-id>`.
- `iota-hub drafts owc <id> --none`.

Resolving the link is optional and does not change the exit code.

These commands call `update_draft` (`PATCH /observations/{id}`):

| Command | Field set |
| ------- | --------- |
| `drafts owc --pick` | `owc_link_choice: "picked"` |
| `drafts owc --link` | `owc_link_choice: "pasted"` |
| `drafts owc --none` | `owc_link_choice: "proceed_unlinked"` |
| `drafts confirm-asteroid` | `confirm_unnumbered_asteroid_id: true` |
| `drafts comment` | `comments` |

Read the observation first, then send its `version` with the fields needed for
the chosen action. Report `409 stale_version` without retrying; the caller must
read the changed draft before deciding how to proceed.

## 9. Idempotency

An `Idempotency-Key` prevents duplicate writes when a request is retried.
Create draft, finalize draft, finalize file upload, finalize attachment upload
and submit accept this header. Reusing a key returns the first call's response.
Records expire after 24 hours.

An in-progress call with the same key returns `409 concurrent_operation`.
Wait for it to finish and read the result. Do not retry with a new key.

- Generate a UUID4 for each logical call and reuse it across transport retries.
- Use a new key for a new logical call, such as submitting after a correction.
- A normal `submit_folder` call uses separate keys for create, finalize and
  submit. Upload recovery adds a key for each per-file or attachment finalize.
- Per-file upload initialization does not take a key; it generates an upload
  target.

Submitting an already submitted observation is also safe without a key: the
API returns `200` and its current state. A lost submit response can therefore
be recovered by submitting that observation again.

The submit `version` is optional. Send the last version read when available;
the workflow uses its final poll's version. Otherwise omit it and send `{}`
to submit the current version.

If a create response is lost and its key is unavailable, use the observation
lookup (§ 11b) before creating another draft. A new process or new `submit`
invocation does not reuse a previous run's keys.

## 10. Error model

API errors use this JSON structure:

```json
{"code": "open_findings", "message": "...", "hint": "...", "details": {"open_findings": 1}}
```

The base exception exposes these attributes:

| Attribute | Meaning |
| --------- | ------- |
| `code` | Stable error identifier. Use it for program logic, not `message`. |
| `message` | Human-readable explanation; wording may change. |
| `hint` | Suggested next step, when available. |
| `details` | Fields specific to the error, or `{}`. |
| `status` | HTTP status, or null if no response arrived. |
| `retry_after` | Delay in seconds from `Retry-After`, when present. |

Its string form is `<code>: <message> (hint: <hint>)` when a hint is available.

Exception subclasses group errors by category. Match them in this order:

| Category | Matches |
| -------- | ------- |
| Rate limit | Status `429` or code `rate_limited`. |
| Authentication | `missing_api_key`, `malformed_api_key`, `unknown_api_key`, `invalid_api_key`, `expired_api_key`, `inactive_user`, `insufficient_scope`, `forbidden`, or any `401`/`403`. |
| Not found | Code `not_found` or status `404`. |
| Conflict | Any `409`, including `open_findings`, `checks_stale`, `already_processed` and `already_submitted`. |
| Request | Status `400` or `422`, including `validation_error`, `missing_required`, `file_too_large`, `not_dismissable` and `identity_changed`. |
| Transport | No response; preserve the underlying error. |
| Base | Other errors, including `5xx` `internal_error`. |

Preserve API `details` in both exceptions and CLI JSON errors. State errors
such as `already_submitted`, `already_processed`, `identity_changed`,
`checks_stale` and `open_findings` include `status`, `observation_id` and
`amendment_count`. `identity_changed` also includes
`changed: [{field, stored, parsed}]`. This lets callers identify the problem
without another request.

The only client addition is `observation_id` on errors raised by `submit_files`
after draft creation, if the API did not already provide it (§ 1).

If a `4xx` or `5xx` body is not in the expected error format, return a client
error with empty `details`: `rate_limited` for `429`, `internal_error` for
`5xx` and `http_error` otherwise. Do not expose a JSON parsing exception.

### Client error codes

Use the same code for the same condition in every language. CLI exit codes are
specified in § 12.

| `code` | Raised when |
| ------ | ----------- |
| `upload_failed` | S3 rejects a presigned POST. Includes `status` and `details.slot`. |
| `download_failed` | S3 rejects a presigned GET, such as an expired link returning `403`. Also used before writing if a filename is empty, `.` or `..` after removing directories, or filenames collide when compared case-insensitively. |
| `transport_error` | A request or download fails at the network level. `status` is null. A failed download includes `details.slot` and `details.filename`. |
| `file_exists` | A download would replace an existing file without `overwrite`. |
| `timeout` | The check wait exceeds its timeout (§ 8). |
| `ambiguous_files` | Multiple files match one slot. Includes `details.slot` and `details.candidates`. |
| `missing_files` | A folder has no report or cannot be listed, or an explicit path is missing, unreadable or a directory. Check paths before requests in `submit_files`, `replace_file` and `add_attachment`. `details.missing` lists slots, using `attachment` for an invalid `--attach` path. |
| `invalid_extension` | An explicit file has the wrong extension for its slot. |
| `not_a_directory` | The submission folder is not a directory, or a file blocks the download destination directory. |
| `not_writable` | The client cannot write a download or config path because of permissions or a conflicting directory. Includes `details.path`. |
| `blocked_attachment` | An attachment has a blocked extension (§ 11a). Includes `details.files` and `details.blocked_extensions`. |
| `attachment_too_large` | An attachment exceeds 50 MB. Includes `details.files` and `details.max_size_bytes`. |
| `too_many_attachments` | More than 25 attachments were selected. Includes `details.files`, `details.count` and `details.max_allowed`. |
| `missing_api_key` | No key is configured. Uses the authentication category. |
| `unknown_profile` | The explicitly selected profile does not exist. |
| `invalid_config` | Unreadable or invalid TOML, unsupported characters in saved settings, a URL that fails § 4's HTTPS rule, or an environment URL that conflicts with a profile's key (§ 3). |
| `confirmation_required` | A destructive command needs confirmation but stdin is not a terminal. The hint names `--yes`. |
| `aborted` | A prompt was declined or cancelled in JSON mode. Plain mode prints `Aborted!`; both use exit `1`. |
| `invalid_response` | A `2xx` response is not JSON or does not match the operation's schema. Includes `status`, the method and path in `details.call`, and up to 10 field names in `details.fields`, without their values. Do not retry. |
| `usage_error` | The argument parser rejects the invocation. |

Submitting an already submitted observation succeeds. `already_submitted` can
still come from the API when attempting to delete a submitted observation.

Client errors must include a useful `hint`, such as the flag or command needed
to resolve the problem.

## 11. Folder → slot mapping

`DIR` defaults to the current directory. Select files using these filename and
extension rules, without inspecting their contents:

| Slot | Rule | Flag |
| ---- | ---- | ---- |
| `report` | Exactly one `.xlsx`, `.xls` or `.json` | `--report` |
| `lightcurve` | At most one `.csv` | `--lightcurve` |
| `log` | At most one `.txt` with `log` in its name, case-insensitive | `--log` |
| `vizier` | At most one `.dat` | `--vizier` |
| `aperture_record` | At most one `.lc` or `.pymovie` | `--aperture-record` |

Only the report is required before upload. POS and UNSURE observations also
need a light curve and log; NEG observations do not. The server determines the
result from the report and lists missing files in `readiness.missing_required`.
`vizier` and `aperture_record` are optional.

### Reports and aperture records

A report is an NA spreadsheet (`.xlsx`/`.xls`, the primary format) or a Tangra
JSON report (`.json`, using `reportData` and `optionalData`). A spreadsheet and
any JSON file in the same folder count as two report candidates, even if the
JSON is unrelated. Require `--report` to select one. The server parses the
report; the client does not.

An aperture record contains the per-frame data behind the light curve: a
Tangra `.lc` or PyMovie `.pymovie` file. It has a separate, optional slot with
a server-enforced 500 MB limit. Attachment limits do not apply. The server
checks the `.pymovie` signature; the client checks its extension.

### Selection rules

- Read only the selected directory. Skip subdirectories and dotfiles.
- Match extensions case-insensitively.
- Multiple candidates for a slot raise `ambiguous_files`, with `details.slot`
  and `details.candidates`. A missing report raises `missing_files`, with
  `details.missing`. Name the appropriate override flag in the error.
- Spell flags with hyphens in place of underscores, including
  `--aperture-record`.
- Explicit paths override automatic selection for that slot. Validate the
  extension, raising `invalid_extension` if it is wrong. `--log` accepts any
  `.txt` filename, including Tangra fit logs and AOTA exports; the filename
  only needs `log` for automatic selection.
- Upload remaining files as attachments, sorted by filename codepoint for
  consistent ordering across platforms.
- `--no-attachments` (library: `attachments=False`) excludes those extra files
  and reports them as `ignored`, displayed as "not uploaded".
- Repeated `--attach PATH` (library: `attach=`) adds files from any location,
  even with `--no-attachments`. Do not add a path twice if it is already mapped
  to a slot or included as an attachment.
- Support one observation per directory. Folders containing several stations
  (`_POS-1`, `_POS-2`) are not supported; multiple reports cause a selection
  error. Use a separate folder for each station.

The [observer naming convention](https://iota-hub.com/data-submission) describes
how to name observation folders and files.

## 11a. Attachments

Check attachment limits before sending any request. If a limit fails, create
no draft and report all affected files.

| Rule | Value | Code (usage, exit `2`) | `details` |
|---|---|---|---|
| Blocked extension (trailing dots and spaces stripped, case-insensitive) | `.exe .bat .cmd .sh .ps1 .js .py .php .pl .rb .jar .msi .dll .so` | `blocked_attachment` | `files`, `blocked_extensions` |
| Per-file size | 50 MB (`52428800` bytes) | `attachment_too_large` | `files`, `max_size_bytes` |
| Per-observation count | 25 | `too_many_attachments` | `files`, `count`, `max_allowed` |

Each error suggests `--no-attachments`. The server also enforces these limits.
If its rules differ from the client's constants, report the server's `422`
response (exit `1`).

Declare attachments as `{slot: "attachment", filename, size, sha256}` after the
fixed file slots. The create response returns one upload target per file in
the same order. Match attachment targets to paths by position; each target
includes its `attachment_id`.

If an attachment upload fails, request a new target once through
`attachments/upload/init`, upload it and call `attachments/upload/finalize`.
Delete the old declaration through `DELETE …/attachments/{old attachment_id}`.
Otherwise draft finalization would continue returning `409 upload_missing`.

`drafts files add ID attachment PATH` adds an attachment. Check its extension
and size locally; the server checks the total count because it knows the
observation's existing attachments. `drafts files rm ID attachment ATTACHMENT_ID`
removes one. IDs appear in `attachments[]` and `drafts show`.

## 11b. Check and amend

### Find an existing observation

The transport method is
`check_observation(asteroid_name, star_id, observed_date, *, asteroid_id,
star_catalog, station)`. It calls `POST /observations/check`, which uses the
`read` scope and read rate limit. The CLI equivalent is:

```text
iota-hub observations check --asteroid-name <name> --star-id <star> --date YYYY-MM-DD [--asteroid-id <number>] [--star-catalog <catalog>] [--station <station>]
```

| `found` | Meaning |
| ------- | ------- |
| `observation` | The caller's newest matching draft or submission, with `status`, `amendment_count` and `web_url`. `event` contains its linked event, if any. |
| `event` | No observation belongs to the caller, but the event matches the asteroid, star and date. Returns only the event ID and status. |
| `none` | No matching observation or event was found. |

Never return another observer's observation. Without `asteroid_id`, skip event
lookup because events are keyed by asteroid number. An absent, null or empty
`station` refers to the same station.

A successful lookup exits `0` for all three results. If it finds a draft, print
`iota-hub drafts show <id>` as the next command.

`submit` does not perform this lookup: it does not parse the report's identity,
and its internal retries use idempotency keys. After a crash or lost create
response, the caller should check before starting another `submit` command.

### Amend a submitted observation

There is no separate amend operation. Replacing a fixed-slot file as the owner
with `replace_file` or `drafts files add ID SLOT PATH` marks a submitted
observation as `amended`, with `amendment_count` and `amended_at`. It does not
start a check run.

| Error | Reason | Category | Exit |
| ----- | ------ | -------- | ---- |
| `422 identity_changed` | The replacement report describes a different observation. `details.changed` lists the fields; the original is unchanged. | Request | `2` |
| `409 already_processed` | The event is `IN_REVIEW` or later, so the owner can no longer change files. | Conflict | `1` |
| `403 forbidden` | The owner tried to clear a fixed slot in a submitted observation with `drafts files rm`. | Authentication | `3` |

`identity_changed` requires a corrected report, not a retry or wait.
`already_processed` requires a coordinator's help. Treat the derived `status`
as an open set: currently `draft`, `submitted`, `amended` and `locked`.
`locked` describes the owner's access; a coordinator may still change files.

Every observation includes `web_url`: the draft editor for drafts and the
observation page otherwise. Print `Web:` when `submit` returns `needs_attention`,
`ready` or `draft`, and include `web_url` at the top level of `submit --json`.

## 12. CLI exit codes

| Code | Meaning | Typical cause |
| ---- | ------- | ------------- |
| `0` | Success | Includes `ready` and `draft` outcomes requested by the caller. |
| `1` | Other error | Server errors, network failures or unclassified errors. |
| `2` | Usage | Invalid arguments or files, ambiguous file selection, or `identity_changed`. |
| `3` | Authentication | Authentication category from § 10. |
| `4` | Not ready | `open_findings`, `missing_required`, `checks_stale`, `event_files_conflict` or a check timeout. |
| `5` | Rate limited | Rate-limit category after retries are exhausted. |
| `130` | Interrupted | Ctrl-C outside a prompt. There is no error document (§ 13). |

Use these mappings for specific codes:

| Exit | Codes or outcomes |
| ---- | ----------------- |
| `1` | `upload_failed`, `download_failed`, `file_exists`, `invalid_config`, `aborted`, `invalid_response`, `already_processed`, `already_submitted` |
| `2` | `usage_error`, `ambiguous_files`, `missing_files`, `invalid_extension`, `not_a_directory`, `not_writable`, `blocked_attachment`, `attachment_too_large`, `too_many_attachments`, `unknown_profile`, `confirmation_required`, `identity_changed` |
| `3` | `missing_api_key` |
| `4` | `timeout`, outcome `needs_attention` |

Use one mapping function and a shared error handler for all commands. Check
specific codes before exception categories: for example, `422 missing_required`
is exit `4`, while `missing_api_key` remains exit `3`.

In JSON mode, argument parser errors return `usage_error`, the parser's message,
the hint `Run the command with --help.` and exit `2`. Without JSON mode, use the
parser's normal usage output.

### Commands that report status

`submit` and `drafts submit` exit `4` when the draft is not ready.
Reporting commands such as `drafts show`, `drafts check`, `observations show`
and `auth status` return `0` when they successfully report a result, even if it
shows an unready draft or an invalid key. Callers must inspect `readiness.state`
or the reported authentication check. Request failures still use the error
mapping above.

### Submit an existing draft

`drafts submit` first reads the observation and its `version`.

- If it is already submitted, return it with exit `0` without sending a write.
  Check this before readiness, which submitted observations do not have.
- If it is not ready, raise the applicable code in this order:
  `missing_required`, `event_files_conflict`, `open_findings`, then
  `checks_stale`. This matches the API's refusal without sending a request
  that would fail.
- Otherwise submit with the version just read.

### Check an existing draft

For `drafts check`, `409 check_run_in_progress` means a run already exists.
Wait for that run instead of failing. With `--no-wait`, report the existing run
on stderr and exit `0`.

## 13. Output

### Streams and JSON

- Use human-readable output by default. Every command accepts `--json`.
- Put results on stdout and progress, logs and prompts on stderr. This includes
  the hidden key prompt and confirmations.
- Accept `--json`, `--profile` and `--base-url` before or after the subcommand.
  If an option appears in both places, the value after the subcommand wins.
- In JSON mode, return one JSON document on stdout. On error, leave stdout
  empty and write the error object to stderr:

  ```json
  {"code": "...", "message": "...", "hint": "...", "details": {}, "status": 409, "retry_after": null, "exit_code": 4}
  ```

Always include `status` and `retry_after`. `status` is null if no response
arrived; `retry_after` is the header's delay in seconds, or null if absent.

Buffer target, progress, warning and other notice lines in JSON mode. Print
them on stderr when returning a result, including a `needs_attention` result
with exit `4`. Discard them on error so stderr contains only the error JSON.
This means JSON mode does not provide live progress.

A terminal prompt may precede the error document because it has already been
shown. A declined or cancelled prompt raises `aborted` with exit `1`, matching
plain mode. It is not `confirmation_required`, which means prompting was
unavailable.

An interrupt (Ctrl-C) outside a prompt is the one failure with no error
document. Exit `130`, the standard interrupt code, because an interrupted run
has no result to report. Leave stdout empty, write no error object and print
the buffered lines on stderr instead of discarding them, so stderr is empty or
plain text. Scripts must check for exit `130` before parsing stderr as JSON.
Plain mode also exits `130`, without `Aborted!`. An interrupted `submit` may
leave a draft, and nothing reports its `observation_id`.

### Plain and styled output

Plain output defines which facts are displayed and their order. Style a stream
only if it is a terminal, `NO_COLOR` is unset and JSON mode is off. Styling may
add color, bold or dim text, and section spacing, but must not change the facts
or their order.

Wrap prose to the terminal width, capped at 100 columns. Do not insert line
breaks into table rows, file rows, field values, commands or progress lines;
let the terminal wrap them. Findings with the same check, code and fix may be
grouped under one fix, with their fingerprints listed together.

Plain labels use ASCII; server-provided strings may contain other characters.
Styled output may use `✓`, `✗` and `›` only on UTF-8 streams. Use `ok`, `x`
and `>` otherwise. Tables use aligned columns without box drawing or emoji.

Suppress consecutive duplicate progress lines. Never prompt without a terminal
on stdin. Destructive commands require `--yes` in that case, or raise
`confirmation_required`. Piped input to `auth login` is the exception because
stdin supplies the replacement key (§ 3).

### Suggested commands and errors

Use the library's `next_actions_commands` to display commands for API actions:

| Verb | Printed as |
| ---- | ---------- |
| `upload:<slot>` | `iota-hub drafts files add <id> <slot> <path>` |
| `run_checks` | `iota-hub drafts check <id>` |
| `poll` | `iota-hub drafts show <id>` |
| `submit` | `iota-hub drafts submit <id>` |
| `dismiss_or_fix` | One `iota-hub drafts dismiss <id> <fingerprint> --note "..."` per open finding, followed by the `files add` command as an alternative. |
| `confirm_asteroid_id` | `iota-hub drafts confirm-asteroid <id>` (§ 8a). |
| `resolve_event_files_conflict` | Instructions to finish in the web app; the public API has no operation for resolving this conflict. |
| Unknown verb | `<verb>: see iota-hub guide`. Display it without attempting an action. |

Display errors on stderr as `error: <code>: <message>`, followed by
`hint: <hint>` when available. Include `details: <one line of JSON>` when
nonempty, and `retry after <n> s` when the response includes `Retry-After`.

Print dates as returned by the API, without converting to local time.
`observed_at_utc` stays in UTC.

### JSON results by command

A command returning one resource prints its model. A command returning one
page prints `{"items": [...], "next_cursor": ...}`, with the exceptions listed
below. These structures are part of the versioning contract: changing them
requires a major version bump.

| Command | Document |
|---|---|
| `submit` | `{observation_id, outcome, submitted, uploads: {slot: filename}, attachments: [filename], ignored: [filename], next_commands: [...], skipped_checks: [{check_number, code, name, status, reason}], owc_commands: [...], web_url, observation: {...}}`. Attachment IDs are in `observation.attachments[]`. Capture `skipped_checks` and `owc_commands` before submission (§ 8a). |
| `submit --dry-run` | `{target, directory, uploads: [{slot, filename, path, size}], attachments: [{filename, path, size}], ignored: [filename]}`. No requests or key required. Attachment validation still applies. |
| `drafts show`, `drafts check`, `drafts files add`, `drafts files rm`, `drafts owc`, `drafts confirm-asteroid`, `drafts comment`, `drafts submit`, `observations show` | `PublicObservation` |
| `drafts dismiss` | `PublicChecks` returned by the dismissal |
| `observations check` | `PublicCheckResponse`: `{found, observation: {observation_id, status, amendment_count, web_url} \| null, event: {event_id, status} \| null}` |
| `events show` | `PublicEvent` |
| `drafts list` | `{items: [...]}`. Follows all pages; no `next_cursor` is returned. The open-draft limit is 100. |
| `observations list`, `events list` | `{items: [...], next_cursor}`. With `--all`, follows all pages and returns `next_cursor: null`. |
| `auth login` | `{profile, base_url, key_prefix, config_file, replaced}` |
| `auth status` | `{profile, base_url, key_prefix, key_source, config_file, check}` |
| `auth logout` | `{profile, deleted, config_file}` |
| `drafts delete` | `{observation_id, deleted}` |
| `files download` | `{files: [path]}` |
| `guide` | `{guide: "<the markdown>"}` |

Never include a secret key. Authentication output may include `key_prefix`.

### Dry runs

Use the same fields and file layout as a real submission:

- `Target: <base URL>` on stderr, even for production.
- `Directory` and `Outcome: dry run, nothing sent`.
- A `Files` section with `<slot>  <filename>  (<size>)`, using B/KB/MB as in
  the web app.

The JSON result includes `target`. Send no requests and require no key, but
apply the same attachment checks as a real submission.

### Observation and event identities

Use the same identity fields throughout human output:

- `Observer`.
- `Asteroid`: `(<number>) <name>`.
- `Star`: `<catalog> <id>`, omitting missing parts or using `-` if both are absent.
- `Observed` for an observation's time; `Date` for an event's date window.

Apply this to `submit`, `drafts show|check|submit`, `observations show` and
`events show`, and to the ASTEROID and STAR columns in observation and event
lists.

For `observations check`, print `Found: yes, one of yours`, `Found: no, but the
event exists` or `Found: no`. Show `Event` and `Event status` separately.
The JSON `found` values remain unchanged.

## 14. The shared fixture folder

`tests/fixtures/observation/` contains a synthetic observation with these files:

```text
20180305_9721_Doty_Observer_POS.xlsx            report
20180305_9721_Doty_Observer_POS.csv             lightcurve
20180305_9721_Doty_Observer_POS_pyote_log.txt   log
```

It tests selection of the three files required for a positive observation,
including the `log` filename rule, without a VizieR or aperture record file.
Development smoke tests submit and delete it. Ship the same fixture in every
client repository so tests use the same input.

`tests/fixtures/clean-observation/` contains the corresponding valid submission
fixture: `20260926_33777_1999RM158_Observer_POS.{xlsx,csv}` and
`_pyote_log.txt`. Its PyMovie light curve, PyOTE log and report describe the
real event (33777) 1999 RM158 / UCAC4 326-152286 on 2026-09-26. The expected OWC
match is `2094-33777-995661-234407-U152286`. Against development, the expected
`submit --draft` result is `ready`, with no open findings or skipped checks.

Check 11 uses the station prediction from the light curve's Astrid
`#OCCULTATION-PREDICTED-CENTER-TIME` key. The report contains no coordinates.

Fixtures in this public repository must use fictional observer identities.
These fixtures use "Test Observer", `cli-smoke@example.com`, no coordinates
and no real station. Check new fixtures for personal information before
committing them.

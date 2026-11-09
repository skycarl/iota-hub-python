# iota-hub

`iota-hub` is the command-line tool and Python library for submitting asteroid
occultation observations to [IOTA Hub NA](https://iota-hub.com). It uploads your
files, runs the observer checks and submits the observation when it is ready.

Use the CLI to submit a folder or manage observations. Use the Python library
to add these operations to your own scripts.

## Quickstart

Create an [API key](https://iota-hub.com/developers/keys). If you have
[uv](https://docs.astral.sh/uv/), you can run the CLI without installing it:

```bash
uvx iota-hub auth login
cd 20180305_9721_Doty_Observer_POS
uvx iota-hub submit
```

`auth login` prompts for your key and saves it. To install the command instead,
run `uv tool install iota-hub` or `pipx install iota-hub`, then use `iota-hub`
in place of `uvx iota-hub`.

To upgrade, run `uv tool upgrade iota-hub` or `pipx upgrade iota-hub`. `uvx`
can reuse a cached older version; run `uvx iota-hub@latest ...` to get the
latest release.

The CLI creates a draft, uploads the files directly to storage, waits for checks
and submits when the draft is ready. Progress goes to stderr; the result goes
to stdout. A successful result includes these lines (other fields omitted):

```text
Observation:  3c9a7e52-8f14-4b6d-a0e2-5d71c8b4f936
Outcome:      submitted
```

If checks find missing requirements or findings that need attention, the
observation stays in your Drafts. The CLI prints the findings, commands to
address them and a `Web:` link to the draft. It exits with code `4`. Failed
submissions do not delete the draft.

| Option | Effect |
| ------ | ------ |
| `--draft` | Upload and run checks, but leave the observation as a draft for review. |
| `--dry-run` | Show the selected files, their sizes and the target API without sending a request. |
| `--json` | Return the result as JSON for scripts and agents. |
| `--no-wait` | Return after upload and finalization, without waiting for checks or submitting. |
| `--timeout S` | Set the wait for checks in seconds. The default is 600; request time is additional. |

Skipped checks appear as warnings with a reason for each one. They do not block
submission. Check 12 ("Near predicted time"), for example, needs an Occult
Watcher Cloud (OWC) event link. If the Hub cannot select an event, the CLI lists candidates and commands
to choose one (`drafts owc <id> --pick <owc_event_id>`), provide a link
(`--link <url>`) or proceed without one (`--none`). Use `submit --draft` to
review these warnings before submitting.

If an earlier command stopped or you lost its output, check for an existing
observation before running `submit` again:

```bash
iota-hub observations check --asteroid-id 9721 --asteroid-name Doty --star-id 123456 --date 2018-03-05
```

Take the values from the report (the Doty example report's star is
`TYC 123456`), spelled as the report spells them, with the UTC date. This
finds your newest matching draft or submission, or reports whether the event
exists. The event is looked up only with `--asteroid-id`.
A new `submit` command creates a new draft. To submit an existing draft, use
`iota-hub drafts submit <id>`. That command also succeeds if the observation
has already been submitted.

## Files to submit

`submit` reads the current folder, or a folder you specify. It selects files by
extension and filename:

| File | Filename requirements | Required |
| ---- | --------------------- | -------- |
| `report` | One `.xlsx`, `.xls` or Tangra `.json` report | Yes |
| `lightcurve` | One `.csv` file | For POS/UNSURE |
| `log` | One `.txt` file with `log` in its name | For POS/UNSURE |
| `vizier` | One `.dat` file | No |
| `aperture_record` | One Tangra `.lc` or PyMovie `.pymovie` file | No |

A missing report stops the command before upload. The Hub reads the report to
determine the observation result. If a POS or UNSURE observation is missing a
light curve or log, it remains a draft and lists the missing files.

The CLI skips subfolders, dotfiles and symbolic links, and says which links it
skipped. Extensions and the `log` filename match
are case-insensitive. Keep each observation in its own folder, following the
[submission naming convention](https://iota-hub.com/data-submission). Folders
containing multiple stations (`_POS-1`, `_POS-2`) are not supported.

If more than one file matches, specify which one to use:

```bash
iota-hub submit --report observation.xlsx --lightcurve corrected.csv
```

The available flags are `--report`, `--lightcurve`, `--log`, `--vizier` and
`--aperture-record`. Explicit paths must have the correct extension, and a
relative path is relative to your current directory, not to the folder. The
folder's other candidates for a slot you choose are not uploaded; add
`--attach PATH` to upload one as an attachment. `--log`
accepts any `.txt` filename, including a Tangra fit log. A folder with both a
spreadsheet and a JSON file needs `--report` to select one.

### Attachments

Other files in the folder, such as plots and notes, are uploaded as attachments.
The CLI checks these limits before creating the draft:

- Up to 25 attachments per observation.
- Up to 50 MB per attachment.
- No executables or scripts: `.exe`, `.bat`, `.cmd`, `.sh`, `.ps1`, `.js`,
  `.py`, `.php`, `.pl`, `.rb`, `.jar`, `.msi`, `.dll` or `.so`.

A violation exits with code `2` and lists the affected files. The aperture
record is separate from attachments and has a 500 MB limit.

Use `--no-attachments` to exclude the extra files, or repeat `--attach PATH`
to add individual files, including files outside the folder. `--dry-run` shows
attachments as well as the main observation files.

To add or remove an attachment later:

```bash
iota-hub drafts files add <id> attachment <path>
iota-hub drafts files rm <id> attachment <attachment_id> --yes
```

`--yes` confirms the removal; without a terminal the command otherwise fails
with `confirmation_required`. Find attachment IDs with `iota-hub drafts show <id>`.

### Correct a submitted observation

Replace a file with the same command used for drafts:

```bash
iota-hub drafts files add <id> <slot> <path>
```

The observation becomes `amended`. A replacement report must describe the same
observation; otherwise the API returns `identity_changed` (exit `2`) and leaves
the original unchanged. Once the event is in review, replacements return
`already_processed` (exit `1`); the observation's `status` is then `locked`.
Contact a coordinator for changes at that stage. A replacement runs the
observer checks again, but their results appear only on the observation's
page on IOTA Hub, not in the CLI.

## API keys and profiles

Create keys on the [API keys page](https://iota-hub.com/developers/keys).
Each key is shown once, stored hashed by the Hub and limited to its owner's
permissions. An account can have up to 10 keys.

- Choose `read` for read operations and `observations:write` for writes.
  A missing scope returns `403 insufficient_scope`.
- Choose an expiry of 30 days, 90 days, 1 year (the default) or never.
  An expired key returns `401 expired_api_key`.

Run `iota-hub auth login` to save a key, or set `IOTA_HUB_API_KEY` in your
script or CI environment. Never put a key in a command-line argument: it can
appear in shell history and process listings.

`iota-hub auth status` checks the key with a read request and shows the profile,
API address and key prefix. It never prints the secret. `iota-hub auth logout`
removes the selected profile.

Every command selects a profile in this order: `--profile`,
`IOTA_HUB_PROFILE`, the saved `default_profile`, then a profile named `default`.
The first profile you save becomes the default. Login asks before replacing an
existing key on a terminal; `--yes` skips that prompt. A piped key replaces the
existing key, with a notice on stderr.

Logging out of the default profile does not select a replacement. If profiles
remain, the CLI lists them. Commands without a profile selection then use a
profile named `default`, or report that no key is configured.

Profiles save the key and its API address together:

```bash
iota-hub auth login --profile dev --base-url <dev-base-url>
iota-hub submit --profile dev
```

Settings use flags first, then environment variables, saved settings and
defaults. A saved key stays with the address saved beside it: if `--base-url`
or `IOTA_HUB_BASE_URL` differs from a profile key's address, the CLI returns
`invalid_config`. Log in to a profile for that address, or set
`IOTA_HUB_API_KEY` to a key for it. `IOTA_HUB_API_KEY` needs its own address
too: with a profile that stores a non-production address, set
`IOTA_HUB_BASE_URL` as well, or unset `IOTA_HUB_API_KEY` to use the profile.

`--json`, `--profile` and `--base-url` work before or after the subcommand.
If the same option appears in both places, the value after the subcommand wins.
For a non-default API address, the CLI prints `Target: <base URL>` on stderr.
With `--json`, that line is held until the command returns a result.

The configuration file is stored here:

| Platform | Path |
| -------- | ---- |
| Linux | `~/.config/iota-hub/config.toml` |
| macOS | `~/Library/Application Support/iota-hub/config.toml` |
| Windows | `%LOCALAPPDATA%\iota-hub\config.toml` |

On POSIX, the file has mode `0600`. On Windows, it relies on your user profile
directory's permissions; the CLI does not set an ACL. There is no OS keyring
integration.

## Scripts and agents

Every command accepts `--json`. Results are one JSON document on stdout.
Errors go to stderr, with stdout left empty:

```json
{
  "code": "open_findings",
  "message": "...",
  "hint": "...",
  "details": {},
  "status": 409,
  "retry_after": null,
  "exit_code": 4
}
```

Check `code`, not the message text. Invalid arguments return `usage_error`
with exit code `2`. A `submit` that ends with `needs_attention` exits `4` with
its full result on stdout, not an error. Errors after draft creation include
`details.observation_id` so you can recover the draft. A network failure during
an upload or a download returns `transport_error` with the file's
`details.slot` and `details.filename`.

In JSON mode, progress and target messages are buffered. They are printed to
stderr when a result is returned and discarded on error, leaving only the JSON
error. Interactive prompts can still appear on stderr; noninteractive commands
do not prompt. Destructive commands require `--yes` without a terminal.

| Exit code | Meaning |
| --------- | ------- |
| `0` | Success, including `--draft`, `--dry-run` and `--no-wait`. |
| `1` | Other errors, including server and network failures, local configuration problems (`invalid_config`) and existing download files (`file_exists`). |
| `2` | Invalid arguments or files, including ones the server rejects (`validation_error`), ambiguous file selection, or `identity_changed`. |
| `3` | Missing key or an authorization error. |
| `4` | Findings to address, missing requirements or a check timeout. |
| `5` | Rate limit still exceeded after automatic retries. |
| `130` | Interrupted with Ctrl-C outside a prompt. |

An interrupted run has no result to report, so it writes no JSON error: stdout
is empty and stderr holds only any buffered messages. Check for exit code `130`
before parsing stderr as JSON. An interrupted `submit` may leave a draft without
naming it; run `drafts list` before submitting again. A Ctrl-C in the first
moments of startup kills the process by signal instead (a shell still reports
`130`; Python's `subprocess` reports `-2`), so treat that the same way.

`submit --json` includes `next_commands` for available actions, `skipped_checks`
with their reasons and `owc_commands` for an unresolved OWC link. Skipped checks
and an unresolved link do not change the exit code. Its `uploads` is
`{slot: filename}`; `submit --dry-run --json` lists `uploads` as
`[{slot, filename, path, size}]` instead.

`auth status` exits `0` even when the key check fails. Read `check` in
`auth status --json` (`valid` or the API's error code).

The client retries temporary API failures. Do not wrap calls in your own retry
loop; see the [retry rules](https://github.com/skycarl/iota-hub-python/blob/main/docs/client-conventions.md#6-retries).

Output is styled on a terminal and plain when piped, under `--json` or when
`NO_COLOR` is set. Plain labels use ASCII for compatibility with Windows
consoles.

Run `iota-hub guide` for the full offline CLI guide. [SKILL.md](https://github.com/skycarl/iota-hub-python/blob/main/SKILL.md)
provides instructions for agents submitting or managing observations.

## Python library

Install with `pip install iota-hub` or `uv add iota-hub`:

```python
from iota_hub import Client, resolve

# The key and address from IOTA_HUB_API_KEY and IOTA_HUB_BASE_URL, or from a
# profile saved with `iota-hub auth login`.
settings = resolve()
with Client(settings.api_key, settings.base_url) as client:
    result = client.submit_folder("path/to/observation")
    print(result.outcome, result.observation_id)

    if result.outcome == "needs_attention":
        print(result.observation.web_url)
        checks = result.observation.checks
        for finding in checks.findings if checks else []:
            print(finding.fingerprint, finding.message, finding.how_to_fix)
```

| `result.outcome` | Meaning |
| ---------------- | ------- |
| `submitted` | The observation is submitted. |
| `ready` | Ready to submit, but `submit=False` left it as a draft. |
| `needs_attention` | Findings, missing requirements or a failed check run need attention. |
| `draft` | Uploaded and finalized with `wait=False`; the client did not wait for checks. |

`result.observation.web_url` links to the observation in the web app.
Its `status` describes the observation as `draft`, `submitted`, `amended` or
`locked`.

Use the same client to look up observations, replace files or download them:

```python
# These examples assume an open client and an existing observation_id.
existing = client.check_observation(
    asteroid_id="9721",
    asteroid_name="Doty",
    star_id="123456",
    observed_date="2018-03-05",
)
client.replace_file(observation_id, "lightcurve", "fixed.csv")
for observation in client.iter_observations(submission_status="draft"):
    print(observation.observation_id, observation.submission_status)
client.download_files("observation", observation_id, "downloads/")
```

The library has methods for individual API operations, such as `create_draft`
and `list_events`, and methods that combine steps, such as `submit_folder`,
`submit_files`, `wait_for_checks`, `replace_file` and `download_files`.
Responses use Pydantic models that ignore unknown fields.

API requests retry network failures, `429` and `5xx`, respecting `Retry-After`.
Workflow methods create idempotency keys and declare SHA-256 checksums for
uploads. When calling individual API operations, supply their `idempotency_key`
argument where supported. Do not add an outer retry loop.

All library errors inherit from `IotaHubError`, with `code`, `message`, `hint`,
`details`, `status` and `retry_after`. Catch `AuthError`, `RateLimitError`,
`NotFoundError`, `ConflictError`, `RequestError` or `TransportError` to handle
an error category.

See the [Python library guide](https://github.com/skycarl/iota-hub-python/blob/main/docs/library.md) for the full workflow and
examples. It is also published at
[iota-hub.com/developers/library](https://iota-hub.com/developers/library).

## Environments

The default API address is `https://api.iota-hub.com`. The public API's
production release is pending. Until then, use the development deployment and
create a key there. Its `/developers` page lists the development API address;
paste it into `--base-url` as shown.

```bash
iota-hub auth login --profile dev --base-url <dev-base-url>
iota-hub observations list --profile dev
```

`--base-url` and `IOTA_HUB_BASE_URL` take the API base address, with or
without a trailing `/public/v1`; the client adds that path when it is missing. For a local API checkout, use
`--base-url http://localhost:8000`. The package has no built-in development URL.

## HTTP API and other clients

To call `/public/v1` directly, use the
[HTTP API guide](https://iota-hub.com/developers/api),
[interactive reference](https://iota-hub.com/developers/reference) or
[Markdown guide](https://iota-hub.com/developers/guide.md).

For a client in another language, start with [spec/openapi.json](https://github.com/skycarl/iota-hub-python/blob/main/spec/openapi.json)
and [client conventions](https://github.com/skycarl/iota-hub-python/blob/main/docs/client-conventions.md). The OpenAPI document is
copied from the deployed API and checked for changes in CI. The conventions
define configuration, retries, polling, file selection, exit codes and output.

## Requirements

Python 3.10 or newer on Windows, macOS or Linux. Runtime dependencies are
`httpx`, `pydantic`, `typer` and `platformdirs`, plus `tomli` on Python 3.10.

## Versioning

The package uses semantic versioning and remains at `0.x` until the public API
reaches production. Removing a command or flag, or changing a `--json` output
structure, requires a major version bump. New commands, optional flags and
JSON fields are additive changes.

## Contributing

Submit issues and pull requests to
[skycarl/iota-hub-python](https://github.com/skycarl/iota-hub-python).
You can also report API issues there, such as an incorrect status code, a
missing filter or an unclear finding. The maintainer will pass them to the API
project.

## License

MIT. See [LICENSE](https://github.com/skycarl/iota-hub-python/blob/main/LICENSE).

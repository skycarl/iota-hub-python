# iota-hub

[IOTA Hub](https://iota-hub.com) is where observers of asteroid occultations send
their observations to IOTA: a report, a light curve, an instrument log, and
optionally a VizieR file, checked and reviewed in one place instead of by email.
`iota-hub` is the command-line tool and Python library for its public API v1 —
it creates the draft, uploads the files, runs the observer checks, and submits
when the checks are clean. It is meant for observers who want their submission
scripted or reduced to one command, and for AI agents acting on an observer's
behalf. There are three ways in, in the order you should try them: **the CLI
first**, the **Python library** second when you are building something of your
own, and the **raw HTTP API** last, documented at
<https://iota-hub.com/developers>.

## Quickstart (CLI)

No install needed if you have [uv](https://docs.astral.sh/uv/):

```bash
uvx iota-hub auth login          # paste your API key when prompted
cd 20180305_9721_Doty_Observer_POS
uvx iota-hub submit
```

Prefer it on your PATH: `uv tool install iota-hub`, or `pipx install iota-hub`,
and then run `iota-hub` instead of `uvx iota-hub`.

A clean run looks about like this (progress on stderr, the result on stdout):

```
Creating draft (3 files)...
Uploading report (1/3)...
Uploading lightcurve (2/3)...
Uploading log (3/3)...
Finalizing draft...
Waiting for checks...

Submitted obs_01HZY4A1K2QX
  report      20180305_9721_Doty_Observer_POS.xlsx
  lightcurve  20180305_9721_Doty_Observer_POS.csv
  log         20180305_9721_Doty_Observer_POS_pyote_log.txt
  checks      complete, 0 open findings
  not uploaded: field_notes.png (attach it in the web app if you need it)
```

`submit` is the whole job: it creates a draft, uploads each file straight to
storage (files never travel through the API), finalizes, waits for the observer
check run, and submits the observation when — and only when — the draft comes
back `ready`. If the run leaves open findings, or something required is still
missing, it does not submit: the draft stays in your Drafts, the findings are
printed with the exact commands that resolve them, and the exit code is `4`.
Nothing is ever guessed and nothing is ever thrown away — a draft it could not
submit is recoverable in the web app.

Useful variations:

- `iota-hub submit --draft` — do everything except the final submit, so you can
  review it in the web app first.
- `iota-hub submit --dry-run` — print the slot mapping, the file sizes and the
  target, and call nothing.
- `iota-hub submit --json` — one JSON document on stdout, for scripts and agents.
- `iota-hub submit --no-wait` — return after the upload with the draft id;
  `--timeout S` changes how long it waits (default 10 minutes).

## Which file goes where

`submit` takes a directory (the current one by default) and maps its files onto
the four slots by rules only. The observer naming convention
(`YYYYMMDD_<number>_<name>_<lastname>_POS|NEG[-X]`, see
<https://iota-hub.com/data-submission>) already carries enough signal, so there
is no classification and no content sniffing:

| Slot         | Rule                                                        | Required |
| ------------ | ----------------------------------------------------------- | -------- |
| `report`     | exactly one `.xlsx` / `.xls`                                | yes      |
| `lightcurve` | exactly one `.csv`                                          | yes      |
| `log`        | exactly one `.txt` whose name contains `log`                | yes      |
| `vizier`     | at most one `.dat`                                          | no       |

The listing is non-recursive and skips subdirectories and dotfiles; extensions
match case-insensitively, so `.CSV` is a light curve.

Anything the rules do not claim — `field_notes.txt`, a `.png`, a second CSV's
companions — is listed as **not uploaded** and left alone. The public API has no
attachment slot; attach those in the web app if the reviewer needs them.

**Ambiguity is an error, never a guess.** Two candidates for one slot, or none
for a required one, stops the command with the names of the files involved and
the flag that settles it. The overrides are `--report PATH`,
`--lightcurve PATH`, `--log PATH` and `--vizier PATH`; each is checked only for
the slot's extension, so `--log notes.txt` is accepted even though its name does
not contain `log`.

## Getting a key

Sign in to IOTA Hub, open your profile page, and use the **API Keys** card. The
key is shown once, is stored hashed, and you may have up to 10.

- **Scopes**, fixed when the key is created: `read` for every read,
  `observations:write` for every write. A key missing the scope a call needs is
  `403 insufficient_scope`.
- **Lifetime**, chosen when the key is created: 30 days, 90 days, 1 year (the
  default), or never. An expired key is `401 expired_api_key`.
- A key **acts as its owner** — exactly what that account can do in the web app,
  nothing more.

Give the key to the tool in one of two ways, never as a command-line argument
(it would land in your shell history and in `ps`):

```bash
iota-hub auth login              # prompts with hidden input, stores it
export IOTA_HUB_API_KEY=...      # the CI and agent path, no login step
```

`iota-hub auth status` shows which profile is in use, the target base URL and
the identifying prefix of the key (`iotahub_<key_id>_...`, never the secret),
and makes one cheap read to say whether the key is actually valid.
`iota-hub auth logout` removes a stored profile.

Profiles keep a key **and** its base URL together, so a development key is never
sent to production or the reverse:

```bash
iota-hub auth login --profile dev --base-url <dev base URL>
iota-hub --profile dev submit          # or IOTA_HUB_PROFILE=dev
```

Whenever the resolved target is not the production default, the CLI prints
`Target: <base URL>` on stderr before acting, so a test run is never mistaken
for a real submission. Settings resolve flag > environment > config file >
default, and the config file lives where the platform puts configuration:

| Platform | Path                                                     |
| -------- | -------------------------------------------------------- |
| Linux    | `~/.config/iota-hub/config.toml`                          |
| macOS    | `~/Library/Application Support/iota-hub/config.toml`      |
| Windows  | `%APPDATA%\iota-hub\config.toml`                          |

On POSIX the file is written `0600`. On Windows the protection is the ACL on
your user profile directory; the tool does not set permissions itself. There is
no OS keyring integration.

## For scripts and agents

- **`--json` works on every command**: exactly one JSON document on stdout and
  nothing else. Errors are JSON too, on **stderr**, in the API's problem-details
  shape plus the exit code:
  `{"code": "open_findings", "message": "...", "hint": "...", "details": {}, "exit_code": 4}`.
  Branch on `code`, never on `message`.
- **Exit codes** — branch on these rather than parsing output:

  | Code | Meaning                  | Typical cause                                                             |
  | ---- | ------------------------ | ------------------------------------------------------------------------- |
  | `0`  | success                  | also `--draft`, `--dry-run` and `--no-wait`, which end where you asked     |
  | `1`  | error                    | any other failure, including `5xx` and network failures                   |
  | `2`  | usage                    | bad flags, missing argument, an ambiguous or incomplete slot mapping      |
  | `3`  | auth                     | no key configured, or the API rejected it                                 |
  | `4`  | open findings / not ready | findings to fix or dismiss, something required missing, or a wait timeout |
  | `5`  | rate limited             | still rate limited after the built-in retries                             |

- **`iota-hub guide`** prints the full workflow guide, bundled in the package —
  no network, and the same text an agent can read before acting.
- **[`SKILL.md`](SKILL.md)** in this repository is a ready-made skill for Claude
  Code or any agent that reads `SKILL.md`. Point your agent at it.
- **Never prompts when stdin is not a TTY.** Destructive commands take `--yes`.
- Output is colorless when piped or when `NO_COLOR` is set, and is plain ASCII
  by default so a non-UTF-8 Windows console survives it.
- The literal next command for every action the API says is available is
  printed in human output and returned as `next_commands` in `submit --json`,
  so neither a person nor an agent has to translate a verb into a command.

## Python library

The library is the same code the CLI is built on, importable on its own:

```python
import os
from iota_hub import Client

with Client(api_key=os.environ["IOTA_HUB_API_KEY"]) as client:
    result = client.submit_folder("path/to/observation")
    print(result.outcome, result.observation_id)

    if result.outcome == "needs_attention":
        checks = result.observation.checks
        for finding in checks.findings if checks else []:
            print(finding.fingerprint, finding.message, finding.how_to_fix)
        # Fix one and the draft re-arms its checks:
        client.replace_file(result.observation_id, "lightcurve", "fixed.csv")

    for observation in client.iter_observations(submission_status="draft"):
        print(observation.observation_id, observation.submission_status)

    client.download_files("observation", result.observation_id, "downloads/")
```

`result.outcome` is the vocabulary to branch on: `submitted`, `ready` (clean but
left a draft), `needs_attention` (findings, something missing, or the run
errored), or `draft` (`wait=False`, uploaded and finalized but not waited on).

Two layers, deliberately kept apart. The **transport** layer is one method per
public API operation (`create_draft`, `submit_draft`, `list_events`,
`list_observation_files`, ...), returning hand-written pydantic models. The
**workflow** layer is what the API cannot do for you: `submit_folder`,
`submit_files`, `wait_for_checks`, `replace_file`, `download_files`, and the
folder-to-slot mapping in `iota_hub.files`.

Retries (`429`/`5xx`, honouring `Retry-After`), one idempotency key per logical
call, and the base64 SHA-256 declared on every upload are all handled for you.
Errors are one exception type, `IotaHubError`, carrying `.code`, `.message`,
`.hint`, `.details`, `.status` and `.retry_after`; the subclasses `AuthError`,
`RateLimitError`, `NotFoundError`, `ConflictError`, `RequestError` and
`TransportError` group the codes by category, so you can catch broadly without
enumerating them. Models ignore fields they do not know, so an additive API
change never breaks an installed version.

## Environments

The default target is production, `https://api.iota-hub.com`. **The public API
is not live in production yet** — its release is pending, and until then the
development deployment is where third-party developers build: sign up there,
create a key, and use a `dev` profile. The development base URL is published on
that site's `/developers` page; no non-production URL is baked into this
package.

```bash
iota-hub auth login --profile dev --base-url <the dev base URL>
iota-hub --profile dev observations list
```

`--base-url` (and `IOTA_HUB_BASE_URL`) is the **API base**; the client appends
`/public/v1` itself. Local development against a checkout of the API is
`--base-url http://localhost:8000`.

## Under the hood: the HTTP API

Everything here is a client of one frozen, versioned HTTP surface, `/public/v1`,
which stays public for anyone the CLI and the library do not fit. It is the hard
way — presigned S3 uploads, polling, idempotency keys — which is exactly why
this package exists.

The reference and the quickstart are at <https://iota-hub.com/developers>
(interactive reference at `/developers/reference`, the raw workflow guide at
`/developers/guide.md`). Porting a client to another language starts from
[`spec/openapi.json`](spec/openapi.json), vendored here and checked against the
live document in CI, and
[`docs/client-conventions.md`](docs/client-conventions.md), which specifies
everything the API itself does not: configuration, retries, polling, folder
mapping, exit codes and output.

## Requirements

Python 3.10 or newer, on Windows, macOS or Linux. Runtime dependencies are
`httpx`, `pydantic`, `typer` and `platformdirs`.

## Versioning

Semantic versioning, `0.x` until the public API's production release. A
**major** bump is removing a command or a flag, or changing the shape of a
`--json` document. New commands, new optional flags and new fields in a `--json`
document are additive and are not.

## Contributing

Issues and pull requests about this client go to
[skycarl/iota-hub-python](https://github.com/skycarl/iota-hub-python). Issues
about the API itself — a wrong status code, a missing filter, an unclear
finding — go to [skycarl/iota-hub](https://github.com/skycarl/iota-hub) with the
label `drafts-and-api`.

## License

MIT. See [LICENSE](LICENSE).

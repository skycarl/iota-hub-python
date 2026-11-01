# IOTA Hub CLI guide

Printed by `iota-hub guide`; also at <https://iota-hub.com/developers/cli>.
Each step gives the command, then the library call. The HTTP calls behind them
are at <https://iota-hub.com/developers/api>.

## Install

    uvx iota-hub --version            # run it without installing
    uv tool install iota-hub          # or put it on your PATH, with uv
    pipx install iota-hub             # or with pipx

Python 3.10 or newer, on Windows, macOS or Linux.

## Authenticate

Create a key on <https://iota-hub.com/developers> (API keys). Give it to the
tool one of two ways, never as a command-line argument:

    iota-hub auth login               # hidden prompt, stored per profile
    export IOTA_HUB_API_KEY=...       # the CI and agent path, no login step

    iota-hub auth status              # profile, base URL, key prefix, valid?
    iota-hub auth logout              # forget a stored profile

Library: `Client(api_key=..., base_url=...)`, or `iota_hub.config.resolve()`
for the same flag / environment / config-file precedence.

## Submit an observation

One command, run in the folder holding one observation:

    cd 20180305_9721_Doty_Observer_POS
    iota-hub submit

It creates the draft, uploads the files, finalizes, waits for the observer
checks and submits when the draft is `ready`. A draft it cannot submit stays
in your Drafts, to finish in the web app.

    iota-hub submit --draft           # everything except the final submit
    iota-hub submit --dry-run         # print the mapping and target, call nothing
    iota-hub submit --no-wait         # return after finalize with the draft id
    iota-hub submit --timeout 900     # seconds to wait for checks (default 600)
    iota-hub submit path/to/folder    # a folder other than the current one

### Which file goes where

Files map to slots by extension; there is no content sniffing.

| Slot              | Rule                                           | Required |
| ----------------- | ---------------------------------------------- | -------- |
| `report`          | exactly one `.xlsx` / `.xls` / `.json`         | yes      |
| `lightcurve`      | exactly one `.csv`                             | yes      |
| `log`             | exactly one `.txt` whose name contains `log`   | yes      |
| `vizier`          | at most one `.dat`                             | no       |
| `aperture_record` | at most one `.lc` / `.pymovie`                 | no       |

Anything else in the folder (a PyOTE plot, notes) is uploaded as an
attachment: at most 25, 50 MB each, no executables or scripts.
`--no-attachments` uploads only the slots; `--attach PATH` adds a file from
elsewhere.

Ambiguity is an error, never a guess. Two candidates for one slot, or none for
a required one, stops the command and names the flag that settles it:
`--report`, `--lightcurve`, `--log`, `--vizier` or `--aperture-record`.

    iota-hub submit --lightcurve 20180305_9721_Doty_Observer_POS.csv

One observation per directory: a folder holding several stations (`_POS-1`,
`_POS-2`) is not supported.

Library: `client.submit_folder(dir, lightcurve=..., attach=[...], wait=True,
submit=True)`, `client.submit_files({"report": path, ...})`,
`iota_hub.files.map_folder(dir)`.

## Read the result

`submit` ends in one of four outcomes:

| Outcome           | Meaning                                              | Exit |
| ----------------- | ---------------------------------------------------- | ---- |
| `submitted`       | the observation is submitted (or already was)         | 0    |
| `ready`           | clean, but `--draft` left it a draft                  | 0    |
| `needs_attention` | open findings, something missing, or the run errored  | 4    |
| `draft`           | `--no-wait`: uploaded and finalized, not waited on    | 0    |

When it leaves a draft it prints `Web:`, the draft's page in the web app, and
the literal next command for each action the API offers (`iota-hub drafts
files add <id> lightcurve <path>`, `iota-hub drafts dismiss <id> <fingerprint>
--note "..."`, ...). One action has no command and is finished in the web app:
resolving an event-files conflict.

### Skipped checks and the OWC link

A check that could not run is skipped and never blocks submit; `submit`
prints a `WARNING` block on stderr listing each one. The usual one is check 11,
which needs the Occult Watcher Cloud event. The Hub finds that event itself;
when it cannot choose, the warning lists the candidates and the commands that
settle it:

    iota-hub drafts owc <id> --pick <owc_event_id>    # one of the candidates
    iota-hub drafts owc <id> --link <url-or-id>       # any OWC event, checked
    iota-hub drafts owc <id> --none                   # deliberately no link

Library: `SubmitResult` carries `.outcome`, `.observation`, `.skipped_checks`
and `.owc_commands`.

## Fix and resubmit

    iota-hub drafts list                                  # your open drafts
    iota-hub drafts show <id>                             # state and findings
    iota-hub drafts files add <id> lightcurve <path>      # replace a file
    iota-hub drafts files rm <id> vizier                  # clear a slot
    iota-hub drafts files add <id> attachment <path>      # add an attachment
    iota-hub drafts files rm <id> attachment <attachment_id>  # remove one
    iota-hub drafts dismiss <id> <fingerprint> --note "why this is acceptable"
    iota-hub drafts dismiss <id> <fingerprint> --undo     # undo a dismissal
    iota-hub drafts owc <id> --pick <owc_event_id>        # settle the OWC link
    iota-hub drafts confirm-asteroid <id>                 # unnumbered id is deliberate
    iota-hub drafts comment <id> "text"                   # comment for the reviewer
    iota-hub drafts check <id>                            # start a check run
    iota-hub drafts submit <id>                           # submit when clean
    iota-hub drafts delete <id> --yes                     # throw a draft away

Replacing a file re-runs the checks by itself. A finding about a missing
required item cannot be dismissed: supply the item. `drafts submit` on an
observation that is already submitted succeeds.

Library: `client.replace_file(id, slot, path)`, `delete_file`,
`dismiss_finding(id, fingerprint, note)`, `undo_dismissal`, `run_checks`,
`wait_for_checks`, `update_draft(id, version=..., ...)`,
`submit_draft(id)`, `delete_draft`.

## Already submitted? Check, and amend

Before creating a draft a script may have created already, look it up by what
it observed:

    iota-hub observations check --asteroid-id 9721 --asteroid-name Doty \
        --star-id 1234-56789 --date 2018-03-05 [--star-catalog UCAC4] [--station 2]

`found` is `observation` (your own draft or submission), `event` (not yours,
but the event exists) or `none`. It exits 0 whatever it finds.

To correct a submitted observation, replace the file; there is no separate
amend command. The observation becomes `amended`. A replaced report must
describe the same observation, else `identity_changed` (exit 2). Once the
event is in review every file change is `already_processed` (exit 1); ask a
coordinator.

Library: `client.check_observation(asteroid_name=..., star_id=...,
observed_date=..., ...)`, `client.replace_file(id, slot, path)`.

## Reads and downloads

    iota-hub observations list --submission-status draft
    iota-hub observations show <id>
    iota-hub events list --date-from 2024-01-01 --date-to 2024-12-31
    iota-hub events show <id>
    iota-hub files download observation <id> -o ./downloads
    iota-hub files download event <id> --slot lightcurve -o ./downloads --force

Filters follow the API's query parameters as `--kebab-case` flags; `--help`
lists them. A download never silently overwrites: pass `--force`.

Library: `client.iter_observations(...)`, `iter_events(...)`,
`get_observation`, `get_event`, `download_files(kind, id, dest, slot=...)`.

## Automation and agents

- `--json` on any command prints exactly one JSON document on stdout; errors
  are JSON on stderr in the API's problem-details shape plus `exit_code`.
- Exit codes: `0` success, `1` error, `2` usage (bad flags, or an ambiguous or
  incomplete mapping), `3` auth, `4` open findings or not ready, `5` rate
  limited. Branch on `code` and the exit code, never on a message.
- Retries, idempotency keys and upload checksums are handled for you. Do not
  wrap a mutating command in your own retry loop.
- The tool never prompts when stdin is not a TTY; destructive commands take
  `--yes`. No color when piped or under `--json`.
- `SKILL.md` in <https://github.com/skycarl/iota-hub-python> is a ready-made
  skill for an agent.

## Errors

Every failure carries a stable `code`: the API's (listed per operation in
the reference at <https://iota-hub.com/developers/reference>) plus the tool's
own, `ambiguous_files`, `missing_files`, `invalid_extension`,
`not_a_directory`, `unknown_profile` and `usage_error` (all exit 2) and
`timeout` (exit 4, checks still running).

## Profiles

    iota-hub auth login --profile <name> --base-url <API base URL>
    iota-hub observations list --profile <name>   # or IOTA_HUB_PROFILE=<name>

A profile stores a key with its base URL. Settings resolve flag > environment
> config file > default; the tool prints `Target: <base URL>` on stderr when
the target is not the default. The config file lives at:

    Linux     ~/.config/iota-hub/config.toml
    macOS     ~/Library/Application Support/iota-hub/config.toml
    Windows   %APPDATA%\iota-hub\config.toml

## Where the rest lives

- What belongs in each file: <https://iota-hub.com/data-submission>
- API keys: <https://iota-hub.com/developers>
- The HTTP API: <https://iota-hub.com/developers/api>, the reference at
  `/developers/reference`, the OpenAPI document at `/developers/openapi.json`
- Source, `SKILL.md`, client conventions:
  <https://github.com/skycarl/iota-hub-python>

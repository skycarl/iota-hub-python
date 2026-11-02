# IOTA Hub CLI guide

Use `iota-hub` to submit observations, manage drafts and download files from
IOTA Hub. Run `iota-hub guide` to read this guide in your terminal.

## Install

Requires Python 3.10 or newer on Windows, macOS or Linux.

Install with either uv or pipx:

    uv tool install iota-hub

    pipx install iota-hub

To run a command without installing the tool, use `uvx iota-hub`, for example:

    uvx iota-hub --version

## Authenticate

Create an [API key](https://iota-hub.com/developers/keys), then run:

    iota-hub auth login

Paste your key at the hidden prompt. The CLI checks the key and saves it for
future commands.

    iota-hub auth status    # Show your settings and check the key
    iota-hub auth logout    # Remove the saved key and settings

For automation, set the `IOTA_HUB_API_KEY` environment variable instead.

## Submit an observation

Keep each observation's files in a separate folder, then run:

    iota-hub submit path/to/folder

Or run `iota-hub submit` from inside the folder.

The command uploads your files, runs the observer checks and submits the
observation when it is ready. If something needs attention, it leaves a
draft for you to finish in the CLI or web app.

| Option | Effect |
| ------ | ------ |
| `--draft` | Upload and run checks, but leave the observation as a draft. |
| `--dry-run` | Preview the files and API address without uploading anything. |
| `--no-wait` | Return the draft ID after uploading and starting checks. The command does not wait or submit. |
| `--timeout 900` | Set the wait for checks to 900 seconds. The default is 600. Request time is additional. |

### Which file goes where

The CLI selects files by their extensions and filenames:

| File | Filename requirements | Required |
| ---- | --------------------- | -------- |
| Report | One `.xlsx`, `.xls` or `.json` file | Yes |
| Light curve | One `.csv` file | For POS/UNSURE |
| Log | One `.txt` file with `log` in its name | For POS/UNSURE |
| VizieR data | One `.dat` file | No |
| Aperture record | One `.lc` or `.pymovie` file | No |

If more than one file matches, specify which one to use:

    iota-hub submit --lightcurve observation.csv

Use `--report`, `--lightcurve`, `--log`, `--vizier` or `--aperture-record`
followed by the file path. The CLI reports these problems before uploading.

Negative (NEG) observations do not need a light curve or log. The CLI stops
before uploading if the folder has no report. If a POS or UNSURE observation is
missing its light curve or log, you get a draft that lists what it still needs.

Submit each station's observation from a separate folder. The CLI cannot split
a folder containing files for multiple stations.

Other files in the folder are uploaded as attachments. You can include up to
25 attachments, each up to 50 MB. Executables and scripts are not allowed.
Use `--no-attachments` to exclude these extra files, or `--attach PATH` to add
an attachment explicitly. Files whose names start with a dot and files in
subfolders are excluded from automatic selection.

## Read the result

The command prints an outcome:

| Outcome | Meaning |
| ------- | ------- |
| `submitted` | The observation has been submitted. |
| `ready` | The draft is ready to submit. You used `--draft` to keep it as a draft. |
| `needs_attention` | The draft has problems reported by the checks, missing information or a failed check run. |
| `draft` | You used `--no-wait`. Files are uploaded, but the command has not waited for checks. |

For an unfinished draft, the output includes a web link and commands for the
next steps. Conflicts with existing event files must be resolved in the web app.

### Skipped checks and the OWC link

Checks that could not run appear as warnings. They do not prevent submission.
For example, check 11 needs a link to the Occult Watcher Cloud (OWC) event.

If the Hub cannot select an OWC event automatically, use one of these commands:

    iota-hub drafts owc <id> --pick <owc_event_id>  # Select a suggested event
    iota-hub drafts owc <id> --link <url-or-id>    # Link another OWC event
    iota-hub drafts owc <id> --none                # Continue without a link

## Fix and resubmit

List your drafts and inspect one using its observation ID:

    iota-hub drafts list
    iota-hub drafts show <id>

Replace a file or add an attachment:

    iota-hub drafts files add <id> lightcurve <path>
    iota-hub drafts files add <id> attachment <path>

Replacing a file automatically runs the checks again. To remove a file:

    iota-hub drafts files rm <id> vizier
    iota-hub drafts files rm <id> attachment <attachment_id>

If a finding is acceptable, dismiss it with an explanation. You can undo the
dismissal later:

    iota-hub drafts dismiss <id> <fingerprint> --note "why this is acceptable"
    iota-hub drafts dismiss <id> <fingerprint> --undo

Each finding has an identifier called a `fingerprint`, shown by `drafts show` (labeled `ID` on a terminal). Missing required files
or information must be supplied before submission.

Other draft commands:

| Command | Action |
| ------- | ------ |
| `iota-hub drafts confirm-asteroid <id>` | Confirm that the asteroid designation was entered correctly when no number was given. |
| `iota-hub drafts comment <id> "text"` | Set a comment for the reviewer. |
| `iota-hub drafts check <id>` | Run checks and wait for the results. |
| `iota-hub drafts submit <id>` | Submit a ready draft. |
| `iota-hub drafts delete <id> --yes` | Delete an unsubmitted draft. |

`drafts submit <id>` also succeeds if that observation is already submitted.

## Check for an existing observation

Before repeating a submission, check whether you already have a matching
draft or submitted observation:

    iota-hub observations check --asteroid-id 9721 --asteroid-name Doty --star-id 1234-56789 --date 2018-03-05

Use the observation's UTC date. Add `--star-catalog UCAC4` or `--station 2`
when needed to identify the observation.

The result tells you whether your observation was found. If it was, use the
returned ID to continue the draft or update the submission. If it was not,
the result indicates whether a matching event exists in the Hub.

## Update a submitted observation

Use the same file command to replace a submitted file:

    iota-hub drafts files add <id> lightcurve <path>

The observation's status changes to `amended`. If you replace the report,
it must still describe the same observation. Otherwise, the API rejects it
with `identity_changed` (exit code `2`).

Once the event enters review, you can no longer change its files
(`already_processed`, exit code `1`). Contact a coordinator if a correction is
needed.

## Read observations and download files

    iota-hub observations list --submission-status draft
    iota-hub observations show <id>
    iota-hub events list --date-from 2024-01-01 --date-to 2024-12-31
    iota-hub events show <id>
    iota-hub files download observation <id> -o ./downloads
    iota-hub files download event <id> --slot occelmnt -o ./downloads

Use `--help` on a command to see its filters and options.
`observations list` and `events list` return one page of results. Add `--all` to retrieve every page.

Downloads preserve existing files. Add `--force` to overwrite them.

## Automation and agents

Add `--json` to get results as JSON on stdout. Errors are written as JSON to
stderr, with a `code`, `message`, `hint`, `details` and `exit_code`. Use the
error code in scripts, since message wording can change.

| Exit code | Meaning |
| --------- | ------- |
| `0` | Success |
| `1` | General error, including `already_processed` |
| `2` | Invalid command-line arguments or local files, or `identity_changed` |
| `3` | Authentication or permission error |
| `4` | `submit` or `drafts submit` found the draft needs attention or not ready, or a wait timed out |
| `5` | Rate limit reached |

A successful `observations check` lookup exits with `0` even when nothing
matches. Read its JSON `found` field to distinguish the results:

| `found` | Meaning |
| ------- | ------- |
| `observation` | A matching draft or submission belongs to you. |
| `event` | No matching observation belongs to you, but a matching event exists. |
| `none` | Neither was found. |

The CLI retries temporary failures automatically and verifies file uploads.
Avoid rerunning an entire submission, which can create another draft. Check for
an existing observation before starting again.

Commands do not prompt when input is redirected or piped. Pass `--yes` to
confirm file removal or draft deletion in scripts. Output has no color when
piped or when using `--json`.

Agent instructions are available in
[SKILL.md](https://github.com/skycarl/iota-hub-python/blob/main/SKILL.md).

## Errors

Errors include a code and, when available, instructions for fixing the problem.
Common examples:

| Code | What to do |
| ---- | ---------- |
| `ambiguous_files` | Specify which file to use with the relevant file option. |
| `missing_files` | Add the report to the folder or correct the file path. |
| `invalid_extension` | Use a supported file type. |
| `not_a_directory` | Pass the path to a folder. |
| `unknown_profile` | Check the profile name or create it with `auth login`. |
| `usage_error` | Check the command's `--help`. |
| `timeout` | Checks have not finished. Use `drafts show <id>` to check progress. |

API error codes are listed in the
[API reference](https://iota-hub.com/developers/reference).

## Profiles

Use a named profile to save a key and API address together:

    iota-hub auth login --profile <name> --base-url <API base URL>
    iota-hub observations list --profile <name>

You can also select a profile with `IOTA_HUB_PROFILE`. Command-line settings
take precedence over environment variables, followed by saved settings and
defaults. API keys are supplied through login or `IOTA_HUB_API_KEY`, which
overrides a profile's saved key even with `--profile`.

When using a non-default API address, the CLI prints it on stderr (as `Target:` when not
on a terminal).
Settings are stored in:

    Linux     ~/.config/iota-hub/config.toml
    macOS     ~/Library/Application Support/iota-hub/config.toml
    Windows   %LOCALAPPDATA%\iota-hub\config.toml

## Related documentation

- [Data submission requirements](https://iota-hub.com/data-submission)
- [API keys](https://iota-hub.com/developers/keys)
- [Python library guide](https://iota-hub.com/developers/library)
- [HTTP API guide](https://iota-hub.com/developers/api)
- [API reference](https://iota-hub.com/developers/reference)
- [Observer checks](https://iota-hub.com/developers/checks)
- [OpenAPI document](https://iota-hub.com/developers/openapi.json)
- [Source and client conventions](https://github.com/skycarl/iota-hub-python)

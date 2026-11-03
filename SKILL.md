---
name: iota-hub
description: Submit occultation observation files to IOTA Hub and manage drafts with the iota-hub CLI; use when asked to submit, check, or fix an observation.
---

# Submit and manage IOTA Hub observations

## Prerequisites

- Run the CLI with `uvx iota-hub ...`, or install it with `pipx install iota-hub`.
- Use a key in `IOTA_HUB_API_KEY` or a profile saved with `iota-hub auth login`.
  Never put a key in a command-line argument: it can appear in shell history
  and process listings. If no key is configured, ask the observer to run
  `iota-hub auth login`.
- Use `iota-hub auth status` to check the configuration and key.

## Submit

```bash
iota-hub submit <observation-folder> --json
```

Use `--dry-run` to preview the files and target API without sending a request.
Use `--draft` when the observer wants to review the checks before submission.
`--json`, `--profile` and `--base-url` work before or after the subcommand.

Read `outcome` from the JSON result:

| `outcome` | Meaning and next step |
| --------- | --------------------- |
| `submitted` | The observation is submitted. Report its ID. |
| `ready` | Ready to submit, but `--draft` left it as a draft. |
| `needs_attention` | Address the findings or missing requirements below, or give the observer `web_url`. |
| `draft` | `--no-wait` returned without waiting for checks. Give the observer `web_url`. |

The top-level `web_url` links to the observation in the web app.

If a previous run may have created a draft, check before repeating `submit`:

```bash
iota-hub observations check --asteroid-id <number> --asteroid-name <name> --star-id <star> --date <YYYY-MM-DD> --json
```

If `found` is `observation`, use the returned ID to continue working on the
observer's draft or submission. `event` means the event exists but no matching
observation belongs to this observer. `none` means neither was found.

The CLI already retries temporary API failures. Do not wrap it in a retry loop.
If an error includes `details.observation_id`, use that ID to recover the draft.

## Review checks

`observation.checks.findings[]` describes each finding:

- `message`: the problem.
- `how_to_fix`: the suggested correction.
- `fingerprint`: the ID used by `drafts dismiss`.
- `severity`, `evidence`, `doc_url`: supporting details.

`observation.readiness.missing_required` lists missing files or identity fields
parsed from the report. Supply missing requirements; they cannot be dismissed.

Always report skipped checks and their reasons, even when the outcome is
`submitted` or `ready`. They appear in `skipped_checks` on the submission result
and `checks.skipped[]` on the draft. Skipped checks do not block submission.

If `owc_commands` is not empty, the Occult Watcher Cloud (OWC) link needs a
choice. Show the observer `observation.owc.resolution.candidates` and ask them
to select a candidate, provide a link or proceed without one. Do not choose
for them. Run the command for their choice: `drafts owc <id> --pick
<owc_event_id>`, `--link <url>` or `--none`.

Use the commands in `next_commands`, or the human output's `Next:` lines, for
available actions. Replace placeholders such as `<path>` with the appropriate
value. A dismissal note must come from the observer.

## Update a draft

```bash
iota-hub drafts show <id>
iota-hub drafts files add <id> lightcurve <path>
iota-hub drafts files rm <id> vizier
iota-hub drafts files add <id> attachment <path>
iota-hub drafts files rm <id> attachment <attachment_id>
iota-hub drafts dismiss <id> <fingerprint> --note "observer's reason"
iota-hub drafts dismiss <id> <fingerprint> --undo
iota-hub drafts owc <id> --pick <owc_event_id>
iota-hub drafts confirm-asteroid <id>
iota-hub drafts comment <id> "text"
iota-hub drafts check <id>
iota-hub drafts submit <id>
```

`files add` replaces the named file, including an `aperture_record`, and reruns
the draft's checks. `files rm` clears the named file or removes an attachment.
Attachment IDs are listed by `drafts show`. `confirm-asteroid` records that
an asteroid ID without a number is intentional. `comment` sets the comment
for the reviewer.

### Correct a submitted observation

Use `drafts files add` to replace a file in a submitted observation. Its status
becomes `amended`; this does not start a new check run.

- `identity_changed` (exit `2`): the replacement report describes a different
  observation. Show `details.changed` and ask the observer to correct the
  report. Never work around this by creating another observation.
- `already_processed` (exit `1`): the event is in review. A coordinator must
  make further changes.
- `forbidden` (exit `3`): an observer cannot clear a file slot in a submitted
  observation.

Other commands include `drafts list`, `observations list|show`,
`events list|show`, `files download observation <id> [--slot lightcurve]
[-o DIR] [--force]` and `drafts delete <id> --yes`.

## Exit codes and errors

| Code | Meaning |
| ---- | ------- |
| `0` | Success. |
| `1` | Other errors, including server and network failures. |
| `2` | Invalid arguments or local files, ambiguous file selection, attachment limits, or `identity_changed`. |
| `3` | Missing key or an authorization error. |
| `4` | Findings, missing requirements or a check timeout. |
| `5` | Rate limit still exceeded after automatic retries. |

With `--json`, errors go to stderr as an object with `code`, `message`, `hint`,
`details`, `status`, `retry_after` and `exit_code`. Check `code`, not message
text. Invalid arguments return `usage_error` with exit code `2`.

## Observer decisions and file handling

- Never dismiss a finding without a reason supplied by the observer. The note
  is a permanent record attributed to them.
- Ask before deleting a draft with `drafts delete`.
- Ask the observer to choose an OWC event or confirm an asteroid ID. These
  commands record their decision.
- For a non-production target, confirm that the observer intends to use it.
  The CLI prints `Target: <base URL>` on stderr for a non-default target;
  `--dry-run` also prints it for production. With `--json`, the line appears
  only after a result is returned. Use `auth status --json` to check the target
  before submitting.
- `auth login` selects `--profile`, then `IOTA_HUB_PROFILE`, then the saved
  `default_profile`, then `default`. Specify `--profile` when adding a second
  profile to avoid replacing the current one.
- Logging out of the default profile does not select another. Commands without
  a profile selection then use a profile named `default`, or fail with
  `missing_api_key` (exit `3`). Select the intended profile explicitly.
- If multiple files match a type, show the observer the candidates and ask
  which to use. Pass their choice with `--report`, `--lightcurve`, `--log`,
  `--vizier` or `--aperture-record`. If the report is missing, ask for it.
- A NEG observation needs no light curve or log. A POS or UNSURE observation
  without them stays a draft, with the missing files listed in
  `readiness.missing_required`.
- Reports are NA spreadsheets (`.xlsx`/`.xls`) or Tangra JSON (`.json`). Tangra
  `.lc` and PyMovie `.pymovie` files use the optional `aperture_record` slot.
  `--log PATH` accepts any `.txt` file; automatic selection requires `log`
  in its name. Multiple stations must use separate folders.
- Other files are attachments: up to 25, up to 50 MB each, with no executables
  or scripts. For `blocked_attachment`, `attachment_too_large` or
  `too_many_attachments` (exit `2`), show the affected files. Ask whether the
  observer wants to move them out of the folder or use `--no-attachments`.
  Do not move or delete their files yourself.
- Use `submit --dry-run` to preview all uploads, including attachments.

## Further documentation

`iota-hub guide` prints the full CLI guide offline. The
[HTTP API guide](https://iota-hub.com/developers/api) describes direct API calls.
Create keys on the [API keys page](https://iota-hub.com/developers/keys).

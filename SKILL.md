---
name: iota-hub
description: Submit occultation observation files to IOTA Hub and manage drafts with the iota-hub CLI; use when asked to submit, check, or fix an observation.
---

# Submitting an observation to IOTA Hub

## Prerequisites

- `uv` (then run `uvx iota-hub ...`) or `pipx install iota-hub`.
- A key in `IOTA_HUB_API_KEY`, or a profile the observer saved with
  `iota-hub auth login`. **Never paste a key into a command line** — it leaks
  into shell history and `ps`. If no key is configured, stop and ask the
  observer to run `iota-hub auth login`.
- Check `iota-hub auth status` if anything looks unconfigured.

## The one-command path

```bash
cd <the observation folder>
iota-hub submit --json
```

Read `outcome` from the JSON document on stdout:

| `outcome`         | What to do                                                    |
| ----------------- | ------------------------------------------------------------- |
| `submitted`       | Done (also when it was already submitted). Report the id.      |
| `ready`           | Clean but still a draft (`--draft` was used). Nothing to fix.   |
| `needs_attention` | Work the fix loop below, or give the observer `web_url`.        |
| `draft`           | `--no-wait` was used; nothing has been checked yet.             |

`web_url` (top level of the document) is the observation in the web app — hand
it to the observer whenever the outcome is `needs_attention` or `draft`.

**Before re-running a submit that may already have created a draft** (an
earlier run crashed or its output was lost), look first instead of creating a
second one:

```bash
iota-hub observations check --asteroid-id <number> --asteroid-name <name> \
    --star-id <star> --date <YYYY-MM-DD> --json
```

`found: "observation"` is the observer's own draft or submission (work on that
id); `"event"` or `"none"` means there is none of theirs yet.

Use `--dry-run` first if you want to show the observer the slot mapping before
anything is created. It calls nothing.

`--json`, `--profile` and `--base-url` work after the subcommand, as above, and
before it as well.

## Reading `needs_attention`

The document carries the observation, including `checks.findings[]`. Each
finding has:

- `message` — what is wrong,
- `how_to_fix` — what the observer should do about it,
- `fingerprint` — the id you pass to `drafts dismiss`,
- `severity`, `evidence`, `doc_url` for context.

`readiness.missing_required` lists anything still missing (a file slot, or an
identity field parsed from the report).

**Always report skipped checks.** `skipped_checks` in the `submit --json`
document (and `checks.skipped[]` on the observation) lists every check that did
not run, with its reason. They never block submit, so a `submitted` or `ready`
outcome can still have them — tell the observer which checks did not run and
why. When `owc_commands` is not empty, the Hub could not choose the Occult Watcher
Cloud event itself: show the observer the candidates (`observation.owc.resolution.candidates`)
and let them choose — never pick one yourself. Then run the command they chose
(`iota-hub drafts owc <id> --pick <owc_event_id>`, `--link <url>`, or `--none`). `next_commands` (in `submit --json`) and the
"Next:" lines of human output are the literal next commands for each action the
API says is available; run them as printed rather than paraphrasing them.

## The fix loop

```bash
iota-hub drafts show <id>                          # current state and findings
iota-hub drafts files add <id> lightcurve <path>   # replace a file (any slot, incl. aperture_record)
iota-hub drafts files rm <id> vizier               # clear a slot
iota-hub drafts files add <id> attachment <path>   # add an attachment
iota-hub drafts files rm <id> attachment <attachment_id>  # remove one (ids in drafts show)
iota-hub drafts dismiss <id> <fingerprint> --note "why this is acceptable"
iota-hub drafts dismiss <id> <fingerprint> --undo  # undo a dismissal
iota-hub drafts owc <id> --pick <owc_event_id>     # settle the OWC link (or --link URL, --none)
iota-hub drafts confirm-asteroid <id>              # a non-numeric asteroid id is deliberate
iota-hub drafts comment <id> "text"                # the comment a reviewer reads
iota-hub drafts check <id>                         # start a check run
iota-hub drafts submit <id>                        # submit when clean
```

Replacing a file re-arms the checks automatically. A finding about a missing
required item cannot be dismissed — supply the item instead.

**Amending a submitted observation** is the same `drafts files add` (the
observation then shows `status: amended`). `identity_changed` (exit `2`) means
the replacement report describes a different observation — show the observer
`details.changed` and let them fix the report; never work around it by
creating a new observation. `already_processed` (exit `1`) means the event is
in review: only a coordinator can change it now. An observer can never clear a
slot of a submitted observation (`forbidden`).

Other commands: `iota-hub drafts list`, `iota-hub observations list|show`,
`iota-hub events list|show`,
`iota-hub files download observation <id> [--slot lightcurve] [-o DIR] [--force]`,
`iota-hub drafts delete <id> --yes`.

## Exit codes

Branch on these, not on the text.

| Code | Meaning                                                    |
| ---- | ---------------------------------------------------------- |
| `0`  | success                                                     |
| `1`  | error (including server and network failures)               |
| `2`  | usage: bad flags, an ambiguous or incomplete file mapping, an attachment the API would refuse, or a replacement report for another observation (`identity_changed`) |
| `3`  | auth: no key, or the API rejected it                        |
| `4`  | open findings / not ready — the fix loop above              |
| `5`  | rate limited after the built-in retries                     |

With `--json`, an error is a JSON object on **stderr** with `code`, `message`,
`hint`, `details`, `status`, `retry_after` and `exit_code`. Branch on `code`,
never on `message`. A bad invocation (an unknown flag, a missing argument) is
JSON too: `usage_error`, exit code `2`.

## Safety rules

- **Never dismiss a finding without a reason the observer gave you.** The note
  is a permanent, attributed record; do not invent one.
- **Never delete a draft** (`drafts delete`) without asking the observer first.
- **Never choose an OWC event or confirm an asteroid id for the observer.**
  `drafts owc` and `drafts confirm-asteroid` record the observer's decision;
  ask them.
- Use `iota-hub submit --draft` when the observer wants to review the
  observation in the web app before it is submitted.
- If a `Target: <base URL>` line appears on stderr, the command is **not**
  pointed at production. Confirm with the observer that this is intended.
- An ambiguous mapping (two `.csv` files, no report, a spreadsheet *and* a
  `.json`) is exit code `2`. Do not pick one — show the observer the candidates
  and use the explicit flag they name (`--report`, `--lightcurve`, `--log`,
  `--vizier`, `--aperture-record`).
- The report is the NA spreadsheet (`.xlsx`/`.xls`) or a Tangra JSON report
  (`.json`); a Tangra `.lc` or PyMovie `.pymovie` goes in the optional
  `aperture_record` slot. `--log PATH` takes any `.txt` (e.g. a Tangra fit
  log); in a folder only a `.txt` with `log` in its name is picked up.
- Files the slots do not claim are uploaded as **attachments** (25 max, 50 MB
  each, no executables or scripts). If `submit` exits `2` with
  `blocked_attachment`, `attachment_too_large` or `too_many_attachments`, show
  the observer the named files and ask: move them out of the folder, or re-run
  with `--no-attachments`. Do not delete or move their files yourself.
- Use `submit --dry-run` to show the observer what would be uploaded,
  attachments included, before a real run.

## More

`iota-hub guide` prints the full workflow guide offline. The HTTP API behind all
of this is documented at <https://iota-hub.com/developers/api>; keys are
created on <https://iota-hub.com/developers/keys>.

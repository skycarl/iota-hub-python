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
| `submitted`       | Done. Report the observation id.                               |
| `ready`           | Clean but still a draft (`--draft` was used). Nothing to fix.   |
| `needs_attention` | Work the fix loop below.                                       |
| `draft`           | `--no-wait` was used; nothing has been checked yet.             |

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
identity field parsed from the report). `next_commands` (in `submit --json`) and the
"Next:" lines of human output are the literal next commands for each action the
API says is available; run them as printed rather than paraphrasing them.

## The fix loop

```bash
iota-hub drafts show <id>                          # current state and findings
iota-hub drafts files add <id> lightcurve <path>   # replace a file
iota-hub drafts files rm <id> vizier               # clear a slot
iota-hub drafts dismiss <id> <fingerprint> --note "why this is acceptable"
iota-hub drafts dismiss <id> <fingerprint> --undo  # undo a dismissal
iota-hub drafts check <id>                         # start a check run
iota-hub drafts submit <id>                        # submit when clean
```

Replacing a file re-arms the checks automatically. A finding about a missing
required item cannot be dismissed — supply the item instead.

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
| `2`  | usage: bad flags, or an ambiguous or incomplete file mapping |
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
- Use `iota-hub submit --draft` when the observer wants to review the
  observation in the web app before it is submitted.
- If a `Target: <base URL>` line appears on stderr, the command is **not**
  pointed at production. Confirm with the observer that this is intended.
- An ambiguous mapping (two `.csv` files, no `.xlsx`) is exit code `2`. Do not
  pick one — show the observer the candidates and use the explicit flag they
  name (`--report`, `--lightcurve`, `--log`, `--vizier`).
- Files the mapping does not claim are reported as "not uploaded". That is
  expected; the public API has no attachment slot. Say so rather than retrying.

## More

`iota-hub guide` prints the full workflow guide offline. The HTTP API behind all
of this is documented at <https://iota-hub.com/developers>.

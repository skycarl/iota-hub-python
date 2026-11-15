# IOTA Hub Python library guide

Use `iota_hub` to submit observations, manage drafts and download files from
Python.

## Install

Requires Python 3.10 or newer on Windows, macOS or Linux. Install with pip:

    pip install iota-hub

Or add it to a uv project:

    uv add iota-hub

To upgrade, run `pip install --upgrade iota-hub`, or
`uv lock --upgrade-package iota-hub` in a uv project.

## Connect

Create an [API key](https://iota-hub.com/developers/keys) and set the
`IOTA_HUB_API_KEY` environment variable. Keep the key out of your source code.

```python
import os
from iota_hub import Client

with Client(api_key=os.environ["IOTA_HUB_API_KEY"]) as client:
    result = client.submit_folder("path/to/observation")
    print(result.outcome, result.observation_id)
```

Use the same client for calls to the same API. The `with` block closes its
connections when finished. The examples below assume an open `client`.

The default API address is `https://api.iota-hub.com`. Pass `base_url` to use
another deployment. The library adds `/public/v1` to this address; an address
copied with `/public/v1` already on the end also works.

To use settings saved by the CLI, call `resolve()`:

```python
from iota_hub import Client, resolve

settings = resolve(profile="<name>")
with Client(settings.api_key, settings.base_url) as client:
    observation = client.get_observation("<observation_id>")
```

Omit `profile` to use the default selection. Settings passed to `resolve()`
take precedence over environment variables (`IOTA_HUB_API_KEY`,
`IOTA_HUB_BASE_URL`, `IOTA_HUB_PROFILE`), followed by saved settings and defaults.

A saved key stays with the URL saved beside it. These raise `IotaHubError`
with code `invalid_config` instead of sending a key to the wrong place:

- The key comes from the profile, and `resolve(base_url=...)` or
  `IOTA_HUB_BASE_URL` differs from the profile's saved URL. Use a profile
  saved for that URL, or pass a key for it with `api_key=` or
  `IOTA_HUB_API_KEY`.
- `IOTA_HUB_API_KEY` is set without `IOTA_HUB_BASE_URL` (or `base_url=`), and
  the selected profile stores a non-production URL. Set the URL as well, or
  unset `IOTA_HUB_API_KEY` to use the profile's key.

`resolve()` reads `os.environ` unless you pass `env=`.

## Submit an observation

`submit_folder` uploads the files, runs the observer checks and submits the
observation when it is ready:

```python
result = client.submit_folder("path/to/observation")
print(result.outcome, result.observation_id, result.observation.web_url)
```

Keep each observation in a separate folder. If the draft needs attention, it
remains available for you to finish in the library, CLI or web app. Submit each
station's observation from a separate folder; the library cannot split a folder
containing files for multiple stations.

| Argument | Effect |
| -------- | ------ |
| `attachments=False` | Exclude extra files in the folder. They are listed in `result.ignored`. |
| `attach=[path, ...]` | Add attachments explicitly. |
| `submit=False` | Upload and run checks, but leave the observation as a draft. |
| `wait=False` | Return after uploading and starting checks, without waiting or submitting. |
| `timeout=900` | Set the wait for checks to 900 seconds. The default is 600. Request time is additional. When it expires, `IotaHubError` with code `timeout` is raised; the draft is kept (`.details["observation_id"]`) and you can wait again with `client.wait_for_checks(observation_id)`. |
| `on_progress=print` | Print progress messages. You can supply your own function instead. |

If the files are in different folders, use `submit_files`:

```python
result = client.submit_files(
    {"report": "obs.xlsx", "lightcurve": "obs.csv", "log": "obs_log.txt"},
    attachments=["plot.png"],
)
```

### Which file goes where

`submit_folder` selects files by their extensions and filenames:

| File argument | Filename requirements | Required |
| ------------- | --------------------- | -------- |
| `report` | One `.xlsx`, `.xls` or `.json` file | Yes |
| `lightcurve` | One `.csv` file | For POS/UNSURE |
| `log` | One `.txt` file with `log` in its name | For POS/UNSURE |
| `vizier` | One `.dat` file | No |
| `aperture_record` | One `.lc` or `.pymovie` file | No |

If more than one file matches, specify which one to use:

```python
result = client.submit_folder("observation", lightcurve="observation/final.csv")
```

Explicit paths are used as given, so a relative path is relative to the
current working directory, not to the folder. The folder's other candidates
for that slot are not uploaded; they are listed in `result.ignored`. Pass one
in `attach=[...]` to upload it as an attachment anyway.

A folder with no report raises `MappingError` with code `missing_files`.
Multiple matches raise `MappingError` with code `ambiguous_files`. Both stop
the submission before any API requests.

Negative (NEG) observations do not need a light curve or log. If a POS or UNSURE
observation is missing either one, `result.outcome` is `needs_attention` and
`result.observation.readiness.missing_required` lists what it still needs.

Other files are uploaded as attachments: up to 25 files, each up to 50 MB.
Executables and scripts are not allowed. Files whose names start with a dot
and files in subfolders are excluded from automatic selection.

To preview file selection without uploading, call `iota_hub.map_folder(directory)`.

## Read the result

`result.outcome` tells you how the submission ended:

| Outcome | Meaning |
| ------- | ------- |
| `submitted` | The observation has been submitted. |
| `ready` | The draft is ready to submit. You used `submit=False` to keep it as a draft. |
| `needs_attention` | The draft has unresolved findings, missing information or a failed check run. |
| `draft` | You used `wait=False`. Files are uploaded, but the call has not waited for checks. |

`result.observation` contains the observation details, including its web link
(`web_url`) and any findings from the checks:

```python
if result.outcome == "needs_attention":
    checks = result.observation.checks
    if checks:
        for finding in checks.findings:
            print(finding.message, finding.how_to_fix)
```

`result.skipped_checks` lists checks that could not run. These do not prevent
submission. Check 12 ("Near predicted time"), for example, needs an Occult
Watcher Cloud (OWC) event. If the Hub could not select one,
`result.owc_commands` lists CLI commands for linking one. The library
equivalent is `update_draft` (see below).

`result.uploads` contains the selected filenames by file type.
`result.attachments` lists attachment filenames, `result.ignored` lists
files not uploaded (excluded by `attachments=False`, or other candidates for a
slot you chose a file for), and `result.symlinks` lists symbolic links found
in the folder, which are never uploaded.

## Fix and resubmit

Replace the file that needs correcting, then wait for checks before submitting:

```python
obs_id = result.observation_id
client.replace_file(obs_id, "lightcurve", "fixed.csv")

observation = client.wait_for_checks(obs_id)
if observation.readiness and observation.readiness.state == "ready":
    client.submit_draft(obs_id, version=observation.version)
```

Replacing a file in a draft automatically starts checks. To request another
check run, call `client.run_checks(obs_id)`.

You can also remove a file or add an attachment:

```python
client.delete_file(obs_id, "vizier")
client.add_attachment(obs_id, "notes.pdf")
client.delete_attachment(obs_id, attachment_id)
```

If a finding is acceptable, dismiss it with an explanation. Use its
`fingerprint` value to identify it:

```python
client.dismiss_finding(obs_id, fingerprint, note="why this is acceptable")
client.undo_dismissal(obs_id, fingerprint)  # Restore the finding if needed
```

Missing requirements (`readiness.missing_required`) cannot be dismissed.
Upload the missing file with `replace_file`. If a POS or UNSURE observation
was processed with software that produces no light curve or log, you can
instead submit without them: call `update_draft(obs_id, version=...,
bypass_required_files=True, comments="...")` and explain in `comments` for
the reviewer. Missing identity fields need a corrected report.

Use `update_draft` to set a reviewer comment, select an OWC event or confirm
an asteroid identifier. Pass the observation's latest `version` so the API
can detect changes made since you read it:

```python
observation = client.get_observation(obs_id)
client.update_draft(obs_id, version=observation.version, comments="Clouds after R.")
```

To settle the OWC link, pass `owc_link_choice` with `"picked"` and one of
`observation.owc.resolution.candidates[].owc_event_id`, `"pasted"` and any
OWC event ID or URL, or `"proceed_unlinked"` with no `owc_event`:

```python
candidate = observation.owc.resolution.candidates[0]
client.update_draft(
    obs_id,
    version=observation.version,
    owc_link_choice="picked",
    owc_event=candidate.owc_event_id,
)
```

The API raises `ConflictError` (`stale_version`) if that version is out of
date: read the observation again before retrying.

`submit_draft` succeeds if the observation is already submitted.
`delete_draft(obs_id)` deletes an unsubmitted draft.

## Check for an existing observation

Before repeating a submission, check whether you already have a matching draft
or submitted observation:

```python
answer = client.check_observation(
    asteroid_name="Doty",
    asteroid_id="9721",
    star_id="123456",
    observed_date="2018-03-05",
)
if answer.found == "observation":
    print(answer.observation.observation_id)
```

| `answer.found` | Meaning |
| -------------- | ------- |
| `observation` | A matching draft or submission belongs to you. |
| `event` | No matching observation belongs to you, but a matching event exists. |
| `none` | Neither was found. |

Take the values from the report; the example matches the Doty example report,
whose star is `TYC 123456`. All arguments are keyword-only strings:

- `observed_date` is the UTC date, `YYYY-MM-DD`.
- `asteroid_name` and `star_id` must match the report as it spells them; case
  and repeated spaces are ignored. Write `"1999 RM158"`, not `"1999RM158"`.
- Include `station` and `star_catalog` when needed to identify the
  observation.
- The event is looked up only when you pass `asteroid_id`. Without it, `none`
  only means you have no matching draft or observation.

If several of your observations match, the newest is returned. Use the
returned observation ID to continue the draft or update the submission.

## Update a submitted observation

Use `replace_file` to correct a file on your submitted observation. Its status
changes to `amended`. A replacement report must describe the same observation;
otherwise, the API rejects it with `identity_changed`.

Once the event enters review, the owner can no longer change its files
(`already_processed`). The observation's `status` is then `locked`, so
`client.get_observation(obs_id).status` tells you before you upload. Contact a
coordinator if a correction is needed.

Replacing a file in a submitted observation runs the observer checks again,
but their results appear only on the observation's page on IOTA Hub: a
submitted observation's `checks` is `None` in the API, and `run_checks` and
dismissals work on drafts only.

## Read observations and download files

```python
for obs in client.iter_observations(submission_status="draft"):
    print(obs.observation_id, obs.status)

for event in client.iter_events(date_from="2024-01-01", date_to="2024-12-31"):
    print(event.event_id, event.asteroid_name)

observation = client.get_observation(obs_id)
event = client.get_event(event_id)
client.download_files("observation", obs_id, "downloads/")
client.download_files("event", event_id, "downloads/", slot="occelmnt")
```

`iter_observations` and `iter_events` retrieve all pages. The corresponding
`list_observations` and `list_events` methods return one page. Filters use
the parameter names in the [API reference](https://iota-hub.com/developers/reference).

If a destination file already exists, `download_files` raises `IotaHubError`
with code `file_exists` before downloading anything. Pass `overwrite=True` to
replace existing files.
Without reviewer access, an event's file list is empty until the event is `COMPLETE`.

## Errors

API and connection errors raise `IotaHubError`. Its `.code` identifies the
error, `.message` explains it, and `.hint` suggests what to do next.
Use `.code` in error-handling logic, since message wording can change.

Hints are worded for the CLI and may name its flags. The library equivalents
are: `--force` is `overwrite=True`, `--lightcurve PATH` (and the other slot
flags) is `lightcurve=PATH`, `--no-attachments` is `attachments=False`,
`--attach PATH` is `attach=[PATH]`, and `--profile NAME` is
`resolve(profile=NAME)`.

| Exception | Meaning |
| --------- | ------- |
| `AuthError` | The key is missing, invalid, expired or lacks permission. |
| `NotFoundError` | The requested item was not found or is not visible to your account. |
| `ConflictError` | The action is not allowed in the current state, or the version is out of date. |
| `RateLimitError` | The request still exceeds a rate limit after retries. |
| `RequestError` | The API rejected the request as invalid. |
| `TransportError` | The request failed without an HTTP response. |

Additional information is available in `.details`, `.status` (the HTTP status)
and `.retry_after` (the requested retry delay). API error codes are listed in
the [reference](https://iota-hub.com/developers/reference). A `TransportError`
from a failed upload or download has `.details["slot"]` and
`.details["filename"]`.

If `submit_folder` or `submit_files` fails after creating the draft, the draft is
kept and `.details["observation_id"]` names it.

The library retries temporary failures automatically and verifies uploads using
checksums. Avoid retrying an entire submission, which can create another draft.
Check for an existing observation before starting again.

## Call individual API operations

For more control over submission, use methods such as `create_draft`,
`init_file_upload`, `finalize_draft` and `submit_draft`. Send each file to the
upload target those calls return with `client.upload_to_s3(target, path)`. The
[HTTP API guide](https://iota-hub.com/developers/api) explains the sequence.
Pass an `idempotency_key` when creating or finalizing a draft, and reuse that
key when retrying the same operation to avoid repeating it.

## Related documentation

- [Data submission requirements](https://iota-hub.com/data-submission)
- [API keys](https://iota-hub.com/developers/keys)
- [CLI guide](https://iota-hub.com/developers/cli)
- [HTTP API guide](https://iota-hub.com/developers/api)
- [API reference](https://iota-hub.com/developers/reference)
- [Observer checks](https://iota-hub.com/developers/checks)
- [Source and client conventions](https://github.com/skycarl/iota-hub-python)

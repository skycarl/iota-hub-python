# IOTA Hub Python library guide

Use `iota_hub` to submit observations, manage drafts and download files from
Python.

## Install

Requires Python 3.10 or newer on Windows, macOS or Linux. Install with pip:

    pip install iota-hub

Or add it to a uv project:

    uv add iota-hub

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
another deployment. The library adds `/public/v1` to this address.

To use settings saved by the CLI, call `resolve()`:

```python
from iota_hub import Client, resolve

settings = resolve(profile="dev")
with Client(settings.api_key, settings.base_url) as client:
    observation = client.get_observation("<observation_id>")
```

Omit `profile` to use the default selection. Settings passed to `resolve()`
take precedence over environment variables (`IOTA_HUB_API_KEY`,
`IOTA_HUB_BASE_URL`, `IOTA_HUB_PROFILE`), followed by saved settings and defaults.

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
| `timeout=900` | Set the wait for checks to 900 seconds. The default is 600. Request time is additional. |
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
submission. If the Hub could not select an Occult Watcher Cloud (OWC) event,
`result.owc_commands` lists CLI commands for linking one.

`result.uploads` contains the selected filenames by file type.
`result.attachments` lists attachment filenames, and `result.ignored` lists
files excluded by `attachments=False`.

## Fix and resubmit

Replace the file that needs correcting, then wait for checks before submitting:

```python
obs_id = result.observation_id
client.replace_file(obs_id, "lightcurve", "fixed.csv")

observation = client.wait_for_checks(obs_id)
if observation.readiness and observation.readiness.state == "ready":
    client.submit_draft(obs_id, version=observation.version)
```

Replacing a file automatically starts checks. To request another check run,
call `client.run_checks(obs_id)`.

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

Findings about missing required items cannot be dismissed.

Use `update_draft` to set a reviewer comment, select an OWC event or confirm
an asteroid identifier. Pass the observation's latest `version` so the API
can detect changes made since you read it:

```python
observation = client.get_observation(obs_id)
client.update_draft(obs_id, version=observation.version, comments="Clouds after R.")
```

The API raises `ConflictError` if that version is out of date. See the
[HTTP API guide](https://iota-hub.com/developers/api) for the OWC options.

`submit_draft` succeeds if the observation is already submitted.
`delete_draft(obs_id)` deletes an unsubmitted draft.

## Check for an existing observation

Before repeating a submission, check whether you already have a matching draft
or submitted observation:

```python
answer = client.check_observation(
    asteroid_name="Doty",
    asteroid_id="9721",
    star_id="1234-56789",
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

Use the returned observation ID to continue the draft or update the submission.
Include `station` and `star_catalog` when needed to identify the observation.
An event lookup requires `asteroid_id`.

## Update a submitted observation

Use `replace_file` to correct a file on your submitted observation. Its status
changes to `amended`. A replacement report must describe the same observation;
otherwise, the API rejects it with `identity_changed`.

Once the event enters review, the owner can no longer change its files
(`already_processed`). Contact a coordinator if a correction is needed.

## Read observations and download files

```python
for obs in client.iter_observations(submission_status="draft"):
    print(obs.observation_id, obs.status)

for event in client.iter_events(date_from="2024-01-01", date_to="2024-12-31"):
    print(event.event_id, event.asteroid_name)

observation = client.get_observation(obs_id)
event = client.get_event(event_id)
client.download_files("observation", obs_id, "downloads/")
client.download_files("event", event_id, "downloads/", slot="lightcurve")
```

`iter_observations` and `iter_events` retrieve all pages. The corresponding
`list_observations` and `list_events` methods return one page. Filters use
the parameter names in the [API reference](https://iota-hub.com/developers/reference).

Downloads preserve existing files. Pass `overwrite=True` to replace them.

## Errors

API and connection errors raise `IotaHubError`. Its `.code` identifies the
error, `.message` explains it, and `.hint` suggests what to do next.
Use `.code` in error-handling logic, since message wording can change.

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
the [reference](https://iota-hub.com/developers/reference).

The library retries temporary failures automatically and verifies uploads using
checksums. Avoid retrying an entire submission, which can create another draft.
Check for an existing observation before starting again.

## Call individual API operations

For more control over submission, use methods such as `create_draft`,
`init_file_upload`, `finalize_draft` and `submit_draft`. The
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

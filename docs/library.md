# IOTA Hub Python library guide

The `iota_hub` package, for programs and scripts that submit or read
observations. Also at <https://iota-hub.com/developers/library>. To submit
from a terminal, use the command-line tool instead:
<https://iota-hub.com/developers/cli>.

## Install

    uv add iota-hub                   # in a uv project
    pip install iota-hub              # or with pip

Python 3.10 or newer, on Windows, macOS or Linux. The package also installs
the `iota-hub` command; the library does not need it.

## Connect

Create a key on <https://iota-hub.com/developers> (API keys). Never write it
into source code; read it from the environment or the config file.

```python
import os
from iota_hub import Client

with Client(api_key=os.environ["IOTA_HUB_API_KEY"]) as client:
    ...
```

`Client(api_key, base_url=...)` targets production unless `base_url` names
another deployment. To settle the key and target the way the command-line tool
does (argument, then `IOTA_HUB_API_KEY` / `IOTA_HUB_BASE_URL`, then a stored
profile, then the default), use `resolve()`:

```python
from iota_hub import Client, resolve

settings = resolve(profile="dev")        # or resolve() for the defaults
client = Client(settings.api_key, settings.base_url)
```

A `Client` holds one connection pool: open it once, use it for every call, and
close it (or use `with`).

## Submit an observation

`submit_folder` does the whole job for one observation's folder: it creates
the draft, uploads the files, finalizes, waits for the observer checks and
submits when the draft is `ready`.

```python
result = client.submit_folder("20180305_9721_Doty_Observer_POS")
print(result.outcome, result.observation_id, result.observation.web_url)
```

| Argument                | Effect                                                   |
| ----------------------- | -------------------------------------------------------- |
| `report=`, `lightcurve=`, `log=`, `vizier=`, `aperture_record=` | pick the file for a slot when the folder is ambiguous |
| `attachments=False`     | upload only the slots; the rest come back on `result.ignored` |
| `attach=[path, ...]`    | add attachments from elsewhere                           |
| `submit=False`          | stop at a clean draft (`outcome == "ready"`)             |
| `wait=False`            | return after finalize, without waiting on the checks     |
| `timeout=900`           | seconds to wait for checks (default 600)                 |
| `on_progress=print`     | a callable that receives one progress line at a time     |

When the files are not in one folder, name them yourself:

```python
result = client.submit_files(
    {"report": "obs.xlsx", "lightcurve": "obs.csv", "log": "obs_log.txt"},
    attachments=["plot.png"],
)
```

### Which file goes where

`submit_folder` maps files to slots by extension, with
`iota_hub.map_folder(directory)`; call it yourself to see the mapping without
sending anything.

| Slot              | Rule                                           | Required |
| ----------------- | ---------------------------------------------- | -------- |
| `report`          | exactly one `.xlsx` / `.xls` / `.json`         | yes      |
| `lightcurve`      | exactly one `.csv`                             | yes      |
| `log`             | exactly one `.txt` whose name contains `log`   | yes      |
| `vizier`          | at most one `.dat`                             | no       |
| `aperture_record` | at most one `.lc` / `.pymovie`                 | no       |

Anything else in the folder is an attachment: at most 25, 50 MB each, no
executables or scripts. Two candidates for one slot, or none for a required
one, raises `MappingError` (`code` `ambiguous_files` or `missing_files`)
before any call is made.

## Read the result

`SubmitResult.outcome` is the value to branch on:

| Outcome           | Meaning                                              |
| ----------------- | ---------------------------------------------------- |
| `submitted`       | the observation is submitted (or already was)         |
| `ready`           | clean, but `submit=False` left it a draft             |
| `needs_attention` | open findings, something missing, or the run errored  |
| `draft`           | `wait=False`: uploaded and finalized, not waited on   |

The rest of the result:

- `result.observation`: the observation as the API returned it. `.status` is
  `draft`, `submitted`, `amended` or `locked`; `.web_url` is its page in the
  web app; `.checks.findings` lists the findings.
- `result.skipped_checks`: checks that could not run. They never block
  submit. The usual one is check 11, which needs the Occult Watcher Cloud
  event.
- `result.owc_commands`: when the Hub could not choose the OWC event, the
  ways to settle it.
- `result.uploads`, `result.attachments`, `result.ignored`: what was sent and
  what was left out.

```python
if result.outcome == "needs_attention":
    checks = result.observation.checks
    for finding in checks.findings if checks else []:
        print(finding.fingerprint, finding.message, finding.how_to_fix)
```

## Fix and resubmit

A draft that is not submitted stays in the observer's Drafts. Work on it by id:

```python
obs_id = result.observation_id

client.replace_file(obs_id, "lightcurve", "fixed.csv")   # re-runs the checks
client.delete_file(obs_id, "vizier")                     # clear an optional slot
client.add_attachment(obs_id, "notes.pdf")
client.dismiss_finding(obs_id, fingerprint, note="why this is acceptable")
client.undo_dismissal(obs_id, fingerprint)

observation = client.wait_for_checks(obs_id)
if observation.readiness and observation.readiness.state == "ready":
    client.submit_draft(obs_id)
```

`update_draft(obs_id, version=observation.version, ...)` sets the fields the
report does not own: `owc_link_choice`, `owc_event`,
`confirm_unnumbered_asteroid_id`, `comments`. It takes the `version` you last
read and fails with `ConflictError` if the draft changed since. A finding about
a missing required item cannot be dismissed: supply the item.
`submit_draft` on an observation that is already submitted returns it
unchanged; `delete_draft(obs_id)` throws a draft away.

## Already submitted? Check, and amend

Before creating a draft that an earlier run of your program may have created,
look it up by what it observed:

```python
answer = client.check_observation(
    asteroid_name="Doty", asteroid_id="9721",
    star_id="1234-56789", observed_date="2018-03-05",
)
if answer.found == "observation":
    print("already have", answer.observation.observation_id)
```

`answer.found` is `observation` (your own draft or submission), `event` (not
yours, but the event exists) or `none`.

To correct a submitted observation, `replace_file` on it. Its status becomes
`amended`. A replaced report must describe the same observation, else
`identity_changed`. Once the event is in review every file change fails with
`already_processed`; ask a coordinator.

## Reads and downloads

```python
for obs in client.iter_observations(submission_status="draft"):
    print(obs.observation_id, obs.status)

for event in client.iter_events(date_from="2024-01-01", date_to="2024-12-31"):
    print(event.event_id, event.asteroid_name)

client.get_observation(obs_id)
client.get_event(event_id)
client.download_files("observation", obs_id, "downloads/")
client.download_files("event", event_id, "downloads/", slot="lightcurve")
```

`iter_*` follow the pages for you; `list_*` return one page. Filters are the
API's query parameters as keyword arguments. `download_files` never
overwrites an existing file unless `overwrite=True`.

## Errors

Every failure raises `IotaHubError`, carrying `.code`, `.message`, `.hint`,
`.details`, `.status` and `.retry_after`. Branch on `.code`, never on
`.message`. The subclasses group codes so you can catch broadly:

| Class            | When                                             |
| ---------------- | ------------------------------------------------ |
| `AuthError`      | the key is missing, bad, expired, or not allowed |
| `NotFoundError`  | no such resource, or not yours (never a `403`)   |
| `ConflictError`  | a `409`: stale `version`, already processed, ... |
| `RateLimitError` | a `429` the retries could not wait out           |
| `RequestError`   | a `400` / `422`: the request was rejected        |
| `TransportError` | no response at all (DNS, connect, read, TLS)     |

The codes for each operation are in the reference at
<https://iota-hub.com/developers/reference>.

Retries (`429` and `5xx`, honouring `Retry-After`), one idempotency key per
logical call and the checksum on every upload are handled for you. Do not
wrap a call in your own retry loop. Models ignore fields they do not know, so
an additive API change never breaks an installed version.

## Two layers

The **transport** methods are one per API operation (`create_draft`,
`init_file_upload`, `finalize_draft`, `submit_draft`, `list_events`, ...),
for when the workflow methods do not fit. The **workflow** methods
(`submit_folder`, `submit_files`, `wait_for_checks`, `replace_file`,
`add_attachment`, `download_files`) are built on them. The HTTP calls behind
both are at <https://iota-hub.com/developers/api>.

## Where the rest lives

- What belongs in each file: <https://iota-hub.com/data-submission>
- API keys: <https://iota-hub.com/developers>
- The command-line tool: <https://iota-hub.com/developers/cli>
- The HTTP API: <https://iota-hub.com/developers/api>, the reference at
  `/developers/reference`
- Source and client conventions: <https://github.com/skycarl/iota-hub-python>

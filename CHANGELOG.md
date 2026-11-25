# Changelog

## 1.2.0 - 2026-11-25

- `report_details.events` lists every event of a Tangra JSON report in report
  order: a double star's second star, or each drop of a double asteroid.
  `events[0]` is the same event as the top-level `d_corrected` /
  `r_corrected` / `d_err` / `r_err`. Each has `d_corrected`, `r_corrected`,
  `d_err`, `r_err`, `snr` and `second_star`. It is `null` for spreadsheet
  reports, misses and older observations.
- `report_details` also carries the rest of a Tangra JSON report:
  `double_stars`, `light_curve`, `prediction`, `recording`, `submission`,
  `timing_detail`, `camera_model_for_delays`, `acknowledged_messages`,
  `archive_comment` and `observers_more_than_listed`. `report_site` adds
  `station_latitude`, `station_longitude` and `station_altitude`.
  `readiness.submit_in_progress`, `owc.resolution.report_check` and
  `owc.resolution.report_owc_event_id` are new too. All of these appear in
  `--json` output and as library model fields.
- `Client.key_exchange`, `Client.key_exchange_sign_in_start` and
  `Client.key_exchange_sign_in_collect` let an application trade a user's
  OccultWatcher API key for a Hub API key. They send no Hub key. The CLI
  does not expose them yet.
- `spec/openapi.json` is re-vendored from production.

## 1.1.0 - 2026-11-25

For the Hub's File names check (observer check 13), which lists a file whose
name is not the standard
`YYYYMMDD_<number>_<Name>_<LastName>[_<N>]_<POS|NEG|UNS>` plus its suffix.
On a draft created through the API this finding does not block submission.

- Findings the Hub lists but does not count toward submission are labelled
  `does not block submission` in `submit`, `drafts show`, `drafts check`,
  `drafts dismiss` and the other commands that print findings, and in the
  finding counts. `submit` shows them even when the outcome is `ready` or
  `submitted`. They get no `drafts dismiss` command under `Next:`. Readiness,
  outcomes and exit codes still follow the Hub's `readiness` and
  `checks.open_findings`.
- `submit --json` adds `advisory_findings`, a list of those findings.
  `SubmitResult.advisory_findings` and `iota_hub.advisory_findings(checks)`
  give the same list in the library.
- The README, `iota-hub guide`, SKILL.md and the library guide describe the
  standard file names and say that a report named `..._<Surname>_2_POS.xlsx` or
  `..._POS-2.xlsx` needs `--station 2` (`station="2"`) in `observations check`.
- The multi-station hint of `ambiguous_files` names the standard station form
  (`_Surname_1_POS`).

## 1.0.0

The first release for the production IOTA Hub public API. The default API
address, `https://api.iota-hub.com`, needs no `--base-url`.

From this release, removing a command or flag or changing a `--json` output
structure requires a major version.

Changes since 0.2.2:

- Observations carry `report_details` (equipment, conditions and timing from
  the report) and `report_site` (the observing site, for the observer's own
  observations) in the library and in `--json` output.
- A profile's key is used only with the API address saved beside it. A
  different `--base-url` or `IOTA_HUB_BASE_URL` returns `invalid_config`
  instead of sending the key there.
- `--base-url` accepts an address pasted with a trailing `/public/v1`.
- The API's `validation_error` exits with code `2`.
- Choosing a file for a slot with `--report`, `--lightcurve` and the other
  slot flags leaves the folder's other candidates for that slot unsent. They
  are no longer uploaded as attachments.
- Prompts are written to stderr, so stdout holds only the result.
- Network failures include a hint.
- Uploads stream each file from disk instead of reading it into memory, so a
  500 MB aperture record no longer needs 500 MB of memory.
- A file that cannot be read during an upload returns `missing_files` with its
  slot and filename, and with `observation_id` when `submit` already created
  the draft, instead of a traceback. It is not retried on a fresh target.
- Any other unexpected failure in a command returns `unexpected_error` with
  exit code `1`, as one JSON error under `--json`, instead of a traceback.
- An API key is stripped of surrounding whitespace, and a key that cannot be
  sent as a header returns `invalid_config`. A key ending in a newline used to
  appear in full in the network error.
- `Client.upload_to_s3` sends a file to an upload target, and the helpers
  `skipped_checks`, `owc_unresolved`, `owc_commands` and `check_warnings` are
  importable from `iota_hub`.
- `submit_folder` names its `wait`, `submit`, `timeout`, `on_progress` and
  `auto_submit_when_clean` arguments.
- Library names changed before the 1.0 freeze: `Settings.profile_name` is now
  `Settings.profile` and `Settings.source_of_key` is `Settings.key_source`,
  matching `auth status --json`. `SubmitResult.ignored` and
  `SubmitResult.symlinks` hold filenames, like `uploads` and `attachments`.
  The second argument of `download_files` is named `resource_id`.
- The config file's location no longer depends on the installed
  `platformdirs` version, and the package no longer depends on `platformdirs`.
  On macOS, an absolute `XDG_CONFIG_HOME` places the file at
  `$XDG_CONFIG_HOME/iota-hub/config.toml`, as `platformdirs` 4.6 and later
  already did. With an older `platformdirs` and `XDG_CONFIG_HOME` set, 0.2.x
  read `~/Library/Application Support/iota-hub/config.toml` instead; move the
  file, or unset `XDG_CONFIG_HOME`, to keep using it. Windows reads
  `%LOCALAPPDATA%` and no longer honours `platformdirs`' `WIN_PD_OVERRIDE_*`
  variables.
- The package declares `rich` as a dependency and ships `py.typed`.
- Python 3.14 is supported.

Earlier releases are described by their
[tags](https://github.com/skycarl/iota-hub-python/tags).

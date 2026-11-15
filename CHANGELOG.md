# Changelog

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
- An API key is stripped of surrounding whitespace, and a key that cannot be
  sent as a header returns `invalid_config`. A key ending in a newline used to
  appear in full in the network error.
- `Client.upload_to_s3` sends a file to an upload target, and the helpers
  `skipped_checks`, `owc_unresolved`, `owc_commands` and `check_warnings` are
  importable from `iota_hub`.
- `submit_folder` names its `wait`, `submit`, `timeout`, `on_progress` and
  `auto_submit_when_clean` arguments.
- The package declares `rich` as a dependency and ships `py.typed`.
- Python 3.14 is supported.

Earlier releases are described by their
[tags](https://github.com/skycarl/iota-hub-python/tags).

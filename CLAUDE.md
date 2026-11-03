# CLAUDE.md

Guidance for working in this repository.

## Project

`iota-hub` provides a Python library (`from iota_hub import Client`) and CLI
(`iota-hub`) for the IOTA Hub public API v1. Prioritize the CLI for observers
and their agents, followed by the library for custom scripts. Direct HTTP
access is available for integrations that need it. When adding a library
feature, consider how CLI users will access it.

The API lives in a separate private repository. This client is built and
versioned independently.

## Behavior specifications

Keep these documents current when behavior changes:

- **`spec/openapi.json`** defines the API's operations and schemas. It is copied
  from the deployed API. `scripts/check_openapi_drift.py` compares it with the
  live document in CI and fails if they differ. Use `--update` to refresh the
  local copy, then review the changes before committing.
- **`docs/client-conventions.md`** defines client behavior: configuration,
  environment variables, retries, polling, exit codes and User-Agent format.
  Read it before changing CLI or library behavior and update it in the same
  change. Clients in other languages must follow the same rules.

If this repository disagrees with the API specification, follow the API
specification and file an issue in the API repository. See Issues below.

## Architecture

Keep these layers separate:

1. **Transport**: one method per public `operation_id`, with hand-written
   Pydantic models using `extra="ignore"` to tolerate new API fields. Do not
   put workflow logic here.
2. **Workflow**: submission, polling, downloads, retry and idempotency policies,
   file selection and error handling. These methods are hand-written. Document
   their behavior in `docs/client-conventions.md` so other clients can implement
   it consistently.

## Layout

```text
src/iota_hub/      Library and CLI
tests/            Unit and contract tests; smoke tests excluded by default
tests/smoke/      Live development API tests; require a key and base URL
spec/openapi.json API document copied from the deployed API
docs/            Library guide and client conventions
```

## Commands

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format .
uv build

# Refresh the API document after a public API change.
uv run python scripts/check_openapi_drift.py --update
```

To run smoke tests, set `IOTA_HUB_API_KEY` and `IOTA_HUB_BASE_URL` for a live
development account, then run:

```bash
uv run pytest -m smoke tests/smoke -q
```

Smoke tests are excluded by default and must not run on fork pull requests.

## Compatibility and credentials

- API models must ignore unknown fields (`extra="ignore"`). New API fields
  must not break an installed client.
- Use error `code` and CLI exit codes to determine what happened. Do not parse
  message text.
- Never accept an API key as a CLI argument. Use `IOTA_HUB_API_KEY`, stdin or
  a stored profile to keep keys out of shell history and process listings.
- Never log or print a secret key, including in JSON output and error details.
- Support Windows, macOS and Linux. Tests run on `windows-latest`. Avoid
  POSIX-only path assumptions and shell-specific user commands. Default output
  must work on non-UTF-8 Windows consoles without decorative Unicode.

## Releases

Create and push a `vX.Y.Z` tag matching `pyproject.toml`. `release.yml` checks
that they match, builds the package and publishes to PyPI through Trusted
Publishing, without a stored token.

The publish job uses the `pypi` GitHub environment. Only `v*` tags may deploy
there, and deployment requires maintainer approval. Pin Actions to commit SHAs,
with the tag in a trailing comment. To update an Action, resolve the new tag's
SHA; do not replace a SHA with a tag or branch.

The PyPI project is registered, but the public API's production release is
pending. Until then, the default base URL (`https://api.iota-hub.com`) does not
serve `/public/v1`.

Use semantic versioning and remain at `0.x` until the API reaches production.
Removing a command or flag, or changing a `--json` output structure, requires a
major version bump. New commands, optional flags and JSON fields are additive
changes.

## Code changes

Prefer straightforward implementations. Add only what the current task needs.
Wait for three uses before extracting a shared abstraction.

## Issues

File GitHub issues for problems outside the current task. Do not fix them as
part of an unrelated change or leave them only in the final summary.

Client issues belong in this repository. API issues belong in the private API
repository with the `drafts-and-api` label. Never name or link that private
repository from this public one.
